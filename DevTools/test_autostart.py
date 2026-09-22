"""Offline test of "start when you sign in". NO registry writes: everything here is decided before
one is made.

WHY IT EXISTS. Autostart is ONE registry value written by TWO things that cannot share code: the
installer's `[Registry]` line in `ProjectBuilds/installer.iss`, and `Project/autostart.py` for the
switch in the app. Nothing at runtime would notice them drifting apart, and both failure modes are
silent and nasty:

  * a different VALUE NAME and the installer's `uninsdeletevalue` stops matching, so uninstalling
    leaves a Run entry pointing at a deleted exe - a startup error at every login, with nothing left
    on the machine to turn it off;
  * a different COMMAND and the two switches disagree about what "on" means, so the app reports a
    startup entry as "a different copy" when it is really its own.

So this reads the .iss and compares it with the module. It also pins the `--startup` flag, which is
the whole reason a login launch lands in the tray instead of opening the page.

Run: python DevTools/test_autostart.py
"""
from __future__ import annotations

import io
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401  (puts Project/ on the path; import FIRST)

import autostart

FAILED = []


def check(label, got, want=True):
    ok = (got == want)
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f"  [got {got!r}, want {want!r}]"))
    if not ok:
        FAILED.append(label)


ISS = Path(__file__).resolve().parent.parent / "ProjectBuilds" / "installer.iss"
iss = io.open(ISS, encoding="utf-8").read()

print("THE INSTALLER AND THE APP MUST WRITE THE SAME THING")
# The [Registry] line, joined across its Inno line continuations.
block = re.search(r"Root: HKCU;.*?Tasks: startup", iss, re.S)
check("the installer still has an autostart registry line", block is not None)
line = " ".join(block.group(0).replace("\\\n", " ").split()) if block else ""

check("same registry key", ("Subkey: \"" + autostart.RUN_KEY + "\"") in line)
check("same value name", ("ValueName: \"" + autostart.VALUE_NAME + "\"") in line)
check("the installer still deletes it on uninstall", "uninsdeletevalue" in line)
check("the installer passes the startup flag", autostart.STARTUP_FLAG in line)

# Inno writes ValueData: """{app}\{#AppExe}"" --startup"  -> a quoted path, then the flag.
m = re.search(r'ValueData: "(.*?)";', line)
check("the value data could be read", m is not None)
if m:
    data = m.group(1)
    # Inno doubles a quote to escape it; undo that, then substitute its own constants.
    resolved = data.replace('""', '"').replace("{app}", r"C:\Program Files\AccessCaster")
    resolved = resolved.replace("{#AppExe}", autostart.EXE_NAME)
    ours = autostart.command_for(r"C:\Program Files\AccessCaster" + os.sep + autostart.EXE_NAME)
    check("the installer's command matches command_for()", resolved.strip(), ours.strip())

print("\nTHE FLAG IS WHAT MAKES A LOGIN LAUNCH DIFFERENT")
check("a login launch is recognised", autostart.started_at_login(["--startup"]), True)
check("a launch by hand is not", autostart.started_at_login([]), False)
check("and an unrelated argument is not either", autostart.started_at_login(["--debug"]), False)

print("\nTHE PATH IS QUOTED, BECAUSE THE REAL ONE HAS SPACES IN IT")
cmd = autostart.command_for(r"C:\Program Files\AccessCaster\AccessCaster.exe")
check("the exe is quoted", cmd.startswith('"C:\\Program Files\\'), True)
check("and the flag sits outside the quotes", cmd.endswith('" --startup'), True)

print("\nA SOURCE RUN REFUSES RATHER THAN WRITING SOMETHING THAT WOULD BREAK")
# This suite never runs frozen, so app_exe() is the refusing branch by construction.
check("app_exe() is None from source", autostart.app_exe(), None)
res = autostart.set_enabled(True)
check("turning it on is refused", res["ok"], False)
check("and the refusal says WHY, naming source", "from source" in res["message"], True)
st = autostart.state()
check("state() reports it cannot be changed here", st["supported"], False)
check("but still says whether an entry exists", isinstance(st["enabled"], bool), True)

print("\n" + ("ALL AUTOSTART CHECKS PASSED" if not FAILED else f"{len(FAILED)} FAILED"))
sys.exit(1 if FAILED else 0)
