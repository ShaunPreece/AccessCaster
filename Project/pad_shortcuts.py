"""The pad shortcuts: firing a pad from a global hotkey, stepping the pad bank, and saying what is
playing.

WHAT IT KEEPS, and why it keeps it: a pad press has to be instant, and knowing where a pad sits and
what its play mode is come from a model dump of about 1.5 seconds. So this module caches the pad map,
keeps it honest from every pad edit (`pads_stale`) and an age limit, follows what is playing from
the desk's pushes (`follow_desk`), and remembers what it switched on or is holding down, because the
desk does not echo our own writes. The bank the desk is on is the desk model's (`desk_model.bank`).

The device lock is `desk_access.hid_lock`, as everywhere else in the app.
"""
from __future__ import annotations

import contextlib
import threading
import time

import desk_access
import desk_live
import desk_model
import devices
import diagnostics
import midi_out
import rcp_read
import rcp_transfer
import soundpad
import speech
import wording


def _said(message: str, ok: bool = True) -> dict:
    """A shortcut's result, in the shape every action returns. One whose `ok` is False is spoken
    even when its shortcut is set to be silent (hotkeys.HotkeyManager._run)."""
    return {"ok": ok, "message": message}


# WHERE EACH PAD SITS IN THE POOL, cached, because firing a pad has to be INSTANT.
#
# A pad is engaged by writing `padActive` at its STREAM POSITION, and position is only knowable from
# a model dump - about 1.5 seconds, which is not a cough-key or an airhorn. So the map is cached, and
# kept honest three ways: it is built on first use, MARKED STALE by every pad edit
# (pads_stale) and refreshed in the background once older than _PAD_CACHE_S, and after an
# HID toggle a background pass CHECKS that the pad now engaged is the one asked for. If the
# positions have shifted, the check switches the wrong pad off again and says so, rather than
# leaving a stranger's voice effect on a live mic. `padIdx` is the stable identity (a
# pad's place in the grid), and it is what a shortcut stores; position is the moving part.
_pad_map = {}                # padIdx -> (position, name, pad type, play mode)
_pad_lock = threading.Lock()
_pads_on = set()             # padIdx of pads THIS app engaged, so a second press releases them
_pads_held = set()           # padIdx of HOLD-mode sound pads whose MIDI note we are still holding


def follow_desk() -> None:
    """Keep what is playing following the desk, whether or not announcements are on."""
    desk_live.Follower("pad shortcuts", {"padProgress", "padActive"}, on_push=note_push).start()


def note_push(push) -> None:
    """One push `follow_desk` asked for. Pad playback is addressed at the pad's STREAM POSITION,
    the last path element."""
    if push.name == "padProgress" and push.kind == "float" and push.path:
        note_pad_progress(push.path[-1], push.value)
    elif push.name == "padActive" and push.kind == "bool" and push.path:
        note_pad_active(push.path[-1], push.value)


def current_bank():
    """The bank the desk is on (0-based), or None if we cannot find out. MIDI reaches only the
    current bank, so firing a pad in another one needs this first. None is never to be read as
    bank 1: that fires the wrong pad. Reads the pads when the desk model has no bank, which the
    model then holds."""
    bank = desk_model.bank()
    if bank is None:
        try:
            refresh_pads()
        except Exception:
            return None
        bank = desk_model.bank()
    banks = devices.get_active_device().pad.system_bank
    return bank if bank is not None and 0 <= bank < banks else None


# VOICE-FX PADS WITH NOTHING BEHIND THEM, by padIdx. A voice-FX pad's sound lives in a region-B
# effect object, and the grid record does NOT: a pad can carry the right name, type and colour with
# no object bound to it, which is what a factory reset leaves behind (soundpad.heal_fx_pool). Such a
# pad takes `padActive` and processes nothing, so a shortcut that writes the flag and reports
# success is telling the operator their voice is being disguised when it is not.
#
# READ OFF THE REFRESH DUMP, never asked for separately: `pool_health` costs a full model read on
# its own, and a press cannot pay 1.5 s to find out whether it is allowed.
_fx_missing = frozenset()

