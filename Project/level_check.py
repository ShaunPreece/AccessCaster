"""
AccessCaster - the level check, on one channel (Channels > any channel): talk, and hear that
channel's level spoken every 1.5 s for as long as it takes to ride a fader, ending with the loudest
it got and what to do about it.

WHY IT IS PER CHANNEL. Every Windows STREAM this desk offers is a MIX: "Microphone (RODECaster Pro
II Main Stereo)" is everything at once. A stream answers "is something reaching the computer" and
never "is MIC 2 right", which is the question you actually have while setting up.

IT HEARS ONE CHANNEL by the same means auto gain does: `chat_isolation.ChatIsolation`, whose
reasons all apply here, above all that the multitrack device is not an option.

IT AGREES WITH AUTO GAIN BY CONSTRUCTION: the cross-point is post-fader, so the Watcher is given
`scale=iso.level` and every number it speaks is in channel terms. A check run straight after auto
gain confirms auto gain rather than arguing with it whenever a fader is anywhere but unity.
"""

from __future__ import annotations

import threading
import time

import autogain
import autogain_actions
import chat_isolation
import clip_warning
import desk_access
import diagnostics
import meters
import output_mixes
import speech

# How often the level is spoken during a check. Fast enough to ride a fader by, slow enough that
# the phrases do not run into each other.
_SPEAK_S = 1.5

# How long a check runs: long enough to ride a fader by. Pressing the button again ends it.
_CHECK_S = 30.0

_lock = threading.Lock()
_watcher = None            # the meters.Watcher
_iso = None                # the Chat isolation it is listening through
_label = ""                # what it is called out loud ("Mic 2")
_ends = 0.0
# Each run gets a number, and its end-timer carries that number. Without it, a timer belonging to a
# run that has already been stopped fires later and stops whatever is running THEN: press stop at
# 8 s, start again at 10 s, and the first run's 30-second timer arrives 20 s into the second run.
_run_id = 0


def _summary(w) -> str:
    """What the run came to: the measurement, then the ADVICE.

    The reading alone is only half an answer for somebody who cannot see a meter - "peaking at
    minus 3 dB" still leaves you working out whether that is fine. So a check ends with a verdict
    and, when there is something to do about it, which control to turn.
    The maximum comes from `peak_max`, the true peak over the run, not the decaying hold."""
    live = 1.0 if w.peak_max > meters.SILENCE else 0.0
    # The special cases (muted, engine not started, nothing at all) are whole answers already, with
    # their own instruction in them, so they are reported as `describe` puts them.
    if w.muted or w.wake_failed or w.peak_max <= meters.SILENCE:
        return meters.describe(w.peak_max, live, w.name or "That channel",
                               wake_failed=w.wake_failed, ceiling=w.ceiling,
                               volume_db=w.volume_db, muted=w.muted, clip_at=w.clip_at)
    # Otherwise: the measurement, then the advice. NOT `describe` plus a verdict - describe ends
    # with its own instruction, so the two together would say "turn it down" twice.
    return (f"{w.name or 'That channel'}: peaked at {meters.dbfs(w.peak_max)}. "
            + meters.verdict(w.peak_max, live, volume_db=w.volume_db,
                             ceiling=w.ceiling, clip_at=w.clip_at))


def _state() -> dict:
    """Is a run in progress? A LIVE ISOLATION COUNTS, even with no watcher left alive.

    Answering on the watcher alone leaves a Chat mix isolated: if the watcher thread dies mid-run
    this says "not running", the next press STARTS a second run instead of stopping the first, and
    the first run's isolation - the thing that has to be put back - is dropped on the floor with
    nobody holding a reference to it. Anything that still needs restoring means a run is still in
    progress."""
    with _lock:
        live = _watcher is not None or _iso is not None
        return {"running": live,
                "channel": _label if live else "",
                "seconds_left": max(0.0, round(_ends - time.monotonic(), 1)) if live else 0.0}


def _stop(speak: bool = True, run_id: int | None = None) -> str:
    """End the check, PUT THE CHAT MIX BACK, and say what the run came to.

    The restore is the part that must not be quiet about failing. A Chat output left carrying one
    microphone would break the next call taken on this desk with nothing on screen to explain it,
    so a failed restore is spoken and returned, ahead of the measurement.

    `run_id` is how the end-timer says WHICH run it is ending. A timer with a stale number does
    nothing at all, rather than stopping a run somebody has only just started."""
    global _watcher, _iso
    with _lock:
        if run_id is not None and run_id != _run_id:
            return ""
        w, iso, _watcher, _iso = _watcher, _iso, None, None
    if w is None and iso is None:
        return ""
    if w is not None:
        try:
            w.stop()
        except Exception as exc:
            # The Chat mix still has to go back, below.
            diagnostics.event("level_check.stop_failed", str(exc))
    # The stop tone as soon as nothing is listening, not after the restore below: it is the
    # answer to the press, and the restore takes a second or more.
    if w is not None:
        meters.cue("stop")
    trouble = ""
    if iso is not None:
        try:
            with desk_access.hid_lock:
                if not iso.restore():
                    trouble = chat_isolation.NOT_RESTORED + " "
        except Exception as exc:
            trouble = f"{chat_isolation.NOT_RESTORED} ({exc}) "
    # A WATCHER THAT FELL OVER MID-RUN MUST NOT BE SUMMARISED AS A QUIET CHANNEL. It reports its own
    # failure (meters.Watcher._run), and that answer replaces the measurement rather than
    # decorating it: the peak it reached before it broke is not what the channel did.
    broke = getattr(w, "error", "") if w is not None else ""
    if broke:
        msg = trouble + broke + " Nothing was measured, so no level is being reported."
    else:
        msg = trouble + (_summary(w) if w is not None else "")
    if speak and msg:
        speech.say(msg)
    return msg


