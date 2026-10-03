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
offline half of ProjectMemory/DUO_SUPPORT.md (part A1); the rest still needs a Duo.

Run: python DevTools/test_duo_parity.py
"""
from __future__ import annotations
import re as _re
import sys

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import channels
import devices
import rcp_transfer
import server

SPOKEN: list[str] = []
server._ann_say = lambda text: SPOKEN.append(text)          # capture instead of speak

FAILED = 0
# The Duo's CONFIRMED factory layout (devices.DUO_FADERS.default_vector): 10 slots, source 2 =
# Headset on strip 4, an empty strip at 8 (-1) and USB 2 mirrored into the spare slot 9.
DUO_VEC = [0, 1, 10, 11, 2, 7, 8, 9, -1, 9]


def check(label, got, want):
    global FAILED
    if got == want:
        print(f"  ok   {label}: {got!r}")
    else:
        FAILED += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def check_true(label, got):
    check(label, bool(got), True)


class as_device:
    """Force the ACTIVE device model, so device-aware code can be tested with no desk attached."""

    def __init__(self, dev):
        self.dev = dev

    def __enter__(self):
        self.saved = devices.get_active_device
        devices.get_active_device = lambda: self.dev
        return self.dev

    def __exit__(self, *exc):
        devices.get_active_device = self.saved
        return False


def state_packet(prop: str, strip_idx: int, on: bool) -> bytes:
    """A pushed STATE property (see test_announcer.state_packet): sel = 0x1C + strip index."""
    return (bytes([0x04]) + b"\x00\x00\x00" + b"\x01\x01\x01\x01"
            + bytes([channels.SEL_CHANNEL_PROC + strip_idx]) + prop.encode()
            + b"\x00\x01\x01" + bytes([0x02 if on else 0x03]))


def int_packet(prop: str, sel: int, value: int) -> bytes:
    """A pushed global int property: 01 01 01 01 <sel> <name> 00 01 05 01 <int32 LE>."""
    return (bytes([0x04]) + b"\x00\x00\x00" + b"\x01\x01\x01\x01" + bytes([sel])
            + prop.encode() + b"\x00\x01\x05\x01" + value.to_bytes(4, "little", signed=True))


def run(pkts):
    SPOKEN.clear()
    st = {"bank": None, "pending": None, "at": 0.0}
    for p in pkts:
        server._ann_handle_report(p, st)
    return list(SPOKEN), st


# ---------------------------------------------------------------------------------------------
print("ENCODER RING IS DERIVED FROM THE DEVICE, NOT HARDCODED")
# The ring is the faders with no physical pot, then the monitor. Pro II: 6 physical of 9, so
# faders 7/8/9 + monitor - which must still match the ring OBSERVED on hardware byte for byte.
with as_device(devices.PRO2):
    check("Pro II ring unchanged", server._ann_encoder_ring(), server._ANN_ENCODER_RING_PRO2)
with as_device(devices.DUO):
    # Duo: 4 physical of 9, so five virtual faders (indices 4-8) then the monitor. The ORDER is
    # unobserved on that desk; what matters here is that it is not the Pro II's three.
    check("Duo ring covers its five virtual faders", server._ann_encoder_ring(),
          (("fader", 4), ("fader", 5), ("fader", 6), ("fader", 7), ("fader", 8), ("monitor", 0)))

print("\nSPOKEN NAMES USE THE DUO'S OWN SOURCES (code 2 = Headset, NOT Mic 3)")
with as_device(devices.DUO):
    check("source label 2", server._ann_source_label(2), "Headset")
    check("no Mic 3 to name", server._ann_source_label(3), "Source 3")
    channels.publish_strip_vector(list(DUO_VEC))
    said, _ = run([state_packet("channelOutputMute", 4, True)])
    check("mute on the Headset strip", said, ["Headset muted"])
    said, _ = run([state_packet("channelCueEnable", 3, True)])
    check("solo on the SMART pads strip", said, ["SMART pads solo on"])
    said, _ = run([state_packet("channelOutputMute", 8, True)])
    check("an EMPTY strip (-1) falls back to a number", said, ["Channel 9 muted"])
    said, _ = run([state_packet("channelOutputMute", 9, True)])
    check("the spare slot 9 still names its source", said, ["USB 2 muted"])
with as_device(devices.PRO2):
    check("Pro II still says Mic 3 for the same code", server._ann_source_label(2), "Mic 3")

print("\nCHANNEL-BUTTON SELECTION SPEAKS THE RIGHT THING ON THE DUO")
# systemChannelSelected on selector 0x0f. What the encoder then moves depends on the strip: a mic
# gives mic gain, a line strip its input level, and a bus its fader level - never 'nothing'.
with as_device(devices.DUO):
    channels.publish_strip_vector(list(DUO_VEC))
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
    channels.publish_strip_vector([0, 1, 10, -1, 2, 7, 8, 9, 11, 9])
    said, _ = run([int_packet("systemChannelSelected", 0x0F, 8)])
    check("and the same on the Duo's VIRTUAL fader 9", said,
          ["SMART pads selected. Fader level only"])
    # An EMPTY strip really does have nothing, and is the only case that may say so.
    said, _ = run([int_packet("systemChannelSelected", 0x0F, 3)])
    check("the strip they came off is now empty and says so", said,
          ["Channel 4 selected. Nothing here for the encoder to adjust"])
    channels.publish_strip_vector(list(DUO_VEC))
    said, _ = run([int_packet("systemChannelSelected", 0x0F, -1)])
    check("deselected", said, ["Levels. Encoder sets fader and monitor levels"])
    # Mic 2 on the Duo is combo-linked and is still a mic, so it must still say 'gain'.
    check("Duo Mic 2 is a mic channel",
          next(c['kind'] for c in devices.DUO.ui_channels if c['name'] == 'Mic2'), 'mic')

print("\nGAIN NAMES COME FROM THE ADDRESS THE CHANGE WAS PUSHED AT")
# The desk pushes a gain it moved itself at the address it owns, and the announcer names the channel
# from that. Both desks share ONE rule, so the same address means the same channel on each. Looking
# the low byte up in a per-device selector table instead is what leaves a gain turned ON THE DESK
# matching nothing and never spoken at all, because the Duo's table holds the dead MIX selectors.
with as_device(devices.DUO):
    check("Duo Mic 2 gain at INPUTSOURCE[1]", server._ann_gain_label([0x26F], True), "Mic 2 gain")
    check("Duo Headset level at INPUTSOURCE[2]",
          server._ann_gain_label([0x270], False), "Headset input level")
    check("the DEAD MIX address names nothing", server._ann_gain_label([0x149], True), None)
    check("a source this desk does not have names nothing (Pro II Mic 4)",
          server._ann_gain_label([0x271], True), None)
    check("a mic address asked as a line names nothing",
          server._ann_gain_label([0x26F], False), None)
    check("an unparseable address names nothing", server._ann_gain_label(None, True), None)
with as_device(devices.PRO2):
    check("Pro II Mic 1 gain at INPUTSOURCE[0]", server._ann_gain_label([0x26E], True), "Mic 1 gain")
    check("Pro II Mic 4 gain at INPUTSOURCE[3]", server._ann_gain_label([0x271], True), "Mic 4 gain")

print("\nTHE PUSHED ADDRESS IS READ IN FULL, NOT JUST ITS LOW BYTE")
# 0x026E needs two bytes. Reading only the low one calls it selector 0x6E, which is an ordinary
# one-byte selector elsewhere in the protocol, so the width has to come from the length byte.
for want, addr in (([0x26E], b"\x01\x01\x01\x02\x6e\x02"), ([0x6E], b"\x01\x01\x01\x01\x6e")):
    frame = addr + b"inputMicrophoneGain\x00"
    m = _re.search(b"inputMicrophoneGain", frame)
    check(f"address {addr.hex(' ')} decodes to {want}",
          server._ann_addr_path(frame, m, "inputMicrophoneGain"), want)

print("\nFADER LEVEL ADDRESSES ARE DERIVED, AND THE PRO II'S THREE ARE UNCHANGED")
# The retired Pro II table, asserted against the derivation that replaced it. These three values are
# the only evidence that turning this on for the Duo cannot have moved a byte on Shaun's own desk.
check("Pro II virtual faders still address 0x0a/0x0b/0x0c",
      dict(devices.PRO2.fader.level_selectors), {6: 0x0A, 7: 0x0B, 8: 0x0C})
# The Duo has FIVE virtual faders where the Pro II has three, so its set starts two lower. Read off
# the tree and confirmed by the engine's own response.
check("Duo virtual faders address 0x08..0x0c",
      dict(devices.DUO.fader.level_selectors), {4: 0x08, 5: 0x09, 6: 0x0A, 7: 0x0B, 8: 0x0C})
for _dev in (devices.PRO2, devices.DUO):
    _sel = _dev.fader.level_selectors
    check(f"{_dev.name}: only the VIRTUAL faders are writable",
          sorted(_sel), list(range(_dev.fader.physical, _dev.fader.count)))
    check(f"{_dev.name}: no physical fader has an address",
          any(i < _dev.fader.physical for i in _sel), False)

print("\nLEVEL WRITES ARE ALLOWED ON THE DUO NOW; THE REST STILL REFUSE")
with as_device(devices.DUO):
    check("level writes are no longer gated", server._level_write_gate(), None)
    check("chain-follow is on", server._chain_follow_on(), True)
with as_device(devices.PRO2):
    check("Pro II keeps all three", server._level_write_gate(), None)
    check("Pro II chain-follow on", server._chain_follow_on(), True)

print("\nCAPS AND SHAPES THE DUO WORK ALREADY SETTLED (do not regress these)")
check("Duo fader count", (devices.DUO.fader.count, devices.DUO.fader.physical), (9, 4))
check("Duo writes 10 vector slots", devices.DUO.fader.vector_length, 10)
check("Duo default layout confirmed", devices.DUO.fader.default_vector is not None, True)
check("Duo default layout fills all 10 slots", len(devices.DUO.fader.default_vector), 10)
# THE BOUND IN set_fader_source IS vector_length, NOT count, and this pins it because reading
# `count` broke RESET FADERS and PROFILE RESTORE on a Duo while fader_write was already on: both
# walk all ten slots, and slot 9 met a bound of 9. Asserted by the REFUSAL at index 10, which
# raises before anything is sent, so this stays an offline test. Found live.
try:
    import rcp_transfer as _rt
    _prev = devices.force_device(devices.DUO)
    try:
        _rt.set_fader_source(10, 0)
        check("set_fader_source refuses fader index 10 on a Duo", "no error", "ValueError")
    except ValueError as e:
        check("set_fader_source bound is the Duo's 10 vector slots, not its 9 faders",
              "0-9" in str(e), True)
finally:
    devices.force_device(_prev)
check("Duo pads are 6 per bank", devices.DUO.pad.pads_per_bank, 6)
# 390 is the MIX BLOCK, which is the grid, and it is the same on both desks and both firmwares. The
# old 516 counted MIX plus RCSYNCMIX, which reuses all six cross-point names and follows it in the
# stream, so an unscoped read returned the two concatenated - and 1.7.6 changed that total again.
check("Duo matrix guard counts the MIX block alone",
      (devices.DUO.matrix.cell_count, devices.DUO.matrix.row_stride), (390, 13))
check("processing/effects bases shared with the Pro II",
      (devices.DUO.proc_sel_base, devices.DUO.fx_sel_base),
      (devices.PRO2.proc_sel_base, devices.PRO2.fx_sel_base))
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
# `callme` is OFF: no call has ever been hosted or answered on a Duo.
# `storage_write` is OFF: no Duo here has had a card or drive in it.
DUO_CAPS_OFF = ["callme", "storage_write"]
check("the Duo claims every cap the Pro II does, bar the ones named off",
      sorted(devices.PRO2.caps - devices.DUO.caps), DUO_CAPS_OFF)
# The Duo has ONE legal pair where the Pro II has three, and that is a property of the INPUTS, not
# of how much has been proven: the Duo's third input is a HEADSET jack, so "2 + 3" and "3 + 4" are
# not pairs anyone would want. A device claiming the cap with an EMPTY table still refuses
# (server._stereo_link_gate checks both), which is what protects a future desk from a copy-paste.
check("Duo has exactly one stereo pair, Mic 1 + 2",
      devices.DUO.fader.stereo_pairs, ((4, 0, 1, "Mic 1 + 2"),))
check("Pro II still has its three", len(devices.PRO2.fader.stereo_pairs), 3)

print("\nTHE UNIT'S OWN SETTINGS: NOW WRITTEN ON BOTH DESKS (DUO-T10)")
# The Duo's unit_settings cap rests on the desk's own TREE rather than on a capture: all 20
# selectors in UNIT_SETTING_SEL resolve to a node of the right block carrying the property, and a
# write/read-back/restore runs in each of the three blocks. DevTools/test_duo_caps_live.py re-proves
# that against real hardware; this offline half pins the DECISION only.
for dev in (devices.DUO, devices.PRO2):
    with as_device(dev):
        check(f"{dev.name} has the cap", "unit_settings" in dev.caps, True)
        check(f"{dev.name}: an unknown property is still refused",
              server.action_set_unit_setting("notASetting", 1)["ok"], False)
check_true("every setting the page offers has an address in the table",
           all(s["prop"] in rcp_transfer.UNIT_SETTING_SEL for s in server.UNIT_SETTINGS))
check_true("every exposed setting has a confirmed address",
           all(s["prop"] in rcp_transfer.UNIT_SETTING_SEL for s in server.UNIT_SETTINGS))

print()
if FAILED:
    print(f"{FAILED} CHECK(S) FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
