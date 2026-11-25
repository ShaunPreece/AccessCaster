"""macOS's `a11y`, through the NSAccessibility properties of the control's own view. wx implements
wx.Accessible on Windows only.

A description is the view's accessibility help, which VoiceOver reads as the control's hint after
a pause, the place WebKit puts aria-describedby too. A value in words is its value description,
which VoiceOver reads in place of the number. Nothing else is set, so the control still reads as
the native checkbox, list or slider it is.

A value description does not ask the app each time it is read, so `describe`'s `value` is read
again whenever the control takes the focus, and a slider's whenever it moves (`changed`). A slider
then posts a value change, so VoiceOver reads the new value at once rather than when it next
looks.

VoiceOver does not name a control from the text before it, as Windows does, so `follow_labels`
gives each control that label (`native.labels`) whenever a window or page is shown. A show reaches
the app's handler when the window's own handlers let it pass, as they all do. `relabel` names one
control again after its label changes.
"""
import wx
from AppKit import NSAccessibilityPostNotification, NSAccessibilityValueChangedNotification

from mac.cocoa import focus_view


class _Accessible:
    def __init__(self, window, description, value, moves=False):
        self._view = focus_view(window.GetHandle())
        self._value = value
        self._moves = moves
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
        self._read_value()
        if self._moves:
            NSAccessibilityPostNotification(self._view, NSAccessibilityValueChangedNotification)

    def _read_value(self):
        if self._value is not None:
            self._view.setAccessibilityValueDescription_(self._value() or None)

    def _focused(self, event):
        self._read_value()
        event.Skip()


def describe(window, description="", value=None):
    return _Accessible(window, description, value)


def slider(window, words, description=""):
    return _Accessible(window, description, words, moves=True)


def follow_labels(app):
    app.Bind(wx.EVT_SHOW, _shown)


def relabel(window, text):
    _name(window, text)


def _shown(event):
    event.Skip()
    window = event.GetEventObject()
    if event.IsShown() and isinstance(window, wx.Window):
        _name_inside(window)


def _name_inside(window):
    from native import labels
    for child in window.GetChildren():
        if isinstance(child, labels.NAMED_BY_LABEL):
            text = labels.label_before(child)
            if text:
                _name(child, text)
        elif not isinstance(child, labels.SELF_NAMED):
            _name_inside(child)


def _name(window, text):
    """The control, and whatever it is built from that takes the focus."""
    focus_view(window.GetHandle()).setAccessibilityLabel_(text)
    for part in window.GetChildren():
        if part.AcceptsFocus():
            focus_view(part.GetHandle()).setAccessibilityLabel_(text)
