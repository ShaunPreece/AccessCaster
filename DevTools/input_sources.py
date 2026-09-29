"""The desk's INPUT SOURCE table, and what changed in it since last time.

WHY THIS EXISTS. A fader's assignment is a SOURCE CODE, and the app holds a hand-written list of
which codes mean what. That list was complete until Expanded mode arrived with four more inputs -
Game, Music, A and B - whose codes nobody had read, so the app refused them ("Channel code 12 is
not assignable") and could not name them. Guessing from the gaps was the obvious move and it was
wrong twice over: 4, 5 and 6 are not spare, they are the three stereo-link PAIR codes, and the
four that were wanted turned out to be 12 to 15, above everything the app knew.

WHAT THE TABLE ACTUALLY TELLS YOU. Every desk publishes 30 INPUTSOURCE nodes whatever it has
plugged in, so presence proves nothing. Two fields separate the real from the reserved:

  * `inputColour` ff000000 is a PLACEHOLDER. The firmware gives every usable input a colour, and
    black means the slot exists in the table and nowhere else.
  * `inputDigitalGain` -12 marks the USB/digital family. Every input the computer feeds carries it
    and no microphone does, so it is the signature of a source that arrives over the cable.

COMPARE TWO MODES, DO NOT READ ONE. The point of this script is the diff: dump the table with the
desk in one `outputMultiMode` and again in another, and whatever gains a colour is what that mode
added. That is the only way to name the four without either RODE's app or a working set of virtual
playback devices in Windows, and it costs one mode change that was going to happen anyway.

    python DevTools/input_sources.py                  print the table
    python DevTools/input_sources.py --save NAME      print it and keep it for comparison
    python DevTools/input_sources.py --diff NAME      print it beside a saved one
    python DevTools/input_sources.py --list           what has been saved

Saves land in DevTools/captures/, which is gitignored: a dump is this desk's own configuration.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401,E402  - puts Project/ on sys.path

import model_tree   # noqa: E402
import rcp_read     # noqa: E402

CAPTURES = pathlib.Path(__file__).resolve().parent / "captures"
PLACEHOLDER_COLOUR = "ff000000"
# The gain every input that arrives over USB carries, and no microphone does.
DIGITAL_FAMILY_GAIN = -12

# What the app already knows. Anything else with a colour is a source we cannot name yet, which is
# the whole output of this script.
KNOWN = {0: "Mic 1", 1: "Mic 2", 2: "Mic 3", 3: "Mic 4", 7: "USB 1", 8: "Chat",
         9: "USB 2", 10: "Bluetooth", 11: "SMART pads"}


def read_table(blob=None) -> dict:
    """{inputId: {colour, gain, type}} for every INPUTSOURCE node the desk publishes."""
    if blob is None:
        blob = rcp_read.read_model_stable(fresh=True)
    root, _consumed, _trailing = model_tree.parse(blob)
    out = {}
    for node in model_tree.instances(root, "INPUTSOURCE"):
        d = {k: v for k, v, _kind in node.props}
        if "inputId" not in d:
            continue
        out[int(d["inputId"])] = {"colour": d.get("inputColour", ""),
                                  "gain": d.get("inputDigitalGain"),
                                  "type": d.get("inputType")}
    return out


def usable(entry: dict) -> bool:
    return entry.get("colour") not in ("", PLACEHOLDER_COLOUR)


def describe(code: int, entry: dict) -> str:
    if not usable(entry):
        return "reserved slot"
    if code in KNOWN:
        return KNOWN[code]
    return "UNNAMED" + (", arrives over USB" if entry.get("gain") == DIGITAL_FAMILY_GAIN else "")


def show(table: dict, mode) -> None:
    print(f"outputMultiMode = {mode}")
    print(f"{'code':>4}  {'colour':>8}  {'gain':>4}  {'type':>4}  what")
    for code in sorted(table):
        e = table[code]
        print(f"{code:>4}  {e['colour']:>8}  {str(e['gain']):>4}  {str(e['type']):>4}  "
              f"{describe(code, e)}")
    unnamed = [c for c, e in sorted(table.items()) if usable(e) and c not in KNOWN]
    print(f"\nusable: {sum(1 for e in table.values() if usable(e))} of {len(table)}"
          f"   unnamed: {unnamed}")


def diff(old: dict, new: dict) -> None:
    """What changed between two dumps. A slot GAINING a colour is a source that mode switched on."""
    print("\nCHANGED SINCE THE SAVED TABLE")
    moved = False
    for code in sorted(set(old) | set(new)):
        a, b = old.get(code), new.get(code)
        if a == b:
            continue
        moved = True
        if a is None or b is None:
            print(f"  {code:>3}  {'appeared' if a is None else 'vanished'}")
            continue
        became = "became usable" if usable(b) and not usable(a) else (
                 "became a reserved slot" if usable(a) and not usable(b) else "changed")
        print(f"  {code:>3}  {became}: {a} -> {b}")
    if not moved:
        print("  nothing. The table is identical, so the mode did not add or remove a source.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--save", metavar="NAME", help="keep this table for a later --diff")
    ap.add_argument("--diff", metavar="NAME", help="compare against a saved table")
    ap.add_argument("--list", action="store_true", help="what has been saved")
    args = ap.parse_args()

    CAPTURES.mkdir(exist_ok=True)
    if args.list:
        for p in sorted(CAPTURES.glob("inputsources_*.json")):
            saved = json.loads(p.read_text(encoding="utf-8"))
            print(f"  {p.stem[len('inputsources_'):]:24} outputMultiMode={saved.get('mode')}")
        return 0

    blob = rcp_read.read_model_stable(fresh=True)
    if not rcp_read.is_model_dump(blob):
        print("The desk did not answer with a model. Is it connected?")
        return 1
    mode = rcp_read.first(blob, "outputMultiMode", "int")
    table = read_table(blob)
    show(table, mode)

    if args.diff:
        p = CAPTURES / f"inputsources_{args.diff}.json"
        if not p.exists():
            print(f"\nNo saved table called {args.diff!r}. Try --list.")
            return 1
        saved = json.loads(p.read_text(encoding="utf-8"))
        print(f"\n(saved table was taken at outputMultiMode = {saved.get('mode')})")
        diff({int(k): v for k, v in saved["table"].items()}, table)

    if args.save:
        p = CAPTURES / f"inputsources_{args.save}.json"
        p.write_text(json.dumps({"mode": mode, "table": table}, indent=1), encoding="utf-8")
        print(f"\nsaved to {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
