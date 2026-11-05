"""
AccessCaster - the folders the app's exports go to, and opening one in Explorer with the user in it.
"""

from __future__ import annotations

import os
import time


def documents_folder(name: str) -> str:
    """Documents\\AccessCaster\\<name>, created if need be: where the app's exports go.

    Documents rather than the app's own data folder (%LOCALAPPDATA%): an export you cannot find is
    not an export, and a reinstall wipes the data folder. There is no folder picker: a fixed
    destination is easier to find again than one chosen each time - the reply says the full path,
    and Open folder takes you there."""
    base = os.path.join(os.path.expanduser("~"), "Documents", "AccessCaster", name)
    os.makedirs(base, exist_ok=True)
    return base


def open_folder_reply(folder: str, what: str) -> dict:
    """Open `folder` in Explorer and say so. `what` names it in a failure: "The folder"."""
    try:
        _open_folder_focused(folder)
    except Exception as exc:
        return {"ok": False, "message": f"{what} is {folder}, but it could not be opened: {exc}"}
    return {"ok": True, "message": f"Opened {folder}."}


def _open_folder_focused(folder: str) -> None:
    """Open a folder in Explorer AND PUT THE USER IN IT. Raises if it cannot be opened at all.

    `os.startfile` alone opens the window BEHIND the app's: measured, with no window already at
    that path, the foreground two seconds after the call is still AccessCaster. A window you were not
    moved to is, to a screen-reader user, indistinguishable from nothing having happened.

    Windows will not let a background process take the foreground on its own, so this borrows the
    input state of the window that HAS it (AttachThreadInput), which is the documented way. `AllowSetForegroundWindow` is called first because
    when it does work it is cheaper and needs no window handle.

    FAILING TO FOCUS IS NOT FAILING TO OPEN. The folder is what was asked for; the focus is the
    courtesy. So everything after `startfile` is best-effort and swallows its own errors - reporting
    "could not be opened" for a window that opened perfectly well would be worse than the silence
    this exists to fix.
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
        # " - File Explorer" a moment later, so which of the two you see depends on how fast the poll
        # got there.
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
