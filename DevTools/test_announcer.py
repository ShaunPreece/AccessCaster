"""Unit tests for the control announcer's report-04 decoder. NO DEVICE, no HID, no writes.

Feeds synthetic report-04 packets (built to the shapes recorded on hardware,
including the exact selector bytes captured on hardware) into server._ann_handle_report with
_ann_say stubbed, and asserts what would be SPOKEN.

Run: python DevTools/test_announcer.py
"""
from __future__ import annotations
import sys
import time

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import devices
import server
import channels

# PIN THE DESK. This suite asserts Pro II strip names and spoken labels, so with a Duo attached it
# failed on the Duo's different channel set and looked like a regression. Offline tests must not
# depend on what happens to be plugged into the machine running them. The Duo's own announcer
# expectations live in DevTools/test_duo_parity.py.
devices.force_device(devices.PRO2)

SPOKEN: list[str] = []
server._ann_say = lambda text: SPOKEN.append(text)          # capture instead of speak

FAILED = 0


def check(label, got, want):
    global FAILED
    if got == want:
        print(f"  ok   {label}: {got!r}")
    else:
        FAILED += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def check_true(label, got):
    check(label, bool(got), True)


def state_packet(prop: str, strip_idx: int, on: bool) -> bytes:
    """A pushed STATE property: 01 01 01 01 <sel> <name> 00 01 01 <02 on | 03 off>.
    sel = SEL_CHANNEL_PROC + strip index, exactly as observed on hardware (mute on channel 2 -> 0x1D)."""
    sel = channels.SEL_CHANNEL_PROC + strip_idx
    return (bytes([0x04]) + b"\x00\x00\x00" + b"\x01\x01\x01\x01" + bytes([sel])
            + prop.encode() + b"\x00\x01\x01" + bytes([0x02 if on else 0x03]))


def press_packet(name: str, down: bool = True) -> bytes:
    """A press EDGE. The selector byte before the name is PRINTABLE, so it is absorbed into the
    name (buttons 1/2/3 read as cmutePressed / dmutePressed / emutePressed) - which is why the
    decoder matches on the suffix."""
    return (bytes([0x04]) + b"\x00\x00\x00\x01\x01\x02\x00" + name.encode()
            + b"\x00\x01\x01" + bytes([0x02 if down else 0x03]))


def bank_packet(bank0: int) -> bytes:
    return (bytes([0x04]) + b"\x00\x00\x00\x01\x01\x01\x01\x0a" + b"selectedBank"
            + b"\x00\x01\x05\x01" + bank0.to_bytes(4, "little"))


def run(pkts, st=None, cold_cache=True):
    """Feed packets and return what was spoken."""
    SPOKEN.clear()
    if cold_cache:
        channels.invalidate_strip_vector()
    st = st if st is not None else {"bank": None, "pending": None, "at": 0.0}
    for p in pkts:
        server._ann_handle_report(p, st)
    return list(SPOKEN), st


print("MUTE / SOLO STATE")
# Cold layout cache: must NOT read the device, must fall back to a strip number.
said, _ = run([state_packet("channelOutputMute", 1, True)])
check("mute on, cold cache", said, ["Channel 2 muted"])
said, _ = run([state_packet("channelOutputMute", 1, False)])
check("unmute, cold cache", said, ["Channel 2 unmuted"])

# Warm layout cache: Shaun's real layout, USB 1 on strip 1 and Mic 2 pushed to strip 6.
channels.publish_strip_vector([0, 7, 2, 3, 10, 11, 1, 8, 9, 9])
said, _ = run([state_packet("channelOutputMute", 1, True)], cold_cache=False)
check("mute names the SOURCE on that strip", said, ["USB 1 (Main Stereo) muted"])
said, _ = run([state_packet("channelOutputMute", 6, True)], cold_cache=False)
check("mute on the strip Mic 2 was moved to", said, ["Mic 2 muted"])
said, _ = run([state_packet("channelCueEnable", 6, True)], cold_cache=False)
check("solo on", said, ["Mic 2 solo on"])
said, _ = run([state_packet("channelCueEnable", 6, False)], cold_cache=False)
check("solo off", said, ["Mic 2 solo off"])
said, _ = run([state_packet("channelOutputMute", 0, True)], cold_cache=False)
check("strip 0", said, ["Mic 1 muted"])
said, _ = run([state_packet("channelOutputMute", 9, True)], cold_cache=False)
check("last slot still names its source", said, ["USB 2 (Secondary) muted"])

print("\nNAMES SURVIVE THE CACHE EXPIRING (the flaw the live check found)")
# The TTL'd cache is what processing WRITES use; it goes cold after 15 s of no reads, which made
# every announcement past that point say "Channel 4". Naming now falls back to the last known
# layout, which has no expiry.
channels.publish_strip_vector([0, 7, 2, 3, 10, 11, 1, 8, 9, 9])
channels.invalidate_strip_vector()
check("TTL'd cache is cold", channels.cached_strip_vector(), None)
said, _ = run([state_packet("channelOutputMute", 2, True)], cold_cache=False)
check("still names the channel", said, ["Mic 3 muted"])
said, _ = run([state_packet("channelOutputMute", 4, True)], cold_cache=False)
check("stereo source too", said, ["Bluetooth muted"])

print("\nA MIXER PAD OWNS THE MUTE AND CUE LINES WHILE IT ACTS")
channels.publish_strip_vector([0, 7, 2, 3, 10, 11, 1, 8, 9, 9])
# `padActive` is addressed at the pad's STREAM POSITION, one level, same shape as a state push.
def pad_active(position: int, on: bool) -> bytes:
    return (bytes([0x04]) + b"\x00\x00\x00" + b"\x01\x01\x01\x01" + bytes([position])
            + b"padActive" + b"\x00\x01\x01" + bytes([0x02 if on else 0x03]))

MIXER_POS, SOUND_POS = 5, 6
server._hk_mixer_pos = frozenset({MIXER_POS})
# One Back Channel press: the pad, then the four state pushes it moves. Nothing is worth saying.
said, _ = run([pad_active(MIXER_POS, True),
               state_packet("channelOutputMute", 0, True), state_packet("channelCueEnable", 0, True),
               state_packet("channelOutputMute", 8, True), state_packet("channelCueEnable", 8, True)],
              cold_cache=False)
check("a mixer pad's whole burst is silent", said, [])
# The release burst too, and a repeat press only pushes the deadline further out.
said, _ = run([pad_active(MIXER_POS, False),
               state_packet("channelOutputMute", 0, False),
               state_packet("channelCueEnable", 0, False)], cold_cache=False)
check("and so is the burst it restores", said, [])
# The cache still has to be true: the encoder ring quotes it whether or not anything was spoken.
_, st_q = run([pad_active(MIXER_POS, True), state_packet("channelOutputMute", 1, True)],
              cold_cache=False)
check("the mute cache is updated even when silent", st_q["muted"][1], True)
# A SOUND pad moves nothing, so it must not buy silence.
server._ann_mixer_quiet_until = 0.0
said, _ = run([pad_active(SOUND_POS, True), state_packet("channelOutputMute", 1, True)],
              cold_cache=False)
check("a non-mixer pad suppresses nothing", said, ["USB 1 (Main Stereo) muted"])
# And the window is short: a mute after it has passed speaks as usual.
server._ann_mixer_quiet_until = 0.0
said, _ = run([state_packet("channelOutputMute", 1, False)], cold_cache=False)
check("a real mute once the window passes", said, ["USB 1 (Main Stereo) unmuted"])
server._hk_mixer_pos = frozenset()

