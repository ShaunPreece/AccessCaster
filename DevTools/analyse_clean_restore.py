"""Compare a desk BEFORE a clean profile restore with the desk AFTER it, property by property.

WHAT IT IS FOR. A clean restore factory-resets the desk and then writes the profile on top, so the
only honest test of one is a whole-model diff: 6467 properties, before against after, with every
difference either explained or a defect.

THE ONE QUESTION THAT SPLITS THE CAUSES, when a FACTORY dump is given as well:
  * came back as the FACTORY value -> the write never landed, or was overwritten by the reset;
  * came back as SOMETHING ELSE -> we wrote it, and wrote it wrong.
That split is what retires a "the desk is still applying the factory show" theory: a difference that
is neither the saved value nor the factory one cannot have come from either.

Differences that are BY DESIGN are counted and named, never silently dropped:
  * `channelOutputMute` / `mixMute` - a restore must not silence a desk mid-session, so profiles.py
    deliberately excludes them and the reset clears them.
  * `PAD*` - only when the profile was saved without pads, in which case the reset's factory pads
    stay. Pass --pads-restored when they were included.
  * the self-clearing mix link/unlink request flags, and the shows list, which an export renumbers.

    python DevTools/analyse_clean_restore.py                       # the dumps in DevTools/captures
    python DevTools/analyse_clean_restore.py BEFORE AFTER [FACTORY]
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

from model_tree import parse, walk

CAP = Path(__file__).resolve().parent / "captures"

# Self-moving values: a difference in them means nothing.
NOISY = ("Meter", "meter", "Progress", "CurrentPosition", "elapsed", "Elapsed", "systemTime",
         "systemDate", "recordTime", "recordElapsed", "Rms", "Peak", "batteryLevel", "freeSpace",
         "FreeSpace", "remaining", "Remaining", "uuid", "Uuid", "UUID", "showLastModification",
         "showName", "showUID", "snapshot", "Snapshot", "usbConnected", "screenTouched",
         "storageVolumeState", "mixLinkRequest", "mixUnlinkRequest")

# Differences a correct clean restore is EXPECTED to produce. Named, counted, and reported as a
# total rather than hidden: each one is a decision written down elsewhere.
BY_DESIGN = ("channelOutputMute", "mixMute", "channelCueEnable", "outputMonMute", "outputBTMute")


def noisy(prop: str) -> bool:
    return any(s in prop for s in NOISY)


def snap(path) -> dict:
    """{(block, tree path, prop): value} for every property in a dump."""
    blob = Path(path).read_bytes() if not isinstance(path, (bytes, bytearray)) else path
    root, _, _ = parse(blob)
    out = {}
    for n in walk(root):
        key_path = tuple(n.path())
        for p, v, k in n.props:
            out[(n.name, key_path, p)] = repr(v) if k == "bytes" else v
    return out


def _is_pad(key) -> bool:
    """A pad, or one of the pad EFFECT OBJECTS that live beside them.

    The pad effect pool is EFFECTS_PARAMETERS nested one level down (path length 2, under
    SOUNDPADS); the ten CHANNEL effect blocks of the same name are root children (length 1). So the
    depth tells them apart without hardcoding a node index."""
    block, path, _prop = key
    return block == "PAD" or (block == "EFFECTS_PARAMETERS" and len(path) == 2)


def _cross_point_is_not_ours(key, before: dict) -> bool:
    """True for a cross-point LEVEL that a profile deliberately does not carry.

    Two cases, both written down in PROJECT_GUIDE and in routing_schemes.capture:
      * EXCLUDED (`mixDisabled`): excluding also unlinks, and an unlink freezes the level at
        whatever the fader read when it was unlinked, so the number is inert and recording it makes
        a restore fail its own read-back.
      * LINKED (`mixLink`): a linked cross-point has no level of its own, it follows the channel
        fader. Shaun's desk carries a -1.04e-07 float residue on two of them, which is zero."""
    block, path, prop = key
    if block != "MIX" or prop != "mixLevelWithAnchor":
        return False
    return bool(before.get(("MIX", path, "mixDisabled"))) or bool(before.get(("MIX", path,
                                                                              "mixLink")))