# STREAM POSITIONS OF THE MIXER PADS (padType 3), read off the same refresh dump. A mixer pad's whole
# job is to move channel state - Back Channel mutes and cues its members, Censor mutes the mics - so
# its `padActive` is the announcer's signal that the mute and cue pushes about to arrive are the
# pad doing what it was pressed to do, not a change worth speaking. See announcer._MIXER_QUIET_S.
mixer_positions = frozenset()


def refresh_pads() -> dict:
    """Re-read the pad pool and replace the cache. Returns the new map.

    One dump answers three questions - where each pad sits, what its play mode is, and which
    voice-FX pads have lost their effect object - so the press that follows needs no read of its
    own. The desk model adopts the same dump, which is where the bank comes from."""
    global _pad_map, _pads_at, _fx_missing, mixer_positions
    with desk_access.hid_lock:
        blob = rcp_read.read_model_stable()
    modes = soundpad.pad_play_modes(blob)
    placed = list(soundpad.pad_positions(blob))
    fresh = {pi: (pos, nm, pt, modes.get(pi)) for pos, pi, pt, nm in placed}
    mixer_positions = frozenset(pos for pos, _pi, pt, _nm in placed if pt == 3)
    try:
        _fx_missing = frozenset(soundpad.pool_health(blob).get("missing") or ())
    except Exception:
        _fx_missing = frozenset()    # a health read that fails must never break a press
    with _pad_lock:
        _pad_map = fresh
    _pads_at = time.monotonic()
    return fresh


# What to say about a voice-FX pad whose effect object is gone. One wording, because the shortcut
# that refuses and the read-out that describes it must not disagree about what is wrong or what to
# do about it.
_FX_EMPTY = ("{name} has no effect behind it, so it would change nothing. Use Restore the "
             "factory voice effects on the SMART pads page to rebuild it.")


def _pad_entry(pad_idx: int):
    """The cached entry for a pad, reading the desk again only when we must.

    TWO KINDS OF OUT OF DATE, and they deserve different answers. A pad EDIT makes the map wrong
    NOW - a cleared pad shifts every later position - so `_pads_dirty` forces a read before this
    press, and that cost is worth paying once. Merely OLD is different: nothing is known to have
    changed, so the read happens in the background and this press does not wait for it."""
    if _pads_dirty.is_set():
        _pads_dirty.clear()
        try:
            return refresh_pads().get(pad_idx)
        except Exception:
            pass                # fall back to what we have; a stale name beats a dead shortcut
    with _pad_lock:
        found = _pad_map.get(pad_idx)
    if found is None:
        found = refresh_pads().get(pad_idx)
    return found


# HOW STALE THE PAD MAP MAY GET BEFORE A PRESS PAYS FOR A NEW ONE.
#
# NEVER REFRESH AFTER EVERY PRESS: that is what makes pad firing flaky. A refresh is a FULL MODEL
# DUMP, one and a half to three seconds holding the device lock, so a row of number keys pressed at
# performance speed queues a dump per press - and `HotkeyManager._fire` DROPS a re-press while the
# last one is still running, so presses go missing rather than merely arriving late.
#
# It is also unnecessary on the MIDI path. A dump is only needed to know where a pad SITS, and MIDI
# addresses a pad by bank and slot, which does not move. What the map still supplies - the name and
# the play mode - changes only when somebody edits a pad, so a TTL is the honest shape. The HID
# toggle path keeps its own verification, which is a different job: it catches a stale POSITION
# firing a stranger's voice effect onto a live mic.
_PAD_CACHE_S = 90
_pads_at = 0.0
_refreshing = threading.Event()
_pads_dirty = threading.Event()


def pads_stale() -> None:
    """Mark the pad map KNOWN WRONG, so the next press reads it again before doing anything. Called
    by anything that edits a pad: the TTL alone would leave a renamed or cleared pad wrong for up to
    a minute and a half, and a cleared one moves every later pad's stream position."""
    global _pads_at
    _pads_at = 0.0
    _pads_dirty.set()


