"""Unit tests for the control announcer's report-04 decoder. NO DEVICE, no HID, no writes.

Feeds synthetic report-04 packets (built to the desk's own frame shapes and selector bytes) into
announcer.handle_report with speech.say stubbed, and asserts what would be SPOKEN. The desk's state
is the desk model's (`desk_model`), set up on the pinned Pro II's shape (`desk`), and every packet
reaches it first, as the listener's do.

Run: python DevTools/test_announcer.py
"""
from __future__ import annotations

import struct
import sys
import time

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
import announcer
import channels
import desk_live
import desk_model
import devices
import layout_fixture
import pad_shortcuts
import rcp_hid
import rcp_read
import rcp_transfer
import server
import settings_file
import speech
from _testkit import addr, check, check_true, patched, push, where

# PIN THE DESK. This suite asserts Pro II strip names and spoken labels, so with a Duo attached it
# would fail on the Duo's different channel set and look like a regression. Offline tests must not
# depend on what happens to be plugged into the machine running them. The Duo's own announcer
# expectations live in DevTools/test_duo_parity.py.
devices.force_device(devices.PRO2)

SPOKEN: list[str] = []
speech.say = lambda text: SPOKEN.append(text)          # capture instead of speak


def v_bool(on: bool) -> bytes:
    return bytes([0x01, 0x01, 0x02 if on else 0x03])


def v_int(value: int) -> bytes:
    return bytes([0x01, 0x05, 0x01]) + int(value).to_bytes(4, "little", signed=True)


def v_float(value: float) -> bytes:
    return bytes([0x01, 0x09, 0x04]) + struct.pack("<d", value)


def v_str(text: str) -> bytes:
    body = b"\x05" + text.encode() + b"\x00"
    return bytes([0x01, len(body)]) + body


LAYOUT = [0, 7, 2, 3, 10, 11, 1, 8, 9, 9]      # USB 1 on strip 1, Mic 2 up on fader 7 (strip 6)
desk, at, layout = _testkit.hold_desk, _testkit.hold, _testkit.hold_layout


def select(strip: int) -> None:
    """The channel a channel button has given the encoder, as the desk model holds it."""
    desk_model.apply_push(where("systemChannelSelected"), "systemChannelSelected", strip, "int")


def state_packet(prop: str, strip_idx: int, on: bool) -> bytes:
    """A pushed STATE property at CHANNEL[strip index], as the desk pushes it."""
    return push(addr(rcp_transfer.channel_key(strip_idx)), prop, v_bool(on))


def press_packet(name: str, down: bool = True, button: int = 1) -> bytes:
    """A press EDGE, addressed at the physical BUTTON (0x63 + button - 1), not at a strip."""
    return push([0, 0x62 + button], name, v_bool(down))


def bank_packet(bank0: int) -> bytes:
    return push(where("selectedBank"), "selectedBank", v_int(bank0))


def run(pkts, st=None, cold_cache=True):
    """Feed packets and return what was spoken. `cold_cache` starts from a desk model that has
    never read the desk, so no layout is known."""
    SPOKEN.clear()
    if cold_cache:
        desk_model._reset()
    st = st if st is not None else {"pending": None, "at": 0.0}
    for p in pkts:
        announcer.handle_report(p, st)
    return list(SPOKEN), st


print("MUTE / SOLO STATE")
# No layout known: must NOT read the device, must fall back to a strip number.
said, _ = run([state_packet("channelOutputMute", 1, True)])
check("mute on, no layout", said, ["Channel 2 muted"])
said, _ = run([state_packet("channelOutputMute", 1, False)])
check("unmute, no layout", said, ["Channel 2 unmuted"])

# A real layout, USB 1 on strip 1 and Mic 2 pushed to strip 6.
desk(LAYOUT)
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

print("\nNAMES SURVIVE THE MODEL BEING UNTRUSTED")
# An untrusted desk model makes a WRITE read the layout again; naming takes the layout as last
# seen, so an announcement still names the channel rather than saying "Channel 4".
desk(LAYOUT)
desk_model.distrust()
check("the model is untrusted", desk_model.trusted(), False)
said, _ = run([state_packet("channelOutputMute", 2, True)], cold_cache=False)
check("still names the channel", said, ["Mic 3 muted"])
said, _ = run([state_packet("channelOutputMute", 4, True)], cold_cache=False)
check("stereo source too", said, ["Bluetooth muted"])

print("\nA MIXER PAD OWNS THE MUTE AND CUE LINES WHILE IT ACTS")
desk(LAYOUT)
# `padActive` is addressed at the pad's STREAM POSITION, one level, same shape as a state push.
def pad_active(position: int, on: bool) -> bytes:
    return push([position], "padActive", v_bool(on))

MIXER_POS, SOUND_POS = 5, 6
pad_shortcuts.mixer_positions = frozenset({MIXER_POS})
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
# The desk model still has to be true: the encoder ring quotes it whether or not anything was said.
run([pad_active(MIXER_POS, True), state_packet("channelOutputMute", 1, True)], cold_cache=False)
check("the desk model holds the mute even when silent", desk_model.mutes()[1], True)
# A SOUND pad moves nothing, so it must not buy silence.
announcer._mixer_quiet_until = 0.0
said, _ = run([pad_active(SOUND_POS, True), state_packet("channelOutputMute", 1, True)],
              cold_cache=False)
check("a non-mixer pad suppresses nothing", said, ["USB 1 (Main Stereo) muted"])
# And the window is short: a mute after it has passed speaks as usual.
announcer._mixer_quiet_until = 0.0
said, _ = run([state_packet("channelOutputMute", 1, False)], cold_cache=False)
check("a real mute once the window passes", said, ["USB 1 (Main Stereo) unmuted"])
pad_shortcuts.mixer_positions = frozenset()

print("\nA JOB THAT REWRITES THE DESK HOLDS THE DESK-CHANGE SPEECH")
SPOKEN.clear()
_was_enabled = speech.enabled.is_set()
speech.enabled.set()
announcer.hold_desk_speech()
if not _was_enabled:
    speech.enabled.clear()
check("the hold says nothing itself: the job's page or window speaks for it", list(SPOKEN), [])
announcer.hold_desk_speech()
said, _ = run([state_packet("channelOutputMute", 1, True)], cold_cache=False)
check("a second hold says nothing more, and a mute push is silent", said, [])
check("the desk model still holds the mute while held", desk_model.mutes()[1], True)
announcer.release_desk_speech()
said, _ = run([state_packet("channelOutputMute", 1, False)], cold_cache=False)
check("still held while one job is running", said, [])
announcer.release_desk_speech()
said, _ = run([state_packet("channelOutputMute", 1, True)], cold_cache=False)
check("held for the tail after the last job", said, [])
announcer._hold_until = 0.0
said, _ = run([state_packet("channelOutputMute", 1, False)], cold_cache=False)
check("speaks again once the tail has passed", said, ["USB 1 (Main Stereo) unmuted"])

print("\nA FADER SOURCE CHANGE ON THE STREAM MOVES THE NAME")
desk(LAYOUT)
# channelInputSource is pushed on the same CHANNEL[strip] node; value = the new source code. The
# announcer does not hear it: the listener puts it in the desk model, which a fader source change
# untrusts (`desk_model.STRUCTURAL`), so the next write reads the layout again.
check("the announcer does not listen for it", "channelInputSource" in announcer._HEARD, False)
moved = desk_live.decode(push(addr(rcp_transfer.channel_key(1)), "channelInputSource", v_int(10)))
desk_model.apply_push(moved.path, moved.name, moved.value, moved.kind)
check("naming layout updated", channels.last_known_strip_vector()[1], 10)
check("the model is untrusted, so writes re-read", desk_model.trusted(), False)
said, _ = run([state_packet("channelOutputMute", 1, True)], cold_cache=False)
check("mute on that strip names the NEW source", said, ["Bluetooth muted"])

