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
import struct
import subprocess
import time

import model_tree                    # the tree grammar, and with it the ONE address format
import devices                       # per-device matrix and fader shape
import diagnostics                   # the app's own account of every record it sends
import rcp_hid                       # write-generation counter (invalidates cached model reads)
from rcp_hid import REPORT_BULK_OUT, find_control_interface

BULK_LEN = 256

SEL_TRANSFER_MODE = 0x0F


def _frame(payload: bytes) -> bytes:
    """Wrap a built payload into the fixed 256-byte report-3 record: report-id byte, u32-LE payload
    length, the payload, then zero padding to BULK_LEN."""
    rec = bytes([REPORT_BULK_OUT]) + len(payload).to_bytes(4, "little") + payload
    if len(rec) > BULK_LEN:
        raise ValueError("record too long")
    return rec + bytes(BULK_LEN - len(rec))


# ---- THE ADDRESS, in one place ----------------------------------------------
#
# EVERY ADDRESS THIS FILE BUILDS IS A PATH THROUGH THE MODEL TREE, so do not hand-write a shape as
# its own special case. Against the tree grammar they are all one format (`model_tree.encode_path`):
#
#   global      01 01 01 01 <sel>          -> path [sel]
#   "channel"   01 01 01 02 <lo> <hi>      -> path [lo | hi<<8]
#   pad         01 01 02 02 9a 02 01 <n>   -> path [SOUNDPADS, n]
#
# A GLOBAL SELECTOR IS A CHILD INDEX, NOT A BYTE, so the builders pass it to `build_address` whole.
# `encode_path` emits an index in the fewest bytes that hold it, so 0x0F stays `01 0f` and 258
# becomes `02 02 01`, which is how the desk itself addresses a cross-point above source 13. Masking
# to 0xFF would truncate silently, and a truncated selector is a write to a DIFFERENT, valid node.
# The pad builders keep their mask because a pad index cannot reach 256.
SOUNDPADS_INDEX = 0x29A          # the SOUNDPADS node's position among the root's children


def build_address(path) -> bytes:
    """The address bytes for a tree path. The ONE place an address is formed."""
    return model_tree.encode_path(path)


def build_global_prop_int(name: str, selector: int, value: int) -> bytes:
    """Build a 256-byte report-id-3 record setting a global integer property.

    The value is SIGNED int32: the desk stores an empty fader as channelInputSource -1
    (0xFFFFFFFF) and `model_tree` decodes ints signed, so the write side matches."""
    value_block = bytes([0x01, 0x05, 0x01]) + int(value).to_bytes(4, "little", signed=True)
    return _frame(build_address([selector]) + name.encode("ascii") + b"\x00" + value_block)


# Source CODES: the value a channelInputSource write carries, and the row of the routing matrix.
#
# MIC 4 IS 3, NOT 1027. 1027 is 0x403, Mic 4 with the STEREO-LINK BIT set, which is what a capture
# records when the desk that produced it happened to have inputs 3+4 linked. Reading 0x400 as part
# of the source code rather than as a link flag is also what hides stereo linking entirely.
#
# STEREO-LINKED PAIRS get their own codes, not a bit flag: 4/5/6 fill the exact gap between Mic 4
# and USB 1, one per legal pair. Proven on hardware for all three.
INPUT_SOURCE_CODE = {
    "Mic1": 0, "Mic2": 1, "Mic3": 2, "Mic4": 3,
    "Mic1+2": 4, "Mic2+3": 5, "Mic3+4": 6,
    "Bluetooth": 10, "SMARTpads": 11, "USB1": 7, "Chat": 8, "USB2": 9,
    "CallMe1": 16,
}
# The three legal stereo-link pairs: {pair code: (lower mic code, upper mic code)}. Only ADJACENT
# inputs can link (RODE's own restriction: 1+2, 2+3, 3+4).
#
# A link is ONE property write per fader, nothing more. The RODE app also sends inputMicrophoneType
# and inputColour on a nested path, but those are housekeeping: writing the pair code alone leaves
# the desk in a state byte-identical to the app's own (verified by model diff).
#
# The LOWER mic's fader carries the pair; the other fader is vacated with -1. UNLINK IS ASYMMETRIC,
# and that is the desk's behaviour, not ours: it returns only the lower mic, leaving the upper one
# off-fader. The upper mic's processing survives that round trip only because channels.park_chain
# holds it (see server._follow_chains).
STEREO_LINK_PAIRS = {4: (0, 1), 5: (1, 2), 6: (2, 3)}


# ---- PAD-ADDRESSED RECORDS ---------------------------------------------------
# `selector` is the pad's stream position under SOUNDPADS, and it is NOT a stable pad address: the
# desk reassigns it whenever the set of sound pads changes. The stable identity of a physical pad
# is padIdx (grid position - 1) and its folder on the FAT volume (pads/<grid position>/). A config
# write binds the selector to the target pad by sending padIdx under it first.

def build_pad_prop_string(name: str, value: str, selector: int = 0x01) -> bytes:
    """Build a 256-byte report-id-3 record setting a pad-addressed string property.

    The default selector 0x01 keeps the byte-for-byte selftest against sound4.pcap valid."""
    value_b = value.encode("ascii") + b"\x00"   # the value is null-terminated
    value_block = bytes([0x01, 1 + len(value_b), 0x05]) + value_b
    addr = build_address([SOUNDPADS_INDEX, selector & 0xFF])
    return _frame(addr + name.encode("ascii") + b"\x00" + value_block)


def build_pad_prop_int(name: str, value: int, selector: int = 0x01) -> bytes:
    """Build a 256-byte report-id-3 record setting a pad-addressed integer property (padIdx,
    which binds `selector` to a pad, is the main user). Verified byte-for-byte against
    fx_assign.pcap (padIdx=15 under selector 0x17)."""
    value_block = bytes([0x01, 0x05, 0x01]) + int(value).to_bytes(4, "little")
    addr = build_address([SOUNDPADS_INDEX, selector & 0xFF])
    return _frame(addr + name.encode("ascii") + b"\x00" + value_block)


