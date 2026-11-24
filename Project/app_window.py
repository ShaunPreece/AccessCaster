"""The app's own window, as code outside the window asks for it (the Open AccessCaster shortcut).

`tray.py` hosts the window and installs `show` here; a DevTools run has no window, and `show`
stays None.

The platform supplies `raise_window(handle)`: restore the native window `handle` if minimised and
bring it to the front with a real activation, the kind a screen reader follows, from any thread.
Returns whether the system made it the foreground window; logs the outcome as `window.raise`.
"""
import sys

if sys.platform == "win32":
    from win.app_window import raise_window
elif sys.platform == "darwin":
    from mac.app_window import raise_window
else:
    raise ImportError(f"No window raise for {sys.platform}: the Windows one is win/app_window.py")

# show() -> bool | None, callable from any thread: shows the window, creating it if need be, and
# returns whether the system made it the foreground window, or None while the app is closing, when
# there is no window to show.
show = None

__all__ = ["raise_window", "show"]