print("\nPRESS EDGES ARE HELD, THEN SUPERSEDED BY THE STATE")
desk(LAYOUT)
# The real sequence: edge, then the state ~70 ms later. Only the state should be spoken.
for button in (1, 2, 3):
    said, st = run([press_packet("mutePressed", button=button),
                    state_packet("channelOutputMute", 1, True)], cold_cache=False)
    check(f"mute button {button} + state", said, ["USB 1 (Main Stereo) muted"])
    check(f"mute button {button} pending cleared", st["pending"], None)

said, st = run([press_packet("soloPressed"), state_packet("channelCueEnable", 0, True)],
               cold_cache=False)
check("solo edge + state", said, ["Mic 1 solo on"])

print("\nFALLBACK WHEN NO STATE ARRIVES (device-agnostic safety net)")
said, st = run([press_packet("mutePressed", button=2)], cold_cache=False)
check("edge alone says nothing yet", said, [])
SPOKEN.clear()
announcer.flush_pending(st)
check("not flushed before the window", SPOKEN, [])
st["at"] = time.monotonic() - (announcer._PENDING_S + 0.05)
announcer.flush_pending(st)
check("flushed to the generic word after it", SPOKEN, ["Mute"])
check("pending cleared after flush", st["pending"], None)

said, st = run([press_packet("soloPressed", button=3)], cold_cache=False)
st["at"] = time.monotonic() - (announcer._PENDING_S + 0.05)
SPOKEN.clear()
announcer.flush_pending(st)
check("solo fallback", SPOKEN, ["Solo"])

print("\nRELEASE EDGES AND NOISE ARE IGNORED")
said, _ = run([press_packet("mutePressed", down=False, button=2)], cold_cache=False)
check("release edge", said, [])
said, _ = run([push([0x4C], "mixMute", v_bool(True))], cold_cache=False)
check("per-cross-point mixMute ignored", said, [])
said, _ = run([b"\x03" + state_packet("channelOutputMute", 1, True)[1:]], cold_cache=False)
check("non report-04 ignored", said, [])
# A selector outside the strip range must not be announced as some phantom channel.
said, _ = run([push([0x02], "channelOutputMute", v_bool(True))], cold_cache=False)
check("out-of-range selector ignored", said, [])
# A SLICE OF A MODEL DUMP is not a push, even when a whole state record sits inside it: a page
# reading the desk must never sound like the desk changing. Dump frames are 255 dense bytes.
rec = state_packet("channelOutputMute", 1, True)
dump_slice = (b"\x04" + b"\x30" * 40 + rec[5:5 + int.from_bytes(rec[1:5], "little")]).ljust(
    256, b"\x31")
said, _ = run([dump_slice], cold_cache=False)
check("a model-dump slice is ignored", said, [])

print("\nSPOKEN POSITIONS: IMMEDIATE, INTERRUPTING, NEVER STACKING")


def xpoint_packet(source_code: int, output: int, level: float) -> bytes:
    """A pushed fader position: mixLevelWithAnchor on the cross-point selector for (source, output),
    value the ASCII string 'level|anchor'. The desk sends one per cross-point, 13 per source.

    THE INDEX IS NOT ALWAYS ONE BYTE. MIX[13*source + output] passes root child 255 at source 14,
    so Virtual A and Virtual B arrive with a two-byte index and the width byte says so."""
    path = addr(rcp_transfer.cross_point_key(output, source_code))
    return push(path, "mixLevelWithAnchor", v_str(f"{level:g}|{level:g}"))


def monlevel_packet(level: float) -> bytes:
    return push(addr(rcp_transfer.OUTPUT), "outputMonLevel", v_float(level))


print("OUTPUT MUTES")
# The monitor's mute, which an encoder hold on the monitor level toggles. Addressed at the OUTPUT
# block, not a strip, so it must not be read as a channel's mute.
MON_MUTE = addr(rcp_transfer.OUTPUT)
said, st_o = run([push(MON_MUTE, "outputMonMute", v_bool(True))], cold_cache=False)
check("monitor mute is spoken", said, ["Monitor speakers muted"])
said, _ = run([push(MON_MUTE, "outputMonMute", v_bool(True))], st=st_o, cold_cache=False)
check("a repeat of the same state is not spoken again", said, [])
said, _ = run([push(MON_MUTE, "outputMonMute", v_bool(False))], st=st_o, cold_cache=False)
check("monitor unmute is spoken", said, ["Monitor speakers unmuted"])
announcer.mark_self_write(("output_mute", "outputMonMute"))
said, _ = run([push(MON_MUTE, "outputMonMute", v_bool(True))], st=st_o, cold_cache=False)
check("the app's own monitor mute is not spoken back", said, [])
at(rcp_transfer.OUTPUT, "outputMonMute", False, "bool")
hold_st = {"pending": None, "at": 0.0, "enc_down_at": 1.0, "enc_hold": False}
run([push(MON_MUTE, "outputMonMute", v_bool(True))], st=hold_st, cold_cache=False)
check("a monitor mute during an encoder press marks it a hold", hold_st["enc_hold"], True)
# The hold's mute says where the encoder is, so the next press can name what it moves on to even
# when no level has moved since the app started.
check("a monitor hold places the ring on the monitor", hold_st["enc_pos"],
      announcer._encoder_ring().index(("monitor", 0)))
fader_hold = {"pending": None, "at": 0.0, "enc_down_at": 1.0, "enc_hold": False}
run([state_packet("channelOutputMute", 6, True)], st=fader_hold, cold_cache=False)
check("a virtual fader hold places the ring on that fader", fader_hold["enc_pos"],
      announcer._encoder_ring().index(("fader", 6)))


def fresh_st():
    return {"pending": None, "at": 0.0, "levels": {}, "levels_at": 0.0, "enc_pos": None,
            "enc_down_at": None, "enc_hold": False, "said": {}, "said_at": {}}


# Spoken AS IT MOVES: waiting for the movement to settle is laggy, and since the speech engine
# interrupts the previous phrase, immediate is both snappier and safe from stacking.
# NUMBER FIRST, then the name. Leading with the NAME means riding a physical fader almost never
# says WHICH fader: mid-sweep updates drop the name entirely, and the closing named phrase only
# survives when the last push happens to be held rather than spoken. Leading with the changing fact
# means it always lands, and the name completes whenever the movement pauses.
st = fresh_st()
SPOKEN.clear()
announcer.handle_report(xpoint_packet(0, 0, 0.30), st)
check("first update spoken at once, no waiting", SPOKEN, ["30 percent, Mic 1"])

SPOKEN.clear()
for out in range(1, 13):
    announcer.handle_report(xpoint_packet(0, out, 0.30), st)
check("the same position on the other 12 cross-points adds nothing", SPOKEN, [])

SPOKEN.clear()
st["said_at"][("fader", 0)] = time.monotonic() - (announcer._LEVEL_GAP_S + 0.01)
announcer.handle_report(xpoint_packet(0, 0, 0.42), st)
# Mid-sweep the number leads and "percent" is dropped: the unit is obvious once you are moving, and
# the spare syllables would only eat into the name before the next update interrupts it.
check("a continuing sweep leads with the bare number", SPOKEN, ["42, Mic 1"])

# After a pause, the full phrase comes back, so you always know what you are holding.
SPOKEN.clear()
st["run_at"] = time.monotonic() - (announcer._RUN_S + 0.1)
st["said_at"][("fader", 0)] = time.monotonic() - (announcer._LEVEL_GAP_S + 0.01)
announcer.handle_report(xpoint_packet(0, 0, 0.50), st)
check("after a pause it names the channel again", SPOKEN, ["50 percent, Mic 1"])

