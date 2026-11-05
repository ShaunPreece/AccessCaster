"""Duo REGRESSION guard for everything the live-state work added. NO DEVICE, no HID.

WHY THIS EXISTS. Every feature in this app is reverse-engineered on the Pro II, live-state work
included: mute/solo state announcements, spoken fader positions, the encoder ring, channel-button
selection, virtual-fader + monitor level WRITES, chain-follows-channel. Duo users depend on the same
build and are blind, so a Pro II-shaped constant that slips through does not produce a visible
glitch on their desk - it produces a wrong spoken name, or a write to an unmapped selector, which is
the stray-append hazard that costs a factory reset.

So this file asserts, with the Duo device model forced active and no desk attached, that each
feature is EITHER device-derived (and correct for the Duo's 4 physical / 9 total faders, its 10-strip
vector, and source code 2 = Headset, not Mic 3) OR safely refused behind a capability flag. It is the
offline half of Duo support; the rest needs a Duo (`test_duo_caps_live.py`).

Run: python DevTools/test_duo_parity.py
"""
from __future__ import annotations

import sys

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
import announcer
import change_words
import channels
import desk_labels
import device_page
import devices
import rcp_read
import rcp_transfer
import server
import speech
from _testkit import as_device, check, check_true, hold_desk, hold_layout, push

SPOKEN: list[str] = []
speech.say = lambda text: SPOKEN.append(text)          # capture instead of speak

# The Duo's CONFIRMED factory layout (devices.DUO_FADERS.default_vector): 10 slots, source 2 =
# Headset on strip 4, an empty strip at 8 (-1) and USB 2 mirrored into the spare slot 9.
DUO_VEC = [0, 1, 10, 11, 2, 7, 8, 9, -1, 9]


def state_packet(prop: str, strip_idx: int, on: bool) -> bytes:
    """A pushed STATE property on CHANNEL[strip index]."""
    return push(_testkit.addr(rcp_transfer.channel_key(strip_idx)), prop,
                bytes([0x01, 0x01, 0x02 if on else 0x03]))


def int_packet(prop: str, sel: int, value: int) -> bytes:
    """A pushed global int property."""
    return push([sel], prop, b"\x01\x05\x01" + value.to_bytes(4, "little", signed=True))


def run(pkts):
    SPOKEN.clear()
    st = {"pending": None, "at": 0.0}
    for p in pkts:
        announcer.handle_report(p, st)
    return list(SPOKEN), st


# ---------------------------------------------------------------------------------------------
print("ENCODER RING IS DERIVED FROM THE DEVICE, NOT HARDCODED")
# The ring is the faders with no physical pot, then the monitor. Pro II: 6 physical of 9, so
# faders 7/8/9 + monitor - which must match the desk's own ring order exactly.
with as_device(devices.PRO2):
    check("Pro II ring unchanged", announcer._encoder_ring(), announcer._ENCODER_RING_PRO2)
with as_device(devices.DUO):
    # Duo: 4 physical of 9, so five virtual faders (indices 4-8) then the monitor. The ORDER is
    # unobserved on that desk; what matters here is that it is not the Pro II's three.
    check("Duo ring covers its five virtual faders", announcer._encoder_ring(),
          (("fader", 4), ("fader", 5), ("fader", 6), ("fader", 7), ("fader", 8), ("monitor", 0)))

print("\nSPOKEN NAMES USE THE DUO'S OWN SOURCES (code 2 = Headset, NOT Mic 3)")
with as_device(devices.DUO):
    check("source label 2", change_words.source_name(2), "Headset")
    check("no Mic 3 to name", change_words.source_name(3), "Source 3")
    hold_desk(list(DUO_VEC))
    said, _ = run([state_packet("channelOutputMute", 4, True)])
    check("mute on the Headset strip", said, ["Headset muted"])
    said, _ = run([state_packet("channelCueEnable", 3, True)])
    check("solo on the SMART pads strip", said, ["SMART pads solo on"])
    said, _ = run([state_packet("channelOutputMute", 8, True)])
    check("an EMPTY strip (-1) falls back to a number", said, ["Channel 9 muted"])
    said, _ = run([state_packet("channelOutputMute", 9, True)])
    check("the spare slot 9 still names its source", said, ["USB 2 muted"])
with as_device(devices.PRO2):
    check("Pro II still says Mic 3 for the same code", change_words.source_name(2), "Mic 3")

