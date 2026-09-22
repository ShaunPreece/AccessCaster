"""What does Transfer Mode ACTUALLY mount: only the unit's pad storage, or the recordings drive too?

WHY IT MATTERS. Two claims in the UI rest on one untested assumption, that Transfer Mode exposes pad
storage and nothing else: "recordings can only be deleted on the unit", and "the only way to hear a
recording is the touchscreen". The RODECaster App deletes recordings and plays them back by
entering Transfer Mode and working on the FAT volume, so if the recordings drive mounts here as
well, BOTH claims are wrong and neither the app nor the touchscreen is needed.

`find_pad_volume` cannot answer this: it looks for a `\\pads` folder, so a recordings volume is
invisible to it by construction. This enumerates every removable volume instead, before and after,
and reports what appeared.

RESULT: mode 2 mounts ONLY pad storage (F:, 5033 MB, a `pads` folder, the eight
factory WAVs). The recordings drive does NOT appear. So `--mode` exists to try the OTHER values:
`transferModeType` is an int and the app's pad flow is the only one we have ever watched, so a
different value plausibly selects the recordings volume the app's Transfer Recordings screen gets.

READ-ONLY apart from the mode toggle itself, and the exit is in a `finally`. Nothing is written to
any volume and no recording is touched.

    python DevTools/transfer_volumes.py [--mode N]
"""
from __future__ import annotations
import argparse
import os
import shutil
import string
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import rcp_transfer as _t

AUDIO = (".wav", ".mp3", ".flac", ".aiff", ".aif")


def volumes() -> dict:
    """{'D:\\': (label, total_bytes, fstype)} for every mounted letter above C."""
    out = {}
    for letter in string.ascii_uppercase[3:]:
        root = f"{letter}:\\"
        if not os.path.isdir(root):
            continue
        try:
            total = shutil.disk_usage(root).total
        except OSError:
            total = 0  # a phantom / size-0 mount still deserves reporting, so keep it
        out[root] = total
    return out


def describe(root: str, total: int) -> None:
    print(f"\n  {root}  {total / 1048576:.0f} MB")
    if total == 0:
        print("      size 0: a PHANTOM mount, not usable (see rcp_transfer.find_pad_volume)")
        return
    try:
        entries = sorted(os.listdir(root))
    except OSError as e:
        print(f"      cannot list: {e}")
        return
    print(f"      top level: {', '.join(entries[:14]) or '(empty)'}")
    # Recordings are what we are hunting, so walk shallowly for audio rather than just naming dirs.
    found, n = [], 0
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath.count(os.sep) - root.count(os.sep) > 3:
            dirnames[:] = []
            continue
        for f in filenames:
            if f.lower().endswith(AUDIO):
                n += 1
                if len(found) < 8:
                    found.append(os.path.relpath(os.path.join(dirpath, f), root))
    print(f"      {n} audio file(s)")
    for f in found:
        p = os.path.join(root, f)
        try:
            print(f"        {f}  ({os.path.getsize(p) / 1048576:.1f} MB)")
        except OSError:
            print(f"        {f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", type=int, default=2, help="transferModeType value to enter with")
    args = ap.parse_args()

    before = volumes()
    print("BEFORE transfer mode:")
    for root, total in before.items():
        print(f"  {root}  {total / 1048576:.0f} MB")
    if not before:
        print("  (none above C:)")

    try:
        print(f"\ntransferModeType = {args.mode} (enter) ...")
        _t.send_record(_t.build_global_prop_int("transferModeType", _t.SEL_TRANSFER_MODE,
                                                args.mode))
        time.sleep(1.5)
        print("remountPadStorage ...")
        _t.remount_pad_storage()

        # The re-arm is slow and the first remount is sometimes swallowed, so poll rather than
        # sleeping once; re-issue partway through exactly as find_pad_volume does.
        deadline, reissued = time.time() + 30.0, False
        after = {}
        while time.time() < deadline:
            after = volumes()
            if set(after) - set(before):
                break
            if not reissued and time.time() > deadline - 20.0:
                _t.remount_pad_storage()
                reissued = True
            time.sleep(1.0)

        new = sorted(set(after) - set(before))
        print(f"\n{len(new)} NEW volume(s): {', '.join(new) or 'none'}")
        for root in new:
            describe(root, after[root])
        gone = sorted(set(before) - set(after))
        if gone:
            print(f"\nvolumes that DISAPPEARED: {', '.join(gone)}")
    finally:
        print("\nexiting transfer mode ...")
        _t.set_transfer_mode(False)
        time.sleep(1.5)
        _t.remount_pad_storage()
        time.sleep(2.0)
        print(f"volumes now: {', '.join(volumes()) or '(none above C:)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