print("\nA FADER SOURCE CHANGE ON THE STREAM MOVES THE NAME")
channels.publish_strip_vector([0, 7, 2, 3, 10, 11, 1, 8, 9, 9])
# channelInputSource is pushed with the same 0x1C+strip selector; value = the new source code.
push = (bytes([0x04]) + b"\x00\x00\x00" + b"\x01\x01\x01\x01"
        + bytes([channels.SEL_CHANNEL_PROC + 1]) + b"channelInputSource"
        + b"\x00\x01\x05\x01" + (10).to_bytes(4, "little"))
said, _ = run([push], cold_cache=False)
check("the layout change itself is silent", said, [])
check("naming layout updated", channels.last_known_strip_vector()[1], 10)
check("TTL'd cache dropped, so writes re-read", channels.cached_strip_vector(), None)
said, _ = run([state_packet("channelOutputMute", 1, True)], cold_cache=False)
check("mute on that strip names the NEW source", said, ["Bluetooth muted"])

print("\nPRESS EDGES ARE HELD, THEN SUPERSEDED BY THE STATE")
channels.publish_strip_vector([0, 7, 2, 3, 10, 11, 1, 8, 9, 9])
# The real sequence: edge, then the state ~70 ms later. Only the state should be spoken.
for button in ("cmutePressed", "dmutePressed", "emutePressed"):
    said, st = run([press_packet(button), state_packet("channelOutputMute", 1, True)],
                   cold_cache=False)
    check(f"{button} + state", said, ["USB 1 (Main Stereo) muted"])
    check(f"{button} pending cleared", st["pending"], None)

said, st = run([press_packet("csoloPressed"), state_packet("channelCueEnable", 0, True)],
               cold_cache=False)
check("solo edge + state", said, ["Mic 1 solo on"])

print("\nFALLBACK WHEN NO STATE ARRIVES (device-agnostic safety net)")
said, st = run([press_packet("dmutePressed")], cold_cache=False)
check("edge alone says nothing yet", said, [])
SPOKEN.clear()
server._ann_flush_pending(st)
check("not flushed before the window", SPOKEN, [])
st["at"] = time.time() - (server._ANN_PENDING_S + 0.05)
server._ann_flush_pending(st)
check("flushed to the generic word after it", SPOKEN, ["Mute"])
check("pending cleared after flush", st["pending"], None)

said, st = run([press_packet("esoloPressed")], cold_cache=False)
st["at"] = time.time() - (server._ANN_PENDING_S + 0.05)
SPOKEN.clear()
server._ann_flush_pending(st)
check("solo fallback", SPOKEN, ["Solo"])

print("\nRELEASE EDGES AND NOISE ARE IGNORED")
said, _ = run([press_packet("dmutePressed", down=False)], cold_cache=False)
check("release edge", said, [])
said, _ = run([b"\x04\x00\x00\x00mixMute\x00\x01\x01\x02"], cold_cache=False)
check("per-cross-point mixMute ignored", said, [])
said, _ = run([b"\x03\x00\x00\x00" + b"\x01\x01\x01\x01\x1d" + b"channelOutputMute"
               + b"\x00\x01\x01\x02"], cold_cache=False)
check("non report-04 ignored", said, [])
# A selector outside the strip range must not be announced as some phantom channel.
bad = (bytes([0x04]) + b"\x00\x00\x00\x01\x01\x01\x01" + bytes([0x02])
       + b"channelOutputMute" + b"\x00\x01\x01\x02")
said, _ = run([bad], cold_cache=False)
check("out-of-range selector ignored", said, [])

print("\nSPOKEN POSITIONS: IMMEDIATE, INTERRUPTING, NEVER STACKING")
import struct
import rcp_transfer


def xpoint_packet(source_code: int, output: int, level: float) -> bytes:
    """A pushed fader position: mixLevelWithAnchor on the cross-point selector for (source, output),
    value the ASCII string 'level|anchor'. The desk sends one per cross-point, 13 per source.

    THE SELECTOR IS NOT ALWAYS ONE BYTE. CROSS_POINT_BASE + 13*source + output passes 255 at
    source 14, so Virtual A and Virtual B arrive with a two-byte index and the width byte says so."""
    sel = rcp_transfer.CROSS_POINT_BASE + 13 * source_code + output
    width = 1 if sel <= 0xFF else 2
    val = f"{level:g}|{level:g}".encode()
    return (bytes([0x04]) + bytes(3) + bytes([0x01, 0x01, 0x01, width])
            + sel.to_bytes(width, "little")
            + b"mixLevelWithAnchor" + bytes([0x00, 0x01, len(val) + 1, 0x05]) + val + bytes(1))


def monlevel_packet(level: float) -> bytes:
    return (bytes([0x04]) + bytes(3) + bytes([0x01, 0x01, 0x01, 0x01, 0x0A])
            + b"outputMonLevel" + bytes([0x00, 0x01, 0x09, 0x04]) + struct.pack("<d", level))


def fresh_st():
    return {"bank": None, "pending": None, "at": 0.0, "levels": {}, "levels_at": 0.0,
            "known": {}, "muted": {}, "enc_pos": None, "enc_down_at": None, "enc_hold": False,
            "said": {}, "said_at": {}}


# Spoken AS IT MOVES: waiting for the movement to settle is laggy, and since the speech engine
# interrupts the previous phrase, immediate is both snappier and safe from stacking.
# NUMBER FIRST, then the name. Leading with the NAME means riding a physical fader almost never
# says WHICH fader: mid-sweep updates drop the name entirely, and the closing named phrase only
# survives when the last push happens to be held rather than spoken. Leading with the changing fact
# means it always lands, and the name completes whenever the movement pauses.
st = fresh_st()
SPOKEN.clear()
server._ann_handle_report(xpoint_packet(0, 0, 0.30), st)
check("first update spoken at once, no waiting", SPOKEN, ["30 percent, Mic 1"])

SPOKEN.clear()
for out in range(1, 13):
    server._ann_handle_report(xpoint_packet(0, out, 0.30), st)
check("the same position on the other 12 cross-points adds nothing", SPOKEN, [])

SPOKEN.clear()
st["said_at"][("fader", 0)] = time.time() - (server._ANN_LEVEL_GAP_S + 0.01)
server._ann_handle_report(xpoint_packet(0, 0, 0.42), st)
# Mid-sweep the number leads and "percent" is dropped: the unit is obvious once you are moving, and
# the spare syllables would only eat into the name before the next update interrupts it.
check("a continuing sweep leads with the bare number", SPOKEN, ["42, Mic 1"])

# After a pause, the full phrase comes back, so you always know what you are holding.
SPOKEN.clear()
st["run_at"] = time.time() - (server._ANN_RUN_S + 0.1)
st["said_at"][("fader", 0)] = time.time() - (server._ANN_LEVEL_GAP_S + 0.01)
server._ann_handle_report(xpoint_packet(0, 0, 0.50), st)
check("after a pause it names the channel again", SPOKEN, ["50 percent, Mic 1"])

SPOKEN.clear()
for lvl in (0.44, 0.46, 0.48):
    server._ann_handle_report(xpoint_packet(0, 0, lvl), st)
check("updates inside the spacing window are held", SPOKEN, [])
st["levels_at"] = time.time() - (server._ANN_SETTLE_S + 0.01)
server._ann_flush_pending(st)
check("the closing value names the channel as confirmation", SPOKEN, ["48 percent, Mic 1"])
SPOKEN.clear()
server._ann_flush_pending(st)
check("and not repeated", SPOKEN, [])

SPOKEN.clear()
server._ann_handle_report(monlevel_packet(0.03), fresh_st())
check("monitor level", SPOKEN, ["3 percent, Monitor"])

SPOKEN.clear()
server._ann_handle_report(xpoint_packet(7, 0, 1.0), fresh_st())
check("names the source, not the strip", SPOKEN, ["100 percent, USB 1 (Main Stereo)"])

