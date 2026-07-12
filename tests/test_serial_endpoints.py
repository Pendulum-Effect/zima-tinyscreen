#!/usr/bin/env python3
"""
Serial-endpoint tests: /api/flash, /api/configure, /api/reset_device --
the three endpoints that take EXCLUSIVE control of the serial port and
orchestrate the collector pause/resume dance around it. Same philosophy
as the fake-dockerd updater tests: exercise the real wire, not mocks.

  - The "device" is a real pty pair. The server opens the slave side
    exactly like a physical board's /dev/ttyACM0 -- including running
    its actual `stty raw -echo` configuration on it -- while a device
    thread on the master side parses the line-delimited JSON commands
    and answers with the firmware's real ack lines.
  - esptool is a fake executable placed at the front of PATH that
    records its argv and produces scripted success/failure output, so
    the flash endpoint runs its genuine subprocess path.
  - The collector is the real CollectorManager, its script swapped for
    an inert sleeper so is_running() is deterministic. pause()/resume()
    are wrapped (delegating to the real methods) to append to an events
    log the fake esptool and the fake device also append to -- so tests
    assert the exclusive-access CHOREOGRAPHY by ordering, not just
    return codes: pause -> serial work -> resume, every time, success
    or failure.

What still needs real hardware: the board actually surviving/booting a
flash, and native-USB re-enumeration quirks. Everything protocol- and
lifecycle-side is covered here.

Run:  python3 tests/test_serial_endpoints.py
"""

import json
import os
import select
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

# The server reads these env vars at import time -- set them first.
_TMP = tempfile.TemporaryDirectory(prefix="tinyscreen-serial-tests-")
TMP = Path(_TMP.name)
os.environ["TINYSCREEN_STATUS_FILE"] = str(TMP / "status.json")
os.environ["TINYSCREEN_STATE_DIR"] = str(TMP / "state")
os.environ.pop("TINYSCREEN_SERIAL_PORT", None)

import server  # noqa: E402

EVENTS_FILE = TMP / "events.log"
ARGS_FILE = TMP / "esptool_args.json"


# ---------------------------------------------------------------------
# Module-level fixtures
# ---------------------------------------------------------------------

def setUpModule():
    """Swap the collector script for an inert sleeper (so is_running()
    is deterministic -- the real collector would exit instantly with no
    serial device, or worse, start streaming this test machine's stats
    into our pty), install the fake esptool on PATH, and wrap
    pause/resume with event logging."""
    sleeper = TMP / "sleeper_collector.py"
    sleeper.write_text("import time\ntime.sleep(3600)\n")
    server.COLLECTOR_SCRIPT = sleeper

    bindir = TMP / "bin"
    bindir.mkdir()
    fake = bindir / "esptool"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['FAKE_ESPTOOL_ARGS'], 'w') as f:\n"
        "    json.dump(sys.argv[1:], f)\n"
        "with open(os.environ['FAKE_ESPTOOL_EVENTS'], 'a') as f:\n"
        "    f.write('esptool\\n')\n"
        "mode = os.environ.get('FAKE_ESPTOOL_MODE', 'ok')\n"
        "if mode == 'ok':\n"
        "    print('Writing at 0x00010000... (100 %)')\n"
        "    print('Hash of data verified.')\n"
        "    sys.exit(0)\n"
        "if mode == 'fail_connect':\n"
        "    print('A fatal error occurred: Failed to connect to ESP32-S3: '\n"
        "          'No serial data received.', file=sys.stderr)\n"
        "    sys.exit(2)\n"
        "print('A fatal error occurred: something else broke', file=sys.stderr)\n"
        "sys.exit(1)\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.environ["PATH"] = f"{bindir}:{os.environ['PATH']}"
    os.environ["FAKE_ESPTOOL_EVENTS"] = str(EVENTS_FILE)
    os.environ["FAKE_ESPTOOL_ARGS"] = str(ARGS_FILE)

    # Wrap the real pause/resume on the real singleton -- delegation,
    # not replacement, so the genuine terminate/depth logic still runs.
    orig_pause, orig_resume = server.collector.pause, server.collector.resume

    def pause():
        _log_event("pause")
        orig_pause()

    def resume():
        orig_resume()
        _log_event("resume")

    server.collector.pause = pause
    server.collector.resume = resume


def _log_event(name):
    with open(EVENTS_FILE, "a") as f:
        f.write(name + "\n")