def _maybe_refresh_pads() -> None:
    """Read the pad pool again in the background IF it is stale, and never twice at once."""
    if time.monotonic() - _pads_at < _PAD_CACHE_S or _refreshing.is_set():
        return

    def run():
        try:
            refresh_pads()
        except Exception:
            pass                # a refresh that cannot run must not break the next press
        finally:
            _refreshing.clear()

    _refreshing.set()
    threading.Thread(target=run, daemon=True).start()


def _pad_name(position: int):
    """The name of the pad at a stream POSITION, or None. The pushed stream addresses a pad by
    position, so this is what turns a `padActive` / `padProgress` push back into a name."""
    with _pad_lock:
        for _pi, entry in _pad_map.items():
            if entry[0] == position:
                return entry[1]
    return None


# WHETHER WE HAVE TURNED MIDI ON YET THIS SESSION. `systemMidiControl` is the gate on the whole
# MIDI port, it is NOT on by default, and a profile restore can switch it off again - so anything
# that fires a sound pad has to arm it at the point of use rather than once at startup.
#
# UNCONDITIONALLY, AND NEVER READ-THEN-WRITE. Reading first costs a model dump (about 1.5 s) in
# front of a keypress, and a remembered value can be stale in the one direction that matters. The
# write is idempotent and measured at 0.004 s, so there is nothing to save. Said out loud ONCE, because the
# user did not ask for it and would otherwise have a setting changed silently.
_midi_armed = False


def rearm_midi() -> None:
    """Forget that MIDI was armed, so the next pad press turns it on again. Called when a profile is
    applied: a profile carries `systemMidiControl`, so restoring one taken while MIDI was off
    silently kills every sound-pad shortcut."""
    global _midi_armed
    _midi_armed = False


# HOW LONG A PAD SHORTCUT WAITS FOR THE HID LOCK. A pad press writes to the desk, and a write sent
# while a profile restore has the desk in transfer mode lands inside the transfer. Ordinary holders
# are model reads of 1.5 to 3 s; a restore or a sound upload holds it for far longer, and a press
# that waited for one of those would fire long after it was meant to.
_DESK_WAIT_S = 3.5


class _DeskBusy(Exception):
    """A pad shortcut could not have the HID lock within _DESK_WAIT_S. The message is spoken."""


@contextlib.contextmanager
def _desk():
    if not desk_access.hid_lock.acquire(timeout=_DESK_WAIT_S):
        raise _DeskBusy("The desk is busy with another job, such as a restore, so nothing was "
                        "pressed. Try again when it has finished.")
    try:
        yield
    finally:
        desk_access.hid_lock.release()


def _arm_midi() -> None:
    global _midi_armed
    if _midi_armed:
        return
    with _desk():
        rcp_transfer.set_midi_control(True)
    _midi_armed = True
    speech.say("MIDI control switched on, so the desk will play sound pads.")


# WHAT IS PLAYING, tracked SILENTLY off the pushed stream and read out only when asked.
#
# ON DEMAND, NOT ON PRESS, which is what keeps it cheap as well as quiet: a playing pad pushes
# `padProgress` about twenty times a second, and speaking that would mean throttling twenty
# announcements a second and talking over the pad. As STATE that nobody speaks, the rate costs a
# dict update.
#
# ADDRESSED BY STREAM POSITION, path [666, position] - the same address our own `padActive` write
# uses, not 8 + padIdx. On a factory Pro II every user pad sits at 8 + padIdx, so the two agree
# until a pad's record moves in the stream.
#
# ONLY SOUND PADS PUBLISH PROGRESS. A voice-FX pad pushes `padActive` and no progress at all
# (measured), so "how far through" is a sound-pad answer and nothing else.
_play_lock = threading.Lock()
_playing = {}                # stream position -> {"first", "first_at", "last", "last_at"}
# A playing pad pushes about 20 times a second, so a second of silence means it has stopped without
# us seeing the `padActive` that said so - which happens whenever the app starts mid-sound.
_PLAY_STALE_S = 1.0


