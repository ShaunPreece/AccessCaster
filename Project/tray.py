"""AccessCaster - the packaged Windows app's entry point: the service, the window and the tray icon,
in one process.

Running this is the "app". It brings the service up (announcer, shortcuts and watchers) on a
background thread, opens the native window (`native/`), and parks an icon in the Windows
notification area. Closing the window hides it: the service keeps running behind the icon,
and the window comes back as it was left. The user quits from the tray menu.

Tray right-click (or the keyboard Applications key / Shift+F10 on the selected icon) opens a
NATIVE Win32 popup menu, and native is the whole point: a screen reader reads it as a standard menu
without anything of ours having to describe it. The menu offers:

  - Open AccessCaster    show the window
  - Announcer            checkable toggle for the spoken button-press announcer
  - Diagnostic log       open the app's own log of what it did (see Project/diagnostics.py)
  - Check for updates    or "Install update to version X" once one is waiting (app_updates.py)
  - Quit                 stop the service and exit

The icon is also how the update feature reaches somebody who is not looking at the window: a new
version is announced as a WINDOWS NOTIFICATION from it (`_notify`), which a screen reader reads
without the focus moving and without the app's own voice talking over it.

Accessibility: the icon registers as NOTIFYICON_VERSION_4 so the shell delivers proper keyboard
selection (NIN_KEYSELECT) and context-menu (WM_CONTEXTMENU) notifications, i.e. the whole thing
is operable from the keyboard via the notification area (Win+B), not mouse-only. The menu is a
real HMENU shown with TrackPopupMenu, with the Announcer state exposed as a native checkmark.

The icon's window is a plain Win32 window on the wx thread: wx's message loop dispatches its
messages like any other window's, so the icon and the window share one thread and nothing here
needs a lock.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes

import wx

import announcer
import app_updates
import app_window
import autostart
import desk_access
import diagnostics
import instance
import rode_app
import service
import speech
import win32api
import win32con
import win32gui
import win32process
from native import dialogs
from native.dialogs import TextDialog
from native.frame import MainFrame
from paths import resource_path
from selfupdate import phrases as update_phrases

APP_NAME = "AccessCaster"
TRAY_CLASS = "AccessCasterTray"

# Tray callback + menu command identifiers.
WM_TRAY = win32con.WM_USER + 20          # our private "the tray icon did something" message
ID_OPEN = 1023
ID_ANNOUNCE = 1024
ID_LOG = 1026
ID_UPDATE = 1027
ID_QUIT = 1025
# Posted to show and raise the window: by another thread, and by a second launch of the app, which
# grants this process the foreground first.
WM_OPEN_WINDOW = win32con.WM_USER + 21
# Sent from another thread: shows the window and returns its handle, and raises nothing. The sender
# raises it, because the right to take the foreground is the sender's (`_show_from_any_thread`).
WM_SHOW_WINDOW = win32con.WM_USER + 22
_SHOW_TIMEOUT_MS = 10000

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

# One press can arrive as several open events (see _on_tray); repeats inside this window are
# dropped.
_OPEN_DEBOUNCE_S = 1.0
_OPEN_EVENTS = (NIN_SELECT, NIN_KEYSELECT, win32con.WM_LBUTTONUP, win32con.WM_LBUTTONDBLCLK)

_OTHER_ACCOUNT = ("AccessCaster is already running in another Windows account on this PC.\n\n"
                  "Only one account can use it at a time. There is a single RODECaster on the USB, "
                  "and the spoken announcements and keyboard shortcuts belong to the account that "
                  "started the app.\n\n"
                  "Quit AccessCaster in the other account, or sign that account out, then start it "
                  "again here.")
# How long a second launch waits for this session's copy to show its icon window, when the lock
# is held but no window is found yet: a copy that has only just started.
_FIND_RUNNING_S = 3.0
# How long it then waits for the lock to come free: a copy that has just quit holds it until its
# process ends, a moment after its icon window has gone.
_QUITTING_COPY_S = 10.0

_user32 = ctypes.windll.user32
_user32.GetLastActivePopup.argtypes = [wintypes.HWND]
_user32.GetLastActivePopup.restype = wintypes.HWND
_user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                        wintypes.LPARAM, wintypes.UINT, wintypes.UINT,
                                        ctypes.POINTER(ctypes.c_size_t)]
_user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
_SMTO_ABORTIFHUNG = 0x0002


def _to_raise(hwnd) -> int:
    """The window to raise for `hwnd`: a modal dialog open on it, or the window itself."""
    return _user32.GetLastActivePopup(hwnd) or hwnd


class TrayApp:
    """The process: the service, the tray icon and the window, all owned here.

    The window is built the first time it is wanted and kept when it is closed, so it reopens
    instantly and where it was left; it is destroyed only by Quit."""

    def __init__(self, app: wx.App, loop: wx.GUIEventLoop, open_window: bool):
        self.app = app
        self.loop = loop
        self.hwnd = None
        self.frame = None
        self._ui_thread = threading.get_ident()
        self._starting = True             # the service is still coming up
        self._quitting = False
        self._window_gone = False         # quit has closed the window and its dialogs
        self._last_open = float("-inf")
        self._create_window()
        self._add_icon()
        app_updates.notify = self._notify
        rode_app.on_seen = self._rode_app_seen
        app_updates.open_window = lambda: win32gui.PostMessage(self.hwnd, WM_OPEN_WINDOW, 0, 0)
        app_window.show = self._show_from_any_thread
        # Windows ending the session closes the window through wx as well as through the icon's
        # window. wx's own answer to the query closes the window, which hides it, and so refuses to
        # let Windows end the session; these replace it.
        app.Bind(wx.EVT_QUERY_END_SESSION, lambda event: None)
        app.Bind(wx.EVT_END_SESSION, lambda event: self._quit())
        if open_window:
            self.open_window()
        # Started at sign-in, the announcer's greeting is what says the app is running. With the
        # window opening in front, it says so already.
        greeting = "" if open_window else announcer.GREETING_STARTED
        threading.Thread(target=self._bring_up, args=(greeting,), name="service-start",
                         daemon=True).start()

    # -- the service ------------------------------------------------------------------------------
    def _bring_up(self, greeting: str) -> None:
        service.start(greeting=greeting)
        wx.CallAfter(self._brought_up)

    def _brought_up(self) -> None:
        self._starting = False
        if self._quitting:
            # Quit was chosen while the service came up: finish what `_quit` left, once its window
            # has closed (`_close_window` finishes it otherwise).
            if self._window_gone:
                self._finish_quit()
            return
        if self.frame:
            self.frame.start()

    # -- the window -------------------------------------------------------------------------------
    def _show_window(self) -> int:
        """Show the window, building it the first time. Returns its handle, or 0 while quitting.
        Raises nothing: that needs the right to take the foreground, which the caller holds."""
        if self._quitting:
            return 0
        if not self.frame:
            self.frame = MainFrame(self._request_quit)
            self.frame.present()
            self.frame.tree.SetFocus()
            if not self._starting:
                self.frame.start()
        else:
            self.frame.present()
        return self.frame.GetHandle()

    def open_window(self) -> None:
        """Show and raise the window, on this thread, then open an update decision the tray asked
        for (`app_updates.request_decision`)."""
        hwnd = self._show_window()
        if not hwnd:
            return
        app_window.raise_window(_to_raise(hwnd))
        if not self._starting and app_updates.take_request():
            self.frame.check_updates()

    def _show_from_any_thread(self) -> bool:
        """`app_window.show`. The Open AccessCaster shortcut calls this on the hotkey thread, inline,
        where the foreground right exists (hotkeys._FOREGROUND_COMMANDS). The window is shown on
        the wx thread by a SENT message, so the shortcut's handler is still running when it returns,
        and the raise happens back on the calling thread."""
        if threading.get_ident() == self._ui_thread:
            hwnd = self._show_window()
        else:
            result = ctypes.c_size_t(0)
            if not _user32.SendMessageTimeoutW(self.hwnd, WM_SHOW_WINDOW, 0, 0, _SMTO_ABORTIFHUNG,
                                               _SHOW_TIMEOUT_MS, ctypes.byref(result)):
                return False
            hwnd = result.value
        return bool(hwnd) and app_window.raise_window(_to_raise(hwnd))

    # -- hidden window that receives the tray callbacks -------------------------------------------
    def _create_window(self) -> None:
        hinst = win32gui.GetModuleHandle(None)
        # The shell broadcasts this when Explorer restarts, with every notification icon gone.
        taskbar_created = win32gui.RegisterWindowMessage("TaskbarCreated")
        wc = win32gui.WNDCLASS()
        wc.hInstance = hinst
        wc.lpszClassName = TRAY_CLASS
        wc.lpfnWndProc = {
            WM_TRAY: self._on_tray,
            win32con.WM_COMMAND: self._on_command,
            win32con.WM_CLOSE: lambda h, m, w, l: self._request_quit() or 0,
            WM_OPEN_WINDOW: lambda h, m, w, l: self.open_window() or 0,
            WM_SHOW_WINDOW: lambda h, m, w, l: self._show_window(),
            win32con.WM_QUERYENDSESSION: lambda h, m, w, l: 1,
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
        """The app icon, from the bundled assets/icon.ico; falls back to the stock application
        icon if the file is missing or will not load."""
        path = resource_path("assets", "icon.ico")
        flags = win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE
        try:
            return win32gui.LoadImage(0, path, win32con.IMAGE_ICON, 0, 0, flags)
        except Exception:
            return win32gui.LoadIcon(0, win32con.IDI_APPLICATION)

    def _add_icon(self) -> None:
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        nid = (self.hwnd, _TRAY_ICON_ID, flags, WM_TRAY, self._load_icon(), APP_NAME)
        win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, nid)
        # Opt into v4 notifications: proper keyboard selection + context-menu events, so the icon
        # is fully operable from the keyboard / by a screen-reader user.
        win32gui.Shell_NotifyIcon(win32gui.NIM_SETVERSION,
                                  (self.hwnd, _TRAY_ICON_ID, 0, NOTIFYICON_VERSION_4))

    def _on_taskbar_created(self, hwnd, msg, wparam, lparam):
        """Explorer has restarted and the icon has gone with it: add it again, or the app runs on
        with Quit and the menu out of reach."""
        if not self._quitting:
            try:
                self._add_icon()
            except win32gui.error as exc:
                diagnostics.event("tray.icon_readd_failed", str(exc))
        return 0

    def _remove_icon(self) -> None:
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.hwnd, _TRAY_ICON_ID))
        except Exception:
            pass

    # -- tray icon events ------------------------------------------------------------------------
    def _on_tray(self, hwnd, msg, wparam, lparam):
        # Under NOTIFYICON_VERSION_4 the event id is the low word of lParam.
        event = win32api.LOWORD(lparam)
        if event in (win32con.WM_CONTEXTMENU, win32con.WM_RBUTTONUP):
            self._show_menu()
        elif event == NIN_BALLOONUSERCLICK:
            # The only notifications this icon raises are the update's, so choosing one is asking
            # for the decision. With nothing waiting it simply shows the window.
            if app_updates.held_version() is not None:
                app_updates.request_decision()
            else:
                self.open_window()
        elif event in _OPEN_EVENTS:
            # WM_LBUTTONUP is the only event the Windows 11 hidden-icons overflow sends, for a
            # click, Enter, a screen reader or UIA Invoke alike: it never sends NIN_SELECT. On the
            # taskbar the same click can send WM_LBUTTONUP and NIN_SELECT, hence the debounce.
            now = time.monotonic()
            if now - self._last_open >= _OPEN_DEBOUNCE_S:
                self._last_open = now
                self.open_window()
        return 0

    # -- the native context menu -----------------------------------------------------------------
    def _show_menu(self) -> None:
        menu = win32gui.CreatePopupMenu()
        win32gui.AppendMenu(menu, win32con.MF_STRING, ID_OPEN, "&Open AccessCaster")
        ann_flags = win32con.MF_STRING
        if speech.enabled.is_set():
            ann_flags |= win32con.MF_CHECKED
        win32gui.AppendMenu(menu, ann_flags, ID_ANNOUNCE, "&Announcer")
        win32gui.AppendMenu(menu, win32con.MF_STRING, ID_LOG, "Diagnostic &log")
        waiting = app_updates.held_version()
        win32gui.AppendMenu(menu, win32con.MF_STRING, ID_UPDATE,
                            update_phrases.tray_install(waiting) if waiting is not None
                            else update_phrases.TRAY_CHECK)
        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
        win32gui.AppendMenu(menu, win32con.MF_STRING, ID_QUIT, "&Quit")
        pos = win32gui.GetCursorPos()
        # SetForegroundWindow + the trailing WM_NULL are the documented workaround so the menu
        # dismisses correctly when the user clicks (or tabs) away from it.
        win32gui.SetForegroundWindow(self.hwnd)
        win32gui.TrackPopupMenu(menu, win32con.TPM_LEFTALIGN | win32con.TPM_RIGHTBUTTON,
                                pos[0], pos[1], 0, self.hwnd, None)
        win32gui.PostMessage(self.hwnd, win32con.WM_NULL, 0, 0)
        win32gui.DestroyMenu(menu)

    def _on_command(self, hwnd, msg, wparam, lparam):
        cmd = win32api.LOWORD(wparam)
        if cmd == ID_OPEN:
            self.open_window()
        elif cmd == ID_ANNOUNCE:
            self._toggle_announcer()
        elif cmd == ID_LOG:
            self._open_log()
        elif cmd == ID_UPDATE:
            # A version already waiting goes straight to the decision in the window. Otherwise the
            # check runs on its own thread and answers in a notification, or opens the decision.
            if app_updates.held_version() is not None:
                app_updates.request_decision()
            else:
                app_updates.check_from_tray()
        elif cmd == ID_QUIT:
            self._request_quit()
        return 0

    # -- actions ---------------------------------------------------------------------------------
    def _open_log(self) -> None:
        """Show the diagnostic log. IN NOTEPAD, deliberately, rather than by handing the file to
        the shell: a `.log` extension often has no registered handler, and `os.startfile` on one
        that does not opens the "how do you want to open this" chooser - a dialog with nothing to
        read in it. Notepad is on every Windows machine and reads correctly with a screen reader.

        The log is flushed line by line, so what opens is current to the last two seconds."""
        diagnostics.flush(force=True)
        path = diagnostics.log_path()
        if not os.path.exists(path):
            # Nothing has been written yet (the log starts with the service). Show the folder
            # rather than failing silently, so the menu item always does something visible.
            subprocess.Popen(["explorer.exe", diagnostics.folder()])
            return
        subprocess.Popen(["notepad.exe", path])

    def _notify(self, text: str) -> None:
        """Raise a Windows notification from the tray icon. Called from any thread: Shell_NotifyIcon
        posts to the shell and does not need this window's thread."""
        if len(text) > _NOTIFY_MAX:
            text = text[:_NOTIFY_MAX - 1].rsplit(" ", 1)[0]
        nid = (self.hwnd, _TRAY_ICON_ID, win32gui.NIF_INFO, 0, 0, "", text, 10000,
               update_phrases.NOTIFICATION_TITLE, NIIF_INFO | NIIF_RESPECT_QUIET_TIME)
        win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, nid)

    def _rode_app_seen(self) -> None:
        """RODE's app has opened (`rode_app.on_seen`, on its watcher's thread): said in the app's
        own voice, heard whatever is in front."""
        speech.say(rode_app.NOTICE)

    def _on_end_session(self, hwnd, msg, wparam, lparam):
        """Windows is ending the session, or the installer's Restart Manager is closing the app to
        replace its files (lParam ENDSESSION_CLOSEAPP). Either way stop as Quit does: the shortcuts,
        the HID handles and the port are let go, and an update can then restart the app. Without
        an answer here Restart Manager cannot close the app while its window is hidden."""
        if wparam:
            self._quit()
        return 0

    def _toggle_announcer(self) -> None:
        # Saved or not by `action_set_announce`, by the same rule as the window's switch and the
        # shortcut.
        announcer.action_set_announce(not speech.enabled.is_set())
        if self.frame:
            self.frame.reread("settings")

    def _request_quit(self) -> None:
        """Quit, unless the desk is part-way through being changed (`desk_access.work_in_progress`):
        the window then says what is running, and the app stays. Windows ending the session quits
        regardless, through `_quit`."""
        busy = desk_access.work_in_progress()
        if busy is None:
            self._quit()
            return
        diagnostics.event("app.quit_refused", busy)
        self.open_window()
        if self.frame:
            wx.CallAfter(self._still_working, busy)

    def _still_working(self, busy: str) -> None:
        dialog = TextDialog(self.frame, "Still working",
                            f"{busy} Quit again once it finishes, so the desk is not left half "
                            "written.", [(wx.ID_OK, "OK")], default=wx.ID_OK, escape=wx.ID_OK)
        with dialog:
            dialog.ShowModal()

    def _quit(self) -> None:
        """Quit: the window, the icon, then the service. Safe to call more than once."""
        if self._quitting:
            return
        self._quitting = True
        self._remove_icon()
        self._close_window(set())

    def _close_window(self, ended: set[int]) -> None:
        # A dialog open in the window runs a loop of its own inside this one, and the process waits
        # on it. Only the innermost loop can be ended, so the dialogs end newest first, each once
        # the one inside it has returned. `ended` holds the ids of those already asked to end.
        modal = dialogs.modal_dialogs()
        if modal:
            newest = modal[-1]
            if id(newest) not in ended:
                ended.add(id(newest))
                newest.EndModal(wx.ID_CANCEL)
            wx.CallLater(50, self._close_window, ended)
            return
        if self.frame:
            self.frame.Close(force=True)
        self._window_gone = True
        if not self._starting:
            self._finish_quit()

    def _finish_quit(self) -> None:
        service.stop("quit from the tray")
        win32gui.DestroyWindow(self.hwnd)
        # Not `Exit`, which is ignored unless this loop is the active one: Quit can arrive inside a
        # dialog's loop, which ends first.
        self.loop.ScheduleExit()