SPOKEN.clear()
server._ann_levels_on = False
st2 = fresh_st()
server._ann_handle_report(xpoint_packet(0, 0, 0.5), st2)
server._ann_handle_report(monlevel_packet(0.5), st2)
check("silent when the levels toggle is off", SPOKEN, [])
server._ann_levels_on = True

SPOKEN.clear()
server._ann_handle_report(state_packet("channelOutputMute", 1, True), fresh_st())
check("a mute still gets through", SPOKEN, ["USB 1 (Main Stereo) muted"])

print("\nAFTER AN ENCODER PRESS, ONE CLICK NAMES WHAT YOU ARE HOLDING")
# The press is detected (it always was) but says nothing about WHICH channel it selected, so the
# first movement must name it straight away - otherwise you nudge blind and wait for each settle.
# EXACTLY as captured off the desk: 00 00 00 | 01 01 01 01 | 06 | name | 00 01 01 02.
# The value type sits at name+3. An invented shape with an extra byte matches a hand-written name+4
# offset in the code, and then both are wrong together: the encoder press announces nothing on real
# hardware while this suite stays green. Build packets from captures, not from what the code happens
# to expect.
enc_press = (bytes([0x04]) + b"\x00\x00\x00" + b"\x01\x01\x01\x01" + bytes([0x06])
             + b"encoderPressed" + b"\x00\x01\x01\x02")
# The RELEASE edge, which the desk does push (measured, contradicting the "edge only"
# hardware-observed). Announcing now happens here rather than on the press, because a HOLD is
# the desk's own mute gesture and must not advance our ring - see _ANN_ENC_HOLD_S.
enc_release = enc_press[:-1] + b"\x03"


def tap(st_, packets=()):
    """A short press: down, then up, with anything in between (e.g. the desk's mute on a hold)."""
    server._ann_handle_report(enc_press, st_)
    for p in packets:
        server._ann_handle_report(p, st_)
    server._ann_handle_report(enc_release, st_)


st = {"bank": None, "pending": None, "at": 0.0, "levels": {}, "levels_at": 0.0}
SPOKEN.clear()
server._ann_handle_report(enc_press, st)
check("the press edge alone says nothing", SPOKEN, [])
server._ann_handle_report(enc_release, st)
check("the release is what announces", SPOKEN, ["Select"])
SPOKEN.clear()
server._ann_handle_report(xpoint_packet(8, 0, 0.72), st)     # source 8 = Chat, one click
check("first click names it immediately, no waiting", SPOKEN, ["72 percent, Chat"])
check("and nothing is left pending for the settle", st["levels"], {})
SPOKEN.clear()
st["levels_at"] = time.time() - (server._ANN_SETTLE_S + 0.05)
server._ann_flush_pending(st)
check("the settle does not repeat it", SPOKEN, [])

# Keep turning: the closing position IS announced, because it is a different value.
SPOKEN.clear()
for lvl in (0.75, 0.80, 0.85):
    server._ann_handle_report(xpoint_packet(8, 0, lvl), st)
check("still silent mid-sweep", SPOKEN, [])
st["levels_at"] = time.time() - (server._ANN_SETTLE_S + 0.05)
server._ann_flush_pending(st)
check("closing position spoken once", SPOKEN, ["85 percent, Chat"])

# A second press re-arms it, so the next channel you land on is named at once too.
SPOKEN.clear()
tap(st)
server._ann_handle_report(xpoint_packet(9, 0, 0.9), st)      # source 9 = USB 2
check("second press re-arms the naming", SPOKEN, ["Select", "90 percent, USB 2 (Secondary)"])

print("\nPRESS AND HOLD IS THE DESK'S MUTE, NOT A RING STEP")
# Measured on hardware: holding pushes channelOutputMute ~765 ms in, while still held,
# and mutes the channel selected BEFORE the press. Announcing the next ring position on the press
# edge therefore named the wrong channel AND left our ring one step out - both reported by Shaun.
channels.publish_strip_vector([0, 7, 2, 3, 10, 11, 1, 8, 9, 9])   # faders 7/8/9 = Mic 2, Chat, USB 2
st = fresh_st()
st["enc_pos"] = 0                                            # sitting on fader 7 (Mic 2)
SPOKEN.clear()
tap(st, [state_packet("channelOutputMute", 6, True)])        # strip 6 = fader 7 = Mic 2
check("a hold speaks only the mute", SPOKEN, ["Mic 2 muted"])
check("and does NOT advance the ring", st["enc_pos"], 0)
check("and records the mute", st["muted"], {6: True})

# The duration backstop, for a desk that pushes no mute state (the Duo is unobserved here).
st = fresh_st()
st["enc_pos"] = 0
SPOKEN.clear()
server._ann_handle_report(enc_press, st)
st["enc_down_at"] = time.time() - (server._ANN_ENC_HOLD_S + 0.05)
server._ann_handle_report(enc_release, st)
check("a long hold with no state pushed is still not a ring step", SPOKEN, [])
check("ring unmoved by the backstop", st["enc_pos"], 0)

# A muted channel quotes the mute, not a level: a healthy percentage on something you cannot hear
# is worse than no number at all.
st = fresh_st()
st["enc_pos"] = 0
st["known"][("fader", 7)] = 0.97
st["muted"] = {7: True}
SPOKEN.clear()
tap(st)
check("the ring says muted instead of the level", SPOKEN, ["Chat, muted"])
check("and the ring did advance, because this was a short press", st["enc_pos"], 1)

# Unmuting restores the level.
SPOKEN.clear()
server._ann_handle_report(state_packet("channelOutputMute", 7, False), st)
check("the unmute is announced", SPOKEN, ["Chat unmuted"])
st["enc_pos"] = 0
SPOKEN.clear()
tap(st)
check("and the level is quoted again", SPOKEN, ["Chat 97 percent"])

# A fader move with no press behind it is announced just the same, and just as promptly: the encoder
# press only decides WHOSE name we can predict, it is not what makes levels audible.
SPOKEN.clear()
st2 = fresh_st()
server._ann_handle_report(xpoint_packet(0, 0, 0.4), st2)
check("a plain fader move is spoken at once too", SPOKEN, ["40 percent, Mic 1"])
SPOKEN.clear()
st2["levels_at"] = time.time() - (server._ANN_SETTLE_S + 0.05)
server._ann_flush_pending(st2)
check("and the settle adds nothing to repeat", SPOKEN, [])

print("\nA TURN THAT MOVES NOTHING SAYS SO")
# The encoder is only bound to a level while the desk is on a page that binds one; pressing the BANK
# buttons moves the screen to the pads page and unbinds it (webcam-confirmed). The desk
# still reports the rotation, so we can tell the operator the turn did nothing rather than leave them
# wondering whether the app has broken.


def encsig_packet(delta: int = 1) -> bytes:
    """THE SHAPE THE DESK ACTUALLY SENDS, hexdumped from the wire:

        ... encoderSignal | 00 01 0b 08 | 01 05 01 | <int32 LE> | 01

    THE FOUR BYTES BEFORE THE MARKER MATTER: keep them, or this stops testing anything. A builder
    that emits `00 01 05 01 <int32>`, marker straight after the name, is the shape a decoder is
    tempted to ASSUME rather than the shape the desk sends, so the suite proves the decoder against
    its own assumption while the real delta is thrown away on every turn.
    """
    return (bytes([0x04]) + bytes(3) + bytes([0x01, 0x01, 0x01, 0x01, 0x06])
            + b"encoderSignal" + bytes([0x00, 0x01, 0x0B, 0x08, 0x01, 0x05, 0x01])
            + int(delta).to_bytes(4, "little", signed=True) + bytes([0x01]))