def build_global_prop_string(name: str, value: str, selector: int) -> bytes:
    """Build a 256-byte report-id-3 record setting a global STRING property (wifiSSID / wifiPSK on
    NETWORK 0x10, the CallMe and Icecast strings).

    It joins two separately verified halves: the global address of build_global_prop_int and the
    string value block of build_pad_prop_string (sound4.pcap frame 167). The combination is proven
    per property, so read back the first write on any new one."""
    value_b = value.encode("ascii") + b"\x00"   # the value is null-terminated
    value_block = bytes([0x01, 1 + len(value_b), 0x05]) + value_b
    return _frame(build_address([selector]) + name.encode("ascii") + b"\x00" + value_block)


SEL_SYSTEM = 0x0F  # SYSTEM block: transferModeType, systemMidiControl, haptics, REC button
SEL_NETWORK = 0x10 # NETWORK block: wifi and Bluetooth (proven on hardware)


def build_global_prop_bool(name: str, on: bool, selector: int = SEL_SYSTEM) -> bytes:
    """Build a global report-3 record for a compact boolean property: 01 01 <02 on / 03 off>.

    Enabled is 0x02 and disabled is 0x03, NOT 1/0. Verified byte-for-byte against the
    systemMidiControl ON/OFF captures."""
    value_block = bytes([0x01, 0x01, 0x02 if on else 0x03])
    return _frame(build_address([selector]) + name.encode("ascii") + b"\x00" + value_block)


def build_global_prop_float(name: str, selector: int, value: float) -> bytes:
    """Build a global report-3 record carrying a 64-bit float property: 01 09 04 <f64 LE>.
    Byte-for-byte the format the RODE app uses for every channel-processing parameter."""
    value_block = bytes([0x01, 0x09, 0x04]) + struct.pack("<d", float(value))
    return _frame(build_address([selector]) + name.encode("ascii") + b"\x00" + value_block)


# Mic-input addressing lives in devices.DeviceModel.input_path: it is derived from the channel's
# source code, on every desk, and there is no selector table to keep anywhere.


# The "trailer" after a channel selector is NOT a trailer and NOT a per-device constant. The address
# is length-prefixed and LITTLE-ENDIAN, so `02 6e 02` is "2 bytes, 0x026E", INPUTSOURCE[0] on the Pro
# II; this byte is the HIGH BYTE. The Duo's is 0x01 because its INPUTSOURCE sits at 0x148. The names
# stay because the byte-for-byte selftests use them, but do not reason from the word "trailer".
CHANNEL_ADDR_TRAILER_PRO2 = 0x02
CHANNEL_ADDR_TRAILER_DUO = 0x01


def build_path_prop_int(name: str, value: int, path) -> bytes:
    """A report-3 integer property at a TREE PATH. Value block 01 05 01 <4-byte LE, signed>.

    The path form is the primitive and the selector/trailer pair below is a view onto it. Anything
    addressing a node it resolved through the tree should call this and skip the split."""
    value_block = bytes([0x01, 0x05, 0x01]) + int(value).to_bytes(4, "little", signed=True)
    return _frame(build_address(list(path)) + name.encode("ascii") + b"\x00" + value_block)


def build_path_prop_bool(name: str, on: bool, path) -> bytes:
    """A report-3 compact-boolean property at a TREE PATH. Value block 01 01 <0x02 on / 0x03 off>."""
    value_block = bytes([0x01, 0x01, 0x02 if on else 0x03])
    return _frame(build_address(list(path)) + name.encode("ascii") + b"\x00" + value_block)


def build_channel_prop_int(name: str, value: int, chan_sel: int,
                           trailer: int = CHANNEL_ADDR_TRAILER_PRO2) -> bytes:
    """A channel-addressed integer property: address 01 01 01 02 <chan_sel> <trailer>.

    Pro II verified byte-for-byte against inputMicrophoneGain (Mic 1 0x6e, Mic 2 0x6f) and
    inputPower. The Duo selectors in the selftest (0x48, 0x4f, trailer 0x01) match the RODE app's
    capture exactly, and the Duo's audio engine ignores that node: see
    devices.DeviceModel.input_path. Nothing in the app addresses a Duo input this way."""
    return build_path_prop_int(name, value, [(chan_sel & 0xFF) | ((trailer & 0xFF) << 8)])


def build_channel_prop_bool(name: str, on: bool, chan_sel: int,
                            trailer: int = CHANNEL_ADDR_TRAILER_PRO2) -> bytes:
    """A channel-addressed compact-boolean property: address 01 01 01 02 <chan_sel> <trailer>."""
    return build_path_prop_bool(name, on, [(chan_sel & 0xFF) | ((trailer & 0xFF) << 8)])


def set_midi_control(on: bool) -> int:
    """Enable/disable bi-directional MIDI control (the RODE app's Device Config > System > MIDI).
    A direct global property; no Transfer Mode needed."""
    return send_record(build_global_prop_bool("systemMidiControl", on))


# ----------------------------------------------------------------------------
# ROUTING / OUTPUT settings (see RODECASTER_PROTOCOL.txt section 5):
#   outputMixMinus     PER-OUTPUT, selector 0x31 + output index (CROSS_POINT_OUTPUT_INDEX order);
#                      int 0=Main Mix, 1=Mix-Minus, 2=Custom.
#   recordingMultitrackMode / outputMonAutoMute / outputBTAutoMute : OUTPUT block, selector 0x0d.
#   headphoneType      per headphone output, selector 0x09 for Headphone 1.
# ----------------------------------------------------------------------------

SEL_OUTPUT_GLOBAL = 0x0D            # OUTPUT block (multitrack, auto-mutes, monitor level)
SEL_OUTPUT_MIXMINUS_BASE = 0x31     # outputMixMinus selector = base + output index
SEL_HEADPHONE_TYPE = 0x09           # headphoneType Headphone 1 (default)
MULTITRACK_MODES = {0: "Off", 1: "Pre-Fader", 2: "Post-Fader"}
# recordingCompressionQuality. TWO members, and the enum is the whole range: see set_recording_format
# for why anything else must be refused rather than passed through.
RECORDING_FORMATS = {0: "WAV", 1: "MP3"}


def set_output_routing(output_index: int, mode: int) -> int:
    """outputMixMinus for one output: 0=Main Mix, 1=Mix-Minus, 2=Custom."""
    return send_record(build_global_prop_int(
        "outputMixMinus", SEL_OUTPUT_MIXMINUS_BASE + output_index, mode))


