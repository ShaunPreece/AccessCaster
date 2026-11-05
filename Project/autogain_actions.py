"""
AccessCaster - auto gain's app side: set a microphone's gain by measuring it.

The one studio job that is purely visual - talk, watch a meter, turn the knob - which is why it is
worth building rather than another spoken meter that a listener has to interpret. The measurement
rules, and why each one exists, are in autogain.py.

IT RUNS ON A THREAD, polled through `status`, because it takes half a minute and the page has to
stay answerable throughout. Every device change it makes is recorded so it can be put back: the
Chat mix and the channel's processing bypass are ALWAYS restored, and the gain can be undone from
the result.
"""

from __future__ import annotations

import threading
import time

import announcer
import autogain
import channels
import chat_isolation
import desk_access
import diagnostics
import meters

# The take. 15 s at 20 readings a second is 300, so the 99th percentile still drops the loudest
# three: a cough, not a sentence.
SECONDS = 15.0
# The second, shorter take that checks the new gain. Ten seconds is enough for a few sentences and
# short enough that nobody minds being asked twice.
VERIFY_S = 10.0
# Between a take's instruction and its start tone: long enough for the page's next poll to pick it
# up and a screen reader to finish saying it. It grows with the instruction: on NVDA a ten-word one
# needs about 3.5 s.
LEAD_S = 1.0
LEAD_PER_WORD_S = 0.25
_lock = threading.Lock()
_job = None            # the running or finished job, as a plain dict


def state() -> dict:
    with _lock:
        return dict(_job) if _job else {"phase": "idle"}


def running() -> bool:
    """True while a run is preparing, listening or deciding: the level check refuses then, since
    the run owns the Chat isolation and is writing a gain from it."""
    return state().get("phase") in ("preparing", "listening", "verifying", "working")


def _set(**changes) -> None:
    with _lock:
        if _job is not None:
            _job.update(changes)


def _mic_number(name: str):
    """The 1-based mic number for a UI channel key ("Mic2" -> 2), or None if it is not a mic.
    Only microphone channels have a preamp gain to set."""
    if not str(name).startswith("Mic"):
        return None
    try:
        n = int(str(name)[3:])
    except ValueError:
        return None
    return n if n in channels.MIC_CHANNELS else None


def _label(name: str) -> str:
    return (channels.ui_channel_map().get(name) or {}).get("label", name)


