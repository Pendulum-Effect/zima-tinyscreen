#!/usr/bin/env python3
"""
Tiny Screen stats collector
----------------------------
Collects vital system stats from a ZimaBlade / ZimaBoard (or any Linux box)
and streams them as line-delimited JSON to an ESP32-S3 over USB serial,
which renders the info on a small ESP32-S3-attached display.

Run:
    python3 stats_collector.py --port /dev/ttyACM0

If --port is omitted, the script tries to auto-detect a likely ESP32-S3
serial device.

When run inside the packaged Docker container, TINYSCREEN_HOST_ROOT points
at a read-only bind-mount of the ZimaOS host's real root filesystem (see
app/docker-compose.yml) -- needed because measuring the container's own
overlay filesystem instead of the actual host would report the wrong MMC
numbers, and because the container otherwise has zero visibility into any
extra SATA/PCIe drives ZimaOS has pooled together for NAS storage.
"""

import argparse
import glob
import json
import os
import platform
import re
import signal
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

# Filesystem types that never represent real, user-relevant storage --
# excluded from NAS-pool auto-detection.
VIRTUAL_FSTYPES = {
    "tmpfs", "devtmpfs", "overlay", "squashfs", "proc", "sysfs", "cgroup",
    "cgroup2", "devpts", "mqueue", "debugfs", "tracefs", "securityfs",
    "pstore", "bpf", "autofs", "binfmt_misc", "fusectl", "configfs", "",
}


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
    mmc_total_gb: float
    mmc_pct: float
    net_rx_mbps: float
    net_tx_mbps: float
    nas_available: bool
    nas_total_gb: float
    nas_pct: float


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
    """Reads host network throughput.

    Requires the container to run with network_mode: host (see
    app/docker-compose.yml) -- under the default bridge networking, this
    only ever sees the container's own near-zero-traffic virtual
    interface, not the ZimaBlade's real network activity.
    """

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


def _decimal_gb(num_bytes: float) -> float:
    """Decimal GB (1e9), matching how ZimaOS's own dashboard (and most
    consumer storage tools) display capacity -- not binary GiB (2^30),
    which is what a naive /1024**3 conversion gives and reads as a smaller,
    confusingly-mismatched number for the same physical disk."""
    return round(num_bytes / 1_000_000_000, 1)


def get_mmc(path="/"):
    """Root/OS storage usage. `path` should be the host's root filesystem
    as seen by this process -- either the real "/" (running directly on
    the ZimaBlade) or TINYSCREEN_HOST_ROOT's bind-mount (running in the
    packaged container, where "/" is the container's own overlay
    filesystem, not the host's)."""
    du = psutil.disk_usage(path)
    return _decimal_gb(du.total), round(du.percent, 1)


def get_root_device(path="/"):
    """The block device backing `path`, used to exclude it from NAS-pool
    detection below."""
    best_match = None
    try:
        partitions = psutil.disk_partitions(all=False)
    except Exception:
        return None
    for part in partitions:
        if path == part.mountpoint or path.rstrip("/") == part.mountpoint.rstrip("/"):
            return part.device
        if path.startswith(part.mountpoint) and (
            best_match is None or len(part.mountpoint) > len(best_match[1])
        ):
            best_match = (part.device, part.mountpoint)
    return best_match[0] if best_match else None


