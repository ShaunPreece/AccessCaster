"""AccessCaster - settings profiles (full backup + restore).

A profile is a JSON snapshot of EVERY setting the app can read AND write, so it round-trips the
whole desk: MIDI; for every channel the device exposes (Mic 1-4, Bluetooth, USB1, Chat, USB2, and
the SMART pads bus, which has a chain but no level) the level, plus for mics phantom/polarity/mic
type and the always-on echo+reverb effects, and for every channel the full processing chain
(master on/off, each module's on/off, all 33 params); and routing (the full nine-fader layout, the
virtual fader levels and the monitor speaker level, multitrack mode, the two auto-mutes, each
headphone output's amp type, the Master Compellor on/off + its four params, and output delay); and
the unit's own settings (screen and button brightness, auto-brightness, screen dim time, meter
style, haptics, what the record button does, listen mode and the monitor's fixed output level).
SMART pad SOUNDS are files, not settings: an optional pad backup copies them into the profile's own
pads/ folder (pads_dir) and records their metadata under "pads".

FORMAT: a profile is a FOLDER, profiles/<name>/profile.json (version 2), holding its pads/ audio
beside it.

A RESTORE PUTS BACK EVERYTHING OR THE SETUP ALONE (SCOPES), and either way check() compares the
desk, read back afterwards, with what that scope wrote.

LOCK BOUNDARY: save(), load() and check() are pure (file ops + an already-read model blob). apply()
performs the device WRITES and assumes THE CALLER HOLDS the HID lock (`desk_access.hid_lock`):
`profile_actions` takes it, reads the model under it, and hands the blob or loaded profile here.
"""
from __future__ import annotations

import json
import os
import shutil
import time

import channels
import desk_secrets
import devices
import diagnostics
import master_outputs
import rcp_read
import rcp_transfer
import recorder
import routing_schemes
from paths import data_path, safe_name, write_json
from wording import count

# Saved profiles are user data, kept in the writable %LOCALAPPDATA%\AccessCaster\profiles so they
# survive app reinstall/uninstall and work no matter where the app is installed.
PROFILES_DIR = data_path("profiles")
PROFILE_VERSION = 2
RESTORE_PACE = 0.02            # seconds between device writes during a restore
# Mic channel name -> 1-based number. Pro II-shaped (Mic3/Mic4), but SAFE across devices because
# restore refuses a cross-device restore: a Duo profile only ever carries Mic1/Mic2, and a
# stray higher channel would fail the device's own mic guard (channels._mic_key) anyway.
_MIC_NAME_TO_INT = {"Mic1": 1, "Mic2": 2, "Mic3": 3, "Mic4": 4}

# ---- the full-dump snapshot ------------------------------------------------------------------
# A profile records the CURATED settings above (the ones restore can write back) AND a snapshot of
# EVERY property in the model dump. The dump is already in hand, so the snapshot is free, and it is
# what makes a profile a complete record of the desk rather than a record of the settings we happen
# to understand today: a property we decode NEXT year is already sitting in every profile saved
# from now on, so an old backup becomes restorable retrospectively. It is never written back - the
# curated keys drive restore - so an undecoded property can never be blind-written from here.
#
# REDACTION. The dump carries secrets in plain text: the wifi PSK and the SIP/CallMe account
# password among them. Profiles live in %LOCALAPPDATA% but people do share them, so every secret
# (`desk_secrets`) is replaced with None and named in the profile's "redacted" list. SSIDs and
# usernames are kept: they identify the setup, not access it.
SNAPSHOT_KEY = "device_snapshot"

# ---- the AS-FOUND baseline --------------------------------------------------------------------
# One profile per desk may be marked as the baseline: the state the unit was in before this app ever
# wrote to it, captured the first time the app meets that desk (baseline.baseline_status), and
# filed under the desk's USB serial (HARDWARE_ID_KEY). It is an ordinary profile in every
# other way - same format, same restore path - but it is the only copy of the desk as it came, and
# a factory reset gives factory settings, not these, so nothing rebuilds it. It gets two
# protections that a normal profile does not: an ordinary save can never overwrite that name (a name
# collision would silently replace the one thing that cannot be recreated), and deleting it takes a
# deliberate second step rather than the same click as any other profile.
BASELINE_KEY = "baseline"
# The USB serial of the desk a profile was saved from; "" or absent when it was not known.
HARDWARE_ID_KEY = "hardware_id"

# ---- the paired show ---------------------------------------------------------------------------
# {"snapshot": int, "uuid": str, "name": str}: the desk's own backup taken at the same moment as
# this profile. Present only when there was a card or drive in the unit to export to, since a show
# can only live on removable storage (the internal volume is the pads partition and cannot hold
# one). This is what lets ONE Backups list carry both. Restore always reads this file, never the
# show, because a show import crashes the desk about one time in six
# (backups.action_restore_backup); the show stays as a complete copy on the drive. The profile also
# supplies the NAME, because the unit calls every export after the loaded show and gives no way to
# rename one, so a shows-only list reads as several identical lines.
SHOW_KEY = "show"

# ---- the unit's own settings (display, buttons, haptics, listen mode) --------------------------
# Writable (rcp_transfer.UNIT_SETTING_SEL), and a profile carries them, each read on the node its
# write goes to (rcp_transfer.unit_setting).
#
# WHY IT IS SAFE TO RESTORE THESE and not, say, an undecoded property: every one has a CONFIRMED
# selector-plus-name pairing. The restore is still gated on the device's own "unit_settings" cap,
# so a desk whose selectors were never observed refuses instead of guessing.
UNIT_KEY = "unit"

# SAVED BUT NEVER WRITTEN BACK. Two entries in the address table are not settings at all, they are
# live MUTES that happen to live in the same block, and restoring a mute is the one thing a profile
# must not do: the whole point of this file is that a restore is safe to run mid-session, and
# silencing the speakers or the Bluetooth send because that is how they sat when the profile was
# taken would be a nasty surprise with no visible cause. The same reasoning keeps
# channelOutputMute out of a restore; these are its output-side equivalents.
# They ARE saved, so the full snapshot still records how the desk was found.
UNIT_NEVER_RESTORE = frozenset({"outputMonMute", "outputBTMute"})

# ---- how much of a profile a restore puts back --------------------------------------------------
# EVERYTHING is the whole profile. SETUP is what changes when the desk is re-plugged for another
# job, and nothing else: which channel is on which fader, the virtual fader levels, and each
# channel's input settings, effects and processing. It leaves alone everything a setup is not: the
# master and output section (multitrack, the USB mode, auto-mutes, headphone types, Compellor,
# delay), the output mixes, the monitor speaker level, MIDI, the desk's own settings and the pads.
# So swapping setups never re-enumerates the desk's USB audio and never undoes a tweak made since.
SCOPE_EVERYTHING = "everything"
SCOPE_SETUP = "setup"
SCOPES = (SCOPE_EVERYTHING, SCOPE_SETUP)