def _run(name: str, mic: int, seconds: float) -> None:
    """The whole job: prepare, listen in silence, decide, apply, restore, report."""
    restore_processing = False
    iso = None
    try:
        _set(phase="preparing", message="Getting ready. Do not talk yet.")
        with desk_access.hid_lock:
            gain_before, proc_on = channels.mic_gain_and_processing(mic)
        if gain_before is None:
            _set(phase="failed", done=True,
                 message="Could not read that microphone's gain from the desk.")
            return
        _set(gain_before=gain_before, processing_before=proc_on)

        # PROCESSING OFF. The meter reads the unit's output, which is AFTER the compressor and
        # limiter, so with processing on we would be measuring the safety net and not the gain.
        if proc_on:
            with desk_access.hid_lock:
                channels.set_processing_enabled(mic, False)
            restore_processing = True

        endpoint = autogain.chat_endpoint()
        if endpoint is None:
            raise RuntimeError("the desk's Chat audio device is not available on this computer")
        # Windows out of the way on the endpoint measured, and on no other: its level scales the
        # reading and its mute would make the microphone look dead. The meter's chosen stream may
        # be a different endpoint, which is left as the user set it.
        win = chat_isolation.endpoint_to_unity(endpoint["id"])
        check = None
        # The lock is taken first. `as` binds only once __enter__ succeeds, and a failed __enter__
        # restores for itself.
        with desk_access.hid_lock, chat_isolation.ChatIsolation(mic - 1) as iso:
            result = _take("Talk at the tone.", "listening", seconds, endpoint["id"], iso)
            decision = autogain.verdict(result) if result is not None else None
            target = gain_before
            if decision and decision["ok"]:
                wanted = int(round(gain_before + decision["delta_db"]))
                target = max(channels.GAIN_MIN_DB, min(channels.GAIN_MAX_DB, wanted))
                if target != gain_before:
                    _write_gain(mic, target)
                # CHECK ITS OWN WORK: set the gain from the results, then measure again.
                # Without the second take the result is a PREDICTION rather than a measurement,
                # and nothing tells the user whether it landed. The isolation is still open, so
                # it costs nothing but the ten seconds.
                check = _take("Talk again at the tone.", "verifying", VERIFY_S, endpoint["id"], iso)
            # CANCEL MEANS NOTHING CHANGES, at any point: a take cut short is not evidence about
            # the microphone, so a gain already set from the first take is put back.
            cancelled = _cancelled()
            if cancelled and target != gain_before:
                _write_gain(mic, gain_before)
        if cancelled:
            _set(phase="failed", done=True, message="Cancelled. The gain was not changed.")
            return
        _set(phase="working", message="Working it out.")

        if not decision["ok"]:
            _set(phase="failed", done=True, result=result, message=decision["reason"])
            return
        words = _words(name, gain_before, target, result, win, check)
        _set(phase="done", done=True, gain_after=target, result=result, check=check,
             message=words)
    except Exception as exc:
        _set(phase="failed", done=True, message=f"Auto gain stopped: {exc}")
    finally:
        # THE CHAT MIX AND THE PROCESSING ARE ALWAYS PUT BACK, whatever went wrong, and both
        # restores are VERIFIED: a silent failure leaves the operator sounding different with no
        # sign of why. The isolation's own exit has already tried once, so this is the retry, and
        # if it still cannot be put back it says so LOUDLY in the result.
        if iso is not None and not iso.restored:
            with desk_access.hid_lock:
                if not iso.restore():
                    _warn(chat_isolation.NOT_RESTORED)
        if restore_processing and not _restore_processing(mic):
            _warn("WARNING: processing could not be switched back on for that "
                  "microphone. Switch it on yourself on this page, or that channel will "
                  "sound unprocessed.")


def _cancelled() -> bool:
    return bool(state().get("cancel"))


def _take(instruction: str, phase: str, seconds: float, endpoint_id: str, iso):
    """One take: the instruction, then the tone, then `seconds` of listening. Returns the
    measurement, corrected for the fader, or None if the run was cancelled before it ended.

    The tone finishes before the take starts, not because it would otherwise be measured (it plays
    out on USB 1, and the isolated mix excludes everything except the microphone) but because the
    person should not start talking over their own start signal."""
    _lead(instruction)
    if _cancelled():
        return None
    meters.cue("start")
    _set(phase=phase, seconds=seconds, started=time.monotonic(), message="Listening.")
    result = autogain.measure_endpoint(endpoint_id, seconds, should_stop=_cancelled)
    meters.cue("stop")
    if _cancelled():
        return None
    # POST-FADER, corrected: the cross-point carries the channel at its own level, so the gain
    # derived from it must not move with the fader.
    return autogain.correct_for_level(result, iso.level)


def _write_gain(mic: int, db: int) -> None:
    """Set the mic's gain as auto gain's own change, so the announcer does not report it."""
    channels.set_mic_gain(mic, db)
    announcer.mark_self_gain(mic, True)
    time.sleep(0.4)


def _lead(instruction: str) -> None:
    """Say what to do before the start tone, not after it: the tone is the signal to begin, so
    the instruction has to have been heard by then. Cancel ends the wait early."""
    _set(phase="ready", message=instruction)
    end = time.monotonic() + LEAD_S + LEAD_PER_WORD_S * len(instruction.split())
    while time.monotonic() < end and not _cancelled():
        time.sleep(0.1)


def _warn(warning: str) -> None:
    """End the run as failed, with `warning` added to whatever the result already says."""
    st = state()
    _set(phase="failed", done=True, message=((st.get("message") or "") + " " + warning).strip())


