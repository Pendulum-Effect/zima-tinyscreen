#!/bin/bash
set -e

# --- Self-signed cert for the HTTPS listener (needed for WebSerial) -------
# Generated once and persisted (see the certs volume in docker-compose.yml)
# so restarts don't produce a new cert and re-trigger the browser's
# untrusted-certificate warning every time.
CERT_DIR="${TINYSCREEN_CERT_DIR:-/opt/tinyscreen/certs}"
mkdir -p "$CERT_DIR"
if [ ! -f "$CERT_DIR/cert.pem" ] || [ ! -f "$CERT_DIR/key.pem" ]; then
  echo "Generating self-signed certificate for HTTPS (first run)..."
  openssl req -x509 -nodes -newkey rsa:2048 \
    -keyout "$CERT_DIR/key.pem" \
    -out "$CERT_DIR/cert.pem" \
    -days 3650 \
    -subj "/CN=tinyscreen-dashboard" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
fi

# --- Web server + collector lifecycle -------------------------------------
# server.py now owns the stats collector's lifecycle itself (spawning it,
# restarting it if it dies, and pausing/resuming it around flash/configure
# operations that need the serial port to themselves) via its
# CollectorManager class. It also runs both the HTTP (8989) and HTTPS
# (8990) listeners itself, in one process -- important, since running two
# separate server.py processes (like before) would each spin up their own
# independent CollectorManager and fight over the same collector
# subprocess.
exec python3 /opt/tinyscreen/app/server.py
