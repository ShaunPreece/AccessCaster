"""Offline checks for the PER-CHANNEL level check (Channels > any channel).

NO DEVICE NEEDED. What this pins is the reasoning, not the plumbing:

  * the reading is corrected for the post-fader cross-point, so a check AGREES with auto gain
    instead of arguing with it the moment a fader is anywhere but unity;
  * it is called by the CHANNEL's name out loud, never by the Windows endpoint it is really
    listening to ("Microphone (RODECaster Pro II Chat) clipping" while you check Mic 1 names the
    wrong thing entirely);
  * Chat is refused, with the mechanism said out loud rather than a bare "no";
  * the check lives ONLY on the channel, with no stream-wide check left behind;
  * one Chat isolation at a time, so a level check and auto gain cannot overlap.

and for the main mix clip warning (App settings, Announcements): it watches the main recording
stream, never a playback one, says nothing but clipping, and its switch is saved and reported.

    python DevTools/test_level_check.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
import autogain
import chat_isolation
import clip_warning
import desk_access
import devices
import level_check
import meters
import rcp_transfer
import settings_file
import speech
from _testkit import check, check_in, check_not_in, patched

# PIN THE DESK. This suite asserts Pro II channel LABELS ("USB 2 (Secondary)"), so with a Duo
# attached the Duo's shorter names would read as a regression. Offline tests must not depend on
# what happens to be plugged into the machine running them.
devices.force_device(devices.PRO2)

ROOT = Path(__file__).resolve().parent.parent


class FakeWatcher:
    """Only what level_check._summary reads. The real one needs an audio endpoint."""

    def __init__(self, peak_max, name="Mic 2", volume_db=0.0, ceiling=1.0,
                 muted=False, wake_failed=False, clip_at=0.99):
        self.peak_max = peak_max
        self.name = name
        self.volume_db = volume_db
        self.ceiling = ceiling
        self.muted = muted
        self.wake_failed = wake_failed
        self.clip_at = clip_at


print("\nTHE FADER IS DIVIDED OUT (scale), so a check and auto gain cannot disagree")
w = meters.Watcher("", lambda *_a: None, scale=0.5, label="Mic 2")
check("a post-fader cross-point at half level is carried as the scale", w.scale, 0.5)
check("and the label is what it will be CALLED out loud", w.label, "Mic 2")
# The floor matters: ChatIsolation already refuses below 0.05, but a scale of 0 here would divide
# by zero on the first poll and take the whole check down with it.
check("a zero scale falls back to no correction rather than dividing by zero",
      meters.Watcher("", lambda *_a: None, scale=0).scale, 1.0)
check("an absurdly small scale is floored, not honoured",
      meters.Watcher("", lambda *_a: None, scale=0.0001).scale, 0.05)
check("no scale and no label means no correction and no name",
      (meters.Watcher("", lambda *_a: None).scale, meters.Watcher("", lambda *_a: None).label),
      (1.0, ""))


def one_poll(raw: float, scale: float) -> float:
    """What a Watcher with `scale` reports after one poll of a meter reading `raw`."""
    w = meters.Watcher("", lambda *_a: None, scale=scale, label="Mic 2")
    w.clip_sound = False

    class OnePoll:
        name, wake_failed, ceiling, volume_db, muted = "stub", False, 1.0, 0.0, False

        def __init__(self, _endpoint_id):
            pass

        def peak(self):
            w._stop.set()           # this poll is the last
            return raw

        def refresh_ceiling(self):
            return 1.0

        def close(self):
            pass

    with patched((meters, {"Meter": OnePoll})):
        w._run()
    return w.peak


# A fader 6 dB down halves the amplitude, and dividing by the cross-point level puts the reading
# back where the channel actually is.
check("a channel reading 0.25 through a half-level cross-point is 0.5 at the channel",
      one_poll(0.25, 0.5), 0.5)
check("and nothing is ever reported above full scale", one_poll(0.9, 0.5), 1.0)

print("\nTHE SUMMARY NAMES THE CHANNEL, AND ENDS WITH ADVICE")
msg = level_check._summary(FakeWatcher(0.35))
check_in("it names the channel, not the Windows endpoint", "Mic 2", msg)
check_not_in("the endpoint's own name is nowhere in it", "RODECaster Pro II Chat", msg)
# SPOKEN dB, not "-9 dBFS": this sentence goes to a speech engine, and meters.dbfs writes it the
# way it should be heard ("minus 9 dB"), not the way it would be printed.
check_in("it gives the peak as a level, spoken", "peaked at minus 9 dB", msg)
check_in("and a verdict, because a number alone is half an answer", "good", msg)
hot = level_check._summary(FakeWatcher(0.95))
check_in("a hot channel is told which knob to turn", "gain", hot)
check_in("and that it is the UNIT's knob, not Windows'", "on the desk", hot)
silent = level_check._summary(FakeWatcher(0.0))
check_in("silence is reported as silence rather than as a level", "nothing", silent)
muted = level_check._summary(FakeWatcher(0.0, muted=True))
check_in("a Windows mute is NAMED, since it reads exactly like a dead microphone",
         "mute", muted)

print("\nWHICH CHANNELS IT IS OFFERED ON")
src, why = level_check._source("Mic1")
check("Mic 1 is the first matrix source", (src or {}).get("index"), 0)
check("and it carries the label the check will speak", (src or {}).get("label"), "Mic 1")
check("a mic needs no explanation", why, "")
src, why = level_check._source("USB2")
check("a non-mic channel is offered too: nobody chose the level a USB feed arrives at",
      (src or {}).get("label"), "USB 2 (Secondary)")
src, why = level_check._source("Chat")
check("Chat is refused", src, None)
# THE REASON IS THE MECHANISM, not a rule we chose: the check listens on the Chat OUTPUT, and that
# output is mix-minus, so the Chat source is deliberately not in it.
check_in("and the refusal explains WHY, in terms of the mix", "output", why)
check_in("naming the caller, so it reads as a fact about mix-minus rather than a limitation",
         "caller", why)
src, why = level_check._source("Nonsense")
check("an unknown channel is refused", src, None)
check_in("with the channel name in the message", "Nonsense", why)

print("\nTHE WINDOW AND THE CHECK AGREE ABOUT CHAT")
tabs_src = (ROOT / "Project" / "native" / "channel_tabs.py").read_text(encoding="utf-8")
check_in("the window does not even build the button for Chat", 'if name != "Chat"', tabs_src)

print("\nTHE CHECK IS PER CHANNEL ONLY: NO STREAM-WIDE CHECK, NOT EVEN A HIDDEN ONE")
check("there is no action for one", hasattr(level_check, "action_meter_check"), False)

print("\nA RUN WHOSE WATCHER HAS DIED, AND A STALE TIMER")
# When a run's watcher thread dies mid-run, the next press must stop that run rather than start a
# second one, or the first run's Chat isolation is dropped with nothing left to put it back.
spoken = []
meters.cue = lambda *_a, **_k: None            # no beeps in an offline run
speech.say = lambda text, *a, **k: spoken.append(text)

level_check._watcher = None
level_check._iso = object()             # a run with nothing alive but something to restore
level_check._label = "Mic 4"
check("an isolation with no watcher still counts as a run in progress",
      level_check._state()["running"], True)
check("and it still names the channel, so a stop press can say what it stopped",
      level_check._state()["channel"], "Mic 4")
# Quit asks work_in_progress, so a run it cannot see is quit through with the Chat mix isolated.
desk_access.register_work(level_check.work_in_progress)
check("the quit warning names a running level check", desk_access.work_in_progress(),
      "A level check is running.")
service_src = (ROOT / "Project" / "service.py").read_text(encoding="utf-8")
check_in("and the app registers it", "register_work(level_check.work_in_progress)", service_src)
check_in("with auto gain beside it", "register_work(autogain_actions.work_in_progress)",
         service_src)
level_check._iso = None
level_check._label = ""
check("with nothing to restore, nothing is running", level_check._state()["running"], False)

# A stale end-timer must be a no-op. Press stop at 8 s, start again at 10 s, and the first run's
# 30-second timer would otherwise arrive 20 s into the second run and kill it.
level_check._watcher = FakeWatcher(0.3, name="Mic 2")
level_check._iso = None
level_check._run_id = 7
check("a timer from an older run stops nothing",
      level_check._stop(speak=False, run_id=6), "")
check("and the run it did not belong to is still going",
      level_check._state()["running"], True)
check("the timer for THIS run does end it",
      "Mic 2" in level_check._stop(speak=False, run_id=7), True)
check("after which there is nothing left in progress",
      level_check._state()["running"], False)

# A WATCHER THAT BROKE MID-RUN is not a quiet channel, and must not be summarised as one.
broken = FakeWatcher(0.3, name="Mic 2")
broken.error = "The meter stopped part-way through: device disconnected"
level_check._watcher = broken
level_check._iso = None
level_check._run_id = 8
msg = level_check._stop(speak=True)
check_in("the failure is reported instead of a level", "stopped part-way through", msg)
check_in("and it says outright that nothing was measured", "no level is being reported", msg)
check_not_in("no peak is quoted from a run that broke", "peaked at", msg)
check("and it was SPOKEN, not just returned", spoken[-1], msg)

print("\nTHE REFUSAL A USER WILL ACTUALLY HIT")
# A channel whose fader is down does not reach the Chat mix at all, so this refusal is the common
# path rather than an edge case, and it has to say what to DO instead of describing the mixer.
check_src = (ROOT / "Project" / "level_check.py").read_text(encoding="utf-8")
check_in("the start path recognises the cross-point refusal",
         "not reaching the Chat mix", check_src)
# The phrase is split across source lines, so match the words that carry the instruction.
check_in("and answers it with an instruction about the fader", "fader up", check_src)
check_in("naming where the exclusion would be, since that is the other cause",
         "Routing, Output mixes", check_src)

print("\nTHE CHECK'S STATE")
st = level_check._state()
check("nothing is running to begin with", st["running"], False)
check("and it says so with no channel named", st["channel"], "")

print("\nAUTO GAIN'S VERDICT ON A TAKE WITH NOTHING USABLE")
short = autogain.verdict({"loud_db": -60.0, "max_db": -20.0})
check("a short sound is not used", short["ok"], False)
check_in("and is called a short sound", "short sound", short["reason"])
check_not_in("not a missing microphone", "plugged in", short["reason"])
none = autogain.verdict({"loud_db": -60.0, "max_db": -60.0})
check_in("nothing at all says to check the microphone", "plugged in", none["reason"])

print("\nONE ISOLATION OWNS THE CHAT MIX AT A TIME")
# A level check and auto gain both isolate the Chat mix. A second isolation entered while the first
# is live would snapshot the isolated mix as its "before" and put that back afterwards.
desk = {"mode": 2, "excl": {}, "fail": False}


def fake_snapshot(self):
    return (desk["mode"], {s: desk["excl"].get(s, False) for s in self.sources},
            {s: {"level": 1.0} for s in self.sources})


def fake_exclude(_output, src, on):
    if desk["fail"]:
        raise RuntimeError("the desk did not answer")
    desk["excl"][src] = on


def try_enter(keep):
    try:
        return chat_isolation.ChatIsolation(keep).__enter__()
    except RuntimeError as exc:
        return str(exc)


with patched((chat_isolation.ChatIsolation, {"_snapshot": fake_snapshot}),
             (rcp_transfer, {"set_cross_point_excluded": fake_exclude,
                             "set_output_routing": lambda *_a: None}),
             (chat_isolation.time, {"sleep": lambda *_a: None})):
    first = try_enter(0)
    check("a second isolation entered while one is live is refused", try_enter(1),
          chat_isolation.BUSY)
    check("the first puts the mix back", first.restore(), True)
    again = try_enter(1)
    check("after which the next one may listen", getattr(again, "keep", again), 1)
    desk["fail"] = True
    check("a restore the desk does not take fails", again.restore(), False)
    desk["fail"] = False
    third = try_enter(2)
    check("the next isolation puts the failed one back before taking the mix",
          (again.restored, getattr(third, "keep", third)), (True, 2))
    third.restore()

print("\nTHE MAIN MIX CLIP WARNING")
streams = {"streams": [
    {"id": "out", "name": "Speakers (RODECaster Pro II Main Stereo)", "flow": "playback"},
    {"id": "chat", "name": "Microphone (RODECaster Pro II Chat)", "flow": "recording"},
    {"id": "main", "name": "Microphone (RODECaster Pro II Main Stereo)", "flow": "recording"}]}
check("it watches the main recording stream, never what this computer plays to the desk",
      clip_warning._main_mix_stream(streams)["id"], "main")
check("with no main stream, the first recording stream",
      clip_warning._main_mix_stream({"streams": streams["streams"][:2]})["id"], "chat")
check("with no recording stream, nothing",
      clip_warning._main_mix_stream({"streams": streams["streams"][:1]}), None)
made = {}


class FakeClipWatcher:
    def __init__(self, endpoint_id, say, **kw):
        made.update(kw, endpoint=endpoint_id)
        self.running = True

    def start(self):
        return True

    def stop(self):
        self.running = False


with patched((meters, {"desk_streams": lambda: dict(streams, ok=True, reason=""),
                              "Watcher": FakeClipWatcher})):
    check("a start opens it", clip_warning._clip_start(), "")
check("on the main stream", made["endpoint"], "main")
check("saying nothing about silence or the level, and playing no sound of its own",
      (made["warn_silence"], made.get("speak_every", 0.0), made["clip_sound"]),
      (False, 0.0, False))
check("and called the main mix out loud", made["label"], "Main mix")
clip_warning._clip_stop()
saved = {}
with patched((settings_file, {"update": lambda **kw: saved.update(kw)}),
             (meters, {"desk_streams": lambda: {"ok": False, "streams": [],
                                                       "reason": "Remote Desktop."}})):
    on = clip_warning.action_set_clip_warning(True)
    off = clip_warning.action_set_clip_warning(False)
check("the switch is saved", saved, {"clip_warning": False})
check_in("on with no audio says when it will start", "once the desk's audio is available",
         on["message"])
check("off says so", off["message"], "Clipping will not be announced.")
clip_warning._clip_wake.clear()
print("")
sys.exit(_testkit.finish())
