"""CoreFoundation strings for the Mac halves that call C frameworks through ctypes.

  - `text(ref)`: a CFString's text, "" for a null one.
  - `cfstring(text)`: a new CFString, which the caller releases with `release`.
  - `release(ref)`: CFRelease, a null one ignored.
"""
from __future__ import annotations

import ctypes

_CF = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_UTF8 = 0x08000100

_CF.CFStringGetLength.argtypes = [ctypes.c_void_p]
_CF.CFStringGetLength.restype = ctypes.c_long
_CF.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long,
                                   ctypes.c_uint32]
_CF.CFStringGetCString.restype = ctypes.c_bool
_CF.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
_CF.CFStringCreateWithCString.restype = ctypes.c_void_p
_CF.CFRelease.argtypes = [ctypes.c_void_p]


def text(ref) -> str:
    if not ref:
        return ""
    # A UTF-16 unit never needs more than 3 bytes of UTF-8; one more for the terminator.
    room = _CF.CFStringGetLength(ref) * 3 + 1
    out = ctypes.create_string_buffer(room)
    return out.value.decode("utf-8") if _CF.CFStringGetCString(ref, out, room, _UTF8) else ""


def cfstring(value: str):
    return _CF.CFStringCreateWithCString(None, value.encode("utf-8"), _UTF8)


def release(ref) -> None:
    if ref:
        _CF.CFRelease(ref)