def get_nas_pools(root_path="/", host_root_prefix=None):
    """Best-effort auto-detection of additional storage (SATA/PCIe drives
    ZimaOS has pooled together for NAS use): any mounted, real filesystem
    that isn't the root/MMC device. This is a heuristic, not something
    that specifically understands ZimaOS's own pooling mechanism (btrfs/
    mergerfs etc.) -- if it doesn't correctly find a real pool, the exact
    matching condition below may need adjusting once tested against real
    ZimaOS storage-pool mount points.

    Returns (available: bool, total_gb: float, pct: float).
    """
    root_device = get_root_device(root_path)
    try:
        root_total = psutil.disk_usage(root_path).total
    except (PermissionError, FileNotFoundError, OSError):
        root_total = None

    total = 0
    used = 0
    found = False
    seen_devices = set()

    try:
        partitions = psutil.disk_partitions(all=False)
    except Exception as e:
        print(f"NAS detection: could not list partitions ({e}); reporting unavailable this cycle.",
              file=sys.stderr)
        return False, 0.0, 0.0

    for part in partitions:
        if part.fstype in VIRTUAL_FSTYPES:
            continue
        if part.device == root_device:
            continue
        # When running in the container, only consider mounts that came
        # from the host bind-mount (real host filesystems), not anything
        # inside the container's own overlay.
        if host_root_prefix and not part.mountpoint.startswith(host_root_prefix):
            continue
        if host_root_prefix and part.mountpoint.rstrip("/") == host_root_prefix.rstrip("/"):
            continue  # that's the host root itself, not an extra pool
        if part.device in seen_devices:
            continue
        seen_devices.add(part.device)
        try:
            du = psutil.disk_usage(part.mountpoint)
        except (PermissionError, FileNotFoundError, OSError):
            continue
        # ZimaOS/CasaOS-style setups always have a /DATA-style mount point
        # even without any extra drive attached, backed by the SAME
        # physical disk as root by default -- which can appear as a
        # DIFFERENT device string (e.g. a bind-mounted subdirectory) while
        # reporting IDENTICAL total capacity to root. Comparing device
        # strings alone isn't reliable enough to exclude that case, so
        # also skip anything whose total capacity is suspiciously close to
        # root's own -- a genuinely separate physical pool would almost
        # certainly have a different total size.
        if root_total and abs(du.total - root_total) / root_total < 0.01:
            continue
        total += du.total
        used += du.used
        found = True

    if not found or total == 0:
        return False, 0.0, 0.0
    pct = round((used / total) * 100, 1)
    return True, _decimal_gb(total), pct


# --------------------------------------------------------------------------
# Serial link
# --------------------------------------------------------------------------

def autodetect_port() -> str:
    """Look for a serial device that's likely the ESP32-S3 (CH343 bridge)."""
    patterns = ["/dev/ttyACM*", "/dev/ttyUSB*", "/dev/serial/by-id/*"]
    for pattern in patterns:
        matches = glob.glob(pattern)
        if matches:
            # Prefer the most recently created/modified device node, not
            # alphabetical order -- if a board reset (which this project
            # triggers itself, both after flashing and after applying new
            # settings) leaves a stale device file around momentarily
            # alongside a freshly re-enumerated one, alphabetical sort
            # could silently pick the dead one (e.g. ttyACM0 over the
            # live ttyACM1), and writes to it may not raise an error at
            # all -- they just never reach anything listening.
            matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            return matches[0]
    raise RuntimeError(
        "No serial device found. Pass --port explicitly, e.g. --port /dev/ttyACM0"
    )


def open_serial(port: str, baud: int) -> "serial.Serial":
    if serial is None:
        raise RuntimeError("pyserial is required for live mode: pip install pyserial")
    return serial.Serial(port, baudrate=baud, timeout=1)


def try_reconnect(args):
    """Attempt one reconnect and return (Serial, port_path), or (None, None)
    on any failure. Deliberately never raises: a failed reconnect (e.g. a
    native-USB board that's mid-re-enumeration after a brief hiccup)
    should just be retried again next cycle, not crash the whole
    collector. This is what turned a single transient [Errno 5] I/O error
    into a repeating crash/respawn loop before -- autodetect_port()
    raising RuntimeError wasn't caught here, only serial.SerialException
    was, so the uncaught RuntimeError took the whole process down.
    """
    try:
        port = args.port or autodetect_port()
        ser = open_serial(port, args.baud)
        print(f"Reconnected on {port}.", file=sys.stderr)
        return ser, port
    except Exception as e:
        print(f"Reconnect attempt failed ({e}); will retry next cycle.", file=sys.stderr)
        return None, None


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def _handle_sigterm(signum, frame):
    raise KeyboardInterrupt()


