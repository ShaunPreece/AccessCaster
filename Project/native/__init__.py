"""The native Windows window: a tree of sections on the left, the selected page on the right.
Hosted by `tray.py`.

Importing the package declares the process DPI aware (`native.display`), so a window built from it
is in real pixels whether the app or a test builds it.
"""
from native import display

display.declare_aware()
