"""
AccessCaster - the desk's own shows, the app side: export, import, software factory reset, delete.

The mechanism, the traps and why shows sit alongside profiles rather than replacing them are in
shows.py. What lives here is the gate, the wording, and the rule that no command is ever reported as
having worked until the desk has been read back.
"""

from __future__ import annotations

import announcer
import channels
import desk_access
import desk_live
import devices
import diagnostics
import rcp_read
import recorder
import shows
import wording

# ALL FOUR RUN AS ONE BACKGROUND JOB, sharing one state block, because they are mutually exclusive
# by nature (you cannot import while a reset runs) and because every one of them needs a progress
# dialog: an import and a reset take seconds, and a blind operator staring at a page that has not
# changed cannot tell "working" from "died".
_show_job = desk_access.BackgroundJob("A show command is already running.",
                                      "The show command failed",
                                      "A backup, restore or reset is running on the desk.",
                                      rewrites_desk=True)


def write_gate():
    """SHOWCONTROL, where the consequence of a wrong selector is the worst of any gate: the address
    carries a FACTORY RESET, so a guess could land a reset, or a stray, on some other block.

    BOTH KNOWN DESKS HOLD THIS CAP, so nothing refuses here today. The gate stays because the
    hazard is a property of the ADDRESS and not of any one desk, and the next unknown desk gets the
    same treatment. `DevTools/test_shows_sim.py` proves it with a synthetic capless device."""
    return desk_access.cap_gate("shows", lambda name:
                     f"AccessCaster can list the shows on the {name} but not export, restore or "
                     f"reset them: doing that has only ever been confirmed on a RODECaster Pro 2, "
                     f"and the same control carries the factory reset, so it refuses rather than "
                     f"risk it.")


_NO_BACKUP_STORAGE = ("There is no memory card or USB drive in the desk, so there is nowhere to "
                      "save a backup.")
RESET_STOPPED_NO_BACKUP = ("Stopped before resetting anything, and nothing on the desk has changed: "
                            "the backup did not appear on the drive, so there would have been no "
                            "way back. Check there is a drive in with room on it, and try again.")


# What the Backups page shows from `read_backup_storage` and `read_backups`, for following the
# desk: the shows on the drive, the loaded one, and recordState for a drive going in or out.
SHOW_STATE_PROPS = frozenset(("showName", "showUID", "showUUID", "showSnapshotIndex",
                              "showLastModification", "currentShowName", "currentShowUUID",
                              "recordState"))


def read_backup_storage() -> dict:
    """Whether this desk can make its own backup, and where: the shows cap, and a card or drive to
    write to (`recorder.destination`, the same answer the Recording section gives). Known up front
    so the factory reset's "back the unit up first" is not offered where it cannot work."""
    try:
        with desk_access.hid_lock:
            blob = rcp_read.read_model()
    except Exception as exc:
        return {"ok": False, "message": f"Could not read the desk's storage: {exc}"}
    dest = recorder.destination(recorder.read_volumes(blob))
    return {"ok": True, "writable": "shows" in devices.get_active_device().caps,
            "can_backup": bool(dest),
            "backup_blocked_reason": "" if dest else _NO_BACKUP_STORAGE}


def show_job_status() -> dict:
    """Progress for whichever show command is running. Polled by the page."""
    return _show_job.status()


def run_job(label: str, work) -> dict:
    """Start `work` on a worker thread under the one job state, which all the show commands share
    because they are mutually exclusive. `work` reports through the `step` it is handed."""
    gate = write_gate()
    if gate is not None:
        return gate
    return _show_job.start(lambda job: work(job.step),
                           message=f"{label}, please wait.",
                           started=f"{label}.",
                           failed=f"{label} failed")


def _locked_dump():
    """One fresh model dump, taking the HID lock only for the dump itself.

    Passed to shows.wait_for so a forty-second wait does not hold the lock through its sleeps. See
    the note on shows.wait_for: holding it across the wait blocked the announcer and read as a
    frozen app."""
    with desk_access.hid_lock:
        return rcp_read.read_model_stable(fresh=True)


def _read_shows_now() -> tuple:
    """A FRESH shows list plus the blob it came from. Every command resolves its uid through this
    immediately before writing, because `showUID` is positional and an export renumbers the lot
    (shows.py trap 1)."""
    blob = rcp_read.read_model_stable(fresh=True)
    return shows.read_shows(blob), blob


