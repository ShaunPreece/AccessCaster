"""
AccessCaster - the Channels page's app side: every processing-capable strip the desk exposes (on
the Pro II the four mics, BT, USB 1, Chat, USB 2 and the SMART pads bus), read and written.

One adaptive panel per channel, mirroring the RODE app: mic channels add gain, phantom, polarity,
mic type and effects; line channels have Level and processing. Nodes are resolved per channel from
the connected desk (`channels.proc_key`). The desk side is channels.py; the mute and solo are
mute_solo.py.
"""

from __future__ import annotations

import announcer
import channels
import desk_access
import diagnostics
import mic_presets
import network
import pan_link
import rcp_read
import wording
from desk_labels import (
    MODULE_TOGGLE_LABEL,
    PROCESSING_CONTROLS,
    PROCESSING_MODULES,
    PROCESSING_SPOKEN,
)

_PROCESSING_CONTROL_BY_PROP = {c["prop"]: c for g in PROCESSING_CONTROLS for c in g["controls"]}


def action_set_gain(channel: int, db: int) -> dict:
    if channel not in channels.MIC_CHANNELS:
        return {"ok": False, "message": f"Mic channel {channel} is not valid."}
    if db < channels.GAIN_MIN_DB or db > channels.GAIN_MAX_DB:
        return {"ok": False, "message": f"Gain must be between {channels.GAIN_MIN_DB} and "
                                        f"{channels.GAIN_MAX_DB} dB."}
    with desk_access.hid_lock:
        channels.set_mic_gain(channel, db)
    announcer.mark_self_gain(channel, True)     # the desk echoes this; the UI already reports it
    return {"ok": True, "message": f"Mic {channel} gain set to {db} dB."}


def action_set_phantom(channel: int, on: bool) -> dict:
    if channel not in channels.MIC_CHANNELS:
        return {"ok": False, "message": f"Mic channel {channel} is not valid."}
    with desk_access.hid_lock:
        channels.set_phantom(channel, on)
    return {"ok": True,
            "message": f"Mic {channel} 48V phantom power turned {'on' if on else 'off'}."}


def action_set_polarity(channel: int, on: bool) -> dict:
    if channel not in channels.MIC_CHANNELS:
        return {"ok": False, "message": f"Mic channel {channel} is not valid."}
    with desk_access.hid_lock:
        channels.set_polarity(channel, on)
    return {"ok": True,
            "message": f"Mic {channel} polarity {'inverted' if on else 'normal'}."}


def _mic_processing_snapshot(channel: int) -> dict:
    """{param/module name: value} for one mic's strip, off one dump, as the Channels page shows it.

    Deliberately the SAME reader the page uses (channels.read_channels), so a before/after diff here
    can never disagree with what the user is looking at."""
    try:
        recs = channels.read_channels()
    except Exception as exc:
        diagnostics.event("device.read_failed", "processing snapshot", error=str(exc))
        return {}
    entry = next((c for c in channels.active_ui_channels()
                  if c.get("kind") == "mic" and channels.mic_channel_number(c) == channel), None)
    rec = next((r for r in recs if entry and r.get("name") == entry.get("name")), None)
    if rec is None:
        return {}
    out = dict(rec.get("params") or {})
    out.update(rec.get("modules") or {})
    return out


def action_set_mic_type(channel: int, type_value: int) -> dict:
    """Set a mic's voicing, AND SAY WHAT IT DID TO THE GAIN AND THE PROCESSING CHAIN.

    CHOOSING A VOICING SETS THAT VOICING'S OWN DEFAULT GAIN - the desk does it, not us (measured:
    Dynamic 45, Condenser 25, PodMic 50, Broadcaster 27, Procaster 50, NT1 30, NT1-A 31, RE20 51,
    SM7B 55; Line-in and Instrument leave it alone).
    Without the read-back below, the gain control keeps showing the OLD number and nothing is
    announced, so a screen-reader user is told the voicing changed and never learns their preamp
    moved by up to 30 dB. Costs one dump on a deliberate, rare action.

    IT ALSO REPLACES THE WHOLE PROCESSING CHAIN on a Pro II - 13 params measured (Depth, Sparkle,
    Punch, compressor threshold + gain, de-esser, gate threshold + range, all three EQ gains, both
    Aphex) - and writing the old type back does NOT undo it. A Duo mic moves the gain only. None of
    it is undone here on purpose: the voicing's own chain is what the user asked for by naming their
    microphone, and it is what the desk and the RODE app both do. What must not happen is saying
    nothing, which leaves a screen-reader user with a panel full of stale numbers and no idea their
    compressor moved. So it is COUNTED and ANNOUNCED, exactly as the gain is, and the client re-reads
    the panel on success."""
    if channel not in channels.MIC_CHANNELS:
        return {"ok": False, "message": f"Mic channel {channel} is not valid."}
    # ALL THREE READS INSIDE THE LOCK, like every other read that has to line up with a write of
    # ours: the readers do not take the HID lock themselves, the caller does. The first is adopted
    # by the desk model, which is where the gain before the change comes from.
    with desk_access.hid_lock:
        proc_before = _mic_processing_snapshot(channel)
        was = channels.mic_gain_known(channel)
        channels.set_mic_type(channel, type_value)
        gain, _proc = channels.mic_gain_and_processing(channel)
        proc_after = _mic_processing_snapshot(channel)
    name = channels.MIC_TYPE.get(type_value, f"type {type_value}")
    msg = f"Mic {channel} input type set to {name}."
    if gain is not None and gain != was:
        msg += f" The desk moved the gain to {gain} dB for this microphone."
    moved = sorted(k for k, v in proc_after.items()
                   if k in proc_before and proc_before[k] != v)
    if moved:
        msg += (f" The desk also replaced this channel's processing with the {name} voicing: "
                f"{wording.count(len(moved), 'setting')} changed, so anything you had "
                f"set here by hand is gone.")
    return {"ok": True, "message": msg, "gain_db": gain}


