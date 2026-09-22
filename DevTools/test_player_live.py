"""Can the unit's own PLAYER be driven from software? Load a file, press play, watch it move.

WHY THIS IS THE INTERESTING ONE. Playing a recording back is the biggest thing left on the app's
own Limitations list: today a blind operator can make a recording and has no way to hear it without
the touchscreen. The PLAYER block holds a file path, a state, a speed and a PROGRESS figure, and
that last one is what makes this testable with nobody in the room - if the desk is really playing,
`playerProgress` and `playerCurrentPositionTime` climb. No ears needed for the proof, only for the
pleasure.

SELECTOR: PLAYER = 0x04, PROVEN (a read-back-checked write, playerSpeed 0 -> 1 ->
0, landed in the PLAYER block, no stray, no occurrence count moved anywhere). It was DERIVED before
that, bracketed between the confirmed RECORDER 0x03 and ENCODER 0x06.

WHAT IT PLAYS. With no card or drive in the unit there are no recordings, so this uses one of the
factory SMART pad WAVs on the unit's own internal storage instead. That is a fair test of the
transport: same player, same file path field, a real file on a mounted volume.

RESTORE. `playerState` goes back to 0 and the path back to what it was, in a `finally`, and the
model length is compared so a stray would show.

    python DevTools/test_player_live.py --list
    python DevTools/test_player_live.py --play
"""
from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import rcp_read
import rcp_transfer as _t

SEL_PLAYER = 0x04
FIELDS = ("playerState", "playerFilePath", "playerProgress", "playerCurrentPositionTime",
          "playerFileSize", "playerSpeed")


def read() -> tuple[dict, bytes]:
    blob = rcp_read.read_model_stable(fresh=True)
    out = {}
    for p in FIELDS:
        hits = rcp_read.find_all(blob, p)
        out[p] = hits[0][0] if hits else None
    return out, blob


def show(tag: str, v: dict) -> None:
    print(f"  {tag:<10} state={v['playerState']!r} progress={v['playerProgress']!r} "
          f"pos={v['playerCurrentPositionTime']!r} size={v['playerFileSize']!r} "
          f"path={v['playerFilePath']!r}")


def candidates(blob: bytes) -> list:
    """Something to play: a recording if there is one, otherwise a factory pad WAV."""
    recs = [v for v, k in rcp_read.find_all(blob, "recordingContent") if k == "str"]
    paths = [str(r).split("|")[2] for r in recs if len(str(r).split("|")) > 2]
    if paths:
        return paths
    return [p for p, k in rcp_read.find_all(blob, "padFilePath") if k == "str" and p]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="show what is playable and stop")
    ap.add_argument("--play", action="store_true", help="load a file and press play")
    ap.add_argument("--state", type=int, default=1, help="the value to write to playerState")
    ap.add_argument("--seconds", type=float, default=4.0, help="how long to watch it")
    args = ap.parse_args()

    before, blob0 = read()
    show("BEFORE", before)
    files = candidates(blob0)
    print(f"\n{len(files)} playable file(s); first few:")
    for f in files[:5]:
        print("   ", f)
    if args.list or not args.play:
        return 0
    if not files:
        print("nothing to play.")
        return 1

    target = files[0]
    print(f"\nLOADING {target}")
    try:
        _t.send_record(_t.build_global_prop_string("playerFilePath", target, SEL_PLAYER))
        time.sleep(1.2)
        loaded, _ = read()
        show("LOADED", loaded)
        # A file size the unit filled in ITSELF is the first real signal: it means the path was
        # accepted and the file opened, before a single sample is played.
        print(f"  the unit "
              f"{'DID' if loaded['playerFileSize'] else 'did NOT'} report a file size back")

        print(f"\nPLAY (playerState = {args.state})")
        _t.send_record(_t.build_global_prop_int("playerState", SEL_PLAYER, args.state))
        deadline = time.time() + args.seconds
        seen = []
        while time.time() < deadline:
            now, _ = read()
            seen.append((now["playerState"], now["playerProgress"],
                         now["playerCurrentPositionTime"]))
            show("PLAYING", now)
        moved = len({s[1] for s in seen}) > 1 or len({s[2] for s in seen}) > 1
        print(f"\n  progress {'MOVED - the unit is playing' if moved else 'never moved'}")
    finally:
        print("\nrestoring...")
        _t.send_record(_t.build_global_prop_int("playerState", SEL_PLAYER, 0))
        time.sleep(1.0)
        _t.send_record(_t.build_global_prop_string("playerFilePath",
                                                   str(before["playerFilePath"] or ""), SEL_PLAYER))
        time.sleep(1.0)
        after, blob2 = read()
        show("AFTER", after)
        for k in FIELDS:
            if after[k] != before[k]:
                print(f"  *** {k} did not come back: {before[k]!r} -> {after[k]!r}")
        print(f"  model length {len(blob0)} -> {len(blob2)}"
              + ("  OK, no stray" if len(blob0) == len(blob2) else "  *** LENGTH CHANGED ***"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
