"""Offline check: the tray icon comes back when Explorer restarts, and its menu runs the item
chosen.

Explorer's restart takes every notification icon with it and broadcasts `TaskbarCreated`; an app
that ignores it runs on with Quit and its menu out of reach. This builds the real hidden icon
window (it is never shown and takes no focus), sends it that message, and checks the icon is added
again, and not while the app is quitting.

    python DevTools/test_win_tray.py
"""
from __future__ import annotations

import sys

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
import win32con
import win32gui
from _testkit import check
from win import tray


class _Owner:
    quitting = False
    chosen: list = []

    def menu_chosen(self, key):
        self.chosen.append(key)


owner = _Owner()
icon = tray.Icon(owner)                      # the icon window alone: no wx app, no service
added = []
icon.add = lambda: added.append(True)
taskbar_created = win32gui.RegisterWindowMessage("TaskbarCreated")

print("EXPLORER RESTARTS")
win32gui.SendMessage(icon.hwnd, taskbar_created, 0, 0)
check("the icon is added again", added, [True])

owner.quitting = True
win32gui.SendMessage(icon.hwnd, taskbar_created, 0, 0)
check("but not while the app is quitting", added, [True])

print("\nA MENU ITEM IS CHOSEN")
icon._menu_keys = ["open", "announce", "quit"]
win32gui.SendMessage(icon.hwnd, win32con.WM_COMMAND, tray._FIRST_MENU_ID + 2, 0)
check("the owner hears the item's key", owner.chosen, ["quit"])
win32gui.SendMessage(icon.hwnd, win32con.WM_COMMAND, tray._FIRST_MENU_ID + 3, 0)
check("a command no item was given is ignored", owner.chosen, ["quit"])

icon.destroy()
print()
sys.exit(_testkit.finish())