# ---- a linked pair's own strip -------------------------------------------------------------------
# {str(pair code): chain} under routing, for each stereo pair on a fader when the profile was
# saved, in the shape of channels.read_chains. A pair is a fader code of its own and no channel, so
# the per-channel records cannot hold the processing of the strip it sits on; its two inputs'
# own settings (type, gain, phantom, polarity) are in their channel records, read at their
# INPUTSOURCE nodes, which a link does not take away.
PAIR_CHAINS_KEY = "pair_chains"

# How far apart two floats may be and still be the same setting, when a restore is checked. Wider
# than the desk's own rounding, because in simple mode it derives part of a chain from the three
# VoxLab knobs and lands a hair off the value written.
CHECK_FLOAT_EPS = 0.002
# How many differences the check names before it only counts them: the sentence is listened to.
CHECK_NAMED = 4


def _unit_settings(blob: bytes) -> dict:
    """{prop: int|float} for every unit setting the dump carries. Bools are stored as 0/1 so the
    profile round-trips through JSON unambiguously; a property the desk did not report is left out.

    FLOATS are kept as floats (outputBTLevel): an int-only filter would drop the Bluetooth output
    level silently."""
    out = {}
    for prop in rcp_transfer.UNIT_SETTING_SEL:
        v = rcp_transfer.unit_setting(blob, prop)
        if isinstance(v, bool):
            out[prop] = int(v)
        elif isinstance(v, (int, float)):
            out[prop] = v
    return out


# ---- naming + listing (pure file ops) ------------------------------------------------------

def _list_profile_names() -> list:
    """Every saved profile: each folder holding a profile.json."""
    try:
        return sorted(entry for entry in os.listdir(PROFILES_DIR)
                      if os.path.isfile(os.path.join(PROFILES_DIR, entry, "profile.json")))
    except FileNotFoundError:
        return []


def _profile_read_path(safe: str):
    """The profile's profile.json, or None if there is none."""
    path = os.path.join(PROFILES_DIR, safe, "profile.json")
    return path if os.path.isfile(path) else None


def list_profiles() -> dict:
    return {"ok": True, "profiles": _list_profile_names()}


def pads_dir(safe: str) -> str:
    """Folder where a profile's SMART pad sound files live: profiles/<safe>/pads/."""
    return os.path.join(PROFILES_DIR, safe, "pads")


# ---- save (build the dict from an already-read model blob; no device access) ---------------

def _snapshot(blob: bytes) -> dict:
    """Every property in the dump, secrets redacted. Reference only; never written back."""
    props = rcp_read.snapshot_properties(blob)
    redacted = sorted(n for n in props if desk_secrets.is_secret(n))
    for n in redacted:
        props[n] = [None] * len(props[n])
    return {
        "note": ("Every property the desk reported, for reference and for future restores. "
                 "AccessCaster restores from the named settings above, never from here, so a "
                 "property we have not decoded can never be blind-written onto a desk."),
        "properties": props,
        "property_count": len(props),
        "value_count": sum(len(v) for v in props.values()),
        "redacted": redacted,
    }


def _master_settings(blob: bytes) -> dict:
    """The master and output section as a profile holds it, read from a dump: what a save stores
    and what a restore is checked against, so the two cannot drift apart."""
    def output(n):
        return rcp_read.keyed_value(blob, rcp_transfer.OUTPUT, n)

    usb_mode, usb_expanded = recorder.split_usb_multitrack(output("outputMultiMode"))
    return {
        "multitrack": output("recordingMultitrackMode"),
        # The rest of the "what do I end up with" set. The multitrack MODE without its processing
        # bypass is half a decision: a profile taken on raw pre-fader tracks would come back
        # processed, which is not recoverable after the take. `usb_multitrack` is the USB
        # destination's own mode (see recorder.py).
        "processing_bypass": output("recordingProcessingBypass"),
        # And WHAT IT IS WRITTEN AS, for the same reason: a profile that restores what the take
        # contains but not whether it is a WAV or an MP3 has backed up half the decision, and
        # neither half is recoverable once the take exists.
        "recording_format": output("recordingCompressionQuality"),
        # SAVED AS THE TWO SETTINGS IT IS, not as the raw int (see recorder.py). A profile with a
        # bare 0-2 in `usb_multitrack` and no `usb_expanded` reads as "that output mode, Standard".
        "usb_multitrack": usb_mode,
        "usb_expanded": usb_expanded,
        "usb_processing_bypass": output("outputMultiBypass"),
        "monitor_auto_mute": output("outputMonAutoMute"),
        "bt_auto_mute": output("outputBTAutoMute"),
        "headphone_types": master_outputs.headphone_types(blob),    # one per addressable output
        # The master bus's Compellor and output delay, seven keys, built in master_outputs so the
        # page and the backup cannot drift apart.
        **master_outputs.master_processing(blob),
    }


def _pair_chains(blob: bytes, faders) -> dict:
    """{str(pair code): chain} for each stereo pair on a fader (PAIR_CHAINS_KEY)."""
    pairs = devices.get_active_device().fader.pair_map
    held = [(strip, code) for strip, code in enumerate(faders) if code in pairs]
    if not held:
        return {}
    chains = channels.read_chains(blob)
    return {str(code): chains[strip] for strip, code in held if strip < len(chains)}


