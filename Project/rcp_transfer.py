"""
AccessCaster - RODECaster Pro II config property channel (report id 3 on MI_09).

Decoded from captures of the RODE app. The app configures pads by sending
256-byte "report id 3" records on the MI_09 control interface (the same HID
interface rcp_hid already talks to), each carrying one named property:

  byte 0      : report id 0x03
  bytes 1..4  : payload length (uint32 LE)
  payload:
    address path:
       global property : 01 01 01 01 <selector>
       pad property    : 01 01 02 02 9a 02 01 01   (pad-addressed)
    name   : ASCII, null terminated
    value  : 01 <vlen> <vtype> <value>
             vtype 0x01 = integer (value is vlen-1 bytes, little-endian)
             vtype 0x05 = string  (value is vlen-1 bytes)
  then zero padding to 256 bytes.

Known global selectors (the byte after 01 01 01 01):
  0x0f transferModeType   int  (2 = enter transfer mode, 0 = exit)
  0x06 encoderColour      int
  0x0f remountPadStorage  int  (selector overlaps; distinguished by name)

In Transfer Mode the unit mounts its pad storage as a FAT32 USB drive (the
"Linux File-Stor Gadget", a removable drive on Windows). Pad sounds live there
at \\pads\\<n>\\<name>.wav, so writing a sound is an ordinary file copy; only the
config (this channel) needs the HID protocol.

WARNING: this module WRITES to the device. It only sends records that match what
the RODE app itself sends. Do not invent new selectors/opcodes against hardware.
"""

from __future__ import annotations

import struct

import model_tree                    # the tree grammar, and with it the ONE address format
import devices                       # per-device matrix shape (GAP-5 cross-point guard)
import diagnostics                   # the app's own account of every record it sends
import rcp_hid                       # write-generation counter (invalidates cached model reads)
from rcp_hid import find_control_interface

REPORT_BULK_OUT = 0x03
BULK_LEN = 256

SEL_TRANSFER_MODE = 0x0F
# Pad-addressed properties (e.g. padFilePath, padColourIndex) use this 8-byte
# address header instead of the global "01 01 01 01 <selector>" form. The first 6
# bytes are constant (010102029a02); byte[6] is 0x01 for a normal pad and is then
# followed by byte[7] = the per-pad SELECTOR. Proven on hardware: with
# the correct selector, a report-3 padFilePath written from our own code re-points
# that pad (no RODE app).
PAD_ADDRESS = bytes.fromhex("010102029a020101")  # last byte 0x01 = grid pad 3's selector

# SPECIAL CASE - the first/internal pad (grid position 1, the factory "Music Bed"
# slot) is addressed WITHOUT a selector byte: header 010102029a0200 then the name
# directly (one byte shorter). byte[6]=0x00 means "no selector / internal pad".
PAD_ADDRESS_INTERNAL = bytes.fromhex("010102029a0200")

# STABLE pad identity (session 7 - confirmed on hardware):
#   - padIdx (0-based) == physical/app-grid position - 1.  (grid 16 -> padIdx 15;
#     pad5sound grid 5 -> padIdx 4.)
#   - The sound file lives in pads/<grid position>/ on the FAT volume. Confirmed:
#     assigning to grid 16 created F:\pads\16\sound.wav. So internal folder number
#     == grid position. replace_pad_sound can address pads by grid number directly;
#     it needs NO selector (it globs F:\pads\<grid_n>\).
#
# WARNING - THE SELECTOR BYTE IS DYNAMIC, NOT A STABLE PAD ADDRESS. It is reassigned
# whenever the set of sound pads changes. Proven this session: grid 16 was selector
# 0x0e while an FX pad, then became 0x17 once converted to a Sound pad; at the same
# time grid 2 shifted from 0x17 to 0x16. So the map below is a SNAPSHOT taken during
# the recolour harvest, valid only until the next add/convert of a sound pad. Do NOT
# treat these as permanent. For config writes (padFilePath etc) the app announces
# the binding by sending a padIdx record under the chosen selector at the start of
# the edit; to replay robustly, discover the current selector (recolour-and-capture,
# capture_analyze) or send padIdx=<target> first, then the property, under one
# selector. The stable anchor for "which physical pad" is padIdx / the folder number.
#
# Grid 1 ("Music Bed") is the internal pad addressed with NO selector byte
# (PAD_ADDRESS_INTERNAL, header 010102029a0200).

# The firmware references a pad's sound by this path; <n> is the pad number and
# the filename is whatever WAV lives in F:\pads\<n>\.
PAD_FILE_PATH_FMT = "/Application/emmc-data/pads/{n}/{name}"


def _frame(payload: bytes) -> bytes:
    """Wrap a built payload into the fixed 256-byte report-3 record: report-id byte,
    u32-LE payload length, the payload, then zero-padding to BULK_LEN. Every build_*
    function ends with this identical framing; the _selftest below proves the bytes are
    byte-for-byte the same as the app's captured frames."""
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
# THE DUO'S "QUIRK" IS NOT ONE: its trailing 0x01 where the Pro II has 0x02 is not a
# per-device difference in the protocol, it is the HIGH BYTE of a two-byte index (0x0148 against
# 0x026E). The two desks simply have that node at different positions.
#
# PAD_ADDRESS_INTERNAL (`...9a 02 00`) is the same path [SOUNDPADS, 0] with the index written in a
# zero-length count, which the grammar allows. It came from a RODE app capture and WE HAVE NEVER
# SENT IT: our own pad writes use `01 00` for position 0 and are proven on hardware. So the desk
# accepts both and we keep emitting the one we know works. See model_tree.encode_path.
#
# The builders below derive their bytes from the path, and `_selftest` proves each one is
# byte-identical to the captures they were written from.
SOUNDPADS_INDEX = 0x29A          # the SOUNDPADS node's position among the root's children


def build_address(path) -> bytes:
    """The address bytes for a tree path. The ONE place an address is formed."""
    return model_tree.encode_path(path)


# A GLOBAL SELECTOR IS A CHILD INDEX, NOT A BYTE, so the builders below pass it to
# `build_address` whole. `encode_path` emits an index in the fewest bytes that hold it, so 0x0F
# stays `01 0f` and 258 becomes `02 02 01` - which is how the desk itself addresses a cross-point
# above source 13. Masking to 0xFF truncates silently rather than raising, and a truncated
# selector is a write to a DIFFERENT, valid node. The pad builders keep their mask because a pad
# index cannot reach 256.


def build_global_prop_int(name: str, selector: int, value: int) -> bytes:
    """Build a 256-byte report-id-3 record setting a global integer property."""
    name_b = name.encode("ascii")
    # SIGNED int32: the device stores an "empty fader" as channelInputSource -1 (0xFFFFFFFF) and
    # our reader (rcp_read._val_after) decodes int values signed, so the write side must match.
    # All non-negative callers (selectors, modes, source codes 0..16) encode identically to the
    # old unsigned form, so this is byte-compatible and additionally lets us write -1 to vacate a
    # strip. Confirmed on the Duo (write -1 -> read back -1, byte-clean).
    value_b = int(value).to_bytes(4, "little", signed=True)
    value_block = bytes([0x01, 0x05, 0x01]) + value_b
    payload = build_address([selector]) + name_b + b"\x00" + value_block
    return _frame(payload)


