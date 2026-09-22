"""Survey the guide's PROVED INACTIVE properties on the ATTACHED desk, read-only.

The dead list was established on one desk and is carried across both, because they run the same
firmware and publish the same property names. This says what the OTHER desk's tree actually holds
for each of them: which block carries it, at what address, how many occurrences, and the value it
is sitting at. That is the half of a re-proof that needs no write and no ears.

WHAT IT CANNOT TELL YOU. "Inactive" means the desk does not ACT on the property, and several of
these were proved by an EAR or an absent side effect rather than by a value: a tone that never
sounded, a pad that kept playing, a volume that never mounted, a scan that returned nothing. A
property can store a value perfectly and still do nothing, which is the whole point of the list.
So a clean read here is agreement about SHAPE, not a second proof.

Run it from anywhere, with either desk attached:

    python DevTools/probe_dead_props.py
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import model_tree as mt
import rcp_read
import devices

sys.path.insert(0, str(Path(__file__).resolve().parent))
import property_list


def main() -> int:
    dead = property_list.dead_properties()
    if not dead:
        print("No PROVED INACTIVE list found: PROJECT_GUIDE.md is not beside this repo.")
        return 1

    blob = rcp_read.read_model_raw()
    root, _, _ = mt.parse(blob)
    desk = devices.get_active_device()
    print(f"Desk: {desk.name}   dump: {len(blob)} bytes   dead list: {len(dead)}\n")

    missing = []
    for prop in sorted(dead):
        hits = mt.find_property(root, prop)
        if not hits:
            missing.append(prop)
            print(f"{prop}\n    NOT PRESENT on this desk\n")
            continue
        node, value, kind = hits[0]
        addr = node.address().hex(" ")
        blocks = sorted({n.name for n, _, _ in hits})
        print(f"{prop}")
        print(f"    block   {', '.join(blocks)}   occurrences {len(hits)}")
        print(f"    address {addr}")
        print(f"    value   {value!r} ({kind})")
        if len(hits) > 1:
            vals = [v for _, v, _ in hits]
            print(f"    all     {vals}")
        print(f"    reason  {dead[prop] or '(none recorded)'}")
        print()

    print(f"{len(dead) - len(missing)} of {len(dead)} present.")
    if missing:
        print("NOT PRESENT: " + ", ".join(missing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
