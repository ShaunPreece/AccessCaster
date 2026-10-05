"""Single source of truth for EVERY channel and channel-processing operation (read + write):
the four mics plus the Bluetooth / USB 1 / Chat / USB 2 line channels.

WHY THIS FILE EXISTS: ALL channel logic lives HERE, the way soundpad.py owns all pad logic, so the
web layer and any CLI call into one module and there is one authoritative place for the channel
model. Spread across three files - setters and selector maps in rcp_transfer.py, read mapping in
rcp_read.py, action wrappers in server.py - a duplicate `set_channel_processing` def can slip in and
silently shadow the parameter setter, which breaks every mic slider.

LAYERING (mirrors soundpad.py over pad_records.py):
  rcp_transfer.py  - low-level report-3 byte builders (build_*), the send layer, and the NON-per-channel
                     mixer/system/routing/pad functions. Channels imports its builders + send from here.
  rcp_read.py      - low-level model reader (values, first, read_model, read_model_stable) + the routing
                     matrix + read_settings (the raw mic-input vectors). Channels reads through these.
  channels.py      - THIS FILE: the channel model (selector maps, channel order, read mapping) and every
                     per-channel operation (gain/phantom/polarity/type/level/processing/effects + reads).

All selectors here were decoded by USB capture and device-proven (change-capture-restore); the load-bearing
facts are kept as comments at each definition exactly as they were when they lived in rcp_transfer/rcp_read.
"""

import json as _json        # the off-fader chain store only
import time as _time        # strip-vector cache expiry only
import paths as _paths      # writable data dir for the off-fader chain store
import rcp_transfer as _t   # low-level byte builders + send layer (build_*, send_record, send_records_paced)
import rcp_read as _r        # low-level model reader (values, first, read_model, read_model_stable)
import devices               # per-device model (mic selectors, channel order) keyed by product id


# ============================================================================
# MIC INPUT CHANNELS (gain / phantom / polarity / type) - the XLR preamp block
# ----------------------------------------------------------------------------
# Mixer CHANNEL properties use the address 01 01 01 02 <chan> 02 + name + value. Channel selectors are
# STATIC and sequential (channels are fixed, unlike pads): Mic 1 = 0x6e ... Mic 4 = 0x71. (Mic 1/2 verified
# vs gain_up.pcap / mic2_gain.pcap; 3/4 inferred from the sequence.)
# ============================================================================
# WHERE AN INPUT WRITE GOES IS NOT A PER-DEVICE TABLE ANY MORE. It is INPUTSOURCE[source_code] on
# every desk, derived by DeviceModel.input_path from the one number a channel already has. The two
# DO NOT reintroduce MIC_CHANNEL_SEL / INPUT_DIGITAL_GAIN_SEL: they are a second encoding of that
# same rule, and a per-device copy of them can encode a DIFFERENT and inert address, which is how a
# mic gain comes to report success while changing no audio.


def _mic_path(channel: int) -> list:
    """Tree path of a 1-based mic channel's input values on the CONNECTED device.

    Raises a clear error if this desk's input-mic writes are not enabled, so the app disables the
    control rather than writing to an address nothing has confirmed.
    """
    dev = devices.get_active_device()
    if "input_mic_write" not in dev.caps:
        raise RuntimeError(
            f"Input-mic controls (gain/phantom/type/polarity) are not supported on the "
            f"{dev.name} yet - its write format has not been decoded.")
    ch = next((c for c in (dev.ui_channels or ())
               if c.get("kind") == "mic" and int(c["source_code"]) + 1 == channel), None)
    if ch is None:
        raise ValueError(f"mic channel {channel} out of range for the {dev.name}")
    return dev.input_path(ch)


# Mic input type / voicing. Decoded: property inputMicrophoneType (int), SAME channel selector
# as gain (0x6e..0x71). FULL value->name map captured with sighted help (a helper stepped through
# the app's mic locker on Mic 1 while we read each value back; the carousel resists automation so this needed
# a human to drive it). All 11 selectable types confirmed 0..10. NOT mapped: "Wireless" (sits between Condenser
# and PodMic in the list but only selectable with a RODE wireless device paired, so its value is unknown; the
# device sets it automatically when a wireless mic connects).
MIC_TYPE = {0: "Line-in", 1: "Instrument", 2: "Dynamic", 3: "Condenser", 4: "PodMic",
            5: "Broadcaster", 6: "Procaster", 7: "NT1", 8: "NT1-A", 9: "RE20", 10: "SM7B"}

# Per-channel properties registry (golden rule: understand + have available, even the low-value ones).
# All use the channel selector 0x6e+(ch-1). High-value props have named setters below; the rest are
# NOT reachable from code: a generic writer for them existed and was never called, so it was deleted
#. This table stays as the record of what the block carries; give a property its own
# named setter if it ever needs writing.
CHANNEL_PROP = {
    "inputMicrophoneGain": {"kind": "int",  "note": "XLR preamp gain in dB (set_mic_gain)."},
    "inputPower":          {"kind": "int",  "note": "48V phantom, 1=on/0=off (set_phantom)."},
    "inputPhaseFlip":      {"kind": "bool", "note": "polarity/phase invert (set_polarity)."},
    "inputMicrophoneType": {"kind": "int",  "note": "mic model/voicing (set_mic_type; MIC_TYPE full 0..10)."},
    # channelWirelessMute WAS LISTED HERE AND DOES NOT BELONG. It is in the CHANNEL block, per STRIP,
    # not in this input table - see WIRELESS_MUTE_PROP, which has the measurement and the setter.
    "inputDigitalGain":    {"kind": "int",  "note": "digital/line input gain (non-XLR sources)."},
    # A real stored setting, NOT purely derived: selecting Instrument on a channel sets it to 20, and
    # switching AWAY from Instrument leaves it at 20 (proven on Mic 3). That asymmetry is
    # why it has to be restored explicitly, and why Mic 1 and Mic 2 read 20 here - both have been
    # through Instrument at some point.
    "inputInstrumentGain": {"kind": "int",  "note": "instrument input gain. Set to 20 when the mic "
                                                   "type becomes Instrument; NOT cleared on the way "
                                                   "back out, so restore it explicitly."},
    # A STRING of AARRGGBB hex, read off the live desk: 'ffd43580' on Mic 1, 'ff59dd20' on Mic 2,
    # 'ff000000' on the unused slots. Registering it as an int would write the wrong record shape.
    # There is no string channel-property WRITER (build_channel_prop_int / _bool are all there is),
    # so it is readable and backed up but not restorable.
    "inputColour":         {"kind": "str",  "note": "channel strip colour, AARRGGBB hex string "
                                                   "(decoded). No string writer yet."},
    # inputType IS DERIVED FROM inputMicrophoneType. Proven on Mic 3 (nothing plugged in)
    # by stepping the mic type and reading it back each time:
    #     Line-in (0)    -> inputType 2
    #     Instrument (1) -> inputType 1
    #     Dynamic (2), SM7B (10), PodMic (4), ... any real mic voicing -> inputType 0
    # So inputType is the physical INPUT PATH the desk has put that combo input on: 0 mic, 1
    # instrument, 2 line. It is NOT the 0/-1 validity flag section 4 of the protocol took it for
    # (-1 does mark a slot with no physical input), and it is not something to write on its own:
    # doing that produces a combination the desk's own UI cannot make, e.g. an SM7B voicing sitting
    # on the line path. Set the MIC TYPE and the path follows.
    "inputType":           {"kind": "int",  "note": "DERIVED input path: 0 mic, 1 instrument, 2 "
                                                   "line, following inputMicrophoneType. -1 = a "
                                                   "slot with no physical input. Set the mic type."},
}


def set_mic_gain(channel: int, db: int) -> int:
    """Set a mic channel's input gain in dB (app: Audio Setup > channel gear > Level). channel 1-4
    (Pro II) / 1-2 (Duo). Decoded property 'inputMicrophoneGain'. Sent in normal mode."""
    return _t.send_record(_t.build_path_prop_int("inputMicrophoneGain", db, _mic_path(channel)))


