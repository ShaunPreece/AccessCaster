"""A STAND-IN for macOS's `a11y`: VoiceOver hears each control as wx makes it, with none of the
descriptions, and the app's own slider is not exposed as a slider. wx implements wx.Accessible on
Windows only, so the Mac's needs NSAccessibility on the control's view."""


class _Description:
    def __init__(self, description=""):
        self.description = description

    def changed(self):
        pass


def describe(window, description="", value=None):
    return _Description(description)


def slider(window, words, description=""):
    return _Description(description)
