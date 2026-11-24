"""The Mac's audio devices, through CoreAudio's property API.

`devices()` lists every device the system knows as `Device(id, name, uid, inputs, outputs)`, where
`inputs` and `outputs` count its channels in each direction. The `uid` is the one that stays the
same across a replug, and is what AVFoundation and an AudioQueue take to choose a device; the `id`
is only good until the device list changes.

A device's level and mute are per direction (`INPUT` or `OUTPUT`). Many devices have a level on
each channel and none on the whole device, so `level` reads the whole device's where there is one
and the first channel's otherwise, and `set_level` and `set_mute` write every channel that has one.
"""
from __future__ import annotations

import ctypes
import struct
from dataclasses import dataclass

from mac import cf

_CoreAudio = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")


def _code(text: str) -> int:
    """A four-character code, as CoreAudio's headers spell its constants."""
    return int.from_bytes(text.encode("ascii"), "big")


_SYSTEM_OBJECT = 1
_DEVICES = _code("dev#")
_NAME = _code("lnam")
_UID = _code("uid ")
_STREAM_CONFIGURATION = _code("slay")
_SCOPE_GLOBAL = _code("glob")
_SCOPE_INPUT = _code("inpt")
_SCOPE_OUTPUT = _code("outp")
_ELEMENT_MAIN = 0
_ALIVE = _code("livn")
_NOMINAL_RATE = _code("nsrt")
_VOLUME_DB = _code("vold")
_VOLUME_RANGE_DB = _code("vdb#")
_MUTE = _code("mute")

INPUT, OUTPUT = _SCOPE_INPUT, _SCOPE_OUTPUT


class _Address(ctypes.Structure):
    _fields_ = [("mSelector", ctypes.c_uint32), ("mScope", ctypes.c_uint32),
                ("mElement", ctypes.c_uint32)]


_CoreAudio.AudioObjectGetPropertyDataSize.argtypes = [
    ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32, ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint32)]
_CoreAudio.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
_CoreAudio.AudioObjectGetPropertyData.argtypes = [
    ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32, ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
_CoreAudio.AudioObjectGetPropertyData.restype = ctypes.c_int32
_CoreAudio.AudioObjectSetPropertyData.argtypes = [
    ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32, ctypes.c_void_p,
    ctypes.c_uint32, ctypes.c_void_p]
_CoreAudio.AudioObjectSetPropertyData.restype = ctypes.c_int32
_CoreAudio.AudioObjectHasProperty.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Address)]
_CoreAudio.AudioObjectHasProperty.restype = ctypes.c_bool

@dataclass(frozen=True)
class Device:
    id: int
    name: str
    uid: str
    inputs: int
    outputs: int


def _raw(object_id: int, selector: int, scope: int = _SCOPE_GLOBAL) -> bytes:
    address = _Address(selector, scope, _ELEMENT_MAIN)
    size = ctypes.c_uint32(0)
    status = _CoreAudio.AudioObjectGetPropertyDataSize(object_id, ctypes.byref(address), 0, None,
                                                       ctypes.byref(size))
    if status:
        raise OSError(f"CoreAudio refused the size of {selector:#x} (status {status})")
    data = ctypes.create_string_buffer(size.value)
    status = _CoreAudio.AudioObjectGetPropertyData(object_id, ctypes.byref(address), 0, None,
                                                   ctypes.byref(size), data)
    if status:
        raise OSError(f"CoreAudio refused {selector:#x} (status {status})")
    return data.raw[:size.value]


def _string(object_id: int, selector: int) -> str:
    """A CFString property, which the caller owns and releases."""
    address = _Address(selector, _SCOPE_GLOBAL, _ELEMENT_MAIN)
    ref = ctypes.c_void_p()
    size = ctypes.c_uint32(ctypes.sizeof(ref))
    status = _CoreAudio.AudioObjectGetPropertyData(object_id, ctypes.byref(address), 0, None,
                                                   ctypes.byref(size), ctypes.byref(ref))
    if status or not ref.value:
        return ""
    try:
        return cf.text(ref)
    finally:
        cf.release(ref)


