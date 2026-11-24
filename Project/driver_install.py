"""Fetch RODE's Virtual Devices driver and hand it to the system to install.

ONE OF TWO THINGS IN THE APP THAT REACH THE INTERNET; the other is AccessCaster's own update check
(`selfupdate/`). Everything else stays on this computer and the USB cable. This one runs ONLY
when the user presses the button, never at startup or on a timer. The README and Help list both,
because a user who chose this app partly for what it does not do deserves to know exactly what it
contacts.

WHY THE APP DOES IT AT ALL, rather than the installer. The Routing page already reports whether the
driver is installed, running and connected, so the fix belongs where the problem is named. An
installer checkbox cannot know, months later, that somebody has decided to try Expanded, and it
cannot help an install that already happened. The installer stays out of it.

WHAT IT DOES NOT DO: install silently. RODE's own installer runs with the system's normal installer
UI and its normal permission prompt. A program that installs a kernel driver without showing you is not one
anybody should trust, and ours is not going to be the first.

THE SIGNATURE IS THE CHECK, NOT A HASH. The URL carries no version and RODE replace the file in
place, so a pinned hash would refuse every release they ever ship, which trains people to skip the
check. A signature by Freedman Electronics Pty Ltd, RODE's legal entity, verified on the 1.0.5
Windows MSI, stays true across releases and is the thing actually worth proving.

AND THERE IS ALWAYS THE MANUAL ROUTE. `PAGE_URL` is offered as its own button on the page, not only
after a failure: a screen-reader user should not have to make something go wrong to discover the
way round it. The two routes are independent, so RODE moving either one leaves the other working.

The platform supplies RODE's download for it and the system's checks:
  - `ZIP_URL`: RODE's archive for this system; `INSTALLER_SUFFIX`: the installer inside it.
  - `signature(path)`: a `Signature(valid, report, organisation, subject)`, `report` being what the
    system says of it. Raises when the check itself cannot run.
  - `launch(path)`: hand the installer to the system, with its own UI and permission prompt, and
    return without waiting. Raises when it cannot be started.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import zipfile

import paths
import web
import wording

if sys.platform == "win32":
    from win.driver_install import INSTALLER_SUFFIX, ZIP_URL, launch, signature
elif sys.platform == "darwin":
    from mac.driver_install import INSTALLER_SUFFIX, ZIP_URL, launch, signature
else:
    raise ImportError(f"No driver installer for {sys.platform}: the Windows one is "
                      "win/driver_install.py")

# The human route. RODE's software page renders in JavaScript, so its contents cannot be checked
# from here and this is a best-effort landing page rather than a verified deep link; the machine
# route above is the one that is verified. That is the point of having both.
PAGE_URL = "https://rode.com/en/software"

# The signer to insist on: the certificate's organisation (O=), compared whole. Freedman Electronics
# Pty Ltd is RODE's legal entity and is what signs the MSI; matching on that rather than on "RODE"
# is what makes the check mean anything, and comparing the field whole rather than searching the
# subject is what stops any certificate whose subject merely contains the name from passing.
SIGNER = "Freedman Electronics Pty Ltd"

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
        with web.opened(ZIP_URL, DOWNLOAD_TIMEOUT_S) as r, open(dest, "wb") as f:
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


def _extract_installer(zip_path: str, into: str, on_step=_no_step) -> str:
    on_step("Unpacking it.")
    try:
        with zipfile.ZipFile(zip_path) as z:
            # The installer by EXTENSION, wherever RODE put it in the archive, rather than by a
            # filename carrying a version number that changes on their every release.
            names = [n for n in z.namelist() if n.lower().endswith(INSTALLER_SUFFIX)]
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


def verify_signature(path: str) -> str:
    """The installer's signer, or raise. Returns the signer subject for the log."""
    try:
        got = signature(path)
    except Exception as exc:
        raise DriverInstallError(f"The driver's signature could not be checked ({exc}), so it has "
                                 f"not been installed.")
    if not got.valid:
        raise DriverInstallError(
            f"The file downloaded from RODE is not validly signed ({wording.OS_NAME} reports "
            f"{got.report or 'no signature'}), so it has NOT been installed.")
    if got.organisation.casefold() != SIGNER.casefold():
        raise DriverInstallError(
            f"The file downloaded from RODE is signed by someone else ({got.subject or 'unknown'}), "
            f"so it has NOT been installed.")
    return got.subject


def _launch(path: str, on_step=_no_step) -> None:
    """Hand the installer over, and do not wait: the install belongs to the user from here, and
    holding a worker thread open for however long they take over a permission prompt buys
    nothing."""
    on_step(f"Starting RODE's installer. {wording.OS_NAME} will ask for permission.")
    try:
        launch(path)
    except Exception as exc:
        raise DriverInstallError(f"RODE's installer could not be started ({exc}).")


def install(on_step=None) -> dict:
    """Download, verify and start RODE's installer. Raises DriverInstallError with a sentence for
    the user. Returns {signer, installer} once their installer has been handed over."""
    on_step = on_step or _no_step
    folder = paths.data_path("driver")
    os.makedirs(folder, exist_ok=True)
    # A fresh temp file per attempt, so a half-written download from a dropped connection can never
    # be the thing we check the signature of.
    fd, zip_path = tempfile.mkstemp(suffix=".zip", dir=folder)
    os.close(fd)
    try:
        _download(zip_path, on_step)
        installer = _extract_installer(zip_path, folder, on_step)
        on_step("Checking it is signed by RODE.")
        signer = verify_signature(installer)
        _launch(installer, on_step)
        return {"signer": signer, "installer": installer}
    finally:
        try:
            os.remove(zip_path)
        except OSError:
            pass
