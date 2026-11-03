"""The app's own window, as code outside the window asks for it (the Open AccessCaster shortcut).

`tray.py` hosts the window and installs `show` here; a DevTools run has no window, and `show`
stays None.
"""

# show() -> bool, callable from any thread: shows the window, creating it if need be, and returns
# whether Windows made it the foreground window.
show = None
