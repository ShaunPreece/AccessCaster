"""WHAT THE DESK SENDS WHEN A HUMAN PRESSES SOMETHING, and whether the model dump follows.

TWO QUESTIONS IN ONE SITTING, because both need a person at the desk and neither needs more than
that.

1. DOES THE MODEL DUMP CARRY THE PHYSICAL PANEL STATE? Measured once, 22nd September 2026: with
   every channel muted AT THE DESK, `channelOutputMute` read False on all ten and `mutePressed`
   False on all six, while `outputMonAutoMuteActive` tracked the real state correctly. The app
   DISPLAYS mute and fader state from the dump, so if that holds it is a defect and not a
   curiosity. The test was designed then and never run: hold the mute on, dump, and see whether the
   dump moved or only the push did.

2. IS EVERY PUSH ONE WHOLE RECORD? The announcer acts only on a frame `rcp_read.parse_push` accepts,
   which is exactly one record. `mutePressed`, `soloPressed`, `encoderPressed`, `encoderSignal` and
   `padProgress` arrive ONLY from a physical control, and `channelOutputMute` / `channelCueEnable`
   are pushed only when the DESK changes them, so only a person at the desk can show they all
   qualify. A frame shaped like a push (a plausible declared length, zeros after it) that is NOT one
   record is counted and dumped: any at all means the announcer is deaf to something.

NOTHING IS WRITTEN TO THE DESK. This only listens and dumps.

    python DevTools/probe_panel_presses.py --seconds 240
"""
from __future__ import annotations
import argparse
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import rcp_hid
import rcp_read

# What a press is expected to move, so the report can say which did and which did not arrive.
WANTED = ["mutePressed", "soloPressed", "encoderPressed", "encoderSignal",
          "channelOutputMute", "channelCueEnable", "faderLevel", "potLevel",
          "padActive", "padProgress"]

ap = argparse.ArgumentParser()
ap.add_argument("--seconds", type=float, default=240.0, help="how long to listen")
args = ap.parse_args()

records = []          # (path, name, value, kind)
odd = []              # frames shaped like a push that are not exactly one record
stop = threading.Event()


def push_shaped(frame: bytes) -> bool:
    """A plausible declared length followed only by zeros: what a push looks like from outside. A
    dump frame is 255 dense bytes and fails the zeros test."""
    n = int.from_bytes(frame[1:5], "little")
    return 6 <= n <= rcp_read.PUSH_MAX_RECORD and 5 + n <= len(frame) and not any(frame[5 + n:])


def listen(dev):
    try:
        dev.set_nonblocking(False)
        while not stop.is_set():
            try:
                rep = dev.read(rcp_read.BULK_LEN, timeout_ms=400)
            except Exception:
                return
            if not rep or rep[0] != 0x04:
                continue
            frame = bytes(rep)
            rec = rcp_read.parse_push(frame)
            if rec is not None:
                records.append(rec)
            elif push_shaped(frame):
                odd.append(frame)
    finally:
        rcp_hid.close_handle(dev, "panel probe")


dev = rcp_hid.open_handle(rcp_hid.find_control_interface(), "panel probe")
threading.Thread(target=listen, args=(dev,), daemon=True).start()
time.sleep(0.4)
print(f"listening for {args.seconds:.0f}s - press away", flush=True)
time.sleep(args.seconds)

# THE DUMP IS TAKEN WHILE THE STATE IS STILL HELD, which is the whole of question 1: take it after
# the mute is released and the answer is meaningless either way.
print("taking the model dump with whatever is still held on...", flush=True)
blob = rcp_read.read_model_stable(fresh=True)
stop.set()
time.sleep(0.6)

print(f"\n{len(records)} push record(s)\n")
rows = defaultdict(lambda: {"n": 0, "kinds": set(), "vals": []})
for path, name, val, kind in records:
    r = rows[name]
    r["n"] += 1
    r["kinds"].add(kind)
    if len(r["vals"]) < 6:
        r["vals"].append((path[-1] if path else None, val))

print(f"  {'property':<26} {'kind':<6} {'n':>4}")
for name in sorted(rows):
    r = rows[name]
    print(f"  {name:<26} {'/'.join(sorted(r['kinds'])):<6} {r['n']:>4}")

print(f"\n  push-shaped frames that are NOT one record: {len(odd)}"
      + ("  <- the announcer cannot hear these" if odd else ""))
for frame in odd[:5]:
    n = int.from_bytes(frame[1:5], "little")
    print(f"    {frame[:5 + n].hex(' ')}")

print("\n  samples (address tail, value):")
for name in sorted(rows):
    if name in WANTED:
        print(f"    {name:<26} {rows[name]['vals']}")

missing = [w for w in WANTED if w not in rows]
if missing:
    print(f"\n  never pushed: {', '.join(missing)}")

print("\n=== QUESTION 1: does the DUMP agree with what is held on? ===")
for prop in ("channelOutputMute", "channelCueEnable", "mutePressed", "soloPressed",
             "faderLevel", "outputMonAutoMuteActive"):
    vals = rcp_read.values(blob, prop)
    print(f"  {prop:<26} {vals[:10]}")
print("\n  Compare each with what was actually held on at the desk. A push that moved while the")
print("  dump did not is the defect: the app displays mute and fader state FROM THE DUMP.")
