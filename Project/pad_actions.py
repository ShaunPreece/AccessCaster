"""
AccessCaster - the SMART pads' app side: the pad grid, every live pad edit, sounds, voice effects
and their live preview, exporting a pad's sound, and the factory voice effects restore.

The protocol and the pad table are soundpad.py; this module owns the HID lock and the wording.
Restoring a backup's pads runs on this module's `restore_job` but lives with the backups.
"""

from __future__ import annotations

import os
import tempfile
import threading

import desk_access
import devices
import diagnostics
import folders
import pad_shortcuts
import soundpad

_preview_grid = None               # grid of the live-preview scratch pad, if one is engaged


# Pad grid geometry is DEVICE-AWARE (devices.PadSpec): the Pro II is 8 pads/bank, the Duo 6.
# These module constants are the Pro II DEFAULTS, kept only as the floor for the dynamic bank
# growth (PAD_MIN_BANKS) and the offline fallback in soundpad_model(). The per-device pads/bank,
# user-bank count and the per-action range limit come from the active device: read_pads_live()
# resolves the geometry, and max_user_pads() gives the hard range limit for a pad action.
# The visible grid is DYNAMIC - it grows with content (every filled bank plus one spare), floor
# PAD_MIN_BANKS - so a new bank appears as you fill the previous one, like the device itself.
PAD_MIN_BANKS = 3
PADS_PER_BANK = 8      # Pro II default (fallback); active device's PadSpec.pads_per_bank wins


def soundpad_model() -> dict:
    """Live pad grid: what is on each pad now, plus the effect-object pool health (which gates
    clear / assign-effect for safety) and the voice-effect choices.

    Reads the device model (~1.5s) under the HID lock. On a read failure it returns the static
    layout with an error note so the section still renders."""
    try:
        with desk_access.hid_lock:
            # max_banks / pads_per_bank default to the ACTIVE DEVICE geometry (Pro II 8/bank,
            # Duo 6/bank) - do NOT force the Pro II numbers here. read_pads_live also returns a
            # read_only flag (True on a device without the pad_write cap).
            return soundpad.read_pads_live(min_banks=PAD_MIN_BANKS)
    except Exception as exc:
        return {
            "banks": PAD_MIN_BANKS,
            "pads_per_bank": PADS_PER_BANK,
            "pads": [],
            "recipes": list(soundpad.all_recipes().keys()),
            "custom_fx": list(soundpad.load_user_fx_presets().keys()),
            "health_message": f"Could not read the pad layout from the device: {exc}",
        }


def max_user_pads() -> int:
    """Highest 1-based grid number a pad action may target on the ACTIVE device (Pro II = 56 on
    a 8/bank x 7-user-bank grid; Duo = 42 on a 6/bank grid). Device-aware so a Duo action cannot
    address a slot beyond its 48-slot model. Pad WRITES are also gated by soundpad's pad_write cap."""
    return devices.get_active_device().pad.max_user_pads


def _pad_write(grid: int, failed: str, work, invalid: str | None = None,
               plain_value_error: bool = False) -> dict:
    """Range-check a grid pad, take the HID lock, run `work()`, and turn a crash into a sentence.

    THE SHAPE EVERY LIVE PAD EDIT HAS. Only one sentence differs between them: `failed`, the
    "Could not ..." wording, which is the only part worth reading at a call site.

    `invalid` is a refusal message for a value this function cannot check itself (a play mode, a
    trigger field), and it is checked AFTER the range and BEFORE the lock: the pad number is the
    more basic complaint, and nothing should take the device lock to find out a value was wrong.

    `plain_value_error` surfaces a ValueError's own text as the message instead of wrapping it.
    soundpad raises those with a finished, user-facing sentence in them (a polluted effect pool,
    an unknown effect name), and wrapping that in "Could not assign the voice effect:" buries the
    part that says what to do about it.
    """
    if grid < 1 or grid > max_user_pads():
        return {"ok": False, "message": f"Pad {grid} is out of range."}
    if invalid:
        return {"ok": False, "message": invalid}
    # THE SHORTCUT LAYER'S PAD MAP IS NOW WRONG, whatever this edit turns out to be. It caches the
    # name, type, play mode and stream POSITION of every pad on a timer, and every one of those can
    # move here - clearing a pad shifts the positions of the ones after it. Marked stale rather than
    # re-read: this is the wrong moment to spend a model dump, and the next press reads it anyway.
    pad_shortcuts.pads_stale()
    try:
        with desk_access.hid_lock:
            return work()
    except ValueError as exc:
        if plain_value_error:
            return {"ok": False, "message": str(exc)}
        return {"ok": False, "message": f"{failed}: {exc}"}
    except Exception as exc:
        return {"ok": False, "message": f"{failed}: {exc}"}


