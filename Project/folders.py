"""The folders the app's exports go to, and opening one with the user in it.

The platform supplies `open_focused(folder)`: open `folder` in the system's file browser and move
the user into that window. Raises only when the folder could not be opened at all: failing to
focus is not failing to open, so the focus is best-effort and swallows its own errors.
"""
from __future__ import annotations

import os
import sys

if sys.platform == "win32":
    from win.folders import open_focused
elif sys.platform == "darwin":
    from mac.folders import open_focused
else:
    raise ImportError(f"No folder opener for {sys.platform}: the Windows one is win/folders.py")


def documents_folder(name: str) -> str:
    """Documents/AccessCaster/<name>, created if need be: where the app's exports go.

    Documents rather than the app's own data folder: an export you cannot find is not an export,
    and a reinstall wipes the data folder. There is no folder picker: a fixed destination is easier
    to find again than one chosen each time; the reply says the full path, and Open folder takes
    you there."""
    base = os.path.join(os.path.expanduser("~"), "Documents", "AccessCaster", name)
    os.makedirs(base, exist_ok=True)
    return base


def open_folder_reply(folder: str, what: str) -> dict:
    """Open `folder` and say so. `what` names it in a failure: "The folder"."""
    try:
        open_focused(folder)
    except Exception as exc:
        return {"ok": False, "message": f"{what} is {folder}, but it could not be opened: {exc}"}
    return {"ok": True, "message": f"Opened {folder}."}