def note_pad_progress(position: int, progress: float) -> None:
    """One `padProgress` push. Keeps the FIRST sample as well as the latest, because time remaining
    is worked out from the RATE between two samples: progress is a 0-1 fraction and the desk
    publishes no duration anywhere, so a single sample says how far through but never how long."""
    now = time.monotonic()
    with _play_lock:
        seen = _playing.get(position)
        if seen is None or progress < seen["last"] or now - seen["last_at"] > _PLAY_STALE_S:
            # A new sound, or the same pad restarted from the beginning (One Shot), or one we lost
            # track of. Either way the old rate is meaningless, so start again from here.
            _playing[position] = {"first": progress, "first_at": now,
                                  "last": progress, "last_at": now}
            return
        seen["last"], seen["last_at"] = progress, now


def note_pad_active(position: int, on: bool) -> None:
    """One `padActive` push. Off means the sound has finished or been stopped."""
    with _play_lock:
        if on:
            _playing.setdefault(position, {"first": 0.0, "first_at": time.monotonic(),
                                           "last": 0.0, "last_at": time.monotonic()})
        else:
            _playing.pop(position, None)


def play_snapshot() -> dict:
    """position -> (progress, seconds remaining or None), for pads pushing progress RIGHT NOW."""
    now, out = time.monotonic(), {}
    with _play_lock:
        for position, seen in list(_playing.items()):
            if now - seen["last_at"] > _PLAY_STALE_S:
                _playing.pop(position, None)
                continue
            span, moved = seen["last_at"] - seen["first_at"], seen["last"] - seen["first"]
            left = None
            if span > 0.2 and moved > 0.0:
                left = max(0.0, (1.0 - seen["last"]) * span / moved)
            out[position] = (seen["last"], left)
    return out


def _pad_verify(pad_idx: int, position: int, want_on: bool) -> None:
    """After a press, re-read and confirm the RIGHT pad reacted. Runs on its own thread.

    A press that missed engaged whichever pad the fresh map puts at the stream `position` written,
    so that pad is the one switched off; any other pad the desk has engaged is left alone."""
    try:
        fresh = refresh_pads()
        with desk_access.hid_lock:
            latched = {pi for _pos, pi, _nm in soundpad.latched_pads()}
        if not want_on or pad_idx in latched:
            return
        wrong = next((pi for pi, entry in fresh.items() if entry[0] == position), None)
        if wrong in latched:
            with desk_access.hid_lock:
                soundpad.unlatch_pad(position)
            _pads_on.discard(pad_idx)
            speech.say(f"That fired the wrong pad, {fresh[wrong][1]}, and it has been switched off "
                       f"again. The pad list was out of date and has been read again.")
    except Exception as exc:    # a verification that cannot run must not break the press
        diagnostics.event("hotkeys.pad_verify_failed", str(exc))


# TWO TRANSPORTS, AND THEY COVER DIFFERENT PAD TYPES. Dispatch is PER PAD and never per bank,
# because the kinds are mixed inside a bank.
#
#   SOUND pad  -> MIDI, CC 35 on the pad's own channel. It is a PLAY, and what a second press does
#                 is the pad's own `padPlayMode`. Over HID a sound pad sets `padActive` and plays
#                 nothing, so do not route sound pads down the HID path.
#   VOICE-FX
#   and MIXER  -> HID, `padActive` at the pad's stream position. It is a TOGGLE and we hold the
#                 state in `_pads_on`, because for those two the flag IS the state and the desk
#                 pushes nothing back for our own write.
#
# So one key says "Megaphone on" for one pad and "Airhorn playing" for the next. Do not collapse
# them into one word: "on" for a sound that has already finished would be a lie, and "playing" for a
# voice effect that is sitting on a live mic hides the thing you most need to know.
def fire_pad(pad_idx: int) -> dict:
    """Fire ONE pad by its padIdx, choosing the transport from its type. The spoken result says
    which of the two things happened."""
    entry = _pad_entry(pad_idx)
    if entry is None:
        return _said("That pad is empty now. Choose another on the Keyboard shortcuts tab.",
                     ok=False)
    position, name, pad_type, play_mode = entry
    if pad_type == soundpad.PAD_TYPE_SOUND:
        return _pad_play(pad_idx, position, name, play_mode)
    return _pad_toggle(pad_idx, position, name)