def export_and_wait(step, label: str) -> dict:
    """Export the desk and WAIT FOR THE FILE TO APPEAR. Shared by every path that needs a backup.

    Identifies the new show by a NEW SNAPSHOT INDEX appearing. It cannot be done by uuid: three
    exports of one desk state all carry the same uuid, so "a uuid we have not seen" never fires on
    the second export and every repeat backup is silently reported as a failure. It is not done by
    "uid 0" either, which is true but would also be satisfied by an export somebody else triggered
    from the touchscreen mid-wait.

    IT REFUSES BEFORE SENDING ANYTHING IF THERE IS NOWHERE TO WRITE, and this is the only place that
    has to, because it is the only place an export is sent from. `read_backup_storage` computes the
    same fact for the page, so the reset dialog can grey out its tick, but a check on that side alone
    leaves every non-UI route asking the impossible: the command goes out, the desk refuses it
    silently, and we poll the whole 136 kB model for up to 40 s waiting for a file that can never
    appear.

    THE LIVE CASE IS A DUO. `clean_restore_reset` calls this unconditionally and the clean-restore
    gate only checks the shows CAPABILITY, which a Duo has, while a Duo has no removable storage at
    all - so a Duo owner choosing a clean profile restore gets a pointless command, a 40 second wait
    and a refusal.

    It also matters for the USB fault: a real export is the prime suspect for taking a desk off the
    bus, so not sending one that cannot succeed removes the risk from every case that was never going
    to work anyway.
    """
    with desk_access.hid_lock:
        before, blob = _read_shows_now()
        dest = recorder.destination(recorder.read_volumes(blob))
        if not dest:
            # `no_storage` so callers can word their own refusal for this specific cause - a clean
            # restore wants to point at the merge restore, which needs no backup at all.
            return {"ok": False, "count": len(before), "no_storage": True,
                    "message": _NO_BACKUP_STORAGE}
        known = {s["snapshot"] for s in before if s.get("snapshot") is not None}
        step(label)
        shows.send_export()

    def appeared(blob):
        for s in shows.read_shows(blob):
            if s.get("snapshot") is not None and s["snapshot"] not in known:
                return s
        return None
    found, _ = shows.wait_for(appeared, read=_locked_dump)
    if not found:
        return {"ok": False, "count": len(before),
                "message": ("The desk took the command but no new backup has appeared on the "
                            "drive. Check there is a drive in and that it has room.")}
    return {"ok": True, "show": found, "count": len(before) + 1}


# How long to listen for the reset's own confirmation. The whole push burst finishes by t+4.96 s on
# a Duo (measured), so this is generous rather than tight - the cost of it being too short is a
# needless fallback dump, and no Pro II figure has been taken yet.
_RESET_PUSH_WAIT_S = 20.0


