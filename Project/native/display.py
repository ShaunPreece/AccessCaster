"""How the process presents itself to the display.

`declare_aware()` tells the platform the app lays its windows out in real pixels. It runs when the
package is imported, before any window exists, so a size is given through `pages.dip`.

Each platform supplies its own. Only Windows needs one: elsewhere the toolkit works in scaled units
throughout.
"""
import sys

if sys.platform == "win32":
    from native.win.dpi import declare_aware
else:
    def declare_aware():
        pass

__all__ = ["declare_aware"]