def enccolour_packet(value: int) -> bytes:
    return (bytes([0x04]) + bytes(3) + bytes([0x01, 0x01, 0x01, 0x01, 0x07])
            + b"encoderColour" + bytes([0x00, 0x01, 0x05, 0x01])
            + int(value).to_bytes(4, "little", signed=True))


# THE DELTA HAS TO SURVIVE THE DECODER, and for as long as this feature existed it did not. Pin the
# value itself, not just the behaviour built on it: a decoder that silently returns None turns every
# turn into a delta of 0, which _ann_at_limit reads as "do not know" and lets through.
check("a clockwise click decodes as +1",
      server._ann_decode_int(encsig_packet(1), encsig_packet(1).find(b"encoderSignal")
                             + len(b"encoderSignal") + 1), 1)
check("and an anticlockwise click as -1",
      server._ann_decode_int(encsig_packet(-1), encsig_packet(-1).find(b"encoderSignal")
                             + len(b"encoderSignal") + 1), -1)

# The at-limit checks further down are what this protects: they set enc_pos and the level cache by
# hand and then send a turn, so they only ever proved anything if the DELTA in that turn survives.
# Under the old builder they passed against a packet shape the desk does not send.

st = fresh_st()
SPOKEN.clear()
for _ in range(6):
    server._ann_handle_report(encsig_packet(1), st)
check("the rotation itself is not announced", SPOKEN, [])
server._ann_flush_pending(st)
check("and nothing is said inside the wait", SPOKEN, [])
st["enc_turn_at"] = time.time() - (server._ANN_ENC_DEAD_S + 0.05)
server._ann_flush_pending(st)
check("a turn that moved nothing says so", SPOKEN, ["Encoder not linked to anything"])

SPOKEN.clear()
st["enc_turn_at"] = time.time() - (server._ANN_ENC_DEAD_S + 0.05)
server._ann_flush_pending(st)
check("not repeated within the quiet gap", SPOKEN, [])

# When we know WHICH channel is holding the encoder, name it - and name what that channel HAS, never
# what it lacks. A phrase like "SMART pads is selected and has nothing..." breaks that, and lands on
# any turn the ring does not see: Mic 1 gets told it has nothing to adjust while the encoder is
# adjusting its gain. Routing it through _ann_selected_phrase works the claim out from the strip.
st = fresh_st()
channels.publish_strip_vector([0, 7, 2, 3, 10, 11, 1, 8, 9, 9])
st["chan_sel"] = 5                     # strip 5 = SMART pads
st["enc_turn_at"] = time.time() - (server._ANN_ENC_DEAD_S + 0.05)
SPOKEN.clear()
server._ann_flush_pending(st)
check("it names the channel holding the encoder", SPOKEN[0].startswith("SMART pads selected"), True)
check("and says what that channel HAS, not what it lacks",
      "nothing" in SPOKEN[0].lower(), False)
check("and names the way out, which is the channel button",
      "channel button again" in SPOKEN[0], True)

# A MIC holding the encoder must never be told it has nothing to adjust: its gain is exactly what the
# encoder is moving.
st = fresh_st()
channels.publish_strip_vector([0, 7, 2, 3, 10, 11, 1, 8, 9, 9])
st["chan_sel"] = 0                     # strip 0 = Mic 1
st["enc_turn_at"] = time.time() - (server._ANN_ENC_DEAD_S + 0.05)
SPOKEN.clear()
server._ann_flush_pending(st)
check("a selected MIC is described by its gain", SPOKEN[0].startswith("Mic 1 selected"), True)
check("and is never told it has nothing to adjust", "nothing" in SPOKEN[0].lower(), False)

# AN ENCODER PRESS DOES NOT TAKE THE ENCODER BACK OFF A SELECTED CHANNEL, measured on the desk 4th
# September 2026: systemChannelSelected stayed at 0 across two presses. Advancing our ring here
# announced USB 1 and then Chat while the desk had never left Mic 1, and left enc_pos pointing at an
# entry the encoder is not on - the same value _ann_at_limit reads to decide "is this against a stop".
st = fresh_st()
channels.publish_strip_vector([0, 7, 2, 3, 10, 11, 1, 8, 9, 9])
st["chan_sel"] = 0
st["enc_pos"] = 1
SPOKEN.clear()
server._ann_handle_report(press_packet("encoderPressed", True), st)
server._ann_handle_report(press_packet("encoderPressed", False), st)
check("the ring does NOT advance while a channel is selected", st["enc_pos"], 1)
check("and the press says what the encoder is still on", SPOKEN, ["Mic 1 selected. Encoder sets its gain"])

# With nothing selected it walks the ring exactly as before.
st = fresh_st()
st["chan_sel"] = -1
st["enc_pos"] = 0
SPOKEN.clear()
server._ann_handle_report(press_packet("encoderPressed", True), st)
server._ann_handle_report(press_packet("encoderPressed", False), st)
check("but it still advances when nothing is selected", st["enc_pos"], 1)

st = fresh_st()
SPOKEN.clear()
server._ann_handle_report(encsig_packet(1), st)
server._ann_handle_report(xpoint_packet(8, 0, 0.72), st)
check("a turn that moves a level announces the level", SPOKEN, ["72 percent, Chat"])
check("and cancels the not-linked warning", st.get("enc_turn_at"), None)
SPOKEN.clear()
server._ann_flush_pending(st)
check("so nothing complains afterwards", SPOKEN, [])

st = fresh_st()
server._ann_handle_report(enccolour_packet(-1), st)
check("colour -1 reads as unbound", st.get("enc_bound"), False)
server._ann_handle_report(enccolour_packet(7), st)
check("a real colour reads as bound", st.get("enc_bound"), True)

st = fresh_st()
SPOKEN.clear()
server._ann_levels_on = False
server._ann_handle_report(encsig_packet(1), st)
st["enc_turn_at"] = time.time() - (server._ANN_ENC_DEAD_S + 0.05)
server._ann_flush_pending(st)
check("silent when levels are off", SPOKEN, [])
server._ann_levels_on = True

print("\nTHE ENCODER RING: PREDICT WHAT A PRESS SELECTED")
# Hardware-observed order: fader 7 -> fader 8 -> fader 9 -> monitor -> round again. Once a real
# movement has seated us, every later press can be announced BEFORE it is turned.
channels.publish_strip_vector([0, 7, 2, 3, 10, 11, 1, 8, 9, 9])   # faders 7/8/9 = Mic 2, Chat, USB 2


def fader_level_packet(fader_idx: int, value127: int) -> bytes:
    sel = devices.FADER_NODE_BASE + fader_idx
    return (bytes([0x04]) + b"\x00\x00\x01\x01\x02\x00\x01" + bytes([sel]) + b"faderLevel"
            + b"\x00\x01\x05\x01" + int(value127).to_bytes(4, "little"))


st = fresh_st()
SPOKEN.clear()
tap(st)
check("before we know where we are, it just says Select", SPOKEN, ["Select"])

# A real turn on fader 8 (Chat) at 123/127 seats the ring and caches the level.
server._ann_handle_report(fader_level_packet(7, 123), st)
check("the movement seats the ring", st["enc_pos"], 1)
check("and caches the level", round(st["known"][("fader", 7)], 4), round(123 / 127, 4))

SPOKEN.clear()
tap(st)
# With no level cached yet it names the target and says NOTHING about the level, rather than
# inventing a zero - the level only appears once we have actually seen it.
check("next press predicts fader 9 by name", SPOKEN, ["USB 2 (Secondary)"])
SPOKEN.clear()
tap(st)
check("then the monitor", SPOKEN, ["Monitor level"])
SPOKEN.clear()
tap(st)
check("then round to fader 7, named from the layout", SPOKEN, ["Mic 2"])

# Levels it already knows are spoken with the value.
st["known"][("monitor", 0)] = 0.52
st["enc_pos"] = 1
SPOKEN.clear()
tap(st)
tap(st)
check("a known level is included", SPOKEN[-1], "Monitor level 52 percent")