def _channels(device_id: int, scope: int) -> int:
    """The channels in an AudioBufferList: a count, then one 16-byte AudioBuffer per stream,
    starting 8 bytes in, each opening with its channel count."""
    try:
        data = _raw(device_id, _STREAM_CONFIGURATION, scope)
    except OSError:
        return 0
    if len(data) < 4:
        return 0
    (buffers,) = struct.unpack_from("=I", data, 0)
    return sum(struct.unpack_from("=I", data, 8 + 16 * n)[0] for n in range(buffers)
               if 8 + 16 * n + 4 <= len(data))


def devices() -> list[Device]:
    data = _raw(_SYSTEM_OBJECT, _DEVICES)
    ids = struct.unpack(f"={len(data) // 4}I", data)
    return [Device(id=i, name=_string(i, _NAME), uid=_string(i, _UID),
                   inputs=_channels(i, _SCOPE_INPUT), outputs=_channels(i, _SCOPE_OUTPUT))
            for i in ids]


def _has(object_id: int, selector: int, scope: int, element: int = _ELEMENT_MAIN) -> bool:
    return bool(_CoreAudio.AudioObjectHasProperty(object_id,
                                                  ctypes.byref(_Address(selector, scope, element))))


def _get(object_id: int, selector: int, scope: int, element: int, kind):
    value = kind()
    size = ctypes.c_uint32(ctypes.sizeof(value))
    status = _CoreAudio.AudioObjectGetPropertyData(
        object_id, ctypes.byref(_Address(selector, scope, element)), 0, None, ctypes.byref(size),
        ctypes.byref(value))
    if status:
        raise OSError(f"CoreAudio refused {selector:#x} (status {status})")
    return value


def _set(object_id: int, selector: int, scope: int, element: int, value) -> None:
    status = _CoreAudio.AudioObjectSetPropertyData(
        object_id, ctypes.byref(_Address(selector, scope, element)), 0, None,
        ctypes.sizeof(value), ctypes.byref(value))
    if status:
        raise OSError(f"CoreAudio refused to set {selector:#x} (status {status})")


def _elements(device_id: int, selector: int, scope: int) -> list[int]:
    """The main element when it has the property, else every channel that does."""
    if _has(device_id, selector, scope):
        return [_ELEMENT_MAIN]
    count = _channels(device_id, scope)
    return [n for n in range(1, count + 1) if _has(device_id, selector, scope, n)]


class _ValueRange(ctypes.Structure):
    _fields_ = [("mMinimum", ctypes.c_double), ("mMaximum", ctypes.c_double)]


def alive(device_id: int) -> bool:
    try:
        return bool(_get(device_id, _ALIVE, _SCOPE_GLOBAL, _ELEMENT_MAIN, ctypes.c_uint32).value)
    except OSError:
        return False


def nominal_rate(device_id: int) -> float:
    return float(_get(device_id, _NOMINAL_RATE, _SCOPE_GLOBAL, _ELEMENT_MAIN,
                      ctypes.c_double).value)


def level(device_id: int, direction: int):
    """(dB, (min dB, max dB) or None, muted) in one direction, or None when it has no level."""
    elements = _elements(device_id, _VOLUME_DB, direction)
    if not elements:
        return None
    element = elements[0]
    db = float(_get(device_id, _VOLUME_DB, direction, element, ctypes.c_float).value)
    try:
        span = _get(device_id, _VOLUME_RANGE_DB, direction, element, _ValueRange)
        span = (float(span.mMinimum), float(span.mMaximum))
    except OSError:
        span = None
    return db, span, muted(device_id, direction)


def muted(device_id: int, direction: int) -> bool:
    """False for a device with no mute."""
    elements = _elements(device_id, _MUTE, direction)
    return bool(elements) and bool(_get(device_id, _MUTE, direction, elements[0],
                                        ctypes.c_uint32).value)


def set_level(device_id: int, direction: int, db: float) -> None:
    elements = _elements(device_id, _VOLUME_DB, direction)
    if not elements:
        raise OSError("This device has no level macOS can set.")
    for element in elements:
        _set(device_id, _VOLUME_DB, direction, element, ctypes.c_float(db))


def set_mute(device_id: int, direction: int, muted: bool) -> None:
    elements = _elements(device_id, _MUTE, direction)
    if not elements:
        raise OSError("This device has no mute macOS can set.")
    for element in elements:
        _set(device_id, _MUTE, direction, element, ctypes.c_uint32(1 if muted else 0))