print("\nCHANNEL-BUTTON SELECTION SPEAKS THE RIGHT THING ON THE DUO")
# systemChannelSelected on selector 0x0f. What the encoder then moves depends on the strip: a mic
# gives mic gain, a line strip its input level, and a bus its fader level - never 'nothing'.
with as_device(devices.DUO):
    hold_desk(list(DUO_VEC))
    said, _ = run([int_packet("systemChannelSelected", 0x0F, 0)])
    check("Mic 1 selected", said, ["Mic 1 selected. Encoder sets its gain"])
    said, _ = run([int_packet("systemChannelSelected", 0x0F, 4)])
    check("Headset selected (a LINE strip on the Duo)", said,
          ["Headset selected. Encoder sets its input level in decibels"])
    said, _ = run([int_packet("systemChannelSelected", 0x0F, 3)])
    check("SMART pads on the Duo's PHYSICAL fader 4", said,
          ["SMART pads selected. Fader level only"])
    # The SAME on fader 9, the empty virtual strip. The phrase deliberately does NOT vary with
    # physical vs virtual: a bus has a fader either way, and a message that branched on the layout
    # would be one more thing to keep in step for no gain to the listener.
    hold_desk([0, 1, 10, -1, 2, 7, 8, 9, 11, 9])
    said, _ = run([int_packet("systemChannelSelected", 0x0F, 8)])
    check("and the same on the Duo's VIRTUAL fader 9", said,
          ["SMART pads selected. Fader level only"])
    # An EMPTY strip really does have nothing, and is the only case that may say so.
    said, _ = run([int_packet("systemChannelSelected", 0x0F, 3)])
    check("the strip they came off is now empty and says so", said,
          ["Channel 4 selected. Nothing here for the encoder to adjust"])
    hold_layout(list(DUO_VEC))
    said, _ = run([int_packet("systemChannelSelected", 0x0F, -1)])
    check("deselected", said, ["Virtual faders and monitor. Encoder changes their levels"])
    # Mic 2 on the Duo is combo-linked and is still a mic, so it must still say 'gain'.
    check("Duo Mic 2 is a mic channel",
          next(c['kind'] for c in devices.DUO.ui_channels if c['name'] == 'Mic2'), 'mic')

print("\nGAIN NAMES COME FROM THE ADDRESS THE CHANGE WAS PUSHED AT")
# The desk pushes a gain it moved itself at the address it owns, and the announcer names the channel
# from that. Both desks share ONE rule, so the same address means the same channel on each. Looking
# the low byte up in a per-device selector table instead is what leaves a gain turned ON THE DESK
# matching nothing and never spoken at all, because the Duo's table holds the dead MIX selectors.
with as_device(devices.DUO):
    check("Duo Mic 2 gain at INPUTSOURCE[1]", change_words.gain_name(rcp_read.push_key([0x26F]), True), "Mic 2 gain")
    check("Duo Headset level at INPUTSOURCE[2]",
          change_words.gain_name(rcp_read.push_key([0x270]), False), "Headset input level")
    check("the DEAD MIX address names nothing", change_words.gain_name(rcp_read.push_key([0x149]), True), None)
    check("a source this desk does not have names nothing (Pro II Mic 4)",
          change_words.gain_name(rcp_read.push_key([0x271]), True), None)
    check("a mic address asked as a line names nothing",
          change_words.gain_name(rcp_read.push_key([0x26F]), False), None)
    check("an unparseable address names nothing", change_words.gain_name(None, True), None)
with as_device(devices.PRO2):
    check("Pro II Mic 1 gain at INPUTSOURCE[0]", change_words.gain_name(rcp_read.push_key([0x26E]), True), "Mic 1 gain")
    check("Pro II Mic 4 gain at INPUTSOURCE[3]", change_words.gain_name(rcp_read.push_key([0x271]), True), "Mic 4 gain")

print("\nTHE PUSHED ADDRESS IS READ IN FULL, NOT JUST ITS LOW BYTE")
# 0x026E needs two bytes. Reading only the low one calls it selector 0x6E, which is an ordinary
# one-byte selector elsewhere in the protocol, so the width has to come from the length byte.
for want, addr in (([0x26E], b"\x01\x01\x01\x02\x6e\x02"), ([0x6E], b"\x01\x01\x01\x01\x6e")):
    rec = addr + b"inputMicrophoneGain\x00\x01\x05\x01" + bytes(4)
    frame = (b"\x04" + len(rec).to_bytes(4, "little") + rec).ljust(256, b"\x00")
    check(f"address {addr.hex(' ')} decodes to {want}", rcp_read.parse_push(frame)[0], want)

print("\nONLY THE VIRTUAL FADERS HAVE A SETTABLE LEVEL")
# The Pro II has three virtual faders and the Duo FIVE, read off the tree and confirmed by the
# engine's own response. Their addresses are test_addressing's.
check("Pro II: faders 7 to 9", devices.PRO2.fader.level_faders, (6, 7, 8))
check("Duo: faders 5 to 9", devices.DUO.fader.level_faders, (4, 5, 6, 7, 8))
for _dev in (devices.PRO2, devices.DUO):
    check(f"{_dev.name}: no physical fader is writable",
          any(i < _dev.fader.physical for i in _dev.fader.level_faders), False)

print("\nLEVEL WRITES AND CHAIN-FOLLOW ARE ON FOR BOTH DESKS")
with as_device(devices.DUO):
    check("level writes are not gated", server._level_write_gate(), None)
    check("chain-follow is on", server._chain_follow_on(), True)
with as_device(devices.PRO2):
    check("Pro II level writes are not gated", server._level_write_gate(), None)
    check("Pro II chain-follow on", server._chain_follow_on(), True)

