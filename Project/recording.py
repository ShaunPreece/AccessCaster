"""
AccessCaster - the Recording page: the recorder's transport, the recordings on the card or drive,
and ejecting and erasing the card or drive. The protocol side is recorder.py; this is the app side.
"""

from __future__ import annotations

import time

import announcer
import desk_access
import desk_live
import devices
import diagnostics
import model_tree
import rcp_read
import rcp_transfer
import recorder
import speech
import wording


# THE RECORDER. Start, pause and stop from software, and say what the unit is doing.
#
# TWO THINGS SHAPE THIS CODE:
#
#   * **The state comes from a PUSH, not a poll.** recorder.Watcher follows the desk listener and
#     hears `recordState` change within ~30 ms, from our writes AND from the unit's own REC
#     button. So `read_live` costs nothing and can be polled while the page is shown,
#     while `read` (the full page: volumes and the recordings list) does a ~1.5 s dump.
#   * **A write is never trusted.** `requestRecordState` is a request the firmware consumes and
#     resets, so the only honest confirmation is reading `recordState` back, which is what
#     action_record does before it tells anyone the recording started.

_watch = None


def _state_changed(was, now) -> None:
    """Speak a recording state change, and SWITCH ANNOUNCEMENTS OFF FOR THE TAKE. On the Watcher's
    thread, so this runs whether the recording was started from here or from the unit's own button.

    ANNOUNCEMENTS ARE SUSPENDED WHILE RECORDING because there is no other way to keep them out of
    the recording. Sending the speech to the computer's own speakers instead of the desk is NOT a
    fix: it moves the announcements off the USB return and they are picked up by the microphones
    anyway. Anything audible in the room reaches the mics, so silence is the only fix that works.

    THE NOTICE IS SPOKEN BEFORE THE SILENCE, and only when the recording was started ON THE UNIT.
    Started from this app, the reply carries the same words to the window instead, spoken by the
    screen reader rather than by this app, and therefore cannot land in the take. Started on the unit there is
    no reply to carry it, so one short phrase at the top of the recording is the price of the
    operator knowing what just happened.

    A PAUSE GIVES ANNOUNCEMENTS BACK until the take resumes. Nothing reaches the take while it is
    paused, and without speech a paused desk sounds exactly like a recording one. A resume from the
    UNIT says "Recording again" as it suspends them, which lands at the start of the resumed section
    for the same reason "Recording started" does; a resume from this app says "Resuming" first and
    waits for it to finish (action_record).

    WHAT IS SAID COMES FROM THE TRANSITION, NOT FROM `now` ALONE (recorder.spoken_change), which is
    what tells a drive being plugged in apart from a recording stopping. The SUSPENSION decisions
    are keyed on the new state alone: a take is over once the desk leaves RECORDING and PAUSED,
    whatever the reason it left."""
    ours = announcer.is_self_echo(("record",))
    # The last destination we knew about, which on a REMOVAL is the thing that just left. Best
    # effort by design: it is empty until a full page read has happened, and spoken_change falls
    # back to naming no medium at all rather than guessing one.
    medium = _context.get("where") or ""
    phrase = recorder.spoken_change(was, now, medium)
    # A tone replaces the phrase, and plays for this app's own presses too: a shortcut's spoken
    # reply to a start is lost to the silence the take begins with. It is queued before that
    # silence, and the speech thread plays what is already queued.
    cue = recorder.record_cue(was, now)
    if cue and announcer.record_feedback == "silent":
        # A transport change, chosen to make no sound. Announcements are still suspended for the
        # take and given back after it, below.
        phrase = ""
    if announcer.record_feedback != "tone":
        cue = None
    if now == recorder.RECORDING:
        # Only a NEW take explains the silence: after a pause it is already known.
        going_quiet = (speech.enabled.is_set() and announcer.record_mutes_announcer()
                       and was != recorder.PAUSED)
        if cue:
            speech.tone(cue)
        elif speech.enabled.is_set() and not ours and phrase:
            # One phrase, not two: the speech pump coalesces, so a second would silence the first.
            speech.say(f"{phrase}."
                     + (" Announcements off until you stop." if going_quiet else ""))
        announcer.suspend_for_record()
        return
    if now == recorder.PAUSED:
        # Restored only if the take suspended them, so the resume suspends them again the same way.
        announcer.restore_after_record()
        if cue:
            speech.tone(cue)
        elif speech.enabled.is_set() and not ours and phrase:
            speech.say(f"{phrase}.")
        return
    if now in (recorder.READY, recorder.NO_DESTINATION):
        # Unconditional, and before the speech: a take that ended because the drive was pulled must
        # give announcements back just the same as one that was stopped. It is a no-op when we did
        # not switch them off in the first place, so an idle plug costs nothing here.
        came_back = announcer.restore_after_record()
        if cue:
            speech.tone(cue)
        elif not ours and speech.enabled.is_set() and phrase:
            speech.say(phrase + (". Announcements are back on." if came_back else "."))
        return
    if speech.enabled.is_set() and not ours and phrase:
        speech.say(phrase)


