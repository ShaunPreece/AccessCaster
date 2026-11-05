"""Every channel and channel-processing operation, read and write: inputs (gain, phantom,
polarity, mic type, line Level), the per-strip chain (processing, effects, mute, solo, wireless
mute), and the reads behind the Channels page. The web layer and the CLI call into this one
module, the way soundpad.py owns all pad logic.

LAYERING (mirrors soundpad.py over pad_records.py):
  rcp_transfer.py  - report-3 byte builders (build_*), the send layer, and the non-per-channel
                     mixer/system/routing functions.
  rcp_read.py      - the model reader (values, first, path_value, read_model, read_model_stable).
  channels.py      - THIS FILE.

TWO ADDRESSING SCHEMES, and every write here uses exactly one. INPUT values live at the channel's
INPUTSOURCE node (DeviceModel.input_key), keyed by source code. The CHAIN lives on the fader STRIP
the channel currently sits on, keyed by its position in the live fader vector.
"""

import contextlib as _contextlib
import time as _time

import desk_live  # the one listener, whose chain pushes untrust the fader layout
import desk_model  # the desk as last seen: the fader layout and the USB mode
import devices  # per-device model (mic selectors, channel order) keyed by product id
import diagnostics
import paths as _paths  # writable data dir for the off-fader chain store
import rcp_read as _r  # low-level model reader (values, first, read_model, read_model_stable)
import rcp_transfer as _t  # low-level byte builders + send layer (build_*, send_record, send_records_paced)
import recorder  # outputMultiMode's two halves
import rode_app  # RODE's app open: its fader moves are not pushed

# ============================================================================
# MIC INPUT CHANNELS (gain / phantom / polarity / type) - the XLR preamp block
# ----------------------------------------------------------------------------
# Every input write goes to INPUTSOURCE[source_code] (DeviceModel.input_key), on every desk.
# DO NOT reintroduce a per-device input selector table (MIC_CHANNEL_SEL / INPUT_DIGITAL_GAIN_SEL):
# it is a second encoding of the same rule, and a per-device copy can encode a DIFFERENT and inert
# address, which is how a mic gain comes to report success while changing no audio.
# ============================================================================


def _mic_key(channel: int) -> tuple:
    """The node key of a 1-based mic channel's input values on the CONNECTED device.

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
    return dev.input_key(ch)


# Mic input type / voicing: inputMicrophoneType (int), at the same address as the gain. All 11
# selectable types confirmed 0..10 by stepping RODE's mic locker and reading each back. NOT mapped:
# "Wireless", selectable only with a RODE wireless device paired, which the desk sets by itself when
# one connects.
MIC_TYPE = {0: "Line-in", 1: "Instrument", 2: "Dynamic", 3: "Condenser", 4: "PodMic",
            5: "Broadcaster", 6: "Procaster", 7: "NT1", 8: "NT1-A", 9: "RE20", 10: "SM7B"}

# Every property the INPUTSOURCE node carries, as a record (the coverage reports read it). Only the
# ones with a named setter below are writable; give a property its own setter if it ever needs one.
CHANNEL_PROP = {
    "inputMicrophoneGain": {"kind": "int",  "note": "XLR preamp gain in dB (set_mic_gain)."},
    "inputPower":          {"kind": "int",  "note": "48V phantom, 1=on/0=off (set_phantom)."},
    "inputPhaseFlip":      {"kind": "bool", "note": "polarity/phase invert (set_polarity)."},
    "inputMicrophoneType": {"kind": "int",  "note": "mic model/voicing (set_mic_type; MIC_TYPE full 0..10)."},
    # channelWirelessMute does NOT belong here: it is per STRIP, in the CHANNEL block (see
    # WIRELESS_MUTE_PROP).
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
    # Restoring it has never been tried on hardware, so it is readable and backed up but not
    # restorable.
    "inputColour":         {"kind": "str",  "note": "channel strip colour, AARRGGBB hex string "
                                                   "(decoded). Not restored."},
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
    return _t.write_at(_mic_key(channel), "inputMicrophoneGain", "int", db)


def mic_gain(blob: bytes, channel: int):
    """A mic channel's CURRENT input gain in dB, or None, read at the SAME address set_mic_gain
    writes to.

    THE PAIR IS THE POINT. Anything that has to put a gain back must take it from the desk rather
    than from the desk model, and reading it at the write's own address is the only way to be
    certain it is the same value. `server.wake_channels` is the case that forces it: its callers
    are a reset and an import, the two moments the model is least to be trusted, so sourcing the
    "put it straight back" value from it writes the app's last seen gain over every
    freshly-restored one.
    """
    v = _r.keyed_value(blob, _mic_key(channel), "inputMicrophoneGain", None)
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def mic_gain_known(channel: int):
    """A mic channel's gain as the desk model last saw it, or None. For saying what changed, never
    for putting a gain back (`mic_gain`)."""
    v = desk_model.value(_mic_key(channel), "inputMicrophoneGain")
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def set_phantom(channel: int, on: bool) -> int:
    """Enable/disable 48V phantom power (P48) on a mic channel. Property 'inputPower'
    (int 1=on, 0=off). Sent in normal mode."""
    return _t.write_at(_mic_key(channel), "inputPower", "int", 1 if on else 0)


def set_polarity(channel: int, invert: bool) -> int:
    """Invert (flip) a mic channel's polarity/phase. Property 'inputPhaseFlip' (compact bool).
    On the INPUT node like gain/P48 - WRITE-VERIFIED via read-back. Sent in normal mode."""
    return _t.write_at(_mic_key(channel), "inputPhaseFlip", "bool", invert)


def set_mic_type(channel: int, type_value: int) -> int:
    """Set a mic channel's input type/voicing. See MIC_TYPE for confirmed names.

    THIS ALSO MOVES THE GAIN. The desk gives each voicing its own default preamp gain and applies it
    on selection, so a caller that has just set a gain has to write this FIRST or lose it (see
    profiles.apply, and server.action_set_mic_type which reads the gain back and says so).
    Measured on Mic 3:

        Dynamic 45, Condenser 25, PodMic 50, Broadcaster 27, Procaster 50,
        NT1 30, NT1-A 31, RE20 51, SM7B 55

    Line-in and Instrument are the two that leave the gain alone - they are not microphone voicings.
    Instrument instead sets inputInstrumentGain to 20 and leaves it there when you move off it."""
    return _t.write_at(_mic_key(channel), "inputMicrophoneType", "int", type_value)


def set_instrument_gain(channel: int, value: int) -> int:
    """Instrument-input gain on a mic/combo channel (property 'inputInstrumentGain')."""
    return _t.write_at(_mic_key(channel), "inputInstrumentGain", "int", int(value))


# ============================================================================
# PER-CHANNEL PROCESSING (EQ / comp / gate / de-esser / HPF / pan / VoxLab / Aphex)
# ----------------------------------------------------------------------------
# The RODE app drives the channel-strip DSP over report-3 as GLOBAL records: the property NAME selects
# the parameter and the SELECTOR selects the channel. Float params use value-type 0x04 (normalised 0..1).
#   01 01 01 01 <sel>  <propName> 00  01 09 04  <float64 LE>
# ============================================================================

# The names behind a 1-based channel NUMBER (1-4 = Mic 1-4, then the Pro II's factory order), so a
# caller may pass either. A number only names a channel here; its strip always comes from the live
# fader vector, never from this order.
PROC_CHANNEL_ORDER = ["Mic1", "Mic2", "Mic3", "Mic4", "Bluetooth", "SMARTpads",
                      "USB1", "Chat", "USB2", "CallMe1"]


def _ui_entry_for(dev, channel):
    """Map `channel` (a device ui-channel NAME, or a 1-based PROC_CHANNEL_ORDER number) to the
    active device's ui_channels entry, or None if the device does not carry it right now."""
    cmap = {c["name"]: c for c in dev.ui_channels_for(usb_expanded())}
    if isinstance(channel, str):
        return cmap.get(channel)
    if isinstance(channel, int) and 1 <= channel <= len(PROC_CHANNEL_ORDER):
        return cmap.get(PROC_CHANNEL_ORDER[channel - 1])
    return None


