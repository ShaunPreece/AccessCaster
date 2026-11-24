"""macOS's sign-in entry (Project/mac/autostart.py), a LaunchAgent plist, offline and on any
system: it is a plain file, so it is written to a scratch folder here.

Run: python DevTools/test_launch_agent.py
"""
from __future__ import annotations

import os
import plistlib
import sys
import tempfile

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
from _testkit import check
from mac import autostart

print("THE MAC'S SIGN-IN ENTRY")
with tempfile.TemporaryDirectory() as scratch:
    autostart.AGENT = os.path.join(scratch, "LaunchAgents", f"{autostart.LABEL}.plist")
    exe = "/Applications/Access Caster.app/Contents/MacOS/AccessCaster"
    command = autostart.command_for(exe, "--startup")
    check("no entry reads as none", autostart.current(), None)
    autostart.install(command)
    check("an entry written reads back as the same command", autostart.current(), command)
    with open(autostart.AGENT, "rb") as file:
        agent = plistlib.load(file)
    check("it runs the executable with the flag, the path in one piece",
          agent["ProgramArguments"], [exe, "--startup"])
    check("at sign-in, in a desktop session",
          (agent["Label"], agent["RunAtLoad"], agent["LimitLoadToSessionType"]),
          (autostart.LABEL, True, "Aqua"))
    autostart.remove()
    check("removed, it reads as none", autostart.current(), None)
    autostart.remove()
    check("removing it again is not a failure", autostart.current(), None)

sys.exit(_testkit.finish())
