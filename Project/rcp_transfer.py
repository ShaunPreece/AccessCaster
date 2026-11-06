"""
AccessCaster - the write side of the desk's property channel (report id 3 on MI_09).

Every setting the desk holds is written as one 256-byte "report id 3" record on the MI_09 control
interface, each carrying one named property:

  byte 0      : report id 0x03
  bytes 1..4  : payload length (uint32 LE)
  payload     : <address> <name> 00 <value block>
  then zero padding to 256 bytes.

The address is a path through the model tree (`model_tree.encode_path`). The value block is
`01 <vlen> <vtype> <value>`: int 01 05 01 <i32 LE>, float 01 09 04 <f64 LE>, compact bool
01 01 <02 on / 03 off>, string 01 <vlen> 05 <ascii> 00.

This module holds the record builders, the setters for the global and output settings, the send
layer, and Transfer Mode: the unit mounts its pad storage as a FAT32 USB drive, so writing a pad
sound is an ordinary file copy and only the pad config goes over this channel.

WARNING: this module WRITES to the device. A wrong selector does not error, it appends a stray
property that only a factory reset removes, so only send records whose address is mapped.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import string
import subprocess
import time

import desk_drives  # which drive letter is the desk's own volume
import devices  # per-device matrix and fader shape
import diagnostics  # the app's own account of every record it sends
import model_tree  # the tree grammar, and with it the ONE address format
import rcp_read  # the attached desk's layout, which every write address is looked up in
import rcp_hid  # write-generation counter (invalidates cached model reads)
import write_guard
from rcp_hid import REPORT_BULK_OUT, find_control_interface

BULK_LEN = 256


def _frame(payload: bytes) -> bytes:
    """Wrap a built payload into the fixed 256-byte report-3 record: report-id byte, u32-LE payload
    length, the payload, then zero padding to BULK_LEN."""
    rec = bytes([REPORT_BULK_OUT]) + len(payload).to_bytes(4, "little") + payload
    if len(rec) > BULK_LEN:
        raise ValueError("record too long")
    return rec + bytes(BULK_LEN - len(rec))


# ---- THE ADDRESS, in one place ----------------------------------------------
#
# EVERY ADDRESS IS A PATH THROUGH THE MODEL TREE (`model_tree.encode_path`), and every path a write
# uses is LOOKED UP on the attached desk's own tree by a key that names the node, `("OUTPUT", 0)`,
# `("MIX", 13 * source + output)`. No position is written down here: a firmware that grows one block
# moves every block after it, and a write to a stale position lands on a different, valid node or
# appends a stray that only a factory reset clears. `address_of` refuses a key the desk does not
# have, and a property its node does not carry.
#
# A position INSIDE a block is still arithmetic the code owns, because it is the block's own shape:
# a cross-point is MIX copy `13 * source + output`, a fader's level is FADER copy `index`.

def address_of(key, prop: str | None = None) -> list:
    """THE WRITE ADDRESS: the tree path of the node `key` names, on the attached desk, checked to
    carry `prop`. Raises model_tree.AddressUnknown rather than guess."""
    return rcp_read.layout().path(key, prop)


def build_address(path, empty_zero: bool = False) -> bytes:
    """The address bytes for a tree path. The ONE place an address is formed."""
    return model_tree.encode_path(path, empty_zero)


def value_block(kind: str, value) -> bytes:
    """A property's value block: `01 <vlen> <type> <payload>`.

    int    01 05 01 <i32 LE, SIGNED>   the desk stores an empty fader as channelInputSource -1
    bool   01 01 <02 on / 03 off>      enabled is 0x02 and disabled 0x03, NOT 1 and 0
    float  01 09 04 <f64 LE>           every processing parameter, as the RODE app sends it
    str    01 <vlen> 05 <ascii> 00     null-terminated

    Encoded by `model_tree.encode_value`, the one value encoder; a write is limited to these four
    kinds, and a string to ASCII.
    """
    if kind not in ("int", "bool", "float", "str"):
        raise ValueError(f"no value block for kind {kind!r}")
    if kind == "str":
        str(value).encode("ascii")
    return model_tree.encode_value(value, kind)


def build_record(name: str, kind: str, value, path, empty_zero: bool = False) -> bytes:
    """THE RECORD PRIMITIVE: one property write at a tree path, framed for report 3."""
    return _frame(build_address(list(path), empty_zero) + name.encode("ascii") + b"\x00"
                  + value_block(kind, value))


def build_at(key, name: str, kind: str, value) -> bytes:
    """One property write on the node `key` names, addressed through the attached desk's tree."""
    return build_record(name, kind, value, address_of(key, name))


def write_at(key, name: str, kind: str, value) -> int:
    """Build and send one keyed property write. Returns bytes written."""
    return send_record(build_at(key, name, kind, value))


def build_path_prop_int(name: str, value: int, path) -> bytes:
    """An integer property at a tree path the caller read off a fresh dump."""
    return build_record(name, "int", value, path)


def build_path_prop_bool(name: str, on: bool, path) -> bytes:
    """A compact boolean property at a tree path the caller read off a fresh dump."""
    return build_record(name, "bool", on, path)


# Source CODES: the value a channelInputSource write carries, and the row of the routing matrix.
# Each desk's are `devices.DeviceModel.source_codes`, built from its channel table and its stereo
# pairs.
#
# MIC 4 IS 3, NOT 1027. 1027 is 0x403, Mic 4 with the STEREO-LINK BIT set, which is what a capture
# records when the desk that produced it happened to have inputs 3+4 linked. Reading 0x400 as part
# of the source code rather than as a link flag is also what hides stereo linking entirely.
#
# STEREO-LINKED PAIRS get their own codes, not a bit flag: 4/5/6 fill the exact gap between Mic 4
# and USB 1, one per legal pair.
# STEREO LINK. The legal pairs, per desk, are devices.FaderSpec.stereo_pairs. Only ADJACENT inputs
# can link (RODE's own restriction: 1+2, 2+3, 3+4).
#
# A link is ONE property write per fader, nothing more. The RODE app also sends inputMicrophoneType
# and inputColour on a nested path, but those are housekeeping: writing the pair code alone leaves
# the desk in a state byte-identical to the app's own.
#
# The LOWER mic's fader carries the pair; the other fader is vacated with -1. UNLINK IS ASYMMETRIC,
# and that is the desk's behaviour, not ours: it returns only the lower mic, leaving the upper one
# off-fader. The upper mic's processing survives that round trip only because channels.park_chain
# holds it (see faders.follow_chains).


