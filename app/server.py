#!/usr/bin/env python3
"""
Tiny Screen ZimaOS app web server.

Serves the WebSerial flasher pages (and firmware binaries), a status
endpoint, and -- the point of this rewrite -- two server-side endpoints
that let a board plugged directly into the ZimaBlade (rather than into a
separate computer) be flashed and configured without any browser/WebSerial
involvement at all:

  POST /api/flash      -- flashes the bundled firmware binaries via esptool
  POST /api/configure   -- pushes a settings JSON command over serial

Both need exclusive access to the same USB serial port the stats collector
is also constantly using, so CollectorManager below pauses/resumes it
around those operations.

Runs as a SINGLE process serving both HTTP (8989) and HTTPS (8990) in
separate threads -- deliberately not two separate processes (the old
approach), since each would otherwise spin up its own independent
CollectorManager and fight over the same collector subprocess.
"""

import glob
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import ssl
import sys
import subprocess
import threading
import time
import urllib.request
import urllib.error
from datetime import timedelta
from pathlib import Path

from flask import Flask, jsonify, redirect, request, send_from_directory, session

from dockerapi import DockerClient, DockerAPIError

APP_DIR = Path(__file__).resolve().parent
WEBFLASHER_DIR = APP_DIR.parent / "webflasher"
FIRMWARE_DIR = WEBFLASHER_DIR / "firmware"
COLLECTOR_SCRIPT = APP_DIR.parent / "collector" / "stats_collector.py"
STATUS_FILE = Path(os.environ.get("TINYSCREEN_STATUS_FILE", "/tmp/tinyscreen_status.json"))
DOCKER_SOCK = os.environ.get("TINYSCREEN_DOCKER_SOCK", "/var/run/docker.sock")
STATE_DIR = Path(os.environ.get("TINYSCREEN_STATE_DIR", "/opt/tinyscreen/state"))
UPDATE_STATE_FILE = STATE_DIR / "update_state.json"

app = Flask(__name__, static_folder=None)

# --- Finding 4: cap request bodies globally ---------------------------
# Only /api/upload_cert previously limited its input; every other POST
# would read an unbounded body into memory (a trivial way to OOM a small
# ZimaBlade). 1 MiB is comfortably above any real request here -- the
# largest is a cert+key pair, itself separately capped at 64 KiB each.
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024


def _request_is_https():
    """True when the request came in on the TLS listener (port 8990).
    Derived from the server-side WSGI environ, not any client-supplied
    header, so it can't be spoofed by the caller."""
    env = request.environ
    if env.get("wsgi.url_scheme") == "https":
        return True
    return str(env.get("SERVER_PORT", "")) == "8990"


def _server_error(user_message, exc):
    """Return a stable, generic error to the client while logging the
    real exception server-side. Raw str(exception) can carry internal
    filesystem paths or details handy for probing, so the client gets a
    fixed sentence and the operator gets the specifics in the logs."""
    print(f"[server] {user_message}: {exc!r}", file=sys.stderr)
    return jsonify({"ok": False, "error": user_message}), 500

# --- Finding 1 + 5: same-origin / custom-header guard -----------------
# There is (by design, for now) no login on this LAN appliance. The real
# exposure that creates is CSRF: a random website you visit could make
# your browser POST to http://<zimablade>:8989/api/reset_device behind
# your back. Browsers let a page send a "simple" cross-site POST (form
# content-types) WITHOUT a preflight -- but they will NOT let it set a
# custom request header cross-origin without a preflight our server
# never answers. So we require, on every state-changing request, either:
#   - a same-origin Origin/Referer header (normal dashboard use), or
#   - our custom X-TinyScreen-Request header (set by our own fetch()
#     calls, and settable by intentional non-browser clients like a
#     certbot deploy hook curling a renewed cert in).
# A forged cross-site form POST has neither, and is refused. This is not
# authentication -- anyone actually on your LAN is still trusted -- it
# only closes the drive-by-browser hole.
CUSTOM_REQUEST_HEADER = "X-TinyScreen-Request"
_GUARDED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _same_origin(value):
    """True if an Origin/Referer URL points at this same host:port. Host
    match only -- scheme/port can legitimately differ (the dashboard is
    reached on both :8989 and :8990), and an attacker can't forge a
    victim host's Origin from another site anyway."""
    if not value:
        return False
    try:
        from urllib.parse import urlparse
        their = urlparse(value).hostname
    except (ValueError, TypeError):
        return False
    if not their:
        return False
    here = urlparse("http://" + (request.host or "")).hostname
    return their == here


@app.before_request
def _csrf_guard():
    if request.method not in _GUARDED_METHODS:
        return None
    if request.headers.get(CUSTOM_REQUEST_HEADER):
        return None
    if _same_origin(request.headers.get("Origin")) or \
       _same_origin(request.headers.get("Referer")):
        return None
    return jsonify({
        "ok": False,
        "error": ("Request blocked: cross-site or headerless request to a "
                  "state-changing endpoint. Browser requests must come from "
                  "the dashboard itself; scripted clients should send the "
                  f"{CUSTOM_REQUEST_HEADER} header."),
    }), 403


# ---------------------------------------------------------------------
# Optional PIN authentication (0.9.5)
# ---------------------------------------------------------------------
# The threat model this addresses (see README "Security model"): the app
# deliberately trusts the LAN for convenience, but "the LAN" on a home
# network includes every family laptop, phone, smart TV, and guest
# device -- and this container holds the Docker socket, so gating the
# API raises the bar in front of any future endpoint bug, not just the
# annoyance-tier actions (reflash/reset/reconfigure) an open API allows.
#
# Design decisions, so future rounds don't relitigate them:
#   - OFF by default, enabled from the dashboard's General tab. This is
#     a display gadget; mandatory auth in the wizard would be friction
#     disproportionate to the asset for many installs.
#   - When enabled, every state-changing (POST/PUT/PATCH/DELETE) /api/
#     endpoint requires a logged-in session. A separate "lock_stats"
#     toggle extends that to the read-only GET endpoints for households
#     that consider system stats sensitive. Static pages stay open
#     either way -- they're the same files for everyone and contain no
#     data.
#   - PIN is pbkdf2-sha256, per-PIN random salt, 600k iterations,
#     stored 0600 in the state dir (survives app updates). Changing,
#     disabling, or toggling lock_stats requires the CURRENT pin in the
#     request body -- a hijacked session cookie alone can't take over
#     or remove the lock.
#   - Login is globally rate-limited (5 consecutive failures -> 60s
#     lockout) rather than per-IP: on a flat LAN, per-IP limits are
#     trivially dodged and the only cost of a global limit is that a
#     failed brute force briefly locks everyone out -- acceptable, and
#     visible.
#   - Sessions are Flask's signed cookies (HttpOnly, SameSite=Lax,
#     30 days). The Secure flag is deliberately NOT set: the primary
#     interface is plain HTTP on the LAN (8989), and a Secure cookie
#     would silently break login there. The cookie grants access to a
#     LAN display dashboard; anyone positioned to sniff it is already
#     on the trusted network segment this model accepts.
#   - Recovery from a forgotten PIN: delete auth.json from the app's
#     state directory (documented in the README) -- physical/ZimaOS
#     access outranks the PIN, which is the right hierarchy for a home
#     appliance.

