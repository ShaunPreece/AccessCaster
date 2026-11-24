"""AccessCaster - level metering, read from the computer rather than from the desk.

The desk publishes no meter values over its USB control channel, but it is also a USB audio
interface, and the system meters every audio endpoint. So this reads the peak of the desk's own
streams (Main Stereo, Chat, Secondary, Multitrack), and judges and speaks it.

Limits:
  - It is a PEAK per ENDPOINT, not per channel. "Is Mic 2 live" is answerable only when Mic 2 is
    alone on a stream (the per-channel check builds such a mix).
  - It is linear 0..1 amplitude, not a calibrated reading of the desk's own meters, and will not
    agree with them exactly.

The platform supplies:
  - `list_endpoints()`: every audio endpoint the system knows about, as dicts of "id", "name",
    "flow" (`PLAYBACK` or `RECORDING`), "state" (in words) and "active". Never raises.
  - `remote_session()`: True when this session cannot reach the console's audio devices.
  - `EndpointReader(endpoint_id)`: one endpoint held open for polling, made, read and closed on ONE
    thread. `name`; `is_capture`; `wake_failed`, True when an input's level cannot be read because
    recording from it could not be started; `peak()`, 0 to 1, raising once the endpoint has gone;
    `level()`, the system's own (level in dB, muted) for it, None when it has none, raising when it
    cannot be read; `close()`.
  - `read_level(endpoint_id)`: {"name", "db", "range": (min dB, max dB, step) or None when the
    driver reports none, "muted"}. Raises.
  - `write_mute(endpoint_id, muted)` and `write_level(endpoint_id, db)`: set it and return what
    reads back. Raise.
"""
from __future__ import annotations

import math
import sys
import threading
import time

import diagnostics
import speech
import write_guard
from wording import OS_NAME

if sys.platform == "win32":
    from win.meters import (EndpointReader, list_endpoints, read_level, remote_session,
                            write_level, write_mute)
elif sys.platform == "darwin":
    from mac.meters import (EndpointReader, list_endpoints, read_level, remote_session,
                            write_level, write_mute)
else:
    raise ImportError(f"No level meters for {sys.platform}: the Windows ones are win/meters.py")

PLAYBACK, RECORDING = "playback", "recording"

# The desk's own streams, matched by substring: RODE spell the product with a slashed O in some
# endpoint names and not others.
DESK_HINTS = ("rodecaster", "rødecaster")


def is_desk_endpoint(name: str) -> bool:
    low = (name or "").lower()
    return any(h in low for h in DESK_HINTS)


def desk_streams() -> dict:
    """What the app can meter right now. With nothing to meter, the reason distinguishes a remote
    session (nothing is wrong) from a console with no desk audio (unplugged, or the driver).
    Transfer mode leaves the desk's audio devices in place."""
    endpoints = list_endpoints()
    desk = [e for e in endpoints if is_desk_endpoint(e["name"])]
    active = [e for e in desk if e["active"]]
    if active:
        return {"ok": True, "streams": active, "reason": "",
                "all_desk": desk, "remote": remote_session()}
    if remote_session():
        # Only Windows reports a remote session: every other feature talks HID, which works from
        # any session.
        return {"ok": False, "streams": [], "all_desk": desk, "remote": True,
                "reason": ("Level meters need the RODECaster's audio streams, and Windows gives "
                           "each Remote Desktop session its own audio devices: from a remote "
                           "session the desk's streams are not there to read. Everything else in "
                           "this app works over Remote Desktop because it talks to the desk over "
                           "USB directly. Run the app at the computer itself to use the meters.")}
    return {"ok": False, "streams": [], "all_desk": desk, "remote": False,
            "reason": (f"{OS_NAME} is not reporting any RODECaster audio streams. That usually "
                       "means the desk's USB audio is not connected.")}


# How often a running Watcher re-reads the system's endpoint level.
CEILING_REFRESH_S = 2.0

# A ceiling at or below this is not believed and becomes 1.0. Clamping it up instead (`max(0.05,
# c)`) turns a failed volume read into a clip threshold of 0.05, announcing a whisper as clipping;
# for a warning that interrupts a take, a miss is cheaper than a false alarm.
MIN_TRUSTED_CEILING = 0.05