SPOKEN.clear()
for lvl in (0.44, 0.46, 0.48):
    announcer.handle_report(xpoint_packet(0, 0, lvl), st)
check("updates inside the spacing window are held", SPOKEN, [])
st["levels_at"] = time.monotonic() - (announcer._SETTLE_S + 0.01)
announcer.flush_pending(st)
check("the closing value names the channel as confirmation", SPOKEN, ["48 percent, Mic 1"])
SPOKEN.clear()
announcer.flush_pending(st)
check("and not repeated", SPOKEN, [])

SPOKEN.clear()
announcer.handle_report(monlevel_packet(0.03), fresh_st())
check("monitor level", SPOKEN, ["3 percent, Monitor"])

SPOKEN.clear()
announcer.handle_report(xpoint_packet(7, 0, 1.0), fresh_st())
check("names the source, not the strip", SPOKEN, ["100 percent, USB 1 (Main Stereo)"])

SPOKEN.clear()
announcer.levels_on = False
st2 = fresh_st()
announcer.handle_report(xpoint_packet(0, 0, 0.5), st2)
announcer.handle_report(monlevel_packet(0.5), st2)
check("silent when the levels toggle is off", SPOKEN, [])
announcer.levels_on = True

SPOKEN.clear()
announcer.handle_report(state_packet("channelOutputMute", 1, True), fresh_st())
check("a mute still gets through", SPOKEN, ["USB 1 (Main Stereo) muted"])

print("\nAFTER AN ENCODER PRESS, ONE CLICK NAMES WHAT YOU ARE HOLDING")
# The press is detected but says nothing about WHICH channel it selected, so the first movement
# must name it straight away - otherwise you nudge blind and wait for each settle.
# Addressed at 0x06.
enc_press = push([0x06], "encoderPressed", v_bool(True))
# The RELEASE edge, which the desk does push. Announcing happens here rather than on the press,
# because a HOLD is the desk's own mute gesture and must not advance our ring - see announcer._ENC_HOLD_S.
enc_release = push([0x06], "encoderPressed", v_bool(False))


def tap(st_, packets=()):
    """A short press: down, then up, with anything in between (e.g. the desk's mute on a hold)."""
    announcer.handle_report(enc_press, st_)
    for p in packets:
        announcer.handle_report(p, st_)
    announcer.handle_report(enc_release, st_)


st = {"pending": None, "at": 0.0, "levels": {}, "levels_at": 0.0}
SPOKEN.clear()
announcer.handle_report(enc_press, st)
check("the press edge alone says nothing", SPOKEN, [])
announcer.handle_report(enc_release, st)
check("the release is what announces", SPOKEN, ["Select"])
SPOKEN.clear()
announcer.handle_report(xpoint_packet(8, 0, 0.72), st)     # source 8 = Chat, one click
check("first click names it immediately, no waiting", SPOKEN, ["72 percent, Chat"])
check("and nothing is left pending for the settle", st["levels"], {})
SPOKEN.clear()
st["levels_at"] = time.monotonic() - (announcer._SETTLE_S + 0.05)
announcer.flush_pending(st)
check("the settle does not repeat it", SPOKEN, [])

# Keep turning: the closing position IS announced, because it is a different value.
SPOKEN.clear()
for lvl in (0.75, 0.80, 0.85):
    announcer.handle_report(xpoint_packet(8, 0, lvl), st)
check("still silent mid-sweep", SPOKEN, [])
st["levels_at"] = time.monotonic() - (announcer._SETTLE_S + 0.05)
announcer.flush_pending(st)
check("closing position spoken once", SPOKEN, ["85 percent, Chat"])

# A second press re-arms it, so the next channel you land on is named at once too.
SPOKEN.clear()
tap(st)
announcer.handle_report(xpoint_packet(9, 0, 0.9), st)      # source 9 = USB 2
check("second press re-arms the naming", SPOKEN, ["Select", "90 percent, USB 2 (Secondary)"])

print("\nPRESS AND HOLD IS THE DESK'S MUTE, NOT A RING STEP")
# Holding pushes channelOutputMute about 765 ms in (a measured figure), while still held, and
# mutes the channel selected BEFORE the press. Announcing the next ring position on the press
# edge would name the wrong channel AND leave our ring one step out.
desk(LAYOUT)                                                 # faders 7/8/9 = Mic 2, Chat, USB 2
st = fresh_st()
st["enc_pos"] = 0                                            # sitting on fader 7 (Mic 2)
SPOKEN.clear()
tap(st, [state_packet("channelOutputMute", 6, True)])        # strip 6 = fader 7 = Mic 2
check("a hold speaks only the mute", SPOKEN, ["Mic 2 muted"])
check("and does NOT advance the ring", st["enc_pos"], 0)
check("and the desk model holds the mute", desk_model.mutes()[6], True)

# The duration backstop, for a desk that pushes no mute state (the Duo is unobserved here).
st = fresh_st()
st["enc_pos"] = 0
SPOKEN.clear()
announcer.handle_report(enc_press, st)
st["enc_down_at"] = time.monotonic() - (announcer._ENC_HOLD_S + 0.05)
announcer.handle_report(enc_release, st)
check("a long hold with no state pushed is still not a ring step", SPOKEN, [])
check("ring unmoved by the backstop", st["enc_pos"], 0)

# A muted channel quotes the mute, not a level: a healthy percentage on something you cannot hear
# is worse than no number at all.
st = fresh_st()
st["enc_pos"] = 0
at(rcp_transfer.fader_key(7), "faderLevel", 123, "int")      # 97 percent
at(rcp_transfer.channel_key(7), "channelOutputMute", True, "bool")
SPOKEN.clear()
tap(st)
check("the ring says muted instead of the level", SPOKEN, ["Chat, muted"])
check("and the ring did advance, because this was a short press", st["enc_pos"], 1)

# Unmuting restores the level.
SPOKEN.clear()
announcer.handle_report(state_packet("channelOutputMute", 7, False), st)
check("the unmute is announced", SPOKEN, ["Chat unmuted"])
st["enc_pos"] = 0
SPOKEN.clear()
tap(st)
check("and the level is quoted again", SPOKEN, ["Chat 97 percent"])

# A fader move with no press behind it is announced just the same, and just as promptly: the encoder
# press only decides WHOSE name we can predict, it is not what makes levels audible.
SPOKEN.clear()
st2 = fresh_st()
announcer.handle_report(xpoint_packet(0, 0, 0.4), st2)
check("a plain fader move is spoken at once too", SPOKEN, ["40 percent, Mic 1"])
SPOKEN.clear()
st2["levels_at"] = time.monotonic() - (announcer._SETTLE_S + 0.05)
announcer.flush_pending(st2)
check("and the settle adds nothing to repeat", SPOKEN, [])

print("\nA TURN THAT MOVES NOTHING SAYS SO")
# The encoder is only bound to a level while the desk is on a page that binds one; pressing the BANK
# buttons moves the screen to the pads page and unbinds it. The desk
# still reports the rotation, so we can tell the operator the turn did nothing rather than leave them
# wondering whether the app has broken.


def encsig_packet(delta: int = 1) -> bytes:
    """THE SHAPE THE DESK SENDS:

        ... encoderSignal | 00 | 01 0b 08 | 01 05 01 <int32 LE> | 01 ...

    A COMPOSITE (type 0x08) whose first member is the step. Keep it a composite, or this stops
    testing the one property whose value is not where every other property puts it."""
    return push([0x06], "encoderSignal",
                bytes([0x01, 0x0B, 0x08]) + v_int(delta) + v_bool(True))


def enccolour_packet(value: int) -> bytes:
    return push([0x07], "encoderColour", v_int(value))