AUTH_FILE_NAME = "auth.json"
_PBKDF2_ITERATIONS = 600_000
_PIN_MIN_LEN, _PIN_MAX_LEN = 4, 128
_LOCKOUT_THRESHOLD = 5
_LOCKOUT_SECONDS = 60

_login_lock = threading.Lock()
_login_failures = 0
_login_locked_until = 0.0

# Endpoints that must work while locked, or nothing can ever unlock:
_AUTH_EXEMPT_PATHS = {"/api/auth/status", "/api/auth/login"}


def _auth_file():
    return STATE_DIR / AUTH_FILE_NAME


def _load_auth():
    """The stored auth config, or None when the PIN is not enabled.
    Read per-request on purpose: it's a tiny local file, and rereading
    means an admin deleting auth.json (the documented recovery path)
    takes effect immediately, no restart."""
    try:
        cfg = json.loads(_auth_file().read_text())
        if not isinstance(cfg, dict) or "hash" not in cfg or "salt" not in cfg:
            return None
        return cfg
    except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _hash_pin(pin: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, iterations)


def _verify_pin(pin, cfg) -> bool:
    if not isinstance(pin, str):
        return False
    try:
        salt = bytes.fromhex(cfg["salt"])
        want = bytes.fromhex(cfg["hash"])
        iters = int(cfg.get("iterations", _PBKDF2_ITERATIONS))
    except (KeyError, ValueError, TypeError):
        return False
    return hmac.compare_digest(_hash_pin(pin, salt, iters), want)


def _write_auth(pin: str, lock_stats: bool) -> None:
    salt = secrets.token_bytes(16)
    cfg = {
        "algo": "pbkdf2-sha256",
        "iterations": _PBKDF2_ITERATIONS,
        "salt": salt.hex(),
        "hash": _hash_pin(pin, salt, _PBKDF2_ITERATIONS).hex(),
        "lock_stats": bool(lock_stats),
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _auth_file().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg))
    os.chmod(tmp, 0o600)
    os.replace(tmp, _auth_file())


def _valid_new_pin(pin):
    return isinstance(pin, str) and _PIN_MIN_LEN <= len(pin) <= _PIN_MAX_LEN


def _session_secret() -> bytes:
    """Signing key for the session cookie, persisted in the state dir so
    logins survive app updates and restarts. Generated on first use."""
    path = STATE_DIR / "session_secret"
    try:
        secret = path.read_bytes()
        if len(secret) >= 32:
            return secret
    except (FileNotFoundError, OSError):
        pass
    secret = secrets.token_bytes(32)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(secret)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as e:
        # Worst case (read-only state dir): sessions won't survive a
        # restart. Auth still works; users just log in again.
        print(f"[auth] could not persist session secret ({e}); "
              f"logins will not survive restarts.", file=sys.stderr)
    return secret


app.secret_key = _session_secret()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)


@app.before_request
def _auth_guard():
    """Runs AFTER _csrf_guard (registration order): a request must be
    same-origin/headered first, then authenticated. No-op until a PIN
    is set."""
    cfg = _load_auth()
    if cfg is None:
        return None
    path = request.path
    if path in _AUTH_EXEMPT_PATHS or not path.startswith("/api/"):
        return None
    needs_auth = request.method in _GUARDED_METHODS or cfg.get("lock_stats")
    if not needs_auth or session.get("auth_ok"):
        return None
    return jsonify({
        "ok": False,
        "auth_required": True,
        "error": "This dashboard is PIN-protected. Enter the PIN to continue.",
    }), 401


@app.route("/api/auth/status", methods=["GET"])
def api_auth_status():
    cfg = _load_auth()
    return jsonify({
        "ok": True,
        "enabled": cfg is not None,
        "authed": bool(session.get("auth_ok")),
        "lock_stats": bool(cfg.get("lock_stats")) if cfg else False,
    })


@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    global _login_failures, _login_locked_until
    cfg = _load_auth()
    if cfg is None:
        return jsonify({"ok": False, "error": "No PIN is set."}), 400

    with _login_lock:
        wait = _login_locked_until - time.time()
        if wait > 0:
            return jsonify({"ok": False,
                            "error": f"Too many attempts. Try again in {int(wait) + 1}s.",
                            "retry_in": int(wait) + 1}), 429

    pin = (request.get_json(silent=True) or {}).get("pin")
    if _verify_pin(pin, cfg):
        with _login_lock:
            _login_failures = 0
        session.permanent = True
        session["auth_ok"] = True
        return jsonify({"ok": True})

    with _login_lock:
        _login_failures += 1
        if _login_failures >= _LOCKOUT_THRESHOLD:
            _login_locked_until = time.time() + _LOCKOUT_SECONDS
            _login_failures = 0
    return jsonify({"ok": False, "error": "Wrong PIN."}), 403


@app.route("/api/auth/set_pin", methods=["POST"])
def api_auth_set_pin():
    """Set, change, or disable the PIN, or toggle the stats lock. Any
    change while a PIN is enabled requires the CURRENT pin in the body
    -- a session cookie alone is deliberately not enough (see design
    notes above)."""
    body = request.get_json(silent=True) or {}
    cfg = _load_auth()

    if cfg is not None and not _verify_pin(body.get("current_pin"), cfg):
        return jsonify({"ok": False,
                        "error": "Current PIN is required (and must be correct) "
                                 "to change these settings."}), 403

    if body.get("disable"):
        if cfg is None:
            return jsonify({"ok": False, "error": "No PIN is set."}), 400
        try:
            _auth_file().unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            return _server_error("Could not remove the PIN.", e)
        return jsonify({"ok": True, "enabled": False})

    new_pin = body.get("new_pin")
    lock_stats = body.get("lock_stats")

    if new_pin is not None:
        if not _valid_new_pin(new_pin):
            return jsonify({"ok": False,
                            "error": f"PIN must be {_PIN_MIN_LEN}-{_PIN_MAX_LEN} "
                                     f"characters."}), 400
        effective_lock = bool(lock_stats) if lock_stats is not None \
            else bool(cfg.get("lock_stats")) if cfg else False
        try:
            _write_auth(new_pin, effective_lock)
        except OSError as e:
            return _server_error("Could not store the PIN.", e)
        # Log the requester in so enabling the PIN doesn't immediately
        # lock out the person who just enabled it.
        session.permanent = True
        session["auth_ok"] = True
        return jsonify({"ok": True, "enabled": True, "lock_stats": effective_lock})

    if lock_stats is not None:
        if cfg is None:
            return jsonify({"ok": False, "error": "Set a PIN first."}), 400
        cfg["lock_stats"] = bool(lock_stats)
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = _auth_file().with_suffix(".json.tmp")
            tmp.write_text(json.dumps(cfg))
            os.chmod(tmp, 0o600)
            os.replace(tmp, _auth_file())
        except OSError as e:
            return _server_error("Could not update the setting.", e)
        return jsonify({"ok": True, "enabled": True, "lock_stats": bool(lock_stats)})

    return jsonify({"ok": False, "error": "Nothing to do."}), 400


