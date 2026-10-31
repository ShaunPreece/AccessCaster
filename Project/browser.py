"""AccessCaster - find a Chromium browser (Edge or Chrome) to show the UI in.

Shared by the app, which opens its UI in an app-mode window, and by DevTools/ui_probe.py, which runs
the page headless, so a machine on which the app opens also runs the probes.
"""

from __future__ import annotations

import os
import shutil


def find_browser() -> str | None:
    """The path to Edge or Chrome, or None. Edge ships with Windows 11, so it is the reliable
    default; Chrome is used if it is found first. Per-machine installs, a per-user one under
    LOCALAPPDATA, then anything on PATH."""
    names = []
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if not base:
            continue
        names.append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
        names.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
    for path in names:
        if os.path.isfile(path):
            return path
    return shutil.which("msedge") or shutil.which("chrome")
