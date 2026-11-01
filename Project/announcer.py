"""The control announcer: speaks the desk's own button presses aloud (an accessibility aid).

A background thread listens to report-04 HID events and speaks bank changes / mute / solo /
encoder presses via SAPI (say.ps1), so a screen-reader user knows what a physical press did
without seeing the touchscreen.

DEVICE-AGNOSTIC: the decoder keys off the report-04 PROPERTY NAMES
(selectedBank / cmutePressed / csoloPressed / encoderPressed), which are protocol-level and shared
across both desks, NOT a Pro II physical control-map or channel-index table. It speaks generic
"Bank N" / "Mute" / "Solo" / "Select" (no per-channel index), so the Duo's fewer faders/banks need
no special-casing. It is also strictly READ-ONLY (one read handle, no writes), so even if a name
ever differs on a future desk the worst case is a missed announcement, never a mis-write.

HID SHARING: the server uses the same control interface for pad/mixer ops. The announcer keeps
ONE read-only handle open for its whole lifetime and reads continuously, WITHOUT taking the device lock
(`hooks.lock`).
This is safe because the Windows HID stack delivers every input report (the report-04 control
events) to each open handle's own queue, so the listener never misses a press and never disturbs
the writes, which open their own handle under that lock (measured: a command still ACKs while a
second handle is open).
DO NOT open and close the handle around each poll to avoid two concurrent handles: closing
FLUSHES the input queue, so every press landing in the gap is dropped and bank/mute/solo
announcements go intermittent. A persistent handle has no deaf windows and no open/close churn.

THE HANDLE IS REOPENED ON A FRESHLY RESOLVED INTERFACE, never on a remembered one. The desk
re-enumerates on `outputMultiMode`, `engineMode` and `systemMainMode`, all of which the app itself
writes, and comes back under a different USB product id and path.
"""
from __future__ import annotations

import threading
import time

import channels
import devices
import diagnostics
import rcp_hid
import rcp_read
import rcp_transfer
import recorder
import settings_file
import speech
import tones


class Hooks:
    """What the announcer needs from the app around it. The server installs its own at import; these
    defaults do nothing, so the decoder also runs alone in the offline tests and DevTools."""

    lock = threading.Lock()   # the server's device lock, taken for the one seeding dump

    def note_bank(self, bank: int) -> None:
        """The desk's selected pad bank changed."""

    def note_pad_progress(self, position: int, progress: float) -> None:
        """A pad's playback position moved."""

    def note_pad_active(self, position: int, on: bool) -> None:
        """A pad was switched on or off."""

    def mixer_positions(self) -> frozenset:
        """The pad positions that hold mixer pads."""
        return frozenset()

    def reconnected(self, was_usb, now_usb) -> None:
        """The desk came back after a real gap, already re-seeded; the USB modes are before and after."""


hooks = Hooks()

_thread = None
# Each loop is started with the next generation and leaves as soon as it is no longer the newest.
# Off and on within one read or one reconnect sleep would otherwise leave the first loop running
# beside the second: two handles, and every report and reconnect handled twice.
_loop_gen = 0
_start_lock = threading.Lock()   # the check-and-start below runs on request threads

# Reconnect pacing. Quick at first, so a momentarily busy handle costs nothing audible and a
# re-enumeration (about 2.5 s) is picked up as soon as the desk is back; slow after that, because
# every attempt re-enumerates the USB HID tree and a desk off the bus may never return without a
# replug.
_RETRY_S = 0.3
_RETRY_SLOW_S = 2.0
_RETRY_FAST_TRIES = 15
# A reconnect is only worth speaking about when the gap was long enough to have swallowed a press.
# Coming straight back on the next pass is not news.
_RECONNECT_SPEAK_AFTER_S = 1.0


# MUTE / SOLO STATE.
# Pressing mute or solo pushes, unasked and about 70 ms after the button edge, the STATE property
# `channelOutputMute` / `channelCueEnable`, addressed at SEL_CHANNEL + strip index. So the
# announcer can say WHICH channel and WHETHER it is now muted, instead of the bare word "Mute" - and
# it catches a mute from any source (button, touchscreen, MIDI), not just a press edge. Still
# strictly read-only. The press edge (`mutePressed` / `soloPressed`) is addressed at the physical
# BUTTON, which names no channel, so it is only the fallback when no state follows.
_STATE_PROPS = {"channelOutputMute": "mute", "channelCueEnable": "solo"}
# HOW LONG A MIXER PAD OWNS THE MUTE AND CUE LINES. One press of a Back Channel pad moves four of
# these at once (both members' mute and cue), and each edge of a momentary pad brings its own burst,
# so speaking them turns a two-second talkback push into eight announcements describing a state the
# operator just asked for. A DEADLINE, not a switch: every mixer `padActive` pushes it further out,
# so repeated presses cannot leave it stuck on, and a missed window costs only today's behaviour.
# Generous against the burst (the pushes arrive together, within tens of ms) and short enough that a
# real mute a moment later still speaks.
_MIXER_QUIET_S = 0.75
_mixer_quiet_until = 0.0
# PRESS AND HOLD ON THE ENCODER MUTES THE SELECTED CHANNEL. Measured: a short press runs
# 475 ms down-to-up and mutes nothing; a hold pushes `channelOutputMute = True` 765 ms after the
# press edge, WHILE STILL HELD, and mutes the channel selected BEFORE the press. So the desk does
# not advance its encoder ring on a hold. RODE's user guide documents press-and-hold only on the REC
# button, so it is not there either.
#
# Two consequences, both of which this handling exists for:
#  1. ANNOUNCING THE NEXT RING POSITION ON THE PRESS EDGE IS WRONG ON A HOLD: you hear the next
#     channel named and then the previous one announced as muted, and our ring position silently
#     desyncs from the desk's, so every later prediction is one out until a real movement re-seats
#     it. So we wait for the RELEASE edge, which the desk does push, and only advance on a short
#     press.
#  2. What tells the two apart is the mute push itself, not a stopwatch: any `channelOutputMute`
#     arriving between our press and release means the desk took the gesture. The duration below is
#     only a backstop for a desk that pushes no state (the Duo is unobserved here), set well clear of
#     both measurements.
_ENC_HOLD_S = 0.9
# How long to wait for the STATE push after a press edge before falling back to the generic word.
# The measured gap is about 70 ms; this is deliberately generous. The fallback keeps the
# announcer device-agnostic: a desk that pushes no state still says "Mute" / "Solo".
_PENDING_S = 0.5


# SPOKEN POSITIONS. The desk pushes a moving fader's position as
# `mixLevelWithAnchor` on every one of that source's 13 cross-points, 8-10 times a second, and the
# monitor encoder as `outputMonLevel`. Speaking each push would be a torrent, so we hold the latest
# value and speak ONE phrase once the movement stops (_SETTLE_S of quiet). Separately switchable
# from the button announcements: some sessions want mute/solo spoken but not a running commentary on
# every fader.
# Persisted, like the volume and the on/off: a bare default means turning levels OFF comes silently
# back ON at the next launch.
levels_on = bool(settings_file.load().get("announcer_levels", True))
# MUTE AS A TONE INSTEAD OF A SENTENCE. Off by default: speech is what everyone gets until they
# choose otherwise, and the tones only make sense to someone who has been told what they mean.
mute_tones = bool(settings_file.load().get("announcer_mute_tones", False))
# SPEAK THE DESK'S STATE WHEN THE APP OPENS. It is an ANNOUNCEMENT, not a page, so it is a setting
# of its own rather than a side effect of which page the app opens on. Default ON: knowing the
# desk's state without looking is the point of the app, and on a normal desk it is one sentence.
startup_summary = bool(settings_file.load().get("announcer_startup_summary", True))


