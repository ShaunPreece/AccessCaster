"""Command line over the model tree parser (Project/model_tree.py, which documents the grammar and
why the write address is the tree path). Prints the parse summary, finds blocks and properties
with their write addresses, and scores the tree against every known selector. NOTHING HERE WRITES
TO THE DEVICE.

Usage:
    python DevTools/tree_tool.py                 # live dump: parse, verify, summarise
    python DevTools/tree_tool.py dump.bin
    python DevTools/tree_tool.py --find STORAGEVOLUME     # path + write address for a block
    python DevTools/tree_tool.py --prop storageVolumeTransfer
    python DevTools/tree_tool.py --anchors        # score the confirmed selectors against the tree
"""
from __future__ import annotations
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import rcp_read
# THE PARSER LIVES IN Project/model_tree.py: it is the app's reader now, not a probing tool.
# THIS FILE IS DELIBERATELY NOT CALLED model_tree.py. It was, for about an hour, and that put TWO
# importable modules of that name on the path - so `import model_tree` inside rcp_transfer picked
# up whichever came first, and this file ended up importing itself. Two modules must never share a
# name; the app's module owns it.
from model_tree import Node, parse, walk, instances, find_property  # noqa: F401

# Every block-level selector known from the wire or from hardware, as (block, instance, selector,
# where it came from), so the tree can be SCORED against ground truth rather than admired.
ANCHORS = [
    ("RECBUTTON", 0, 0x02, "off the wire (recButtonPressed)"),
    ("RECORDER", 0, 0x03, "off the wire (recordState)"),
    ("ENCODER", 0, 0x06, "off the wire (encoderColour push)"),
    ("GUI", 0, 0x07, "captured (screenBrightness)"),
    ("HEADPHONE", 0, 0x09, "captured (headphoneType HP1)"),
    ("HEADPHONE", 1, 0x0A, "confirmed on the Duo (headphoneType HP2)"),
    ("OUTPUT", 0, 0x0D, "captured (outputMonLevel, outputPrefader)"),
    ("MASTERCHANNEL", 0, 0x0E, "captured (masterCompellorOn)"),
    ("SYSTEM", 0, 0x0F, "captured (transferModeType, systemMidiControl)"),
    ("NETWORK", 0, 0x10, "PROVEN: btVisible written, read back, phone paired"),
    ("STREAMERXMIXPRESET", 1, 0x13, "the btDoScan stray landed here"),
    ("CHANNEL", 0, 0x1C, "captured (channelInputSource fader 1, channelOutputMute strip 0)"),
    ("EFFECTS_PARAMETERS", 0, 0x26, "captured (fx selector base)"),
    ("MIXMINUSES", 0, 0x31, "captured (outputMixMinus base)"),
    ("MIX", 0, 0x4C, "captured (cross-point base 76, verified for all 99 cells)"),
]


def _fmt_addr(node: Node) -> str:
    return node.address().hex(" ")


def main() -> int:
    argv = sys.argv[1:]

    def opt(flag):
        if flag in argv:
            k = argv.index(flag)
            return argv[k + 1]
        return None

    find = opt("--find")
    prop = opt("--prop")
    files = [a for a in argv if not a.startswith("--")
             and a not in (find or "", prop or "")]

    if files:
        blob = Path(files[0]).read_bytes()
        print(f"from {files[0]} ({len(blob)} bytes)")
    else:
        blob = rcp_read.read_model_stable(fresh=True)
        print(f"live dump ({len(blob)} bytes)")

    root, used, tail = parse(blob)
    nodes = list(walk(root))
    junk = tail.strip(b"\x00")
    print(f"\nPARSE: {used} bytes consumed, {len(tail)} left over "
          f"({'all zero padding - THE WHOLE DUMP IS ACCOUNTED FOR' if not junk else 'NOT all padding'})")
    print(f"  {len(nodes)} nodes, {sum(len(n.props) for n in nodes)} properties, "
          f"root has {len(root.kids)} children")
    if junk:
        print(f"  WARNING: non-zero trailing bytes: {tail[:32].hex(' ')}")

    if "--anchors" in argv:
        print("\nSCORING the tree against every CONFIRMED selector")
        right = wrong = 0
        for block, inst, want, why in ANCHORS:
            got = instances(root, block)
            path = got[inst].path() if inst < len(got) else None
            ok = path == [want]
            right += ok
            wrong += not ok
            print(f"  {'ok  ' if ok else 'MISS'} {block}[{inst}]".ljust(32)
                  + f"known 0x{want:02X}  tree {path}  {why}")
        print(f"\n{right} match, {wrong} miss")

    if find:
        got = instances(root, find.upper())
        print(f"\n{find.upper()}: {len(got)} instance(s)")
        for n in got[:40]:
            par = n.parent.name if n.parent else "-"
            print(f"  path {n.path()}  under {par}"
                  f"{f'[{n.parent.index}]' if n.parent and n.parent.parent else ''}"
                  f"  address {_fmt_addr(n)}")
            if n is got[0]:
                for p, v, k in n.props:
                    print(f"      {p:<34} {k:<5} {v!r}")

    if prop:
        got = find_property(root, prop)
        print(f"\n{prop}: {len(got)} occurrence(s)")
        for n, v, k in got[:40]:
            print(f"  {n.name} path {n.path()}  = {v!r} ({k})   address {_fmt_addr(n)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
