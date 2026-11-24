"""macOS's `midi_out`, on CoreMIDI.

A port is a CoreMIDI destination, found by its endpoint name ("MIDI function") or, failing that,
by a display name that contains it, which CoreMIDI builds from the device's name and the
endpoint's. A handle is an output port of this app's one MIDI client, with the destination it
sends to. The client lives as long as the process.

A MIDIPacketList is packed to 4 bytes, so a packet's 64-bit timestamp starts 4 bytes in, straight
after the list's count; a timestamp of 0 means now.
"""
from __future__ import annotations

import ctypes
import struct
import threading

from mac import cf

_MIDI = ctypes.CDLL("/System/Library/Frameworks/CoreMIDI.framework/CoreMIDI")
_MIDI.MIDIGetNumberOfDestinations.restype = ctypes.c_ulong
_MIDI.MIDIGetDestination.argtypes = [ctypes.c_ulong]
_MIDI.MIDIGetDestination.restype = ctypes.c_uint32
_MIDI.MIDIObjectGetStringProperty.argtypes = [ctypes.c_uint32, ctypes.c_void_p,
                                              ctypes.POINTER(ctypes.c_void_p)]
_MIDI.MIDIObjectGetStringProperty.restype = ctypes.c_int32
_MIDI.MIDIClientCreate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.POINTER(ctypes.c_uint32)]
_MIDI.MIDIClientCreate.restype = ctypes.c_int32
_MIDI.MIDIOutputPortCreate.argtypes = [ctypes.c_uint32, ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_uint32)]
_MIDI.MIDIOutputPortCreate.restype = ctypes.c_int32
_MIDI.MIDISend.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_char_p]
_MIDI.MIDISend.restype = ctypes.c_int32
_MIDI.MIDIPortDispose.argtypes = [ctypes.c_uint32]
_MIDI.MIDIPortDispose.restype = ctypes.c_int32

_NAME = ctypes.c_void_p.in_dll(_MIDI, "kMIDIPropertyName")
_DISPLAY_NAME = ctypes.c_void_p.in_dll(_MIDI, "kMIDIPropertyDisplayName")

_client = ctypes.c_uint32(0)
_client_lock = threading.Lock()


def _property(endpoint: int, key) -> str:
    ref = ctypes.c_void_p()
    if _MIDI.MIDIObjectGetStringProperty(endpoint, key, ctypes.byref(ref)):
        return ""
    try:
        return cf.text(ref)
    finally:
        cf.release(ref)


def _destinations():
    for index in range(_MIDI.MIDIGetNumberOfDestinations()):
        endpoint = _MIDI.MIDIGetDestination(index)
        if endpoint:
            yield endpoint, _property(endpoint, _NAME), _property(endpoint, _DISPLAY_NAME)


def ports() -> list:
    return [display or name for _endpoint, name, display in _destinations()]


def find(name: str):
    found = list(_destinations())
    exact = next((e for e, own, _display in found if own == name), None)
    return exact or next((e for e, _own, display in found if name in display), None)


def _client_ref() -> int:
    with _client_lock:
        if not _client.value:
            label = cf.cfstring("AccessCaster")
            try:
                status = _MIDI.MIDIClientCreate(label, None, None, ctypes.byref(_client))
            finally:
                cf.release(label)
            if status:
                raise RuntimeError(f"macOS would not let AccessCaster use MIDI (error {status})")
        return _client.value


def open_port(name: str):
    destination = find(name)
    if destination is None:
        raise RuntimeError(f"macOS is not offering the desk's {name!r} MIDI port")
    port = ctypes.c_uint32(0)
    label = cf.cfstring("AccessCaster pads")
    try:
        status = _MIDI.MIDIOutputPortCreate(_client_ref(), label, ctypes.byref(port))
    finally:
        cf.release(label)
    if status:
        raise RuntimeError(f"could not open the desk's MIDI port (error {status})")
    return port.value, destination


def send(handle, status: int, data1: int, data2: int) -> None:
    port, destination = handle
    packets = struct.pack("<IQH3B", 1, 0, 3, status, data1, data2)
    result = _MIDI.MIDISend(port, destination, packets)
    if result:
        raise OSError(f"MIDISend returned {result}")


def close_port(handle) -> None:
    _MIDI.MIDIPortDispose(handle[0])
