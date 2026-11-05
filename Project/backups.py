"""
AccessCaster - the Backups page: one backup, both mechanisms underneath.

A backup is a profile on this computer, paired with the desk's own show on its drive when there is
one. The page lists, restores and deletes them as one thing, and the factory reset keeps a backup
of the factory pads it leaves.
"""

from __future__ import annotations

import desk_access
import devices
import faders
import pad_shortcuts
import profile_actions
import profiles
import rcp_read
import recorder
import show_actions
import shows
import soundpad
import wording
from paths import safe_name


def _removable_volume_ready() -> bool:
    """Is there a card or USB drive in the unit with room for a show?

    A show can ONLY go to removable storage: measured, every export takes about 24 MB off the USB
    drive and leaves the internal volume untouched, and that internal volume is the SMART pad
    partition, which reports it cannot even record. So with nothing inserted there is nowhere for a
    backup to go, and asking for one times out rather than failing cleanly.

    ANSWERED BY `recorder.destination`, LIKE EVERY OTHER CALLER. A hand-rolled inserted/mounted/
    can_record loop here would be a SECOND OPINION on whether a backup is possible, and the
    Backups page, the Recording section and this could disagree. Deliberately NOT fresh: the short-lived cache is what keeps this cheap
    enough to ask on a page load."""
    try:
        with desk_access.hid_lock:
            blob = rcp_read.read_model()
        return bool(recorder.destination(recorder.read_volumes(blob)))
    except Exception:
        return False


def action_backup_now(name: str) -> dict:
    """BACK THE UNIT UP: one name, one button, and both mechanisms underneath.

    THE OBVIOUS ALTERNATIVE LOOKS TIDIER AND IS WORSE: offering shows OR profiles depending on
    whether a drive is in. It fails two ways: pull the drive to offload recordings and the user's
    whole backup history vanishes from the page (and dies for good if they format it), and a
    shows-only list cannot be named, because the unit names every export after the loaded show and
    offers no rename, so it reads as a column of identical lines.

    So: export a show when there is somewhere to put one, ALWAYS save a profile, and pair them. The
    user types one name and never learns there are two mechanisms. With no removable storage the
    profile goes on alone and nothing announces a missing feature.

    The show is exported FIRST, while the desk still holds exactly what is being named."""
    safe = safe_name(name)
    if not safe:
        return {"ok": False, "message": "Backup name must be 1 to 40 letters, digits, "
                                        "spaces, dashes or underscores."}

    return profile_actions.save_job.start(lambda job: backup_unit(job, safe),
                                          message="Backing up, please wait.",
                                          started="Backing up.")


def backup_unit(job, safe: str) -> dict:
    """The body of a backup, run inside `job`: the unit's own copy when there is a drive, then the
    settings and pads on this computer. See `action_backup_now`."""
    show = None
    note = ""
    if "shows" in devices.get_active_device().caps and _removable_volume_ready():
        got = show_actions.export_and_wait(job.step, "Backing the desk up to its own drive...")
        if got.get("ok"):
            s = got["show"]
            show = {"snapshot": s.get("snapshot"), "uuid": s.get("uuid"), "name": s.get("name")}
        else:
            # The profile is still worth having, so carry on rather than losing the lot. Say so
            # plainly: a backup that quietly did half of what was asked is worse than a slow one.
            note = (" The desk's own backup did not appear on the drive, so this is a settings "
                    "and pads backup only. Check there is a drive in with room on it.")
    res = profile_actions.save_desk(safe, True, job.tick, show=show)
    if not res.get("ok"):
        return res
    res["message"] = _backup_wording(safe, show) + note
    return res


def _backup_wording(safe: str, show) -> str:
    """What a finished backup says. A restore always reads the copy on this computer, so that is
    what both wordings lead with; the unit's own copy is an extra, and its absence is not a fault."""
    if show:
        return (f"Backed up as {safe}: your settings and pads on this computer, plus a copy of "
                f"the whole desk on its own drive.")
    return f"Backed up as {safe}: your settings and pads, on this computer."


