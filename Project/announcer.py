"""The control announcer: speaks the desk's own button presses aloud (an accessibility aid).

A background thread follows what the desk pushes (`desk_live`) and speaks bank changes / mute /
solo / encoder presses through `speech`, so a screen-reader user knows what a physical press did
without seeing the touchscreen.

DEVICE-AGNOSTIC: the decoder keys off the report-04 PROPERTY NAMES
(selectedBank / cmutePressed / csoloPressed / encoderPressed), which are protocol-level and shared
across both desks, NOT a Pro II physical control-map or channel-index table. It speaks generic
"Bank N" / "Mute" / "Solo" / "Select" (no per-channel index), so the Duo's fewer faders/banks need
no special-casing. It is also strictly READ-ONLY, so even if a name ever differs on a future desk
the worst case is a missed announcement, never a mis-write.

Switching announcements on subscribes to the listener and switching them off unsubscribes; the
listener itself, and the reconnect after a re-enumeration, belong to `desk_live`.
"""
from __future__ import annotations

import threading
import time

import change_words
import channels
import desk_live
import desk_model
import devices
import diagnostics
import rcp_read
import rcp_transfer
import recorder
import settings_file
import speech
import tones


class Hooks:
    """What the announcer needs from the app around it. `service.start` installs its own; these
    defaults do nothing, so the decoder also runs alone in the offline tests and DevTools."""

    lock = threading.Lock()   # the device lock, for a seeding dump; the app's is hid_lock

    def mixer_positions(self) -> frozenset:
        """The pad positions that hold mixer pads."""
        return frozenset()

    def reconnected(self, was_usb, now_usb) -> None:
        """The desk came back after a real gap, already read again; the USB modes are before and
        after."""


hooks = Hooks()

_thread = None
# Each loop is started with the next generation and leaves as soon as it is no longer the newest.
# Off and on within one wait would otherwise leave the first loop running beside the second, and
# every push and reconnect handled twice.
_loop_gen = 0
_start_lock = threading.Lock()   # the check-and-start below runs on request threads
# How often pending speech is looked at when the desk pushes nothing, so a held press edge is
# spoken promptly when no state follows it.
_TICK_S = 0.2
# A reconnect is only worth speaking about when the gap was long enough to have swallowed a press.
# Coming straight back on the next pass is not news.
_RECONNECT_SPEAK_AFTER_S = 1.0


# MUTE / SOLO STATE.
# Pressing mute or solo pushes, unasked and about 70 ms after the button edge, the STATE property
# `channelOutputMute` / `channelCueEnable`, addressed at CHANNEL[strip index]. So the
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
# HELD WHILE THE APP ITSELF REWRITES THE DESK: a profile restore, a show command, a pad restore.
# Every setting such a job writes comes back as a push, and reading each one out ("55 d B, Mic 1")
# describes nothing the operator chose and buries the job's own progress. Only the SPEECH is held:
# the state cache keeps updating, so the first turn afterwards still quotes the truth. A count,
# because jobs on different job states can overlap, and a tail after the last release, because the
# final writes' pushes arrive after the job has returned.
_HOLD_TAIL_S = 1.5
_hold_lock = threading.Lock()
_holds = 0
_hold_until = 0.0


def hold_desk_speech() -> None:
    """Stop speaking desk changes until the matching `release_desk_speech`. Says nothing itself: a
    job is always started from a page or the window, which speaks its progress through the screen
    reader, and a sentence in this voice as well talks over it."""
    global _holds
    with _hold_lock:
        _holds += 1


def release_desk_speech() -> None:
    global _holds, _hold_until
    with _hold_lock:
        _holds = max(0, _holds - 1)
        if _holds == 0:
            _hold_until = time.monotonic() + _HOLD_TAIL_S


def _desk_speech_held() -> bool:
    return _holds > 0 or time.monotonic() < _hold_until


def _say(text: str) -> None:
    """Speak a desk change, unless a job is rewriting the desk."""
    if _desk_speech_held():
        diagnostics.event("announcer.held", text)
    else:
        speech.say(text)


def _tone(name: str) -> bool:
    """Play a desk-change cue; False when held, so the caller's spoken fallback is held too."""
    return False if _desk_speech_held() else speech.tone(name)
