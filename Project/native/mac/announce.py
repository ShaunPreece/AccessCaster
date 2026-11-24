"""macOS's `announce.Notifier`: an NSAccessibility announcement request, which VoiceOver speaks.

The announcement is posted on the application, which VoiceOver hears whichever of its windows is
in front; `window` is not needed. A line waits behind what VoiceOver is reading (medium priority);
a `recent` one interrupts it (high), which is the nearest the system comes to replacing an older
line. It is posted from the main thread, as AppKit requires, whichever thread says it.
"""
from AppKit import (NSAccessibilityAnnouncementKey, NSAccessibilityAnnouncementRequestedNotification,
                    NSAccessibilityPostNotificationWithUserInfo, NSAccessibilityPriorityHigh,
                    NSAccessibilityPriorityKey, NSAccessibilityPriorityMedium, NSApplication)

from mac.cocoa import on_main


class Notifier:
    def __init__(self, window, name):
        self.name = name
        self.closed = False

    def say(self, text, recent=False):
        if self.closed or not text:
            return None
        priority = NSAccessibilityPriorityHigh if recent else NSAccessibilityPriorityMedium
        on_main(lambda: NSAccessibilityPostNotificationWithUserInfo(
            NSApplication.sharedApplication(), NSAccessibilityAnnouncementRequestedNotification,
            {NSAccessibilityAnnouncementKey: text, NSAccessibilityPriorityKey: priority}), 0)
        return None

    def close(self):
        self.closed = True