def reading_scale(volume_db: float, is_capture: bool) -> tuple:
    """(the system level that scales a meter reading, the ceiling it gives): what a true
    full-scale signal reads.

    AN OUTPUT'S METER READS BEFORE THE SYSTEM'S VOLUME, AN INPUT'S AFTER IT. The input's level
    scales its meter: at -1.08 dB a true full-scale signal reads 10^(-1.08/20) = 0.883, so a fixed
    clip threshold of 0.99 is unreachable. On the desk's Main Stereo pair a -6 dBFS tone reads
    0.500 on the output at both -6.3 and -18.3 dB, while the input's reading falls 12 dB with its
    level. Scaling an output by its volume calls ordinary speech clipping, so an output is always
    read at full scale. (Measured on Windows.)"""
    if not is_capture:
        return 0.0, 1.0
    return volume_db, min(1.0, 10.0 ** (volume_db / 20.0))


def trusted_ceiling(ceiling: float) -> float:
    """The ceiling to use. Shared by describe, verdict and Watcher so they cannot disagree."""
    try:
        c = float(ceiling)
    except (TypeError, ValueError):
        return 1.0
    return c if MIN_TRUSTED_CEILING < c <= 1.0 else 1.0


class Meter:
    """One endpoint's meter, held open for polling. Create, read and close it on ONE thread
    (`EndpointReader`).

    `wake_failed` says an input could not be started, so a reading of 0 says nothing about the
    microphone. The system's own level and mute are followed with `refresh_ceiling`: a muted
    endpoint reads 0, and must be reported as a mute rather than as a silent microphone.
    """

    def __init__(self, endpoint_id: str):
        self.id = endpoint_id
        self._reader = EndpointReader(endpoint_id)
        self.name = self._reader.name
        self.is_capture = self._reader.is_capture
        self.wake_failed = self._reader.wake_failed
        self.volume_db = 0.0
        self.ceiling = 1.0
        self.muted = False
        self.refresh_ceiling()

    def refresh_ceiling(self) -> float:
        """Re-read the endpoint's level and mute, and return the ceiling (`reading_scale`).
        Re-read rather than cached, because the system level is a live slider anything can move,
        and the clip threshold follows it. An endpoint with no level keeps unity."""
        try:
            level = self._reader.level()
            if level is not None:
                db, muted = level
                self.volume_db, self.ceiling = reading_scale(db, self.is_capture)
                self.muted = muted
        except Exception as exc:
            # The previous ceiling stands, so the clip threshold may be stale.
            diagnostics.event("meters.volume_read_failed", str(exc), endpoint=self.name)
        return self.ceiling

    def peak(self) -> float:
        """The current peak, 0 to 1. Raises when the endpoint has gone (the desk unplugged, or its
        USB mode changed), which a caller must not read as silence."""
        return self._reader.peak()

    def close(self):
        self._reader.close()


def cue(kind: str, despite_off: bool) -> None:
    """The tone marking the start or end of a timed step, "start" or "stop" (`tones.METER_CUES`),
    on the announcer's output. Returns once it has played, so nobody talks over their own start
    signal. `despite_off` plays it with announcements off, as the answer to a press in the window;
    a caller passes False while a recording has suspended the announcer, so it stays out of the
    take."""
    speech.tone_and_wait(f"meter_{kind}", despite_off=despite_off)


# The line between silence and signal: -60 dBFS, about a live mic's floor in a quiet room.
SILENCE = 0.001
# Below this a spoken level says "quiet" once and then nothing: under -50 dBFS is a room or a
# noise floor, not anything worth riding a fader to.
QUIET = 10 ** (-50 / 20)
# The bands `describe` and `verdict` both sort a peak into.
NEAR_CLIP = 0.89                 # about -1 dBFS
HOT = 0.5                        # about -6 dBFS
HEALTHY = 0.126                  # about -18 dBFS


def level_note(volume_db: float) -> str:
    """A sentence about the system's own level for this endpoint, or "" when it is close to unity.

    The desk's capture endpoints map Windows' percentage slider as measured: 0% is -96 dB, 50% is
    -1.08 dB, about 55% is unity, 75% is +7.67 dB, 100% is +30 dB. So the common advice of 75%
    makes anything the desk sends above -7.67 dBFS clip in Windows while the desk is fine. The
    advice points at the app's dB control, which can land exactly on unity where the slider cannot.
    """
    if volume_db > 1.0:
        return (f"{OS_NAME} is adding {volume_db:.1f} dB to this input, so it will clip in {OS_NAME} "
                f"before the desk does. Use Set it to no change below, then set your gain on the "
                f"desk itself.")
    if volume_db < -3.0:
        return (f"{OS_NAME} is taking {abs(volume_db):.1f} dB off this input, so everything reads "
                f"lower here than the desk is really sending.")
    return ""


