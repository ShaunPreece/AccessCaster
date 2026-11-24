"""The Categories tree (Project/native/category_tree.py), offline, in both forms: the Win32 tree
view Windows uses and the outline view macOS uses, which wx can build on either system. Each must
select, report and rebuild the same way, since the window drives both through one interface.

Run: python DevTools/test_native_tree.py
"""
from __future__ import annotations

import sys

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
import wx
from _testkit import check
from native.category_tree import OutlineTree, Win32Tree
from native.pages import Page

app = wx.App()


def pages(*keys):
    return [Page(key, key.title(), window=object()) for key in keys]


def run(kind):
    print(f"THE CATEGORIES TREE, {kind.__name__}")
    frame = wx.Frame(None)
    reported = []
    tree = kind(frame, lambda page: reported.append(page.key))
    mixer = pages("status")
    tree.add_branch("Mixer", mixer)
    tree.add_branch("Device", pages("state", "hardware", "network"))
    tree.add_branch("Help", pages("help"))
    check("adding sections reports nothing", reported, [])
    check("a section with one page lists none under it", tree.pages(0), [])
    check("a section with three lists all three",
          [p.key for p in tree.pages(1)], ["state", "hardware", "network"])

    tree.select(0)
    check("selecting a section reports its first page", reported, ["status"])
    check("and it is the selection", (tree.selected()[0], tree.selected()[1].key), (0, "status"))

    reported.clear()
    tree.select(1, "network")
    check("selecting a page by key reports it", reported, ["network"])
    check("and selects it under its section", (tree.selected()[0], tree.selected()[1].key),
          (1, "network"))
    tree.select(1, "missing")
    check("an unknown key selects the section", tree.selected()[1].key, "state")

    reported.clear()
    tree.select(1, "hardware")
    reported.clear()
    tree.set_pages(1, pages("state", "hardware"))
    check("a rebuild keeps the page that survives", tree.selected()[1].key, "hardware")
    check("and reports it once", reported, ["hardware"])

    reported.clear()
    tree.set_pages(1, pages("state"))
    check("a rebuild that drops the page leaves the section selected",
          (tree.selected()[0], tree.selected()[1].key), (1, "state"))
    check("and reports what is selected", reported, ["state"])

    tree.select(0)
    reported.clear()
    tree.set_pages(0, pages("mic1", "mic2"))
    check("a single-page section gains pages", [p.key for p in tree.pages(0)], ["mic1", "mic2"])
    check("a rebuild elsewhere leaves the selection on the section", tree.selected()[0], 0)
    check("and reports the section's new first page", reported, ["mic1"])
    tree.select(0, "mic2")
    check("its new pages can be selected", tree.selected()[1].key, "mic2")
    frame.Destroy()


run(Win32Tree)
run(OutlineTree)
sys.exit(_testkit.finish())