# PRESS AND HOLD ON THE ENCODER TOGGLES THE MUTE OF WHAT THE ENCODER IS ON: a virtual fader's
# channel (`channelOutputMute`), or the monitor speakers (`outputMonMute`). With a channel button
# holding the encoder, a hold mutes nothing. Measured: a short press runs about 200 to 475 ms
# down-to-up and mutes nothing; a hold pushes the mute about 750 ms after the press edge, WHILE
# STILL HELD, and the desk does not advance its encoder ring. RODE's user guide documents
# press-and-hold only on the REC button.
#
# Two consequences, both of which this handling exists for:
#  1. ANNOUNCING THE NEXT RING POSITION ON THE PRESS EDGE IS WRONG ON A HOLD: you hear the next
#     target named and then the previous one announced as muted, and our ring position silently
#     desyncs from the desk's, so every later prediction is one out until a real movement re-seats
#     it. So we wait for the RELEASE edge, which the desk does push, and only advance on a short
#     press.
#  2. What tells the two apart is the mute push itself, not a stopwatch: either mute arriving
#     between our press and release means the desk took the gesture. The duration below is
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
# WHAT RECORDING STARTING, PAUSING AND STOPPING SOUNDS LIKE, from the unit or from here
# (recording._state_changed): spoken, a tone in its place, or nothing. Never both: speech after
# the tone repeats it and lands in the take. A card or drive going in or out is spoken whichever is
# chosen. Spoken by default, like the mutes.
RECORD_FEEDBACK = {"speak": "Speak it", "tone": "Play a tone", "silent": "Say nothing"}


def _saved_record_feedback(saved: dict) -> str:
    mode = saved.get("announcer_record_feedback")
    if mode in RECORD_FEEDBACK:
        return mode
    # The earlier tick box, "play a tone instead of speaking it".
    return "tone" if saved.get("announcer_record_tones") else "speak"


record_feedback = _saved_record_feedback(settings_file.load())


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
# guess costs one spoken phrase because the first real movement re-seats us (see _ring_place).
_ENCODER_RING_PRO2 = (("fader", 6), ("fader", 7), ("fader", 8), ("monitor", 0))


def _encoder_ring() -> tuple:
    """The encoder's press-cycle order for the CONNECTED desk: virtual faders, then monitor level."""
    try:
        spec = devices.get_active_device().fader
        ring = tuple(("fader", i) for i in range(spec.physical, spec.count)) + (("monitor", 0),)
        return ring if len(ring) > 1 else _ENCODER_RING_PRO2
    except Exception:
        return _ENCODER_RING_PRO2


# The headphone pots. Each POT node under PHYSICALINTERFACE[0] publishes its OWN potMin and potMax
# (0 and 127 on a Pro II, read off the desk), so this constant is the desk's figure rather than an
# assumed one - check those two properties before trusting it on a new device.
# Four pots, one per headphone output.
_POT_LEVEL_MAX = 127.0
_POT_COUNT = 4
# TURNING WITH NOTHING BOUND. The encoder only adjusts a level while the desk has one bound to it,
# and pressing the BANK buttons moves the screen to the pads page, which unbinds it.
# In that state the desk still reports the rotation as `encoderSignal` but nothing moves. With no
# channel selected, `encoder_home` puts it back on the virtual faders and monitor by itself; with
# one selected, a turn the ring does not see names that channel, whose button gives the encoder back.
# What the encoder is bound to is touchscreen state with no property of its own:
# `systemChannelSelected` can read -1 while it turns a mic gain.
_ENC_DEAD_S = 0.45     # a turn with no level change within this was a turn against nothing
_ENC_DEAD_GAP_S = 4.0  # and do not repeat that message more often than this


def handle_report(b: bytes, st: dict) -> None:
    """`handle_push` for one raw report-04 frame, for a tool reading a handle of its own: the push is
    applied to the desk model first, as the listener does. A slice of a model dump decodes as
    nothing and is ignored."""
    push = desk_live.decode(b)
    if push is not None:
        was = desk_model.apply_push(push.path, push.name, push.value, push.kind)
        handle_push(push._replace(was=was), st)


