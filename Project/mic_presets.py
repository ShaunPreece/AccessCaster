"""
AccessCaster - mic processing presets on the Channels page: the RODE app's factory set, which ships
as mic_presets.json, and the user's own, saved from a strip's live chain; applying one, and which one
a strip is on. Writing a preset to the strip is channels.apply_processing_preset.
"""

from __future__ import annotations

import json

import channels
import desk_access
import device_page
import rcp_read
import wording
from paths import data_path, read_json, resource_path, write_json

_FACTORY_PRESET_FILE = resource_path("mic_presets.json")   # ships with the app (read-only)
_USER_PRESET_FILE = data_path("user_presets.json")         # user-saved (writable, %LOCALAPPDATA%)


def _load_factory_presets() -> dict:
    try:
        with open(_FACTORY_PRESET_FILE, encoding="utf-8") as f:
            return (json.load(f) or {}).get("factory", {})
    except (OSError, ValueError):
        return {}


def _load_user_presets() -> dict:
    return read_json(_USER_PRESET_FILE, {})


def _save_user_presets(d: dict) -> None:
    write_json(_USER_PRESET_FILE, d)


# The property set a preset stores = the keys of a factory preset (floats + the index).
_PRESET_PROPS = ("eqLowGain", "eqMidGain", "eqHighGain", "aphexBBDrive", "aphexAEMix",
                 "compressorThreshold", "compressorGain", "deesserThreshold",
                 "noiseGateThreshold", "noiseGateRange", "noiseGateRelease",
                 "channelDepth", "channelSparkle", "channelPunch",
                 # The processing MODE belongs to a voicing (see channels.CHAIN_ADVANCED_PROP).
                 # Without it, a saved advanced voicing applied to a strip in simple mode gets
                 # regenerated from the VoxLab knobs. Written FIRST by apply_processing_preset;
                 # the marker below is written LAST.
                 channels.CHAIN_ADVANCED_PROP,
                 # The MODULE on/off flags. A voicing is not just parameter values, it is which parts
                 # of the chain are engaged: without these, applying a preset to a strip with its
                 # modules off sets every value, changes nothing audible, and reports success.
                 # channelBypassProcessing is deliberately NOT here: the master on/off is the
                 # operator's control, not part of the voicing (channels.apply_processing_preset).
                 *channels.PROCESSING_MODULE_BOOLS,
                 "channelCurrentFxPreset")


def preset_list() -> list:
    """[{name, custom}] for the UI combo box, factory first then user presets."""
    out = [{"name": n, "custom": False} for n in _load_factory_presets()]
    out += [{"name": n, "custom": True} for n in _load_user_presets()]
    return out


# --- WHICH PRESET A STRIP IS ON ----------------------------------------------------------------
# The device cannot really answer this, so without the record below the Presets tab cannot either:
# apply Podcast Studio, leave the channel, come back, and nothing says what it is on.
#
# `channelCurrentFxPreset` is a NAME-only marker with three factory slots (0/1/2, -1 = none) that
# the unit blanks the moment any parameter is written. So it cannot name a custom preset at all, and
# it stops naming a factory one at the first tweak. Reading the device harder does not fix that.
#
# Instead we keep our OWN record of what we last applied and then CHECK it against the live values
# rather than trusting it. A record saying "Podcast Studio" on a chain that no longer matches is
# reported as edited, not as the preset. That covers custom presets, stays honest through tweaks,
# and self-corrects when the strip is changed from the RODE app or the desk, because the values
# simply stop matching. The device marker is still preferred when it HAS an answer (see preset_now).
#
# The record is per-machine and per-device: it carries the hardware id it was written against, and a
# record from another desk is ignored rather than claimed. On a fresh install there is no record, so
# the marker is all we have until something is applied - which the UI says plainly.
_APPLIED_PRESETS_KEY = "applied_presets"
_PRESET_MATCH_TOL = 0.005     # normalised 0..1 floats: a read-back is never bit-identical
# The factory marker slots, in channelCurrentFxPreset order, confirmed against the RODE app's own
# list.
_FACTORY_MARKER_NAMES = ("Neutral", "Podcast Studio", "Broadcast")


