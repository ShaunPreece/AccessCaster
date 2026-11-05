"""
AccessCaster - the desk's firmware on Device > Hardware: checking RODE's server for an update, and
installing one. The desk does the work through its own updater, driven by desk_firmware.py; this
is the app side: the refusals, the backup first, the job and the wording.
"""

from __future__ import annotations

import backups
import callme
import desk_access
import desk_firmware
import desk_live
import model_tree
import network
import rcp_read
import rcp_transfer
import recorder

_UPDATE_HOW = "Install it with Install firmware, under Device, Hardware."

# The backup an update takes first, replaced by the next update's.
BACKUP_NAME = "Before firmware update"

_job = desk_access.BackgroundJob("A firmware update is already running.",
                                 "The firmware update failed",
                                 "A firmware update is running.")


def _update_sentence(upd: dict) -> str:
    if upd.get("available"):
        what = f"Firmware {upd['version']}" if upd.get("version") else "A firmware update"
        return f"{what} is available. {_UPDATE_HOW}"
    if upd.get("no_internet"):
        return ("The desk could not reach RODE's update server. It needs wifi or a network cable "
                "to check.")
    return ""


def read_install() -> dict:
    """Whether the unit's waiting firmware update can be installed now: `version`, and `refusal`
    as a sentence when it cannot."""
    try:
        with desk_access.hid_lock:
            blob = rcp_read.read_model_stable(fresh=True)
    except Exception as exc:
        return {"ok": False, "message": f"Could not read the desk: {exc}"}
    state = desk_firmware.read(blob)
    recording = recorder.read_state(blob) in (recorder.RECORDING, recorder.PAUSED)
    try:
        in_call = any(slot.get("state") not in (None, "Idle")
                      for slot in callme.read(blob).get("slots") or [])
    except Exception as exc:
        # An update restarts the desk, so a call that cannot be ruled out refuses it.
        return {"ok": False, "message": f"Could not tell whether a CallMe call is up: {exc}"}
    refusal = desk_firmware.refusal(state, network.connection(blob)["online"], recording, in_call)
    busy = desk_access.work_in_progress()
    if not refusal and busy:
        refusal = f"{busy} Try again when it finishes."
    return {"ok": True, "version": desk_firmware.offered(state),
            "firmware": desk_firmware.version(blob) or "", "refusal": refusal}


def action_install(backup_first: bool = True) -> dict:
    """Install the firmware update the unit offers: a backup first, unless declined, then the
    unit's own download, install and restart (desk_firmware.install). Refuses before anything is
    written if the update cannot run now."""
    ready = read_install()
    if not ready.get("ok"):
        return ready
    if ready["refusal"]:
        return {"ok": False, "message": ready["refusal"]}
    target = ready["version"]

    def work(job):
        if backup_first:
            backed = backups.backup_unit(job, BACKUP_NAME)
            if not backed.get("ok"):
                return {"ok": False,
                        "message": "The backup did not finish, so the update was not started and "
                                   "nothing on the desk has changed. "
                                   + (backed.get("message") or "")}
        res = desk_firmware.install(desk_firmware.Desk(desk_access.hid_lock), job.step, target)
        if res.get("ok") and backup_first:
            res["message"] += (f" Your settings were backed up first as {BACKUP_NAME}, "
                               "on the Backups page.")
        return res
    return _job.start(work, message=f"Updating the desk to firmware {target}.",
                      started=f"Updating the desk to firmware {target}.")


def job_status() -> dict:
    return _job.status()


def action_check() -> dict:
    """Ask the desk to check RODE's server for a firmware update, and say what it found.

    `updateCheckRequested` is the touchscreen's Check for Update: the desk raises `updateChecking`
    and clears both within about a second."""
    listener = desk_live.PushListener()
    try:
        with desk_access.hid_lock:
            blob = rcp_read.read_model_stable(fresh=True)
            found = model_tree.find_property(model_tree.parse(blob)[0], "updateCheckRequested")
            if not found:
                return {"ok": False, "message": "This desk does not offer a firmware check."}
            path = found[0][0].path()
            listener.start()        # before the write: the desk answers within a second
            rcp_transfer.send_record(
                rcp_transfer.build_path_prop_bool("updateCheckRequested", True, path))
        listener.wait_for("updateChecking", desk_firmware.CHECK_WAIT_S, match=lambda v: v is False)
        with desk_access.hid_lock:
            blob = rcp_read.read_model_stable(fresh=True)
    except Exception as exc:
        return {"ok": False, "message": f"Could not check for a firmware update: {exc}"}
    finally:
        listener.stop()
    upd = desk_firmware.update(blob)
    fw = desk_firmware.version(blob) or "unknown"
    msg = (_update_sentence(upd)
           or f"Your desk is up to date. Firmware {fw} is the latest.")
    return {"ok": True, "firmware": fw, "firmware_update": upd, "message": msg}
