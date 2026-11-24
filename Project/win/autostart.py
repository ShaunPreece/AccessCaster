"""Windows' `autostart`: the Run value the installer's checkbox writes.

THE VALUE IS THE INSTALLER'S, NOT OURS, AND THAT IS DELIBERATE. Same key, same NAME:

    HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
      "AccessCaster" = "<install path>\\AccessCaster.exe" --startup

`installer.iss` sets it with `uninsdeletevalue`, so uninstalling removes it. Write it under any
other name and an uninstall leaves a Run entry pointing at a deleted exe: a startup error every
login, with nothing left on the machine to turn it off. `DevTools/test_win_autostart.py` reads
`installer.iss` and asserts the two still agree, because nothing else would notice them drifting.

HKCU AND NEVER HKLM. Only one copy of AccessCaster runs per PC (`instance`), so a machine-wide Run
entry would start it for every account that signs in and all but the first would be refused. A
machine can have somebody at the console and somebody else in an RDP session at once.
"""
from __future__ import annotations

import winreg

# The installer's key, name and exe. Change any of these and change installer.iss in the same edit.
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "AccessCaster"
EXE_NAME = "AccessCaster.exe"


def command_for(exe: str, flag: str) -> str:
    """Quoted, because the install path contains spaces (Program Files, or a user profile with a
    space in the name) and an unquoted path with a space is parsed as a command plus arguments."""
    return f'"{exe}" {flag}'


def _open_run_key(write: bool):
    access = winreg.KEY_READ | (winreg.KEY_SET_VALUE if write else 0)
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, access)


def current() -> str | None:
    try:
        with _open_run_key(False) as key:
            value, _kind = winreg.QueryValueEx(key, VALUE_NAME)
            return str(value)
    except Exception:
        return None


def install(command: str) -> None:
    with _open_run_key(True) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)


def remove() -> None:
    try:
        with _open_run_key(True) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        pass