def save(safe: str, blob: bytes, chans: list, firmware, fader_count: int, pads=None,
         product_id=None, device_name=None, baseline=False, show=None, hardware_id="") -> dict:
    """Build the full profile from an ALREADY-READ model `blob` + channel read `chans`, and write
    it to profiles/<safe>/profile.json. No device access - the caller did the read (and, for the
    optional `pads` backup, the pad read + file copy) under the lock. `pads` is the metadata list
    from soundpad.backup_pads (sound files already copied into profiles/<safe>/pads/); when given it
    is stored under the profile's "pads" key so restore can rebuild the grid.

    `fader_count` is the CAPTURE length (devices.FaderSpec.capture_length), which is deliberately
    allowed to exceed the number of slots restore will WRITE (FaderSpec.vector_length): both desks
    carry a 10-slot channelInputSource whose last slot is an unexposed spare mirroring USB 2, so we
    record it for the round trip while `apply` writes only the first `fader_count` it is given.

    DEVICE-AWARENESS: `product_id`/`device_name` stamp the desk the profile was saved on so
    restore can REFUSE a cross-device restore (a Pro II profile's fader vector, matrix shape and
    channel set do not map onto a Duo).

    `baseline` marks this as the AS-FOUND capture (see BASELINE_KEY). It also decides who may
    overwrite whom: an ordinary save REFUSES to replace a baseline, because the one profile that
    cannot be recreated must not be lost to a name collision.

    `show` PAIRS THIS PROFILE WITH THE DESK'S OWN BACKUP taken in the same breath, as
    {"snapshot": int, "uuid": str, "name": str}. It is what makes one Backups list possible: the
    show is the fast exact restore, this file is the NAME and the fallback for when the drive has
    been pulled, wiped or filled. `snapshot` is the handle because it is the only stable per-file
    one (a uid renumbers on every export, a uuid names the show rather than the file). None when
    there was no removable drive to export to, which is the whole of a Duo's case."""
    existing = load(safe)
    if existing.get("ok") and is_baseline(existing["prof"]) and not baseline:
        return {"ok": False, "message":
                f"{safe} is an as-found baseline, the only record of how a desk was before this "
                f"app changed anything, so it cannot be replaced by an ordinary save. "
                f"Choose another name."}
    def output(n):
        return rcp_read.keyed_value(blob, rcp_transfer.OUTPUT, n)

    unit = _unit_settings(blob)
    cis = rcp_read.strip_values(blob, "channelInputSource")
    midi = rcp_read.keyed_value(blob, rcp_transfer.SYSTEM, "systemMidiControl")

    # The two levels the desk otherwise keeps to itself: each fader's faderLevel (0..127, per
    # STRIP, only the VIRTUAL slots are ours to write) and the monitor SPEAKER level
    # (outputMonLevel, a 0..1 float). Both are recorded RAW, so a profile round-trips
    # them exactly. Like the fader vector, capture is deliberately wider than write: we store every
    # slot's value and apply writes only the slots this device has a confirmed level selector for.
    # Each is read on its own node, the one its write goes to, and one that fails to decode is
    # kept as None: the lists are POSITIONAL (index = strip, or headphone output).
    fader_levels = [int(v) if isinstance(v, (int, float)) else None
                    for v in rcp_transfer.fader_levels(blob)]
    mon_level = output("outputMonLevel")

    # THE OUTPUT MIX MATRIX IS CAPTURED AS A MIX SCHEME. routing_schemes.capture already records
    # the WHOLE matrix - every output's mode, every cross-point's include/exclude, its link state
    # and, where unlinked, its manual level - keyed by NAME rather than by index, so a profile
    # stores that scheme verbatim rather than keeping a second, weaker copy of the same read.
    #
    # It covers EVERY output, not only the monitor ones, so a level set on the REC, USB or Chat mix
    # is backed up; and it records nothing but `excluded` for an excluded cross-point, whose
    # "level" is an inert number frozen where the fader stood (see PROJECT_GUIDE). Writing a manual
    # level to a non-monitor cross-point is a confirmed write, so no new capability is being
    # assumed here.
    #
    # The read refuses if the matrix changed shape (a phone caller) or if this device's matrix read
    # is unverified, so we store null + a note rather than a wrong snapshot. Same blob, no extra
    # read.
    mix = routing_schemes.capture(safe, blob)
    mix_scheme = mix["scheme"] if mix.get("ok") else None
    matrix_note = "" if mix.get("ok") else mix.get("message", "matrix not readable")
    pair_chains = _pair_chains(blob, cis[:fader_count])

    profile = {
        "version": PROFILE_VERSION,
        "name": safe,
        "saved_at": time.time(),
        # The desk this profile was saved on. restore refuses a cross-device restore when
        # product_id is present and differs from the connected device. A profile without either
        # key was saved on a Pro II; it is allowed and relies on the per-write guards.
        "product_id": product_id,
        "device_name": device_name,
        # The unit, by USB serial: which desk an as-found backup belongs to (find_baseline).
        HARDWARE_ID_KEY: hardware_id or "",
        "midi": bool(midi) if midi is not None else None,
        # The full per-channel state straight from the live read (read_channels gives every UI
        # channel: level, processing on/modules/params, and for mics phantom/polarity/type and
        # echo+reverb on/params).
        "channels": chans,
        "routing": {
            "faders": [cis[i] if i < len(cis) else None for i in range(fader_count)],
            # Virtual fader levels (raw 0..127, indexed by strip) + the monitor speaker level
            # (raw 0..1). See the capture comment above.
            "fader_levels": fader_levels,
            "monitor_level": mon_level,
            **_master_settings(blob),
            # THE WHOLE output mix matrix, as a mix scheme (see the capture comment above), and
            # why not when it could not be read (a phone caller).
            "mix_scheme": mix_scheme,
            "matrix_note": matrix_note,
            PAIR_CHAINS_KEY: pair_chains,
        },
        # The unit's own settings (display, button brightness, haptics, REC button, listen mode,
        # fixed output level).
        UNIT_KEY: unit,
        # wifi is recorded for reference only; it is not re-applied on restore.
        "wifi": {"ssid": rcp_read.keyed_value(blob, rcp_transfer.NETWORK, "wifiSSID") or "",
                 "ip": rcp_read.keyed_value(blob, rcp_transfer.NETWORK, "wifiIpAddress") or ""},
        "firmware": firmware,
        SNAPSHOT_KEY: _snapshot(blob),
    }
    if pads is not None:                  # optional SMART pad backup (files already copied)
        profile["pads"] = pads
    if show is not None:
        profile[SHOW_KEY] = show
    if baseline:
        profile[BASELINE_KEY] = True

    try:
        os.makedirs(os.path.join(PROFILES_DIR, safe), exist_ok=True)
        write_json(os.path.join(PROFILES_DIR, safe, "profile.json"), profile, indent=1)
    except OSError as exc:
        return {"ok": False, "message": f"Could not save the profile: {exc}"}
    snap = profile[SNAPSHOT_KEY]
    msg = (f"Saved profile {safe} with the desk's full current settings, and a record of all "
           f"{snap['property_count']} settings the desk reports.")
    if pads is not None:
        msg += f" Included {count(len(pads), 'SMART pad')}."
    return {"ok": True, "name": safe, "message": msg}


def load(name: str) -> dict:
    """Resolve + read a profile file. Returns {ok:True, safe, prof} or {ok:False, message}."""
    safe = safe_name(name)
    path = _profile_read_path(safe) if safe else None
    if not safe or not path:
        return {"ok": False, "message": f"Profile {name} not found."}
    try:
        with open(path, encoding="utf-8") as f:
            return {"ok": True, "safe": safe, "prof": json.load(f)}
    except (OSError, ValueError) as exc:
        return {"ok": False, "message": f"Could not read the profile: {exc}"}


def is_baseline(prof: dict) -> bool:
    """Is this loaded profile the as-found baseline? Tolerant of an older profile that has no such
    key at all, which is simply not one."""
    return bool((prof or {}).get(BASELINE_KEY))


def _baseline_of(prof: dict, hardware_id: str, product_id) -> bool:
    """Is `prof` the as-found baseline of the desk with this serial and model?

    A baseline saved without a serial matches any desk of its model, a profile with no product id
    being a Pro II's. Such a baseline was the only one the store could hold, so the desk it came
    from is the likeliest owner; the cost of a wrong match is one desk not offered its own."""
    if not is_baseline(prof):
        return False
    saved = prof.get(HARDWARE_ID_KEY) or ""
    if saved:
        return bool(hardware_id) and saved == hardware_id
    saved_pid = prof.get("product_id")
    return (saved_pid if saved_pid is not None else devices.PID_PRO2) == product_id


