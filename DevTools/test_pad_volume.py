"""Offline checks on which drive `rcp_transfer.find_pad_volume` takes for the desk's pad volume.

THE RULES THIS PINS:
  * only a drive on the desk's own USB device is the pad volume, so a USB stick with a copy of the
    pad folder at E: is never taken, before or after the desk's volume mounts;
  * the desk's letter with nothing readable on it (out of transfer mode, or a dead letter left by
    a stuck one) keeps the wait going rather than being taken;
  * with no desk disk present, nothing is taken;
  * inside a held transfer_session every find returns the root the first one found.

No device and no drives: the drive scan, the desk lookup and the volume check are stubbed. The live
lookup is `desk_drives.on_desk`, run by DevTools/test_desk_drives_live.py.

    python DevTools/test_pad_volume.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401
import _testkit
import desk_drives
import rcp_transfer as t
from _testkit import check

mounted: set[str] = set()            # drives holding a readable `pads` folder
desk: set[str] = {"F:\\", "G:\\"}    # drives on the desk's USB device
t._drive_roots = lambda: ["E:\\", "F:\\", "G:\\"]
t._is_pad_volume = lambda root: root in mounted
desk_drives.on_desk = lambda roots: {r for r in roots if r in desk}
t.remount_pad_storage = lambda: 0


def find():
    return t.find_pad_volume(timeout_s=0.6, reissue_remount_every=60)


print("A DRIVE NOT ON THE DESK IS NEVER THE PAD VOLUME")
mounted.add("E:\\")                     # the USB stick with a copy of the pad folder
check("the stick alone is not taken", find(), None)
mounted.add("F:\\")                     # the desk's volume, after entry
check("the desk's volume is taken over the stick", find(), "F:\\")
mounted.clear()

print("THE DESK'S LETTER WITH NOTHING READABLE ON IT IS NOT TAKEN")
check("an empty or dead desk letter keeps the wait going", find(), None)
mounted.add("F:\\")
check("the same letter is taken once it reads", find(), "F:\\")
mounted.clear()

print("WITH NO DESK DISK, NOTHING IS TAKEN")
desk.clear()
mounted.update({"E:\\", "F:\\"})
check("no desk disk, no pad volume", find(), None)
desk.update({"F:\\", "G:\\"})
mounted.clear()

print("A HELD SESSION KEEPS ITS ROOT")
t._transfer_hold = 1
try:
    mounted.add("G:\\")
    check("the first find in the session", find(), "G:\\")
    mounted.add("F:\\")
    check("a later find returns the same root", find(), "G:\\")
finally:
    t._transfer_hold = 0
    t._session_pad_root = None


sys.exit(_testkit.finish())