def handle_push(push, st: dict) -> None:
    """Speak one push. The desk's state is the desk model's, already holding the pushed value, with
    the value it replaced as `push.was`; `st` is what has been said and what is waiting to be."""
    global _mixer_quiet_until
    path, name, val, kind, node = push[:5]
    was = push.was
    if push.revealed:
        # Found by a read while RODE's app is open: no gesture behind it, and the whole batch
        # arrives at once, so all of it is worded together.
        _note_change(st, push)
        return
    # Typed views of the value, so a branch that wants an int never acts on a string or a bool.
    ival = val if kind == "int" else None
    bval = val if kind == "bool" else None
    fval = val if kind == "float" else None
    # The strip when the addressed node is CHANNEL[strip], else -1.
    strip = node[1] if node and node[0] == "CHANNEL" and node[1] < 16 else -1
    if name == "selectedBank":
        if ival is not None and ival != was:
            _say(f"Bank {ival + 1}")
        return
    # A pad switched on or off is addressed at the pad's STREAM POSITION, the last path element.
    if name == "padActive":
        if bval is not None and path:
            # BOTH EDGES. A momentary pad restores every channel it moved when it is released, and
            # that burst says no more than the one on the way in.
            if path[-1] in hooks.mixer_positions():
                _mixer_quiet_until = time.monotonic() + _MIXER_QUIET_S
        return
    if name == "mixLevelWithAnchor":
        # A LINKED cross-point follows its fader, so a fader is moving: one phrase per gesture,
        # not per push, held until flush_pending speaks it once the pushes stop. The MIX node gives
        # the source, and the matrix row IS the source code. An UNLINKED one is a send level of
        # its own, worded with the other changes.
        cell = rcp_transfer.cross_point_of(node)
        level = rcp_read.decode_level_cell(val)
        if cell is None or level["level"] is None:
            return
        if not level["linked"] or _gathering:
            _note_change(st, push)
            return
        if not levels_on:
            return
        if time.monotonic() - st.get("layout_at", 0.0) < _LAYOUT_QUIET_S:
            # The source has just moved fader, and its level is the new fader's: not a movement.
            st.setdefault("said", {})[("fader", cell[1])] = level["level"]
            st.get("levels", {}).pop(("fader", cell[1]), None)
            return
        _note_level(st, "fader", cell[1], level["level"])
        return
    if name == "channelInputSource":
        st["layout_at"] = time.monotonic()
        _note_change(st, push)
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
        if ival is None or ival == was or is_self_echo("channel_selected"):
            return
        _say("Virtual faders and monitor. Encoder changes their levels" if ival < 0
                 else _selected_phrase(ival))
        return
    if name in ("inputMicrophoneGain", "inputDigitalGain"):
        # The gain the encoder moves while a channel is selected. BOTH kinds matter: a MIC strip
        # moves inputMicrophoneGain, a line strip (USB 1, Chat, USB 2, Bluetooth) moves
        # inputDigitalGain. Miss the second and a working turn on USB 1 announces "not linked" while
        # the system audio audibly changes. Pushed at the channel's INPUTSOURCE node.
        if not levels_on or ival is None:
            return
        st["enc_turn_at"] = None              # the turn DID do something: it moved a gain
        who = change_words.gain_name(node, name == "inputMicrophoneGain")
        if who and st.get("gain_said") != (who, ival):
            # Record it either way, so a suppressed echo is not spoken by a later duplicate push.
            st["gain_said"] = (who, ival)
            if not is_self_echo(("gain", who)):
                # Number first, as for any level that moved (see change_words.level_words).
                _say(f"{ival} d B, {who}")
        return
    if name == "faderLevel":
        # The VIRTUAL fader level, 0-127. This is the encoder's own report of
        # what it just moved, and its address names the FADER directly
        # (PHYSICALINTERFACE.FADER[index]), so it is the cleanest signal for re-seating the
        # ring - the cross-point pushes that follow say the same thing by source.
        idx = node[3] if node and node[:3] == ("PHYSICALINTERFACE", 0, "FADER") else -1
        if ival is not None and 0 <= idx < 16:
            _ring_place(st, "fader", idx)
        return
    if name == "outputMonLevel":
        if levels_on and fval is not None:
            _ring_place(st, "monitor", 0)
            _note_level(st, "monitor", 0, fval)
        return
    if name in change_words.OUTPUT_MUTES:
        if bval is None or bval == was:
            return
        if st.get("enc_down_at"):
            st["enc_hold"] = True
            if name == "outputMonMute":
                _ring_place(st, "monitor", 0)
        if is_self_echo(("output_mute", name)):
            return
        if not (mute_tones and _tone("muted" if bval else "unmuted")):
            _say(f"{change_words.OUTPUT_MUTES[name]} {'muted' if bval else 'unmuted'}")
        return
    # THE HEADPHONE KNOBS. They are physical pots with NO TACTILE INDICATOR, so there is no way to
    # know where one is set by touch, which is exactly the case software has to answer. The desk
    # pushes `potLevel` per step as the knob turns: measured 199 pushes in 12 seconds, one per
    # increment, with the pot identified by the path. They go through the ordinary level machinery,
    # so a sweep settles into ONE closing phrase rather than a torrent, exactly as a fader does. NOT
    # seated on the encoder ring: the ring cycles what the ENCODER selects, and a headphone knob is
    # not on it.
    if name == "potLevel":
        idx = node[3] if node and node[:3] == ("PHYSICALINTERFACE", 0, "POT") else -1
        if levels_on and ival is not None and 0 <= idx < _POT_COUNT:
            _note_level(st, "headphone", idx, max(0.0, min(1.0, ival / _POT_LEVEL_MAX)))
        return
    if name in _STATE_PROPS:
        if bval is None or strip < 0:
            return
        on, what = bval, _STATE_PROPS[name]
        st["pending"] = None            # the state supersedes the press edge
        who = change_words.strip_name(strip)
        # Our own write, echoed back: the UI announced it already (see mark_self_write). Before
        # the press-and-hold inference, because a software mute is not a gesture on the desk. Solo
        # is marked too (mark_self_solo), though this desk pushes nothing back for a software solo,
        # so that branch is insurance rather than an observed fix.
        if is_self_echo((what, strip)):
            return
        # A mixer pad is mid-action, so this mute or cue is the pad doing its job.
        if time.monotonic() < _mixer_quiet_until:
            return
        if what == "mute":
            # A mute landing mid-press is what identifies the gesture as the desk's press-and-hold
            # rather than a selection change.
            if st.get("enc_down_at"):
                st["enc_hold"] = True
                # A hold mutes the ring target only while no channel button holds the encoder;
                # with one, the mute is encoder_hold's, of the selected channel.
                selected = desk_model.selected_channel()
                if selected is None or selected < 0:
                    _ring_place(st, "fader", strip)
            # A TONE IF THEY HAVE ASKED FOR ONE, and the sentence if the tone cannot be played. Mute
            # is the announcement that arrives too late as words: you press it mid-sentence, and by
            # the time "Mic 2 muted" has been said the moment has gone. The cue does not name the
            # channel, which is the trade and is why this is an option rather than the default.
            if not (mute_tones and _tone("muted" if on else "unmuted")):
                _say(f"{who} {'muted' if on else 'unmuted'}")
        else:
            _say(f"{who} solo {'on' if on else 'off'}")
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
        sel_now = desk_model.selected_channel()
        if sel_now is not None and sel_now >= 0:
            _say(_selected_phrase(sel_now))
            return
        # A short press advances the fixed ring (_encoder_ring), so once we know where we are we
        # can say WHAT was just selected and where it sits - before a click moves it. Until the first
        # movement seats us, all we can honestly say is "Select". A release with no press behind it
        # (the announcer started mid-press) counts as a short press: a possibly-wrong name
        # self-corrects, silence just leaves you guessing.
        pos = st.get("enc_pos")
        if pos is None:
            _say("Select")
        else:
            st["enc_pos"] = (pos + 1) % len(_encoder_ring())
            _say(_ring_phrase(st, st["enc_pos"]))
        return
    _note_change(st, push)


