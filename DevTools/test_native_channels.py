"""The native window's channel pages (Project/native/channels_section.py and channel_tabs.py),
offline: a page and each of its tabs are built only when shown, a tab follows the one last chosen
on any channel page, and a newer record reaches only the tabs built so far.

Run: python DevTools/test_native_channels.py
"""
from __future__ import annotations

import sys

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _a11y
import _testkit
import channels
import desk_labels
import wx
from _native_fakes import FakeDesk as BaseDesk, FakeFrame as BaseFrame
from _testkit import check, check_true
from native import channels_section

app = wx.App()


class FakeDesk(BaseDesk):
    def __init__(self, data):
        super().__init__()
        self.channels = data


class FakeFrame(BaseFrame):
    def __init__(self, data):
        super().__init__(FakeDesk(data))
        wx.StaticText(self.window, label="Categories")    # as MainFrame names its tree
        self.tree = wx.TreeCtrl(self.window)

    def pages_changed(self, section):
        pass

    def show(self, page):
        """As MainFrame._show does: build, `before_show` while hidden, then show."""
        window = page.window
        window.before_show()
        for other in section.channel_pages.values():
            other.Hide()
        window.Show()
        return window


def record(name, kind):
    return {"name": name, "label": name, "kind": kind, "muted": False, "solo": False,
            "processing_on": True, "advanced": True, "modules": {}, "params": {},
            "level_db": 40 if kind == "mic" else -12, "stereo": False, "on_fader": True,
            "effects": {"echo": False, "reverb": False, "echo_params": {}, "reverb_params": {}}}


data = {"ok": True, "groups": desk_labels.PROCESSING_CONTROLS, "modules": desk_labels.PROCESSING_MODULES,
        "mic_types": sorted([list(kv) for kv in channels.MIC_TYPE.items()]), "presets": [],
        "pan_links": {}, "wireless_mics": {}, "gain_min_db": channels.GAIN_MIN_DB,
        "gain_max_db": channels.GAIN_MAX_DB, "level_min_db": channels.LEVEL_MIN_DB,
        "level_max_db": channels.LEVEL_MAX_DB}
mics = [c["name"] for c in channels.active_ui_channels() if c["kind"] == "mic"][:2]
frame = FakeFrame(data)
section = channels_section.ChannelsSection(frame)
section._desk_event("read", {"ok": True, "channels": [record(n, "mic") for n in mics]})
by_key = {p.key: p for p in section.pages}

# ---- building on first show --------------------------------------------------------------------
check("no channel page is built by the read", section.channel_pages, {})
first = frame.show(by_key[mics[0]])
check("showing a page builds that page only", list(section.channel_pages), [mics[0]])
check("only its first tab is built", [t.built for t in first.tabs][:3], [True, False, False])
check_true("every tab is listed all the same", first.book.GetPageCount() == len(first.tabs)
           and len(first.tabs) > 3)

# ---- moving tab --------------------------------------------------------------------------------
dyn = next(i for i, t in enumerate(first.tabs) if t.key == "dyn")
first.book.SetSelection(dyn)
check_true("a tab the user moves to is built", first.tabs[dyn].built)
check_true("its controls are made", len(first.tabs[dyn].controls) > 0)
check("the chosen tab is remembered", section.tab_key, "dyn")


def native_change(page, new):
    """A change as the native tab control makes it on Windows: PAGE_CHANGING names the old tab."""
    old = page.book.GetSelection()
    for kind, sel in ((wx.wxEVT_NOTEBOOK_PAGE_CHANGING, old), (wx.wxEVT_NOTEBOOK_PAGE_CHANGED, new)):
        event = wx.BookCtrlEvent(kind, page.book.GetId(), sel, old)
        event.SetEventObject(page.book)
        page.book.GetEventHandler().ProcessEvent(event)


proc = next(i for i, t in enumerate(first.tabs) if t.key == "processing")
native_change(first, proc)
check_true("a tab the native control moves to is built before it is shown",
           first.tabs[proc].built and len(first.tabs[proc].controls) > 0)
first.book.ChangeSelection(proc)    # the native control's own selection change
first.book.SetSelection(dyn)

second = frame.show(by_key[mics[1]])
check("the next channel opens on the same tab", second.tabs[second.book.GetSelection()].key, "dyn")
check("and builds only that tab", [t.key for t in second.tabs if t.built], ["dyn"])

# ---- a newer record ----------------------------------------------------------------------------
newer = [record(n, "mic") for n in mics]
newer[0]["muted"] = newer[1]["muted"] = True
section._desk_event("read", {"ok": True, "channels": newer})
settings = first.tabs[0]
check("a built tab shows the newer record", settings.mute.box.GetValue(), True)
check("an unbuilt tab is left alone", second.tabs[0].built, False)
frame.show(by_key[mics[1]])
second.book.SetSelection(0)
check_true("and reads the record when it is built", second.tabs[0].built
           and second.tabs[0].mute.box.GetValue() is True)

# ---- a list applied by its button keeps the user's choice ---------------------------------------
types = [value for value, _text in data["mic_types"]]


def read_mic(mic_type, muted):
    recs = [record(n, "mic") for n in mics]
    recs[0].update(mic_type=mic_type, muted=muted)
    section._desk_event("read", {"ok": True, "channels": recs})


read_mic(types[0], False)
check("the mic type list shows the desk's type", settings.mic_type.GetSelection(), 0)
settings.mic_type.SetSelection(1)
read_mic(types[0], True)
check("a choice not yet applied survives a change elsewhere on the channel",
      settings.mic_type.GetSelection(), 1)
read_mic(types[2], True)
check("a new type from the desk is shown", settings.mic_type.GetSelection(), 2)

frame.window.Destroy()
_a11y.check_windows("Channels")
sys.exit(_testkit.finish())
