#!/usr/bin/env python3
"""
Minimal Docker Engine API client over the unix socket -- stdlib only.

Deliberately NOT docker-py: this project already learned the hard way
(pyserial) that heavyweight dependencies can behave surprisingly on this
platform, and the self-updater only needs a handful of Engine API calls
(inspect / pull / create / start / stop / rename / remove / exec). Talking
HTTP over /var/run/docker.sock with http.client keeps the image slim and
the behavior fully visible.

Used by BOTH:
  - app/server.py            (/api/update_app: pull image, launch helper)
  - app/self_update_helper.py (the actual stop-old/start-new swap, run
                               inside the short-lived helper container)

API version: pinned to v1.41 (Docker Engine 20.10+, well below anything a
current ZimaOS ships) so behavior doesn't shift under us.
"""

import http.client
import json
import socket


DEFAULT_SOCKET = "/var/run/docker.sock"
API_PREFIX = "/v1.41"


class DockerAPIError(Exception):
    """Raised for any non-2xx Engine API response, with status + body."""

    def __init__(self, status, message):
        self.status = status
        super().__init__(f"Docker API {status}: {message}")


class _UnixHTTPConnection(http.client.HTTPConnection):
    """http.client.HTTPConnection that dials an AF_UNIX socket instead of
    TCP. The 'host' passed to the parent is only used for the Host:
    header, which the Docker daemon ignores.
    """

    def __init__(self, socket_path, timeout):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


class DockerClient:
    def __init__(self, socket_path=DEFAULT_SOCKET, timeout=30):
        self.socket_path = socket_path
        self.timeout = timeout

    # -- low-level ----------------------------------------------------

    def _request(self, method, path, body=None, timeout=None, stream_ok=False):
        """One request/response cycle. Returns parsed JSON (or None for
        empty 2xx bodies). For streaming endpoints (image pull), pass
        stream_ok=True: the whole stream is read to completion and
        returned as a list of parsed JSON-lines objects.
        """
        conn = _UnixHTTPConnection(self.socket_path, timeout or self.timeout)
        try:
            headers = {}
            payload = None
            if body is not None:
                payload = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
            conn.request(method, API_PREFIX + path, body=payload, headers=headers)
            resp = conn.getresponse()
            data = resp.read()  # reads chunked streams to EOF too
            if resp.status < 200 or resp.status >= 300:
                try:
                    msg = json.loads(data.decode("utf-8", "replace")).get("message", "")
                except (json.JSONDecodeError, AttributeError):
                    msg = data.decode("utf-8", "replace")[:500]
                raise DockerAPIError(resp.status, msg)
            if not data:
                return None
            text = data.decode("utf-8", "replace")
            if stream_ok:
                out = []
                for line in text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass  # progress noise; ignore unparseable lines
                return out
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        finally:
            conn.close()

    # -- the calls the updater actually needs --------------------------

    def ping(self):
        # /_ping lives OUTSIDE the version prefix; cheat with a relative path
        conn = _UnixHTTPConnection(self.socket_path, self.timeout)
        try:
            conn.request("GET", "/_ping")
            resp = conn.getresponse()
            resp.read()
            return resp.status == 200
        finally:
            conn.close()

    def inspect_container(self, id_or_name):
        return self._request("GET", f"/containers/{id_or_name}/json")

    def list_containers(self, all=True):
        return self._request("GET", f"/containers/json?all={'true' if all else 'false'}")

    def pull_image(self, image_ref):
        """Pull an image, blocking until the stream completes. image_ref
        may include a tag ('user/repo:latest'); default tag is latest.
        Raises DockerAPIError if the daemon reports an error in-stream
        (e.g. registry unreachable), since the HTTP status alone is 200
        even for failed pulls.
        """
        if ":" in image_ref.rsplit("/", 1)[-1]:
            from_image, tag = image_ref.rsplit(":", 1)
        else:
            from_image, tag = image_ref, "latest"
        # Pulls can be slow on a ZimaBlade's uplink; give this call a
        # much longer leash than the default.
        events = self._request(
            "POST", f"/images/create?fromImage={from_image}&tag={tag}",
            timeout=600, stream_ok=True,
        )
        for ev in events or []:
            if isinstance(ev, dict) and "error" in ev:
                raise DockerAPIError(200, f"pull failed: {ev['error']}")
        return events

    def inspect_image(self, image_ref):
        return self._request("GET", f"/images/{image_ref}/json")

    def create_container(self, config, name=None):
        path = "/containers/create"
        if name:
            path += f"?name={name}"
        return self._request("POST", path, body=config)

    def start_container(self, id_or_name):
        return self._request("POST", f"/containers/{id_or_name}/start")

    def stop_container(self, id_or_name, timeout_s=10):
        # stop waits for the container to exit; pad the HTTP timeout past
        # the daemon-side kill timeout so we don't give up first.
        return self._request(
            "POST", f"/containers/{id_or_name}/stop?t={timeout_s}",
            timeout=timeout_s + 20,
        )

    def rename_container(self, id_or_name, new_name):
        return self._request("POST", f"/containers/{id_or_name}/rename?name={new_name}")

    def remove_container(self, id_or_name, force=False):
        return self._request(
            "DELETE", f"/containers/{id_or_name}?force={'true' if force else 'false'}"
        )

    def exec_run(self, container_id, cmd, timeout=15):
        """Run a command in a container, wait for it, return its exit
        code (or None if the exec couldn't be inspected). Output is read
        and discarded -- the updater only cares about pass/fail.
        """
        created = self._request(
            "POST", f"/containers/{container_id}/exec",
            body={"AttachStdout": True, "AttachStderr": True, "Cmd": cmd},
        )
        exec_id = created["Id"]
        # Detach=false streams output until the command exits; reading the
        # body to EOF (which _request does) doubles as "wait for exit".
        self._request(
            "POST", f"/exec/{exec_id}/start",
            body={"Detach": False, "Tty": False},
            timeout=timeout, stream_ok=True,
        )
        info = self._request("GET", f"/exec/{exec_id}/json")
        return info.get("ExitCode")