def _claim_when_free(wait_s: float) -> bool:
    """Take the single-instance lock once a quitting copy lets it go, trying for up to `wait_s`."""
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        time.sleep(0.25)
        if instance.claim():
            return True
    return False


def _running_instance(wait_s: float = 0.0):
    """This session's running copy's icon window, or None, looking for up to `wait_s`. FindWindow
    sees only this desktop's windows, so another account's copy is never found here."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            found = win32gui.FindWindow(TRAY_CLASS, None)
        except win32gui.error:
            found = 0
        if found or time.monotonic() >= deadline:
            return found or None
        time.sleep(0.2)


def _refuse_other_account(interactive: bool) -> None:
    """Say that another Windows account already has the app. Only when started by a person: at
    sign-in nobody is watching, and a blocking dialog would sit there unexplained. A native
    MessageBox is a real modal dialog to every screen reader."""
    diagnostics.event("app.refused", "another Windows account is running AccessCaster")
    if interactive:
        MB_OK, MB_ICONWARNING, MB_SETFOREGROUND = 0x0, 0x30, 0x10000
        _user32.MessageBoxW(0, _OTHER_ACCOUNT, APP_NAME, MB_OK | MB_ICONWARNING | MB_SETFOREGROUND)


def main() -> int:
    # STARTED BY THE RUN ENTRY, OR BY A PERSON? The Run value carries `--startup` (see
    # autostart.py, which writes the same string installer.iss does). At sign-in we come up in the
    # tray with the announcer and the shortcuts live and open NOTHING: a window taking the focus
    # of whatever you signed in to do is the reason people turn autostart off again. A restart
    # after an update opens the window: the update was started from it, and App settings there
    # reads what the update did.
    open_window = not autostart.started_at_login()

    # Single instance, across every account. The lock held, the copy is either this session's,
    # which shows its window while this one goes, one that is quitting, whose lock is waited for,
    # or another account's, which is refused.
    running = None
    if not instance.claim():
        running = _running_instance(_FIND_RUNNING_S)
        if running is None and not _claim_when_free(_QUITTING_COPY_S):
            _refuse_other_account(open_window)
            return 1
    if running is not None:
        if open_window:
            # This process was just started by the user, so it holds the foreground right, and
            # hands it to the running copy before asking it to raise its window.
            _, pid = win32process.GetWindowThreadProcessId(running)
            _user32.AllowSetForegroundWindow(pid)
            win32gui.PostMessage(running, WM_OPEN_WINDOW, 0, 0)
        return 0

    app = wx.App()
    # An event loop of our own rather than `app.MainLoop`, which returns at once when no window
    # exists, as at sign-in, and ends when the last one is destroyed. This one runs until Quit.
    loop = wx.GUIEventLoop()
    TrayApp(app, loop, open_window)
    activator = wx.EventLoopActivator(loop)
    loop.Run()
    del activator
    return 0


if __name__ == "__main__":
    sys.exit(main())
