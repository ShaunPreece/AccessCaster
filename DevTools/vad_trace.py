"""Watch RODE's Virtual Device service trace itself while it tries to bind to the desk.

`RODECasterVADService.exe` traces through `OutputDebugStringW`: `%s Enter`, `%s Leave, returning
%#08x`, `Found Device: %s`, `No devices found`, `DeviceInterfaceAlias disabled.` and the whole
KS pin path (`CKsFilter::Instantiate`, `Failed to instantiate Capture Pin`). Nothing else on this
computer says why one arrival of the desk binds and the next does not.

The service runs as LocalSystem in session 0, so its output goes to the GLOBAL debug channel.
Reading that means creating `Global\\DBWIN_BUFFER` and its two events, which needs
SeCreateGlobalPrivilege: RUN THIS ELEVATED. Only one process on the computer can hold the
channel, so close DebugView first.

    python DevTools/vad_trace.py                    every process, until Ctrl+C
    python DevTools/vad_trace.py --seconds 120 --out DevTools/captures/vad_trace.txt

Read-only, and it takes nothing away from the service: a process with no debugger attached
discards its own output, so capturing it changes nothing about the bind.
"""
from __future__ import annotations

import argparse
import ctypes
import sys
import time
from ctypes import wintypes

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_adv = ctypes.WinDLL("advapi32", use_last_error=True)

INVALID_HANDLE_VALUE = wintypes.HANDLE(-1)
PAGE_READWRITE = 0x04
FILE_MAP_READ = 0x0004
WAIT_OBJECT_0 = 0x0
WAIT_TIMEOUT = 0x102
BUFFER_BYTES = 4096


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD),
                ("lpSecurityDescriptor", wintypes.LPVOID),
                ("bInheritHandle", wintypes.BOOL)]


_k32.CreateFileMappingW.restype = wintypes.HANDLE
_k32.CreateFileMappingW.argtypes = (wintypes.HANDLE, ctypes.POINTER(SECURITY_ATTRIBUTES),
                                    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.LPCWSTR)
_k32.MapViewOfFile.restype = wintypes.LPVOID
_k32.MapViewOfFile.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                               ctypes.c_size_t)
_k32.CreateEventW.restype = wintypes.HANDLE
_k32.CreateEventW.argtypes = (ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.BOOL, wintypes.BOOL,
                              wintypes.LPCWSTR)
_k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
_k32.SetEvent.argtypes = (wintypes.HANDLE,)
_adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
    wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID),
    ctypes.POINTER(wintypes.DWORD))


def _everyone() -> SECURITY_ATTRIBUTES:
    """Grant everyone access to the three objects. The service is LocalSystem and we are not, and
    a default descriptor would leave it unable to open the buffer, which silences the channel
    rather than erroring."""
    sd = wintypes.LPVOID()
    ok = _adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        "D:(A;;GA;;;WD)", 1, ctypes.byref(sd), None)
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    sa = SECURITY_ATTRIBUTES()
    sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
    sa.lpSecurityDescriptor = sd
    sa.bInheritHandle = False
    return sa


def listen(seconds: float, out, match: str = "") -> int:
    """Hold the global debug channel and print what arrives. Returns the number of lines seen.

    The protocol is one shared page: the writer takes DBWIN_BUFFER_READY, writes its process id
    and an ANSI string, then sets DBWIN_DATA_READY. So the reader must set BUFFER_READY again
    after every line or the next OutputDebugString call blocks its caller for ten seconds."""
    sa = _everyone()
    ready = _k32.CreateEventW(ctypes.byref(sa), False, True, r"Global\DBWIN_BUFFER_READY")
    if not ready:
        raise ctypes.WinError(ctypes.get_last_error())
    data = _k32.CreateEventW(ctypes.byref(sa), False, False, r"Global\DBWIN_DATA_READY")
    if not data:
        raise ctypes.WinError(ctypes.get_last_error())
    mapping = _k32.CreateFileMappingW(INVALID_HANDLE_VALUE, ctypes.byref(sa), PAGE_READWRITE,
                                      0, BUFFER_BYTES, r"Global\DBWIN_BUFFER")
    if not mapping:
        raise ctypes.WinError(ctypes.get_last_error())
    view = _k32.MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, 0)
    if not view:
        raise ctypes.WinError(ctypes.get_last_error())

    at = time.monotonic()
    seen = 0
    _k32.SetEvent(ready)
    while time.monotonic() - at < seconds:
        if _k32.WaitForSingleObject(data, 500) != WAIT_OBJECT_0:
            continue
        pid = ctypes.c_ulong.from_address(view).value
        text = ctypes.string_at(view + 4, BUFFER_BYTES - 4).split(b"\0", 1)[0]
        _k32.SetEvent(ready)
        line = text.decode("cp1252", "replace").rstrip("\r\n")
        if match and match not in line:
            continue
        seen += 1
        stamp = f"{time.monotonic() - at:8.3f}s  pid={pid:<6}"
        print(f"{stamp} {line}", file=out, flush=True)
    return seen


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=180.0)
    ap.add_argument("--out", default="", help="write to this file instead of the console")
    ap.add_argument("--match", default="", help="only lines holding this text")
    args = ap.parse_args()

    out = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout
    try:
        print(f"--- holding the global debug channel for {args.seconds:.0f} s ---",
              file=out, flush=True)
        seen = listen(args.seconds, out, args.match)
        print(f"--- {seen} lines ---", file=out, flush=True)
    finally:
        if args.out:
            out.close()


if __name__ == "__main__":
    main()