def action_get_channels(percent: bool = True) -> dict:
    """Read every UI channel's full adaptive state (one model dump). Params are 0-100 percent ints,
    or with `percent` False the desk's own 0..1 floats, which `mic_presets.preset_now` can compare again
    after a pushed change (a whole percent is as coarse as its tolerance)."""
    try:
        with desk_access.hid_lock:
            # ONE DUMP FOR BOTH. The wireless-mic slots ride the same read as the channels so the
            # "Mute the wireless mic" switches can be made unavailable when the unit reports no
            # transmitter, without a second trip to the desk for a fact we already have in hand.
            blob = rcp_read.read_model_stable()
    except Exception as exc:
        return {"ok": False, "message": f"Could not read channels from the device: {exc}"}
    return channels_reply(blob, percent)


def channels_reply(blob: bytes, percent: bool) -> dict:
    """`action_get_channels`' reply from a dump already in hand; the window's full read
    (`window_state`) builds on it."""
    try:
        chans = channels.read_channels(blob)
    except Exception as exc:
        return {"ok": False, "message": f"Could not read channels from the device: {exc}"}
    # Which preset each mic strip is on. BEFORE the percent conversion below, because the comparison
    # is against the preset's own normalised 0..1 values (see mic_presets.preset_now).
    for c in chans:
        if c.get("kind") == "mic":
            ui = channels.ui_channel(c.get("name"))
            if ui is not None:
                c["preset_now"] = mic_presets.preset_now(channels.mic_channel_number(ui), c)
    if percent:
        for c in chans:
            c["params"] = {p: (round(v * 100) if v is not None else None)
                           for p, v in c["params"].items()}
    return {
        "ok": True, "channels": chans, "groups": PROCESSING_CONTROLS,
        "wireless_mics": network.wireless_mics(blob),
        "modules": PROCESSING_MODULES, "mic_types": sorted([list(kv) for kv in channels.MIC_TYPE.items()]),
        "presets": mic_presets.preset_list(),
        "pan_links": pan_link.pan_links(),
        "gain_min_db": channels.GAIN_MIN_DB, "gain_max_db": channels.GAIN_MAX_DB,
        "level_min_db": channels.LEVEL_MIN_DB, "level_max_db": channels.LEVEL_MAX_DB,
        "message": "Read all channels from the device.",
    }


def action_set_channel_level(name: str, db: int) -> dict:
    """Set a channel's Level: mics use mic gain (0-76 dB); line channels use inputDigitalGain."""
    ch = channels.ui_channel(name)
    if ch is None:
        return {"ok": False, "message": f"Unknown channel: {name}."}
    if ch["kind"] == "mic":
        return action_set_gain(channels.mic_channel_number(ch), db)
    if db < channels.LEVEL_MIN_DB or db > channels.LEVEL_MAX_DB:
        return {"ok": False, "message": f"Level must be between {channels.LEVEL_MIN_DB} and "
                                        f"{channels.LEVEL_MAX_DB} dB."}
    with desk_access.hid_lock:
        channels.set_channel_level(name, db)
    announcer.mark_self_gain(name, False)       # line strips push inputDigitalGain back at us
    return {"ok": True, "message": f"{ch['label']} level set to {db} dB."}


