"""The installer and the app write the same sign-in entry. NO registry writes.

Autostart is ONE registry value written by TWO things that cannot share code: the installer's
`[Registry]` line in `ProjectBuilds/installer.iss`, and `Project/win/autostart.py` for the switch
in the app. Nothing at runtime would notice them drifting apart, and both failure modes are silent:

  * a different VALUE NAME and the installer's `uninsdeletevalue` stops matching, so uninstalling
    leaves a Run entry pointing at a deleted exe: a startup error at every login, with nothing left
    on the machine to turn it off;
  * a different COMMAND and the two switches disagree about what "on" means, so the app reports a
    startup entry as "a different copy" when it is really its own.

Run: python DevTools/test_win_autostart.py
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401  (puts Project/ on the path; import FIRST)
import _testkit
import autostart
from _testkit import check, check_true
from win import autostart as win_autostart

ISS = Path(__file__).resolve().parent.parent / "ProjectBuilds" / "installer.iss"
iss = ISS.read_text(encoding="utf-8")
INSTALL_DIR = r"C:\Program Files\AccessCaster"

print("THE INSTALLER AND THE APP MUST WRITE THE SAME THING")
# The [Registry] line, joined across its Inno line continuations.
block = re.search(r"Root: HKCU;.*?Tasks: startup", iss, re.S)
check_true("the installer still has an autostart registry line", block is not None)
line = " ".join(block.group(0).replace("\\\n", " ").split()) if block else ""

check_true("same registry key", ("Subkey: \"" + win_autostart.RUN_KEY + "\"") in line)
check_true("same value name", ("ValueName: \"" + win_autostart.VALUE_NAME + "\"") in line)
check_true("the installer still deletes it on uninstall", "uninsdeletevalue" in line)
check_true("the installer passes the startup flag", autostart.STARTUP_FLAG in line)

# Inno writes ValueData: """{app}\{#AppExe}"" --startup"  -> a quoted path, then the flag.
m = re.search(r'ValueData: "(.*?)";', line)
check_true("the value data could be read", m is not None)
if m:
    data = m.group(1)
    # Inno doubles a quote to escape it; undo that, then substitute its own constants.
    resolved = data.replace('""', '"').replace("{app}", INSTALL_DIR)
    resolved = resolved.replace("{#AppExe}", win_autostart.EXE_NAME)
    ours = win_autostart.command_for(INSTALL_DIR + os.sep + win_autostart.EXE_NAME,
                                     autostart.STARTUP_FLAG)
    check("the installer's command matches command_for()", resolved.strip(), ours.strip())

print("\nTHE PATH IS QUOTED, BECAUSE THE REAL ONE HAS SPACES IN IT")
cmd = win_autostart.command_for(INSTALL_DIR + r"\AccessCaster.exe", autostart.STARTUP_FLAG)
check("the exe is quoted", cmd.startswith('"C:\\Program Files\\'), True)
check("and the flag sits outside the quotes", cmd.endswith('" --startup'), True)

sys.exit(_testkit.finish())