def _read_events():
    try:
        return EVENTS_FILE.read_text().split()
    except FileNotFoundError:
        return []


def _clear_events():
    EVENTS_FILE.write_text("")
    if ARGS_FILE.exists():
        ARGS_FILE.unlink()


# ---------------------------------------------------------------------
# The fake device: a pty pair with a firmware-shaped responder thread
# ---------------------------------------------------------------------

class FakeDevice:
    """Owns the master side of a pty whose slave path the server treats
    as the board's serial device. Parses line-delimited JSON off the
    wire; lines with a "cmd" field are recorded and answered with the
    firmware's real ack shape (unless ack_mode says otherwise). Non-cmd
    lines (stats traffic) are ignored, like real firmware ignores its
    own input echo."""

    # A realistic firmware 1.18-shaped config, inlined into get_config
    # acks -- board 1 with a few pages, so dashboard-facing tests render
    # a believable device.
    DEFAULT_CONFIG = {
        "board": 1, "board_name": "ESP32-S3-TOUCH-1.69", "has_touch": True,
        "pages": ["cpu", "temp", "net"], "layouts": {"cpu": "ring"},
        "cycle_mode": "auto", "cycle_seconds": 10, "brightness": 90,
        "rotation": 0, "square_fit": False,
        "night_enabled": False, "night_start_min": 1320,
        "night_end_min": 420, "night_brightness": 10,
        "saver_enabled": False, "saver_minutes": 5, "saver_style": "clock",
        "saver_brightness": 30, "configured": True, "aspect_mode": 0,
        "firmware_version": "1.18.0",
    }

    def __init__(self, ack_mode="ack", device_config=None):
        self.device_config = dict(device_config or self.DEFAULT_CONFIG)
        self.master_fd, self.slave_fd = os.openpty()
        self.port = os.ttyname(self.slave_fd)
        self.ack_mode = ack_mode  # "ack" | "silent" | "garbage"
        self.commands = []
        self._stop = threading.Event()
        self._buf = b""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            r, _, _ = select.select([self.master_fd], [], [], 0.1)
            if not r:
                continue
            try:
                chunk = os.read(self.master_fd, 1024)
            except OSError:
                break
            if not chunk:
                continue
            self._buf += chunk
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                self._handle(line)

    def _handle(self, line):
        try:
            msg = json.loads(line.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(msg, dict) or "cmd" not in msg:
            return  # stats line or noise -- firmware ignores these too
        self.commands.append(msg)
        _log_event("device-got-" + str(msg["cmd"]))
        if self.ack_mode == "ack":
            # COMPACT separators are part of the wire contract: real
            # firmware serializes acks via ArduinoJson, which emits no
            # whitespace ({"ack":"set_config","ok":true}), and the
            # server's ack detection is an exact substring match on that
            # compact form. Python's json.dumps default (space after
            # colons) is NOT what a real board sends -- using it here
            # made the server correctly report acked:false.
            reply = {"ack": msg["cmd"], "ok": True}
            if msg["cmd"] == "get_config":
                # Real firmware inlines the whole config in the ack line,
                # and api_current_config caches that entire dict as
                # last_config.json -- the shape the dashboard renders
                # from when the device is unplugged.
                reply.update(self.device_config)
            os.write(self.master_fd,
                     (json.dumps(reply, separators=(",", ":")) + "\n").encode())
        elif self.ack_mode == "garbage":
            os.write(self.master_fd, b'{"note":"not an ack"}\n\xff\xfe junk\n')
        # "silent": say nothing; the endpoint should time out its ack
        # wait and still report ok:true acked:false.

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        for fd in (self.master_fd, self.slave_fd):
            try:
                os.close(fd)
            except OSError:
                pass


# ---------------------------------------------------------------------
# Shared test plumbing
# ---------------------------------------------------------------------

class SerialTestBase(unittest.TestCase):
    def setUp(self):
        _clear_events()
        os.environ["FAKE_ESPTOOL_MODE"] = "ok"
        self.client = server.app.test_client()

    def tearDown(self):
        os.environ.pop("TINYSCREEN_SERIAL_PORT", None)

    def post(self, path, payload=None, **kw):
        """POST with the anti-CSRF custom header, like the dashboard's
        own fetch() calls (and scripted clients) do."""
        return self.client.post(path, json=payload or {},
                                headers={server.CUSTOM_REQUEST_HEADER: "1"}, **kw)

    def assert_choreography(self, *middle):
        """The invariant every exclusive-serial request must uphold:
        pause first, the serial work strictly in between, resume last --
        and depth back to zero so the watchdog can respawn the collector."""
        events = _read_events()
        expected = ["pause", *middle, "resume"]
        self.assertEqual(events, expected,
                         f"exclusive-access order violated: {events}")
        self.assertEqual(server.collector._pause_depth, 0)
        self.assertFalse(server.collector.paused)


def _make_firmware_tree():
    """A fake bundled-firmware directory shaped like CI's staging."""
    fwroot = TMP / f"firmware-{time.time_ns()}"
    for variant in ("bridge", "native"):
        d = fwroot / variant
        d.mkdir(parents=True)
        for name in ("bootloader.bin", "partitions.bin", "firmware.bin"):
            (d / name).write_bytes(b"\xe9fake-" + variant.encode())
    return fwroot


# ---------------------------------------------------------------------
# /api/flash
# ---------------------------------------------------------------------

class TestFlash(SerialTestBase):
    def setUp(self):
        super().setUp()
        self.dev = FakeDevice()
        os.environ["TINYSCREEN_SERIAL_PORT"] = self.dev.port
        self._orig_fw_dir = server.FIRMWARE_DIR
        server.FIRMWARE_DIR = _make_firmware_tree()

    def tearDown(self):
        server.FIRMWARE_DIR = self._orig_fw_dir
        self.dev.close()
        super().tearDown()

    def test_happy_path_board0_bridge(self):
        r = self.post("/api/flash", {"board": 0})
        body = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(body["ok"])
        self.assertIsNone(body["hint"])
        self.assertIn("Hash of data verified", body["log"])
        args = json.loads(ARGS_FILE.read_text())
        # The exact esptool v5 invocation contract: chip, OUR pty as the
        # port, dashed write-flash, and the three offset/file pairs in
        # order, pointing into the bridge/ variant for board 0.
        self.assertEqual(args[:4], ["--chip", "esp32s3", "--port", self.dev.port])
        self.assertIn("write-flash", args)
        wf = args.index("write-flash")
        offsets = args[wf + 1::2][:3]
        files = args[wf + 2::2][:3]
        self.assertEqual(offsets, ["0x0", "0x8000", "0x10000"])
        self.assertEqual([Path(f).name for f in files],
                         ["bootloader.bin", "partitions.bin", "firmware.bin"])
        self.assertTrue(all("/bridge/" in f for f in files), files)
        self.assert_choreography("esptool")

    def test_board1_selects_native_variant(self):
        r = self.post("/api/flash", {"board": 1})
        self.assertTrue(r.get_json()["ok"])
        files = [a for a in json.loads(ARGS_FILE.read_text()) if a.endswith(".bin")]
        self.assertTrue(all("/native/" in f for f in files), files)

    def test_failure_with_boot_button_hint(self):
        os.environ["FAKE_ESPTOOL_MODE"] = "fail_connect"
        r = self.post("/api/flash", {"board": 1})
        body = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertFalse(body["ok"])
        self.assertIn("BOOT button", body["hint"])
        # Failure must NOT leak the pause: collector comes back either way.
        self.assert_choreography("esptool")

    def test_generic_failure_has_no_hint(self):
        os.environ["FAKE_ESPTOOL_MODE"] = "fail_generic"
        body = self.post("/api/flash", {"board": 0}).get_json()
        self.assertFalse(body["ok"])
        self.assertIsNone(body["hint"])

    def test_outcome_persisted_for_debugging_view(self):
        self.post("/api/flash", {"board": 1})
        flash = self.client.get("/api/last_flash").get_json()["flash"]
        self.assertTrue(flash["ok"])
        self.assertEqual(flash["board"], 1)
        self.assertIn("Hash of data verified", flash["log"])
        self.assertLessEqual(len(flash["log"]), 8000)

    def test_missing_firmware_files_is_500_not_flash_attempt(self):
        for name in ("partitions.bin", "firmware.bin"):
            (server.FIRMWARE_DIR / "bridge" / name).unlink()
        r = self.post("/api/flash", {"board": 0})
        body = r.get_json()
        self.assertEqual(r.status_code, 500)
        self.assertFalse(body["ok"])
        self.assertIn("partitions.bin", body["error"])
        self.assertIn("firmware.bin", body["error"])
        # Refused before touching the port: no pause, no esptool run.
        self.assertEqual(_read_events(), [])

    def test_no_device_is_400(self):
        os.environ.pop("TINYSCREEN_SERIAL_PORT", None)
        orig = server.glob.glob
        server.glob.glob = lambda pattern: []
        try:
            r = self.post("/api/flash", {"board": 0})
        finally:
            server.glob.glob = orig
        self.assertEqual(r.status_code, 400)
        self.assertIn("plugged in", r.get_json()["error"])
        self.assertEqual(_read_events(), [])


# ---------------------------------------------------------------------
# /api/configure and /api/reset_device
# ---------------------------------------------------------------------

class TestConfigure(SerialTestBase):
    def _with_device(self, ack_mode="ack"):
        dev = FakeDevice(ack_mode=ack_mode)
        os.environ["TINYSCREEN_SERIAL_PORT"] = dev.port
        self.addCleanup(dev.close)
        return dev

    def test_happy_path_full_wire_round_trip(self):
        dev = self._with_device()
        r = self.post("/api/configure", {
            "board": 1,
            "pages": ["cpu", "temp", "net"],
            "layouts": {"cpu": "ring", "net": "graph"},
            "cycle_mode": "auto",
            "cycle_seconds": "15",       # string on purpose: must cast
            "brightness": 80,
            "night_enabled": True,
            "night_start_min": 1320,
            "tz_name": "America/Chicago",
        })
        body = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["acked"])

        # What the DEVICE actually received over the wire:
        self.assertEqual(len(dev.commands), 1)
        cmd = dev.commands[0]
        self.assertEqual(cmd["cmd"], "set_config")
        self.assertEqual(cmd["board"], 1)
        self.assertEqual(cmd["pages"], ["cpu", "temp", "net"])
        self.assertEqual(cmd["cycle_seconds"], 15)      # cast to int
        self.assertEqual(cmd["layouts"], {"cpu": "ring", "net": "graph"})
        self.assertEqual(cmd["night_start_min"], 1320)
        self.assertNotIn("saver_enabled", cmd)  # absent field NOT sent --
        # the firmware's only-update-what-appears semantics depend on it
        self.assertNotIn("tz_name", cmd)  # server-side state, never serial

        # Timezone landed in the state dir for the collector:
        tz = (Path(os.environ["TINYSCREEN_STATE_DIR"]) / "timezone.txt").read_text()
        self.assertEqual(tz.strip(), "America/Chicago")

        self.assert_choreography("device-got-set_config")

    def test_silent_device_reports_unacked_not_error(self):
        self._with_device(ack_mode="silent")
        body = self.post("/api/configure", {"board": 0, "pages": ["temp"]}).get_json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["acked"])
        self.assert_choreography("device-got-set_config")

    def test_garbage_reply_is_not_mistaken_for_ack(self):
        self._with_device(ack_mode="garbage")
        body = self.post("/api/configure", {"board": 0, "pages": ["temp"]}).get_json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["acked"])

    def test_reset_device_sends_clear_config(self):
        dev = self._with_device()
        body = self.post("/api/reset_device").get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["acked"])
        self.assertEqual(dev.commands, [{"cmd": "clear_config"}])
        self.assert_choreography("device-got-clear_config")

    def test_invalid_timezone_rejected_valid_kept(self):
        dev = self._with_device()
        tz_file = Path(os.environ["TINYSCREEN_STATE_DIR"]) / "timezone.txt"
        self.post("/api/configure", {"board": 0, "pages": ["temp"],
                                     "tz_name": "Europe/Paris"})
        self.assertEqual(tz_file.read_text().strip(), "Europe/Paris")
        # A hostile / malformed name must not clobber the stored zone.
        for bad in ("../../../etc/passwd", "America/Chicago/../..",
                    "not a zone", "", 42, {"x": 1}):
            self.post("/api/configure", {"board": 0, "pages": ["temp"],
                                         "tz_name": bad})
            self.assertEqual(tz_file.read_text().strip(), "Europe/Paris",
                             f"tz file clobbered by {bad!r}")
        self.assertEqual(len(dev.commands), 7)  # serial part still ran each time

    def test_concurrent_exclusive_operations_get_409_not_corruption(self):
        """0.9.5.2: two simultaneous exclusive-serial requests (two
        browser tabs, or spam during a real operation) must not share
        the port -- exactly one runs, the other gets an immediate 409,
        and exactly one pause/resume cycle happens."""
        dev = self._with_device()
        results = []
        def hit():
            c = server.app.test_client()
            r = c.post("/api/configure", json={"board": 0, "pages": ["temp"]},
                       headers={server.CUSTOM_REQUEST_HEADER: "1"})
            results.append(r)
        t1 = threading.Thread(target=hit)
        t2 = threading.Thread(target=hit)
        t1.start(); time.sleep(0.4); t2.start()
        t1.join(timeout=30); t2.join(timeout=30)

        statuses = sorted(r.status_code for r in results)
        self.assertEqual(statuses, [200, 409], statuses)
        loser = next(r for r in results if r.status_code == 409)
        self.assertTrue(loser.get_json()["busy"])
        winner = next(r for r in results if r.status_code == 200)
        self.assertTrue(winner.get_json()["acked"])
        # One command reached the device, one pause/resume cycle total,
        # and the lock is free again afterwards.
        self.assertEqual(len(dev.commands), 1)
        self.assert_choreography("device-got-set_config")
        self.assertTrue(server._exclusive_serial.acquire(blocking=False))
        server._exclusive_serial.release()

    def test_no_device_is_400(self):
        os.environ.pop("TINYSCREEN_SERIAL_PORT", None)
        orig = server.glob.glob
        server.glob.glob = lambda pattern: []
        try:
            r = self.post("/api/configure", {"board": 0})
        finally:
            server.glob.glob = orig
        self.assertEqual(r.status_code, 400)
        self.assertEqual(_read_events(), [])

    def test_csrf_guard_covers_serial_endpoints(self):
        """A forged cross-site form POST (no custom header, no
        same-origin Origin/Referer) must be refused BEFORE any serial
        or collector work happens."""
        dev = self._with_device()
        for path in ("/api/configure", "/api/flash", "/api/reset_device"):
            r = self.client.post(path, json={"board": 0})
            self.assertEqual(r.status_code, 403, path)
        self.assertEqual(_read_events(), [])
        self.assertEqual(dev.commands, [])


