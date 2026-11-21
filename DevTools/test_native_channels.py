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
import device_page
import pan_link
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

# ---- a pan link ---------------------------------------------------------------------------------
links, written = {}, {}
with _testkit.patched(
        (device_page, {"desk_record": lambda _key: links,
                       "save_desk_record": lambda _key, value: links.update(value)}),
        (channels, {"set_channel_processing":
                    lambda _name, prop, v: written.__setitem__(prop, round(v * 100))})):
    pan_link.action_link_pan(mics[0], True, 40, 60)
    pan_link.action_set_linked_pan(mics[0], 10, 20, 80)
check("a link at centre starts again from legs moved since",
      written, {"channelPanL": 30, "channelPanR": 90})
check("a pan position and its offset are worded alike",
      [desk_labels.pan_words(p) for p in (0, 30, 50, 70, 100)],
      ["hard left", "40 percent left", "centre", "40 percent right", "hard right"])

# ---- processing in real units -------------------------------------------------------------------
# What RODE's app displays for these knobs at 0.25 and 0.75 (read off it on a Pro II, firmware
# 1.7.7, app 2.0.115): each curve is pinned by two positions and its range.
RODE_SHOWS = {
    ("hpfFrequency", 0.25): 31.25, ("hpfFrequency", 0.75): 121.25,
    ("eqHighBell", 0.25): 2625.0, ("eqMidBell", 0.75): 1212.5, ("eqLowGain", 0.25): -6.0,
    ("compressorThreshold", 0.75): -15.0, ("compressorRatio", 0.25): 1.69,
    ("compressorAttack", 0.75): 3.16, ("compressorRelease", 0.25): 12.57,
    ("compressorRelease", 0.75): 79.53, ("compressorGain", 0.75): 6.75,
    ("noiseGateAttack", 0.25): 1.0, ("noiseGateAttack", 0.75): 100.0,
    ("noiseGateHold", 0.75): 795.27, ("noiseGateHysteresis", 0.25): 25.0,
    ("deesserRatio", 0.75): 6.06, ("deesserRelease", 0.25): 125.74,
    ("deesserFrequency", 0.25): 1681.79, ("deesserFrequency", 0.75): 4756.83,
    ("aphexBBTune", 0.25): 75.75, ("aphexAETune", 0.75): 3075.0,
}
check("every knob's real value matches what RODE's app shows",
      {k: v for k, v in RODE_SHOWS.items()
       if abs(desk_labels.processing_value(*k) - v) > 0.01},
      {})
check("the factory defaults land on the engine's round defaults",
      [round(desk_labels.processing_value(p, x), 1) for p, x in
       (("hpfFrequency", 0.527046), ("eqHighBell", 0.894427), ("compressorRelease", 0.38903),
        ("noiseGateRelease", 0.375804), ("eqMidQ", 0.040404))],
      [70.0, 10000.0, 21.0, 200.0, 0.5])
check("processing values are worded with their units",
      [desk_labels.processing_words(p, x) for p, x in
       (("compressorThreshold", 2 / 3), ("compressorRatio", 0.25), ("deesserFrequency", 0.25),
        ("hpfFrequency", 0.527046), ("compressorAttack", 0.25), ("hpfSlope", 1 / 3),
        ("eqLowGain", 0.5), ("noiseGateRange", 0.3), ("channelPan", 0.5))],
      ["-20 decibels", "1.7 to 1", "1.68 kilohertz", "70 hertz", "0.32 milliseconds",
       "12 decibels per octave", "0 decibels", "30 percent", "centre"])
spoken = {}
for mode in desk_labels.PROCESSING_SPEECH:
    desk_labels.processing_speech = mode
    spoken[mode] = [desk_labels.processing_words(p, x) for p, x in
                    (("compressorThreshold", 2 / 3), ("aphexBBDrive", 0.4), ("channelPan", 0.5))]
desk_labels.processing_speech = "units"
check("processing values are spoken as chosen; a percent knob and pan once whatever the choice",
      spoken,
      {"units": ["-20 decibels", "40 percent", "centre"],
       "percent": ["67 percent", "40 percent", "centre"],
       "units_percent": ["-20 decibels, 67 percent", "40 percent", "centre"],
       "percent_units": ["67 percent, -20 decibels", "40 percent", "centre"]})
check("every processing control is worded by the one function",
      sorted(p for g in desk_labels.PROCESSING_CONTROLS for c in g["controls"]
             for p in [c["prop"]]
             if p not in desk_labels.PROCESSING_UNITS and p not in desk_labels.PAN_PROPS),
      ["channelDepth", "channelPunch", "channelSparkle", "noiseGateRange"])

frame.window.Destroy()
_a11y.check_windows("Channels")
sys.exit(_testkit.finish())