# The single blocks the app writes to, as keys.
SYSTEM = ("SYSTEM", 0)           # transferModeType, systemMidiControl, haptics, REC button
NETWORK = ("NETWORK", 0)         # wifi and Bluetooth
OUTPUT = ("OUTPUT", 0)           # multitrack, auto-mutes, monitor level, listen mode
MASTER = ("MASTERCHANNEL", 0)    # the master Compellor and output delay
GUI = ("GUI", 0)                 # the display and button brightness, the selected pad bank
RECORDER = ("RECORDER", 0)
RECORDINGS = ("RECORDINGS", 0)
SHOWCONTROL = ("SHOWCONTROL", 0)
SOUNDPADS = ("SOUNDPADS", 0)
PADEFFECTS = ("PADEFFECTS", 0)


def channel_key(strip: int) -> tuple:
    """A processing strip: CHANNEL[strip]. A fader IS a strip, so a fader's channelInputSource and
    a strip's processing chain are on the same node."""
    return ("CHANNEL", int(strip))


def channel_fx_key(strip: int) -> tuple:
    """A strip's echo and reverb: the root's EFFECTS_PARAMETERS[strip]. The pads' effect objects
    are the same block name under PADEFFECTS, a different parent, so they are not counted."""
    return ("EFFECTS_PARAMETERS", int(strip))


def input_key(source_code: int) -> tuple:
    """A channel's INPUT values (gain, phantom, polarity, mic type): INPUTSOURCE[source code], read
    and written there on every desk. See devices.DeviceModel.input_key."""
    return ("INPUTSOURCE", int(source_code))


def output_key(output_index: int) -> tuple:
    """An output's routing mode: MIXMINUSES[output index], in CROSS_POINT_OUTPUT_INDEX order."""
    return ("MIXMINUSES", int(output_index))


def headphone_key(index: int) -> tuple:
    """A headphone output's amp profile: HEADPHONE[index], 0 = Headphone 1."""
    return ("HEADPHONE", int(index))


def fader_key(fader_index: int) -> tuple:
    """A fader's LEVEL: PHYSICALINTERFACE.FADER[index]. The FADER nodes follow four POT nodes,
    which is why the level's second path index reads as 0x04 + fader."""
    return ("PHYSICALINTERFACE", 0, "FADER", int(fader_index))


def set_midi_control(on: bool) -> int:
    """Enable/disable bi-directional MIDI control (the RODE app's Device Config > System > MIDI).
    A direct global property; no Transfer Mode needed."""
    return write_at(SYSTEM, "systemMidiControl", "bool", on)


# ----------------------------------------------------------------------------
# ROUTING / OUTPUT settings (see RODECASTER_PROTOCOL.txt section 5):
#   outputMixMinus     PER-OUTPUT, MIXMINUSES[output index] (CROSS_POINT_OUTPUT_INDEX order);
#                      int 0=Main Mix, 1=Mix-Minus, 2=Custom.
#   recordingMultitrackMode / outputMonAutoMute / outputBTAutoMute : the OUTPUT block.
#   headphoneType      per headphone output, HEADPHONE[index].
# ----------------------------------------------------------------------------

MULTITRACK_MODES = {0: "Off", 1: "Pre-Fader", 2: "Post-Fader"}
# recordingCompressionQuality. TWO members, and the enum is the whole range: see set_recording_format
# for why anything else must be refused rather than passed through.
RECORDING_FORMATS = {0: "WAV", 1: "MP3"}


def set_output_routing(output_index: int, mode: int) -> int:
    """outputMixMinus for one output: 0=Main Mix, 1=Mix-Minus, 2=Custom."""
    return write_at(output_key(output_index), "outputMixMinus", "int", mode)


# ----------------------------------------------------------------------------
# ROUTING MATRIX cross-points (an output's Custom mix). Full detail and the index tables are in
# RODECASTER_PROTOCOL.txt section 5.
#
# Each cross-point (output o, source s) is its own MIX node:
#       MIX[13*source_index + output_index]
# Output is the fast axis (stride 1); source is the slow axis (stride 13 = 13 destinations). The
# source index is the channelInputSource source CODE (DeviceModel.source_codes), not the fader order.
#
# Three property writes ride that node:
#   mixUnlinkRequest / mixLinkRequest : value = CONSTANT compound payload
#       01 07 08 01 01 02 01 01 02   (it is NOT the address - the node is).
#   mixDisabled : compact bool 0x02 = excluded, 0x03 = included.
#   mixLevelWithAnchor : ASCII '<level>|<anchor>', only effective on an UNLINKED cross-point.
# Setting the output back to Main Mix (set_output_routing(o, 0)) discards the whole custom matrix.
# ----------------------------------------------------------------------------

CROSS_POINT_OUTPUT_STRIDE = 1  # output is the fast axis
CROSS_POINT_SOURCE_STRIDE = 13 # source is the slow axis (13 destinations)

# Output index (the matrix column). 0-10 are confirmed on a Pro II. 11/12 are INFERRED to
# be the CallMe 2/3 outputs (the desk has 3 call slots; the app only shows CallMe 1 until more
# callers connect, so they were never captured), which is why `_check_cross_point` refuses them
# unless the caller opts in.
CROSS_POINT_OUTPUT_INDEX = {
    "Headphone1": 0, "Headphone2": 1, "Headphone3": 2, "Headphone4": 3,
    "Monitor": 4, "REC": 5, "Bluetooth": 6, "USB1": 7,
    "Chat": 8, "USB2": 9, "CallMe1": 10,
    "CallMe2": 11, "CallMe3": 12,                        # INFERRED (2nd/3rd phone callers)
}


def cross_point_key(output_index: int, source_index: int) -> tuple:
    """The (output, source) matrix cross-point: MIX[13*source + output].

    Its path can pass 255 (Virtual A, source 14, sits at 258 on the Pro II), and `build_address`
    encodes that as a two-byte index exactly as the desk does on the push side. WHICH cells the
    connected desk exposes is `_check_cross_point`'s job; the layout refuses a cell past the grid."""
    cell = CROSS_POINT_SOURCE_STRIDE * int(source_index) + CROSS_POINT_OUTPUT_STRIDE * int(output_index)
    if cell < 0:
        raise ValueError(f"cross-point cell {cell} is not a MIX node")
    return ("MIX", cell)


