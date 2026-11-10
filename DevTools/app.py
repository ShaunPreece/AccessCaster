"""Quit, start or restart AccessCaster run from source, without touching its window.

    python DevTools/app.py status
    python DevTools/app.py quit
    python DevTools/app.py start [--window]
    python DevTools/app.py restart [--window]

QUIT is WM_CLOSE to the tray's hidden window, which is what the tray's own Quit does: refused, with a
dialog on screen, while desk work is running. The window is found from Python: a PowerShell
FindWindow from this tooling does not see it.

START COMES UP IN THE TRAY, as at sign-in (`--startup`), so it takes nobody's focus; `--window`
opens the window as a person starting it would. It runs on the build's venv, `ProjectBuilds/.venv`,
the one Python that has the app's dependencies.

START NEVER RUNS A SECOND COPY. A second copy hands over to the first and brings its window up on
the screen of whoever is working, so start refuses while one is running.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import _bootstrap
import win32api
import win32con
import win32event
import win32gui
import win32process

TRAY_CLASS = "AccessCasterTray"     # tray.TRAY_CLASS; tray itself is not imported, it loads wx
STARTUP_FLAG = "--startup"          # autostart.STARTUP_FLAG
QUIT_WAIT_S = 20.0
START_WAIT_S = 20.0
# The app's dependencies, wx among them, are installed only in the build's venv.
PYTHONW = _bootstrap.repo_path("ProjectBuilds", ".venv", "Scripts", "pythonw.exe")

# Detached from this console and in its own process group, so the app outlives the shell that
# started it.
_DETACHED = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP


def _window() -> int:
    return win32gui.FindWindow(TRAY_CLASS, None)


def _wait(up: bool, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if bool(_window()) == up:
            return True
        time.sleep(0.25)
    return bool(_window()) == up


def quit_app() -> bool:
    hwnd = _window()
    if not hwnd:
        print("AccessCaster is not running.")
        return True
    # The PROCESS, not the window: the window goes first, and the desk and the single-instance lock
    # are let go only as the process ends.
    _thread, pid = win32process.GetWindowThreadProcessId(hwnd)
    process = win32api.OpenProcess(win32con.SYNCHRONIZE, False, pid)
    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    try:
        ended = win32event.WaitForSingleObject(process, int(QUIT_WAIT_S * 1000)) == 0
    finally:
        win32api.CloseHandle(process)
    if ended:
        print("AccessCaster has quit.")
        return True
    print("AccessCaster is still running: it may be refusing because desk work is in progress.")
    return False


def _cannot_start() -> str:
    """Why a source run cannot start, or "" when it can."""
    if not os.path.isfile(PYTHONW):
        return f"AccessCaster cannot start: there is no {PYTHONW}. Run the build once to make it."
    return ""


def start_app(window: bool = False) -> bool:
    if _window():
        print("AccessCaster is already running.")
        return False
    if _cannot_start():
        print(_cannot_start())
        return False
    args = [PYTHONW, "tray.py"]
    subprocess.Popen(args if window else [*args, STARTUP_FLAG],
                     cwd=_bootstrap.PROJECT, creationflags=_DETACHED, close_fds=True)
    if _wait(True, START_WAIT_S):
        print("AccessCaster is running.")
        return True
    print("AccessCaster did not start: see the diagnostic log.")
    return False


def main() -> int:
    what = sys.argv[1] if len(sys.argv) > 1 else "status"
    window = "--window" in sys.argv[2:]
    if what == "status":
        print("AccessCaster is running." if _window() else "AccessCaster is not running.")
        return 0
    if what == "quit":
        return 0 if quit_app() else 1
    if what == "start":
        return 0 if start_app(window) else 1
    if what == "restart":
        # Checked before quitting, so a restart that cannot start leaves the running copy alone.
        if _cannot_start():
            print(_cannot_start() + " The running copy was left alone.")
            return 1
        return 0 if quit_app() and start_app(window) else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
