"""Offline checks on which drive `rcp_transfer.find_pad_volume` takes for the desk's pad volume.

THE RULES THIS PINS:
  * only a drive on the desk's own USB device is the pad volume, so a USB stick with a copy of the
    pad folder at E: is never taken, before or after the desk's volume mounts;
  * the desk's letter with nothing readable on it (out of transfer mode, or a dead letter left by
    a stuck one) keeps the wait going rather than being taken;
  * with no desk disk present, nothing is taken;
  * inside a held transfer_session every find returns the root the first one found;
  * a desk volume with no `pads` folder is taken only by a wait that allows it, once it has stayed
    that way, and a wait that gives up says which state the desk's drive was left in;
  * the remount is re-sent at the short interval only while the desk's drive has no volume.

No device and no drives: the drive scan, the desk lookup and the volume check are stubbed. The live
lookup is `desk_drives.on_desk`, run by DevTools/test_desk_drives_live.py.

    python DevTools/test_pad_volume.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401
import _testkit
import desk_drives
import rcp_transfer as t
from _testkit import check, check_true

mounted: set[str] = set()            # drives holding a readable `pads` folder
desk: set[str] = {"F:\\", "G:\\"}    # drives on the desk's USB device
other: dict = {}                     # root -> (state, detail) for a drive in any other state
remounts: list = []
real_state = t._volume_state
t._drive_roots = lambda: ["E:\\", "F:\\", "G:\\"]
t._volume_state = lambda root: ((t.READY, "") if root in mounted
                                else other.get(root, (t.ABSENT, "size 0")))
desk_drives.on_desk = lambda roots: {r for r in roots if r in desk}
t.remount_pad_storage = lambda: remounts.append(1)


def find(timeout_s=0.6, **kw):
    kw.setdefault("reissue_remount_every", 60)
    return t.find_pad_volume(timeout_s=timeout_s, **kw)


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
mounted.clear()

print("A DESK VOLUME WITH NO PADS FOLDER")
other["F:\\"] = (t.EMPTY, "no pads folder, 0 entries at the root")
check("a wait that reads does not take it", find(), None)
check("and says so", t.pad_volume_problem(),
      "The desk's pad storage appeared as F:, but it holds no pads folder.")
t.EMPTY_SETTLE_S = 0.3
check("a wait that writes takes it once it has stayed empty", find(empty_ok=True), "F:\\")
check("and leaves no problem behind", t.pad_volume_problem(), "")
t.EMPTY_SETTLE_S = 5.0
check("but not before it has settled", find(empty_ok=True), None)
other.clear()

print("A WAIT THAT GIVES UP SAYS WHY")
other["G:\\"] = (t.UNREADABLE, "[WinError 5] Access is denied")
find()
check("a drive that will not read is named, with the error", t.pad_volume_problem(),
      "The desk's pad storage appeared as G:, but Windows could not read it ([WinError 5] Access "
      "is denied). Security software that blocks or scans removable drives can cause this.")
other.clear()
find()
check("a drive that never got a volume", t.pad_volume_problem(),
      "The desk's pad storage drive, F:, G:, stayed empty in Windows.")
desk.clear()
find()
check("no desk drive at all", t.pad_volume_problem(),
      "Windows did not show the desk's pad storage as a drive. Check the desk is connected "
      "directly by USB.")
desk.update({"F:\\", "G:\\"})

print("THE REMOUNT IS NOT RE-SENT INTO A VOLUME STILL SETTLING")
remounts.clear()
find(timeout_s=1.3, reissue_remount_every=0.4)
check_true("with no volume it is re-sent at the short interval", len(remounts) >= 2)
remounts.clear()
other["F:\\"] = (t.UNREADABLE, "[WinError 87] The parameter is incorrect")
find(timeout_s=1.3, reissue_remount_every=0.4)
check("with a volume not yet readable it waits the longer interval", remounts, [])
other.clear()

print("THE STATE OF A REAL FOLDER")
with tempfile.TemporaryDirectory() as root:
    check("a volume with no pads folder is empty", real_state(root)[0], t.EMPTY)
    os.makedirs(os.path.join(root, "pads", "1"))
    check("a pads folder with no audio is ready", real_state(root)[0], t.READY)
    with open(os.path.join(root, "pads", "1", "sound.wav"), "wb") as fh:
        fh.write(b"RIFF")
    check("and one whose audio reads", real_state(root)[0], t.READY)
check("a letter with nothing behind it is absent",
      real_state(os.path.join(tempfile.gettempdir(), "no such volume"))[0], t.ABSENT)


sys.exit(_testkit.finish())