def mic_gain(blob: bytes, channel: int):
    """A mic channel's CURRENT input gain in dB, or None, read at the SAME address set_mic_gain
    writes to.

    THE PAIR IS THE POINT. Anything that has to put a gain back must take it from the desk rather
    than from the app's cached view, and reading it at the write's own address is the only way to
    be certain it is the same value. `server.wake_channels` is the case that forces it: its callers
    are a reset and an import, the two moments `_session_state` is guaranteed stale, so sourcing
    the "put it straight back" value from the cache writes the app's last displayed gain over every
    freshly-restored one.
    """
    v = _r.path_value(blob, _mic_path(channel), "inputMicrophoneGain", None)
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def set_phantom(channel: int, on: bool) -> int:
    """Enable/disable 48V phantom power (P48) on a mic channel. Property 'inputPower'
    (int 1=on, 0=off). Sent in normal mode."""
    return _t.send_record(_t.build_path_prop_int("inputPower", 1 if on else 0, _mic_path(channel)))


def set_polarity(channel: int, invert: bool) -> int:
    """Invert (flip) a mic channel's polarity/phase. Property 'inputPhaseFlip' (compact bool).
    Uses the INPUT selector like gain/P48 - WRITE-VERIFIED via read-back. Sent in normal mode."""
    return _t.send_record(_t.build_path_prop_bool("inputPhaseFlip", invert, _mic_path(channel)))


def set_mic_type(channel: int, type_value: int) -> int:
    """Set a mic channel's input type/voicing. See MIC_TYPE for confirmed names.

    THIS ALSO MOVES THE GAIN. The desk gives each voicing its own default preamp gain and applies it
    on selection, so a caller that has just set a gain has to write this FIRST or lose it (see
    profiles._apply_v2, and server.action_set_mic_type which reads the gain back and says so).
    Measured on Mic 3:

        Dynamic 45, Condenser 25, PodMic 50, Broadcaster 27, Procaster 50,
        NT1 30, NT1-A 31, RE20 51, SM7B 55

    Line-in and Instrument are the two that leave the gain alone - they are not microphone voicings.
    Instrument instead sets inputInstrumentGain to 20 and leaves it there when you move off it."""
    return _t.send_record(_t.build_path_prop_int("inputMicrophoneType", type_value, _mic_path(channel)))


def set_instrument_gain(channel: int, value: int) -> int:
    """Instrument-input gain on a mic/combo channel (property 'inputInstrumentGain')."""
    return _t.send_record(_t.build_path_prop_int("inputInstrumentGain", int(value), _mic_path(channel)))


# ============================================================================
# PER-CHANNEL PROCESSING (EQ / comp / gate / de-esser / HPF / pan / VoxLab / Aphex)
# ----------------------------------------------------------------------------
# The RODE app drives the channel-strip DSP over report-3 as GLOBAL records: the property NAME selects
# the parameter and the SELECTOR selects the channel. Float params use value-type 0x04 (normalised 0..1).
#   01 01 01 01 <sel>  <propName> 00  01 09 04  <float64 LE>
# ============================================================================
SEL_CHANNEL_PROC = 0x1C     # the selected channel's processing block (selector base)

# Full channel-strip order (matches rcp_read.CHANNELS). The processing block of EVERY channel is addressed
# at selector SEL_CHANNEL_PROC + index, in this order. Verified by capture: Bluetooth=0x20,
# USB1=0x22, Chat=0x23, USB2=0x24 (one Processing-toggle each); mics 0x1c-0x1f and CallMe1=0x25 sit on the
# same contiguous line.
# SMARTpads (0x21) IS STILL IN THIS LIST, AND THAT IS ABOUT ADDRESSING, NOT ABOUT FEATURES. The
# strip exists and its processing block is at 0x21, so the selector line is contiguous and this table
# stays complete. What the bus does NOT get is a processing chain in the app: the writes all land and
# read back and none of them reaches the audio, heard on both desks, exactly as
# RODE's own guide and their app always said. This list was briefly cited as evidence the bus was
# processed; it is not, it is a map of where a block sits. See devices.PRO2_UI_CHANNELS.
PROC_CHANNEL_ORDER = ["Mic1", "Mic2", "Mic3", "Mic4", "Bluetooth", "SMARTpads",
                      "USB1", "Chat", "USB2", "CallMe1"]


def _static_proc_index(channel) -> int:
    """Legacy FIXED-layout channel -> processing index (Pro II). `channel` is a 1-based index into
    PROC_CHANNEL_ORDER (1=Mic1 ... 5=Bluetooth ... 10=CallMe1) or a name in it.
    Used for channels a device does not list in ui_channels."""
    if isinstance(channel, str):
        if channel not in PROC_CHANNEL_ORDER:
            raise ValueError(f"unknown channel {channel!r}; known: {PROC_CHANNEL_ORDER}")
        return PROC_CHANNEL_ORDER.index(channel)
    idx = channel - 1
    if not 0 <= idx < len(PROC_CHANNEL_ORDER):
        raise ValueError(f"channel index out of range (1-{len(PROC_CHANNEL_ORDER)}): {channel}")
    return idx


def _ui_entry_for(dev, channel):
    """Map `channel` (a device ui-channel NAME, or a 1-based PROC_CHANNEL_ORDER index / mic number)
    to the active device's ui_channels entry, or None if the device does not expose it there."""
    cmap = {c["name"]: c for c in dev.ui_channels_for(usb_expanded())}
    if isinstance(channel, str):
        return cmap.get(channel)
    if isinstance(channel, int) and 1 <= channel <= len(PROC_CHANNEL_ORDER):
        return cmap.get(PROC_CHANNEL_ORDER[channel - 1])
    return None


# ---- fader vector cache (the strip map) -------------------------------------------------------
# Processing and effects are addressed PER STRIP (fader position), so every such read or write has
# to know the live channelInputSource vector. Reading it means a full ~600 ms model dump, and
# rcp_read's own cache is dropped by EVERY write, so resolving it per write would make dragging one
# EQ slider fire a dump between each step. This cache is keyed on the fader layout alone: it is
# refreshed for free whenever the app reads all channels (publish_strip_vector below, called from
# read_channels), dropped explicitly whenever WE change the layout, and otherwise expires on a
# timer. The timer is the only exposure: a fader source changed on the desk itself, not through us,
# could take up to STRIP_VECTOR_TTL_S to be noticed.
STRIP_VECTOR_TTL_S = 15.0
_strip_vec = {"vec": None, "at": 0.0}


# LAST KNOWN layout, with NO expiry. Separate from the TTL'd cache above on purpose: the TTL exists
# because addressing a processing WRITE to a stale strip is dangerous, whereas the announcer only
# needs a NAME for a strip, where "the last layout we saw" beats saying "Channel 4". Kept current by
# publish_strip_vector below and by note_strip_source when the desk reports a change.
_strip_vec_last = {"vec": None}


# WHETHER THE DESK IS IN EXPANDED, no expiry, same contract as _strip_vec_last: it decides a NAME
# and which sources may be offered, never an address. Held here rather than read on demand because
# the callers that need it - the announcer's read loop, the fader label helper - must not trigger a
# model dump, and because Expanded changes only by a write that takes the desk off the USB bus, so
# a cached answer cannot go quietly stale the way a fader layout can.
_usb_expanded = {"on": None}


def publish_usb_expanded(on) -> None:
    """Cache the Expanded flag from a read that has just decoded `outputMultiMode`."""
    if on is not None:
        _usb_expanded["on"] = bool(on)


def usb_expanded() -> bool:
    """Whether the desk is in Expanded, as last seen.

    False until something has published, which is a safe default only in the sense that it offers
    FEWER sources and the plainer name. It is NOT a harmless one: unpublished, the fader dropdown
    omits Game, Music, A and B AND the write gate refuses them, so on a desk that is in Expanded
    the four cannot be assigned at all. Seed it at startup (`server._bring_up_server`) rather than
    leaving the first publisher to whichever page the user happens to open."""
    return bool(_usb_expanded["on"])


def usb_expanded_known() -> bool:
    """Has anything published the mode yet? Lets the startup seed skip a dump it does not need."""
    return _usb_expanded["on"] is not None


def publish_strip_vector(vec) -> None:
    """Cache a channelInputSource vector we have just read as part of some other read."""
    if vec:
        _strip_vec["vec"] = list(vec)
        _strip_vec["at"] = _time.time()
        _strip_vec_last["vec"] = list(vec)


def last_known_strip_vector():
    """The most recent fader layout we have seen, however old, or None if we have never seen one.
    NAMING ONLY - never use it to address a write (see _strip_vec_last)."""
    v = _strip_vec_last["vec"]
    return list(v) if v is not None else None