def _pad_name_from_filename(filename: str) -> str:
    """Derive a tidy pad name from an uploaded filename (stem, alnum/space/underscore, capped)."""
    stem = os.path.splitext(os.path.basename(filename or "Sound"))[0]
    clean = "".join(c if (c.isalnum() or c in " _-") else " " for c in stem).strip()
    clean = " ".join(clean.split())            # collapse runs of whitespace
    return (clean[:24] or "Sound")


def action_add_sound(pad_n: int, filename: str, raw: bytes, replay: bool = True) -> dict:
    """Put the uploaded sound on the pad via the PROVEN clear+create path (soundpad.create_sound),
    NEVER an overwrite-in-place. create_sound clears the slot if occupied, then creates a fresh
    sound pad writing pads/<grid>/sound.<ext>, and is gated on a clean effect-object pool.

    NO CONVERSION: the unit's pad player accepts WAV at every rate (22k-192k), bit depth
    (16/24/32-float) and channel count, plus MP3, all played natively. So we write the file
    AS-IS (no resample / downmix / bit-depth change) and never degrade quality. We only require the
    file to be WAV or MP3 (the two formats the RODE manual lists) and preserve its extension on
    the device, because an MP3 written under a .wav name plays as silence (the firmware trusts the
    extension).
    """
    if pad_n < 1 or pad_n > max_user_pads():
        return {"ok": False, "message": f"Pad {pad_n} is out of range."}
    if not raw:
        return {"ok": False, "message": "The uploaded file was empty."}

    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in (".wav", ".mp3"):
        return {"ok": False,
                "message": "Only WAV or MP3 files can be added to a pad. "
                           "Please choose a .wav or .mp3 file."}

    name = _pad_name_from_filename(filename)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=ext, prefix="accesscaster_")
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        with desk_access.hid_lock:
            # dest_filename = the real upload name so the device stores it as the factory does
            # (pads/<n>/RealName.ext), not a generic sound.wav. name = the on-screen pad label.
            return soundpad.create_sound(pad_n, tmp_path, name=name, replay=replay,
                                         dest_filename=filename)
    except Exception as exc:
        return {"ok": False, "message": f"Could not add the pad sound: {exc}"}
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def action_set_replay(grid: int, on: bool) -> dict:
    """Toggle padReplay on an existing sound pad in place (restart-on-re-press vs pause/resume)."""
    return _pad_write(grid, "Could not change the pad's replay setting",
                      lambda: soundpad.set_pad_replay(grid, on))


def action_set_pad_gain(grid: int, db: float) -> dict:
    """Set per-pad output volume (padGain, dB) on an existing pad, in place. Read-back verifiable."""
    return _pad_write(grid, "Could not change the pad's volume",
                      lambda: soundpad.set_pad_property(grid, "padGain", db))


def action_set_pad_name(grid: int, name: str) -> dict:
    """Rename an existing pad in place (padName). Read-back verifiable."""
    name = (name or "").strip()
    return _pad_write(grid, "Could not rename the pad",
                      lambda: soundpad.set_pad_property(grid, "padName", name),
                      invalid=None if name else "Enter a name for the pad.")


def action_set_pad_loop(grid: int, on: bool) -> dict:
    """Toggle loop (padLoop) on an existing sound pad. Read-back verifiable."""
    return _pad_write(grid, "Could not change the pad's loop setting",
                      lambda: soundpad.set_pad_property(grid, "padLoop", on))


def action_set_play_mode(grid: int, mode: int) -> dict:
    """Sound pads: Toggle (0), Hold (1) or One Shot (2). The stored
    order is NOT the guide's printed "Toggle / One Shot / Hold" order."""
    return _pad_write(grid, "Could not change how the pad plays",
                      lambda: soundpad.set_play_mode(grid, mode),
                      invalid=None if mode in soundpad.PLAY_MODE_LABEL else
                      "Play mode must be 0 (Toggle), 1 (Hold) or 2 (One Shot).")


