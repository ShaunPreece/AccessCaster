"""Fire the desk's own controls over its class-compliant USB MIDI port.

WHY THIS EXISTS. A SMART pad cannot be played over the HID control interface: writing `padActive`
engages voice-FX and mixer pads but a SOUND pad sets the flag and plays nothing, and
`padButtonPressed` is stored and ignored (both settled negatives, RODECASTER_PROTOCOL 8.1). The desk
DOES play a pad when the same request arrives on its MIDI endpoint, named "MIDI function". That is
what lets a keyboard shortcut play any pad on the desk.

No package and no driver: Windows' own winmm (`midiOutOpen` / `midiOutShortMsg`) through ctypes.

THE ADDRESSING RULE, and it is the opposite way round from what you would guess: the CC NUMBER is
the FUNCTION and the MIDI CHANNEL is WHICH pad. Play-a-pad is CC 35, so pad 1 is status 0xB0 and
pad 8 is 0xB7. A play is value 127 then value 0, i.e. press then release.

`systemMidiControl` must be ON or the desk ignores the port entirely. That is an ordinary HID write
(`rcp_transfer.set_midi_control`) and the server arms it at the point of use; `midi_enabled()` here
is the TOOL shape, which puts the setting back afterwards.

THE PORT IS OPENED ONCE AND KEPT. `midiOutOpen` is not free and a pad shortcut has to be instant, so
this module owns one process-wide handle behind a lock, opens it lazily on the first send and
REOPENS it once if a send fails - which is what a replug looks like from here. Nothing about it is
exclusive (two `midiOutOpen` calls on this port both succeed, measured), so
holding it open cannot lock another program out.

BANK SELECT IS NOT A MIDI PROBLEM. MIDI reaches only the bank the desk is on, and the bank is an
ordinary HID write - `soundpad.select_bank`. Do not go looking for the CC 0 / CC 1 bank select in
RODE's spec; it is marked "verify" and it is not needed.
"""
from __future__ import annotations
import contextlib
import ctypes
import os
import threading
import time
from ctypes import wintypes

# AN OFFLINE TEST MUST NOT BE ABLE TO PLAY A PAD. Read from the environment rather than imported
# from rcp_hid, which keeps this module dependency-light: it speaks winmm through ctypes and must
# not drag hidapi in behind it. `DevTools/_bootstrap` sets the variable for any `test_*` script.
_NO_WRITE = bool(os.environ.get("ACCESSCASTER_NO_DEVICE_WRITE")) and not os.environ.get(
    "ACCESSCASTER_ALLOW_DEVICE_WRITE")

PORT_NAME = "MIDI function"
CC_PLAY_PAD = 35

_winmm = ctypes.WinDLL("winmm")


class _MIDIOUTCAPS(ctypes.Structure):
    _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD),
                ("vDriverVersion", wintypes.UINT), ("szPname", wintypes.WCHAR * 32),
                ("wTechnology", wintypes.WORD), ("wVoices", wintypes.WORD),
                ("wNotes", wintypes.WORD), ("wChannelMask", wintypes.WORD),
                ("dwSupport", wintypes.DWORD)]


def ports() -> list:
    """Every MIDI OUT port name, in device-id order."""
    out = []
    for i in range(_winmm.midiOutGetNumDevs()):
        caps = _MIDIOUTCAPS()
        _winmm.midiOutGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps))
        out.append(caps.szPname)
    return out


def find_port(name: str = PORT_NAME):
    """The device id of the desk's MIDI OUT port, or None. Matched on a prefix, because Windows
    truncates a MIDI port name to 31 characters and appends an index when several are present."""
    for i, got in enumerate(ports()):
        if got.startswith(name[:31]):
            return i
    return None


def _refuse_in_tests() -> None:
    """GUARDED LIKE EVERY OTHER DEVICE WRITE. A send PLAYS A PAD, so an offline suite that reaches
    one makes a noise in somebody's room and possibly in their recording."""
    if _NO_WRITE:
        raise RuntimeError("REFUSING TO PLAY A PAD: this is an offline test "
                           "(ACCESSCASTER_NO_DEVICE_WRITE). Stub it, or set "
                           "ACCESSCASTER_ALLOW_DEVICE_WRITE if it is a live suite.")


def _cc_message(channel: int, control: int, value: int) -> int:
    """midiOutShortMsg packs a message little-endian into one DWORD: status, data1, data2."""
    return (0xB0 | (channel & 0x0F)) | ((control & 0x7F) << 8) | ((value & 0x7F) << 16)