def _refusal(name: str, task: str = "") -> str:
    so = f", so it cannot {task}" if task else ""
    return (f"AccessCaster can read the recorder on the {name} but not drive it{so}: that has "
            f"only been confirmed on a RODECaster Pro 2.")


def _gate(task: str = ""):
    return desk_access.cap_gate("recorder", lambda name: _refusal(name, task))


def watcher():
    """The running Watcher, started on first use. Returns None if it could not be started, and the
    callers then fall back to reading the state out of a model dump."""
    global _watch
    if _watch is None:
        try:
            w = recorder.Watcher(on_change=_state_changed)
            w.start()
            _watch = w
        except Exception as exc:
            diagnostics.event("recorder.watcher_failed", "the recorder listener did not start",
                              error=str(exc))
            return None
    return _watch


def _payload(blob: bytes) -> dict:
    """The Recording section from one dump: the read and every transport command's reply."""
    dev = devices.get_active_device()
    out = recorder.describe(blob)
    # Multitrack lives here rather than on Routing: it decides what a recording CONTAINS. The write
    # is `action_set_multitrack`.
    mt = rcp_read.values(blob, "recordingMultitrackMode", "int")
    out["multitrack"] = mt[0] if mt else None
    # Normalised, because the desk stores out-of-range values (see recorder.RECORDING_FORMAT_LABEL):
    # anything non-zero IS an MP3, so the control shows MP3 rather than nothing for a stray 3.
    fmt = rcp_read.values(blob, "recordingCompressionQuality", "int")
    out["format"] = None if not fmt or fmt[0] is None else (0 if fmt[0] == 0 else 1)
    pb = rcp_read.values(blob, "recordingProcessingBypass", "bool")
    out["processing_bypass"] = bool(pb[0]) if pb else None
    # Known BEFORE the control is drawn, so a button that cannot work is disabled with the reason
    # attached rather than failing when it is pressed.
    out["can_delete"] = "recorder" in dev.caps
    out["delete_note"] = ("" if out["can_delete"] else
                          _refusal(dev.name, "delete a recording"))
    out["storage_writable"] = "storage_write" in dev.caps
    return out


def read() -> dict:
    """The Recording page, read from the unit. The desk model adopts the dump, so a push the
    listener missed is put right here, and the Watcher's count catches up with it untimed."""
    try:
        with desk_access.hid_lock:
            blob = rcp_read.read_model_stable()
    except Exception as exc:
        return {"ok": False, "message": f"Could not read the recorder: {exc}"}
    out = _payload(blob)
    w = watcher()
    if w is not None and out.get("state") is not None:
        out["live"] = w.snapshot()
    _note_context(out)
    out["ok"] = True
    out["message"] = _sentence(out)
    return out