def find_baseline(hardware_id: str, product_id) -> dict:
    """{name, pads, saved_at, device_name, product_id} for the baseline of the desk with this USB
    serial and model, or {} if it has none.

    Reads each profile once. The store is small (a handful of profiles), and this runs when the
    page asks whether to offer a first-run capture, so the cost is not worth caching and getting
    wrong."""
    for name in _list_profile_names():
        got = load(name)
        if got.get("ok") and _baseline_of(got["prof"], hardware_id, product_id):
            prof = got["prof"]
            return {"name": got["safe"], "pads": len(prof.get("pads") or []),
                    "saved_at": prof.get("saved_at"), "device_name": prof.get("device_name"),
                    "product_id": prof.get("product_id")}
    return {}


def attach_pads(name: str, pads: list) -> dict:
    """Add a SMART pad backup to a profile that was saved WITHOUT one, leaving its settings exactly
    as they were captured.

    This is the "add the pads later" path for a baseline whose pad copy was skipped to save time. It
    deliberately does NOT re-run save(): that would re-read the desk as it is TODAY, quietly turning
    an as-found record into a record of now - the one thing a baseline must never become. The sound
    files are already in pads_dir(); this only records their metadata."""
    got = load(name)
    if not got.get("ok"):
        return got
    safe, prof = got["safe"], got["prof"]
    prof["pads"] = pads
    folder = os.path.join(PROFILES_DIR, safe)
    if not _child_of_profiles(folder):
        return {"ok": False, "message": "That profile name is not one this app can write to."}
    try:
        os.makedirs(folder, exist_ok=True)
        write_json(os.path.join(folder, "profile.json"), prof, indent=1)
    except OSError as exc:
        return {"ok": False, "message": f"Could not update the profile: {exc}"}
    return {"ok": True, "name": safe,
            "message": f"Added {count(len(pads), 'SMART pad')} to {safe}. Its settings are still the "
                       f"ones captured when it was saved."}


def _child_of_profiles(path: str) -> bool:
    """True when path is a DIRECT child of PROFILES_DIR. Every destructive file operation in this
    module checks this after safe_name, so a crafted name can never reach outside the store."""
    return os.path.dirname(os.path.abspath(path)) == os.path.abspath(PROFILES_DIR)


def rename(name: str, new_name: str) -> dict:
    """Rename a saved profile in place. Moves the whole folder, so the pads/ audio travels with it.
    No device access."""
    old = safe_name(name)
    new = safe_name(new_name)
    if not old:
        return {"ok": False, "message": f"Profile {name} not found."}
    if not new:
        return {"ok": False, "message": "The new name must be 1 to 40 letters, digits, spaces, "
                                        "dashes or underscores."}
    if new == old:
        return {"ok": False, "message": "That is already the profile's name."}
    if new in _list_profile_names():
        return {"ok": False, "message": f"There is already a profile called {new}. "
                                        f"Pick another name."}
    old_folder = os.path.join(PROFILES_DIR, old)
    new_folder = os.path.join(PROFILES_DIR, new)
    if not all(_child_of_profiles(p) for p in (old_folder, new_folder)):
        return {"ok": False, "message": "That profile name is not one this app can rename."}
    if not os.path.isdir(old_folder):
        return {"ok": False, "message": f"Profile {old} not found."}
    try:
        os.rename(old_folder, new_folder)
    except OSError as exc:
        return {"ok": False, "message": f"Could not rename the profile: {exc}"}
    return {"ok": True, "message": f"Renamed {old} to {new}."}


def delete(name: str, force: bool = False) -> dict:
    """Delete a saved profile from this computer: its folder, profile.json and pads/ audio
    together. No device access.

    The name goes through safe_name first (so no separators, no dots, no traversal), and the
    resolved path is then checked to be a DIRECT child of PROFILES_DIR before anything is removed.
    That second check is the one that matters: this is the only recursive delete in the app, and it
    must not be reachable with a crafted name.

    The AS-FOUND baseline needs `force`. It is the only record of the desk before this app touched
    it and a factory reset gives factory settings rather than these, so it does not go the same way
    as an ordinary profile: the caller gets `baseline: True` back and has to ask first."""
    safe = safe_name(name)
    if not safe:
        return {"ok": False, "message": f"Profile {name} not found."}
    if not force:
        got = load(safe)
        if got.get("ok") and is_baseline(got["prof"]):
            return {"ok": False, "baseline": True, "name": safe, "message":
                    f"{safe} is the as-found baseline: how this desk was set up before "
                    f"AccessCaster changed anything. It cannot be captured again, and a factory "
                    f"reset gives factory settings, not these. Confirm to delete it anyway."}
    folder = os.path.join(PROFILES_DIR, safe)
    if not _child_of_profiles(folder):
        return {"ok": False, "message": "That profile name is not one this app can delete."}
    if not os.path.isdir(folder):
        return {"ok": False, "message": f"Profile {safe} not found."}
    try:
        shutil.rmtree(folder)
    except OSError as exc:
        return {"ok": False, "message": f"Could not delete the profile: {exc}"}
    return {"ok": True, "message": f"Deleted the profile {safe} from this computer. "
                                   f"The desk's settings were not changed."}


# ---- apply (device WRITES; THE CALLER MUST HOLD THE HID LOCK) -------------------------------

class _Restore:
    """The writes of one restore: each is paced and counted, and a failure is recorded and passed
    over, so one odd value cannot abort the rest."""

    def __init__(self, progress=None):
        self.applied = 0
        self.failed = 0
        self.errs = []
        self._progress = progress

    def step(self, msg):
        """Report a section boundary to the caller's `progress`."""
        if self._progress:
            self._progress(msg)

    def write(self, desc, fn):
        """Run one write. Everything that can raise on a damaged value, conversions included,
        belongs inside `fn`, so it is counted as this write's failure."""
        try:
            fn()
            self.applied += 1
        except Exception as exc:
            self._fail(desc, exc)
        time.sleep(RESTORE_PACE)

    def _fail(self, desc, exc):
        self.failed += 1
        if len(self.errs) < 6:
            self.errs.append(f"{desc}: {exc}")

    def choice(self, desc, value, allowed, fn):
        """Write `fn(code)` for a saved code that is one of `allowed`. A code off the list is
        skipped, since the control itself would refuse it; one that is not a number is a failure.
        Returns the code written, or None."""
        try:
            code = int(value)
        except (TypeError, ValueError) as exc:
            self._fail(desc, exc)
            return None
        if code not in allowed:
            return None
        self.write(desc, lambda: fn(code))
        return code

    def refuse(self, what):
        """A whole section the connected desk cannot take."""
        self.failed += 1
        self.errs.append(what)

    def result(self) -> dict:
        return {"applied": self.applied, "failed": self.failed, "errs": self.errs}