def _applied_preset_record() -> dict:
    """{mic channel as str: preset name} for THIS desk. Empty if the record belongs to another one -
    two desks would otherwise inherit each other's claims."""
    return device_page.desk_record(_APPLIED_PRESETS_KEY)


def _note_applied_preset(channel: int, name: str) -> None:
    """Remember that we just applied `name` to a mic channel. Best-effort, like every other setting:
    losing it costs a name in the UI, never a device state."""
    device_page.save_desk_record(_APPLIED_PRESETS_KEY,
                                 {**_applied_preset_record(), str(int(channel)): name})


def _preset_matches(props: dict, ch: dict) -> bool:
    """Does the live chain still hold every value this preset writes?

    Read straight off the channel dump the UI already asked for, so this costs no device access.
    Floats compare within _PRESET_MATCH_TOL; the module flags and the processing mode compare
    exactly, because those are what make a voicing audible. The preset-name marker is skipped: it is
    a name, not a value, and the unit blanks it as soon as the first parameter lands."""
    params, modules = (ch.get("params") or {}), (ch.get("modules") or {})
    for prop, want in props.items():
        if prop == channels.CHAIN_PRESET_MARKER_PROP:
            continue
        if prop == channels.CHAIN_ADVANCED_PROP:
            got = ch.get("advanced")
        elif prop in channels.PROCESSING_MODULE_BOOLS:
            got = modules.get(prop)
        else:
            got = params.get(prop)
            if got is None:
                return False
            try:
                if abs(float(got) - float(want)) > _PRESET_MATCH_TOL:
                    return False
            except (TypeError, ValueError):
                return False
            continue
        if got is None or bool(got) != bool(want):
            return False
    return True


def preset_now(channel: int, ch: dict):
    """What this mic strip is on: {name, edited, source}, or None when nothing can be claimed.

    Order of trust, most reliable first:
      1. The DEVICE marker, when it has an answer. A non-negative marker means a factory preset was
         applied and nothing has been written since, which beats any record of ours - it is also the
         only thing that knows about a preset applied from the RODE app.
      2. OUR record, checked against the live values, which is the only way a custom preset or an
         edited factory one can be named at all.
      3. Nothing. The strip is on settings we have no name for, and the UI says exactly that rather
         than inventing "custom"."""
    idx = ch.get("preset_index")
    if isinstance(idx, int) and 0 <= idx < len(_FACTORY_MARKER_NAMES):
        return {"name": _FACTORY_MARKER_NAMES[idx], "edited": False, "source": "device"}
    name = _applied_preset_record().get(str(int(channel)))
    if not name:
        return None
    props = {**_load_factory_presets(), **_load_user_presets()}.get(name)
    if not props:
        # Applied, then deleted from the preset list: we can still name it, but there is nothing left
        # to compare against, so we do not claim either way.
        return {"name": name, "edited": None, "source": "record"}
    return {"name": name, "edited": not _preset_matches(props, ch), "source": "record"}


def _read_channel_preset_props(channel: int) -> dict:
    """Read the current preset-property values for one mic channel (for 'save as').

    NEVER INDEX THE PROCESSING ARRAYS WITH `channel - 1`: that assumes a mic's chain sits at its own
    slot, and processing is per fader STRIP. On a reordered layout - a line
    channel on fader 2 pushing Mic 2 to strip 6 - "save Mic 2's settings as a preset" then silently
    saves whatever chain sits on strip 1. The strip is resolved the same way every other processing
    read and write does it (channels._channel_strip, off the live fader vector). The processing MODE
    is saved too, so applying a saved advanced voicing restores advanced mode rather than landing on
    a strip that re-derives it from the VoxLab knobs."""
    blob = rcp_read.read_model_stable()
    idx = channels._channel_strip(channel)
    out = {}
    for p in _PRESET_PROPS:
        vals = rcp_read.values(blob, p)
        if idx < len(vals):
            out[p] = vals[idx]
    return out


# Plain names for the module flags, for the "switched on" sentence. Spelled as the Channels page
# spells them so the two are not read aloud as if they were different controls.
_MODULE_LABEL = {"hpfOn": "high-pass filter", "eqOn": "equaliser", "compressorOn": "compressor",
                 "noiseGateOn": "noise gate", "deesserOn": "de-esser", "aphexOn": "tone shaping",
                 "channelPanOn": "pan", "eqHighOn": "EQ high band", "eqMidOn": "EQ mid band",
                 "eqLowOn": "EQ low band"}


