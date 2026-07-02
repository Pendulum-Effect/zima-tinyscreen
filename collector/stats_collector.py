#!/usr/bin/env python3
"""
Tiny Screen stats collector
----------------------------
Collects vital system stats from a ZimaBlade / ZimaBoard (or any Linux box)
and streams them as line-delimited JSON to an ESP32-S3 over USB serial,
which renders the info on a 1.28" round touch display.

Run:
    python3 stats_collector.py --port /dev/ttyACM0

If --port is omitted, the script tries to auto-detect a likely ESP32-S3
serial device.
"""

import argparse
import glob
import json
import os
import platform
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import psutil

try:
    import serial
except ImportError:
    serial = None  # only required when actually talking to the ESP32-S3


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

DEFAULT_BAUD = 115200
POLL_INTERVAL_SEC = 1.0
NET_IFACE = None  # None = sum of all interfaces except loopback
RAPL_GLOB = "/sys/class/powercap/intel-rapl:0/energy_uj"
HWMON_TEMP_LABELS = ("Package id 0", "Tctl", "Tdie", "CPU")


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Stats:
    cpu_name: str
    cpu_pct: float
    cpu_temp_c: float
    cpu_watts: float
    ram_total_gb: float
    ram_pct: float
    ssd_total_gb: float
    ssd_pct: float
    net_rx_mbps: float
    net_tx_mbps: float


# --------------------------------------------------------------------------
# Collectors
# --------------------------------------------------------------------------

def get_cpu_name() -> str:
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except FileNotFoundError:
        pass
    return platform.processor() or platform.uname().processor or "Unknown CPU"


def get_cpu_temp_c() -> float:
    """Best-effort CPU temperature across Intel/AMD boards."""
    temps = psutil.sensors_temperatures() if hasattr(psutil, "sensors_temperatures") else {}
    for chip, entries in temps.items():
        for entry in entries:
            label = entry.label or chip
            if any(key.lower() in label.lower() for key in HWMON_TEMP_LABELS):
                return round(entry.current, 1)
    # Fallback: just take the first sensor we find
    for entries in temps.values():
        if entries:
            return round(entries[0].current, 1)
    return -1.0


class RaplPowerMeter:
    """Reads Intel RAPL energy counters to estimate CPU package wattage.

    ZimaBoard/ZimaBlade use Intel Celeron N-series SoCs which expose RAPL
    via /sys/class/powercap/intel-rapl:0/energy_uj. If unavailable (e.g.
    different hardware, permissions), returns -1.0 (unknown).
    """

    def __init__(self):
        candidates = glob.glob("/sys/class/powercap/intel-rapl:*/energy_uj")
        self.path = candidates[0] if candidates else None
        self._last_uj = None
        self._last_t = None

    def _read_uj(self):
        try:
            with open(self.path) as f:
                return int(f.read().strip())
        except Exception:
            return None

    def sample_watts(self) -> float:
        if not self.path:
            return -1.0
        now = time.time()
        uj = self._read_uj()
        if uj is None:
            return -1.0
        if self._last_uj is None:
            self._last_uj, self._last_t = uj, now
            return -1.0
        d_uj = uj - self._last_uj
        d_t = now - self._last_t
        # RAPL counter wraps around; ignore a wrapped sample
        if d_uj < 0 or d_t <= 0:
            self._last_uj, self._last_t = uj, now
            return -1.0
        watts = (d_uj / 1_000_000.0) / d_t
        self._last_uj, self._last_t = uj, now
        return round(watts, 2)


class NetworkMeter:
    def __init__(self, iface=None):
        self.iface = iface
        self._last = None
        self._last_t = None

    def _counters(self):
        if self.iface:
            c = psutil.net_io_counters(pernic=True).get(self.iface)
            return (c.bytes_recv, c.bytes_sent) if c else (0, 0)
        c = psutil.net_io_counters()
        return (c.bytes_recv, c.bytes_sent)

    def sample_mbps(self):
        now = time.time()
        rx, tx = self._counters()
        if self._last is None:
            self._last, self._last_t = (rx, tx), now
            return 0.0, 0.0
        d_t = max(now - self._last_t, 1e-6)
        d_rx = max(rx - self._last[0], 0)
        d_tx = max(tx - self._last[1], 0)
        self._last, self._last_t = (rx, tx), now
        rx_mbps = round((d_rx * 8 / 1_000_000) / d_t, 2)
        tx_mbps = round((d_tx * 8 / 1_000_000) / d_t, 2)
        return rx_mbps, tx_mbps