def reset_and_wait(step) -> dict:
    """Software-factory-reset the desk and wait for the evidence.

    The evidence is the LOADED SHOW BECOMING A DIFFERENT ONE: `newFromDefault` mints a new uuid, so
    a changed uuid is the desk confirming it in the only way it does.

    WE LISTEN FOR IT RATHER THAN ASK FOR IT. Re-dumping the whole property model every 1.5 s until
    the uuid changes is about 136 kB a time, six or seven times over, and it puts our traffic on the
    wire for the whole time the desk is rewriting its configuration and re-enumerating its USB audio
    function. The desk pushes `currentShowUUID` by itself 0.02 s after the command, and its whole
    burst is done by t+4.96 s (measured on a Duo), so the answer arrives sooner with nothing of ours
    in the way.

    THE HID LOCK IS HELD FOR THE WRITE ONLY, not across the wait: listening needs no lock, so an
    announcement or a page read does not queue behind this.

    A READ IS STILL THE FALLBACK. A missed push is not a failed reset, and hidapi makes no promise
    about a frame arriving, so a timeout costs one dump before anything is reported as wrong.
    """
    listener = desk_live.PushListener()
    try:
        with desk_access.hid_lock:
            before_uuid = shows.read_current(rcp_read.read_model_stable(fresh=True)).get("uuid")
            step("Resetting the desk to factory settings and pads...")
            listener.start()        # before the write: the push burst starts 20 ms after it
            shows.send_new_from_default()
        # NEVER LONGER THAN THE SHOW-COMMAND TIMEOUT ITSELF. Two independent waits for one command is
        # one too many: `shows.COMMAND_TIMEOUT_S` is the ceiling for "how long we give the desk", so
        # this listens for the shorter of the two and the offline sim's single knob still governs it.
        after = listener.wait_for("currentShowUUID",
                                  timeout=min(_RESET_PUSH_WAIT_S, shows.COMMAND_TIMEOUT_S),
                                  match=lambda v: v and v != before_uuid)
        listener_lost = listener.lost
        if after:
            # DO NOT ACT ON THE ANSWER THE INSTANT IT ARRIVES. The uuid lands at about t+1.8 s and
            # the desk carries on rewriting itself until t+4.96 s, so the wake below - which WRITES
            # to every mic channel - would otherwise land in the middle of that, and on a reset that
            # is also while the desk re-enumerates USB.
            step("Waiting for the desk to finish...")
            listener.wait_until_quiet()
    finally:
        listener.stop()

    # THE FALLBACK READ ALSO ANSWERS A SECOND QUESTION: is the desk still THERE? "It took the command
    # and did nothing" and "it has left the USB bus" are different failures needing different words:
    # only the second needs the cable unplugged.
    answered = None
    if not after:
        step("Checking the desk...")
        try:
            with desk_access.hid_lock:
                got = shows.read_current(rcp_read.read_model_stable(fresh=True)).get("uuid")
            answered = bool(got)
            after = got if (got and got != before_uuid) else None
        except Exception:
            answered = False

    if not after:
        if answered:
            # The desk is present and talking, so nothing needs replugging and nothing is lost.
            return {"ok": False,
                    "message": ("The desk took the command but its show has not changed, so the "
                                "reset does not look like it happened. Nothing has been lost; "
                                "check the Shows list in a moment.")}
        # Not answering: the command has almost certainly applied, and nothing about it can be
        # verified at all.
        return {"ok": False,
                "message": ("The desk stopped answering before it could confirm the reset"
                            + (", and the connection dropped. " if listener_lost else ". ")
                            + "The reset itself was almost certainly carried out, but nothing can "
                              "be checked while the desk is not responding. Unplug the USB cable "
                              "in the socket nearest the power inlet, wait a few seconds and plug "
                              "it back in, then look at the Shows list. If it still does not come "
                              "back, try that once or twice more, and if it will not appear at "
                              "all, disconnect the power, hold the red button on the back for "
                              "sixty seconds, then reconnect the power supply on its own and "
                              "switch the desk on. Nothing is lost either way.")}
    wake_channels()              # a reset leaves the same silent-channel fault; see wake_channels
    # THE OTHER THING A RESET LEAVES BROKEN - the voice-FX pads' effect definitions - IS HEALED BY
    # THE CALLER, NOT HERE, and deliberately. soundpad.heal_fx_pool rebuilds six pads at about
    # fifteen seconds each, and a CLEAN PROFILE RESTORE is about to rewrite the whole pad grid
    # anyway, so healing here would spend ninety seconds building pads that are cleared moments
    # later. Each caller runs it once its own pad phase is finished; it is a no-op when nothing is
    # missing, so the ordering costs nothing when the profile did carry the pads.
    return {"ok": True}


def clean_restore_reset(step) -> dict:
    """The reset half of a CLEAN profile restore: back the desk up, then reset it.

    REFUSES TO RESET IF THE BACKUP DID NOT LAND. A clean restore is chosen precisely because it
    destroys what it does not replace, so proceeding without the undo would be the one outcome
    nobody asked for."""
    saved = export_and_wait(step, "Backing the desk up, so the reset can be undone...")
    if not saved.get("ok"):
        if saved.get("no_storage"):
            # NAME THE OTHER OPTION. This is the Duo's permanent case, not a "put a drive in and try
            # again" case, so telling them to find storage would be telling them to buy hardware.
            return {"ok": False,
                    "message": (saved["message"] + " A clean restore has to back the desk up first, "
                                "because it wipes anything the profile does not cover, so it cannot "
                                "be done. Choose the ordinary restore instead: it puts your settings "
                                "back and leaves everything else as it is.")}
        return {"ok": False, "message": RESET_STOPPED_NO_BACKUP}
    done = reset_and_wait(step)
    if not done.get("ok"):
        return done
    return {"ok": True,
            "message": (f"Reset the desk to factory settings first, after backing it up as "
                        f"\"{saved['show']['name']}\".")}


