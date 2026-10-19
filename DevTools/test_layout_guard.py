"""Offline check: a desk whose tree has its blocks elsewhere is refused before anything is sent.

Every write address is a root-child position read off one Pro II and one Duo. A second Pro II on
the same firmware publishes the same blocks in another order (CHANNEL from 0x1A, INPUTSOURCE from
0xDF), and its systemName still says Pro II, so it is handed the Pro II table. These are the rules
that keep that from reaching the desk: the check reads the start of the CHANNEL and INPUTSOURCE
runs, a CHANNEL that is merely somewhere in its run does not pass, an empty dump proves nothing, and
both the send layer and the pad writers refuse before a handle opens.

Offline: synthetic dumps (fake_model) and a stubbed interface, so no unit is needed.

    python DevTools/test_layout_guard.py
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import devices
import fake_model
import model_tree
import rcp_hid
import rcp_transfer
import soundpad

FAILS = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILS.append(f"{name}: got {got!r}, wanted {want!r}")
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")


def tree(runs, length=0x2A0):
    """A parsed root whose children are named by `runs` ({first index: (name, count)}), every other
    child an unrelated block."""
    names = ["OTHER"] * length
    for first, (name, count) in runs.items():
        names[first:first + count] = [name] * count
    root, _used, _tail = model_tree.parse(fake_model.node("Rodecaster", (), [
        fake_model.node(n, {"x": 0}) for n in names]))
    return root


def iface(serial, pid):
    return rcp_hid.RodeInterface(path=b"stub", product_id=pid, interface_number=9,
                                 usage_page=rcp_hid.CONTROL_USAGE_PAGE,
                                 usage=rcp_hid.CONTROL_USAGE, product_string="stub",
                                 serial_number=serial)


AS_TABLED = tree({0x1C: ("CHANNEL", 10), 0x26E: ("INPUTSOURCE", 30)})
SHIFTED = tree({0x1A: ("CHANNEL", 9), 0xDF: ("INPUTSOURCE", 12), 0x100: ("INPUTSOURCE", 18)})

print("the check reads where each run STARTS")
check("a tree laid out as the Pro II table passes", devices.layout_mismatch(devices.PRO2, AS_TABLED),
      None)
check("and as the Duo's, which shares both positions", devices.layout_mismatch(devices.DUO, AS_TABLED),
      None)
check("a shifted tree says where both runs are",
      devices.layout_mismatch(devices.PRO2, SHIFTED),
      "CHANNEL starts at 0x1a, not 0x1c; INPUTSOURCE starts at 0xdf, not 0x26e")
check("CHANNEL at 0x1C as the THIRD of its run does not pass",
      devices.layout_mismatch(devices.PRO2, tree({0x1A: ("CHANNEL", 9), 0x26E: ("INPUTSOURCE", 30)})),
      "CHANNEL starts at 0x1a, not 0x1c")
check("a tree with no CHANNEL at all says so",
      devices.layout_mismatch(devices.PRO2, tree({0x26E: ("INPUTSOURCE", 30)})),
      "it has no CHANNEL block")
check("an empty dump proves nothing", devices.layout_mismatch(devices.PRO2, None), None)

print("\nthe verdict is kept per desk, and a pinned device records none")
SHIFTED_DESK, TABLED_DESK = iface("SHIFTED-1", 0x0092), iface("TABLED-1", 0x0037)
real_active = devices.get_active_device
devices.get_active_device = lambda: devices.PRO2
try:
    prev = devices.force_device(devices.PRO2)
    devices.note_layout(SHIFTED_DESK, SHIFTED)
    check("nothing recorded while a device is pinned", devices._LAYOUT_VERDICT, {})
    devices.force_device(prev)
    devices.note_layout(SHIFTED_DESK, SHIFTED)
    devices.note_layout(TABLED_DESK, AS_TABLED)
    try:
        devices.refuse_misplaced_write(SHIFTED_DESK)
        said = None
    except devices.LayoutMismatch as exc:
        said = str(exc)
    check("the shifted desk is refused, with where its blocks are and that nothing was sent",
          (said is not None and "CHANNEL starts at 0x1a" in said and said.endswith("Nothing was sent.")),
          True)
    devices.refuse_misplaced_write(TABLED_DESK)
    check("the desk laid out as tabled is not", True, True)
    devices.refuse_misplaced_write(iface("NEVER-READ", 0x0092))
    check("a desk no dump has been read from is not refused on a guess", True, True)
finally:
    devices.get_active_device = real_active

print("\nboth write paths refuse before a handle opens")
opened = []
real = (rcp_hid.refuse_write_in_tests, rcp_transfer.find_control_interface, rcp_hid.open_handle)
rcp_hid.refuse_write_in_tests = lambda: None
rcp_transfer.find_control_interface = lambda *a, **k: SHIFTED_DESK
rcp_hid.open_handle = lambda *a, **k: opened.append(a) or (_ for _ in ()).throw(AssertionError("opened"))
pinned = devices.force_device(devices.PRO2)          # pad_write: the cap gate passes, the layout one must not
try:
    try:
        rcp_transfer.send_record(rcp_transfer.build_global_prop_bool("eqOn", True, 0x1C))
        sent = "sent"
    except devices.LayoutMismatch:
        sent = "refused"
    check("the send layer refuses", (sent, len(opened)), ("refused", 0))
    try:
        soundpad._require_pad_write()
        pad = "allowed"
    except devices.LayoutMismatch:
        pad = "refused"
    check("the pad writers' gate refuses", (pad, len(opened)), ("refused", 0))
finally:
    rcp_hid.refuse_write_in_tests, rcp_transfer.find_control_interface, rcp_hid.open_handle = real
    devices.force_device(pinned)
    devices._LAYOUT_VERDICT.clear()

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILURES'}")
for f in FAILS:
    print("  " + f)
raise SystemExit(1 if FAILS else 0)