def _pad_play(pad_idx: int, position: int, name: str, play_mode) -> dict:
    """Play a SOUND pad over MIDI, selecting its bank first if the desk is on another one.

    A STALE PAD MAP CANNOT FIRE THE WRONG SOUND HERE, which is why there is no verification pass
    like the HID toggle has. MIDI addresses a pad by bank and slot - that is padIdx, the pad's fixed
    place in the grid - so it does not care where the record sits in the stream. Position is only
    used to name what is playing."""
    dev = devices.get_active_device()
    per_bank = dev.pad.pads_per_bank
    bank, slot = pad_idx // per_bank, pad_idx % per_bank + 1
    try:
        _arm_midi()
        if current_bank() != bank:
            _release_held_pads()
            with _desk():
                soundpad.select_bank(bank)
            time.sleep(_BANK_SETTLE_S)
        # HOLD MODE PLAYS ONLY WHILE THE PAD IS HELD, and a hotkey has no release event to hang a
        # release on: Windows delivers WM_HOTKEY on the press and says nothing when the key comes
        # up. A tap would therefore give a blip. So the two presses of the shortcut become the two
        # ends of one hold, which is the only shape that makes a Hold pad usable from a keyboard.
        if play_mode == soundpad.PLAY_MODE_HOLD:
            if pad_idx in _pads_held:
                midi_out.release_pad(slot)
                _pads_held.discard(pad_idx)
                return _said(f"{name} released.")
            midi_out.press_pad(slot)
            _pads_held.add(pad_idx)
            return _said(f"{name} playing. It is set to Hold, so press again to stop it.")
        was_playing = position in play_snapshot()
        midi_out.play_pad(slot)
    except _DeskBusy as exc:
        return _said(str(exc), ok=False)
    except Exception as exc:
        _pads_held.discard(pad_idx)
        return _said(f"{name} would not play: {exc}", ok=False)
    _maybe_refresh_pads()
    if was_playing and play_mode == soundpad.PLAY_MODE_TOGGLE:
        return _said(f"{name} stopped.")
    if was_playing and play_mode == soundpad.PLAY_MODE_ONE_SHOT:
        return _said(f"{name} playing from the start.")
    return _said(f"{name} playing.")


def _pad_toggle(pad_idx: int, position: int, name: str) -> dict:
    """Switch a VOICE-FX or MIXER pad on, and off again on the next press.

    Writing `padActive` at a pad's position ENGAGES it, and that is the same mechanism a latched
    voice-FX pad uses, the one that can put a robot voice on a live mic. A
    voice-FX pad engages and releases, and a MIXER pad performs its whole action (Back Channel moves
    `channelCueEnable`, `channelTalkbackEnable`, `mixMute` and `channelOutputMute` together).

    INJECTING THE BUTTON DOES NOT WORK, and that was the obvious route: `padButtonPressed` at the
    PADBUTTON node reads back exactly as written and moves NOTHING else in the whole 6000-property
    model, on either address encoding, for a sound pad or a mixer pad. See the padActive entry in
    PROJECT_GUIDE.md.

    A VOICE-FX PAD WITH NO EFFECT OBJECT IS REFUSED RATHER THAN SWITCHED ON. `padActive` latches on
    one of those perfectly happily and processes nothing, so writing it would leave the desk holding
    a flag the operator has been told is doing something. Only the ON half is guarded: releasing a
    pad that is somehow already latched must always be possible."""
    on = pad_idx not in _pads_on
    if on and pad_idx in _fx_missing:
        return _said(_FX_EMPTY.format(name=name), ok=False)
    try:
        with _desk():
            if on:
                soundpad.engage_pad(position)
            else:
                soundpad.unlatch_pad(position)
        if on:
            _pads_on.add(pad_idx)
        else:
            _pads_on.discard(pad_idx)
    except _DeskBusy as exc:
        return _said(str(exc), ok=False)     # nothing was written, so the pad is as it was
    except Exception as exc:
        _pads_on.discard(pad_idx)
        return _said(f"{name} would not respond: {exc}", ok=False)
    threading.Thread(target=_pad_verify, args=(pad_idx, position, on), daemon=True).start()
    return _said(f"{name} on." if on else f"{name} off.")


