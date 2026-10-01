"""A CROSS-POINT ABOVE SOURCE 13 ON THE REAL DESK: does a TWO-BYTE selector land?

`cross_point_selector` is `76 + 13*source + output`, so Virtual A (source 14) is 258 and Virtual B
is 271. Those do not fit in the one-byte address every other global write uses, and the point of
this test is that a two-byte index addresses the same node the tree says it should.

WHAT IT PROVES, AND WHY A READ-BACK IS NOT OPTIONAL HERE. A write to a selector the desk does not
recognise does not fail: it APPENDS a stray property that only a factory reset clears. So absence
of complaint proves nothing, and this checks three separate things:

  1. The tree agrees. The MIX instance at `13*source + output` must sit at root child
     `cross_point_selector(output, source)`. That is the selector CONFIRMED rather than derived
     from the arithmetic that produced it.
  2. The write lands. `mixDisabled` reads back True on that node and False again after the restore.
  3. Nothing strayed. The MIX instance count, the node count and the property count are all
     unchanged at the end.

THE OUTPUT'S MODE IS NEVER TOUCHED, so nothing here is audible. `mixDisabled` only changes routing
for an output in Custom mode; stored on an output in Main Mix it is inert, which is exactly what
makes it a safe thing to toggle on an output somebody may be listening to.

THE TWO REQUEST TRIGGERS DO NOT COME BACK, and that is the desk rather than us: `mixLinkRequest`
and `mixUnlinkRequest` are write-only, and the desk answers our composite payload by storing a
CLEARED composite in the same slot, so their decoded kind changes bool -> pair on any cross-point
you touch. Nothing reads either property. Every value that means something restores exactly.

Usage:
    python DevTools/test_cross_point_wide_live.py              # Virtual A into Headphone 1
    python DevTools/test_cross_point_wide_live.py --source 15 --output 0
"""
from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import model_tree
import rcp_read
import rcp_transfer as t

SETTLE = 1.0          # the desk needs a moment before a fresh dump shows a write
FAILED = 0

# The properties on a cross-point that carry meaning and must come back exactly. The two request
# triggers are deliberately absent: see the docstring.
MEANINGFUL = ("mixDisabled", "mixLink", "mixMute", "mixLevelWithAnchor")


def check(label, got, want):
    global FAILED
    if got == want:
        print(f"  ok   {label}: {got!r}")
    else:
        FAILED += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def snapshot():
    """(the cross-point's meaningful values, the whole-model counts for the stray check)."""
    root, _consumed, _tail = model_tree.parse(rcp_read.read_model_stable(fresh=True))
    nodes = list(model_tree.walk(root))
    cell = model_tree.instances(root, "MIX")[INDEX]
    props = dict((p, v) for p, v, _k in cell.props)
    return ({p: props.get(p) for p in MEANINGFUL},
            {"mix": len(model_tree.instances(root, "MIX")),
             "nodes": len(nodes),
             "props": sum(len(n.props) for n in nodes)},
            cell.path())


ap = argparse.ArgumentParser()
ap.add_argument("--source", type=int, default=14, help="matrix source code (14 = Virtual A)")
ap.add_argument("--output", type=int, default=0, help="matrix output index (0 = Headphone 1)")
args = ap.parse_args()

INDEX = t.CROSS_POINT_SOURCE_STRIDE * args.source + t.CROSS_POINT_OUTPUT_STRIDE * args.output
sel = t.cross_point_selector(args.output, args.source)
addr = t.build_address([sel]).hex(" ")
print(f"=== cross-point source {args.source} -> output {args.output}: "
      f"MIX instance {INDEX}, selector {sel} ({sel:#06x}), address {addr} ===")

before, counts_before, path = snapshot()
check("the tree puts that MIX instance at the selector this code builds", path, [sel])
print(f"  baseline {before}")

# The restore RE-LINKS, which throws away a manual level. A cell that starts out unlinked is
# somebody's hand-set mix, so refuse rather than overwrite it; pick another output instead.
if before.get("mixLink") is False:
    print("  SKIP this cross-point is UNLINKED, so it carries a hand-set level that the restore "
          "would discard. Run it against an output whose mix follows the faders.")
    raise SystemExit(2)

try:
    t.set_cross_point_excluded(args.output, args.source, True)
    time.sleep(SETTLE)
    during, _c, _p = snapshot()
    check("excluding sets mixDisabled", during.get("mixDisabled"), True)
    check("excluding clears mixLink", during.get("mixLink"), False)
finally:
    t.set_cross_point_excluded(args.output, args.source,
                               bool(before.get("mixDisabled")))
    time.sleep(SETTLE)

after, counts_after, _p = snapshot()
check("every meaningful value is back", after, before)
check("no stray: the model's node, property and MIX counts are unchanged",
      counts_after, counts_before)

print(f"\n{'ALL CHECKS PASSED' if not FAILED else str(FAILED) + ' CHECK(S) FAILED'}")
raise SystemExit(1 if FAILED else 0)
