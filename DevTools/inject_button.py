"""TEST: inject physical-button events from the host.

The device reports button presses as report-04 pushes. Replayed as report-03 (host->device), they
emulate the press:
  mute : 'mutePressed'   compact-bool  (down=02, up=03) -> device toggles Mic mute
  solo : 'soloPressed'   compact-bool  (down=02, up=03) -> device toggles channel cue
  bank : 'selectedBank'  global int on SEL_GUI          -> sets the pad bank
Both presses sit at path [0, 0x63], the first SOLOMUTEBUTTON.

Usage:
  python inject_button.py mute
  python inject_button.py solo
  python inject_button.py bank <n>
"""
import sys
import time

import _bootstrap  # noqa: F401  - puts Project/ on sys.path
import rcp_read as rr
import rcp_transfer as t

BUTTON_PATH = [0, 0x63]


def press(name: str) -> None:
    """Down, then up, on the first SOLOMUTEBUTTON."""
    t.send_record(t.build_path_prop_bool(name, True, BUTTON_PATH))
    time.sleep(0.12)
    t.send_record(t.build_path_prop_bool(name, False, BUTTON_PATH))
    time.sleep(0.12)


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "mute":
        print("injecting mutePressed (down+up) ...")
        press("mutePressed")
        print("sent. ASK USER: did the mic mute or unmute?")
    elif cmd == "solo":
        before = [v for v, k in rr.find_all(rr.read_model_stable(), "channelCueEnable")]
        print(f"channelCueEnable before: {before[:4]}")
        press("soloPressed")
        time.sleep(0.3)
        after = [v for v, k in rr.find_all(rr.read_model_stable(), "channelCueEnable")]
        print(f"channelCueEnable after:  {after[:4]}")
        print("CHANGED" if before != after else "no change")
    elif cmd == "bank":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        before = [v for v, k in rr.find_all(rr.read_model_stable(), "selectedBank")]
        print(f"selectedBank before: {before}")
        t.send_record(t.build_global_prop_int("selectedBank", t.SEL_GUI, n))
        time.sleep(0.3)
        after = [v for v, k in rr.find_all(rr.read_model_stable(), "selectedBank")]
        print(f"selectedBank after:  {after}  (asked for {n})")
        print("CHANGED" if before != after else "no change")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
