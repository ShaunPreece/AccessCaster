"""AccessCaster - whether RODE's own app is running on this computer.

The desk pushes no fader move made by software (RODECASTER_PROTOCOL.txt section 6), so while RODE's
app is open the desk model cannot keep the fader layout current from pushes: every write addressed
by the layout reads the desk first (`channels.strip_vector`). The user is warned once per run of
RODE's app: spoken in the app's own voice when it is first seen (`on_seen`), and a dialog the next
time the window comes to the front (`take_warning`), because a background program cannot take the
focus from the one in front. A tray notification is not used: on Windows 11 one has been raised
without error and never appeared.

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

NOTICE = ("RODE's app is open. Using it alongside AccessCaster is not advised. Until you quit it, "
          "AccessCaster checks the faders before every change, which makes changes slower.")
DIALOG_TITLE = "RODE's app is open"
DIALOG_TEXT = ("Using RODE's app and AccessCaster at the same time is not advised. The unit does "
               "not tell AccessCaster when a fader is moved in RODE's app, so a change made here "
               "could reach the wrong channel.\n\n"
               "While RODE's app is open, AccessCaster checks the faders before every change to "
               "prevent that, which makes each change about half a second slower. Quit RODE's app "
               "when you have finished with it.")

# Called on the watcher's thread when RODE's app is first seen in a run of it: the tray's spoken
# warning.
on_seen = None

_lock = threading.Lock()
_open = False
_warning_owed = False
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


def take_warning() -> bool:
    """Whether the dialog is owed: True once per run of RODE's app, then False until it is next
    opened."""
    global _warning_owed
    with _lock:
        owed, _warning_owed = _warning_owed, False
    return owed


def note(seen: bool) -> None:
    """One look at whether RODE's app is running. The watcher's poll; offline tests call it
    directly."""
    global _open, _warning_owed
    with _lock:
        was, _open = _open, seen
        if seen and not was:
            _warning_owed = True
        elif was and not seen:
            _warning_owed = False
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