def _restore_silence_warnings(prof, blob) -> list:
    """What this backup would leave you unable to hear, worked out BEFORE anything is sent.

    THE ONE WARNING THAT HAS TO ARRIVE EARLY. Everything the app says after a restore is spoken
    through the desk, so a restore that silences an output silences its own explanation: lose Main
    Stereo and the announcement telling you so goes with it. The confirmation dialog is read while
    the audio still works, and it can still be declined, so this is the only place a warning about
    silence is any use.

    TWO WAYS A BACKUP LEAVES A CHANNEL SILENT, and both are invisible afterwards:
      * it MOVES the channel to a different fader, and the level belongs to the fader rather than
        the channel, so it inherits whatever that strip was sitting at;
      * it sets that fader's level to zero outright.

    A backup with no `fader_levels` at all is the worse case, and most older ones are like that: it
    rearranges the layout and then has nothing to say about the levels it scrambled.
    """
    out = []
    try:
        spec = faders.active_faders()
        routing = prof.get("routing") or {}
        want = routing.get("faders")
        want_levels = routing.get("fader_levels")
        now_vec = faders.read_fader_vector(blob, spec)
        now_levels = rcp_read.values(blob, "faderLevel")

        def level_of(i, table):
            return table[i] if table and i < len(table) and isinstance(table[i], int) else None

        for i in spec.level_faders:
            code = want[i] if want and i < len(want) else (now_vec[i] if i < len(now_vec) else None)
            if code is None or code < 0:
                continue
            after = level_of(i, want_levels) if want_levels else level_of(i, now_levels)
            if after == 0:
                out.append(f"{faders.channel_name(code, spec)} will be silent: it ends up on "
                           f"{faders.fader_label(i, spec)}, which is at zero.")
        if want and list(want[:len(now_vec)]) != list(now_vec[:len(want)]):
            movers = [faders.channel_name(c, spec) for i, c in enumerate(want)
                      if i < len(now_vec) and c != now_vec[i] and c is not None and c >= 0]
            if movers:
                out.append("It rearranges the faders, moving "
                           + wording.join_and(sorted(set(movers))) + ".")
            if not want_levels:
                out.append("It does not set fader levels, so each channel keeps whatever level "
                           "the fader it lands on is already at.")
    except Exception:
        return out          # a warning that crashes must never block the restore it describes
    return out


def backup_restore_plan(name: str) -> dict:
    """How this backup WOULD be restored, without restoring it, so the confirmation can say what is
    about to happen, in the user's terms, BEFORE the user commits."""
    if name.startswith("show:"):
        # One the unit made, with no profile beside it: there is only one way to put it back, and
        # NOTHING CAN BE SAID ABOUT WHAT IS IN IT. The show file is the desk's own and we cannot
        # read it, so the honest answer is to say so rather than to imply we checked.
        return {"ok": True, "name": name, "via": "show", "paired": True,
                "seconds_per_pad": profile_actions.PAD_RESTORE_SECONDS, "pads": 0,
                "warnings": ["This backup was made by the desk itself, so there is no way to "
                             "tell you what it contains before it is restored. It may move "
                             "channels between faders or change their levels."]}
    loaded = profiles.load(name)
    if not loaded.get("ok"):
        return loaded
    prof = loaded["prof"]
    paired = prof.get(profiles.SHOW_KEY) or None
    blob = None
    try:
        with desk_access.hid_lock:
            blob = rcp_read.read_model()
    except Exception:
        blob = None
    # THE PROFILE IS READ EVEN WHEN THE SHOW WILL BE USED. The two halves were saved in the same
    # instant, so the profile is the only readable description of what the show is about to do.
    warnings = _restore_silence_warnings(prof, blob) if blob is not None else []
    return {
        "ok": True,
        "name": loaded["safe"],
        "via": "profile",             # always; see action_restore_backup
        "paired": bool(paired),
        "seconds_per_pad": profile_actions.PAD_RESTORE_SECONDS,
        "pads": len(prof.get("pads") or []),
        "warnings": warnings,
    }


