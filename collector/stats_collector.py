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
"""

import argparse
import glob
import json
import os
import platform
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import psutil


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


def parse_proc_net_dev(path):
    """Parse a /proc/net/dev-format file directly, returning
    (total_rx_bytes, total_tx_bytes) summed across all real (non-loopback,
    non-virtual) interfaces. Returns None if the file can't be read.

    Format reference (two header lines, then one line per interface):
        Inter-|   Receive                                ...
         face |bytes    packets errs drop fifo frame compressed multicast|...
          eth0: 123456       10    0    0    0     0          0        0  ...

    rx_bytes is the 1st field after the interface name, tx_bytes is the
    9th (index 8) -- see /usr/include/linux/if_link.h's field order, which
    /proc/net/dev follows.
    """
    try:
        with open(path) as f:
            lines = f.readlines()[2:]  # skip the two header lines
    except (FileNotFoundError, PermissionError, OSError):
        return None

    total_rx = 0
    total_tx = 0
    found_any = False
    for line in lines:
        if ":" not in line:
            continue
        iface, data = line.split(":", 1)
        iface = iface.strip()
        if iface == "lo" or iface.startswith(("docker", "veth", "br-", "virbr")):
            continue
        fields = data.split()
        if len(fields) < 9:
            continue
        try:
            rx_bytes = int(fields[0])
            tx_bytes = int(fields[8])
        except ValueError:
            continue
        total_rx += rx_bytes
        total_tx += tx_bytes
        found_any = True

    return (total_rx, total_tx) if found_any else None


class NetworkMeter:
    """Reads network throughput.

    If TINYSCREEN_HOST_NET_DEV points at a bind-mounted copy of the host's
    /proc/1/net/dev (PID 1 always lives in the host's real network
    namespace -- see app/docker-compose.yml), reads and parses that file
    directly for accurate host-level throughput. This is a deliberately
    narrow, single-file bind mount -- NOT network_mode: host, which would
    share the container's entire network stack with the host just to read
    a few counters, and which correlated with a separate, serious
    connectivity regression in this project's history.

    Falls back to psutil.net_io_counters() when that env var isn't set
    (e.g. running directly on a host with no Docker involved at all) --
    but note that under plain bridge networking inside a container, this
    fallback only sees the container's own near-zero-traffic virtual
    interface, not real host activity.
    """

    def __init__(self, iface=None):
        self.iface = iface
        self.host_net_dev = os.environ.get("TINYSCREEN_HOST_NET_DEV")
        self._last = None
        self._last_t = None

    def _counters(self):
        if self.host_net_dev:
            result = parse_proc_net_dev(self.host_net_dev)
            if result is not None:
                return result
            # Fall through to psutil if the bind-mounted file couldn't be
            # read for some reason, rather than reporting nothing at all.
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


def get_mmc(path="/DATA"):
    """Onboard storage usage. `path` should be ZimaOS/CasaOS's /DATA
    mount (via TINYSCREEN_DATA_PATH -- see --disk-path default in
    main()), confirmed via a real df -h check to be the actual onboard
    partition -- NOT the container's own "/" (backed by Docker's storage
    driver, wherever that happens to live -- discovered directly: moving
    Docker's own data-root to a different drive changed what the
    container's own "/" reported) and NOT literal host root either (a
    separate, much smaller boot/OS partition, unrelated to the storage
    concept users actually care about)."""
    du = psutil.disk_usage(path)
    return _decimal_gb(du.total), round(du.percent, 1)


def get_nas_pool(data_path, mmc_total_bytes):
    """Detects a ZimaOS/CasaOS storage pool via a single, narrow,
    non-recursive bind mount of /DATA (see app/docker-compose.yml) --
    deliberately NOT the sprawling recursive host-root mount used by an
    earlier, since-reverted version of this feature, which pulled in
    every other container's own overlay filesystem and correlated with a
    serious, separate connectivity regression.

    ZimaOS/CasaOS always has a /DATA mount, even with no extra drives
    attached (backed by the OS disk itself in that case). When a real
    storage pool is created from additional SATA/PCIe drives, ZimaOS
    expands that same /DATA mount to cover the pooled capacity. So: if
    /DATA's total capacity is basically the same as the OS/MMC disk's own
    total, there's no real separate pool yet; if it's meaningfully
    larger, that difference in scale is the pool.

    Returns (available: bool, total_gb: float, pct: float).
    """
    try:
        du = psutil.disk_usage(data_path)
    except (PermissionError, FileNotFoundError, OSError):
        return False, 0.0, 0.0

    if mmc_total_bytes and abs(du.total - mmc_total_bytes) / mmc_total_bytes < 0.01:
        return False, 0.0, 0.0  # /DATA is just the OS disk, no separate pool

    return True, _decimal_gb(du.total), round(du.percent, 1)


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


class RawSerialPort:
    """Minimal drop-in replacement for the handful of pyserial.Serial
    methods this script actually uses (write, close) -- implemented via a
    plain POSIX file descriptor and an explicit `stty` call, NOT pyserial.

    Root-caused via a full isolation test matrix across several rounds:
    raw shell writes to the device (both write-only `>` redirects and
    read+write `exec 3<>` file descriptors) worked perfectly for 5+
    minutes straight, on both the bare ZimaOS host and a bare Docker
    container -- but this collector script via pyserial failed
    completely and immediately, on Linux specifically (not on macOS),
    even with DTR/RTS explicitly disabled beforehand. Something pyserial
    itself does when opening/configuring the port on Linux -- beyond
    DTR/RTS, which was ruled out separately -- disrupts this particular
    native-USB board's connection. Rather than keep guessing at exactly
    which pyserial internal behavior is responsible, this bypasses
    pyserial's serial-port handling entirely and replicates the proven-
    working shell approach directly: configure the tty via `stty` (the
    exact command verified working manually), then read/write the
    device as a plain file descriptor.
    """

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 1):
        self.port = port
        result = subprocess.run(
            ["stty", "-F", port, str(baudrate), "raw", "-echo"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise OSError(f"stty configuration failed for {port}: {result.stderr.strip()}")
        self._fd = os.open(port, os.O_RDWR | os.O_NOCTTY)

    def write(self, data: bytes):
        # os.write() can write FEWER bytes than requested and simply
        # return that count -- it does not raise for a partial write.
        # The previous version ignored the return value entirely, meaning
        # a partial write silently dropped the remainder of the line with
        # no error anywhere. This likely explains a long-running mystery:
        # short test payloads (~50 bytes) never seemed to trigger this,
        # but our real stats payload (~250-300+ bytes once cpu_name, MMC,
        # NAS, and network fields were all added) is much more exposed to
        # it -- and the timing of when this became a problem lines up
        # with exactly when the payload grew that much larger.
        total_written = 0
        while total_written < len(data):
            n = os.write(self._fd, data[total_written:])
            if n <= 0:
                raise OSError("write() returned no bytes -- connection may be closed")
            total_written += n

    def close(self):
        try:
            os.close(self._fd)
        except OSError:
            pass


def open_serial(port: str, baud: int) -> RawSerialPort:
    return RawSerialPort(port, baudrate=baud, timeout=1)


def try_reconnect(args):
    """Attempt one reconnect and return (RawSerialPort, port_path), or
    (None, None) on any failure. Deliberately never raises: a failed
    reconnect (e.g. a native-USB board that's mid-re-enumeration after a
    brief hiccup) should just be retried again next cycle, not crash the
    whole collector. This is what turned a single transient [Errno 5] I/O
    error into a repeating crash/respawn loop before -- autodetect_port()
    raising RuntimeError wasn't caught here, only a narrower exception
    type was, so the uncaught RuntimeError took the whole process down.
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
    print(f"[debug] Received SIGTERM (signal {signum}) -- converting to KeyboardInterrupt", file=sys.stderr)
    raise KeyboardInterrupt()


