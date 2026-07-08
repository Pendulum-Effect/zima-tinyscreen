#!/usr/bin/env python3
"""
Tiny Screen self-update helper (Stage 2 of the app updater).

WHY THIS EXISTS AS A SEPARATE CONTAINER: a container cannot replace
itself. The moment the dashboard's own server process asks the Docker
daemon to stop its container (necessary to free ports 8989/8990 for the
replacement), that process is killed mid-script and can never issue the
"start the new one" call. So /api/update_app instead spawns THIS script
in a short-lived helper container -- created from the NEWLY PULLED image
(no third-party updater image, no standing second service; `AutoRemove`
deletes it the moment it exits, total lifetime ~10-20s) -- and the helper
performs the swap from outside the dying container:

    validate -> stop old -> rename old aside -> create new (same config,
    new image) -> start -> health-check -> remove old
                                        (on ANY failure: roll back --
                                        remove new, rename old back,
                                        start old)

Because the helper runs the NEW image's code, the config validation step
checks the NEW version's requirements (its own canonical_config.json)
against the old container's actual mounts/env/ports -- if the new version
needs something the current deployment doesn't have, the update is
REFUSED before anything is touched, and the user is told to do a normal
remove + reinstall through ZimaOS with the updated compose text.

Every step is appended to a state file on a bind-mounted host directory
(the old and new containers mount the same dir at /opt/tinyscreen/state),
so the dashboard can report the outcome after the swap -- including the
failure reason if the helper had to roll back.

Inputs (env vars, set by server.py when it creates this container):
    TS_TARGET_CONTAINER  full ID of the container to replace
    TS_TARGET_IMAGE      image ref to run the replacement from
    TS_STATE_FILE        state file path inside THIS container
                         (default /opt/tinyscreen/state/update_state.json)
"""

import json
import os
import sys
import time
from pathlib import Path

# When packaged, this lives next to dockerapi.py in /opt/tinyscreen/app/.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from dockerapi import DockerClient, DockerAPIError  # noqa: E402

CANONICAL_CONFIG = Path(__file__).resolve().parent / "canonical_config.json"
STATE_FILE = Path(os.environ.get("TS_STATE_FILE", "/opt/tinyscreen/state/update_state.json"))
OLD_NAME_SUFFIX = "-old"
HEALTH_TIMEOUT_S = 45
HEALTH_CMD = [
    "python3", "-c",
    "import urllib.request,sys;"
    "r=urllib.request.urlopen('http://127.0.0.1:8989/api/status',timeout=3);"
    "sys.exit(0 if r.status==200 else 1)",
]


def log(state, msg):
    """Append a timestamped line to the state file's log and print it
    (docker logs on the helper is the debugging fallback if the state
    file itself can't be written)."""
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(f"[updater] {line}", flush=True)
    state.setdefault("log", []).append(line)
    write_state(state)


def write_state(state):
    state["updated_at"] = time.time()
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        print(f"[updater] WARNING: could not write state file: {e}", flush=True)


def validate_against_canonical(old):
    """Compare the running container's actual config against THIS image's
    canonical_config.json. Returns a list of human-readable problems
    (empty list = compatible)."""
    canonical = json.loads(CANONICAL_CONFIG.read_text())
    problems = []

    mount_dests = {m.get("Destination") for m in old.get("Mounts", [])}
    for dest in canonical.get("required_bind_destinations", []):
        if dest not in mount_dests:
            problems.append(f"missing required mount at {dest}")

    env_names = set()
    for entry in (old.get("Config", {}).get("Env") or []):
        env_names.add(entry.split("=", 1)[0])
    for name in canonical.get("required_env", []):
        if name not in env_names:
            problems.append(f"missing required environment variable {name}")

    port_bindings = (old.get("HostConfig", {}).get("PortBindings") or {})
    for port in canonical.get("required_ports", []):
        if port not in port_bindings:
            problems.append(f"missing required published port {port}")

    return problems


def build_new_container_spec(old, new_image):
    """Recreate the old container's deployment config, pointed at the new
    image. HostConfig is carried over wholesale (binds, port bindings,
    privileged, restart policy -- everything ZimaOS's compose install set
    up). Config is carried selectively: Env and Labels are preserved
    (ZimaOS identifies its apps by the com.docker.compose.* labels, so
    dropping them would orphan the app in the ZimaOS UI), but Entrypoint/
    Cmd/Hostname are deliberately NOT copied so the NEW image's own
    defaults apply -- otherwise a future version that changes its
    entrypoint would be launched with the old one forever.
    """
    old_config = old.get("Config", {})
    spec = {
        "Image": new_image,
        "Env": old_config.get("Env") or [],
        "Labels": old_config.get("Labels") or {},
        "ExposedPorts": old_config.get("ExposedPorts") or {},
        "HostConfig": old.get("HostConfig") or {},
    }
    # Reattach to the same user-defined networks (compose creates one per
    # project). Aliases are rebuilt by Docker for a fresh container; the
    # old ones reference the old container's ID and shouldn't be copied.
    networks = (old.get("NetworkSettings", {}) or {}).get("Networks") or {}
    if networks:
        spec["NetworkingConfig"] = {
            "EndpointsConfig": {name: {} for name in networks}
        }
    return spec


