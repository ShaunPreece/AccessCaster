"""
AccessCaster - settings profiles, the app side: save, restore, list, rename and delete, and putting
a profile's SMART pads back on their own.

The format and the field detail are in profiles.py. This module owns the HID lock around them: it
reads the model under the lock for a save and holds it across the writes for a restore.
"""

from __future__ import annotations

import channels
import desk_access
import desk_firmware
import devices
import faders
import master_outputs
import output_mixes
import pad_actions
import pad_shortcuts
import profiles
import rcp_read
import rcp_transfer
import show_actions
import soundpad
import wording
from paths import safe_name

# Measured, and the ONLY honest basis for the waiting-time the page quotes. A pad
# restore is dominated by moving its audio over transfer mode, and it is per pad, so the total grows
# with the pad count without limit - the page must work it out, not guess a phrase.
PAD_RESTORE_SECONDS = 15
SETTINGS_RESTORE_SECONDS = 25


def _profile_file_job_busy():
    """Message if a save or restore is in flight, else None. Renaming or deleting a profile is
    pure file work, but either job is mid-way through reading or writing that same folder, so the
    file operations stand aside rather than pulling it out from under them."""
    if save_job.busy():
        return "A profile save is running. Try again when it finishes."
    if restore_job.busy():
        return "A profile restore is running. Try again when it finishes."
    return None


def profile_info(name: str) -> dict:
    """What the page needs to describe a profile BEFORE restoring it, above all its pad count: pads
    dominate the time (about 15 seconds each, measured), so a 40-pad profile is a ten-minute job
    rather than the minute or so a settings-only restore takes. Reads the one profile, not the whole
    store."""
    loaded = profiles.load(name)
    if not loaded.get("ok"):
        return loaded
    prof = loaded["prof"]
    return {"ok": True, "name": loaded["safe"], "pads": len(prof.get("pads") or []),
            "saved_at": prof.get("saved_at"), "device_name": prof.get("device_name"),
            "seconds_per_pad": PAD_RESTORE_SECONDS, "settings_seconds": SETTINGS_RESTORE_SECONDS}


def delete_profile(name: str, force: bool = False) -> dict:
    """Delete a saved profile from this computer. No HID lock and no device access.

    `force` is the second step for the as-found baseline only: without it, deleting the baseline
    comes back with baseline: True and a message for the page to confirm against."""
    busy = _profile_file_job_busy()
    return {"ok": False, "message": busy} if busy else profiles.delete(name, force=force)


def rename_profile(name: str, new_name: str) -> dict:
    """Rename a saved profile on this computer. No HID lock and no device access."""
    busy = _profile_file_job_busy()
    return {"ok": False, "message": busy} if busy else profiles.rename(name, new_name)


# A settings-only profile save is quick, but include_pads copies every pad's sound file off the unit
# over transfer mode and takes a minute or more, so it runs as a job and the window polls
# `profile_save_status`.
save_job = desk_access.BackgroundJob("A profile save is already running.",
                                     "Could not save the profile", "A backup is running.")


def profile_save_status() -> dict:
    return save_job.status()


def save_desk(safe: str, include_pads: bool, prog, baseline: bool = False, show=None) -> dict:
    """Read the whole desk under the HID lock, then hand it to profiles.save. With include_pads,
    also back up the SMART pad grid (metadata + copy each sound file off the unit) in the same
    locked session."""
    try:
        with desk_access.hid_lock:
            # PHASES, NOT ITEMS. Everything spoken during a job goes to one dialog's notifier, so
            # each line cuts off the one before it; with a line per pad you hear the first two words of
            # each and nothing else, and the closing line sounds like one more pad. Three lines for
            # the whole job, and the last word of the last one cannot be mistaken for a pad name.
            prog("Backing up the settings.")
            blob = rcp_read.read_model_stable()
            chans = channels.read_channels(blob)
            if include_pads:
                pad_backup = soundpad.backup_pads(profiles.pads_dir(safe), progress=None)
                n = len(pad_backup.get("pads") or [])
                prog(f"Settings done. Backed up {wording.count(n, 'SMART pad')}.")
            else:
                pad_backup = None
    except Exception as exc:
        return {"ok": False, "message": f"Could not read settings from the device: {exc}"}
    firmware = desk_firmware.version(blob)
    dev = devices.get_active_device()
    # capture_length, not count: a profile must capture the WHOLE channelInputSource vector,
    # including any slot the desk does not expose as a fader (both known desks have a 10th, measured),
    # so the saved layout is the real one rather than a truncated view.
    # Capture is deliberately WIDER than write: profiles.apply only writes the first
    # vector_length entries, so the extra slot round-trips into the JSON and is never written back.
    res = profiles.save(safe, blob, chans, firmware, faders.active_faders().capture_length,
                        pads=(pad_backup["pads"] if pad_backup else None),
                        product_id=dev.product_id, device_name=dev.name,
                        headphone_output_count=len(master_outputs.active_headphones().outputs),
                        baseline=baseline, show=show)
    if pad_backup and pad_backup.get("unrecognised"):
        odd = pad_backup["unrecognised"]
        grids = wording.join_and(str(g) for g in odd)
        res["message"] += (f" Note: the voice-FX {wording.plural(len(odd), 'pad')} at grid {grids} "
                           f"carry a name that matches no factory or saved effect (likely "
                           f"renamed), so the effect could not be identified and they will be "
                           f"skipped on restore.")
    return res


