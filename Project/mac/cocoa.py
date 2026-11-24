"""What the Mac's halves share for calling AppKit through PyObjC.

  - `ns_object(handle)`: the Objective-C object behind a wx handle. A wx control's `GetHandle()`
    is its NSView, and a frame's is its content view.
  - `ns_window(handle)`: the NSWindow holding that view, or the window itself.
  - `on_main(call, timeout)`: run `call` on the main thread and return its result, waiting at
    most `timeout` seconds, after which it returns None and the call still runs. AppKit may only
    be touched from the main thread, which is wx's.
"""
from __future__ import annotations

import threading

import objc
from Foundation import NSThread
from PyObjCTools import AppHelper


def ns_object(handle: int):
    return objc.objc_object(c_void_p=handle)


def ns_window(handle: int):
    target = ns_object(handle)
    return target.window() if target.respondsToSelector_("window") else target


def on_main(call, timeout: float = 2.0):
    if NSThread.isMainThread():
        return call()
    done = threading.Event()
    result = []

    def run():
        try:
            result.append(call())
        finally:
            done.set()

    AppHelper.callAfter(run)
    done.wait(timeout)
    return result[0] if result else None
