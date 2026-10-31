"""Offline checks on which drive `rcp_transfer.find_pad_volume` takes for the desk's pad volume.

THE RULES THIS PINS:
  * a drive holding a `pads` folder before transfer mode is entered is never the pad volume, so a
    USB stick with a copy of the pad folder at E: loses to the desk's volume at F:;
  * the desk's own volume, still mounted from a session that has just ended, is accepted again;
  * inside a held transfer_session every find returns the root the first one found.

No device and no drives: the drive scan, the volume check and the serial are stubbed.

    python DevTools/test_pad_volume.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401
import rcp_transfer as t

FAILS = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILS.append(f"{name}: got {got!r}, wanted {want!r}")
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")


mounted: set[str] = set()
SERIALS = {"E:\\": 0x1111, "F:\\": 0x2222, "G:\\": 0x3333}
t._drive_roots = lambda: ["E:\\", "F:\\", "G:\\"]
t._is_pad_volume = lambda root: root in mounted
t._volume_serial = lambda root: SERIALS.get(root)
t.remount_pad_storage = lambda: 0


def find():
    return t.find_pad_volume(timeout_s=0.6, reissue_remount_every=60)


print("A DRIVE PRESENT BEFORE ENTRY IS NOT THE PAD VOLUME")
mounted.add("E:\\")                     # the USB stick with a copy of the pad folder
t._note_volumes_before_entry()
mounted.add("F:\\")                     # the desk's volume, after entry
check("the new volume is taken over the one already there", find(), "F:\\")
mounted.discard("F:\\")
check("with no new volume, nothing is taken", find(), None)

print("THE DESK'S OWN VOLUME IS ACCEPTED AGAIN")
mounted.add("F:\\")                     # still mounted from the session that just ended
t._note_volumes_before_entry()
check("the desk's volume, already mounted at entry, is taken", find(), "F:\\")

print("A HELD SESSION KEEPS ITS ROOT")
mounted.clear()
t._note_volumes_before_entry()
t._transfer_hold = 1
try:
    mounted.add("G:\\")
    check("the first find in the session", find(), "G:\\")
    mounted.add("F:\\")
    check("a later find returns the same root", find(), "G:\\")
finally:
    t._transfer_hold = 0
    t._session_pad_root = None


if FAILS:
    print(f"\n{len(FAILS)} FAILED:")
    for f in FAILS:
        print("  " + f)
    sys.exit(1)
print("\nAll pad volume checks passed.")