def action_show_import(snapshot: int, as_name: str = "") -> dict:
    """Restore the whole desk from an exported show.

    `as_name` IS THE NAME THE USER GAVE IT, and it exists because the unit does not keep one: every
    export is named after whichever show was loaded, so a backup saved as "Testing New Backup Flow"
    is called "Default Show" on the drive. Reporting the desk's name therefore tells the user they
    have restored something they never chose, which is the worst sentence a restore can produce.
    The Backups page passes the name it holds; the Shows list has none to pass and falls back to
    the desk's.

    TAKES A SNAPSHOT INDEX, NOT A UID AND NOT A UUID. A uid is a position that any export renumbers,
    so a page holding one is holding something that may already mean a different file; a uuid names
    the SHOW rather than the file, so several backups can share one. The snapshot index is the only
    stable per-file handle (shows.py trap 2), and it is resolved to a uid against a read taken
    moments before the write, because the protocol only accepts a uid."""
    try:
        snapshot = int(snapshot)
    except (TypeError, ValueError):
        return {"ok": False, "message": "Choose which backup to restore."}

    def work(step):
        with desk_access.hid_lock:
            current, blob = _read_shows_now()
            match = shows.find_by_snapshot(current, snapshot)
            if match is None:
                return {"ok": False,
                        "message": "That backup is no longer on the drive. The list has been "
                                   "refreshed."}
            # NO UUID PRE-CHECK. Refusing a backup whose uuid matches the loaded show assumes the
            # unit would reject it, and THE UNIT DOES NOT: a backup sharing the loaded uuid
            # restores, with showControlLastError staying 0. Since a uuid only changes on a factory
            # reset, that refusal blocks the ordinary case this feature exists for - change
            # something, put it back - and leaves shows usable only straight after a reset. The unit's own refusal
            # (error 1) is reported below; we simply never guess it in advance.
            before = shows.restore_fingerprint(blob)
            err_before = shows.read_last_error(blob)
            shown = as_name or match["name"]     # see the docstring: the user's name wins
            step(f"Restoring \"{shown}\". This also moves the monitor level.")
            shows.send_import(match["uid"])
        target_uuid = match.get("uuid")
        loaded_uuid = shows.read_current(blob).get("uuid")

        # THE EVIDENCE IS THE STATE MOVING, not the uuid: see shows.restore_fingerprint for why
        # every other signal fails. The uuid test is kept as a second route because it is the one
        # that fires when the backup IS a different show (the undo for a factory reset).
        # ONLY A CODE THAT CHANGED IS THIS IMPORT'S ANSWER. The code latches, so one left by an
        # earlier refusal reads non-zero from the start; a refusal then shows as nothing moving, and
        # the timeout branch below reports it from the code still standing.
        def settled(blob):
            err = shows.read_last_error(blob)
            if err and err != err_before:
                return {"error": err}
            if target_uuid != loaded_uuid and shows.read_current(blob).get("uuid") == target_uuid:
                return {"done": True}
            if shows.restore_fingerprint(blob) != before:
                return {"done": True}
            return None
        got, blob_after = shows.wait_for(settled, read=_locked_dump)
        if got and got.get("error"):
            return {"ok": False, "message": shows.error_message(got["error"])}
        tail = ("Everything is back: settings, routing, the fader layout and the SMART pads. The "
                "monitor speaker level will have moved, so check it before turning speakers up.")
        if not got:
            # THE DESK NEVER ANSWERED AT ALL, which is a different outcome from "it did nothing"
            # and has to be said differently. `shows.wait_for` hands back no blob when not one
            # usable dump arrived in the whole wait, and that is what a desk leaving the bus
            # mid-restore looks like from here: it stops answering about 30 s after the import
            # command and Windows drops it a minute later.
            if blob_after is None:
                return {"ok": False,
                        "message": ("The desk stopped responding during the restore and is no "
                                    "longer answering. Unplug its USB cable and plug it back in, "
                                    "then check the Device page before trusting any setting.")}
            # Nothing moved and the unit raised no error. That is only benign for a backup of the
            # SAME show, where "nothing to do" is a real and reportable outcome rather than a
            # failure - saying "it did not work" would send someone hunting a problem that is not
            # there. For a DIFFERENT show it is a genuine failure, because a successful import
            # would have changed the loaded uuid and it did not.
            # A success clears the code to 0, so one still standing is a refusal.
            err_after = shows.read_last_error(blob_after)
            if err_after:
                return {"ok": False, "message": shows.error_message(err_after)}
            if target_uuid == loaded_uuid:
                return {"ok": True,
                        "message": (f"The desk already matched \"{shown}\", so nothing "
                                    f"needed to change.")}
            return {"ok": False,
                    "message": (f"The desk took the command but has not loaded "
                                f"\"{shown}\" yet. Check the list in a moment.")}
        # THE ENGINE CAN COME BACK WITH A CHANNEL SILENT. See wake_channels: the model looks
        # perfect, so this cannot be conditional on detecting anything.
        wake_channels()
        return {"ok": True, "message": f"Restored \"{shown}\". {tail}"}
    return run_job("Restoring the show", work)


