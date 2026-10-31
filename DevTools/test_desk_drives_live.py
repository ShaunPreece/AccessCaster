"""Live check that `desk_drives.on_desk` picks out the desk's own drive letter and nothing else.

Read only: it asks Windows' device tree, and sends nothing to the desk. Needs the desk attached;
transfer mode does not matter, because the desk's letter is assigned in or out of it. The answer is
checked against the disk Windows names "Linux File-Stor Gadget", the desk's mass-storage gadget, so
a mistake in the walk up the device tree shows as a mismatch.

    python DevTools/test_desk_drives_live.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401
import _testkit
import desk_drives
import rcp_transfer
from _testkit import check, check_true

roots = rcp_transfer._drive_roots()
found = desk_drives.on_desk(roots)
print("drives on the desk:", " ".join(sorted(found)) or "none")

gadgets = {desk_drives._disk_number(i) for i in desk_drives._disk_interfaces()
           if "PROD_FILE-STOR_GADGET" in i.upper()}
gadget_roots = {r for r in roots if desk_drives._disk_number("\\\\.\\" + r[0] + ":") in gadgets}
print("drives on a File-Stor Gadget disk:", " ".join(sorted(gadget_roots)) or "none")

check_true("the desk has a drive letter", found, "is the desk attached?")
check("it is the File-Stor Gadget's letter, and no other drive is", found, gadget_roots)

sys.exit(_testkit.finish())