def note_strip_source(idx: int, code: int) -> bool:
    """Record a single channelInputSource change seen on the PUSHED report-04 stream.

    Keeps the naming layout current when a fader source is changed somewhere we did not do it (the
    desk's own touchscreen, the RODE app, MIDI), and invalidates the TTL'd cache so the next
    processing write re-reads the real layout instead of trusting a copy we now know is out of date
    (before this, such a change went unnoticed for up to STRIP_VECTOR_TTL_S). Returns True if this
    actually changed our picture."""
    v = _strip_vec_last["vec"]
    changed = True
    if v is not None and 0 <= idx < len(v):
        changed = (v[idx] != code)
        v[idx] = code
    if changed:
        invalidate_strip_vector()
    return changed


def invalidate_strip_vector() -> None:
    """Drop the cached fader layout. Call this after ANY write that moves a fader source."""
    _strip_vec["vec"] = None
    _strip_vec["at"] = 0.0


def strip_vector() -> list:
    """The live channelInputSource vector (position = strip/fader, value = source code)."""
    vec = _strip_vec["vec"]
    if vec is not None and (_time.time() - _strip_vec["at"]) <= STRIP_VECTOR_TTL_S:
        return vec
    vec = [v for v in _r.values(_r.read_model(), "channelInputSource")]
    publish_strip_vector(vec)
    return vec


def cached_strip_vector():
    """The cached fader layout, or None if there is no FRESH one. NEVER touches the device.

    For callers that must not trigger a ~0.5 s model dump and hold no HID lock - the control
    announcer runs in its own thread off a read-only handle, so it uses this to name a channel and
    falls back to 'Channel N' when the cache is cold, rather than dumping from the listener thread."""
    vec = _strip_vec["vec"]
    if vec is not None and (_time.time() - _strip_vec["at"]) <= STRIP_VECTOR_TTL_S:
        return vec
    return None


def _strip_index(entry) -> int:
    """The device strip index for a ui-channel entry, resolved EXACTLY like the read path: a fixed
    proc_idx if one is pinned, else the LIVE fader position channelInputSource.index(source_code).
    BOTH desks now take the dynamic path: processing/effects are per fader STRIP on the Pro II too
    (hardware-confirmed, see devices.PRO2_UI_CHANNELS), so a channel moved to another
    fader is processed by the chain sitting on that fader."""
    pi = entry.get("proc_idx")
    if pi is not None:
        return pi
    src = entry.get("source_code")
    try:
        return strip_vector().index(src)
    except (ValueError, TypeError):
        raise RuntimeError(
            f"{entry.get('name')!r} is not assigned to a fader strip right now, so its "
            f"processing/effects cannot be addressed - assign it to a fader first.")


def _channel_strip(channel) -> int:
    """Resolve any channel identifier to its device strip index (device-aware). Falls back to the
    legacy fixed mapping for channels a device does not list in ui_channels."""
    dev = devices.get_active_device()
    entry = _ui_entry_for(dev, channel)
    if entry is not None:
        return _strip_index(entry)
    return _static_proc_index(channel)


def proc_selector(channel) -> int:
    """Map a channel to its processing selector (SEL_CHANNEL_PROC + strip index), DEVICE-AWARE.

    `channel` may be a device ui-channel name ("Mic1", "Bluetooth", "Headset", ...), a 1-based
    PROC_CHANNEL_ORDER index, or a 1-based mic number. On a FIXED-layout desk (Pro II) the strip is
    the pinned proc_idx, so this is byte-identical to the old static mapping (incl. CallMe1). On a
    DYNAMIC-layout desk (Duo) the strip is the channel's LIVE fader position, matching the read path,
    so BT/USB/Chat/Headset address the right strip instead of the wrong (Pro II-ordered) slot.
    """
    return SEL_CHANNEL_PROC + _channel_strip(channel)


def fx_selector(channel) -> int:
    """Effects selector (SEL_CHANNEL_FX + strip index), using the SAME strip resolution as
    proc_selector so effects and processing always agree. Confirmed on the Duo: Mic2 -> 0x27."""
    return SEL_CHANNEL_FX + _channel_strip(channel)


# Whitelist of processing parameters we have decoded (all float64, normalised 0..1). Only names the RODE
# app itself sends are allowed, per the capture-replay safety rule.
PROCESSING_PROPS = frozenset({
    "eqHighGain", "eqHighBell", "eqMidGain", "eqMidBell", "eqLowGain", "eqLowShelf",
    # THE THREE Q VALUES, and two plausible claims about them are BOTH wrong: "Q follows the other
    # EQ parameters device-side, so no writer is needed", and "Q cannot be written at all" - the
    # second of which leaves a 2e-8 float32 residue from the RODE app impossible to clean up.
    # Measured on Mic 3 (strip 2, selector 0x1E):
    #   - writing eqLowGain does NOT move any Q. Writing eqLowShelf / eqMidBell / eqHighBell does not
    #     move any Q either. So Q is NOT derived: PARITY was wrong.
    #   - writing eqLowQ / eqMidQ / eqHighQ directly LANDS and reads back exactly (0.7 / 0.6 / 0.5
    #     asked, 0.7 / 0.6 / 0.5 read), restores byte-for-byte, model length unchanged and occurrence
    #     counts unchanged, so no stray. That sweep was wrong about "cannot write" and right about
    #     "belongs in the chain set".
    # Note this is the one entry in this set that is NOT justified by a capture of the RODE app: the
    # app has no Q control, so there was never a capture route. It is justified by the hardware
    # read-back above, which is the stronger evidence of the two.
    "eqLowQ", "eqMidQ", "eqHighQ",
    "compressorThreshold", "compressorRatio", "compressorAttack", "compressorRelease",
    "compressorGain",
    "noiseGateThreshold", "noiseGateAttack", "noiseGateHold", "noiseGateRelease",
    "noiseGateRange", "noiseGateHysteresis",
    "deesserThreshold", "deesserRatio", "deesserAttack", "deesserRelease",
    "deesserGain", "deesserFrequency",
    "hpfFrequency", "hpfSlope",
    # channelPan pans a MONO source and does nothing on a stereo one. A stereo source pans with
    # channelPanL / channelPanR, where each leg of the pair sits (defaults 0.0 and 1.0): RODE's app
    # shows them as two knobs, and both legs at 0.0 is heard hard left.
    "channelPan", "channelPanL", "channelPanR",
})
# Extra channel-processing parameters a PRESET writes: the Aphex aural-exciter / big-bottom and the
# Depth/Sparkle/Punch (VoxLab) macros (all normalised 0..1 floats). aphexBBTune/aphexAETune are the two
# Aphex frequency TUNE knobs, decoded.
PRESET_EXTRA_FLOAT_PROPS = frozenset({
    "aphexBBDrive", "aphexAEMix", "channelDepth", "channelSparkle", "channelPunch",
    "aphexBBTune", "aphexAETune",
})
PRESET_FLOAT_PROPS = PROCESSING_PROPS | PRESET_EXTRA_FLOAT_PROPS


def _require_processing() -> None:
    """Guard the channel processing/effects WRITE path by the active device's "processing" cap.

    proc_selector()/fx_selector() are now device-aware: a fixed-layout desk (Pro II) uses its pinned
    proc_idx and a dynamic-layout desk (Duo) resolves the strip from the LIVE fader position
    (channelInputSource.index(source_code)), matching the read path - so non-Mic1 selectors are
    correct on both. The cap still gates the whole write path so a desk whose processing scheme is
    not yet verified refuses rather than writing a wrong selector (the stray-append hazard)."""
    dev = devices.get_active_device()
    if "processing" not in dev.caps:
        raise RuntimeError(
            f"Channel processing/effects writes are not yet verified on the {dev.name} "
            f"(only Mic1's selector is confirmed), so they are disabled to avoid a wrong-selector write.")


def set_channel_processing(channel: int, prop: str, value: float) -> int:
    """Set one channel-processing parameter (EQ/comp/gate/deesser/HPF/pan/VoxLab/Aphex).

    channel: 1-based index into PROC_CHANNEL_ORDER or a channel name (1-4 = Mic 1-4; also
    Bluetooth/USB1/Chat/USB2/CallMe1). prop: a name in PRESET_FLOAT_PROPS. value: NORMALISED
    0.0..1.0 (the unit maps it to the real dB/Hz/ms). Each channel has its OWN selector via
    proc_selector; the property name picks the parameter."""
    _require_processing()
    if prop not in PRESET_FLOAT_PROPS:
        raise ValueError(f"unknown/unsupported processing property: {prop}")
    v = max(0.0, min(1.0, float(value)))
    return _t.send_record(_t.build_global_prop_float(prop, proc_selector(channel), v))


