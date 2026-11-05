"""
AccessCaster - the main mix clip warning, the window's one background meter: a switch under
Announcements that says "Main mix clipping" when the desk's main recording stream reaches full
scale. Fixed thresholds, no stream to choose. Its words go through speech.say, so they follow the
announcer, recordings included. The metering itself is meters.py.
"""

from __future__ import annotations

import threading

import diagnostics
import meters
import settings_file
import speech

# Where a clip warning fires, as a 0..1 peak: TRUE FULL SCALE. Do not lower it to catch more: this
# desk's compressor and limiter park peaks just under full scale, so -1 dB is where normal processed
# speech lives, and a warning there fires on every sentence.
CLIP_AT = 1.0


# A SUPERVISOR KEEPS IT RUNNING: the endpoint goes away whenever the desk does (a USB drop, Transfer
# Mode), the watcher's thread then ends, and the next pass opens it again.
_CLIP_RETRY_S = 10.0
_clip_watcher = None
_clip_lock = threading.Lock()
_clip_wake = threading.Event()
_clip_supervisor = None


def clip_warning_on() -> bool:
    return bool(settings_file.load().get("clip_warning", False))


def _main_mix_stream(found: dict):
    """The desk's main recording endpoint, or its first recording endpoint, or None."""
    inputs = [s for s in found.get("streams") or []
              if s.get("flow") == meters.FLOW_NAMES[meters.CAPTURE]]
    main = [s for s in inputs if "main stereo" in s["name"].lower()]
    return (main or inputs or [None])[0]


def _clip_start() -> str:
    """Open the watcher on the main mix. Returns "" once it runs, else why it could not."""
    global _clip_watcher
    found = meters.desk_streams()
    if not found["ok"]:
        return found["reason"] or "The desk's audio is not available."
    stream = _main_mix_stream(found)
    if stream is None:
        return "The desk is offering no recording stream."
    w = meters.Watcher(stream["id"], speech.say, clip_at=CLIP_AT,
                       warn_silence=False, clip_sound=False, label="Main mix")
    if not w.start():
        return w.error or "The meter could not start."
    with _clip_lock:
        _clip_watcher = w
    return ""


def _clip_stop() -> None:
    global _clip_watcher
    with _clip_lock:
        w, _clip_watcher = _clip_watcher, None
    if w is not None:
        w.stop()


def _clip_supervise() -> None:
    """Keep the watcher matching the setting, retrying every `_CLIP_RETRY_S` while the desk's
    audio is away. A failure is logged once until it changes."""
    said = ""
    while True:
        with _clip_lock:
            running = bool(_clip_watcher and _clip_watcher.running)
        if clip_warning_on():
            if not running:
                why = _clip_start()
                if why and why != said:
                    diagnostics.event("meters.clip_watch", "could not start", reason=why)
                said = why
        elif running:
            _clip_stop()
        _clip_wake.wait(_CLIP_RETRY_S)
        _clip_wake.clear()


def start_clip_warning() -> None:
    """Start the supervisor. It opens nothing while the setting is off."""
    global _clip_supervisor
    if _clip_supervisor is None:
        _clip_supervisor = threading.Thread(target=_clip_supervise, daemon=True,
                                            name="clip-warning")
        _clip_supervisor.start()


def action_set_clip_warning(on: bool) -> dict:
    """Turn the main mix clip warning on or off."""
    on = bool(on)
    settings_file.update(clip_warning=on)
    _clip_wake.set()
    if not on:
        return {"ok": True, "clip_warning": False, "message": "Clipping will not be announced."}
    found = meters.desk_streams()
    later = "" if found["ok"] else (f" It starts once the desk's audio is available: "
                                    f"{found['reason']}")
    return {"ok": True, "clip_warning": True,
            "message": "The main mix will be announced when it clips." + later}