def _read_channel_modules(channel: int) -> dict:
    """The module on/off flags for one mic channel's STRIP, plus its master processing state.

    Same strip resolution as every other processing read (channels._channel_strip off the live
    fader vector), so it is correct on a reordered layout. CALLER HOLDS THE HID LOCK."""
    blob = rcp_read.read_model(fresh=True)
    idx = channels._channel_strip(channel)
    out = {}
    for p in channels.PROCESSING_MODULE_BOOLS:
        vals = rcp_read.values(blob, p)
        out[p] = vals[idx] if idx < len(vals) else None
    bypass = rcp_read.values(blob, "channelBypassProcessing")
    out["_processing_on"] = (None if idx >= len(bypass) else not bypass[idx])
    return out


def action_apply_mic_preset(channel: int, name: str) -> dict:
    if channel not in channels.MIC_CHANNELS:
        return {"ok": False, "message": f"Mic channel {channel} is not valid."}
    presets = {**_load_factory_presets(), **_load_user_presets()}
    props = presets.get(name)
    if not props:
        return {"ok": False, "message": f"Preset {name} was not found."}
    # Read the modules FIRST so we can say what actually changed: nothing on screen otherwise shows
    # a module moving.
    try:
        with desk_access.hid_lock:
            before = _read_channel_modules(channel)
            channels.apply_processing_preset(channel, props)
            after = _read_channel_modules(channel)
    except Exception as exc:
        return {"ok": False, "message": f"Could not apply the preset: {exc}"}
    # Remember it, so coming back to this channel can still say what it is on. The device marker
    # cannot do that job for a custom preset, or for a factory one once a slider moves (preset_now).
    _note_applied_preset(channel, name)
    msg = f"Mic {channel} set to the {name} preset."
    turned_on = [_MODULE_LABEL.get(p, p) for p in channels.PROCESSING_MODULE_BOOLS
                 if after.get(p) and not before.get(p)]
    turned_off = [_MODULE_LABEL.get(p, p) for p in channels.PROCESSING_MODULE_BOOLS
                  if before.get(p) and not after.get(p)]
    if turned_on:
        msg += f" Switched on: {wording.join_and(turned_on)}."
    if turned_off:
        msg += f" Switched off: {wording.join_and(turned_off)}."
    # The master on/off is never part of a preset, so say so when it would mask the whole thing.
    if after.get("_processing_on") is False:
        msg += (" Note: processing is still off for this channel, so the preset is loaded but not "
                "heard. Turn processing on to use it.")
    return {"ok": True, "message": msg}


def action_save_mic_preset(channel: int, name: str) -> dict:
    name = (name or "").strip()
    if channel not in channels.MIC_CHANNELS:
        return {"ok": False, "message": f"Mic channel {channel} is not valid."}
    if not name:
        return {"ok": False, "message": "Please enter a name for the preset."}
    if name in _load_factory_presets():
        return {"ok": False, "message": f"{name} is a built-in preset name; choose another."}
    try:
        with desk_access.hid_lock:
            props = _read_channel_preset_props(channel)
    except Exception as exc:
        return {"ok": False, "message": f"Could not read the current settings: {exc}"}
    if not props:
        return {"ok": False, "message": "Could not read the current settings to save."}
    user = _load_user_presets()
    user[name] = props
    _save_user_presets(user)
    # The channel now IS this preset by definition - it was saved from these very values - so the
    # strip adopts the new name straight away rather than still claiming whatever it was based on.
    _note_applied_preset(channel, name)
    return {"ok": True, "message": f"Saved Mic {channel}'s current settings as {name}.",
            "presets": preset_list()}


def action_delete_mic_preset(name: str) -> dict:
    user = _load_user_presets()
    if name not in user:
        return {"ok": False, "message": f"{name} is not a custom preset (built-ins can't be deleted)."}
    del user[name]
    _save_user_presets(user)
    return {"ok": True, "message": f"Deleted the {name} preset.", "presets": preset_list()}
