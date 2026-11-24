"""Windows' `folders.open_focused`, in Explorer."""
from __future__ import annotations

import os
import time


def open_focused(folder: str) -> None:
    """`os.startfile` alone opens the window BEHIND the app's: with no window already at that path,
    AccessCaster still holds the foreground two seconds after the call. A window you were not moved
    to is, to a screen-reader user, indistinguishable from nothing having happened.

    Windows will not let a background process take the foreground on its own, so this borrows the
    input state of the window that HAS it (AttachThreadInput), which is the documented way. Unlike
    the bare raise `app_window` warns about, this is a real activation: NVDA follows into Explorer.
    `AllowSetForegroundWindow` is called first because when it does work it is cheaper and needs no
    window handle. Everything after `startfile` swallows its own errors.
    """
    import ctypes
    from ctypes import wintypes

    u32 = ctypes.windll.user32
    try:
        u32.AllowSetForegroundWindow(-1)          # ASFW_ANY, before the window exists
    except Exception:
        pass
    os.startfile(folder)                          # noqa: S606 - app-owned folder, never user input

    want = os.path.basename(os.path.normpath(folder))
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _visit(hwnd, _lparam):
        cls = ctypes.create_unicode_buffer(64)
        u32.GetClassNameW(hwnd, cls, 64)
        # Explorer's folder windows only. Anything else with this title is not ours to raise.
        if cls.value not in ("CabinetWClass", "ExploreWClass"):
            return True
        n = u32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        u32.GetWindowTextW(hwnd, buf, n + 1)
        # PREFIX, NOT EQUALITY. Explorer titles the window with the folder name and then appends
        # " - File Explorer" a moment later, so which of the two the poll sees depends on its
        # timing.
        title = buf.value
        if title == want or title.startswith(want + " - "):
            found.append(hwnd)
            return False
        return True

    # Explorer takes a moment to put the window up; poll rather than guess a sleep.
    for _ in range(20):
        found.clear()
        try:
            u32.EnumWindows(_visit, 0)
        except Exception:
            break
        if found:
            break
        time.sleep(0.1)
    if not found:
        return
    hwnd = found[0]
    try:
        fg = u32.GetForegroundWindow()
        ours = u32.GetWindowThreadProcessId(hwnd, None)
        theirs = u32.GetWindowThreadProcessId(fg, None)
        u32.ShowWindow(hwnd, 9)                   # SW_RESTORE, in case it opened minimised
        if ours != theirs:
            u32.AttachThreadInput(theirs, ours, True)
            u32.SetForegroundWindow(hwnd)
            u32.AttachThreadInput(theirs, ours, False)
        else:
            u32.SetForegroundWindow(hwnd)
    except Exception:
        pass