# WHERE A RECORDING WOULD GO and HOW LONG THERE IS ROOM FOR, from the last full read.
#
# Kept so the CHEAP read can answer in full sentences too. `read_live` costs no device
# read at all (it is answered from the push listener), and without this it could only say the bare
# state - so "Recording status" would give you less than arriving on the page did.
_context = {"where": "", "room": "", "low": None}


def _note_context(payload: dict) -> None:
    dest = payload.get("destination")
    _context["where"] = dest["spoken"] if dest else ""
    _context["room"] = (payload.get("room") or {}).get("words") or ""
    _context["low"] = dest if (dest and dest.get("low")) else None


def _sentence(state, elapsed: str = "") -> str:
    """What the unit is doing, said in the order it matters while you are working.

    THE ORDER IS THE WHOLE DESIGN OF THIS SENTENCE: while a recording is running the only things
    worth hearing are WHETHER it is running and HOW LONG it has been going, with the time left at
    the END. Where it is recording to is not repeated - you
    were told that when you pressed record - and the storage detail is not here at all; that lives
    on the Device page, where you go to look at it deliberately.

    `state` may be the state code or the whole payload dict, so the page read and the cheap live
    read produce identical wording rather than two versions that drift."""
    if isinstance(state, dict):
        elapsed = elapsed or ((state.get("live") or {}).get("elapsed") or "")
        state = state.get("state")
    where = f" to the {_context['where']}" if _context["where"] else ""
    # ROOM LEFT IN TIME, not megabytes: whether a two-hour show fits is the actual question, and
    # nobody can answer it from a free-space figure and a bitrate while a recording is running.
    room = _context["room"]
    room_left = f" {room} left." if room else ""
    if state == recorder.RECORDING:
        so_far = f" {elapsed} so far." if elapsed else (
            " It started before AccessCaster was watching, so there is no time to give.")
        return f"Recording.{so_far}{room_left}"
    if state == recorder.PAUSED:
        so_far = f" {elapsed} recorded." if elapsed else ""
        return f"Paused.{so_far}{room_left}"
    if state == recorder.READY:
        low = _context["low"]
        if low:
            room_left += (f" Careful: only {low['free_pct']} percent of the drive is free, "
                          f"about {low['free_mb']} megabytes.")
        return f"Ready to record{where}.{room_left}"
    if state == recorder.NO_DESTINATION:
        return ("The desk has nowhere to record to. Put a memory card in, or a USB drive in the "
                "second USB port, and this will say Ready.")
    return "The desk did not report a recording state."


def read_live() -> dict:
    """The cheap one: what the unit is doing right now, with NO device read at all.

    Answered entirely from the Watcher's last push, so the page can poll it while it is open
    without costing a 1.5 s dump or fighting anything else for the HID lock."""
    w = watcher()
    if w is None:
        return {"ok": False, "message": "The recording state listener is not running."}
    snap = w.snapshot()
    snap["ok"] = True
    # The same sentence the page read produces, built from the listener's state plus the
    # destination and room noted at the last full read. One wording, two endpoints.
    snap["message"] = _sentence(snap.get("state"), snap.get("elapsed") or "")
    return snap


