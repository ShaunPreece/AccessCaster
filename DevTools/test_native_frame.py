"""The main window (Project/native/frame.py), offline and never shown: built whole, its Categories
tree shows the page selected, Ctrl+Tab steps a section's pages, and a section's pages can change
under it.

Run: python DevTools/test_native_frame.py
"""
from __future__ import annotations

import sys

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
import wx
from _testkit import check
from native.frame import MainFrame
from native.pages import Page

app = wx.App()
frame = MainFrame(quit_app=lambda: None)
print("THE MAIN WINDOW")
branch, page = frame.tree.selected()
check("it opens on the first section", branch, 0)
check("showing its first page", frame._shown is page.window, True)

frame.go_to("status", "hardware")
branch, page = frame.tree.selected()
check("go_to selects the page asked for", (frame.sections[branch].route, page.key),
      ("status", "hardware"))
check("and shows it", frame._shown is page.window, True)
keys = [p.key for p in frame.tree.pages(branch)]
frame._step_page(1)
check("Ctrl+Tab selects the next page", frame.tree.selected()[1].key,
      keys[(keys.index("hardware") + 1) % len(keys)])
frame._step_page(-1)
frame._step_page(-1)
check("Ctrl+Shift+Tab goes back past it", frame.tree.selected()[1].key,
      keys[(keys.index("hardware") - 1) % len(keys)])

channels = frame.sections[0]
frame.tree.select(0)
first = channels.pages[0]
channels.pages = [first, Page("extra", "Extra", window=wx.Panel(frame.host))]
frame.pages_changed(channels)
check("a section's new pages are listed", [p.key for p in frame.tree.pages(0)],
      [first.key, "extra"])
frame.go_to(channels.route, "extra")
check("and can be gone to", frame.tree.selected()[1].key, "extra")
channels.pages = [first]
frame.pages_changed(channels)
check("losing them leaves the section shown", frame._shown is first.window, True)
frame.Destroy()
sys.exit(_testkit.finish())
