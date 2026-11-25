"""Windows' `tray`: the notification-area icon, its hidden window, its menu and its notifications,
and finding the copy already running.

The menu is a real HMENU shown with TrackPopupMenu, with a checkable item's state as a native
checkmark, so a screen reader reads it as a standard menu without anything of ours describing it.
The icon registers as NOTIFYICON_VERSION_4 so the shell delivers keyboard selection (NIN_KEYSELECT)
and context-menu (WM_CONTEXTMENU) notifications: the icon is operable from the keyboard through the
notification area (Win+B), not mouse-only.

The icon's window is a plain Win32 window on the wx thread: wx's message loop dispatches its
messages like any other window's, so the icon and the app's window share one thread.
"""
from __future__ import annotations

import ctypes
import subprocess
import time
from ctypes import wintypes

import win32api
import win32con
import win32gui
import win32process

import diagnostics
from paths import resource_path

APP_NAME = "AccessCaster"
TRAY_CLASS = "AccessCasterTray"

# The icon's private "the tray icon did something" message.
WM_TRAY = win32con.WM_USER + 20
# Posted to show and raise the window: by another thread, and by a second launch of the app, which
# grants this process the foreground first.
WM_OPEN_WINDOW = win32con.WM_USER + 21
# Sent from another thread: shows the window and returns its handle, and raises nothing. The sender
# raises it, because the right to take the foreground is the sender's.
WM_SHOW_WINDOW = win32con.WM_USER + 22
_SHOW_TIMEOUT_MS = 10000
_FIRST_MENU_ID = 1023

# Shell notification-icon constants not all exposed by win32con.
NIN_SELECT = win32con.WM_USER + 0        # left-click / Space on the selected icon
NIN_KEYSELECT = win32con.WM_USER + 1     # Enter on the selected icon
NIN_BALLOONUSERCLICK = win32con.WM_USER + 5   # the user chose our notification
NIIF_INFO = 0x01
# A notification raised in the user's first hour after signing in to a new machine waits, as
# Windows asks of every app.
NIIF_RESPECT_QUIET_TIME = 0x80
# The shell refuses a notification body longer than this, in characters.
_NOTIFY_MAX = 255
NOTIFYICON_VERSION_4 = 4
_TRAY_ICON_ID = 1

# One press can arrive as several open events (see `_on_tray`); repeats inside this window are
# dropped.
_OPEN_DEBOUNCE_S = 1.0
_OPEN_EVENTS = (NIN_SELECT, NIN_KEYSELECT, win32con.WM_LBUTTONUP, win32con.WM_LBUTTONDBLCLK)

_user32 = ctypes.windll.user32
_user32.GetLastActivePopup.argtypes = [wintypes.HWND]
_user32.GetLastActivePopup.restype = wintypes.HWND
_user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                        wintypes.LPARAM, wintypes.UINT, wintypes.UINT,
                                        ctypes.POINTER(ctypes.c_size_t)]
_user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
_SMTO_ABORTIFHUNG = 0x0002


def window_to_raise(handle: int) -> int:
    """A modal dialog open on the window, or the window itself."""
    return _user32.GetLastActivePopup(handle) or handle