# The band the level control offers, narrower than the driver's. The desk's capture endpoints report
# -96 to +30 dB in 1.5 dB steps and its playback endpoints -96 to 0, but -96 dB is off, and a control
# that can silently mute every recording on the machine is not one to offer unseen.
OFFERED_LEVEL_MIN_DB = -30.0
OFFERED_LEVEL_MAX_DB = 30.0

# For an endpoint the system cannot level (RODE's Virtual Devices outputs): says where the level is.
FIXED_LEVEL_NOTE = (f"{OS_NAME} cannot change this device's level. Use the channel's fader on the "
                    "desk, or this app's own level control.")


def _offered_range(level_range) -> tuple:
    """(min dB, max dB, step, fixed) the control offers: the driver's range narrowed to the band
    above. Shared by the reader and the writer so they cannot disagree.

    `fixed` is a DEGENERATE range, not a missing one. RODE's Virtual Devices endpoint `System
    (RØDECaster)` reports min 0, max 0, has no level at all, and refuses a write with "the parameter
    is incorrect", so it gets no slider. A driver that reports no range at all is unknown, and gets
    the band.
    """
    lo, hi, step = OFFERED_LEVEL_MIN_DB, OFFERED_LEVEL_MAX_DB, 0.5
    if level_range is None:
        return lo, hi, step, False
    r_lo, r_hi, r_step = level_range
    if r_hi <= r_lo:
        return r_lo, r_lo, 0.5, True
    lo = max(OFFERED_LEVEL_MIN_DB, r_lo)
    hi = min(OFFERED_LEVEL_MAX_DB, r_hi)
    if r_step > 0:
        step = r_step
    return lo, hi, step, False


def endpoint_volume(endpoint_id: str) -> dict:
    """The system's own level for one endpoint, in dB (see `level_note` for why not percent), with
    the range the control offers."""
    try:
        got = read_level(endpoint_id)
        lo, hi, step, fixed = _offered_range(got["range"])
        return {"ok": True, "id": endpoint_id, "name": got["name"], "db": got["db"],
                "min_db": lo, "max_db": hi, "step_db": step, "muted": got["muted"],
                "fixed": fixed,
                "note": (FIXED_LEVEL_NOTE if fixed else level_note(got["db"]))}
    except Exception as exc:
        return {"ok": False,
                "message": f"Could not read the {OS_NAME} level for that stream: {exc}"}


def set_endpoint_mute(endpoint_id: str, muted: bool) -> dict:
    """Mute or unmute the endpoint in the system, from a control the user pressed, and read it
    back. A muted input reads exactly like a dead microphone."""
    try:
        write_guard.refuse_in_tests(f"change {OS_NAME} audio")
        got = write_mute(endpoint_id, bool(muted))
        return {"ok": got == bool(muted), "muted": got}
    except Exception as exc:
        return {"ok": False, "message": f"Could not change the {OS_NAME} mute: {exc}"}


def set_endpoint_volume_db(endpoint_id: str, db: float) -> dict:
    """Set the system's own level for one endpoint, in dB, from a control the user operates (it
    changes what every application on the machine records), and read it back: the desk's `... Chat`
    input silently stays at -1.08 dB whatever is written, by dB or by scalar."""
    try:
        write_guard.refuse_in_tests(f"change {OS_NAME} audio")
        lo, hi, _step, fixed = _offered_range(read_level(endpoint_id)["range"])
        if fixed:
            # The driver's own refusal is "the parameter is incorrect", which names nothing
            # to act on.
            return {"ok": False, "fixed": True, "message": FIXED_LEVEL_NOTE}
        want = max(lo, min(hi, float(db)))
        got = write_level(endpoint_id, want)
        return {"ok": abs(got - want) < 0.2, "db": got, "wanted": want,
                "note": level_note(got)}
    except Exception as exc:
        return {"ok": False, "message": f"Could not set the {OS_NAME} level: {exc}"}


def dbfs(peak: float) -> str:
    """A peak spoken in dBFS. Never a percentage: the meter is linear amplitude, so "50 percent" is
    already -6 dBFS. "minus 6" rather than "-6", which a screen reader may read as "dash six"."""
    if peak <= 0:
        return "silence"
    db = 20.0 * math.log10(peak)
    if db <= -60:
        return "below minus 60 dB"
    return (f"minus {abs(db):.0f} dB" if db < -0.5
            else "0 dB" if db < 0.5 else f"plus {db:.0f} dB")


