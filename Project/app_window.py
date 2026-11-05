"""The app's own window, as code outside the window asks for it (the Open AccessCaster shortcut).

`tray.py` hosts the window and installs `show` here; a DevTools run has no window, and `show`
stays None. `raise_window` brings a window to the front with a real activation.
"""

import os

import diagnostics

# show() -> bool, callable from any thread: shows the window, creating it if need be, and returns
# whether Windows made it the foreground window.
show = None


def raise_window(hwnd: int) -> bool:
    """Restore `hwnd` if minimised and bring it to the foreground, from any thread. Returns whether
    Windows made it the foreground window. Logs the outcome as `window.raise`."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    # Declared: an undeclared restype is a C int, which TRUNCATES a 64-bit HWND and would make the
    # success check below compare two different halves of the same window.
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    SW_RESTORE = 9
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)

    # A PLAIN SetForegroundWindow, AND NOTHING ELSE. Windows grants this process the right to set
    # the foreground while the hotkey it registered is being handled, so the call is allowed and
    # produces a NORMAL activation - which is the whole point, because a normal activation is what
    # tells a screen reader the window came forward.
    #
    # DO NOT ADD A WORKAROUND HERE IF IT FAILS. Every one of them - a minimize/restore flip, an
    # Alt tap, AttachThreadInput - puts the window in front WITHOUT a real activation: measured,
    # the window became foreground and held it, GetGUIThreadInfo agreed the keyboard focus was
    # ours, and NVDA went on reading the app the user came from. Alt-Tabbing to the same window
    # announced it perfectly, which is what proved the window and the screen reader were both
    # fine. A refusal here means the CALLER has lost the foreground right, and the fix is at that
    # end - see hotkeys._FOREGROUND_COMMANDS, where dispatching to a worker thread was spending it.
    user32.SetForegroundWindow(hwnd)
    # ASK WINDOWS WHAT HAPPENED RATHER THAN ASSUMING IT WORKED: the caller stays silent on a
    # success, so a refusal reported as success would be silent too.
    got = user32.GetForegroundWindow()
    ok = bool(got) and got == hwnd
    # LOG THE FOCUS WINDOW, NOT JUST THE FOREGROUND ONE. They are different things and only the
    # second is what a screen reader follows, so "the window came up but my screen reader still
    # says I am in the other app" has two possible causes that look identical from outside: a
    # window that is merely in front without holding focus, or a screen reader reporting a stale
    # object because nothing generated a focus event. This one line separates them.
    # GetGUIThreadInfo(0) reports the foreground thread's own focus window.
    try:
        class _GTI(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                        ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                        ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                        ("rcCaret", wintypes.RECT)]
        gti = _GTI()
        gti.cbSize = ctypes.sizeof(_GTI)
        user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(_GTI)]
        user32.GetGUIThreadInfo(0, ctypes.byref(gti))

        def _title(h):
            if not h:
                return "none"
            n = user32.GetWindowTextLengthW(h)
            b = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(h, b, n + 1)
            return (b.value or "")[:40]

        diagnostics.event("window.raise", "app window", activated=ok,
                          wanted=f"{hwnd}", foreground=f"{got}",
                          fg_title=_title(got),
                          active=f"{gti.hwndActive}", active_title=_title(gti.hwndActive),
                          focus=f"{gti.hwndFocus}", focus_title=_title(gti.hwndFocus))
    except Exception as exc:
        diagnostics.event("window.raise", "app window", activated=ok, error=f"{exc}")
    return ok
