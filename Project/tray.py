"""AccessCaster - system-tray host (the packaged Windows app's entry point).

Running this is the "app". It starts the local AccessCaster server in the background, opens the
accessible web UI, and parks an icon in the Windows notification area (system tray). Unlike the
plain `python server.py` launcher, closing the web window does NOT quit: the server keeps running
behind the tray icon. The user fully quits from the tray menu.

Tray right-click (or the keyboard Applications key / Shift+F10 on the selected icon) opens a
NATIVE Win32 popup menu, and native is the whole point: a screen reader reads it as a standard menu
without anything of ours having to describe it. The menu offers:

  - Open AccessCaster    re-open / focus the configurator web page
  - Announcer            checkable toggle for the spoken button-press announcer
  - Diagnostic log       open the app's own log of what it did (see Project/diagnostics.py)
  - Quit                 stop the server and exit

Accessibility: the icon registers as NOTIFYICON_VERSION_4 so the shell delivers proper keyboard
selection (NIN_KEYSELECT) and context-menu (WM_CONTEXTMENU) notifications, i.e. the whole thing
is operable from the keyboard via the notification area (Win+B), not mouse-only. The menu is a
real HMENU shown with TrackPopupMenu, with the Announcer state exposed as a native checkmark.
"""

from __future__ import annotations

import os
import subprocess
import sys

import win32api
import win32con
import win32gui

import autostart
import diagnostics
import server
from paths import resource_path

APP_NAME = "AccessCaster"
URL = f"http://localhost:{server.HTTP_PORT}/"

# Tray callback + menu command identifiers.
WM_TRAY = win32con.WM_USER + 20          # our private "the tray icon did something" message
ID_OPEN = 1023
ID_ANNOUNCE = 1024
ID_LOG = 1026
ID_QUIT = 1025

# Shell notification-icon constants not all exposed by win32con.
NIN_SELECT = win32con.WM_USER + 0        # left-click / Space on the selected icon
NIN_KEYSELECT = win32con.WM_USER + 1     # Enter on the selected icon
NOTIFYICON_VERSION_4 = 4
_TRAY_ICON_ID = 1


def _show_page() -> None:
    """Raise the app window if one is open, otherwise open a fresh one."""
    if not server._focus_app_window():
        server.open_ui(URL)


class TrayApp:
    def __init__(self):
        self.hwnd = None
        self._create_window()
        self._add_icon()

    # -- hidden message-only-ish window that receives the tray callbacks --------------------------
    def _create_window(self) -> None:
        hinst = win32gui.GetModuleHandle(None)
        wc = win32gui.WNDCLASS()
        wc.hInstance = hinst
        wc.lpszClassName = "AccessCasterTray"
        wc.lpfnWndProc = {
            WM_TRAY: self._on_tray,
            win32con.WM_COMMAND: self._on_command,
            win32con.WM_DESTROY: self._on_destroy,
            win32con.WM_CLOSE: lambda h, m, w, l: win32gui.DestroyWindow(h),
        }
        class_atom = win32gui.RegisterClass(wc)
        self.hwnd = win32gui.CreateWindow(class_atom, APP_NAME, win32con.WS_OVERLAPPED,
                                          0, 0, 0, 0, 0, 0, hinst, None)
        win32gui.UpdateWindow(self.hwnd)

    def _load_icon(self):
        """The app icon, from the bundled assets/icon.ico; falls back to the stock application
        icon if the file is missing (e.g. running from source before the icon is generated)."""
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
        elif event in (NIN_KEYSELECT, NIN_SELECT, win32con.WM_LBUTTONDBLCLK):
            _show_page()
        return 0

    # -- the native context menu -----------------------------------------------------------------
    def _show_menu(self) -> None:
        menu = win32gui.CreatePopupMenu()
        win32gui.AppendMenu(menu, win32con.MF_STRING, ID_OPEN, "&Open AccessCaster")
        ann_flags = win32con.MF_STRING
        if server._announce_on.is_set():
            ann_flags |= win32con.MF_CHECKED
        win32gui.AppendMenu(menu, ann_flags, ID_ANNOUNCE, "&Announcer")
        win32gui.AppendMenu(menu, win32con.MF_STRING, ID_LOG, "Diagnostic &log")
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
            _show_page()
        elif cmd == ID_ANNOUNCE:
            self._toggle_announcer()
        elif cmd == ID_LOG:
            self._open_log()
        elif cmd == ID_QUIT:
            self._quit()
        return 0

    # -- actions ---------------------------------------------------------------------------------
    def _open_log(self) -> None:
        """Show the diagnostic log. IN NOTEPAD, deliberately, rather than by handing the file to
        the shell: a `.log` extension often has no registered handler, and `os.startfile` on one
        that does not opens the "how do you want to open this" chooser - a dialog with nothing to
        read in it. Notepad is on every Windows machine and reads correctly with a screen reader.

        The log is flushed line by line, so what opens is current to the last two seconds."""
        diagnostics.flush()
        path = diagnostics.log_path()
        if not os.path.exists(path):
            # Nothing has been written yet (the log starts with the server). Show the folder
            # rather than failing silently, so the menu item always does something visible.
            subprocess.Popen(["explorer.exe", diagnostics.folder()])
            return
        subprocess.Popen(["notepad.exe", path])

    def _toggle_announcer(self) -> None:
        # Saved or not by the server, by the same rule as the page's switch and the shortcut.
        server.action_set_announce(not server._announce_on.is_set())

    def _quit(self) -> None:
        self._remove_icon()
        server.stop_server()
        win32gui.DestroyWindow(self.hwnd)

    def _on_destroy(self, hwnd, msg, wparam, lparam):
        self._remove_icon()
        win32gui.PostQuitMessage(0)
        return 0


def main() -> int:
    # STARTED BY THE RUN ENTRY, OR BY A PERSON? The Run value carries `--startup` (see
    # autostart.py, which writes the same string installer.iss does). At sign-in we come up in the
    # tray with the announcer and the shortcuts live and open NOTHING: a browser window taking the
    # focus of whatever you signed in to do is the reason people turn autostart off again.
    at_login = autostart.started_at_login()

    # ANOTHER SIGNED-IN ACCOUNT ON THIS PC HAS IT: refuse and stop. Checked before the
    # already-serving branch below, which answers over localhost and so cannot tell whose instance
    # replied; without this it opens the other account's UI, in which nothing this user presses or
    # hears belongs to them.
    if server._other_account_holds_port(server.HTTP_PORT):
        server._refuse_other_account(not at_login)
        return 1

    # Single instance: if a server is already answering, just open/focus its page and exit. This
    # stops a second tray + server starting, and never disturbs the one already running.
    if server._app_already_serving(server.HTTP_PORT):
        # ...except at login, where there is nothing to show and nobody asked to see it. Something
        # is already serving, which is the whole point, so leave it alone and go.
        if at_login:
            return 0
        _show_page()
        return 0

    started = server.start_server_managed(open_browser=not at_login)
    if started is None:
        # A race: someone else grabbed the port in the moment between our check and bind. Same
        # reasoning as above - at login the other instance IS the outcome we wanted, silently.
        if at_login:
            return 0
        _show_page()
        return 0

    TrayApp()
    win32gui.PumpMessages()
    return 0


if __name__ == "__main__":
    sys.exit(main())
