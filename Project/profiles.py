"""AccessCaster - settings profiles (full backup + restore).

A profile is a JSON snapshot of EVERY setting the app can read AND write, so it round-trips the
whole desk: MIDI; for every channel the device exposes (Mic 1-4, Bluetooth, USB1, Chat, USB2, and
the SMART pads bus, which has a chain but no level) the level, plus for
mics phantom/polarity/mic type and the always-on echo+reverb effects, and for every channel the
full processing chain (master on/off, each module's on/off, all 33 params); and routing (the full
nine-fader layout, the virtual fader levels and the monitor speaker level, multitrack mode, the two
auto-mutes, each headphone output's amp type, the Master Compellor on/off + its four params, and
output delay); and the unit's own settings (screen and button brightness, auto-brightness, screen
dim time, meter style, haptics, what the record button does, listen mode and the monitor's fixed
output level). SMART pad SOUNDS are files, not settings, and are a separate optional backup, so they
are not in here yet.

FORMAT: a profile is a FOLDER, profiles/<name>/profile.json (version 2). Legacy flat
profiles/<name>.json (the old settings-subset) are still listed and still restore, so nothing
already saved is lost.

LOCK BOUNDARY: this module never imports the server, so there is no circular import. save() and
load() are pure (file ops + an already-read model blob). apply() performs the device WRITES and
assumes THE CALLER HOLDS the HID lock - the server owns that lock and reads the model under it,
then hands the blob / loaded profile here.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time

import channels
import devices
import rcp_read
import rcp_transfer
import routing_schemes
from paths import data_path

# Saved profiles are user data, kept in the writable %LOCALAPPDATA%\AccessCaster\profiles so they
# survive app reinstall/uninstall and work no matter where the app is installed.
PROFILES_DIR = data_path("profiles")
PROFILE_VERSION = 2
RESTORE_PACE = 0.02            # seconds between device writes during a restore
# Mic channel name -> 1-based number. Pro II-shaped (Mic3/Mic4), but SAFE across devices because
# restore refuses a cross-device restore (GAP-4): a Duo profile only ever carries Mic1/Mic2, and a
# stray higher channel would fail the device's own mic guard (channels._mic_path) anyway.
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
# password among them. Profiles live in %LOCALAPPDATA% but people do share them (two of our users
# are on a friend's machine), so anything password-shaped is replaced with None and named in the
# profile's "redacted" list. SSIDs and usernames are kept: they identify the setup, not access it.
_REDACT = re.compile(r"psk|password|passwd|secret|token|rodecode", re.I)
SNAPSHOT_KEY = "device_snapshot"

# ---- the AS-FOUND baseline --------------------------------------------------------------------
# One profile per desk may be marked as the baseline: the state the unit was in before this app ever
# wrote to it, captured on first run (server.baseline_status). It is an ordinary profile in every
# other way - same format, same restore path - but it is the only copy of the desk as it came, and
# there is no software factory reset to fall back on if it is lost. So it gets two protections that
# a normal profile does not: an ordinary save can never overwrite that name (a name collision would
# silently replace the one thing that cannot be recreated), and deleting it takes a deliberate
# second step rather than the same click as any other profile.
BASELINE_KEY = "baseline"

# ---- the paired show ---------------------------------------------------------------------------
# {"snapshot": int, "uuid": str, "name": str}: the desk's own backup taken at the same moment as
# this profile. Present only when there was a card or drive in the unit to export to, since a show
# can only live on removable storage (the internal volume is the pads partition and cannot hold
# one). This is what lets ONE Backups list offer both: restore prefers the show, which is exact and
# takes seconds, and falls back to this file when the show has gone with the drive. The profile
# also supplies the NAME, because the unit calls every export after the loaded show and gives no
# way to rename one, so a shows-only list reads as several identical lines.
SHOW_KEY = "show"

# ---- the unit's own settings (display, buttons, haptics, listen mode) --------------------------
# Writable (rcp_transfer.UNIT_SETTING_SEL), and a profile carries them. They are read in ONE pass
# with snapshot_properties rather than name by name, because there are forty-odd of them.
# (Name-by-name is safe too: the tree index matches whole names, not substrings.)
#
# WHY IT IS SAFE TO RESTORE THESE and not, say, an undecoded property: every one has a CONFIRMED
# selector-plus-name pairing, read back on hardware. The restore is still gated on the device's own
# "unit_settings" cap, so a desk whose selectors were never observed refuses instead of guessing.
UNIT_KEY = "unit"

# SAVED BUT NEVER WRITTEN BACK. Two entries in the address table are not settings at all, they are
# live MUTES that happen to live in the same block, and restoring a mute is the one thing a profile
# must not do: the whole point of this file is that a restore is safe to run mid-session, and
# silencing the speakers or the Bluetooth send because that is how they sat when the profile was
# taken would be a nasty surprise with no visible cause. Exactly the reasoning that keeps
# channelOutputMute out (see the profile-coverage report); these are its output-side equivalents.
# They ARE saved, so the full snapshot still records how the desk was found.
UNIT_NEVER_RESTORE = frozenset({"outputMonMute", "outputBTMute"})


def _unit_settings(props: dict) -> dict:
    """{prop: int|float} for every unit setting the dump carries. Bools are stored as 0/1 so the
    profile round-trips through JSON unambiguously; a property the desk did not report is left out.

    FLOATS are kept as floats (outputBTLevel is the first one). Before that every entry
    in the table happened to be an int or a bool, and an int-only filter looked harmless - it would
    have dropped the Bluetooth output level silently, which is the failure mode this whole audit
    trail exists to prevent."""
    out = {}
    for prop in rcp_transfer.UNIT_SETTING_SEL:
        vals = props.get(prop) or []
        v = vals[0] if vals else None
        if isinstance(v, bool):
            out[prop] = int(v)
        elif isinstance(v, (int, float)):
            out[prop] = v
    return out


# ---- naming + listing (pure file ops) ------------------------------------------------------

def safe_name(name: str):
    name = (name or "").strip()
    if not re.match(r"^[A-Za-z0-9 _-]{1,40}$", name):
        return None
    return name


def _list_profile_names() -> list:
    """Every saved profile: new-format folders (containing profile.json) and legacy flat
    <name>.json files, de-duplicated."""
    names = set()
    try:
        for entry in os.listdir(PROFILES_DIR):
            full = os.path.join(PROFILES_DIR, entry)
            if entry.endswith(".json") and os.path.isfile(full):
                names.add(entry[:-5])
            elif os.path.isdir(full) and os.path.isfile(os.path.join(full, "profile.json")):
                names.add(entry)
    except FileNotFoundError:
        return []
    return sorted(names)


def _profile_read_path(safe: str):
    """Path to load a profile from: prefer the new folder format, fall back to legacy flat."""
    folder = os.path.join(PROFILES_DIR, safe, "profile.json")
    if os.path.isfile(folder):
        return folder
    flat = os.path.join(PROFILES_DIR, safe + ".json")
    return flat if os.path.isfile(flat) else None


def list_profiles() -> dict:
    return {"ok": True, "profiles": _list_profile_names()}


def pads_dir(safe: str) -> str:
    """Folder where a profile's SMART pad sound files live: profiles/<safe>/pads/."""
    return os.path.join(PROFILES_DIR, safe, "pads")


