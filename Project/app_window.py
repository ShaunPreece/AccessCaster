"""The app's own window, as code outside the window asks for it (the Open AccessCaster shortcut).

`tray.py` hosts the window and installs `show` here. A dev server run has no window, so `show`
stays None and callers fall back to the web page.
"""

# show() -> bool, callable from any thread: shows the window, creating it if need be, and returns
# whether Windows made it the foreground window.
show = None