def wait_for_health(client, container_id, state):
    """A container that merely reports Running isn't proof of anything --
    the process could be crash-looping. Exec an HTTP probe against
    /api/status INSIDE the new container (the helper isn't on the app's
    compose network, so it can't reach the port directly) until it
    answers 200 or the timeout expires."""
    deadline = time.time() + HEALTH_TIMEOUT_S
    last_err = "no probe attempted"
    while time.time() < deadline:
        try:
            info = client.inspect_container(container_id)
            if not info.get("State", {}).get("Running"):
                last_err = "container not in Running state"
                time.sleep(1.5)
                continue
            exit_code = client.exec_run(container_id, HEALTH_CMD)
            if exit_code == 0:
                return True, None
            last_err = f"health probe exit code {exit_code}"
        except DockerAPIError as e:
            last_err = str(e)
        time.sleep(1.5)
    return False, last_err


def rollback(client, state, old_id, old_name, new_id):
    log(state, "rolling back...")
    if new_id:
        try:
            client.remove_container(new_id, force=True)
            log(state, "removed failed new container")
        except DockerAPIError as e:
            log(state, f"could not remove new container: {e}")
    try:
        client.rename_container(old_id, old_name)
        log(state, f"restored old container name '{old_name}'")
    except DockerAPIError as e:
        log(state, f"could not rename old container back: {e}")
    try:
        client.start_container(old_id)
        log(state, "old container restarted")
    except DockerAPIError as e:
        log(state, f"could not restart old container: {e}")


def main():
    old_id = os.environ.get("TS_TARGET_CONTAINER", "")
    new_image = os.environ.get("TS_TARGET_IMAGE", "")
    state = {"status": "swapping", "started_at": time.time(),
             "target_image": new_image, "log": []}

    if not old_id or not new_image:
        state["status"] = "failed"
        state["reason"] = "helper launched without TS_TARGET_CONTAINER / TS_TARGET_IMAGE"
        write_state(state)
        return 1

    client = DockerClient()
    log(state, f"helper started for container {old_id[:12]} -> image {new_image}")

    # ---- validate BEFORE touching anything ---------------------------
    try:
        old = client.inspect_container(old_id)
    except DockerAPIError as e:
        state["status"] = "failed"
        state["reason"] = f"could not inspect target container: {e}"
        write_state(state)
        return 1

    problems = validate_against_canonical(old)
    if problems:
        state["status"] = "refused"
        state["reason"] = (
            "The new version's deployment requirements don't match this "
            "install: " + "; ".join(problems) + ". A one-click update "
            "can't fix this -- please remove the app in ZimaOS and "
            "reinstall it with the updated Docker Compose configuration."
        )
        log(state, f"refused: {'; '.join(problems)}")
        write_state(state)
        return 0

    old_name = (old.get("Name") or "/tinyscreen-dashboard").lstrip("/")
    parked_name = old_name + OLD_NAME_SUFFIX
    log(state, "config validated -- compatible")

    # A leftover parked container from a previous half-finished attempt
    # would collide on the rename below; clear it first.
    try:
        client.remove_container(parked_name, force=True)
        log(state, f"removed stale '{parked_name}' from a previous attempt")
    except DockerAPIError:
        pass  # normal case: nothing to clean

    # ---- the swap -----------------------------------------------------
    new_id = None
    try:
        log(state, "stopping current container...")
        client.stop_container(old_id, timeout_s=10)

        client.rename_container(old_id, parked_name)
        log(state, f"parked old container as '{parked_name}'")

        spec = build_new_container_spec(old, new_image)
        created = client.create_container(spec, name=old_name)
        new_id = created["Id"]
        log(state, f"created new container {new_id[:12]}")

        client.start_container(new_id)
        log(state, "started new container, waiting for health...")

        healthy, why = wait_for_health(client, new_id, state)
        if not healthy:
            raise RuntimeError(f"new container never became healthy: {why}")

        log(state, "new container healthy -- removing old one")
        client.remove_container(old_id, force=True)

        state["status"] = "success"
        state["new_container"] = new_id
        log(state, "update complete")
        write_state(state)
        return 0

    except (DockerAPIError, RuntimeError, KeyError) as e:
        log(state, f"swap failed: {e}")
        rollback(client, state, old_id, old_name, new_id)
        state["status"] = "rolled_back"
        state["reason"] = str(e)
        write_state(state)
        return 1


if __name__ == "__main__":
    sys.exit(main())
