"""A pad-volume flush that fails leaves a trace in the log. NO DEVICE.

The fast flush (`FlushFileBuffers` on the volume) falls back to PowerShell's `Write-VolumeCache`;
when both fail, a pad's audio may reach the desk truncated, so each failure must be logged.

Run: python DevTools/test_win_desk_drives.py
"""
from __future__ import annotations

import sys

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
from _testkit import check, patched
from win import desk_drives

class _Call:
    """A kernel32 function that returns `result`, with the attributes ctypes setup assigns."""
    def __init__(self, result):
        self.result = result

    def __call__(self, *_args):
        return self.result


class _Kernel32:
    def __init__(self, handle, flushed):
        self.CreateFileW = _Call(handle)
        self.FlushFileBuffers = _Call(flushed)
        self.CloseHandle = _Call(1)


class _PowerShellFailed:
    returncode = 1
    stderr = b"Write-VolumeCache : No MSFT_Volume objects found"


for label, handle, flushed, first in (
        ("no volume handle", 0, 0, "no volume handle"),
        ("a flush Windows refuses", 0x1234, 0, "FlushFileBuffers failed")):
    events = []
    k32 = _Kernel32(handle, flushed)
    with patched((desk_drives.diagnostics,
                  {"event": lambda kind, detail="", log=events.append, **f: log((kind, detail, f))}),
                 (desk_drives.ctypes, {"WinDLL": lambda *_a, k32=k32, **_k: k32}),
                 (desk_drives.subprocess, {"run": lambda *_a, **_k: _PowerShellFailed()})):
        desk_drives.flush("Q:\\")
    check(f"{label}: the fast path's failure is logged",
          [(k, d) for k, d, _f in events[:1]], [("transfer.flush_fallback", first)])
    check(f"{label}: and so is the PowerShell exit code",
          [(k, f.get("exit_code")) for k, _d, f in events[1:]], [("transfer.flush_failed", 1)])

sys.exit(_testkit.finish())
