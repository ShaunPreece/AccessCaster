"""Start AccessCaster when this user signs in: the same entry the installer writes, readable and
writable from inside the app.

WHY IT IS A SWITCH IN THE APP AT ALL. The installer offers it once, on a screen somebody is trying
to get past, and missing it leaves no way back short of running Setup again. It is a per-user
entry, so the app can own it with no admin rights and no elevation prompt. Autostart is a choice
each account makes for itself, which is why the wording says "when YOU sign in".

THE `--startup` FLAG IS THE WHOLE DIFFERENCE BETWEEN THIS AND A NORMAL LAUNCH. Started by hand you
want the window; started at login you want the tray, the announcer and the global shortcuts, and
no window taking the focus you were using for something else. `tray.main` reads it.

The platform supplies the entry itself:
  - `command_for(exe, flag)`: what the entry holds when it starts `exe` with `flag`, as text.
  - `current()`: what the entry holds now, or None when there is none. Never raises.
  - `install(command)`: write the entry. Raises on failure.
  - `remove()`: delete it; absent is not a failure. Raises on any other failure.
"""
from __future__ import annotations

import os
import sys

from wording import TRAY

if sys.platform == "win32":
    from win.autostart import command_for, current, install, remove
elif sys.platform == "darwin":
    from mac.autostart import command_for, current, install, remove
else:
    raise ImportError(f"No sign-in entry for {sys.platform}: the Windows one is "
                      "win/autostart.py")

STARTUP_FLAG = "--startup"


def started_at_login(argv=None) -> bool:
    """Was this process launched by the sign-in entry rather than by a person?"""
    return STARTUP_FLAG in (sys.argv[1:] if argv is None else argv)


def app_exe() -> str | None:
    """The installed executable, or None when we are not running as one.

    A dev run's `sys.executable` is the Python interpreter and the app is a script argument, so
    writing that would produce an entry that breaks the moment the repo moves, SILENTLY, at a
    login. Refusing is the honest answer.
    """
    if not getattr(sys, "frozen", False):
        return None
    exe = os.path.abspath(sys.executable)
    return exe if os.path.isfile(exe) else None


def state() -> dict:
    """{enabled, supported, value, message} for the UI.

    `supported` False means the switch must refuse rather than write, because this is not the
    installed app. The message says so, because "it did not work" with no reason is the failure a
    blind user cannot investigate.

    An entry that names a DIFFERENT command is reported as enabled with a message saying so, never
    silently corrected. It usually means another copy of the app is set to start, a second install
    or one that moved, and quietly rewriting somebody's login behaviour to point at us is not ours
    to do without saying it.
    """
    exe = app_exe()
    value = current()
    if exe is None:
        return {"enabled": value is not None, "supported": False, "value": value,
                "message": ("This copy is running from source rather than from an installed "
                            "AccessCaster, so it cannot set itself to start when you sign in. The "
                            "installed app can." if value is None else
                            "Something is set to start when you sign in, but this copy is running "
                            "from source and cannot change it.")}
    want = command_for(exe, STARTUP_FLAG)
    if value is None:
        return {"enabled": False, "supported": True, "value": None,
                "message": "AccessCaster does not start when you sign in."}
    if value.strip().lower() != want.strip().lower():
        return {"enabled": True, "supported": True, "value": value,
                "message": (f"AccessCaster starts when you sign in, but from a different copy: "
                            f"{value}. Switching this off and on again points it at this one.")}
    return {"enabled": True, "supported": True, "value": value,
            "message": f"AccessCaster starts when you sign in, in {TRAY}."}


def set_enabled(on: bool) -> dict:
    """Turn autostart on or off for THIS user. {ok, enabled, message}."""
    exe = app_exe()
    if on and exe is None:
        return {"ok": False, "enabled": current() is not None,
                "message": ("This copy is running from source rather than from an installed "
                            "AccessCaster. Only the installed app can set itself to start when you sign "
                            "in, because the startup entry has to name a program that will still be "
                            "there.")}
    try:
        if on:
            install(command_for(exe, STARTUP_FLAG))
        else:
            remove()   # already off; asking for what you have is not a failure
    except Exception as exc:
        return {"ok": False, "enabled": current() is not None,
                "message": f"Could not change the startup setting: {exc}"}
    return {"ok": True, "enabled": bool(on),
            "message": (f"AccessCaster will start when you sign in, in {TRAY}. It does not open "
                        f"its window: use its icon in {TRAY}, or your shortcut keys, which work as "
                        "soon as it is running." if on else
                        "AccessCaster will no longer start when you sign in.")}
