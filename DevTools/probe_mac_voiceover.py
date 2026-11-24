"""A window of wx controls that are the system's own on macOS, for a VoiceOver trial: whether wx
can carry the app on a Mac with native controls in place of the two it draws itself there (the
category tree and the app's slider).

    .venv/bin/python DevTools/probe_mac_voiceover.py

The status line under the controls says what each one last did, so a control that VoiceOver
reaches but cannot operate shows up as one whose action never lands. Close the window to end it.
"""
from __future__ import annotations

import wx
import wx.dataview as dv

TREE = [("Mixer", ["Mic 1", "Mic 2", "Chat"]),
        ("Device", ["Hardware", "Network"]),
        ("App settings", ["General", "Shortcuts"])]


class Probe(wx.Frame):
    def __init__(self):
        super().__init__(None, title="VoiceOver probe", size=(640, 520))
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        sizer.Add(wx.StaticText(panel, label="Categories"), 0, wx.ALL, 6)
        self.tree = dv.DataViewTreeCtrl(panel, size=(300, 180))
        for category, pages in TREE:
            node = self.tree.AppendContainer(dv.NullDataViewItem, category)
            for page in pages:
                self.tree.AppendItem(node, page)
            self.tree.Expand(node)
        self.tree.Bind(dv.EVT_DATAVIEW_SELECTION_CHANGED, self._on_tree)
        sizer.Add(self.tree, 0, wx.ALL | wx.EXPAND, 6)

        sizer.Add(wx.StaticText(panel, label="Mic 1 level"), 0, wx.ALL, 6)
        self.slider = wx.Slider(panel, value=50, minValue=0, maxValue=100, name="Mic 1 level")
        self.slider.Bind(wx.EVT_SLIDER, lambda _e: self._status(f"Level {self.slider.GetValue()}"))
        sizer.Add(self.slider, 0, wx.ALL | wx.EXPAND, 6)

        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(panel, label="Input type"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
        self.choice = wx.Choice(panel, choices=["Dynamic", "Condenser", "Line"])
        self.choice.SetSelection(0)
        self.choice.Bind(wx.EVT_CHOICE, lambda _e: self._status(
            f"Input type {self.choice.GetStringSelection()}"))
        row.Add(self.choice, 0, wx.ALL, 6)
        sizer.Add(row)

        self.check = wx.CheckBox(panel, label="Phantom power")
        self.check.Bind(wx.EVT_CHECKBOX, lambda _e: self._status(
            "Phantom power " + ("on" if self.check.GetValue() else "off")))
        sizer.Add(self.check, 0, wx.ALL, 6)

        sizer.Add(wx.StaticText(panel, label="Presets"), 0, wx.ALL, 6)
        self.list = wx.ListBox(panel, choices=["Podcast", "Broadcast", "Music"], size=(300, 80))
        self.list.Bind(wx.EVT_LISTBOX, lambda _e: self._status(
            f"Preset {self.list.GetStringSelection()}"))
        sizer.Add(self.list, 0, wx.ALL, 6)

        button = wx.Button(panel, label="Apply")
        button.Bind(wx.EVT_BUTTON, lambda _e: self._status("Applied"))
        sizer.Add(button, 0, wx.ALL, 6)

        self.status = wx.StaticText(panel, label="Nothing done yet.")
        sizer.Add(self.status, 0, wx.ALL, 6)
        panel.SetSizer(sizer)
        self.tree.SetFocus()

    def _on_tree(self, _event):
        item = self.tree.GetSelection()
        if item.IsOk():
            self._status(f"Category {self.tree.GetItemText(item)}")

    def _status(self, text: str) -> None:
        self.status.SetLabel(text)


if __name__ == "__main__":
    app = wx.App()
    Probe().Show()
    app.MainLoop()
