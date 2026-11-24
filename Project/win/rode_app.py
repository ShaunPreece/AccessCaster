"""Windows' `rode_app` process lookup, from one Toolhelp snapshot (a few milliseconds)."""
from __future__ import annotations

import ctypes
from ctypes import wintypes

PROCESS = "RODECaster App.exe"


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


def running(process: str) -> bool:
    kernel32 = _api()
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)       # TH32CS_SNAPPROCESS
    if snapshot in (None, wintypes.HANDLE(-1).value):
        return False
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(_ProcessEntry)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        want = process.lower()
        while more:
            if entry.szExeFile.lower() == want:
                return True
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return False
    finally:
        kernel32.CloseHandle(snapshot)
