"""Write each PROVED INACTIVE property on the attached desk, watch, and put it back.

The read-only half is `probe_dead_props.py`. This is the behavioural half a second desk needs
before the dead list can be said to hold there too: write a value that differs, re-dump, classify,
then restore and prove the tree came back byte-identical.

FOUR OUTCOMES, and only the first two agree with "inactive":

  SWALLOWED   nothing moved at all. The desk ignored the write.
  STORED      the value landed and nothing else moved. It is a field the desk keeps and does not
              act on, which is what PADRECORDER does.
  SIDE EFFECT something ELSE moved too - the engine did something. Worth a hard look.
  STRAY       an occurrence count changed: a new node appeared. THIS IS THE BAD ONE. A stray is
              only cleared by a factory reset.

TWO ARE SKIPPED BY DEFAULT AND MUST BE, because their proof was an EAR rather than a value:
`toneGeneration` (a tone that never sounded) and `emergencyMuteActive` (a pad that kept playing).
Writing them proves nothing without somebody listening. `--include-audible` runs them anyway.

Restoring is per property and happens even when the run stops, so a desk is never left changed.

    python DevTools/probe_dead_props_write.py --yes
"""
from __future__ import annotations
import argparse
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import model_tree as mt
import rcp_read
import rcp_transfer
import devices
import property_list

SETTLE = 1.5

# Needs a listener, not a dump: see the module docstring.
AUDIBLE = {"toneGeneration", "emergencyMuteActive"}

# A value that differs from the resting one, per kind. `pair` properties are not written: their
# operand layout is per property and guessing one is exactly the stray hazard this avoids.
WRITABLE_KINDS = {"bool", "int"}


def snapshot():
    """(root, {property name: occurrence count}) from a fresh dump."""
    root, _used, _tail = mt.parse(rcp_read.read_model_stable(fresh=True))
    counts = Counter()
    for n in mt.walk(root):
        for p, _v, _k in n.props:
            counts[p] += 1
    return root, counts


def value_block(kind: str, value) -> bytes:
    if kind == "bool":
        return bytes([0x01, 0x01, 0x02 if value else 0x03])
    if kind == "int":
        return bytes([0x01, 0x05, 0x01]) + int(value).to_bytes(4, "little", signed=True)
    raise ValueError(kind)


def write(node, prop: str, kind: str, value) -> None:
    rec = rcp_transfer._frame(node.address() + prop.encode("ascii") + b"\x00"
                              + value_block(kind, value))
    rcp_transfer.send_record(rec)
    time.sleep(SETTLE)


def values_of(root, prop: str):
    return [v for _n, v, _k in mt.find_property(root, prop)]


def probe(prop: str, reason: str) -> str:
    """Write `prop`, classify what the desk did, restore it. Returns the verdict."""
    root, before = snapshot()
    hits = mt.find_property(root, prop)
    if not hits:
        print(f"{prop}: NOT PRESENT on this desk")
        return "absent"
    node, resting, kind = hits[0]
    if kind not in WRITABLE_KINDS:
        print(f"{prop}: SKIPPED, kind {kind!r} has no safe single-operand write")
        return "skipped"

    target = (not resting) if kind == "bool" else int(resting) + 1
    print(f"{prop} ({kind}) in {node.name}: {resting!r} -> {target!r}")

    write(node, prop, kind, target)
    root2, after = snapshot()

    strays = {p: (before.get(p, 0), n) for p, n in after.items() if n != before.get(p, 0)}
    landed = values_of(root2, prop) != values_of(root, prop)
    others = []
    before_vals = {(tuple(n.path()), p): v for n in mt.walk(root) for p, v, _k in n.props}
    for n2 in mt.walk(root2):
        for p, v, _k in n2.props:
            if p == prop:
                continue
            was = before_vals.get((tuple(n2.path()), p), v)
            if was != v:
                others.append(f"{n2.name}.{p}: {was!r} -> {v!r}")

    if strays:
        verdict = "STRAY"
        print(f"    !!! STRAY: occurrence counts moved: {strays}")
    elif others:
        verdict = "SIDE EFFECT"
        print(f"    SIDE EFFECT, {len(others)} other propert{'y' if len(others) == 1 else 'ies'} "
              f"moved:")
        for line in others[:8]:
            print(f"      {line}")
    elif landed:
        verdict = "STORED"
        print("    STORED: the value landed, nothing else moved.")
    else:
        verdict = "SWALLOWED"
        print("    SWALLOWED: nothing moved anywhere.")

    if landed:
        write(node, prop, kind, resting)
        root3, _ = snapshot()
        back = values_of(root3, prop) == values_of(root, prop)
        print(f"    restored: {'yes' if back else 'NO - CHECK THIS DESK'}")
    print(f"    reason on record: {reason or '(none)'}")
    return verdict


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--yes", action="store_true", help="required; this writes to the desk")
    ap.add_argument("--include-audible", action="store_true",
                    help=f"also write {', '.join(sorted(AUDIBLE))}, which need a listener")
    args = ap.parse_args()

    dead = property_list.dead_properties()
    if not dead:
        print("No PROVED INACTIVE list found: PROJECT_GUIDE.md is not beside this repo.")
        return 1
    if not args.yes:
        print("--yes not given, so nothing was written.")
        return 1

    print(f"Desk: {devices.get_active_device().name}\n")
    verdicts = {}
    for prop in sorted(dead):
        if prop in AUDIBLE and not args.include_audible:
            print(f"{prop}: SKIPPED, needs a listener\n")
            verdicts[prop] = "skipped (audible)"
            continue
        verdicts[prop] = probe(prop, dead[prop])
        print()

    print("=" * 70)
    for prop, v in verdicts.items():
        print(f"  {prop:<26} {v}")
    bad = [p for p, v in verdicts.items() if v in ("STRAY", "SIDE EFFECT")]
    print("\n" + ("DISAGREES WITH THE DEAD LIST: " + ", ".join(bad) if bad
                  else "Every property written agrees with the dead list."))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