def main():
    # server.py's CollectorManager stops this process with SIGTERM when it
    # needs exclusive access to the serial port (for flashing or pushing a
    # config change). Converting that into KeyboardInterrupt reuses the
    # existing clean-shutdown path below, which closes the serial port
    # properly instead of leaving it locked.
    signal.signal(signal.SIGTERM, _handle_sigterm)

    host_root = os.environ.get("TINYSCREEN_HOST_ROOT", "").rstrip("/") or None
    default_disk_path = host_root if host_root else "/"

    parser = argparse.ArgumentParser(description="Tiny Screen stats collector")
    parser.add_argument("--port", help="Serial port for the ESP32-S3 (e.g. /dev/ttyACM0)")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument("--interval", type=float, default=POLL_INTERVAL_SEC)
    parser.add_argument("--iface", default=NET_IFACE, help="Limit network stats to one interface")
    parser.add_argument("--disk-path", default=default_disk_path,
                         help="Path/mount used for MMC (root storage) usage stats")
    parser.add_argument("--print-only", action="store_true", help="Print JSON instead of sending over serial")
    args = parser.parse_args()

    cpu_name = get_cpu_name()
    power_meter = RaplPowerMeter()
    net_meter = NetworkMeter(args.iface)

    ser = None
    current_port = None
    if not args.print_only:
        current_port = args.port or autodetect_port()
        print(f"Opening serial port {current_port} @ {args.baud} baud...")
        ser = open_serial(current_port, args.baud)
        time.sleep(2)  # let the ESP32-S3 finish booting / reset after DTR toggle

    print("Streaming stats. Ctrl+C to stop.")
    psutil.cpu_percent(interval=None)  # prime the non-blocking cpu_percent call

    try:
        while True:
            try:
                cpu_pct = psutil.cpu_percent(interval=None)
                cpu_temp = get_cpu_temp_c()
                cpu_watts = power_meter.sample_watts()
                ram_total, ram_pct = get_ram()
                mmc_total, mmc_pct = get_mmc(args.disk_path)
                nas_available, nas_total, nas_pct = get_nas_pools(args.disk_path, host_root)
                rx_mbps, tx_mbps = net_meter.sample_mbps()
            except Exception as e:
                # Never let a single bad reading (e.g. a transient issue
                # scanning the recursively-bind-mounted host filesystem for
                # NAS detection) crash the whole collector -- skip this
                # cycle and try again next second instead.
                print(f"Error gathering stats ({e}); skipping this cycle.", file=sys.stderr)
                time.sleep(args.interval)
                continue

            stats = Stats(
                cpu_name=cpu_name,
                cpu_pct=round(cpu_pct, 1),
                cpu_temp_c=cpu_temp,
                cpu_watts=cpu_watts,
                ram_total_gb=ram_total,
                ram_pct=ram_pct,
                mmc_total_gb=mmc_total,
                mmc_pct=mmc_pct,
                net_rx_mbps=rx_mbps,
                net_tx_mbps=tx_mbps,
                nas_available=nas_available,
                nas_total_gb=nas_total,
                nas_pct=nas_pct,
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
                if ser is None:
                    ser, current_port = try_reconnect(args)
                elif not args.port:
                    # Only relevant when auto-detecting (not pinned to an
                    # explicit --port): cheaply re-check that the device
                    # path we're connected to is still the one autodetect
                    # would currently pick. This board resets itself both
                    # after flashing and after applying new settings, and
                    # a reset can cause native-USB re-enumeration under a
                    # DIFFERENT path (e.g. ttyACM0 -> ttyACM1) -- writes to
                    # the old, now-dead path may not raise an error at
                    # all, they just never reach anything listening.
                    try:
                        detected = autodetect_port()
                    except RuntimeError:
                        detected = None
                    if detected and detected != current_port:
                        print(f"Device path changed ({current_port} -> {detected}); reconnecting...",
                              file=sys.stderr)
                        try:
                            ser.close()
                        except Exception:
                            pass
                        ser, current_port = try_reconnect(args)

                if ser is not None:
                    try:
                        ser.write(payload.encode("utf-8"))
                    except serial.SerialException as e:
                        print(f"Serial write failed ({e}); will reconnect next cycle.", file=sys.stderr)
                        try:
                            ser.close()
                        except Exception:
                            pass
                        ser = None
                else:
                    print("No serial connection; will retry next cycle.", file=sys.stderr)

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