# ----------------------------------------------------------------------------
# ROUTING MATRIX cross-points (an output's Custom mix). Full detail and the index tables are in
# RODECASTER_PROTOCOL.txt section 5.
#
# Each cross-point (output o, source s) has its own global selector:
#       selector = CROSS_POINT_BASE + 13*source_index + output_index
# Output is the fast axis (stride 1); source is the slow axis (stride 13 = 13 destinations). The
# source index is the channelInputSource source CODE (INPUT_SOURCE_CODE), not the fader order.
#
# Three property writes ride that selector:
#   mixUnlinkRequest / mixLinkRequest : value = CONSTANT compound payload
#       01 07 08 01 01 02 01 01 02   (it is NOT the address - the selector is).
#   mixDisabled : compact bool 0x02 = excluded, 0x03 = included.
#   mixLevelWithAnchor : ASCII '<level>|<anchor>', only effective on an UNLINKED cross-point.
# Setting the output back to Main Mix (set_output_routing(o, 0)) discards the whole custom matrix.
# ----------------------------------------------------------------------------

CROSS_POINT_BASE = 76          # 0x4c
CROSS_POINT_OUTPUT_STRIDE = 1  # output is the fast axis
CROSS_POINT_SOURCE_STRIDE = 13 # source is the slow axis (13 destinations)

# Output index (the matrix column). 0-10 are confirmed by capture on a Pro II. 11/12 are INFERRED to
# be the CallMe 2/3 outputs (the desk has 3 call slots; the app only shows CallMe 1 until more
# callers connect, so they were never captured), which is why `_check_cross_point` refuses them
# unless the caller opts in.
CROSS_POINT_OUTPUT_INDEX = {
    "Headphone1": 0, "Headphone2": 1, "Headphone3": 2, "Headphone4": 3,
    "Monitor": 4, "REC": 5, "Bluetooth": 6, "USB1": 7,
    "Chat": 8, "USB2": 9, "CallMe1": 10,
    "CallMe2": 11, "CallMe3": 12,                        # INFERRED (2nd/3rd phone callers)
}


def cross_point_selector(output_index: int, source_index: int) -> int:
    """Selector for the (output, source) matrix cross-point.

    IT IS AN INDEX, NOT A BYTE: Virtual A (source 14) lands at 258 and Virtual B at 271, and
    `build_address` encodes those as a two-byte index exactly as the desk does on the push side.
    The ceiling here is the address format's own (an index wider than two bytes is not a MIX node,
    it is a bad sum); WHICH indices the connected desk exposes is `_check_cross_point`'s job."""
    sel = (CROSS_POINT_BASE
           + CROSS_POINT_SOURCE_STRIDE * source_index
           + CROSS_POINT_OUTPUT_STRIDE * output_index)
    if not 0 <= sel <= 0xFFFF:
        raise ValueError(f"cross-point selector {sel} is not a possible node index")
    return sel


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


def build_mix_link_request(selector: int, link: bool) -> bytes:
    """mixLinkRequest (link=True) / mixUnlinkRequest (link=False) on a cross-point, with the
    constant request payload. Byte-for-byte what the app sends for a cross-point's chain icon."""
    name_b = (b"mixLinkRequest" if link else b"mixUnlinkRequest")
    value_block = bytes([0x01, 0x07, 0x08, 0x01, 0x01, 0x02, 0x01, 0x01, 0x02])
    return _frame(build_address([selector]) + name_b + b"\x00" + value_block)


def set_cross_point_linked(output_index: int, source_index: int, linked: bool,
                           allow_inferred: bool = False) -> int:
    """Link one cross-point to its channel fader (True) or give it a level of its own (False).
    Idempotent on the desk, so it is safe to send without reading the current state first."""
    _check_cross_point(output_index, source_index, allow_inferred)
    return send_record(build_mix_link_request(cross_point_selector(output_index, source_index),
                                              link=linked))


def set_cross_point_excluded(output_index: int, source_index: int, excluded: bool,
                             allow_inferred: bool = False) -> int:
    """Exclude (True) or include (False) one source in one output's Custom mix.

    The output must already be in Custom mode (set_output_routing(o, 2)). Sends the matching
    link/unlink request then mixDisabled, both on the cross-point selector. Returns total bytes
    written; read mixDisabled back to confirm."""
    _check_cross_point(output_index, source_index, allow_inferred)
    sel = cross_point_selector(output_index, source_index)
    n = send_record(build_mix_link_request(sel, link=not excluded))
    n += send_record(build_global_prop_bool("mixDisabled", excluded, sel))
    return n


def _level_str(level: float) -> str:
    """Format a 0..1 cross-point level the way the app does: minimal decimal, no
    trailing zeros ('0.89', '0.9', '0', '1')."""
    level = max(0.0, min(1.0, float(level)))
    s = f"{level:.2f}".rstrip("0").rstrip(".")
    return s if s else "0"


def build_cross_point_level(selector: int, level: float, anchor: int = 0) -> bytes:
    """mixLevelWithAnchor on a cross-point: the ASCII string '<level>|<anchor>'.

    level = normalised fader position 0.0 .. 1.0; anchor = 0 for an unlinked (absolute)
    cross-point. Decoded from a fader-drag capture (captures/matrix_level.pcap)."""
    s = f"{_level_str(level)}|{int(anchor)}".encode("ascii")
    value_block = bytes([0x01, len(s) + 2, 0x05]) + s + b"\x00"
    return _frame(build_address([selector]) + b"mixLevelWithAnchor" + b"\x00" + value_block)


def set_cross_point_level(output_index: int, source_index: int, level: float,
                          allow_inferred: bool = False) -> int:
    """Set one cross-point's level (0..1). The output must be in Custom mode and the cross-point
    UNLINKED (a linked one follows the channel fader); set_cross_point_excluded(..., False) leaves
    it linked, so call set_cross_point_linked(..., False) first."""
    _check_cross_point(output_index, source_index, allow_inferred)
    sel = cross_point_selector(output_index, source_index)
    return send_record(build_cross_point_level(sel, level))