def apply(prof, *, fader_count, fader_code_set, progress=None, scope=SCOPE_EVERYTHING) -> dict:
    """Apply a loaded profile to the device. Returns {applied, failed, errs}. Paces the writes and
    keeps going past an individual failure, so one odd value cannot abort the whole restore.

    `scope` is how much of it: SCOPE_EVERYTHING, or SCOPE_SETUP for the fader layout, the virtual
    fader levels and the channels alone (see SCOPES).

    THE CALLER MUST HOLD THE HID LOCK (this module does no locking). progress, if given, is called
    with a short human sentence at each section boundary, so a caller polling from a UI has
    something to say during the ~20 seconds of paced writes instead of one frozen message."""
    if scope not in SCOPES:
        raise ValueError(f"no restore scope {scope!r}")
    whole = scope == SCOPE_EVERYTHING
    rs = _Restore(progress)
    if whole and prof.get("midi") is not None:
        rs.write("MIDI control", lambda: rcp_transfer.set_midi_control(bool(prof["midi"])))
    routing = prof.get("routing") or {}
    # THE FADER LAYOUT GOES FIRST, and the levels straight after it, before any channel processing.
    # Processing, effects and faderLevel are all addressed per fader STRIP (channels._strip_index),
    # so the layout decides where each of them has to be written. Written later, every restored
    # chain lands against the OLD layout and then has the layout moved out from under it.
    _restore_layout(rs, routing, fader_count, fader_code_set)
    _restore_levels(rs, routing, monitor=whole)
    _restore_channels(rs, prof.get("channels") or [])
    _restore_pairs(rs, routing, fader_count, fader_code_set)
    if whole:
        _restore_master(rs, routing)
        # The unit's own settings go last: none of them affects where another write lands.
        _restore_unit(rs, prof.get(UNIT_KEY) or {})
    return rs.result()


def _layout_codes(fader_code_set) -> set:
    """What a restore may put on a fader: an assignable channel, NOTHING (-1), and on a desk that
    links inputs, one of its stereo pairs.

    THE LAST TWO ARE NOT ASSIGNABLE AND MUST STILL BE RESTORED. Skip an empty fader and it keeps
    the channel it had, which the profile has just put on another fader as well. Skip a pair and
    the fader keeps one of its two mics, the other stays wherever it was, and a line source wired
    to that pair of inputs goes silent with every setting reading as saved. Writing the code alone
    is the whole of a link (faders.action_set_stereo_link)."""
    dev = devices.get_active_device()
    pairs = set(dev.fader.pair_map) if "stereo_link" in dev.caps else set()
    return set(fader_code_set) | {-1} | pairs


def _restore_layout(rs, routing, fader_count, fader_code_set):
    """The fader layout, on a device with the "fader_write" cap; refused, and the user told,
    elsewhere."""
    faders = routing.get("faders") or []
    if "fader_write" in devices.get_active_device().caps:
        rs.step("Restoring the fader layout.")
        allowed = _layout_codes(fader_code_set)
        for i, code in enumerate(faders):
            if i < fader_count and code is not None and code in allowed:
                rs.write(f"fader {i + 1}",
                         lambda i=i, code=code: rcp_transfer.set_fader_source(i, int(code)))
    elif any(c is not None for c in faders):
        rs.refuse("fader layout: changing faders is not enabled on this device, not restored")


def _restore_pairs(rs, routing, fader_count, fader_code_set):
    """The processing of each strip a stereo pair sits on (PAIR_CHAINS_KEY), after the layout has
    put the pair there and the channels have set its two inputs. Older profiles carry none."""
    saved = routing.get(PAIR_CHAINS_KEY) or {}
    dev = devices.get_active_device()
    if not saved or "fader_write" not in dev.caps:
        return
    allowed = _layout_codes(fader_code_set)
    for strip, code in enumerate((routing.get("faders") or [])[:fader_count]):
        chain = saved.get(str(code))
        if chain is None or code not in dev.fader.pair_map or code not in allowed:
            continue
        label = dev.fader.pair_label.get(code, f"pair {code}")
        rs.step(f"Restoring {label}.")
        rs.write(f"{label} processing",
                 lambda strip=strip, chain=chain: channels.write_chain(strip, chain))


def _restore_levels(rs, routing, monitor=True):
    """The virtual fader levels and, with `monitor`, the monitor speaker level. Gated on the
    device's own "level_write" cap AND on it having a virtual fader, the same refusal the Levels
    page's sliders make. Older profiles carry neither key and skip the whole block."""
    dev = devices.get_active_device()
    lvl_faders = dev.fader.level_faders if "level_write" in dev.caps else ()
    saved_levels = routing.get("fader_levels") or []
    mon_level = routing.get("monitor_level") if monitor else None
    if lvl_faders:
        rs.step("Restoring fader and monitor levels." if monitor else "Restoring fader levels.")
        for idx in lvl_faders:
            if idx < len(saved_levels) and saved_levels[idx] is not None:
                rs.write(f"fader {idx + 1} level", lambda idx=idx, v=saved_levels[idx]:
                         rcp_transfer.set_fader_level(idx, int(v)))
        if mon_level is not None:
            rs.write("monitor speaker level",
                     lambda v=mon_level: rcp_transfer.set_monitor_level(float(v)))
    elif saved_levels or mon_level is not None:
        rs.refuse("fader and monitor levels: setting levels is not enabled on this device, "
                  "not restored")


def _mic_types_now() -> dict:
    """{mic channel name: its mic type} as the desk holds them before the restore, or {} when
    unreadable, which makes every type write wait for its voicing."""
    try:
        return {rec.get("name"): rec.get("mic_type") for rec in channels.read_channels()
                if rec.get("kind") == "mic"}
    except Exception as exc:
        diagnostics.event("restore.mic_types_unread", str(exc))
        return {}


def _chain_of(name):
    """The channel's processing params from a FRESH dump. Through the model cache the second read
    returns the first one's blob, the two always agree, and `_await_voicing` ends at once."""
    try:
        blob = rcp_read.read_model(fresh=True)
        rec = next((r for r in channels.read_channels(blob) if r.get("name") == name), None)
    except Exception as exc:
        diagnostics.event("restore.chain_unread", str(exc), channel=name)
        return None
    return None if rec is None else dict(rec.get("params") or {})


def _await_voicing(name, timeout=8.0):
    """Block until two consecutive dumps show the strip's params unchanged, so the chain lands on a
    settled voicing.

    A voicing does not just set its default gain: it replaces the strip's whole processing chain,
    and it takes the desk a second or two to do it (Pro II). A chain write landing inside that
    window is thrown away: one sent 0.12 s after a type change reads back as the voicing's own
    value. Only paid when the voicing actually CHANGES."""
    end = time.monotonic() + timeout
    prev = _chain_of(name)
    # A desk that cannot be read now answers no sooner by being asked again at once.
    while prev is not None and time.monotonic() < end:
        cur = _chain_of(name)
        if cur is None or cur == prev:
            return
        prev = cur


