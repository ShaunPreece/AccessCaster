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
import profiles
import server
from _testkit import check


def page_name(entry: dict) -> str:
    """What `selectedBackupName()` in app.js sends for a backups list entry."""
    return entry["key"] if entry["kind"] == "unit" else entry["name"]


def write_profile(folder: str, name: str, prof: dict) -> None:
    os.makedirs(os.path.join(folder, name))
    with open(os.path.join(folder, name, "profile.json"), "w", encoding="utf-8") as f:
        json.dump(prof, f)


print("RENAME AND DELETE TAKE WHAT THE PAGE SENDS")
tmp = tempfile.mkdtemp(prefix="ac_backups_test_")
saved_dir = profiles.PROFILES_DIR
saved_device = server.devices.get_active_device
profiles.PROFILES_DIR = tmp
server.devices.get_active_device = lambda: SimpleNamespace(caps=set())
try:
    write_profile(tmp, "Tuesday", {"saved_at": "now"})
    write_profile(tmp, "As found", {"saved_at": "now", profiles.BASELINE_KEY: True})
    entries = {e["name"]: e for e in server.read_backups()["backups"]}
    check("both profiles are listed", sorted(entries), ["As found", "Tuesday"])

    res = server.rename_profile(page_name(entries["Tuesday"]), "Wednesday")
    check("rename by the page's name succeeds", res.get("ok"), True)
    entries = {e["name"]: e for e in server.read_backups()["backups"]}
    check("the renamed profile is listed under its new name", "Wednesday" in entries, True)

    res = server.action_delete_backup(page_name(entries["Wednesday"]))
    check("delete by the page's name succeeds", res.get("ok"), True)

    res = server.action_delete_backup(page_name(entries["As found"]))
    check("the as-found backup is refused without force", res.get("ok"), False)
    check("the refusal arms the second press", bool(res.get("baseline")), True)
    res = server.action_delete_backup(page_name(entries["As found"]), True)
    check("the as-found backup is deleted with force", res.get("ok"), True)
    check("the store is empty", server.read_backups()["backups"], [])
finally:
    profiles.PROFILES_DIR = saved_dir
    server.devices.get_active_device = saved_device
    shutil.rmtree(tmp, ignore_errors=True)


print("EVERY KEY THE PAGE POSTS IS READ BY ITS ROUTE")
APP_JS = Path(server.STATIC_DIR) / "app.js"
CALL = re.compile(r'postJSON\(\s*"(/api/[^"]+)"\s*,\s*\{')


def object_keys(text: str, start: int) -> list[str]:
    """The top-level keys of the object literal whose opening brace is at `start`."""
    depth, pieces, piece = 0, [], ""
    for ch in text[start + 1:]:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                pieces.append(piece)
                break
            depth -= 1
        if ch == "," and depth == 0:
            pieces.append(piece)
            piece = ""
        else:
            piece += ch
    keys = []
    for p in pieces:
        m = re.match(r"\s*(\w+)\s*(?::|$)", p)
        if m:
            keys.append(m.group(1))
    return keys


source = APP_JS.read_text(encoding="utf-8")
calls = 0
for m in CALL.finditer(source):
    route = m.group(1)
    if route not in server.POST_ROUTES:
        continue
    calls += 1
    read = {key for (_, key, _) in server.POST_ROUTES[route][1]}
    dropped = [k for k in object_keys(source, m.end() - 1) if k not in read]
    line = source.count("\n", 0, m.start()) + 1
    check(f"app.js:{line} {route} posts only keys its route reads", dropped, [])
check("the page's literal posts were found", calls > 30, True)


sys.exit(_testkit.finish())
