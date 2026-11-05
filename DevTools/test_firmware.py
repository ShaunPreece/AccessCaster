"""Offline: the app side of a firmware update (`firmware`): it refuses while another job is part-way
through, and an install whose backup fails never reaches the desk. No device needed.

Run: python DevTools/test_firmware.py
"""
from __future__ import annotations

import sys
import time

import _bootstrap  # noqa: F401  (puts Project/ on sys.path; import FIRST)
import _testkit
import desk_access
import desk_firmware
import fake_model
import firmware
import rcp_read
import recorder
from _testkit import check, check_in, patched


def desk(online=True):
    """A desk offering 1.7.7, on wifi or not, recording nothing."""
    rows = [("SYSTEM", name, False) for name in desk_firmware.PROPS
            if name not in ("systemFirmwareVersion", "osUpdateAvailable", "updateVersion")]
    rows += [("SYSTEM", "systemFirmwareVersion", "1.7.6"), ("SYSTEM", "osUpdateAvailable", True),
             ("SYSTEM", "updateVersion", "1.7.7"),
             ("NETWORK", "wifiIpAddress", "192.168.1.20" if online else ""),
             ("RECORDER", "recordState", recorder.READY)]
    return fake_model.encode(rows)


def reading(blob):
    return (rcp_read, {"read_model_stable": lambda *a, **k: blob})


print("an update that can run is offered")
with patched(reading(desk()), (desk_access, {"work_in_progress": lambda: None})):
    ready = firmware.read_install()
check("nothing refuses it", ready["refusal"], "")
check("the offered version", ready["version"], "1.7.7")
check("the version it is on", ready["firmware"], "1.7.6")

print("another job part-way through refuses it, in that job's words")
with patched(reading(desk()),
             (desk_access, {"work_in_progress": lambda: "A restore is running."})):
    ready = firmware.read_install()
check("the job is named", ready["refusal"], "A restore is running. Try again when it finishes.")

print("the desk's own refusal comes first")
with patched(reading(desk(online=False)),
             (desk_access, {"work_in_progress": lambda: "A restore is running."})):
    ready = firmware.read_install()
check_in("offline is the reason given", "not connected to the internet", ready["refusal"])

print("a failed backup stops the install before anything is written")
installs = []
with patched(reading(desk()), (desk_access, {"work_in_progress": lambda: None}),
             (desk_firmware, {"install": lambda *a: installs.append(a) or {"ok": True}}),
             (firmware, {"backup": lambda job, name: {"ok": False, "message": "No room."}})):
    started = firmware.action_install(backup_first=True)
    deadline = time.monotonic() + 5
    while firmware.job_status().get("running") and time.monotonic() < deadline:
        time.sleep(0.01)
    status = firmware.job_status()
check("the job started", started.get("started"), True)
check("the desk was never asked to install", installs, [])
check("the job failed", status.get("ok"), False)
check_in("and says the update was not started", "the update was not started", status["message"])
check_in("with the backup's own reason", "No room.", status["message"])

sys.exit(_testkit.finish())