def action_record(command: str) -> dict:
    """Start, pause, resume or stop, then READ THE STATE BACK before saying what happened.

    The read-back is the whole point. `requestRecordState` is consumed and reset by the firmware,
    so a write that lands on a unit with no destination looks exactly like one that started a
    recording. Reporting "recording" to someone who cannot see the unit, when it is not recording,
    is the single worst thing this page could do."""
    cmd = (command or "").strip().lower()
    if cmd not in recorder.COMMANDS:
        return {"ok": False, "message": f"Unknown recording command: {command}."}
    gate = _gate()
    if gate is not None:
        return gate

    w = watcher()
    before = w.snapshot().get("state") if w else None
    if cmd == "pause" and before == recorder.PAUSED:
        return {"ok": True, "message": "It is already paused. Resume carries on with it."}
    if cmd in ("start", "resume") and before == recorder.RECORDING:
        return {"ok": True, "message": "It is already recording."}
    if cmd == "stop" and before in (recorder.READY, recorder.NO_DESTINATION):
        return {"ok": True, "message": "Nothing is recording."}
    if cmd == "resume" and before == recorder.PAUSED and announcer.record_feedback == "speak":
        # Announcements are back on during a pause (_state_changed), and this is the one
        # resume where the notice can be finished before the take carries on. With record tones,
        # the start tone marks the resume instead, and with nothing chosen nothing is said.
        speech.say_and_wait("Resuming")

    # Mark it BEFORE the write: the unit pushes the new state back in about 30 ms, which can beat
    # this function's own reply to the page.
    announcer.mark_self_write(("record",))
    wanted = {"start": recorder.RECORDING, "resume": recorder.RECORDING,
              "pause": recorder.PAUSED, "stop": recorder.READY}[cmd]
    listener = desk_live.PushListener()
    try:
        listener.start()            # before the write: the desk answers within 30 ms
        with desk_access.hid_lock:
            recorder.send_command(recorder.COMMANDS[cmd])
        heard = _await_record(listener, wanted)
        with desk_access.hid_lock:
            blob = rcp_read.read_model_stable(fresh=True)
    except Exception as exc:
        return {"ok": False, "message": f"Could not {cmd} the recording: {exc}"}
    finally:
        listener.stop()
    diagnostics.event("record.answered", cmd, **heard)

    state = recorder.read_state(blob)
    if w is not None:
        w.note_state(state, timed=(cmd in ("start", "resume")))
    # Idempotent, and belt and braces: the Watcher has almost certainly done this already off the
    # unit's push, but a missed push must not leave announcements talking into a take, or leave
    # them off after one.
    announcer_back = False
    if state == recorder.RECORDING:
        announcer.suspend_for_record()
    elif state in (recorder.PAUSED, recorder.READY, recorder.NO_DESTINATION):
        announcer_back = announcer.restore_after_record()
    info = _payload(blob)
    _note_context(info)
    if state == wanted:
        msg = {"start": "Recording.", "resume": "Recording again.",
               "pause": "Paused.", "stop": "Stopped."}[cmd]
        dest = info.get("destination")
        if cmd == "start" and dest:
            msg = f"Recording to the {dest['spoken']}."
        if cmd == "stop":
            newest = (info.get("recordings") or [None])[0]
            if newest:
                msg = f"Stopped. Saved {newest['name']}, {newest['duration']}."
        # SAID IN THE REPLY, not spoken: this is the one path where the notice can reach the
        # operator without making a sound, and therefore without landing in the recording.
        if cmd == "start" and announcer.suspended_for_record:
            msg += " Announcements are off until you stop."
        elif cmd == "stop" and announcer_back:
            msg += " Announcements are back on."
        return {"ok": True, "state": state, "recorder": info, "message": msg}
    # It did not do what was asked. Say what it IS doing, and why that is usually the answer.
    return {"ok": False, "state": state, "recorder": info,
            "message": f"That did not take. The desk says: {_sentence(info)}"}