class Icon:
    def __init__(self, owner):
        self._owner = owner
        self._last_open = float("-inf")
        self._menu_keys: list = []
        hinst = win32gui.GetModuleHandle(None)
        # The shell broadcasts this when Explorer restarts, with every notification icon gone.
        taskbar_created = win32gui.RegisterWindowMessage("TaskbarCreated")
        wc = win32gui.WNDCLASS()
        wc.hInstance = hinst
        wc.lpszClassName = TRAY_CLASS
        wc.lpfnWndProc = {
            WM_TRAY: self._on_tray,
            win32con.WM_COMMAND: self._on_command,
            win32con.WM_CLOSE: lambda *_: owner.request_quit() or 0,
            WM_OPEN_WINDOW: lambda *_: owner.open_window() or 0,
            WM_SHOW_WINDOW: lambda *_: owner.show_window(),
            win32con.WM_QUERYENDSESSION: lambda *_: 1,
            win32con.WM_ENDSESSION: self._on_end_session,
            taskbar_created: self._on_taskbar_created,
        }
        class_atom = win32gui.RegisterClass(wc)
        self.hwnd = win32gui.CreateWindow(class_atom, APP_NAME, win32con.WS_OVERLAPPED,
                                          0, 0, 0, 0, 0, 0, hinst, None)
        # A process's first ShowWindow takes the show state its launcher asked for, whatever it
        # passes, so a launch with a hidden window style would hide the main window while wx
        # records it as shown, and no later Show could bring it up. This window takes that call.
        win32gui.ShowWindow(self.hwnd, win32con.SW_HIDE)
        win32gui.UpdateWindow(self.hwnd)

    def _load_icon(self):
        """The app icon, from the bundled assets/icon.ico; the stock application icon if the file
        is missing or will not load."""
        path = resource_path("assets", "icon.ico")
        flags = win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE
        try:
            return win32gui.LoadImage(0, path, win32con.IMAGE_ICON, 0, 0, flags)
        except win32gui.error as exc:
            diagnostics.event("tray.icon_missing", f"{path}: {exc}")
            return win32gui.LoadIcon(0, win32con.IDI_APPLICATION)

    def add(self) -> None:
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        nid = (self.hwnd, _TRAY_ICON_ID, flags, WM_TRAY, self._load_icon(), APP_NAME)
        win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, nid)
        win32gui.Shell_NotifyIcon(win32gui.NIM_SETVERSION,
                                  (self.hwnd, _TRAY_ICON_ID, 0, NOTIFYICON_VERSION_4))

    def _on_taskbar_created(self, hwnd, msg, wparam, lparam):
        """Explorer has restarted and the icon has gone with it: add it again, or the app runs on
        with Quit and the menu out of reach."""
        if not self._owner.quitting:
            try:
                self.add()
            except win32gui.error as exc:
                diagnostics.event("tray.icon_readd_failed", str(exc))
        return 0

    def remove(self) -> None:
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.hwnd, _TRAY_ICON_ID))
        except Exception as exc:    # the icon is already gone when Explorer is not running
            diagnostics.event("tray.icon_remove_failed", str(exc))

    def destroy(self) -> None:
        win32gui.DestroyWindow(self.hwnd)

    def _on_tray(self, hwnd, msg, wparam, lparam):
        # Under NOTIFYICON_VERSION_4 the event id is the low word of lParam.
        event = win32api.LOWORD(lparam)
        if event in (win32con.WM_CONTEXTMENU, win32con.WM_RBUTTONUP):
            self._show_menu()
        elif event == NIN_BALLOONUSERCLICK:
            self._owner.notice_chosen()
        elif event in _OPEN_EVENTS:
            # WM_LBUTTONUP is the only event the Windows 11 hidden-icons overflow sends, for a
            # click, Enter, a screen reader or UIA Invoke alike: it never sends NIN_SELECT. On the
            # taskbar the same click can send WM_LBUTTONUP and NIN_SELECT, hence the debounce.
            now = time.monotonic()
            if now - self._last_open >= _OPEN_DEBOUNCE_S:
                self._last_open = now
                self._owner.open_window()
        return 0

    def _show_menu(self) -> None:
        menu = win32gui.CreatePopupMenu()
        self._menu_keys = []
        for item in self._owner.menu_items():
            if item is None:
                win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
                continue
            key, label, checked = item
            flags = win32con.MF_STRING | (win32con.MF_CHECKED if checked else 0)
            win32gui.AppendMenu(menu, flags, _FIRST_MENU_ID + len(self._menu_keys), label)
            self._menu_keys.append(key)
        pos = win32gui.GetCursorPos()
        # SetForegroundWindow and the trailing WM_NULL are the documented workaround so the menu
        # dismisses correctly when the user clicks or tabs away from it.
        win32gui.SetForegroundWindow(self.hwnd)
        win32gui.TrackPopupMenu(menu, win32con.TPM_LEFTALIGN | win32con.TPM_RIGHTBUTTON,
                                pos[0], pos[1], 0, self.hwnd, None)
        win32gui.PostMessage(self.hwnd, win32con.WM_NULL, 0, 0)
        win32gui.DestroyMenu(menu)

    def _on_command(self, hwnd, msg, wparam, lparam):
        index = win32api.LOWORD(wparam) - _FIRST_MENU_ID
        if 0 <= index < len(self._menu_keys):
            self._owner.menu_chosen(self._menu_keys[index])
        return 0

    def _on_end_session(self, hwnd, msg, wparam, lparam):
        """Windows is ending the session, or the installer's Restart Manager is closing the app to
        replace its files (lParam ENDSESSION_CLOSEAPP). Without an answer here Restart Manager
        cannot close the app while its window is hidden."""
        if wparam:
            self._owner.session_ending()
        return 0

    def notify(self, text: str, title: str) -> None:
        """Shell_NotifyIcon posts to the shell and does not need this window's thread."""
        if len(text) > _NOTIFY_MAX:
            text = text[:_NOTIFY_MAX - 1].rsplit(" ", 1)[0]
        nid = (self.hwnd, _TRAY_ICON_ID, win32gui.NIF_INFO, 0, 0, "", text, 10000,
               title, NIIF_INFO | NIIF_RESPECT_QUIET_TIME)
        win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, nid)

    def post_open(self) -> None:
        win32gui.PostMessage(self.hwnd, WM_OPEN_WINDOW, 0, 0)

    def show_from_other_thread(self):
        """A SENT message, so the caller is still running when it returns."""
        result = ctypes.c_size_t(0)
        if not _user32.SendMessageTimeoutW(self.hwnd, WM_SHOW_WINDOW, 0, 0, _SMTO_ABORTIFHUNG,
                                           _SHOW_TIMEOUT_MS, ctypes.byref(result)):
            return None
        return result.value


