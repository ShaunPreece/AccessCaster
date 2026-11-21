"""Windows' `a11y`: wx.Accessible, which wx serves to screen readers as MSAA. wx implements
wx.Accessible on Windows only.

`describe` overrides only the description and the value. Everything else (name, role, state) is
left unimplemented, which makes wx fall through to the control's standard accessibility, so the
control still reads as the native checkbox or button it is.

`slider` answers every question itself, because the control it serves is drawn by the app and has
no standard accessibility to fall through to.
"""
import wx


class _Accessible(wx.Accessible):
    def __init__(self, window, description, value):
        super().__init__(window)
        self.description = description
        self.value = value

    def GetDescription(self, childId):
        if childId == wx.ACC_SELF and self.description:
            return (wx.ACC_OK, self.description)
        return (wx.ACC_NOT_IMPLEMENTED, "")

    def GetValue(self, childId):
        if childId == wx.ACC_SELF and self.value is not None:
            return (wx.ACC_OK, self.value())
        return (wx.ACC_NOT_IMPLEMENTED, "")


def describe(window, description="", value=None):
    acc = _Accessible(window, description, value)
    window.SetAccessible(acc)
    return acc


class _SliderAccessible(_Accessible):
    """A slider named by its window's label, valued by `value()`. Every answer is given here: a
    window of the app's own class has no standard accessibility worth falling through to."""

    def GetName(self, childId):
        return (wx.ACC_OK, self.GetWindow().GetLabel())

    def GetRole(self, childId):
        return (wx.ACC_OK, wx.ROLE_SYSTEM_SLIDER)

    def GetState(self, childId):
        window = self.GetWindow()
        state = wx.ACC_STATE_SYSTEM_FOCUSABLE
        if window.HasFocus():
            state |= wx.ACC_STATE_SYSTEM_FOCUSED
        if not window.IsEnabled():
            state |= wx.ACC_STATE_SYSTEM_UNAVAILABLE
        if not window.IsShownOnScreen():
            state |= wx.ACC_STATE_SYSTEM_INVISIBLE
        return (wx.ACC_OK, state)

    def GetChildCount(self):
        return (wx.ACC_OK, 0)

    def GetLocation(self, childId):
        return (wx.ACC_OK, self.GetWindow().GetScreenRect())

    def GetFocus(self, childId):
        if self.GetWindow().HasFocus():
            return (wx.ACC_OK, wx.ACC_SELF, None)
        return (wx.ACC_FALSE, 0, None)

    def HitTest(self, pt):
        return (wx.ACC_OK, wx.ACC_SELF, None)


def slider(window, value, description=""):
    acc = _SliderAccessible(window, description, value)
    window.SetAccessible(acc)
    return acc


def value_changed(window):
    wx.Accessible.NotifyEvent(wx.ACC_EVENT_OBJECT_VALUECHANGE, window, wx.OBJID_CLIENT,
                              wx.ACC_SELF)
