"""Windows' `install_handoff`: Setup run silently, and Restart Manager bringing the app back.

ACCESSCASTER DOES NOT EXIT ITSELF TO INSTALL. Setup's Restart Manager asks the running app to close
(`tray.py` answers WM_QUERYENDSESSION and WM_ENDSESSION for it), and /RESTARTAPPLICATIONS brings
back a process registered with `RegisterApplicationRestart`. Exiting first would leave Restart
Manager nothing to restart, so AccessCaster would install and never come back.

REGISTERED ONLY WHEN AN INSTALL IS ABOUT TO START, never at launch. The same registration also
offers to relaunch after a crash, a hang or a reboot, none of which is this feature's business: the
flags below switch all three off.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys

from selfupdate.checker import log

# /VERYSILENT rather than /SILENT: under /SILENT Setup's progress window takes focus on its Cancel
# button, JAWS reads nothing from its bar, and a Space pressed into the wait cancels the install.
# /SUPPRESSMSGBOXES is deliberately NOT given: a Setup message box still shows under /VERYSILENT,
# and an error the user can read and answer beats one that is swallowed. /NORESTART because nothing
# here justifies rebooting the machine.
INSTALLER_ARGS = ["/VERYSILENT", "/RESTARTAPPLICATIONS", "/NORESTART"]

# Detached, so the installer outlives the process that started it: Restart Manager is about to
# close this one.
_DETACHED = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

# Restart for an installer, and for nothing else:
# RESTART_NO_CRASH | RESTART_NO_HANG | RESTART_NO_REBOOT.
_RESTART_FLAGS = 1 | 2 | 8

# What a restart after our own installer is started with. The app treats it as any launch a person
# makes, and opens its window.
UPDATED_ARG = "--updated"


def start(installer: str) -> None:
    register_restart()
    try:
        subprocess.Popen([installer] + INSTALLER_ARGS, cwd=os.path.dirname(installer),
                         creationflags=_DETACHED, close_fds=True)
    except OSError:
        unregister_restart()
        raise


def register_restart() -> bool:
    """True if Windows accepted it. Refused outside a frozen build, where sys.executable is the
    Python interpreter and relaunching it with this argument would start nothing recognisable."""
    if not getattr(sys, "frozen", False):
        log("not a frozen build, so no restart was registered")
        return False
    command = subprocess.list2cmdline([UPDATED_ARG])
    try:
        result = ctypes.windll.kernel32.RegisterApplicationRestart(
            ctypes.c_wchar_p(command), ctypes.c_uint(_RESTART_FLAGS))
    except (AttributeError, OSError) as exc:
        log(f"could not register for restart: {type(exc).__name__}: {exc}")
        return False
    if result != 0:  # S_OK
        log(f"could not register for restart: HRESULT {result & 0xFFFFFFFF:#010x}")
        return False
    log(f"registered to restart as: {command}")
    return True


def unregister_restart() -> None:
    """Best effort: a stale registration only matters if something else closes this process
    through Restart Manager."""
    try:
        ctypes.windll.kernel32.UnregisterApplicationRestart()
    except (AttributeError, OSError):
        pass
