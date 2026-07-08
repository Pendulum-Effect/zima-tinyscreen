#!/usr/bin/env python3
"""
Stage 2 self-updater tests, run against a FAKE Docker Engine API daemon
listening on a real unix socket -- same philosophy as the pty-pair serial
tests: exercise the actual wire protocol (HTTP over AF_UNIX, chunked
streams, real JSON bodies), not mocked-out method calls. Network is
disabled in the sandbox and there's no real dockerd, so this is the
highest-fidelity check available before real-hardware testing.

Covers:
  1. DockerClient basics against the fake daemon
  2. Helper happy path: validate -> stop -> rename -> create -> start ->
     health -> remove old; new container inherits HostConfig/Env/Labels
  3. Helper refusal on canonical-config mismatch (old container untouched)
  4. Helper rollback when the new container never becomes healthy
  5. server.py: /api/update_app spawns the helper with the right spec;
     /api/update_state; /api/app_version version fields (GitHub mocked)
"""

import copy
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import UnixStreamServer

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from dockerapi import DockerClient, DockerAPIError  # noqa: E402


# ---------------------------------------------------------------------
# Fake Docker Engine API daemon
# ---------------------------------------------------------------------

class FakeDockerState:
    """In-memory container/image store, mimicking just enough dockerd."""

    def __init__(self):
        self.containers = {}   # id -> inspect-shaped dict
        self.images = {}       # ref -> {"Id": ...}
        self.execs = {}        # exec id -> {"ExitCode": int}
        self.pull_should_fail = False
        self.health_exit_code = 0     # what exec health probes return
        self.start_should_fail_for = set()  # container ids
        self.fail_start_for_new = False     # every container created from now on
        self.events = []       # (verb, subject) audit log
        self.lock = threading.Lock()
        self._counter = 0

    def new_id(self):
        self._counter += 1
        return f"{self._counter:02d}" + "ab" * 31  # 64 hex chars

    def add_container(self, name, image="pendulumeffect/tinyscreen-dashboard:latest",
                      running=True, mounts=None, env=None, ports=None, labels=None):
        cid = self.new_id()
        mounts = mounts if mounts is not None else [
            {"Destination": d, "Source": "/DATA/x" + d.replace("/", "_"), "Type": "bind"}
            for d in ["/dev", "/host_net_dev", "/host_data",
                      "/opt/tinyscreen/certs", "/opt/tinyscreen/state",
                      "/var/run/docker.sock"]
        ]
        env = env if env is not None else [
            "TINYSCREEN_HOST_NET_DEV=/host_net_dev",
            "TINYSCREEN_DATA_PATH=/host_data",
            "TINYSCREEN_STATUS_FILE=/tmp/tinyscreen_status.json",
        ]
        ports = ports if ports is not None else {
            "8989/tcp": [{"HostPort": "8989"}], "8990/tcp": [{"HostPort": "8990"}]}
        self.containers[cid] = {
            "Id": cid,
            "Name": "/" + name,
            "State": {"Running": running},
            "Config": {
                "Image": image,
                "Env": env,
                "Labels": labels or {"com.docker.compose.project": "tinyscreen-dashboard"},
                "ExposedPorts": {k: {} for k in ports},
            },
            "HostConfig": {
                "Binds": [f"{m['Source']}:{m['Destination']}" for m in mounts],
                "PortBindings": ports,
                "Privileged": True,
                "RestartPolicy": {"Name": "unless-stopped"},
                "AutoRemove": False,
            },
            "Mounts": mounts,
            "NetworkSettings": {"Networks": {"tinyscreen-dashboard_default": {}}},
        }
        return cid

    def resolve(self, ref):
        ref = ref.strip("/")
        if ref in self.containers:
            return ref
        for cid, c in self.containers.items():
            if c["Name"].lstrip("/") == ref or cid.startswith(ref):
                return cid
        return None