# THE DELTA HAS TO SURVIVE THE DECODER. Pin the value itself, not just the behaviour built on it: a
# decoder that loses it turns every turn into a delta of 0, which announcer._at_limit reads as "do not
# know" and lets through, so the at-limit checks further down would pass while proving nothing.
check("a clockwise click decodes as +1", rcp_read.parse_push(encsig_packet(1))[2][0][0], 1)
check("and an anticlockwise click as -1", rcp_read.parse_push(encsig_packet(-1))[2][0][0], -1)

st = fresh_st()
SPOKEN.clear()
for _ in range(6):
    announcer.handle_report(encsig_packet(1), st)
check("the rotation itself is not announced", SPOKEN, [])
announcer.flush_pending(st)
check("and nothing is said inside the wait", SPOKEN, [])
st["enc_turn_at"] = time.monotonic() - (announcer._ENC_DEAD_S + 0.05)
announcer.flush_pending(st)
# With no channel selected the encoder is put back without a word (encoder_home).
check("a turn that moved nothing with no channel selected says nothing", SPOKEN, [])

# When we know WHICH channel is holding the encoder, name it - and name what that channel HAS, never
# what it lacks. A phrase like "SMART pads is selected and has nothing..." breaks that, and lands on
# any turn the ring does not see: Mic 1 gets told it has nothing to adjust while the encoder is
# adjusting its gain. Routing it through announcer._selected_phrase works the claim out from the strip.
st = fresh_st()
desk(LAYOUT)
select(5)                              # strip 5 = SMART pads
st["enc_turn_at"] = time.monotonic() - (announcer._ENC_DEAD_S + 0.05)
SPOKEN.clear()
announcer.flush_pending(st)
check("it names the channel holding the encoder", SPOKEN[0].startswith("SMART pads selected"), True)
check("and says what that channel HAS, not what it lacks",
      "nothing" in SPOKEN[0].lower(), False)
check("and names the way out, which is the channel button",
      "channel button again" in SPOKEN[0], True)

# A MIC holding the encoder must never be told it has nothing to adjust: its gain is exactly what the
# encoder is moving.
st = fresh_st()
desk(LAYOUT)
select(0)                              # strip 0 = Mic 1
st["enc_turn_at"] = time.monotonic() - (announcer._ENC_DEAD_S + 0.05)
SPOKEN.clear()
announcer.flush_pending(st)
check("a selected MIC is described by its gain", SPOKEN[0].startswith("Mic 1 selected"), True)
check("and is never told it has nothing to adjust", "nothing" in SPOKEN[0].lower(), False)

# AN ENCODER PRESS DOES NOT TAKE THE ENCODER BACK OFF A SELECTED CHANNEL: systemChannelSelected
# stays at 0 across presses. Advancing our ring here would announce USB 1
# and then Chat while the desk never leaves Mic 1, and leave enc_pos pointing at an entry the
# encoder is not on - the same value announcer._at_limit reads to decide "is this against a stop".
st = fresh_st()
desk(LAYOUT)
select(0)
st["enc_pos"] = 1
SPOKEN.clear()
announcer.handle_report(press_packet("encoderPressed", True), st)
announcer.handle_report(press_packet("encoderPressed", False), st)
check("the ring does NOT advance while a channel is selected", st["enc_pos"], 1)
check("and the press says what the encoder is still on", SPOKEN, ["Mic 1 selected. Encoder sets its gain"])

# With nothing selected it walks the ring exactly as before.
st = fresh_st()
select(-1)
st["enc_pos"] = 0
SPOKEN.clear()
announcer.handle_report(press_packet("encoderPressed", True), st)
announcer.handle_report(press_packet("encoderPressed", False), st)
check("but it still advances when nothing is selected", st["enc_pos"], 1)

st = fresh_st()
SPOKEN.clear()
announcer.handle_report(encsig_packet(1), st)
announcer.handle_report(xpoint_packet(8, 0, 0.72), st)
check("a turn that moves a level announces the level", SPOKEN, ["72 percent, Chat"])
check("and cancels the not-linked warning", st.get("enc_turn_at"), None)
SPOKEN.clear()
announcer.flush_pending(st)
check("so nothing complains afterwards", SPOKEN, [])

st = fresh_st()
SPOKEN.clear()
announcer.handle_report(enccolour_packet(-1), st)
announcer.handle_report(enccolour_packet(7), st)
check("the encoder's colour is logged, never spoken", SPOKEN, [])

st = fresh_st()
SPOKEN.clear()
announcer.levels_on = False
announcer.handle_report(encsig_packet(1), st)
st["enc_turn_at"] = time.monotonic() - (announcer._ENC_DEAD_S + 0.05)
announcer.flush_pending(st)
check("silent when levels are off", SPOKEN, [])
announcer.levels_on = True

print("\nTHE ENCODER RING: PREDICT WHAT A PRESS SELECTED")
# The desk's order: fader 7 -> fader 8 -> fader 9 -> monitor -> round again. Once a real
# movement has seated us, every later press can be announced BEFORE it is turned.
desk(LAYOUT)                                                       # faders 7/8/9 = Mic 2, Chat, USB 2


def fader_level_packet(fader_idx: int, value127: int) -> bytes:
    return push(addr(rcp_transfer.fader_key(fader_idx)), "faderLevel", v_int(value127))


st = fresh_st()
SPOKEN.clear()
tap(st)
check("before we know where we are, it just says Select", SPOKEN, ["Select"])

# A real turn on fader 8 (Chat) at 123/127 seats the ring, and the desk model holds the level.
announcer.handle_report(fader_level_packet(7, 123), st)
check("the movement seats the ring", st["enc_pos"], 1)
check("and the level is held", round(desk_model.fader_level(7), 4), round(123 / 127, 4))

SPOKEN.clear()
tap(st)
check("next press predicts fader 9, with its level", SPOKEN, ["USB 2 (Secondary) 0 percent"])
SPOKEN.clear()
tap(st)
check("then the monitor", SPOKEN, ["Monitor level"])
SPOKEN.clear()
tap(st)
check("then round to fader 7, named from the layout", SPOKEN, ["Mic 2 0 percent"])
# With no level known it names the target and says NOTHING about the level, rather than inventing
# a zero - the level only appears once the desk has been read.
desk_model._reset()
layout(LAYOUT)
check("a level never read is not invented", announcer._ring_phrase(fresh_st(), 2),
      "USB 2 (Secondary)")
desk(LAYOUT)

# Levels it already knows are spoken with the value.
at(rcp_transfer.OUTPUT, "outputMonLevel", 0.52, "float")
st["enc_pos"] = 1
SPOKEN.clear()
tap(st)
tap(st)
check("a known level is included", SPOKEN[-1], "Monitor level 52 percent")

# A WRONG prediction self-corrects: the real movement re-seats us.
st["enc_pos"] = 0
announcer.handle_report(fader_level_packet(8, 64), st)     # actually fader 9 moved
check("a real movement overrides the guess", st["enc_pos"], 2)

print("\nTHE RING QUOTES THE DESK MODEL, AND THE ENDS OF THE RANGE ARE NOT 'NOT LINKED'")
# Treating 0 as absent would announce "not linked" when it is simply the floor.
desk(LAYOUT)                                                       # faders 7/8/9 = Mic 2, Chat, USB 2

st = fresh_st()
SPOKEN.clear()
# A level THIS APP writes reaches the ring through the desk model, as the send layer hands it on.
rcp_hid.note_written(rcp_transfer.build_fader_level(addr(rcp_transfer.fader_key(6)), 57))
check("our own fader write says nothing", SPOKEN, [])
st["enc_pos"] = 3                                                 # monitor; next press wraps to 7
tap(st)
check("and the next press quotes the level it set", SPOKEN, ["Mic 2 45 percent"])

