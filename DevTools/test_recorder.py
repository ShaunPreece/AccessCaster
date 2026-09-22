"""Offline checks for the recorder: parsing, wording, storage labels and the elapsed-time count.

NO DEVICE NEEDED, and that is the point - every fact these pin came off the hardware and is
expensive to re-establish, since it needs a drive in the unit and a real take. The live driving is
DevTools/test_recorder_live.py; this is what stops the parsing regressing.

    python DevTools/test_recorder.py
"""
from __future__ import annotations
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from datetime import datetime, timezone

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)

import recorder

FAILS = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILS.append(f"{name}: got {got!r}, wanted {want!r}")
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")


def check_in(name, needle, haystack):
    ok = needle in haystack
    if not ok:
        FAILS.append(f"{name}: {needle!r} not in {haystack!r}")
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")


# A real recordingContent string, copied off the unit. The trailing six fields are unmapped.
CONTENT = ("Recording 4|94486d8a68da47c7a516e1400a3279dc|"
           "/Application/ssd-mount/RODECaster/4 - 28 Aug 2026/Stereo Mix.wav|"
           "1787914947088|10.8173|0|0|1|0|0|1|1")

# A real report-04 push, captured on the wire while a recording started (state 2) and stopped (0).
PUSH_RECORDING = bytes.fromhex(
    "041800000001010101037265636f7264537461746500010501020000000000000000000000000000")
PUSH_STOPPED = bytes.fromhex(
    "041800000001010101037265636f7264537461746500010501000000000000000000000000000000")


print("\nRECORDING RECORDS")
r = recorder.parse_recording(4, CONTENT)
check("name", r["name"], "Recording 4")
check("seconds", r["seconds"], 10.8)
check("duration in words", r["duration"], "11 seconds")
# THE TIMESTAMP RULE, CORRECTED (this test asserted the OLD, WRONG rule). The
# field is an ordinary UTC epoch - four takes made to order all matched this PC's UTC clock to the
# second while local time was UTC+1 - so it is shown in THIS COMPUTER'S LOCAL time, which is the
# wall clock the person remembers. Rendering it as UTC showed every recording an hour early all
# summer. The expectation is computed rather than written out, because a literal would only pass in
# one timezone.
stamp_ms = 1787914947088
local = datetime.fromtimestamp(stamp_ms / 1000.0)
check("timestamp is shown in local time", r["when"], local.strftime("%d %B %Y at %H:%M"))
utc_render = datetime.fromtimestamp(stamp_ms / 1000.0, tz=timezone.utc).strftime("%d %B %Y at %H:%M")
if utc_render != r["when"]:
    check("and it is NOT the raw UTC rendering, which was the bug", r["when"] != utc_render, True)
else:
    print("  note this machine is on UTC, so local and UTC read the same; the direction of the "
          "fix cannot be pinned here")
check("a malformed record does not raise",
      recorder.parse_recording(1, "nonsense")["seconds"], None)

print("\nLENGTHS IN WORDS (never punctuation: a reader says '0:07.3' as literal colons)")
check("under a minute", recorder.describe_duration(7), "7 seconds")
check("exactly one", recorder.describe_duration(1), "1 second")
check("whole minutes lose the seconds", recorder.describe_duration(120), "2 minutes")
check("minutes and seconds", recorder.describe_duration(125), "2 minutes 5 seconds")
check("hours", recorder.describe_duration(3600 * 2 + 60), "2 hours 1 minute")
check("unknown is said, not shown as zero", recorder.describe_duration(None), "length unknown")

print("\nSTORAGE SLOT NAMES (slot 1 is the USB port, not a second card slot)")
check("slot 0 is the card slot", recorder.volume_label(0), "Memory card slot")
check("slot 1 is the USB port, NOT 'Card slot 2'", recorder.volume_label(1), "USB drive")
check("slot 2 is the pad volume", recorder.volume_label(2), "Internal storage (SMART pads)")
check("an unexpected extra slot is numbered, not guessed",
      recorder.volume_label(7), "Storage 8")