def _restore_processing(mic: int) -> bool:
    """Switch a mic's processing back on and CHECK it took. Returns True when it is really on.

    Read back rather than assumed: the write is an ordinary property write and can fail while the
    unit is busy. The read resolves the mic's STRIP off the live fader vector, as the run's own
    read does, so a reordered layout checks the right strip.
    """
    for attempt in range(3):
        try:
            with desk_access.hid_lock:
                channels.set_processing_enabled(mic, True)
                time.sleep(0.4)
                _gain, on = channels.mic_gain_and_processing(mic)
            if on:
                return True
        except Exception as exc:
            diagnostics.event("autogain.restore_failed", "processing restore attempt",
                              mic=mic, attempt=attempt, error=str(exc))
        time.sleep(0.5 * (attempt + 1))
    return False


def _words(name: str, before: int, after: int, result: dict, win: str, check=None) -> str:
    """The result, said the way it would be said out loud.

    IT REPORTS THE SECOND TAKE, not a prediction. A closing line like "talk again and it should sit
    around minus 9" promises a result nothing has measured AND reads as an instruction to speak while
    nothing is listening. By the time this runs the second take has happened, so it says what was
    actually heard. What was done to the Windows level (`win`) comes last: before the result, its
    "no change" reads as the result."""
    label = _label(name)
    was = f"You were peaking at {meters.dbfs(result['loud'])}."
    if after == before:
        head = f"{label} was already right, so the gain is still {before} dB. {was}"
    else:
        way = "down" if after < before else "up"
        head = f"{label} gain turned {way} from {before} to {after} dB. {was}"
    if not check:
        return f"{head} {win}".strip()
    now = check["loud_db"]
    at = f"Checking again, you peaked at {meters.dbfs(check['loud'])}."
    if check["loud_db"] < autogain.MIN_USABLE_DBFS:
        return (f"{head} {at} That was too quiet to confirm - nothing was heard on the check. "
                f"{win}").strip()
    off = now - autogain.TARGET_DBFS
    if abs(off) <= 3.0:
        return f"{head} {at} That is right. {win}".strip()
    way = "loud" if off > 0 else "quiet"
    return (f"{head} {at} Still about {abs(off):.0f} dB too {way}. Run it again, speaking as "
            f"you did the first time, or set the gain yourself. {win}").strip()


def action_autogain_start(name: str, seconds: float = 0.0) -> dict:
    """Begin an auto-gain run on one mic channel."""
    global _job
    gate = desk_access.cap_gate("input_mic_write",
                                lambda dev_name: f"Mic gain cannot be set on the {dev_name} from "
                                                 f"here.")
    if gate is not None:
        return gate
    mic = _mic_number(name)
    if mic is None:
        return {"ok": False, "message": "Only microphone channels have a gain to set."}
    with _lock:
        if _job and not _job.get("done"):
            return {"ok": False, "message": "Auto gain is already running."}
        _job = {"phase": "starting", "channel": name, "done": False, "cancel": False,
                "message": "Starting."}
    secs = float(seconds) if seconds and seconds > 0 else SECONDS
    secs = max(5.0, min(120.0, secs))
    threading.Thread(target=_run, args=(name, mic, secs), daemon=True).start()
    return {"ok": True, "message": "Auto gain started.", "seconds": secs}


def status() -> dict:
    st = state()
    st["ok"] = True
    started, secs = st.get("started"), st.get("seconds")
    if st.get("phase") == "listening" and started and secs:
        st["seconds_left"] = max(0, int(round(secs - (time.monotonic() - started))))
    return st


def action_autogain_stop() -> dict:
    """Cancel the run. Nothing changes: a take cut short is not evidence about the microphone, so
    `_run` puts back any gain it has already set."""
    _set(cancel=True)
    return {"ok": True, "message": "Stopping."}


def action_autogain_undo() -> dict:
    """Put the gain back. ONLY the gain: the processing bypass is restored by the run itself, and
    the Windows level is left at no change, because that is where it should be anyway."""
    st = state()
    name, before = st.get("channel"), st.get("gain_before")
    mic = _mic_number(name) if name else None
    if mic is None or before is None:
        return {"ok": False, "message": "There is no gain change to undo."}
    try:
        with desk_access.hid_lock:
            channels.set_mic_gain(mic, int(before))
        announcer.mark_self_gain(mic, True)
    except Exception as exc:
        return {"ok": False, "message": f"Could not put the gain back: {exc}"}
    _set(gain_after=int(before))
    return {"ok": True, "message": f"{_label(name)} gain put back to {before} dB."}
