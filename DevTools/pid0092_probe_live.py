"""READ-ONLY live listener for the PID 0x0092 desk: print every push it sends, with what it means.

TOUCHES THE DESK, READ ONLY, which is why the name ends in _live. It does exactly two things a desk
can see, and both are things AccessCaster already does on every start:
  1. ONE model dump (rcp_read.read_model, the ad10a7b0 read trigger), so the layout of THIS desk is
     known and every address can be named;
  2. it opens the control interface and LISTENS, like the announcer does. It never sends a record,
     never enters transfer mode, never writes a property.

For a session with the desk owner: turn a knob, press a button, move a fader, and each push prints as

    12.345  OUTPUT[0x0B] outputMonLevel = 0.42  (OUTPUT block: monitor level)

so knobs and outputs can be identified together. Ctrl+C ends it. Pass --seconds N to stop on its own,
and --raw to also print the record bytes.

Run:  python DevTools/pid0092_probe_live.py [--seconds 120] [--raw]
"""
from __future__ import annotations

import argparse
import sys
import time

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import devices
import model_tree
import rcp_hid
import rcp_read


def meaning(lay, dev, path) -> str:
    """A best-effort reading of an address against the desk's own layout. Never a write."""
    if not path:
        return "?"
    top = path[0]
    if len(path) == 2 and top == lay.single("PHYSICALINTERFACE"):
        for node in ("POT", "FADER"):
            r = lay.physical(node)
            if r is not None and r.index_of(path[1]) is not None:
                return f"PHYSICALINTERFACE {node} {r.index_of(path[1]) + 1}"
        return f"PHYSICALINTERFACE child {path[1]}"
    for r in lay.runs:
        i = r.index_of(top)
        if i is None:
            continue
        if r.name == "CHANNEL":
            return f"CHANNEL strip {i} (fader {i + 1})"
        if r.name == "INPUTSOURCE":
            return f"INPUTSOURCE source {i} ({dev.fader.all_labels.get(i, 'unlabelled')})"
        if r.name == "MIX":
            s, o = divmod(i, lay.count("MIX") // max(1, lay.count("INPUTSOURCE")))
            return (f"MIX cell {i}: source {s} ({dev.fader.all_labels.get(s, 'unlabelled')}), "
                    f"output {o}")
        if r.name == "MIXMINUSES":
            return f"MIXMINUSES output {i}"
        return f"{r.name}[{i}]"
    for name, idx in lay.singles:
        if idx == top:
            return f"{name} block"
    later = next((r for r in lay.all_runs if r.index_of(top) is not None), None)
    if later is not None:
        return f"{later.name}[{later.index_of(top)}] (NOT a main-mixer run)"
    return "unknown root child"


def block_name(lay, path) -> str:
    top = path[0] if path else -1
    r = next((r for r in lay.all_runs if r.index_of(top) is not None), None)
    where = f"{r.name}[0x{top:02X}]" if r is not None else f"[0x{top:02X}]"
    return where + ("" if len(path) == 1 else "/" + "/".join(str(p) for p in path[1:]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seconds", type=float, default=0.0, help="stop after this long (0 = Ctrl+C)")
    ap.add_argument("--raw", action="store_true", help="also print each record's bytes")
    args = ap.parse_args()

    iface = rcp_hid.find_control_interface()
    if iface is None:
        print("No control interface (MI_09) found. Is the desk on its USB 1 port?")
        return 1
    print(f"interface: {iface.describe()}")
    blob = rcp_read.read_model(fresh=True)              # the one read trigger, as the app sends
    dev = devices.get_active_device()
    lay = dev.layout
    if lay is None:
        print(f"{dev.name}: no usable layout from the dump ({len(blob)} bytes); stopping.")
        return 1
    print(f"{dev.name}: {len(blob)} bytes, {lay.root_children} root children")
    for r in lay.runs:
        print(f"  {r.name:<20} base 0x{r.base:03X} x{r.count}")
    for r in lay.physical_runs:
        print(f"  PHYSICALINTERFACE {r.name:<8} children {r.base}..{r.base + r.count - 1}")
    print("listening (read only). Ctrl+C to stop.\n")

    h = rcp_hid.open_handle(iface, "pid0092 probe (listen only)")
    t0 = time.time()
    try:
        h.set_nonblocking(False)
        while not args.seconds or time.time() - t0 < args.seconds:
            rep = h.read(rcp_read.BULK_LEN, timeout_ms=rcp_read.READ_TIMEOUT_MS)
            if not rep or rep[0] != 0x04:
                continue
            frame = bytes(rep)
            n = rcp_read._push_record_len(frame)
            if n is None:
                continue
            try:
                path, name, val, kind, _end = model_tree.parse_record(frame[5:5 + n])
            except ValueError:
                continue
            print(f"{time.time() - t0:8.3f}  {block_name(lay, path)} {name} = {val!r}  "
                  f"({meaning(lay, dev, path)})")
            if args.raw:
                print(f"          {frame[5:5 + n].hex(' ')}")
    except KeyboardInterrupt:
        pass
    finally:
        rcp_hid.close_handle(h, "pid0092 probe (listen only)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