# The spoken form exists so a sentence does not lower-case an initialism into "uzb drive".
check("spoken form keeps the initialism", recorder.volume_spoken(1), "USB drive")
check("spoken form of the card", recorder.volume_spoken(0), "memory card")

print("\nWHERE A RECORDING GOES")
vols = [
    {"index": 0, "label": "Memory card slot", "spoken": "memory card", "inserted": False,
     "mounted": False, "can_record": True, "free_bytes": 0, "low": False},
    {"index": 1, "label": "USB drive", "spoken": "USB drive", "inserted": True,
     "mounted": True, "can_record": True, "free_bytes": 123_000_000_000, "low": False},
    {"index": 2, "label": "Internal storage (SMART pads)", "spoken": "internal storage",
     "inserted": True, "mounted": True, "can_record": False, "free_bytes": 5_000_000_000,
     "low": False},
]
check("an EMPTY slot is not a destination, even though the unit flags it as eligible",
      recorder.destination(vols)["spoken"], "USB drive")
check("the pad volume is never a destination",
      recorder.destination([vols[2]]), None)
check("nothing in the unit means nowhere to record", recorder.destination([]), None)

print("\nHOW LONG WILL IT FIT (the figure a bar gives a sighted operator)")
check("no destination, no claim",
      recorder.room_left(b"", None)["seconds"], None)

print("\nDECODING THE PUSH (real packets, captured on the wire)")
check("a start push reads as recording", recorder._state_from_push(PUSH_RECORDING), 2)
check("a stop push reads as ready", recorder._state_from_push(PUSH_STOPPED), 0)
check("a report that is not 04 is ignored",
      recorder._state_from_push(b"\x03" + PUSH_RECORDING[1:]), None)
check("an unrelated packet is ignored",
      recorder._state_from_push(b"\x04\x00\x00\x00\x00selectedBank\x00\x01\x05\x01\x02\x00\x00\x00"),
      None)
# The SELECTOR is checked as well as the name, so a same-named field in another block cannot be
# read as the recorder's.
check("the wrong selector is refused",
      recorder._state_from_push(PUSH_RECORDING[:9] + b"\x0f" + PUSH_RECORDING[10:]), None)

print("\nELAPSED TIME (counted here, because the unit will not: recordTimeMs stays 0)")
said = []
w = recorder.Watcher(on_change=lambda was, now: said.append(recorder.STATE_SPOKEN.get(now)))
w._apply(recorder.READY)
w._apply(recorder.RECORDING)
time.sleep(0.3)
check("a recording we started IS timed", w.snapshot()["elapsed_seconds"] is not None, True)
w._apply(recorder.PAUSED)
paused_at = w.snapshot()["elapsed_seconds"]
time.sleep(0.4)
check("a PAUSE stops the clock, exactly as it stops the recording",
      w.snapshot()["elapsed_seconds"], paused_at)
w._apply(recorder.RECORDING)
time.sleep(0.2)
check("resuming carries on from where it paused, it does not restart",
      w.snapshot()["elapsed_seconds"] > paused_at, True)
w._apply(recorder.READY)
check("stopping clears the count", w.snapshot()["elapsed_seconds"], None)
check("every transition was spoken, and in order",
      said, ["Recording stopped", "Recording started", "Recording paused",
             "Recording started", "Recording stopped"])

print("\nAN ELAPSED TIME WE CANNOT HONESTLY GIVE")
w2 = recorder.Watcher()
# The app started while the unit was ALREADY recording: we learned the state from a model dump, so
# we never saw it begin. A number here would be wrong, so there is no number.
w2.note_state(recorder.RECORDING, timed=False)
check("no start seen, no elapsed claimed", w2.snapshot()["elapsed_seconds"], None)
check("but the state is still reported", w2.snapshot()["state_label"], "Recording")
w2.note_state(recorder.RECORDING, timed=False)
check("a repeated read does not reset anything", w2.snapshot()["state"], recorder.RECORDING)

print("\nCOMMANDS")
check("start and resume are the same command to the unit",
      recorder.COMMANDS["start"], recorder.COMMANDS["resume"])
