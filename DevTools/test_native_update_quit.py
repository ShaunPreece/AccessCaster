"""Offline check: the app ends when the installer closes it mid-update, and Quit never hangs on a
dialog.

An update hands over to Setup with the "Updating AccessCaster" dialog held open on Please wait.
Setup's Restart Manager then sends every top-level window of the process WM_QUERYENDSESSION and
WM_ENDSESSION with ENDSESSION_CLOSEAPP, from another process, and the process must end, or Setup
says it was unable to close all applications. Each case runs in a child process with the real tray
window, a frame and the real `JobDialog` in that state, and the real exit: the check is that the
child ends, with code 0, in time, with the service stopped. The cases with a dialog whose `EndModal`
fails are the stall a 3.0.0 update hit.

    python DevTools/test_native_update_quit.py
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes

import _bootstrap  # noqa: F401  (puts Project/ on sys.path; gives this suite its own desktop)
import _testkit
from _testkit import check

ENDSESSION_CLOSEAPP = 0x1
HUNG = 3                  # the child's exit code when its own watchdog had to end it
CHILD_LIMIT_S = 8.0


def child(case: str) -> None:
    """One case, in this process: build the app, open the update dialog, close it the case's way."""
    import service
    import speech
    import tray
    import win32con
    import win32gui
    import win32process
    import wx
    from native.dialogs import JobDialog

    speech.say = lambda *_a, **_k: None
    speech.start_loop = lambda *_a, **_k: None
    service.stop = lambda reason="": print(f"stopped: {reason}", flush=True)
    if case.endswith("-stuck"):
        def failing_end_modal(self, code):
            raise RuntimeError("C++ assertion \"IsRunning()\" failed: Use ScheduleExit() on "
                               "non-active loop")
        JobDialog.EndModal = failing_end_modal

    app = wx.App()
    loop = wx.GUIEventLoop()
    tray_app = tray.TrayApp.__new__(tray.TrayApp)     # the real quit path, without the service
    tray_app.app, tray_app.loop = app, loop
    tray_app.hwnd, tray_app.frame = None, None
    tray_app._ui_thread = threading.get_ident()
    tray_app._starting = False
    tray_app._quitting = False
    tray_app._ended = False
    tray_app._window_gone = False
    tray_app._add_icon = tray_app._remove_icon = lambda: None
    tray_app._create_window()
    tray_app.frame = wx.Frame(None, title="AccessCaster")
    app.Bind(wx.EVT_QUERY_END_SESSION, lambda event: None)
    app.Bind(wx.EVT_END_SESSION, lambda event: tray_app._end_now("Windows is closing the app"))

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetThreadDesktop.restype = wintypes.HANDLE
    ui_desktop = user32.GetThreadDesktop(threading.get_ident())

    def restart_manager():
        """What Setup's Restart Manager does, from outside the UI thread: ask every top-level
        window of this process, then tell them all the session is ending for this app."""
        user32.SetThreadDesktop(wintypes.HANDLE(ui_desktop))
        pid = win32process.GetCurrentProcessId()
        windows = []
        win32gui.EnumWindows(lambda h, _: windows.append(h)
                             if win32process.GetWindowThreadProcessId(h)[1] == pid else None, None)
        result = ctypes.c_size_t()
        for msg, wparam in ((win32con.WM_QUERYENDSESSION, 0), (win32con.WM_ENDSESSION, 1)):
            for hwnd in windows:
                user32.SendMessageTimeoutW(hwnd, msg, wparam, ENDSESSION_CLOSEAPP, 0, 2000,
                                           ctypes.byref(result))

    def begin(dialog):
        dialog.hold()                     # as `_Follower` leaves it once the installer starts
        if case.startswith("installer"):
            threading.Thread(target=restart_manager, daemon=True).start()
        else:
            wx.CallLater(100, tray_app._quit)

    def update():
        job = JobDialog(tray_app.frame, "Updating AccessCaster", "Downloading.", begin,
                        stop=lambda: None, stop_label="Cancel")
        with job:
            job.run()

    def watchdog():
        time.sleep(CHILD_LIMIT_S)
        os._exit(HUNG)

    threading.Thread(target=watchdog, daemon=True).start()
    wx.CallAfter(update)
    activator = wx.EventLoopActivator(loop)
    loop.Run()
    del activator
    print("loop returned", flush=True)


def run(case: str):
    """(exit code, seconds, output) of one case run in a child process."""
    started = time.monotonic()
    done = subprocess.run([sys.executable, os.path.abspath(__file__), "--case", case],
                          capture_output=True, text=True, timeout=CHILD_LIMIT_S + 10)
    return done.returncode, time.monotonic() - started, done.stdout


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--case":
        child(sys.argv[2])
        sys.exit(0)

    import tray

    print("THE INSTALLER CLOSES THE APP WITH THE UPDATE DIALOG HELD OPEN")
    code, took, out = run("installer")
    check("the process ends", code, 0)
    check("with the service stopped", "stopped: Windows is closing the app" in out, True)

    print("THE SAME, WITH A DIALOG WHOSE LOOP WILL NOT END")
    code, took, out = run("installer-stuck")
    check("the installer still ends it", code, 0)
    check("with the service stopped", "stopped: Windows is closing the app" in out, True)

    print("QUIT FROM THE TRAY WITH A DIALOG WHOSE LOOP WILL NOT END")
    code, took, out = run("quit-stuck")
    check("the process ends", code, 0)
    check("with the service stopped",
          "stopped: 'Updating AccessCaster' did not close" in out, True)
    check("once the dialog has had its chance", took > tray.DIALOGS_END_WITHIN_S, True)
    print()
    sys.exit(_testkit.finish())