# A WRONG prediction self-corrects: the real movement re-seats us.
st["enc_pos"] = 0
server._ann_handle_report(fader_level_packet(8, 64), st)     # actually fader 9 moved
check("a real movement overrides the guess", st["enc_pos"], 2)

print("\nTHE RING CACHE STAYS CURRENT, AND THE ENDS OF THE RANGE ARE NOT 'NOT LINKED'")
# TWO FAULTS THE RING CACHE IS CAPABLE OF: keying it by fader index while level announcements
# update a source-code key makes the press quote a stale level, and treating 0 as absent announces
# "not linked" when it is simply the floor.
channels.publish_strip_vector([0, 7, 2, 3, 10, 11, 1, 8, 9, 9])   # faders 7/8/9 = Mic 2, Chat, USB 2

st = fresh_st()
SPOKEN.clear()
# A level change arriving by SOURCE code (a cross-point push, which is what our own slider writes
# produce) must update the FADER-INDEX entry the ring quotes.
server._ann_handle_report(xpoint_packet(1, 0, 0.45), st)          # source 1 = Mic 2, on fader 7
check("the level is announced", SPOKEN, ["45 percent, Mic 2"])
check("and the ring cache is updated by fader index", st["known"].get(("fader", 6)), 0.45)

st["enc_pos"] = 3                                                 # monitor; next press wraps to 7
SPOKEN.clear()
tap(st)
check("so the press quotes the CURRENT level", SPOKEN, ["Mic 2 45 percent"])

# Turning down at zero: nothing moves, and that is a floor, not an unbound encoder.
st = fresh_st()
st["enc_pos"] = 0
st["known"][("fader", 6)] = 0.0
SPOKEN.clear()
server._ann_handle_report(encsig_packet(-1), st)
check("a downward turn at 0 is not treated as a dead turn", st.get("enc_turn_at"), None)
st["levels_at"] = time.time() - (server._ANN_SETTLE_S + 0.05)
server._ann_flush_pending(st)
check("so nothing says 'not linked'", SPOKEN, [])

# Turning UP from 0 could move, so a dead turn there is still worth reporting.
SPOKEN.clear()
server._ann_handle_report(encsig_packet(1), st)
check("an upward turn at 0 is still watched", st.get("enc_turn_at") is not None, True)
st["enc_turn_at"] = time.time() - (server._ANN_ENC_DEAD_S + 0.05)
server._ann_flush_pending(st)
check("and reported when nothing moves", SPOKEN, ["Encoder not linked to anything"])

# Same at the top of the range.
st = fresh_st()
st["enc_pos"] = 1
st["known"][("fader", 7)] = 1.0
SPOKEN.clear()
server._ann_handle_report(encsig_packet(1), st)
check("an upward turn at maximum is not a dead turn", st.get("enc_turn_at"), None)

# With no cached level we cannot tell, so we keep watching rather than guess.
st = fresh_st()
st["enc_pos"] = 2
server._ann_handle_report(encsig_packet(-1), st)
check("unknown level means we still watch", st.get("enc_turn_at") is not None, True)

print("\nTHE ENCODER ON A SELECTED CHANNEL: GAIN FOR MICS, LEVEL FOR LINE STRIPS")
# Pressing a channel button hands the encoder to that channel's gain. Mic strips move
# inputMicrophoneGain; line strips (USB 1, Chat, USB 2, Bluetooth) move inputDigitalGain. Decoding
# only the first meant a working turn on USB 1 was announced as "not linked" while Shaun's system
# audio audibly changed.


def chan_gain_packet(prop: bytes, chan_sel: int, value: int, trailer: int = 0x02) -> bytes:
    """A pushed CHANNEL-address property: 01 01 01 02 <chan_sel> <trailer> <name> 00 01 05 01 <int>."""
    return (bytes([0x04]) + bytes(3) + bytes([0x01, 0x01, 0x01, 0x02, chan_sel, trailer])
            + prop + bytes([0x00, 0x01, 0x05, 0x01])
            + int(value).to_bytes(4, "little", signed=True))


def chansel_packet(value: int) -> bytes:
    return (bytes([0x04]) + bytes(3) + bytes([0x01, 0x01, 0x01, 0x01, 0x0F])
            + b"systemChannelSelected" + bytes([0x00, 0x01, 0x05, 0x01])
            + int(value).to_bytes(4, "little", signed=True))


channels.publish_strip_vector([0, 7, 2, 3, 10, 11, 1, 8, 9, 9])

st = fresh_st()
SPOKEN.clear()
server._ann_handle_report(chansel_packet(1), st)
# The phrase says up front what the encoder will do, worked out from the channel itself: gain for a
# mic, level for a line strip, and "nothing to adjust" for SMART pads - which has no processing at
# all per RODE's guide, and is the case that started the whole encoder hunt.
check("a channel button names what took the encoder and what it does",
      SPOKEN, ["USB 1 (Main Stereo) selected. Encoder sets its input level in decibels"])
SPOKEN.clear()
server._ann_handle_report(chansel_packet(1), st)
check("the same selection is not repeated", SPOKEN, [])
SPOKEN.clear()
server._ann_handle_report(chansel_packet(5), st)          # strip 5 = SMART pads
check("SMART pads names what it HAS, at press time, without a fruitless turn",
      SPOKEN, ["SMART pads selected. Fader level only"])
SPOKEN.clear()

# SAY WHAT THE CHANNEL HAS, NOT WHAT IT LACKS. "Nothing here for the encoder to adjust" is simply
# false of the SMART pads: they can sit on the last fader (index 8, virtual) and be ridden daily.
# The phrase is the same on a physical and a virtual fader ON PURPOSE - a bus has a fader either way.
channels.publish_strip_vector([0, 7, 2, 3, 10, 1, 8, 9, 11])   # pads on strip 8, the last fader
server._ann_handle_report(chansel_packet(8), st)
check("and says the same on a VIRTUAL fader - no device branch to drift",
      SPOKEN, ["SMART pads selected. Fader level only"])
SPOKEN.clear()
server._ann_handle_report(chansel_packet(6), st)               # strip 6 = Chat, also virtual
check("a LINE channel on a virtual fader still names its own level, not the ring",
      SPOKEN, ["Chat selected. Encoder sets its input level in decibels"])
SPOKEN.clear()
channels.publish_strip_vector([0, 7, 2, 3, 10, 11, 1, 8, 9, 9])   # back to this block's layout

server._ann_handle_report(chansel_packet(0), st)          # strip 0 = Mic 1
check("a mic strip says gain", SPOKEN, ["Mic 1 selected. Encoder sets its gain"])
SPOKEN.clear()
server._ann_handle_report(chansel_packet(-1), st)
check("and deselecting says the encoder is back on levels",
      SPOKEN, ["Levels. Encoder sets fader and monitor levels"])

# THE PHANTOM "LEVELS" ANNOUNCEMENT: "Levels. Encoder sets fader and monitor levels" spoken a while
# after startup with nobody touching anything. The pushed stream reports CHANGES, so an ABSENT
# st["chan_sel"] makes the desk's first push of the resting value (-1, nothing selected) read as
# one. _ann_seed_layout seeds it for exactly this reason: seeded state must stay SILENT for an
# unchanged value.
SPOKEN.clear()
seeded = fresh_st()
seeded["chan_sel"] = -1                      # what _ann_seed_layout now reads from the startup dump
server._ann_handle_report(chansel_packet(-1), seeded)
check("a resting push of 'nothing selected' after startup says NOTHING", SPOKEN, [])
SPOKEN.clear()
server._ann_handle_report(chansel_packet(0), seeded)
check("but a real selection still speaks", SPOKEN, ["Mic 1 selected. Encoder sets its gain"])
SPOKEN.clear()
unseeded = fresh_st()                        # unseeded: the state that produces the spurious phrase
server._ann_handle_report(chansel_packet(-1), unseeded)
check("UNSEEDED state is what speaks out of nowhere",
      SPOKEN, ["Levels. Encoder sets fader and monitor levels"])