def wake_channels() -> dict:
    """Force the audio engine to re-latch every mic channel, after a reset or an import.

    THE FAULT THIS EXISTS FOR: after a software factory reset followed by a show import, a mic
    channel can be COMPLETELY SILENT while the control model matches its working state exactly:
    level, chain, mute, gain and phantom all correct, and no audio. So NOTHING CAN DETECT THIS BY
    READING THE DESK, and any fix that displays a value is wasted effort.

    WHAT WAKES IT IS A REAL VALUE CHANGE, and only that. Writing the gain back at its CURRENT value
    does nothing at all (measured), which is why the desk's own encoder recovers it: an encoder
    sends a genuine change. So this nudges the gain by
    a step and puts it straight back.

    Kept to MIC channels because that is where a dead input is silently catastrophic and where we
    have a cheap real-value write. Whether the fault can hit a non-mic channel is UNKNOWN.
    """
    gate = desk_access.cap_gate("input_mic_write",
                     lambda name: f"Cannot wake the channels on a {name}.")
    if gate is not None:
        return gate
    # THE GAIN COMES FROM A FRESH READ, NOT FROM THE DESK MODEL. The "put it straight back" half of
    # the nudge has to write the value the desk currently holds: the only callers are a reset and
    # an import, exactly the two moments the cache is guaranteed stale. Sourcing it from the cache
    # would write the gain the app last displayed over the one the restore just put back.
    try:
        with desk_access.hid_lock:
            blob = rcp_read.read_model_stable(fresh=True)
    except Exception as exc:
        diagnostics.event("wake.read_failed", str(exc))
        blob = None
    if not rcp_read.is_model_dump(blob):
        return {"ok": False, "woken": [],
                "message": "The desk is not answering, so its mic channels could not be woken."}
    woken = []
    for ch in channels.MIC_CHANNELS:
        try:
            now = channels.mic_gain(blob, ch)
            if now is None:
                continue
            # A step DOWN unless that would fall off the bottom, so the nudge is always a real
            # change. Putting the original back immediately means the user never hears the step.
            other = now - 1 if now > channels.GAIN_MIN_DB else now + 1
            with desk_access.hid_lock:
                channels.set_mic_gain(ch, other)
                channels.set_mic_gain(ch, now)
            announcer.mark_self_gain(ch, True)
            woken.append(ch)
        except Exception as exc:          # one channel failing must not leave the rest unwoken
            diagnostics.event("wake.channel_failed", str(exc), mic=ch)
    return {"ok": True, "woken": woken,
            "message": (f"Nudged {wording.count(len(woken), 'mic channel')} so the desk's audio "
                        f"engine picks them up again." if woken
                        else "No mic channels needed waking.")}


def action_show_delete(snapshot: int) -> dict:
    """Delete one exported backup from the drive. By snapshot index (see action_show_import).

    Deleting the backup that happens to hold the LOADED show is ALLOWED. Refusing it treats the uuid
    as a file identity, which it is not (shows.py trap 2), so with several backups of the current
    state such a rule refuses to delete ANY of them - exactly the case where the drive fills up. A
    show FILE is not the loaded state, and deleting one does not disturb the desk."""
    try:
        snapshot = int(snapshot)
    except (TypeError, ValueError):
        return {"ok": False, "message": "Choose which backup to delete."}

    return run_job("Deleting the backup",
                         lambda step: delete_work(snapshot, step))


def delete_work(snapshot: int, step) -> dict:
    """Take one backup off the drive and wait for it to be gone.

    RUNS INSIDE A SHOW JOB THAT THE CALLER OWNS, which is the whole reason it is not just the body
    of the function above: the unified delete has to remove the unit's copy AND this computer's in
    ONE job, so the user gets one progress dialog and one result for one backup."""
    with desk_access.hid_lock:
        current, blob = _read_shows_now()
        match = shows.find_by_snapshot(current, snapshot)
        if match is None:
            return {"ok": False,
                    "message": "That backup is no longer on the drive. The list has been "
                               "refreshed."}
        step(f"Deleting \"{match['name']}\"...")
        shows.send_delete(match["uid"])
    # The evidence is that SNAPSHOT INDEX leaving the list. A count drop alone would be fooled
    # by anything else removing a backup at the same moment.
    def gone(blob):
        return True if shows.find_by_snapshot(shows.read_shows(blob), snapshot) is None else None
    done, _ = shows.wait_for(gone, read=_locked_dump)
    if not done:
        return {"ok": False,
                "message": f"The desk took the command but \"{match['name']}\" is still on the "
                           f"drive."}
    return {"ok": True, "message": f"Deleted \"{match['name']}\" and gave the space back."}