# A burst of changes is spoken this long after its last push, as one sentence.
_CHANGES_SETTLE_S = 0.4
# How long after a fader's source changes the levels its cross-points push are the new layout's,
# not a movement.
_LAYOUT_QUIET_S = 1.0
# Changes that are levels, which the levels switch silences.
_LEVEL_TARGETS = frozenset({"level", "send", "outputMonLevel", "potLevel"})


# WHILE RODE'S APP IS OPEN, changes are gathered rather than spoken (`gather_changes`), and said
# together when the window reads the desk on coming back to the front or as RODE's app closes
# (`speak_gathered`). Most of what RODE's app changes is never pushed and is only found by that
# read; what it does push, such as the levels and sends it rewrites while moving a channel to
# another fader, would otherwise be heard on its own, out of context, while the user is in RODE's app.
_gathering = False
_release_until = 0.0
# Long enough for the read's revealed changes to be queued and settle.
_RELEASE_S = 2.0


def gather_changes(on: bool) -> None:
    global _gathering
    _gathering = bool(on)


def speak_gathered() -> None:
    """Say what has been gathered, with whatever the read just made has revealed."""
    global _release_until
    _release_until = time.monotonic() + _RELEASE_S


def _note_change(st: dict, push) -> None:
    """Hold a change made elsewhere to be spoken with the rest of its burst (`flush_pending`). A
    revealed change with no words is counted, and logged by name so the words can follow."""
    if push.name in change_words.NOT_NEWS:
        return
    change = change_words.describe(push.name, push.value, push.kind, push.key, push.was)
    if change is None:
        if push.revealed and push.was is not None and push.was != push.value:
            st["unworded"] = st.get("unworded", 0) + 1
            diagnostics.event("announcer.unworded", push.name, key=push.key)
        else:
            return
    elif change.target[0] in _LEVEL_TARGETS and not levels_on:
        return
    else:
        changes = st.setdefault("changes", {})
        changes.pop(change.target, None)
        changes[change.target] = change
    st["changes_at"] = time.monotonic()