# THE SAME SHAPE OF BUG, SECOND PROPERTY: st["bank"] also starts None, so the desk's first push of
# the RESTING bank reads as a change and "Bank 1" speaks at startup. Both are seeded from the
# startup dump by _ann_seed_layout.
SPOKEN.clear()
bank_seeded = fresh_st()
bank_seeded["bank"] = 0                      # the resting bank, raw (the desk numbers banks from 0)
server._ann_handle_report(bank_packet(0), bank_seeded)
check("a resting push of the current bank after startup says NOTHING", SPOKEN, [])
SPOKEN.clear()
server._ann_handle_report(bank_packet(1), bank_seeded)
check("but a real bank change still speaks", SPOKEN, ["Bank 2"])
SPOKEN.clear()
server._ann_handle_report(bank_packet(0), fresh_st())
check("UNSEEDED bank is what speaks out of nowhere", SPOKEN, ["Bank 1"])

# OUR OWN WRITES ARE NOT ECHOED on the desk's push stream, so anything the announcer caches has to
# be told about them. That covers the fader LAYOUT and the encoder ring's LEVEL cache alike: without
# the level half, moving a virtual fader with our slider leaves the next encoder press quoting the
# level from before the move.
SPOKEN.clear()
live = fresh_st()
live["known"][("fader", 6)] = 0.20
server._ann_live_state = live
try:
    server._ann_publish_level("fader", 6, 0.75)
    check("our own fader-level write updates the ring cache", live["known"][("fader", 6)], 0.75)
    server._ann_publish_level("monitor", 0, 0.42)
    check("and the monitor level too", live["known"][("monitor", 0)], 0.42)
    check("publishing a level says NOTHING (the web UI already announces it)", SPOKEN, [])
    server._ann_publish_level("fader", 6, 9.9)
    check("a level is clamped to 0..1", live["known"][("fader", 6)], 1.0)
finally:
    server._ann_live_state = None
server._ann_publish_level("fader", 6, 0.5)             # must be a safe no-op when off
check("publishing with the announcer off is a no-op", server._ann_live_state, None)

# A MIC strip: gain in dB.
st = fresh_st()
SPOKEN.clear()
server._ann_handle_report(chan_gain_packet(b"inputMicrophoneGain", 0x6E, 66), st)
check("mic gain is spoken", SPOKEN, ["Mic 1 gain 66 d B"])
SPOKEN.clear()
server._ann_handle_report(chan_gain_packet(b"inputMicrophoneGain", 0x6E, 66), st)
check("the same value is not repeated", SPOKEN, [])

# A LINE strip: the desk calls it level, not gain.
st = fresh_st()
SPOKEN.clear()
server._ann_handle_report(encsig_packet(1), st)
server._ann_handle_report(chan_gain_packet(b"inputDigitalGain", 0x75, 6), st)
check("USB 1 input level is spoken", SPOKEN, ["USB 1 (Main Stereo) input level 6 d B"])
check("and it cancels the not-linked warning", st.get("enc_turn_at"), None)
SPOKEN.clear()
server._ann_flush_pending(st)
check("so nothing calls it unlinked", SPOKEN, [])

# An unknown selector is not announced as some phantom channel, but still counts as movement.
st = fresh_st()
SPOKEN.clear()
server._ann_handle_report(encsig_packet(1), st)
server._ann_handle_report(chan_gain_packet(b"inputDigitalGain", 0x99, 3), st)
check("an unknown channel selector says nothing", SPOKEN, [])
check("but is still treated as movement", st.get("enc_turn_at"), None)

print("\nSTILL WORKS: BANK AND ENCODER")
said, st = run([bank_packet(2)], cold_cache=False)
check("bank change", said, ["Bank 3"])
SPOKEN.clear()
server._ann_handle_report(bank_packet(2), st)
check("same bank not repeated", SPOKEN, [])
said, _ = run([enc_press, enc_release], cold_cache=False)
check("encoder press", said, ["Select"])
# And the invented shape must NOT be accepted, or the bug can come back unnoticed.
fake_press = (bytes([0x04]) + b"\x00\x00\x00\x01\x01\x01\x01\x0a" + b"encoderPressed"
              + b"\x00\x01\x01\x01\x02")
said, _ = run([fake_press], cold_cache=False)
check("a malformed press value is ignored", said, [])

print("\nOUR OWN LAYOUT WRITES REFRESH THE NAMING LAYOUT")
# THE TRAP: invalidating only the TTL'd cache (the one that addresses processing writes) leaves the
# no-expiry NAMING layout holding the pre-reset vector, because our own writes are not echoed on the
# pushed stream the way a change made on the desk is. An encoder PRESS then announces the old
# channel ("Mic 2") while a TURN announces the new one ("USB 1"): the turn reads the source code off
# the cross-point selector, the press has to look the layout up.
import rcp_read  # noqa: E402

OLD_LAYOUT = [0, 7, 2, 3, 10, 11, 1, 8, 9, 9]      # before the reset: Mic 2 up on fader 7
NEW_LAYOUT = [0, 1, 2, 3, 10, 11, 7, 8, 9, 9]      # after the reset: USB 1 on fader 7
channels.publish_strip_vector(list(OLD_LAYOUT))
check("names the OLD channel before the write", server._ann_fader_label(6), "Mic 2")

_real_find_all, _real_read_model = rcp_read.find_all, rcp_read.read_model
rcp_read.read_model = lambda *a, **k: b"stub-dump"
rcp_read.find_all = lambda blob, name: ([(v, "int") for v in NEW_LAYOUT]
                                        if name == "channelInputSource"
                                        else _real_find_all(blob, name))
try:
    # This is what every layout write does immediately after writing: re-read to report the result.
    got = server._read_fader_vector()
    check("the re-read reports the new layout", got, NEW_LAYOUT[:9])
    check("and PUBLISHES it for naming", channels.last_known_strip_vector(), NEW_LAYOUT)
    check("so fader 7 now names USB 1", server._ann_fader_label(6), "USB 1 (Main Stereo)")
    st = {"bank": None, "pending": None, "at": 0.0, "enc_pos": 0,
          "known": {("fader", 6): 0.5}}
    check("and an encoder PRESS says so too", server._ann_ring_phrase(st, 0),
          "USB 1 (Main Stereo) 50 percent")
    check("the write cache is fresh as well", channels.cached_strip_vector(), NEW_LAYOUT)
    # MUTE / SOLO and the channel button name the strip through the SAME lookup, so a stale layout
    # mis-names all four paths at once.
    said, _ = run([state_packet("channelOutputMute", 6, True)], cold_cache=False)
    check("mute on fader 7 names USB 1", said, ["USB 1 (Main Stereo) muted"])
    said, _ = run([state_packet("channelCueEnable", 1, True)], cold_cache=False)
    check("solo on fader 2 names Mic 2 again", said, ["Mic 2 solo on"])
    check("and the channel button agrees", server._ann_selected_phrase(6),
          "USB 1 (Main Stereo) selected. Encoder sets its input level in decibels")
finally:
    rcp_read.find_all, rcp_read.read_model = _real_find_all, _real_read_model

print("\nMUTING FROM SOFTWARE KEEPS THE ANNOUNCER'S MUTE CACHE HONEST")
# The class of bug this closes is the one that has now bitten three times: THE DESK DOES NOT ECHO OUR
# OWN WRITES. So a mute set from the web UI never appears on the pushed stream, and any announcer
# cache holding mute state has to be told about it explicitly. Left untold, the encoder ring would
# quote a healthy percentage for a channel that is now silent - the mirror image of the stale-level
# bug fixed the same day.
import rcp_transfer  # noqa: E402