def apply_processing_preset(channel: int, props: dict, delay: float = 0.08) -> int:
    """Apply a full processing preset to any processing channel (see proc_selector).

    `props` maps property name -> value: every float name is written as a normalised 0..1 float;
    channelAdvancedProcessing and each <module>On flag as a bool, channelCurrentFxPreset as an int.
    All land on the channel's OWN selector, exactly as the RODE app does when you pick a preset.

    MODULES ARE PART OF A PRESET. Without them it is a silent no-op: applied to a strip whose
    modules are switched off it writes all 33 parameters, reports the preset name, and sounds
    exactly the same, because nothing in the chain is engaged. A preset carries its module set as
    DATA (see ProjectAssets/
    mic_presets.json and _read_channel_preset_props, which records the live states on "save as"),
    rather than this function sweeping every module on, which would be a guess dressed as a preset.

    WHAT IT DELIBERATELY DOES NOT TOUCH: channelBypassProcessing, the channel's master on/off. That
    is the operator's own control, not part of a voicing: a mic left bypassed on purpose, and a
    preset that un-bypassed it mid-session would be a nasty surprise.

    ORDER MATTERS, hardware-established (see CHAIN_ADVANCED_PROP): the processing MODE first, so
    the strip is in the right mode before its values
    land and cannot re-derive them from the VoxLab knobs; then the modules, so a parameter never
    lands on a module that is about to be switched; then the floats; then the preset-name marker
    LAST, because the device blanks it to -1 the moment any parameter is written.
    Returns total bytes written."""
    _require_processing()
    sel = proc_selector(channel)
    mode, modules, floats, marker = [], [], [], []
    for name, val in props.items():
        if name == CHAIN_PRESET_MARKER_PROP:
            marker.append(_t.build_global_prop_int(name, sel, int(val)))
        elif name == CHAIN_ADVANCED_PROP:
            mode.append(_t.build_global_prop_bool(name, bool(val), sel))
        elif name in PROCESSING_MODULE_BOOLS:
            modules.append(_t.build_global_prop_bool(name, bool(val), sel))
        elif name in PRESET_FLOAT_PROPS:
            v = max(0.0, min(1.0, float(val)))
            floats.append(_t.build_global_prop_float(name, sel, v))
        else:
            raise ValueError(f"unsupported preset property: {name}")
    return _t.send_records_paced(mode + modules + floats + marker, delay=delay)


# Per-channel PROCESSING module on/off toggles (compact BOOL on the channel's processing selector).
# Decoded. The Aphex aural-exciter + Big Bottom share ONE enable (aphexOn).
PROCESSING_MODULE_ON = {
    "hpf": "hpfOn", "eq": "eqOn", "compressor": "compressorOn", "noiseGate": "noiseGateOn",
    "deesser": "deesserOn", "aphex": "aphexOn", "pan": "channelPanOn",
    "eqHigh": "eqHighOn", "eqMid": "eqMidOn", "eqLow": "eqLowOn",
}


# THE REST OF THE PER-STRIP CHAIN, added after the coverage audit.
# The 33 float params + the module bools + channelBypassProcessing are NOT the whole chain. Diffing
# the RODE app's own fader drag showed two more properties travelling with a channel, and both matter:
#   channelAdvancedProcessing - simple (the three VoxLab knobs) vs advanced (the 8 modules). In SIMPLE
#     mode the desk DERIVES the rest of the chain from Depth/Sparkle/Punch, so a chain restored onto a
#     strip left in the wrong mode can be regenerated over the top.
#   channelCurrentFxPreset    - which preset NAME the strip displays (0/1/2 = Neutral/Podcast Studio/
#     Broadcast, -1 = custom). The device blanks it to -1 as soon as a parameter is written, so it must
#     be written LAST, and it is a NAME only: never infer the values from it.
# The three eq*Q values also move with a channel, but the USB capture of a preset apply shows the app
# NEVER writes them - the device recomputes them from the EQ params - so they need no writer here.
CHAIN_ADVANCED_PROP = "channelAdvancedProcessing"
CHAIN_PRESET_MARKER_PROP = "channelCurrentFxPreset"


def set_channel_advanced(channel: int, on: bool) -> int:
    """Set a strip's processing MODE: True = advanced (modules + params), False = simple (the three
    VoxLab knobs). Compact bool on the channel's own processing selector, i.e. the same record shape
    as any module toggle. Verified on hardware: writing this flips the mode and changes NO
    parameter (the RODE app's "disabling Advanced will reset all parameters" warning is the APP
    clearing them itself, not the firmware)."""
    _require_processing()
    return _t.send_record(_t.build_global_prop_bool(CHAIN_ADVANCED_PROP, bool(on),
                                                   proc_selector(channel)))


def set_channel_preset_marker(channel: int, index: int) -> int:
    """Set which preset NAME a strip displays (channelCurrentFxPreset; -1 = custom). Write this LAST,
    after every parameter, or the device will blank it again."""
    _require_processing()
    return _t.send_record(_t.build_global_prop_int(CHAIN_PRESET_MARKER_PROP, proc_selector(channel),
                                                  int(index)))


def set_processing_module(channel: int, module: str, on: bool) -> int:
    """Enable/disable a per-channel PROCESSING module on any processing channel (mics + BT/USB1/
    Chat/USB2/CallMe1; see proc_selector). `module` is a key in PROCESSING_MODULE_ON (hpf / eq /
    compressor / noiseGate / deesser / aphex / pan / eqHigh / eqMid / eqLow), or the raw <module>On
    property name. Compact bool on the channel's processing selector."""
    _require_processing()
    sel = proc_selector(channel)
    prop = PROCESSING_MODULE_ON.get(module)
    if prop is None:
        if module in PROCESSING_MODULE_ON.values():
            prop = module
        else:
            raise ValueError(f"unknown processing module {module!r}; known: {list(PROCESSING_MODULE_ON)}")
    return _t.send_record(_t.build_global_prop_bool(prop, on, sel))


def set_processing_enabled(channel: int, on: bool) -> int:
    """Master per-channel PROCESSING on/off (the panel's 'Processing' toggle). The device property is
    INVERTED: channelBypassProcessing (compact bool) on the channel's processing selector - True =
    bypassed/OFF. So processing ON => channelBypassProcessing False. Do NOT name it
    set_channel_processing: that collides with the float param setter."""
    _require_processing()
    return _t.send_record(_t.build_global_prop_bool("channelBypassProcessing", not on, proc_selector(channel)))


# CHANNEL MUTE. `channelOutputMute` is a compact bool on the channel's own processing selector
# (SEL_CHANNEL_PROC + strip), i.e. byte-for-byte the same record shape as any module toggle above.
# PROVEN ON HARDWARE (Mic 2, strip 1, selector 0x1D): wrote True, channelOutputMute[1]
# flipped, and the firmware PROPAGATED it into the mix matrix - the twelve mixMute cells for that
# source all went True - so it is a REAL mute, not a stored flag. Wrote False, everything returned
# to baseline, no stray appended. See the long note in rcp_transfer.py for why an earlier "mute is
# unwritable" finding was wrong: it was tested in the mic-preamp CHANNEL-address format, which is
# not the format this property takes.
#
# Gated by the same "processing" cap as the module toggles, because it is the same selector family
# (both desks have that cap, and both have it hardware-confirmed for processing writes). EAR-PROVEN
# ON THE DUO TOO, on Mic 2 (strip 1, deliberately not strip 0): the mic went
# silent and unmute brought it back. On that desk a mute moves 14 properties, its own plus the 13
# matrix cross-points for the source, which is the firmware propagating it exactly as here.
MUTE_PROP = "channelOutputMute"


def set_channel_mute(channel, on: bool) -> int:
    """Mute or unmute a channel (the unit's own mute button, from software).

    `channel` is anything proc_selector accepts (1-4 = Mic 1-4, or a channel name), so the write
    follows the channel to whichever fader STRIP it is currently on, exactly as processing does.
    Muting here is the same mute as the desk's button: it silences that channel in every mix, not
    just the monitor."""
    _require_processing()
    return _t.send_record(_t.build_global_prop_bool(MUTE_PROP, bool(on), proc_selector(channel)))


