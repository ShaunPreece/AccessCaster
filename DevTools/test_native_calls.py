"""The native window's desk calls (Project/native/calls.py), offline: a call that raises is a
refusal and a diagnostic log line, and in the write pacing (LatestWins) taps each send, a held
arrow key sends its newest value every `max_wait_ms` instead of nothing until it is let go, and
without `max_wait_ms` (a list) a held key sends once, at the end. Runs a real event loop for a
second or two; nothing is shown and nothing touches a desk.

Run: python DevTools/test_native_calls.py
"""
from __future__ import annotations

import sys

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
import diagnostics
import wx
from _testkit import check, check_true, patched
from native import calls

app = wx.App()
frame = wx.Frame(None)


def held(max_wait_ms, presses=40, every_ms=30):
    """Change a writer `presses` times, `every_ms` apart, as a held arrow key repeats; return what
    it sent."""
    sent = []
    with patched((calls, {"call": lambda _owner, fn, *a, done=None: done(fn(*a))})):
        writer = calls.LatestWins(frame, lambda v: sent.append(v) or {"ok": True},
                                  lambda _v, _r: None, max_wait_ms=max_wait_ms)
        value = [0]

        def press():
            value[0] += 1
            writer.change(value[0])
            if value[0] < presses:
                wx.CallLater(every_ms, press)
            else:
                wx.CallLater(500, app.ExitMainLoop)
        wx.CallLater(10, press)
        app.MainLoop()
    return sent


sent = held(max_wait_ms=300)
check_true("a held key sends its newest value as it goes, not only at the end",
           len(sent) >= 3 and sent[-1] == 40 and sent == sorted(sent))
check("a held key with no maximum wait sends once, when it is let go", held(None), [40])
check("taps further apart than the delay each send", held(None, presses=3, every_ms=250),
      [1, 2, 3])

events, replies = [], []


def broken():
    raise KeyError("backups")


with patched((diagnostics, {"event": lambda kind, detail="", **f: events.append((kind, f))})):
    calls.call(frame, broken, done=lambda reply: (replies.append(reply), app.ExitMainLoop()))
    wx.CallLater(3000, app.ExitMainLoop)
    app.MainLoop()
check("a call that raises is a refusal that does not blame the desk",
      [r.get("message") for r in replies], ["That did not work: 'backups'"])
check("and leaves a line in the diagnostic log", [k for k, _f in events], ["window.call_failed"])
check_true("saying where it was raised", "broken" in (events[0][1].get("at", "") if events else ""))

frame.Destroy()
sys.exit(_testkit.finish())