def _release_held_pads() -> None:
    """Let go of any HOLD-mode pad we are still holding down.

    THE SAME CLASS OF FAULT AS A LATCHED VOICE-FX PAD, which is why it is called from the shutdown
    path AND from every bank change: a held note nobody releases leaves the desk playing with
    nothing on screen to explain it, and a MIDI channel means "pad N of the current bank", so
    moving the bank strands the hold on a pad we can no longer name. Never raises."""
    dev = devices.get_active_device()
    for pad_idx in list(_pads_held):
        try:
            midi_out.release_pad(pad_idx % dev.pad.pads_per_bank + 1)
        except Exception:
            pass
    _pads_held.clear()


def release_held() -> None:
    """The shutdown half: release the notes, then drop the MIDI handle."""
    _release_held_pads()
    try:
        midi_out.close()
    except Exception:
        pass


def fire(target: str) -> dict:
    """Fire the pad this shortcut names. The target is a padIdx: the pad's FIXED place in the grid,
    so the shortcut keeps pointing at the same sound however the pool is rearranged."""
    target = (target or "").strip()
    if not target:
        return _said("No pad is assigned to this shortcut yet. Choose one on the Keyboard "
                     "shortcuts tab.", ok=False)
    try:
        pad_idx = int(target)
    except ValueError:
        return _said(f"{target} is not a pad.", ok=False)
    return fire_pad(pad_idx)


# How long to let a bank change land before sending the MIDI press. The desk publishes no
# acknowledgement, and a press that arrives first plays the pad in the OLD bank - which is a
# stranger's sound, not a missing one, so this is a correctness dwell rather than a politeness one.
_BANK_SETTLE_S = 0.25
_BANK_UNKNOWN = "I cannot tell which pad bank the desk is on. Is it plugged in?"


def slot(target: str) -> dict:
    """Fire pad N of whatever bank the desk is on. The number-row half of the pad scheme.

    NOT the same command as `pad.fire`, and the difference is the whole point: `pad.fire` is pinned
    to one sound for ever, while this one moves with the desk, so eight keys reach every pad on it.
    N IS PER-DEVICE - eight on a Pro II, six on a Duo - and it comes from the device table, so a Duo
    is never offered a seventh."""
    try:
        slot = int((target or "").strip())
    except ValueError:
        return _said(f"{target} is not a pad number.", ok=False)
    per_bank = devices.get_active_device().pad.pads_per_bank
    if not 1 <= slot <= per_bank:
        return _said(f"This desk has {per_bank} pads in a bank, so there is no pad {slot}.",
                     ok=False)
    bank = current_bank()
    if bank is None:
        return _said(_BANK_UNKNOWN, ok=False)
    return fire_pad(bank * per_bank + slot - 1)


def _filled_banks() -> list:
    """The user banks with a pad on them, 0-based, from the pad map: read again first when a pad
    edit has made it wrong, or when there is none yet. The system bank is never among them."""
    if _pads_dirty.is_set():
        _pads_dirty.clear()
        refresh_pads()
    with _pad_lock:
        entries = dict(_pad_map)
    if not entries:
        entries = refresh_pads()
    spec = devices.get_active_device().pad
    return sorted({pad_idx // spec.pads_per_bank for pad_idx, (_pos, _name, pad_type, _mode)
                   in entries.items()
                   if pad_idx is not None and pad_idx < spec.max_user_pads
                   and pad_type != soundpad.SYSTEM_PAD_TYPE})


