"""macOS's `app_window.raise_window`: un-minimise the window, make it key and activate the app,
which moves VoiceOver to it. An app that is not in front is only activated when it asks; the
system can refuse, and then the app's Dock icon bounces instead."""
import diagnostics
from AppKit import NSApplication
from mac.cocoa import ns_window, on_main


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
        return bool(app.isActive() or window.isKeyWindow())

    raised = bool(on_main(bring))
    diagnostics.event("window.raise", "raised" if raised else "not raised")
    return raised