def action_drop_marker() -> dict:
    """Put a marker at the current point of the recording.

    THE REPLY IS DELIBERATELY NOT "DONE". Nothing over USB confirms a marker: the desk consumes the
    write and pushes the field back cleared, and no readable property changes, so the app can only
    honestly say it was sent and where it believes it landed. The marker lands in the file, as a
    standard WAV cue point.

    Refused when nothing is running, because a marker has no meaning without a position. The
    ELAPSED TIME is what makes the reply useful: "marker at 2 minutes 5 seconds" is something you
    can find again in an editor, where "marker sent" is not."""
    gate = _gate("drop a marker")
    if gate is not None:
        return gate
    w = watcher()
    state = w.snapshot().get("state") if w else None
    if state != recorder.RECORDING:
        return {"ok": False,
                "message": ("Markers can only be dropped while something is recording."
                            if state != recorder.PAUSED else
                            "Nothing is being recorded while it is paused, so there is nowhere to "
                            "put a marker.")}
    try:
        with desk_access.hid_lock:
            recorder.drop_marker()
    except Exception as exc:
        return {"ok": False, "message": f"Could not drop a marker: {exc}"}
    where = (w.snapshot().get("elapsed") if w else "") or ""
    return {"ok": True,
            "message": (f"Marker dropped at {where}." if where else "Marker dropped.")
                       + " It will show up in the recording as a marker your editor can find."}


# The unit takes a moment to act on a delete request. Measured at well under a second, so this is
# a comfortable margin, and the read-back that follows is what actually decides the answer.
_RECORD_SETTLE_S = 1.2
# The most a transport press waits for the desk's answer. Only a press the unit ignores waits it
# out, so it is set well clear of the slowest stop measured (1.06 s): a later answer would be
# reported as a press that did not take.
_RECORD_ANSWER_S = 3.0


def _await_record(listener, wanted: int) -> dict:
    """Wait, at most `_RECORD_ANSWER_S`, for the desk to push `recordState` at `wanted`, and return
    how long it took, for the log. Measured: a start answers in about 40 ms, a stop in 0.5 to 1.1 s,
    and a stopped take is already in the recordings list by then (no other push marks it).

    A request the unit ignores (nowhere to record to) pushes nothing, so the full margin passes and
    the read-back reports what the unit is doing instead."""
    started = time.monotonic()
    if listener.wait_for("recordState", _RECORD_ANSWER_S, match=lambda v: v == wanted) is None:
        return {}
    return {"recordState": round(time.monotonic() - started, 3)}


def action_delete_recording(uid: int) -> dict:
    """Delete one recording from the unit's card or drive, by UID.

    THE READ-BACK IS THE ANSWER, not the write. `requestDeleteUID` self-clears whether or not it did
    anything (the same pattern as the record command and the marker), so this re-reads the
    recordings list and reports what it finds: gone, or still there. It never says "deleted"
    because a write was sent.

    Refuses while a recording is running: deleting a take from under the recorder is the one version
    of this that could cost somebody real material rather than a test file."""
    gate = _gate("delete a recording")
    if gate is not None:
        return gate
    w = watcher()
    if (w.snapshot().get("state") if w else None) in (recorder.RECORDING, recorder.PAUSED):
        return {"ok": False,
                "message": "Stop the recording first. Nothing is deleted while the recorder is "
                           "running."}
    try:
        with desk_access.hid_lock:
            before = recorder.read_recordings(rcp_read.read_model_stable(fresh=True))
            target = next((r for r in before if r.get("uid") == int(uid)), None)
            if target is None:
                return {"ok": False, "message": f"There is no recording with id {uid} on the desk."}
            recorder.delete_recording(int(uid))
            time.sleep(_RECORD_SETTLE_S)
            blob = rcp_read.read_model_stable(fresh=True)
    except Exception as exc:
        return {"ok": False, "message": f"Could not delete the recording: {exc}"}
    info = _payload(blob)
    still = any(r.get("uid") == int(uid) for r in (info.get("recordings") or []))
    if still:
        return {"ok": False, "recorder": info,
                "message": (f"{target['name']} is still on the desk. The request was sent and the "
                            f"desk did not act on it.")}
    return {"ok": True, "recorder": info,
            "message": (f"Deleted {target['name']}, {target['duration']}. "
                        f"{wording.count(len(info.get('recordings') or []), 'recording')} left "
                        f"on the desk.")}