RECORDS: list = []
_real_send = rcp_transfer.send_record
rcp_transfer.send_record = lambda rec: RECORDS.append(rec) or 1
try:
    channels.publish_strip_vector([0, 1, 2, 3, 10, 11, 7, 8, 9, 9])
    RECORDS.clear()
    channels.set_channel_mute("Mic2", True)
    rec = RECORDS[0] if RECORDS else b""
    # The record shape is the module-toggle shape: global address, compact bool, selector
    # SEL_CHANNEL_PROC + strip. Mic 2 is on strip 1 in this layout, so 0x1D - the exact selector the
    # hardware test used.
    check("one record sent", len(RECORDS), 1)
    check_true("addressed as a global property on 0x1C + strip (Mic 2 -> 0x1D)",
               b"\x01\x01\x01\x01\x1d" + b"channelOutputMute\x00\x01\x01\x02" in rec)
    RECORDS.clear()
    channels.set_channel_mute("Mic2", False)
    check_true("unmute is the same record with the OFF byte",
               b"channelOutputMute\x00\x01\x01\x03" in (RECORDS[0] if RECORDS else b""))
    # And it follows the CHANNEL, not a fixed slot: with Mic 2 up on fader 7 (strip 6) the write has
    # to land on 0x22, exactly as a processing write on that channel would.
    channels.publish_strip_vector([0, 7, 2, 3, 10, 11, 1, 8, 9, 9])
    RECORDS.clear()
    channels.set_channel_mute("Mic2", True)
    check_true("the write follows the channel to the strip it is on (fader 7 -> 0x22)",
               b"\x01\x01\x01\x01\x22" + b"channelOutputMute\x00" in (RECORDS[0] if RECORDS else b""))

    # Now the cache. A live announcer state, as _announce_loop publishes it.
    st = {"bank": None, "pending": None, "at": 0.0, "known": {("fader", 6): 0.9}, "muted": {}}
    server._ann_live_state = st
    SPOKEN.clear()
    res = server.action_set_channel_mute("Mic2", True)
    check_true("the write reports ok", res.get("ok"))
    check_true("and says it silences every mix, not just what you hear",
               "every mix" in res.get("message", ""))
    check("the announcer does NOT speak our own write (the UI already did)", SPOKEN, [])
    check("but its mute cache knows, keyed by STRIP", st["muted"], {6: True})
    check("so an encoder press quotes the mute, not a stale 90 percent",
          server._ann_ring_phrase(st, 0), "Mic 2, muted")
    server.action_set_channel_mute("Mic2", False)
    check("unmuting clears it again", st["muted"], {6: False})
    check("and the ring goes back to the level", server._ann_ring_phrase(st, 0), "Mic 2 90 percent")
    server._ann_live_state = None
    check_true("publishing with the announcer OFF is a safe no-op",
               server.action_set_channel_mute("Mic2", True).get("ok"))

    # ---- ECHO SUPPRESSION: our own writes are not read back at us ----
    # The checks above only prove the WRITE itself is silent. The desk then PUSHES the change back,
    # and that echo lands on top of the screen reader reading the UI's own live region, so every
    # change is announced twice. Hardware-proven for levels by announcer_echo_check.py: a marked
    # write is silent, the same write unmarked comes back as "90 percent, USB 2". These pin the
    # behaviour, including that it is SUPPRESSION and not deafness.
    print("\n-- echo suppression (our own writes are not spoken back)")
    channels.publish_strip_vector([0, 7, 2, 3, 10, 11, 1, 8, 9, 9])
    st = fresh_st()
    server._ann_live_state = st
    # Earlier sections publish their own levels, and the whole suite runs well inside the 1.5 s
    # window, so those marks are still live here and would suppress a channel this section wants to
    # hear. Start from a clean slate rather than from whatever ran before.
    server._ann_self_writes.clear()

    # A LEVEL. Marked by fader INDEX, echoed by SOURCE CODE, so this exercises the translation:
    # fader index 8 carries source code 9 (USB 2).
    server._ann_publish_level("fader", 8, 0.60)
    SPOKEN.clear()
    server._ann_handle_report(xpoint_packet(9, 0, 0.60), st)
    check("a level set from the app is not spoken back at us", SPOKEN, [])
    st["levels_at"] = time.time() - (server._ANN_SETTLE_S + 0.01)
    server._ann_flush_pending(st)
    check("and the settle flush does not say it a moment later either", SPOKEN, [])

    SPOKEN.clear()
    server._ann_handle_report(xpoint_packet(1, 0, 0.45), st)
    check("a DIFFERENT channel moving on the desk still speaks at once",
          SPOKEN, ["45 percent, Mic 2"])

    server._ann_self_writes[("fader", 9)] = time.time() - (server._ANN_SELF_ECHO_S + 0.1)
    st["said_at"][("fader", 9)] = time.time() - (server._ANN_LEVEL_GAP_S + 0.01)
    SPOKEN.clear()
    server._ann_handle_report(xpoint_packet(9, 0, 0.70), st)
    check("and a hand on that same fader is audible again once the window passes",
          SPOKEN, ["70 percent, USB 2 (Secondary)"])

    # A MUTE, keyed by strip index (Mic 2 sits on fader 7 = strip 6 in this layout).
    server._ann_publish_mute(6, True)
    SPOKEN.clear()
    server._ann_handle_report(state_packet("channelOutputMute", 6, True), st)
    check("the echo of a mute we set is silent too", SPOKEN, [])
    check("but the cache still follows it, so the ring quotes the truth", st["muted"], {6: True})
    server._ann_self_writes[("mute", 6)] = time.time() - (server._ANN_SELF_ECHO_S + 0.1)
    server._ann_handle_report(state_packet("channelOutputMute", 6, False), st)
    check("a mute pressed ON THE DESK still speaks", SPOKEN, ["Mic 2 unmuted"])

    # A GAIN, keyed by the label the push side builds (Mic 1's channel-address selector is 0x6E).
    server._ann_mark_self_gain(1, True)
    SPOKEN.clear()
    server._ann_handle_report(chan_gain_packet(b"inputMicrophoneGain", 0x6E, 44), st)
    check("a gain set from the app is not spoken back", SPOKEN, [])
    server._ann_self_writes[("gain", "Mic 1 gain")] = time.time() - (server._ANN_SELF_ECHO_S + 0.1)
    server._ann_handle_report(chan_gain_packet(b"inputMicrophoneGain", 0x6E, 45), st)
    check("but the encoder moving that gain does", SPOKEN, ["Mic 1 gain 45 d B"])
    server._ann_live_state = None
    server._ann_self_writes.clear()

    # ---- the announcer's VOICE and OUTPUT DEVICE ----------------
    # No sound is made here and no setting is left changed: SAPI's two lists are stubbed, the
    # settings writer is stubbed, and the originals go back at the end. What is under test is the
    # matching (exact wins, substring accepted, nonsense refused), that a change RESTARTS the speech
    # worker rather than being applied to a live one, and that the confirmation is spoken so the new
    # voice and output are heard rather than trusted.
    print("\n-- announcer voice and output device")
    _real_lists, _real_update = server._sapi_lists, server.update_settings
    _saved = (server._ann_voice, server._ann_say_device, server._ann_worker_gen)
    WROTE: dict = {}
    server._sapi_lists = lambda refresh=False: {
        "voices": ["Microsoft Hazel Desktop - English (Great Britain)",
                   "Microsoft Zira Desktop - English (United States)"],
        "outputs": ["Speakers (RODECaster Pro II Main Stereo)", "Speakers (Realtek(R) Audio)"],
    }
    server.update_settings = lambda **kw: WROTE.update(kw)
    try:
        SPOKEN.clear()
        gen0 = server._ann_worker_gen
        res = server.action_set_announce_voice(
            "Microsoft Zira Desktop - English (United States)")
        check_true("an exact voice name is accepted", res.get("ok"))
        check("and it is what the worker will be started with", server._ann_voice,
              "Microsoft Zira Desktop - English (United States)")
        check_true("the change restarts the worker (voice is fixed when the SpVoice is made)",
                   server._ann_worker_gen > gen0)
        check("it is remembered between sessions", WROTE.get("announcer_voice"),
              "Microsoft Zira Desktop - English (United States)")
        check_true("and a sample is SPOKEN, so the new voice is heard not assumed",
                   len(SPOKEN) == 1 and "Zira" in SPOKEN[0])

        res = server.action_set_announce_voice("hazel")
        check_true("a partial name still works (a script, or an older saved setting)", res.get("ok"))
        check("resolved to the full name SAPI reports", server._ann_voice,
              "Microsoft Hazel Desktop - English (Great Britain)")

        SPOKEN.clear()
        gen1 = server._ann_worker_gen
        res = server.action_set_announce_voice("Microsoft Aria (Natural)")
        check_true("a voice this computer does not have is REFUSED", not res.get("ok"))
        check("the setting is left alone", server._ann_voice,
              "Microsoft Hazel Desktop - English (Great Britain)")
        check("the worker is not restarted for a refused change", server._ann_worker_gen, gen1)
        check("and nothing is spoken", SPOKEN, [])

        res = server.action_set_announce_voice("")
        check_true("empty means the Windows default, which is a real choice", res.get("ok"))
        check("so the worker is started with no -Voice at all", server._ann_voice, "")

        SPOKEN.clear()
        res = server.action_set_announce_output("Realtek")
        check_true("an output can be picked by part of its name", res.get("ok"))
        check("resolved to the full device name", server._ann_say_device,
              "Speakers (Realtek(R) Audio)")
        check("remembered too", WROTE.get("announcer_output"), "Speakers (Realtek(R) Audio)")
        check_true("with the confirmation spoken THROUGH it, which is the real test",
                   len(SPOKEN) == 1 and "Realtek" in SPOKEN[0])
        check_true("an output this computer does not have is refused",
                   not server.action_set_announce_output("Focusrite").get("ok"))
        check("and that setting is left alone too", server._ann_say_device,
              "Speakers (Realtek(R) Audio)")
        # A name with a regex/wildcard character in it must be matched literally: the real device
        # here is "Speakers (Realtek(R) Audio)", and PowerShell's -like would read [ ] as wildcards.
        check_true("a name with brackets in it matches literally",
                   server.action_set_announce_output("Realtek(R)").get("ok"))
    finally:
        server._sapi_lists, server.update_settings = _real_lists, _real_update
        (server._ann_voice, server._ann_say_device, server._ann_worker_gen) = _saved