# --- FADER ASSIGNMENT (which channel sits on which fader) ---------------------
# The fader layout IS the channelInputSource vector: position = fader (0-based), value = the source
# CODE on that fader. The app reorders by writing channelInputSource on a per-FADER selector,
# 0x1c + fader index; no systemChannelSelected pre-write is needed. The mic faders are writable
# too. See RODECASTER_PROTOCOL.txt section 6.
FADER_SELECTOR_BASE = 0x1C   # channelInputSource selector for fader 1; + index for the rest
FADER_COUNT = 9              # Pro II fallback when no device is known; see set_fader_source


def set_fader_source(fader_index: int, source_code: int) -> int:
    """Put a source CODE (INPUT_SOURCE_CODE) on a fader (0-based), or -1 to VACATE it.

    The bound is the connected device's fader VECTOR LENGTH (Pro II 9, Duo 10), NOT its exposed
    fader `count`. The two differ only on the Duo, which exposes 9 faders but whose factory default
    layout occupies all ten slots, so the fader reset and a profile restore both walk index 9.
    Writing slot 9 is proven safe on hardware (set, vacate and restore, byte-clean)."""
    try:
        count = devices.get_active_device().fader.vector_length
    except Exception:
        count = FADER_COUNT
    if not 0 <= fader_index < count:
        raise ValueError(f"fader_index must be 0-{count - 1}, got {fader_index}")
    return send_record(build_global_prop_int(
        "channelInputSource", FADER_SELECTOR_BASE + fader_index, int(source_code)))


def set_multitrack_mode(mode: int) -> int:
    """recordingMultitrackMode: 0=Off, 1=Pre-Fader, 2=Post-Fader."""
    return send_record(build_global_prop_int(
        "recordingMultitrackMode", SEL_OUTPUT_GLOBAL, mode))


def set_recording_format(value: int) -> int:
    """recordingCompressionQuality: 0=WAV, 1=MP3. Same global OUTPUT block as multitrack.

    THE GUARD IS NOT DEFENSIVE PADDING. The desk stores anything you send here (2, 3, 99, -1 and
    255 all stick) and the recorder takes any non-zero as MP3, so an out-of-range write is silently
    accepted and leaves the desk on a value its own touchscreen cannot represent. Refuse instead.
    """
    if value not in RECORDING_FORMATS:
        raise ValueError(f"recording format must be 0 (WAV) or 1 (MP3), got {value}")
    return send_record(build_global_prop_int(
        "recordingCompressionQuality", SEL_OUTPUT_GLOBAL, int(value)))


def set_recording_processing_bypass(on: bool) -> int:
    """recordingProcessingBypass: record the SEPARATE tracks with no channel processing.

    It applies to MULTITRACK ONLY, and the mixed track always carries the processing (RODE's guide
    says both), so with multitrack Off this setting changes nothing you can hear."""
    return send_record(build_global_prop_bool("recordingProcessingBypass", on, SEL_OUTPUT_GLOBAL))


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
    before = find_control_interface()
    n = send_record(build_global_prop_int("outputMultiMode", SEL_OUTPUT_GLOBAL, int(mode)))
    rcp_hid.wait_for_reenumeration(before)
    return n


def set_usb_multitrack_bypass(on: bool) -> int:
    """outputMultiBypass: the USB stream's half of the processing bypass, exactly as
    `set_recording_processing_bypass` is the recorder's. Separate tracks only; the mix pair the
    desk sends on channels 1 and 2 keeps its processing either way."""
    return send_record(build_global_prop_bool("outputMultiBypass", on, SEL_OUTPUT_GLOBAL))


def set_monitor_auto_mute(on: bool) -> int:
    return send_record(build_global_prop_bool("outputMonAutoMute", on, SEL_OUTPUT_GLOBAL))


def set_bt_auto_mute(on: bool) -> int:
    return send_record(build_global_prop_bool("outputBTAutoMute", on, SEL_OUTPUT_GLOBAL))


SEL_MASTER_PROC = 0x0E              # master output-processing block (the Compellor, output delay)


def set_master_compellor(on: bool) -> int:
    """Toggle the master output Compellor (the output compressor / leveler). Speech that pumps up
    and down is the pad AUTODUCK, not this."""
    return send_record(build_global_prop_bool("masterCompellorOn", on, SEL_MASTER_PROC))


# Master Compellor PARAMS - NORMALISED 0..1 floats on the master block 0x0E (the UI shows dB/ms but
# stores normalised; factory Threshold/Attack/Release/Gain = 0.67/0.50/0.38/0.22).
MASTER_COMPELLOR_PARAM = {
    "threshold": "masterCompellorThreshold", "attack": "masterCompellorAttack",
    "release": "masterCompellorRelease", "gain": "masterCompellorGain",
}


def set_master_compellor_param(param: str, value01: float) -> int:
    """Set a Master Compellor param (threshold / attack / release / gain) as a normalised 0..1
    float on the master block 0x0E."""
    prop = MASTER_COMPELLOR_PARAM.get(param)
    if prop is None:
        raise ValueError(f"unknown compellor param {param!r}; known: {list(MASTER_COMPELLOR_PARAM)}")
    return send_record(build_global_prop_float(prop, SEL_MASTER_PROC, max(0.0, min(1.0, float(value01)))))


def set_output_delay(on: bool, seconds: float | None = None) -> int:
    """Output Delay (A/V sync for livestreaming): masterDelayOn, then masterDelaySeconds (float
    SECONDS, 0..2.0) if `seconds` is given, both on the master block 0x0E."""
    n = send_record(build_global_prop_bool("masterDelayOn", on, SEL_MASTER_PROC))
    if seconds is not None:
        n += send_record(build_global_prop_float("masterDelaySeconds", SEL_MASTER_PROC,
                                                 max(0.0, min(2.0, float(seconds)))))
    return n


# ----------------------------------------------------------------------------
# VIRTUAL FADER LEVEL + MONITOR LEVEL (RODECASTER_PROTOCOL.txt 6.1).
#
# faderLevel needs an ADDRESS LAYOUT OF ITS OWN. The global `01 01 01 01 <sel>` and the
# channel-address `01 01 01 02 <sel> <trailer>` are both silently ignored. What works mirrors the
# record the DEVICE ITSELF pushes when the encoder moves a fader:
#     01 01 02 00 01 <selector>  faderLevel 00  01 05 01 <int32 LE>
# Selectors are 0x04 + fader index, and only the VIRTUAL faders are ours to write (the physical ones
# are analog pots). They live per-device in devices.FaderSpec.level_selectors, all observed on the
# wire; server.py resolves them, so no caller passes a guessed selector here.
FADER_LEVEL_MAX = 127            # faderMax; faderLevel / 127 IS that source's cross-point level
SEL_MONITOR_LEVEL = 0x0D         # outputMonLevel, an ordinary global float on the OUTPUT block