def seed(st: dict) -> None:
    """Make sure the desk model has been read on this connection, and count the levels it holds as
    already spoken.

    The pushed stream only ever reports CHANGES, and the desk pushes a resting value now and then:
    unseeded, the first of them ("Monitor 95 percent") would be announced as though something had
    moved it, on every connect and reconnect. `flush_pending` speaks a level unless it matches
    st["said"], which this fills in.

    Takes hooks.lock for the one dump, when the model needs it, and gives up quietly on any
    failure: a missing read costs a channel NAME, not the announcer."""
    if not desk_model.trusted():
        try:
            with hooks.lock:
                rcp_read.read_model(fresh=True)
        except Exception as exc:
            diagnostics.event("announcer.seed_failed", "the announcer could not read the desk",
                              error=str(exc))
    said = {}
    vec = desk_model.strip_vector() or []
    for fader, source in enumerate(vec):
        level = desk_model.fader_level(fader)
        if level is not None and source is not None and source >= 0:
            said[("fader", source)] = level
    if desk_model.monitor_level() is not None:
        said[("monitor", 0)] = desk_model.monitor_level()
    for i in range(_POT_COUNT):
        if desk_model.pot_level(i) is not None:
            said[("headphone", i)] = desk_model.pot_level(i)
    st["said"] = said


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
    who = change_words.strip_name(strip_idx)
    dev = devices.get_active_device()
    vec = desk_model.strip_vector() or []
    code = vec[strip_idx] if 0 <= strip_idx < len(vec) else None
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


def _at_limit(st: dict, delta: int) -> bool:
    """Would a turn in this direction do nothing because we are already at the end of the range?

    Only ever suppresses the "not linked" message when the level is known to be against a stop."""
    pos = st.get("enc_pos")
    if pos is None or not delta:
        return False
    ring = _encoder_ring()
    lvl = _ring_level(*ring[pos % len(ring)])
    if lvl is None:
        return False
    return (delta < 0 and lvl <= 0.001) or (delta > 0 and lvl >= 0.999)


def _moved_phrase(kind: str, source: int, level: float, short: bool = False) -> str:
    """A level that just MOVED. `source` is a SOURCE CODE, never a fader index.

    Source code is the right space here and a fader index is not, because a channel can be OFF
    FADER entirely - the Pro II always has one - and its level still changes and still deserves a
    name. `_ring_phrase` works in the other space, and the two are easy to confuse because both
    speak of a "fader" kind; check which space you are in before calling either."""
    name = ("Monitor" if kind == "monitor"
            else f"Headphone {source + 1}" if kind == "headphone"
            else change_words.source_name(source))
    return change_words.level_words(name, level, name_first=False, say_percent=not short)


def _fader_label(fader_idx: int) -> str:
    """Name the channel sitting on a FADER (by index), from the desk model's layout - so the encoder
    ring announces "Chat" rather than "fader 8", and follows the layout if a source is moved."""
    vec = desk_model.strip_vector()
    if vec and 0 <= fader_idx < len(vec) and vec[fader_idx] is not None and vec[fader_idx] >= 0:
        return change_words.source_name(vec[fader_idx])
    return f"Fader {fader_idx + 1}"