def work_in_progress() -> str | None:
    """For `desk_access.work_in_progress`: a run has the Chat mix carrying one channel alone."""
    return "A level check is running." if _state()["running"] else None


def stop_check() -> None:
    """End a running check and put the Chat mix back, saying nothing: the app is closing."""
    _stop(speak=False)


def _source(name: str):
    """The matrix source a channel name refers to, or (None, reason)."""
    src = next((s for s in output_mixes.active_matrix().sources_for(True) if s["key"] == name), None)
    if src is None:
        return None, f"{name} is not a channel this desk exposes to the mixes."
    # CHAT IS THE ONE CHANNEL THIS CANNOT DO, and the reason is the mechanism rather than a rule:
    # the isolation listens on the Chat OUTPUT, and the Chat output is mix-minus - it deliberately
    # excludes the Chat source so a caller does not hear themselves. So there is nothing to hear.
    if src["key"] == "Chat":
        return None, ("The Chat channel cannot be checked this way: the check listens on the Chat "
                      "output, which deliberately leaves the caller out of their own mix.")
    return src, ""


def action_channel_level_check(name: str = "") -> dict:
    """Start the check on one channel, or end the one running. A toggle."""
    if _state()["running"]:
        return {"ok": True, "running": False, "message": _stop(speak=False)}

    global _watcher, _iso, _label, _ends, _run_id
    src, why = _source(name)
    if src is None:
        return {"ok": False, "running": False, "message": why}
    if autogain_actions.running():
        return {"ok": False, "running": False,
                "message": "Auto gain is running. Wait for it to finish, or stop it first."}
    gate = output_mixes.write_gate()      # None when writes are allowed; a refusal dict otherwise
    if gate is not None:
        return {"ok": False, "running": False, "message": gate["message"]}

    endpoint = autogain.chat_endpoint()
    if endpoint is None:
        return {"ok": False, "running": False,
                "message": "The desk's Chat audio device is not available on this computer, so "
                           "there is no way to listen to one channel on its own."}
    # Windows out of the way on the endpoint actually being metered: its level scales every reading
    # and its mute would make a working microphone read as silence. Said in the reply, not silently.
    win = chat_isolation.endpoint_to_unity(endpoint["id"])

    try:
        with desk_access.hid_lock:
            iso = chat_isolation.ChatIsolation(src["index"])
            iso.__enter__()
    except Exception as exc:
        # THE COMMON REFUSAL, and it needs an instruction rather than a diagnosis. A channel whose
        # fader is right down does not reach the Chat mix at all, so there is nothing to listen to,
        # and on a desk whose Chat preset carries one mic that is most of them. The isolation's own
        # wording explains the mixer; this adds what to DO.
        why = str(exc)
        if "not reaching the Chat mix" in why:
            return {"ok": False, "running": False,
                    "message": (f"{src['label']} is not reaching the Chat mix, which is what the "
                                f"check listens on, so there is nothing to measure. Bring that "
                                f"channel's fader up, or check it is not excluded from Chat under "
                                f"Routing, Output mixes.")}
        return {"ok": False, "running": False, "message": f"Could not listen to {src['label']} on "
                                                          f"its own: {why}"}
    w = meters.Watcher(endpoint["id"], speech.say,
                       clip_at=clip_warning.CLIP_AT, clip_gap=4.0,
                       warn_silence=False,        # a check is not a watch: no went-silent nagging
                       speak_every=_SPEAK_S,
                       scale=iso.level, label=src["label"])
    if not w.start():
        err = w.error or "Could not open the Chat stream."
        with _lock:
            _watcher, _iso = None, iso
        _stop(speak=False)             # the mix still has to go back
        return {"ok": False, "running": False, "message": err}
    with _lock:
        _run_id += 1
        run_id = _run_id
        _watcher, _iso = w, iso
        _label, _ends = src["label"], time.monotonic() + _CHECK_S
    threading.Timer(_CHECK_S, _stop, kwargs={"run_id": run_id}).start()
    # A BEEP RATHER THAN A SENTENCE to say it has started. The check speaks a level within a second
    # and a half, so anything said here is read over the first reading - and this is the moment the
    # user starts talking, which is not a moment to be listening to the computer.
    meters.cue("start")
    return {"ok": True, "running": True, "seconds": _CHECK_S, "channel": src["label"],
            "message": win, "level_note": meters.level_note(w.volume_db)}
