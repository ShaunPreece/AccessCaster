"""macOS's `app_window.raise_window`: un-minimise the window, make it key and activate the app,
which moves VoiceOver to it. An app that is not in front is only activated when it asks; the
system can refuse, and then the app's Dock icon bounces instead.

ACTIVATION IS ASYNCHRONOUS. `activate` is a request the window server answers through the main
thread's event loop, so straight after it the app still reads as inactive, and the main thread,
where every raise runs, cannot wait for the answer without blocking it. So the result is whether
the window is in front and activation was asked for, and the settled outcome is logged as
`window.raise` once the answer has had time to arrive.
"""
import diagnostics
from AppKit import NSApplication
from PyObjCTools import AppHelper
from mac.cocoa import ns_window, on_main

# How long the window server is given to answer an activation before the outcome is logged.
_SETTLE_S = 0.5


def _settled(window) -> None:
    app = NSApplication.sharedApplication()
    raised = bool(app.isActive() and window.isKeyWindow())
    diagnostics.event("window.raise", "raised" if raised else "not raised")


def raise_window(handle: int) -> bool:
    def bring():
        window = ns_window(handle)
        if window.isMiniaturized():
            window.deminiaturize_(None)
        window.makeKeyAndOrderFront_(None)
        app = NSApplication.sharedApplication()
        if app.respondsToSelector_("activate"):
            app.activate()                         # macOS 14 and later
        else:
            app.activateIgnoringOtherApps_(True)
        AppHelper.callLater(_SETTLE_S, _settled, window)
        return bool(window.isVisible())

    return bool(on_main(bring))
