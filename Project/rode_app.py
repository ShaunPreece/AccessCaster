"""AccessCaster - whether RODE's own app is running on this computer.

The desk pushes no write made by other software (RODECASTER_PROTOCOL.txt, "Who pushes a layout
change"), so while RODE's app is open the desk model cannot keep current from pushes: every write
addressed by the layout reads the desk first (`channels.strip_vector`), and every read is compared
with the desk model (`on_look`) so what RODE's app changed reaches everything that follows the
desk. When RODE's app is first seen in a run of it, that is said in the app's own voice
(`on_seen`, `NOTICE`), heard whatever is in front. A tray notification is not used: on Windows 11
one has been raised without error and never appeared.

RODE's app is one process, and its Quit button ends it. The virtual device driver's own processes
(RODECasterVADService, RodecasterSystray) run all the time and control no fader, so only the app
counts.
"""
from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes

import diagnostics

EXE = "RODECaster App.exe"
POLL_S = 3.0

NOTICE = "RODE's app is open. AccessCaster catches up with its changes when you come back to it."

# Called on the watcher's thread when RODE's app is first seen in a run of it: the tray speaking
# `NOTICE`.
on_seen = None
# Called on the watcher's thread after every look, with whether RODE's app is open: the server
# comparing reads with the desk model while it is (`server._follow_rode_app`).
on_look = None

_lock = threading.Lock()
_open = False
_stop = threading.Event()
_thread = None


class _ProcessEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


_kernel32 = None


def _api():
    """kernel32's process-snapshot calls, declared on a private handle to the DLL so the 64-bit
    handle is passed whole and no other module's declarations are touched."""
    global _kernel32
    if _kernel32 is None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        for name in ("Process32FirstW", "Process32NextW"):
            getattr(k, name).argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
            getattr(k, name).restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        _kernel32 = k
    return _kernel32


def running() -> bool:
    """Whether a process named `EXE` exists, from one process snapshot (a few milliseconds)."""
    if os.name != "nt":
        return False
    kernel32 = _api()
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)       # TH32CS_SNAPPROCESS
    if snapshot in (None, wintypes.HANDLE(-1).value):
        return False
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(_ProcessEntry)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        want = EXE.lower()
        while more:
            if entry.szExeFile.lower() == want:
                return True
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return False
    finally:
        kernel32.CloseHandle(snapshot)


def is_open() -> bool:
    """RODE's app as last seen by the watcher."""
    return _open


def note(seen: bool) -> None:
    """One look at whether RODE's app is running. The watcher's poll; offline tests call it
    directly."""
    global _open
    with _lock:
        was, _open = _open, seen
    if seen == was:
        return
    diagnostics.event("rode_app.open" if seen else "rode_app.closed", EXE)
    if seen and on_seen is not None:
        try:
            on_seen()
        except Exception as exc:
            diagnostics.event("rode_app.warning_failed", str(exc))


def start() -> None:
    """Watch for RODE's app every `POLL_S` seconds until `stop`."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()

    def watch():
        while True:
            try:
                note(running())
                if on_look is not None:
                    on_look(_open)
            except Exception as exc:
                diagnostics.event("rode_app.check_failed", str(exc))
            if _stop.wait(POLL_S):
                return

    _thread = threading.Thread(target=watch, name="rode-app", daemon=True)
    _thread.start()


def stop() -> None:
    global _thread
    _stop.set()
    if _thread is not None and _thread is not threading.current_thread():
        _thread.join(timeout=1.0)
    _thread = None