# CHANNEL SOLO / LISTEN. `channelCueEnable` is the desk's Solo (RODE call it "Listen"): it replaces
# the headphone feed with just the soloed channels. Same record shape and same selector family as the
# mute above - a compact bool on SEL_CHANNEL_PROC + strip - which is exactly why it was worth trying.
#
# It had sat in PARITY.md as "physical/MIDI only" for a year on the reasoning that the RODE app has
# no control for it. That reasoning has now been wrong five times: no app control means no CAPTURE
# ROUTE, never unwritable.
#
# PROVEN ON HARDWARE (Mic 3, strip 2, selector 0x1E): wrote True, channelCueEnable[2]
# flipped to True and nothing else in the model moved; wrote False, the whole vector returned to
# baseline. Model length identical before and after (139485 bytes) and the occurrence counts for
# channelCueEnable / channelOutputMute / channelInputSource were unchanged, so no stray was appended.
#
# UNLIKE the mute, the desk pushes NOTHING back when this is written from software: not the
# property, not a matrix propagation. A solo is a monitoring change, so there is no mix matrix for
# it to propagate into.
CUE_PROP = "channelCueEnable"


def set_channel_solo(channel, on: bool) -> int:
    """Solo ("Listen") a channel, or clear it: the unit's own Solo button, from software.

    `channel` is anything proc_selector accepts, so the write follows the channel to whichever fader
    STRIP it is currently on. This only changes what you HEAR in the headphones - the mixes, the
    recording and anything being streamed are untouched - which is the opposite of the mute above and
    is worth saying plainly wherever it is offered."""
    _require_processing()
    return _t.send_record(_t.build_global_prop_bool(CUE_PROP, bool(on), proc_selector(channel)))


def read_solos(blob: bytes | None = None) -> list:
    """The live solo state of every strip, indexed by STRIP (same indexing as the mute vector)."""
    if blob is None:
        blob = _r.read_model_stable()
    return _r.values(blob, CUE_PROP)


def clear_all_solos(blob: bytes | None = None) -> int:
    """Clear every solo on the desk, and report how many were actually cleared.

    Exists because a solo left on is the one state in this file that can leave a blind operator with
    NO usable audio and nothing on screen saying why: the headphones carry one channel and the rest
    of the desk sounds dead. Anything that solos should be able to undo it in one call, without first
    having to know which strip did it."""
    cleared = 0
    for strip, on in enumerate(read_solos(blob)):
        if on:
            _t.send_record(_t.build_global_prop_bool(CUE_PROP, False, proc_selector_for_strip(strip)))
            cleared += 1
    return cleared


# WIRELESS MIC MUTE. `channelWirelessMute` mutes a RODE wireless transmitter paired to the channel,
# at the transmitter, which is a different thing from channelOutputMute above: that one silences the
# strip inside the desk, this one is the desk telling the mic itself to stop sending.
#
# THE ADDRESS IS THE SAME FAMILY, and that is measured, not assumed. Read off a live Duo: ten
# occurrences, CHANNEL[strip] at 0x1C..0x25, which is byte-for-byte the same set
# of nodes channelOutputMute and channelCueEnable sit on. Both desks publish ten. So it is a compact
# bool on proc_selector(channel) like every other toggle here, and it needs no capture: a capture
# would only re-derive an address the tree already gives.
#
# IT DOES NOT BELONG IN CHANNEL_PROP, which lists the properties addressed at the INPUT selector: a
# 30-entry table indexed by SOURCE CODE, a different block and a different index, where a write goes
# to an address the audio engine does not read.
#
# NOT EAR-PROVEN, and it cannot be here: nobody on this project owns a RODE wireless mic, though
# users do. The write is right; whether a transmitter
# obeys it is the user's to confirm. That is why the reply says what was sent rather than what
# happened, and why nothing here claims the mic went quiet.
WIRELESS_MUTE_PROP = "channelWirelessMute"


def set_channel_wireless_mute(channel, on: bool) -> int:
    """Mute or unmute a RODE wireless mic paired to a channel, from software.

    `channel` is anything proc_selector accepts, so the write follows the channel to whichever fader
    STRIP it is currently on, exactly as the mute and the solo do."""
    _require_processing()
    return _t.send_record(_t.build_global_prop_bool(WIRELESS_MUTE_PROP, bool(on),
                                                    proc_selector(channel)))


def channel_strip_index(channel) -> int:
    """The 0-based fader STRIP a channel currently sits on (device-aware, same resolution as
    proc_selector). Public because the server needs it to key the announcer's per-strip caches -
    mute state arrives from the desk keyed by strip, and our own writes have to agree."""
    return _channel_strip(channel)


# Per-channel "always on" EFFECTS (Echo + Reverb) live on a SEPARATE selector base from the processing
# modules: fx base 0x26 + STRIP index (decoded; echoOn/reverbOn confirmed). Mic channels only
# in the app's UI. CONFIRMED strip-indexed on the Duo: Mic2's echoOn write used selector 0x27
# (base 0x26 + strip 1). Now addressed via fx_selector(), the SAME device-aware strip resolver as
# processing, so a mic moved off its default fader still hits the right strip (was 0x26+(ch-1)).
SEL_CHANNEL_FX = 0x26
CHANNEL_FX_ON = {"echo": "echoOn", "reverb": "reverbOn"}


def set_channel_effect(channel: int, effect: str, on: bool) -> int:
    """Enable/disable a per-channel always-on EFFECT (echo / reverb), channel 1-4. Compact bool on the
    channel-FX selector fx_selector(channel) = fx base 0x26 + the channel's LIVE strip index
    (device-aware), so a mic moved off its default fader still hits the right strip."""
    _require_processing()
    if not 1 <= channel <= 4:
        raise ValueError(f"channel must be 1-4, got {channel}")
    prop = CHANNEL_FX_ON.get(effect)
    if prop is None:
        raise ValueError(f"unknown channel effect {effect!r}; known: {list(CHANNEL_FX_ON)}")
    return _t.send_record(_t.build_global_prop_bool(prop, on, fx_selector(channel)))


# The adjustable PARAMS for each channel effect. Decoded by app-drive capture: the app
# writes them as GLOBAL float64 (normalised 0..1) on the SAME per-channel selector as the on/off,
# 0x26+(ch-1) - byte-for-byte e.g. Mic1 echoMix -> selector 0x26, Mic2 echoMix -> 0x27, Mic1
# reverbMix -> 0x26. Names match the pad-FX names (echoMix=Level, reverbModel=size/type, etc.).
CHANNEL_FX_PARAMS = {
    "echo": ["echoMix", "echoLowCut", "echoHighCut", "echoDelay", "echoDecay"],
    "reverb": ["reverbMix", "reverbModel", "reverbLowCut", "reverbHighCut"],
}
_CHANNEL_FX_PARAM_SET = {p for ps in CHANNEL_FX_PARAMS.values() for p in ps}


def set_channel_effect_param(channel: int, param: str, value01: float) -> int:
    """Set one channel-effect PARAM (echo*/reverb*) on a mic channel 1-4, value normalised 0..1.
    Global float64 on the channel-FX selector fx_selector(channel) = fx base 0x26 + the channel's
    LIVE strip index (device-aware); byte-for-byte the app's own write."""
    _require_processing()
    if not 1 <= channel <= 4:
        raise ValueError(f"channel must be 1-4, got {channel}")
    if param not in _CHANNEL_FX_PARAM_SET:
        raise ValueError(f"unknown channel-effect param {param!r}")
    v = max(0.0, min(1.0, float(value01)))
    return _t.send_record(_t.build_global_prop_float(param, fx_selector(channel), v))


# Per-channel "Level" (dB) for the NON-MIC channels. The app's channel panel shows a -/+ Level for
# Bluetooth/USB1/Chat/USB2 (and Headset on the Duo); the device property is inputDigitalGain (signed
# int dB), NOT the 0x1c processing line. It sits on the channel's INPUTSOURCE node beside mic gain
# and everything else input-shaped, so it is addressed by DeviceModel.input_path like the rest.
#
# THE DUO'S LINE LEVEL WAS BROKEN IN EXACTLY THE WAY ITS MIC GAIN WAS, and had never been run: its
# retired selectors (0x48 + source_code) pointed at the same dead MIX block. Only the Headset one had
# ever been written at all, and the giveaway is in the dump - of 390 MIX nodes, the only three
# carrying an input property are the three this app has written to. See DeviceModel.input_path.


def line_level_channels(dev=None) -> list:
    """The channel names that take a Level: every non-mic, non-bus input this desk exposes."""
    dev = dev or devices.get_active_device()
    return [c["name"] for c in dev.ui_channels_for(usb_expanded())
            if c.get("kind") == "line"]


