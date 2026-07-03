#!/bin/sh
set -e

# Start the stats collector in the background. If TINYSCREEN_SERIAL_PORT is
# unset, the collector will try to auto-detect the ESP32-S3's serial port.
COLLECTOR_ARGS=""
if [ -n "$TINYSCREEN_SERIAL_PORT" ]; then
  COLLECTOR_ARGS="--port $TINYSCREEN_SERIAL_PORT"
fi

(
  # Keep retrying — the ESP32-S3 might get flashed/replugged after the
  # container starts.
  while true; do
    python3 /opt/tinyscreen/collector/stats_collector.py $COLLECTOR_ARGS || true
    echo "collector exited, retrying in 5s..."
    sleep 5
  done
) &

exec python3 /opt/tinyscreen/app/server.py
