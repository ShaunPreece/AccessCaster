"""The Mac's audio devices, through CoreAudio's property API.

`devices()` lists every device the system knows as `Device(id, name, uid, inputs, outputs)`, where
`inputs` and `outputs` count its channels in each direction. The `uid` is the one that stays the
same across a replug, and is what AVFoundation takes to choose a device; the `id` is only good
until the device list changes.
"""
from __future__ import annotations

import ctypes
import struct
from dataclasses import dataclass

_CoreAudio = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
_CF = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")


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
_UTF8 = 0x08000100


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
_CF.CFStringGetLength.argtypes = [ctypes.c_void_p]
_CF.CFStringGetLength.restype = ctypes.c_long
_CF.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long,
                                   ctypes.c_uint32]
_CF.CFStringGetCString.restype = ctypes.c_bool
_CF.CFRelease.argtypes = [ctypes.c_void_p]


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
        room = _CF.CFStringGetLength(ref) * 4 + 1
        text = ctypes.create_string_buffer(room)
        return text.value.decode("utf-8") if _CF.CFStringGetCString(ref, text, room, _UTF8) else ""
    finally:
        _CF.CFRelease(ref)


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
