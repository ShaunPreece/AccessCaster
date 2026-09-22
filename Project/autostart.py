"""Start AccessCaster when this user signs in: the same registry value the installer's checkbox
writes, readable and writable from inside the app.

WHY IT IS A SWITCH IN THE APP AT ALL. The installer offers it once, on a screen somebody is trying
to get past, and missing it leaves no way back short of running Setup again. It is one HKCU value,
so the
app can own it with no admin rights and no elevation prompt.

THE VALUE IS THE INSTALLER'S, NOT OURS, AND THAT IS DELIBERATE. Same key, same NAME:

    HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
      "AccessCaster" = "<install path>\\AccessCaster.exe" --startup

`installer.iss` sets it with `uninsdeletevalue`, so uninstalling removes it. Write it under any other
name and an uninstall leaves a Run entry pointing at a deleted exe - a startup error every login,
with nothing left on the machine to turn it off. `DevTools/test_autostart.py` reads `installer.iss`
and asserts the two still agree, because nothing else would notice them drifting.

HKCU AND NEVER HKLM. AccessCaster serves a FIXED port (8770), so a machine-wide Run entry would
start an instance for every account that signs in and they would fight over it. That is not
hypothetical: a machine can have somebody at the console and somebody else in an RDP session at
once. Autostart is a choice each account makes for itself, which is why the wording says
"when YOU sign in".

THE `--startup` FLAG IS THE WHOLE DIFFERENCE BETWEEN THIS AND A NORMAL LAUNCH. Started by hand you
want the page; started at login you want the tray, the announcer and the global shortcuts, and no
browser window taking the focus you were using for something else. `tray.main` reads it.
"""
from __future__ import annotations

import os
import sys

# The installer's key, name and flag. Change any of these and change installer.iss in the same edit.
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "AccessCaster"
STARTUP_FLAG = "--startup"
EXE_NAME = "AccessCaster.exe"

# THE NAME THIS VALUE HAD BEFORE the app's name became one word. Anyone
# who ticked autostart before then has a Run entry under the old name, pointing at the same exe.
# Leaving it would start the app TWICE at sign-in, so both this module and installer.iss delete it.
# Kept as a constant rather than a literal because two places have to agree on it, and it can only
# be removed once no machine can still be carrying one.
LEGACY_VALUE_NAME = "Access Caster"


def started_at_login(argv=None) -> bool:
    """Was this process launched by the Run entry rather than by a person?"""
    return STARTUP_FLAG in (sys.argv[1:] if argv is None else argv)


def app_exe() -> str | None:
    """The installed executable, or None when we are not running as one.

    A dev run's `sys.executable` is python.exe and the app is a script argument, so writing that
    would produce a Run entry that breaks the moment the repo moves - and it would break SILENTLY,
    at a login, which is the worst place to find out. Refusing is the honest answer.
    """
    if not getattr(sys, "frozen", False):
        return None
    exe = os.path.abspath(sys.executable)
    return exe if os.path.isfile(exe) else None


def command_for(exe: str) -> str:
    """The exact Run value for an executable. Quoted, because the install path contains spaces
    (Program Files, or a user profile with a space in the name) and an unquoted path with a space
    is parsed as a command plus arguments."""
    return f'"{exe}" {STARTUP_FLAG}'


def _open_run_key(write: bool):
    import winreg
    access = winreg.KEY_READ | (winreg.KEY_SET_VALUE if write else 0)
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, access)


def read_value() -> str | None:
    """What the Run entry currently says, or None if there is not one. Never raises.

    FALLS BACK TO THE LEGACY NAME so a machine that has not been upgraded since the rename still
    reports autostart as ON. Without that the switch would read as off, the user would turn it on,
    and they would end up with both entries.
    """
    for name in (VALUE_NAME, LEGACY_VALUE_NAME):
        try:
            import winreg
            with _open_run_key(False) as key:
                value, _kind = winreg.QueryValueEx(key, name)
                return str(value)
        except OSError:
            continue
        except Exception:
            return None
    return None


def _delete_value(name: str) -> None:
    """Remove one Run value if it is there. Absent is not a failure."""
    import winreg
    try:
        with _open_run_key(True) as key:
            winreg.DeleteValue(key, name)
    except FileNotFoundError:
        pass


def state() -> dict:
    """{enabled, supported, value, message} for the UI.

    `supported` False means the switch must refuse rather than write: either this is not the
    installed exe, or the registry is unreadable. The message says which, because "it did not work"
    with no reason is the failure a blind user cannot investigate.

    A value that exists but names a DIFFERENT command is reported as enabled with a message saying
    so, never silently corrected. It usually means another copy of the app is set to start - a
    second install, or one that moved - and quietly rewriting somebody's login behaviour to point at
    us is not ours to do without saying it.
    """
    exe = app_exe()
    value = read_value()
    if exe is None:
        return {"enabled": value is not None, "supported": False, "value": value,
                "message": ("This copy is running from source rather than from an installed "
                            "AccessCaster, so it cannot set itself to start when you sign in. The "
                            "installed app can." if value is None else
                            "Something is set to start when you sign in, but this copy is running "
                            "from source and cannot change it.")}
    want = command_for(exe)
    if value is None:
        return {"enabled": False, "supported": True, "value": None,
                "message": "AccessCaster does not start when you sign in."}
    if value.strip().lower() != want.strip().lower():
        return {"enabled": True, "supported": True, "value": value,
                "message": (f"AccessCaster starts when you sign in, but from a different copy: "
                            f"{value}. Switching this off and on again points it at this one.")}
    return {"enabled": True, "supported": True, "value": value,
            "message": "AccessCaster starts when you sign in, in the system tray."}


def set_enabled(on: bool) -> dict:
    """Turn autostart on or off for THIS user. {ok, enabled, message}."""
    exe = app_exe()
    if on and exe is None:
        return {"ok": False, "enabled": read_value() is not None,
                "message": ("This copy is running from source rather than from an installed "
                            "AccessCaster. Only the installed app can set itself to start when you sign "
                            "in, because the startup entry has to name a program that will still be "
                            "there.")}
    try:
        import winreg
        if on:
            with _open_run_key(True) as key:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command_for(exe))
        else:
            _delete_value(VALUE_NAME)   # already off; asking for what you have is not a failure
        # EITHER WAY, the pre-rename entry goes. Switching autostart ON while it survived would
        # start the app twice at sign-in, and switching OFF while it survived would leave it
        # starting anyway, with the switch saying it does not.
        _delete_value(LEGACY_VALUE_NAME)
    except Exception as exc:
        return {"ok": False, "enabled": read_value() is not None,
                "message": f"Could not change the startup setting: {exc}"}
    return {"ok": True, "enabled": bool(on),
            "message": ("AccessCaster will start when you sign in, in the system tray. It does not "
                        "open its page: use the tray icon, or your shortcut keys, which work as soon "
                        "as it is running." if on else
                        "AccessCaster will no longer start when you sign in.")}
