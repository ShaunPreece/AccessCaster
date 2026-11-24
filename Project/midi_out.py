"""Fire the desk's own controls over its class-compliant USB MIDI port.

WHY THIS EXISTS. A SMART pad cannot be played over the HID control interface: writing `padActive`
engages voice-FX and mixer pads but a SOUND pad sets the flag and plays nothing, and
`padButtonPressed` is stored and ignored (both settled negatives, RODECASTER_PROTOCOL 8.1). The desk
DOES play a pad when the same request arrives on its MIDI endpoint, named "MIDI function". That is
what lets a keyboard shortcut play any pad on the desk.

THE ADDRESSING RULE, and it is the opposite way round from what you would guess: the CC NUMBER is
the FUNCTION and the MIDI CHANNEL is WHICH pad. Play-a-pad is CC 35, so pad 1 is status 0xB0 and
pad 8 is 0xB7. A play is value 127 then value 0, i.e. press then release.

`systemMidiControl` must be ON or the desk ignores the port entirely. That is an ordinary HID write
(`rcp_transfer.set_midi_control`) and `pad_shortcuts` arms it at the point of use;
`midi_enabled()` here is the TOOL shape, which puts the setting back afterwards.

THE PORT IS OPENED ONCE AND KEPT. Opening it is not free and a pad shortcut has to be instant, so
this module owns one process-wide handle behind a lock, opens it lazily on the first send and
REOPENS it once if a send fails, which is what a replug looks like from here. Nothing about it is
exclusive, so holding it open cannot lock another program out.

BANK SELECT IS NOT A MIDI PROBLEM. MIDI reaches only the bank the desk is on, and the bank is an
ordinary HID write: `soundpad.select_bank`. Do not go looking for the CC 0 / CC 1 bank select in
RODE's spec; it is marked "verify" and it is not needed.

The platform supplies the ports, with no package and no driver:
  - `ports()`: every MIDI output's name.
  - `find(name)`: the id of the output named `name`, or None.
  - `open_port(name)`: a handle on it. Raises RuntimeError saying what to do when it cannot.
  - `send(handle, status, data1, data2)`: one short message. Raises OSError when it is refused.
  - `close_port(handle)`: never raises.
"""
from __future__ import annotations

import contextlib
import sys
import threading
import time

import write_guard

if sys.platform == "win32":
    from win.midi_out import close_port, find, open_port, ports, send
else:
    raise ImportError(f"No MIDI output for {sys.platform}: the Windows one is win/midi_out.py")

PORT_NAME = "MIDI function"
CC_PLAY_PAD = 35


def find_port(name: str = PORT_NAME):
    """The id of the desk's MIDI output, or None."""
    return find(name)


# One handle for the life of the process, because a pad shortcut has to be instant and the open is
# the slow part. Guarded by a lock: two shortcuts pressed together must not interleave on one
# handle.
_lock = threading.Lock()
_handle = None


def _open_locked():
    global _handle
    if _handle is None:
        _handle = open_port(PORT_NAME)
    return _handle


def _close_locked() -> None:
    global _handle
    if _handle is not None:
        close_port(_handle)
        _handle = None


def close() -> None:
    """Drop the shared handle. Only needed at shutdown, or to force the next send to reopen."""
    with _lock:
        _close_locked()


def send_cc(channel: int, control: int, value: int) -> None:
    """One control change on the shared handle, opening it if this is the first send.

    RETRIES ONCE ON A FAILED SEND, after throwing the handle away. A handle stays valid across a
    great many things but not across a replug, and without the retry the first unplug of a session
    would leave every later pad shortcut dead until the app was restarted, which reads as "pads
    stopped working" rather than as "the cable moved"."""
    write_guard.refuse_in_tests("play a pad")
    message = (0xB0 | (channel & 0x0F), control & 0x7F, value & 0x7F)
    with _lock:
        try:
            send(_open_locked(), *message)
            return
        except OSError:
            _close_locked()
        try:
            send(_open_locked(), *message)
        except OSError as exc:
            _close_locked()
            raise RuntimeError(f"the desk would not take the MIDI message ({exc})") from None


def press_pad(pad: int) -> None:
    """Press pad `pad` (1-based, in the bank the desk is on) and HOLD it. The release is yours."""
    send_cc(pad - 1, CC_PLAY_PAD, 127)


def release_pad(pad: int) -> None:
    send_cc(pad - 1, CC_PLAY_PAD, 0)


def play_pad(pad: int, hold_s: float = 0.05) -> None:
    """Press and release pad `pad`: what the desk sees as a finger tap.

    WHAT A SECOND CALL DOES IS THE PAD'S OWN SETTING, not ours. A Toggle sound pad stops, a One Shot
    one starts again from the beginning, and there is no global stop (PROJECT_GUIDE part 2). Hold
    mode is the exception this signature cannot serve: it plays only while the note is held, so use
    press_pad / release_pad for that."""
    press_pad(pad)
    time.sleep(hold_s)
    release_pad(pad)


@contextlib.contextmanager
def midi_enabled():
    """Turn `systemMidiControl` on for the body and put it back to what it was. FOR TOOLS AND TESTS.

    The APP must not use this: it arms MIDI at the point of use and leaves it on, because a
    read-then-restore costs a model dump (about 1.5 s) in front of a keypress and can restore a
    stale value if the setting was changed on the desk meanwhile. Here the tidy-up is worth it,
    because a test should leave the desk as it found it.
    """
    import rcp_read
    import rcp_transfer
    was = rcp_read.keyed_value(rcp_read.read_model(fresh=True), rcp_transfer.SYSTEM,
                               "systemMidiControl")
    if not was:
        rcp_transfer.set_midi_control(True)
    try:
        yield bool(was)
    finally:
        if not was:
            rcp_transfer.set_midi_control(False)
