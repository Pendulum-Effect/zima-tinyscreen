#!/usr/bin/env python3
"""
Tiny Screen ZimaOS app web server.

Serves the WebSerial flasher page (and firmware binaries) on port 8989,
plus a tiny status endpoint showing whether the background collector is
alive and what it last sent to the ESP32-S3.
"""

import json
import os
import time
from pathlib import Path

from flask import Flask, jsonify, send_from_directory

APP_DIR = Path(__file__).resolve().parent
WEBFLASHER_DIR = APP_DIR.parent / "webflasher"
STATUS_FILE = Path(os.environ.get("TINYSCREEN_STATUS_FILE", "/tmp/tinyscreen_status.json"))

app = Flask(__name__, static_folder=None)


@app.route("/")
@app.route("/<path:filename>")
def serve_flasher(filename="index.html"):
    return send_from_directory(WEBFLASHER_DIR, filename)


@app.route("/firmware/<path:filename>")
def serve_firmware(filename):
    return send_from_directory(WEBFLASHER_DIR / "firmware", filename)


@app.route("/api/status")
def status():
    if not STATUS_FILE.exists():
        return jsonify({"collector_running": False, "last_stats": None})
    try:
        data = json.loads(STATUS_FILE.read_text())
    except Exception:
        data = None
    age = None
    if STATUS_FILE.exists():
        age = time.time() - STATUS_FILE.stat().st_mtime
    return jsonify({
        "collector_running": age is not None and age < 10,
        "last_stats": data,
        "age_seconds": age,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8989)
