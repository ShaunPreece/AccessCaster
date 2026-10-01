"""THE REAL DOWNLOAD FROM RODE, end to end except for handing the MSI over.

The offline half (`test_driver_install.py`) pins the decisions. This one answers the questions only
the internet can: is the URL still there, is the file still a zip holding exactly one MSI, and is
that MSI still signed by Freedman Electronics. Those are the three things that rot without anybody
here doing anything, because RODE replace the file in place.

IT DOES NOT INSTALL. `install()` ends by handing the MSI to Windows; this runs every step before
that and stops, so it can be run on a machine that already has the driver without disturbing it.

It is a `probe_`, not a `test_`, for two reasons: it reaches the internet, which no test should do
unasked, and `_bootstrap` would give a `test_` a throwaway data dir.

    python DevTools/probe_driver_fetch_live.py
"""
from __future__ import annotations
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import driver_install as di

FAILED = 0


def check(label, got, want):
    global FAILED
    if got == want:
        print(f"  ok   {label}: {got!r}")
    else:
        FAILED += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


folder = tempfile.mkdtemp(prefix="ac_driver_fetch_")
zip_path = os.path.join(folder, "driver.zip")

print(f"=== fetching {di.ZIP_URL} ===")
steps = []
at = time.time()
try:
    di._download(zip_path, steps.append)
except di.DriverInstallError as exc:
    print(f"  FAIL the download refused: {exc}")
    raise SystemExit(1)
size = os.path.getsize(zip_path)
print(f"  ok   downloaded {size} bytes in {time.time() - at:.1f}s")
check("it is a plausible size for one MSI, not a web page", 200_000 < size < 20_000_000, True)

msi = di._extract_msi(zip_path, folder, steps.append)
print(f"  ok   one MSI inside: {os.path.basename(msi)!r}")

subject = di.verify_signature(msi)
check("signed by the entity the app insists on", di.SIGNER.lower() in subject.lower(), True)
print(f"       {subject}")

print(f"\n  the steps a user would hear: {steps}")

# WORTH KNOWING WHEN IT MOVES, rather than only that it still works. The app does not pin a version
# and must not, but a session running this should see what RODE are shipping today.
local = Path(__file__).resolve().parent.parent / "_driver" / "RODECasterDriver_WIN.zip"
if local.is_file():
    same = local.stat().st_size == size
    print(f"\n  the copy in _driver/ is {'the SAME size' if same else 'a DIFFERENT size'} "
          f"({local.stat().st_size} bytes), so RODE have "
          f"{'not changed' if same else 'CHANGED'} the file since it was taken.")

print(f"\nFAILURES: {FAILED}")
raise SystemExit(1 if FAILED else 0)