# --- channelInputSource (Audio Setup "drag and drop input source") ------------
# Decoded from a drag/drop capture (captures/inputsrc.pcap +
# inputsel2.pcap): reassigning a top-bar channel strip's input source in Audio
# Setup sends ONE global int property write, channelInputSource, on a PER-STRIP
# selector. It stands alone - NO systemChannelSelected pre-write is needed.
# build_global_prop_int reproduces the captured bytes byte-for-byte.
#
# Selector = per-strip, SEQUENTIAL from 0x1c, in the fixed top-bar order (same as
# rcp_read.CHANNELS). The five FLEXIBLE strips 0x20..0x24 are ALL CONFIRMED by
# capture (inputsrc/inputsel2/inputmid.pcap). The four mic strips 0x1c..0x1f are
# inferred from the same line (the app gives no way to reassign a mic strip's input).
CHANNEL_INPUT_SOURCE_SELECTOR = {
    "Mic1": 0x1C, "Mic2": 0x1D, "Mic3": 0x1E, "Mic4": 0x1F,   # inferred (linear)
    "Bluetooth": 0x20,                                         # CONFIRMED
    "SMARTpads": 0x21, "USB1": 0x22, "Chat": 0x23,            # CONFIRMED
    "USB2": 0x24,                                             # CONFIRMED
}
# Source CODES (the int value written), read off the model vector + the two captures.
#
# MIC 4 IS 3, NOT 1027. 1027 is 0x403, Mic 4 with the STEREO-LINK BIT set, which is what a capture
# records when the desk that produced it happened to have inputs 3+4 linked. Confirmed against a
# desk reading 3 and against the app-driven link/unlink captures. Reading 0x400 as part of the
# source code rather than as a link flag is also what hides stereo linking entirely.
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
# the desk in a state byte-identical to the app's own (verified by model diff), and
# the app itself then draws the strip as linked. So this needs no nested-path address builder.
#
# The LOWER mic's fader carries the pair; the other fader is vacated with -1. UNLINK IS ASYMMETRIC,
# and that is the desk's behaviour, not ours: it returns only the lower mic, leaving the upper one
# off-fader. The upper mic's processing survives that round trip only because channels.park_chain
# holds it (see server._follow_chains).
STEREO_LINK_PAIRS = {4: (0, 1), 5: (1, 2), 6: (2, 3)}


def build_channel_input_source(strip: str, source_code: int) -> bytes:
    """Build the report-3 record that sets a channel strip's input source.

    `strip` is a key of CHANNEL_INPUT_SOURCE_SELECTOR (top-bar order); `source_code`
    is an INPUT_SOURCE_CODE value. Verified byte-for-byte against the drag captures
    (e.g. strip 'USB2' / code 9 == captures/inputsrc.pcap restore write).
    """
    return build_global_prop_int(
        "channelInputSource", CHANNEL_INPUT_SOURCE_SELECTOR[strip], source_code
    )


def build_pad_prop_string(name: str, value: str, selector: int = 0x01) -> bytes:
    """Build a 256-byte report-id-3 record setting a pad-addressed string property.

    `selector` is the per-pad address byte (PAD_ADDRESS[7]); default 0x01 = pad 2,
    which keeps the byte-for-byte selftest against sound4.pcap valid.

    Layout (verified byte-for-byte against sound4.pcap frame 167, padFilePath):
        report 0x03 | payload-len u32 LE | address(8) | name | 0x00
        | 0x01 | vlen | 0x05 | <value bytes> 0x00      (vlen = 1 + len(value)+1)
    """
    # The pad path: SOUNDPADS, then this pad's stream position. Was a hardcoded 8-byte constant
    # with byte 7 overwritten, which is the same bytes for every selector we send.
    addr = build_address([SOUNDPADS_INDEX, selector & 0xFF])
    name_b = name.encode("ascii")
    value_b = value.encode("ascii") + b"\x00"  # value is null-terminated
    vlen = 1 + len(value_b)  # the vtype byte plus the value+null
    value_block = bytes([0x01, vlen, 0x05]) + value_b
    payload = addr + name_b + b"\x00" + value_block
    return _frame(payload)


def build_pad_prop_int(name: str, value: int, selector: int = 0x01) -> bytes:
    """Build a 256-byte report-id-3 record setting a pad-addressed integer property.

    Verified byte-for-byte against fx_assign.pcap (padIdx=15 under selector 0x17):
        report 0x03 | payload-len u32 LE | address(8) | name | 0x00
        | 0x01 0x05 0x01 | <value as 4-byte LE>
    Used for padIdx (the 0-based pad index = grid position - 1), which the app sends
    under its chosen selector to bind that selector to the target pad.
    """
    # The pad path: SOUNDPADS, then this pad's stream position. Was a hardcoded 8-byte constant
    # with byte 7 overwritten, which is the same bytes for every selector we send.
    addr = build_address([SOUNDPADS_INDEX, selector & 0xFF])
    name_b = name.encode("ascii")
    value_b = int(value).to_bytes(4, "little")
    value_block = bytes([0x01, 0x05, 0x01]) + value_b
    payload = addr + name_b + b"\x00" + value_block
    return _frame(payload)


def build_global_prop_string(name: str, value: str, selector: int) -> bytes:
    """Build a 256-byte report-id-3 record setting a global STRING property.

    Needed first for wifi (wifiSSID / wifiPSK on NETWORK 0x10); every global write
    before this one was an int, a bool or a float, so there was no string form on this path.

    NOTHING NEW IS INVENTED HERE, which matters given the standing rule against making up record
    shapes. It is two halves we already have, each verified separately:
      * the global address prefix `01 01 01 01 <selector>`, identical to build_global_prop_int, which
        is byte-for-byte against the app's own captures and has driven hundreds of writes;
      * the string value block `01 <vlen> 05 <value> 00`, identical to build_pad_prop_string, which is
        verified byte-for-byte against sound4.pcap frame 167 (padFilePath).
    It is still the FIRST time the two have been combined, so treat the first write on any property
    as unproven until it is read back. rcp_read._val_after decodes exactly this shape.
    """
    name_b = name.encode("ascii")
    value_b = value.encode("ascii") + b"\x00"   # the value is null-terminated
    vlen = 1 + len(value_b)                     # the vtype byte plus the value and its null
    value_block = bytes([0x01, vlen, 0x05]) + value_b
    payload = build_address([selector]) + name_b + b"\x00" + value_block
    return _frame(payload)


SEL_SYSTEM = 0x0F  # global selector for system/global properties (also transferModeType)
SEL_NETWORK = 0x10 # NETWORK block: wifi and Bluetooth (proven on hardware)


def build_global_prop_bool(name: str, on: bool, selector: int = SEL_SYSTEM) -> bytes:
    """Build a global report-3 record for a compact boolean property.

    Value encoding (decoded from midi_on/midi_off captures): 01 01 <0x02=on/0x03=off>.
    Verified byte-for-byte against systemMidiControl ON/OFF. NOTE: enabled is 0x02 and
    disabled is 0x03 (NOT 1/0).
    """
    name_b = name.encode("ascii")
    value_block = bytes([0x01, 0x01, 0x02 if on else 0x03])
    payload = build_address([selector]) + name_b + b"\x00" + value_block
    return _frame(payload)


# Mic-input addressing lives in devices.DeviceModel.input_path: it is derived from the channel's
# source code, on every desk, and there is no selector table to keep anywhere.