def get_ram():
    vm = psutil.virtual_memory()
    return round(vm.total / (1024 ** 3), 1), round(vm.percent, 1)


def get_ssd(path="/"):
    du = psutil.disk_usage(path)
    return round(du.total / (1024 ** 3), 1), round(du.percent, 1)


# --------------------------------------------------------------------------
# Serial link
# --------------------------------------------------------------------------

def autodetect_port() -> str:
    """Look for a serial device that's likely the ESP32-S3 (CH343 bridge)."""
    patterns = ["/dev/ttyACM*", "/dev/ttyUSB*", "/dev/serial/by-id/*"]
    for pattern in patterns:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[0]
    raise RuntimeError(
        "No serial device found. Pass --port explicitly, e.g. --port /dev/ttyACM0"
    )


def open_serial(port: str, baud: int) -> "serial.Serial":
    if serial is None:
        raise RuntimeError("pyserial is required for live mode: pip install pyserial")
    return serial.Serial(port, baudrate=baud, timeout=1)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Tiny Screen stats collector")
    parser.add_argument("--port", help="Serial port for the ESP32-S3 (e.g. /dev/ttyACM0)")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument("--interval", type=float, default=POLL_INTERVAL_SEC)
    parser.add_argument("--iface", default=NET_IFACE, help="Limit network stats to one interface")
    parser.add_argument("--disk-path", default="/", help="Path/mount used for SSD usage stats")
    parser.add_argument("--print-only", action="store_true", help="Print JSON instead of sending over serial")
    args = parser.parse_args()

    cpu_name = get_cpu_name()
    power_meter = RaplPowerMeter()
    net_meter = NetworkMeter(args.iface)

    ser = None
    if not args.print_only:
        port = args.port or autodetect_port()
        print(f"Opening serial port {port} @ {args.baud} baud...")
        ser = open_serial(port, args.baud)
        time.sleep(2)  # let the ESP32-S3 finish booting / reset after DTR toggle

    print("Streaming stats. Ctrl+C to stop.")
    psutil.cpu_percent(interval=None)  # prime the non-blocking cpu_percent call

    try:
        while True:
            cpu_pct = psutil.cpu_percent(interval=None)
            cpu_temp = get_cpu_temp_c()
            cpu_watts = power_meter.sample_watts()
            ram_total, ram_pct = get_ram()
            ssd_total, ssd_pct = get_ssd(args.disk_path)
            rx_mbps, tx_mbps = net_meter.sample_mbps()

            stats = Stats(
                cpu_name=cpu_name,
                cpu_pct=round(cpu_pct, 1),
                cpu_temp_c=cpu_temp,
                cpu_watts=cpu_watts,
                ram_total_gb=ram_total,
                ram_pct=ram_pct,
                ssd_total_gb=ssd_total,
                ssd_pct=ssd_pct,
                net_rx_mbps=rx_mbps,
                net_tx_mbps=tx_mbps,
            )

            payload = json.dumps(asdict(stats), separators=(",", ":")) + "\n"

            status_file = os.environ.get("TINYSCREEN_STATUS_FILE")
            if status_file:
                try:
                    Path(status_file).write_text(payload)
                except OSError:
                    pass

            if args.print_only:
                print(payload, end="")
            else:
                try:
                    ser.write(payload.encode("utf-8"))
                except serial.SerialException as e:
                    print(f"Serial write failed ({e}); reconnecting...", file=sys.stderr)
                    time.sleep(2)
                    ser = open_serial(args.port or autodetect_port(), args.baud)

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        if ser:
            ser.close()


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