def _restore_channels(rs, prof_channels):
    """Every channel's input settings, effects and processing. The bulk of a restore, so each
    channel reports itself: this is where the wait actually is."""
    if not prof_channels:
        return
    types_now = _mic_types_now()
    for ch_i, ch in enumerate(prof_channels):
        cname = ch.get("name")
        rs.step(f"Restoring {cname} ({ch_i + 1} of {len(prof_channels)}).")
        if ch.get("kind") == "mic":
            cnum = _MIC_NAME_TO_INT.get(cname)
            if cnum is None:
                continue
            _restore_mic(rs, ch, cname, cnum, types_now)
        # THE CONNECTED DESK's line channels, not the Pro II's. Testing against the Pro II map
        # silently never restores a Duo profile's Headset level.
        elif ch.get("level_db") is not None and cname in channels.line_level_channels():
            rs.write(f"{cname} level", lambda cname=cname, v=ch["level_db"]:
                     channels.set_channel_level(cname, int(v)))
        _restore_processing(rs, ch, cname)


def _restore_mic(rs, ch, cname, cnum, types_now):
    """A mic channel's type, gain, input switches and effects."""
    # THE MIC TYPE GOES FIRST, before the gain: CHOOSING A VOICING SETS THAT VOICING'S OWN DEFAULT
    # GAIN (Dynamic 45, Condenser 25, PodMic 50, Broadcaster 27, Procaster 50, NT1 30, NT1-A 31,
    # RE20 51, SM7B 55; only Line-in and Instrument leave the gain alone). The other way round, any
    # restore that CHANGES the voicing leaves the desk on the new voicing's default. A restore onto
    # the state the profile was saved from cannot show it, because its type write is a no-op; only
    # a clean restore, where the reset has moved the voicing, does.
    if ch.get("mic_type") is not None:
        code = rs.choice(f"{cname} mic type", ch["mic_type"], channels.MIC_TYPE,
                         lambda v: channels.set_mic_type(cnum, v))
        if code is not None and types_now.get(cname) != code:
            rs.step(f"Waiting for the {cname} voicing to finish applying.")
            _await_voicing(cname)
    if ch.get("level_db") is not None:
        rs.write(f"{cname} gain", lambda v=ch["level_db"]: channels.set_mic_gain(cnum, int(v)))
    if ch.get("phantom") is not None:
        rs.write(f"{cname} phantom", lambda v=ch["phantom"]: channels.set_phantom(cnum, bool(v)))
    if ch.get("polarity") is not None:
        rs.write(f"{cname} polarity",
                 lambda v=ch["polarity"]: channels.set_polarity(cnum, bool(v)))
    # INSTRUMENT GAIN is restored; INPUT TYPE deliberately is not. inputType is DERIVED from the
    # mic type (Line-in -> 2, Instrument -> 1, any mic voicing -> 0), so writing it as well could
    # only produce a state the desk's own UI cannot make. The instrument gain is not derived:
    # selecting Instrument sets it to 20 and leaving Instrument does not clear it. inputColour is
    # SAVE ONLY: it is a string and there is no string channel-property writer.
    if ch.get("instrument_gain") is not None:
        rs.write(f"{cname} instrument gain", lambda v=ch["instrument_gain"]:
                 channels.set_instrument_gain(cnum, int(v)))
    fx = ch.get("effects") or {}
    for eff in ("echo", "reverb"):
        if fx.get(eff) is not None:
            rs.write(f"{cname} {eff}", lambda eff=eff, v=fx[eff]:
                     channels.set_channel_effect(cnum, eff, bool(v)))
    for pkey in ("echo_params", "reverb_params"):
        for p, v in (fx.get(pkey) or {}).items():
            if v is not None:
                rs.write(f"{cname} {p}", lambda p=p, v=v:
                         channels.set_channel_effect_param(cnum, p, float(v)))


def _restore_processing(rs, ch, cname):
    """A channel's processing, mic or line.

    ORDER MATTERS (see channels.CHAIN_ADVANCED_PROP): the processing MODE goes FIRST, because in
    simple mode the desk derives the chain from the three VoxLab knobs, so an advanced chain
    restored onto a strip left in simple mode can be regenerated over the top. The preset-name
    marker goes LAST, because the device blanks it to -1 as soon as any parameter is written.
    Older profiles have neither key and skip both."""
    if ch.get("advanced") is not None:
        rs.write(f"{cname} processing mode", lambda v=ch["advanced"]:
                 channels.set_channel_advanced(cname, bool(v)))
    if ch.get("processing_on") is not None:
        rs.write(f"{cname} processing", lambda v=ch["processing_on"]:
                 channels.set_processing_enabled(cname, bool(v)))
    for mod_prop, on in (ch.get("modules") or {}).items():
        if on is not None:
            rs.write(f"{cname} {mod_prop}", lambda mod_prop=mod_prop, v=on:
                     channels.set_processing_module(cname, mod_prop, bool(v)))
    for prop, v in (ch.get("params") or {}).items():
        if v is not None and prop in channels.PRESET_FLOAT_PROPS:
            rs.write(f"{cname} {prop}", lambda prop=prop, v=v:
                     channels.set_channel_processing(cname, prop, float(v)))
    if ch.get("preset_index") is not None:
        rs.write(f"{cname} preset name", lambda v=ch["preset_index"]:
                 channels.set_channel_preset_marker(cname, int(v)))


_MASTER_OUT_KEYS = ("multitrack", "processing_bypass", "recording_format", "usb_multitrack",
                    "usb_expanded", "usb_processing_bypass", "monitor_auto_mute", "bt_auto_mute",
                    "headphone_types", "compellor_on", "compellor_threshold", "compellor_attack",
                    "compellor_release", "compellor_gain", "delay_on")


