"""Offline test of the STORAGE judgement: when does the app say a volume is nearly full?

Why offline. The judgement itself is the interesting part, and it cannot be exercised on the real
desk without deliberately filling a card up - which nobody is going to do, and which would still
only test one threshold. So this feeds `server._storage_low` synthetic `storageVolumeState` strings
and pins the decisions. Nothing is read from the unit and nothing is written to it.

Why the judgement exists at all. The desk reports capacity and free space as raw byte counts. A
sighted operator glances at a bar; read aloud, "4960 MB free of 5033" is a division you have to do
in your head while a recording is running, and the answer only matters when it is bad. So the app
decides, in one place, what "nearly full" means, and both the Hardware tab and the Current state
overview read that same decision rather than each doing their own arithmetic.

Run: python DevTools/test_storage_state.py
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import rcp_read
import server

FAILED = 0
GB = 1024 * 1024 * 1024


def check(label, got, want):
    global FAILED
    if got == want:
        print(f"  ok   {label}: {got!r}")
    else:
        FAILED += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def low_for(states):
    """Run _storage_low over a list of storageVolumeState strings, with find_all stubbed.

    Stubbing rather than building a synthetic dump is deliberate: a hand-built blob would be testing
    the parser, and the parser is already exercised against the real desk every time the Device page
    is opened. What has never been exercised is the THRESHOLD."""
    real = rcp_read.find_all
    rcp_read.find_all = lambda blob, name: (
        [(s, "str") for s in states] if name == "storageVolumeState" else real(blob, name))
    try:
        return server._storage_low(b"")
    finally:
        rcp_read.find_all = real


def state(capacity, free, inserted=True, mounted=True):
    return f"{capacity}|{free}|0|{1 if inserted else 0}|{1 if mounted else 0}"


def main() -> int:
    print("THE THRESHOLD (less than a fifth left counts as nearly full)")
    check("half empty is not a warning", low_for([state(100 * GB, 50 * GB)]), [])
    check("exactly a fifth left is not a warning either (the rule is BELOW 20)",
          low_for([state(100 * GB, 20 * GB)]), [])
    one = low_for([state(100 * GB, 19 * GB)])
    check("just under a fifth is", [v["free_pct"] for v in one], [19])
    check("and it says how much is left, not just that it is low",
          [v["free_mb"] for v in one], [19 * 1024])

    print("\nWHAT IS NOT A WARNING, and this is the part that keeps the overview usable")
    check("an EMPTY slot is silent, however little space it reports",
          low_for([state(0, 0, inserted=False)]), [])
    check("a slot reporting nothing at all is silent rather than dividing by zero",
          low_for([state(0, 0, inserted=True)]), [])
    check("a malformed state string is skipped, not crashed on", low_for(["nonsense"]), [])
    check("a short state string is skipped too", low_for(["1|2|3"]), [])
    check("a non-numeric capacity is skipped", low_for(["x|y|0|1|1"]), [])

    # SLOT 1 IS THE USB PORT, not a second card slot. Settled with a pendrive in the unit, where
    # slot 1 reported the drive's capacity byte-for-byte identically to Windows while the card slot
    # read empty. The names live in recorder.VOLUME_NAMES, keyed on the slot INDEX rather than on
    # the position in the list: the unit adds a fourth volume while it is recording, and a
    # position-keyed lookup renames every slot mid-take.
    print("\nNAMING: slot 0 is the card slot, slot 1 the USB port, slot 2 the unit's pad storage")
    named = low_for([state(64 * GB, 1 * GB), state(0, 0, inserted=False),
                     state(5 * GB, 100 * 1024 * 1024)])
    check("the first is the memory card slot", [v["label"] for v in named][0], "Memory card slot")
    check("the last is the internal pad volume", [v["label"] for v in named][-1],
          "Internal storage (SMART pads)")
    check("both were reported, and the empty slot between them was not",
          len(named), 2)

    print("\nA FULL CARD is reported, not treated as a broken read")
    check("nothing free at all", [v["free_pct"] for v in low_for([state(64 * GB, 0)])], [0])

    print("\n" + ("ALL CHECKS PASSED" if not FAILED else f"{FAILED} FAILED"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