finally:
    rcp_transfer.send_record = _real_send
    server._ann_live_state = None

print("\nONE FORMATTER, TWO WORD ORDERS, AND THEY MUST STAY OPPOSITE")
# Both announcements come through _ann_level_words. The orders are opposite ON PURPOSE and drifting
# to a single order would break one of them, so assert both from the shared function itself.
check("a level that MOVED leads with the number, because the name gets cut off mid-turn",
      server._ann_level_words("Mic 1", 0.30, name_first=False), "30 percent, Mic 1")
check("and drops the unit during a run",
      server._ann_level_words("Mic 1", 0.30, name_first=False, say_percent=False), "30, Mic 1")
check("a SELECTION leads with the name, because that is the question the press asks",
      server._ann_level_words("Chat", 0.97, name_first=True), "Chat 97 percent")
check("percent is clamped and rounded once, in one place",
      [server._ann_level_words("X", v, name_first=False) for v in (-0.5, 0.006, 1.4)],
      ["0 percent, X", "1 percent, X", "100 percent, X"])

print("\nTHE TWO INDEX SPACES ARE REAL: A SOURCE ON NO FADER STILL GETS ANNOUNCED")
# This is why speech is keyed by SOURCE CODE and the encoder ring by FADER INDEX, and why they
# cannot be collapsed into one. The Pro II always has one source off-fader (10 assignable, 9
# faders), and its level still moves and still deserves its name. Collapsing to fader indices
# would silence it, which is exactly the regression this guards.
# SMART pads (source 11) left off the layout. Not CallMe 1: its cross-point selector is
# 0x4C + 13*16, which is past a single byte, so it cannot even be expressed as a one-byte push.
channels.publish_strip_vector([0, 1, 2, 3, 10, 7, 8, 9, 16])   # source 11 is on no fader
said, st = run([xpoint_packet(11, 0, 0.42)], cold_cache=False)
check("an OFF-FADER source is still named and spoken", said, ["42 percent, SMART pads"])
check("and it takes no ring slot, because it has no fader",
      [k for k in (st.get("known") or {})], [])
said, _ = run([xpoint_packet(7, 0, 0.42)], cold_cache=False)
check("a source that IS on a fader still works normally", said, ["42 percent, USB 1 (Main Stereo)"])

print("\nA STEREO-LINKED PAIR IS NAMED, NOT SPOKEN AS A BARE CODE")
# A linked pair is its OWN source code (4, 5, 6) and can sit in the fader vector, but it is
# deliberately NOT assignable - so an announcer lookup through the assignable-only map is one link
# away from reading a bare number aloud ("0 percent, Source 4" for a linked Mic 1 + 2). Names come
# from FaderSpec.all_labels, and there is no `code_label` helper to reach for the narrow map again.
check("a pair code has a spoken name at all",
      [server._ann_source_label(c) for c in (4, 5, 6)],
      ["Mic 1 + 2", "Mic 2 + 3", "Mic 3 + 4"])
check("and no assignable source lost its name",
      [server._ann_source_label(c) for c in (0, 7, 11)],
      ["Mic 1", "USB 1 (Main Stereo)", "SMART pads"])
# The whole way through: a cross-point push for a linked pair, spoken.
channels.publish_strip_vector([4, -1, 2, 3, 10, 7, 8, 9, 11])
said, _ = run([xpoint_packet(4, 0, 0.0)], cold_cache=False)
check("a level push for a linked pair says the pair, not 'Source 4'", said,
      ["0 percent, Mic 1 + 2"])
check("and the strip it sits on is named the same way", server._ann_channel_label(0), "Mic 1 + 2")
check("the vacated fader beside it is not claimed by anything",
      server._ann_channel_label(1), "Channel 2")

print("\nA CROSS-POINT SELECTOR PAST 255 IS STILL DECODED")
# The selector is CROSS_POINT_BASE + 13*source + output, which leaves byte range at source 14, so
# Virtual A and Virtual B are addressed with two bytes. Their low bytes alone are 2 and 15, both
# below the cross-point base, so reading one byte discards the push and the fader moves in silence -
# no level, no name. Only codes 14 and 15 reach that far, and only in Expanded.
channels.publish_usb_expanded(True)
channels.publish_strip_vector([0, 12, 13, 14, 15, 11, 7, 8, 9])
check("a one-byte selector is unaffected",
      run([xpoint_packet(12, 0, 0.25)], cold_cache=False)[0], ["25 percent, Game"])
check("a two-byte selector is spoken, not dropped",
      run([xpoint_packet(14, 0, 0.5)], cold_cache=False)[0], ["50 percent, Virtual A"])
check("and so is the one past it",
      run([xpoint_packet(15, 0, 1.0)], cold_cache=False)[0], ["100 percent, Virtual B"])

print()
if FAILED:
    print(f"{FAILED} CHECK(S) FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