# The trailing address byte after the channel selector. The Pro II uses 0x02; the
# RODECaster Duo uses 0x01 (captured; see RODECASTER_PROTOCOL.txt section 4.1). It is
# the ONLY structural difference in the channel-address format between the two desks,
# so the builders take it as a parameter (default 0x02 keeps every Pro II caller and
# the byte-for-byte selftest unchanged); the Duo passes 0x01 via devices.DUO.
#
# WHAT IT ACTUALLY IS, decoded from our own captures: NOT a
# trailer and NOT a per-device constant. The address is length-prefixed and LITTLE-ENDIAN, so
# `02 6e 02` is "2 bytes, 0x026E" - and 0x026E is INPUTSOURCE[0] in the block walk. This byte is the
# HIGH BYTE. The Duo's is 0x01 because the Duo is a smaller desk whose INPUTSOURCE sits at 0x148,
# not because the two desks frame channels differently. The names below are kept because every
# caller uses them and renaming them would churn the byte-for-byte selftests, but do not reason from
# the word "trailer": a two-byte address is the general case and 0xFFFF is the real ceiling.
CHANNEL_ADDR_TRAILER_PRO2 = 0x02
CHANNEL_ADDR_TRAILER_DUO = 0x01


def build_path_prop_int(name: str, value: int, path) -> bytes:
    """A report-3 integer property at a TREE PATH. Value block 01 05 01 <4-byte LE, signed>.

    The path form is the primitive and the selector/trailer pair below is a view onto it, because
    the "trailer" was only ever the high byte of a two-byte index (see the note above). Anything
    addressing a node it resolved through the tree should call this and skip the split."""
    name_b = name.encode("ascii")
    value_block = bytes([0x01, 0x05, 0x01]) + int(value).to_bytes(4, "little", signed=True)
    return _frame(build_address(list(path)) + name_b + b"\x00" + value_block)


def build_path_prop_bool(name: str, on: bool, path) -> bytes:
    """A report-3 compact-boolean property at a TREE PATH. Value block 01 01 <0x02 on / 0x03 off>."""
    name_b = name.encode("ascii")
    value_block = bytes([0x01, 0x01, 0x02 if on else 0x03])
    return _frame(build_address(list(path)) + name_b + b"\x00" + value_block)


def build_channel_prop_int(name: str, value: int, chan_sel: int,
                           trailer: int = CHANNEL_ADDR_TRAILER_PRO2) -> bytes:
    """Build a channel-addressed report-3 integer property (mixer channel setting).

    Address 01 01 01 02 <chan_sel> <trailer>, value block 01 05 01 <4-byte LE>.
    Pro II verified byte-for-byte against inputMicrophoneGain (Mic 1 0x6e, Mic 2 0x6f)
    and inputPower (trailer 0x02). Duo verified byte-for-byte against inputPower/
    inputMicrophoneGain (sel 0x48) + inputDigitalGain (sel 0x4f) with trailer 0x01.
    Sent in NORMAL mode (no Transfer Mode).

    THE DUO SELECTORS IN THAT LAST SENTENCE ARE BYTE-CORRECT AND FUNCTIONALLY DEAD: the bytes match
    the RODE app's capture exactly, and the desk's audio engine ignores that node. See
    devices.DeviceModel.input_path. Nothing in the app addresses a Duo input that way any more.
    """
    return build_path_prop_int(name, value, [(chan_sel & 0xFF) | ((trailer & 0xFF) << 8)])


def build_channel_prop_bool(name: str, on: bool, chan_sel: int,
                            trailer: int = CHANNEL_ADDR_TRAILER_PRO2) -> bytes:
    """Build a channel-addressed report-3 compact-boolean property.

    Address 01 01 01 02 <chan_sel> <trailer>, value block 01 01 <0x02=on/0x03=off>
    (same compact-bool encoding as the global bool, verified against systemMidiControl).
    Used for channel on/off settings (mute, processing toggles, polarity). `trailer` is
    0x02 for the Pro II (default) and 0x01 for the Duo (devices.DUO).
    """
    return build_path_prop_bool(name, on, [(chan_sel & 0xFF) | ((trailer & 0xFF) << 8)])


# >>> CHANNEL MUTE **IS** WRITABLE. Proven on hardware. <<<
# "An exhaustive scan of all 256 selectors found nothing" is the kind of negative that looks
# settled and is not: that scan runs in the CHANNEL ADDRESS format (01 01 01 02 <sel> <trailer>),
# which is the MIC-PREAMP format. channelOutputMute lives in the CHANNEL block and takes the GLOBAL
# format (01 01 01 01 <sel>) on SEL_CHANNEL_PROC + strip, byte-identical to how
# channels.set_processing_module writes noiseGateOn.
# TEST (Mic 2, strip 1, selector 0x1D): wrote True, channelOutputMute[1] flipped, and the firmware
# PROPAGATED it into the mix matrix - the twelve mixMute cells for source 1 all went True - so it is
# a real mute, not a stored flag. Wrote False, everything returned to baseline, no stray appended.
# LESSON: a negative result is only as good as the address and format it was tested with. Check the
# section header (DevTools/selector_map.py) before trusting one.
# Still true: the input* mic-preamp settings use the channel-address format above.


# Mic input setters (gain/phantom/polarity/type) + CHANNEL_PROP/MIC_TYPE (moved to channels.py - the single source of truth for channel + processing ops)


def set_midi_control(on: bool) -> int:
    """Enable/disable bi-directional MIDI control (app: Device Config > System > MIDI).

    Sent as a direct global property on MI_09; NO Transfer Mode needed. Turning this
    ON is the long-standing chicken-and-egg unlock: it lets the sibling Access Deck
    project do MIDI mute/fader/trigger control of the mixer without the RODE app.
    """
    return send_record(build_global_prop_bool("systemMidiControl", on))


# ----------------------------------------------------------------------------
# ROUTING / OUTPUT settings (sessions 16-17). Same report-3 mechanism; decoded
# selectors (see RODECASTER_PROTOCOL.txt section 5):
#   outputMixMinus     PER-OUTPUT, selector 0x31 + output_index; int
#                      0=Main Mix, 1=Mix-Minus, 2=Custom (HP4 + hp1..monitor
#                      confirmed by capture, rest follow the sequential pattern).
#   recordingMultitrackMode / outputMonAutoMute / outputBTAutoMute : global
#                      output block, selector 0x0d.
#   headphoneType      output 1, selector 0x09; int 0=NTH-100, 1=High Sensitivity.
#   channelInputSource PER-STRIP, see build_channel_input_source above.
# ----------------------------------------------------------------------------

SEL_OUTPUT_GLOBAL = 0x0D            # global output block (multitrack, auto-mutes)
SEL_OUTPUT_MIXMINUS_BASE = 0x31     # outputMixMinus selector = base + output index
SEL_HEADPHONE_TYPE = 0x09           # headphoneType Headphone 1 (default)
# Fixed output order the outputMixMinus selectors follow (= 0x31 + output_index).
# ALL CONFIRMED (captures/matrix_outputs.pcap). The earlier list was missing
# REC (the session-18/19 off-by-one that mis-targeted outputs past Monitor); REC is index
# 5, so Bluetooth=6 / USB1=7 / Chat=8 / USB2=9 / CallMe1=10. This order matches
# CROSS_POINT_OUTPUT_INDEX exactly. NOTE: session 19's "USB1=0x37" was that off-by-one;
# the captured truth is USB1 mode selector = 0x38 (0x37 = Bluetooth) - re-confirm by ear
# before relying on the old USB-recording fix.
OUTPUTS = ["Headphone 1", "Headphone 2", "Headphone 3", "Headphone 4", "Monitor",
           "REC", "Bluetooth", "USB 1", "Chat", "USB 2", "CallMe 1"]
MULTITRACK_MODES = {0: "Off", 1: "Pre-Fader", 2: "Post-Fader"}
# recordingCompressionQuality. TWO members, and the enum is the whole range: see set_recording_format
# for why anything else must be refused rather than passed through.
RECORDING_FORMATS = {0: "WAV", 1: "MP3"}


