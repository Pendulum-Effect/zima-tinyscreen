#!/usr/bin/env python3
"""Tests for the collector's drive-health interpretation -- the pure
functions behind the ZimaOS Drive layout's Healthy/Warning/Critical
pill. Run directly: python3 tests/test_collector_health.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))
from stats_collector import (interpret_emmc_health, interpret_smart_output,
                             worst_health, _parent_disk)


def main():
    # JEDEC eMMC health registers
    assert interpret_emmc_health(0x01, 0x02, 0x03) == "healthy"
    assert interpret_emmc_health(0x02, 0x01, 0x01) == "warning"   # pre-EOL warning
    assert interpret_emmc_health(0x03, 0x01, 0x01) == "critical"  # pre-EOL urgent
    assert interpret_emmc_health(0x01, 0x0A, 0x01) == "warning"   # 90%+ life used
    assert interpret_emmc_health(0x01, 0x0B, 0x01) == "critical"  # life exceeded
    assert interpret_emmc_health(0x00, 0x00, 0x00) == ""          # registers absent

    # smartctl -H verdicts
    assert interpret_smart_output(
        "SMART overall-health self-assessment test result: PASSED") == "healthy"
    assert interpret_smart_output(
        "SMART overall-health self-assessment test result: FAILED!") == "critical"
    assert interpret_smart_output("SMART Health Status: OK") == "healthy"
    assert interpret_smart_output("") == ""
    assert interpret_smart_output("garbage the tool printed") == ""

    # pool aggregation: worst known wins, unknowns don't dilute
    assert worst_health(["healthy", "healthy"]) == "healthy"
    assert worst_health(["healthy", "", "warning"]) == "warning"
    assert worst_health(["healthy", "critical"]) == "critical"
    assert worst_health(["", ""]) == ""
    assert worst_health([]) == ""

    # partition -> parent disk mapping
    assert _parent_disk("/dev/sda1") == "/dev/sda"
    assert _parent_disk("/dev/sdb12") == "/dev/sdb"
    assert _parent_disk("/dev/nvme0n1p2") == "/dev/nvme0n1"
    assert _parent_disk("/dev/mmcblk0p4") == "/dev/mmcblk0"
    assert _parent_disk("/dev/sdb") == "/dev/sdb"          # already a disk
    assert _parent_disk("tmpfs") == ""                     # not a device

    print("ALL COLLECTOR HEALTH TESTS PASS")


if __name__ == "__main__":
    main()
