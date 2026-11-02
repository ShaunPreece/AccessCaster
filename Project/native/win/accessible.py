"""Windows' `a11y.describe`: a wx.Accessible, which wx serves to screen readers as MSAA. wx
implements wx.Accessible on Windows only.

Only the description and the value are overridden. Everything else (name, role, state) is left unimplemented,
which makes wx fall through to the control's standard accessibility, so the control still reads as
the native checkbox or slider it is.
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