def describe(peak: float, active_fraction: float, name: str, wake_failed: bool = False,
             ceiling: float = 1.0, volume_db: float = 0.0, muted: bool = False,
             clip_at: float = 0.99) -> str:
    """A peak as a judgement first and a number second: live, too quiet, or about to clip. The
    bands follow the desk's own meters: green to about -18 dBFS, amber to -6, red above."""
    at = dbfs(peak)
    if muted:
        # First, because a muted endpoint reads 0 whatever the microphone is doing.
        return f"{name}: MUTED IN {OS_NAME}. Nothing recorded from it will have any sound."
    if peak <= SILENCE and wake_failed:
        return (f"{name}: no level can be read. {OS_NAME} only reports an input's level while "
                f"something is recording from it, and that could not be started here.")
    if peak <= SILENCE:
        return f"{name}: silent. Nothing is coming through."
    # Only the clip line is scaled by the ceiling; the bands below are judgements by ear.
    if peak >= clip_at * trusted_ceiling(ceiling):
        # With the system boosting the input, the desk's gain is the wrong knob.
        if volume_db > 1.0:
            return (f"{name}: CLIPPING at {at}, but {OS_NAME} is adding {volume_db:.1f} dB to this "
                    f"input. Use Set it to no change below before touching the desk.")
        return f"{name}: CLIPPING at {at}. Turn it down."
    if peak >= NEAR_CLIP:
        return f"{name}: very hot, {at}, close to clipping."
    if peak >= HOT:
        return f"{name}: strong, peaking at {at}."
    if peak >= HEALTHY:
        return f"{name}: healthy, peaking at {at}."
    if active_fraction > 0.2:
        return f"{name}: live but quiet, peaking at only {at}. It would record thin."
    return f"{name}: almost nothing, peaking at {at}."


def verdict(peak: float, active_fraction: float, volume_db: float = 0.0,
            ceiling: float = 1.0, clip_at: float = 0.99) -> str:
    """The advice at the end of a check: is this usable, and which control to turn if not (the
    system's level when the system is boosting the input, otherwise the desk's gain). `describe`
    reports; this decides."""
    boosted = volume_db > 1.0
    down = (f"Turn down the {OS_NAME} level above, which is adding {volume_db:.1f} dB to this input"
            if boosted else "Turn the gain down on the desk")
    up = ("Turn up the gain on the desk" if not boosted
          else f"Turn up the gain on the desk rather than the {OS_NAME} level")
    if peak <= SILENCE:
        return "Nothing came through at all."
    if peak >= clip_at * trusted_ceiling(ceiling):
        return f"Audio is CLIPPING. {down}."
    if peak >= NEAR_CLIP:
        return f"Audio is very close to clipping. {down}."
    if peak >= HOT:
        return f"Audio is usable but hot. {down} a little to be safe."
    if peak >= HEALTHY:
        return "Audio is good."
    if active_fraction > 0.2:
        return f"Audio is too quiet to record well. {up}."
    return f"Almost nothing came through. Check the channel is live, then {up[0].lower()}{up[1:]}."


