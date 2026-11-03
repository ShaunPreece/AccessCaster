"""Offline checks: announcements go quiet for a recording, and come back afterwards.

WHY IT WORKS THIS WAY. Announcements are spoken aloud, so during a take they end up in the
recording. Sending the speech to the computer's own speakers instead of the desk does not help:
anything audible in the room reaches the microphones. Silence is the only fix that works, so the
recorder switches announcements off for the duration and back on at the end.

THE RULES THIS PINS, each of which is a way it could go wrong and be very annoying:
  * a user who had announcements OFF must not find them ON after a recording;
  * a user who turns them back on MID-TAKE must not have them switched off again at the end;
  * suspending must not SPEAK ("Control announcer is off" would be the first thing in the take);
  * a pause gives them back and says so, and a resume takes them away again;
  * the notice is spoken only when the recording was started ON THE UNIT - started from the app,
    the reply carries the same words silently, and silence is the whole point.

No device and no speech: `speech.say` and `announcer.action_set_announce` are stubbed, so nothing is spoken and
no announcer thread is started.

    python DevTools/test_announcer_suspend.py
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401
import _testkit
import announcer
import recorder
import server
import settings_file
import speech
from _testkit import check

SAID = []


def fake_say(text):
    SAID.append(text)


def fake_set_announce(on, chosen=True):
    """Stands in for the real one: flips the flag without starting the announcer thread (which
    would open the HID device), and clears our claim exactly as the real one does."""
    announcer.suspended_for_record = False
    if on:
        speech.enabled.set()
    else:
        speech.enabled.clear()
    return {"ok": True, "announce": bool(on)}


speech.say = fake_say
announcer.action_set_announce = fake_set_announce


def reset(announcing: bool):
    SAID.clear()
    announcer.suspended_for_record = False
    announcer._quiet_stop.clear()
    announcer._quiet_start.clear()
    if announcing:
        speech.enabled.set()
    else:
        speech.enabled.clear()
    # Nothing is a self-echo unless a test says so.
    announcer._self_writes.clear()


def mark_ours():
    """Mark a recorder write as OURS, the way action_record does before it writes."""
    announcer.mark_self_write(("record",))


print("\nA RECORDING STARTED ON THE UNIT (nothing marked as ours)")
reset(announcing=True)
server._recorder_state_changed(recorder.READY, recorder.RECORDING)
check("announcements are switched off", speech.enabled.is_set(), False)
check("and the app remembers that IT did it", announcer.suspended_for_record, True)
check("the notice is spoken BEFORE the silence, in one phrase", len(SAID), 1)
check("and it says both what happened and what it did to announcements",
      "Recording started" in SAID[0] and "Announcements off" in SAID[0], True)
check("the announcer thread is told to leave QUIETLY, so its sign-off is not recorded",
      announcer._quiet_stop.is_set(), True)

print("\nAND STOPPED AGAIN")
SAID.clear()
server._recorder_state_changed(recorder.RECORDING, recorder.READY)
check("announcements are back on", speech.enabled.is_set(), True)
check("the claim is released", announcer.suspended_for_record, False)
check("one phrase, saying both things", len(SAID), 1)
check("and it says they are back", "back on" in SAID[0], True)
check("the announcer is restarted QUIETLY, so its greeting cannot silence that phrase",
      announcer._quiet_start.is_set(), True)

print("\nA USER WHO HAD ANNOUNCEMENTS OFF KEEPS THEM OFF")
reset(announcing=False)
server._recorder_state_changed(recorder.READY, recorder.RECORDING)
check("nothing is spoken", SAID, [])
check("no claim is made on the switch", announcer.suspended_for_record, False)
server._recorder_state_changed(recorder.RECORDING, recorder.READY)
check("and they are STILL off after the recording", speech.enabled.is_set(), False)
check("still nothing spoken", SAID, [])

print("\nSTARTED FROM THE APP: the notice is NOT spoken (the reply carries it, silently)")
reset(announcing=True)
mark_ours()
server._recorder_state_changed(recorder.READY, recorder.RECORDING)
check("announcements are still switched off", speech.enabled.is_set(), False)
check("but nothing was said out loud, so nothing lands in the take", SAID, [])

print("\nA PAUSE ON THE UNIT GIVES ANNOUNCEMENTS BACK UNTIL IT RESUMES")
reset(announcing=True)
server._recorder_state_changed(recorder.READY, recorder.RECORDING)
SAID.clear()
server._recorder_state_changed(recorder.RECORDING, recorder.PAUSED)
check("pausing brings announcements back", speech.enabled.is_set(), True)
check("and says it paused", SAID, ["Recording paused."])
SAID.clear()
server._recorder_state_changed(recorder.PAUSED, recorder.RECORDING)
check("resuming switches them off again", speech.enabled.is_set(), False)
check("saying only that it is recording again, not the off notice a second time",
      SAID, ["Recording again."])
server._recorder_state_changed(recorder.RECORDING, recorder.READY)
check("and the stop still gives them back", speech.enabled.is_set(), True)

print("\nA PAUSE WITH ANNOUNCEMENTS OFF SAYS NOTHING AND LEAVES THEM OFF")
reset(announcing=False)
server._recorder_state_changed(recorder.READY, recorder.RECORDING)
server._recorder_state_changed(recorder.RECORDING, recorder.PAUSED)
check("still off while paused", speech.enabled.is_set(), False)
check("nothing spoken", SAID, [])

print("\nA PAUSE FROM THE APP: announcements come back, the reply carries the word")
reset(announcing=True)
server._recorder_state_changed(recorder.READY, recorder.RECORDING)
SAID.clear()
mark_ours()
server._recorder_state_changed(recorder.RECORDING, recorder.PAUSED)
check("back on", speech.enabled.is_set(), True)
check("nothing spoken by the watcher", SAID, [])

print("\nTHE USER TURNS THEM BACK ON MID-TAKE")
reset(announcing=True)
server._recorder_state_changed(recorder.READY, recorder.RECORDING)
check("off, as before", speech.enabled.is_set(), False)
announcer.action_set_announce(True)              # a deliberate toggle by the user
check("their choice releases our claim", announcer.suspended_for_record, False)
server._recorder_state_changed(recorder.RECORDING, recorder.READY)
check("so stopping does not switch them off again", speech.enabled.is_set(), True)

print("\nTHE OPTION OFF: a user with a separate headphone output keeps their announcements")
# Settings are stubbed from here on, so the real settings.json is neither read nor written.
_settings = {}
settings_file.load = lambda: dict(_settings)
settings_file.update = lambda **kw: _settings.update(kw)
check("it defaults to ON, because most people do not want speech in the take",
      announcer.record_mutes_announcer(), True)
_settings["record_mute_announcer"] = False
reset(announcing=True)
server._recorder_state_changed(recorder.READY, recorder.RECORDING)
check("announcements keep playing through the take", speech.enabled.is_set(), True)
check("no claim is made on the switch", announcer.suspended_for_record, False)
check("and the spoken notice does NOT promise silence that is not coming",
      len(SAID) == 1 and "Announcements off" not in SAID[0], True)
server._recorder_state_changed(recorder.RECORDING, recorder.READY)
check("they are still on afterwards", speech.enabled.is_set(), True)

print("\nTURNING THE OPTION OFF MID-TAKE GIVES THEM BACK STRAIGHT AWAY")
_settings["record_mute_announcer"] = True
reset(announcing=True)
server._recorder_state_changed(recorder.READY, recorder.RECORDING)
check("off while recording", speech.enabled.is_set(), False)
announcer.action_set_record_mute_announcer(False)
check("switching the option off restores them without waiting for the stop",
      speech.enabled.is_set(), True)
_settings["record_mute_announcer"] = True

print("\nNO DESTINATION IS A STOP TOO")
reset(announcing=True)
server._recorder_state_changed(recorder.READY, recorder.RECORDING)
server._recorder_state_changed(recorder.RECORDING, recorder.NO_DESTINATION)
check("the drive being pulled mid-take still gives announcements back",
      speech.enabled.is_set(), True)

print("\nPLUGGING THE DRIVE IN AND OUT, WORDED FROM THE SERVER")
# recorder.spoken_change owns the words; this pins the WIRING round it - the medium name coming
# from the last full page read, and the full stop the phrase does not carry itself.
server._note_recorder_context({"destination": {"spoken": "USB drive", "low": False},
                               "room": {"words": "3 hours"}})
reset(announcing=True)
server._recorder_state_changed(recorder.NO_DESTINATION, recorder.READY)
check("putting it IN does not say a recording stopped", SAID, ["Recording now available."])
reset(announcing=True)
server._recorder_state_changed(recorder.READY, recorder.NO_DESTINATION)
check("taking it out while idle names the drive that left",
      SAID, ["Recording no longer available: the USB drive was removed."])
check("and it did not touch announcements, having never suspended them",
      speech.enabled.is_set(), True)
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

print("\nRECORD TONES INSTEAD OF SPEECH")
# Each tone with whether announcements were on as it was queued: a start's must be queued before
# the take's silence, a pause's and a stop's after announcements come back.
TONES = []
_tone, speech.tone = speech.tone, lambda name: TONES.append((name, speech.enabled.is_set())) or True
announcer.record_feedback = "tone"
reset(announcing=True)
server._recorder_state_changed(recorder.READY, recorder.RECORDING)
check("a start plays the start tone, before the silence", TONES, [("record_start", True)])
check("and says nothing", SAID, [])
check("and announcements still go quiet for the take", speech.enabled.is_set(), False)
TONES.clear()
server._recorder_state_changed(recorder.RECORDING, recorder.PAUSED)
server._recorder_state_changed(recorder.PAUSED, recorder.RECORDING)
server._recorder_state_changed(recorder.RECORDING, recorder.READY)
check("pause, resume and stop each play their own",
      TONES, [("record_pause", True), ("record_start", True), ("record_stop", True)])
check("with nothing spoken", SAID, [])
reset(announcing=True)
TONES.clear()
mark_ours()
server._recorder_state_changed(recorder.READY, recorder.RECORDING)
check("a press from this app plays one too", TONES, [("record_start", True)])
reset(announcing=True)
server._recorder_state_changed(recorder.READY, recorder.RECORDING)
TONES.clear()
SAID.clear()
server._recorder_state_changed(recorder.RECORDING, recorder.NO_DESTINATION)
check("a take ended by a pulled drive has no tone", TONES, [])
check("and is spoken, saying why", SAID[0].startswith("Recording stopped"), True)
reset(announcing=True)
server._recorder_state_changed(recorder.NO_DESTINATION, recorder.READY)
check("a drive going in has no tone either", TONES, [])

print("\nSAY NOTHING: the transport is silent, storage is still spoken")
announcer.record_feedback = "silent"
reset(announcing=True)
TONES.clear()
server._recorder_state_changed(recorder.READY, recorder.RECORDING)
check("a start makes no sound", (TONES, SAID), ([], []))
check("and announcements still go quiet for the take", speech.enabled.is_set(), False)
server._recorder_state_changed(recorder.RECORDING, recorder.PAUSED)
server._recorder_state_changed(recorder.PAUSED, recorder.RECORDING)
server._recorder_state_changed(recorder.RECORDING, recorder.READY)
check("nor do pause, resume and stop", (TONES, SAID), ([], []))
check("and announcements are back after it", speech.enabled.is_set(), True)
reset(announcing=True)
server._recorder_state_changed(recorder.READY, recorder.RECORDING)
SAID.clear()
server._recorder_state_changed(recorder.RECORDING, recorder.NO_DESTINATION)
check("a take ended by a pulled drive is still spoken",
      bool(SAID) and SAID[0].startswith("Recording stopped"), True)
reset(announcing=True)
SAID.clear()
server._recorder_state_changed(recorder.NO_DESTINATION, recorder.READY)
check("and so is a drive going in", bool(SAID), True)
announcer.record_feedback = "speak"
speech.tone = _tone

print("\nTHE CHOICE IS SAVED, AND THE EARLIER TICK BOX STILL MEANS WHAT IT DID")
check("the old tick box on reads as tones", announcer._saved_record_feedback(
    {"announcer_record_tones": True}), "tone")
check("off, or never set, reads as spoken", announcer._saved_record_feedback({}), "speak")
check("the new choice wins over the old box", announcer._saved_record_feedback(
    {"announcer_record_feedback": "silent", "announcer_record_tones": True}), "silent")
check("a choice that is not one is refused",
      announcer.action_set_announce_record_feedback("loud")["ok"], False)

print("\nSPEAK AND WAIT: what a resume from the app says before it acts")
reset(announcing=False)
t0 = time.monotonic()
speech.say_and_wait("Resuming")
check("with announcements off it does not wait", time.monotonic() - t0 < 0.5, True)


def fake_submit_played(item, logged):
    """The speech thread's side: play the phrase, then release whoever waits on it."""
    SAID.append(item.text)
    threading.Timer(0.2, speech._release, args=(item,)).start()


_submit, speech._submit = speech._submit, fake_submit_played
reset(announcing=True)
t0 = time.monotonic()
speech.say_and_wait("Resuming")
took = time.monotonic() - t0
check("it waits until the phrase has played", 0.15 < took < 2.0, True)
check("and the phrase carries the words", SAID, ["Resuming"])
dropped = speech._Item("Resuming", done=threading.Event())
speech._release(dropped)
check("a phrase that is dropped releases its waiter", dropped.done.is_set(), True)
speech._submit = _submit

sys.exit(_testkit.finish())