def action_set_mixer_trigger(grid: int, mode: int) -> dict:
    """Mixer pads: Momentary (0, only while held) or Latching (1, press on / press again off)."""
    return _pad_write(grid, "Could not change the pad's trigger mode",
                      lambda: soundpad.set_mixer_trigger(grid, mode),
                      invalid=None if mode in (0, 1) else
                      "Trigger mode must be 0 (Momentary) or 1 (Latching).")


def action_set_pad_trigger(grid: int, field: str, value: int) -> dict:
    """MIDI trigger pads: change ONE field of what the pad sends (type, control, channel, on, off),
    or turn the custom mapping off again with field="custom", value=0.

    ONE field per request, like every other live pad edit, so each change is read back and spoken on
    its own. `field` is validated against soundpad.TRIGGER_EDIT (the same spec that builds the
    window's controls and the write ranges), so an unknown field is REFUSED rather than quietly
    ignored - a silent no-op on a pad mapping is the worst outcome here, since nothing about the desk
    would look wrong afterwards."""
    known = {f["key"]: f for f in soundpad.TRIGGER_EDIT}
    if field == "custom":
        def _custom():
            res = soundpad.set_trigger_mapping(grid, custom=bool(value))
            # Switching custom OFF is destructive: the desk resets the control number, channel and
            # values to its own defaults and does not give them back if you switch it on again
            # (measured). Say so in the message, because a switch reads as reversible.
            if res.get("ok") and not value:
                res["message"] = (res.get("message", "") + " The pad is back on its default "
                                  "mapping, and the numbers you had set are not kept.")
            return res

        return _pad_write(grid, "Could not change the MIDI mapping", _custom)
    spec = known.get(field)
    if not spec:
        bad = ("Unknown MIDI trigger setting %r. Expected one of: %s."
               % (field, ", ".join(known)))
    elif value < spec["min"] or value > spec["max"]:
        bad = "%s must be between %d and %d." % (spec["label"], spec["min"], spec["max"])
    else:
        bad = None
    return _pad_write(grid, "Could not change the MIDI trigger",
                      lambda: soundpad.set_trigger_mapping(grid, **{field: value}),
                      invalid=bad)


def action_set_pad_envelope(grid: int, field: str, percent: float) -> dict:
    """Sound pads: one point of the trim/fade envelope, as a percentage through the sound.

    The four points are start / fade_in / fade_out / stop, and they are POSITIONS along the sound
    (see soundpad.ENV_FIELD). soundpad validates the field name and the range
    and reports when the result is a fade that cannot be heard."""
    return _pad_write(grid, "Could not change the pad's trim and fade",
                      lambda: soundpad.set_envelope_percent(grid, field, percent))


def action_set_pad_fade(grid: int, field: str, seconds: float) -> dict:
    """Fade mixer pads: how long the fade in or the fade out takes, in seconds."""
    return _pad_write(grid, "Could not change the fade time",
                      lambda: soundpad.set_fade_seconds(grid, field, seconds))


def action_set_pad_flag(grid: int, name: str, on: bool) -> dict:
    """The two named per-pad switches: a Fade pad excluding the host, and a Censor pad using your own
    sound instead of the beep. Named (soundpad.PAD_FLAG) rather than taking a property, so a
    caller cannot ask for an arbitrary pad write."""
    return _pad_write(grid, "Could not change that pad setting",
                      lambda: soundpad.set_pad_flag(grid, name, on))


def action_set_back_channel(grid: int, source: str, on: bool) -> dict:
    """Add or remove one channel from a Back Channel pad's off-air talkback room.

    `source` is the padMixerBackChannel* property name, as handed to the UI in the pads payload
    (soundpad.back_channel_sources); soundpad validates it against what this desk actually has.
    A Back Channel pad with no members links nothing and does nothing when pressed, so this is
    the setting that makes the pad work at all."""
    return _pad_write(grid, "Could not change the Back Channel members",
                      lambda: soundpad.set_back_channel_member(grid, source, on))


def action_set_pad_colour(grid: int, colour: int) -> dict:
    """Set a pad's display colour index (padColourIndex 0-11). Read-back verifiable."""
    return _pad_write(grid, "Could not change the pad's colour",
                      lambda: soundpad.set_pad_property(grid, "padColourIndex", colour))


def action_clear_pad(grid: int) -> dict:
    """Clear the pad at grid position `grid`. Gated on a clean effect-object pool by soundpad."""
    return _pad_write(grid, "Could not clear the pad",
                      lambda: soundpad.clear_grid_pad(grid))