# ----------------------------------------------------------------------------
# EJECTING AND ERASING THE CARD OR DRIVE. Per-volume switches on the STORAGEVOLUME nodes, found
# by walking the tree; section 7.2a of the protocol spec. The third volume is the unit's INTERNAL
# storage, which holds the pad sounds, so only a volume the desk marks as a recording destination
# may be touched, and the guard is checked against the live dump on every call.
_ERASE_WAIT_S = 90.0        # a 117 GB drive took about 20 s


def _storage_gate():
    return desk_access.cap_gate("storage_write", lambda name:
                     f"AccessCaster can show the {name}'s storage but not eject or erase it: that "
                     f"has only been confirmed on a RODECaster Pro 2.")


def _storage_target(index: int):
    """(volume, node path, blob) for a card or drive this may act on, or (None, refusal, None)."""
    blob = rcp_read.read_model_stable(fresh=True)
    vols = recorder.read_volumes(blob)
    vol = next((v for v in vols if v["index"] == int(index)), None)
    if vol is None or not vol.get("can_record"):
        return None, "That is not a card slot or drive this can change.", None
    nodes = [n for n in model_tree.walk(model_tree.parse(blob)[0]) if n.name == "STORAGEVOLUME"]
    if int(index) >= len(nodes):
        return None, "The desk does not publish that slot.", None
    return vol, nodes[int(index)].path(), blob


def _storage_reply(blob: bytes, ok: bool, message: str) -> dict:
    return {"ok": ok, "recorder": _payload(blob), "message": message}


def action_storage_eject(index: int, on: bool = True) -> dict:
    """Eject the card or drive (`on`), or reconnect one ejected from here (`on=False`).

    An ejected volume reads as empty, so it can be pulled out safely, and clearing the flag mounts
    it again with no replug. Refused while recording: the take is on that volume."""
    gate = _storage_gate()
    if gate is not None:
        return gate
    on = bool(on)
    try:
        with desk_access.hid_lock:
            vol, path, blob = _storage_target(index)
            if vol is None:
                return {"ok": False, "message": path}
            if on and recorder.read_state(blob) in (recorder.RECORDING, recorder.PAUSED):
                return {"ok": False, "message": f"The {vol['spoken']} is being recorded to. Stop "
                                                f"the recording first."}
            if on and not vol["inserted"]:
                return {"ok": False, "message": f"Nothing is in the {vol['spoken']}."}
            rcp_transfer.send_record(
                rcp_transfer.build_path_prop_bool("storageVolumeEject", on, path))
            time.sleep(1.5 if on else 3.0)
            blob = rcp_read.read_model_stable(fresh=True)
        now = next((v for v in recorder.read_volumes(blob) if v["index"] == vol["index"]), None)
    except Exception as exc:
        return {"ok": False, "message": f"Could not change the {recorder.volume_spoken(index)}: "
                                        f"{exc}"}
    if now is None:
        return _storage_reply(blob, False, f"The desk no longer reports the {vol['spoken']}. If it "
                                           f"has been unplugged, plug it in again to use it.")
    if on:
        if now["mounted"]:
            return _storage_reply(blob, False, f"The {vol['spoken']} is still in use: the desk "
                                               f"did not eject it.")
        return _storage_reply(blob, True, f"The {vol['spoken']} is ejected. You can unplug it "
                                          f"now, or reconnect it here to use it again.")
    if not now["mounted"]:
        return _storage_reply(blob, False, f"The {vol['spoken']} did not come back. If it has "
                                           f"been unplugged, plug it in again.")
    return _storage_reply(blob, True, f"The {vol['spoken']} is back, with {now['free_mb']} MB "
                                      f"free.")


