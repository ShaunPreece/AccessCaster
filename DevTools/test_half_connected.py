"""Offline check: the app NAMES the half-connected state instead of claiming all is well.

THE STATE IT IS ABOUT. The RODECaster has two USB ports and this protocol works over exactly one
interface, MI_09, on the port nearest the power inlet (USB 1 / Main). Pull that cable with the other
one still in and the Pro II's second vendor interface (PID 0x0026, MI_03) is still enumerated, and
it answers nothing: measured on hardware with both cables in, `get_version`
returned '' and `ping` False. It presents as the app having frozen.

TWO THINGS ARE PINNED HERE, and the first is why the second is reachable. `find_control_interface`
must return None rather than falling back to MI_03, so that every caller's own "no device" path
runs instead of a silent no-op; and `read_device_status` must then tell a half-connected desk from
no desk at all, since that is the difference between "check the cable" and "check which socket".
So the finder is NOT stubbed below: only the interface list is, and the real finder runs over it.

WHY IT IS WORTH A TEST. On that exact state produced a whole causal theory - "putting
storage on USB 2 tears down the control interface" - which fitted every observation and was false;
the cable had simply been unplugged to free the port. A blind operator gets no warning at all from
the back of a unit, so the app has to say it.

Offline: the interface list is stubbed, so no unit is needed and no cable has to be pulled.

    python DevTools/test_half_connected.py
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import rcp_hid
import server

FAILS = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILS.append(f"{name}: got {got!r}, wanted {want!r}")
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")


class FakeIface:
    """Just enough of rcp_hid.RodeInterface for read_device_status."""

    def __init__(self, interface_number, product_id=0x0026):
        self.interface_number = interface_number
        self.product_id = product_id
        self.path = b"fake"
        self.is_control = True

    def describe(self):
        return f"PID={self.product_id:#06x} MI_{self.interface_number:02d} (stub)"


def with_interfaces(ifaces):
    """Run a body with the HID interface list stubbed. The FINDER is left real on purpose."""
    real_enum = rcp_hid.enumerate_interfaces
    rcp_hid.enumerate_interfaces = lambda *a, **k: list(ifaces)
    try:
        return server.read_device_status(), rcp_hid.find_control_interface()
    finally:
        rcp_hid.enumerate_interfaces = real_enum


print("\nNOTHING PLUGGED IN AT ALL")
d, found = with_interfaces([])
check("reports not connected", d["connected"], False)
check("and says to check the power", "powered on" in d["message"].lower(), True)
check("and says WHICH socket, because a cable in the other one gives exactly this message",
      "nearest the power inlet" in d["message"].lower(), True)

print("\nHALF CONNECTED: the audio interface is there, the control one is not")
d, found = with_interfaces([FakeIface(3, 0x0026)])
check("the finder refuses MI_03 rather than handing it out as a control interface", found, None)
check("this is NOT reported as connected", d["connected"], False)
check("the message says half connected, not 'no unit found'",
      "half connected" in d["message"].lower(), True)
check("it names the CABLE, since that is the fix",
      "cable" in d["message"].lower(), True)
check("and says WHICH cable, by where it is rather than by a label nobody can read",
      "nearest the power socket" in d["message"].lower(), True)
check("it still names the unit, so it is clearly not a 'device missing' error",
      bool(d.get("device_name")), True)
check("and the interface it did find is still reported for troubleshooting",
      bool(d.get("control_interface")), True)

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILURES'}")
for f in FAILS:
    print("  " + f)
raise SystemExit(1 if FAILS else 0)