def snapshot_profile(name: str, include_pads: bool = False, baseline: bool = False) -> dict:
    """Validate the name, then start the save on a worker thread. The name check stays synchronous
    so a bad name comes straight back as an error instead of through the progress dialog."""
    safe = safe_name(name)
    if not safe:
        return {"ok": False, "message": "Profile name must be 1 to 40 letters, digits, "
                                        "spaces, dashes or underscores."}
    return save_job.start(
        lambda job: save_desk(safe, include_pads, job.tick, baseline=baseline),
        message="Reading the desk's settings, please wait.",
        started="Saving the profile.")


# Profile restore runs in a background thread (the settings, then an optional pad rebuild at about
# PAD_RESTORE_SECONDS a pad), so the page is not blocked and can poll progress for live-region
# announcements.
restore_job = desk_access.BackgroundJob("A profile restore is already running.",
                                        "Could not restore the profile", "A restore is running.",
                                        rewrites_desk=True)


def profile_restore_status() -> dict:
    return restore_job.status()


def restore_profile(name: str, clean: bool = False) -> dict:
    """Start a background restore: apply the settings, then (if the profile carries pads) rebuild
    the SMART pad grid. The window polls `profile_restore_status` for progress. profiles.apply
    and soundpad.restore_pads do the device writes and assume the caller holds the HID lock.

    `clean` PICKS BETWEEN TWO GENUINELY DIFFERENT OPERATIONS, and the page names both rather than
    choosing for the user:

      * MERGE (the default): write the profile's values over
        whatever the desk holds now. Everything the profile does not cover is LEFT ALONE. That is
        the right choice for "put my show back", and it is the only one that preserves settings this
        app cannot write - a paired Bluetooth phone, a pad's custom censor sound, the strip colours.

      * CLEAN: software-factory-reset the desk first (shows.send_new_from_default), then restore on
        top. The desk then matches the profile EXACTLY, because the ~300 properties a profile cannot
        write are at factory defaults rather than at some unknown leftover value. It also clears
        strays and orphan pads, which no profile restore can do. The cost is real and is stated on
        the page: anything the profile does not carry is DESTROYED rather than left, and the pad
        phase gets slower because the reset puts 16 factory pads back for it to replace.

    An export is taken before the reset as the undo, for the reason action_show_reset gives.
    """
    loaded = profiles.load(name)
    if not loaded.get("ok"):
        return loaded
    safe, prof = loaded["safe"], loaded["prof"]
    refusal = _other_desk_refusal(prof)
    if refusal:
        return refusal
    pad_list = prof.get("pads") or []
    # Refuse a clean restore we cannot actually do CLEANLY, rather than silently doing a merge and
    # calling it clean. Someone who asked for an exact match needs to know they did not get one.
    if clean:
        gate = show_actions.write_gate()
        if gate is not None:
            return {"ok": False,
                    "message": ("A clean restore resets the desk first, and that is not available "
                                "on this desk. " + gate["message"] + " Restore without the reset "
                                "instead: it puts your settings back but leaves anything the "
                                "profile does not cover as it is.")}

    def _work(job):
        # TWO PROGRESS CHANNELS, deliberately. The settings phase reports its own sections (it is
        # ~20s of paced writes) so it uses job.step, which does not touch the count; the pad phase
        # is a countable list, so it uses job.tick. The pad phase formats its OWN counts too
        # (soundpad.restore_pads knows how many pads are actually being written, which is not the
        # same as how many the profile holds once the verify pass has spared the ones already
        # correct), so no `phrase` is passed and the message goes through as it comes.
        reset_note = ""
        if clean:
            # THE RESET, AND ITS UNDO, BEFORE ANY PROFILE WRITE. Order is load-bearing: the export
            # has to be taken while the desk still holds the state being replaced, and the reset has
            # to land before profiles.apply, or the reset would wipe the values we just wrote.
            undo = show_actions.clean_restore_reset(job.step)
            if not undo.get("ok"):
                return undo
            reset_note = undo["message"]
        # AN OLD PROFILE REARRANGES THE FADERS AND THEN SAYS NOTHING ABOUT THE LEVELS, which is how
        # it can leave a channel silent. A fader's level belongs to the STRIP, so a channel moved to
        # a different fader inherits whatever that strip was sitting at - and profiles saved before
        # `fader_levels` existed carry the layout without the levels, so a restore can put a channel
        # on a fader at zero with every reading clean, because nothing IS wrong, and leave the
        # silence with no explanation.
        # So when the profile has no levels of its own, the levels FOLLOW THE CHANNELS instead, the
        # same way `action_set_fader` already carries them on a manual reassignment. When the
        # profile DOES carry levels they are authoritative and this must not run: `profiles.apply`
        # writes them after the layout, and overwriting them with the pre-restore values would throw
        # away the very thing the backup recorded.
        spec = faders.active_faders()
        carry_levels = not (prof.get("routing") or {}).get("fader_levels")
        vec_before = levels_before = None
        with desk_access.hid_lock:
            if carry_levels and faders.level_write_gate() is None:
                try:
                    blob0 = rcp_read.read_model()
                    vec_before = faders.read_fader_vector(blob0, spec)
                    levels_before = rcp_read.values(blob0, "faderLevel")
                except Exception:
                    vec_before = levels_before = None
            res = profiles.apply(prof, fader_count=faders.active_faders().vector_length,
                                 fader_code_set=faders.active_faders().code_set, progress=job.step)
            if levels_before is not None:
                faders.follow_levels(vec_before, faders.read_fader_vector(spec=spec), levels_before, spec)
        # A PROFILE CARRIES `systemMidiControl`, so one taken while MIDI was off has just switched
        # the MIDI port back off and every sound-pad shortcut with it. Forget that we armed it and
        # the next pad press arms it again - which is why arming is per-press-until-done rather than
        # once at startup.
        pad_shortcuts.rearm_midi()
        # A restore can also rewrite the whole pad grid, so the shortcut layer's map is wrong.
        pad_shortcuts.pads_stale()
        # Output mix matrix + monitor-mix levels (v2 profiles only).
        job.step("Settings done. Restoring output routing...")
        with desk_access.hid_lock:
            mx_res = output_mixes.apply_matrix(prof.get("routing") or {})
        pad_res = None
        pad_tidy = None
        pad_extra = None
        if pad_list:
            job.step("Restoring SMART pads...")
            with desk_access.hid_lock:
                # ONE TRANSFER SESSION FOR THE WHOLE PAD RESTORE. Without it each create enters
                # transfer mode, remounts the volume and leaves again for itself: sixteen cycles for
                # sixteen pads, every one dropping the desk's audio, popping a Windows device sound,
                # and costing about a second per pad. The session exits in its own __exit__, so a
                # failure on any pad still closes it.
                with rcp_transfer.transfer_session():
                    pad_res = soundpad.restore_pads(pad_list, profiles.pads_dir(safe),
                                                    progress=job.tick)
                # Only on a clean restore: there every pad outside the profile is one the reset
                # put back. An ordinary restore leaves pads the profile does not mention alone.
                if clean:
                    pad_extra = soundpad.clear_pads_not_in(pad_list, progress=job.step)
                # Pads replaced by the restore left their old audio behind; clear it out.
                pad_tidy = soundpad.tidy_unused_pad_audio()

        # AFTER THE PAD PHASE, AND ONLY ON A CLEAN RESTORE. The reset puts sixteen factory pads back,
        # six of them voice-FX pads with no effect definition behind them (soundpad.heal_fx_pool).
        # A profile that CARRIES pads has just rebuilt its own, so this finds nothing missing and
        # costs one dump; a profile saved without pads leaves the factory six needing the rebuild.
        healed = None
        if clean:
            job.step("Checking the voice effects survived the reset...")
            with desk_access.hid_lock:
                healed = soundpad.heal_fx_pool(progress=job.step)

        msg = profiles.restore_result(safe, res)["message"]
        if reset_note:
            msg = reset_note + " " + msg
        if mx_res and not mx_res.get("skipped"):
            msg += f" Re-applied output routing ({mx_res['applied']} matrix writes)."
        if pad_res is not None:
            msg += _pad_restore_words(pad_res, pad_extra, pad_tidy)
        if healed and healed.get("message"):
            msg += " " + healed["message"]
        return {"ok": True, "message": msg}

    return restore_job.start(_work, total=len(pad_list),
                              message=("Backing the desk up before resetting it..." if clean
                                       else "Applying settings, please wait."),
                              started="Restoring the profile.")