class MidiOut:
    """One open MIDI OUT handle, as a context manager. For tools and tests that want the handle to
    end with the block. Long-running code should use the module functions instead, which share one
    handle for the life of the process."""

    def __init__(self, port=None):
        self.port = find_port() if port is None else port
        if self.port is None:
            raise RuntimeError(f"no {PORT_NAME!r} MIDI port (is the desk on USB? ports: {ports()})")
        self._h = wintypes.HANDLE()

    def __enter__(self):
        rc = _winmm.midiOutOpen(ctypes.byref(self._h), self.port, 0, 0, 0)
        if rc:
            raise RuntimeError(f"midiOutOpen failed on port {self.port} (rc {rc})")
        return self

    def __exit__(self, *exc) -> None:
        _winmm.midiOutReset(self._h)
        _winmm.midiOutClose(self._h)
        self._h = None      # so a send after the block says so, instead of "rc 5" from winmm

    def cc(self, channel: int, control: int, value: int) -> None:
        """One control-change message. `channel` is 0-based (MIDI channel 1 = 0)."""
        _refuse_in_tests()
        if self._h is None:
            raise RuntimeError("this MidiOut is closed (send inside the `with` block)")
        rc = _winmm.midiOutShortMsg(self._h, wintypes.DWORD(_cc_message(channel, control, value)))
        if rc:
            raise RuntimeError(f"midiOutShortMsg failed (rc {rc})")

    def press_pad(self, pad: int) -> None:
        """Press pad `pad` (1-based, within the bank the desk is on) and hold it."""
        self.cc(pad - 1, CC_PLAY_PAD, 127)

    def release_pad(self, pad: int) -> None:
        self.cc(pad - 1, CC_PLAY_PAD, 0)

    def play_pad(self, pad: int, hold_s: float = 0.05) -> None:
        """Press and release, which is what the desk sees as a finger tap."""
        self.press_pad(pad)
        time.sleep(hold_s)
        self.release_pad(pad)


# --- the shared handle ---------------------------------------------------------------------------
# One handle for the life of the process, because a pad shortcut has to be instant and the open is
# the slow part. Guarded by a lock: two shortcuts pressed together must not interleave on one handle.
_lock = threading.Lock()
_handle = None


def _open_locked():
    global _handle
    if _handle is not None:
        return _handle
    port = find_port()
    if port is None:
        raise RuntimeError(f"the desk's {PORT_NAME!r} MIDI port is not there. Is it plugged in?")
    h = wintypes.HANDLE()
    rc = _winmm.midiOutOpen(ctypes.byref(h), port, 0, 0, 0)
    if rc:
        raise RuntimeError(f"could not open the desk's MIDI port (rc {rc})")
    _handle = h
    return _handle


def _close_locked() -> None:
    global _handle
    if _handle is None:
        return
    _winmm.midiOutReset(_handle)      # winmm reports failure by return code, never by raising
    _winmm.midiOutClose(_handle)
    _handle = None


def close() -> None:
    """Drop the shared handle. Only needed at shutdown, or to force the next send to reopen."""
    with _lock:
        _close_locked()


def send_cc(channel: int, control: int, value: int) -> None:
    """One control change on the shared handle, opening it if this is the first send.

    RETRIES ONCE ON A FAILED SEND, after throwing the handle away. A handle stays valid across a
    great many things but not across a replug, and without the retry the first unplug of a session
    would leave every later pad shortcut dead until the app was restarted - which reads as "pads
    stopped working" rather than as "the cable moved"."""
    _refuse_in_tests()
    with _lock:
        _open_locked()
        rc = _winmm.midiOutShortMsg(_handle, wintypes.DWORD(_cc_message(channel, control, value)))
        if not rc:
            return
        _close_locked()
        _open_locked()
        rc = _winmm.midiOutShortMsg(_handle, wintypes.DWORD(_cc_message(channel, control, value)))
        if rc:
            _close_locked()
            raise RuntimeError(f"the desk would not take the MIDI message (rc {rc})")


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
    was = (rcp_read.snapshot_properties(rcp_read.read_model(fresh=True))
           .get("systemMidiControl") or [None])[0]
    if not was:
        rcp_transfer.set_midi_control(True)
    try:
        yield bool(was)
    finally:
        if not was:
            rcp_transfer.set_midi_control(False)
