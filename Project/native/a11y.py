"""What a control says to a screen reader beyond what it says on its own.

`describe(window, description="", value=None)` gives a native control a description read on focus
(the native counterpart of aria-describedby) and, optionally, `value`: a callable returning the
value in words, asked each time a screen reader reads it. Name, role and state are left to the
control, so it still reads as the native checkbox or button it is. It returns an object whose
`description` can be changed later.

`slider(window, value, description="")` is the accessibility of `native.slider.SliderCtrl`: a
slider named by the window's label, with `value()` as its value. It returns an object with a
changeable `description` and `changed()`, which tells screen readers the value has changed.

`follow_labels(app)` makes every control named by the static text just before it
(`native.labels`) carry that name for the screen reader, in every window from then on;
`relabel(window, text)` names one again after its label changes.

Each platform supplies its own; on any other platform this import fails rather than leaving every
description unread.
"""
import sys

if sys.platform == "win32":
    from native.win.accessible import describe, follow_labels, relabel
    from native.win.uia_slider import slider
elif sys.platform == "darwin":
    from native.mac.a11y import describe, follow_labels, relabel, slider
else:
    raise ImportError(f"No control descriptions for {sys.platform}: the Windows ones are in "
                      "native/win/accessible.py and native/win/uia_slider.py")

__all__ = ["describe", "follow_labels", "relabel", "slider"]