def usb_expanded() -> bool:
    """Whether the desk is in Expanded, as the desk model last saw it. It decides a NAME and which
    sources may be offered, never an address, and reads no dump: the announcer's thread and the
    fader label helper call it.

    False while the model has never seen the mode, which is a safe default only in the sense that it
    offers FEWER sources and the plainer name. It is NOT a harmless one: unknown, the fader dropdown
    omits Game, Music, A and B AND the write gate refuses them, so on a desk that is in Expanded the
    four cannot be assigned at all. The server reads the desk at start (`seed_usb_expanded`)
    rather than leaving it to whichever page the user happens to open."""
    return bool(recorder.split_usb_multitrack(desk_model.usb_mode())[1])


def usb_expanded_known() -> bool:
    """Has the desk model seen the USB mode yet? Lets the startup seed skip a dump it does not need."""
    return desk_model.usb_mode() is not None


# ---- the fader layout (the strip map) ---------------------------------------------------------
# Processing, effects, mutes and solos are addressed PER STRIP (fader position), so every such write
# has to know the live channelInputSource vector. The desk model holds it: the touchscreen pushes
# every change to it and our own writes reach the model as they are sent, so a full dump (0.44 s on
# a Pro II) is taken only while the model is not trusted. A fader moved in RODE's app pushes NO
# channelInputSource, so while that app is running (`rode_app`) every write reads the layout; the
# regenerated chain values the desk pushes for a strip whose source changed are a second sign,
# which `follow_desk` treats as a reason to read the layout again.

# How long after our own write of a chain value a push of it is taken for that write's echo. The
# desk echoes most writes, late enough during a slider drag that the echo of one step lands after
# the next step is written, so its value differs from the model's and would read as a change.
CHAIN_ECHO_S = 1.5
_chain_written: dict = {}     # (path, name) -> when this app last wrote it


def last_known_strip_vector():
    """The desk model's fader layout, however old, or None before the first read. For NAMING:
    a write is addressed through `strip_vector`, which reads the desk when the model is not
    trusted."""
    return desk_model.strip_vector()