class RawSerialPort:
    """Minimal raw-device serial port, NOT pyserial -- see the matching
    class/comment in collector/stats_collector.py for why: isolation
    testing proved pyserial itself (independent of DTR/RTS, independent
    of read vs. write mode) breaks this board's native-USB connection on
    Linux, while a plain POSIX file descriptor configured via `stty`
    works perfectly. /api/configure needs the same fix as the collector,
    since it also talks to the board over serial directly.
    """

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 2):
        self.port = port
        result = subprocess.run(
            ["stty", "-F", port, str(baudrate), "raw", "-echo"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise OSError(f"stty configuration failed for {port}: {result.stderr.strip()}")
        self._fd = os.open(port, os.O_RDWR | os.O_NOCTTY)
        self._timeout = timeout

    def write(self, data: bytes):
        os.write(self._fd, data)

    def read(self, size: int = 256) -> bytes:
        import select
        r, _, _ = select.select([self._fd], [], [], self._timeout)
        if not r:
            return b""
        try:
            return os.read(self._fd, size)
        except OSError:
            return b""

    def close(self):
        try:
            os.close(self._fd)
        except OSError:
            pass


# ---------------------------------------------------------------------
# Serial port detection (same convention as the collector script)
# ---------------------------------------------------------------------

def detect_port():
    env_port = os.environ.get("TINYSCREEN_SERIAL_PORT")
    if env_port:
        return env_port
    for pattern in ["/dev/ttyACM*", "/dev/ttyUSB*"]:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[0]
    return None


# ---------------------------------------------------------------------
# Collector lifecycle management
# ---------------------------------------------------------------------

class CollectorManager:
    """Runs the stats collector as a background subprocess, restarting it
    if it dies, and pausing it on request so something else (flashing,
    pushing a config) can have the serial port to itself for a moment.
    """

    def __init__(self):
        self.proc = None
        self.lock = threading.RLock()
        self.paused = False
        self._pause_depth = 0  # tracks overlapping pause() calls -- see pause()/resume()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def _log(self, msg):
        print(f"[collectormgr {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr)

    def _spawn(self):
        args = ["python3", str(COLLECTOR_SCRIPT)]
        port = os.environ.get("TINYSCREEN_SERIAL_PORT")
        if port:
            args += ["--port", port]
        self.proc = subprocess.Popen(args)
        self._log(f"spawned collector, pid={self.proc.pid}")

    def _watchdog(self):
        while True:
            try:
                with self.lock:
                    proc_dead = self.proc is None or self.proc.poll() is not None
                    if not self.paused and proc_dead:
                        self._log(f"tick: paused={self.paused} proc_dead={proc_dead} -> spawning")
                        self._spawn()
                    else:
                        # Only log the "doing nothing" case when paused,
                        # since a healthy running collector ticking by
                        # silently every 5s would otherwise flood the logs.
                        if self.paused:
                            self._log(f"tick: paused={self.paused} (pause_depth={self._pause_depth}) -> not spawning")
            except Exception as e:
                # Never let the watchdog thread itself die -- a single
                # unhandled error here previously meant the collector
                # would never be respawned again for the rest of the
                # container's life (e.g. after any /api/flash or
                # /api/configure call, which both pause() then rely
                # entirely on this thread noticing resume() and
                # restarting it), with no visible error anywhere.
                print(f"CollectorManager watchdog error ({e}); will retry next cycle.",
                      file=sys.stderr)
            time.sleep(5)

    def pause(self):
        """Stop the collector and hold off restarting it until every
        matching resume() call has happened. Depth-counted so that two
        OVERLAPPING pause()/resume() pairs (e.g. two nearly-simultaneous
        /api/flash or /api/configure requests, which Flask's threaded=True
        mode allows to run concurrently) can't have one request's resume()
        prematurely clear the pause while the other request's operation is
        still in-flight and still needs exclusive access to the port.
        """
        with self.lock:
            self._pause_depth += 1
            self._log(f"pause() called, depth now {self._pause_depth}")
            self.paused = True
            if self.proc and self.proc.poll() is None:
                self._log(f"terminating collector pid={self.proc.pid}")
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._log("terminate timed out, killing")
                    self.proc.kill()
                    self.proc.wait(timeout=5)
            self.proc = None
        time.sleep(1)  # give the OS a moment to actually release the port

    def resume(self):
        with self.lock:
            self._pause_depth = max(0, self._pause_depth - 1)
            self._log(f"resume() called, depth now {self._pause_depth}")
            if self._pause_depth == 0:
                self.paused = False
                self._log("depth reached 0, paused=False")
            else:
                self._log(f"still {self._pause_depth} overlapping pause(s) outstanding, staying paused")

    def is_running(self):
        with self.lock:
            return self.proc is not None and self.proc.poll() is None


collector = CollectorManager()


# ---------------------------------------------------------------------
# Static file serving
# ---------------------------------------------------------------------

# The legacy pages (index.html WebSerial flasher, settings.html
# configurator, onboard.html on-device flow) are retired as of 0.8.9.1 --
# the wizard + dashboard fully replaced them. Old bookmarks and README
# links redirect to their modern equivalents instead of 404ing.
LEGACY_REDIRECTS = {
    "index.html": "/dashboard.html",
    "settings.html": "/dashboard.html",
    "onboard.html": "/wizard.html",
}


@app.route("/")
@app.route("/<path:filename>")
def serve_flasher(filename="dashboard.html"):
    if filename in LEGACY_REDIRECTS:
        return redirect(LEGACY_REDIRECTS[filename], code=302)
    return send_from_directory(WEBFLASHER_DIR, filename)


@app.route("/firmware/<path:filename>")
def serve_firmware(filename):
    return send_from_directory(FIRMWARE_DIR, filename)


# ---------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------

# The ZimaBlade's hostname, learned via `docker info` over the socket
# (the container's own hostname is just its container ID). Fetched once
# and cached -- hostnames don't change mid-session, and the dashboard
# polls /api/status frequently.
_host_name_cache = {"fetched": False, "name": None}


def get_host_name():
    if not _host_name_cache["fetched"]:
        _host_name_cache["fetched"] = True
        _host_name_cache["name"] = None
        try:
            if os.path.exists(DOCKER_SOCK):
                info = DockerClient(DOCKER_SOCK).info()
                name = (info or {}).get("Name")
                if isinstance(name, str) and name:
                    _host_name_cache["name"] = name
        except (DockerAPIError, OSError):
            pass  # no socket / daemon hiccup -> UI just omits the name
    return _host_name_cache["name"]


@app.route("/api/status")
def status():
    if not STATUS_FILE.exists():
        return jsonify({"collector_running": False, "last_stats": None,
                        "hostname": get_host_name()})
    try:
        data = json.loads(STATUS_FILE.read_text())
    except Exception:
        data = None
    age = time.time() - STATUS_FILE.stat().st_mtime
    return jsonify({
        "collector_running": age < 10,
        "last_stats": data,
        "age_seconds": age,
        "hostname": get_host_name(),
    })


# ---------------------------------------------------------------------
# Server-side configure (board plugged into this ZimaBlade, not a computer)
# ---------------------------------------------------------------------

def build_set_config_payload(cfg):
    """The serial set_config command for a dashboard/wizard save request.
    The original fields always send (every caller has always supplied
    them); the night-mode/screensaver fields added in firmware 1.1.0 are
    passthrough-only-if-present, preserving the firmware's own
    "only update fields that appear in the command" semantics -- an old
    wizard page or a partial save can never silently reset a schedule.
    """
    payload = {
        "cmd": "set_config",
        "board": int(cfg.get("board", 0)),
        "pages": cfg.get("pages", ["temp"]),
        "cycle_mode": cfg.get("cycle_mode", "static"),
        "cycle_seconds": int(cfg.get("cycle_seconds", 10)),
        "brightness": int(cfg.get("brightness", 100)),
    }
    for key, cast in [("night_enabled", bool), ("night_start_min", int),
                      ("night_end_min", int), ("night_brightness", int),
                      ("tz_offset_min", int), ("saver_enabled", bool),
                      ("saver_minutes", int), ("saver_style", str),
                      ("rotation", int), ("square_fit", bool)]:
        if key in cfg:
            payload[key] = cast(cfg[key])
    # Per-page layout styles: pass the whole mapping through untouched --
    # the firmware whitelists per page, so unknown ids degrade to default
    # on-device rather than being policed twice.
    if isinstance(cfg.get("layouts"), dict):
        payload["layouts"] = {str(k): str(v) for k, v in cfg["layouts"].items()}
    return payload


# IANA zone names look like "America/Chicago", "Europe/Paris",
# "Etc/GMT+5", or a bare "UTC". Require that shape rather than any
# slash-bearing string: the previous pattern already blocked traversal
# (no "." allowed), this narrows it further to real zone syntax as
# defense-in-depth before the name ever reaches ZoneInfo().
_TZ_NAME_RE = re.compile(r"^(UTC|[A-Za-z][A-Za-z0-9_+-]{0,30}"
                         r"(/[A-Za-z0-9_+-]{1,30}){0,2})$")


def store_timezone_name(tz_name):
    """Persist the browser-reported IANA zone name (e.g.
    'America/Chicago') to the state dir, where the collector picks it up
    to compute DST-correct local time for the display. Server-side state,
    deliberately NOT forwarded to the firmware -- the board has no zone
    database; it just consumes the resulting local_min from the stats
    stream. Returns True if written."""
    if not isinstance(tz_name, str) or not _TZ_NAME_RE.match(tz_name):
        return False
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_DIR / "timezone.txt.tmp"
        tmp.write_text(tz_name + "\n")
        os.replace(tmp, STATE_DIR / "timezone.txt")
        return True
    except OSError as e:
        print(f"[server] could not persist timezone: {e}", file=sys.stderr)
        return False


@app.route("/api/configure", methods=["POST"])
def api_configure():
    cfg = request.get_json(silent=True) or {}
    # Timezone is server-side state (see store_timezone_name) -- persist
    # it before any serial work so it lands even if the device is
    # unplugged right now.
    if "tz_name" in cfg:
        store_timezone_name(cfg["tz_name"])
    port = detect_port()
    if not port:
        return jsonify({"ok": False, "error": "No ESP32 serial device found. Is it plugged in?"}), 400

    collector.pause()
    ser = None
    try:
        ser = RawSerialPort(port, baudrate=115200, timeout=2)
        time.sleep(2)  # let the board's USB settle if it just reset

        payload = json.dumps(build_set_config_payload(cfg)) + "\n"
        ser.write(payload.encode("utf-8"))

        acked = False
        buf = b""
        deadline = time.time() + 4
        while time.time() < deadline and not acked:
            chunk = ser.read(256)
            if chunk:
                buf += chunk
                if b'"ack":"set_config"' in buf:
                    acked = True

        return jsonify({"ok": True, "acked": acked})
    except Exception as e:
        return _server_error("Failed to send settings to the device.", e)
    finally:
        if ser:
            try:
                ser.close()
            except Exception:
                pass
        collector.resume()


@app.route("/api/reset_device", methods=["POST"])
def api_reset_device():
    """Factory-reset the display: send clear_config over serial, which
    wipes the device's stored settings and reboots it into the same
    hands-off unconfigured state as a fresh flash. The dashboard then
    walks the user back into the first-time wizard. Same
    pause-collector / exclusive-serial discipline as /api/configure --
    these two must never run concurrently with the collector's writes.
    """
    port = detect_port()
    if not port:
        return jsonify({"ok": False, "error": "No ESP32 serial device found. Is it plugged in?"}), 400

    collector.pause()
    ser = None
    try:
        ser = RawSerialPort(port, baudrate=115200, timeout=2)
        time.sleep(2)  # let the board's USB settle if it just reset

        ser.write(b'{"cmd":"clear_config"}\n')

        acked = False
        buf = b""
        deadline = time.time() + 4
        while time.time() < deadline and not acked:
            chunk = ser.read(256)
            if chunk:
                buf += chunk
                if b'"ack":"clear_config"' in buf:
                    acked = True

        return jsonify({"ok": True, "acked": acked})
    except Exception as e:
        return _server_error("Failed to reset the device.", e)
    finally:
        if ser:
            try:
                ser.close()
            except Exception:
                pass
        collector.resume()


@app.route("/api/app_version", methods=["GET"])
def api_app_version():
    """Stage 1 of the app/container updater: detection only, no auto-apply
    yet (see the project discussion on Watchtower vs. a custom
    self-updater -- this deliberately doesn't touch the Docker socket at
    all). Compares the commit SHA this image was built from (stamped at
    CI build time, see .github/workflows/docker-build-push.yml's "Stamp
    app version" step) against GitHub's actual latest commit on the same
    branch, via a plain public API call -- no Docker Hub API or image
    digest involved, sidestepping the chicken-and-egg problem of a
    running container never being able to know its own image's digest
    from the inside.
    """
    version_file = WEBFLASHER_DIR / "app_version.json"
    try:
        baked_in = json.loads(version_file.read_text())
        current_commit = baked_in["commit"]
        branch = baked_in["branch"]
        repo = baked_in["repo"]
    except (FileNotFoundError, OSError, json.JSONDecodeError, KeyError):
        return jsonify({"ok": False, "error": "No app_version.json baked into this image."})

    # The human-facing version string (repo-root VERSION file, e.g.
    # "0.8.6.2", stamped into app_version.json at CI time). Purely for
    # display -- update DETECTION still keys off the commit SHA, which is
    # unambiguous and can't be forgotten the way a manual version bump
    # can. Older images predate this field, hence .get().
    current_version = baked_in.get("version")

    # update_ready: whether one-click self-update (Stage 2) can actually
    # run on this deployment -- i.e. the Docker socket is mounted. If an
    # update is available but this is False, the dashboard tells the user
    # a remove+reinstall with the new compose config is needed (once).
    update_ready = os.path.exists(DOCKER_SOCK)

    api_url = f"https://api.github.com/repos/{repo}/commits/{branch}"
    try:
        req = urllib.request.Request(api_url, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            latest = json.loads(resp.read().decode("utf-8"))
        latest_commit = latest["sha"]
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, KeyError) as e:
        # GitHub being unreachable/rate-limited shouldn't be treated as a
        # hard error -- just report that we couldn't determine it, same
        # pattern as the firmware update check.
        return jsonify({
            "ok": True,
            "version": current_version,
            "current_commit": current_commit,
            "latest_commit": None,
            "update_available": False,
            "update_ready": update_ready,
            "note": f"Could not reach GitHub: {e}",
        })

    # Best-effort lookup of the LATEST version string (raw VERSION file on
    # GitHub), so "Update available" can name the version it would update
    # TO. Isolated failure domain: commits API succeeding but raw.github
    # failing just means we show the short SHA instead.
    latest_version = None
    try:
        raw_url = f"https://raw.githubusercontent.com/{repo}/{branch}/VERSION"
        with urllib.request.urlopen(raw_url, timeout=5) as resp:
            latest_version = resp.read().decode("utf-8").strip() or None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        pass

    return jsonify({
        "ok": True,
        "version": current_version,
        "latest_version": latest_version,
        "current_commit": current_commit,
        "latest_commit": latest_commit,
        "update_available": current_commit != latest_commit,
        "update_ready": update_ready,
    })


# ---------------------------------------------------------------------
# Stage 2 of the app updater: one-click self-update via the Docker socket
# ---------------------------------------------------------------------
#
# A container cannot replace itself (stopping ourselves to free ports
# 8989/8990 kills this very process before it could start the
# replacement), so the actual swap is delegated to a short-lived helper
# container -- spawned from the NEWLY PULLED image, auto-removed the
# moment it finishes, alive for ~10-20 seconds total. NOT the Watchtower
# model (a permanent second service polling in the background); the
# helper only ever exists during a user-initiated update click. See
# self_update_helper.py for the swap/validate/rollback logic itself.

_update_lock = threading.Lock()
_update_thread = None


def _write_update_state(state):
    state["updated_at"] = time.time()
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = UPDATE_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        os.replace(tmp, UPDATE_STATE_FILE)
    except OSError as e:
        print(f"[updater] WARNING: could not write update state: {e}", file=sys.stderr)


def _detect_own_container_id(client):
    """Figure out which container WE are, so the helper knows what to
    replace. Several fallbacks, most-reliable first:
      1. /proc/self/mountinfo -- the container's writable layer paths
         embed the full 64-hex container ID.
      2. $HOSTNAME -- Docker sets the hostname to the short container ID
         unless the compose file overrides it (ours doesn't).
      3. The compose-pinned container name 'tinyscreen-dashboard'.
    Whatever we find is verified with an actual inspect before use.
    """
    import re
    candidates = []
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text()
        m = re.search(r"/(?:docker|moby)/containers/([0-9a-f]{64})/", mountinfo)
        if m:
            candidates.append(m.group(1))
    except OSError:
        pass
    hostname = os.environ.get("HOSTNAME", "")
    if re.fullmatch(r"[0-9a-f]{12,64}", hostname):
        candidates.append(hostname)
    candidates.append("tinyscreen-dashboard")

    for cand in candidates:
        try:
            info = client.inspect_container(cand)
            return info["Id"], info
        except (DockerAPIError, KeyError):
            continue
    return None, None


def _run_update(image_ref, own_id, own_info):
    """Background worker for /api/update_app: pull the new image, then
    spawn the helper container that performs the actual swap (which will
    kill this very process partway through -- that's the design)."""
    client = DockerClient(DOCKER_SOCK)
    state = {"status": "pulling", "target_image": image_ref,
             "started_at": time.time(),
             "log": [f"[{time.strftime('%H:%M:%S')}] pulling {image_ref}..."]}
    _write_update_state(state)
    try:
        client.pull_image(image_ref)
        state["log"].append(f"[{time.strftime('%H:%M:%S')}] pull complete")
        state["status"] = "launching_helper"
        _write_update_state(state)

        # The helper needs the state dir too (to keep reporting progress
        # after this container dies); reuse the same HOST path this
        # container has it bind-mounted from rather than hardcoding a
        # /DATA path that could differ between installs. The lookup and
        # bind target use the CANONICAL in-container path (the helper
        # always reads/writes its default TS_STATE_FILE there), NOT this
        # server's STATE_DIR, which env can relocate independently.
        canonical_state_dest = "/opt/tinyscreen/state"
        state_host_path = None
        for m in own_info.get("Mounts", []):
            if m.get("Destination") == canonical_state_dest:
                state_host_path = m.get("Source")
                break
        binds = [f"{DOCKER_SOCK}:/var/run/docker.sock"]
        if state_host_path:
            binds.append(f"{state_host_path}:{canonical_state_dest}")

        helper_name = "tinyscreen-updater"
        try:  # clear any leftover helper from an interrupted prior run
            client.remove_container(helper_name, force=True)
        except DockerAPIError:
            pass
        helper = client.create_container({
            "Image": image_ref,
            "Entrypoint": ["python3", "/opt/tinyscreen/app/self_update_helper.py"],
            "Env": [
                f"TS_TARGET_CONTAINER={own_id}",
                f"TS_TARGET_IMAGE={image_ref}",
            ],
            "HostConfig": {"Binds": binds, "AutoRemove": True},
        }, name=helper_name)
        client.start_container(helper["Id"])
        state["status"] = "swapping"
        state["log"].append(f"[{time.strftime('%H:%M:%S')}] helper started -- "
                            "this app will now restart")
        _write_update_state(state)
        # The helper takes it from here; it will stop this container
        # within seconds. Nothing more for this thread to do.
    except (DockerAPIError, OSError, KeyError) as e:
        state["status"] = "failed"
        state["reason"] = f"could not start the update: {e}"
        _write_update_state(state)


@app.route("/api/update_app", methods=["POST"])
def api_update_app():
    """Kick off a one-click self-update. Returns immediately; progress is
    reported via /api/update_state (and, once the swap begins, via this
    whole app briefly going away and coming back on the new version --
    the dashboard polls through the gap)."""
    global _update_thread
    if not os.path.exists(DOCKER_SOCK):
        return jsonify({
            "ok": False,
            "error": ("The Docker socket isn't mounted into this container, so "
                      "one-click updates can't run on this install. Remove the "
                      "app in ZimaOS and reinstall it with the updated Docker "
                      "Compose configuration (which adds the "
                      "/var/run/docker.sock volume) to enable them."),
        }), 400

    client = DockerClient(DOCKER_SOCK)
    try:
        if not client.ping():
            return jsonify({"ok": False, "error": "Docker daemon did not answer on the socket."}), 502
    except OSError as e:
        return jsonify({"ok": False, "error": f"Could not reach the Docker daemon: {e}"}), 502

    own_id, own_info = _detect_own_container_id(client)
    if not own_id:
        return jsonify({"ok": False,
                        "error": "Couldn't identify this app's own container via the Docker API."}), 500

    image_ref = (own_info.get("Config", {}) or {}).get("Image")
    if not image_ref:
        return jsonify({"ok": False, "error": "Couldn't determine this container's image reference."}), 500

    with _update_lock:
        if _update_thread and _update_thread.is_alive():
            return jsonify({"ok": False, "error": "An update is already in progress."}), 409
        _update_thread = threading.Thread(
            target=_run_update, args=(image_ref, own_id, own_info), daemon=True)
        _update_thread.start()

    return jsonify({"ok": True, "status": "started", "image": image_ref})


@app.route("/api/update_state", methods=["GET"])
def api_update_state():
    """Last/current self-update progress and outcome, persisted across
    the container swap via the state-dir bind mount. The dashboard polls
    this through the update gap and also checks it on page load to
    surface a refused or rolled-back attempt."""
    try:
        state = json.loads(UPDATE_STATE_FILE.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return jsonify({"ok": True, "state": None})
    return jsonify({"ok": True, "state": state})


@app.route("/api/about", methods=["GET"])
def api_about():
    """Content for the dashboard's About tab. The REPO copies of
    about.json and CHANGELOG.json are the single source of truth
    (per-project decision: no maintaining the same info in two places),
    so this fetches them live from raw.githubusercontent.com on each
    page load -- meaning credits/changelog edits apply to every install
    WITHOUT shipping a new image. The copies baked into the image at
    build time are the offline fallback, so the tab still works when
    GitHub is unreachable.
    """
    # Which repo/branch to fetch from comes from the same CI-stamped
    # file the version check uses -- never hardcoded.
    repo, branch = None, None
    try:
        baked_ver = json.loads((WEBFLASHER_DIR / "app_version.json").read_text())
        repo, branch = baked_ver.get("repo"), baked_ver.get("branch")
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        pass

    def load(filename):
        if repo and branch:
            try:
                url = f"https://raw.githubusercontent.com/{repo}/{branch}/{filename}"
                with urllib.request.urlopen(url, timeout=5) as resp:
                    return json.loads(resp.read().decode("utf-8")), "github"
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                    json.JSONDecodeError):
                pass
        try:
            return json.loads((WEBFLASHER_DIR / filename).read_text()), "baked"
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None, "missing"

    about, about_src = load("about.json")
    changelog, changelog_src = load("CHANGELOG.json")
    if about is None and changelog is None:
        return jsonify({"ok": False, "error": "No About content available."})
    return jsonify({
        "ok": True,
        "about": about,
        "changelog": changelog,
        "source": {"about": about_src, "changelog": changelog_src},
    })


@app.route("/api/last_flash", methods=["GET"])
def api_last_flash():
    """The most recent firmware-flash outcome (esptool output included),
    persisted across app updates via the state dir -- surfaced in the
    General tab's Debugging view."""
    try:
        return jsonify({"ok": True,
                        "flash": json.loads((STATE_DIR / "last_flash.json").read_text())})
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return jsonify({"ok": True, "flash": None})


@app.route("/api/firmware_info", methods=["GET"])
def api_firmware_info():
    """Reports the firmware version bundled in THIS running image, stamped
    at CI build time from FIRMWARE_VERSION in main.cpp (see
    .github/workflows/docker-build-push.yml's "Stage firmware binaries"
    step). Compared against a device's own reported version (from
    get_config, via /api/current_config) to tell the General tab's
    "Check for Update" whether a newer firmware is actually available to
    flash -- not auto-generated from the git commit, so unrelated
    Python/dashboard-only changes don't make every deploy look like a
    firmware update.
    """
    version_file = FIRMWARE_DIR / "version.txt"
    try:
        version = version_file.read_text().strip()
    except (FileNotFoundError, OSError):
        version = None
    return jsonify({"ok": version is not None, "bundled_version": version})


@app.route("/api/current_config", methods=["GET"])
def api_current_config():
    """Queries the device's actual current saved config via the get_config
    firmware command -- previously this whole protocol was write-only, so
    the only way to know what was currently configured was to remember
    whatever was last sent. Needed for the settings dashboard to show
    real current state (pages, cycle mode, brightness, board, firmware
    version) instead of just being another blind form.

    When the device is unplugged, the last successfully read config
    (cached in the state dir on every good read) is returned alongside
    no_device=true -- the dashboard renders in a degraded "no device
    detected" mode with real data instead of a full-page error.
    """
    port = detect_port()
    if not port:
        cached = None
        try:
            cached = json.loads((STATE_DIR / "last_config.json").read_text())
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass
        return jsonify({"ok": False, "no_device": True,
                        "error": "No ESP32 serial device found. Is it plugged in?",
                        "cached_config": cached}), 400

    collector.pause()
    ser = None
    try:
        ser = RawSerialPort(port, baudrate=115200, timeout=2)
        time.sleep(2)  # let the board's USB settle if it just reset

        ser.write(b'{"cmd":"get_config"}\n')

        buf = b""
        deadline = time.time() + 4
        result = None
        while time.time() < deadline and result is None:
            chunk = ser.read(256)
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    parsed = json.loads(line.decode("utf-8", errors="ignore"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(parsed, dict) and parsed.get("ack") == "get_config":
                    result = parsed
                    break

        if result is None:
            return jsonify({"ok": False, "error": "No response from device -- is it configured yet?"}), 504

        # Remember the last good read so an unplugged device still gets a
        # populated (read-only-ish) dashboard instead of an error page.
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = STATE_DIR / "last_config.json.tmp"
            tmp.write_text(json.dumps(result))
            os.replace(tmp, STATE_DIR / "last_config.json")
        except OSError:
            pass
        return jsonify({"ok": True, "config": result})
    except Exception as e:
        return _server_error("Failed to read the device configuration.", e)
    finally:
        if ser:
            try:
                ser.close()
            except Exception:
                pass
        collector.resume()


# ---------------------------------------------------------------------
# Server-side flash (same deal -- no computer/WebSerial needed)
# ---------------------------------------------------------------------

# Which firmware variant each board needs -- see platformio.ini and
# main.cpp's top-of-file note for why this split exists (ARDUINO_USB_CDC_ON_BOOT
# is a compile-time flag, so board 1's opposite USB mode needs a fully
# separate binary, not just different runtime config).
NATIVE_USB_BOARDS = {1}


def firmware_dir_for_board(board_id):
    variant = "native" if board_id in NATIVE_USB_BOARDS else "bridge"
    return FIRMWARE_DIR / variant


@app.route("/api/flash", methods=["POST"])
def api_flash():
    port = detect_port()
    if not port:
        return jsonify({"ok": False, "error": "No ESP32 serial device found. Is it plugged in?"}), 400

    body = request.get_json(silent=True) or {}
    board_id = int(body.get("board", 0))
    fw_dir = firmware_dir_for_board(board_id)

    bootloader = fw_dir / "bootloader.bin"
    partitions = fw_dir / "partitions.bin"
    firmware = fw_dir / "firmware.bin"
    missing = [f.name for f in (bootloader, partitions, firmware) if not f.exists()]
    if missing:
        return jsonify({"ok": False, "error": f"Missing firmware file(s) in {fw_dir.name}/: {', '.join(missing)}"}), 500

    collector.pause()
    try:
        result = subprocess.run(
            [
                "esptool", "--chip", "esp32s3", "--port", port, "--baud", "460800",
                "write-flash",
                "0x0", str(bootloader),
                "0x8000", str(partitions),
                "0x10000", str(firmware),
            ],
            capture_output=True, text=True, timeout=180,
        )
        log = (result.stdout or "") + "\n" + (result.stderr or "")
        ok = result.returncode == 0
        hint = None
        if not ok and ("No serial data received" in log or "Failed to connect" in log):
            hint = ("Couldn't reach the bootloader. Some boards (ones without a separate "
                    "USB-UART bridge chip) need you to hold the BOOT button while this "
                    "starts, then release it once flashing begins.")
        # Keep the outcome around for the Debugging view (View Logs).
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = STATE_DIR / "last_flash.json.tmp"
            tmp.write_text(json.dumps({"at": time.time(), "ok": ok,
                                       "board": board_id, "log": log[-8000:],
                                       "hint": hint}))
            os.replace(tmp, STATE_DIR / "last_flash.json")
        except OSError:
            pass
        return jsonify({"ok": ok, "log": log, "hint": hint})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Flash timed out after 180s"}), 500
    except Exception as e:
        return _server_error("Flashing failed unexpectedly.", e)
    finally:
        collector.resume()


# ---------------------------------------------------------------------
# HTTPS certificate management
# ---------------------------------------------------------------------
# The HTTPS listener holds a mutable SSLContext (_https_ctx below).
# Uploading a new cert/key pair calls load_cert_chain() on that same
# object, so NEW connections present the new certificate immediately --
# no restart, no dropped sessions. Validation runs the exact operation
# the server performs at startup, so what passes here is what serves.

_https_ctx = None


def cert_dir() -> Path:
    return Path(os.environ.get("TINYSCREEN_CERT_DIR", "/opt/tinyscreen/certs"))


def _harden_cert_perms():
    """Private keys are secrets: 0700 dir, 0600 files. Best-effort --
    a read-only or odd filesystem shouldn't take HTTPS down with it."""
    d = cert_dir()
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    for name in ("key.pem", "cert.pem", "key.pem.bak", "cert.pem.bak"):
        p = d / name
        if p.exists():
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass


def _openssl_cert_info(path):
    """Subject / issuer / expiry via the openssl binary (already in the
    image for self-signed generation) -- avoids a cryptography-library
    dependency for three fields."""
    try:
        out = subprocess.run(
            ["openssl", "x509", "-in", str(path), "-noout",
             "-subject", "-issuer", "-enddate"],
            capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return {}
        info = {}
        for line in out.stdout.splitlines():
            if line.startswith("subject="):
                info["subject"] = line[len("subject="):].strip()
            elif line.startswith("issuer="):
                info["issuer"] = line[len("issuer="):].strip()
            elif line.startswith("notAfter="):
                info["expires"] = line[len("notAfter="):].strip()
        info["self_signed"] = bool(info.get("subject")) and \
            info.get("subject") == info.get("issuer")
        return info
    except (subprocess.SubprocessError, OSError):
        return {}


def _validate_pair(cert_text, key_text):
    """Empty string if the PEM pair loads as a working TLS identity,
    else a human-readable reason. Catches malformed PEM and a key that
    doesn't match the certificate in one step."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        cp, kp = Path(td) / "c.pem", Path(td) / "k.pem"
        cp.write_text(cert_text)
        kp.write_text(key_text)
        try:
            probe = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            probe.load_cert_chain(str(cp), str(kp))
        except ssl.SSLError as e:
            return "OpenSSL rejected the pair: " + (getattr(e, "reason", None) or str(e))
        except (OSError, ValueError) as e:
            return str(e)
    return ""


def _reload_https_ctx():
    """Swap the live listener onto the current on-disk pair. Returns
    True when new connections will use it; False if the listener isn't
    running (started without certs) or the reload failed."""
    if _https_ctx is None:
        return False
    d = cert_dir()
    try:
        _https_ctx.load_cert_chain(str(d / "cert.pem"), str(d / "key.pem"))
        return True
    except (ssl.SSLError, OSError) as e:
        print(f"WARNING: HTTPS context reload failed: {e}")
        return False


def _install_pair(cert_text, key_text):
    """Atomic install with a .bak of whatever was serving before, then
    hot-reload. Returns whether the live listener picked it up."""
    d = cert_dir()
    d.mkdir(parents=True, exist_ok=True)
    for name, text in (("cert.pem", cert_text), ("key.pem", key_text)):
        target = d / name
        if target.exists():
            shutil.copy2(target, d / (name + ".bak"))
        tmp = d / (name + ".new")
        tmp.write_text(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    _harden_cert_perms()
    return _reload_https_ctx()


def _generate_self_signed(into_dir):
    """Same recipe as entrypoint.sh's first-run certificate."""
    subprocess.run(
        ["openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048",
         "-keyout", str(Path(into_dir) / "key.pem"),
         "-out", str(Path(into_dir) / "cert.pem"),
         "-days", "3650", "-subj", "/CN=tinyscreen-dashboard",
         "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
        capture_output=True, timeout=60, check=True)


@app.route("/api/cert_info", methods=["GET"])
def api_cert_info():
    d = cert_dir()
    cp = d / "cert.pem"
    if not cp.exists():
        return jsonify({"ok": True, "present": False,
                        "https_running": _https_ctx is not None})
    return jsonify({"ok": True, "present": True,
                    "https_running": _https_ctx is not None,
                    "has_backup": (d / "cert.pem.bak").exists(),
                    **_openssl_cert_info(cp)})


@app.route("/api/upload_cert", methods=["POST"])
def api_upload_cert():
    # Finding 6: a private key sent over plain HTTP (8989) travels the LAN
    # unencrypted. Only accept uploads on the HTTPS listener (8990), where
    # the key is protected in transit -- which is also where the dashboard
    # naturally lives once you're managing certs. certbot hooks should
    # target https://host:8990/api/upload_cert for the same reason.
    if not _request_is_https():
        return jsonify({
            "ok": False,
            "error": ("For your key's safety, certificate upload is only "
                      "accepted over HTTPS (port 8990), so the private key "
                      "isn't sent across your network in the clear. Reopen "
                      "the dashboard at https://<this-host>:8990 and retry."),
        }), 403
    # Two shapes: JSON {"cert": ..., "key": ...} from the dashboard, or
    # multipart files named cert/key -- the latter so a certbot
    # deploy-hook can curl renewals straight in.
    cert_text = key_text = None
    if request.files:
        cf, kf = request.files.get("cert"), request.files.get("key")
        if cf:
            cert_text = cf.read().decode("utf-8", "replace")
        if kf:
            key_text = kf.read().decode("utf-8", "replace")
    else:
        body = request.get_json(silent=True) or {}
        cert_text = body.get("cert")
        key_text = body.get("key")
    if not cert_text or not key_text:
        return jsonify({"ok": False, "error": "Need both a certificate and a private key (PEM)."}), 400
    if len(cert_text) > 65536 or len(key_text) > 65536:
        return jsonify({"ok": False, "error": "That file is too large to be a PEM cert or key."}), 400
    err = _validate_pair(cert_text, key_text)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    reloaded = _install_pair(cert_text, key_text)
    return jsonify({"ok": True, "hot_reloaded": reloaded,
                    **_openssl_cert_info(cert_dir() / "cert.pem")})


@app.route("/api/reset_cert", methods=["POST"])
def api_reset_cert():
    """Back to a fresh self-signed pair (the previous pair is kept as
    .bak, same as any other install)."""
    import tempfile
    try:
        with tempfile.TemporaryDirectory() as td:
            _generate_self_signed(td)
            cert_text = (Path(td) / "cert.pem").read_text()
            key_text = (Path(td) / "key.pem").read_text()
    except (subprocess.SubprocessError, OSError) as e:
        return jsonify({"ok": False, "error": f"openssl generation failed: {e}"}), 500
    reloaded = _install_pair(cert_text, key_text)
    return jsonify({"ok": True, "hot_reloaded": reloaded,
                    **_openssl_cert_info(cert_dir() / "cert.pem")})


# ---------------------------------------------------------------------
# Entrypoint: run both HTTP and HTTPS listeners in one process
# ---------------------------------------------------------------------

# --- Production WSGI server (0.9.3) -----------------------------------
# Both listeners used to run on Flask's built-in development server
# (app.run / Werkzeug), which Werkzeug's own docs say not to deploy: no
# request timeouts, weaker connection handling, and a "this is a
# development server" warning on every boot. cheroot (the production
# server underneath CherryPy) replaces it on both ports. cheroot was
# picked over the more common waitress for exactly one reason: it
# terminates TLS natively via a pluggable ssl.SSLContext, which preserves
# the certificate hot-swap trick -- _reload_https_ctx() calls
# load_cert_chain() on the very SSLContext object the listener wraps new
# connections with, so an uploaded certificate takes effect immediately,
# no restart. waitress has no TLS support at all.
#
# cheroot is imported lazily inside these functions (not at module top)
# so the test suite -- which imports this module for its Flask routes but
# never starts a real listener -- keeps working in environments where
# only Flask is installed.

def run_http():
    from cheroot import wsgi
    server = wsgi.Server(("0.0.0.0", 8989), app, server_name="tinyscreen")
    server.start()


def run_https():
    global _https_ctx
    d = cert_dir()
    cert_path, key_path = d / "cert.pem", d / "key.pem"
    if not (cert_path.exists() and key_path.exists()):
        print(f"WARNING: cert/key not found at {d}; HTTPS listener (8990) not started. "
              f"The WebSerial flasher pages need HTTPS to work.")
        return
    _harden_cert_perms()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        ctx.load_cert_chain(str(cert_path), str(key_path))
    except (ssl.SSLError, OSError) as e:
        print(f"WARNING: could not load the certificate pair ({e}); "
              f"HTTPS listener (8990) not started.")
        return
    _https_ctx = ctx  # uploads hot-swap the chain on this exact object

    from cheroot import wsgi
    from cheroot.ssl.builtin import BuiltinSSLAdapter
    server = wsgi.Server(("0.0.0.0", 8990), app, server_name="tinyscreen")
    # BuiltinSSLAdapter builds its own context from the file paths; we
    # immediately replace it with OUR context object so the existing
    # hot-swap path (_reload_https_ctx -> _https_ctx.load_cert_chain)
    # keeps affecting live connections exactly as before. The adapter
    # also makes cheroot set wsgi.url_scheme = "https", which
    # _request_is_https() relies on.
    adapter = BuiltinSSLAdapter(str(cert_path), str(key_path))
    adapter.context = ctx
    server.ssl_adapter = adapter
    server.start()


if __name__ == "__main__":
    https_thread = threading.Thread(target=run_https, daemon=True)
    https_thread.start()
    run_http()  # main thread
