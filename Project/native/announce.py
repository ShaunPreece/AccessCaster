"""Speech through the user's screen reader, raised from a window of ours.

`Notifier(window, name)` raises its notifications from `window`, a never-shown control the caller
keeps for the purpose, and `name` is what a reader calls the source. `say(text, recent=False)`
speaks a line, queued behind what is being read, or with `recent` replacing an older line not yet
spoken. `close()` must run before `window` is destroyed.

Each platform supplies its own Notifier. There is none outside Windows, so on any other platform
this import fails rather than leaving the window silent.
"""
import sys

if sys.platform == "win32":
    from native.win.uia_notify import Notifier
else:
    raise ImportError(f"No screen reader notifications for {sys.platform}: the Windows Notifier is "
                      "native/win/uia_notify.py")

__all__ = ["Notifier"]