def build_fader_level(selector: int, value: int) -> bytes:
    """Build the faderLevel record for one fader. `value` is 0..FADER_LEVEL_MAX."""
    v = max(0, min(FADER_LEVEL_MAX, int(value)))
    value_block = bytes([0x01, 0x05, 0x01]) + v.to_bytes(4, "little", signed=True)
    payload = (bytes([0x01, 0x01, 0x02, 0x00, 0x01, selector & 0xFF])
               + b"faderLevel" + b"\x00" + value_block)
    return _frame(payload)


def set_fader_level(selector: int, value: int) -> int:
    """Set one VIRTUAL fader's level (0..127) on an OBSERVED selector (see build_fader_level)."""
    return send_record(build_fader_level(selector, value))


def set_monitor_level(level01: float) -> int:
    """Set the monitor speaker level, 0.0..1.0 (outputMonLevel), the property the desk's own
    encoder writes."""
    v = max(0.0, min(1.0, float(level01)))
    return send_record(build_global_prop_float("outputMonLevel", SEL_MONITOR_LEVEL, v))


def set_headphone_type(value: int, selector: int = SEL_HEADPHONE_TYPE) -> int:
    """headphoneType amp profile: 0=NTH-100, 1=High Sensitivity (louder), 2=Low Sensitivity.
    `selector` picks the headphone output: 0x09=Headphone 1 (default), 0x0A=Headphone 2. The server
    resolves the selector from the active device's HeadphoneTypeSpec, so callers never pass an
    unmapped selector here."""
    return send_record(build_global_prop_int("headphoneType", selector, value))


# ----------------------------------------------------------------------------
# UNIT SETTINGS - the display, haptics, REC button, listen mode, clock, lockouts.
#
# A selector addresses a BLOCK of settings and the property NAME picks the field inside it. Three
# blocks cover them all: 0x07 GUI (the display), 0x0D OUTPUT and 0x0F SYSTEM. RODECASTER_PROTOCOL.txt
# section 11.1 has the detail.
#
# Every entry was written from our code and read back on hardware. The captured ones come from the
# RODE app; the DERIVED ones come from the block rule (DevTools/selector_map.py) and were proven by
# writing, reading back and restoring with the model unchanged in length and occurrence counts
# (DevTools/probe_derived_write.py). Several have no control in the RODE app at all: "no app
# control" means no capture route, not unwritable.
SEL_GUI = 0x07                   # GUI block: the display and button brightness settings

# prop -> (selector, kind). `kind` picks the record builder. Values are NOT range-checked here; the
# server owns the value domain (it is UI copy as much as protocol) and this table is just the address.
UNIT_SETTING_SEL = {
    # OUTPUT block (0x0D) - captured
    "outputPrefader": (SEL_OUTPUT_GLOBAL, "bool"),          # False = After-fader Listen
    "outputMonFixed": (SEL_OUTPUT_GLOBAL, "bool"),          # Fixed Output Level
    # GUI block (0x07) - captured, except activeButtonsBrightness (derived; the app's Buttons
    # slider writes only the inactive one)
    "metering": (SEL_GUI, "int"),                           # 0 Default / 1 Broadcast meter style
    "screenBrightness": (SEL_GUI, "int"),                   # 0..255
    "autoBrightness": (SEL_GUI, "bool"),
    "screenDimAfterSeconds": (SEL_GUI, "int"),              # seconds; 0 = never
    "inactiveButtonsBrightness": (SEL_GUI, "int"),          # 0..255
    "activeButtonsBrightness": (SEL_GUI, "int"),            # 0..255
    # SYSTEM block (0x0F) - captured
    "systemHapticSetting": (SEL_SYSTEM, "int"),             # 0 none / 1 on hold / 2 always
    "systemRecButtonSetting": (SEL_SYSTEM, "int"),          # 0 press to pause / 1 press to stop
    # ---- DERIVED from the block rule, each proven by write, read-back and restore.
    # OUTPUT block (0x0D)
    "outputMonMute": (SEL_OUTPUT_GLOBAL, "bool"),           # mute the monitor SPEAKERS
    "outputBTMute": (SEL_OUTPUT_GLOBAL, "bool"),            # mute what a paired phone hears
    "outputBTLevel": (SEL_OUTPUT_GLOBAL, "float"),          # 0..1, the Bluetooth OUTPUT level
    # SYSTEM block (0x0F): HOW THE CLOCK IS SHOWN, not what time it is. The time itself is not
    # writable from here at all - the unit sets its own over the network. Daylight saving and the
    # timezone are stored and only applied by the touchscreen, so both are profile-only.
    "systemDateTime24h": (SEL_SYSTEM, "bool"),              # 24-hour clock vs 12-hour
    "systemDateTimeDaylightSavings": (SEL_SYSTEM, "bool"),
    "systemDateTimeOnHome": (SEL_SYSTEM, "bool"),           # show the clock on the home screen
    # IN THIS TABLE FOR PROFILE SAVE/RESTORE ONLY, and DELIBERATELY NOT ON THE DEVICE PAGE: it is
    # measured display-only, so offering it would be a control that lies. The reason sits next to
    # the decision, in server.UNIT_SETTINGS_NOT_ON_PAGE.
    "systemDateTimezone": (SEL_SYSTEM, "int"),              # an INDEX into the unit's own list
    # SYSTEM block (0x0F): the lockouts. Reversible from here by the same write, and each one was
    # set and cleared on hardware, but they are the entries most able to make a working desk feel
    # broken, so whatever offers them has to say what they do in plain words.
    "disableAllHeadphoneOutputs": (SEL_SYSTEM, "bool"),
    "disableAllLineoutOutputs": (SEL_SYSTEM, "bool"),
    "disableAllPhysicalButtons": (SEL_SYSTEM, "bool"),
}


