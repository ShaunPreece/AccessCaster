"""Where every block INSTANCE sits in the dump, and what flat selector each rule gives it.

Written to answer one question the selector model could not: STORAGEVOLUME and
WIFISCANRESULT are excluded from the walk as "no slot", but is that exclusion ever TESTED by an
anchor? It is only load-bearing if those blocks appear BEFORE the last confirmed anchor (MIX[0],
0x4C). If they appear after it, excluding them is a free choice that no measurement has ever
challenged, and their real addresses may simply be their walk positions.

Prints, side by side:
  A = the shipped rule (PHYSICALINTERFACE subtree, STORAGEVOLUME and WIFISCANRESULT take no slot)
  B = the same walk with STORAGEVOLUME and WIFISCANRESULT COUNTED

and scores both against every anchor, so the difference is a measurement rather than an opinion.

Usage: python DevTools/block_order.py [dump.bin] [--grep NAME]
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import rcp_read
import selector_predict as sp


def walk(seq, no_slot):
    """{(block, instance): flat selector} for a given set of skipped block names."""
    out, seen, sel = {}, {}, 0
    order = []
    for nm in seq:
        inst = seen.get(nm, 0)
        seen[nm] = inst + 1
        if nm in sp.NESTED or nm in no_slot:
            order.append((nm, inst, None))
            continue
        out[(nm, inst)] = sel
        order.append((nm, inst, sel))
        sel += 1
    return out, order


def score(model) -> tuple:
    right = [(b, i, s) for b, i, s, _w in sp.ANCHORS if model.get((b, i)) == s]
    wrong = [(b, i, s, model.get((b, i))) for b, i, s, _w in sp.ANCHORS if model.get((b, i)) != s]
    return right, wrong


def main() -> int:
    argv = sys.argv[1:]
    grep = None
    if "--grep" in argv:
        k = argv.index("--grep")
        grep = argv[k + 1].upper()
        argv = argv[:k] + argv[k + 2:]
    args = [a for a in argv if not a.startswith("--")]
    if args:
        blob = Path(args[0]).read_bytes()
        print(f"from {args[0]} ({len(blob)} bytes)")
    else:
        blob = rcp_read.read_model_stable(fresh=True)
        print(f"live dump ({len(blob)} bytes)")

    seq = sp._runs(blob)
    print(f"{len(seq)} block instances in dump order\n")

    a_model, a_order = walk(seq, sp.NO_SLOT)
    b_model, _b_order = walk(seq, set())

    for label, model in (("A shipped rule (STORAGEVOLUME + WIFISCANRESULT skipped)", a_model),
                         ("B same walk, both blocks COUNTED", b_model)):
        right, wrong = score(model)
        print(f"{label}: {len(right)} anchors match, {len(wrong)} miss")
        for b, i, want, got in wrong:
            print(f"    MISS {b}[{i}] known 0x{want:02X}, this walk "
                  + (f"0x{got:02X}" if got is not None else "-"))
    print()

    # WHERE the two skipped blocks actually are, in walk order, with the nearest anchor before them.
    anchor_at = {(b, i): s for b, i, s, _w in sp.ANCHORS}
    last_anchor = None
    for nm, inst, sel in a_order:
        if (nm, inst) in anchor_at:
            last_anchor = (nm, inst, anchor_at[(nm, inst)])
        if nm in sp.NO_SLOT and inst == 0:
            print(f"FIRST {nm} instance sits after "
                  + (f"{last_anchor[0]}[{last_anchor[1]}] = 0x{last_anchor[2]:02X}"
                     if last_anchor else "no anchor at all")
                  + f"; walk B gives it 0x{b_model[(nm, inst)]:04X}")
    print()

    if grep:
        print(f"EVERY {grep} INSTANCE (A = shipped, B = counted)")
        for nm, inst, sel in a_order:
            if grep in nm:
                a = f"0x{sel:04X}" if sel is not None else "  -   "
                print(f"  {nm}[{inst}]".ljust(28) + f"A {a}   B 0x{b_model[(nm, inst)]:04X}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