def compare(before: dict, after: dict, factory: dict | None = None,
            pads_restored: bool = False) -> dict:
    """Split every difference into by-design, pads, factory-valued and real."""
    res = {"by_design": [], "pads": [], "factory": [], "real": [], "gone": [], "new": []}
    for key, was in before.items():
        block, _path, prop = key
        # The SHOWS LIST is not desk state: every export renumbers it and adds a node, so a clean
        # restore (which backs up first) is EXPECTED to leave one more backup than it found.
        if noisy(prop) or block == "SHOW":
            continue
        if key not in after:
            (res["pads"] if (_is_pad(key) and not pads_restored) else res["gone"]).append(
                (key, was, None, None))
            continue
        now = after[key]
        if now == was:
            continue
        fac = (factory or {}).get(key, "<no factory dump>")
        row = (key, was, now, fac)
        if prop in BY_DESIGN or _cross_point_is_not_ours(key, before):
            res["by_design"].append(row)
        elif _is_pad(key) and not pads_restored:
            res["pads"].append(row)
        elif factory is not None and now == fac:
            res["factory"].append(row)
        else:
            res["real"].append(row)
    for key in after:
        if key in before or noisy(key[2]) or key[0] == "SHOW":
            continue
        (res["pads"] if (_is_pad(key) and not pads_restored) else res["new"]).append(
            (key, None, after[key], None))
    return res


def show(res: dict, factory: bool) -> int:
    """Print the verdict. Returns the number of unexplained differences."""
    def line(row):
        (block, path, prop), was, now, fac = row
        tail = f" (factory {fac!r})" if factory and fac is not None else ""
        return f"  {block}{list(path)}.{prop}: {was!r} -> {now!r}{tail}"

    print(f"BY DESIGN (mutes and solo, never restored): {len(res['by_design'])}")
    print(f"PADS (the profile carried none, so the reset's factory pads stayed): {len(res['pads'])}")
    if res["gone"]:
        print(f"PRESENT BEFORE, ABSENT AFTER: {len(res['gone'])}")
        for row in res["gone"][:10]:
            print(line(row))
    if res["new"]:
        print(f"NEW SINCE (a stray would show up here): {len(res['new'])}")
        for row in res["new"][:10]:
            print(line(row))
    if factory:
        print(f"\nLOST AND BACK AT THE FACTORY VALUE (never written, or overwritten): "
              f"{len(res['factory'])}")
        for row in res["factory"]:
            print(line(row))
    print(f"\nREAL DIFFERENCES (we wrote it and got it wrong): {len(res['real'])}")
    for row in res["real"]:
        print(line(row))
    unexplained = len(res["real"]) + len(res["factory"]) + len(res["gone"]) + len(res["new"])
    print(f"\nUNEXPLAINED IN TOTAL: {unexplained}")
    return unexplained


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("before", nargs="?", default=str(CAP / "pre-showreset-raw.bin"))
    ap.add_argument("after", nargs="?", default=str(CAP / "post-cleanrestore-raw.bin"))
    ap.add_argument("factory", nargs="?", default=str(CAP / "post-showreset-raw.bin"))
    ap.add_argument("--pads-restored", action="store_true",
                    help="the profile carried pads, so pad differences are NOT expected")
    args = ap.parse_args()

    before, after = snap(args.before), snap(args.after)
    factory = snap(args.factory) if args.factory and Path(args.factory).is_file() else None
    print(f"before  {len(before)} properties  {Path(args.before).name}")
    print(f"after   {len(after)} properties  {Path(args.after).name}")
    print(f"factory {len(factory) if factory else 0} properties  "
          f"{Path(args.factory).name if factory else '(none given)'}\n")
    res = compare(before, after, factory, pads_restored=args.pads_restored)
    return 1 if show(res, factory is not None) else 0


if __name__ == "__main__":
    raise SystemExit(main())