def _load_remember() -> bool:
    """Whether announcements start the way they were left. OFF BY DEFAULT, so the app opens talking:
    a start-up in silence is indistinguishable from the app or the desk being broken. With no saved
    answer, a saved `announcer` of False counts as ticked: a settings file that predates this option
    holds the tray's own on/off, and somebody who switched announcements off there keeps them off."""
    s = settings_file.load()
    if "announcer_remember" in s:
        return bool(s["announcer_remember"])
    return s.get("announcer") is False


remember = _load_remember()


def starts_on() -> bool:
    """Whether the announcer comes up on at start-up: always, unless it is remembered as off."""
    return not (remember and settings_file.load().get("announcer") is False)
_SETTLE_S = 0.12       # a safety net: guarantees the closing value is spoken
_LEVEL_GAP_S = 0.10    # minimum spacing between spoken level updates while moving
_RUN_S = 1.5           # within this of the last update on the same target, say just the number
# WHAT THE ENCODER PRESS SELECTS, tracked by us because the desk never says.
#
# We DO see every press, so since the press CYCLES through a fixed order we keep our own position in
# that order and say what has just been selected BEFORE it is turned - which matters, because the
# first click already moves the value by 1/127.
#
# On a Pro II, pressing walks fader 7 -> fader 8 -> fader 9 -> monitor level -> round again, i.e.
# exactly the things with no physical fader. Selectors: faderLevel 0x04 + fader index (7 -> 0x0a,
# 8 -> 0x0b, 9 -> 0x0c), outputMonLevel 0x0d.
#
# We never TRUST the prediction blindly: the first movement reports the real target, which re-seats
# our position, so a wrong guess self-corrects and costs one spoken phrase.
#
# DEVICE-AWARE: the ring is the faders with NO physical pot, then the monitor,
# so it is derived from devices.FaderSpec rather than hardcoded. Pro II (physical 6, count 9) yields
# exactly the observed ring - faders 7/8/9 then monitor - byte for byte. The Duo (physical 4, count 9)
# yields faders 5-9 then monitor, which is the same RULE but an UNOBSERVED order on that desk; a wrong
# guess costs one spoken phrase because the first real movement re-seats us (see _ring_seat).
_ENCODER_RING_PRO2 = (("fader", 6), ("fader", 7), ("fader", 8), ("monitor", 0))


def _encoder_ring() -> tuple:
    """The encoder's press-cycle order for the CONNECTED desk: virtual faders, then monitor level."""
    try:
        spec = devices.get_active_device().fader
        ring = tuple(("fader", i) for i in range(spec.physical, spec.count)) + (("monitor", 0),)
        return ring if len(ring) > 1 else _ENCODER_RING_PRO2
    except Exception:
        return _ENCODER_RING_PRO2


_FADER_LEVEL_MAX = 127.0     # faderMax; faderLevel/127 IS the cross-point level, checked exactly
# The headphone pots. Each POT node under PHYSICALINTERFACE[0] publishes its OWN potMin and potMax
# (0 and 127 on a Pro II, read off the desk), so this constant is the desk's figure rather than an
# assumed one - check those two properties before trusting it on a new device.
# Four pots, one per headphone output.
_POT_LEVEL_MAX = 127.0
_POT_COUNT = 4
# TURNING WITH NOTHING BOUND. The encoder only adjusts a level while the desk has one bound to it,
# and pressing the BANK buttons moves the screen to the pads page, which unbinds it.
# In that state the desk still reports the rotation as `encoderSignal` but nothing moves,
# so silence is technically correct and infuriating in practice. Say so instead: if a turn is followed
# by no level change, announce that it is not linked. `encoderColour` corroborates it - it read 7 while
# the encoder was driving a fader and -1 once it went inert - so we use that when the desk offers it.
_ENC_DEAD_S = 0.45     # a turn with no level change within this was a turn against nothing
_ENC_DEAD_GAP_S = 4.0  # and do not repeat that message more often than this
# A pushed cross-point selector is CROSS_POINT_BASE + 13*source + output (rcp_transfer), and the
# matrix row IS the source code, so one selector gives the source directly.
_XPOINT_BASE = rcp_transfer.CROSS_POINT_BASE
_XPOINT_STRIDE = rcp_transfer.CROSS_POINT_SOURCE_STRIDE


def _source_label(source_code: int) -> str:
    """A speakable name for a matrix SOURCE code, e.g. 'Mic 1'. Never touches the device.

    `labels_for`, NOT `code_label`: a fader can HOLD a stereo-linked pair (codes 4, 5, 6) even
    though a pair is never something you assign, and code_label covers only the assignable set, so
    a linked Mic 1 + 2 would be announced as "Source 4"."""
    try:
        lbl = devices.get_active_device().fader.labels_for(channels.usb_expanded()).get(source_code)
        if lbl:
            return lbl
        for s in devices.get_active_device().matrix.sources_for(True):
            if s.get("index") == source_code:
                return s.get("label") or f"source {source_code}"
    except Exception:
        pass
    return f"Source {source_code}"


def channel_label(strip_idx: int) -> str:
    """A speakable name for a strip index, e.g. 'Mic 2', from the known fader layout.

    Never touches the device: the announcer thread holds no HID lock, so a model dump from here
    would fight the writes. Falls back to the LAST KNOWN layout when the TTL'd cache has expired:
    nothing refreshes that cache while the operator is only pressing buttons. The layout is seeded
    once when the announcer starts and kept current from the stream (channels.note_strip_source).
    'Channel N' when no layout has been seen or it does not name that slot."""
    try:
        vec = channels.cached_strip_vector() or channels.last_known_strip_vector()
        if vec and 0 <= strip_idx < len(vec):
            # labels_for, so a linked pair is named rather than spoken as a bare code, and the
            # Expanded four are named rather than announced as "Source 12".
            label = devices.get_active_device().fader.labels_for(
                channels.usb_expanded()).get(vec[strip_idx])
            if label:
                return label
    except Exception:
        pass
    return f"Channel {strip_idx + 1}"