def set_channel_level(channel_name: str, db: int) -> int:
    """Set a non-mic channel's Level (inputDigitalGain, signed dB int): Bluetooth/USB1/Chat/USB2,
    plus Headset on the Duo. Mic channels use set_mic_gain instead."""
    dev = devices.get_active_device()
    if "input_mic_write" not in dev.caps:
        raise RuntimeError(
            f"Channel Level (inputDigitalGain) is not supported on the {dev.name} yet - "
            f"its write format has not been decoded.")
    ch = next((c for c in dev.ui_channels_for(usb_expanded())
               if c.get("kind") == "line" and c["name"] == channel_name), None)
    if ch is None:
        raise ValueError(
            f"set_channel_level only for {line_level_channels(dev)}; got {channel_name!r}")
    return _t.send_record(_t.build_path_prop_int("inputDigitalGain", int(db), dev.input_path(ch)))


# Routing mode (Device Config > Output > Routing): each USB/headphone output has an outputMixMinus int
# (0 = Main Mix, 1 = Mix Minus, 2 = Custom), written as a GLOBAL record on the OUTPUT's own selector.
# Decoded (captured one output's toggle on selector 0x38). The full per-output selector map +
# the Custom channel-include matrix are not yet enumerated, so no setter is exposed here yet.
OUTPUT_MIX_MINUS = {"property": "outputMixMinus", "values": {0: "Main Mix", 1: "Mix Minus", 2: "Custom"}}


# ============================================================================
# READS - per-channel state for the UI (one model dump covers everything)
# ----------------------------------------------------------------------------
# The processing block (modules + params, 10 occurrences) is indexed by position in PROC_CHANNEL_ORDER.
# The mic input block (gain/phantom/polarity/type, 30 occurrences) puts Mic1-4 at slots 0-3. The non-mic
# "Level" (inputDigitalGain) sits at the slots below, decoded (write selectors 0x75-0x78 ->
# read slots 7-10).
# ============================================================================

# Channel-processing PARAMETERS (all normalised 0..1 floats). Nth occurrence = Nth channel.
PROCESSING_READ_PROPS = (
    "eqLowGain", "eqLowShelf", "eqMidGain", "eqMidBell", "eqHighGain", "eqHighBell",
    "eqLowQ", "eqMidQ", "eqHighQ",      # independent and writable (see PROCESSING_PROPS)
    "hpfFrequency", "hpfSlope",
    "compressorThreshold", "compressorRatio", "compressorAttack", "compressorRelease",
    "compressorGain",
    "noiseGateThreshold", "noiseGateAttack", "noiseGateHold", "noiseGateRelease",
    "noiseGateRange", "noiseGateHysteresis",
    "deesserThreshold", "deesserRatio", "deesserAttack", "deesserRelease",
    "deesserGain", "deesserFrequency",
    "channelPan", "channelPanL", "channelPanR",
    "channelDepth", "channelSparkle", "channelPunch", "aphexBBDrive", "aphexAEMix",
    "aphexBBTune", "aphexAETune",
)
# Module enable bools, read per channel (10 occurrences, proc index).
PROCESSING_MODULE_BOOLS = (
    "hpfOn", "eqOn", "compressorOn", "noiseGateOn", "deesserOn", "aphexOn", "channelPanOn",
    "eqHighOn", "eqMidOn", "eqLowOn",
)
# The per-channel UI READ MAP now lives PER-DEVICE in devices.py (see DeviceModel.ui_channels). The
# readers below use the ACTIVE device's ui_channels, so a Duo maps to its own strips instead of the
# Pro II's. UI_CHANNELS stays as the Pro II list so external references (e.g. server.py's
# _UI_CHAN_BY_NAME) still resolve. An entry carries "source_code" (the one per-channel number: input
# address and matrix source alike) and an optional "proc_idx"; a missing proc_idx means "resolve
# dynamically as channelInputSource.index(source_code)".
UI_CHANNELS = devices.PRO2.ui_channels


# --- Active-device UI-channel helpers (used by the web server's Channels view) ------------------
# NEVER build one static {name: entry} map from the Pro II UI_CHANNELS at import time: it breaks on
# the Duo, whose channels are "Headset"/"USB1"/... rather than "Mic 3"/"Mic 4". These resolve
# against the CONNECTED device each call, so the write actions look up the right strip.

def active_ui_channels() -> tuple:
    """The connected device's UI channels (falls back to the Pro II layout when offline)."""
    return devices.get_active_device().ui_channels_for(usb_expanded())


def ui_channel_map() -> dict:
    """{name: ui-channel entry} for the connected device, e.g. {"Mic1": {...}, "Headset": {...}}."""
    return {c["name"]: c for c in active_ui_channels()}


def mic_channel_number(ch: dict) -> int:
    """The 1-based mic channel number for a mic UI-channel entry, valid on every desk.

    Mics take the lowest source codes on both desks (Pro II 0-3, Duo 0-1), so channel ==
    source_code + 1. Used to route the adaptive Channels
    view's mic Level/effects to set_mic_gain / set_channel_effect (which take a 1-based mic
    channel)."""
    if ch.get("kind") != "mic":
        raise ValueError(f"{ch.get('name')!r} is not a mic channel")
    return int(ch["source_code"]) + 1


# --- THE INPUT READ -----------------------------------------------------------------------------
# Every per-input value (mic gain, phantom, polarity, mic type, instrument gain, line level, input
# type, strip colour) comes through here, and NOTHING reads them out of a values() list by position.

def input_value(blob: bytes, ch: dict, prop: str, dev=None):
    """One INPUT property for one channel, read at the same address the matching write goes to.

    ONE lookup, because there is one address: INPUTSOURCE[source_code] (DeviceModel.input_path).

    NO per-device "override" node is tried first. On a Duo that override is the dead MIX block, so
    after any write the app READS BACK ITS OWN INERT COPY and reports it as the desk's setting,
    which makes the gain display go stale the moment anybody touches the desk itself. Reading only
    the live address fixes the display for the same reason it fixes the write, and it leaves the
    stale copies where they are: harmless once nothing
    reads them, and not removable without a factory reset.

    Returns None when this desk does not publish that value for that channel (a bus has no input).
    """
    dev = dev or devices.get_active_device()
    return _r.path_value(blob, dev.input_path(ch), prop, None)


def read_mixer() -> dict:
    """Mic-focused live read for the web UI: gain + phantom + polarity + type per mic, MIDI, fw.

    Device-aware: reads exactly the mics the CONNECTED device exposes (4 on the Pro II, 2 on the
    Duo), keyed by 1-based mic channel, each mic read from its own ADDRESS - so the Duo shows
    Mic 1/Mic 2, never mislabels its Headset slot as "Mic 3", and never reports one mic's gain as
    another's. Gated by the device's "channel_read" cap so a desk whose input layout is not yet
    verified refuses rather than guess."""
    dev = devices.get_active_device()
    if "channel_read" not in dev.caps:
        raise RuntimeError(
            f"Live mixer read is not yet enabled on the {dev.name} (its per-channel read map "
            f"awaits one hardware check - see devices.DUO).")
    blob = _r.read_model()
    mics = {}
    for entry in active_ui_channels():
        if entry.get("kind") != "mic":
            continue
        power = input_value(blob, entry, "inputPower", dev)
        phase = input_value(blob, entry, "inputPhaseFlip", dev)
        mics[mic_channel_number(entry)] = {
            "gain_db": input_value(blob, entry, "inputMicrophoneGain", dev),
            "phantom": None if power is None else (power == 1),
            "polarity": None if phase is None else (phase is True),
            "mic_type": input_value(blob, entry, "inputMicrophoneType", dev),
        }
    return {
        "midi": _r.first(blob, "systemMidiControl"),
        "firmware": _r.first(blob, "systemFirmwareVersion"),
        "mics": mics,
    }


# ============================================================================
# THE WHOLE PER-STRIP CHAIN, addressed BY STRIP (for "the chain follows the channel")
# ----------------------------------------------------------------------------
# Everything above addresses a CHANNEL and resolves its strip from the live fader vector, which is
# exactly right for editing. Moving a chain between strips needs the opposite: address a STRIP
# directly, because during a swap the vector is changing underneath us.
#
# WHAT IS IN THE CHAIN (settled by diffing the RODE app's own drag):
# channelBypassProcessing + the 10 module bools + the float params + the FX block (echoOn/reverbOn
# and their params) + channelAdvancedProcessing + channelCurrentFxPreset. NOT the routing matrix
# (source-keyed) and NOT the mic input properties (gain/type/P48/polarity belong to
# the physical XLR, and the app leaves them alone). The three eq*Q values follow device-side.
CHAIN_BOOL_PROPS = tuple(PROCESSING_MODULE_BOOLS) + ("channelBypassProcessing", CHAIN_ADVANCED_PROP)
CHAIN_FX_BOOL_PROPS = ("echoOn", "reverbOn")
# Floats: the processing params plus the echo/reverb params, all normalised 0..1.
CHAIN_FLOAT_PROPS = tuple(PRESET_FLOAT_PROPS)
CHAIN_FX_FLOAT_PROPS = tuple(_CHANNEL_FX_PARAM_SET)
# A float difference smaller than this is the device's own float32 rounding, not a real change, so we
# do not waste a write on it (the RODE app's own swap leaves differences this size behind).
CHAIN_FLOAT_EPS = 1e-6