def _ring_level(kind: str, idx: int):
    """Where a ring entry sits, 0 to 1, from the desk model, or None when it is not known."""
    return desk_model.monitor_level() if kind == "monitor" else desk_model.fader_level(idx)


def _ring_phrase(st: dict, pos: int) -> str:
    """What to say the moment the encoder is pressed: the thing now selected and, when we know it,
    where it currently sits. The level comes from the desk model, so it costs no device read and no
    turning.

    THIS SIDE WORKS IN FADER INDICES, because that is what the encoder cycles through. `_moved_phrase`
    works in SOURCE CODES. Keep them straight."""
    ring = _encoder_ring()
    kind, idx = ring[pos % len(ring)]
    name = "Monitor level" if kind == "monitor" else _fader_label(idx)
    # A muted channel's level is not the useful fact about it, and quoting a healthy-sounding
    # percentage for something you cannot hear is actively misleading. Say the mute instead. Fader
    # index and strip index are the same space here: both index the channelInputSource vector.
    if kind == "fader" and desk_model.mutes().get(idx):
        return f"{name}, muted"
    lvl = _ring_level(kind, idx)
    if lvl is None:
        return name
    return change_words.level_words(name, lvl, name_first=True)


def _ring_place(st: dict, kind: str, idx: int) -> None:
    """Set our ring position to (kind, idx) when it is on the ring. A real movement, or an encoder
    hold's mute, says where the encoder is, which is what makes a wrong prediction self-correcting."""
    for i, entry in enumerate(_encoder_ring()):
        if entry == (kind, idx):
            st["enc_pos"] = i
            return


def _note_level(st: dict, kind: str, source: int, level: float) -> None:
    """Hold a level change to be spoken, and speak it AT ONCE when it is the first one after an
    encoder press.

    `source` is a SOURCE CODE (0 for the monitor, the knob's index for a headphone): everything
    spoken is keyed by source, because a source can be off-fader and still have a level.

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
        _say(_moved_phrase(kind, source, level, short=short))


def flush_pending(st: dict) -> None:
    """Speak anything being held: a press edge with no STATE behind it (_PENDING_S), and the
    settled position of a fader or the monitor encoder (_SETTLE_S)."""
    if st.get("pending") and (time.monotonic() - st.get("at", 0.0)) > _PENDING_S:
        _say(st["pending"])
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
            sel = desk_model.selected_channel()
            if sel is not None and sel >= 0:
                _say(_selected_phrase(sel)
                         + ". Press its channel button again for the fader and monitor levels")

    changes = st.get("changes")
    if ((changes or st.get("unworded"))
            and (time.monotonic() - st.get("changes_at", 0.0)) > _CHANGES_SETTLE_S
            and (not _gathering or time.monotonic() < _release_until)):
        text = change_words.summarise(list(changes.values()) if changes else [],
                                      st.get("unworded", 0))
        if changes:
            changes.clear()
        st["unworded"] = 0
        if text:
            _say(text)

    levels = st.get("levels")
    if levels and (time.monotonic() - st.get("levels_at", 0.0)) > _SETTLE_S:
        said = st.setdefault("said", {})
        for key, lvl in list(levels.items()):
            if _pct(said.get(key)) == _pct(lvl):
                continue                       # this position was already spoken while moving
            said[key] = lvl
            st.setdefault("said_at", {})[key] = time.monotonic()
            st["run_key"] = None            # the gesture is over; name it again next time
            _say(_moved_phrase(key[0], key[1], lvl))
        levels.clear()


# The record of what WE have just written, so the desk's echo of it is not read back at us.
#
# OUR OWN WRITES *ARE* ECHOED ON THE PUSH STREAM, most of them. Hardware proof, against the running
# app with no hand on the desk: three level writes come straight back and are announced - "60
# percent, USB 2 (Secondary)", "80 percent, Monitor".
#
# Without the suppression below, every change is said TWICE: the screen reader reads the window's
# reply and the announcer speaks the echo. The fix is narrow on purpose: suppress the SPEECH for the
# exact target just written, for a moment. A change made ON the desk is unaffected and still speaks
# at once, which is the whole point of the announcer and why "turn it off while the app has focus"
# is the wrong shape. Dragging a slider stays quiet all the way down, and a hand on that same fader
# is audible again _SELF_ECHO_S after the last write.
#
# NOT covered, deliberately: a profile RESTORE, which writes the whole desk at once. Narrating that
# is a different problem (it wants one "restoring" phrase, not 40 suppressions) and it has its own
# progress reporting already.
#
# `live_state` is the running loop's speech state, and None while announcements are off.
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
    has an ADDRESS - so we resolve ours to that same address and let change_words.gain_name do the naming,
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
        who = change_words.gain_name(dev.input_key(entry), is_mic)
        if who:
            mark_self_write(("gain", who))
    except Exception:
        pass


def mark_self_level(kind: str, idx: int) -> None:
    """Mark a level WE just wrote, `kind` "fader" with a fader index or "monitor", so the desk's
    echo is not spoken over the window's own report of it. The echo is a cross-point keyed by
    SOURCE CODE, so a fader is marked under the source the desk model has on it."""
    if kind == "monitor":
        mark_self_write(("monitor", 0))
        return
    vec = desk_model.strip_vector() or []
    if 0 <= int(idx) < len(vec) and vec[int(idx)] is not None:
        mark_self_write(("fader", vec[int(idx)]))


def mark_self_mute(strip_idx: int) -> None:
    """Mark a mute WE just wrote, keyed by STRIP index exactly as the desk's own pushes are, so the
    echo is not spoken over the window's own report of it."""
    mark_self_write(("mute", int(strip_idx)))


