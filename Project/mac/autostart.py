"""macOS's `autostart`: a LaunchAgent, which launchd runs when this user signs in.

    ~/Library/LaunchAgents/<LABEL>.plist
      ProgramArguments = [<the app's executable>, --startup]

System Settings lists it under Login Items, where it can be switched off too. A login item added
through SMAppService is the other way in, and it cannot pass the flag, which is how the app knows it
was started at sign-in.

Writing the file does not start anything: launchd reads it at the next sign-in.
"""
from __future__ import annotations

import os
import plistlib
import shlex

LABEL = "io.github.shaunpreece.accesscaster"
AGENT = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")


def command_for(exe: str, flag: str) -> str:
    return shlex.join([exe, flag])


def current() -> str | None:
    try:
        with open(AGENT, "rb") as file:
            return shlex.join(plistlib.load(file)["ProgramArguments"])
    except Exception:
        return None


def install(command: str) -> None:
    agent = {
        "Label": LABEL,
        "ProgramArguments": shlex.split(command),
        "RunAtLoad": True,
        # Only in a signed-in desktop session, never a background or SSH one.
        "LimitLoadToSessionType": "Aqua",
        "ProcessType": "Interactive",
    }
    os.makedirs(os.path.dirname(AGENT), exist_ok=True)
    partial = AGENT + ".partial"
    with open(partial, "wb") as file:
        plistlib.dump(agent, file)
    os.replace(partial, AGENT)


def remove() -> None:
    try:
        os.remove(AGENT)
    except FileNotFoundError:
        pass
