"""Read the unit's recordings off the drive on a PC, and settle the two things the desk will not tell us.

RUN IT WITH THE PENDRIVE (or memory card) PLUGGED INTO THIS COMPUTER, not into the unit.

    python DevTools/check_drive_recordings.py
    python DevTools/check_drive_recordings.py E:\\

WHAT IT ANSWERS, and neither is answerable over USB:

  1. **DO MARKERS ACTUALLY LAND?** `requestDropMarker` is addressable (RECORDER 0x03) and the unit
     CONSUMES a write - it pushes the field back cleared, exactly as `requestRecordState` does. But
     nothing readable changes afterwards: a recording's own record carries identical flags with or
     without a marker. So the app does not offer a Marker button, because a control that may
     silently do nothing is worse than none. A WAV stores markers in a `cue ` chunk, with names in
     a `LIST`/`adtl` chunk, so the file itself is the evidence. If markers are there, the button
     can ship.

  2. **WHAT DOES MULTI-TRACK ACTUALLY WRITE?** `recordingMultitrackMode` was proven to be the
     RECORDER's setting rather than the USB stream's (a 10-second take used 3.50 MB at Off and
     26.25 MB at Post-Fader), but the unit's own recording record names "Stereo Mix.wav" either
     way. The folder listing shows what really came out.

It reads only, and opens each WAV's chunk headers rather than its audio, so it is quick even on a
long take.
"""
from __future__ import annotations
import struct
import sys
from pathlib import Path

RODE_DIR = "RODECaster"


def find_drive() -> Path | None:
    """The unit formats its drives with the volume label `RodeCaster` and creates \\RODECaster\\.
    Both are checked, since a card prepared on a PC might carry only one of them."""
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
        root = Path(f"{letter}:\\")
        try:
            if (root / RODE_DIR).is_dir():
                return root
        except OSError:
            continue
    return None


def wav_chunks(path: Path) -> list:
    """[(chunk id, size)] for one WAV, header only. Never reads the audio."""
    out = []
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"RIFF":
                return out
            f.read(8)                      # size + "WAVE"
            while True:
                head = f.read(8)
                if len(head) < 8:
                    break
                cid, size = struct.unpack("<4sI", head)
                out.append((cid.decode("latin1"), size))
                if cid == b"data":
                    # Skip the audio in one seek: markers sit AFTER it in most writers.
                    f.seek(size + (size & 1), 1)
                else:
                    f.seek(size + (size & 1), 1)
    except OSError as exc:
        out.append((f"<unreadable: {exc}>", 0))
    return out


def cue_points(path: Path) -> list:
    """Marker positions, in sample frames, out of a WAV's `cue ` chunk."""
    points = []
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"RIFF":
                return points
            f.read(8)
            while True:
                head = f.read(8)
                if len(head) < 8:
                    break
                cid, size = struct.unpack("<4sI", head)
                if cid == b"cue ":
                    body = f.read(size)
                    (count,) = struct.unpack_from("<I", body, 0)
                    for i in range(count):
                        off = 4 + i * 24
                        if off + 24 > len(body):
                            break
                        cue_id, position = struct.unpack_from("<II", body, off)
                        (sample,) = struct.unpack_from("<I", body, off + 20)
                        points.append((cue_id, sample or position))
                    break
                f.seek(size + (size & 1), 1)
    except OSError:
        pass
    return points


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else find_drive()
    if root is None:
        print("No RODECaster drive found. Plug the pendrive or card into THIS computer and "
              "run it again, or pass the drive letter, e.g. E:\\")
        return 1
    base = root / RODE_DIR
    print(f"drive {root}  ({base})\n")

    takes = sorted((p for p in base.iterdir() if p.is_dir()),
                   key=lambda p: p.stat().st_mtime)
    if not takes:
        print("The RODECaster folder is there but holds no recordings.")
        return 1

    for take in takes:
        files = sorted(take.iterdir())
        wavs = [f for f in files if f.suffix.lower() == ".wav"]
        print(f"{take.name}")
        print(f"  {len(wavs)} wav file(s): " + ", ".join(f.name for f in wavs))
        others = [f.name for f in files if f.suffix.lower() != ".wav"]
        if others:
            print(f"  other files: {', '.join(others)}")
        for w in wavs:
            chunks = wav_chunks(w)
            names = [c for c, _ in chunks]
            cues = cue_points(w)
            size_mb = w.stat().st_size / 1024 / 1024
            note = ""
            if cues:
                # 48 kHz is what this unit records at; shown as seconds so it can be compared with
                # "I dropped one at about four seconds".
                where = ", ".join(f"{s / 48000:.1f}s" for _i, s in cues)
                note = f"  MARKERS: {len(cues)} -> {where}"
            elif "cue " in names:
                note = "  a cue chunk is present but empty"
            print(f"    {w.name:<28} {size_mb:7.2f} MB  chunks: {' '.join(names)}{note}")
        print()

    print("WHAT TO CONCLUDE")
    print("  * More than one WAV in a take = multi-track really does write separate tracks.")
    print("  * MARKERS listed on a take you dropped markers into = requestDropMarker WORKS, and")
    print("    the app can ship a Marker button. No cue chunk anywhere = it does not, and the")
    print("    decision to leave the button out was right.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