def announce_mute(strip_idx: int, on: bool) -> None:
    """Speak a mute this app made in answer to a gesture on the desk (`encoder_hold`), as the desk's
    own mute is spoken, and mark it as `mark_self_mute` does. Silent while the announcer is off."""
    mark_self_mute(strip_idx)
    if live_state is None:
        return
    if not (mute_tones and _tone("muted" if on else "unmuted")):
        _say(f"{change_words.strip_name(strip_idx)} {'muted' if on else 'unmuted'}")


def mark_self_solo(strip_idx: int) -> None:
    """Mark a solo WE wrote, so a future firmware that echoes it does not get it spoken twice.

    Measured: this desk pushes NOTHING back for a software solo, so the mark is a no-op on it. It is
    here anyway because the announcer's solo branch speaks every channelCueEnable push it sees, so a
    firmware that starts echoing would have a channel announcing its own solo on top of the UI."""
    mark_self_write(("solo", int(strip_idx)))


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
        # (`usb_audio._tidy_after_standard`), so this says only what is certain.
        return "USB input is now Expanded." if now_exp else "The desk has left Expanded USB input."
    # A value outside the six measured ones is still worth SAYING, just not naming: going silent on
    # an unknown would hide exactly the case nobody has seen before.
    label = recorder.USB_MULTITRACK_LABEL.get(now_mode)
    return (f"USB output to this computer is now {label.lower()}." if label else
            "The desk's USB audio mode has changed.")


# The first thing said once the desk is open: the app's own start-up names the app, a person
# switching announcements back on hears that they are on.
GREETING_ON = "Announcements on."
GREETING_STARTED = "AccessCaster started."