def action_clear_all_pads() -> dict:
    """Clear every user pad (sound/FX/mixer/MIDI/quick-dial), leaving the System pads untouched.
    Destructive - the window confirms with a dialog before calling this."""
    try:
        with desk_access.hid_lock:
            res = soundpad.clear_all_pads()
            # allow_empty: with every pad cleared, NO pad points at any file, so all of it is
            # genuinely unused. This is the only caller that may say so - see tidy_unused_pad_audio.
            tidy = soundpad.tidy_unused_pad_audio(allow_empty=True)
        if tidy.get("removed"):
            res["message"] = ((res.get("message") or "Cleared every user pad.") + " "
                              + tidy["message"])
        return res
    except Exception as exc:
        return {"ok": False, "message": f"Could not clear all pads: {exc}"}


# Pad-defaults restore runs in a background thread (it takes ~a minute) so the page is not blocked
# and can poll progress for live-region announcements. This state is the single source the status
# endpoint reports.
restore_job = desk_access.BackgroundJob("A pad restore is already running.",
                                             "The pad restore failed", "A pad restore is running.",
                                             rewrites_desk=True)


def restore_status() -> dict:
    return restore_job.status()


def action_restore_factory_fx() -> dict:
    """Put the six factory VOICE EFFECTS back on their original pads.

    DESTRUCTIVE and narrower than it sounds, which is what the confirm dialog has to get across: it
    clears and recreates those six grid positions only. The eight factory SOUND pads are untouched,
    and so is everything past pad 16. Reuses the pad-restore progress channel, so the page shows the
    same progress dialog it already knows."""
    def _work(job):
        with desk_access.hid_lock:
            return soundpad.restore_factory_fx_pads(live=True, progress=job.tick)

    return restore_job.start(
        _work, total=len(soundpad.factory_fx_names()),
        message="Starting the factory voice effect restore.",
        phrase=lambda msg, _n, _total: f"{msg}.",
        started="Restoring the factory voice effects.",
        failed="Could not restore the factory voice effects")


def export_dir() -> str:
    """Where an exported pad sound goes."""
    return folders.documents_folder("Pad exports")


def action_export_pad(pad: int) -> dict:
    """Copy one pad's sound off the unit into the exports folder. Reads the unit; changes nothing."""
    return _pad_write(pad, f"Could not export pad {pad}",
                      lambda: soundpad.export_pad_audio(int(pad), export_dir()))


def action_open_pad_exports() -> dict:
    """Open the exports folder in Explorer, so the file can actually be used once it is copied."""
    return folders.open_folder_reply(export_dir(), "The exports folder")


def action_tidy_pad_audio() -> dict:
    """Manual sweep of pad audio nothing plays. The destructive pad operations do this for
    themselves; this covers clearing individual pads (a clear is HID-only by design - it mounts
    nothing and writes no files, which is why it raises no drive toast - so it cannot tidy up after
    itself without giving that up) and audio left by older builds."""
    try:
        with desk_access.hid_lock:
            return soundpad.tidy_unused_pad_audio()
    except Exception as exc:
        return {"ok": False, "message": f"Could not tidy up the pad audio: {exc}"}


def action_assign_fx(grid: int, effect: str) -> dict:
    """Put a factory voice effect on the pad at grid position `grid`. Gated on a clean pool."""
    return _pad_write(grid, "Could not assign the voice effect",
                      lambda: soundpad.assign_fx_to_grid(grid, effect),
                      plain_value_error=True)


def action_create_special_pad(grid: int, type_str: str) -> dict:
    """Create a Mixer-action pad ("mixer:0".."mixer:4") or a MIDI Trigger pad ("trigger") on `grid`.
    Both are pool-safe (no region-B writes). Clears the slot first if occupied (soundpad does this)."""
    def _make():
        if type_str.startswith("mixer:"):
            return soundpad.create_mixer(grid, int(type_str.split(":", 1)[1]))
        if type_str == "trigger":
            return soundpad.create_trigger(grid)
        return {"ok": False, "message": f"Unknown pad type {type_str!r}."}

    return _pad_write(grid, "Could not create the pad", _make, plain_value_error=True)


def action_save_fx_preset(name: str, stack: dict, colour: int) -> dict:
    """Save a user-built custom voice effect (no device access; just persists to the user file).
    soundpad.validate_fx_stack sanitises engines/params and clamps every value to range."""
    try:
        saved = soundpad.save_user_fx_preset(name, stack, colour=colour)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    except Exception as exc:
        return {"ok": False, "message": f"Could not save the effect: {exc}"}
    return {"ok": True,
            "message": f"Saved {saved}. It is now in the Assign a voice effect list.",
            "recipes": list(soundpad.all_recipes().keys()),
            "custom_fx": list(soundpad.load_user_fx_presets().keys())}


