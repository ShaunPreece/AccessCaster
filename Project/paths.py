"""AccessCaster - path resolution that works both from source and from a frozen build.

When the app is packaged with PyInstaller, the Python files no longer live on disk next to
their data, so `os.path.dirname(__file__)` stops pointing at the real assets. Worse, the
install folder may not be a good place to WRITE user data. This module draws the line clearly:

  - resource_path(*parts): READ-ONLY assets that ship WITH the app (static/, docs/, the
    factory/seed JSON files). Resolved inside the bundle when frozen, or the source tree when
    running `python server.py`.

  - data_path(*parts): WRITABLE user data that the app creates and the user owns (saved
    profiles, custom presets, the tray's settings). Always under %LOCALAPPDATA%\\AccessCaster so
    it is writable regardless of where the app is installed, and so it survives reinstall and
    uninstall.

Running from source behaves exactly as before for read-only assets (same source dir); only the
writable data moves to %LOCALAPPDATA%, which is created on first use.
"""

from __future__ import annotations

import os
import sys

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
    """Absolute path to a read-only bundled asset (e.g. resource_path('static', 'index.html')).

    When frozen, every bundled asset sits flat under RESOURCE_DIR (sys._MEIPASS). Running from
    source the assets are split across the repo: the app's own files (static/, say.ps1,
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
