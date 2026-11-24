"""Which controls take their name from the static text created just before them, under the same
parent: Win32's rule, which Windows applies itself and `a11y.follow_labels` applies for VoiceOver
on a Mac. `DevTools/_a11y.py` checks every window keeps to it.

A control in either list is whole to a screen reader, so what it is built from inside (a Mac's
number box is a text box and arrows that wx puts together) is never named separately.
"""
import wx
import wx.dataview as dv

from native.slider import DrawnSlider, SystemSlider

NAMED_BY_LABEL = (wx.TextCtrl, wx.Choice, wx.ComboBox, wx.ListBox, DrawnSlider, SystemSlider,
                  wx.SpinCtrl, wx.SpinCtrlDouble, wx.ListCtrl, wx.TreeCtrl, dv.DataViewCtrl)
SELF_NAMED = (wx.Button, wx.CheckBox, wx.RadioButton, wx.ToggleButton)


def text_of(label: str) -> str:
    return label.replace("&", "").strip()


def label_before(window) -> str:
    """The static text just before `window` under its parent, or ""."""
    parent = window.GetParent()
    if parent is None:
        return ""
    kids = list(parent.GetChildren())
    index = next((i for i, kid in enumerate(kids) if kid is window), 0)
    before = kids[index - 1] if index else None
    return text_of(before.GetLabel()) if isinstance(before, wx.StaticText) else ""