def follow_desk() -> None:
    """Untrust the desk model's fader layout when the desk pushes a changed chain value that this
    app did not write: a fader moved in RODE's app pushes nothing else."""
    desk_live.Follower("fader moves", set(DERIVED_FLOAT_PROPS), on_push=note_chain_push,
                       own_writes=True).start()


def note_chain_push(push) -> None:
    """One pushed, or written, derived chain value."""
    key = (push.path, push.name)
    if push.ours:
        _chain_written[key] = _time.monotonic()
        return
    if push.key is None or push.key[0] != "CHANNEL" or push.was is None or push.was == push.value:
        return
    at = _chain_written.get(key)
    if at is not None and _time.monotonic() - at < CHAIN_ECHO_S:
        return
    desk_model.distrust()


def strip_vector() -> list:
    """The live channelInputSource vector (position = strip/fader, value = source code), for
    addressing a write: the desk model's while it is trusted and RODE's app is not open (its
    fader moves are not pushed), otherwise a fresh read, which the model adopts."""
    if desk_model.trusted() and not rode_app.is_open():
        vec = desk_model.strip_vector()
        if vec:
            return vec
    return list(_r.values(_r.read_model(fresh=True), "channelInputSource"))


def _strip_index(entry) -> int:
    """The device strip index for a ui-channel entry: its LIVE fader position,
    channelInputSource.index(source_code), exactly as the read path resolves it. Processing and
    effects are per fader STRIP (see devices.PRO2_UI_CHANNELS), so a channel moved to another fader
    is processed by the chain sitting on that fader."""
    src = entry.get("source_code")
    try:
        return strip_vector().index(src)
    except (ValueError, TypeError):
        raise RuntimeError(
            f"{entry.get('name')!r} is not assigned to a fader strip right now, so its "
            f"processing/effects cannot be addressed - assign it to a fader first.")


def _channel_strip(channel) -> int:
    """Resolve any channel identifier to its device strip index.

    A channel the connected desk does not carry REFUSES. There is no fixed fallback strip: Mic 3 in
    a Pro II profile restored onto a Duo would otherwise land on Duo strip 2, which holds Bluetooth."""
    dev = devices.get_active_device()
    entry = _ui_entry_for(dev, channel)
    if entry is None:
        raise ValueError(f"the {dev.name} has no channel {channel!r} right now")
    return _strip_index(entry)


def proc_key(channel) -> tuple:
    """Map a channel to its processing node, CHANNEL[strip index], DEVICE-AWARE.

    `channel` may be a device ui-channel name ("Mic1", "Bluetooth", "Headset", ...), a 1-based
    PROC_CHANNEL_ORDER index, or a 1-based mic number. The strip is the channel's LIVE fader
    position, matching the read path.
    """
    return _t.channel_key(_channel_strip(channel))


def fx_key(channel) -> tuple:
    """Effects node, EFFECTS_PARAMETERS[strip index], using the SAME strip resolution as proc_key
    so effects and processing always agree."""
    return _t.channel_fx_key(_channel_strip(channel))


