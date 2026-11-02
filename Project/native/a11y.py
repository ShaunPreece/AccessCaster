"""What a native control says beyond its label: a description read on focus (the native
counterpart of aria-describedby), and a value in words for a slider, which on its own reads a bare
number.

Only these two answers are overridden. Everything else (name, role, state) is left unimplemented,
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
    """Give `window` a description and, optionally, `value`: a callable returning the value in
    words, asked each time a screen reader reads it. Returns the object, whose `description` can be
    changed later."""
    acc = _Accessible(window, description, value)
    window.SetAccessible(acc)
    return acc