def set_output_routing(output_index: int, mode: int) -> int:
    """outputMixMinus for one output: 0=Main Mix, 1=Mix-Minus, 2=Custom."""
    return send_record(build_global_prop_int(
        "outputMixMinus", SEL_OUTPUT_MIXMINUS_BASE + output_index, mode))


# ----------------------------------------------------------------------------
# ROUTING MATRIX cross-points (output Custom mix) - DECODED.
# Full detail + the source/output index tables in RODECASTER_PROTOCOL.txt section 5.
#
# Reached in the app via Device Config > Output > Routing > pick output > Custom,
# then each source has a CHAIN icon cycling linked -> unlinked -> excluded.
#
# Each cross-point (output o, source s) has a UNIQUE SELECTOR on the ordinary
# global-property framing 01 01 01 01 <selector>:
#       selector = CROSS_POINT_BASE + 13*source_index + output_index
# Output is the FAST axis (stride 1, consecutive outputs = consecutive selectors,
# CONFIRMED HP3=2/HP4=3); source is the SLOW axis (stride 13 = 13 internal dests).
#
# Two property writes ride that selector:
#   mixUnlinkRequest / mixLinkRequest : value = CONSTANT compound payload
#       01 07 08 01 01 02 01 01 02   (it is NOT the address - the selector is).
#   mixDisabled : compact bool 0x02 = excluded, 0x03 = included (same as any bool).
# To EXCLUDE source from output: mixUnlinkRequest(sel) then mixDisabled(sel)=excluded.
# To RESTORE: mixLinkRequest(sel)+mixDisabled(sel)=included, OR set the output to
# Main Mix (set_output_routing(o, 0)) which discards the whole custom matrix.
# ----------------------------------------------------------------------------

CROSS_POINT_BASE = 76          # 0x4c
CROSS_POINT_OUTPUT_STRIDE = 1  # output is the fast axis
CROSS_POINT_SOURCE_STRIDE = 13 # source is the slow axis (13 destinations)

# Source index in the matrix = the channelInputSource source-CODE (that is why USB1=7,
# Chat=8, USB2=9, BT=10, SMART=11 line up with INPUT_SOURCE_CODE). NOT the fader display
# order. The 9 below are CONFIRMED by capture (matrix_src.pcap, all 9 faders at HP4).
# Indices 4/5/6 are INFERRED to be the 3 SIP callers' incoming audio (CallMe1/2/3 as a
# source): the device has 3 call slots (sipCallSlotId 0/1/2) and the app only shows caller
# faders when a call is live, so we could not capture them. NOT confirmed - see the guard.
CROSS_POINT_SOURCE_INDEX = {
    "Mic1": 0, "Mic2": 1, "Mic3": 2, "Mic4": 3,
    "USB1": 7, "Chat": 8, "USB2": 9, "Bluetooth": 10, "SMARTpads": 11,
    "CallMe1src": 4, "CallMe2src": 5, "CallMe3src": 6,   # INFERRED (callers as sources)
}
# The 9 source indices proven byte-for-byte against a capture (Pro II). Kept as the FALLBACK for
# a device whose MatrixSpec carries no source list; the live guard (GAP-5) validates against the
# CONNECTED device's matrix.source_indices instead, so a Duo refuses a Pro II-only source (Mic 4).
CONFIRMED_SOURCE_INDICES = {0, 1, 2, 3, 7, 8, 9, 10, 11}

# Output index. Indices 0-10 ALL CONFIRMED (captures/matrix_outputs.pcap swept
# every visible output button; (output,Mic1) selector = 76 + output_index reads each index
# directly). Contiguous 0-10 in grid order. The 2 hidden destinations 11/12 are INFERRED to
# be CallMe2/CallMe3 outputs (the device has 3 SIP call slots; the app only shows CallMe1
# until more callers connect, so we could not capture them). NOT confirmed - see the guard.
CROSS_POINT_OUTPUT_INDEX = {
    "Headphone1": 0, "Headphone2": 1, "Headphone3": 2, "Headphone4": 3,
    "Monitor": 4, "REC": 5, "Bluetooth": 6, "USB1": 7,
    "Chat": 8, "USB2": 9, "CallMe1": 10,
    "CallMe2": 11, "CallMe3": 12,                        # INFERRED (2nd/3rd phone callers)
}
# The 11 output indices proven byte-for-byte against a capture (Pro II). FALLBACK only; the live
# guard (GAP-5) validates against the CONNECTED device's matrix.outputs (so a Duo, which exposes no
# Headphone 3/4, refuses output indices 2/3 instead of writing to a phantom slot).
CONFIRMED_OUTPUT_INDICES = set(range(0, 11))


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
    """Hard guard: refuse any output/source index the CONNECTED device does not expose in its
    routing matrix, unless the caller explicitly opts in to the INFERRED CallMe2/3 outputs +
    caller-source slots. Device-aware (GAP-5): validates against the active device's MatrixSpec
    (its exposed matrix.outputs + matrix.source_indices) rather than the Pro II capture-confirmed
    constants, so a Pro II-only cross-point (Mic 4 = source 3, Headphone 3/4 = outputs 2/3) is
    refused on a Duo instead of silently writing to a phantom slot. Falls back to the Pro II
    confirmed sets for a device whose matrix carries no label/source list. This stops a stray
    index from silently writing to an unintended (possibly live-call) slot.

    THE EXPANDED FOUR COUNT AS EXPOSED IN BOTH MODES, because the grid carries all 30 source rows
    on the wire whichever mode the desk is in: rows 12 to 15 are ordinary MIX nodes that the read
    path already walks and gets values from. Whether to OFFER them is a UI question, answered by
    `MatrixSpec.sources_for`; this guard is about whether the node exists."""
    dev = devices.get_active_device()
    m = dev.matrix
    valid_outputs = {o["index"] for o in m.outputs} if m.outputs else CONFIRMED_OUTPUT_INDICES
    valid_sources = (set(m.all_source_indices) if m.source_indices
                     else CONFIRMED_SOURCE_INDICES)
    if output_index not in valid_outputs:
        if not (allow_inferred and output_index in (11, 12)):
            raise ValueError(
                f"output_index {output_index} is not an exposed cross-point output on the "
                f"{dev.name} (exposed {sorted(valid_outputs)}; 11/12 = inferred CallMe2/3, "
                f"pass allow_inferred=True to use them).")
    if source_index not in valid_sources:
        if not (allow_inferred and source_index in (4, 5, 6)):
            raise ValueError(
                f"source_index {source_index} is not an exposed cross-point source on the "
                f"{dev.name} (exposed {sorted(valid_sources)}; 4/5/6 = inferred callers, "
                f"pass allow_inferred=True to use them).")


def build_mix_link_request(selector: int, link: bool) -> bytes:
    """mixLinkRequest (link=True) / mixUnlinkRequest (link=False) on a cross-point.

    The value is the CONSTANT request payload 01 07 08 01 01 02 01 01 02 (decoded; it does not
    encode the address, the selector does). Byte-for-byte the
    format the app sends when you click a cross-point's chain icon.
    """
    name_b = (b"mixLinkRequest" if link else b"mixUnlinkRequest")
    value_block = bytes([0x01, 0x07, 0x08, 0x01, 0x01, 0x02, 0x01, 0x01, 0x02])
    payload = build_address([selector]) + name_b + b"\x00" + value_block
    return _frame(payload)