def _other_desk_refusal(prof: dict) -> dict | None:
    """A refusal for a profile saved on another model of desk, or None. A profile stamps the product
    id of the desk it was saved on; its fader vector, matrix shape, channel set and pad grid are
    device-specific, so applying a Pro II profile to a Duo (or the reverse) would mis-write. Older
    profiles carry no product id (all saved on the Pro II) and are allowed, with the per-write
    guards still protecting."""
    saved_pid = prof.get("product_id")
    active = devices.get_active_device()
    if saved_pid is None or saved_pid == active.product_id:
        return None
    saved_name = prof.get("device_name") or devices.resolve_device(saved_pid).name
    return {"ok": False,
            "message": (f"This profile was saved from a {saved_name}, but a {active.name} is "
                        f"connected. Profiles are device-specific and cannot be restored across "
                        f"different RODECaster models.")}


def _pad_restore_words(pad_res: dict, extra: dict | None, tidy: dict | None) -> str:
    """What a pad restore did, for the end of its reply: what was rebuilt and what was already right,
    anything it could not restore, the pads it emptied, and the audio it tidied."""
    unchanged = pad_res.get("unchanged") or []
    rebuilt = sum(1 for r in pad_res["results"] if r.get("ok") and not r.get("unchanged"))
    total_pads = len(pad_res["results"])
    # What was left alone as well as what was written: a restore that finishes in seconds because
    # everything already matched must not read like it did nothing.
    if unchanged:
        msg = (f" SMART pads: {len(unchanged)} of {total_pads} were already exactly as saved and "
               f"were left alone, {rebuilt} rebuilt.")
    else:
        msg = f" Restored {rebuilt} of {total_pads} SMART pads."
    if pad_res.get("skipped"):
        msg += " Could not restore: " + "; ".join(pad_res["skipped"]) + "."
    if extra and extra["cleared"]:
        msg += (f" Cleared {wording.plural(len(extra['cleared']), 'pad')} "
                f"{wording.join_and(map(str, extra['cleared']))}, empty in the profile.")
    if extra and extra["failed"]:
        msg += (f" Could not clear {wording.plural(len(extra['failed']), 'pad')} "
                f"{wording.join_and(map(str, extra['failed']))}, which the profile has empty.")
    if tidy and tidy.get("removed"):
        msg += " " + tidy["message"]
    return msg


