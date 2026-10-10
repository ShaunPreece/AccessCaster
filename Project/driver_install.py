"""Fetch RODE's Virtual Devices driver and hand it to Windows to install.

THIS IS THE APP'S ONLY OUTBOUND INTERNET CALL. Everything else it does talks to localhost and the
USB cable. That is a property worth keeping deliberate rather than losing by accident, so this runs
ONLY when the user presses the button: never at startup, never on a timer, never as a check for a
newer version. Said plainly in the README and in Help, because a user who chose this app partly for
what it does not do deserves to know where the one exception is.

WHY THE APP DOES IT AT ALL, rather than the installer. The Routing page already reports whether the
driver is installed, running and connected, so the fix belongs where the problem is named. An
installer checkbox cannot know, months later, that somebody has decided to try Expanded, and it
cannot help an install that already happened. The installer stays out of it.

WHAT IT DOES NOT DO: install silently. RODE's own MSI runs with the normal Windows Installer UI and
the normal UAC prompt. A program that installs a kernel driver without showing you is not one
anybody should trust, and ours is not going to be the first.

THE SIGNATURE IS THE CHECK, NOT A HASH. The URL carries no version and RODE replace the file in
place, so a pinned hash would refuse every release they ever ship, which trains people to skip the
check. An Authenticode signature by Freedman Electronics Pty Ltd - RODE's legal entity, verified on
the 1.0.5 MSI - stays true across releases and is the thing actually worth proving.

AND THERE IS ALWAYS THE MANUAL ROUTE. `PAGE_URL` is offered as its own button on the page, not only
after a failure: a screen-reader user should not have to make something go wrong to discover the
way round it. The two routes are independent, so RODE moving either one leaves the other working.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile

import paths

# The file RODE's own installer pulls. No version in the path: they replace it in place.
ZIP_URL = "https://update.rode.com/virtual_dev_driver/RODECasterDriver_WIN.zip"
# The human route. RODE's software page renders in JavaScript, so its contents cannot be checked
# from here and this is a best-effort landing page rather than a verified deep link; the machine
# route above is the one that is verified. That is the point of having both.
PAGE_URL = "https://rode.com/en/software"

# The signer to insist on. Freedman Electronics Pty Ltd is RODE's legal entity and is what signs
# the MSI; matching on that rather than on "RODE" is what makes the check mean anything.
SIGNER = "Freedman Electronics"

DOWNLOAD_TIMEOUT_S = 60
# The zip is about 1.1 MB. The cap is not a guess at its size, it is a refusal to write an
# unbounded download to somebody's disk if the URL ever stops being what we think it is.
MAX_BYTES = 50 * 1024 * 1024

# Every failure ends by pointing at the manual route, which works whatever went wrong here.
FALLBACK = " Use the button to open RODE's software page instead."


class DriverInstallError(Exception):
    """Anything that stops the automatic route. The message is shown to the user as-is, so it says
    what to do next rather than only what went wrong; FALLBACK is added to every one."""

    def __init__(self, reason: str):
        super().__init__(reason + FALLBACK)


def _no_step(_text: str) -> None:
    pass


def _download(dest: str, on_step=_no_step) -> str:
    on_step("Downloading the driver from RODE.")
    try:
        req = urllib.request.Request(ZIP_URL, headers={"User-Agent": "AccessCaster"})
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_S) as r, open(dest, "wb") as f:
            total = 0
            while True:
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_BYTES:
                    raise DriverInstallError(
                        "The download from RODE was far larger than expected, so it was stopped.")
                f.write(chunk)
    except DriverInstallError:
        raise
    except Exception as exc:
        raise DriverInstallError(
            f"The driver could not be downloaded ({exc}). You may have no internet connection, or "
            f"RODE may have moved the file.")
    if not os.path.getsize(dest):
        raise DriverInstallError("The download from RODE was empty.")
    return dest


def _extract_msi(zip_path: str, into: str, on_step=_no_step) -> str:
    on_step("Unpacking it.")
    try:
        with zipfile.ZipFile(zip_path) as z:
            # The MSI by EXTENSION, wherever RODE put it in the archive, rather than by a filename
            # carrying a version number that changes on their every release.
            names = [n for n in z.namelist() if n.lower().endswith(".msi")]
            if not names:
                raise DriverInstallError(
                    "RODE's download did not contain an installer this app recognises.")
            if len(names) > 1:
                raise DriverInstallError(
                    "RODE's download contained more than one installer, so this app will not guess "
                    "which to run.")
            # EXTRACTED BY NAME, NEVER BY THE PATH IN THE ARCHIVE: a zip entry may hold `..` or an
            # absolute path and write outside the folder we chose.
            target = os.path.join(into, os.path.basename(names[0]))
            with z.open(names[0]) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            return target
    except DriverInstallError:
        raise
    except Exception as exc:
        raise DriverInstallError(f"RODE's download could not be unpacked ({exc}).")


def verify_signature(msi_path: str) -> str:
    """The MSI's Authenticode signer, or raise. Returns the signer subject for the log.

    PowerShell rather than ctypes: this runs once, when a human presses a button, and
    `Get-AuthenticodeSignature` is the same check Windows itself applies - worth far more than a
    hand-rolled WinVerifyTrust wrapper nobody can audit.
    """
    # THE PATH GOES BY ENVIRONMENT VARIABLE, NOT AS AN ARGUMENT. `powershell -Command "..." -args
    # <path>` does not populate `$args` the way it looks as though it should: the check then ran
    # against an empty path, reported its status as "-args", and REFUSED RODE's genuine signed
    # MSI. An env var cannot be mis-parsed and cannot be injected into the command either.
    env = dict(os.environ, ACCESSCASTER_MSI=msi_path)
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "$s = Get-AuthenticodeSignature -LiteralPath $env:ACCESSCASTER_MSI; "
             "Write-Output $s.Status; Write-Output $s.SignerCertificate.Subject"],
            capture_output=True, text=True, timeout=60, env=env)
    except Exception as exc:
        raise DriverInstallError(f"The driver's signature could not be checked ({exc}), so it has "
                                 f"not been installed.")
    lines = [ln.strip() for ln in (out.stdout or "").splitlines() if ln.strip()]
    status = lines[0] if lines else ""
    subject = lines[1] if len(lines) > 1 else ""
    if status != "Valid":
        raise DriverInstallError(
            f"The file downloaded from RODE is not validly signed (Windows reports "
            f"{status or 'no signature'}), so it has NOT been installed.")
    if SIGNER.lower() not in subject.lower():
        raise DriverInstallError(
            f"The file downloaded from RODE is signed by someone else ({subject or 'unknown'}), so "
            f"it has NOT been installed.")
    return subject


def _launch(msi_path: str, on_step=_no_step) -> None:
    """Hand the MSI to Windows. Normal installer UI, normal UAC prompt, and we do not wait: the
    install belongs to the user from here, and holding a worker thread open for however long they
    take over a UAC prompt buys nothing."""
    on_step("Starting RODE's installer. Windows will ask for permission.")
    try:
        os.startfile(msi_path)          # noqa: S606 - the point is to let Windows' UI and UAC run
    except Exception as exc:
        raise DriverInstallError(f"RODE's installer could not be started ({exc}).")


def install(on_step=None) -> dict:
    """Download, verify and start RODE's installer. Raises DriverInstallError with a sentence for
    the user. Returns {signer, msi} once their installer has been handed over."""
    on_step = on_step or _no_step
    folder = paths.data_path("driver")
    os.makedirs(folder, exist_ok=True)
    # A fresh temp file per attempt, so a half-written download from a dropped connection can never
    # be the thing we check the signature of.
    fd, zip_path = tempfile.mkstemp(suffix=".zip", dir=folder)
    os.close(fd)
    try:
        _download(zip_path, on_step)
        msi = _extract_msi(zip_path, folder, on_step)
        on_step("Checking it is signed by RODE.")
        signer = verify_signature(msi)
        _launch(msi, on_step)
        return {"signer": signer, "msi": msi}
    finally:
        try:
            os.remove(zip_path)
        except OSError:
            pass