def bank_step(target: str) -> dict:
    """Move to the next or previous bank with a pad on it, and say where you have landed.

    EMPTY BANKS ARE SKIPPED, and so is the system bank: from a shortcut a pad can only be played,
    and an empty bank has nothing to play. The desk's own bank buttons add one empty spare, which is
    there for putting a pad on.

    STOPS AT THE ENDS RATHER THAN WRAPPING. Wrapping from the last bank to the first is silent and
    disorienting when you cannot see the desk: you press once more expecting nothing and every
    number key is suddenly a different sound."""
    step = -1 if str(target).strip() == "down" else 1
    bank = current_bank()
    if bank is None:
        return _said(_BANK_UNKNOWN, ok=False)
    try:
        filled = _filled_banks()
    except Exception as exc:
        return _said(f"The pads could not be read: {exc}", ok=False)
    if not filled:
        return _said("There are no pads on any bank yet.", ok=False)
    ahead = [b for b in filled if (b > bank if step > 0 else b < bank)]
    if not ahead:
        where = "last" if step > 0 else "first"
        if bank in filled:
            return _said(f"Bank {bank + 1} already, and that is the {where} bank with pads on it.")
        return _said(f"No bank {'after' if step > 0 else 'before'} this one has pads on it.")
    want = ahead[0] if step > 0 else ahead[-1]
    _release_held_pads()
    try:
        with _desk():
            soundpad.select_bank(want)
    except _DeskBusy as exc:
        return _said(str(exc), ok=False)
    except Exception as exc:
        return _said(f"The bank would not change: {exc}", ok=False)
    _maybe_refresh_pads()
    return _said(f"Bank {want + 1}.")


def playing_now(_target: str = "") -> dict:
    """Say what is playing, on demand.

    ON DEMAND AND NEVER ON PRESS, or it talks over the pad it is describing. Reads the state
    `follow_desk` keeps, so twenty progress pushes a second cost nothing until somebody asks.

    FALLS BACK TO A MODEL READ WHILE NOTHING IS LISTENING TO THE DESK: without this the shortcut
    would answer "nothing" on a desk that is audibly playing, and a wrong answer is worse than a
    slow one. The read costs about 1.5 s and cannot give time remaining, which needs two samples of
    a moving progress figure.

    THE THREE SOURCES ARE MERGED BY STREAM POSITION, NOT CONCATENATED. They overlap: a voice effect
    this app engaged is in `_pads_on` AND in the desk's own `padActive`, so appending both says
    "Voice Disguise is switched on. Voice Disguise is switched on." Position is the one identity all
    three share."""
    said = {}                       # stream position -> the sentence about it, first source wins
    for position, (progress, left) in play_snapshot().items():
        name = _pad_name(position) or f"The pad at position {position}"
        said[position] = (f"{name}, {round(progress * 100)} percent through, "
                          f"{wording.describe_duration(left)} left." if left is not None
                          else f"{name}, {round(progress * 100)} percent through.")
    if not said and not desk_live.connected():
        said.update(_playing_from_model())
    # HID-ENGAGED PADS ARE NOT IN THE STREAM AT ALL. The desk does not echo our own `padActive`
    # write (measured: engaging a voice-FX pad over HID pushes nothing whatever), so a voice
    # effect this app switched on is only knowable from what we remember doing.
    for pad_idx in sorted(_pads_on):
        entry = _pad_entry(pad_idx)
        if entry:
            # SAY WHEN THERE IS NOTHING BEHIND IT. `_pads_on` records what this app WROTE, not
            # what the desk is doing, so a voice-FX pad that lost its effect object reads as
            # switched on here and is inaudible on the mic. That gap is the whole reason this line
            # cannot just report the flag.
            said.setdefault(entry[0], _FX_EMPTY.format(name=entry[1])
                            if pad_idx in _fx_missing else f"{entry[1]} is switched on.")
    for pad_idx in sorted(_pads_held):
        entry = _pad_entry(pad_idx)
        if entry:
            said[entry[0]] = f"{entry[1]} is being held down."
    bits = [said[p] for p in sorted(said)]
    return _said(" ".join(bits) if bits else "Nothing is playing.")


def _playing_from_model() -> dict:
    """What the DESK says is active, read fresh. The answer while nothing is listening.

    Returns {stream position: sentence}, keyed the same way as the tracked state so the caller can
    merge the two rather than say the same pad twice."""
    try:
        with desk_access.hid_lock:
            blob = rcp_read.read_model_stable()
        pads = soundpad.pad_nodes(blob)
    except Exception:
        return {-1: "I could not ask the desk what is playing."}
    out = {}
    for p in pads:
        if p.props.get("padActive") is not True:
            continue
        frac = p.props.get("padProgress")
        if p.type == soundpad.PAD_TYPE_SOUND and isinstance(frac, float):
            out[p.position] = f"{p.name}, {round(frac * 100)} percent through."
        else:
            out[p.position] = f"{p.name} is switched on."
    return out