def cross_point_of(key) -> tuple | None:
    """(output index, source index) for a MIX key, or None for any other key. The reverse of
    `cross_point_key`, for naming a pushed cross-point."""
    if not key or len(key) != 2 or key[0] != "MIX":
        return None
    source, output = divmod(int(key[1]), CROSS_POINT_SOURCE_STRIDE)
    return output, source


def _check_cross_point(output_index: int, source_index: int, allow_inferred: bool) -> None:
    """Refuse any output or source index the CONNECTED device's MatrixSpec does not expose, so a Pro
    II-only cross-point (Mic 4 = source 3, Headphone 3/4 = outputs 2/3) is refused on a Duo rather
    than written to a slot that may be a live call's. The inferred CallMe 2/3 outputs (11/12) pass
    only with `allow_inferred`.

    THE EXPANDED FOUR COUNT AS EXPOSED IN BOTH MODES, because the grid carries all 30 source rows
    on the wire whichever mode the desk is in: rows 12 to 15 are ordinary MIX nodes that the read
    path already walks and gets values from. Whether to OFFER them is a UI question, answered by
    `MatrixSpec.sources_for`; this guard is about whether the node exists."""
    dev = devices.get_active_device()
    valid_outputs = {o["index"] for o in dev.matrix.outputs}
    valid_sources = set(dev.matrix.all_source_indices)
    if output_index not in valid_outputs:
        if not (allow_inferred and output_index in (11, 12)):
            raise ValueError(
                f"output_index {output_index} is not an exposed cross-point output on the "
                f"{dev.name} (exposed {sorted(valid_outputs)}; 11/12 = inferred CallMe2/3, "
                f"pass allow_inferred=True to use them).")
    if source_index not in valid_sources:
        raise ValueError(
            f"source_index {source_index} is not an exposed cross-point source on the "
            f"{dev.name} (exposed {sorted(valid_sources)}).")


# The link request's value, verbatim: a composite of two complete value blocks.
_MIX_LINK_REQUEST_VALUE = bytes([0x01, 0x07, 0x08, 0x01, 0x01, 0x02, 0x01, 0x01, 0x02])


def build_mix_link_request(path, link: bool) -> bytes:
    """mixLinkRequest (link=True) / mixUnlinkRequest (link=False) on a cross-point's path, with the
    constant request payload. Byte-for-byte what the app sends for a cross-point's chain icon."""
    name_b = (b"mixLinkRequest" if link else b"mixUnlinkRequest")
    return _frame(build_address(list(path)) + name_b + b"\x00" + _MIX_LINK_REQUEST_VALUE)


def build_cross_point_link(key, link: bool) -> bytes:
    """`build_mix_link_request` on the cross-point `key` names."""
    name = "mixLinkRequest" if link else "mixUnlinkRequest"
    return build_mix_link_request(address_of(key, name), link)


def set_cross_point_linked(output_index: int, source_index: int, linked: bool,
                           allow_inferred: bool = False) -> int:
    """Link one cross-point to its channel fader (True) or give it a level of its own (False).
    Idempotent on the desk, so it is safe to send without reading the current state first."""
    _check_cross_point(output_index, source_index, allow_inferred)
    return send_record(build_cross_point_link(cross_point_key(output_index, source_index),
                                              link=linked))


def set_cross_point_excluded(output_index: int, source_index: int, excluded: bool,
                             allow_inferred: bool = False) -> int:
    """Exclude (True) or include (False) one source in one output's Custom mix.

    The output must already be in Custom mode (set_output_routing(o, 2)). Sends the matching
    link/unlink request then mixDisabled, both on the cross-point selector. Returns total bytes
    written; read mixDisabled back to confirm."""
    _check_cross_point(output_index, source_index, allow_inferred)
    key = cross_point_key(output_index, source_index)
    n = send_record(build_cross_point_link(key, link=not excluded))
    n += write_at(key, "mixDisabled", "bool", excluded)
    return n


def _level_str(level: float) -> str:
    """Format a 0..1 cross-point level the way the app does: minimal decimal, no
    trailing zeros ('0.89', '0.9', '0', '1')."""
    level = max(0.0, min(1.0, float(level)))
    s = f"{level:.2f}".rstrip("0").rstrip(".")
    return s if s else "0"


def cross_point_level_value(level: float, anchor: int = 0) -> str:
    """mixLevelWithAnchor's value: the ASCII string '<level>|<anchor>'.

    level = normalised fader position 0.0 .. 1.0; anchor = 0 for an unlinked (absolute)
    cross-point."""
    return f"{_level_str(level)}|{int(anchor)}"


def set_cross_point_level(output_index: int, source_index: int, level: float,
                          allow_inferred: bool = False) -> int:
    """Set one cross-point's level (0..1). The output must be in Custom mode and the cross-point
    UNLINKED (a linked one follows the channel fader); set_cross_point_excluded(..., False) leaves
    it linked, so call set_cross_point_linked(..., False) first."""
    _check_cross_point(output_index, source_index, allow_inferred)
    return write_at(cross_point_key(output_index, source_index), "mixLevelWithAnchor", "str",
                    cross_point_level_value(level))


# --- FADER ASSIGNMENT (which channel sits on which fader) ---------------------
# The fader layout IS the channelInputSource vector: position = fader (0-based), value = the source
# CODE on that fader. The app reorders by writing channelInputSource on CHANNEL[fader index]; no
# systemChannelSelected pre-write is needed. The mic faders are writable too. See
# RODECASTER_PROTOCOL.txt section 6.


def set_fader_source(fader_index: int, source_code: int) -> int:
    """Put a source CODE (DeviceModel.source_codes) on a fader (0-based), or -1 to VACATE it.

    The bound is the connected device's fader VECTOR LENGTH (Pro II 9, Duo 10), NOT its exposed
    fader `count`. The two differ only on the Duo, which exposes 9 faders but whose factory default
    layout occupies all ten slots, so the fader reset and a profile restore both walk index 9.
    Writing slot 9 is safe: set, vacate and restore leave the model byte-clean."""
    count = devices.get_active_device().fader.vector_length
    if not 0 <= fader_index < count:
        raise ValueError(f"fader_index must be 0-{count - 1}, got {fader_index}")
    return write_at(channel_key(fader_index), "channelInputSource", "int", int(source_code))