class FakeDockerHandler(BaseHTTPRequestHandler):
    state = None  # injected

    def log_message(self, *a):
        pass

    def _json(self, code, obj=None):
        body = json.dumps(obj or {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        st = self.state
        if self.path == "/_ping":
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"OK")
            return
        m = re.match(r"/v1\.\d+/containers/([^/]+)/json", self.path)
        if m:
            cid = st.resolve(m.group(1))
            if not cid:
                return self._json(404, {"message": "No such container"})
            return self._json(200, st.containers[cid])
        m = re.match(r"/v1\.\d+/exec/([^/]+)/json", self.path)
        if m:
            info = st.execs.get(m.group(1))
            if not info:
                return self._json(404, {"message": "No such exec"})
            return self._json(200, info)
        m = re.match(r"/v1\.\d+/images/(.+)/json", self.path)
        if m:
            ref = m.group(1)
            if ref in st.images:
                return self._json(200, st.images[ref])
            return self._json(404, {"message": "No such image"})
        if re.match(r"/v1\.\d+/containers/json", self.path):
            return self._json(200, list(st.containers.values()))
        self._json(404, {"message": f"fake daemon: unhandled GET {self.path}"})

    def do_POST(self):
        st = self.state
        body = self._read_body()

        m = re.match(r"/v1\.\d+/images/create\?fromImage=([^&]+)&tag=(.+)", self.path)
        if m:
            with st.lock:
                st.events.append(("pull", f"{m.group(1)}:{m.group(2)}"))
            if st.pull_should_fail:
                # dockerd reports pull failures IN-STREAM with HTTP 200
                payload = b'{"status":"Pulling"}\n{"error":"registry unreachable"}\n'
            else:
                st.images[f"{m.group(1)}:{m.group(2)}"] = {"Id": "sha256:" + "f" * 64}
                payload = b'{"status":"Pulling"}\n{"status":"Download complete"}\n'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        m = re.match(r"/v1\.\d+/containers/create(?:\?name=(.+))?$", self.path)
        if m:
            name = m.group(1) or "unnamed"
            if st.resolve(name) and st.containers[st.resolve(name)]["Name"].lstrip("/") == name:
                return self._json(409, {"message": f"name {name} already in use"})
            cid = st.new_id()
            mounts = []
            for b in (body.get("HostConfig", {}).get("Binds") or []):
                src, dest = b.split(":", 1)[0], b.split(":", 1)[1].split(":")[0]
                mounts.append({"Source": src, "Destination": dest, "Type": "bind"})
            st.containers[cid] = {
                "Id": cid, "Name": "/" + name,
                "State": {"Running": False},
                "Config": {
                    "Image": body.get("Image"),
                    "Env": body.get("Env") or [],
                    "Labels": body.get("Labels") or {},
                    "ExposedPorts": body.get("ExposedPorts") or {},
                    "Entrypoint": body.get("Entrypoint"),
                },
                "HostConfig": body.get("HostConfig") or {},
                "Mounts": mounts,
                "NetworkSettings": {"Networks": {}},
            }
            if st.fail_start_for_new:
                st.start_should_fail_for.add(cid)
            with st.lock:
                st.events.append(("create", name))
            return self._json(201, {"Id": cid, "Warnings": []})

        m = re.match(r"/v1\.\d+/containers/([^/]+)/start", self.path)
        if m:
            cid = st.resolve(m.group(1))
            if not cid:
                return self._json(404, {"message": "No such container"})
            if cid in st.start_should_fail_for:
                return self._json(500, {"message": "simulated start failure"})
            st.containers[cid]["State"]["Running"] = True
            with st.lock:
                st.events.append(("start", st.containers[cid]["Name"].lstrip("/")))
            return self._json(204)

        m = re.match(r"/v1\.\d+/containers/([^/]+)/stop", self.path)
        if m:
            cid = st.resolve(m.group(1))
            if not cid:
                return self._json(404, {"message": "No such container"})
            st.containers[cid]["State"]["Running"] = False
            with st.lock:
                st.events.append(("stop", st.containers[cid]["Name"].lstrip("/")))
            return self._json(204)

        m = re.match(r"/v1\.\d+/containers/([^/]+)/rename\?name=(.+)", self.path)
        if m:
            cid = st.resolve(m.group(1))
            if not cid:
                return self._json(404, {"message": "No such container"})
            with st.lock:
                st.events.append(("rename", f"{st.containers[cid]['Name'].lstrip('/')}"
                                            f"->{m.group(2)}"))
            st.containers[cid]["Name"] = "/" + m.group(2)
            return self._json(204)

        m = re.match(r"/v1\.\d+/containers/([^/]+)/exec", self.path)
        if m:
            eid = "exec" + st.new_id()[:8]
            st.execs[eid] = {"ExitCode": st.health_exit_code}
            return self._json(201, {"Id": eid})

        m = re.match(r"/v1\.\d+/exec/([^/]+)/start", self.path)
        if m:
            payload = b'{"status":"done"}\n'
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self._json(404, {"message": f"fake daemon: unhandled POST {self.path}"})

    def do_DELETE(self):
        st = self.state
        m = re.match(r"/v1\.\d+/containers/([^/?]+)", self.path)
        if m:
            cid = st.resolve(m.group(1))
            if not cid:
                return self._json(404, {"message": "No such container"})
            name = st.containers[cid]["Name"].lstrip("/")
            del st.containers[cid]
            with st.lock:
                st.events.append(("remove", name))
            return self._json(204)
        self._json(404, {"message": "unhandled DELETE"})


class UnixHTTPServer(UnixStreamServer, HTTPServer):
    def server_bind(self):
        UnixStreamServer.server_bind(self)


def start_fake_daemon(sock_path):
    state = FakeDockerState()
    handler = type("H", (FakeDockerHandler,), {"state": state})
    srv = UnixHTTPServer(sock_path, handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, state


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------

class UpdaterTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sock_path = os.path.join(self.tmp.name, "docker.sock")
        self.srv, self.state = start_fake_daemon(self.sock_path)
        self.client = DockerClient(self.sock_path, timeout=5)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()


class TestDockerClient(UpdaterTestBase):
    def test_ping_inspect_and_errors(self):
        self.assertTrue(self.client.ping())
        cid = self.state.add_container("tinyscreen-dashboard")
        info = self.client.inspect_container("tinyscreen-dashboard")
        self.assertEqual(info["Id"], cid)
        with self.assertRaises(DockerAPIError):
            self.client.inspect_container("nope")

    def test_pull_success_and_instream_failure(self):
        self.client.pull_image("pendulumeffect/tinyscreen-dashboard:latest")
        self.assertIn("pendulumeffect/tinyscreen-dashboard:latest", self.state.images)
        self.state.pull_should_fail = True
        with self.assertRaises(DockerAPIError):
            self.client.pull_image("pendulumeffect/tinyscreen-dashboard:latest")

    def test_lifecycle_calls(self):
        cid = self.state.add_container("x")
        self.client.stop_container(cid)
        self.assertFalse(self.state.containers[cid]["State"]["Running"])
        self.client.rename_container(cid, "x-old")
        self.assertEqual(self.state.containers[cid]["Name"], "/x-old")
        self.client.remove_container(cid, force=True)
        self.assertNotIn(cid, self.state.containers)


class HelperRunner:
    """Run self_update_helper.main() in-process with env/paths pointed at
    the fake daemon and a temp state file."""

    def __init__(self, sock_path, tmpdir):
        self.state_file = Path(tmpdir) / "update_state.json"
        self.sock_path = sock_path

    def run(self, target_id, image):
        import importlib
        import dockerapi
        env_backup = dict(os.environ)
        os.environ["TS_TARGET_CONTAINER"] = target_id
        os.environ["TS_TARGET_IMAGE"] = image
        os.environ["TS_STATE_FILE"] = str(self.state_file)
        # helper's DockerClient() default socket -> point at the fake
        orig_default = dockerapi.DEFAULT_SOCKET
        dockerapi.DEFAULT_SOCKET = self.sock_path
        try:
            if "self_update_helper" in sys.modules:
                del sys.modules["self_update_helper"]
            helper = importlib.import_module("self_update_helper")
            helper.HEALTH_TIMEOUT_S = 6  # keep failure tests fast
            # DockerClient() reads DEFAULT_SOCKET at call time via default
            # arg... it does NOT (default args bind at def time). Patch the
            # client class default by wrapping:
            orig_client = helper.DockerClient
            helper.DockerClient = lambda: orig_client(self.sock_path, timeout=5)
            rc = helper.main()
            return rc, json.loads(self.state_file.read_text())
        finally:
            dockerapi.DEFAULT_SOCKET = orig_default
            os.environ.clear()
            os.environ.update(env_backup)


class TestHelperSwap(UpdaterTestBase):
    IMAGE = "pendulumeffect/tinyscreen-dashboard:latest"

    def test_happy_path(self):
        old_id = self.state.add_container("tinyscreen-dashboard")
        old_hostconfig = copy.deepcopy(self.state.containers[old_id]["HostConfig"])
        old_env = list(self.state.containers[old_id]["Config"]["Env"])
        old_labels = dict(self.state.containers[old_id]["Config"]["Labels"])

        rc, st = HelperRunner(self.sock_path, self.tmp.name).run(old_id, self.IMAGE)
        self.assertEqual(rc, 0)
        self.assertEqual(st["status"], "success")

        # old container gone; exactly one container left, named correctly
        self.assertNotIn(old_id, self.state.containers)
        names = [c["Name"] for c in self.state.containers.values()]
        self.assertEqual(names, ["/tinyscreen-dashboard"])
        new = list(self.state.containers.values())[0]
        # deployment config carried over verbatim
        self.assertEqual(new["HostConfig"], old_hostconfig)
        self.assertEqual(new["Config"]["Env"], old_env)
        self.assertEqual(new["Config"]["Labels"], old_labels)
        # entrypoint NOT inherited -- new image defaults must apply
        self.assertIsNone(new["Config"]["Entrypoint"])
        # order sanity: stop old before creating new, remove old last
        verbs = [v for v, _ in self.state.events]
        self.assertLess(verbs.index("stop"), verbs.index("create"))
        self.assertEqual(verbs[-1], "remove")

    def test_refusal_on_config_mismatch(self):
        # Deployment missing the docker.sock + state mounts (i.e. a
        # pre-Stage-2 install)
        mounts = [{"Destination": d, "Source": "/DATA/x", "Type": "bind"}
                  for d in ["/dev", "/host_net_dev", "/host_data",
                            "/opt/tinyscreen/certs"]]
        old_id = self.state.add_container("tinyscreen-dashboard", mounts=mounts)
        rc, st = HelperRunner(self.sock_path, self.tmp.name).run(old_id, self.IMAGE)
        self.assertEqual(rc, 0)
        self.assertEqual(st["status"], "refused")
        self.assertIn("/opt/tinyscreen/state", st["reason"])
        self.assertIn("reinstall", st["reason"])
        # old container completely untouched and still running
        self.assertIn(old_id, self.state.containers)
        self.assertTrue(self.state.containers[old_id]["State"]["Running"])
        self.assertEqual(self.state.events, [])  # no lifecycle calls at all

    def test_rollback_on_unhealthy_new_container(self):
        old_id = self.state.add_container("tinyscreen-dashboard")
        self.state.health_exit_code = 1  # new container's probe never passes
        rc, st = HelperRunner(self.sock_path, self.tmp.name).run(old_id, self.IMAGE)
        self.assertEqual(rc, 1)
        self.assertEqual(st["status"], "rolled_back")
        self.assertIn("never became healthy", st["reason"])
        # old container restored: right name, running, new one removed
        self.assertIn(old_id, self.state.containers)
        old = self.state.containers[old_id]
        self.assertEqual(old["Name"], "/tinyscreen-dashboard")
        self.assertTrue(old["State"]["Running"])
        self.assertEqual(len(self.state.containers), 1)

    def test_rollback_on_start_failure(self):
        old_id = self.state.add_container("tinyscreen-dashboard")
        self.state.fail_start_for_new = True  # new container's start blows up
        rc, st = HelperRunner(self.sock_path, self.tmp.name).run(old_id, self.IMAGE)
        self.assertEqual(rc, 1)
        self.assertEqual(st["status"], "rolled_back")
        self.assertIn(old_id, self.state.containers)
        self.assertEqual(self.state.containers[old_id]["Name"], "/tinyscreen-dashboard")
        self.assertTrue(self.state.containers[old_id]["State"]["Running"])


class TestServerEndpoints(UpdaterTestBase):
    def setUp(self):
        super().setUp()
        os.environ["TINYSCREEN_DOCKER_SOCK"] = self.sock_path
        self.state_dir = tempfile.TemporaryDirectory()
        os.environ["TINYSCREEN_STATE_DIR"] = self.state_dir.name
        for mod in ["server"]:
            if mod in sys.modules:
                del sys.modules[mod]
        import server
        self.server = server
        # point module-level constants at the fakes (env was read at import
        # in a prior test run's module instance; fresh import above reads
        # the env we just set, but be explicit anyway)
        server.DOCKER_SOCK = self.sock_path
        server.STATE_DIR = Path(self.state_dir.name)
        server.UPDATE_STATE_FILE = Path(self.state_dir.name) / "update_state.json"
        self.app = server.app.test_client()

    def tearDown(self):
        self.state_dir.cleanup()
        os.environ.pop("TINYSCREEN_DOCKER_SOCK", None)
        os.environ.pop("TINYSCREEN_STATE_DIR", None)
        super().tearDown()

    def test_update_app_spawns_helper_with_correct_spec(self):
        own_id = self.state.add_container("tinyscreen-dashboard")
        # make _detect_own_container_id resolve via the name fallback
        # (mountinfo/hostname won't match in the sandbox)
        r = self.app.post("/api/update_app")
        data = r.get_json()
        self.assertTrue(data["ok"], data)

        # wait for the background pull+spawn thread
        deadline = time.time() + 10
        helper = None
        while time.time() < deadline and helper is None:
            for c in self.state.containers.values():
                if c["Name"] == "/tinyscreen-updater":
                    helper = c
            time.sleep(0.1)
        self.assertIsNotNone(helper, "helper container never created")
        self.assertEqual(helper["Config"]["Entrypoint"],
                         ["python3", "/opt/tinyscreen/app/self_update_helper.py"])
        env = dict(e.split("=", 1) for e in helper["Config"]["Env"])
        self.assertEqual(env["TS_TARGET_CONTAINER"], own_id)
        self.assertEqual(env["TS_TARGET_IMAGE"],
                         "pendulumeffect/tinyscreen-dashboard:latest")
        self.assertTrue(helper["HostConfig"]["AutoRemove"])
        binds = helper["HostConfig"]["Binds"]
        self.assertIn(f"{self.sock_path}:/var/run/docker.sock", binds)
        # state dir bind reuses the OLD container's host path for that dest
        state_src = next(m["Source"] for m in
                         self.state.containers[own_id]["Mounts"]
                         if m["Destination"] == "/opt/tinyscreen/state")
        self.assertIn(f"{state_src}:/opt/tinyscreen/state", binds)
        self.assertTrue(helper["State"]["Running"])
        # image was pulled before the helper was created
        verbs = [v for v, _ in self.state.events]
        self.assertLess(verbs.index("pull"), verbs.index("create"))
        # state file progressed to swapping
        st = json.loads((Path(self.state_dir.name) / "update_state.json").read_text())
        self.assertEqual(st["status"], "swapping")

    def test_update_app_no_socket(self):
        self.server.DOCKER_SOCK = "/nonexistent/docker.sock"
        r = self.app.post("/api/update_app")
        self.assertEqual(r.status_code, 400)
        self.assertIn("reinstall", r.get_json()["error"])

    def test_update_app_pull_failure_recorded(self):
        self.state.add_container("tinyscreen-dashboard")
        self.state.pull_should_fail = True
        r = self.app.post("/api/update_app")
        self.assertTrue(r.get_json()["ok"])
        deadline = time.time() + 10
        st = {}
        while time.time() < deadline:
            try:
                st = json.loads((Path(self.state_dir.name) / "update_state.json").read_text())
                if st.get("status") == "failed":
                    break
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            time.sleep(0.1)
        self.assertEqual(st.get("status"), "failed")
        self.assertIn("pull failed", st.get("reason", ""))

    def test_update_state_roundtrip(self):
        r = self.app.get("/api/update_state")
        self.assertIsNone(r.get_json()["state"])
        (Path(self.state_dir.name) / "update_state.json").write_text(
            json.dumps({"status": "rolled_back", "reason": "x"}))
        r = self.app.get("/api/update_state")
        self.assertEqual(r.get_json()["state"]["status"], "rolled_back")

    def test_app_version_fields(self):
        import urllib.request as ur
        version_json = self.server.WEBFLASHER_DIR / "app_version.json"
        wrote = not version_json.exists()
        if wrote:
            version_json.write_text(json.dumps({
                "version": "0.8.6.2", "commit": "a" * 40,
                "branch": "master", "repo": "Pendulum-Effect/zima-tinyscreen"}))

        class FakeResp:
            def __init__(self, payload, status=200):
                self._p = payload
                self.status = status
            def read(self):
                return self._p
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=5):
            url = req if isinstance(req, str) else req.full_url
            if "api.github.com" in url:
                return FakeResp(json.dumps({"sha": "b" * 40}).encode())
            if "raw.githubusercontent" in url:
                return FakeResp(b"0.8.7.0\n")
            raise AssertionError("unexpected url " + url)

        orig = ur.urlopen
        self.server.urllib.request.urlopen = fake_urlopen
        try:
            r = self.app.get("/api/app_version")
            d = r.get_json()
            self.assertTrue(d["ok"])
            self.assertEqual(d["version"], "0.8.6.2")
            self.assertEqual(d["latest_version"], "0.8.7.0")
            self.assertTrue(d["update_available"])
            self.assertTrue(d["update_ready"])  # fake socket exists
        finally:
            self.server.urllib.request.urlopen = orig
            if wrote:
                version_json.unlink()

    def test_about_endpoint_github_and_fallback(self):
        import json as _json
        import shutil
        # Stage the baked copies the Dockerfile would create at build time
        # (the repo tree deliberately does NOT duplicate them into
        # webflasher/ -- root about.json/CHANGELOG.json are the only copies)
        staged = []
        for fname in ("about.json", "CHANGELOG.json"):
            dest = self.server.WEBFLASHER_DIR / fname
            if not dest.exists():
                shutil.copy(ROOT / fname, dest)
                staged.append(dest)
        self.addCleanup(lambda: [p.unlink() for p in staged])
        version_json = self.server.WEBFLASHER_DIR / "app_version.json"
        wrote = not version_json.exists()
        if wrote:
            version_json.write_text(_json.dumps({
                "version": "0.8.7.0", "commit": "a" * 40,
                "branch": "master", "repo": "Pendulum-Effect/zima-tinyscreen"}))

        class FakeResp:
            def __init__(self, payload):
                self._p = payload
            def read(self):
                return self._p
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def github_ok(url, timeout=5):
            u = url if isinstance(url, str) else url.full_url
            if u.endswith("/about.json"):
                return FakeResp(_json.dumps({"project": {"name": "FromGitHub"}}).encode())
            if u.endswith("/CHANGELOG.json"):
                return FakeResp(_json.dumps({"entries": [{"version": "9.9.9"}]}).encode())
            raise AssertionError("unexpected " + u)

        def github_down(url, timeout=5):
            import urllib.error
            raise urllib.error.URLError("no network")

        orig = self.server.urllib.request.urlopen
        try:
            # 1) GitHub reachable -> live content wins, source says github
            self.server.urllib.request.urlopen = github_ok
            d = self.app.get("/api/about").get_json()
            self.assertTrue(d["ok"])
            self.assertEqual(d["about"]["project"]["name"], "FromGitHub")
            self.assertEqual(d["source"], {"about": "github", "changelog": "github"})

            # 2) GitHub down -> baked repo copies serve as fallback
            self.server.urllib.request.urlopen = github_down
            d = self.app.get("/api/about").get_json()
            self.assertTrue(d["ok"])
            self.assertEqual(d["source"]["about"], "baked")
            self.assertEqual(d["about"]["project"]["name"], "TinyScreen")
            self.assertTrue(any(e["version"] == "0.8.7.0"
                                for e in d["changelog"]["entries"]))
        finally:
            self.server.urllib.request.urlopen = orig
            if wrote:
                version_json.unlink()

    def test_build_set_config_payload_passthrough(self):
        build = self.server.build_set_config_payload
        # legacy request (e.g. the wizard): no new fields leak in
        legacy = build({"board": 1, "pages": ["cpu"], "cycle_mode": "auto",
                        "cycle_seconds": 5, "brightness": 70})
        for k in ("night_enabled", "saver_enabled", "tz_offset_min"):
            self.assertNotIn(k, legacy)
        self.assertEqual(legacy["cmd"], "set_config")
        # full dashboard save: everything passes through, typed
        full = build({"board": 1, "pages": ["cpu"], "cycle_mode": "static",
                      "cycle_seconds": 10, "brightness": 100,
                      "night_enabled": True, "night_start_min": 1320,
                      "night_end_min": 420, "night_brightness": 0,
                      "tz_offset_min": -300, "saver_enabled": True,
                      "saver_minutes": 5, "saver_style": "clock"})
        self.assertIs(full["night_enabled"], True)
        self.assertEqual(full["night_brightness"], 0)
        self.assertEqual(full["tz_offset_min"], -300)
        self.assertEqual(full["saver_style"], "clock")

    def test_reset_device_over_real_pty(self):
        import pty
        import threading

        master, slave = pty.openpty()
        slave_path = os.ttyname(slave)

        def fake_device():
            buf = b""
            deadline = time.time() + 8
            while time.time() < deadline:
                try:
                    chunk = os.read(master, 256)
                except OSError:
                    return
                if not chunk:
                    continue
                buf += chunk
                if b'"cmd":"clear_config"' in buf:
                    os.write(master, b'{"ack":"clear_config","ok":true}\n')
                    return

        t = threading.Thread(target=fake_device, daemon=True)
        t.start()

        # Point the server at our pty "device", neutralize collector
        # pause/resume and the USB settle delay to keep the test fast.
        orig_detect = self.server.detect_port
        orig_sleep = self.server.time.sleep
        orig_pause = self.server.collector.pause
        orig_resume = self.server.collector.resume
        pauses = []
        self.server.detect_port = lambda: slave_path
        self.server.time.sleep = lambda s: None
        self.server.collector.pause = lambda: pauses.append("pause")
        self.server.collector.resume = lambda: pauses.append("resume")
        try:
            r = self.app.post("/api/reset_device")
            d = r.get_json()
            self.assertTrue(d["ok"], d)
            self.assertTrue(d["acked"], "device ack never seen")
            self.assertEqual(pauses, ["pause", "resume"])
        finally:
            self.server.detect_port = orig_detect
            self.server.time.sleep = orig_sleep
            self.server.collector.pause = orig_pause
            self.server.collector.resume = orig_resume
            os.close(master)
            os.close(slave)

    def test_reset_device_no_port(self):
        orig = self.server.detect_port
        self.server.detect_port = lambda: None
        try:
            r = self.app.post("/api/reset_device")
            self.assertEqual(r.status_code, 400)
            self.assertIn("No ESP32", r.get_json()["error"])
        finally:
            self.server.detect_port = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
