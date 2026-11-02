"""AccessCaster in a native window, in place of the web page. It runs the same service the tray app
does (announcer, shortcuts, watchers and the web server), so a section not built natively yet still
opens in the browser. Closing the window stops the service.

Run: python Project/native_app.py
"""
import sys

import wx

from native.frame import MainFrame


def main() -> int:
    app = wx.App()
    frame = MainFrame()
    frame.Show()
    frame.tree.SetFocus()
    frame.start()
    app.MainLoop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
