"""Stand-ins for the main window, the desk state and the job dialogs, shared by the offline native
suites (`test_native_*.py`).

    from _native_fakes import FakeFrame, FakeJob, first_focusable, text_dialog

A suite subclasses `FakeFrame` or `FakeDesk` for whatever else its section asks of them.
"""
from __future__ import annotations

import wx


class FakeDesk:
    """The desk state as a section sees it: the listener it subscribes and the names it watches."""

    def __init__(self):
        self.listener = None
        self.watched: list[tuple[set, object]] = []

    def subscribe(self, fn):
        self.listener = fn

    def watch(self, names, fn):
        self.watched.append((set(names), fn))


class FakeFrame:
    """The main window: a hidden frame holding the tree and the host panel a section builds into,
    with every sentence the section speaks collected in `said` and each `pages_changed` counted in
    `changed`."""

    def __init__(self, desk: FakeDesk | None = None):
        self.window = wx.Frame(None)
        wx.StaticText(self.window, label="Categories")    # as MainFrame names its tree
        self.tree = wx.TreeCtrl(self.window)
        self.host = wx.Panel(self.window)
        self.host.SetSizer(wx.BoxSizer(wx.VERTICAL))
        self.desk = desk if desk is not None else FakeDesk()
        self.said: list[str] = []
        self.changed = 0

    def pages_changed(self, _section):
        self.changed += 1

    def say(self, text, recent=False):
        self.said.append(text)

    def reply(self, source, reply, recent=False):
        self.said.append(reply.get("message"))


class FakeJob:
    """`JobDialog`: runs `begin` at once and returns whatever the job finished with."""

    def __init__(self, parent, title, text, begin, stop=None, closable=True, **_options):
        self.title, self.begin, self.stop, self.closable = title, begin, stop, closable
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def run(self):
        self.begin(self)
        return self.result

    def finish(self, result):
        self.result = result


def text_dialog(shown: list) -> type:
    """A `TextDialog` that records each (title, text) it opens with in `shown` and answers OK."""

    class FakeResult:
        def __init__(self, _parent, title, text, *_args, **_options):
            shown.append((title, text))

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def ShowModal(self):
            return wx.ID_OK

    return FakeResult


def first_focusable(window):
    """The control Tab reaches first in `window`: where a dialog's focus lands when it opens."""
    for child in window.GetChildren():
        if isinstance(child, (wx.Panel, wx.StaticBox)):
            found = first_focusable(child)
            if found is not None:
                return found
        elif child.AcceptsFocus() and child.IsShown() and child.IsEnabled():
            return child
    return None
