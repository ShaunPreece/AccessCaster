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
refuses("an archive with no MSI in it is refused",
        lambda: di._extract_msi(zip_with(["readme.txt"]), tmp), "did not contain")
refuses("and so is one with TWO, rather than guessing which to run",
        lambda: di._extract_msi(zip_with(["a.msi", "b.msi"]), tmp), "more than one")
# FOUND BY EXTENSION, NEVER BY NAME: RODE put the version in the filename ("RODECasterDriver
# (1.0.5).msi"), so matching a literal name would break on their every release.
got = di._extract_msi(zip_with(["RODECasterDriver (9.9.9).msi"]), tmp)
check("an MSI is found by its extension, whatever version is in the name",
      os.path.basename(got), "RODECasterDriver (9.9.9).msi")

# A ZIP ENTRY IS NOT A SAFE PATH. `..` in an entry name writes outside the folder we chose, so the
# extract takes the BASENAME and never the archive's own path.
evil = os.path.join(tmp, "evil.zip")
with zipfile.ZipFile(evil, "w") as z:
    z.writestr("../../escaped.msi", b"x")
got = di._extract_msi(evil, tmp)
check("a path-traversing entry lands inside the chosen folder, not above it",
      os.path.dirname(os.path.abspath(got)) == os.path.abspath(tmp), True)

print("\nTHE SIGNATURE: what counts as RODE's")
unsigned = os.path.join(tmp, "unsigned.msi")
with open(unsigned, "wb") as f:
    f.write(b"this is not a signed installer")
refuses("an unsigned file is refused and NOT installed",
        lambda: di.verify_signature(unsigned), "not validly signed")
check("the signer insisted on is RODE's legal entity, not the brand name",
      di.SIGNER, "Freedman Electronics Pty Ltd")
check("the organisation is read from its own field",
      di.subject_organisation("CN=Freedman Electronics Pty Ltd, O=Freedman Electronics Pty Ltd, "
                              "L=Silverwater, S=New South Wales, C=AU"),
      "Freedman Electronics Pty Ltd")
check("a subject that only CONTAINS the name is not RODE's",
      di.subject_organisation('CN=Freedman Electronics Pty Ltd, O="Not Freedman Electronics Pty '
                              'Ltd, Honest", C=US').casefold() == di.SIGNER.casefold(), False)

print("\nTHE REAL MSI, if a copy is on this machine")
local = None
for root, _dirs, files in os.walk(os.path.join(_bootstrap.REPO, "_driver")):
    for f in files:
        if f.lower().endswith(".msi"):
            local = os.path.join(root, f)
if local:
    subject = di.verify_signature(local)
    check("RODE's own MSI passes", di.subject_organisation(subject), di.SIGNER)
    print(f"       signed by: {subject[:72]}")
else:
    print("  SKIP no local copy in _driver/ to check against")

print("\nTHE REFUSAL TO INSTALL OVER ONE THAT IS ALREADY THERE")


class _St:
    installed = True


with patched((virtual_devices, {"read_status": lambda: _St()})):
    r = usb_audio.action_install_driver()
check("it refuses rather than downloading again", r.get("ok"), False)
check("and points at the manual route for a repair",
      "software page" in (r.get("message") or "").lower(), True)

print("\nWHAT THE APP CONTACTS, AND NOTHING ELSE")
# The README and Help both list what reaches the internet: RODE's driver download, on a button
# press, and AccessCaster's own update check and download. Asserted rather than trusted: no other
# module fetches anything. Matched on the ACT of fetching a URL rather than on the import.
proj = _bootstrap.PROJECT
callers = []
for path in glob.glob(os.path.join(proj, "**", "*.py"), recursive=True):
    with open(path, encoding="utf-8") as source:
        for line in source:
            if not re.search(r"urlopen\s*\(|urlretrieve\s*\(", line):
                continue
            callers.append(os.path.relpath(path, proj).replace(os.sep, "/"))
check("only the driver download and the update opener reach beyond this computer",
      sorted(set(callers)), ["driver_install.py", "selfupdate/http.py"])
check("and the update opener is only ever pointed at our own update server",
      update_check.ROOT, "https://updates.slightperception.com/")

raise SystemExit(_testkit.finish())