def read_backups() -> dict:
    """THE ONE LIST BEHIND THE BACKUPS PAGE: every backup this desk can be put back from, whichever
    mechanism happens to hold it.

    The user types one name, presses one button and never learns there are two mechanisms; this is
    the read that lets the page keep that promise. Three kinds come out of it, and the kind is the
    page's business, not the user's:
      * a PAIRED backup - a profile on this computer plus the unit's own show on the drive. It is
        restored from the profile like any other (action_restore_backup); the show is a spare copy.
      * a PROFILE-ONLY backup - either there was no storage when it was taken, or the show has since
        been deleted or the drive erased.
      * a UNIT-MADE show with no profile beside it, which is one taken at the touchscreen or by
        RODE's own software. Named by the desk, not by anyone here, so it cannot be renamed.

    ONE DEVICE READ, NOT ONE PER BACKUP. `backup_restore_plan` answers "is the paired show still
    there" for a single name and reads the model to do it; asking it per profile would cost a dump
    each. The shows are read once and matched by snapshot."""
    names = profiles._list_profile_names()
    dev = devices.get_active_device()
    on_drive, storage, note = [], False, ""
    if "shows" in dev.caps:
        try:
            with desk_access.hid_lock:
                blob = rcp_read.read_model()
            on_drive = shows.read_shows(blob)
            storage = bool(recorder.destination(recorder.read_volumes(blob)))
        except Exception as exc:
            note = f"The desk's own backups could not be read: {exc}"
    by_snapshot = {s.get("snapshot"): s for s in on_drive}

    out, claimed = [], set()
    for safe in names:
        loaded = profiles.load(safe)
        if not loaded.get("ok"):
            continue
        prof = loaded["prof"]
        paired = prof.get(profiles.SHOW_KEY) or None
        snap = paired.get("snapshot") if paired else None
        show = by_snapshot.get(snap) if snap is not None else None
        if show is not None:
            claimed.add(snap)
        out.append({
            "key": "profile:" + safe,
            "name": safe,
            "kind": "paired" if show else "profile",
            "pads": len(prof.get("pads") or []),
            "saved_at": prof.get("saved_at"),
            "baseline": bool(prof.get("baseline")),
            "renameable": True,
            "snapshot": snap if show else None,
        })
    for s in on_drive:
        if s.get("snapshot") in claimed:
            continue
        out.append({
            # The DESK named this one, after whichever show was loaded, and it offers no rename, so
            # several can share a name. The modification date is the only thing that tells them
            # apart, which is why it is part of the label rather than a detail line.
            "key": "show:%s" % s.get("snapshot"),
            "name": s.get("name") or "Made on the desk",
            "kind": "unit",
            "pads": None,
            "saved_at": None,
            "modified": s.get("modified"),
            "baseline": False,
            "renameable": False,
            "snapshot": s.get("snapshot"),
        })
    return {"ok": True, "backups": out, "storage": storage, "shows_capable": "shows" in dev.caps,
            "note": note}


def action_delete_backup(name: str, force: bool = False) -> dict:
    """Delete a backup, BOTH HALVES OF IT. `name` is a profile name, or "show:<snapshot>" for one
    the unit made with no profile beside it.

    The page offers one Delete because the user made one backup; leaving the unit's copy behind
    would quietly fill the drive with 24 MB files they believe they have deleted. The show goes
    first: if that fails there is still a profile listed, whereas the reverse leaves an orphan
    nobody can name."""
    if name.startswith("show:"):
        try:
            snap = int(name.split(":", 1)[1])
        except ValueError:
            return {"ok": False, "message": "That backup could not be identified."}
        return show_actions.action_show_delete(snap)
    loaded = profiles.load(name)
    if not loaded.get("ok"):
        return loaded
    # THE AS-FOUND BACKUP'S TWO-STEP SURVIVES THIS ROUTE, and it is asked BEFORE the unit's own
    # copy is touched. `profiles.delete` refuses the baseline without `force` and the page arms the
    # second press from that refusal; deleting the show first and then hitting the refusal would
    # destroy half of the one record of how the desk arrived, while reporting that nothing happened.
    if not force and profiles.is_baseline(loaded["prof"]):
        return profiles.delete(name, force=False)
    paired = (loaded["prof"].get(profiles.SHOW_KEY) or {}).get("snapshot")
    if paired is not None and "shows" in devices.get_active_device().caps:
        try:
            with desk_access.hid_lock:
                blob = rcp_read.read_model()
            still_there = shows.find_by_snapshot(shows.read_shows(blob), paired)
        except Exception:
            still_there = None
        if still_there is not None:
            # BOTH HALVES IN ONE JOB, so the page shows one progress dialog and one result. `via`
            # tells it which job state to watch, exactly as the restore does.
            res = _delete_show_then_profile(paired, name)
            res["via"] = "show"
            return res
    res = profile_actions.delete_profile(name, force=True)
    res["via"] = "profile"
    return res


