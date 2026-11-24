"""Windows' `driver_install`: RODE's MSI, its Authenticode signature, and Windows Installer."""
from __future__ import annotations

import os
import re
import subprocess
from typing import NamedTuple

# The file RODE's own installer pulls. No version in the path: they replace it in place.
ZIP_URL = "https://update.rode.com/virtual_dev_driver/RODECasterDriver_WIN.zip"
INSTALLER_SUFFIX = ".msi"


class Signature(NamedTuple):
    valid: bool
    report: str          # what Windows reports, for a sentence: "Valid", "NotSigned", ...
    organisation: str
    subject: str


def subject_organisation(subject: str) -> str:
    """The O= field of a certificate subject as Windows writes it ("CN=x, O=y, C=AU"), or "" when
    there is none. A value holding a comma is quoted, with any quote inside it doubled."""
    for m in re.finditer(r'(?:^|,)\s*([\w.]+)=("(?:[^"]|"")*"|[^,]*)', subject):
        if m.group(1).upper() == "O":
            value = m.group(2).strip()
            return value[1:-1].replace('""', '"') if value.startswith('"') else value
    return ""


def signature(path: str) -> Signature:
    """PowerShell rather than ctypes: this runs once, when a human presses a button, and
    `Get-AuthenticodeSignature` is the same check Windows itself applies, worth far more than a
    hand-rolled WinVerifyTrust wrapper nobody can audit."""
    # THE PATH GOES BY ENVIRONMENT VARIABLE, NOT AS AN ARGUMENT. `powershell -Command "..." -args
    # <path>` does not populate `$args` the way it looks as though it should: the check runs
    # against an empty path, reports its status as "-args", and refuses RODE's genuine signed MSI.
    # An env var cannot be mis-parsed and cannot be injected into the command either.
    env = dict(os.environ, ACCESSCASTER_MSI=path)
    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "$s = Get-AuthenticodeSignature -LiteralPath $env:ACCESSCASTER_MSI; "
         "Write-Output $s.Status; Write-Output $s.SignerCertificate.Subject"],
        capture_output=True, text=True, timeout=60, env=env)
    lines = [ln.strip() for ln in (out.stdout or "").splitlines() if ln.strip()]
    status = lines[0] if lines else ""
    subject = lines[1] if len(lines) > 1 else ""
    return Signature(status == "Valid", status, subject_organisation(subject), subject)


def launch(path: str) -> None:
    os.startfile(path)          # noqa: S606 - the point is to let Windows' UI and UAC run