def set_cross_point_excluded(output_index: int, source_index: int, excluded: bool,
                             allow_inferred: bool = False) -> int:
    """Exclude (True) or include (False) one source in one output's Custom mix.

    The output must already be in Custom mode (set_output_routing(o, 2)). Sends the
    matching link/unlink request then the mixDisabled bool, both on the cross-point
    selector. Returns total bytes written. READ BACK mixDisabled to confirm. Refuses
    non-confirmed indices unless allow_inferred=True (CallMe2/3 + caller sources)."""
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
    """mixLevelWithAnchor on a cross-point: value is the ASCII string '<level>|<anchor>'.

    level = normalised fader position 0.0 (bottom/min) .. 1.0 (top/max); anchor = 0 for
    an unlinked (absolute) cross-point. Value framing 01 <strlen+2> 05 <ascii> 00 (string
    type 0x05). Decoded from a fader-drag capture (captures/matrix_level.pcap).
    Only writable when the cross-point is UNLINKED; a linked cross-point follows the
    channel fader. Byte-for-byte the format the app sends.
    """
    s = f"{_level_str(level)}|{int(anchor)}".encode("ascii")
    value_block = bytes([0x01, len(s) + 2, 0x05]) + s + b"\x00"
    payload = build_address([selector]) + b"mixLevelWithAnchor" + b"\x00" + value_block
    return _frame(payload)


def set_cross_point_level(output_index: int, source_index: int, level: float,
                          allow_inferred: bool = False) -> int:
    """Set one cross-point's level (0..1). The cross-point must be UNLINKED first
    (set_cross_point_excluded(..., False) leaves it included+linked; to make the level
    independent, send a mixUnlinkRequest first). The output must be in Custom mode.
    Refuses non-confirmed indices unless allow_inferred=True."""
    _check_cross_point(output_index, source_index, allow_inferred)
    sel = cross_point_selector(output_index, source_index)
    return send_record(build_cross_point_level(sel, level))


def set_channel_input_source(strip: str, source_code: int) -> int:
    """Set which source feeds a (flexible) channel strip. See build_channel_input_source."""
    return send_record(build_channel_input_source(strip, source_code))


# --- FADER ASSIGNMENT (which channel sits on which fader) ---------------------
# DECODED (sighted-assisted drag of fader 1 onto fader 2 + before/after
# model diff + captured writes; see RODECASTER_PROTOCOL.txt section 6). The fader layout IS the
# channelInputSource vector: position = fader (0-based), value = the channel/source
# CODE on that fader. The app reorders by writing channelInputSource on a per-FADER
# selector = 0x1c + fader_index (fader 1 = 0x1c ... fader 9 = 0x24). Captured swap
# was sel 0x1c value 1 (Mic2->fader1) and sel 0x1d value 0 (Mic1->fader2). The mic
# faders 1-4 ARE writable. Verified from our own code by writing a swap back.
FADER_SELECTOR_BASE = 0x1C   # channelInputSource selector for fader 1; + index for the rest
FADER_COUNT = 9              # Pro II fallback (6 physical + 3 virtual); the real bound is
                             # device-aware (the Duo has 10) - see set_fader_source.


def set_fader_source(fader_index: int, source_code: int) -> int:
    """Put a channel/source CODE on a fader (0-based: 0 = fader 1 ...).

    One channelInputSource write on selector 0x1c + fader_index. source_code is an
    INPUT_SOURCE-style channel code (Mic1=0..Mic4=3, USB1=7, Chat=8, USB2=9,
    Bluetooth=10, SMARTpads=11, CallMe1=16), or -1 to VACATE the strip (encoded signed;
    the Duo stores an empty strip as -1, confirmed read-back). Byte-for-byte
    the record the RODE app sends when you drag a channel onto a fader.

    The valid fader-index range is DEVICE-AWARE: the connected device's fader VECTOR LENGTH
    (Pro II 9, Duo 10), falling back to the Pro II FADER_COUNT when offline/unknown.

    IT IS `vector_length` AND NOT `count`, and reading `count` here broke RESET FADERS on a Duo
    (found, live, with fader_write already on). The two differ only on the Duo:
    it EXPOSES 9 faders and we are willing to WRITE 10 slots, because its factory default layout
    genuinely occupies all ten. So every caller that walks `default_vector` or a saved profile - the
    reset, and profiles._apply_v2 - handed index 9 to a bound of 9 and got a ValueError, which
    action_reset_faders reported as "Could not reset the faders" and profile restore as a failed
    restore. Writing slot 9 is itself proven safe on hardware (set, vacate and restore,
    byte-clean). The docstring already said vector length; only the code disagreed.
    """
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

    RODE's own guide is explicit about the two halves of this, and both matter to the label:
    it applies to MULTITRACK ONLY, and the mixed track is never affected - it always carries the
    processing. So with multitrack Off this setting changes nothing you can hear.

    Proven writable: written on the confirmed
    OUTPUT block, read back, restored, model byte-length unchanged."""
    return send_record(build_global_prop_bool("recordingProcessingBypass", on, SEL_OUTPUT_GLOBAL))


def set_usb_multitrack_mode(mode: int) -> int:
    """outputMultiMode: what the MAIN USB device sends this computer. 0=Off, 1=Pre-Fader,
    2=Post-Fader, the same three the recorder uses (RODE presents both under one Multitrack
    screen, one section per destination).

    **TAKES THE COMPOSED VALUE, 0-5.** The property also carries the USB 1 INPUT mode in the same
    int: +3 means Expanded. Compose with `recorder.join_usb_multitrack` rather than passing a bare
    output mode, or a desk in Expanded is silently dropped back to Standard and unassigns every
    extended input on a fader.

    **Switching this RE-ENUMERATES the desk's whole USB audio function** - measured:
    all four endpoints drop for a second or two and Windows moves the default device. Every caller
    must say so before doing it, and anything that flips this to reach an endpoint must restore the
    previous mode afterwards.

    **IT RETURNS ONLY WHEN THE DESK IS BACK**, which is why this is not a bare send_record. The
    desk is off the bus about 60 ms after the write, so anything sent in the next couple of seconds
    is simply lost: a profile restore measured 27 settings failing with "no control interface", and
    told the user their desk was unplugged. See `rcp_hid.wait_for_reenumeration`; a write the desk
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


SEL_MASTER_PROC = 0x0E              # master output-processing block (the Compellor)


def set_master_compellor(on: bool) -> int:
    """Toggle the master output Compellor (the output compressor / leveler). Global bool at
    selector 0x0E. Decoded by capturing the RODE app's Device Configuration >
    Output > Processing > Master Compellor power toggle (captures compellor_on/off.pcap); our
    build_global_prop_bool reproduces the app's write byte-for-byte. Sent in normal mode like the
    other global toggles. (NB: speech that pumps up and down is the pad AUTODUCK, not this - see
    duckerDepth / per-pad autoduck; the Compellor is a master-output control.)"""
    return send_record(build_global_prop_bool("masterCompellorOn", on, SEL_MASTER_PROC))


# Master Compellor PARAMS - NORMALISED 0..1 floats on the master block 0x0E (the UI shows dB/ms but
# stores normalised; e.g. factory Threshold/Attack/Release/Gain = 0.67/0.50/0.38/0.22). Decoded
# by dragging each knob; build_global_prop_float reproduces the writes.
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
    """Output Delay (Outputs > Processing > Output Delay; for A/V sync in livestreaming). Enable via
    masterDelayOn (bool) and set the time via masterDelaySeconds (float SECONDS, 0..2.0), both on the
    master block 0x0E. Decoded. If `seconds` is given it is written after the enable."""
    n = send_record(build_global_prop_bool("masterDelayOn", on, SEL_MASTER_PROC))
    if seconds is not None:
        n += send_record(build_global_prop_float("masterDelaySeconds", SEL_MASTER_PROC,
                                                 max(0.0, min(2.0, float(seconds)))))
    return n