def handle_report(b: bytes, st: dict) -> None:
    """Decode one report-04 frame and speak it, updating the announcer state dict `st`
    ({"bank", "pending", "at"}). Only a PUSH is acted on: a slice of a model dump decodes as None
    and is ignored, so a page reading the desk never sounds like the desk changing."""
    global _mixer_quiet_until
    rec = rcp_read.parse_push(b)
    if rec is None:
        return
    path, name, val, kind = rec
    # Typed views of the value, so a branch that wants an int never acts on a string or a bool.
    ival = val if kind == "int" else None
    bval = val if kind == "bool" else None
    fval = val if kind == "float" else None
    # The strip a one-level push at SEL_CHANNEL + strip is addressed at, else -1.
    strip = path[0] - rcp_transfer.SEL_CHANNEL if len(path) == 1 else -1
    if not 0 <= strip < 16:
        strip = -1

    if name == "selectedBank":
        if ival is None:
            return
        # Keep the shortcut layer's copy current whether or not this is a change: the bank decides
        # which pad a number key fires, so a stale one plays a stranger's sound.
        hooks.note_bank(ival)
        if ival != st["bank"]:
            st["bank"] = ival
            speech.say(f"Bank {ival + 1}")
        return
    # PAD PLAYBACK, TRACKED AND NEVER SPOKEN. A playing pad pushes `padProgress` about twenty times a
    # second, so this is the one branch where saying anything at all would be wrong: the "what is
    # playing" shortcut reads the state out on demand instead, rather than talking over the pad the
    # whole time it plays. Both are addressed at the pad's STREAM POSITION, the last path element.
    if name == "padProgress":
        if fval is not None and path:
            hooks.note_pad_progress(path[-1], fval)
        return
    if name == "padActive":
        if bval is not None and path:
            hooks.note_pad_active(path[-1], bval)
            # BOTH EDGES. A momentary pad restores every channel it moved when it is released, and
            # that burst says no more than the one on the way in.
            if path[-1] in hooks.mixer_positions():
                _mixer_quiet_until = time.monotonic() + _MIXER_QUIET_S
        return
    if name == "mixLevelWithAnchor":
        # A fader is moving. One phrase per gesture, not per push: hold the latest value and let
        # flush_pending speak it once the pushes stop. A cross-point selector is
        # CROSS_POINT_BASE + 13*source + output, which passes 255 at source 14, so Virtual A and
        # Virtual B are addressed with a two-byte index: use the whole path element, never a byte.
        if not levels_on or kind != "str" or "|" not in val or len(path) != 1:
            return
        if path[0] < _XPOINT_BASE:
            return
        try:
            lvl = float(val.split("|", 1)[0])
        except ValueError:
            return
        _note_level(st, "fader", (path[0] - _XPOINT_BASE) // _XPOINT_STRIDE, lvl)
        return
    if name == "encoderSignal":
        # Raw rotation, a COMPOSITE whose first member is the signed step (+1 clockwise, -1 anti).
        # We do not announce the turn itself - the level change that follows says everything - but
        # if NOTHING follows, flush_pending speaks up (see _ENC_DEAD_S). EXCEPT at the ends
        # of the range: turning down at 0, or up at maximum, legitimately changes nothing.
        if levels_on:
            delta = val[0][0] if kind == "pair" and val and val[0][1] == "int" else 0
            st["enc_turn_at"] = None if _at_limit(st, delta) else time.monotonic()
        return
    if name == "systemChannelSelected":
        # -1 = nothing selected, and the encoder is back on the levels ring. Anything else is a STRIP
        # INDEX: that channel is selected and the encoder now moves its gain. The channel buttons
        # emit no press edge of their own - this state change IS the event.
        if ival is None or ival == st.get("chan_sel"):
            return
        st["chan_sel"] = ival
        speech.say("Levels. Encoder sets fader and monitor levels" if ival < 0
                 else _selected_phrase(ival))
        return
    if name in ("inputMicrophoneGain", "inputDigitalGain"):
        # The gain the encoder moves while a channel is selected. BOTH kinds matter: a MIC strip
        # moves inputMicrophoneGain, a line strip (USB 1, Chat, USB 2, Bluetooth) moves
        # inputDigitalGain. Miss the second and a working turn on USB 1 announces "not linked" while
        # the system audio audibly changes. Pushed at the channel's INPUTSOURCE node, whose index is
        # two bytes (0x26E + source_code).
        if not levels_on or ival is None:
            return
        st["enc_turn_at"] = None              # the turn DID do something: it moved a gain
        who = _gain_label(path, name == "inputMicrophoneGain")
        if who and st.get("gain_said") != (who, ival):
            # Record it either way, so a suppressed echo is not spoken by a later duplicate push.
            st["gain_said"] = (who, ival)
            if not is_self_echo(("gain", who)):
                # Number first, as for any level that moved (see _level_words).
                speech.say(f"{ival} d B, {who}")
        return
    if name == "encoderColour":
        # -1 = the encoder is bound to nothing; anything else is the colour of what it drives.
        if ival is not None:
            st["enc_bound"] = (ival >= 0)
        return
    if name == "faderLevel":
        # The VIRTUAL fader level, 0-127. This is the encoder's own report of
        # what it just moved, and its address names the FADER directly
        # (PHYSICALINTERFACE.FADER[base + index]), so it is the cleanest signal for re-seating the
        # ring - the cross-point pushes that follow say the same thing by source.
        idx = (path[1] - devices.FADER_NODE_BASE) if len(path) == 2 and path[0] == 0 else -1
        if ival is not None and 0 <= idx < 16:
            _ring_seat(st, "fader", idx, max(0.0, min(1.0, ival / _FADER_LEVEL_MAX)))
        return
    if name == "outputMonLevel":
        if levels_on and fval is not None:
            _ring_seat(st, "monitor", 0, fval)
            _note_level(st, "monitor", 0, fval)
        return
    # THE HEADPHONE KNOBS. They are physical pots with NO TACTILE INDICATOR, so there is no way to
    # know where one is set by touch, which is exactly the case software has to answer. The desk
    # pushes `potLevel` per step as the knob turns: measured 199 pushes in 12 seconds, one per
    # increment, with the pot identified by the path. They go through the ordinary level machinery,
    # so a sweep settles into ONE closing phrase rather than a torrent, exactly as a fader does. NOT
    # seated on the encoder ring: the ring cycles what the ENCODER selects, and a headphone knob is
    # not on it.
    if name == "potLevel":
        idx = path[1] if len(path) == 2 and path[0] == 0 else -1
        if levels_on and ival is not None and 0 <= idx < _POT_COUNT:
            _note_level(st, "headphone", idx, max(0.0, min(1.0, ival / _POT_LEVEL_MAX)))
        return
    if name == "channelInputSource":
        # A fader source changed. Keep the naming layout current and drop the TTL'd cache, so a
        # change made on the desk itself is picked up at once instead of after up to 15 s. We do NOT
        # speak it: our own fader writes push this too, and the UI already reports those.
        if ival is not None and strip >= 0:
            channels.note_strip_source(strip, ival)
        return
    if name in _STATE_PROPS:
        if bval is None or strip < 0:
            return
        on, what = bval, _STATE_PROPS[name]
        st["pending"] = None            # the state supersedes the press edge
        who = channel_label(strip)
        if what == "mute":
            # Track it: the encoder ring quotes this instead of a level (a muted channel's level is
            # not the useful fact), and a mute landing mid-press is what identifies the gesture as
            # the desk's press-and-hold rather than a selection change.
            st.setdefault("muted", {})[strip] = on
        # Our own write, echoed back: the UI announced it already (see mark_self_write). After
        # the cache, so the ring still quotes the truth, and before the press-and-hold inference,
        # because a software mute is not a gesture on the desk. Solo is marked too
        # (publish_solo), though this desk pushes nothing back for a software solo, so that
        # branch is insurance rather than an observed fix.
        if is_self_echo((what, strip)):
            return
        # A mixer pad is mid-action, so this mute or cue is the pad doing its job. After the cache
        # above, which must stay true whether or not anything is said.
        if time.monotonic() < _mixer_quiet_until:
            return
        if what == "mute":
            if st.get("enc_down_at"):
                st["enc_hold"] = True
            # A TONE IF THEY HAVE ASKED FOR ONE, and the sentence if the tone cannot be played. Mute
            # is the announcement that arrives too late as words: you press it mid-sentence, and by
            # the time "Mic 2 muted" has been said the moment has gone. The cue does not name the
            # channel, which is the trade and is why this is an option rather than the default.
            if not (mute_tones and speech.tone("muted" if on else "unmuted")):
                speech.say(f"{who} {'muted' if on else 'unmuted'}")
        else:
            speech.say(f"{who} solo {'on' if on else 'off'}")
        return
    # Press EDGES. Do not speak yet: hold the generic word for _PENDING_S in case the state
    # property follows (it names the channel, which is strictly better).
    if name == "mutePressed" and bval is True:
        st["pending"], st["at"] = "Mute", time.monotonic()
        return
    if name == "soloPressed" and bval is True:
        st["pending"], st["at"] = "Solo", time.monotonic()
        return
    if name == "encoderPressed" and bval is True:
        # Say NOTHING yet. A short press advances the ring; a hold mutes instead and leaves the
        # selection alone (see _ENC_HOLD_S), and at the press edge we cannot yet tell which this
        # is. The cost is announcing on release, about 400 ms later on a measured press, which is
        # far cheaper than naming the wrong channel and desyncing the ring.
        st["enc_at"], st["enc_named"] = time.monotonic(), False
        st["enc_down_at"], st["enc_hold"] = time.monotonic(), False
        return
    if name == "encoderPressed" and bval is False:
        down_at = st.get("enc_down_at")
        held = bool(st.get("enc_hold")) or (
            down_at is not None and (time.monotonic() - down_at) >= _ENC_HOLD_S)
        st["enc_down_at"], st["enc_hold"] = None, False
        if held:
            return          # the desk muted; that announcement already named the channel
        # A CHANNEL BUTTON HOLDS THE ENCODER, AND AN ENCODER PRESS DOES NOT TAKE IT BACK. Measured
        # on the desk: with `systemChannelSelected` at 0, two encoder presses leave it at 0, and only
        # the channel button pressed again returns it to -1. Advancing the ring here announces USB 1
        # and then Chat while the desk has never left Mic 1 - and worse than the wrong words, it
        # leaves `enc_pos` pointing at a ring entry the encoder is not on, which is the same value
        # `_at_limit` reads to decide whether a turn is against a stop. Say what the encoder is
        # still on, and leave the ring position alone.
        sel_now = st.get("chan_sel")
        if sel_now is not None and sel_now >= 0:
            speech.say(_selected_phrase(sel_now))
            return
        # A short press advances the fixed ring (_encoder_ring), so once we know where we are we
        # can say WHAT was just selected and where it sits - before a click moves it. Until the first
        # movement seats us, all we can honestly say is "Select". A release with no press behind it
        # (the announcer started mid-press) counts as a short press: a possibly-wrong name
        # self-corrects, silence just leaves you guessing.
        pos = st.get("enc_pos")
        if pos is None:
            speech.say("Select")
        else:
            st["enc_pos"] = (pos + 1) % len(_encoder_ring())
            speech.say(_ring_phrase(st, st["enc_pos"]))
        return


def seed_layout(st: dict) -> None:
    """Read the fader layout ONCE when the announcer starts, so the very first mute press can say
    'Mic 2 muted' rather than 'Channel 7 muted'. Fills in st["known"] (the encoder-ring level cache)
    and st["muted"].

    Seeding the mutes matters because the pushed stream only ever reports CHANGES: without this, a
    channel muted before the announcer started would have the ring quote its level as though it were
    audible, and stay wrong until someone happened to toggle it.

    Takes hooks.lock for the one dump (unlike the read loop, which deliberately never does), and
    gives up quietly on any failure - a missing layout only costs us the channel NAME. Every later
    refresh is free: any page read republishes it, and note_strip_source follows changes on the
    pushed stream."""
    known = {}
    try:
        with hooks.lock:
            blob = rcp_read.read_model()
        vec = rcp_read.values(blob, "channelInputSource")
        channels.publish_strip_vector(vec)
        # Seed the encoder ring's level cache too, so the FIRST press can already say "Chat 97
        # percent" instead of just naming it. faderLevel is 0-127 and only the virtual faders hold a
        # value; outputMonLevel is a 0-1 float.
        fl = rcp_read.values(blob, "faderLevel")
        for i, v in enumerate(fl):
            if isinstance(v, int):
                known[("fader", i)] = max(0.0, min(1.0, v / _FADER_LEVEL_MAX))
        mon = rcp_read.values(blob, "outputMonLevel")
        if mon and isinstance(mon[0], float):
            known[("monitor", 0)] = mon[0]
        # The headphone pots, for the same reason and on the same contract: seeded so a resting
        # value can never be announced as a change. They are read positionally because the four POT
        # nodes appear in pot order under PHYSICALINTERFACE[0] - the same order the pushed path
        # gives - and that is the only place `potLevel` occurs.
        for i, v in enumerate(rcp_read.values(blob, "potLevel")):
            if isinstance(v, int) and i < _POT_COUNT:
                known[("headphone", i)] = max(0.0, min(1.0, v / _POT_LEVEL_MAX))
        mutes = rcp_read.values(blob, "channelOutputMute")
        st["muted"] = {i: v for i, v in enumerate(mutes) if isinstance(v, bool)}
        # SEED EVERY VALUE THE HANDLER COMPARES AGAINST: the stream reports CHANGES, and an
        # unseeded value makes the desk's first push of a resting state ("Levels", "Bank 1") read
        # as a change and be announced out of nowhere. Seeding makes an unchanged value silent.
        sel = rcp_read.values(blob, "systemChannelSelected")
        if sel and isinstance(sel[0], int):
            st["chan_sel"] = sel[0]
        # Stored RAW (the desk numbers banks from 0); the announcement adds 1.
        bank = [v for v in rcp_read.values(blob, "selectedBank") if isinstance(v, int)]
        if bank:
            st["bank"] = bank[0]
            hooks.note_bank(bank[0])
        # THE USB AUDIO MODE is seeded for the opposite reason: not to keep a resting value quiet,
        # but so a reconnect can tell that it MOVED. The desk
        # can drop out of Expanded on its own (measured: off the bus unprompted, back two seconds
        # later in Standard with the setting reset), and the change is invisible - the operator's
        # extended inputs stop carrying anything and their computer's audio has rearranged
        # itself. The push cannot carry this: the desk leaves the bus as it changes, so
        # any push is sent into the gap. Comparing across the reconnect is the only way to see it.
        st["usb_mode"] = rcp_read.first(blob, "outputMultiMode", "int")
        channels.publish_usb_expanded(recorder.split_usb_multitrack(st["usb_mode"])[1])
    except Exception as exc:
        diagnostics.event("announcer.seed_failed", "the announcer could not read the layout",
                          error=str(exc))
    st["known"] = known
    # WHAT COUNTS AS ALREADY SPOKEN, seeded on the same contract: `flush_pending` speaks a
    # level unless it matches st["said"], so an empty one opens every connect and reconnect with
    # "Monitor 95 percent" as though something had moved it.
    st["said"] = dict(known)


def _pct(level):
    """A level as the whole percent we would SAY, or None. Comparing what we would say (rather than
    the raw float) is what stops the same number being announced twice."""
    return None if level is None else max(0, min(100, int(round(level * 100))))


def _selected_phrase(strip_idx: int) -> str:
    """What to say when a channel button hands the encoder to a strip.

    Says up front whether there is anything to adjust, rather than letting the operator turn the
    encoder and get silence: the SMART pads strip has no gain and no processing (RODE's own guide:
    "You can add processing to any audio channel except the SMART pads"), so its set-up screen leaves
    the encoder with nothing to move. We work that out from the channel itself - whether its source
    has a mic-gain or line-level selector - so it is known at press time, no waiting.

    SAY WHAT THE CHANNEL HAS, NEVER WHAT IT LACKS. A bus such as the SMART pads has no gain but
    still rides a fader, so it gets "Fader level only", which is true whether that fader is
    physical or virtual. Only a strip with NOTHING on it gets "nothing to adjust"."""
    who = channel_label(strip_idx)
    dev = devices.get_active_device()
    try:
        vec = channels.cached_strip_vector() or channels.last_known_strip_vector() or []
        code = vec[strip_idx] if 0 <= strip_idx < len(vec) else None
    except Exception:
        code = None
    # Match on the SOURCE CODE only, never on the strip index. They are different numbers the moment
    # the faders are reassigned, so an index match announces "gain" for a line channel as soon as a
    # line source sits on a strip whose index belongs to a mic.
    entry = next((c for c in (dev.ui_channels or ()) if code is not None
                  and c.get("source_code") == code), None)
    if entry and entry.get("kind") == "mic":
        return f"{who} selected. Encoder sets its gain"
    if entry and entry.get("kind") == "line":
        return f"{who} selected. Encoder sets its input level in decibels"
    # No gain of its own: a bus, or a source this desk's read map does not list (CallMe 1 on either
    # desk). It still has a fader, so say so.
    if code is not None and code >= 0:
        return f"{who} selected. Fader level only"
    return f"{who} selected. Nothing here for the encoder to adjust"


def _gain_label(path, is_mic: bool):
    """Name the channel whose GAIN just moved, from the ADDRESS the change was pushed at.

    Mic strips carry 'gain'; line strips carry 'level', which is what the app and the desk call them,
    so the spoken phrase matches the control the operator knows.

    THE ADDRESS IS THE WHOLE PATH, not the low selector byte, matched against
    DeviceModel.input_path: the announcer and the writer read the same rule, so they cannot drift.
    A per-device selector table would miss every gain turned on a Duo's own controls."""
    dev = devices.get_active_device()
    entry = dev.channel_for_input_path(path)
    if entry is None:
        return None
    if is_mic:
        if entry.get("kind") != "mic":
            return None
        return f"Mic {channels.mic_channel_number(entry)} gain"
    if entry.get("kind") != "line":
        return None
    # RESOLVED from the wide set above, NAMED from the current mode: the entry that an address
    # matches is fixed, what it is called is not.
    label = (dev.fader.labels_for(channels.usb_expanded()).get(entry.get("source_code"))
             or entry.get("label") or entry.get("name"))
    return f"{label} input level"


def _at_limit(st: dict, delta: int) -> bool:
    """Would a turn in this direction do nothing because we are already at the end of the range?

    Uses the ring's own cache of where things sit, so it only ever suppresses the "not linked"
    message when we actually know the level is against a stop."""
    pos = st.get("enc_pos")
    if pos is None or not delta:
        return False
    ring = _encoder_ring()
    lvl = (st.get("known") or {}).get(ring[pos % len(ring)])
    if lvl is None:
        return False
    return (delta < 0 and lvl <= 0.001) or (delta > 0 and lvl >= 0.999)


def _level_words(name: str, level: float, *, name_first: bool, say_percent: bool = True) -> str:
    """THE one place a level is turned into words. Both announcements below come through here.

    WHY THE WORD ORDER IS A PARAMETER AND NOT A CONSTANT. The two callers want opposite orders and
    both are right, which is the whole reason this is one function with a flag rather than two
    functions that drifted apart:

      * A LEVEL MOVED -> number first. Speaking the name first is what makes a turn feel laggy:
        "USB 2 (Main Stereo)" takes about a second, and each new update purges and restarts the
        speech, so the number never arrives until the movement stops. Number-first means the
        changing fact lands even when the next update cuts the phrase mid-word, and the name
        completes on the first pause.
      * SOMETHING WAS SELECTED -> name first. Nothing has moved yet; the question the press asks is
        "what have I got", so the answer has to lead.

    `say_percent` is dropped only during a RUN of updates, where the unit is obvious and the extra
    syllables just eat into the name."""
    pct = _pct(level)
    if name_first:
        return f"{name} {pct} percent" if say_percent else f"{name} {pct}"
    return f"{pct} percent, {name}" if say_percent else f"{pct}, {name}"


def _moved_phrase(kind: str, source: int, level: float, short: bool = False) -> str:
    """A level that just MOVED. `source` is a SOURCE CODE, never a fader index.

    Source code is the right space here and a fader index is not, because a channel can be OFF
    FADER entirely - the Pro II always has one - and its level still changes and still deserves a
    name. `_ring_phrase` works in the other space, and the two are easy to confuse because both
    speak of a "fader" kind; check which space you are in before calling either."""
    name = ("Monitor" if kind == "monitor"
            else f"Headphone {source + 1}" if kind == "headphone"
            else _source_label(source))
    return _level_words(name, level, name_first=False, say_percent=not short)


def _fader_label(fader_idx: int) -> str:
    """Name the channel sitting on a FADER (by index), from the known layout - so the encoder ring
    announces "Chat" rather than "fader 8", and follows the layout if a source is moved."""
    try:
        vec = channels.cached_strip_vector() or channels.last_known_strip_vector()
        if vec and 0 <= fader_idx < len(vec) and vec[fader_idx] is not None and vec[fader_idx] >= 0:
            return _source_label(vec[fader_idx])
    except Exception:
        pass
    return f"Fader {fader_idx + 1}"


def _ring_phrase(st: dict, pos: int) -> str:
    """What to say the moment the encoder is pressed: the thing now selected and, when we know it,
    where it currently sits. The level comes from our own tracking (seeded at startup and updated
    from every push), so it costs no device read and no turning.

    THIS SIDE WORKS IN FADER INDICES, because that is what the encoder cycles through and what
    `st["known"]` and `st["muted"]` are keyed by. `_moved_phrase` works in SOURCE CODES. Keep
    them straight: the translation between the two lives in `_note_level` and nowhere else."""
    ring = _encoder_ring()
    kind, idx = ring[pos % len(ring)]
    name = "Monitor level" if kind == "monitor" else _fader_label(idx)
    # A muted channel's level is not the useful fact about it, and quoting a healthy-sounding
    # percentage for something you cannot hear is actively misleading. Say the mute instead. The
    # map is seeded from the startup dump and kept current from every pushed
    # `channelOutputMute`, so it costs no device read. Fader index and strip index are the same
    # space here: both index the channelInputSource vector.
    if kind == "fader" and (st.get("muted") or {}).get(idx):
        return f"{name}, muted"
    lvl = (st.get("known") or {}).get((kind, idx))
    if lvl is None:
        return name
    return _level_words(name, lvl, name_first=True)


def _ring_seat(st: dict, kind: str, idx: int, level: float) -> None:
    """A real movement just told us what is selected: re-seat our ring position and cache the level.
    This is what makes a wrong prediction self-correcting."""
    st.setdefault("known", {})[(kind, idx)] = level
    for i, entry in enumerate(_encoder_ring()):
        if entry == (kind, idx):
            st["enc_pos"] = i
            return


def _note_level(st: dict, kind: str, source: int, level: float) -> None:
    """Record a level change, and speak it AT ONCE when it is the first one after an encoder press.

    `source` is a SOURCE CODE (0 for the monitor). THIS IS THE ONE PLACE the two index spaces meet:
    everything spoken is keyed by source, because a source can be off-fader and still have a level,
    while the encoder ring's cache is keyed by FADER INDEX because that is what the encoder cycles.
    The translation below is the only crossing point; do not add another.

    The PRESS (`encoderPressed`) carries nothing that says WHICH channel it selected, and the target
    is chosen by touch. So the first movement has to do that job: speaking it immediately means
    ONE click names the channel, instead of nudging repeatedly and waiting for each nudge to
    settle to find out what is being held. Later movements in the same gesture still wait for the
    settle, so a full sweep is one closing phrase rather than a torrent."""
    key = (kind, source)
    now = time.monotonic()
    st.setdefault("levels", {})[key] = level
    st["levels_at"] = now
    st["enc_turn_at"] = None        # something moved, so that turn was not against nothing
    # THE CROSSING POINT. The ring's cache is keyed by FADER INDEX (what faderLevel reports and what
    # the encoder cycles); cross-point pushes arrive keyed by SOURCE CODE. Translate through the
    # layout, or a level changed from our own sliders leaves the ring quoting an old number on the
    # next press. A source that is on NO fader simply has no ring entry, which
    # is correct - and it is still spoken, because speech is keyed by source.
    known = st.setdefault("known", {})
    if kind == "monitor":
        known[("monitor", 0)] = level
    elif kind == "headphone":
        # A headphone pot is its own space: it is not a source and it is not on a fader, so there
        # is nothing to translate and the crossing point below must not be asked to try.
        known[("headphone", source)] = level
    else:
        try:
            vec = channels.cached_strip_vector() or channels.last_known_strip_vector() or []
            fidx = vec.index(source)
        except (ValueError, AttributeError):
            fidx = None
        if fidx is not None:
            known[("fader", fidx)] = level
    if st.get("enc_at") and not st.get("enc_named"):
        st["enc_named"] = True
    said = st.setdefault("said", {})
    at = st.setdefault("said_at", {})
    # OUR OWN write, echoed back by the desk (see mark_self_write). The UI has already announced
    # it, so stay quiet - but record it as spoken and drop the held value, or the settle flush would
    # speak it a moment later and we would be back where we started.
    if is_self_echo(key):
        said[key], at[key] = level, now
        st["levels"].pop(key, None)
        return
    # Speak straight away unless we have just spoken this same target: identical percentages are not
    # worth repeating (each update arrives 13 times, once per cross-point), and back-to-back updates
    # are spaced so a sweep sounds like a count instead of a stutter.
    if _pct(said.get(key)) != _pct(level) and (now - at.get(key, 0.0)) >= _LEVEL_GAP_S:
        # Bare number while the SAME thing keeps moving; full name when the target changes or after a
        # pause, so you always know what you grabbed without hearing the name over and over.
        short = (st.get("run_key") == key and (now - st.get("run_at", 0.0)) < _RUN_S)
        said[key], at[key] = level, now
        st["run_key"], st["run_at"] = key, now
        st["levels"].pop(key, None)
        speech.say(_moved_phrase(kind, source, level, short=short))


def flush_pending(st: dict) -> None:
    """Speak anything being held: a press edge with no STATE behind it (_PENDING_S), and the
    settled position of a fader or the monitor encoder (_SETTLE_S)."""
    if st.get("pending") and (time.monotonic() - st.get("at", 0.0)) > _PENDING_S:
        speech.say(st["pending"])
        st["pending"] = None
    # A turn that moved nothing: tell the operator, once per burst.
    turn_at = st.get("enc_turn_at") if levels_on else None
    if turn_at and (time.monotonic() - turn_at) > _ENC_DEAD_S:
        st["enc_turn_at"] = None
        if (time.monotonic() - st.get("enc_dead_said", 0.0)) > _ENC_DEAD_GAP_S:
            st["enc_dead_said"] = time.monotonic()
            # Name what is holding the encoder, saying what the channel HAS (_selected_phrase):
            # this fires on ANY turn the ring does not see, including a mic gain that is moving
            # perfectly well, so a flat "nothing to adjust" would be wrong.
            #
            # The way out is MEASURED, not assumed: the channel button TOGGLES
            # (systemChannelSelected 0, then -1 on the second press), so that is what is named here.
            sel = st.get("chan_sel")
            if sel is not None and sel >= 0:
                speech.say(_selected_phrase(sel)
                         + ". Press its channel button again for the fader and monitor levels")
            else:
                speech.say("Encoder not linked to anything")
    levels = st.get("levels")
    if levels and (time.monotonic() - st.get("levels_at", 0.0)) > _SETTLE_S:
        said = st.setdefault("said", {})
        for key, lvl in list(levels.items()):
            if _pct(said.get(key)) == _pct(lvl):
                continue                       # this position was already spoken while moving
            said[key] = lvl
            st.setdefault("said_at", {})[key] = time.monotonic()
            st["run_key"] = None            # the gesture is over; name it again next time
            speech.say(_moved_phrase(key[0], key[1], lvl))
        levels.clear()


# The LIVE announcer state, published so the rest of the server can keep it current, and the record
# of what WE have just written so the desk's echo of it is not read back at us.
#
# OUR OWN WRITES *ARE* ECHOED ON THE PUSH STREAM. Hardware proof, against the running app with no
# hand on the desk: three level writes over the HTTP API come straight back and are announced -
# "60 percent, USB 2 (Secondary)", "80 percent, Monitor". What the echo is NOT is a
# reliable cache: it can lag, and it is absent while the announcer is off, which is why the publish
# helpers below still exist.
#
# Without the suppression below, every change is said TWICE: the screen reader reads our live region
# and the announcer speaks the echo. The fix is narrow on purpose: suppress the SPEECH for the exact
# target just written, for a moment, and never the cache updates. A change made ON the desk is
# unaffected and still speaks at once, which is the whole point of the announcer and why "turn it
# off while the app has focus" is the wrong shape. The window is refreshed by each write, so dragging
# a slider stays quiet all the way down, and a hand on that same fader is audible again
# _SELF_ECHO_S after the last write.
#
# NOT covered, deliberately: a profile RESTORE, which writes the whole desk at once. Narrating that
# is a different problem (it wants one "restoring" phrase, not 40 suppressions) and it has its own
# progress reporting already.
live_state = None
_SELF_ECHO_S = 1.5
_self_writes: dict = {}


def mark_self_write(key) -> None:
    """Remember that WE just wrote `key`, so the desk's echo of it is not spoken back at us.

    Keys are in the space the PUSH arrives in, not the space the writer thinks in: a fader is keyed
    by its SOURCE CODE (what the cross-point selector carries), not its fader index. Marked whether
    or not the announcer is running, since it costs nothing and the two can be toggled apart."""
    _self_writes[key] = time.monotonic()


def is_self_echo(key) -> bool:
    """Is this push the echo of our own write to the same target, close enough behind it to be that
    write rather than a fresh change on the desk?"""
    at = _self_writes.get(key)
    return at is not None and (time.monotonic() - at) < _SELF_ECHO_S


def mark_self_gain(key, is_mic: bool) -> None:
    """Mark a gain/level write of ours under the SAME label the push side builds for it.

    The two sides name a gain differently - the writer has a mic number or a channel name, the push
    has an ADDRESS - so we resolve ours to that same address and let _gain_label do the naming,
    rather than keeping two naming schemes in step. `key` is the mic number (mic strips) or the
    channel name (line strips)."""
    try:
        dev = devices.get_active_device()
        if is_mic:
            def matches(c):
                return c.get("kind") == "mic" and channels.mic_channel_number(c) == key
        else:
            def matches(c):
                return c.get("kind") == "line" and c["name"] == key
        entry = next((c for c in (dev.ui_channels or ()) if matches(c)), None)
        if entry is None:
            return
        who = _gain_label(dev.input_path(entry), is_mic)
        if who:
            mark_self_write(("gain", who))
    except Exception:
        pass


def publish_level(kind: str, idx: int, level01: float) -> None:
    """Tell the announcer about a level WE just wrote, so the encoder ring quotes the new value.

    Cache only, deliberately SILENT: the web UI already announces the result in its live region, and
    speaking as well would say everything twice. Keyed exactly as the ring is - ("fader", fader
    index) or ("monitor", 0) - so no layout translation is needed (that is only for cross-point
    pushes, which arrive keyed by source code). Safe no-op when the announcer is off.

    It also marks the echo, which DOES need that translation: the desk pushes this write back as a
    cross-point keyed by source code, and speaking it would be the second announcement of a change
    the UI has already reported."""
    if kind == "monitor":
        mark_self_write(("monitor", 0))
    else:
        try:
            vec = channels.cached_strip_vector() or channels.last_known_strip_vector() or []
            if 0 <= int(idx) < len(vec) and vec[int(idx)] is not None:
                mark_self_write(("fader", vec[int(idx)]))
        except (TypeError, ValueError, AttributeError):
            pass
    st = live_state
    if st is None:
        return
    try:
        st.setdefault("known", {})[(kind, int(idx))] = max(0.0, min(1.0, float(level01)))
    except (TypeError, ValueError):
        pass


def publish_mute(strip_idx: int, on: bool) -> None:
    """Tell the announcer about a mute WE just wrote, so the encoder ring keeps quoting the truth.

    Same contract as publish_level: cache only, deliberately SILENT (the web UI announces the
    mute itself in its live region), and it marks the echo so the announcer does not say it a second
    time. The ring says "Mic 2 muted" instead of a level for a muted channel, so without the cache a
    mute set from here would leave the ring reading out a level nobody can hear, the mirror image of
    a stale level. Keyed by STRIP index, exactly as the desk's own pushes are,
    so no translation is needed here. Safe no-op when the announcer is off."""
    try:
        mark_self_write(("mute", int(strip_idx)))
    except (TypeError, ValueError):
        pass
    st = live_state
    if st is None:
        return
    try:
        st.setdefault("muted", {})[int(strip_idx)] = bool(on)
    except (TypeError, ValueError):
        pass


def publish_solo(strip_idx: int, on: bool) -> None:
    """Mark a solo WE wrote, so a future firmware that echoes it does not get it spoken twice.

    Measured: this desk pushes NOTHING back for a software solo, so the mark is a no-op on it. It is
    here anyway because the announcer's solo branch speaks every channelCueEnable push it sees, so a
    firmware that starts echoing would have a channel announcing its own solo on top of the UI, the
    same bug the level path already carries a fix for."""
    try:
        mark_self_write(("solo", int(strip_idx)))
    except (TypeError, ValueError):
        pass


def forget_solos() -> None:
    """Mark every strip's solo as ours, for the clear-all path: one call can flip many strips, and
    each would otherwise be a separate push to speak over the single message the UI gives."""
    for strip in range(16):
        mark_self_write(("solo", strip))


def usb_mode_change(was, now) -> str:
    """What to say when the USB audio mode is not what it was before the desk left the bus.

    Empty when it has not moved, so the caller falls back to its own wording.

    IT NAMES THE HALF THAT CHANGED, not both. A re-enumeration resets the whole property, but
    reciting two settings when one moved buries the one that matters, and Expanded going away is
    the consequential half: the operator's Game, Music, A and B inputs go dead and every
    RODECaster device on the computer is renamed under it. Spoken rather than shown because
    the desk does this unprompted, when nobody is looking at the app.
    """
    if was is None or now is None or was == now:
        return ""
    _, was_exp = recorder.split_usb_multitrack(was)
    now_mode, now_exp = recorder.split_usb_multitrack(now)
    if was_exp != now_exp:
        # Leaving it, the faders are tidied and spoken about separately
        # (`server._tidy_after_standard`), so this says only what is certain.
        return "USB input is now Expanded." if now_exp else "The unit has left Expanded USB input."
    # A value outside the six measured ones is still worth SAYING, just not naming: going silent on
    # an unknown would hide exactly the case nobody has seen before.
    label = recorder.USB_MULTITRACK_LABEL.get(now_mode)
    return (f"USB output to this computer is now {label.lower()}." if label else
            "The unit's USB audio mode has changed.")


def _loop(gen: int | None = None) -> None:
    global live_state
    if gen is None:
        gen = _loop_gen

    def current() -> bool:
        return speech.enabled.is_set() and gen == _loop_gen

    if not current():
        return                        # superseded before it started; the newer loop greets
    iface = rcp_transfer.find_control_interface()
    if iface is None:
        speech.say("The control announcer could not open the device.")
        speech.enabled.clear()
        return
    # One persistent read-only handle for the whole session. No hooks.lock, no per-cycle
    # close: input reports are delivered to this handle's own queue regardless of the
    # pad/mixer writes, so there are no deaf windows where a press gets dropped.
    dev = None
    misses = 0                        # consecutive failed opens, for the backoff below
    lost_at = 0.0
    st = {"bank": None, "pending": None, "at": 0.0, "levels": {}, "levels_at": 0.0,
          "known": {}, "muted": {}, "enc_pos": None, "enc_down_at": None, "enc_hold": False,
          "said": {}}
    announced_on = False
    seed_layout(st)
    live_state = st              # publish it so our own writes can keep the caches current
    while current():
        if dev is None:
            if iface is None:
                iface = rcp_transfer.find_control_interface(fresh=True)
            try:
                dev = rcp_hid.open_handle(iface, "announcer") if iface is not None else None
            except Exception as exc:
                diagnostics.event("device.open_failed", "announcer", error=f"{exc}")
                dev = None
            if dev is None:
                # THE CACHED PATH GOES STALE ON A RE-ENUMERATION, so it is dropped after any failed
                # open and re-resolved on the next pass. The desk returns as a DIFFERENT USB product
                # id - the id encodes the audio configuration, so `outputMultiMode` changes it - and
                # a handle re-opened on the old path fails for ever, leaving the announcer silently
                # retrying until the app restarts.
                iface = None
                misses += 1
                time.sleep(_RETRY_S if misses <= _RETRY_FAST_TRIES
                           else _RETRY_SLOW_S)
                continue
            misses = 0
            if not announced_on:
                # Quiet when the RECORDER is putting announcements back: the stop phrase already
                # says they are back on, and the pump keeps only the newest phrase, so a greeting
                # here would silence it.
                if not _quiet_start.is_set():
                    speech.say("Control announcer is on.")
                _quiet_start.clear()
                announced_on = True
            elif time.monotonic() - lost_at >= _RECONNECT_SPEAK_AFTER_S:
                # Back after a real gap. Re-seed first: mutes, levels and the fader layout can all
                # have moved while we were deaf, and Expanded mode reassigns sources as it goes.
                was_usb = st.get("usb_mode")
                seed_layout(st)
                hooks.reconnected(was_usb, st.get("usb_mode"))
        try:
            rep = dev.read(256, timeout_ms=200)
        except Exception as exc:
            diagnostics.event("device.lost", "announcer read failed", error=f"{exc}")
            rcp_hid.forget_interfaces()
            rcp_hid.close_handle(dev, "announcer")
            dev = None                                     # reopen on the next pass
            lost_at = time.monotonic()
            time.sleep(0.3)
            continue
        if not current():
            break
        try:
            if rep:
                handle_report(bytes(rep), st)
            # Runs every read cycle (the read above blocks at most 200 ms), so a held press edge is
            # spoken promptly when the desk sends no state to supersede it.
            flush_pending(st)
        except Exception as exc:
            # Decoding is best-effort: a malformed or unexpected packet costs us ONE announcement,
            # never the announcer. Carrying on beats a thread that dies and leaves the UI claiming
            # announcements are on.
            diagnostics.event("announcer.push_undecoded", str(exc))
    if dev is not None:
        rcp_hid.close_handle(dev, "announcer")
    if gen != _loop_gen:
        # Superseded: the newer loop owns the published state and does its own greeting.
        _quiet_stop.clear()
        return
    live_state = None            # stale state must not outlive the announcer
    # A SUSPEND MUST NOT SPEAK. The sign-off is right when the user switched announcements off; it
    # is exactly wrong when the recorder switched them off, because that phrase would be the first
    # thing in the recording - the very thing the suspend exists to prevent.
    if not _quiet_stop.is_set():
        speech.say("Control announcer is off.")
    _quiet_stop.clear()


# Set while the RECORDER is switching announcements off or back on, so the announcer thread leaves
# and returns without a greeting of its own (`server._recorder_state_changed` says why that
# matters).
_quiet_stop = threading.Event()
_quiet_start = threading.Event()
# True when WE suspended announcements for a recording, so they are only ever switched back on if
# we were the ones who switched them off. A user who had them off keeps them off.
suspended_for_record = False


# ON BY DEFAULT, and it is a CHOICE rather than a policy. Most people do not want announcements in
# the recording, so the default has to be off-while-recording. But somebody
# whose announcements already go to a separate output - a second pair of earbuds, say - is not
# putting anything into the take and would rather keep hearing the unit. Assuming that away would
# silence the operator for the whole show for no reason.
_RECORD_MUTE_DEFAULT = True


def record_mutes_announcer() -> bool:
    return bool(settings_file.load().get("record_mute_announcer", _RECORD_MUTE_DEFAULT))


def action_set_record_mute_announcer(on: bool) -> dict:
    """Whether a recording switches announcements off for its duration."""
    on = bool(on)
    settings_file.update(record_mute_announcer=on)
    if not on:
        # Turning the option off mid-take must give the announcements back straight away, or the
        # switch would appear to do nothing until the recording ended.
        restore_after_record()
    return {"ok": True, "mute_announcer": on,
            "message": ("Announcements will be switched off while recording."
                        if on else "Announcements will keep playing while recording.")}


def suspend_for_record() -> bool:
    """Switch announcements off for the duration of a recording. Returns True if it did anything.

    WHY SILENCE RATHER THAN A DIFFERENT OUTPUT: sending the speech to the computer's own speakers
    only takes it off the USB return, and anything audible in the room reaches the microphones
    anyway."""
    global suspended_for_record
    if not record_mutes_announcer():
        return False                       # the user keeps their announcements; see above
    if not speech.enabled.is_set():
        return False                       # already off: leave the user's own choice alone
    _quiet_stop.set()
    speech.enabled.clear()
    suspended_for_record = True
    return True


def restore_after_record() -> bool:
    """Switch announcements back on, but ONLY if we were the ones who switched them off."""
    global suspended_for_record
    if not suspended_for_record:
        return False
    suspended_for_record = False
    if speech.enabled.is_set():
        return False                       # the user turned them back on mid-take; leave it
    _quiet_start.set()
    action_set_announce(True, chosen=False)
    return True


def action_set_announce(on: bool, chosen: bool = True) -> dict:
    """Switch announcements on or off. `chosen` is a person's decision (the page, the tray, the
    shortcut), which is saved when they have asked for it to be remembered; the app switching them
    back on at start-up or after a take is not."""
    global _thread, suspended_for_record, _loop_gen
    # A DELIBERATE toggle cancels our claim on it either way, so a recording stopping later never
    # overrides what the user has since chosen.
    suspended_for_record = False
    if chosen and remember:
        settings_file.update(announcer=bool(on))
    if on:
        with _start_lock:
            if not speech.enabled.is_set():
                _loop_gen += 1
                speech.enabled.set()
                _thread = threading.Thread(target=_loop, args=(_loop_gen,),
                                                    daemon=True)
                _thread.start()
        return {"ok": True, "announce": True,
                "message": "Button-press announcements are on."}
    speech.enabled.clear()
    return {"ok": True, "announce": False,
            "message": "Button-press announcements are off."}


def action_set_announce_levels(on: bool) -> dict:
    """Whether the announcer also speaks a fader or monitor level once you stop moving it.

    Separate from the button announcements on purpose: mute and solo are events you always want
    spoken, whereas a running commentary on every fader move suits some sessions and not others.
    One phrase per gesture, not per update (see _SETTLE_S)."""
    global levels_on
    levels_on = bool(on)
    settings_file.update(announcer_levels=levels_on)
    return {"ok": True, "levels": levels_on,
            "message": ("Fader and monitor levels will be spoken when you stop moving them."
                        if on else "Fader and monitor levels will not be spoken.")}


def action_set_announce_mute_tones(on: bool) -> dict:
    """Whether a mute or unmute is a TONE rather than a spoken sentence.

    The reasoning is timing: mute is pressed in the middle of something, and a sentence finishes
    after the moment it was describing. A cue of two notes, falling for muted and rising for
    unmuted, lands in a sixth of a second.

    THE TRADE IS THE CHANNEL NAME, which a tone cannot carry. That is why it is an option and why it
    is off by default. Everything else the announcer says is unaffected.

    The confirmation plays the tones themselves, in order, after the sentence: hearing them is the
    only way to learn them, and being told "two tones" teaches nobody which is which.
    """
    global mute_tones
    mute_tones = bool(on)
    settings_file.update(announcer_mute_tones=mute_tones)
    if on:
        # PLAYED WITH A GAP. The pump coalesces whatever is waiting down to the newest item, so two
        # cues queued back to back play only the second. Long enough for the first tone to play and
        # to clear the pump's own pacing.
        speech.tone("muted")
        time.sleep(max(0.45, speech.PACE_S * 2 + tones.DURATION_S))
        speech.tone("unmuted")
    return {"ok": True, "mute_tones": mute_tones,
            "message": ("Mutes will play a tone instead of being spoken: two notes falling means "
                        "muted, two notes rising means unmuted."
                        if on else "Mutes will be spoken, naming the channel.")}


def action_set_announce_remember(on: bool) -> dict:
    """Whether announcements start the way they were left, rather than always on.

    Ticking it saves the CURRENT state at once, so "the way I left them" holds from now on rather
    than from the next toggle. A take that has suspended announcements counts as on: the person
    left them on, the recording only paused them."""
    global remember
    remember = bool(on)
    if remember:
        settings_file.update(announcer_remember=True,
                        announcer=speech.enabled.is_set() or suspended_for_record)
    else:
        settings_file.update(announcer_remember=False)
    return {"ok": True, "remember": remember,
            "message": ("Announcements will start the way you leave them." if remember
                        else "Announcements will always be on when the app starts.")}


def action_set_announce_startup_summary(on: bool) -> dict:
    """Whether the desk's state is spoken when the app opens.

    Only the EXCEPTIONS: what is muted or soloed, a pad left switched on, a recording already
    running, a nearly-full drive, and any output not on its usual mix. On a desk with nothing to
    report it is a single sentence saying so. The full picture stays on the Device page, which is
    where you go when the sentence tells you something is wrong.
    """
    global startup_summary
    startup_summary = bool(on)
    settings_file.update(announcer_startup_summary=startup_summary)
    return {"ok": True, "startup_summary": startup_summary,
            "message": ("The desk's state will be spoken when the app opens."
                        if on else "The desk's state will not be spoken when the app opens.")}


def apply_settings(changes: dict) -> None:
    """Push restored announcer preferences into the running announcer, not just the file.

    `announcer` (the remembered on or off) is left to the next launch: switching speech on or off
    in the middle of an import would be its own surprise."""
    global remember, levels_on, mute_tones, startup_summary
    if "announcer_remember" in changes:
        remember = bool(changes["announcer_remember"])
    if "announcer_levels" in changes:
        levels_on = bool(changes["announcer_levels"])
    if "announcer_mute_tones" in changes:
        mute_tones = bool(changes["announcer_mute_tones"])
    if "announcer_startup_summary" in changes:
        startup_summary = bool(changes["announcer_startup_summary"])