# WRITE ORDER WITHIN THE FLOATS, and why it is not arbitrary (learned the hard way on the first live
# swap: four values would not stick). In SIMPLE mode the desk DERIVES most of the chain
# from the three VoxLab knobs, so writing a derived value BEFORE a knob just gets it recomputed away.
# The first swap wrote them in dict order and lost compressorThreshold, deesserThreshold,
# noiseGateThreshold and aphexAEMix to exactly that. So: knobs FIRST, then everything neutral, then
# the derived family LAST, which makes the copied value win in either mode (in advanced mode nothing
# derives, so the order is harmless there). This also reproduces the source strip EXACTLY, where the
# RODE app's own swap regenerates the derived values and lands slightly off.
VOXLAB_KNOB_PROPS = ("channelDepth", "channelSparkle", "channelPunch")
DERIVED_FLOAT_PROPS = ("eqLowGain", "eqMidGain", "eqHighGain",
                       "compressorThreshold", "compressorGain", "deesserThreshold",
                       "noiseGateThreshold", "noiseGateRange", "noiseGateRelease",
                       "aphexAEMix", "aphexBBDrive")


def _float_write_order(props) -> list:
    """Chain float property names in safe write order (see VOXLAB_KNOB_PROPS)."""
    knobs = [p for p in VOXLAB_KNOB_PROPS if p in props]
    derived = [p for p in DERIVED_FLOAT_PROPS if p in props]
    rest = [p for p in props if p not in knobs and p not in derived]
    return knobs + rest + derived


def proc_selector_for_strip(strip_idx: int) -> int:
    """Processing selector for a STRIP index (0-based), not a channel."""
    return SEL_CHANNEL_PROC + int(strip_idx)


def fx_selector_for_strip(strip_idx: int) -> int:
    """Effects selector for a STRIP index (0-based), not a channel."""
    return SEL_CHANNEL_FX + int(strip_idx)


def read_chains(blob: bytes | None = None, strips: int | None = None) -> list:
    """Every strip's full chain, from ONE model dump. Returns a list indexed by STRIP:
    {"bools": {...}, "floats": {...}, "fx_bools": {...}, "fx_floats": {...}, "marker": int|None}.

    Read-only. Values are exactly what the device reports, so two chains can be diffed directly."""
    if blob is None:
        blob = _r.read_model_stable()

    def vec(prop, kind=None):
        return _r.values(blob, prop, kind)

    bools = {p: vec(p) for p in CHAIN_BOOL_PROPS}
    floats = {p: vec(p, "float") for p in CHAIN_FLOAT_PROPS}
    fx_bools = {p: vec(p) for p in CHAIN_FX_BOOL_PROPS}
    fx_floats = {p: vec(p, "float") for p in CHAIN_FX_FLOAT_PROPS}
    markers = vec(CHAIN_PRESET_MARKER_PROP, "int")
    if strips is None:
        # The strip count is whatever the per-strip arrays actually carry (10 on both desks).
        strips = max([len(v) for v in bools.values()] + [len(markers)] or [0])

    def at(lst, i):
        return lst[i] if 0 <= i < len(lst) else None

    out = []
    for i in range(strips):
        out.append({
            "bools": {p: at(bools[p], i) for p in CHAIN_BOOL_PROPS},
            "floats": {p: at(floats[p], i) for p in CHAIN_FLOAT_PROPS},
            "fx_bools": {p: at(fx_bools[p], i) for p in CHAIN_FX_BOOL_PROPS},
            "fx_floats": {p: at(fx_floats[p], i) for p in CHAIN_FX_FLOAT_PROPS},
            "marker": at(markers, i),
        })
    return out


def chain_diff(desired: dict, current: dict) -> list:
    """Which chain properties differ between two chain dicts, as [(kind, prop, value)] in WRITE
    ORDER: the processing mode first, then bools, then floats, then the preset marker last (see
    apply_processing_preset for why). `current` may be None to force a full write."""
    out = []
    cur = current or {}

    def differs(group, prop, val):
        if val is None:
            return False
        other = (cur.get(group) or {}).get(prop)
        if other is None:
            return True
        if isinstance(val, float) and isinstance(other, float):
            return abs(val - other) > CHAIN_FLOAT_EPS
        return val != other

    # Mode FIRST: in simple mode the desk derives the chain from the VoxLab knobs, so the strip must
    # be in the right mode before its values land.
    adv = (desired.get("bools") or {}).get(CHAIN_ADVANCED_PROP)
    if differs("bools", CHAIN_ADVANCED_PROP, adv):
        out.append(("bool", CHAIN_ADVANCED_PROP, adv))
    for p, v in (desired.get("bools") or {}).items():
        if p != CHAIN_ADVANCED_PROP and differs("bools", p, v):
            out.append(("bool", p, v))
    for p, v in (desired.get("fx_bools") or {}).items():
        if differs("fx_bools", p, v):
            out.append(("fx_bool", p, v))
    floats = desired.get("floats") or {}
    for p in _float_write_order(list(floats)):
        if differs("floats", p, floats[p]):
            out.append(("float", p, floats[p]))
    for p, v in (desired.get("fx_floats") or {}).items():
        if differs("fx_floats", p, v):
            out.append(("fx_float", p, v))
    # Marker LAST: the device blanks it the moment any parameter is written.
    m = desired.get("marker")
    if m is not None and m != cur.get("marker"):
        out.append(("int", CHAIN_PRESET_MARKER_PROP, m))
    return out


def write_chain(strip_idx: int, desired: dict, current: dict | None = None,
                delay: float = 0.02) -> dict:
    """Write a chain onto ONE strip, sending only what differs from `current` (pass None for a full
    write). Returns {"written": <count>, "props": [names]}. Caller must hold the HID lock."""
    _require_processing()
    ops = chain_diff(desired, current)
    if not ops:
        return {"written": 0, "props": []}
    psel, fsel = proc_selector_for_strip(strip_idx), fx_selector_for_strip(strip_idx)
    recs = []
    for kind, prop, val in ops:
        if kind == "bool":
            recs.append(_t.build_global_prop_bool(prop, bool(val), psel))
        elif kind == "fx_bool":
            recs.append(_t.build_global_prop_bool(prop, bool(val), fsel))
        elif kind == "float":
            recs.append(_t.build_global_prop_float(prop, psel, max(0.0, min(1.0, float(val)))))
        elif kind == "fx_float":
            recs.append(_t.build_global_prop_float(prop, fsel, max(0.0, min(1.0, float(val)))))
        elif kind == "int":
            recs.append(_t.build_global_prop_int(prop, psel, int(val)))
    _t.send_records_paced(recs, delay=delay)
    return {"written": len(recs), "props": [p for _k, p, _v in ops]}


# ============================================================================
# OFF-FADER CHAIN STORE
# ----------------------------------------------------------------------------
# Processing and effects belong to a fader STRIP, not to a channel. So a channel that leaves the
# layout leaves its voicing behind on the strip it vacated, and the next occupant of that strip
# overwrites it. Nothing on the desk remembers it: an off-fader channel has no strip, therefore no
# processing to read. Only we can hold it.
#
# Without this, "the chain follows the channel" covers just the channels present in BOTH layouts.
# A channel that goes off-fader loses its chain outright, and one that comes BACK silently inherits
# whatever voicing sat on the strip it lands on. Measured: a fader move brought
# Mic 2 back from off-fader onto the strip USB 1 had just left, and Mic 2 came back with USB 1's
# processing - gate off, EQ flat, processing bypassed.
#
# Stereo link makes this the common case rather than an edge case, since linking a pair evicts the
# upper mic and the unlink that returns it may be days later. Hence a FILE, not a memory cache.
_ORPHAN_FILE = "off_fader_chains.json"


def _orphan_load() -> dict:
    """The whole store: {device hardware id: {channel code as str: chain}}. {} if absent/corrupt -
    a lost parked chain is a channel that comes back with the wrong voicing, never a crash."""
    try:
        with open(_paths.data_path(_ORPHAN_FILE), "r", encoding="utf-8") as fh:
            data = _json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _orphan_save(store: dict) -> None:
    try:
        with open(_paths.data_path(_ORPHAN_FILE), "w", encoding="utf-8") as fh:
            _json.dump(store, fh)
    except Exception:
        pass          # never fail a fader move because the parking file could not be written


