"""SEARCH THE DESK'S OWN FIRMWARE. Offline, read-only, and it never touches the device.

WHY IT BEATS EVERY OTHER SOURCE. A model dump shows what the desk PUBLISHES; the RODE app's binary
shows what the app KNOWS; the firmware shows what the desk actually DOES. That is the difference
between "there is no clock property" and "the clock is set by settimeofday in the touchscreen GUI,
and by `ntpdate pool.ntp.org` when there is a network" - which is the answer that turned a dead end
into a route.

WHERE IT COMES FROM, and none of this is a secret: the RODE app fetches
`http://update.rode.com/manifest.json`, which names `update.rode.com/rcp2/<version>/app.zip` (the
two Linux binaries) and `os.tar.gz` (the whole OS, 169 MB). The zip's md5 is published beside it and
this project verified it. The firmware is RODE's, so it lives OUTSIDE the repo, at
`C:\\Users\\shaun\\rode-firmware`, and is never committed.

    C:\\Users\\shaun\\rode-firmware\\app-1.7.3\\usr\\bin\\rc_audio_mixer   the engine: owns the
                                                                          property tree and speaks
                                                                          report-3 to us
    C:\\Users\\shaun\\rode-firmware\\app-1.7.3\\usr\\bin\\rc_gui_juce      the TOUCHSCREEN, a JUCE
                                                                          app: every screen we
                                                                          cannot reach lives here

Usage:
    python DevTools/firmware_dig.py ntpdate settimeofday       # context around each match
    python DevTools/firmware_dig.py --count ntp hwclock rtc    # just how many, per binary
    python DevTools/firmware_dig.py --names                    # the property-name table it carries
"""
from __future__ import annotations
import argparse
import re
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import rcp_read
from model_tree import parse, walk

# The unpacked firmware, which is RODE's and is never in this repo. Point ACCESSCASTER_FIRMWARE
# at the `usr/bin` inside an unpacked app image, or drop one beside the repo as `rode-firmware`.
FW = Path(os.environ.get("ACCESSCASTER_FIRMWARE")
          or _bootstrap.repo_path("..", "rode-firmware", "app-1.7.3", "usr", "bin"))
BINARIES = ("rc_audio_mixer", "rc_gui_juce")
IDENT = re.compile(rb"[a-zA-Z][A-Za-z0-9]{3,44}")
PAD = 12000
# Names the desk publishes, spread across blocks, used to find the name table by anchoring.
ANCHORS = ("systemDateTimezone", "channelCurrentFxPreset", "showControlExport",
           "inputMicrophoneGain", "padTriggerControl", "wifiSSID", "outputMonMute",
           "mixLevelWithAnchor", "systemHapticSetting", "recordingContent")


def printable(b: bytes) -> str:
    return re.sub(rb"[^\x20-\x7e]+", b" | ", b).decode("ascii").strip()


def dump_names() -> set:
    try:
        blob = rcp_read.read_model_stable(fresh=True)
    except Exception:
        caps = Path(__file__).resolve().parent / "captures"
        blob = max(caps.glob("*-raw.bin"), key=lambda p: p.stat().st_mtime).read_bytes()
    root, _, _ = parse(blob)
    return {p for n in walk(root) for p, _v, _k in n.props}


def name_table(raw: bytes) -> list:
    offs = [i for i in (raw.find(a.encode()) for a in ANCHORS) if i > 0]
    if len(offs) < 3:
        return []
    lo, hi = max(0, min(offs) - PAD), min(len(raw), max(offs) + PAD)
    out, seen = [], set()
    for m in IDENT.finditer(raw[lo:hi]):
        n = m.group().decode("ascii")
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("needles", nargs="*", help="substrings to look for")
    ap.add_argument("--count", action="store_true", help="counts only, no context")
    ap.add_argument("--names", action="store_true", help="the property-name table it carries")
    ap.add_argument("--pad", type=int, default=140, help="context bytes either side")
    ap.add_argument("--dir", default=str(FW))
    args = ap.parse_args()

    root = Path(args.dir)
    if not root.is_dir():
        print(f"firmware not found at {root}. Fetch it: the URL is in this file's docstring.")
        return 2

    if args.names:
        known = dump_names()
        for name in BINARIES:
            names = name_table((root / name).read_bytes())
            un = [n for n in names if n not in known]
            print(f"\n{name}: {len(names)} names in the table region, {len(un)} the desk does NOT "
                  f"publish")
            print("  " + "  ".join(un))
        return 0

    if not args.needles:
        print("give at least one thing to look for, or --names")
        return 2

    for name in BINARIES:
        raw = (root / name).read_bytes()
        print(f"==== {name} ({len(raw) // 1048576} MB)")
        for needle in args.needles:
            nb = needle.encode()
            hits = list(re.finditer(re.escape(nb), raw))
            if not hits:
                continue
            if args.count:
                print(f"   {needle:20s} x{len(hits)}")
                continue
            print(f"   -- {needle} ({len(hits)}) --")
            for m in hits[:6]:
                s, e = max(0, m.start() - args.pad), min(len(raw), m.end() + args.pad)
                print(f"      {printable(raw[s:e])[:300]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