# Turning down at zero: nothing moves, and that is a floor, not an unbound encoder.
st = fresh_st()
st["enc_pos"] = 0
at(rcp_transfer.fader_key(6), "faderLevel", 0, "int")
SPOKEN.clear()
announcer.handle_report(encsig_packet(-1), st)
check("a downward turn at 0 is not treated as a dead turn", st.get("enc_turn_at"), None)
st["levels_at"] = time.monotonic() - (announcer._SETTLE_S + 0.05)
announcer.flush_pending(st)
check("so nothing says 'not linked'", SPOKEN, [])

# Turning UP from 0 could move, so a dead turn there is still worth reporting.
SPOKEN.clear()
announcer.handle_report(encsig_packet(1), st)
check("an upward turn at 0 is still watched", st.get("enc_turn_at") is not None, True)

# Same at the top of the range.
st = fresh_st()
st["enc_pos"] = 1
at(rcp_transfer.fader_key(7), "faderLevel", 127, "int")
SPOKEN.clear()
announcer.handle_report(encsig_packet(1), st)
check("an upward turn at maximum is not a dead turn", st.get("enc_turn_at"), None)

# With no level known we cannot tell, so we keep watching rather than guess.
desk_model._reset()
st = fresh_st()
st["enc_pos"] = 2
announcer.handle_report(encsig_packet(-1), st)
check("unknown level means we still watch", st.get("enc_turn_at") is not None, True)

print("\nTHE ENCODER ON A SELECTED CHANNEL: GAIN FOR MICS, LEVEL FOR LINE STRIPS")
# Pressing a channel button hands the encoder to that channel's gain. Mic strips move
# inputMicrophoneGain; line strips (USB 1, Chat, USB 2, Bluetooth) move inputDigitalGain. Decoding
# only the first would announce a working turn on USB 1 as "not linked" while the computer's audio
# audibly changed.


def chan_gain_packet(prop: bytes, chan_sel: int, value: int, trailer: int = 0x02) -> bytes:
    """A pushed property at a channel's INPUTSOURCE node, whose index is two bytes: the low byte is
    `chan_sel`, the high byte `trailer`."""
    return push([chan_sel | trailer << 8], prop.decode(), v_int(value))


def chansel_packet(value: int) -> bytes:
    return push(where("systemChannelSelected"), "systemChannelSelected", v_int(value))


desk(LAYOUT)

st = fresh_st()
SPOKEN.clear()
announcer.handle_report(chansel_packet(1), st)
# The phrase says up front what the encoder will do, worked out from the channel itself: gain for a
# mic, level for a line strip, and "fader level only" for SMART pads, which has no input gain.
check("a channel button names what took the encoder and what it does",
      SPOKEN, ["USB 1 (Main Stereo) selected. Encoder sets its input level in decibels"])
SPOKEN.clear()
announcer.handle_report(chansel_packet(1), st)
check("the same selection is not repeated", SPOKEN, [])
SPOKEN.clear()
announcer.handle_report(chansel_packet(5), st)          # strip 5 = SMART pads
check("SMART pads names what it HAS, at press time, without a fruitless turn",
      SPOKEN, ["SMART pads selected. Fader level only"])
SPOKEN.clear()

# SAY WHAT THE CHANNEL HAS, NOT WHAT IT LACKS. "Nothing here for the encoder to adjust" is simply
# false of the SMART pads: they can sit on the last fader (index 8, virtual) and be ridden daily.
# The phrase is the same on a physical and a virtual fader ON PURPOSE - a bus has a fader either way.
layout([0, 7, 2, 3, 10, 1, 8, 9, 11])                      # pads on strip 8, the last fader
announcer.handle_report(chansel_packet(8), st)
check("and says the same on a VIRTUAL fader - no device branch to drift",
      SPOKEN, ["SMART pads selected. Fader level only"])
SPOKEN.clear()
announcer.handle_report(chansel_packet(6), st)               # strip 6 = Chat, also virtual
check("a LINE channel on a virtual fader still names its own level, not the ring",
      SPOKEN, ["Chat selected. Encoder sets its input level in decibels"])
SPOKEN.clear()
layout(LAYOUT)                                             # back to this block's layout

announcer.handle_report(chansel_packet(0), st)          # strip 0 = Mic 1
check("a mic strip says gain", SPOKEN, ["Mic 1 selected. Encoder sets its gain"])
SPOKEN.clear()
announcer.handle_report(chansel_packet(-1), st)
check("and deselecting says the encoder is back on levels",
      SPOKEN, ["Virtual faders and monitor. Encoder changes their levels"])

# THE APP'S OWN DOUBLE PRESS (server.action_reset_encoder) says one sentence of its own, so the two
# selection pushes it causes are not spoken over it.
SPOKEN.clear()
announcer.mark_self_write("channel_selected")
announcer.handle_report(chansel_packet(0), st)
announcer.handle_report(chansel_packet(-1), st)
check("our own select and deselect are not spoken", SPOKEN, [])
announcer._self_writes.pop("channel_selected", None)

# A PHANTOM "LEVELS" ANNOUNCEMENT AT STARTUP. The pushed stream reports CHANGES, so with no
# selection known the desk's first push of the resting value (-1, nothing selected) reads as one, and
# "Virtual faders and monitor. Encoder changes their levels" is spoken with nobody touching anything.
# The desk model holds the value from the startup read, so the push replaces itself and is silent.
SPOKEN.clear()
desk(LAYOUT)
select(-1)                                   # what the startup read leaves in the desk model
announcer.handle_report(chansel_packet(-1), fresh_st())
check("a resting push of 'nothing selected' after startup says NOTHING", SPOKEN, [])
SPOKEN.clear()
announcer.handle_report(chansel_packet(0), fresh_st())
check("but a real selection still speaks", SPOKEN, ["Mic 1 selected. Encoder sets its gain"])
SPOKEN.clear()
desk_model._reset()                          # never read: the state that produces the phrase
announcer.handle_report(chansel_packet(-1), fresh_st())
check("a desk never read is what speaks out of nowhere",
      SPOKEN, ["Virtual faders and monitor. Encoder changes their levels"])

# THE SAME SHAPE OF BUG, SECOND PROPERTY: the desk's first push of the RESTING bank must not read
# as a change and speak "Bank 1" at startup.
SPOKEN.clear()
desk(LAYOUT)                                 # the resting bank, 0, from the startup read
announcer.handle_report(bank_packet(0), fresh_st())
check("a resting push of the current bank after startup says NOTHING", SPOKEN, [])
SPOKEN.clear()
announcer.handle_report(bank_packet(1), fresh_st())
check("but a real bank change still speaks", SPOKEN, ["Bank 2"])
SPOKEN.clear()
desk_model._reset()
announcer.handle_report(bank_packet(0), fresh_st())
check("a desk never read is what speaks out of nowhere", SPOKEN, ["Bank 1"])

# THE DESK DOES NOT PUSH ALL OF OUR OWN WRITES BACK, so the send layer hands each one to the desk
# model (`rcp_hid.note_written`). Without that, moving a virtual fader with our slider leaves the
# next encoder press quoting the level from before the move.
desk(LAYOUT)
SPOKEN.clear()
rcp_hid.note_written(rcp_transfer.build_fader_level(addr(rcp_transfer.fader_key(6)), 95))
check("our own fader-level write reaches the ring", round(desk_model.fader_level(6), 2), 0.75)
rcp_hid.note_written(rcp_transfer.build_at(rcp_transfer.OUTPUT, "outputMonLevel", "float", 0.42))
check("and the monitor level too", desk_model.monitor_level(), 0.42)
check("a level we write says NOTHING (the window already announces it)", SPOKEN, [])

