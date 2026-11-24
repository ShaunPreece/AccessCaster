"""Unit tests for fetching RODE's driver. NO DOWNLOAD, no install, no device.

The live half (the real URL, the real signature) is `probe_driver_fetch_live.py`. This pins the
DECISIONS, which are the part that must not rot: what the archive is allowed to contain, what
counts as RODE's signature, and that the app refuses to install over a driver that is already
there. Every one of them is a refusal, and a refusal that silently stops refusing is the failure
mode worth testing for.

Run: python DevTools/test_driver_install.py
"""
from __future__ import annotations

import glob
import os
import re
import tempfile
import zipfile
from types import SimpleNamespace

import _bootstrap  # puts Project/ on sys.path
import _testkit
import driver_install as di
import selfupdate.check as update_check
import usb_audio
import virtual_devices
from _testkit import check, check_true, patched


def refuses(label, fn, must_say):
    """The call raises DriverInstallError AND its message tells the user what to do next."""
    try:
        fn()
    except di.DriverInstallError as exc:
        said = str(exc)
        check_true(label, must_say.lower() in said.lower() and "software page" in said.lower(),
                   said[:96])
        return
    check_true(label, False, "it did NOT refuse")


# Held for the whole run: the folder is removed when this object goes, at exit however it ends.
tmp_dir = tempfile.TemporaryDirectory(prefix="ac_driver_test_", ignore_cleanup_errors=True)
tmp = tmp_dir.name


def zip_with(names):
    path = os.path.join(tmp, f"z{len(os.listdir(tmp))}.zip")
    with zipfile.ZipFile(path, "w") as z:
        for n in names:
            z.writestr(n, b"not really an installer")
    return path


print("THE ARCHIVE: what it is allowed to contain")
ext = di.INSTALLER_SUFFIX
refuses("an archive with no installer in it is refused",
        lambda: di._extract_installer(zip_with(["readme.txt"]), tmp), "did not contain")
refuses("and so is one with TWO, rather than guessing which to run",
        lambda: di._extract_installer(zip_with(["a" + ext, "b" + ext]), tmp), "more than one")
# FOUND BY EXTENSION, NEVER BY NAME: RODE put the version in the filename ("RODECasterDriver
# (1.0.5).msi"), so matching a literal name would break on their every release.
got = di._extract_installer(zip_with(["RODECasterDriver (9.9.9)" + ext]), tmp)
check("an installer is found by its extension, whatever version is in the name",
      os.path.basename(got), "RODECasterDriver (9.9.9)" + ext)

# A ZIP ENTRY IS NOT A SAFE PATH. `..` in an entry name writes outside the folder we chose, so the
# extract takes the BASENAME and never the archive's own path.
evil = os.path.join(tmp, "evil.zip")
with zipfile.ZipFile(evil, "w") as z:
    z.writestr("../../escaped" + ext, b"x")
got = di._extract_installer(evil, tmp)
check("a path-traversing entry lands inside the chosen folder, not above it",
      os.path.dirname(os.path.abspath(got)) == os.path.abspath(tmp), True)

print("\nTHE SIGNATURE: what counts as RODE's")
RODE_SUBJECT = "CN=Freedman Electronics Pty Ltd, O=Freedman Electronics Pty Ltd, C=AU"


def signed(valid, organisation, subject=RODE_SUBJECT, report="Valid"):
    return lambda _path: SimpleNamespace(valid=valid, report=report, organisation=organisation,
                                         subject=subject)


def cannot_check(_path):
    raise OSError("no checker")


check("the signer insisted on is RODE's legal entity, not the brand name",
      di.SIGNER, "Freedman Electronics Pty Ltd")
with patched((di, {"signature": signed(False, "", "", "NotSigned")})):
    refuses("an unsigned file is refused and NOT installed",
            lambda: di.verify_signature("x"), "not validly signed")
with patched((di, {"signature": signed(True, "Someone Else Ltd", "CN=Someone Else Ltd")})):
    refuses("a file signed by somebody else is refused",
            lambda: di.verify_signature("x"), "signed by someone else")
with patched((di, {"signature": cannot_check})):
    refuses("and so is one whose signature cannot be checked",
            lambda: di.verify_signature("x"), "could not be checked")
with patched((di, {"signature": signed(True, "freedman electronics pty ltd")})):
    check("RODE's signature passes, whatever its case", di.verify_signature("x"), RODE_SUBJECT)


print("\nTHE REFUSAL TO INSTALL OVER ONE THAT IS ALREADY THERE")


class _St:
    installed = True


with patched((virtual_devices, {"read_status": lambda: _St()})):
    r = usb_audio.action_install_driver()
check("it refuses rather than downloading again", r.get("ok"), False)
check("and points at the manual route for a repair",
      "software page" in (r.get("message") or "").lower(), True)

print("\nTHE SOFTWARE PAGE BUTTON WITH NO BROWSER TO OPEN IT")
# webbrowser.open says it failed by returning False, not by raising.
with patched((usb_audio.webbrowser, {"open": lambda _url: False})):
    r = usb_audio.action_open_driver_page()
check("it reports the failure", r.get("ok"), False)
check_true("and gives the address to type in", di.PAGE_URL in (r.get("message") or ""),
           str(r.get("message")))
with patched((usb_audio.webbrowser, {"open": lambda _url: True})):
    r = usb_audio.action_open_driver_page()
check("an opened page is reported as opening", r.get("ok"), True)

print("\nWHAT THE APP CONTACTS, AND NOTHING ELSE")
# The README and Help both list what reaches the internet: RODE's driver download, on a button
# press, and AccessCaster's own update check and download. Asserted rather than trusted: only
# `web` fetches a URL, and only those two ask it to. Matched on the ACT of fetching, not the import.
proj = _bootstrap.PROJECT


def modules_matching(pattern):
    found = set()
    for path in glob.glob(os.path.join(proj, "**", "*.py"), recursive=True):
        with open(path, encoding="utf-8") as source:
            if any(re.search(pattern, line) for line in source):
                found.add(os.path.relpath(path, proj).replace(os.sep, "/"))
    return sorted(found)


check("only the web module fetches a URL", modules_matching(r"urlopen\s*\(|urlretrieve\s*\("),
      ["web.py"])
check("and only the driver download and the update ask it to",
      modules_matching(r"\bweb\.opened\s*\("),
      ["driver_install.py", "selfupdate/checker.py", "selfupdate/downloader.py"])
check("and the update opener is only ever pointed at our own update server",
      update_check.ROOT, "https://updates.slightperception.com/")

raise SystemExit(_testkit.finish())
