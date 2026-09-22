"""Offline checks for the PER-CHANNEL level check (Channels > any channel).

NO DEVICE NEEDED. What this pins is the reasoning, not the plumbing:

  * the reading is corrected for the post-fader cross-point, so a check AGREES with auto gain
    instead of arguing with it the moment a fader is anywhere but unity;
  * it is called by the CHANNEL's name out loud, never by the Windows endpoint it is really
    listening to ("Microphone (RODECaster Pro II Chat) clipping" while you check Mic 1 names the
    wrong thing entirely);
  * Chat is refused, with the mechanism said out loud rather than a bare "no";
  * the check lives ONLY on the channel, so there is no Device > Levels endpoint left behind for a
    page to call.

    python DevTools/test_level_check.py
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401  (puts Project/ on sys.path)

import devices
import meters
import server

# PIN THE DESK. This suite asserts Pro II channel LABELS ("USB 2 (Secondary)"), so with a Duo
# attached it failed on the Duo's shorter names and looked like a regression. Offline tests must not
# depend on what happens to be plugged into the machine running them.
devices.force_device(devices.PRO2)

ROOT = Path(__file__).resolve().parent.parent
FAILS = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILS.append(f"{name}: got {got!r}, wanted {want!r}")
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")


def check_in(name, needle, haystack):
    ok = needle.lower() in (haystack or "").lower()
    if not ok:
        # TRUNCATED: one of these haystacks is the whole of server.py, and a failure printed
        # 366 KB of it, which buries the failure it is reporting.
        FAILS.append(f"{name}: {needle!r} not in {(haystack or '')[:160]!r}...")
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")


def check_not_in(name, needle, haystack):
    ok = needle.lower() not in (haystack or "").lower()
    if not ok:
        FAILS.append(f"{name}: {needle!r} IS in {(haystack or '')[:160]!r}...")
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")


class FakeWatcher:
    """Only what _level_check_summary reads. The real one needs an audio endpoint."""

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
# The floor matters: _ChatIsolation already refuses below 0.05, but a scale of 0 here would divide
# by zero on the first poll and take the whole check down with it.
check("a zero scale falls back to no correction rather than dividing by zero",
      meters.Watcher("", lambda *_a: None, scale=0).scale, 1.0)
check("an absurdly small scale is floored, not honoured",
      meters.Watcher("", lambda *_a: None, scale=0.0001).scale, 0.05)
check("no scale and no label means no correction and no name",
      (meters.Watcher("", lambda *_a: None).scale, meters.Watcher("", lambda *_a: None).label),
      (1.0, ""))
# The arithmetic itself, stated as the loop does it: a fader 6 dB down halves the amplitude, and
# dividing by the cross-point level puts the reading back where the channel actually is.
raw, level = 0.25, 0.5
check("a channel reading 0.25 through a half-level cross-point is 0.5 at the channel",
      min(1.0, raw / level), 0.5)
check("and nothing is ever reported above full scale",
      min(1.0, 0.9 / 0.5), 1.0)

print("\nTHE SUMMARY NAMES THE CHANNEL, AND ENDS WITH ADVICE")
msg = server._level_check_summary(FakeWatcher(0.35))
check_in("it names the channel, not the Windows endpoint", "Mic 2", msg)
check_not_in("the endpoint's own name is nowhere in it", "RODECaster Pro II Chat", msg)
# SPOKEN dB, not "-9 dBFS": this sentence goes to a speech engine, and meters.dbfs writes it the
# way it should be heard ("minus 9 dB"), not the way it would be printed.
check_in("it gives the peak as a level, spoken", "peaked at minus 9 dB", msg)
check_in("and a verdict, because a number alone is half an answer", "good", msg)
hot = server._level_check_summary(FakeWatcher(0.95))
check_in("a hot channel is told which knob to turn", "gain", hot)
check_in("and that it is the UNIT's knob, not Windows'", "on the unit", hot)
silent = server._level_check_summary(FakeWatcher(0.0))
check_in("silence is reported as silence rather than as a level", "nothing", silent)
muted = server._level_check_summary(FakeWatcher(0.0, muted=True))
check_in("a Windows mute is NAMED, since it reads exactly like a dead microphone",
         "mute", muted)

print("\nWHICH CHANNELS IT IS OFFERED ON")
src, why = server._chan_check_source("Mic1")
check("Mic 1 is the first matrix source", (src or {}).get("index"), 0)
check("and it carries the label the check will speak", (src or {}).get("label"), "Mic 1")
check("a mic needs no explanation", why, "")
src, why = server._chan_check_source("USB2")
check("a non-mic channel is offered too: nobody chose the level a USB feed arrives at",
      (src or {}).get("label"), "USB 2 (Secondary)")
src, why = server._chan_check_source("Chat")
check("Chat is refused", src, None)
# THE REASON IS THE MECHANISM, not a rule we chose: the check listens on the Chat OUTPUT, and that
# output is mix-minus, so the Chat source is deliberately not in it.
check_in("and the refusal explains WHY, in terms of the mix", "output", why)
check_in("naming the caller, so it reads as a fact about mix-minus rather than a limitation",
         "caller", why)
src, why = server._chan_check_source("Nonsense")
check("an unknown channel is refused", src, None)
check_in("with the channel name in the message", "Nonsense", why)

print("\nTHE UI AND THE SERVER AGREE ABOUT CHAT")
app_js = (ROOT / "Project" / "static" / "app.js").read_text(encoding="utf-8")
check_in("the page does not even build the button for Chat", 'ch.name !== "Chat"', app_js)

print("\nTHE OLD STREAM CHECK IS GONE, NOT JUST HIDDEN")
check("its endpoint is not in the POST routes",
      "/api/meters/check" in server.POST_ROUTES, False)
check("and there is no action left to call",
      hasattr(server, "action_meter_check"), False)
check_not_in("the page has no orphan button for it", "meter-check-btn", app_js)
index_html = (ROOT / "Project" / "static" / "index.html").read_text(encoding="utf-8")
check_not_in("nor any markup for one", 'id="meter-check-btn"', index_html)
# The stream dropdown STAYS: the background watch still meters one stream.
check_in("the stream chooser survives, for the watch", 'id="meter-stream"', index_html)
check_in("and the tab points at where the check went", "Channels page", index_html)

print("\nTHE TWO FAULTS A LIVE RUN FOUND ON")
# Both came out of one real sequence: a run's watcher thread died mid-run, and the next press of the
# button started a SECOND run instead of stopping the first - which dropped the first run's Chat
# isolation with nobody left holding a reference to put it back.
spoken = []
meters.cue = lambda *_a, **_k: None            # no beeps in an offline run
server._ann_say = lambda text, *a, **k: spoken.append(text)

server._chan_check = None
server._chan_check_iso = object()             # a run with nothing alive but something to restore
server._chan_check_label = "Mic 4"
check("an isolation with no watcher still counts as a run in progress",
      server._chan_check_state()["running"], True)
check("and it still names the channel, so a stop press can say what it stopped",
      server._chan_check_state()["channel"], "Mic 4")
server._chan_check_iso = None
server._chan_check_label = ""
check("with nothing to restore, nothing is running", server._chan_check_state()["running"], False)

# A stale end-timer must be a no-op. Press stop at 8 s, start again at 10 s, and the first run's
# 30-second timer would otherwise arrive 20 s into the second run and kill it.
server._chan_check = FakeWatcher(0.3, name="Mic 2")
server._chan_check_iso = None
server._chan_check_id = 7
check("a timer from an older run stops nothing",
      server._chan_check_stop(speak=False, run_id=6), "")
check("and the run it did not belong to is still going",
      server._chan_check_state()["running"], True)
check("the timer for THIS run does end it",
      "Mic 2" in server._chan_check_stop(speak=False, run_id=7), True)
check("after which there is nothing left in progress",
      server._chan_check_state()["running"], False)

# A WATCHER THAT BROKE MID-RUN is not a quiet channel, and must not be summarised as one.
broken = FakeWatcher(0.3, name="Mic 2")
broken.error = "The meter stopped part-way through: device disconnected"
server._chan_check = broken
server._chan_check_iso = None
server._chan_check_id = 8
msg = server._chan_check_stop(speak=True)
check_in("the failure is reported instead of a level", "stopped part-way through", msg)
check_in("and it says outright that nothing was measured", "no level is being reported", msg)
check_not_in("no peak is quoted from a run that broke", "peaked at", msg)
check("and it was SPOKEN, not just returned", spoken[-1], msg)

print("\nTHE REFUSAL A USER WILL ACTUALLY HIT")
# Measured on the desk: with Chat carrying Mic 1 only, Mic 1, USB 1 and USB 2 ran, while
# Mic 3 and Bluetooth refused - both with their faders down, so neither reaches the Chat mix at all.
# That makes this the common path rather than an edge case, and it has to say what to DO instead of
# describing the mixer.
server_src = (ROOT / "Project" / "server.py").read_text(encoding="utf-8")
check_in("the start path recognises the cross-point refusal",
         "not reaching the Chat mix", server_src)
# The phrase is split across source lines, so match the words that carry the instruction.
check_in("and answers it with an instruction about the fader", "fader up", server_src)
check_in("naming where the exclusion would be, since that is the other cause",
         "Routing, Output mixes", server_src)

print("\nTHE STATUS ENDPOINT")
check("it is a GET route, since asking must never start anything",
      "/api/channels/level-check/status" in server.GET_ROUTES, True)
check("and the toggle is a POST",
      "/api/channels/level-check" in server.POST_ROUTES, True)
st = server.channel_level_check_status()
check("nothing is running to begin with", st["running"], False)
check("and it says so with no channel named", st["channel"], "")

print("")
if FAILS:
    print("FAILURES (%d):" % len(FAILS))
    for f in FAILS:
        print("  " + f)
    raise SystemExit(1)
print("ALL CHECKS PASSED")
