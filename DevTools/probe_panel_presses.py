"""WHAT THE DESK SENDS WHEN A HUMAN PRESSES SOMETHING, and whether the model dump follows.

TWO QUESTIONS IN ONE SITTING, because both need a person at the desk and neither needs more than
that.

1. DOES THE MODEL DUMP CARRY THE PHYSICAL PANEL STATE? Measured once, 22nd September 2026: with
   every channel muted AT THE DESK, `channelOutputMute` read False on all ten and `mutePressed`
   False on all six, while `outputMonAutoMuteActive` tracked the real state correctly. The app
   DISPLAYS mute and fader state from the dump, so if that holds it is a defect and not a
   curiosity. The test was designed then and never run: hold the mute on, dump, and see whether the
   dump moved or only the push did.

2. DO THE ANNOUNCER'S DECODERS READ A PRESS CORRECTLY? `probe_push_decoders.py` covers everything
   reachable from software and says so; `mutePressed`, `soloPressed`, `encoderPressed`,
   `encoderSignal` and `padProgress` arrive ONLY from a physical control, and `channelOutputMute` /
   `channelCueEnable` are pushed only when the DESK changes them. Same measurement as that probe:
   the announcer's decoder against `model_tree.parse_record`, plus the gap between the name and its
   value marker. `encoderSignal` is the one composite the announcer handles, so a turn is the only
   hardware check its window-scanning decoder has ever had.

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

import model_tree
import rcp_hid
import rcp_read
import server

NAME = server._ANN_NAME
DECODER = {"bool": server._ann_decode_bool,
           "float": server._ann_decode_float,
           "int": server._ann_decode_int}
# What a press is expected to move, so the report can say which did and which did not arrive.
WANTED = ["mutePressed", "soloPressed", "encoderPressed", "encoderSignal",
          "channelOutputMute", "channelCueEnable", "faderLevel", "potLevel",
          "padActive", "padProgress"]

ap = argparse.ArgumentParser()
ap.add_argument("--seconds", type=float, default=240.0, help="how long to listen")
args = ap.parse_args()

records = []          # (frame, path, name, value, kind, when)
stop = threading.Event()


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
            n = rcp_read._push_record_len(frame)     # drops model-dump slices
            if n is None:
                continue
            try:
                path, name, val, kind, _e = model_tree.parse_record(frame[5:5 + n])
            except ValueError:
                continue
            records.append((frame, tuple(path), name, val, kind, time.time()))
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
rows = defaultdict(lambda: {"n": 0, "gaps": set(), "kinds": set(), "vals": [],
                            "agree": 0, "disagree": 0, "unreachable": 0, "example": None})
for frame, path, name, val, kind, _when in records:
    r = rows[name]
    r["n"] += 1
    r["kinds"].add(kind)
    if len(r["vals"]) < 6:
        r["vals"].append((path[-1] if path else None, val))
    hit = next((m for m in NAME.finditer(frame) if server._ann_is(m.group().decode(), name)), None)
    if hit is None:
        r["unreachable"] += 1
        continue
    at = hit.end() + 1
    v = frame[at:at + 24]
    gap = next((i for i in range(len(v) - 2)
                if v[i] == 0x01 and v[i + 2] in (0x01, 0x02, 0x03, 0x04, 0x05)), None)
    r["gaps"].add(gap)
    # A COMPOSITE IS NOT "no decoder": `encoderSignal` is a pair and the announcer reads it with
    # _ann_decode_int, which is the whole reason that decoder scans a window instead of trusting a
    # fixed offset. Compare against the composite's FIRST member, which is what it is reaching for.
    if kind == "pair":
        inner = val[0] if val else (None, None)
        if inner[1] != "int":
            continue
        val, kind, decode = inner[0], "int", server._ann_decode_int
    else:
        decode = DECODER.get(kind)
        if decode is None:
            continue
    got = decode(frame, at)
    same = (got is not None and abs(got - val) < 1e-9) if kind == "float" else (got == val)
    if same:
        r["agree"] += 1
    else:
        r["disagree"] += 1
        r["example"] = (val, got)

print(f"  {'property':<26} {'kind':<6} {'n':>4}  {'gap':<5} verdict")
for name in sorted(rows):
    r = rows[name]
    gaps = ",".join("?" if g is None else str(g) for g in sorted(r["gaps"], key=str))
    if r["unreachable"]:
        verdict = f"NAME NOT FOUND by the announcer's scan in {r['unreachable']}"
    elif r["disagree"]:
        verdict = f"MISMATCH: grammar {r['example'][0]!r}, decoder {r['example'][1]!r}"
    elif r["agree"]:
        verdict = "decoder agrees with the grammar"
    else:
        verdict = "no announcer decoder for this kind"
    print(f"  {name:<26} {'/'.join(sorted(r['kinds'])):<6} {r['n']:>4}  {gaps:<5} {verdict}")

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
