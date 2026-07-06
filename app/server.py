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
import json
import os
import sys
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

APP_DIR = Path(__file__).resolve().parent
WEBFLASHER_DIR = APP_DIR.parent / "webflasher"
FIRMWARE_DIR = WEBFLASHER_DIR / "firmware"
COLLECTOR_SCRIPT = APP_DIR.parent / "collector" / "stats_collector.py"
STATUS_FILE = Path(os.environ.get("TINYSCREEN_STATUS_FILE", "/tmp/tinyscreen_status.json"))

app = Flask(__name__, static_folder=None)


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

@app.route("/")
@app.route("/<path:filename>")
def serve_flasher(filename="index.html"):
    return send_from_directory(WEBFLASHER_DIR, filename)


@app.route("/firmware/<path:filename>")
def serve_firmware(filename):
    return send_from_directory(FIRMWARE_DIR, filename)


# ---------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------

@app.route("/api/status")
def status():
    if not STATUS_FILE.exists():
        return jsonify({"collector_running": False, "last_stats": None})
    try:
        data = json.loads(STATUS_FILE.read_text())
    except Exception:
        data = None
    age = time.time() - STATUS_FILE.stat().st_mtime
    return jsonify({
        "collector_running": age < 10,
        "last_stats": data,
        "age_seconds": age,
    })


# ---------------------------------------------------------------------
# Server-side configure (board plugged into this ZimaBlade, not a computer)
# ---------------------------------------------------------------------

@app.route("/api/configure", methods=["POST"])
def api_configure():
    cfg = request.get_json(force=True, silent=True) or {}
    port = detect_port()
    if not port:
        return jsonify({"ok": False, "error": "No ESP32 serial device found. Is it plugged in?"}), 400

    collector.pause()
    ser = None
    try:
        ser = RawSerialPort(port, baudrate=115200, timeout=2)
        time.sleep(2)  # let the board's USB settle if it just reset

        payload = json.dumps({
            "cmd": "set_config",
            "board": int(cfg.get("board", 0)),
            "pages": cfg.get("pages", ["temp"]),
            "cycle_mode": cfg.get("cycle_mode", "static"),
            "cycle_seconds": int(cfg.get("cycle_seconds", 10)),
            "brightness": int(cfg.get("brightness", 100)),
        }) + "\n"
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
        return jsonify({"ok": False, "error": str(e)}), 500
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

    body = request.get_json(force=True, silent=True) or {}
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
        return jsonify({"ok": ok, "log": log, "hint": hint})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Flash timed out after 180s"}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        collector.resume()


# ---------------------------------------------------------------------
# Entrypoint: run both HTTP and HTTPS listeners in one process
# ---------------------------------------------------------------------

def run_http():
    app.run(host="0.0.0.0", port=8989, threaded=True, use_reloader=False)


def run_https():
    cert_dir = Path(os.environ.get("TINYSCREEN_CERT_DIR", "/opt/tinyscreen/certs"))
    cert_path = cert_dir / "cert.pem"
    key_path = cert_dir / "key.pem"
    if not (cert_path.exists() and key_path.exists()):
        print(f"WARNING: cert/key not found at {cert_dir}; HTTPS listener (8990) not started. "
              f"The WebSerial flasher pages need HTTPS to work.")
        return
    app.run(host="0.0.0.0", port=8990, ssl_context=(str(cert_path), str(key_path)),
            threaded=True, use_reloader=False)


if __name__ == "__main__":
    https_thread = threading.Thread(target=run_https, daemon=True)
    https_thread.start()
    run_http()  # main thread
