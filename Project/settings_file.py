"""The app's own preferences: ONE small JSON file, %LOCALAPPDATA%\\AccessCaster\\settings.json.

Every read and write goes through here so a writer can never clobber a key it does not know about:
`update` MERGES, where a writer that dumped {"announcer": ...} over the whole file would silently
eat the announcer volume the first time the tray toggled the announcer.
"""
from __future__ import annotations

import threading

import diagnostics
from paths import data_path, read_json, write_json

FILE = data_path("settings.json")
_lock = threading.Lock()


def _read() -> dict | None:
    """The settings file's contents, {} when there is none yet or it did not parse (see
    `paths.read_json`), or None when it could not be opened, which may be passing, such as a
    scanner holding it."""
    try:
        return read_json(FILE, {})
    except OSError:
        return None


def load() -> dict:
    return _read() or {}


def update(**changes) -> None:
    """Merge changes into the settings file. Best-effort: a preference that cannot be written is
    not worth failing an action over, it just will not survive a restart.

    A file that will not parse is kept as settings.json.bad before a fresh one is started, so the
    shortcuts, contacts and other preferences in it can still be recovered by hand. A file that cannot
    be opened at all is left alone and this change is dropped: writing would replace every other
    key with nothing."""
    with _lock:
        data = _read()
        if data is None:
            diagnostics.event("settings.unreadable", "settings.json could not be opened",
                              dropped=",".join(changes))
            return
        try:
            write_json(FILE, {**data, **changes})
        except OSError as exc:
            diagnostics.event("settings.write_failed", "settings.json not written", error=str(exc))
