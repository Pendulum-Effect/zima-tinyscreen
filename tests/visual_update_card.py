#!/usr/bin/env python3
"""
Visual check of the dashboard's Software Version card states, served from
a REAL local HTTP server (never file:// -- fetch is blocked there and
produces misleading failures). A stub API mimics server.py's endpoints,
including a scripted /api/update_app run-through: pulling -> swapping ->
(app "down": stub 503s everything briefly) -> success on a new commit.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "webflasher"
SHOTS = Path("/tmp/shots")
SHOTS.mkdir(exist_ok=True)

SCENARIO = {"name": "ready", "update_started_at": None}

BASE_CONFIG = {"ok": True, "config": {
    "board": 1, "board_name": "ESP32-S3-Touch-LCD-1.69", "configured": True,
    "firmware_version": "1.1.0", "has_touch": True,
    "pages": ["temp", "cpu"],
    "cycle_mode": "static", "cycle_seconds": 10, "brightness": 100,
    "night_enabled": True, "night_start_min": 1320, "night_end_min": 420,
    "night_brightness": 15, "tz_offset_min": -300,
    "saver_enabled": True, "saver_minutes": 10, "saver_style": "clock"}}
LAST_CONFIGURE_BODY = {}
BASE_STATUS = {"ok": True, "collector_running": True, "age_seconds": 1.0,
               "last_stats": {}, "hostname": "ZimaBlade"}
FW_INFO = {"ok": True, "bundled_version": "1.1.0"}


def app_version():
    s = SCENARIO["name"]
    base = {"ok": True, "version": "0.8.6.2", "current_commit": "a" * 40,
            "latest_commit": "b" * 40, "latest_version": "0.8.7.0",
            "update_available": True, "update_ready": True}
    if s == "uptodate":
        base.update(latest_commit="a" * 40, update_available=False,
                    latest_version="0.8.6.2")
    elif s == "not_ready":
        base.update(update_ready=False)
    elif s == "updating" and SCENARIO["update_started_at"]:
        elapsed = time.time() - SCENARIO["update_started_at"]
        if elapsed > 9:  # new build answering
            base.update(version="0.8.7.0", current_commit="b" * 40,
                        update_available=False)
    return base


def update_state():
    s = SCENARIO["name"]
    if s == "rolled_back":
        return {"ok": True, "state": {"status": "rolled_back",
                "reason": "new container never became healthy: probe exit 1"}}
    if s == "updating" and SCENARIO["update_started_at"]:
        elapsed = time.time() - SCENARIO["update_started_at"]
        if elapsed < 3:
            return {"ok": True, "state": {"status": "pulling"}}
        if elapsed < 6:
            return {"ok": True, "state": {"status": "swapping"}}
        if elapsed < 9:
            return None  # simulate the app being down mid-swap
        return {"ok": True, "state": {"status": "success"}}
    return {"ok": True, "state": None}


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/"):
            route = self.path.split("?")[0]
            if route == "/api/update_state":
                st = update_state()
                if st is None:
                    return self._send({"error": "down"}, 503)
                return self._send(st)
            if route == "/api/about":
                about = json.loads((ROOT / "about.json").read_text())
                changelog = json.loads((ROOT / "CHANGELOG.json").read_text())
                return self._send({"ok": True, "about": about,
                                   "changelog": changelog,
                                   "source": {"about": "github", "changelog": "github"}})
            mapping = {"/api/current_config": BASE_CONFIG,
                       "/api/status": BASE_STATUS,
                       "/api/firmware_info": FW_INFO,
                       "/api/app_version": app_version()}
            if route in mapping:
                return self._send(mapping[route])
            return self._send({"ok": False}, 404)
        f = WEB / self.path.lstrip("/")
        if f.is_file():
            ctype = "text/html" if f.suffix == ".html" else "application/octet-stream"
            return self._send(f.read_bytes(), ctype=ctype)
        self._send({"ok": False}, 404)

    def do_POST(self):
        if self.path == "/api/reset_device":
            SCENARIO["reset_called"] = True
            return self._send({"ok": True, "acked": True})
        if self.path == "/api/configure":
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else {}
            LAST_CONFIGURE_BODY.clear()
            LAST_CONFIGURE_BODY.update(body)
            return self._send({"ok": True, "acked": True})
        if self.path == "/api/update_app":
            SCENARIO["update_started_at"] = time.time()
            return self._send({"ok": True, "status": "started"})
        self._send({"ok": False}, 404)


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 8765), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        def load():
            page.goto("http://127.0.0.1:8765/dashboard.html")
            page.wait_for_selector("#general-content", state="visible", timeout=10000)
            page.wait_for_function(
                "document.getElementById('app-version-status').textContent !== 'Checking…'",
                timeout=10000)
            page.wait_for_timeout(300)  # settle secondary fetches

        def shot(name):
            load()
            page.screenshot(path=str(SHOTS / f"{name}.png"))
            print("shot:", name)

        SCENARIO["name"] = "uptodate"; shot("1_uptodate")
        SCENARIO["name"] = "ready"; shot("2_update_available_ready")
        SCENARIO["name"] = "not_ready"; shot("3_update_available_not_ready")
        SCENARIO["name"] = "rolled_back"; shot("4_last_attempt_rolled_back")

        # full click-through: arm -> confirm -> pulling -> down -> success
        SCENARIO["name"] = "ready"
        load()
        SCENARIO["name"] = "updating"
        page.click("#update-app-btn")
        page.wait_for_timeout(400)
        page.screenshot(path=str(SHOTS / "5_armed_confirm.png")); print("shot: 5_armed_confirm")
        page.click("#update-app-btn")
        page.wait_for_timeout(2000)
        page.screenshot(path=str(SHOTS / "6_pulling.png")); print("shot: 6_pulling")
        page.wait_for_timeout(5000)
        page.screenshot(path=str(SHOTS / "7_waiting_through_gap.png")); print("shot: 7_waiting_through_gap")
        page.wait_for_timeout(6000)
        page.screenshot(path=str(SHOTS / "8_success.png")); print("shot: 8_success")

        browser.close()
    srv.shutdown()


if __name__ == "__main__":
    main()
