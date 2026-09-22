"""Play a pad over the desk's MIDI port from the command line.

THE MODULE ITSELF LIVES AT `Project/midi_out.py`, because the app fires pads over MIDI and a feature
cannot import from DevTools. This is the command line around it and nothing else.

    python DevTools/midi_out.py --pad 1              # play pad 1 in the current bank
    python DevTools/midi_out.py --pad 1 --hold 3     # press, wait 3 s, release
    python DevTools/midi_out.py --pad 1 --bank 2     # select bank 2 first, and put the bank back
    python DevTools/midi_out.py --ports              # list MIDI outs
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401  (puts Project/ on sys.path)

import midi_out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ports", action="store_true", help="list MIDI OUT ports and stop")
    ap.add_argument("--pad", type=int, help="pad number to play, 1-based, in the current bank")
    ap.add_argument("--bank", type=int, help="1-based bank to select first, then restore")
    ap.add_argument("--hold", type=float, default=0.05, help="seconds to hold the press")
    ap.add_argument("--no-enable", action="store_true",
                    help="do not touch systemMidiControl (use when it is already on)")
    args = ap.parse_args()

    if args.ports or args.pad is None:
        for i, name in enumerate(midi_out.ports()):
            print(f"  {i}  {name!r}")
        print(f"\ndesk port: {midi_out.find_port()}")
        return 0

    def fire():
        with midi_out.MidiOut() as m:
            print(f"CC {midi_out.CC_PLAY_PAD} on channel {args.pad} (pad {args.pad}), 127 then 0")
            m.play_pad(args.pad, args.hold)

    def fire_in_bank():
        if args.bank is None:
            return fire()
        import soundpad
        was = soundpad.current_bank()
        soundpad.select_bank(args.bank - 1)
        try:
            fire()
        finally:
            if was is not None:
                soundpad.select_bank(was)
                print(f"bank restored to {was + 1}")

    if args.no_enable:
        fire_in_bank()
    else:
        with midi_out.midi_enabled() as was_on:
            print(f"systemMidiControl was {'ON' if was_on else 'OFF'}")
            fire_in_bank()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
