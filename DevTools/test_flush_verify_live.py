"""Does the FAST flush actually get the bytes onto the desk? Writes a pad, then goes and looks.

WHY THIS EXISTS. `rcp_transfer.flush_volume` calls `FlushFileBuffers` on a volume handle rather than
launching PowerShell (`Write-VolumeCache`) per pad, which costs 1.2 to 2.3 seconds and is the
biggest single item in a restore after transfer-mode cycling.

THE FAST PATH HAS A SILENT, EXPENSIVE FAILURE MODE, which is the whole reason for this file: a
flush that does nothing leaves the audio in Windows' cache, the desk gets a truncated or empty file,
and nobody finds out until the pad is pressed. "It looked fine" is not evidence. So this:

  1. creates a sound pad on a FREE slot from a known file,
  2. LEAVES transfer mode entirely and re-enters it, so the volume is remounted and any Windows
     cache for it is gone,
  3. reads the file back off the desk and compares its md5 with the source,
  4. clears the slot again and reports pool health either side.

A pass means the bytes are really on the unit. Anything else means put the PowerShell flush back.

Safe: it writes to a free grid slot, never over one of yours, and clears it afterwards. A sound
create+clear writes nothing to region B, so it cannot leave an effect-object orphan. The server must
be stopped first (it owns the HID handle).

Usage: python DevTools/test_flush_verify_live.py [--slot N]
"""
from __future__ import annotations
import argparse
import hashlib
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import rcp_transfer as t
import soundpad

FAILED = 0


def check(label, got, want):
    global FAILED
    if got == want:
        print(f"  ok   {label}: {got!r}")
    else:
        FAILED += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", type=int, default=None)
    args = ap.parse_args()

    live = soundpad.read_pads_live()
    pads = {p["grid"]: p for p in live["pads"]}
    slot = args.slot
    if slot is None:
        slot = next((p["grid"] for p in sorted(live["pads"], key=lambda x: x["grid"])
                     if not p["present"]), None)
    if slot is None or (pads.get(slot) and pads[slot]["present"]):
        print("No free pad slot to test on, and this never writes over one of yours.")
        return 1
    source = next(((p["grid"], p["file"]) for p in live["pads"]
                   if p["present"] and p["type"] == 1 and p.get("file")), None)
    if source is None:
        print("No sound pad on the unit to borrow a file from.")
        return 1

    print(f"free slot: grid {slot}   borrowing the audio from pad {source[0]} ({source[1]})")
    before_pool = soundpad.pool_health()
    print("pool before:", before_pool)

    # Copy the source off the unit first, so the create has a normal local file to work from.
    tmp = tempfile.mkdtemp(prefix="ac_flush_")
    t.set_transfer_mode(True); time.sleep(1.0)
    t.remount_pad_storage()
    root = t.find_pad_volume()
    if root is None:
        t.set_transfer_mode(False)
        print("the pad volume did not mount")
        return 1
    src_local = os.path.join(tmp, source[1])
    shutil.copyfile(os.path.join(root, "pads", str(source[0]), source[1]), src_local)
    t.set_transfer_mode(False)
    want = md5(src_local)
    size = os.path.getsize(src_local)
    print(f"source: {size / 1024 / 1024:.2f} MB, md5 {want[:12]}")

    started = time.time()
    res = soundpad.create_sound(slot, src_local, name="FlushTest", colour=5)
    took = time.time() - started
    check("the create reported ok", bool(res.get("ok")), True)
    print(f"  create took {took:.2f}s")

    # THE POINT: leave transfer mode completely, come back, and read what the DESK has.
    print("\nleaving transfer mode, remounting, and reading the file back off the unit")
    time.sleep(1.0)
    t.set_transfer_mode(True); time.sleep(1.0)
    t.remount_pad_storage()
    root = t.find_pad_volume()
    got = None
    try:
        if root is None:
            print("  the volume did not come back")
        else:
            on_device = os.path.join(root, "pads", str(slot), os.path.basename(src_local))
            if os.path.isfile(on_device):
                check("the file is on the unit", os.path.getsize(on_device), size)
                got = md5(on_device)
            else:
                listing = []
                folder = os.path.join(root, "pads", str(slot))
                if os.path.isdir(folder):
                    listing = os.listdir(folder)
                print(f"  no such file on the unit. pads/{slot}/ holds: {listing}")
    finally:
        t.set_transfer_mode(False)
    check("and it is byte-for-byte the source", got, want)

    print("\ntidying up")
    soundpad.clear_grid_pad(slot)     # by GRID: clear_pad takes a stream POSITION
    time.sleep(1.0)
    after = soundpad.read_pads_live()
    still = next((p for p in after["pads"] if p["grid"] == slot and p["present"]), None)
    check("the slot is empty again", still is None, True)
    check("the effect-object pool is unchanged",
          soundpad.pool_health()["region_b_count"], before_pool["region_b_count"])
    shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("FAILURES:", FAILED)
    if FAILED:
        print("PUT THE POWERSHELL FLUSH BACK: rcp_transfer.flush_volume should call "
              "_flush_volume_powershell unconditionally until this passes.")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
