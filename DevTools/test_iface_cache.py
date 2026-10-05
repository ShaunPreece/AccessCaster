"""Offline check: the remembered interface list in rcp_hid scans only when it has to.

A full `hid.enumerate` blocks for tens of seconds on a desk half off the bus, and which desk is
attached is asked several times per request. So the finder reuses the last scan, and these are the
rules that keep that honest: reuse within the age limit, scan when asked for a fresh answer, scan
after anything forgets the list, and a failed open on a remembered path forgets it.

Offline: the scan and the device class are stubbed, so no unit is needed.

    python DevTools/test_iface_cache.py
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import rcp_hid

FAILS = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILS.append(f"{name}: got {got!r}, wanted {want!r}")
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")


def iface(pid, path):
    return rcp_hid.RodeInterface(path=path, product_id=pid, interface_number=9,
                                 usage_page=rcp_hid.CONTROL_USAGE_PAGE,
                                 usage=rcp_hid.CONTROL_USAGE, product_string="stub",
                                 serial_number="S1")


scans = []
bus = [iface(0x0078, b"a")]


def fake_enumerate():
    scans.append(1)
    return list(bus)


class DeadDevice:
    def open_path(self, path):
        raise OSError("open failed")


real_enum, real_hid_device, real_ttl = rcp_hid.enumerate_interfaces, rcp_hid.hid.device, \
    rcp_hid._IFACE_TTL_S
rcp_hid.enumerate_interfaces = fake_enumerate
rcp_hid.forget_interfaces()
try:
    print("\nREUSE")
    first = rcp_hid.find_control_interface()
    second = rcp_hid.find_control_interface()
    check("two lookups in a row scan once", len(scans), 1)
    check("and give the same interface", second.path, first.path)

    print("\nFRESH")
    bus[:] = [iface(0x0094, b"b")]
    check("a fresh lookup sees the desk come back as a new device",
          rcp_hid.find_control_interface(fresh=True).product_id, 0x0094)
    check("and the next ordinary lookup reuses that scan",
          (rcp_hid.find_control_interface().product_id, len(scans)), (0x0094, 2))

    print("\nFORGET")
    bus[:] = []
    rcp_hid.forget_interfaces()
    check("after forgetting, a desk that left reads as gone", rcp_hid.find_control_interface(), None)
    check("and an absent desk is remembered too, so it costs one scan",
          (rcp_hid.find_control_interface(), len(scans)), (None, 3))

    print("\nAGE")
    bus[:] = [iface(0x0078, b"c")]
    rcp_hid._IFACE_TTL_S = 0.0
    check("a list past its age limit is scanned again",
          rcp_hid.find_control_interface().path, b"c")
    rcp_hid._IFACE_TTL_S = real_ttl

    print("\nA FAILED OPEN")
    rcp_hid.hid.device = DeadDevice
    try:
        rcp_hid.open_handle(rcp_hid.find_control_interface(), "test")
    except OSError:
        pass
    check("clears the list", rcp_hid._iface_cache, None)

    print("\nSTATUS SCAN")
    bus[:] = [iface(0x0078, b"d")]
    before = len(scans)
    rcp_hid.refresh_interfaces()
    rcp_hid.find_control_interface()
    check("a status refresh is one scan the finder then reuses", len(scans) - before, 1)
finally:
    rcp_hid.enumerate_interfaces = real_enum
    rcp_hid.hid.device = real_hid_device
    rcp_hid._IFACE_TTL_S = real_ttl
    rcp_hid.forget_interfaces()

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILURES'}")
for f in FAILS:
    print("  " + f)
raise SystemExit(1 if FAILS else 0)
