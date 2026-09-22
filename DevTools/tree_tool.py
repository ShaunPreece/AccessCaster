"""THE MODEL IS A SELF-DESCRIBING TREE. This parses it properly, and the parse is its own proof.

Decoded off a live dump, after `block_order.py` showed that skipping STORAGEVOLUME
and WIFISCANRESULT in the flat walk is LOAD-BEARING (their instances sit between SYSTEM 0x0F and the
proven NETWORK 0x10, so they really do take no slot) - which meant the flat walk was fitting around
a structure it could not see.

THE GRAMMAR, and every byte of the dump is accounted for by it:

    node     := NAME "\\0" count(P) property x P  count(C) node x C
    property := name "\\0" 0x01 <vlen> <payload, vlen bytes; payload[0] = type>
    count(n) := <length byte L> <n, L bytes little-endian>      # L = 0 means the count is 0

So the two bytes that look like "an array marker before a block name" are the PARENT's child COUNT,
and the lone 0x00 between two adjacent POT blocks is the first POT's own child count of zero.
Nothing is special-cased; SOUNDPADS is not a hole in the walk, it is an ordinary node with 0
properties and 24 children.

WHY THIS MATTERS MORE THAN TIDINESS. The write address is that tree path, not a flat number:

    01 01 <levels> [ <len> <index, len bytes LE> ] x levels

`levels` is the DEPTH. So a node that is a child of the root takes ONE index (which is why every
selector we have ever confirmed equals its root-child position, `0x1C` = CHANNEL[0], `0x026E` =
INPUTSOURCE[0] as a two-byte index), and a node one level deeper takes TWO - which is exactly the
pad path `01 01 02 02 9a 02 01 <n>`: root child 0x029A = SOUNDPADS, then child n = PAD[n].

That retires the "flat index past 0xFF" problem AND the unexplained "drift": PAD[0..23] were never
root children, so counting them as such shifted everything after them. It also gives STORAGEVOLUME
and WIFISCANRESULT real addresses for the first time - they are not slotless, they are one level
down, under a parent the flat walk could not see.

`address_of` returns the path for any node. NOTHING HERE WRITES TO THE DEVICE.

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

# The confirmed block selectors, copied from selector_predict.ANCHORS. Kept as a list of
# (block, instance, selector) so the tree can be SCORED against ground truth rather than admired.
try:
    import selector_predict
    ANCHORS = selector_predict.ANCHORS
except Exception:                                        # pragma: no cover - tooling only
    ANCHORS = []


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
        # selector_predict's flat walk reads a block name by stepping back over ALNUM bytes, which
        # stops at an underscore, so EFFECTS_PARAMETERS reaches it as "PARAMETERS". The tree reads
        # the real name. Aliased here rather than changing the proven flat walk.
        alias = {"PARAMETERS": "EFFECTS_PARAMETERS"}
        right = wrong = 0
        for block, inst, want, why in ANCHORS:
            got = instances(root, alias.get(block, block))
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