# A mute the app makes for a hold on the desk is spoken, because the desk does not report it back.
SPOKEN.clear()
announcer.live_state = fresh_st()
try:
    with patched((announcer, {"mute_tones": False})):
        announcer.announce_mute(0, True)
    check("a hold's mute is spoken", SPOKEN, [f"{announcer.channel_label(0)} muted"])
finally:
    announcer.live_state = None
SPOKEN.clear()
announcer.announce_mute(0, False)
check("with the announcer off, nothing is said", SPOKEN, [])

# A MIC strip: gain in dB.
st = fresh_st()
SPOKEN.clear()
announcer.handle_report(chan_gain_packet(b"inputMicrophoneGain", 0x6E, 66), st)
check("mic gain is spoken", SPOKEN, ["66 d B, Mic 1 gain"])
SPOKEN.clear()
announcer.handle_report(chan_gain_packet(b"inputMicrophoneGain", 0x6E, 66), st)
check("the same value is not repeated", SPOKEN, [])

# A LINE strip: the desk calls it level, not gain.
st = fresh_st()
SPOKEN.clear()
announcer.handle_report(encsig_packet(1), st)
announcer.handle_report(chan_gain_packet(b"inputDigitalGain", 0x75, 6), st)
check("USB 1 input level is spoken", SPOKEN, ["6 d B, USB 1 (Main Stereo) input level"])
check("and it cancels the not-linked warning", st.get("enc_turn_at"), None)
SPOKEN.clear()
announcer.flush_pending(st)
check("so nothing calls it unlinked", SPOKEN, [])

# An unknown selector is not announced as some phantom channel, but still counts as movement.
st = fresh_st()
SPOKEN.clear()
announcer.handle_report(encsig_packet(1), st)
announcer.handle_report(chan_gain_packet(b"inputDigitalGain", 0x99, 3), st)
check("an unknown channel selector says nothing", SPOKEN, [])
check("but is still treated as movement", st.get("enc_turn_at"), None)

print("\nSTILL WORKS: BANK AND ENCODER")
desk(LAYOUT)
said, st = run([bank_packet(2)], cold_cache=False)
check("bank change", said, ["Bank 3"])
SPOKEN.clear()
announcer.handle_report(bank_packet(2), st)
check("same bank not repeated", SPOKEN, [])
said, _ = run([enc_press, enc_release], cold_cache=False)
check("encoder press", said, ["Select"])
# And the invented shape must NOT be accepted, or the bug can come back unnoticed.
fake_press = push([0x0A], "encoderPressed", bytes([0x01, 0x01, 0x01]))   # an int type byte
said, _ = run([fake_press], cold_cache=False)
check("a malformed press value is ignored", said, [])

print("\nOUR OWN LAYOUT WRITES REFRESH THE NAMING LAYOUT")
# THE TRAP: our own writes are not all echoed on the pushed stream, so a layout this app writes has
# to reach the desk model as it is sent, or an encoder PRESS announces the old channel ("Mic 2")
# while a TURN announces the new one ("USB 1"): the turn reads the source code off the cross-point
# selector, the press has to look the layout up.

OLD_LAYOUT = [0, 7, 2, 3, 10, 11, 1, 8, 9, 9]      # before the reset: Mic 2 up on fader 7
NEW_LAYOUT = [0, 1, 2, 3, 10, 11, 7, 8, 9, 9]      # after the reset: USB 1 on fader 7
desk(OLD_LAYOUT)
check("names the OLD channel before the write", announcer._fader_label(6), "Mic 2")
for strip in (1, 6):
    rcp_hid.note_written(rcp_transfer.build_at(rcp_transfer.channel_key(strip),
                                               "channelInputSource", "int", NEW_LAYOUT[strip]))
check("the writes reach the naming layout", channels.last_known_strip_vector(), NEW_LAYOUT)
check("so fader 7 now names USB 1", announcer._fader_label(6), "USB 1 (Main Stereo)")
at(rcp_transfer.fader_key(6), "faderLevel", 64, "int")
check("and an encoder PRESS says so too", announcer._ring_phrase(fresh_st(), 0),
      "USB 1 (Main Stereo) 50 percent")
# MUTE / SOLO and the channel button name the strip through the SAME lookup, so a stale layout
# mis-names all four paths at once.
said, _ = run([state_packet("channelOutputMute", 6, True)], cold_cache=False)
check("mute on fader 7 names USB 1", said, ["USB 1 (Main Stereo) muted"])
said, _ = run([state_packet("channelCueEnable", 1, True)], cold_cache=False)
check("solo on fader 2 names Mic 2 again", said, ["Mic 2 solo on"])
check("and the channel button agrees", announcer._selected_phrase(6),
      "USB 1 (Main Stereo) selected. Encoder sets its input level in decibels")

print("\nMUTING FROM SOFTWARE KEEPS THE DESK MODEL HONEST")
# THE DESK DOES NOT ECHO ALL OF OUR OWN WRITES, so the send layer hands each one to the desk model.
# Left out, the encoder ring would quote a healthy percentage for a channel that is now silent, the
# mirror image of quoting a stale level.


def sent(rec: bytes) -> int:
    """`rcp_transfer.send_record`, as the send layer does it: recorded, then handed on."""
    RECORDS.append(rec)
    rcp_hid.note_written(rec)
    return 1