# Whitelist of processing parameters we have decoded (all float64, normalised 0..1). Only names the RODE
# app itself sends are allowed, per the capture-replay safety rule.
PROCESSING_PROPS = frozenset({
    "eqHighGain", "eqHighBell", "eqMidGain", "eqMidBell", "eqLowGain", "eqLowShelf",
    # THE THREE Q VALUES are independent and writable: no other EQ write moves a Q, and a direct
    # write lands and reads back with no stray. The RODE app has no Q control.
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

    proc_key()/fx_key() resolve the strip from the LIVE fader position, matching the read path.
    The cap gates the whole write path so a desk whose processing scheme is not verified refuses
    rather than writing to a wrong node (the stray-append hazard)."""
    dev = devices.get_active_device()
    if "processing" not in dev.caps:
        raise RuntimeError(
            f"Channel processing and effects writes are not verified on the {dev.name}, so they "
            f"are disabled to avoid a wrong-selector write.")


def set_channel_processing(channel: int, prop: str, value: float) -> int:
    """Set one channel-processing parameter (EQ/comp/gate/deesser/HPF/pan/VoxLab/Aphex).

    channel: 1-based index into PROC_CHANNEL_ORDER or a channel name (1-4 = Mic 1-4; also
    Bluetooth/USB1/Chat/USB2/CallMe1). prop: a name in PRESET_FLOAT_PROPS. value: NORMALISED
    0.0..1.0 (the unit maps it to the real dB/Hz/ms). Each channel has its OWN node via
    proc_key; the property name picks the parameter."""
    _require_processing()
    if prop not in PRESET_FLOAT_PROPS:
        raise ValueError(f"unknown/unsupported processing property: {prop}")
    v = max(0.0, min(1.0, float(value)))
    return _t.write_at(proc_key(channel), prop, "float", v)


def apply_processing_preset(channel: int, props: dict, delay: float = 0.08) -> int:
    """Apply a full processing preset to any processing channel (see proc_key).

    `props` maps property name -> value: every float name is written as a normalised 0..1 float;
    channelAdvancedProcessing and each <module>On flag as a bool, channelCurrentFxPreset as an int.
    All land on the channel's OWN node, exactly as the RODE app does when you pick a preset.

    MODULES ARE PART OF A PRESET. Without them it is a silent no-op: applied to a strip whose
    modules are switched off it writes all 33 parameters, reports the preset name, and sounds
    exactly the same, because nothing in the chain is engaged. A preset carries its module set as
    DATA (see ProjectAssets/mic_presets.json and server._read_channel_preset_props, which records
    the live states on "save as"),
    rather than this function sweeping every module on, which would be a guess dressed as a preset.

    WHAT IT DELIBERATELY DOES NOT TOUCH: channelBypassProcessing, the channel's master on/off. That
    is the operator's own control, not part of a voicing: a mic left bypassed on purpose, and a
    preset that un-bypassed it mid-session would be a nasty surprise.

    ORDER MATTERS (see CHAIN_ADVANCED_PROP): the processing MODE first, so
    the strip is in the right mode before its values
    land and cannot re-derive them from the VoxLab knobs; then the modules, so a parameter never
    lands on a module that is about to be switched; then the floats, knobs first and derived values
    last (see VOXLAB_KNOB_PROPS); then the preset-name marker LAST, because the device blanks it to
    -1 the moment any parameter is written.
    Returns total bytes written."""
    _require_processing()
    key = proc_key(channel)
    mode, modules, marker, float_vals = [], [], [], {}
    for name, val in props.items():
        if name == CHAIN_PRESET_MARKER_PROP:
            marker.append(_t.build_at(key, name, "int", int(val)))
        elif name == CHAIN_ADVANCED_PROP:
            mode.append(_t.build_at(key, name, "bool", bool(val)))
        elif name in PROCESSING_MODULE_BOOLS:
            modules.append(_t.build_at(key, name, "bool", bool(val)))
        elif name in PRESET_FLOAT_PROPS:
            float_vals[name] = max(0.0, min(1.0, float(val)))
        else:
            raise ValueError(f"unsupported preset property: {name}")
    floats = [_t.build_at(key, name, "float", float_vals[name])
              for name in _float_write_order(list(float_vals))]
    return _t.send_records_paced(mode + modules + floats + marker, delay=delay)


# Per-channel PROCESSING module on/off toggles (compact BOOL on the channel's processing node).
# Decoded. The Aphex aural-exciter + Big Bottom share ONE enable (aphexOn).
PROCESSING_MODULE_ON = {
    "hpf": "hpfOn", "eq": "eqOn", "compressor": "compressorOn", "noiseGate": "noiseGateOn",
    "deesser": "deesserOn", "aphex": "aphexOn", "pan": "channelPanOn",
    "eqHigh": "eqHighOn", "eqMid": "eqMidOn", "eqLow": "eqLowOn",
}


# THE REST OF THE PER-STRIP CHAIN. The float params, the module bools and channelBypassProcessing
# are not the whole chain: diffing the RODE app's own fader drag shows two more properties
# travelling with a channel, and both matter:
#   channelAdvancedProcessing - simple (the three VoxLab knobs) vs advanced (the 8 modules). In SIMPLE
#     mode the desk DERIVES the rest of the chain from Depth/Sparkle/Punch, so a chain restored onto a
#     strip left in the wrong mode can be regenerated over the top.
#   channelCurrentFxPreset    - which preset NAME the strip displays (0/1/2 = Neutral/Podcast Studio/
#     Broadcast, -1 = custom). The device blanks it to -1 as soon as a parameter is written, so it must
#     be written LAST, and it is a NAME only: never infer the values from it.
CHAIN_ADVANCED_PROP = "channelAdvancedProcessing"
CHAIN_PRESET_MARKER_PROP = "channelCurrentFxPreset"


def set_channel_advanced(channel: int, on: bool) -> int:
    """Set a strip's processing MODE: True = advanced (modules + params), False = simple (the three
    VoxLab knobs). Compact bool on the channel's own processing node, i.e. the same record shape
    as any module toggle. Writing this flips the mode and changes NO
    parameter (the RODE app's "disabling Advanced will reset all parameters" warning is the APP
    clearing them itself, not the firmware)."""
    _require_processing()
    return _t.write_at(proc_key(channel), CHAIN_ADVANCED_PROP, "bool", bool(on))


def set_channel_preset_marker(channel: int, index: int) -> int:
    """Set which preset NAME a strip displays (channelCurrentFxPreset; -1 = custom). Write this LAST,
    after every parameter, or the device will blank it again."""
    _require_processing()
    return _t.write_at(proc_key(channel), CHAIN_PRESET_MARKER_PROP, "int", int(index))


def set_processing_module(channel: int, module: str, on: bool) -> int:
    """Enable/disable a per-channel PROCESSING module on any processing channel (mics + BT/USB1/
    Chat/USB2/CallMe1; see proc_key). `module` is a key in PROCESSING_MODULE_ON (hpf / eq /
    compressor / noiseGate / deesser / aphex / pan / eqHigh / eqMid / eqLow), or the raw <module>On
    property name. Compact bool on the channel's processing node."""
    _require_processing()
    key = proc_key(channel)
    prop = PROCESSING_MODULE_ON.get(module)
    if prop is None:
        if module in PROCESSING_MODULE_ON.values():
            prop = module
        else:
            raise ValueError(f"unknown processing module {module!r}; known: {list(PROCESSING_MODULE_ON)}")
    return _t.write_at(key, prop, "bool", on)


def set_processing_enabled(channel: int, on: bool) -> int:
    """Master per-channel PROCESSING on/off (the panel's 'Processing' toggle). The device property is
    INVERTED: channelBypassProcessing (compact bool) on the channel's processing node - True =
    bypassed/OFF. So processing ON => channelBypassProcessing False. Do NOT name it
    set_channel_processing: that collides with the float param setter."""
    _require_processing()
    return _t.write_at(proc_key(channel), "channelBypassProcessing", "bool", not on)


# CHANNEL MUTE. `channelOutputMute` is a compact bool on the channel's own processing node
# (CHANNEL[strip]), i.e. byte-for-byte the same record shape as any module toggle above.
# The firmware PROPAGATES it into the mix matrix (every mixMute cell for that source follows), so
# it is a REAL mute, not a stored flag, and clearing it puts everything back. It does NOT take the
# input-address format: written there it looks unwritable.
#
# Gated by the same "processing" cap as the module toggles, because it is the same node.
# The Duo behaves the same: a mute moves 14 properties, its own plus the 13 matrix cross-points
# for the source.
MUTE_PROP = "channelOutputMute"


def set_channel_mute(channel, on: bool) -> int:
    """Mute or unmute a channel (the unit's own mute button, from software).

    `channel` is anything proc_key accepts (1-4 = Mic 1-4, or a channel name), so the write
    follows the channel to whichever fader STRIP it is currently on, exactly as processing does.
    Muting here is the same mute as the desk's button: it silences that channel in every mix that
    follows the fader, not just the monitor. A cross-point unlinked at a manual level keeps playing."""
    _require_processing()
    return _t.write_at(proc_key(channel), MUTE_PROP, "bool", bool(on))


# CHANNEL SOLO / LISTEN. `channelCueEnable` is the desk's Solo (RODE call it "Listen"): it replaces
# the headphone feed with just the soloed channels. Same record shape and same node as the
# mute above - a compact bool on CHANNEL[strip]. The RODE app has no control for it, which
# means no capture route, never that it is unwritable.
#
# A write moves channelCueEnable for that strip and nothing else in the model, and appends no
# stray.
#
# UNLIKE the mute, the desk pushes NOTHING back when this is written from software: not the
# property, not a matrix propagation. A solo is a monitoring change, so there is no mix matrix for
# it to propagate into.
CUE_PROP = "channelCueEnable"


def set_channel_solo(channel, on: bool) -> int:
    """Solo ("Listen") a channel, or clear it: the unit's own Solo button, from software.

    `channel` is anything proc_key accepts, so the write follows the channel to whichever fader
    STRIP it is currently on. This only changes what you HEAR in the headphones - the mixes, the
    recording and anything being streamed are untouched - which is the opposite of the mute above and
    is worth saying plainly wherever it is offered."""
    _require_processing()
    return _t.write_at(proc_key(channel), CUE_PROP, "bool", bool(on))


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
    _require_processing()
    cleared = 0
    for strip, on in enumerate(read_solos(blob)):
        if on:
            _t.write_at(proc_key_for_strip(strip), CUE_PROP, "bool", False)
            cleared += 1
    return cleared


# WIRELESS MIC MUTE. `channelWirelessMute` mutes a RODE wireless transmitter paired to the channel,
# at the transmitter, which is a different thing from channelOutputMute above: that one silences the
# strip inside the desk, this one is the desk telling the mic itself to stop sending.
#
# THE ADDRESS IS THE SAME FAMILY, and that is measured, not assumed. Read off a live Duo: ten
# occurrences, CHANNEL[strip] for every strip, which is byte-for-byte the same set
# of nodes channelOutputMute and channelCueEnable sit on. Both desks publish ten. So it is a compact
# bool on proc_key(channel) like every other toggle here, and it needs no capture: a capture
# would only re-derive an address the tree already gives.
#
# IT DOES NOT BELONG IN CHANNEL_PROP, which lists the properties addressed at the INPUT node: a
# 30-entry table indexed by SOURCE CODE, a different block and a different index, where a write goes
# to an address the audio engine does not read.
#
# UNTESTED WITH A TRANSMITTER, so the reply says what was sent rather than what happened, and
# nothing here claims the mic went quiet.
WIRELESS_MUTE_PROP = "channelWirelessMute"


def set_channel_wireless_mute(channel, on: bool) -> int:
    """Mute or unmute a RODE wireless mic paired to a channel, from software.

    `channel` is anything proc_key accepts, so the write follows the channel to whichever fader
    STRIP it is currently on, exactly as the mute and the solo do."""
    _require_processing()
    return _t.write_at(proc_key(channel), WIRELESS_MUTE_PROP, "bool", bool(on))


def channel_strip_index(channel) -> int:
    """The 0-based fader STRIP a channel currently sits on (device-aware, same resolution as
    proc_key). Public because the server needs it to key the announcer's per-strip caches -
    mute state arrives from the desk keyed by strip, and our own writes have to agree."""
    return _channel_strip(channel)


def strip_on_fader(channel) -> int | None:
    """channel_strip_index, or None for a channel this desk carries but no fader holds. For a walk
    over every channel, where a channel taken off its fader is skipped rather than refused: it has
    no strip, so no mute, solo or processing to address."""
    try:
        return _channel_strip(channel)
    except RuntimeError:
        return None


# Per-channel "always on" EFFECTS (Echo + Reverb) live on a SEPARATE block from the processing
# modules: EFFECTS_PARAMETERS[STRIP index], resolved by fx_key exactly as processing is. Mic
# channels only in the app's UI. Confirmed strip-indexed on the Duo: Mic 2's echoOn on 0x27.
CHANNEL_FX_ON = {"echo": "echoOn", "reverb": "reverbOn"}


def set_channel_effect(channel: int, effect: str, on: bool) -> int:
    """Enable/disable a per-channel always-on EFFECT (echo / reverb), channel 1-4. Compact bool on the
    channel-FX node fx_key(channel) = EFFECTS_PARAMETERS[the channel's LIVE strip index]
    (device-aware), so a mic moved off its default fader still hits the right strip."""
    _require_processing()
    if not 1 <= channel <= 4:
        raise ValueError(f"channel must be 1-4, got {channel}")
    prop = CHANNEL_FX_ON.get(effect)
    if prop is None:
        raise ValueError(f"unknown channel effect {effect!r}; known: {list(CHANNEL_FX_ON)}")
    return _t.write_at(fx_key(channel), prop, "bool", on)


# The adjustable PARAMS for each channel effect: GLOBAL float64
# (normalised 0..1) on the same strip node as the on/off. Names match the pad-FX names
# (echoMix = Level, reverbModel = size/type, etc.).
CHANNEL_FX_PARAMS = {
    "echo": ["echoMix", "echoLowCut", "echoHighCut", "echoDelay", "echoDecay"],
    "reverb": ["reverbMix", "reverbModel", "reverbLowCut", "reverbHighCut"],
}
_CHANNEL_FX_PARAM_SET = {p for ps in CHANNEL_FX_PARAMS.values() for p in ps}


def set_channel_effect_param(channel: int, param: str, value01: float) -> int:
    """Set one channel-effect PARAM (echo*/reverb*) on a mic channel 1-4, value normalised 0..1.
    Float64 on the channel-FX node fx_key(channel) = EFFECTS_PARAMETERS[the channel's
    LIVE strip index (device-aware); byte-for-byte the app's own write."""
    _require_processing()
    if not 1 <= channel <= 4:
        raise ValueError(f"channel must be 1-4, got {channel}")
    if param not in _CHANNEL_FX_PARAM_SET:
        raise ValueError(f"unknown channel-effect param {param!r}")
    v = max(0.0, min(1.0, float(value01)))
    return _t.write_at(fx_key(channel), param, "float", v)


# Per-channel "Level" (dB) for the NON-MIC channels. The app's channel panel shows a -/+ Level for
# Bluetooth/USB1/Chat/USB2 (and Headset on the Duo); the device property is inputDigitalGain (signed
# int dB), NOT the CHANNEL processing node. It sits on the channel's INPUTSOURCE node beside mic
# gain and everything else input-shaped, so it is addressed by DeviceModel.input_key like the rest.


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
    return _t.write_at(dev.input_key(ch), "inputDigitalGain", "int", int(db))


# ============================================================================
# READS - per-channel state for the UI (one model dump covers everything)
# ----------------------------------------------------------------------------
# The chain arrays (10 occurrences on both desks) are indexed by STRIP. Input values are read at
# the channel's INPUTSOURCE node through input_value, never by position.
# ============================================================================

# Channel-processing PARAMETERS (all normalised 0..1 floats). Nth occurrence = Nth strip.
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
# Module enable bools, read per strip.
PROCESSING_MODULE_BOOLS = (
    "hpfOn", "eqOn", "compressorOn", "noiseGateOn", "deesserOn", "aphexOn", "channelPanOn",
    "eqHighOn", "eqMidOn", "eqLowOn",
)
# --- Active-device UI-channel helpers (used by the web server's Channels view) ------------------
# NEVER build one static {name: entry} map from the Pro II channels at import time: it breaks on
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

    ONE lookup, because there is one address: INPUTSOURCE[source_code] (DeviceModel.input_key).

    NO per-device "override" node is tried first. On a Duo that override is the dead MIX block, so
    after any write the app READS BACK ITS OWN INERT COPY and reports it as the desk's setting,
    which makes the gain display go stale the moment anybody touches the desk itself. Reading only
    the live address fixes the display for the same reason it fixes the write, and it leaves the
    stale copies where they are: harmless once nothing
    reads them, and not removable without a factory reset.

    Returns None when this desk does not publish that value for that channel (a bus has no input).
    """
    dev = dev or devices.get_active_device()
    return _r.keyed_value(blob, dev.input_key(ch), prop, None)


def read_mixer() -> dict:
    """Mic-focused live read: gain + phantom + polarity + type per mic, MIDI, fw.

    Device-aware: reads exactly the mics the CONNECTED device exposes (4 on the Pro II, 2 on the
    Duo), keyed by 1-based mic channel, each mic read from its own ADDRESS - so the Duo shows
    Mic 1/Mic 2, never mislabels its Headset slot as "Mic 3", and never reports one mic's gain as
    another's. Gated by the device's "channel_read" cap so a desk whose input layout is not
    verified refuses rather than guess."""
    dev = devices.get_active_device()
    if "channel_read" not in dev.caps:
        raise RuntimeError(f"Live mixer reads are not verified on the {dev.name}.")
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
# the physical XLR, and the app leaves them alone). The three eq*Q values are among the floats.
CHAIN_BOOL_PROPS = tuple(PROCESSING_MODULE_BOOLS) + ("channelBypassProcessing", CHAIN_ADVANCED_PROP)
CHAIN_FX_BOOL_PROPS = ("echoOn", "reverbOn")
# Floats: the processing params plus the echo/reverb params, all normalised 0..1.
CHAIN_FLOAT_PROPS = tuple(PRESET_FLOAT_PROPS)
CHAIN_FX_FLOAT_PROPS = tuple(_CHANNEL_FX_PARAM_SET)
# A float difference smaller than this is the device's own float32 rounding, not a real change, so we
# do not waste a write on it (the RODE app's own swap leaves differences this size behind).
CHAIN_FLOAT_EPS = 1e-6

# WRITE ORDER WITHIN THE FLOATS IS NOT ARBITRARY. In SIMPLE mode the desk DERIVES most of the chain
# from the three VoxLab knobs, so a derived value written BEFORE a knob is recomputed away (in dict
# order, a swap loses compressorThreshold, deesserThreshold, noiseGateThreshold and aphexAEMix).
# So: knobs FIRST, then everything neutral, then the derived family LAST, which makes the copied
# value win in either mode. This reproduces the source strip EXACTLY, where the RODE app's own swap
# regenerates the derived values and lands slightly off.
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


def proc_key_for_strip(strip_idx: int) -> tuple:
    """Processing node for a STRIP index (0-based), not a channel."""
    return _t.channel_key(strip_idx)


def fx_key_for_strip(strip_idx: int) -> tuple:
    """Effects node for a STRIP index (0-based), not a channel."""
    return _t.channel_fx_key(strip_idx)


def read_chains(blob: bytes | None = None, strips: int | None = None) -> list:
    """Every strip's full chain, from ONE model dump. Returns a list indexed by STRIP:
    {"bools": {...}, "floats": {...}, "fx_bools": {...}, "fx_floats": {...}, "marker": int|None}.

    Read-only. Values are exactly what the device reports, so two chains can be diffed directly.

    READ PER NODE, BY THE KEY ITS WRITE USES, never by position in the dump: the pads' effect
    objects are EFFECTS_PARAMETERS blocks too, and a strip's node can carry fewer properties than
    its neighbours, so the Nth occurrence of a name is not strip N's."""
    if blob is None:
        blob = _r.read_model_stable()
    if not blob:
        return []
    if strips is None:
        strips = _r.layout_of(blob).count("CHANNEL")

    def props(key):
        node = _r.keyed_node(blob, key)
        return {p: (v, k) for p, v, k in node.props} if node is not None else {}

    def val(found, prop, kind=None):
        v, k = found.get(prop, (None, None))
        return v if kind is None or k == kind else None

    out = []
    for i in range(strips):
        proc, fx = props(proc_key_for_strip(i)), props(fx_key_for_strip(i))
        out.append({
            "bools": {p: val(proc, p) for p in CHAIN_BOOL_PROPS},
            "floats": {p: val(proc, p, "float") for p in CHAIN_FLOAT_PROPS},
            "fx_bools": {p: val(fx, p) for p in CHAIN_FX_BOOL_PROPS},
            "fx_floats": {p: val(fx, p, "float") for p in CHAIN_FX_FLOAT_PROPS},
            "marker": val(proc, CHAIN_PRESET_MARKER_PROP, "int"),
        })
    return out


def fit_chain(chain: dict, strip_idx: int) -> dict:
    """`chain` with only the properties strip `strip_idx` carries, for writing there.

    THE STRIPS ARE NOT ALIKE. On a Duo the tenth strip's effects node carries echo and reverb on/off
    and none of their parameters, so a chain moved there must leave those out: the desk has nowhere
    to keep them, and a write of one would append a stray."""
    book = _r.layout()
    pkey, fkey = proc_key_for_strip(strip_idx), fx_key_for_strip(strip_idx)

    def keep(group, key):
        return {p: v for p, v in (chain.get(group) or {}).items() if book.has(key, p)}

    return {"bools": keep("bools", pkey), "floats": keep("floats", pkey),
            "fx_bools": keep("fx_bools", fkey), "fx_floats": keep("fx_floats", fkey),
            "marker": chain.get("marker") if book.has(pkey, CHAIN_PRESET_MARKER_PROP) else None}


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
    write). Only what the strip carries is written (`fit_chain`). Returns {"written": <count>,
    "props": [names]}. Caller must hold the HID lock."""
    _require_processing()
    ops = chain_diff(fit_chain(desired, strip_idx), current)
    if not ops:
        return {"written": 0, "props": []}
    pkey, fkey = proc_key_for_strip(strip_idx), fx_key_for_strip(strip_idx)
    recs = []
    for kind, prop, val in ops:
        if kind == "bool":
            recs.append(_t.build_at(pkey, prop, "bool", bool(val)))
        elif kind == "fx_bool":
            recs.append(_t.build_at(fkey, prop, "bool", bool(val)))
        elif kind == "float":
            recs.append(_t.build_at(pkey, prop, "float", max(0.0, min(1.0, float(val)))))
        elif kind == "fx_float":
            recs.append(_t.build_at(fkey, prop, "float", max(0.0, min(1.0, float(val)))))
        elif kind == "int":
            recs.append(_t.build_at(pkey, prop, "int", int(val)))
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
# Each linked pair's mic mutes as they were at the link, for the unlink to hand back.
_LINK_MUTES_FILE = "stereo_link_mutes.json"


def _parked_load(name: str) -> dict:
    """A whole parking store: {device hardware id: {code as str: record}}. {} if absent or corrupt
    (see `paths.read_json`), or if it cannot be opened: a lost record is a channel that comes back
    with the wrong voicing or mute, never a crash."""
    try:
        return _paths.read_json(_paths.data_path(name), {})
    except OSError as exc:
        diagnostics.event("chain.park_unreadable", str(exc), file=name)
        return {}


def _parked_save(name: str, store: dict) -> None:
    # Never fail a fader move because the parking file could not be written.
    with _contextlib.suppress(Exception):
        _paths.write_json(_paths.data_path(name), store, indent=None)


def _park(name: str, device_id: str, code: int, record: dict) -> None:
    store = _parked_load(name)
    store.setdefault(str(device_id or ""), {})[str(int(code))] = record
    _parked_save(name, store)


def _take(name: str, device_id: str, code: int) -> dict | None:
    store = _parked_load(name)
    per_device = store.get(str(device_id or ""))
    if not isinstance(per_device, dict):
        return None
    record = per_device.pop(str(int(code)), None)
    if record is not None:
        _parked_save(name, store)
    return record if isinstance(record, dict) else None


def park_chain(device_id: str, code: int, chain: dict) -> None:
    """Keep `code`'s chain while it has no strip. Keyed by DESK as well as channel, so two desks
    do not hand each other their voicings (same reasoning as _applied_preset_record in server)."""
    if chain:
        _park(_ORPHAN_FILE, device_id, code, chain)


def take_chain(device_id: str, code: int) -> dict | None:
    """Pop `code`'s parked chain, or None if we never saw it leave. Popping is the point: once it
    is back on a strip the desk owns it again, and a stale copy would later overwrite an edit."""
    return _take(_ORPHAN_FILE, device_id, code)


def park_link_mutes(device_id: str, pair_code: int, lower: bool, upper: bool, pair: bool) -> None:
    """Keep each mic's mute from just before `pair_code` was linked, and the pair's mute just
    after, until the unlink."""
    _park(_LINK_MUTES_FILE, device_id, pair_code,
          {"lower": bool(lower), "upper": bool(upper), "pair": bool(pair)})


def take_link_mutes(device_id: str, pair_code: int) -> dict | None:
    """Pop what `park_link_mutes` kept for `pair_code`, or None if the app did not make the link."""
    return _take(_LINK_MUTES_FILE, device_id, pair_code)


def read_channels(blob: bytes | None = None) -> list:
    """Read every UI channel's full state in one model dump, using the ACTIVE device's read map.

    Returns a list (device ui_channels order) of dicts: name, label, kind, plus muted, level_db,
    processing_on, modules{}, params{} for all; and for mics also phantom, polarity, mic_type,
    effects{}. Mirrors the RODE app's per-channel panel so the window can present one adaptive
    Channels view.

    INPUT values (mic gain / power / polarity / type; line inputDigitalGain) are read BY ADDRESS
    through `input_value`, never by position in a values() list. PROCESSING/EFFECTS arrays ARE
    positional, and legitimately so: they are per-STRIP and the strip index is resolved from the
    live fader vector, so the index means something: channelInputSource.index(source_code), and a
    source with no strip assigned reads its processing as None.

    Gated by the device's "channel_read" cap: a device whose per-channel read map is not verified
    refuses here rather than returning Pro II-shaped, mislabelled values."""
    dev = devices.get_active_device()
    if "channel_read" not in dev.caps:
        raise RuntimeError(f"Per-channel reads are not verified on the {dev.name}.")
    if blob is None:
        blob = _r.read_model_stable()

    def vec(prop, kind=None):
        return _r.values(blob, prop, kind)

    bypass = vec("channelBypassProcessing")
    # MUTE is live state rather than a stored setting, but it belongs on the channel panel: it is
    # the control you reach for most. Indexed by STRIP like the processing arrays.
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

    def at(lst, i):
        return lst[i] if (i is not None and 0 <= i < len(lst)) else None

    def proc_index(ch):
        try:
            return src_vec.index(ch.get("source_code"))
        except (ValueError, TypeError):
            return None   # source not assigned to any strip

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
            # True of Expanded's four until they are assigned.
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
            # carries no digital gain - and its chain does nothing to the audio, heard on both
            # desks. Left as None rather than omitted so every channel record has the same keys
            # and the UI can test one field.
            rec["level_db"] = None
        else:
            rec["level_db"] = inp(ch, "inputDigitalGain")
        out.append(rec)
    return out
