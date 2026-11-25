"""macOS's `driver_install`: RODE's .pkg, Gatekeeper's verdict on it, and the system's Installer.

The archive holds one package, "RODE Virtual Devices-<version>.pkg", signed with RODE's Developer
ID Installer certificate and notarised. Installer shows its own windows and asks for an
administrator's password; the package restarts the system's audio service and starts RODE's menu
bar app when it is done.
"""
from __future__ import annotations

import re
import subprocess
from typing import NamedTuple

# The file RODE's Virtual Devices guide links. No version in the path: they replace it in place.
ZIP_URL = "https://update.rode.com/virtual_dev_driver/RODECasterDriver_MACOS-BETA.zip"
INSTALLER_SUFFIX = ".pkg"

_SPCTL = "/usr/sbin/spctl"
_OPEN = "/usr/bin/open"
# "origin=Developer ID Installer: FREEDMAN ELECTRONICS PTY LTD (Z9T72PWTJA)"
_ORIGIN = re.compile(r"^origin=(?P<subject>.*?:\s*(?P<name>.+?)\s*\((?P<team>[A-Z0-9]+)\))\s*$",
                     re.MULTILINE)


class Signature(NamedTuple):
    valid: bool
    report: str          # what Gatekeeper says: "accepted", "rejected", ...
    organisation: str
    subject: str


def signature(path: str) -> Signature:
    """Gatekeeper's own assessment of an installer, which is what the system applies when the
    package is opened: a valid Developer ID signature and Apple's notarisation, both required.
    The path is an argument of its own, so nothing in it is read as an option."""
    done = subprocess.run([_SPCTL, "--assess", "-vv", "--type", "install", "--", path],
                          capture_output=True, text=True, timeout=60, check=False)
    # spctl writes its verdict to standard error.
    text = (done.stderr or "") + (done.stdout or "")
    # The first line is "<path>: accepted"; the path may hold a colon, the verdict does not.
    first = text.splitlines()[0] if text.strip() else ""
    verdict = first.rsplit(":", 1)[-1].strip()
    found = _ORIGIN.search(text)
    return Signature(done.returncode == 0, verdict or f"exit code {done.returncode}",
                     found.group("name") if found else "",
                     found.group("subject") if found else "")


def launch(path: str) -> None:
    subprocess.run([_OPEN, "--", path], check=True, capture_output=True, timeout=30)
