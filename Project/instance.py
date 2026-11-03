"""One copy of AccessCaster per PC, across every signed-in account.

There is one desk on the USB, and the announcer, the global shortcuts and the tray icon belong to
the session that started them, so a second account's copy is refused rather than shared. The lock
is a named mutex in the `Global\\` namespace, which every session sees. Windows releases it when
the process ends, however it ends, so a crash leaves no stale lock behind.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

MUTEX_NAME = "Global\\AccessCaster"
_ERROR_ALREADY_EXISTS = 183

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

_handle = None


def claim() -> bool:
    """Hold the lock for the rest of this process's life. False when another process holds it.

    A mutex made by another account opens with ERROR_ACCESS_DENIED and no handle, so a failure to
    open is read as held: the one case it can be is somebody else's copy."""
    global _handle
    if _handle:
        return True
    handle = _kernel32.CreateMutexW(None, False, MUTEX_NAME)
    error = ctypes.get_last_error()
    if not handle:
        return False
    if error == _ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(handle)
        return False
    _handle = handle
    return True