def action_set_processing_enabled(name: str, on: bool) -> dict:
    """Master per-channel processing on/off (the 'Processing' toggle)."""
    ch = channels.ui_channel(name)
    if ch is None:
        return {"ok": False, "message": f"Unknown channel: {name}."}
    with desk_access.hid_lock:
        channels.set_processing_enabled(name, on)
    return {"ok": True,
            "message": f"{ch['label']} processing turned {'on' if on else 'off'}."}


def action_set_channel_advanced(name: str, on: bool) -> dict:
    """Switch a channel between ADVANCED processing (the modules and their parameters) and SIMPLE
    (the three tone-shaping controls only). channelAdvancedProcessing, per fader strip.

    Worth knowing, and why the message says so: in SIMPLE mode the unit DERIVES most of the chain
    from Depth/Sparkle/Punch, so the module parameters below can be recomputed from those three. The
    RODE app clears them outright when you leave advanced mode; we do not touch them, so switching
    back and forth here is non-destructive."""
    ch = channels.ui_channel(name)
    if ch is None:
        return {"ok": False, "message": f"Unknown channel: {name}."}
    try:
        with desk_access.hid_lock:
            channels.set_channel_advanced(name, on)
    except Exception as exc:
        return {"ok": False, "message": f"Could not change the processing mode: {exc}"}
    return {"ok": True,
            "message": (f"{ch['label']} set to advanced processing; the modules and their "
                        f"parameters are in charge." if on else
                        f"{ch['label']} set to simple processing; the desk works the chain out "
                        f"from Depth, Sparkle and Punch.")}


def action_set_channel_module(name: str, module: str, on: bool) -> dict:
    """Enable/disable one processing module (hpf/eq/compressor/noiseGate/deesser/aphex/pan)."""
    ch = channels.ui_channel(name)
    if ch is None:
        return {"ok": False, "message": f"Unknown channel: {name}."}
    try:
        with desk_access.hid_lock:
            channels.set_processing_module(name, module, on)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True,
            "message": (f"{ch['label']} {MODULE_TOGGLE_LABEL.get(module, module)} turned "
                        f"{'on' if on else 'off'}.")}


def action_set_channel_effect(name: str, effect: str, on: bool) -> dict:
    """Toggle a mic channel's always-on effect (echo / reverb). Mic channels only (the app shows
    Echo/Reverb only on mics)."""
    ch = channels.ui_channel(name)
    if ch is None or ch["kind"] != "mic":
        return {"ok": False, "message": "Echo and reverb are available on mic channels only."}
    try:
        with desk_access.hid_lock:
            channels.set_channel_effect(channels.mic_channel_number(ch), effect, on)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": f"{ch['label']} {effect} turned {'on' if on else 'off'}."}


def action_set_channel_effect_param(name: str, param: str, value01: float) -> dict:
    """Set one channel-effect param (echo*/reverb*) on a mic channel. value01 is 0.0..1.0
    (decoded: float64 on the strip's EFFECTS_PARAMETERS node, byte-for-byte the RODE app's write)."""
    ch = channels.ui_channel(name)
    if ch is None or ch["kind"] != "mic":
        return {"ok": False, "message": "Echo and reverb are available on mic channels only."}
    try:
        v = max(0.0, min(1.0, float(value01)))
        with desk_access.hid_lock:
            channels.set_channel_effect_param(channels.mic_channel_number(ch), param, v)
    except (ValueError, TypeError) as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": f"{ch['label']} {param} set to {round(v * 100)} percent."}


def action_set_channel_param(name: str, prop: str, value01: float) -> dict:
    """Set one processing parameter (0..1 normalised) on any UI channel."""
    ch = channels.ui_channel(name)
    if ch is None:
        return {"ok": False, "message": f"Unknown channel: {name}."}
    ctrl = _PROCESSING_CONTROL_BY_PROP.get(prop)
    if ctrl is None:
        return {"ok": False, "message": f"Unknown processing control: {prop}."}
    try:
        v = max(0.0, min(1.0, float(value01)))
    except (TypeError, ValueError):
        return {"ok": False, "message": "Value must be a number from 0 to 1."}
    with desk_access.hid_lock:
        channels.set_channel_processing(name, prop, v)
    pct = round(v * 100)
    if ctrl.get("pan"):
        where = ("centre" if pct == 50 else "hard left" if pct == 0 else "hard right" if pct == 100
                 else f"{(50 - pct) * 2} percent left" if pct < 50 else f"{(pct - 50) * 2} percent right")
    else:
        where = f"{pct} percent"
    return {"ok": True,
            "message": f"{ch['label']} {PROCESSING_SPOKEN[prop].lower()} set to {where}."}