def set_multitrack_mode(mode: int) -> int:
    """recordingMultitrackMode: 0=Off, 1=Pre-Fader, 2=Post-Fader."""
    return write_at(OUTPUT, "recordingMultitrackMode", "int", mode)


def set_recording_format(value: int) -> int:
    """recordingCompressionQuality: 0=WAV, 1=MP3. Same global OUTPUT block as multitrack.

    THE GUARD IS NOT DEFENSIVE PADDING. The desk stores anything you send here (2, 3, 99, -1 and
    255 all stick) and the recorder takes any non-zero as MP3, so an out-of-range write is silently
    accepted and leaves the desk on a value its own touchscreen cannot represent. Refuse instead.
    """
    if value not in RECORDING_FORMATS:
        raise ValueError(f"recording format must be 0 (WAV) or 1 (MP3), got {value}")
    return write_at(OUTPUT, "recordingCompressionQuality", "int", int(value))


def set_recording_processing_bypass(on: bool) -> int:
    """recordingProcessingBypass: record the SEPARATE tracks with no channel processing.

    It applies to MULTITRACK ONLY, and the mixed track always carries the processing (RODE's guide
    says both), so with multitrack Off this setting changes nothing you can hear."""
    return write_at(OUTPUT, "recordingProcessingBypass", "bool", on)


def set_usb_multitrack_mode(mode: int) -> int:
    """outputMultiMode: what the MAIN USB device sends this computer. 0=Off, 1=Pre-Fader,
    2=Post-Fader, the same three the recorder uses.

    **TAKES THE COMPOSED VALUE, 0-5.** The property also carries the USB 1 INPUT mode in the same
    int: +3 means Expanded. Compose with `recorder.join_usb_multitrack` rather than passing a bare
    output mode, or a desk in Expanded is silently dropped back to Standard and unassigns every
    extended input on a fader.

    **Switching this RE-ENUMERATES the desk's whole USB audio function**: all four endpoints drop
    for a second or two and Windows moves the default device. Every caller must say so before doing
    it, and anything that flips this to reach an endpoint must restore the previous mode afterwards.

    **IT RETURNS ONLY WHEN THE DESK IS BACK**, which is why this is not a bare send_record. The
    desk is off the bus about 60 ms after the write, so anything sent in the next couple of seconds
    is lost with "no control interface". See `rcp_hid.wait_for_reenumeration`; a write the desk
    ignores because it already holds that value costs a fraction of a second here and no more."""
    rec = build_at(OUTPUT, "outputMultiMode", "int", int(mode))
    before = find_control_interface()
    n = send_record(rec)
    rcp_hid.wait_for_reenumeration(before)
    return n


def set_usb_multitrack_bypass(on: bool) -> int:
    """outputMultiBypass: the USB stream's half of the processing bypass, exactly as
    `set_recording_processing_bypass` is the recorder's. Separate tracks only; the mix pair the
    desk sends on channels 1 and 2 keeps its processing either way."""
    return write_at(OUTPUT, "outputMultiBypass", "bool", on)


def set_monitor_auto_mute(on: bool) -> int:
    return write_at(OUTPUT, "outputMonAutoMute", "bool", on)


def set_bt_auto_mute(on: bool) -> int:
    return write_at(OUTPUT, "outputBTAutoMute", "bool", on)


def set_master_compellor(on: bool) -> int:
    """Toggle the master output Compellor (the output compressor / leveler). Speech that pumps up
    and down is the pad AUTODUCK, not this."""
    return write_at(MASTER, "masterCompellorOn", "bool", on)


# Master Compellor PARAMS - NORMALISED 0..1 floats on the MASTERCHANNEL block (the UI shows dB/ms but
# stores normalised; factory Threshold/Attack/Release/Gain = 0.67/0.50/0.38/0.22).
MASTER_COMPELLOR_PARAM = {
    "threshold": "masterCompellorThreshold", "attack": "masterCompellorAttack",
    "release": "masterCompellorRelease", "gain": "masterCompellorGain",
}


def set_master_compellor_param(param: str, value01: float) -> int:
    """Set a Master Compellor param (threshold / attack / release / gain) as a normalised 0..1
    float on the MASTERCHANNEL block."""
    prop = MASTER_COMPELLOR_PARAM.get(param)
    if prop is None:
        raise ValueError(f"unknown compellor param {param!r}; known: {list(MASTER_COMPELLOR_PARAM)}")
    return write_at(MASTER, prop, "float", max(0.0, min(1.0, float(value01))))


def set_output_delay(on: bool, seconds: float | None = None) -> int:
    """Output Delay (A/V sync for livestreaming): masterDelayOn, then masterDelaySeconds (float
    SECONDS, 0..2.0) if `seconds` is given, both on the MASTERCHANNEL block."""
    n = write_at(MASTER, "masterDelayOn", "bool", on)
    if seconds is not None:
        n += write_at(MASTER, "masterDelaySeconds", "float", max(0.0, min(2.0, float(seconds))))
    return n


# ----------------------------------------------------------------------------
# VIRTUAL FADER LEVEL + MONITOR LEVEL (RODECASTER_PROTOCOL.txt 6.1).
#
# faderLevel lives on PHYSICALINTERFACE.FADER[fader] (`fader_key`), a two-level path, and is
# written in the form the DEVICE ITSELF pushes when the encoder moves a fader, with the zero first
# index sent as a zero-length count:
#     01 01 02 00 01 <fader node>  faderLevel 00  01 05 01 <int32 LE>
# A one-level address at the same number is silently ignored. Only the VIRTUAL faders are ours to
# write (the physical ones are analog pots): devices.FaderSpec.level_faders lists them.
FADER_LEVEL_MAX = 127            # faderMax; faderLevel / 127 IS that source's cross-point level


def build_fader_level(path, value: int) -> bytes:
    """The faderLevel record at a fader's path. `value` is 0..FADER_LEVEL_MAX."""
    v = max(0, min(FADER_LEVEL_MAX, int(value)))
    return build_record("faderLevel", "int", v, path, empty_zero=True)


def set_fader_level(fader_index: int, value: int) -> int:
    """Set one VIRTUAL fader's level (0..127). `fader_index` is 0-based."""
    return send_record(build_fader_level(address_of(fader_key(fader_index), "faderLevel"), value))