def set_unit_setting(prop: str, value) -> int:
    """Write one UNIT SETTING (see UNIT_SETTING_SEL) as an ordinary global property.

    Refuses any property name not in the table rather than guessing a selector: a wrong
    selector-plus-property pairing does not simply miss, it can append a stray occurrence that only
    a factory reset clears."""
    entry = UNIT_SETTING_SEL.get(prop)
    if entry is None:
        raise ValueError(f"unknown unit setting {prop!r}; known: {sorted(UNIT_SETTING_SEL)}")
    selector, kind = entry
    if kind == "bool":
        return send_record(build_global_prop_bool(prop, bool(value), selector))
    if kind == "float":
        return send_record(build_global_prop_float(
            prop, selector, max(0.0, min(1.0, float(value)))))
    return send_record(build_global_prop_int(prop, selector, int(value)))


# ---- THE SEND LAYER --------------------------------------------------------

def _write_records(records, purpose: str, delay: float = 0.0) -> int:
    """Write `records` on ONE open connection to the control interface, `delay` seconds apart.
    Returns bytes written. Every write to the desk passes through here."""
    rcp_hid.refuse_write_in_tests()
    iface = find_control_interface()
    if iface is None:
        raise RuntimeError("no control interface (is the unit connected? close the RODE app)")
    dev = rcp_hid.open_handle(iface, purpose)
    n = 0
    try:
        for r in records:
            diagnostics.command(r, purpose)
            n += dev.write(r)
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

# remountPadStorage as the RODE app sends it, verbatim: its value block (01 01 02 00) carries a
# trailing byte no standard bool encoding has.
REMOUNT_PAD_STORAGE = bytes.fromhex(
    "031a000000010101010f72656d6f756e7450616453746f726167650001010200"
).ljust(BULK_LEN, b"\x00")


# A pad create enters and leaves transfer mode for itself, which is right for ONE pad and wasteful
# for sixteen: every cycle drops the desk's audio and pops a Windows device sound, and the cycling
# was measured at about 11 s of the 15 s a pad took in a profile restore.
#
# A HOLD IS A PROMISE THAT SOMEBODY ELSE OWNS THE SESSION. While held, set_transfer_mode is ignored,
# so the per-pad code keeps its own enter/exit calls and they simply stop taking effect. The holder
# enters once at the top and exits once on the way out, unconditionally, success or not.
_transfer_hold = 0


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
    rec = build_global_prop_int("transferModeType", SEL_TRANSFER_MODE, 2 if on else 0)
    # Logged as its own event as well as the record below it: entering transfer mode drops the
    # desk's audio and re-presents a USB mass-storage gadget, so it is the one write whose
    # position in the timeline is worth being able to find without decoding anything.
    diagnostics.event("transfer.enter" if on else "transfer.exit")
    return send_record(rec)


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
    return send_record(REMOUNT_PAD_STORAGE)


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
    except Exception:
        pass          # fall through to the slow but proven path
    _flush_volume_powershell(root)


def _flush_volume_powershell(root: str) -> None:
    """Flush through `Write-VolumeCache`: correct, and about two seconds per call."""
    # CREATE_NO_WINDOW: the server runs under pythonw (no console), so without this each call would
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
                pass
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


def find_pad_volume(timeout_s: float = 30.0, reissue_remount_every: float = 6.0):
    """Wait for the pad-storage FAT volume to mount; return its root path (e.g. 'F:\\'), or None
    if it never appears within timeout_s.

    The re-arm can be slow (over 8 s after a fresh enter), so the timeout is generous and
    remountPadStorage is re-issued periodically in case the first one was swallowed.

    A volume counts only when it passes THREE checks, because each weaker one has been passed by a
    volume that then failed:
      * isdir(<root>\\pads) - passed by a size-0 PHANTOM letter left after a stuck transfer, which
        fails every write with EINVAL;
      * shutil.disk_usage(root).total > 0 - passed by the first entry after the desk has been idle,
        which then fails the first file read with EINVAL;
      * pad_volume_readable(root) - reads bytes, which is what the caller does next.
    A not-yet-ready volume just keeps the wait loop going.
    """
    deadline = time.time() + timeout_s
    next_remount = time.time() + reissue_remount_every
    while time.time() < deadline:
        for letter in string.ascii_uppercase[3:]:  # skip A-C
            root = f"{letter}:\\"
            if os.path.isdir(os.path.join(root, "pads")):
                try:
                    with quiet_drive_errors():
                        ready = shutil.disk_usage(root).total > 0
                    if ready and pad_volume_readable(root):
                        return root
                except OSError:
                    pass  # phantom/size-0 mount, keep waiting
        if time.time() >= next_remount:
            try:
                remount_pad_storage()
            except Exception:
                pass
            next_remount = time.time() + reissue_remount_every
        time.sleep(0.5)
    return None