# ----------------------------------------------------------------------------
# VIRTUAL FADER LEVEL + MONITOR LEVEL - the two levels the desk otherwise keeps to itself.
# PROVEN ON HARDWARE (written, read back, restored): see RODECASTER_PROTOCOL.txt 6.1.
#
# faderLevel needs an ADDRESS LAYOUT OF ITS OWN. Not the global `01 01 01 01 <sel>` and not the
# channel-address `01 01 01 02 <sel> <trailer>` - both were tried first and silently ignored. What
# works is mirroring the record the DEVICE ITSELF pushes when the encoder moves a fader:
#     01 01 02 00 01 <selector>  faderLevel 00  01 05 01 <int32 LE>
# Selectors are 0x04 + fader index, and only the VIRTUAL faders are ours to write (the physical ones
# are analog pots). They live per-device in devices.FaderSpec.level_selectors, all observed on the
# wire; server.py resolves them, so no caller passes a guessed selector here.
FADER_LEVEL_MAX = 127            # faderMax; faderLevel / 127 IS that source's cross-point level
SEL_MONITOR_LEVEL = 0x0D         # outputMonLevel, an ordinary global float (observed + verified)


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
    """Set the monitor speaker level, 0.0..1.0 (outputMonLevel). The desk's own encoder writes the
    same property; this was listed as permanently unwritable until the selector turned up in a
    device push."""
    v = max(0.0, min(1.0, float(level01)))
    return send_record(build_global_prop_float("outputMonLevel", SEL_MONITOR_LEVEL, v))


def set_headphone_type(value: int, selector: int = SEL_HEADPHONE_TYPE) -> int:
    """headphoneType amp profile: 0=NTH-100, 1=High Sensitivity (louder), 2=Low Sensitivity.
    `selector` picks the headphone output: 0x09=Headphone 1 (default), 0x0A=Headphone 2. Both are
    KNOWN headphoneType selectors (GAP-6); the server resolves the selector from the active device's
    HeadphoneTypeSpec, so callers never pass an unmapped selector here."""
    return send_record(build_global_prop_int("headphoneType", selector, value))


# ----------------------------------------------------------------------------
# UNIT SETTINGS - the display, haptics, REC button, listen mode and fixed output level.
#
# NINE WRITE SELECTORS CAPTURED AND READ-BACK VERIFIED ON HARDWARE: one USBPcap capture
# per control while self-driving the RODE app, then each property written from OUR code and read
# back on the live Pro II. All nine correct, and the desk was byte-identical to its pre-capture
# baseline afterwards (0 non-telemetry properties changed). Written up in
# RODECASTER_PROTOCOL.txt section 11.1.
#
# THE BLOCK RULE, which is the important part: a selector addresses a BLOCK of settings and the
# property NAME picks the field inside it. Three blocks cover all nine - 0x07 GUI (the display),
# 0x0D OUTPUT (which already carried multitrack, the auto-mutes and outputMonLevel), and 0x0F
# SYSTEM (which already carried systemMidiControl and transferModeType). So these are ordinary
# global property writes on selectors we had already proven; only the pairing was unknown.
#
# activeButtonsBrightness is the ONE entry here that was never captured: the RODE app's Buttons
# slider writes only the INACTIVE one, and no app control for it exists. It was written on the
# DERIVED GUI selector 0x07 (the header-to-selector rule, DevTools/selector_map.py) and read back
# 180 as written, then restored to 255 - the predictive test that proved the derivation rule. Same
# block as the other four display settings, which have confirmed members, so it satisfies the
# "confirmed member in the same block" condition for a derived write.
SEL_GUI = 0x07                   # GUI block: the display and button brightness settings
SEL_SYSTEM = 0x0F                # SYSTEM block: haptics, REC button (also systemMidiControl)

