"""macOS's check of RODE's driver package (Project/mac/driver_install.py). NO DOWNLOAD.

Run: python DevTools/test_mac_driver_install.py
"""
from __future__ import annotations

import os
import tempfile

import _bootstrap  # puts Project/ on sys.path
import _testkit
import driver_install
from _testkit import check
from mac import driver_install as mac_di

print("THE ORIGIN LINE: the name and the team are their own fields")
found = mac_di._ORIGIN.search("x.pkg: accepted\nsource=Notarized Developer ID\n"
                              "origin=Developer ID Installer: FREEDMAN ELECTRONICS PTY LTD "
                              "(Z9T72PWTJA)\n")
check("the name is read whole", found.group("name"), "FREEDMAN ELECTRONICS PTY LTD")
check("the name matches RODE's signer", found.group("name").casefold(),
      driver_install.SIGNER.casefold())
check("the team is read", found.group("team"), "Z9T72PWTJA")

print("\nAN UNSIGNED FILE, as Gatekeeper reads it")
with tempfile.TemporaryDirectory(prefix="ac_driver_test_", ignore_cleanup_errors=True) as tmp:
    unsigned = os.path.join(tmp, "unsigned: odd name.pkg")
    with open(unsigned, "wb") as f:
        f.write(b"this is not a signed installer")
    got = mac_di.signature(unsigned)
    check("is not valid", got.valid, False)
    check("has no signer", got.organisation, "")
    print(f"       Gatekeeper says: {got.report}")

print("\nTHE REAL PACKAGE, if a copy is on this machine")
local = None
for root, _dirs, files in os.walk(os.path.join(_bootstrap.REPO, "_driver")):
    for f in files:
        if f.lower().endswith(".pkg"):
            local = os.path.join(root, f)
if local:
    subject = driver_install.verify_signature(local)
    check("RODE's own package passes", mac_di._ORIGIN.search(f"origin={subject}") is not None, True)
    print(f"       signed by: {subject[:72]}")
else:
    print("  SKIP no local copy in _driver/ to check against")

raise SystemExit(_testkit.finish())
