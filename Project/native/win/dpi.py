"""Declares the process per-monitor DPI aware, so Windows and every screen reader are given
positions in real pixels.

Undeclared, Windows stretches the window on a scaled display and reports positions in scaled units,
and JAWS scales a custom control's position twice. JAWS reads a control its scripts cannot confirm
(`native.slider.SliderCtrl`) at that position, and reads nothing once the doubled position is off
the screen, so a slider far enough right or down is silent as it is arrowed.
"""
import ctypes
from ctypes import wintypes

_PER_MONITOR_AWARE_V2 = -4


def declare_aware():
    """Runs before the process creates its first window: Windows refuses the call after that, and
    the process stays as it was."""
    user32 = ctypes.WinDLL("user32")
    user32.SetProcessDpiAwarenessContext.argtypes = [wintypes.HANDLE]
    user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
    user32.SetProcessDpiAwarenessContext(_PER_MONITOR_AWARE_V2)