# ---- save (build the dict from an already-read model blob; no device access) ---------------

def _snapshot(blob: bytes, props: dict | None = None) -> dict:
    """Every property in the dump, redacted (see _REDACT). Reference only; never written back.

    `props` lets the caller hand in an already-walked dump (save() needs the same walk for the unit
    settings) instead of walking 55 KB twice. NOTE: redaction MUTATES it, so anything read out of
    that dict must be read BEFORE this is called."""
    if props is None:
        props = rcp_read.snapshot_properties(blob)
    redacted = sorted(n for n in props if _REDACT.search(n))
    for n in redacted:
        props[n] = [None] * len(props[n])
    return {
        "note": ("Every property the unit reported, for reference and for future restores. "
                 "AccessCaster restores from the named settings above, never from here, so a "
                 "property we have not decoded can never be blind-written onto a desk."),
        "properties": props,
        "property_count": len(props),
        "value_count": sum(len(v) for v in props.values()),
        "redacted": redacted,
    }


def save(safe: str, blob: bytes, chans: list, firmware, fader_count: int, pads=None,
         product_id=None, device_name=None, monitor_output_indices=None,
         headphone_output_count=None, baseline=False, show=None) -> dict:
    """Build the full profile from an ALREADY-READ model `blob` + channel read `chans`, and write
    it to profiles/<safe>/profile.json. No device access - the server did the read (and, for the
    optional `pads` backup, the pad read + file copy) under the lock. `pads` is the metadata list
    from soundpad.backup_pads (sound files already copied into profiles/<safe>/pads/); when given it
    is stored under the profile's "pads" key so restore can rebuild the grid.

    `fader_count` is the CAPTURE length (devices.FaderSpec.capture_length), which is deliberately
    allowed to exceed the number of slots restore will WRITE (FaderSpec.vector_length): both desks
    carry a 10-slot channelInputSource whose last slot is an unexposed spare mirroring USB 2, so we
    record it for the round trip while `_apply_v2` writes only the first `fader_count` it is given.

    GAP-4 (device-awareness): `product_id`/`device_name` stamp the desk the profile was saved on so
    restore can REFUSE a cross-device restore (a Pro II profile's fader vector, matrix shape and
    channel set do not map onto a Duo). `monitor_output_indices` is the list of monitor-mix output
    indices the CONNECTED device exposes (Pro II = 0-4 Headphone 1-4 + Monitor; Duo = 0,1,4), so we
    snapshot only that device's real monitor outputs rather than the Pro II-hardcoded range(5).
    `headphone_output_count` is how many headphone outputs the connected device addresses
    (devices.HeadphoneTypeSpec.outputs), so we record ONE TYPE PER OUTPUT rather than the single
    Headphone-1 value the old format kept.

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
                f"'{safe}' is the as-found baseline of this unit, the only record of how it was "
                f"before this app changed anything, so it cannot be replaced by an ordinary save. "
                f"Choose another name."}
    if monitor_output_indices is None:
        monitor_output_indices = list(range(5))     # Pro II fallback (Headphone 1-4 + Monitor)
    # ONE walk of the dump, shared by the unit settings below and by the full snapshot. Read the
    # unit settings out of it FIRST: _snapshot redacts in place (none of these is password-shaped,
    # but relying on that would be a trap for whoever adds the next setting).
    all_props = rcp_read.snapshot_properties(blob)
    unit = _unit_settings(all_props)
    if headphone_output_count is None:
        headphone_output_count = len(devices.get_active_device().headphones.outputs)
    def first(n):
        return rcp_read.first(blob, n)

    cis = rcp_read.values(blob, "channelInputSource")
    midi = first("systemMidiControl")

    # The two levels the desk otherwise keeps to itself (writes proven on hardware): each fader's
    # faderLevel (0..127, per STRIP, only the VIRTUAL slots are ours to write) and the monitor
    # SPEAKER level (outputMonLevel, a 0..1 float). Both are recorded RAW, so a profile round-trips
    # them exactly. Like the fader vector, capture is deliberately wider than write: we store every
    # slot's value and _apply_v2 writes only the slots this device has a confirmed level selector for.
    # A value that fails to decode is kept as None rather than dropped: the list is POSITIONAL
    # (index = strip), and dropping one would shift every level after it onto the wrong fader.
    fader_levels = [int(v) if isinstance(v, (int, float)) else None
                    for v in rcp_read.values(blob, "faderLevel")]
    mon_level = first("outputMonLevel")
    # ONE HEADPHONE TYPE PER OUTPUT (GAP-6). The dump carries them in output order, mapped
    # positionally to the outputs the device addresses - the same read read_routing_live does, and
    # the ordering is hardware-confirmed. The old single "headphone_type" key is still written (it
    # is Headphone 1's value) so a profile saved now still restores on an older build.
    hp_all = [v for v in rcp_read.values(blob, "headphoneType") if v is not None and v != ""]
    headphone_types = [hp_all[i] if i < len(hp_all) else None
                       for i in range(int(headphone_output_count))]

    # THE OUTPUT MIX MATRIX IS CAPTURED AS A MIX SCHEME. routing_schemes.capture already records
    # the WHOLE matrix - every output's mode, every cross-point's include/exclude, its link state
    # and, where unlinked, its manual level - keyed by NAME rather than by index, so a profile
    # stores that scheme verbatim rather than keeping a second, weaker copy of the same read.
    #
    # TWO THINGS A NARROWER CAPTURE GETS WRONG:
    #   - it walked `monitor_output_indices` ONLY, so a level set on the REC, USB or Chat mix was
    #     simply not backed up. Every output is captured now.
    #   - it recorded a level for EXCLUDED cross-points, whose "level" is an inert number frozen at
    #     wherever the fader stood when the exclude unlinked it (see PROJECT_GUIDE). capture records
    #     nothing but `excluded` for those, which is what made mix schemes round-trip cleanly.
    # Writing a manual level to a non-monitor cross-point is hardware-proven (the mix-scheme live
    # test set one on the Chat output and read it back), so no new capability is being assumed here.
    #
    # The read refuses if the matrix changed shape (a phone caller) or if this device's matrix read
    # is unverified, so we store null + a note rather than a wrong snapshot. Same blob, no extra read.
    mix = routing_schemes.capture(safe, blob)
    mix_scheme = mix["scheme"] if mix.get("ok") else None
    matrix_note = "" if mix.get("ok") else mix.get("message", "matrix not readable")
    # LEGACY KEYS, still written so a profile saved by this build still restores on the INSTALLED
    # v2.00, which knows only these three. They are derived from the same read; nothing here is a
    # second device access. Restore prefers "mix_scheme" and falls back to these (server._apply_matrix).
    ms = rcp_read.read_matrix_state(blob)
    ml = rcp_read.read_matrix_levels(blob)
    output_modes = None
    matrix_excluded = []
    monitor_unlinked = []
    if ms.get("ok"):
        modes = ms["modes"]
        output_modes = [int(modes[o]) for o in sorted(modes)]
        for o, srcs in ms["excluded"].items():
            for s, ex in srcs.items():
                if ex:
                    matrix_excluded.append([int(o), int(s)])
    if ml.get("ok"):
        for o in monitor_output_indices:   # the monitor outputs THIS device exposes (device-aware, GAP-4)
            for s, cell in ml["levels"].get(o, {}).items():
                if cell.get("linked") is False and cell.get("level") is not None:
                    monitor_unlinked.append({"o": int(o), "s": int(s), "level": float(cell["level"])})

    profile = {
        "version": PROFILE_VERSION,
        "name": safe,
        "saved_at": time.time(),
        # The desk this profile was saved on (GAP-4). restore refuses a cross-device restore when
        # product_id is present and differs from the connected device. Older profiles have neither
        # key (all saved on the Pro II historically); those are allowed and rely on per-write guards.
        "product_id": product_id,
        "device_name": device_name,
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
            "multitrack": first("recordingMultitrackMode"),
            # The rest of the "what do I end up with" set. Saving the multitrack
            # MODE without its processing bypass backed up half a decision: a profile taken on
            # raw pre-fader tracks would come back processed, which is not recoverable after the
            # take. `usb_multitrack` is the USB destination's own mode (see recorder.py).
            "processing_bypass": first("recordingProcessingBypass"),
            # And WHAT IT IS WRITTEN AS, for the same reason: a profile that restores what the take
            # contains but not whether it is a WAV or an MP3 has backed up half the decision, and
            # neither half is recoverable once the take exists.
            "recording_format": first("recordingCompressionQuality"),
            "usb_multitrack": first("outputMultiMode"),
            "usb_processing_bypass": first("outputMultiBypass"),
            "monitor_auto_mute": first("outputMonAutoMute"),
            "bt_auto_mute": first("outputBTAutoMute"),
            "headphone_type": hp_all[0] if hp_all else None,   # Headphone 1, kept for old builds
            "headphone_types": headphone_types,                # one per addressable output
            # The master bus's Compellor and output delay, seven keys, built in rcp_read so the
            # page and the backup cannot drift apart.
            **rcp_read.master_processing(blob),
            # THE WHOLE output mix matrix, as a mix scheme (see the capture comment above). This is
            # what restore applies; the three keys under it are the legacy form, kept for old builds.
            "mix_scheme": mix_scheme,
            "output_modes": output_modes,          # 11 per-output modes (0 Main / 1 MixMinus / 2 Custom)
            "matrix_excluded": matrix_excluded,     # [[output, source], ...] excluded in Custom outputs
            "monitor_unlinked": monitor_unlinked,   # [{o, s, level}, ...] manual monitor levels
            "matrix_note": matrix_note,             # set if the matrix could not be read (phone caller)
        },
        # The unit's own settings (display, button brightness, haptics, REC button, listen mode,
        # fixed output level). Read above, before _snapshot redacts the shared walk.
        UNIT_KEY: unit,
        # wifi is recorded for reference only; it is not re-applied on restore.
        "wifi": {"ssid": first("wifiSSID") or "", "ip": first("wifiIpAddress") or ""},
        "firmware": firmware,
        SNAPSHOT_KEY: _snapshot(blob, all_props),
    }
    if pads is not None:                  # optional SMART pad backup (files already copied)
        profile["pads"] = pads
    if show is not None:
        profile[SHOW_KEY] = show
    if baseline:
        profile[BASELINE_KEY] = True

    try:
        os.makedirs(os.path.join(PROFILES_DIR, safe), exist_ok=True)
        with open(os.path.join(PROFILES_DIR, safe, "profile.json"), "w", encoding="utf-8") as f:
            json.dump(profile, f, indent=1)
    except OSError as exc:
        return {"ok": False, "message": f"Could not save the profile: {exc}"}
    snap = profile[SNAPSHOT_KEY]
    msg = (f"Saved profile '{safe}' with the unit's full current settings, and a record of all "
           f"{snap['property_count']} settings the unit reports.")
    if pads is not None:
        msg += f" Included {len(pads)} SMART pad(s)."
    return {"ok": True, "name": safe, "message": msg}


def load(name: str) -> dict:
    """Resolve + read a profile file. Returns {ok:True, safe, prof} or {ok:False, message}."""
    safe = safe_name(name)
    path = _profile_read_path(safe) if safe else None
    if not safe or not path:
        return {"ok": False, "message": f"Profile '{name}' not found."}
    try:
        with open(path, encoding="utf-8") as f:
            return {"ok": True, "safe": safe, "prof": json.load(f)}
    except (OSError, ValueError) as exc:
        return {"ok": False, "message": f"Could not read the profile: {exc}"}


def is_baseline(prof: dict) -> bool:
    """Is this loaded profile the as-found baseline? Tolerant of an older profile that has no such
    key at all, which is simply not one."""
    return bool((prof or {}).get(BASELINE_KEY))


def find_baseline() -> dict:
    """{name, pads, saved_at, device_name} for the baseline profile, or {} if there is none.

    Reads each profile once. The store is small (a handful of profiles), and this only runs when the
    page asks whether to offer a first-run capture, so the cost is not worth caching and getting
    wrong."""
    for name in _list_profile_names():
        got = load(name)
        if got.get("ok") and is_baseline(got["prof"]):
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
        with open(os.path.join(folder, "profile.json"), "w", encoding="utf-8") as f:
            json.dump(prof, f, indent=1)
    except OSError as exc:
        return {"ok": False, "message": f"Could not update the profile: {exc}"}
    return {"ok": True, "name": safe,
            "message": f"Added {len(pads)} SMART pad(s) to '{safe}'. Its settings are still the "
                       f"ones captured when it was saved."}


def _child_of_profiles(path: str) -> bool:
    """True when path is a DIRECT child of PROFILES_DIR. Every destructive file operation in this
    module checks this after safe_name, so a crafted name can never reach outside the store."""
    return os.path.dirname(os.path.abspath(path)) == os.path.abspath(PROFILES_DIR)


def rename(name: str, new_name: str) -> dict:
    """Rename a saved profile in place. Moves the whole folder, so the pads/ audio travels with it,
    and handles a legacy flat <name>.json too. No device access."""
    old = safe_name(name)
    new = safe_name(new_name)
    if not old:
        return {"ok": False, "message": f"Profile '{name}' not found."}
    if not new:
        return {"ok": False, "message": "The new name must be 1 to 40 letters, digits, spaces, "
                                        "dashes or underscores."}
    if new == old:
        return {"ok": False, "message": "That is already the profile's name."}
    if new in _list_profile_names():
        return {"ok": False, "message": f"There is already a profile called '{new}'. "
                                        f"Pick another name."}
    old_folder = os.path.join(PROFILES_DIR, old)
    new_folder = os.path.join(PROFILES_DIR, new)
    old_flat = os.path.join(PROFILES_DIR, old + ".json")
    new_flat = os.path.join(PROFILES_DIR, new + ".json")
    if not all(_child_of_profiles(p) for p in (old_folder, new_folder, old_flat, new_flat)):
        return {"ok": False, "message": "That profile name is not one this app can rename."}
    try:
        if os.path.isdir(old_folder):
            os.rename(old_folder, new_folder)
        elif os.path.isfile(old_flat):
            os.rename(old_flat, new_flat)
        else:
            return {"ok": False, "message": f"Profile '{old}' not found."}
    except OSError as exc:
        return {"ok": False, "message": f"Could not rename the profile: {exc}"}
    return {"ok": True, "message": f"Renamed '{old}' to '{new}'."}


def delete(name: str, force: bool = False) -> dict:
    """Delete a saved profile from this computer: the new-format folder (profile.json plus its
    pads/ audio) and any legacy flat <name>.json of the same name. No device access.

    The name goes through safe_name first (so no separators, no dots, no traversal), and the
    resolved path is then checked to be a DIRECT child of PROFILES_DIR before anything is removed.
    That second check is the one that matters: this is the only recursive delete in the app, and it
    must not be reachable with a crafted name.

    The AS-FOUND baseline needs `force`. It is the only record of the desk before this app touched
    it and there is no software factory reset to rebuild it from, so it does not go the same way as
    an ordinary profile: the caller gets `baseline: True` back and has to ask first."""
    safe = safe_name(name)
    if not safe:
        return {"ok": False, "message": f"Profile '{name}' not found."}
    if not force:
        got = load(safe)
        if got.get("ok") and is_baseline(got["prof"]):
            return {"ok": False, "baseline": True, "name": safe, "message":
                    f"'{safe}' is the as-found baseline: how this unit was set up before "
                    f"AccessCaster changed anything. It cannot be captured again, and the unit has no "
                    f"software factory reset to fall back on. Confirm to delete it anyway."}
    folder = os.path.join(PROFILES_DIR, safe)
    flat = os.path.join(PROFILES_DIR, safe + ".json")
    if not _child_of_profiles(folder) or not _child_of_profiles(flat):
        return {"ok": False, "message": "That profile name is not one this app can delete."}
    removed = False
    try:
        if os.path.isdir(folder):
            shutil.rmtree(folder)
            removed = True
        if os.path.isfile(flat):
            os.remove(flat)
            removed = True
    except OSError as exc:
        return {"ok": False, "message": f"Could not delete the profile: {exc}"}
    if not removed:
        return {"ok": False, "message": f"Profile '{safe}' not found."}
    return {"ok": True, "message": f"Deleted the profile '{safe}' from this computer. "
                                   f"The unit's settings were not changed."}


# ---- apply (device WRITES; THE CALLER MUST HOLD THE HID LOCK) -------------------------------

def apply(prof, *, fader_count, fader_code_set, input_strip_vec_index,
          valid_source_codes, mic_channels, progress=None) -> dict:
    """Apply a loaded profile to the device, dispatching by format. Returns {applied, failed, errs}.
    THE CALLER MUST HOLD THE HID LOCK (this module does no locking). progress, if given, is called
    with a short human sentence at each section boundary, so a caller polling from a UI has
    something to say during the ~20 seconds of paced writes instead of one frozen message."""
    if "channels" in prof:                       # version 2 (full) profile
        return _apply_v2(prof, fader_count=fader_count, fader_code_set=fader_code_set,
                         mic_channels=mic_channels, progress=progress)
    return _apply_legacy(prof, input_strip_vec_index=input_strip_vec_index,   # legacy subset
                         valid_source_codes=valid_source_codes, mic_channels=mic_channels)


def _apply_v2(prof, *, fader_count, fader_code_set, mic_channels, progress=None) -> dict:
    """Apply a version-2 (full) profile. Paces the writes and keeps going past an individual
    failure so one odd value cannot abort the whole restore."""
    applied = 0
    failed = 0
    errs = []

    def step(msg):
        if progress:
            progress(msg)

    def w(desc, fn):
        nonlocal applied, failed
        try:
            fn()
            applied += 1
        except Exception as exc:
            failed += 1
            if len(errs) < 6:
                errs.append(f"{desc}: {exc}")
        time.sleep(RESTORE_PACE)

    if prof.get("midi") is not None:
        w("MIDI control", lambda: rcp_transfer.set_midi_control(bool(prof["midi"])))

    r = prof.get("routing") or {}
    faders = r.get("faders") or []
    # THE FADER LAYOUT GOES FIRST, before any channel processing. Processing and effects are
    # addressed per fader STRIP (channels._strip_index; per-strip behaviour hardware-confirmed
    # so the layout decides where each channel's chain has to be written. Run this block LAST and
    # every restored chain is written against the OLD layout and then has the layout moved out from
    # under it, so a profile saved with a reordered layout restores its voicings onto the wrong
    # channels.
    # Still gated on the device's "fader_write" cap (GAP-4, folding the GAP-3 follow-up): the Duo's
    # fader vector carries empty strips encoded as -1 and its write semantics are unconfirmed, so
    # writing a saved layout there is exactly the unconfirmed path GAP-1 refuses. On a device
    # without the cap we SKIP the fader restore (and tell the user) rather than write it.
    if "fader_write" in devices.get_active_device().caps:
        step("Restoring the fader layout.")
        for i, code in enumerate(faders):
            if i < fader_count and code is not None and code in fader_code_set:
                w(f"fader {i + 1}", lambda i=i, code=code: rcp_transfer.set_fader_source(i, int(code)))
                # Keep the NAMING layout current too, not just the write cache: our own writes are
                # not echoed on the pushed stream, so without this the announcer would keep naming
                # the pre-restore channel on an encoder press.
                channels.note_strip_source(i, int(code))
        channels.invalidate_strip_vector()   # the layout is the processing strip map
    elif any(c is not None for c in faders):
        failed += 1
        errs.append("fader layout: changing faders is not enabled on this device, not restored")

    # VIRTUAL FADER LEVELS + THE MONITOR SPEAKER LEVEL. These go straight AFTER the layout and
    # before anything else, for the same reason the layout goes first: faderLevel is addressed per
    # STRIP, so the level has to land on the strip the saved layout just put that channel on.
    # Gated on the device's own "level_write" cap AND on it having confirmed level selectors (the
    # Duo has neither yet), so an unmapped selector is never written - the same refusal the Routing
    # page's level sliders make. Older profiles carry neither key and skip the whole block.
    dev = devices.get_active_device()
    lvl_sel = dev.fader.level_selectors if "level_write" in dev.caps else {}
    saved_levels = r.get("fader_levels") or []
    mon_level = r.get("monitor_level")
    if lvl_sel:
        step("Restoring fader and monitor levels.")
        for idx, sel in sorted(lvl_sel.items()):
            if idx < len(saved_levels) and saved_levels[idx] is not None:
                w(f"fader {idx + 1} level", lambda sel=sel, v=saved_levels[idx]:
                  rcp_transfer.set_fader_level(sel, int(v)))
        if mon_level is not None:
            w("monitor speaker level", lambda v=mon_level: rcp_transfer.set_monitor_level(float(v)))
    elif saved_levels or mon_level is not None:
        failed += 1
        errs.append("fader and monitor levels: setting levels is not enabled on this device, "
                    "not restored")

    # A MIC TYPE THAT CHANGES IS LET FINISH BEFORE THAT CHANNEL'S CHAIN IS WRITTEN.
    # A voicing does not just set its default gain: it replaces the strip's whole processing chain,
    # and it takes the desk a second or two to do it (Pro II). A chain write landing
    # inside that window is thrown away - PROVEN directly, by writing eqLowGain 0.3125 exactly 0.12 s
    # after a type change and reading back the voicing's 0.59375 instead.
    # THIS IS INSURANCE, NOT A BUG BEING FIXED: with the wait disabled, an end-to-end restore still
    # keeps its chain, because the real writes between the type and the params take longer than the
    # 0.12 s the probe used. So the path passes on
    # timing it never asserted, which would vary with the voicing pair, the device and any future
    # change to what gets written in between. The wait makes it not depend on luck.
    # Only paid when the voicing actually CHANGES, which is why the current types are read once here
    # rather than a settle being run for every channel.
    types_now = {}
    try:
        for rec in channels.read_channels():
            if rec.get("kind") == "mic":
                types_now[rec.get("name")] = rec.get("mic_type")
    except Exception:
        types_now = {}                         # unreadable: settle after every type write instead

    def _chain_of(name):
        try:
            rec = next((r for r in channels.read_channels() if r.get("name") == name), None)
        except Exception:
            return None
        return None if rec is None else dict(rec.get("params") or {})

    def _await_voicing(name, timeout=8.0):
        """Block until the strip's params stop moving, so the chain lands on a settled voicing."""
        end = time.time() + timeout
        prev = _chain_of(name)
        while time.time() < end:
            cur = _chain_of(name)
            if cur is not None and cur == prev:
                return
            prev = cur

    # The per-channel writes are the bulk of a restore (every level, effect and processing
    # parameter, paced), so each channel reports itself: this is where the wait actually is.
    prof_channels = prof.get("channels") or []
    for ch_i, ch in enumerate(prof_channels):
        cname = ch.get("name")
        kind = ch.get("kind")
        step(f"Restoring {cname} ({ch_i + 1} of {len(prof_channels)}).")
        if kind == "mic":
            cnum = _MIC_NAME_TO_INT.get(cname)
            if cnum is None:
                continue
            # THE MIC TYPE GOES FIRST, before the gain, for the same reason the fader layout goes
            # before the processing: CHOOSING A VOICING SETS THAT VOICING'S OWN DEFAULT GAIN.
            # Measured on Mic 3: Dynamic 45,
            # Condenser 25, PodMic 50, Broadcaster 27, Procaster 50, NT1 30, NT1-A 31, RE20 51,
            # SM7B 55. Only Line-in and Instrument leave the gain alone.
            # Write the gain AFTER the type. The other way round, any restore that actually
            # CHANGES the voicing throws the restored gain away and leaves the desk on the new
            # voicing's default. It is invisible to the obvious test, because a restore onto the
            # state the profile was saved from writes the type it is already on and that write is a
            # no-op; only a CLEAN restore, where the reset has moved the voicing, shows it.
            mt = ch.get("mic_type")
            if mt is not None and int(mt) in channels.MIC_TYPE:
                changing = types_now.get(cname) != int(mt)
                w(f"{cname} mic type", lambda cnum=cnum, v=mt: channels.set_mic_type(cnum, int(v)))
                # See _await_voicing: the chain below is written into the voicing's own window
                # otherwise, and the desk wins.
                if changing:
                    step(f"Waiting for the {cname} voicing to finish applying.")
                    _await_voicing(cname)
            if ch.get("level_db") is not None:
                w(f"{cname} gain", lambda cnum=cnum, v=ch["level_db"]: channels.set_mic_gain(cnum, int(v)))
            if ch.get("phantom") is not None:
                w(f"{cname} phantom", lambda cnum=cnum, v=ch["phantom"]: channels.set_phantom(cnum, bool(v)))
            if ch.get("polarity") is not None:
                w(f"{cname} polarity", lambda cnum=cnum, v=ch["polarity"]: channels.set_polarity(cnum, bool(v)))
            # INSTRUMENT GAIN is restored; INPUT TYPE deliberately is not (profile-coverage report).
            # inputType is DERIVED from the mic type just restored above (proven on hardware:
            # Line-in -> 2, Instrument -> 1, any mic voicing -> 0), so the path comes back on its
            # own, and writing it as well could only ever produce a state the desk's own UI cannot
            # make. The instrument gain is NOT derived the same way - selecting Instrument sets it to
            # 20 and leaving Instrument does not clear it - so that one does need writing.
            # inputColour is saved but not restored either: it is a string and there is no string
            # channel-property writer, so it is SAVE ONLY.
            if ch.get("instrument_gain") is not None:
                w(f"{cname} instrument gain", lambda cnum=cnum, v=ch["instrument_gain"]:
                  channels.set_instrument_gain(cnum, int(v)))
            fx = ch.get("effects") or {}
            for eff in ("echo", "reverb"):
                if fx.get(eff) is not None:
                    w(f"{cname} {eff}", lambda cnum=cnum, eff=eff, v=fx[eff]:
                      channels.set_channel_effect(cnum, eff, bool(v)))
            for pkey in ("echo_params", "reverb_params"):
                for p, v in (fx.get(pkey) or {}).items():
                    if v is not None:
                        w(f"{cname} {p}", lambda cnum=cnum, p=p, v=v:
                          channels.set_channel_effect_param(cnum, p, float(v)))
        else:
            # THE CONNECTED DESK's line channels, not the Pro II's. Testing against the Pro II map
            # silently never restores a Duo profile's Headset level.
            if ch.get("level_db") is not None and cname in channels.line_level_channels():
                w(f"{cname} level", lambda cname=cname, v=ch["level_db"]: channels.set_channel_level(cname, int(v)))

        # Processing applies to every channel (mic + line).
        # ORDER MATTERS (hardware-established, see channels.CHAIN_ADVANCED_PROP): the processing
        # MODE goes FIRST, because in simple mode the desk derives the chain from the three VoxLab
        # knobs, so restoring an advanced chain onto a strip left in simple mode can be regenerated
        # over the top. The preset-name marker goes LAST, because the device blanks it to -1 as soon
        # as any parameter is written. Older profiles have neither key and simply skip both.
        if ch.get("advanced") is not None:
            w(f"{cname} processing mode", lambda cname=cname, v=ch["advanced"]:
              channels.set_channel_advanced(cname, bool(v)))
        if ch.get("processing_on") is not None:
            w(f"{cname} processing", lambda cname=cname, v=ch["processing_on"]:
              channels.set_processing_enabled(cname, bool(v)))
        for mod_prop, on in (ch.get("modules") or {}).items():
            if on is not None:
                w(f"{cname} {mod_prop}", lambda cname=cname, mod_prop=mod_prop, v=on:
                  channels.set_processing_module(cname, mod_prop, bool(v)))
        for prop, v in (ch.get("params") or {}).items():
            if v is not None and prop in channels.PRESET_FLOAT_PROPS:
                w(f"{cname} {prop}", lambda cname=cname, prop=prop, v=v:
                  channels.set_channel_processing(cname, prop, float(v)))
        if ch.get("preset_index") is not None:
            w(f"{cname} preset name", lambda cname=cname, v=ch["preset_index"]:
              channels.set_channel_preset_marker(cname, int(v)))

    # Master/global output-block writes (multitrack, auto-mutes, headphone type, Compellor, delay)
    # are gated on the device's "output_write" cap: their selectors are decoded on the Pro II but
    # NOT yet verified on the Duo, and an unmapped global selector would append a stray. So on a
    # device without the cap we SKIP them (and tell the user) rather than write blindly. Fader
    # source writes above are channelInputSource and stay allowed (verified on both desks).
    _MASTER_OUT_KEYS = ("multitrack", "processing_bypass", "recording_format", "usb_multitrack",
                        "usb_processing_bypass", "monitor_auto_mute", "bt_auto_mute",
                        "headphone_type", "headphone_types", "compellor_on", "compellor_threshold",
                        "compellor_attack", "compellor_release", "compellor_gain", "delay_on")
    if "output_write" in devices.get_active_device().caps:
        step("Restoring master output settings.")
        if r.get("multitrack") is not None and int(r["multitrack"]) in rcp_transfer.MULTITRACK_MODES:
            w("multitrack", lambda v=r["multitrack"]: rcp_transfer.set_multitrack_mode(int(v)))
        if r.get("processing_bypass") is not None:
            w("raw multitrack recording", lambda v=r["processing_bypass"]:
              rcp_transfer.set_recording_processing_bypass(bool(v)))
        # MEMBERSHIP-CHECKED like multitrack above, and here it matters more: the desk stores
        # out-of-range values for this one, so a profile captured from a desk somebody had left on
        # a 3 would otherwise try to write that 3 straight back. A profile is not a licence to
        # restore a value the control itself would refuse.
        if (r.get("recording_format") is not None
                and int(r["recording_format"]) in rcp_transfer.RECORDING_FORMATS):
            w("recording format", lambda v=r["recording_format"]:
              rcp_transfer.set_recording_format(int(v)))
        # THE USB MODE GETS ITS OWN SPOKEN STEP, because it is the one write in this whole restore
        # that the person hears happen: it re-enumerates the desk's USB audio function, so every
        # RODECaster input and output on the machine drops for a second or two. Restored anyway
        # rather than skipped - a profile that saves a setting and quietly declines to put it back
        # is the mismatch this project has already been caught by once - but announced first.
        if (r.get("usb_multitrack") is not None
                and int(r["usb_multitrack"]) in rcp_transfer.MULTITRACK_MODES):
            step("Setting what the unit sends this computer. Its audio devices will drop out for "
                 "a couple of seconds.")
            w("USB multitrack", lambda v=r["usb_multitrack"]:
              rcp_transfer.set_usb_multitrack_mode(int(v)))
        if r.get("usb_processing_bypass") is not None:
            w("raw USB multitrack", lambda v=r["usb_processing_bypass"]:
              rcp_transfer.set_usb_multitrack_bypass(bool(v)))
        if r.get("monitor_auto_mute") is not None:
            w("monitor auto-mute", lambda v=r["monitor_auto_mute"]: rcp_transfer.set_monitor_auto_mute(bool(v)))
        if r.get("bt_auto_mute") is not None:
            w("Bluetooth auto-mute", lambda v=r["bt_auto_mute"]: rcp_transfer.set_bt_auto_mute(bool(v)))
        # Headphone amp type, ONE PER OUTPUT (GAP-6). New profiles carry "headphone_types" (a value
        # per addressable output, positional); older ones carry only "headphone_type", which is
        # Headphone 1's, so we treat it as a one-entry list. Values are validated against the
        # CONNECTED device's spec rather than a literal (0, 1) - that literal silently dropped
        # "Low sensitivity" (2), which the UI has offered since GAP-6 landed - and the selector comes
        # from the same spec, so an unmapped headphone output is never written.
        hp_spec = devices.get_active_device().headphones
        hp_saved = r.get("headphone_types")
        if hp_saved is None and r.get("headphone_type") is not None:
            hp_saved = [r["headphone_type"]]
        for out_i, hp_val in enumerate(hp_saved or []):
            if out_i >= len(hp_spec.outputs) or hp_val is None:
                continue
            if int(hp_val) not in hp_spec.value_codes:
                continue
            sel, out_label = hp_spec.outputs[out_i]
            w(f"{out_label} type", lambda sel=sel, v=hp_val:
              rcp_transfer.set_headphone_type(int(v), sel))
        if r.get("compellor_on") is not None:
            w("Compellor", lambda v=r["compellor_on"]: rcp_transfer.set_master_compellor(bool(v)))
        for pk, profkey in (("threshold", "compellor_threshold"), ("attack", "compellor_attack"),
                            ("release", "compellor_release"), ("gain", "compellor_gain")):
            v = r.get(profkey)
            if v is not None:
                w(f"Compellor {pk}", lambda pk=pk, v=v: rcp_transfer.set_master_compellor_param(pk, float(v)))
        if r.get("delay_on") is not None:
            secs = r.get("delay_seconds")
            w("output delay", lambda on=r["delay_on"], secs=secs:
              rcp_transfer.set_output_delay(bool(on), float(secs) if secs is not None else None))
    elif any(r.get(k) is not None for k in _MASTER_OUT_KEYS):
        failed += 1
        errs.append("master output settings (multitrack, auto-mute, headphone, Compellor, delay): "
                    "read-only on this device, not restored")

    # THE UNIT'S OWN SETTINGS, last: they touch nothing else, and none of them affects where another
    # write lands (unlike the layout, which has to go first). Order within the block does not matter
    # either - each is an independent field in its block.
    # Only values a profile actually recorded are written, and only names in the address table, so a
    # profile from a newer build carrying a setting THIS build does not know about is skipped rather
    # than written on a guessed selector. Gated on the device's "unit_settings" cap.
    unit = prof.get(UNIT_KEY) or {}
    if unit and "unit_settings" in devices.get_active_device().caps:
        step("Restoring the unit's own settings.")
        for prop, v in sorted(unit.items()):
            if prop not in rcp_transfer.UNIT_SETTING_SEL or not isinstance(v, (int, float)):
                continue
            if prop in UNIT_NEVER_RESTORE:
                continue        # saved for the record, deliberately not written back - see above
            w(prop, lambda prop=prop, v=v: rcp_transfer.set_unit_setting(prop, v))
    elif unit:
        failed += 1
        errs.append("unit settings (display, buttons, haptics, record button, listen mode): "
                    "read-only on this device, not restored")

    return {"applied": applied, "failed": failed, "errs": errs}


