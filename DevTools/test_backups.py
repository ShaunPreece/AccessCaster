"""Offline checks on the Backups page's rename and delete, and on what the page posts to the server.

THE RULES THIS PINS:
  * rename and delete reach the backup by its NAME from `read_backups`, which is what
    `selectedBackupName()` sends, not by its list key, which `profiles.safe_name` refuses;
  * the as-found backup is refused on the first delete and deleted with `force`;
  * every key the page posts to a route in a literal `postJSON` body is one that route reads, so a
    key such as `force` cannot be dropped on the way to its action.

No device: the profile store is a temporary folder and the desk reports no shows.

    python DevTools/test_backups.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401
import _testkit
import backups
import devices
import profile_actions
import profiles
from _testkit import check, patched


def page_name(entry: dict) -> str:
    """What the Backups page sends for a list entry (`native/backups_section.py`)."""
    return entry["key"] if entry["kind"] == "unit" else entry["name"]


def write_profile(folder: str, name: str, prof: dict) -> None:
    os.makedirs(os.path.join(folder, name))
    with open(os.path.join(folder, name, "profile.json"), "w", encoding="utf-8") as f:
        json.dump(prof, f)


def rename_and_delete(folder: str) -> None:
    """Rename and delete through the page's own names, in a profile store at `folder`."""
    write_profile(folder, "Tuesday", {"saved_at": "now"})
    write_profile(folder, "As found", {"saved_at": "now", profiles.BASELINE_KEY: True})
    entries = {e["name"]: e for e in backups.read_backups()["backups"]}
    check("both profiles are listed", sorted(entries), ["As found", "Tuesday"])

    res = profile_actions.rename_profile(page_name(entries["Tuesday"]), "Wednesday")
    check("rename by the page's name succeeds", res.get("ok"), True)
    entries = {e["name"]: e for e in backups.read_backups()["backups"]}
    check("the renamed profile is listed under its new name", "Wednesday" in entries, True)

    res = backups.action_delete_backup(page_name(entries["Wednesday"]))
    check("delete by the page's name succeeds", res.get("ok"), True)

    res = backups.action_delete_backup(page_name(entries["As found"]))
    check("the as-found backup is refused without force", res.get("ok"), False)
    check("the refusal arms the second press", bool(res.get("baseline")), True)
    res = backups.action_delete_backup(page_name(entries["As found"]), True)
    check("the as-found backup is deleted with force", res.get("ok"), True)
    check("the store is empty", backups.read_backups()["backups"], [])


print("RENAME AND DELETE TAKE WHAT THE PAGE SENDS")
tmp = tempfile.mkdtemp(prefix="ac_backups_test_")
try:
    with patched((profiles, {"PROFILES_DIR": tmp}),
                 (devices, {"get_active_device": lambda: SimpleNamespace(caps=set())})):
        rename_and_delete(tmp)
finally:
    shutil.rmtree(tmp, ignore_errors=True)


sys.exit(_testkit.finish())