def _handle_sigint_ignore(signum, frame):
    # Confirmed via a prior diagnostic round that something sends SIGINT
    # to every freshly-spawned collector process within seconds, on EVERY
    # spawn -- including the very first one at container startup, before
    # any /api/flash or /api/configure call has ever happened. That ruled
    # out our own SIGTERM-based pause() mechanism as the source (the
    # SIGTERM debug print above never appeared). The exact origin of this
    # SIGINT is still unknown (possibly process-group signal propagation
    # specific to this container environment), but since this collector
    # should only ever stop deliberately via our own SIGTERM handler,
    # explicitly ignoring SIGINT here (log it, but don't exit) should
    # make it resilient to whatever is sending this, regardless of the
    # exact mechanism.
    print(f"[debug] Received SIGINT (signal {signum}) -- ignoring, staying alive", file=sys.stderr)


def main():
    # server.py's CollectorManager stops this process with SIGTERM when it
    # needs exclusive access to the serial port (for flashing or pushing a
    # config change). Converting that into KeyboardInterrupt reuses the
    # existing clean-shutdown path below, which closes the serial port
    # properly instead of leaving it locked.
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigint_ignore)

    parser = argparse.ArgumentParser(description="Tiny Screen stats collector")
    parser.add_argument("--port", help="Serial port for the ESP32-S3 (e.g. /dev/ttyACM0)")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument("--interval", type=float, default=POLL_INTERVAL_SEC)
    parser.add_argument("--iface", default=NET_IFACE, help="Limit network stats to one interface")
    parser.add_argument("--disk-path", default=os.environ.get("TINYSCREEN_DATA_PATH", "/DATA"),
                         help="Path/mount used for MMC (onboard storage) usage stats -- "
                              "ZimaOS/CasaOS's /DATA convention, confirmed via df -h to be "
                              "the actual onboard partition (mmcblk0p8 in testing), NOT "
                              "literal host root (which is a separate, much smaller "
                              "boot/OS partition unrelated to the storage users actually "
                              "care about)")
    parser.add_argument("--data-path", default=os.environ.get("TINYSCREEN_DATA_PATH", "/DATA"),
                         help="Path/mount used for NAS storage-pool detection (ZimaOS's /DATA convention)")
    parser.add_argument("--print-only", action="store_true", help="Print JSON instead of sending over serial")
    args = parser.parse_args()

    cpu_name = get_cpu_name()
    power_meter = RaplPowerMeter()
    net_meter = NetworkMeter(args.iface)

    ser = None
    current_port = None
    cycles_since_connect = 0
    PERIODIC_RECONNECT_CYCLES = 20  # ~20s at the default 1s interval -- see below
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
                mmc_total_bytes = psutil.disk_usage(args.disk_path).total
                nas_available, nas_total, nas_pct = get_nas_pool(args.data_path, mmc_total_bytes)
                rx_mbps, tx_mbps = net_meter.sample_mbps()
            except Exception as e:
                # Never let a single bad reading crash the whole
                # collector -- skip this cycle and try again next second.
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
                    cycles_since_connect = 0
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
                        cycles_since_connect = 0

                if ser is not None:
                    cycles_since_connect += 1
                    if cycles_since_connect >= PERIODIC_RECONNECT_CYCLES:
                        # Proactively refresh the connection on a schedule,
                        # regardless of whether any error has occurred.
                        # Root-caused via extensive isolation testing: a
                        # provably healthy, continuously-running collector
                        # can still have its writes silently never reach
                        # the board after a config-triggered reset -- no
                        # exception ever gets thrown, so the existing
                        # error-triggered and path-change-triggered
                        # reconnect logic never fires. This can't
                        # currently be detected (there's no round-trip
                        # confirmation for the regular stats stream, only
                        # for config commands), so a periodic unconditional
                        # refresh is the pragmatic fix -- worst case this
                        # costs a brief reconnect blip every ~20s, which
                        # beats staying silently stale indefinitely.
                        print("Periodic proactive reconnect (defensive refresh).", file=sys.stderr)
                        try:
                            ser.close()
                        except Exception:
                            pass
                        ser, current_port = try_reconnect(args)
                        cycles_since_connect = 0

                if ser is not None:
                    try:
                        ser.write(payload.encode("utf-8"))
                    except OSError as e:
                        print(f"Serial write failed ({e}); will reconnect next cycle.", file=sys.stderr)
                        try:
                            ser.close()
                        except Exception:
                            pass
                        ser = None
                        cycles_since_connect = 0
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