def _apply_legacy(prof, *, input_strip_vec_index, valid_source_codes, mic_channels) -> dict:
    """Apply an old settings-subset (flat) profile: MIDI, per-mic gain/phantom/polarity/processing,
    and the routing subset. Kept so profiles saved before the full format still restore."""
    applied = 0
    if prof.get("midi") is not None:
        rcp_transfer.set_midi_control(bool(prof["midi"])); applied += 1
    for cs, mc in (prof.get("mics") or {}).items():
        c = int(cs)
        if c not in mic_channels:
            continue
        if mc.get("gain_db") is not None:
            channels.set_mic_gain(c, int(mc["gain_db"])); applied += 1
        if mc.get("phantom") is not None:
            channels.set_phantom(c, bool(mc["phantom"])); applied += 1
        if mc.get("polarity") is not None:
            channels.set_polarity(c, bool(mc["polarity"])); applied += 1
        for prop, val in (mc.get("processing") or {}).items():
            if val is not None and prop in channels.PRESET_FLOAT_PROPS:
                channels.set_channel_processing(c, prop, float(val)); applied += 1
    r = prof.get("routing") or {}
    # Same output_write gate as _apply_v2: skip the master/global output writes on a device whose
    # selectors are not yet verified (the Duo), so a legacy restore cannot append a stray.
    if "output_write" in devices.get_active_device().caps:
        if r.get("multitrack") is not None:
            rcp_transfer.set_multitrack_mode(int(r["multitrack"])); applied += 1
        if r.get("monitor_auto_mute") is not None:
            rcp_transfer.set_monitor_auto_mute(bool(r["monitor_auto_mute"])); applied += 1
        if r.get("bt_auto_mute") is not None:
            rcp_transfer.set_bt_auto_mute(bool(r["bt_auto_mute"])); applied += 1
        # A legacy profile only ever carried Headphone 1's type. Validated against the connected
        # device's spec (same fix as _apply_v2), so "Low sensitivity" is no longer dropped.
        hp_spec = devices.get_active_device().headphones
        if r.get("headphone_type") is not None and int(r["headphone_type"]) in hp_spec.value_codes:
            rcp_transfer.set_headphone_type(int(r["headphone_type"]),
                                            hp_spec.outputs[0][0]); applied += 1
    for strip, code in (r.get("input_sources") or {}).items():
        if code is not None and strip in input_strip_vec_index and code in valid_source_codes:
            rcp_transfer.set_channel_input_source(strip, int(code)); applied += 1
    return {"applied": applied, "failed": 0, "errs": []}


def restore_result(safe, res) -> dict:
    """Format the user-facing message for a completed restore."""
    applied, failed, errs = res["applied"], res["failed"], res["errs"]
    msg = f"Restored profile '{safe}': applied {applied} settings"
    if failed:
        msg += f", {failed} could not be set ({'; '.join(errs)})"
    msg += ". Open Channels, Processing or Routing to read them back and confirm."
    return {"ok": True, "applied": applied, "failed": failed, "message": msg}