def pad_backups() -> dict:
    """The backups on this computer that hold SMART pads saved on this model of desk, newest first,
    for restoring their pads alone. Reads each profile once; the store is a handful of files."""
    active = devices.get_active_device()
    found = []
    for name in profiles.list_profiles()["profiles"]:
        loaded = profiles.load(name)
        if not loaded.get("ok"):
            continue
        prof = loaded["prof"]
        pads = len(prof.get("pads") or [])
        if pads and prof.get("product_id") in (None, active.product_id):
            found.append({"name": loaded["safe"], "pads": pads, "saved_at": prof.get("saved_at")})
    found.sort(key=lambda b: b["saved_at"] or 0, reverse=True)
    return {"ok": True, "backups": found, "seconds_per_pad": PAD_RESTORE_SECONDS}


def action_restore_backup_pads(name: str) -> dict:
    """Put a backup's SMART pads back and nothing else: every pad as the backup has it, a pad the
    backup has empty emptied, the rest of the desk untouched. A backup taken just after a factory
    reset makes this the way back to the factory pads, sounds included, which the app holds no copy
    of. Runs on the pad-restore progress channel, like the factory voice effects restore."""
    loaded = profiles.load(name)
    if not loaded.get("ok"):
        return loaded
    safe, prof = loaded["safe"], loaded["prof"]
    refusal = _other_desk_refusal(prof)
    if refusal:
        return refusal
    pad_list = prof.get("pads") or []
    if not pad_list:
        return {"ok": False, "message": f"{safe} has no SMART pads in it."}
    if restore_job.busy():
        return {"ok": False, "message": "A profile restore is running. Try again when it finishes."}

    def _work(job):
        with desk_access.hid_lock:
            # One transfer session for every pad, as the full restore does.
            with rcp_transfer.transfer_session():
                pad_res = soundpad.restore_pads(pad_list, profiles.pads_dir(safe),
                                                progress=job.tick)
            # Outside the session: a clear only completes when the desk leaves transfer mode.
            extra = soundpad.clear_pads_not_in(pad_list, progress=job.step)
            # Unspoken: the result follows straight after and would cut it off, and says what it
            # removed.
            tidy = soundpad.tidy_unused_pad_audio()
        pad_shortcuts.pads_stale()
        return {"ok": True, "message": (f"The SMART pads from {safe} are back, and nothing else on "
                                        f"the desk was changed."
                                        + _pad_restore_words(pad_res, extra, tidy))}

    return pad_actions.restore_job.start(_work, total=len(pad_list),
                                         message=f"Restoring the SMART pads from {safe}.",
                                         failed="Could not restore the pads")
