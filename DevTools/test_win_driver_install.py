"""Windows' check of RODE's driver signature (Project/win/driver_install.py). NO DOWNLOAD.

Run: python DevTools/test_win_driver_install.py
"""
from __future__ import annotations

import os
import tempfile

import _bootstrap  # puts Project/ on sys.path
import _testkit
import driver_install
from _testkit import check
from win import driver_install as win_di

print("THE SUBJECT: the organisation is its own field")
check("the organisation is read from its own field",
      win_di.subject_organisation("CN=Freedman Electronics Pty Ltd, O=Freedman Electronics Pty "
                                  "Ltd, L=Silverwater, S=New South Wales, C=AU"),
      "Freedman Electronics Pty Ltd")
check("a subject that only CONTAINS the name is not RODE's",
      win_di.subject_organisation('CN=Freedman Electronics Pty Ltd, O="Not Freedman Electronics '
                                  'Pty Ltd, Honest", C=US').casefold()
      == driver_install.SIGNER.casefold(), False)

print("\nAN UNSIGNED FILE, as Windows reads it")
with tempfile.TemporaryDirectory(prefix="ac_driver_test_", ignore_cleanup_errors=True) as tmp:
    unsigned = os.path.join(tmp, "unsigned.msi")
    with open(unsigned, "wb") as f:
        f.write(b"this is not a signed installer")
    check("is not valid", win_di.signature(unsigned).valid, False)

print("\nTHE REAL MSI, if a copy is on this machine")
local = None
for root, _dirs, files in os.walk(os.path.join(_bootstrap.REPO, "_driver")):
    for f in files:
        if f.lower().endswith(".msi"):
            local = os.path.join(root, f)
if local:
    subject = driver_install.verify_signature(local)
    check("RODE's own MSI passes", win_di.subject_organisation(subject), driver_install.SIGNER)
    print(f"       signed by: {subject[:72]}")
else:
    print("  SKIP no local copy in _driver/ to check against")

raise SystemExit(_testkit.finish())
