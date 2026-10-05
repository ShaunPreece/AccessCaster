"""WHICH ADDRESS DOES THE DUO'S AUDIO ENGINE ACTUALLY LISTEN TO FOR A MIC'S INPUT VALUES?

The Duo keeps a mic's gain at two addresses (measured):

    MIX[0x148 + source]          what the RODE app writes, and what we copied from its capture
    INPUTSOURCE[0x26E + source]  what the DESK'S OWN ENCODER writes, and what the Pro II uses

An ear test said the audio follows the INPUTSOURCE copy, which would make our writes cosmetic. But
a read-back cannot tell a live cell from a dead one, and neither can a pcap: the RODE app really
does send the MIX address, host->device, single standalone record, byte-identical to ours.

SO USE THE DESK'S OWN ENGINE AS THE WITNESS, WHICH NEEDS NO EARS.

Selecting a mic VOICING makes the engine apply that voicing's default preamp gain (measured on a
Pro II: Dynamic 45, Condenser 25, PodMic 50, ...). That gain move is the ENGINE acting, not us.
So write only the mic TYPE, then diff the WHOLE tree:

  * the engine moved a gain  -> it saw the write, and the address it moved is where the live gain is
  * nothing but our own byte -> the engine never saw the write, and that address is dead storage

THE WRITE IS SAFE ONLY IF BOTH PROPERTIES EXIST AT BOTH ADDRESSES, AND ON CURRENT FIRMWARE THEY DO
NOT: on both desks `inputMicrophoneGain` and `inputMicrophoneType` occur 30 times each, all in
INPUTSOURCE, with no MIX copy. Whether the MIX copy was a stray or something the firmware stopped
publishing is not established. A write to an address that does not carry the property is exactly
how a stray gets appended, and a stray costs a factory reset, so `probe()` checks the precondition
on the live tree and refuses rather than trusting this docstring.

    python DevTools/duo_gain_probe.py            # report the current state of both copies
    python DevTools/duo_gain_probe.py probe      # run the differential, then restore
"""
from __future__ import annotations
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import devices
import model_tree
import rcp_hid
import rcp_read
import rcp_transfer as _t

# Source 1 (Mic 2) by default: the mic is physically in MIC 2, and strip 1 is the one an addressing
# mistake cannot flatter (strip 0 looks correct even when the resolution is wrong). Source 0 is worth
# running too, because Mic 1 is the channel the RODE app pcaps actually captured.
SOURCE = 1
MIX_SEL = MIX_TR = TBL_SEL = TBL_TR = MIX_PATH = TBL_PATH = None


def set_source(source: int):
    global SOURCE, MIX_SEL, MIX_TR, TBL_SEL, TBL_TR, MIX_PATH, TBL_PATH
    SOURCE = source
    MIX_SEL, MIX_TR = 0x48 + source, 0x01       # -> path [0x148 + source], the RODE app's address
    TBL_SEL, TBL_TR = 0x6E + source, 0x02       # -> path [0x26E + source], the desk's own address
    MIX_PATH = (0x148 + source,)
    TBL_PATH = (0x26E + source,)

# Voicings to step through. Each has a different default gain on the Pro II, so whichever copy the
# engine owns has to MOVE, and move to a different number each time.
PODMIC, DYNAMIC, CONDENSER = 4, 2, 3

NOISY = ("Meter", "meter", "Progress", "CurrentPosition", "elapsed", "Elapsed",
         "Time", "time", "Rssi", "Battery", "Temperature", "Uptime")


def snapshot() -> dict:
    """Every property in the tree, keyed by (path, name), so a diff can name the exact node."""
    rcp_hid.bump_write_generation()
    root, _, _ = model_tree.parse(rcp_read.read_model())
    out = {}
    for node in model_tree.walk(root):
        p = tuple(node.path())
        for name, value, _kind in node.props:
            out[(p, name)] = value
    return out


def diff(before: dict, after: dict, quiet: bool = True) -> list:
    rows, dropped = [], 0
    for key in set(before) | set(after):
        b, a = before.get(key, "<absent>"), after.get(key, "<absent>")
        if b == a:
            continue
        if quiet and any(n in key[1] for n in NOISY):
            dropped += 1
            continue
        rows.append((key, b, a))
    rows.sort(key=lambda r: (r[0][0], r[0][1]))
    if dropped:
        print(f"    ({dropped} self-moving properties ignored)")
    return rows