def action_delete_fx_preset(name: str) -> dict:
    """Delete a user-built custom voice effect. Built-in effects cannot be deleted (not in the
    user file), so this only ever removes the user's own presets."""
    try:
        existed = soundpad.delete_user_fx_preset(name)
    except Exception as exc:
        return {"ok": False, "message": f"Could not delete the effect: {exc}"}
    if not existed:
        return {"ok": False, "message": f"{name} is not one of your custom effects."}
    return {"ok": True,
            "message": f"Deleted {name}.",
            "recipes": list(soundpad.all_recipes().keys()),
            "custom_fx": list(soundpad.load_user_fx_presets().keys())}


def action_preview_start(stack: dict, colour: int, mic: int) -> dict:
    """Create a scratch preview pad carrying the in-progress effect and engage it on the live
    mic. Returns the grid used, or no_pad:True if every pad is full."""
    global _preview_grid
    try:
        with desk_access.hid_lock:
            res = soundpad.preview_start(stack or {}, colour=colour, mic=mic)
        if res.get("ok"):
            _preview_grid = res.get("grid")
        return res
    except Exception as exc:
        return {"ok": False, "message": f"Could not start live preview: {exc}"}


class _PreviewWriter:
    """Live-preview edits, written by one thread in as few transfer-mode sessions as it can.

    A session costs about two seconds to open and close (the pad volume mounts and dismounts), and
    a change is a single record, so each edit is queued, the newest value per setting winning, and
    the thread writes whatever is waiting in one session, holding it open while more arrive within
    IDLE_S (soundpad.preview_write). A write that fails is reported by the next submit."""

    IDLE_S = 0.6

    def __init__(self):
        self._cond = threading.Condition()
        self._pending = {}       # {(grid, param): (value, kind)}, oldest first
        self._failure = None
        self._thread = None

    def submit(self, grid: int, param: str, value, kind: str) -> dict:
        with self._cond:
            failure, self._failure = self._failure, None
            self._pending.pop((grid, param), None)
            self._pending[(grid, param)] = (value, kind)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="preview writer",
                                                daemon=True)
                self._thread.start()
            self._cond.notify()
        return failure or {"ok": True}

    def forget(self, grid: int) -> None:
        """Drop what is waiting for `grid`, whose preview has ended."""
        with self._cond:
            self._pending = {key: v for key, v in self._pending.items() if key[0] != grid}

    def _take(self, wait):
        with self._cond:
            if not self._pending:
                self._cond.wait(wait)
            edits, self._pending = self._pending, {}
            return edits

    def _run(self) -> None:
        while True:
            edits = self._take(None)
            if not edits:
                continue
            try:
                with desk_access.hid_lock:
                    soundpad.preview_write(edits, more=lambda: self._take(self.IDLE_S))
            except Exception as exc:
                diagnostics.event("preview.write_failed", str(exc))
                with self._cond:
                    self._failure = {"ok": False,
                                     "message": f"Could not update live preview: {exc}"}


_preview_writer = _PreviewWriter()


def action_preview_update(grid: int, param: str, value) -> dict:
    """Queue one control change for the live preview pad (an effect param, on-flag, or mic input).
    The value is validated and clamped first. Returns at once; a failed write is reported by the
    next change."""
    try:
        p, v, kind = soundpad.sanitise_preview_edit(param, value)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    return _preview_writer.submit(int(grid), p, v, kind)


def action_preview_stop(grid: int) -> dict:
    """Clear the preview scratch pad, removing the effect from the live mic."""
    global _preview_grid
    _preview_writer.forget(int(grid))
    try:
        with desk_access.hid_lock:
            res = soundpad.preview_stop(grid)
        _preview_grid = None
        return res
    except Exception as exc:
        return {"ok": False, "message": f"Could not stop live preview: {exc}"}


def cleanup_preview_on_exit() -> None:
    """If the server stops (watchdog/quit/Ctrl-C) while a live preview is engaged, clear the
    scratch pad so no effect is left on the mic. Best effort; failures are ignored."""
    if _preview_grid is None:
        return
    try:
        with desk_access.hid_lock:
            soundpad.preview_stop(_preview_grid)
    except Exception:
        pass
