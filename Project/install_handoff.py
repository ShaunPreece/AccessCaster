"""Handing a downloaded update to the system's installer, which closes the app, replaces it and
starts it again.

  - `start(installer)`: start the installer at that path, detached, so it outlives this process.
    The app does not exit itself: the installer closes it, and the restarted app opens its window,
    because the update was started from the window that closed with it. Raises OSError when the
    installer could not be started, with nothing left registered.

Each platform supplies its own.
"""
import sys

if sys.platform == "win32":
    from win.install_handoff import start
elif sys.platform == "darwin":
    from mac.install_handoff import start
else:
    raise ImportError(f"No installer hand-off for {sys.platform}: the Windows one is "
                      "win/install_handoff.py")

__all__ = ["start"]
