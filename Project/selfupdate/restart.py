"""
restart.py

Asking Windows to start AccessCaster again after the installer has closed it.

ACCESSCASTER DOES NOT EXIT ITSELF TO INSTALL. The installer's Restart Manager
asks the running app to close (`tray.py` answers WM_QUERYENDSESSION and
WM_ENDSESSION for it), and /RESTARTAPPLICATIONS brings back a process that
registered here. Exiting first would leave Restart Manager nothing to restart,
so AccessCaster would install and never come back.

REGISTERED ONLY WHEN AN INSTALL IS ABOUT TO START, never at launch. The same
registration also offers to relaunch after a crash, a hang or a reboot, none
of which is this feature's business: the flags below switch all three off.

THE RESTARTED APP OPENS ITS WINDOW. The update was started from the window,
which closed with the app, and App settings there reads what the update did.
"""

import ctypes
import subprocess
import sys

from .checker import log

# Restart for an installer, and for nothing else:
# RESTART_NO_CRASH | RESTART_NO_HANG | RESTART_NO_REBOOT.
_FLAGS = 1 | 2 | 8

# What a restart after our own installer is started with. The app treats it as
# any launch a person makes, and opens its window.
UPDATED_ARG = "--updated"


def register():
    """Register this process to be started again with UPDATED_ARG once an
    installer has closed it. True if Windows accepted it.

    Refused outside a frozen build, where sys.executable is the Python
    interpreter and relaunching it with this argument would start nothing
    recognisable."""
    if not getattr(sys, "frozen", False):
        log("not a frozen build, so no restart was registered")
        return False
    command = subprocess.list2cmdline([UPDATED_ARG])
    try:
        result = ctypes.windll.kernel32.RegisterApplicationRestart(
            ctypes.c_wchar_p(command), ctypes.c_uint(_FLAGS))
    except (AttributeError, OSError) as exc:
        log(f"could not register for restart: {type(exc).__name__}: {exc}")
        return False
    if result != 0:  # S_OK
        log(f"could not register for restart: HRESULT {result & 0xFFFFFFFF:#010x}")
        return False
    log(f"registered to restart as: {command}")
    return True


def unregister():
    """Drop the registration, for when the installer could not be started
    after all. Best effort: a stale registration only matters if something
    else closes this process through Restart Manager."""
    try:
        ctypes.windll.kernel32.UnregisterApplicationRestart()
    except (AttributeError, OSError):
        pass
