"""EAR TEST for the monitor output: does `outputMonLevel` actually change the speakers, and does
`outputMonFixed` actually override it?

ANSWER, PROVEN ON HARDWARE: BOTH WORK. See the two rules at the bottom of this
docstring before changing anything here.

WHY IT NEEDED EARS. Both properties only show up as SOUND ON THE REAR LINE OUTS. Everything proved
about them before this was a read-back, and a read-back only proves the desk STORED a value: this
desk has a list of properties that accept a write, read back exactly as written, and do nothing
whatever (`padRecordStateRequest`, `storageVolumeTransfer`, `toneGeneration`). So this needs
something plugged into the monitor output and somebody listening to it.

    python DevTools/test_monitor_output_live.py --block off   # control: should fluctuate
    python DevTools/test_monitor_output_live.py --block on     # should stay flat

TWO RULES THIS TOOL EXISTS TO ENFORCE:

1. **RUN ONE BLOCK PER INVOCATION, AND TELL THE LISTENER WHAT TO EXPECT FIRST.** Running "fixed
   off" and "fixed on" back to back in one go leaves the listener unable to say which block they
   are in, so "did block 2 differ from block 1" has no honest answer. Sending the instructions in
   the same message that starts the sweep is worse: a screen reader is still reading "listen for
   this" while the sweep is half over, so the listener hears the test before the description of it.
   So: ONE block, described and agreed BEFORE the process starts, counting down before it touches
   anything.

2. **READ EVERY WRITE BACK WITH `fresh=True`.** This script writes THROUGH THE SERVER and reads in
   ITS OWN PROCESS, and `rcp_read`'s dump cache is invalidated by a write-generation counter that is
   PER PROCESS. The server's writes do not invalidate this process's cache, so a plain read returns
   the pre-write value until the TTL expires, and the conclusion comes out confidently wrong ("the
   level writes are landing nowhere") while the writes land perfectly.

DO NOT SPEAK THE CUES THROUGH THE DESK. Playing this script's own speech through the desk's
playback endpoint, so that the signal carries its own label, reaches NOBODY: not one of those lines
is audible to a listener who hears music through the same speakers throughout. That route does not
reach the monitor output and is an open question in its own right. Use whatever the
listener already has playing; it is a better signal and it is one less thing that can be silently
broken.

SAFETY. It restores the level, Fixed and mute in a `finally` and reads all three back. The
listener's SCREEN READER shares this output, so it goes quiet along with everything else at the low
end of the sweep; that is expected and is why the sweep is short.
"""
from __future__ import annotations
import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import rcp_read

BASE = "http://127.0.0.1:8770"


def post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def read_state() -> dict:
    """ALWAYS fresh=True. See rule 2 in the module docstring."""
    blob = rcp_read.read_model(fresh=True)
    out = {}
    for key in ("outputMonLevel", "outputMonFixed", "outputMonMute"):
        hits = rcp_read.find_all(blob, key)
        out[key] = hits[0][0] if hits else None
    return out


def set_level(percent: int) -> float:
    """Set the level and return WHAT THE DESK HOLDS, not what the API claimed. The endpoint reports
    success from the write returning, which is not evidence of anything."""
    resp = post("/api/monitor/level", {"percent": int(percent)})
    time.sleep(0.4)
    got = read_state()["outputMonLevel"]
    ok = isinstance(got, float) and abs(got - percent / 100.0) < 0.02
    print(f"    level {percent:>3}% -> api ok={resp.get('ok')}, desk holds {got}"
          f"{'' if ok else '   *** DID NOT LAND ***'}", flush=True)
    return got


def set_fixed(on: bool) -> bool:
    resp = post("/api/device/setting", {"prop": "outputMonFixed", "value": 1 if on else 0})
    time.sleep(0.4)
    got = read_state()["outputMonFixed"]
    print(f"    fixed {'ON ' if on else 'OFF'} -> api ok={resp.get('ok')}, desk holds {got}"
          f"{'' if bool(got) == on else '   *** DID NOT LAND ***'}", flush=True)
    return bool(got)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--block", choices=("on", "off"), required=True,
                    help="'off' is the control (should fluctuate); 'on' tests Fixed (should not)")
    ap.add_argument("--quiet-level", type=int, default=10)
    ap.add_argument("--loud-level", type=int, default=100)
    ap.add_argument("--alternations", type=int, default=6)
    ap.add_argument("--gap", type=float, default=3.0)
    ap.add_argument("--countdown", type=float, default=5.0,
                    help="seconds before the first write, so a screen reader can finish the brief")
    args = ap.parse_args()

    before = read_state()
    print(f"BEFORE: {before}", flush=True)
    if before["outputMonMute"] is True:
        print("\nThe monitor output is MUTED. Unmute it and run this again: every step would be "
              "silent and the test would prove nothing.")
        return 2

    fixed_on = args.block == "on"
    print(f"\nBLOCK: fixed {'ON' if fixed_on else 'OFF'}. The level will alternate between "
          f"{args.quiet_level}% and {args.loud_level}%, {args.alternations} times, "
          f"{args.gap}s apart.")
    print("EXPECTED: " + ("no fluctuation at all, apart from one step as Fixed engages and "
                          "another as it is put back."
                          if fixed_on else
                          "the listener hears every one of them. This is the CONTROL."))
    print(f"starting in {args.countdown:.0f}s", flush=True)
    time.sleep(max(0.0, args.countdown))

    try:
        set_fixed(fixed_on)
        time.sleep(2.0)
        for i in range(args.alternations):
            pct = args.loud_level if i % 2 else args.quiet_level
            print(f"  {i + 1}/{args.alternations}", flush=True)
            set_level(pct)
            time.sleep(args.gap)
    finally:
        print("\nRESTORING", flush=True)
        set_fixed(bool(before["outputMonFixed"]))
        if isinstance(before["outputMonLevel"], float):
            post("/api/monitor/level", {"percent": round(before["outputMonLevel"] * 100)})
        time.sleep(0.8)
        after = read_state()
        print(f"AFTER:  {after}", flush=True)
        same = (abs((after["outputMonLevel"] or 0) - (before["outputMonLevel"] or 0)) < 0.02
                and after["outputMonFixed"] == before["outputMonFixed"]
                and after["outputMonMute"] == before["outputMonMute"])
        print("  restored clean" if same else "  *** NOT RESTORED, put it back by hand ***")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
