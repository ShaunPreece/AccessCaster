"""Does a pad bank change leave the encoder bound to nothing, and what binds it again?

Each step injects one encoder turn (`encoderSignal`, +1) and looks for ANY level that moved: the
monitor, a virtual fader, a headphone pot or a channel gain. Something moved means the encoder is
bound; the same turn back (-1) undoes it. Nothing moved means a dead turn.

    1. a turn as found
    2. after a bank change and back (what a pad restore does)
    3. if that left it dead: after `systemChannelSelected` 0 then -1 (select Mic 1, deselect),
       which no turn is made between, so no gain moves

`rebind` runs only the software double press (`systemChannelSelected` 0 then -1), then one turn:
whether it puts the encoder on the virtual faders and monitor, as a real double press does.

A TURN MOVES WHATEVER THE TOUCHSCREEN HAS SELECTED, which can be Mic 1's gain whatever
`systemChannelSelected` says. Run only with the owner's say-so, while Mic 1 is not in use.

Every level is checked against the start at the end and written back if a turn was not undone.
Writes: encoderSignal, selectedBank (back to where it was), systemChannelSelected (back to -1).
Quit AccessCaster first (`python DevTools/app.py quit`).

Run: python DevTools/probe_encoder_bind.py [rebind]
"""
from __future__ import annotations

import sys
import time

import _bootstrap  # noqa: F401
import _desk
import model_tree
import rcp_read
import rcp_transfer as t
import soundpad

ENCODER = ("ENCODER", 0)
LEVELS = ("outputMonLevel", "faderLevel", "potLevel", "inputMicrophoneGain", "inputDigitalGain")
STATE = ("encoderColour", "systemChannelSelected", "systemMixSelected", "selectedBank")
SETTLE_S = 0.8


def read():
    blob = rcp_read.read_model_stable(fresh=True)
    return ({name: list(rcp_read.values(blob, name)) for name in LEVELS},
            {name: rcp_read.first(blob, name) for name in STATE})


def moved(before, after):
    return {name: [(i, a, b) for i, (a, b) in enumerate(zip(before[name], after[name])) if a != b]
            for name in LEVELS if before[name] != after[name]}


def turn(step: int) -> None:
    path = t.address_of(ENCODER, "encoderSignal")
    t.send_record(t._frame(t.build_address(path) + b"encoderSignal\x00"
                           + model_tree.encode_value([(step, "int"), (True, "bool")], "pair")))
    time.sleep(SETTLE_S)


def bound(label: str) -> bool:
    before, state = read()
    turn(+1)
    after, _ = read()
    change = moved(before, after)
    if change:
        turn(-1)
    print(f"{label}: {'BOUND' if change else 'DEAD'}  state={state}  moved={change}")
    return bool(change)


def put_back(start) -> None:
    now, _ = read()
    left = moved(start, now)
    if not left:
        print("every level is as it was at the start")
        return
    print(f"NOT BACK, writing the start values: {left}")
    for i, _was, _now in left.get("outputMonLevel", []):
        t.set_monitor_level(start["outputMonLevel"][i])
    for i, _was, _now in left.get("faderLevel", []):
        t.set_fader_level(i, start["faderLevel"][i])
    time.sleep(SETTLE_S)
    rest = moved(start, read()[0])
    print("now back" if not rest else f"STILL DIFFERENT, by hand: {rest}")


def double_press() -> None:
    """A channel button pressed twice, from software: select Mic 1, then nothing."""
    t.write_at(t.SYSTEM, "systemChannelSelected", "int", 0)
    time.sleep(SETTLE_S)
    t.write_at(t.SYSTEM, "systemChannelSelected", "int", -1)
    time.sleep(SETTLE_S)


def main() -> int:
    server = _desk.service()
    with server._hid_lock:
        start, state = read()
        bank = state["selectedBank"]
        print(f"start: {state}")
        if sys.argv[1:] == ["rebind"]:
            double_press()
            bound("after a software double press")
            put_back(start)
            print(f"end: {read()[1]}")
            return 0
        bound("1. as found")

        soundpad.select_bank(1 if bank == 0 else 0)
        time.sleep(SETTLE_S)
        soundpad.select_bank(bank)
        time.sleep(SETTLE_S)
        if not bound("2. after a bank change and back"):
            double_press()
            bound("3. after selecting and deselecting a channel")
        put_back(start)
        print(f"end: {read()[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