def set_monitor_level(level01: float) -> int:
    """Set the monitor speaker level, 0.0..1.0 (outputMonLevel), the property the desk's own
    encoder writes."""
    v = max(0.0, min(1.0, float(level01)))
    return write_at(OUTPUT, "outputMonLevel", "float", v)


def set_headphone_type(value: int, output: int = 0) -> int:
    """headphoneType amp profile: 0=NTH-100, 1=High Sensitivity (louder), 2=Low Sensitivity.
    `output` picks the headphone output, 0 = Headphone 1; callers take it from the active device's
    HeadphoneTypeSpec, which lists only the confirmed ones."""
    return write_at(headphone_key(output), "headphoneType", "int", value)


# ----------------------------------------------------------------------------
# UNIT SETTINGS - the display, haptics, REC button, listen mode, clock, lockouts.
#
# A key addresses a BLOCK of settings and the property NAME picks the field inside it. Three
# blocks cover them all: GUI (the display), OUTPUT and SYSTEM. RODECASTER_PROTOCOL.txt section 11.1
# has the detail.
#
# Every entry is a confirmed write. Most are the RODE app's own; the DERIVED ones come from the block
# rule (DevTools/selector_map.py), and DevTools/probe_derived_write.py confirms a new one: write,
# read back and restore, with the model unchanged in length and occurrence counts. Several have no
# control in the RODE app at all, which never makes a setting unwritable.

# prop -> (block key, kind). `kind` picks the value block. Values are NOT range-checked here; the
# caller owns the value domain (it is UI copy as much as protocol) and this table is just the address.
UNIT_SETTING_SEL = {
    # OUTPUT block - captured
    "outputPrefader": (OUTPUT, "bool"),          # False = After-fader Listen
    "outputMonFixed": (OUTPUT, "bool"),          # Fixed Output Level
    # GUI block - captured, except activeButtonsBrightness (derived; the app's Buttons
    # slider writes only the inactive one)
    "metering": (GUI, "int"),                           # 0 Default / 1 Broadcast meter style
    "screenBrightness": (GUI, "int"),                   # 0..255
    "autoBrightness": (GUI, "bool"),
    "screenDimAfterSeconds": (GUI, "int"),              # seconds; 0 = never
    "inactiveButtonsBrightness": (GUI, "int"),          # 0..255
    "activeButtonsBrightness": (GUI, "int"),            # 0..255
    # SYSTEM block - captured
    "systemHapticSetting": (SYSTEM, "int"),             # 0 none / 1 on hold / 2 always
    "systemRecButtonSetting": (SYSTEM, "int"),          # 0 press to pause / 1 press to stop
    # ---- DERIVED from the block rule.
    # OUTPUT block
    "outputMonMute": (OUTPUT, "bool"),           # mute the monitor SPEAKERS
    "outputBTMute": (OUTPUT, "bool"),            # mute what a paired phone hears
    "outputBTLevel": (OUTPUT, "float"),          # 0..1, the Bluetooth OUTPUT level
    # SYSTEM block: HOW THE CLOCK IS SHOWN, not what time it is. The time itself is not
    # writable from here at all - the unit sets its own over the network. Daylight saving and the
    # timezone are stored and only applied by the touchscreen, so both are profile-only.
    "systemDateTime24h": (SYSTEM, "bool"),              # 24-hour clock vs 12-hour
    "systemDateTimeDaylightSavings": (SYSTEM, "bool"),
    "systemDateTimeOnHome": (SYSTEM, "bool"),           # show the clock on the home screen
    # IN THIS TABLE FOR PROFILE SAVE/RESTORE ONLY, and DELIBERATELY NOT ON THE DEVICE PAGE: it is
    # measured display-only, so offering it would be a control that lies. The reason sits next to
    # the decision, in device_page.UNIT_SETTINGS_NOT_ON_PAGE.
    "systemDateTimezone": (SYSTEM, "int"),              # an INDEX into the unit's own list
    # SYSTEM block: the lockouts. Each is a confirmed write, reversible from here by the same
    # write, but they are the entries most able to make a working desk feel
    # broken, so whatever offers them has to say what they do in plain words.
    "disableAllHeadphoneOutputs": (SYSTEM, "bool"),
    "disableAllLineoutOutputs": (SYSTEM, "bool"),
    "disableAllPhysicalButtons": (SYSTEM, "bool"),
}


def set_unit_setting(prop: str, value) -> int:
    """Write one UNIT SETTING (see UNIT_SETTING_SEL) as an ordinary global property.

    Refuses any property name not in the table rather than guessing a selector: a wrong
    selector-plus-property pairing does not simply miss, it can append a stray occurrence that only
    a factory reset clears."""
    entry = UNIT_SETTING_SEL.get(prop)
    if entry is None:
        raise ValueError(f"unknown desk setting {prop!r}; known: {sorted(UNIT_SETTING_SEL)}")
    key, kind = entry
    if kind == "bool":
        return write_at(key, prop, "bool", bool(value))
    if kind == "float":
        return write_at(key, prop, "float", max(0.0, min(1.0, float(value))))
    return write_at(key, prop, "int", int(value))


# ---- THE SEND LAYER --------------------------------------------------------

def _write_records(records, purpose: str, delay: float = 0.0) -> int:
    """Write `records` on ONE open connection to the control interface, `delay` seconds apart.
    Returns bytes written. Every write to the desk passes through here."""
    write_guard.refuse_in_tests("write to the device")
    iface = find_control_interface()
    if iface is None:
        raise RuntimeError("no control interface (is the desk connected? close the RODE app)")
    dev = rcp_hid.open_handle(iface, purpose)
    n = 0
    try:
        for r in records:
            diagnostics.command(r, purpose)
            n += dev.write(r)
            rcp_hid.note_written(r)
            if delay:
                time.sleep(delay)
    finally:
        rcp_hid.close_handle(dev, purpose)
        rcp_hid.bump_write_generation()   # device state changed: drop any cached model read
    return n


def send_record(rec: bytes) -> int:
    """Open the control interface, write one record, close. Returns bytes written."""
    return _write_records((rec,), "property write")


