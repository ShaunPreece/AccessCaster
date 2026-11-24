"""Windows' `midi_out` ports, on winmm (`midiOutOpen` / `midiOutShortMsg`) through ctypes."""
from __future__ import annotations

import ctypes
from ctypes import wintypes

_winmm = ctypes.WinDLL("winmm")


class _MIDIOUTCAPS(ctypes.Structure):
    _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD),
                ("vDriverVersion", wintypes.UINT), ("szPname", wintypes.WCHAR * 32),
                ("wTechnology", wintypes.WORD), ("wVoices", wintypes.WORD),
                ("wNotes", wintypes.WORD), ("wChannelMask", wintypes.WORD),
                ("dwSupport", wintypes.DWORD)]


def ports() -> list:
    """In device-id order."""
    out = []
    for i in range(_winmm.midiOutGetNumDevs()):
        caps = _MIDIOUTCAPS()
        _winmm.midiOutGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps))
        out.append(caps.szPname)
    return out


def find(name: str):
    """The device id. Matched on a prefix, because Windows truncates a MIDI port name to 31
    characters and appends an index when several are present."""
    for i, got in enumerate(ports()):
        if got.startswith(name[:31]):
            return i
    return None


def open_port(name: str):
    # Windows MIDI Services (midisrv) can stay up with its USB transports unloaded, so the desk is
    # on the bus with no MIDI port; a replug does not bring it back, a restart of the service does.
    # A process that opened winmm before that restart then fails every open until it restarts too.
    port = find(name)
    if port is None:
        raise RuntimeError(f"Windows is not offering the desk's {name!r} MIDI port. "
                           f"Restarting the Windows MIDI Service, or the computer, brings it back")
    h = wintypes.HANDLE()
    rc = _winmm.midiOutOpen(ctypes.byref(h), port, 0, 0, 0)
    if rc:
        raise RuntimeError(f"could not open the desk's MIDI port (error {rc}). If the Windows "
                           f"MIDI Service was restarted, restart AccessCaster")
    return h


def send(handle, status: int, data1: int, data2: int) -> None:
    # midiOutShortMsg packs a message little-endian into one DWORD: status, data1, data2.
    rc = _winmm.midiOutShortMsg(handle, wintypes.DWORD(status | (data1 << 8) | (data2 << 16)))
    if rc:
        raise OSError(f"midiOutShortMsg returned {rc}")


def close_port(handle) -> None:
    # winmm reports failure by return code, never by raising.
    _winmm.midiOutReset(handle)
    _winmm.midiOutClose(handle)
