"""Unit tests for fetching RODE's driver. NO DOWNLOAD, no install, no device.

The live half (the real URL, the real signature) is `probe_driver_fetch_live.py`. This pins the
DECISIONS, which are the part that must not rot: what the archive is allowed to contain, what
counts as RODE's signature, and that the app refuses to install over a driver that is already
there. Every one of them is a refusal, and a refusal that silently stops refusing is the failure
mode worth testing for.

Run: python DevTools/test_driver_install.py
"""
from __future__ import annotations
import io
import os
import sys
import tempfile
import zipfile

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import driver_install as di

FAILED = 0


def check(label, got, want):
    global FAILED
    if got == want:
        print(f"  ok   {label}: {got!r}")
    else:
        FAILED += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def refuses(label, fn, must_say):
    """The call raises DriverInstallError AND its message tells the user what to do next."""
    global FAILED
    try:
        fn()
    except di.DriverInstallError as exc:
        said = str(exc)
        ok = must_say.lower() in said.lower() and "software page" in said.lower()
        FAILED += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label}: {said[:96]}...")
        return
    FAILED += 1
    print(f"  FAIL {label}: it did NOT refuse")


tmp = tempfile.mkdtemp(prefix="ac_driver_test_")


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
      di.SIGNER, "Freedman Electronics")

print("\nTHE REAL MSI, if a copy is on this machine")
local = None
for root, _dirs, files in os.walk(os.path.join(os.path.dirname(_bootstrap.__file__), "..",
                                               "_driver")):
    for f in files:
        if f.lower().endswith(".msi"):
            local = os.path.join(root, f)
if local:
    subject = di.verify_signature(local)
    check("RODE's own MSI passes", di.SIGNER.lower() in subject.lower(), True)
    print(f"       signed by: {subject[:72]}")
else:
    print("  SKIP no local copy in _driver/ to check against")

print("\nTHE REFUSAL TO INSTALL OVER ONE THAT IS ALREADY THERE")
import server                                              # noqa: E402  (after _bootstrap)
import virtual_devices                                     # noqa: E402
_real = virtual_devices.read_status
try:
    class _St:
        installed = True
    virtual_devices.read_status = lambda: _St()
    r = server.action_install_driver()
    check("it refuses rather than downloading again", r.get("ok"), False)
    check("and points at the manual route for a repair",
          "software page" in (r.get("message") or "").lower(), True)
finally:
    virtual_devices.read_status = _real

print("\nTHE ONE OUTBOUND CALL IS THE ONLY ONE")
# The claim the README and Help both make, asserted rather than trusted: no other module reaches
# the network. `urllib`/`socket` appear elsewhere for the LAN server and for reading this machine's
# own address, so match on the ACT of fetching a URL rather than on the import.
import glob                                                # noqa: E402
import re                                                  # noqa: E402
proj = os.path.join(os.path.dirname(os.path.abspath(_bootstrap.__file__)), "..", "Project")
callers = []
for path in glob.glob(os.path.join(proj, "*.py")):
    for line in io.open(path, encoding="utf-8"):
        if not re.search(r"urlopen\s*\(|urlretrieve\s*\(", line):
            continue
        # LOCALHOST IS NOT OUTBOUND. `server.py` pings its own `/api/announce` to see whether
        # another instance is already serving, which is the app talking to itself.
        if re.search(r"localhost|127\.0\.0\.1", line):
            continue
        callers.append(os.path.basename(path))
check("exactly one module reaches anything beyond this computer",
      sorted(set(callers)), ["driver_install.py"])

print(f"\n{'ALL CHECKS PASSED' if not FAILED else str(FAILED) + ' CHECK(S) FAILED'}")
raise SystemExit(1 if FAILED else 0)
