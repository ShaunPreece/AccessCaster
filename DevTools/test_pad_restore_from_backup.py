"""Restoring only the SMART pads from a backup (server.action_restore_backup_pads, pad_backups),
offline: the backup's own pad folder is restored in one transfer session, a pad the backup has
empty is cleared, nothing but pads is written, and a backup from another model of desk or with no
pads is refused before anything is sent. The chooser lists only backups holding pads from this
model of desk, newest first. The desk and the profile store are replaced: nothing touches either.

Run: python DevTools/test_pad_restore_from_backup.py
"""
from __future__ import annotations

import contextlib
import sys
import time

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
import devices
import output_mixes
import pad_actions
import pad_shortcuts
import profiles
import rcp_transfer
import server
import soundpad
from _testkit import as_device, check, check_in, check_true, patched

PADS = [{"grid": 1, "name": "Applause"}, {"grid": 3, "name": "Robot"}]
STORE = {
    "Show night": {"pads": PADS, "product_id": devices.PRO2.product_id, "saved_at": 200.0},
    "Old": {"pads": PADS[:1], "saved_at": 100.0},
    "Settings only": {"pads": [], "product_id": devices.PRO2.product_id, "saved_at": 300.0},
    "Duo": {"pads": PADS, "product_id": devices.DUO.product_id, "device_name": "RODECaster Duo",
            "saved_at": 400.0},
}


def load(name):
    if name not in STORE:
        return {"ok": False, "message": f"Profile {name} not found."}
    return {"ok": True, "safe": name, "prof": STORE[name]}


def wait_for_job():
    for _ in range(100):
        st = pad_actions.restore_status()
        if st["done"] and not st["running"]:
            return st
        time.sleep(0.05)
    return pad_actions.restore_status()


calls = []


@contextlib.contextmanager
def session():
    calls.append("session open")
    yield
    calls.append("session closed")


def restore_pads(pads, src_dir, progress=None):
    calls.append(("restore", [p["grid"] for p in pads], src_dir))
    return {"results": [{"ok": True}, {"ok": True, "unchanged": True}], "unchanged": [3]}


fakes = (
    (profiles, {"load": load, "list_profiles": lambda: {"ok": True, "profiles": list(STORE)},
                "apply": lambda *a, **k: calls.append("settings")}),
    (rcp_transfer, {"transfer_session": session}),
    (soundpad, {"restore_pads": restore_pads,
                "clear_pads_not_in": lambda pads, progress=None: calls.append("clear extras")
                or {"cleared": [2], "failed": []},
                "tidy_unused_pad_audio": lambda progress=None: calls.append("tidy") or {}}),
    (pad_shortcuts, {"pads_stale": lambda: calls.append("shortcuts stale")}),
    (output_mixes, {"apply_matrix": lambda *a: calls.append("matrix")}),
)

with as_device(devices.PRO2), patched(*fakes):
    listed = server.pad_backups()
    check("only backups with pads from this model of desk, newest first",
          [b["name"] for b in listed["backups"]], ["Show night", "Old"])

    reply = server.action_restore_backup_pads("Duo")
    check_true("a backup from another desk is refused before anything is sent",
               not reply["ok"] and "Duo" in reply["message"] and not calls)
    reply = server.action_restore_backup_pads("Settings only")
    check_true("a backup with no pads is refused", not reply["ok"] and not calls)

    reply = server.action_restore_backup_pads("Show night")
    check_true("a backup with pads starts", reply["ok"])
    st = wait_for_job()
    check("the pads are restored from the backup's own folder in one session, then the extras "
          "cleared outside it and the audio tidied",
          calls, ["session open", ("restore", [1, 3], profiles.pads_dir("Show night")),
                  "session closed", "clear extras", "tidy", "shortcuts stale"])
    check_true("it finished", st["ok"])
    check_in("the reply says nothing else changed", "nothing else on the desk was changed",
             st["message"])
    check_in("and what it did to the pads", "1 of 2 were already exactly as saved",
             st["message"])
    check_in("and the pad it emptied", "Cleared pad 2, empty in the profile", st["message"])

sys.exit(_testkit.finish())