def _selftest() -> None:
    """Prove the builders reproduce the RODE app's captured bytes, no hardware."""
    # padFilePath assignment, sound4.pcap frame 167 (pad 2 -> sound.wav).
    expected = bytes.fromhex(
        "033f000000010102029a02010170616446696c6550617468000129052f4170706c"
        "69636174696f6e2f656d6d632d646174612f706164732f322f736f756e642e7761"
        "7600"
    ).ljust(BULK_LEN, b"\x00")
    got = build_pad_prop_string("padFilePath", "/Application/emmc-data/pads/2/sound.wav")
    assert got == expected, (
        "build_pad_prop_string mismatch:\n"
        f"  expected {expected[:64].hex()}\n  got      {got[:64].hex()}"
    )
    # padFilePath clear (empty value), sound4.pcap frame 9.
    expected_clear = bytes.fromhex(
        "0318000000010102029a02010170616446696c65506174680001020500"
    ).ljust(BULK_LEN, b"\x00")
    got_clear = build_pad_prop_string("padFilePath", "")
    assert got_clear == expected_clear, (
        "empty padFilePath mismatch:\n"
        f"  expected {expected_clear[:32].hex()}\n  got      {got_clear[:32].hex()}"
    )
    # padIdx pad-addressed int, fx_assign.pcap (padIdx=15 under selector 0x17).
    expected_idx = bytes.fromhex(
        "0316000000010102029a020117706164496478000105010f000000"
    ).ljust(BULK_LEN, b"\x00")
    got_idx = build_pad_prop_int("padIdx", 15, 0x17)
    assert got_idx == expected_idx, (
        "build_pad_prop_int mismatch:\n"
        f"  expected {expected_idx[:32].hex()}\n  got      {got_idx[:32].hex()}"
    )
    # systemMidiControl ON/OFF, midi_on.pcap / midi_off.pcap.
    expected_midi_on = bytes.fromhex(
        "031a000000010101010f73797374656d4d696469436f6e74726f6c00010102"
    ).ljust(BULK_LEN, b"\x00")
    expected_midi_off = bytes.fromhex(
        "031a000000010101010f73797374656d4d696469436f6e74726f6c00010103"
    ).ljust(BULK_LEN, b"\x00")
    assert build_global_prop_bool("systemMidiControl", True) == expected_midi_on, "midi ON mismatch"
    assert build_global_prop_bool("systemMidiControl", False) == expected_midi_off, "midi OFF mismatch"
    # inputMicrophoneGain Mic 1 = 56 dB (gain_up.pcap) and Mic 2 (mic2_gain.pcap).
    exp_gain1 = bytes.fromhex(
        "0321000000010101026e02696e7075744d6963726f70686f6e654761696e0001050138000000"
    ).ljust(BULK_LEN, b"\x00")
    assert build_channel_prop_int("inputMicrophoneGain", 56, 0x6E) == exp_gain1, "mic1 gain mismatch"
    exp_gain2 = bytes.fromhex(
        "0321000000010101026f02696e7075744d6963726f70686f6e654761696e0001050133000000"
    ).ljust(BULK_LEN, b"\x00")
    assert build_channel_prop_int("inputMicrophoneGain", 0x33, 0x6F) == exp_gain2, "mic2 gain mismatch"
    # inputPower (P48) Mic 1 ON (p48_on.pcap).
    exp_p48 = bytes.fromhex(
        "0318000000010101026e02696e707574506f7765720001050101000000"
    ).ljust(BULK_LEN, b"\x00")
    assert build_channel_prop_int("inputPower", 1, 0x6E) == exp_p48, "p48 mismatch"

    # --- DUO channel-address format (trailing 0x01, selector 0x48 + source_code) ---
    # Captured + decoded from the Duo console (see RODECASTER_PROTOCOL.txt section 4.1).
    # inputPower Mic1 (sel 0x48) = off:
    exp_duo_p48 = bytes.fromhex(
        "0318000000010101024801696e707574506f7765720001050100000000"
    ).ljust(BULK_LEN, b"\x00")
    assert build_channel_prop_int("inputPower", 0, 0x48, CHANNEL_ADDR_TRAILER_DUO) == exp_duo_p48, \
        "duo inputPower mismatch"
    # inputMicrophoneGain Mic1 (sel 0x48) = 54 dB (0x36):
    exp_duo_gain = bytes.fromhex(
        "0321000000010101024801696e7075744d6963726f70686f6e654761696e0001050136000000"
    ).ljust(BULK_LEN, b"\x00")
    assert build_channel_prop_int("inputMicrophoneGain", 54, 0x48, CHANNEL_ADDR_TRAILER_DUO) == exp_duo_gain, \
        "duo inputMicrophoneGain mismatch"
    # inputDigitalGain USB1 (sel 0x4f) = -11 dB (signed LE f5ffffff):
    exp_duo_dig = bytes.fromhex(
        "031e000000010101024f01696e7075744469676974616c4761696e00010501f5ffffff"
    ).ljust(BULK_LEN, b"\x00")
    assert build_channel_prop_int("inputDigitalGain", -11, 0x4F, CHANNEL_ADDR_TRAILER_DUO) == exp_duo_dig, \
        "duo inputDigitalGain mismatch"

    # --- DUO polarity + mic-type (channel form) and the two Mic1 processing/global-form props
    # (channelBypassProcessing + channelCurrentFxPreset), all byte-for-byte from console captures
    # (duo_phaseflip / duo_mt_condenser / duo_procbypass / duo_preset).
    # inputPhaseFlip Mic1 (channel sel 0x48, trailer 0x01) = ON (duo_phaseflip.pcap frame 117):
    exp_duo_phase = bytes.fromhex(
        "0318000000010101024801696e7075745068617365466c697000010102"
    ).ljust(BULK_LEN, b"\x00")
    assert build_channel_prop_bool("inputPhaseFlip", True, 0x48, CHANNEL_ADDR_TRAILER_DUO) == exp_duo_phase, \
        "duo inputPhaseFlip mismatch"
    # inputMicrophoneType Mic1 (channel sel 0x48, trailer 0x01) = 3 Condenser (duo_mt_condenser.pcap):
    exp_duo_mtype = bytes.fromhex(
        "0321000000010101024801696e7075744d6963726f70686f6e65547970650001050103000000"
    ).ljust(BULK_LEN, b"\x00")
    assert build_channel_prop_int("inputMicrophoneType", 3, 0x48, CHANNEL_ADDR_TRAILER_DUO) == exp_duo_mtype, \
        "duo inputMicrophoneType mismatch"
    # channelBypassProcessing Mic1 (global/processing form, sel 0x1C) = ON (duo_procbypass.pcap frame 67):
    exp_duo_bypass = bytes.fromhex(
        "0320000000010101011c6368616e6e656c42797061737350726f63657373696e6700010102"
    ).ljust(BULK_LEN, b"\x00")
    assert build_global_prop_bool("channelBypassProcessing", True, 0x1C) == exp_duo_bypass, \
        "duo channelBypassProcessing mismatch"
    # DUO: NON-Mic1 processing selectors, from the duo_proc_<chan>.pcap captures.
    # channelBypassProcessing rides the GLOBAL/processing form at sel = proc_sel_base (0x1C)
    # + STRIP index, where strip index = channelInputSource.index(source_code). Six channels
    # on non-contiguous strips prove STRIP-index (not carousel-position) addressing:
    #   Mic1 s0->0x1C, USB1 s1->0x1D, Chat s2->0x1E, USB2 s3->0x1F, Headset s6->0x22,
    #   Bluetooth s8->0x24. The gaps (strips 4/5/7 empty) are the decisive evidence.
    _duo_proc_name = "6368616e6e656c42797061737350726f63657373696e67"  # "channelBypassProcessing"
    for _sel in (0x1D, 0x1E, 0x1F, 0x22, 0x24):
        _exp = bytes.fromhex("0320000000" + "01010101" + f"{_sel:02x}" + _duo_proc_name
                             + "00010102").ljust(BULK_LEN, b"\x00")
        assert build_global_prop_bool("channelBypassProcessing", True, _sel) == _exp, \
            f"duo channelBypassProcessing strip-selector 0x{_sel:02x} mismatch"
    # channelCurrentFxPreset Mic1 (global/processing form, sel 0x1C) = 1 Podcast Studio (duo_preset.pcap):
    exp_duo_preset = bytes.fromhex(
        "0323000000010101011c6368616e6e656c43757272656e7446785072657365740001050101000000"
    ).ljust(BULK_LEN, b"\x00")
    assert build_global_prop_int("channelCurrentFxPreset", 0x1C, 1) == exp_duo_preset, \
        "duo channelCurrentFxPreset mismatch"
    # DUO: EFFECTS on a NON-Mic1 strip (captures/duo_fx_mic2_strip1.pcap). echoOn rides the
    # GLOBAL/effects form at sel = fx_sel_base (0x26) + STRIP index; Mic2 sits on strip 1 -> 0x27.
    exp_duo_fx_on = bytes.fromhex(
        "030f000000" + "01010101" + "27" + "6563686f4f6e" + "00010102"    # echoOn=True, sel 0x27
    ).ljust(BULK_LEN, b"\x00")
    exp_duo_fx_off = bytes.fromhex(
        "030f000000" + "01010101" + "27" + "6563686f4f6e" + "00010103"    # echoOn=False, sel 0x27
    ).ljust(BULK_LEN, b"\x00")
    assert build_global_prop_bool("echoOn", True, 0x27) == exp_duo_fx_on, \
        "duo echoOn strip-1 fx-selector 0x27 (ON) mismatch"
    assert build_global_prop_bool("echoOn", False, 0x27) == exp_duo_fx_off, \
        "duo echoOn strip-1 fx-selector 0x27 (OFF) mismatch"

    # DUO: MASTER / GLOBAL OUTPUT writes (captures/duo_outwrite.pcap). The selectors are
    # byte-identical to the Pro II, which is what the "output_write" cap rests on:
    # recordingMultitrackMode 0x0D (int), outputBTAutoMute 0x0D (bool), masterCompellorOn 0x0E
    # (bool), headphoneType 0x09 (Headphone 1) / 0x0A (Headphone 2) (int).
    exp_duo_mt = bytes.fromhex(
        "0324000000" + "01010101" + "0d"
        + "7265636f7264696e674d756c7469747261636b4d6f6465" + "00" + "010501" + "01000000"
    ).ljust(BULK_LEN, b"\x00")
    assert build_global_prop_int("recordingMultitrackMode", 0x0D, 1) == exp_duo_mt, \
        "duo recordingMultitrackMode 0x0D mismatch"
    exp_duo_btmute = bytes.fromhex(
        "0319000000" + "01010101" + "0d" + "6f757470757442544175746f4d757465" + "00" + "010102"
    ).ljust(BULK_LEN, b"\x00")
    assert build_global_prop_bool("outputBTAutoMute", True, 0x0D) == exp_duo_btmute, \
        "duo outputBTAutoMute 0x0D mismatch"
    exp_duo_comp = bytes.fromhex(
        "031a000000" + "01010101" + "0e" + "6d6173746572436f6d70656c6c6f724f6e" + "00" + "010103"
    ).ljust(BULK_LEN, b"\x00")
    assert build_global_prop_bool("masterCompellorOn", False, 0x0E) == exp_duo_comp, \
        "duo masterCompellorOn 0x0E mismatch"
    exp_duo_hp1 = bytes.fromhex(
        "031a000000" + "01010101" + "09" + "6865616470686f6e6554797065" + "00" + "010501" + "01000000"
    ).ljust(BULK_LEN, b"\x00")
    assert build_global_prop_int("headphoneType", 0x09, 1) == exp_duo_hp1, \
        "duo headphoneType 0x09 (Headphone 1) mismatch"
    exp_duo_hp2 = bytes.fromhex(
        "031a000000" + "01010101" + "0a" + "6865616470686f6e6554797065" + "00" + "010501" + "01000000"
    ).ljust(BULK_LEN, b"\x00")
    assert build_global_prop_int("headphoneType", 0x0A, 1) == exp_duo_hp2, \
        "duo headphoneType 0x0A (Headphone 2) mismatch"

    # CROSS-POINT ADDRESS WIDTH. A selector is a child index, so one over 255 must widen the
    # address rather than wrap: Music (source 13, output 0) is 245 and stays `01 f5`, Virtual A
    # (14) is 258 and becomes `02 02 01`, Virtual B (15) is 271 and becomes `02 0f 01`. Both
    # halves are asserted because a mask would pass the first and silently redirect the second
    # to a node 256 lower - a valid write to the wrong cross-point.
    exp_xp_music = bytes.fromhex(
        "0314000000" + "01010101" + "f5" + "6d697844697361626c6564" + "00" + "010102"
    ).ljust(BULK_LEN, b"\x00")
    assert build_global_prop_bool(
        "mixDisabled", True, cross_point_selector(0, 13)) == exp_xp_music, \
        "cross-point source 13 should still address in ONE byte"
    exp_xp_va = bytes.fromhex(
        "0315000000" + "01010102" + "0201" + "6d697844697361626c6564" + "00" + "010102"
    ).ljust(BULK_LEN, b"\x00")
    assert build_global_prop_bool(
        "mixDisabled", True, cross_point_selector(0, 14)) == exp_xp_va, \
        "cross-point source 14 (Virtual A) should address in TWO bytes"
    exp_xp_vb = bytes.fromhex(
        "0322000000" + "01010102" + "0f01" + "6d69784c6576656c57697468416e63686f72" + "00"
        + "010705" + "302e357c30" + "00"
    ).ljust(BULK_LEN, b"\x00")
    assert build_cross_point_level(cross_point_selector(0, 15), 0.5) == exp_xp_vb, \
        "cross-point source 15 (Virtual B) level should address in TWO bytes"


_USAGE = "usage: rcp_transfer.py enter | exit | remount | selftest | midi on|off"


if __name__ == "__main__":
    import sys
    # No default command: a bare run must not enter transfer mode and drop the desk's audio.
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
