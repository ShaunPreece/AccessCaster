"""macOS's `tray`: an icon in the menu bar, with wx's TaskBarIcon, which is an NSStatusItem
showing an NSMenu, the system's own menu that VoiceOver reads as one.

Choosing the icon opens its menu: a menu bar item has no separate "open" action. Notifications are
wx's NotificationMessage, which macOS shows in its Notification Centre.
"""
from __future__ import annotations

import subprocess
import threading

import wx
import wx.adv

import diagnostics
from paths import resource_path

APP_NAME = "AccessCaster"
_SHOW_TIMEOUT_S = 10.0


class _MenuBarIcon(wx.adv.TaskBarIcon):
    def __init__(self, owner):
        super().__init__()
        self._owner = owner

    def CreatePopupMenu(self):
        menu = wx.Menu()
        for item in self._owner.menu_items():
            if item is None:
                menu.AppendSeparator()
                continue
            key, label, checked = item
            if checked:
                entry = menu.AppendCheckItem(wx.ID_ANY, label)
                entry.Check(True)
            else:
                entry = menu.Append(wx.ID_ANY, label)
            menu.Bind(wx.EVT_MENU, lambda _event, k=key: self._owner.menu_chosen(k), entry)
        return menu


class Icon:
    def __init__(self, owner):
        self._owner = owner
        self._icon = _MenuBarIcon(owner)
        self._notice = None

    def add(self) -> None:
        path = resource_path("assets", "icon.ico")
        icon = wx.Icon(path, wx.BITMAP_TYPE_ICO)
        if not icon.IsOk():
            diagnostics.event("tray.icon_missing", path)
            icon = wx.ArtProvider.GetIcon(wx.ART_INFORMATION)
        self._icon.SetIcon(icon, APP_NAME)

    def remove(self) -> None:
        self._icon.RemoveIcon()

    def destroy(self) -> None:
        self._icon.Destroy()

    def notify(self, text: str, title: str) -> None:
        wx.CallAfter(self._show_notice, text, title)

    def _show_notice(self, text: str, title: str) -> None:
        notice = wx.adv.NotificationMessage(title, text)
        notice.Bind(wx.adv.EVT_NOTIFICATION_MESSAGE_CLICK,
                    lambda _event: self._owner.notice_chosen())
        notice.Show()
        self._notice = notice        # held, or the click has nothing left to arrive at

    def post_open(self) -> None:
        wx.CallAfter(self._owner.open_window)

    def show_from_other_thread(self):
        shown = threading.Event()
        handle = []

        def show():
            handle.append(self._owner.show_window())
            shown.set()

        wx.CallAfter(show)
        return handle[0] if shown.wait(_SHOW_TIMEOUT_S) else None


def window_to_raise(handle: int) -> int:
    return handle


def find_running(wait_s: float):
    """None: a second copy is told apart from another account's only by the lock."""
    return None


def open_running(running) -> None:
    pass


def show_refusal(text: str) -> None:
    """A native alert through AppleScript, with the text passed as an argument so nothing in it is
    read as script."""
    script = ['-e', 'on run argv',
              '-e', f'display alert "{APP_NAME}" message (item 1 of argv) as warning',
              '-e', 'end run']
    subprocess.run(["osascript", *script, text], capture_output=True)


def open_log(path: str, folder: str) -> None:
    """In TextEdit, which VoiceOver reads, whatever the system would open a `.log` file with."""
    if path is None:
        subprocess.Popen(["open", folder])
    else:
        subprocess.Popen(["open", "-e", path])
