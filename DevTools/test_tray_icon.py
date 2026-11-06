"""Offline check: the tray icon comes back when Explorer restarts.

Explorer's restart takes every notification icon with it and broadcasts `TaskbarCreated`; an app
that ignores it runs on with Quit and its menu out of reach. This builds the real hidden icon
window (it is never shown and takes no focus), sends it that message, and checks the icon is added
again, and not while the app is quitting.

    python DevTools/test_tray_icon.py
"""
from __future__ import annotations

import sys

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
import tray
import win32gui
from _testkit import check

added = []
app = tray.TrayApp.__new__(tray.TrayApp)     # the icon window alone: no wx app, no service
app._quitting = False
app._add_icon = lambda: added.append(True)
app._create_window()
taskbar_created = win32gui.RegisterWindowMessage("TaskbarCreated")

print("EXPLORER RESTARTS")
win32gui.SendMessage(app.hwnd, taskbar_created, 0, 0)
check("the icon is added again", added, [True])

app._quitting = True
win32gui.SendMessage(app.hwnd, taskbar_created, 0, 0)
check("but not while the app is quitting", added, [True])

win32gui.DestroyWindow(app.hwnd)
print()
sys.exit(_testkit.finish())