print("\nCAPS AND SHAPES THE DUO WORK ALREADY SETTLED (do not regress these)")
check("Duo fader count", (devices.DUO.fader.count, devices.DUO.fader.physical), (9, 4))
check("Duo writes 10 vector slots", devices.DUO.fader.vector_length, 10)
check("Duo default layout confirmed", devices.DUO.fader.default_vector is not None, True)
check("Duo default layout fills all 10 slots", len(devices.DUO.fader.default_vector), 10)
# THE BOUND IN set_fader_source IS vector_length, NOT count. RESET FADERS and PROFILE RESTORE both
# walk all ten slots, so a bound of `count` (9) refuses slot 9 on a Duo. Asserted by the REFUSAL at
# index 10, which raises before anything is sent, so this stays an offline test.
with as_device(devices.DUO):
    try:
        rcp_transfer.set_fader_source(10, 0)
        check("set_fader_source refuses fader index 10 on a Duo", "no error", "ValueError")
    except ValueError as e:
        check("set_fader_source bound is the Duo's 10 vector slots, not its 9 faders",
              "0-9" in str(e), True)
check("Duo pads are 6 per bank", devices.DUO.pad.pads_per_bank, 6)
# 390 is the MIX BLOCK, which is the grid, and it is the same on both desks and both firmwares.
# RCSYNCMIX reuses all six cross-point names and follows MIX in the stream, so an unscoped read
# returns the two concatenated, a total that changes with the firmware.
check("Duo matrix guard counts the MIX block alone",
      (devices.DUO.matrix.cell_count, devices.DUO.matrix.row_stride), (390, 13))
for cap in ("input_mic_write", "processing", "channel_read", "matrix_read", "matrix_write",
            "output_write", "pad_write", "fader_write", "unit_settings", "level_write",
            "chain_follow", "stereo_link", "network_write", "recorder", "shows"):
    check(f"Duo cap {cap}", cap in devices.DUO.caps, True)
# THE DUO CLAIMS EVERY CAP THE PRO II DOES BUT ONE, and that one is listed here by name so a new
# Pro II cap still cannot silently skip the Duo.
# `recorder` and `shows` are on by the TREE-then-ENGINE route with the hardware missing: the
# recorder CONSUMES a record request (255 -> 3) and SHOWCONTROL answers an impossible export with
# error 8 in one second. Read the comments on both caps in devices.py before treating either as
# witnessed - an engine answering proves an ADDRESS is live, never that a FEATURE works, and nobody
# has made a recording or a show on a Duo.
# `storage_write` is OFF: no Duo here has had a card or drive in it.
DUO_CAPS_OFF = ["storage_write"]
check("the Duo claims every cap the Pro II does, bar the ones named off",
      sorted(devices.PRO2.caps - devices.DUO.caps), DUO_CAPS_OFF)
# The Duo has ONE legal pair where the Pro II has three, and that is a property of the INPUTS, not
# of how much has been proven: the Duo's third input is a HEADSET jack, so "2 + 3" and "3 + 4" are
# not pairs anyone would want. A device claiming the cap with an EMPTY table still refuses
# (server._stereo_link_gate checks both), which is what protects a future desk from a copy-paste.
check("Duo has exactly one stereo pair, Mic 1 + 2",
      devices.DUO.fader.stereo_pairs, ((4, 0, 1, "Mic 1 + 2"),))
check("Pro II still has its three", len(devices.PRO2.fader.stereo_pairs), 3)

print("\nTHE UNIT'S OWN SETTINGS: WRITTEN ON BOTH DESKS")
# The Duo's unit_settings cap rests on the desk's own TREE rather than on a capture: all 20
# selectors in UNIT_SETTING_SEL resolve to a node of the right block carrying the property, and a
# write/read-back/restore runs in each of the three blocks. DevTools/test_duo_caps_live.py re-proves
# that against real hardware; this offline half pins the DECISION only.
for dev in (devices.DUO, devices.PRO2):
    with as_device(dev):
        check(f"{dev.name} has the cap", "unit_settings" in dev.caps, True)
        check(f"{dev.name}: an unknown property is still refused",
              device_page.action_set_unit_setting("notASetting", 1)["ok"], False)
check_true("every setting the page offers has an address in the table",
           all(s["prop"] in rcp_transfer.UNIT_SETTING_SEL for s in desk_labels.UNIT_SETTINGS))

# A CHANNEL THE DESK DOES NOT CARRY REFUSES, with no fixed fallback strip. Mic 3 from a Pro II
# profile would otherwise land on Duo strip 2, which holds Bluetooth. Raises before any dump.
with as_device(devices.DUO):
    for ident in ("Mic3", 3, "Mic4", 4):
        try:
            channels.proc_key(ident)
            check(f"Duo refuses a chain write to {ident!r}", "no error", "ValueError")
        except ValueError:
            check(f"Duo refuses a chain write to {ident!r}", "ValueError", "ValueError")

print()
sys.exit(_testkit.finish())