check("stop is 0", recorder.COMMANDS["stop"], 0)
check("pause is 1", recorder.COMMANDS["pause"], 1)
check("every state has words", sorted(recorder.STATE_LABEL), [0, 1, 2, 3])
check("every state has a spoken form", sorted(recorder.STATE_SPOKEN), [0, 1, 2, 3])
check_in("no destination is EXPLAINED, not just named",
         "nowhere", recorder.STATE_SPOKEN[recorder.NO_DESTINATION])

print("\nPLUGGING A DRIVE IN IS NOT A RECORDING STOPPING")
# THE WORDING NEEDS BOTH STATES. Keyed on the NEW state alone, inserting a pen drive
# (NO_DESTINATION -> READY) announces READY's own word for itself, "Recording stopped".
S = recorder.spoken_change
check("putting a card or drive IN says recording is available",
      S(recorder.NO_DESTINATION, recorder.READY), "Recording now available")
check("and it never says anything stopped",
      "stopped" in S(recorder.NO_DESTINATION, recorder.READY).lower(), False)
check("taking it out while idle says it is no longer available, and names what left",
      S(recorder.READY, recorder.NO_DESTINATION, "USB drive"),
      "Recording no longer available: the USB drive was removed")
check("with no name known it still explains itself rather than guessing a medium",
      S(recorder.READY, recorder.NO_DESTINATION),
      "Recording no longer available: the unit has nowhere to record to")
# The one case where a plug really does stop a recording. It must NOT be softened into the
# "no longer available" wording: the operator has just lost the end of a take.
check("pulling it MID-TAKE is a stop, and says why",
      S(recorder.RECORDING, recorder.NO_DESTINATION, "memory card"),
      "Recording stopped: the memory card was removed")
check_in("a mid-take pull with no name still reports a stop",
         "Recording stopped", S(recorder.RECORDING, recorder.NO_DESTINATION))
check("a genuine stop is still a plain stop",
      S(recorder.RECORDING, recorder.READY), "Recording stopped")
check("an insertion never names the drive that was pulled out before it",
      S(recorder.NO_DESTINATION, recorder.READY, "USB drive"), "Recording now available")
check("resuming from a pause is not a fresh start",
      S(recorder.PAUSED, recorder.RECORDING), "Recording again")
check("starting from ready is", S(recorder.READY, recorder.RECORDING), "Recording started")
# `was` of None is the FIRST state the app ever sees. Nothing moved, so nothing is said - this is
# what stops the app greeting you with "Recording stopped" on startup.
check("the first state we ever see is not a change", S(None, recorder.READY), "")
check("nor is one that did not move", S(recorder.READY, recorder.READY), "")
check("an unreadable state says nothing", S(recorder.READY, None), "")

print("\nTWO MULTITRACKS, AND NEITHER LABEL MAY BE MISTAKEN FOR THE OTHER")
# The whole reason these two were indistinguishable for months is that RODE calls both of them
# "Multitrack". If our labels ever collide, that confusion is back in the app.
check("the recorder offers three modes", sorted(recorder.MULTITRACK_LABEL), [0, 1, 2])
check("so does the USB stream", sorted(recorder.USB_MULTITRACK_LABEL), [0, 1, 2])
check("no label appears in both lists",
      sorted(set(recorder.MULTITRACK_LABEL.values())
             & set(recorder.USB_MULTITRACK_LABEL.values())), [])
check("every mode says what you get, not just pre or post",
      [v for v in list(recorder.MULTITRACK_LABEL.values())
       + list(recorder.USB_MULTITRACK_LABEL.values()) if len(v.split()) < 2], [])

print("\nTHE RAW SWITCH SAYS BOTH HALVES OF WHAT IT DOES")
# RODE's guide: multitrack only, and the mix track always keeps its processing. A hint that gave
# only the first half would leave someone expecting a raw mixed track, which they never get.
check_in("it is separate tracks only", "Separate tracks only", recorder.BYPASS_HINT)
check_in("and the mixed track is called out", "mixed track", recorder.BYPASS_HINT)
check_in("with multitrack off, it says nothing happens yet",
         "does nothing", recorder.BYPASS_NEEDS_MULTITRACK)

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILURES'}")
for f in FAILS:
    print("  " + f)
raise SystemExit(1 if FAILS else 0)