def park_chain(device_id: str, code: int, chain: dict) -> None:
    """Keep `code`'s chain while it has no strip. Keyed by DESK as well as channel, so two desks
    do not hand each other their voicings (same reasoning as _applied_preset_record in server)."""
    if not chain:
        return
    store = _orphan_load()
    store.setdefault(str(device_id or ""), {})[str(int(code))] = chain
    _orphan_save(store)


def take_chain(device_id: str, code: int) -> dict | None:
    """Pop `code`'s parked chain, or None if we never saw it leave. Popping is the point: once it
    is back on a strip the desk owns it again, and a stale copy would later overwrite an edit."""
    store = _orphan_load()
    per_device = store.get(str(device_id or ""))
    if not isinstance(per_device, dict):
        return None
    chain = per_device.pop(str(int(code)), None)
    if chain is not None:
        _orphan_save(store)
    return chain if isinstance(chain, dict) else None


def read_channels(blob: bytes | None = None) -> list:
    """Read every UI channel's full state in one model dump, using the ACTIVE device's read map.

    Returns a list (device ui_channels order) of dicts: name, label, kind, plus muted, level_db,
    processing_on, modules{}, params{} for all; and for mics also phantom, polarity, mic_type,
    effects{}. Mirrors the RODE app's per-channel panel so the web UI can present one adaptive
    Channels view.

    INPUT values (mic gain / power / polarity / type; line inputDigitalGain) are read BY ADDRESS
    through `input_value`, never by position in a values() list. PROCESSING/EFFECTS arrays ARE
    positional, and legitimately so: they are per-STRIP and the strip index is resolved from the
    live fader vector, so the index means something. Indexed by proc_idx when the device pins one
    (Pro II), else channelInputSource.index(source_code) (Duo), so a source with no strip assigned
    reads its processing as None.

    Gated by the device's "channel_read" cap: a device whose per-channel read map is not yet
    verified (e.g. the Duo, pending one hardware check - see devices.DUO) refuses here rather than
    returning Pro II-shaped, mislabelled values."""
    dev = devices.get_active_device()
    if "channel_read" not in dev.caps:
        raise RuntimeError(
            f"Per-channel reads are not yet enabled on the {dev.name}: the read mapping is decoded "
            f"but awaits one hardware check (see devices.DUO), so it refuses rather than mislabel.")
    if blob is None:
        blob = _r.read_model_stable()

    def vec(prop, kind=None):
        return _r.values(blob, prop, kind)

    bypass = vec("channelBypassProcessing")
    # MUTE is live state rather than a stored setting, but it belongs on the channel panel: it is
    # the control you reach for most and, until now, the only way to reach it was the desk itself.
    # Indexed by STRIP like the processing arrays (see set_channel_mute).
    mutes = vec(MUTE_PROP)
    # SOLO, the same shape and the same indexing as the mute (see set_channel_solo). Live state, not
    # a stored setting, so it is read every time the panel is filled rather than cached.
    solos = vec(CUE_PROP)
    # WIRELESS MIC MUTE, same block and same strip indexing again (see WIRELESS_MUTE_PROP). Read for
    # every channel and surfaced only on the mics, because that is where a wireless mic can land.
    wireless = vec(WIRELESS_MUTE_PROP)
    modules = {m: vec(m) for m in PROCESSING_MODULE_BOOLS}
    params = {p: vec(p, "float") for p in PROCESSING_READ_PROPS}
    echo = vec("echoOn")
    reverb = vec("reverbOn")
    # The rest of the chain (see CHAIN_ADVANCED_PROP): the processing MODE and the preset-name marker
    # are per strip and travel with a channel, so profiles and the fader swap need them too.
    advanced = vec(CHAIN_ADVANCED_PROP)
    preset_marker = vec(CHAIN_PRESET_MARKER_PROP)
    fx_params = {p: vec(p, "float") for p in _CHANNEL_FX_PARAM_SET}
    src_vec = vec("channelInputSource")   # for dynamic proc-index resolution (both desks)
    publish_strip_vector(src_vec)         # refresh the strip map for free off this dump

    def at(lst, i):
        return lst[i] if (i is not None and 0 <= i < len(lst)) else None

    def proc_index(ch):
        pi = ch.get("proc_idx")
        if pi is not None:
            return pi
        try:
            return src_vec.index(ch.get("source_code"))
        except (ValueError, TypeError):
            return None   # source not assigned to any strip (e.g. Duo Mic2 while combo-linked)

    def inp(ch, prop):
        return input_value(blob, ch, prop, dev)

    out = []
    for ch in dev.ui_channels_for(usb_expanded()):
        pi = proc_index(ch)
        # A BUS HAS NO PROCESSING, and this is where that is decided - see the note on
        # devices.PRO2_UI_CHANNELS. The strip PUBLISHES a whole chain and the desk accepts writes to
        # it, so every value below reads back perfectly; none of it reaches the audio. Reporting it
        # would put an equaliser on the page that does nothing, which is worse than having none.
        chain = ch["kind"] != "bus"
        rec = {
            "name": ch["name"], "label": ch["label"], "kind": ch["kind"],
            "stereo": ch.get("stereo", False),
            # WHETHER IT HAS A STRIP AT ALL, which is a DIFFERENT thing from the desk not
            # reporting its state and needs different words. Mute, solo and processing are all
            # per-STRIP, so a source on no fader has none of them - and "the unit did not report
            # this" sends somebody looking for a fault when the answer is "put it on a fader".
            # True of Expanded's four until they are assigned, and of a Duo's Mic 2 while combo
            # linked.
            "on_fader": pi is not None,
            "muted": at(mutes, pi),
            "solo": at(solos, pi),
            "processing_on": (None if not chain or at(bypass, pi) is None
                              else (not at(bypass, pi))),
            "modules": {m: (at(modules[m], pi) if chain else None)
                        for m in PROCESSING_MODULE_BOOLS},
            "params": {p: (at(params[p], pi) if chain else None) for p in PROCESSING_READ_PROPS},
            # Part of the chain, not decoration: see CHAIN_ADVANCED_PROP.
            "advanced": at(advanced, pi) if chain else None,
            "preset_index": at(preset_marker, pi) if chain else None,
            # Read for the profile's sake (and for anyone deciding what these mean later). The strip
            # colour is the unit's own AARRGGBB string; input_type is None on a slot with no physical
            # input, because -1 there is a placeholder rather than a setting.
            "colour": inp(ch, "inputColour"),
            "instrument_gain": inp(ch, "inputInstrumentGain"),
            "input_type": (None if inp(ch, "inputType") in (None, -1) else inp(ch, "inputType")),
        }
        if ch["kind"] == "mic":
            rec["level_db"] = inp(ch, "inputMicrophoneGain")
            # None, not False, when the desk does not publish it: a mic whose phantom state is
            # unknown must not read as "phantom is off" (bool(None) is False and says the wrong
            # thing out loud to someone who cannot see the screen).
            power = inp(ch, "inputPower")
            phase = inp(ch, "inputPhaseFlip")
            rec["phantom"] = None if power is None else bool(power == 1)
            rec["polarity"] = None if phase is None else bool(phase)
            rec["mic_type"] = inp(ch, "inputMicrophoneType")
            # None, not False, when the desk does not publish it, for the same reason phantom is:
            # "the wireless mic is not muted" and "we do not know" must not sound the same.
            rec["wireless_muted"] = at(wireless, pi)
            rec["effects"] = {
                "echo": at(echo, pi), "reverb": at(reverb, pi),
                "echo_params": {p: at(fx_params[p], pi) for p in CHANNEL_FX_PARAMS["echo"]},
                "reverb_params": {p: at(fx_params[p], pi) for p in CHANNEL_FX_PARAMS["reverb"]},
            }
        elif ch["kind"] == "bus":
            # NO LEVEL, AND NO PROCESSING EITHER. A bus (the SMART pads mix) has mute, solo and a
            # fader, and that is all. Its level is the FADER, not an input gain - its input slot
            # carries no digital gain - and its chain does nothing to the audio, which was heard on
            # BOTH desks on. Left as None rather than omitted so every channel
            # record has the same keys and the UI can test one field.
            rec["level_db"] = None
        else:
            rec["level_db"] = inp(ch, "inputDigitalGain")
        out.append(rec)
    return out