# ---------------------------------------------------------------------
# CollectorManager lifecycle (no HTTP): depth counting + respawn
# ---------------------------------------------------------------------

class TestCurrentConfig(SerialTestBase):
    """/api/current_config (GET, but takes exclusive serial access):
    the read path the dashboard renders from, plus the last-good-read
    cache that keeps the dashboard browsable when the device is
    unplugged. Had NO coverage until 0.9.6 -- and the dashboard side of
    the cache contract had been broken (const reassignment) for several
    releases without anything noticing."""

    def _with_device(self, **kw):
        dev = FakeDevice(**kw)
        os.environ["TINYSCREEN_SERIAL_PORT"] = dev.port
        self.addCleanup(dev.close)
        return dev

    def test_happy_path_reads_and_caches(self):
        dev = self._with_device()
        r = self.client.get("/api/current_config")
        body = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(body["ok"])
        cfg = body["config"]
        self.assertEqual(cfg["board"], 1)
        self.assertEqual(cfg["pages"], ["cpu", "temp", "net"])
        self.assertEqual(dev.commands, [{"cmd": "get_config"}])
        self.assert_choreography("device-got-get_config")
        # The ENTIRE ack dict is the cache -- the dashboard's unplugged
        # mode renders from exactly this file.
        cached = json.loads(
            (Path(os.environ["TINYSCREEN_STATE_DIR"]) / "last_config.json").read_text())
        self.assertEqual(cached, cfg)

    def test_unplugged_serves_cache_with_no_device_flag(self):
        self._with_device()
        self.client.get("/api/current_config")  # warm the cache
        _clear_events()
        os.environ.pop("TINYSCREEN_SERIAL_PORT", None)
        orig = server.glob.glob
        server.glob.glob = lambda pattern: []
        try:
            r = self.client.get("/api/current_config")
        finally:
            server.glob.glob = orig
        body = r.get_json()
        self.assertEqual(r.status_code, 400)
        self.assertFalse(body["ok"])
        self.assertTrue(body["no_device"])
        self.assertEqual(body["cached_config"]["board"], 1)
        self.assertEqual(body["cached_config"]["pages"], ["cpu", "temp", "net"])
        self.assertEqual(_read_events(), [])  # no serial work, no pause

    def test_unplugged_with_no_cache(self):
        os.environ.pop("TINYSCREEN_SERIAL_PORT", None)
        cache = Path(os.environ["TINYSCREEN_STATE_DIR"]) / "last_config.json"
        cache.unlink(missing_ok=True)
        orig = server.glob.glob
        server.glob.glob = lambda pattern: []
        try:
            body = self.client.get("/api/current_config").get_json()
        finally:
            server.glob.glob = orig
        self.assertTrue(body["no_device"])
        self.assertIsNone(body["cached_config"])