def _loop(gen: int | None = None, greeting: str = GREETING_ON) -> None:
    global live_state
    if gen is None:
        gen = _loop_gen

    def current() -> bool:
        return speech.enabled.is_set() and gen == _loop_gen

    if not current():
        return                        # superseded before it started; the newer loop greets
    if rcp_transfer.find_control_interface() is None:
        speech.say("The control announcer could not open the device.")
        speech.enabled.clear()
        return
    st = {"pending": None, "at": 0.0, "levels": {}, "levels_at": 0.0, "enc_pos": None,
          "enc_down_at": None, "enc_hold": False, "said": {}}
    announced_on = False
    seed(st)
    live_state = st
    # Every push: anything changed elsewhere is worded (`_note_change`).
    sub = desk_live.subscribe("announcer")
    try:
        while current():
            event = sub.get(_TICK_S)
            if not current():
                break
            try:
                if isinstance(event, desk_live.Push):
                    handle_push(event, st)
                elif isinstance(event, desk_live.Lost):
                    # THE USB AUDIO MODE BEFORE THE DESK LEFT, so the return can tell that it MOVED.
                    # The desk can drop out of Expanded on its own (measured: off the bus
                    # unprompted, back two seconds later in Standard with the setting reset), and
                    # the change is invisible: the push is sent into the gap. The desk model keeps
                    # the old mode until the desk is read again on its return.
                    st["usb_mode"] = desk_model.usb_mode()
                elif isinstance(event, desk_live.Connected):
                    if not announced_on:
                        # Quiet when the RECORDER is putting announcements back: the stop phrase
                        # already says they are back on, and the pump keeps only the newest phrase,
                        # so a greeting here would silence it.
                        if greeting and not _quiet_start.is_set():
                            speech.say(greeting)
                        _quiet_start.clear()
                        announced_on = True
                    elif event.gap is not None and event.gap >= _RECONNECT_SPEAK_AFTER_S:
                        # Back after a real gap. Read the desk first: mutes, levels and the fader
                        # layout can all have moved while we were deaf, and Expanded mode
                        # reassigns sources as it goes.
                        seed(st)
                        hooks.reconnected(st.get("usb_mode"), desk_model.usb_mode())
                flush_pending(st)
            except Exception as exc:
                # Decoding is best-effort: a malformed or unexpected push costs us ONE
                # announcement, never the announcer. Carrying on beats a thread that dies and
                # leaves the UI claiming announcements are on.
                diagnostics.event("announcer.push_undecoded", str(exc))
    finally:
        sub.close()
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
# and returns without a greeting of its own (`recording._state_changed` says why that
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


def action_set_announce(on: bool, chosen: bool = True, greeting: str = GREETING_ON) -> dict:
    """Switch announcements on or off. `chosen` is a person's decision (the page, the tray, the
    shortcut), which is saved when they have asked for it to be remembered; the app switching them
    back on at start-up or after a take is not. `greeting` is what is said once the desk is open,
    if anything."""
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
                _thread = threading.Thread(target=_loop, args=(_loop_gen, greeting),
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
        # PLAYED WITH A GAP. The speech thread keeps only the newest item waiting, so two cues
        # queued back to back play only the second. The gap also keeps them apart as two cues.
        speech.tone("muted")
        time.sleep(tones.DURATION_S + 0.4)
        speech.tone("unmuted")
    return {"ok": True, "mute_tones": mute_tones,
            "message": ("Mutes will play a tone instead of being spoken: two notes falling means "
                        "muted, two notes rising means unmuted."
                        if on else "Mutes will be spoken, naming the channel.")}


def action_set_announce_record_feedback(mode: str) -> dict:
    """What recording starting, pausing and stopping sounds like: `RECORD_FEEDBACK`.

    A tone is short enough to be over before the take is under way, and it plays for every press,
    a shortcut's and the unit's own button alike. Choosing tones plays the three cues in turn."""
    global record_feedback
    if mode not in RECORD_FEEDBACK:
        return {"ok": False, "message": f"{mode!r} is not one of the choices."}
    record_feedback = mode
    settings_file.update(announcer_record_feedback=mode)
    if mode == "tone":
        # One at a time, with a gap: the speech thread keeps only the newest item waiting.
        for name in ("record_start", "record_pause", "record_stop"):
            speech.tone(name)
            time.sleep(tones.record_cue_seconds(name) + 0.4)
    return {"ok": True, "record_feedback": mode, "message": {
        "speak": "Recording starting, pausing and stopping will be spoken.",
        "tone": ("Recording will play a tone instead of being spoken: one long beep for "
                 "recording, two for paused, three for stopped."),
        "silent": ("Recording starting, pausing and stopping will make no sound. A card or drive "
                   "going in or out is still spoken."),
    }[mode]}


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


def apply_settings(changes: dict) -> None:
    """Push restored announcer preferences into the running announcer, not just the file.

    `announcer` (the remembered on or off) is left to the next launch: switching speech on or off
    in the middle of an import would be its own surprise."""
    global remember, levels_on, mute_tones, record_feedback
    if "announcer_remember" in changes:
        remember = bool(changes["announcer_remember"])
    if "announcer_levels" in changes:
        levels_on = bool(changes["announcer_levels"])
    if "announcer_mute_tones" in changes:
        mute_tones = bool(changes["announcer_mute_tones"])
    if "announcer_record_feedback" in changes or "announcer_record_tones" in changes:
        record_feedback = _saved_record_feedback(changes)