def action_restore_backup(name: str, clean: bool = False) -> dict:
    """RESTORE A BACKUP, from this computer's copy. See RESTORE_USES_THE_PROFILE below.

    THE SHOW IMPORT CRASHES THE DESK ABOUT ONE TIME IN SIX, so this does not use it. Measured on a
    Pro II with the diagnostic log running:

        PROFILE restores :   9   failed: 0   median 126s
        SHOW imports     :  31   failed: 5   median   6s

    Five crashes, on three different backups, with Bluetooth connected and disconnected. The desk
    hangs inside its own firmware, keeps a stuck audio buffer looping on the headphone output, stays
    electrically present on USB for another minute, and then needs its MAINS pulled - a USB replug
    does not bring it back, because the link is not the problem. Nothing we send is wrong and no
    change to our code prevents it; avoiding the path is the only fix available to us.

    WHY THAT IS WORTH THE COST, and the cost is real: the profile writes back 148 of 182 writable
    properties one at a time and takes two minutes against six seconds. What it misses that anyone
    would notice is the monitor output level and the auto-duck depth. Bluetooth pairing, the
    headline reason for shows existing at all, is NOT carried by a show either.
    Against that: the desk IS the screen reader's audio device for the people this app is for, so a
    desk that stops responding takes their speech with it, and an NVDA add-on that can escape to
    another output is not something everyone has.

    THE EXPORT HALF STAYS. It is the import that crashes; backing up to a show has never
    misbehaved, costs seconds, and leaves a complete copy on the drive if it is ever wanted.

    `name` may also be "show:<snapshot>", which is a backup the unit made with no profile beside it.
    That one has no profile to restore from, so it is the single remaining case that must use the
    import, and it says so before it runs."""
    # THE REPLY SAYS WHICH HALF RAN, in `via`, and the page needs it rather than merely liking it:
    # the two halves report progress through DIFFERENT job states (the show commands share one, the
    # profile save/restore another), so a page that guessed wrong would poll a job that is not
    # running and watch a finished restore in silence.
    if name.startswith("show:"):
        try:
            res = show_actions.action_show_import(int(name.split(":", 1)[1]))
        except ValueError:
            return {"ok": False, "message": "That backup could not be identified."}
        res["via"] = "show"
        return res
    # "RESET THE UNIT TO FACTORY FIRST" IS A PROFILE-RESTORE MODE, so asking for it forces the
    # profile path even where a show is available. The show is an exact copy of the desk, which is
    # the opposite of what this mode is for: it exists to clear everything the profile does NOT
    # cover, so that what is left matches the profile and nothing else survives underneath.
    if clean:
        res = profile_actions.restore_profile(name, clean=True)
        res["via"] = "profile"
        return res
    # RESTORE_USES_THE_PROFILE, never the paired show, however available the show is; the docstring
    # holds the measurement behind that. The pairing is still made and still exported, so backing up
    # is unaffected and a paired backup is not stranded - only which half is read back.
    res = profile_actions.restore_profile(name)
    res["via"] = "profile"
    return res


def factory_backup_name() -> str:
    """The backup a factory reset leaves on this computer, one per model of desk."""
    desk = devices.get_active_device().name.removeprefix("RODECaster ").strip()
    return safe_name(f"Factory pads and settings {desk}"[:40])