# prop -> (selector, kind). `kind` picks the record builder: "bool" is the compact bool, "int" the
# 4-byte little-endian int. Values are NOT range-checked here; the server owns the value domain (it
# is UI copy as much as protocol) and this table is deliberately just the address.
UNIT_SETTING_SEL = {
    # OUTPUT block (0x0D) - captured
    "outputPrefader": (SEL_OUTPUT_GLOBAL, "bool"),          # False = After-fader Listen
    "outputMonFixed": (SEL_OUTPUT_GLOBAL, "bool"),          # Fixed Output Level
    # GUI block (0x07) - captured, except activeButtonsBrightness (derived, read-back proven)
    "metering": (SEL_GUI, "int"),                           # 0 Default / 1 Broadcast meter style
    "screenBrightness": (SEL_GUI, "int"),                   # 0..255
    "autoBrightness": (SEL_GUI, "bool"),
    "screenDimAfterSeconds": (SEL_GUI, "int"),              # seconds; 0 = never
    "inactiveButtonsBrightness": (SEL_GUI, "int"),          # 0..255
    "activeButtonsBrightness": (SEL_GUI, "int"),            # 0..255 (derived selector, see above)
    # SYSTEM block (0x0F) - captured
    "systemHapticSetting": (SEL_SYSTEM, "int"),             # 0 none / 1 on hold / 2 always
    "systemRecButtonSetting": (SEL_SYSTEM, "int"),          # 0 press to pause / 1 press to stop
    # ---- Selector DERIVED from the block rule, then PROVEN by writing each one,
    # reading it back off the desk, restoring it and confirming the model came back the same length
    # with the same occurrence counts (DevTools/probe_derived_write.py). Not one of these has a
    # capture behind it, and several have no control in the RODE app at all - which is the whole
    # point: "no app control" means no capture route, not unwritable. Five properties had already
    # made that point before these (outputMonLevel, activeButtonsBrightness, channelOutputMute,
    # channelCueEnable, eq*Q), so this is a rule now rather than a run of luck.
    # OUTPUT block (0x0D)
    "outputMonMute": (SEL_OUTPUT_GLOBAL, "bool"),           # mute the monitor SPEAKERS
    "outputBTMute": (SEL_OUTPUT_GLOBAL, "bool"),            # mute what a paired phone hears
    "outputBTLevel": (SEL_OUTPUT_GLOBAL, "float"),          # 0..1, the Bluetooth OUTPUT level
    # SYSTEM block (0x0F): HOW THE CLOCK IS SHOWN, not what time it is. The time itself is not
    # writable from here at all - the unit sets its own over the network - so the only one of these
    # that changes anything a recording carries is daylight saving, which the unit will NOT do for
    # itself and which stamps every take an hour out until somebody changes it.
    "systemDateTime24h": (SEL_SYSTEM, "bool"),              # 24-hour clock vs 12-hour
    "systemDateTimeDaylightSavings": (SEL_SYSTEM, "bool"),
    "systemDateTimeOnHome": (SEL_SYSTEM, "bool"),           # show the clock on the home screen
    # IN THIS TABLE FOR PROFILE SAVE/RESTORE ONLY, and DELIBERATELY NOT ON THE DEVICE PAGE: it is
    # measured display-only (three indices, three takes, one UTC second) and the desk's own Time
    # Zone field would not open either, so offering it would be a control that lies. The reason is
    # kept where it can be read next to the decision, in server.UNIT_SETTINGS_NOT_ON_PAGE.
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
    a factory reset clears (btDoScan on a predicted NETWORK selector)."""
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


# ----------------------------------------------------------------------------
# CHANNEL PROCESSING (EQ, compressor, gate, de-esser, HPF, pan) - session 14.
#
# Decoded + ear-verified + read-back-verified from captures (eqfreq, gainsweep,
# harvest). The RODE app drives the whole channel-strip DSP over report-3 with ONE
# format: pick the channel with the global int systemChannelSelected, then write
# each parameter as a GLOBAL record on selector 0x1c whose value is a NORMALISED
# 64-bit float (0.0..1.0, value-type byte 0x04). The property NAME alone selects
# which parameter. The unit maps 0..1 to the real dB/Hz/ms.
#   01 01 01 01 1c  <propName> 00  01 09 04  <float64 LE>
# Earlier channel-address attempts failed because they used int/bool and the wrong
# address; this is the format the app actually uses, and it CHANGES LIVE AUDIO.
# ----------------------------------------------------------------------------

# Processing selector model (SEL_CHANNEL_PROC, proc_selector, PROCESSING_PROPS) (moved to channels.py - the single source of truth for channel + processing ops)


def build_global_prop_float(name: str, selector: int, value: float) -> bytes:
    """Build a global report-3 record carrying a 64-bit float property.

    Value block 01 09 04 <8-byte double LE> (value-type 0x04). Byte-for-byte the
    format the RODE app uses for every channel-processing parameter.
    """
    name_b = name.encode("ascii")
    value_block = bytes([0x01, 0x09, 0x04]) + struct.pack("<d", float(value))
    payload = build_address([selector]) + name_b + b"\x00" + value_block
    return _frame(payload)


def send_records_paced(records: list[bytes], delay: float = 0.12) -> int:
    """Write all records on ONE open connection, pacing each by `delay` seconds.

    The RODE app sends a pad-create block (select-begin, handle binding, then the
    config records) on a single continuous HID session. send_record opens/closes
    per record, which drops the edit-handle binding between records; this keeps the
    session open so the whole block commits as one edit, and the delay gives the
    device time to process each record instead of overrunning its queue.
    """
    import time
    rcp_hid.refuse_write_in_tests()
    iface = find_control_interface()
    if iface is None:
        raise RuntimeError("no control interface (is the unit connected? close the RODE app)")
    dev = rcp_hid.open_handle(iface, "paced write")
    n = 0
    try:
        for r in records:
            diagnostics.command(r, "paced write")
            n += dev.write(r)
            if delay:
                time.sleep(delay)
    finally:
        rcp_hid.close_handle(dev, "paced write")
        rcp_hid.bump_write_generation()   # device state changed: drop any cached model read
    return n



# Channel processing setters (params/modules/enable/effect/level) + OUTPUT_MIX_MINUS (moved to channels.py - the single source of truth for channel + processing ops)


def send_record(rec: bytes) -> int:
    """Open the control interface, write one record, close. Returns bytes written."""
    rcp_hid.refuse_write_in_tests()
    iface = find_control_interface()
    if iface is None:
        raise RuntimeError("no control interface (is the unit connected?)")
    dev = rcp_hid.open_handle(iface, "property write")
    try:
        diagnostics.command(rec, "send_record")
        return dev.write(rec)
    finally:
        rcp_hid.close_handle(dev, "property write")
        rcp_hid.bump_write_generation()   # device state changed: drop any cached model read


# Captured verbatim from the RODE app (sound4.pcap). The "mount the FAT pad
# storage volume" command; replayed exactly rather than reconstructed.
REMOUNT_PAD_STORAGE = bytes.fromhex(
    "031a000000010101010f72656d6f756e7450616453746f726167650001010200"
).ljust(BULK_LEN, b"\x00")


# ---- HOLDING ONE TRANSFER SESSION ACROSS MANY PADS -------------------------------------------
# A pad create enters and leaves transfer mode for itself, which is right for ONE pad and wasteful
# for sixteen: a profile restore paid an enter, a remount and an exit per pad, and every one of those
# cycles drops the desk's audio and pops a Windows device sound. Measured: about
# 11 s of the 15 s per pad was overhead, and this is the larger half of it.
#
# A HOLD IS A PROMISE THAT SOMEBODY ELSE OWNS THE SESSION. While held, set_transfer_mode(False) is
# ignored, so the per-pad code can keep its own enter/exit calls exactly as they are and simply stop
# taking effect. The holder enters once at the top and exits once in a `finally`, so a crash mid-way
# leaves the desk mounted for as long as it takes to notice - which is why the exit is unconditional
# and not conditional on success. `backup_pads` has held one session for the whole grid since it was
# written; this gives the write path the same shape.
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

    def __exit__(self, *exc):
        global _transfer_hold
        _transfer_hold -= 1
        if _transfer_hold <= 0:
            _transfer_hold = 0
            _set_transfer_mode_raw(False)
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
    """Enter (value 2) or exit (value 0) Transfer Mode.

    Confirmed from captures: the app enters with transferModeType=2 (NOT 1) and
    exits with 0.

    WHILE A SESSION IS HELD (see transfer_session) this does nothing: the batch owns the session,
    and a per-pad exit in the middle of it would unmount the volume the next pad is about to write.
    """
    if _transfer_hold > 0:
        return 0
    return _set_transfer_mode_raw(on)


def remount_pad_storage() -> int:
    """Tell the unit to (re)mount its pad-storage FAT volume (makes F: appear)."""
    return send_record(REMOUNT_PAD_STORAGE)


def flush_volume(root: str) -> None:
    """Flush Windows' write cache for the pad-storage volume to the device.

    os.sync() does NOT exist on Windows, so a copied file can sit in cache and never reach the unit
    before we exit transfer mode.

    DONE WITH `FlushFileBuffers` ON A VOLUME HANDLE, not by launching PowerShell.
    `Write-VolumeCache` is the documented way and it works, but starting a PowerShell process costs
    1.2 to 2.3 SECONDS on every pad (measured), the single biggest item in a restore after the
    transfer-mode cycling. The Win32 call is the same operation without the process.
    Opening the volume's DOS device path for write and calling FlushFileBuffers on it is what
    Write-VolumeCache does underneath.

    THE FAILURE MODE IS SILENT AND EXPENSIVE, which is why the PowerShell path is KEPT as a
    fallback: a flush that does nothing leaves a pad's audio in Windows' cache, the desk gets a
    truncated file, and nobody finds out until the pad plays. Proven by writing a pad, leaving
    transfer mode, re-entering, reading the file back off the volume and comparing its md5 with the
    source.
    """
    import ctypes
    letter = root[0]
    B1, B2 = chr(92), chr(92) * 2
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ_WRITE = 0x00000001 | 0x00000002
    OPEN_EXISTING = 3
    INVALID_HANDLE = ctypes.c_void_p(-1).value
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # The DOS device path for a volume is two backslashes, a dot, a backslash, the letter.
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.FlushFileBuffers.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        device = B2 + "." + B1 + letter + ":"
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
    """The original flush: correct, and about two seconds per call. Kept as the fallback."""
    import os
    import subprocess
    letter = root[0]
    # CREATE_NO_WINDOW: the server runs under pythonw (no console), so without this each call would
    # flash a PowerShell window that steals focus - very visible during a multi-pad restore.
    no_window = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", f"Write-VolumeCache {letter}"],
        check=False,
        capture_output=True,
        creationflags=no_window,
    )


def find_existing_pad_wav(root: str, pad_n: int):
    """Return the single WAV path the firmware already plays for pad <pad_n>, or None.

    The unit plays the file its config references BY NAME inside F:\\pads\\<n>\\.
    Pads normally hold exactly one .wav; we replace its bytes to change the sound.
    """
    import glob
    import os
    pad_dir = os.path.join(root, "pads", str(pad_n))
    wavs = glob.glob(os.path.join(pad_dir, "*.wav"))
    return wavs[0] if len(wavs) == 1 else (wavs if wavs else None)


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
            import ctypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            old = ctypes.c_uint(0)
            if k32.SetThreadErrorMode(self._SEM_FAILCRITICALERRORS, ctypes.byref(old)):
                self._old = old.value
                self._k32 = k32
        except Exception:
            pass          # not Windows, or the call is unavailable: the dialogs are the old cost
        return self

    def __exit__(self, *exc):
        if self._old is not None:
            try:
                import ctypes
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
    import os
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
    """Wait for the pad-storage FAT volume to mount; return its root path (e.g. 'F:\\').

    The unit shows up as a removable drive with a recognisable size (~5033 MB) and
    a \\pads folder. The re-arm can be slow (observed > 8 s after a fresh enter),
    so the timeout is generous and we re-issue remountPadStorage periodically in
    case the first one was swallowed by the re-arm quirk. Returns None if the
    volume never appears within timeout_s.

    IMPORTANT: isdir(F:\\pads) alone is NOT enough. After a stuck
    transfer the drive letter can linger as a size-0 PHANTOM (FileSystemType
    Unknown) that passes isdir but fails every write with EINVAL - this is what
    truncated Music_Bed in the old overwrite path. We require a GENUINELY mounted
    volume (shutil.disk_usage(root).total > 0), so a create either gets a real
    volume or aborts cleanly before any pad record is sent.

    AND THOSE TWO CHECKS ARE STILL NOT ENOUGH. The first transfer-mode entry after the desk has
    been idle passes BOTH and then fails on the very first file read with EINVAL, killing a pad
    backup at pad 1 and popping two modal Windows dialogs; the same call succeeds on the retry, so
    the volume is announced before it is readable. The gate READS BYTES off the volume
    (pad_volume_readable), which is what the
    caller does next; a not-yet-ready volume just keeps the wait loop going, which is the recovery
    this function was already built for.
    """
    import os
    import shutil
    import string
    import time
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


def replace_pad_sound(pad_n: int, wav_path: str) -> str:
    """Change a configured pad's sound by OVERWRITING the WAV it already plays.

    Proven on hardware. The firmware plays the file its config
    references by name inside F:\\pads\\<pad_n>\\; we keep that filename and swap
    the bytes. Sequence: enter transfer mode -> wait for the FAT volume -> find
    the pad's existing WAV -> overwrite its bytes with wav_path -> flush the
    volume cache to the device -> exit transfer mode.

    Requires the pad to ALREADY have exactly one .wav (i.e. a sound assigned in
    the app at least once). It does NOT work on an empty/unconfigured pad and does
    not change the displayed name. WAV must be 48 kHz mono 16-bit PCM.

    Returns a status string. On any failure it exits transfer mode cleanly first.
    """
    import os
    import shutil
    import time
    if not os.path.isfile(wav_path):
        raise FileNotFoundError(wav_path)

    print(f"  enter transfer mode (replace pad {pad_n} sound)")
    set_transfer_mode(True)
    time.sleep(1.0)
    remount_pad_storage()

    root = find_pad_volume()
    if root is None:
        set_transfer_mode(False)
        raise RuntimeError("pad-storage volume did not mount; aborted, exited cleanly")

    existing = find_existing_pad_wav(root, pad_n)
    if existing is None:
        set_transfer_mode(False)
        raise RuntimeError(
            f"pad {pad_n} has no assigned WAV to overwrite (assign one in the app "
            "first, or this pad has no folder); aborted, exited cleanly"
        )
    if isinstance(existing, list):
        set_transfer_mode(False)
        raise RuntimeError(
            f"pad {pad_n} has multiple WAVs {[os.path.basename(p) for p in existing]}; "
            "ambiguous which one plays - resolve in the app first; exited cleanly"
        )

    print(f"  overwrite -> {existing}  (keeping name {os.path.basename(existing)})")
    shutil.copyfile(wav_path, existing)
    flush_volume(root)
    time.sleep(0.5)

    print("  exit transfer mode")
    set_transfer_mode(False)
    return f"replaced pad {pad_n} sound (file {os.path.basename(existing)})"


def _selftest() -> None:
    """Prove the builders reproduce the RODE app's captured bytes, no hardware."""
    # padFilePath assignment, sound4.pcap frame 167 (pad 2 -> sound.wav).
    expected = bytes.fromhex(
        "033f000000010102029a02010170616446696c6550617468000129052f4170706c"
        "69636174696f6e2f656d6d632d646174612f706164732f322f736f756e642e7761"
        "7600"
    ).ljust(BULK_LEN, b"\x00")
    got = build_pad_prop_string(
        "padFilePath", PAD_FILE_PATH_FMT.format(n=2, name="sound.wav")
    )
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

    # --- DUO T2/T3 additions: polarity + mic-type (channel form) and the two Mic1
    # processing/global-form props (channelBypassProcessing + channelCurrentFxPreset).
    # All FOUR are byte-for-byte from captured console pcaps (duo_phaseflip / duo_mt_condenser /
    # duo_procbypass / duo_preset).
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
    # DUO: NON-Mic1 processing selectors CONFIRMED by console capture
    # (the duo_proc_<chan>.pcap captures).
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
    # DUO: EFFECTS on a NON-Mic1 strip CONFIRMED by console capture
    # (captures/duo_fx_mic2_strip1.pcap). echoOn rides the GLOBAL/effects form at
    # sel = fx_sel_base (0x26) + STRIP index; Mic2 sits on strip 1 -> selector 0x27.
    # Both On and Off writes captured; our builder reproduces them byte-for-byte.
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

    # DUO: MASTER / GLOBAL OUTPUT writes CONFIRMED by self-driven console capture
    # (captures/duo_outwrite.pcap). All selectors are byte-identical to the Pro II, so the same
    # build_global_prop_* used for both desks reproduces the app's writes exactly. These lock in the
    # "output_write" cap: recordingMultitrackMode 0x0D (int), outputBTAutoMute 0x0D (bool),
    # masterCompellorOn 0x0E (bool), headphoneType 0x09 (Headphone 1) / 0x0A (Headphone 2) (int).
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

    print("selftest OK: builders match captured app bytes "
          "(frames 9, 167, padIdx, midi, gain, p48; "
          "Duo p48/gain/digital/phaseflip/mic-type/bypass/preset; "
          "Duo processing strip-selectors 0x1C-0x24; Duo fx strip-selector 0x27; "
          "Duo output-write 0x0D/0x0E/0x09/0x0A; cross-point address width 1 and 2 bytes)")


if __name__ == "__main__":
    import sys
    import time
    arg = sys.argv[1] if len(sys.argv) > 1 else "enter"
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
    elif arg == "replace":
        # replace <pad_n> <wav_path> : overwrite the pad's existing sound (proven method)
        if len(sys.argv) < 4:
            print("usage: rcp_transfer.py replace <pad_n> <wav_path>")
            sys.exit(2)
        pad_n = int(sys.argv[2])
        wav = sys.argv[3]
        print(replace_pad_sound(pad_n, wav))
    elif arg == "midi":
        # midi on|off : enable/disable MIDI control (no transfer mode needed)
        on = (len(sys.argv) > 2 and sys.argv[2].lower() in ("on", "1", "true", "enable"))
        print(f"systemMidiControl = {'ON' if on else 'OFF'} ...")
        print("  wrote", set_midi_control(on))
    else:
        print(f"unknown command: {arg}")
        print("commands: enter | exit | remount | selftest | replace <n> <wav>")
