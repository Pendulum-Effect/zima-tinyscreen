#!/usr/bin/env python3
"""
Tiny Screen ZimaOS app web server.

Serves the WebSerial flasher page (and firmware binaries) plus a tiny
status endpoint. Run twice by entrypoint.sh: once plain HTTP on 8989 (easy
to hit for the /api/status check, no browser warnings), and once HTTPS on
8990 using a self-signed cert (required for the flasher page, since
browsers only allow the Web Serial API on HTTPS or localhost).
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
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8989)
    parser.add_argument("--https", action="store_true",
                         help="Serve over HTTPS using cert/key from TINYSCREEN_CERT_DIR "
                              "(required for the WebSerial flasher page -- browsers only "
                              "allow Web Serial on HTTPS or localhost)")
    args = parser.parse_args()

    ssl_context = None
    if args.https:
        cert_dir = Path(os.environ.get("TINYSCREEN_CERT_DIR", "/opt/tinyscreen/certs"))
        cert_path = cert_dir / "cert.pem"
        key_path = cert_dir / "key.pem"
        if cert_path.exists() and key_path.exists():
            ssl_context = (str(cert_path), str(key_path))
        else:
            print(f"WARNING: --https requested but {cert_path} / {key_path} not found; "
                  f"falling back to plain HTTP on port {args.port}")

    app.run(host="0.0.0.0", port=args.port, ssl_context=ssl_context)
