"""Windows' `announce.Notifier`: a UIA notification event, raised on a provider of our own. Heard
in NVDA, JAWS and Narrator.

The provider is served from a window's WM_GETOBJECT (`uia_com.WindowProvider`). One from
UiaHostProviderFromHwnd alone returns S_OK and is heard by nobody: readers ignore UIA events from a
window that has no server-side provider.
"""
from native.win.uia_com import WindowProvider, oleaut32, uia

_UIA_NAME_PROPERTY = 30005
_UIA_CONTROL_TYPE_PROPERTY = 30003
_UIA_TEXT_CONTROL_TYPE = 50020

_NOTIFICATION_KIND_OTHER = 4
# NotificationProcessing. ALL queues behind what is being read, and Tab cuts it off in Narrator,
# where IMPORTANT_ALL would not let it. MOST_RECENT is for short lines a newer one should replace.
_ALL = 2
_MOST_RECENT = 3


class Notifier(WindowProvider):
    """A Text element on the window, named `name`, and the notifications raised on it."""

    def __init__(self, window, name):
        super().__init__(window)
        self.name = name

    def _property(self, property_id):
        if property_id == _UIA_NAME_PROPERTY:
            return self.name
        if property_id == _UIA_CONTROL_TYPE_PROPERTY:
            return _UIA_TEXT_CONTROL_TYPE
        return None

    def say(self, text, recent=False):
        if self.closed or not text:
            return None
        display = oleaut32.SysAllocString(text)
        # The activity id names this app, so a reader that sorts notifications can tell its lines
        # from another program's.
        act = oleaut32.SysAllocString("accesscaster")
        try:
            return uia.UiaRaiseNotificationEvent(
                self.ptr, _NOTIFICATION_KIND_OTHER, _MOST_RECENT if recent else _ALL,
                display, act)
        finally:
            oleaut32.SysFreeString(display)
            oleaut32.SysFreeString(act)