def quit_asked_by_user() -> bool:
    """Windows asks wx's end-session question only when the session is ending."""
    return False


def find_running(wait_s: float):
    """FindWindow sees only this desktop's windows, so another account's copy is never found."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            found = win32gui.FindWindow(TRAY_CLASS, None)
        except win32gui.error:
            found = 0
        if found or time.monotonic() >= deadline:
            return found or None
        time.sleep(0.2)


def open_running(running) -> None:
    """This process was just started by the user, so it holds the foreground right, and hands it to
    the running copy before asking it to raise its window."""
    _, pid = win32process.GetWindowThreadProcessId(running)
    _user32.AllowSetForegroundWindow(pid)
    win32gui.PostMessage(running, WM_OPEN_WINDOW, 0, 0)


def show_refusal(text: str) -> None:
    """A native MessageBox, a real modal dialog to every screen reader, with no wx app running."""
    mb_ok, mb_iconwarning, mb_setforeground = 0x0, 0x30, 0x10000
    _user32.MessageBoxW(0, text, APP_NAME, mb_ok | mb_iconwarning | mb_setforeground)


def open_log(path: str, folder: str) -> None:
    """IN NOTEPAD, rather than by handing the file to the shell: a `.log` extension often has no
    registered handler, and `os.startfile` on one that does not opens the "how do you want to open
    this" chooser, a dialog with nothing to read in it. Notepad is on every Windows machine and
    reads correctly with a screen reader. With no log yet, the folder, so the item always does
    something visible."""
    if path is None:
        subprocess.Popen(["explorer.exe", folder])
    else:
        subprocess.Popen(["notepad.exe", path])
