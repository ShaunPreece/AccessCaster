"""AccessCaster - path resolution that works both from source and from a frozen build.

When the app is packaged with PyInstaller, the Python files no longer live on disk next to
their data, so `os.path.dirname(__file__)` stops pointing at the real assets. Worse, the
install folder may not be a good place to WRITE user data. This module draws the line clearly:

  - resource_path(*parts): READ-ONLY assets that ship WITH the app (static/, docs/, the
    factory/seed JSON files). Resolved inside the bundle when frozen, or the source tree when
    running from source.

  - data_path(*parts): WRITABLE user data that the app creates and the user owns (saved
    profiles, custom presets, the tray's settings). Always under %LOCALAPPDATA%\\AccessCaster so
    it is writable regardless of where the app is installed, and so it survives reinstall and
    uninstall.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import tempfile

# Directory that holds the read-only assets. PyInstaller sets sys._MEIPASS to the unpacked
# bundle dir (onedir: the _internal folder; onefile: a temp dir). From source it is this file's
# own directory.
if getattr(sys, "frozen", False):
    RESOURCE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
else:
    RESOURCE_DIR = os.path.dirname(os.path.abspath(__file__))

# Writable per-user data dir. LOCALAPPDATA is set on every modern Windows; fall back to the
# user profile (or, failing that, the resource dir) so non-Windows dev runs still work.
#
# ACCESSCASTER_DATA_DIR OVERRIDES IT WHOLESALE, and exists so a TEST cannot write to the user's
# real profiles, schemes and parked chains (see DevTools/_bootstrap). Read once at import, like
# everything else here, because modules derive their own constants from it at import time
# (`profiles.PROFILES_DIR`, `routing_schemes.SCHEMES_DIR`): setting it later would move some paths
# and not others. So it must be in the environment BEFORE the first Project import.
def _user_data_dir() -> str:
    override = os.environ.get("ACCESSCASTER_DATA_DIR")
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "AccessCaster")


DATA_DIR = _user_data_dir()


def resource_path(*parts: str) -> str:
    """Absolute path to a read-only bundled asset (e.g. resource_path('help', 'help.txt')).

    When frozen, every bundled asset sits flat under RESOURCE_DIR (sys._MEIPASS). Running from
    source the assets are split across the repo: the app's own files (static/,
    assets/icon.ico) live alongside the code in Project/, while the device data (fx_recipes.json,
    mic_presets.json) lives in the sibling ProjectAssets/ folder. So in source mode we look in both
    Project/ and ../ProjectAssets/ and return the first match.
    """
    rel = os.path.join(*parts)
    if getattr(sys, "frozen", False):
        return os.path.join(RESOURCE_DIR, rel)
    for base in (RESOURCE_DIR, os.path.join(os.path.dirname(RESOURCE_DIR), "ProjectAssets")):
        candidate = os.path.join(base, rel)
        if os.path.exists(candidate):
            return candidate
    return os.path.join(RESOURCE_DIR, rel)


def data_path(*parts: str) -> str:
    """Absolute path to a writable user-data file/dir, creating the data dir on first use."""
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, *parts)


def safe_name(name: str) -> str | None:
    """A name the user chose, trimmed, if it can be a file or folder name in the data dir, else
    None. Letters, digits, spaces, hyphens and underscores, 1 to 40 of them: no separator and no
    dot, so a name can never climb out of its folder. Saved profiles and saved mix schemes share
    this rule, so the two stores cannot disagree about what is nameable."""
    name = (name or "").strip()
    return name if re.fullmatch(r"[A-Za-z0-9 _-]{1,40}", name) else None


def read_json(path: str, default):
    """Read a JSON file the app writes with `write_json`. Returns `default` when there is no file.

    A file that does not parse, or holds a value of another type than `default`, is renamed to
    `<path>.bad` and logged, and `default` is returned. Returning `default` while leaving the file
    in place would let the next save replace it with only the new entry, losing every other one;
    the `.bad` copy keeps them for recovery by hand. Raises OSError when the file exists but cannot
    be opened, or cannot be moved aside, so a caller does not save over a file it could not read."""
    import diagnostics  # imported here: diagnostics imports this module
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return default
    except ValueError as exc:
        reason = str(exc)
    else:
        if isinstance(data, type(default)):
            return data
        reason = f"holds {type(data).__name__}, not {type(default).__name__}"
    name = os.path.basename(path)
    try:
        os.replace(path, path + ".bad")
    except OSError as exc:
        diagnostics.event("json.corrupt", f"{name} did not parse and could not be moved aside",
                          reason=reason, error=str(exc))
        raise
    diagnostics.event("json.corrupt", f"{name} did not parse; kept as {name}.bad", reason=reason)
    return default


def write_json(path: str, data, indent: int | None = 2) -> None:
    """Write `data` as JSON so that `path` only ever holds the old file or the whole new one.

    The JSON goes to a temporary file in the same folder, which os.replace then swaps in. The swap
    is atomic on one volume, so a kill or a power cut mid-write leaves the previous file intact,
    where writing in place would leave half a file that no longer parses. Raises OSError, or
    TypeError for data JSON cannot hold, with the temporary file removed."""
    fd, tmp = tempfile.mkstemp(prefix=".", suffix=".tmp", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