def _restore_master(rs, r):
    """The master and global output-block writes (multitrack, auto-mutes, headphone type,
    Compellor, delay). Gated on the device's "output_write" cap, because an unmapped global
    selector would append a stray: on a device without it they are refused, not written blind."""
    if "output_write" not in devices.get_active_device().caps:
        if any(r.get(k) is not None for k in _MASTER_OUT_KEYS):
            rs.refuse("master output settings (multitrack, auto-mute, headphone, Compellor, "
                      "delay): read-only on this device, not restored")
        return
    rs.step("Restoring master output settings.")
    if r.get("multitrack") is not None:
        rs.choice("multitrack", r["multitrack"], rcp_transfer.MULTITRACK_MODES,
                  rcp_transfer.set_multitrack_mode)
    if r.get("processing_bypass") is not None:
        rs.write("raw multitrack recording", lambda v=r["processing_bypass"]:
                 rcp_transfer.set_recording_processing_bypass(bool(v)))
    # Checked against the list like multitrack, and here it matters more: the desk stores
    # out-of-range values for this one, so a profile captured from a desk left on a 3 would
    # otherwise write that 3 straight back.
    if r.get("recording_format") is not None:
        rs.choice("recording format", r["recording_format"], rcp_transfer.RECORDING_FORMATS,
                  rcp_transfer.set_recording_format)
    # THE USB MODE GETS ITS OWN SPOKEN STEP: it re-enumerates the desk's USB audio function, so
    # every RODECaster input and output on the machine drops for a second or two. ONE WRITE FOR
    # BOTH HALVES, because the desk keeps them in one property: two writes would take two
    # re-enumerations and let the second undo the first.
    if r.get("usb_multitrack") is not None:
        def usb_mode(mode):
            rs.step("Setting what the desk sends this computer. Its audio devices will drop out "
                    "for a couple of seconds.")
            rcp_transfer.set_usb_multitrack_mode(
                recorder.join_usb_multitrack(mode, r.get("usb_expanded")))
        rs.choice("USB multitrack", r["usb_multitrack"], rcp_transfer.MULTITRACK_MODES, usb_mode)
    if r.get("usb_processing_bypass") is not None:
        rs.write("raw USB multitrack", lambda v=r["usb_processing_bypass"]:
                 rcp_transfer.set_usb_multitrack_bypass(bool(v)))
    if r.get("monitor_auto_mute") is not None:
        rs.write("monitor auto-mute", lambda v=r["monitor_auto_mute"]:
                 rcp_transfer.set_monitor_auto_mute(bool(v)))
    if r.get("bt_auto_mute") is not None:
        rs.write("Bluetooth auto-mute",
                 lambda v=r["bt_auto_mute"]: rcp_transfer.set_bt_auto_mute(bool(v)))
    # Headphone amp type, ONE PER OUTPUT, positional. Values are checked against the CONNECTED
    # device's spec rather than a literal, so "Low sensitivity" (2) survives, and the selector
    # comes from the same spec, so an unmapped headphone output is never written.
    hp_spec = devices.get_active_device().headphones
    for out_i, hp_val in enumerate(r.get("headphone_types") or []):
        if out_i >= len(hp_spec.outputs) or hp_val is None:
            continue
        hp_index, out_label = hp_spec.outputs[out_i]
        rs.choice(f"{out_label} type", hp_val, hp_spec.value_codes,
                  lambda v, i=hp_index: rcp_transfer.set_headphone_type(v, i))
    if r.get("compellor_on") is not None:
        rs.write("Compellor",
                 lambda v=r["compellor_on"]: rcp_transfer.set_master_compellor(bool(v)))
    for pk, profkey in (("threshold", "compellor_threshold"), ("attack", "compellor_attack"),
                        ("release", "compellor_release"), ("gain", "compellor_gain")):
        v = r.get(profkey)
        if v is not None:
            rs.write(f"Compellor {pk}",
                     lambda pk=pk, v=v: rcp_transfer.set_master_compellor_param(pk, float(v)))
    if r.get("delay_on") is not None:
        secs = r.get("delay_seconds")
        rs.write("output delay", lambda on=r["delay_on"], secs=secs:
                 rcp_transfer.set_output_delay(bool(on), float(secs) if secs is not None else None))


def _restore_unit(rs, unit):
    """The desk's own settings, on a device with the "unit_settings" cap. Only values a profile
    recorded are written, and only names in the address table, so a setting from a newer build
    that THIS build does not know is skipped rather than written on a guessed selector."""
    if not unit:
        return
    if "unit_settings" not in devices.get_active_device().caps:
        rs.refuse("desk settings (display, buttons, haptics, record button, listen mode): "
                  "read-only on this device, not restored")
        return
    rs.step("Restoring the desk's own settings.")
    for prop, v in sorted(unit.items()):
        if prop not in rcp_transfer.UNIT_SETTING_SEL or not isinstance(v, (int, float)):
            continue
        if prop in UNIT_NEVER_RESTORE:
            continue        # saved for the record, deliberately not written back
        rs.write(prop, lambda prop=prop, v=v: rcp_transfer.set_unit_setting(prop, v))


# ---- the check after a restore (pure: an already-read blob and channel read) ------------------
# A restore counts the writes it SENT. Whether the desk holds them is a different fact: a voicing
# still applying throws a chain write away, a channel with no fader has no strip to take one, and
# the desk clamps what it does not like. So the desk is read back and compared with the profile,
# setting by setting, over exactly what that scope writes, and what differs is said by name.

def _same(saved, now) -> bool:
    if isinstance(saved, bool) or isinstance(now, bool):
        return now is not None and bool(saved) == bool(now)
    if isinstance(saved, (int, float)) and isinstance(now, (int, float)):
        return abs(float(saved) - float(now)) <= CHECK_FLOAT_EPS
    return saved == now


def _said(value, names=None) -> str:
    """A value as the sentence says it: on or off, a name where there is one, a short number."""
    if value is None:
        return "nothing"
    if isinstance(value, bool):
        return "on" if value else "off"
    if names is not None and value in names:
        return str(names[value])
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".") or "0"
    return str(value)


class _Check:
    """The comparisons of one check: how many were made, and each difference as a phrase."""

    def __init__(self):
        self.checked = 0
        self.differ = []

    def same(self, what, saved, now, names=None):
        """Compare one setting the profile holds. A setting it does not hold is not checked."""
        if saved is None:
            return
        self.checked += 1
        if isinstance(saved, bool) and now is not None:
            now = bool(now)
        if not _same(saved, now):
            self.differ.append(f"{what} reads {_said(now, names)}, the profile says "
                               f"{_said(saved, names)}")

    def result(self) -> dict:
        return {"checked": self.checked, "differ": self.differ}


def check(prof, blob: bytes, chans: list, *, fader_count, fader_code_set,
          scope=SCOPE_EVERYTHING) -> dict:
    """Compare the desk, as `blob` and its channel read `chans` hold it, with what a restore of
    `prof` in `scope` writes. Returns {checked, differ: [phrase]}. No device access.

    WHAT IS LEFT OUT IS WHAT A RESTORE LEAVES OUT: mutes, which are never written back, the output
    mix matrix, which has its own count in the reply, and the pads."""
    dev = devices.get_active_device()
    ck = _Check()
    routing = prof.get("routing") or {}
    vec = rcp_read.strip_values(blob, "channelInputSource")
    names = dict(dev.fader.all_labels)
    names[-1] = "no channel"
    if "fader_write" in dev.caps:
        allowed = _layout_codes(fader_code_set)
        for i, code in enumerate((routing.get("faders") or [])[:fader_count]):
            if code is not None and code in allowed:
                ck.same(f"fader {i + 1}", int(code), vec[i] if i < len(vec) else None, names)
    if "level_write" in dev.caps:
        levels = rcp_transfer.fader_levels(blob)
        saved_levels = routing.get("fader_levels") or []
        for i in dev.fader.level_faders:
            if i < len(saved_levels):
                ck.same(f"fader {i + 1} level", saved_levels[i],
                        levels[i] if i < len(levels) else None)
    now_chans = {c.get("name"): c for c in chans or []}
    lines = channels.line_level_channels()
    for ch in prof.get("channels") or []:
        _check_channel(ck, ch, now_chans.get(ch.get("name")) or {}, lines)
    _check_pairs(ck, routing, blob, vec, fader_count)
    if scope == SCOPE_EVERYTHING:
        _check_whole(ck, prof, routing, blob)
    return ck.result()


