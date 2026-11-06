"""Desk calls from the window. Each function runs on a worker thread and its reply comes back on
the UI thread, so a desk read or write never freezes the window. The actions take the HID lock
themselves, so calls from here queue behind the announcer, the shortcuts and each other.
"""
import os
import traceback
from concurrent.futures import ThreadPoolExecutor

import wx

import diagnostics

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="native-call")


def call(owner, fn, *args, done=None):
    """Run `fn(*args)` off the UI thread, then `done(reply)` on it, unless `owner` (a window) has
    been destroyed by then. An exception becomes a refusal reply, like every action's, and a line
    in the diagnostic log saying where it was raised, which is all a user can send about a button
    that failed."""
    def work():
        try:
            reply = fn(*args)
        except Exception as exc:
            where = " < ".join(f"{os.path.basename(f.filename)}:{f.lineno} {f.name}"
                               for f in reversed(traceback.extract_tb(exc.__traceback__)))
            diagnostics.event("window.call_failed", f"{type(exc).__name__}: {exc}", at=where)
            reply = {"ok": False, "message": f"That did not work: {exc}"}
        if done is not None:
            wx.CallAfter(_deliver, owner, done, reply)
    _pool.submit(work)


def _deliver(owner, done, reply):
    if owner:
        done(reply)


class LatestWins:
    """One control's writes when it can change faster than the desk takes them (a slider being
    arrowed). Waits `delay_ms` after the last change, sends only the newest value, and while a write
    is in flight holds the newest value back and sends it when that write returns. With
    `max_wait_ms`, a control that keeps changing still sends its newest value that often: a held
    arrow key repeats faster than `delay_ms`, so without it nothing is sent until the key is let go.

    `send(value)` runs on a worker thread; `done(value, reply)` on the UI thread after every write.
    `settled(value, reply)` runs once the control has been still for `settle_ms` and the last write
    has returned: the moment to speak a reply, which the screen reader would otherwise read over
    the value it is still announcing on every arrow press."""

    def __init__(self, owner, send, done, settled=None, delay_ms=150, settle_ms=1500,
                 max_wait_ms=None):
        self._owner = owner
        self._send = send
        self._done = done
        self._settled = settled
        self._delay_ms = delay_ms
        self._settle_ms = settle_ms
        self._max_wait_ms = max_wait_ms
        self._timer = None
        self._max_timer = None
        self._settle_timer = None
        self._pending = None
        self._in_flight = False
        self._last = None
        self._still = False

    def change(self, value):
        self._pending = value
        self._still = False
        self._timer = _restart(self._timer, self._delay_ms, self._fire)
        # Started by the first unsent change and never restarted, unlike the delay.
        if self._max_wait_ms and (self._max_timer is None or not self._max_timer.IsRunning()):
            self._max_timer = wx.CallLater(self._max_wait_ms, self._fire)
        if self._settled is not None:
            self._settle_timer = _restart(self._settle_timer, self._settle_ms, self._on_still)

    def _fire(self):
        if self._in_flight or self._pending is None:
            return
        value, self._pending = self._pending, None
        self._in_flight = True
        call(self._owner, self._send, value, done=lambda reply: self._returned(value, reply))

    def _returned(self, value, reply):
        self._in_flight = False
        self._last = (value, reply)
        self._done(value, reply)
        if self._pending is not None:
            self._fire()
        elif self._still:
            self._report()

    def _on_still(self):
        self._still = True
        if not self._in_flight and self._pending is None:
            self._report()

    def _report(self):
        self._still = False
        if self._last is not None:
            self._settled(*self._last)
            self._last = None


def _restart(timer, ms, fn):
    if timer is not None and timer.IsRunning():
        timer.Start(ms)
        return timer
    return wx.CallLater(ms, fn)