RECORDS: list = []
with patched((rcp_transfer, {"send_record": sent}), (announcer, {"live_state": None})):
    desk([0, 1, 2, 3, 10, 11, 7, 8, 9, 9])
    RECORDS.clear()
    channels.set_channel_mute("Mic2", True)
    rec = RECORDS[0] if RECORDS else b""
    # The record shape is the module-toggle shape: compact bool on CHANNEL[strip]. Mic 2 is on
    # strip 1 in this layout, so 0x1D on the Pro II.
    check("one record sent", len(RECORDS), 1)
    check_true("addressed as a global property on 0x1C + strip (Mic 2 -> 0x1D)",
               b"\x01\x01\x01\x01\x1d" + b"channelOutputMute\x00\x01\x01\x02" in rec)
    RECORDS.clear()
    channels.set_channel_mute("Mic2", False)
    check_true("unmute is the same record with the OFF byte",
               b"channelOutputMute\x00\x01\x01\x03" in (RECORDS[0] if RECORDS else b""))
    # And it follows the CHANNEL, not a fixed slot: with Mic 2 up on fader 7 (strip 6) the write has
    # to land on 0x22, exactly as a processing write on that channel would.
    layout(LAYOUT)
    RECORDS.clear()
    channels.set_channel_mute("Mic2", True)
    check_true("the write follows the channel to the strip it is on (fader 7 -> 0x22)",
               b"\x01\x01\x01\x01\x22" + b"channelOutputMute\x00" in (RECORDS[0] if RECORDS else b""))

    # The reply reads the mix grid to say what the mute reaches. Every Mic 2 cross-point linked here,
    # so it is silent everywhere; the hand-set case is checked after the cache block.
    MIX_MODES = {o: 0 for o in range(11)}
    MIX_LEVELS = {o: {1: {"level": 0.8, "linked": True}} for o in range(11)}
    with patched((rcp_read, {
            "read_model": lambda *a, **k: b"stub-dump",
            "read_matrix_levels": lambda blob=None: {"ok": True, "modes": MIX_MODES,
                                                     "levels": MIX_LEVELS},
            "read_matrix_state": lambda blob=None: {
                "ok": True, "modes": MIX_MODES,
                "excluded": {o: {1: False} for o in range(11)}}})):

        # Now the desk model, with fader 7 at 90 percent.
        at(rcp_transfer.fader_key(6), "faderLevel", 114, "int")
        st = fresh_st()
        announcer.live_state = st
        SPOKEN.clear()
        res = server.action_set_channel_mute("Mic2", True)
        check_true("the write reports ok", res.get("ok"))
        check_true("and says it silences every mix, not just what you hear",
                   "every mix" in res.get("message", ""))
        check("the announcer does NOT speak our own write (the UI already did)", SPOKEN, [])
        check("but the desk model holds it, keyed by STRIP", desk_model.mutes()[6], True)
        check("so an encoder press quotes the mute, not a stale 90 percent",
              announcer._ring_phrase(st, 0), "Mic 2, muted")
        server.action_set_channel_mute("Mic2", False)
        check("unmuting clears it again", desk_model.mutes()[6], False)
        check("and the ring goes back to the level", announcer._ring_phrase(st, 0), "Mic 2 90 percent")
        announcer.live_state = None
        check_true("a mute with the announcer OFF works the same",
                   server.action_set_channel_mute("Mic2", True).get("ok"))

        # A mute does not reach a cross-point unlinked at a manual level, so the reply must name that
        # output instead of claiming silence everywhere. Monitor (index 4) in Custom, Mic 2 hand-set.
        MIX_MODES[4] = 2
        MIX_LEVELS[4][1] = {"level": 0.5, "linked": False}
        msg = server.action_set_channel_mute("Mic2", True).get("message", "")
        check_true("a hand-set level is named as still hearing the muted channel",
                   "Monitor" in msg and "still hears" in msg and "every mix" not in msg)
        MIX_LEVELS[4][1] = {"level": 0.0, "linked": False}
        check_true("but a hand-set level of zero is silent anyway",
                   "every mix" in server.action_set_channel_mute("Mic2", True).get("message", ""))
        rcp_read.read_matrix_levels = lambda blob=None: {"ok": False}
        check_true("an unreadable grid says so rather than claiming either way",
                   "Could not check" in server.action_set_channel_mute("Mic2", True).get("message", ""))

    # ---- ECHO SUPPRESSION: our own writes are not read back at us ----
    # The checks above only prove the WRITE itself is silent. The desk then PUSHES the change back,
    # and that echo lands on top of the screen reader reading the UI's own live region, so every
    # change is announced twice: an unmarked level write comes back as "90 percent, USB 2". These
    # pin the behaviour, including that it is SUPPRESSION and not deafness.
    print("\n-- echo suppression (our own writes are not spoken back)")
    desk(LAYOUT)
    st = fresh_st()
    announcer.live_state = st
    # Earlier sections publish their own levels, and the whole suite runs well inside the 1.5 s
    # window, so those marks are still live here and would suppress a channel this section wants to
    # hear. Start from a clean slate rather than from whatever ran before.
    announcer._self_writes.clear()

    # A LEVEL. Marked by fader INDEX, echoed by SOURCE CODE, so this exercises the translation:
    # fader index 8 carries source code 9 (USB 2).
    announcer.mark_self_level("fader", 8)
    SPOKEN.clear()
    announcer.handle_report(xpoint_packet(9, 0, 0.60), st)
    check("a level set from the app is not spoken back at us", SPOKEN, [])
    st["levels_at"] = time.monotonic() - (announcer._SETTLE_S + 0.01)
    announcer.flush_pending(st)
    check("and the settle flush does not say it a moment later either", SPOKEN, [])

    SPOKEN.clear()
    announcer.handle_report(xpoint_packet(1, 0, 0.45), st)
    check("a DIFFERENT channel moving on the desk still speaks at once",
          SPOKEN, ["45 percent, Mic 2"])

    announcer._self_writes[("fader", 9)] = time.monotonic() - (announcer._SELF_ECHO_S + 0.1)
    st["said_at"][("fader", 9)] = time.monotonic() - (announcer._LEVEL_GAP_S + 0.01)
    SPOKEN.clear()
    announcer.handle_report(xpoint_packet(9, 0, 0.70), st)
    check("and a hand on that same fader is audible again once the window passes",
          SPOKEN, ["70 percent, USB 2 (Secondary)"])

    # A MUTE, keyed by strip index (Mic 2 sits on fader 7 = strip 6 in this layout).
    announcer.mark_self_mute(6)
    SPOKEN.clear()
    announcer.handle_report(state_packet("channelOutputMute", 6, True), st)
    check("the echo of a mute we set is silent too", SPOKEN, [])
    check("but the desk model follows it, so the ring quotes the truth", desk_model.mutes()[6], True)
    announcer._self_writes[("mute", 6)] = time.monotonic() - (announcer._SELF_ECHO_S + 0.1)
    announcer.handle_report(state_packet("channelOutputMute", 6, False), st)
    check("a mute pressed ON THE DESK still speaks", SPOKEN, ["Mic 2 unmuted"])

    # A GAIN, keyed by the label the push side builds (Mic 1's channel-address selector is 0x6E).
    announcer.mark_self_gain(1, True)
    SPOKEN.clear()
    announcer.handle_report(chan_gain_packet(b"inputMicrophoneGain", 0x6E, 44), st)
    check("a gain set from the app is not spoken back", SPOKEN, [])
    announcer._self_writes[("gain", "Mic 1 gain")] = time.monotonic() - (announcer._SELF_ECHO_S + 0.1)
    announcer.handle_report(chan_gain_packet(b"inputMicrophoneGain", 0x6E, 45), st)
    check("but the encoder moving that gain does", SPOKEN, ["45 d B, Mic 1 gain"])
    announcer.live_state = None
    announcer._self_writes.clear()

    # ---- the announcer's VOICE and OUTPUT DEVICE ----------------
    # No sound is made here and no setting is left changed: SAPI's two lists are stubbed, the
    # settings writer is stubbed, and the originals go back at the end. What is under test is the
    # matching (exact wins, substring accepted, nonsense refused), that a change moves the speech
    # thread's configuration generation, and that the confirmation is spoken so the new
    # voice and output are heard rather than trusted.
    print("\n-- announcer voice and output device")
    WROTE: dict = {}
    # The voice, output and configuration generation are patched with their own values, so whatever the
    # checks below change is put back.
    with patched((speech, {
            "sapi_lists": lambda refresh=False: {
                "voices": ["Microsoft Hazel Desktop - English (Great Britain)",
                           "Microsoft Zira Desktop - English (United States)"],
                "outputs": ["Speakers (RODECaster Pro II Main Stereo)",
                            "Speakers (Realtek(R) Audio)"]},
            "chosen_voice": speech.chosen_voice,
            "chosen_output": speech.chosen_output,
            "config_gen": speech.config_gen}),
            (settings_file, {"update": lambda **kw: WROTE.update(kw)})):
        SPOKEN.clear()
        gen0 = speech.config_gen
        res = speech.action_set_announce_voice(
            "Microsoft Zira Desktop - English (United States)")
        check_true("an exact voice name is accepted", res.get("ok"))
        check("and it is what the speech thread will take up", speech.chosen_voice,
              "Microsoft Zira Desktop - English (United States)")
        check_true("the change is handed to the speech thread", speech.config_gen > gen0)
        check("it is remembered between sessions", WROTE.get("announcer_voice"),
              "Microsoft Zira Desktop - English (United States)")
        check_true("and a sample is SPOKEN, so the new voice is heard not assumed",
                   len(SPOKEN) == 1 and "Zira" in SPOKEN[0])

        res = speech.action_set_announce_voice("hazel")
        check_true("a partial name still works (a script, or an older saved setting)", res.get("ok"))
        check("resolved to the full name SAPI reports", speech.chosen_voice,
              "Microsoft Hazel Desktop - English (Great Britain)")

        SPOKEN.clear()
        gen1 = speech.config_gen
        res = speech.action_set_announce_voice("Microsoft Aria (Natural)")
        check_true("a voice this computer does not have is REFUSED", not res.get("ok"))
        check("the setting is left alone", speech.chosen_voice,
              "Microsoft Hazel Desktop - English (Great Britain)")
        check("a refused change is not handed on", speech.config_gen, gen1)
        check("and nothing is spoken", SPOKEN, [])

        res = speech.action_set_announce_voice("")
        check_true("empty means the Windows default, which is a real choice", res.get("ok"))
        check("so the default voice is used", speech.chosen_voice, "")

        SPOKEN.clear()
        res = speech.action_set_announce_output("Realtek")
        check_true("an output can be picked by part of its name", res.get("ok"))
        check("resolved to the full device name", speech.chosen_output,
              "Speakers (Realtek(R) Audio)")
        check("remembered too", WROTE.get("announcer_output"), "Speakers (Realtek(R) Audio)")
        check_true("with the confirmation spoken THROUGH it, which is the real test",
                   len(SPOKEN) == 1 and "Realtek" in SPOKEN[0])
        check_true("an output this computer does not have is refused",
                   not speech.action_set_announce_output("Focusrite").get("ok"))
        check("and that setting is left alone too", speech.chosen_output,
              "Speakers (Realtek(R) Audio)")
        # A name with a regex/wildcard character in it must be matched literally: the real device
        # here is "Speakers (Realtek(R) Audio)", and PowerShell's -like would read [ ] as wildcards.
        check_true("a name with brackets in it matches literally",
                   speech.action_set_announce_output("Realtek(R)").get("ok"))

