"""Offline checks: announcements go quiet for a recording, and come back afterwards.

WHY IT WORKS THIS WAY. Announcements are spoken aloud, so during a take they end up in the
recording. The first fix was to send our speech to the computer's own speakers instead of the desk,
and Shaun killed it in one line: *"no, it just moves the announcements off mic and still picked up
by mic anyway."* Anything audible in the room reaches the microphones. Silence is the only fix that
works, so the recorder switches announcements off for the duration and back on at the end.

THE RULES THIS PINS, each of which is a way it could go wrong and be very annoying:
  * a user who had announcements OFF must not find them ON after a recording;
  * a user who turns them back on MID-TAKE must not have them switched off again at the end;
  * suspending must not SPEAK ("Control announcer is off" would be the first thing in the take);
  * a pause counts as recording, so it does not toggle around every pause;
  * the notice is spoken only when the recording was started ON THE UNIT - started from the app,
    the reply carries the same words silently, and silence is the whole point.

No device and no speech: `_ann_say` and `action_set_announce` are stubbed, so nothing is spoken and
no announcer thread is started.

    python DevTools/test_announcer_suspend.py
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import recorder
import server

FAILS = []
SAID = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILS.append(f"{name}: got {got!r}, wanted {want!r}")
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")


def fake_say(text):
    SAID.append(text)


def fake_set_announce(on, chosen=True):
    """Stands in for the real one: flips the flag without starting the announcer thread (which
    would open the HID device), and clears our claim exactly as the real one does."""
    server._ann_suspended_for_record = False
    if on:
        server._announce_on.set()
    else:
        server._announce_on.clear()
    return {"ok": True, "announce": bool(on)}


server._ann_say = fake_say
server.action_set_announce = fake_set_announce


def reset(announcing: bool):
    SAID.clear()
    server._ann_suspended_for_record = False
    server._ann_quiet_stop.clear()
    server._ann_quiet_start.clear()
    if announcing:
        server._announce_on.set()
    else:
        server._announce_on.clear()
    # Nothing is a self-echo unless a test says so.
    server._ann_self_writes.clear() if hasattr(server, "_ann_self_writes") else None


def mark_ours():
    """Mark a recorder write as OURS, the way action_record does before it writes."""
    server._ann_mark_self_write(("record",))


print("\nA RECORDING STARTED ON THE UNIT (nothing marked as ours)")
reset(announcing=True)
server._recorder_state_changed(recorder.READY, recorder.RECORDING)
check("announcements are switched off", server._announce_on.is_set(), False)
check("and the app remembers that IT did it", server._ann_suspended_for_record, True)
check("the notice is spoken BEFORE the silence, in one phrase", len(SAID), 1)
check("and it says both what happened and what it did to announcements",
      "Recording started" in SAID[0] and "Announcements off" in SAID[0], True)
check("the announcer thread is told to leave QUIETLY, so its sign-off is not recorded",
      server._ann_quiet_stop.is_set(), True)

print("\nAND STOPPED AGAIN")
SAID.clear()
server._recorder_state_changed(recorder.RECORDING, recorder.READY)
check("announcements are back on", server._announce_on.is_set(), True)
check("the claim is released", server._ann_suspended_for_record, False)
check("one phrase, saying both things", len(SAID), 1)
check("and it says they are back", "back on" in SAID[0], True)
check("the announcer is restarted QUIETLY, so its greeting cannot silence that phrase",
      server._ann_quiet_start.is_set(), True)

print("\nA USER WHO HAD ANNOUNCEMENTS OFF KEEPS THEM OFF")
reset(announcing=False)
server._recorder_state_changed(recorder.READY, recorder.RECORDING)
check("nothing is spoken", SAID, [])
check("no claim is made on the switch", server._ann_suspended_for_record, False)
server._recorder_state_changed(recorder.RECORDING, recorder.READY)
check("and they are STILL off after the recording", server._announce_on.is_set(), False)
check("still nothing spoken", SAID, [])

print("\nSTARTED FROM THE APP: the notice is NOT spoken (the reply carries it, silently)")
reset(announcing=True)
mark_ours()
server._recorder_state_changed(recorder.READY, recorder.RECORDING)
check("announcements are still switched off", server._announce_on.is_set(), False)
check("but nothing was said out loud, so nothing lands in the take", SAID, [])

print("\nA PAUSE IS STILL A TAKE")
reset(announcing=True)
server._recorder_state_changed(recorder.READY, recorder.RECORDING)
SAID.clear()
server._recorder_state_changed(recorder.RECORDING, recorder.PAUSED)
check("pausing does not bring announcements back", server._announce_on.is_set(), False)
check("and does not chatter about it", SAID, [])
server._recorder_state_changed(recorder.PAUSED, recorder.RECORDING)
check("neither does resuming", server._announce_on.is_set(), False)

print("\nTHE USER TURNS THEM BACK ON MID-TAKE")
reset(announcing=True)
server._recorder_state_changed(recorder.READY, recorder.RECORDING)
check("off, as before", server._announce_on.is_set(), False)
server.action_set_announce(True)              # a deliberate toggle by the user
check("their choice releases our claim", server._ann_suspended_for_record, False)
server._recorder_state_changed(recorder.RECORDING, recorder.READY)
check("so stopping does not switch them off again", server._announce_on.is_set(), True)

print("\nTHE OPTION OFF: a user with a separate headphone output keeps their announcements")
# Settings are stubbed from here on, so the real settings.json is neither read nor written.
_settings = {}
server.load_settings = lambda: dict(_settings)
server.update_settings = lambda **kw: _settings.update(kw)
check("it defaults to ON, because most people do not want speech in the take",
      server._record_mutes_announcer(), True)
_settings["record_mute_announcer"] = False
reset(announcing=True)
server._recorder_state_changed(recorder.READY, recorder.RECORDING)
check("announcements keep playing through the take", server._announce_on.is_set(), True)
check("no claim is made on the switch", server._ann_suspended_for_record, False)
check("and the spoken notice does NOT promise silence that is not coming",
      len(SAID) == 1 and "Announcements off" not in SAID[0], True)
server._recorder_state_changed(recorder.RECORDING, recorder.READY)
check("they are still on afterwards", server._announce_on.is_set(), True)

print("\nTURNING THE OPTION OFF MID-TAKE GIVES THEM BACK STRAIGHT AWAY")
_settings["record_mute_announcer"] = True
reset(announcing=True)
server._recorder_state_changed(recorder.READY, recorder.RECORDING)
check("off while recording", server._announce_on.is_set(), False)
server.action_set_record_mute_announcer(False)
check("switching the option off restores them without waiting for the stop",
      server._announce_on.is_set(), True)
_settings["record_mute_announcer"] = True

print("\nNO DESTINATION IS A STOP TOO")
reset(announcing=True)
server._recorder_state_changed(recorder.READY, recorder.RECORDING)
server._recorder_state_changed(recorder.RECORDING, recorder.NO_DESTINATION)
check("the drive being pulled mid-take still gives announcements back",
      server._announce_on.is_set(), True)

print("\nPLUGGING THE DRIVE IN AND OUT, WORDED FROM THE SERVER")
# recorder.spoken_change owns the words; this pins the WIRING round it - the medium name coming
# from the last full page read, and the full stop the phrase does not carry itself.
server._note_recorder_context({"destination": {"spoken": "USB drive", "low": False},
                               "room": {"words": "3 hours"}})
reset(announcing=True)
server._recorder_state_changed(recorder.NO_DESTINATION, recorder.READY)
check("putting it IN no longer says a recording stopped", SAID, ["Recording now available."])
reset(announcing=True)
server._recorder_state_changed(recorder.READY, recorder.NO_DESTINATION)
check("taking it out while idle names the drive that left",
      SAID, ["Recording no longer available: the USB drive was removed."])
check("and it did not touch announcements, having never suspended them",
      server._announce_on.is_set(), True)
# Mid-take: the phrase must arrive WITH the news that announcements are back, in one phrase,
# because the speech pump coalesces and a second call would silence the first.
reset(announcing=True)
server._recorder_state_changed(recorder.READY, recorder.RECORDING)
SAID.clear()
server._recorder_state_changed(recorder.RECORDING, recorder.NO_DESTINATION)
check("a mid-take pull says the recording stopped, why, and that announcements are back",
      SAID, ["Recording stopped: the USB drive was removed. Announcements are back on."])
# The startup case: the first state the app ever sees is not a change, so nothing is said. This is
# the whole reason `was` is checked for None rather than defaulted.
reset(announcing=True)
server._recorder_state_changed(None, recorder.READY)
check("the first state we ever see is announced as nothing at all", SAID, [])
server._note_recorder_context({})

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILURES'}")
for f in FAILS:
    print("  " + f)
raise SystemExit(1 if FAILS else 0)