def names(root, path):
    """The block name for a path, so a diff line reads INPUTSOURCE[623] not just [623]."""
    for node in model_tree.walk(root):
        if tuple(node.path()) == tuple(path):
            return node.name
    return "?"


def report():
    root, _, _ = model_tree.parse(rcp_read.read_model())
    by = {tuple(n.path()): n for n in model_tree.walk(root)}
    for label, path in (("MIX (RODE app writes here)", MIX_PATH),
                        ("INPUTSOURCE (desk writes here)", TBL_PATH)):
        node = by.get(path)
        vals = {p: v for p, v, _ in node.props} if node else {}
        print(f"  {label} path {list(path)}: "
              f"gain={vals.get('inputMicrophoneGain')} type={vals.get('inputMicrophoneType')}")


def write_type(sel, tr, value):
    _t.send_record(_t.build_channel_prop_int("inputMicrophoneType", value, sel, tr))
    time.sleep(1.0)


def write_gain(sel, tr, value):
    _t.send_record(_t.build_channel_prop_int("inputMicrophoneGain", value, sel, tr))
    time.sleep(1.0)


def _both_addresses_carry(root) -> bool:
    """True only if BOTH addresses really hold the two properties on the attached desk.

    The whole differential rests on writing an address that already carries the name, because that
    is what makes the write incapable of appending a stray. Checked live rather than assumed: the
    MIX copy was present when this was written and is absent now."""
    ok = True
    for prop in ("inputMicrophoneGain", "inputMicrophoneType"):
        blocks = {n.name for n, _v, _k in model_tree.find_property(root, prop)}
        for needed in ("MIX", "INPUTSOURCE"):
            if needed not in blocks:
                print(f"  {prop} is not in {needed} on this desk (found: {sorted(blocks) or 'nowhere'})")
                ok = False
    return ok


def probe():
    dev = devices.DUO
    print(f"Desk: {dev.name}. Probing source {SOURCE} (Mic 2).")
    guard_root, _, _ = model_tree.parse(rcp_read.read_model())
    if not _both_addresses_carry(guard_root):
        print("")
        print("REFUSING. This probe only writes addresses that already carry the property; one")
        print("of them does not, so a write there could append a stray, and a stray costs a")
        print("factory reset. See the header: the MIX copy this was built around is gone.")
        return 1
    print("Before:")
    report()

    base = snapshot()
    mix_gain0 = base.get((MIX_PATH, "inputMicrophoneGain"))
    tbl_gain0 = base.get((TBL_PATH, "inputMicrophoneGain"))
    mix_type0 = base.get((MIX_PATH, "inputMicrophoneType"))
    tbl_type0 = base.get((TBL_PATH, "inputMicrophoneType"))

    root, _, _ = model_tree.parse(rcp_read.read_model())

    for label, sel, tr, voicing in (
            ("A: mic type -> MIX (the RODE app's address)", MIX_SEL, MIX_TR, DYNAMIC),
            ("B: mic type -> INPUTSOURCE (the desk's address)", TBL_SEL, TBL_TR, CONDENSER)):
        print(f"\n---- {label}, voicing {voicing} ----")
        before = snapshot()
        write_type(sel, tr, voicing)
        after = snapshot()
        rows = diff(before, after)
        if not rows:
            print("    NOTHING CHANGED AT ALL.")
        for (path, name), b, a in rows:
            print(f"    {names(root, path)}{list(path)}.{name}: {b!r} -> {a!r}")
        report()

    print("\n---- restoring ----")
    write_type(MIX_SEL, MIX_TR, mix_type0)
    write_type(TBL_SEL, TBL_TR, tbl_type0)
    write_gain(MIX_SEL, MIX_TR, mix_gain0)
    write_gain(TBL_SEL, TBL_TR, tbl_gain0)
    report()


if __name__ == "__main__":
    args = sys.argv[1:]
    src = int(args[1]) if len(args) > 1 else 1
    set_source(src)
    if args and args[0] == "probe":
        probe()
    else:
        report()
