"""The native window's accessibility rules, checked on the windows an offline suite has built hidden.

    import _a11y
    _a11y.check_windows("Device")        # every top-level window still open, before `finish`
    _a11y.check_tree(dialog, "Add a shortcut")

A suite builds its section with real data and shows nothing; these walk every control under it,
hidden pages and tabs included, and record one check per window. The rules are the ones the window
is built to (`native.pages`, and PROJECT_GUIDE part 2, Native window):

  * A control a screen reader names from the text before it (a text box, list, slider, number box)
    has a non-empty static label as its previous sibling: Windows takes the name from the static
    text created just before it, which is what `pages.labelled` does.
  * A button, checkbox or radio button has a label of its own, and a group box a title.
  * A read-only text box is multi-line: wx never makes a single-line one a tab stop, so Tab passes a
    reading by.

What it cannot see is everything a screen reader adds: what is actually spoken, and when. That is
for a pass by hand.
"""
from __future__ import annotations

import wx
from _testkit import check

NAMED_BY_LABEL = (wx.TextCtrl, wx.Choice, wx.ComboBox, wx.ListBox, wx.Slider, wx.SpinCtrl,
                  wx.SpinCtrlDouble, wx.ListCtrl, wx.TreeCtrl)
SELF_NAMED = (wx.Button, wx.CheckBox, wx.RadioButton, wx.ToggleButton)


def _text(label: str) -> str:
    return label.replace("&", "").strip()


def _name(window) -> str:
    """Enough to find the control: its class and whatever text it carries."""
    text = _text(window.GetLabel()) if not isinstance(window, wx.TextCtrl) else ""
    return f"{type(window).__name__}" + (f" '{text}'" if text else "")


def problems(window) -> list:
    """Every rule broken under `window`, one line each."""
    found = []
    for parent in _walk(window):
        kids = list(parent.GetChildren())
        for i, control in enumerate(kids):
            if isinstance(control, SELF_NAMED):
                if not _text(control.GetLabel()):
                    found.append(f"{_name(control)} under {_name(parent)} has no label")
            elif isinstance(control, NAMED_BY_LABEL):
                before = kids[i - 1] if i else None
                if not (isinstance(before, wx.StaticText) and _text(before.GetLabel())):
                    found.append(f"{_name(control)} under {_name(parent)} has no label before it"
                                 f" (it follows {_name(before) if before else 'nothing'})")
            elif isinstance(control, wx.StaticBox) and not _text(control.GetLabel()):
                found.append(f"a group box under {_name(parent)} has no title")
            if (isinstance(control, wx.TextCtrl) and control.HasFlag(wx.TE_READONLY)
                    and not control.IsMultiLine()):
                found.append(f"the read-only box after '{_text(kids[i - 1].GetLabel()) if i else ''}'"
                             f" is single-line, so Tab never reaches it")
    return found


def _walk(window):
    yield window
    for child in window.GetChildren():
        yield from _walk(child)


def check_tree(window, where: str) -> bool:
    return check(f"{where}: every control is named and reachable", problems(window), [])


def check_windows(where: str) -> bool:
    """Every top-level window still open."""
    found = [line for top in wx.GetTopLevelWindows() for line in problems(top)]
    return check(f"{where}: every control is named and reachable", found, [])
