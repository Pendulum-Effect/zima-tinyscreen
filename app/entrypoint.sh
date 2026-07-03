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

# --- Stats collector, backgrounded with retry (unchanged from before) -----
COLLECTOR_ARGS=""
if [ -n "$TINYSCREEN_SERIAL_PORT" ]; then
  COLLECTOR_ARGS="--port $TINYSCREEN_SERIAL_PORT"
fi

(
  while true; do
    python3 /opt/tinyscreen/collector/stats_collector.py $COLLECTOR_ARGS || true
    echo "collector exited, retrying in 5s..."
    sleep 5
  done
) &

# --- Web server: plain HTTP on 8989, HTTPS (for the flasher) on 8990 ------
python3 /opt/tinyscreen/app/server.py --port 8989 &
python3 /opt/tinyscreen/app/server.py --port 8990 --https &

wait -n