def _keep_factory_pads(step) -> str:
    """Back the freshly reset desk up, pads and sounds included, so Restore the pads from a backup
    can put the factory pads back on their own later. The app ships none of RODE's sounds: each
    user's copy comes from their own desk. Saved over the last one, so a firmware update's factory
    pads replace the old ones. Returns a sentence for the reset's reply."""
    name = factory_backup_name()
    res = profile_actions.save_desk(name, include_pads=True, prog=step)
    if not res.get("ok"):
        return (" The factory pads could not be kept on this computer: "
                + (res.get("message") or "the backup failed."))
    return (f" A copy of the factory pads is kept on this computer as {name}, and Restore the pads "
            "from a backup puts them back without resetting anything else.")


def action_show_reset(backup_first: bool = True) -> dict:
    """THE SOFTWARE FACTORY RESET: settings and pads back to factory, without the setup wizard.

    `backup_first` DEFAULTS TO TRUE and the UI does not offer False lightly, because an export is
    the only real undo this has: it costs about a second, and without it there is no way back to
    what the desk was.

    What survives: wifi, language and the analytics answer all live outside the show. Recordings on
    the drive are untouched.
    """
    def work(step):
        saved = None
        if backup_first:
            # REFUSE RATHER THAN CARRY ON if the backup does not land. Asked for a reset WITH a
            # backup, doing the reset anyway would destroy the desk's state having promised not to.
            # The user can still choose the unbacked reset explicitly.
            res = show_actions.export_and_wait(step, "Backing the desk up first, so the reset can be undone...")
            if not res.get("ok"):
                if res.get("no_storage"):
                    return {"ok": False,
                            "message": (res["message"] + " Nothing on the desk has changed. Either "
                                        "put a card or drive in, or choose the reset without a "
                                        "backup, which cannot be undone.")}
                return {"ok": False, "message": show_actions.RESET_STOPPED_NO_BACKUP}
            saved = res["show"]
        done = show_actions.reset_and_wait(step)
        if not done.get("ok"):
            return done
        # REBUILD THE VOICE EFFECTS THE DEFAULT SHOW DOES NOT BRING WITH IT. Without this the reset
        # hands back six voice-FX pads with the right name, type and colour that do nothing at all,
        # and says "factory SMART pads" while it does so - see soundpad.heal_fx_pool for the
        # mechanism and the captures that prove it.
        step("Rebuilding the factory voice effects...")
        with desk_access.hid_lock:
            healed = soundpad.heal_fx_pool(progress=step)
        pad_shortcuts.pads_stale()   # the pad grid was rewritten, so the shortcut layer's is stale
        msg = ("The desk is back to factory settings and factory SMART pads. Your wifi, language "
               "and the analytics answer were not part of it and are untouched, and so are the "
               "recordings on the drive. The monitor speaker level has gone back to its default, "
               "so check it before turning speakers up.")
        if healed.get("message"):
            msg += " " + healed["message"]
        msg += _keep_factory_pads(step)
        if saved:
            msg += f" To undo this, restore \"{saved['name']}\" from the Shows list."
        return {"ok": True, "message": msg}
    return show_actions.run_job("Resetting the desk", work)


def _delete_show_then_profile(snapshot: int, name: str):
    """Both halves of one backup, in one job: the unit's copy first, then this computer's.

    THE ORDER IS THE POINT. If the show delete fails, the profile is still listed and the user can
    try again; the other way round leaves 24 MB on the drive under a name nothing on this page knows
    about any more."""
    def work(step):
        res = show_actions.delete_work(snapshot, step)
        step("Deleting this computer's copy...")
        gone = profile_actions.delete_profile(name, force=True)
        if not gone.get("ok"):
            return gone
        tail = (" The desk's own copy went too." if res.get("ok")
                else " The desk's own copy could not be deleted, so it is still on the drive.")
        return {"ok": True, "message": (gone.get("message") or "Deleted.") + tail}
    return show_actions.run_job("Deleting the backup", work)