def send_records_paced(records: list[bytes], delay: float = 0.12) -> int:
    """Write all records on ONE open connection, pacing each by `delay` seconds.

    A pad create (select-begin, handle binding, then the config records) must stay on one
    continuous HID session: send_record opens and closes per record, which drops the edit-handle
    binding between records. The delay gives the device time to process each record instead of
    overrunning its queue."""
    return _write_records(records, "paced write", delay)


# ---- TRANSFER MODE ---------------------------------------------------------

def build_remount_pad_storage() -> bytes:
    """remountPadStorage = True on the SYSTEM block, which makes the unit (re)mount its pad volume.
    Byte-for-byte the RODE app's record: the zero after its value is padding, outside the length
    the record declares."""
    return build_at(SYSTEM, "remountPadStorage", "bool", True)


# A pad create enters and leaves transfer mode for itself, which is right for ONE pad and wasteful
# for sixteen: every cycle drops the desk's audio and pops a Windows device sound, and the cycling
# was measured at about 11 s of the 15 s a pad took in a profile restore.
#
# A HOLD IS A PROMISE THAT SOMEBODY ELSE OWNS THE SESSION. While held, set_transfer_mode is ignored,
# so the per-pad code keeps its own enter/exit calls and they simply stop taking effect. The holder
# enters once at the top and exits once on the way out, unconditionally, success or not.
_transfer_hold = 0
# Whether the desk was last told to enter transfer mode, for `transfer_active`.
_transfer_on = False


class transfer_session:
    """Hold ONE transfer-mode session across a batch of pad writes. Reentrant, so a nested caller
    that also asks for a session does not close the outer one."""

    def __enter__(self):
        global _transfer_hold
        if _transfer_hold == 0:
            set_transfer_mode(True)
        _transfer_hold += 1
        diagnostics.event("transfer.hold", "session held", depth=_transfer_hold)
        return self

    def __exit__(self, exc_type, exc, tb):
        global _transfer_hold
        _transfer_hold -= 1
        if _transfer_hold <= 0:
            _transfer_hold = 0
            try:
                _set_transfer_mode_raw(False)
            except Exception as e:
                # The body's own error is the one worth reporting; a failed exit must not hide it.
                diagnostics.event("transfer.exit_failed", str(e))
                if exc_type is None:
                    raise
        diagnostics.event("transfer.release", "session released", depth=_transfer_hold)
        return False


def _set_transfer_mode_raw(on: bool) -> int:
    """Enter/exit regardless of any hold. Only the session itself should use this."""
    global _session_pad_root, _transfer_on
    _transfer_on = on
    if not on:
        roots = ([_session_pad_root] if _session_pad_root
                 else sorted(desk_drives.on_desk(_drive_roots())))
        for root in roots:
            dismount_volume(root)
        _session_pad_root = None
    rec = build_at(SYSTEM, "transferModeType", "int", 2 if on else 0)
    # Logged as its own event as well as the record below it: entering transfer mode
    # re-presents a USB mass-storage gadget, so it is the one write whose
    # position in the timeline is worth being able to find without decoding anything.
    diagnostics.event("transfer.enter" if on else "transfer.exit")
    return send_record(rec)


def transfer_active() -> bool:
    """Whether the desk is in transfer mode: a pad's sound is being written, read or tidied."""
    return _transfer_on


def set_transfer_mode(on: bool) -> int:
    """Enter (transferModeType 2, NOT 1) or exit (0) Transfer Mode.

    WHILE A SESSION IS HELD (see transfer_session) this does nothing: the batch owns the session,
    and a per-pad exit in the middle of it would unmount the volume the next pad is about to write.
    """
    if _transfer_hold > 0:
        return 0
    return _set_transfer_mode_raw(on)


def remount_pad_storage() -> int:
    """Tell the unit to (re)mount its pad-storage FAT volume (makes the drive letter appear)."""
    return send_record(build_remount_pad_storage())


def flush_volume(root: str) -> None:
    """Flush Windows' write cache for the pad-storage volume to the device.

    os.sync() does NOT exist on Windows, so a copied file can sit in cache and never reach the unit
    before we exit transfer mode. This calls `FlushFileBuffers` on a volume handle, which is what
    `Write-VolumeCache` does underneath without the 1.2 to 2.3 s cost of starting PowerShell per pad.

    THE FAILURE MODE IS SILENT AND EXPENSIVE, which is why the PowerShell path is KEPT as a
    fallback: a flush that does nothing leaves a pad's audio in Windows' cache, the desk gets a
    truncated file, and nobody finds out until the pad plays. DevTools/test_flush_verify_live.py
    proves the fast path by md5 of the file read back off the volume.
    """
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ_WRITE = 0x00000001 | 0x00000002
    OPEN_EXISTING = 3
    INVALID_HANDLE = ctypes.c_void_p(-1).value
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.FlushFileBuffers.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        device = "\\\\.\\" + root[0] + ":"      # the volume's DOS device path, \\.\X:
        handle = k32.CreateFileW(device, GENERIC_WRITE, FILE_SHARE_READ_WRITE,
                                 None, OPEN_EXISTING, 0, None)
        if handle not in (0, INVALID_HANDLE, None):
            try:
                if k32.FlushFileBuffers(handle):
                    return
            finally:
                k32.CloseHandle(handle)
    except Exception as exc:
        diagnostics.event("transfer.flush_fallback", str(exc))   # the slow but proven path follows
    _flush_volume_powershell(root)


