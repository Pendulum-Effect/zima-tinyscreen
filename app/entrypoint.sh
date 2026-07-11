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
# Private keys are secrets. Tighten every boot (not just first run) so
# installs that generated the key under the old default umask -- which
# left it world-readable on the host -- get fixed by updating the app.
chmod 700 "$CERT_DIR" 2>/dev/null || true
for f in key.pem cert.pem key.pem.bak cert.pem.bak; do
  [ -f "$CERT_DIR/$f" ] && chmod 600 "$CERT_DIR/$f" 2>/dev/null || true
done

# --- Update-state directory ------------------------------------------------
# Bind-mounted from AppData in a proper install (so update outcomes survive
# the container swap); created locally as a fallback so a dev run without
# the mount still works.
mkdir -p "${TINYSCREEN_STATE_DIR:-/opt/tinyscreen/state}"

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
