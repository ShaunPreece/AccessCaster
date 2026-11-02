"""What a native control says beyond its label: a description read on focus (the native
counterpart of aria-describedby), and a value in words for a slider, which on its own reads a bare
number.

`describe(window, description="", value=None)` gives `window` a description and, optionally,
`value`: a callable returning the value in words, asked each time a screen reader reads it. It
returns an object whose `description` can be changed later. Name, role and state are left to the
control, so it still reads as the native checkbox or slider it is.

Each platform supplies its own describe. There is none outside Windows, so on any other platform
this import fails rather than leaving every description unread.
"""
import sys

if sys.platform == "win32":
    from native.win.accessible import describe
else:
    raise ImportError(f"No control descriptions for {sys.platform}: the Windows describe is "
                      "native/win/accessible.py")

__all__ = ["describe"]