class TestCollectorLifecycle(unittest.TestCase):
    def test_overlapping_pauses_and_watchdog_respawn(self):
        c = server.collector
        # Let the watchdog bring the sleeper collector up first.
        deadline = time.time() + 15
        while not c.is_running() and time.time() < deadline:
            time.sleep(0.25)
        self.assertTrue(c.is_running(), "watchdog never spawned the collector")

        # Two overlapping exclusive users (e.g. concurrent flash +
        # configure): the first resume must NOT un-pause.
        c.pause()
        self.assertFalse(c.is_running())
        c.pause()
        c.resume()
        self.assertTrue(c.paused, "first resume cleared an outstanding pause")
        self.assertFalse(c.is_running())
        c.resume()
        self.assertFalse(c.paused)

        # And the watchdog notices within one tick and respawns.
        deadline = time.time() + 15
        while not c.is_running() and time.time() < deadline:
            time.sleep(0.25)
        self.assertTrue(c.is_running(), "collector not respawned after resume")


# ---------------------------------------------------------------------
# Pure payload/validation units (no pty, no timing)
# ---------------------------------------------------------------------

class TestPayloadBuilding(unittest.TestCase):
    def test_defaults(self):
        # NO board in the defaults (0.9.7.4): a boardless configure must
        # never tell the device it's board 0 -- that pin profile kills
        # USB on other boards. Only the wizard names boards.
        p = server.build_set_config_payload({})
        self.assertEqual(p, {"cmd": "set_config", "pages": ["temp"],
                             "cycle_mode": "static", "cycle_seconds": 10,
                             "brightness": 100})
        p = server.build_set_config_payload({"board": 1})
        self.assertEqual(p["board"], 1)

    def test_optional_fields_pass_through_only_if_present(self):
        p = server.build_set_config_payload({"saver_enabled": 1, "rotation": "180"})
        self.assertIs(p["saver_enabled"], True)
        self.assertEqual(p["rotation"], 180)
        for absent in ("night_enabled", "night_start_min", "tz_offset_min",
                       "saver_minutes", "saver_brightness", "square_fit"):
            self.assertNotIn(absent, p)
        # saver_brightness (firmware 1.19): int passthrough like the rest
        p = server.build_set_config_payload({"saver_brightness": "45"})
        self.assertEqual(p["saver_brightness"], 45)
        # aspect_mode (firmware 1.22): int passthrough, absent by default
        p = server.build_set_config_payload({"aspect_mode": 2})
        self.assertEqual(p["aspect_mode"], 2)
        self.assertNotIn("aspect_mode", server.build_set_config_payload({}))

    def test_layouts_mapping_stringified_and_non_dict_dropped(self):
        p = server.build_set_config_payload({"layouts": {1: 2, "cpu": "ring"}})
        self.assertEqual(p["layouts"], {"1": "2", "cpu": "ring"})
        p = server.build_set_config_payload({"layouts": ["cpu", "ring"]})
        self.assertNotIn("layouts", p)

    def test_timezone_name_shape(self):
        good = ["UTC", "America/Chicago", "Europe/Paris", "Etc/GMT+5",
                "America/Argentina/Buenos_Aires", "America/Port-au-Prince"]
        bad = ["../etc", "America/../Chicago", "a/b/c/d", "with space",
               "/leading", "trailing/", ""]
        for name in good:
            self.assertTrue(server._TZ_NAME_RE.match(name), name)
        for name in bad:
            self.assertFalse(server._TZ_NAME_RE.match(name), name)


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=1).result
    ok = result.wasSuccessful()
    # Leave the collector permanently paused so the sleeper child is
    # terminated and the watchdog won't respawn it -- otherwise an
    # orphaned child inherits our stdout and holds any pipe this suite
    # is being run through open long after we exit.
    server.collector.pause()
    print("ALL SERIAL ENDPOINT TESTS PASS" if ok else "SERIAL ENDPOINT TESTS FAILED")
    sys.exit(0 if ok else 1)
