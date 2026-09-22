"""EVERY NAME THE PROTOCOL KNOWS, pulled out of the RODE app's own binary. Offline and read-only.

WHY. Our whole picture comes from the model DUMP, which is what the desk PUBLISHES, and a value
like the clock might not be published at all: it could be a command to set an internal timer and
another to read it back, and neither would appear in a dump. Nothing read off the desk can show
such a name.

But the app has to know a name to send it. **This is the safe way to enumerate names**: the unsafe
way is writing a guessed name at the desk, which appends a stray that only a factory reset clears.

HOW IT FINDS THE TABLE, rather than grepping 85 MB and drowning in English words: the protocol's
names sit in ONE contiguous block. Anchor on a handful of names the desk definitely publishes, take
the region they span, widen it, and read every identifier in it. Measured: ten
anchors spanned 7752 bytes, and the region holds 650 names against the desk's 472 - so it carries
the BLOCK names, the enum values and the unpublished properties as well.

    python DevTools/app_name_table.py                  # names the table has that the desk does not
    python DevTools/app_name_table.py --all            # the whole table
    python DevTools/app_name_table.py --grep time,date
"""
from __future__ import annotations
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import rcp_read
from model_tree import parse, walk

APP = Path(r"C:\Program Files\RØDE Microphones\RODECaster App\RODECaster App.exe")
CAP = Path(__file__).resolve().parent / "captures"
IDENT = re.compile(rb"[a-zA-Z][A-Za-z0-9]{3,44}")
PAD = 12000        # bytes either side of the anchor span, so the table's edges are not clipped

# Names the desk certainly publishes, spread across blocks so the span covers the whole table.
ANCHORS = ("systemDateTimezone", "channelCurrentFxPreset", "showControlExport",
           "inputMicrophoneGain", "padTriggerControl", "wifiSSID", "outputMonMute",
           "mixLevelWithAnchor", "systemHapticSetting", "recordingContent")


def table_names(raw: bytes) -> list:
    """Every identifier in the region the anchors span, in order, de-duplicated."""
    offs = [i for i in (raw.find(a.encode()) for a in ANCHORS) if i > 0]
    if len(offs) < 3:
        raise RuntimeError("could not locate the name table: too few anchors found")
    lo, hi = max(0, min(offs) - PAD), min(len(raw), max(offs) + PAD)
    out, seen = [], set()
    for m in IDENT.finditer(raw[lo:hi]):
        n = m.group().decode("ascii")
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def dump_names() -> set:
    """Every property name the DESK publishes, from a live read or the newest capture."""
    try:
        blob = rcp_read.read_model_stable(fresh=True)
    except Exception:
        newest = max(CAP.glob("*-raw.bin"), key=lambda p: p.stat().st_mtime)
        print(f"(no device; using {newest.name})")
        blob = newest.read_bytes()
    root, _, _ = parse(blob)
    return {p for n in walk(root) for p, _v, _k in n.props}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--grep", default="", help="comma-separated substrings, case-insensitive")
    ap.add_argument("--all", action="store_true", help="include names the desk publishes")
    ap.add_argument("--app", default=str(APP))
    args = ap.parse_args()

    path = Path(args.app)
    if not path.is_file():
        print(f"the RODE app is not at {path}")
        return 2
    names = table_names(path.read_bytes())
    known = dump_names()
    print(f"{len(names)} names in the app's table; the desk publishes {len(known)}")

    show = names if args.all else [n for n in names if n not in known]
    if args.grep:
        needles = [s.strip().lower() for s in args.grep.split(",") if s.strip()]
        show = [n for n in show if any(s in n.lower() for s in needles)]
    print(f"{len(show)} shown"
          f"{'' if args.all else ' (in the table, NOT published by the desk)'}:\n")
    for n in show:
        print(f"  {n}{'   [PUBLISHED]' if n in known else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
