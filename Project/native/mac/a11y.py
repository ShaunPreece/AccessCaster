"""macOS's `a11y`, through the NSAccessibility properties of the control's own view. wx implements
wx.Accessible on Windows only.

A description is the view's accessibility help, which VoiceOver reads as the control's hint after
a pause, the place WebKit puts aria-describedby too. A value in words is its value description,
which VoiceOver reads in place of the number. Nothing else is set, so the control still reads as
the native checkbox, list or slider it is.

A value description does not ask the app each time it is read, so `describe`'s `value` is read
again whenever the control takes the focus, and a slider's whenever it moves (`changed`).
"""
import wx

from mac.cocoa import ns_object


class _Accessible:
    def __init__(self, window, description, value):
        self._view = ns_object(window.GetHandle())
        self._value = value
        self.description = description
        # Not read here: a slider's words read the control, which is still being built.
        if value is not None:
            window.Bind(wx.EVT_SET_FOCUS, self._focused)

    @property
    def description(self):
        return self._description

    @description.setter
    def description(self, text):
        self._description = text
        self._view.setAccessibilityHelp_(text or None)

    def changed(self):
        if self._value is not None:
            self._view.setAccessibilityValueDescription_(self._value() or None)

    def _focused(self, event):
        self.changed()
        event.Skip()


def describe(window, description="", value=None):
    return _Accessible(window, description, value)


def slider(window, words, description=""):
    return _Accessible(window, description, words)