def dismount_volume(root: str) -> bool:
    """Flush, lock and dismount the pad volume at `root`, so Windows lets go of it before the desk
    takes the media away. True when Windows dismounted it.

    LEAVING TRANSFER MODE PULLS THE MEDIA WITHOUT WARNING. Without a dismount first, Windows keeps
    its mount of the volume, cached FAT and free-space bitmap included, across the desk's own
    changes to the disk, and marks the volume dirty, which is the "There's a problem with this
    drive" notification on the next mount. A refused lock (another process has a file open) still
    dismounts: those handles are invalid either way once the media goes.
    """
    GENERIC_READ_WRITE = 0x80000000 | 0x40000000
    FILE_SHARE_READ_WRITE = 0x00000001 | 0x00000002
    OPEN_EXISTING = 3
    FSCTL_LOCK_VOLUME = 0x00090018
    FSCTL_DISMOUNT_VOLUME = 0x00090020
    INVALID_HANDLE = ctypes.c_void_p(-1).value
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.DeviceIoControl.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                                        ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
                                        ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        k32.FlushFileBuffers.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        with quiet_drive_errors():
            handle = k32.CreateFileW("\\\\.\\" + root[0] + ":", GENERIC_READ_WRITE,
                                     FILE_SHARE_READ_WRITE, None, OPEN_EXISTING, 0, None)
            if handle in (0, INVALID_HANDLE, None):
                return False         # no volume mounted on that letter: nothing to let go of
            try:
                returned = ctypes.c_uint32(0)

                def ioctl(code: int) -> bool:
                    return bool(k32.DeviceIoControl(handle, code, None, 0, None, 0,
                                                    ctypes.byref(returned), None))

                k32.FlushFileBuffers(handle)
                locked = False
                for _ in range(5):
                    if ioctl(FSCTL_LOCK_VOLUME):
                        locked = True
                        break
                    time.sleep(0.1)
                dismounted = ioctl(FSCTL_DISMOUNT_VOLUME)
            finally:
                k32.CloseHandle(handle)    # closing the handle also releases the lock
        diagnostics.event("transfer.dismount", root, locked=locked, dismounted=dismounted)
        return dismounted
    except Exception as exc:
        diagnostics.event("transfer.dismount_failed", f"{root}: {exc}")
        return False


def _flush_volume_powershell(root: str) -> None:
    """Flush through `Write-VolumeCache`: correct, and about two seconds per call."""
    # CREATE_NO_WINDOW: the app runs under pythonw (no console), so without this each call would
    # flash a PowerShell window that steals focus.
    no_window = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", f"Write-VolumeCache {root[0]}"],
        check=False,
        capture_output=True,
        creationflags=no_window,
    )


class quiet_drive_errors:
    """Stop Windows throwing a modal "There is a problem with this drive" box at the user while we
    probe or read the pad volume.

    A half-mounted removable volume makes the OS raise a HARD ERROR dialog on behalf of whichever
    process touched it, and our recovery for that state is simply to wait and retry - so the dialog
    reports a condition we are already handling, and it lands on the user as a modal they have to
    find and dismiss with a screen reader. SEM_FAILCRITICALERRORS turns the dialog back into the error code
    the caller already catches. Thread-scoped (SetThreadErrorMode, not SetErrorMode) so a background
    pad worker cannot suppress dialogs for the rest of the process. No-op off Windows.
    """

    _SEM_FAILCRITICALERRORS = 0x0001

    def __enter__(self):
        self._old = None
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            old = ctypes.c_uint(0)
            if k32.SetThreadErrorMode(self._SEM_FAILCRITICALERRORS, ctypes.byref(old)):
                self._old = old.value
                self._k32 = k32
        except Exception:
            pass          # not Windows, or the call is unavailable: the dialogs may appear
        return self

    def __exit__(self, *exc):
        if self._old is not None:
            try:
                self._k32.SetThreadErrorMode(self._old, ctypes.byref(ctypes.c_uint(0)))
            except Exception:
                pass      # must not replace the error the block itself is raising
        return False


def pad_volume_readable(root: str) -> bool:
    """Can we actually READ BYTES off this pad volume right now?

    The readiness test has to do the same KIND of I/O the caller is about to do, or it passes on a
    volume the first real read then fails on. Listing a directory is not enough: it can be served
    from the mount's metadata while the media is still not ready.
    """
    base = os.path.join(root, "pads")
    try:
        with quiet_drive_errors():
            for name in sorted(os.listdir(base)):
                pad_dir = os.path.join(base, name)
                if not os.path.isdir(pad_dir):
                    continue
                for fn in sorted(os.listdir(pad_dir)):
                    path = os.path.join(pad_dir, fn)
                    if os.path.isfile(path):
                        with open(path, "rb") as fh:
                            fh.read(4096)
                        return True
            return True       # a grid with no audio at all: nothing to read, and that is not an error
    except OSError:
        return False


def _drive_roots() -> list[str]:
    """Every drive root from D to Z. A and B are floppy letters and C is the system drive."""
    return [f"{letter}:\\" for letter in string.ascii_uppercase[3:]]


def _is_pad_volume(root: str) -> bool:
    """Does `root` hold a pad folder that can be read right now?

    THREE checks, because each weaker one has been passed by a volume that then failed:
      * isdir(<root>\\pads) - passed by a size-0 PHANTOM letter left after a stuck transfer, which
        fails every write with EINVAL;
      * shutil.disk_usage(root).total > 0 - passed by the first entry after the desk has been idle,
        which then fails the first file read with EINVAL;
      * pad_volume_readable(root) - reads bytes, which is what the caller does next.
    """
    if not os.path.isdir(os.path.join(root, "pads")):
        return False
    try:
        with quiet_drive_errors():
            ready = shutil.disk_usage(root).total > 0
        return ready and pad_volume_readable(root)
    except OSError:
        return False


# The root found during a held transfer_session. Every find in the session returns this one, so the
# batch cannot move to another volume part way through.
_session_pad_root: str | None = None


def find_pad_volume(timeout_s: float = 30.0, reissue_remount_every: float = 6.0):
    """Wait for the pad-storage FAT volume to mount; return its root path (e.g. 'F:\\'), or None
    if it never appears within timeout_s.

    Only a drive on the desk's own USB device can be the pad volume (`desk_drives.on_desk`), so a
    USB stick holding a copy of the pad folder is never taken for it: an upload would go to the
    stick and the tidy would delete its files. The desk's letter stays assigned out of transfer
    mode and after a stuck one, as an empty or not-yet-ready drive, which `_is_pad_volume` rejects
    until the volume reads bytes. The re-arm can be slow (over 8 s after a fresh enter), so the
    timeout is generous and remountPadStorage is re-issued periodically in case the first one was
    swallowed.
    """
    global _session_pad_root
    deadline = time.monotonic() + timeout_s
    next_remount = time.monotonic() + reissue_remount_every
    while time.monotonic() < deadline:
        if _transfer_hold > 0 and _session_pad_root:
            candidates = [_session_pad_root]
        else:
            candidates = sorted(desk_drives.on_desk(_drive_roots()))
        for root in candidates:
            if _is_pad_volume(root):
                if _transfer_hold > 0:
                    _session_pad_root = root
                return root
        if time.monotonic() >= next_remount:
            try:
                remount_pad_storage()
            except Exception as exc:
                diagnostics.event("transfer.remount_failed", str(exc))
            next_remount = time.monotonic() + reissue_remount_every
        time.sleep(0.5)
    return None


