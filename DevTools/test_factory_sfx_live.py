"""Are the FACTORY PAD SOUNDS on the desk itself, so the app need not ship them?

THE QUESTION, and it is the one that let the app stop shipping RODE's audio. The pad RECORDS point
at `/Application/emmc-data/pads/<n>/<Name>.wav` on the desk's own internal partition, in every
capture including a post-factory-reset one - but a record is a pointer, and a pointer is not a file.
This looks at the filesystem, which is the only thing that can tell them apart.

WHAT IT DOES. Enters transfer mode, mounts the pad volume, LISTS it, and leaves. It writes nothing
at all: no pad record, no file, no deletion. The only device state it changes is transfer mode
itself, which it always puts back.

THE COST, so nobody is surprised: entering transfer mode DROPS THE DESK'S AUDIO for a second or two
and re-presents a USB mass-storage gadget, which makes Windows play its device-connect sound. That
is unavoidable - the volume is not mountable any other way - and it is why this is a `_live` script
you run deliberately.

WHAT THE ANSWER MEANS:
  * every factory WAV present  -> the desk restored its own, which is what makes a factory reset
    the honest answer to "put my factory sounds back".
  * some missing               -> those pads have been replaced since the last reset and
    `tidy_pad_folder` removed the original, which is expected and is why the app never offers to
    restore a factory SOUND itself.

Run: python DevTools/test_factory_sfx_live.py
"""
import _bootstrap  # noqa: F401  (puts Project/ on the path; import FIRST)

import os
import time

import rcp_read
import rcp_transfer as t
import soundpad

# The eight factory sounds, by grid pad number. WRITTEN OUT HERE rather than imported, because the
# app deliberately holds no table of them any more: it neither ships them nor restores them, so a
# list in `soundpad` would exist only to serve this check. This is a test EXPECTATION, which is the
# one place the names still belong.
EXPECTED = {
    1: "Music_Bed.wav", 2: "Applause.wav", 3: "Crickets.wav", 4: "Trombone.wav",
    5: "Airhorn.wav", 6: "Record_Scratch.wav", 7: "Chimes.wav", 8: "Rim_Shot.wav",
}


def main() -> int:
    print("Reading the pad table (no transfer mode yet)...")
    blob = rcp_read.read_model_stable(fresh=True)
    live = {}
    for pos, pidx, ptype, name in soundpad.pad_positions(blob):
        if ptype == soundpad.PAD_TYPE_SOUND and pidx is not None:
            live[pidx + 1] = name
    paths = [p for p in rcp_read.values(blob, "padFilePath") if p]
    print(f"  {len(live)} sound pads, {len(paths)} with a file path")
    for grid in sorted(EXPECTED):
        print(f"   grid {grid:2}: record says {live.get(grid, '(not a sound pad)')!r}")

    print("\nMounting the pad volume. The desk's audio will drop for a moment.")
    root = None
    t.set_transfer_mode(True)
    try:
        time.sleep(1.0)
        t.remount_pad_storage()
        root = t.find_pad_volume()
        if root is None:
            print("  the pad volume did not mount; nothing was changed.")
            return 2
        print(f"  mounted at {root}")
        found, missing, extra = {}, [], {}
        for grid, fname in sorted(EXPECTED.items()):
            folder = os.path.join(root, "pads", str(grid))
            with t.quiet_drive_errors():
                names = sorted(os.listdir(folder)) if os.path.isdir(folder) else []
            if fname in names:
                size = os.path.getsize(os.path.join(folder, fname))
                found[grid] = (fname, size)
            else:
                missing.append((grid, fname))
            others = [n for n in names if n != fname]
            if others:
                extra[grid] = others

        print("\nFACTORY WAVs ON THE DESK")
        for grid, (fname, size) in found.items():
            print(f"  grid {grid:2}  {fname:22} {size/1024/1024:6.2f} MB")
        for grid, fname in missing:
            print(f"  grid {grid:2}  {fname:22} MISSING")
        if extra:
            print("\nOther files in those folders (a replaced pad leaves its own sound here):")
            for grid, names in extra.items():
                print(f"  grid {grid:2}  {', '.join(names)}")

        print()
        if not missing:
            print("ALL EIGHT ARE ON THE DESK, so a factory reset is a real route back to any one "
                  "of them and the app is right not to carry copies.")
        else:
            print(f"{len(missing)} of {len(EXPECTED)} are gone from the desk. Those pads have been "
                  f"replaced at some point and the original was tidied away, so 'the desk has "
                  f"them' holds only until a pad is replaced.")
        return 0
    finally:
        t.set_transfer_mode(False)
        print("\nTransfer mode off; the desk's audio is back. Nothing was written.")


if __name__ == "__main__":
    raise SystemExit(main())