print("\nONE FORMATTER, TWO WORD ORDERS, AND THEY MUST STAY OPPOSITE")
# Both announcements come through announcer._level_words. The orders are opposite ON PURPOSE and drifting
# to a single order would break one of them, so assert both from the shared function itself.
check("a level that MOVED leads with the number, because the name gets cut off mid-turn",
      announcer._level_words("Mic 1", 0.30, name_first=False), "30 percent, Mic 1")
check("and drops the desk during a run",
      announcer._level_words("Mic 1", 0.30, name_first=False, say_percent=False), "30, Mic 1")
check("a SELECTION leads with the name, because that is the question the press asks",
      announcer._level_words("Chat", 0.97, name_first=True), "Chat 97 percent")
check("percent is clamped and rounded once, in one place",
      [announcer._level_words("X", v, name_first=False) for v in (-0.5, 0.006, 1.4)],
      ["0 percent, X", "1 percent, X", "100 percent, X"])

print("\nTHE TWO INDEX SPACES ARE REAL: A SOURCE ON NO FADER STILL GETS ANNOUNCED")
# This is why speech is keyed by SOURCE CODE and the encoder ring by FADER INDEX, and why they
# cannot be collapsed into one. The Pro II always has one source off-fader (10 assignable, 9
# faders), and its level still moves and still deserves its name. Collapsing to fader indices
# would silence it, which is exactly the regression this guards.
# SMART pads (source 11) left off the layout. Not CallMe 1: its cross-point selector is
# 0x4C + 13*16, which is past a single byte, so it cannot even be expressed as a one-byte push.
desk([0, 1, 2, 3, 10, 7, 8, 9, 16])                          # source 11 is on no fader
said, st = run([xpoint_packet(11, 0, 0.42)], cold_cache=False)
check("an OFF-FADER source is still named and spoken", said, ["42 percent, SMART pads"])
said, _ = run([xpoint_packet(7, 0, 0.42)], cold_cache=False)
check("a source that IS on a fader still works normally", said, ["42 percent, USB 1 (Main Stereo)"])

print("\nA STEREO-LINKED PAIR IS NAMED, NOT SPOKEN AS A BARE CODE")
# A linked pair is its OWN source code (4, 5, 6) and can sit in the fader vector, but it is
# deliberately NOT assignable - so an announcer lookup through the assignable-only map is one link
# away from reading a bare number aloud ("0 percent, Source 4" for a linked Mic 1 + 2). Names come
# from FaderSpec.all_labels, and there is no `code_label` helper to reach for the narrow map again.
check("a pair code has a spoken name at all",
      [announcer._source_label(c) for c in (4, 5, 6)],
      ["Mic 1 + 2", "Mic 2 + 3", "Mic 3 + 4"])
check("and no assignable source lost its name",
      [announcer._source_label(c) for c in (0, 7, 11)],
      ["Mic 1", "USB 1 (Main Stereo)", "SMART pads"])
# The whole way through: a cross-point push for a linked pair, spoken.
desk([4, -1, 2, 3, 10, 7, 8, 9, 11])
said, _ = run([xpoint_packet(4, 0, 0.0)], cold_cache=False)
check("a level push for a linked pair says the pair, not 'Source 4'", said,
      ["0 percent, Mic 1 + 2"])
check("and the strip it sits on is named the same way", announcer.channel_label(0), "Mic 1 + 2")
check("the vacated fader beside it is not claimed by anything",
      announcer.channel_label(1), "Channel 2")

print("\nA CROSS-POINT SELECTOR PAST 255 IS STILL DECODED")
# The cross-point is MIX[13*source + output], whose path leaves byte range at source 14, so
# Virtual A and Virtual B are addressed with two bytes. Their low bytes alone are 2 and 15, both
# below the MIX block, so reading one byte discards the push and the fader moves in silence -
# no level, no name. Only codes 14 and 15 reach that far, and only in Expanded.
desk([0, 12, 13, 14, 15, 11, 7, 8, 9])
at(rcp_transfer.OUTPUT, "outputMultiMode", 5, "int")         # post-fader, Expanded
check("a one-byte selector is unaffected",
      run([xpoint_packet(12, 0, 0.25)], cold_cache=False)[0], ["25 percent, Game"])
check("a two-byte selector is spoken, not dropped",
      run([xpoint_packet(14, 0, 0.5)], cold_cache=False)[0], ["50 percent, Virtual A"])
check("and so is the one past it",
      run([xpoint_packet(15, 0, 1.0)], cold_cache=False)[0], ["100 percent, Virtual B"])

# --- THE EXPANDED FLAG MUST BE SEEDED AT STARTUP, not left to whichever page is opened first ------
# `usb_expanded()` answers False while the desk model has never seen the mode. That is not a
# harmless default: unknown, the fader dropdown omits Game, Music, A and B and the write gate refuses
# them, so on a desk that IS in Expanded the four cannot be assigned at all. The startup seed reads
# the desk, which the desk model adopts, whether or not announcements are on.
print()
print("The Expanded flag is seeded at startup")
seed_reads = []


def _seed_read(**_k):
    seed_reads.append(1)
    at(rcp_transfer.OUTPUT, "outputMultiMode", 5, "int")       # what adopting the read leaves
    return b""


desk_model._reset()
check_true("unknown is distinguishable from False", not channels.usb_expanded_known())
check("and an unknown mode hides Expanded's four from the fader write gate",
      sorted(set(devices.PRO2.fader.code_set_for(channels.usb_expanded())) & {12, 13, 14, 15}), [])
desk_model.dump_finished(desk_model.dump_starting(),
                         layout_fixture.skeleton(layout_fixture.PRO2_177))
desk_model._reset()
with patched((server.rcp_read, {"read_model_stable": _seed_read})):
    server._seed_usb_expanded()
    check_true("the startup seed reads it", channels.usb_expanded_known())
    check("and the desk's real mode is what the app then holds", channels.usb_expanded(), True)
    check("so the four are assignable again",
          sorted(set(devices.PRO2.fader.code_set_for(channels.usb_expanded())) & {12, 13, 14, 15}),
          [12, 13, 14, 15])
    # Idempotent: a second call must not pay for another dump.
    seed_reads.clear()
    server._seed_usb_expanded()
check("a second seed reads nothing, because the mode is already known", len(seed_reads), 0)


print()
sys.exit(_testkit.finish())