def _selftest() -> None:
    """Prove the record primitive reproduces the RODE app's captured bytes, no hardware. Each path
    is the one in the capture; that the KEYS resolve to these paths on a real tree is
    DevTools/test_addressing.py's job."""
    pad = 0x29A
    cases = [
        # padFilePath assignment (pad 2 -> sound.wav), and its clear (empty value).
        ("033f000000010102029a02010170616446696c6550617468000129052f4170706c"
         "69636174696f6e2f656d6d632d646174612f706164732f322f736f756e642e7761"
         "7600",
         build_record("padFilePath", "str", "/Application/emmc-data/pads/2/sound.wav", [pad, 1])),
        ("0318000000010102029a02010170616446696c65506174680001020500",
         build_record("padFilePath", "str", "", [pad, 1])),
        # padIdx = 15 under stream position 0x17.
        ("0316000000010102029a020117706164496478000105010f000000",
         build_record("padIdx", "int", 15, [pad, 0x17])),
        # systemMidiControl on and off.
        ("031a000000010101010f73797374656d4d696469436f6e74726f6c00010102",
         build_record("systemMidiControl", "bool", True, [0x0F])),
        ("031a000000010101010f73797374656d4d696469436f6e74726f6c00010103",
         build_record("systemMidiControl", "bool", False, [0x0F])),
        # inputMicrophoneGain Mic 1 = 56 dB and Mic 2 = 51 dB, and Mic 1 phantom on: INPUTSOURCE,
        # whose two-byte index is the "selector plus trailer" of the old reading.
        ("0321000000010101026e02696e7075744d6963726f70686f6e654761696e0001050138000000",
         build_record("inputMicrophoneGain", "int", 56, [0x26E])),
        ("0321000000010101026f02696e7075744d6963726f70686f6e654761696e0001050133000000",
         build_record("inputMicrophoneGain", "int", 51, [0x26F])),
        ("0318000000010101026e02696e707574506f7765720001050101000000",
         build_record("inputPower", "int", 1, [0x26E])),
        # Signed int: inputDigitalGain -11 dB (captured from the RODE app on a Duo, at 0x14F).
        ("031e000000010101024f01696e7075744469676974616c4761696e00010501f5ffffff",
         build_record("inputDigitalGain", "int", -11, [0x14F])),
        ("0318000000010101024801696e7075745068617365466c697000010102",
         build_record("inputPhaseFlip", "bool", True, [0x148])),
        # Processing on CHANNEL[0], effects on EFFECTS_PARAMETERS[1].
        ("0320000000010101011c6368616e6e656c42797061737350726f63657373696e6700010102",
         build_record("channelBypassProcessing", "bool", True, [0x1C])),
        ("0323000000010101011c6368616e6e656c43757272656e7446785072657365740001050101000000",
         build_record("channelCurrentFxPreset", "int", 1, [0x1C])),
        ("030f000000010101012765" "63686f4f6e00010102",
         build_record("echoOn", "bool", True, [0x27])),
        # OUTPUT, MASTERCHANNEL and HEADPHONE[1].
        ("0324000000010101010d7265636f7264696e674d756c7469747261636b4d6f646500010501" "01000000",
         build_record("recordingMultitrackMode", "int", 1, [0x0D])),
        ("0319000000010101010d6f757470757442544175746f4d75746500010102",
         build_record("outputBTAutoMute", "bool", True, [0x0D])),
        ("031a000000010101010e6d6173746572436f6d70656c6c6f724f6e00010103",
         build_record("masterCompellorOn", "bool", False, [0x0E])),
        ("031a000000010101010a6865616470686f6e65547970650001050101000000",
         build_record("headphoneType", "int", 1, [0x0A])),
        # A cross-point index passes 255 and must WIDEN, not wrap: source 13 output 0 is 245 and
        # stays `01 f5`, Virtual A (258) and Virtual B (271) take two bytes. A mask would pass the
        # first and silently redirect the others to a node 256 lower.
        ("0314000000010101" "01f5" "6d697844697361626c6564" "00010102",
         build_record("mixDisabled", "bool", True, [245])),
        ("0315000000010101" "020201" "6d697844697361626c6564" "00010102",
         build_record("mixDisabled", "bool", True, [258])),
        ("0322000000010101" "020f01" "6d69784c6576656c57697468416e63686f72" "00010705302e357c3000",
         build_record("mixLevelWithAnchor", "str", cross_point_level_value(0.5), [271])),
        # faderLevel on fader 7, in the form the desk pushes: the zero first index has no bytes.
        ("031800000001010200010a6661646572" "4c6576656c0001050140000000",
         build_fader_level([0, 0x0A], 64)),
        # remountPadStorage, as the RODE app sends it.
        ("031a000000010101010f72656d6f756e7450616453746f726167650001010200",
         build_record("remountPadStorage", "bool", True, [0x0F])),
    ]
    for want, got in cases:
        want = bytes.fromhex(want).ljust(BULK_LEN, b"\x00")
        assert got == want, f"record mismatch:\n  expected {want[:48].hex()}\n  got      {got[:48].hex()}"


_USAGE = "usage: rcp_transfer.py enter | exit | remount | selftest | midi on|off"


if __name__ == "__main__":
    import sys
    # No default command: a bare run must not enter transfer mode.
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg in ("enter", "on"):
        print("transferModeType=2 (enter) ...")
        print("  wrote", set_transfer_mode(True))
        time.sleep(1.0)
        print("remountPadStorage ...")
        print("  wrote", remount_pad_storage())
    elif arg in ("exit", "off"):
        print("transferModeType=0 (exit) ...")
        print("  wrote", set_transfer_mode(False))
    elif arg == "remount":
        print("remountPadStorage ...")
        print("  wrote", remount_pad_storage())
    elif arg == "selftest":
        _selftest()
        print("selftest OK: builders match captured app bytes")
    elif arg == "midi":
        on = (len(sys.argv) > 2 and sys.argv[2].lower() in ("on", "1", "true", "enable"))
        print(f"systemMidiControl = {'ON' if on else 'OFF'} ...")
        print("  wrote", set_midi_control(on))
    else:
        print(_USAGE)
        sys.exit(2)