def action_storage_erase(index: int) -> dict:
    """Erase (format) the card or drive. Everything on it is deleted; the page asks first.

    The desk clears `storageVolumeErase` itself when the format is done, which is the
    confirmation; the free space then reads the whole capacity."""
    gate = _storage_gate()
    if gate is not None:
        return gate
    listener = desk_live.PushListener()
    try:
        with desk_access.hid_lock:
            vol, path, blob = _storage_target(index)
            if vol is None:
                return {"ok": False, "message": path}
            if recorder.read_state(blob) in (recorder.RECORDING, recorder.PAUSED):
                return {"ok": False, "message": "A recording is running. Stop it before erasing."}
            if not (vol["inserted"] and vol["mounted"]):
                return {"ok": False, "message": f"Nothing usable is in the {vol['spoken']}."}
            listener.start()
            rcp_transfer.send_record(
                rcp_transfer.build_path_prop_bool("storageVolumeErase", True, path))
        listener.wait_for("storageVolumeErase", _ERASE_WAIT_S, match=lambda v: v is False)
        with desk_access.hid_lock:
            blob = rcp_read.read_model_stable(fresh=True)
    except Exception as exc:
        return {"ok": False, "message": f"Could not erase the {recorder.volume_spoken(index)}: "
                                        f"{exc}"}
    finally:
        listener.stop()
    now = next((v for v in recorder.read_volumes(blob) if v["index"] == vol["index"]), None)
    if not now or now["free_mb"] < now["capacity_mb"] - 64:
        return _storage_reply(blob, False, f"The erase was sent, but the {vol['spoken']} does not "
                                           f"read as empty yet.")
    return _storage_reply(blob, True, f"Erased the {vol['spoken']}. {now['free_mb']} MB free.")


# ----------------------------------------------------------------------------
# RECORDING OPTIONS: what a recording contains and how it is saved. All three live on the OUTPUT
# block, so they share the master output writes' capability.

def _options_gate():
    return desk_access.cap_gate("output_write", lambda name:
                     f"Changing the recording settings is not enabled on the {name} yet, so "
                     f"these controls are read-only here.")


def action_set_multitrack(mode: int) -> dict:
    gate = _options_gate()
    if gate is not None:
        return gate
    if mode not in rcp_transfer.MULTITRACK_MODES:
        return {"ok": False, "message": f"Multitrack mode {mode} is not valid."}
    with desk_access.hid_lock:
        rcp_transfer.set_multitrack_mode(mode)
    # Answered in the same words the control offers (recorder.MULTITRACK_LABEL), not in the unit's
    # own "Pre-Fader" / "Post-Fader", which never say whether you are getting multitrack at all.
    return {"ok": True,
            "message": f"Recording set to {recorder.MULTITRACK_LABEL[mode].lower()}."}


def action_set_recording_format(value: int) -> dict:
    """WAV or MP3 for the unit's own recordings. Refuses anything else, deliberately.

    The desk would ACCEPT a 2 or a 99 and store it, so the validation here is the only thing that
    keeps it on a value its own touchscreen can represent (see rcp_transfer.set_recording_format).
    """
    gate = _options_gate()
    if gate is not None:
        return gate
    if value not in recorder.RECORDING_FORMAT_LABEL:
        return {"ok": False, "message": f"Recording format {value} is not valid."}
    with desk_access.hid_lock:
        rcp_transfer.set_recording_format(value)
    # NOT lowercased, unlike the multitrack reply: these labels open with an initialism, and
    # "written as mp3, smaller files" reads as a typo and is spoken as a word by some voices.
    return {"ok": True,
            "message": f"Recordings will be saved as "
                       f"{recorder.RECORDING_FORMAT_LABEL[value]}."}


def action_set_processing_bypass(on: bool) -> dict:
    """The RECORDER's raw switch: what the unit writes to its card or drive.

    Two actions rather than one with a `which`, because the two destinations live on different
    pages and each endpoint belongs to the page that calls it. They share the sentence, not the
    route."""
    gate = _options_gate()
    if gate is not None:
        return gate
    with desk_access.hid_lock:
        rcp_transfer.set_recording_processing_bypass(on)
    return {"ok": True, "message": recorder.bypass_sentence("Recordings on the card or drive", on)}
