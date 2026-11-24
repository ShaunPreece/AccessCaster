"""One copy of AccessCaster per computer, across every signed-in account.

There is one desk on the USB, and the announcer, the global shortcuts and the tray icon belong to
the session that started them, so a second account's copy is refused rather than shared.

  - `claim()`: hold the lock for the rest of this process's life. True when this process holds it,
    False when another process does, whichever account started it. The system releases it when
    the process ends, however it ends, so a crash leaves no stale lock behind.

Each platform supplies its own.
"""
import sys

if sys.platform == "win32":
    from win.instance import claim
elif sys.platform == "darwin":
    from mac.instance import claim
else:
    raise ImportError(f"No single-instance lock for {sys.platform}: the Windows one is "
                      "win/instance.py")

__all__ = ["claim"]
