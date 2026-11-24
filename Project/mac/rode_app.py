"""macOS's process lookup, through libproc: whether any process runs an executable inside the app
bundle named `process`. Matching the bundle rather than the process name leaves the executable's
own name, and the system's 16-character cut of it, out of the question.

The bundle's name is unconfirmed: RODE's product name with its O-stroke, as their guide writes it.
A path is compared in composed form, since the file system keeps whichever form it was given.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import unicodedata

PROCESS = "RØDECaster App.app"

_PATH_MAX = 4096
_libproc = None


def _api():
    global _libproc
    if _libproc is None:
        lib = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib")
        lib.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.proc_listallpids.restype = ctypes.c_int
        lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        lib.proc_pidpath.restype = ctypes.c_int
        _libproc = lib
    return _libproc


def _fold(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def _paths():
    lib = _api()
    count = lib.proc_listallpids(None, 0)
    # Room for processes started between the two calls.
    pids = (ctypes.c_int * (count + 64))()
    count = lib.proc_listallpids(pids, ctypes.sizeof(pids))
    path = ctypes.create_string_buffer(_PATH_MAX)
    for pid in pids[:max(0, count)]:
        if pid > 0 and lib.proc_pidpath(pid, path, _PATH_MAX) > 0:
            yield path.value.decode("utf-8", "replace")


def running(process: str) -> bool:
    inside = _fold(f"/{process}/Contents/MacOS/")
    return any(inside in _fold(path) for path in _paths())