def _check_channel(ck, ch, now, lines):
    """One channel: what `_restore_mic`, the line level and `_restore_processing` write."""
    label = ch.get("label") or ch.get("name")
    if ch.get("kind") == "mic":
        if ch.get("name") not in _MIC_NAME_TO_INT:
            return
        ck.same(f"{label} mic type", ch.get("mic_type"), now.get("mic_type"), channels.MIC_TYPE)
        ck.same(f"{label} gain", ch.get("level_db"), now.get("level_db"))
        ck.same(f"{label} phantom power", ch.get("phantom"), now.get("phantom"))
        ck.same(f"{label} polarity invert", ch.get("polarity"), now.get("polarity"))
        ck.same(f"{label} instrument gain", ch.get("instrument_gain"), now.get("instrument_gain"))
        fx, fx_now = ch.get("effects") or {}, now.get("effects") or {}
        for eff in ("echo", "reverb"):
            ck.same(f"{label} {eff}", fx.get(eff), fx_now.get(eff))
        for group in ("echo_params", "reverb_params"):
            for p, v in (fx.get(group) or {}).items():
                ck.same(f"{label} {p}", v, (fx_now.get(group) or {}).get(p))
    elif ch.get("name") in lines:
        ck.same(f"{label} level", ch.get("level_db"), now.get("level_db"))
    ck.same(f"{label} advanced processing", ch.get("advanced"), now.get("advanced"))
    ck.same(f"{label} processing", ch.get("processing_on"), now.get("processing_on"))
    for mod, on in (ch.get("modules") or {}).items():
        ck.same(f"{label} {mod}", on, (now.get("modules") or {}).get(mod))
    for prop, v in (ch.get("params") or {}).items():
        if prop in channels.PRESET_FLOAT_PROPS:
            ck.same(f"{label} {prop}", v, (now.get("params") or {}).get(prop))
    ck.same(f"{label} preset name", ch.get("preset_index"), now.get("preset_index"))


def _check_pairs(ck, routing, blob, vec, fader_count):
    """Each saved pair's strip (PAIR_CHAINS_KEY), counted as one setting: its processing."""
    saved = routing.get(PAIR_CHAINS_KEY) or {}
    dev = devices.get_active_device()
    chains = channels.read_chains(blob) if saved else []
    for strip, code in enumerate((routing.get("faders") or [])[:fader_count]):
        chain = saved.get(str(code))
        if chain is None or code not in dev.fader.pair_map:
            continue
        label = dev.fader.pair_label.get(code, f"pair {code}")
        ck.checked += 1
        if strip >= len(vec) or vec[strip] != code or strip >= len(chains):
            continue                # the layout line has already said the pair is not there
        off = [p for group in ("bools", "floats", "fx_bools", "fx_floats")
               for p, v in (chain.get(group) or {}).items()
               if v is not None and chains[strip][group].get(p) is not None
               and not _same(v, chains[strip][group].get(p))]
        if off:
            ck.differ.append(f"{label} processing differs from the profile in "
                             f"{count(len(off), 'value')}, the first being {off[0]}")


def _check_whole(ck, prof, routing, blob):
    """What only SCOPE_EVERYTHING writes: MIDI, the monitor speaker level, the master and output
    section, and the desk's own settings."""
    dev = devices.get_active_device()
    ck.same("MIDI control", prof.get("midi"),
            rcp_read.keyed_value(blob, rcp_transfer.SYSTEM, "systemMidiControl"))
    if "level_write" in dev.caps and dev.fader.level_faders:
        ck.same("the monitor speaker level", routing.get("monitor_level"),
                rcp_read.keyed_value(blob, rcp_transfer.OUTPUT, "outputMonLevel"))
    if "output_write" in dev.caps:
        now = _master_settings(blob)
        words = {"multitrack": "multitrack recording", "processing_bypass": "raw multitrack",
                 "recording_format": "the recording format", "usb_multitrack": "USB multitrack",
                 "usb_expanded": "USB Expanded", "usb_processing_bypass": "raw USB multitrack",
                 "monitor_auto_mute": "monitor auto-mute", "bt_auto_mute": "Bluetooth auto-mute",
                 "compellor_on": "the Compellor", "compellor_threshold": "Compellor threshold",
                 "compellor_attack": "Compellor attack", "compellor_release": "Compellor release",
                 "compellor_gain": "Compellor gain", "delay_on": "output delay",
                 "delay_seconds": "output delay time"}
        named = {"multitrack": rcp_transfer.MULTITRACK_MODES,
                 "usb_multitrack": rcp_transfer.MULTITRACK_MODES,
                 "recording_format": rcp_transfer.RECORDING_FORMATS}
        for key, what in words.items():
            # The delay time is written only with the delay switch, and the USB halves together.
            if key == "delay_seconds" and routing.get("delay_on") is None:
                continue
            if key == "usb_expanded" and routing.get("usb_multitrack") is None:
                continue
            ck.same(what, routing.get(key), now.get(key), named.get(key))
        spec = dev.headphones
        for i, saved in enumerate((routing.get("headphone_types") or [])[:len(spec.outputs)]):
            have = now["headphone_types"]
            if saved in spec.value_codes:
                ck.same(f"{spec.outputs[i][1]} type", saved, have[i] if i < len(have) else None,
                        spec.value_label)
    if "unit_settings" in dev.caps:
        now_unit = _unit_settings(blob)
        for prop, v in sorted((prof.get(UNIT_KEY) or {}).items()):
            if (prop in rcp_transfer.UNIT_SETTING_SEL and prop not in UNIT_NEVER_RESTORE
                    and isinstance(v, (int, float))):
                ck.same(prop, v, now_unit.get(prop))


def check_words(result: dict) -> str:
    """The check as ONE sentence for someone listening: that everything matches, or how many
    settings differ and the first few by name."""
    checked, differ = result["checked"], result["differ"]
    if not differ:
        return f"Read the desk back: all {count(checked, 'setting')} checked match the profile."
    named = differ[:CHECK_NAMED]
    more = len(differ) - len(named)
    verb = "differs" if len(differ) == 1 else "differ"
    return (f"Read the desk back: {count(len(differ), 'setting')} of {checked} {verb}. "
            + "; ".join(named) + (f"; and {more} more" if more else "") + ".")


def restore_result(safe, res, checked=None, scope=SCOPE_EVERYTHING) -> dict:
    """Format the user-facing message for a completed restore. `checked` is `check`'s result when
    the desk was read back, and then its sentence replaces the advice to go and look."""
    applied, failed, errs = res["applied"], res["failed"], res["errs"]
    what = "profile" if scope == SCOPE_EVERYTHING else "the setup from"
    msg = f"Restored {what} {safe}: applied {applied} settings"
    if failed:
        msg += f", {failed} could not be set ({'; '.join(errs)})"
    msg += ". " + (check_words(checked) if checked is not None else
                   "Open Channels, Processing or Routing to read them back and confirm.")
    return {"ok": True, "applied": applied, "failed": failed, "message": msg}