class Watcher:
    """A background thread that watches one stream and speaks only when something needs saying:

      CLIPPING          - at once, then held for `clip_gap` seconds so a hot passage is one warning.
      WENT SILENT/LIVE  - after `silence_after` seconds of nothing, so a breath is not an event.
      THE LEVEL ITSELF  - every `speak_every` seconds, the loudest since the last one spoken;
                          off by default, since a running commentary is unusable while talking.
                          Below `QUIET` it is not spoken.

    Every threshold is a setting, because they are judgements by ear."""

    def __init__(self, endpoint_id: str, say, *, clip_at=0.99, clip_gap=4.0,
                 warn_silence=True, silence_after=5.0, speak_every=0.0, poll=0.05,
                 clip_sound=True, scale=1.0, label=""):
        self.endpoint_id = endpoint_id
        self._say = say
        # scale and label serve the per-channel check (level_check.action_channel_level_check), which
        # hears one channel through a temporary post-fader Chat mix. scale is that cross-point's
        # level: dividing by it puts every reading, and the clip test, back in channel terms, as
        # auto gain does; a microphone clips in the preamp, ahead of the fader. label is the name
        # spoken in place of the system endpoint's.
        self.scale = max(0.05, float(scale or 1.0))
        self.label = str(label or "")
        self.clip_sound = bool(clip_sound)
        self.clip_at = float(clip_at)
        self.clip_gap = float(clip_gap)
        self.warn_silence = bool(warn_silence)
        self.silence_after = float(silence_after)
        self.speak_every = float(speak_every)
        self.poll = float(poll)
        self._stop = threading.Event()
        self._opened = threading.Event()     # set once the meter is open, or has failed to open
        self._thread = None
        self.error = ""
        # Live readings for the page. peak_hold decays like a real meter; peak_max is the true
        # maximum over the run, which a check reports at the end.
        self.peak = 0.0
        self.peak_hold = 0.0
        self.peak_max = 0.0
        self.name = ""
        # Copied from the Meter once open, and the last three followed live.
        self.wake_failed = False
        self.ceiling = 1.0
        self.volume_db = 0.0
        self.muted = False

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._opened.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._opened.wait(timeout=5.0)
        return not self.error

    def stop(self):
        self._stop.set()
        t = self._thread
        if t:
            t.join(timeout=2.0)
        self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self):
        m = None
        try:
            m = Meter(self.endpoint_id)
            self.name = self.label or m.name
            self.wake_failed = m.wake_failed
            self.ceiling = m.ceiling
            self.volume_db = m.volume_db
            self.muted = m.muted
            clip_level = self.clip_at * trusted_ceiling(m.ceiling)
            last_ceiling = time.monotonic()
        except Exception as exc:
            self.error = f"Could not open that stream: {exc}"
            return
        finally:
            self._opened.set()
        last_clip = float("-inf")
        last_level = time.monotonic()
        last_sound = time.monotonic()
        was_silent = False
        loudest = 0.0           # since the level was last spoken
        quiet_said = False
        try:
            while not self._stop.is_set():
                v = m.peak()
                if self.scale != 1.0:
                    v = min(1.0, v / self.scale)
                self.peak = v
                self.peak_hold = max(self.peak_hold * 0.995, v)   # a slow decay, like a real meter
                self.peak_max = max(self.peak_max, v)
                loudest = max(loudest, v)
                now = time.monotonic()

                if (now - last_ceiling) >= CEILING_REFRESH_S:
                    last_ceiling = now
                    self.ceiling = m.refresh_ceiling()
                    self.volume_db = m.volume_db
                    self.muted = m.muted
                    clip_level = self.clip_at * trusted_ceiling(self.ceiling)

                if v >= clip_level and (now - last_clip) >= self.clip_gap:
                    last_clip = now
                    words = f"{self.name} clipping"
                    if self.clip_sound:
                        # The rasp first, since it arrives whole; the words then say which stream.
                        # On a thread, so the meter loop never waits for the cue.
                        threading.Thread(target=self._rasp_then_speak, args=(words,),
                                         daemon=True).start()
                    else:
                        self._speak(words)

                if v > SILENCE:
                    if was_silent and self.warn_silence:
                        was_silent = False
                        self._speak(f"{self.name} is live again")
                    last_sound = now
                elif (self.warn_silence and not was_silent and not self.wake_failed
                        and (now - last_sound) >= self.silence_after):
                    # An input whose engine could not start reads 0 forever: never call it silent.
                    was_silent = True
                    self._speak(f"{self.name} has gone silent")

                if self.speak_every > 0 and (now - last_level) >= self.speak_every:
                    last_level = now
                    # A bare level, short enough to ride a fader by. The loudest since the last
                    # one, not peak_hold: the hold takes half a minute to fall, so it goes on
                    # reading a voice long after the voice has stopped.
                    if loudest >= QUIET:
                        quiet_said = False
                        self._speak(dbfs(loudest))
                    elif not quiet_said:
                        quiet_said = True
                        # A watch says it has gone silent itself, a few seconds later.
                        if not self.warn_silence:
                            self._speak("quiet")
                    loudest = 0.0

                self._stop.wait(self.poll)
        except Exception as exc:
            # Must set self.error: a thread that dies with no error reads as a finished run, and
            # a channel that fell over is summarised as a quiet one.
            self.error = f"The meter stopped part-way through: {exc}"
        finally:
            m.close()

    def _speak(self, text: str):
        try:
            self._say(text)
        except Exception:
            pass          # a meter must never be able to take the announcer, or the app, down

    def _rasp_then_speak(self, text: str):
        """The clipping cue, then `text`, which would replace the cue if queued while it waits. The
        cue follows the announcer like the words, so a recording that suspends it keeps both out of
        the take."""
        speech.tone_and_wait("meter_clip")
        self._speak(text)
