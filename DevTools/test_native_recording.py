"""The native window's Recording (Project/native/recording_section.py), offline: the recordings and
the card and drive are listed with what each is, a choice survives a read, Record starts or resumes
by the state and each transport press is refused where it cannot act, the Now line counts a take
only while the page is shown, a pushed state reads the
recorder only when it is news, the raw switch waits for separate tracks, and a delete, eject,
reconnect and erase each call their action with the right target and read again. Nothing is shown
and nothing touches a desk.

Run: python DevTools/test_native_recording.py
"""
from __future__ import annotations

import sys

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _a11y
import _testkit
import announcer
import recorder
import recording
import wx
from _native_fakes import FakeFrame, FakeJob
from _testkit import check, check_in, check_true, patched
from native import recording_section

app = wx.App()


def take(uid, name, duration="2 minutes", when="Monday at 10:00"):
    return {"uid": uid, "name": name, "duration": duration, "when": when}


def volume(index, label, inserted=False, ejected=False, can_record=True, live_view=False,
           free_mb=900, capacity_mb=1000):
    return {"index": index, "label": label, "spoken": label.lower(), "inserted": inserted,
            "mounted": inserted and not ejected, "ejected": ejected, "can_record": can_record,
            "live_view": live_view, "free_mb": free_mb, "capacity_mb": capacity_mb}


def payload(state=recorder.READY, takes=(), vols=None, multitrack=0, **values):
    data = {"ok": True, "state": state, "recordings": list(takes), "multitrack": multitrack,
            "format": 0, "processing_bypass": False, "can_delete": True, "delete_note": "",
            "storage_writable": True, "writable": True,
            "volumes": vols if vols is not None else [
                volume(0, "Memory card"), volume(1, "USB drive", inserted=True),
                volume(2, "Internal storage", inserted=True, can_record=False)]}
    data.update(values)
    return data


# ---- what each recording and volume is called ------------------------------------------------
check("a recording is its name, length and date",
      recording_section.recording_label(take(1, "REC0001")),
      "REC0001, 2 minutes, recorded Monday at 10:00")
check("one with no date leaves it out",
      recording_section.recording_label(take(1, "REC0001", when="")), "REC0001, 2 minutes")
check("an ejected volume says so",
      recording_section.volume_label(volume(1, "USB drive", inserted=True, ejected=True)),
      "USB drive: ejected")
check("an empty slot says so", recording_section.volume_label(volume(0, "Memory card")),
      "Memory card: empty")
check("one in use gives its space",
      recording_section.volume_label(volume(1, "USB drive", inserted=True)),
      "USB drive: 900 MB free of 1000 MB")
check("the internal storage and the live view are never offered",
      [v["label"] for v in recording_section.volumes(payload(vols=[
          volume(0, "Memory card"), volume(1, "USB drive", inserted=True),
          volume(2, "Internal storage", can_record=False),
          volume(3, "USB drive", inserted=True, live_view=True)]))],
      ["Memory card", "USB drive"])

# ---- the pages show a read -------------------------------------------------------------------
frame = FakeFrame()
reads = []
with patched((announcer, {"record_mutes_announcer": lambda: True})):
    section = recording_section.RecordingSection(frame)
section.read = lambda announce=False: reads.append(announce)

check("before a read each page is only a sentence and Read again",
      [(p.title, type(p.window).__name__) for p in section.pages],
      [("Recorder", "MessagePage"), ("Recordings", "MessagePage"),
       ("Card and drive", "MessagePage")])
section.read_done({"ok": False, "message": "Could not read the recorder: no desk."}, False)
check("a failed read says why on every page",
      [w.text.GetValue() for w in section.status.windows],
      ["Could not read the recorder: no desk."] * 3)
check_true("and builds no page", section.recorder is None and frame.changed == 0)
section.show(payload(takes=[take(5, "REC0005"), take(4, "REC0004")]), "Ready to record.")
rec, takes, storage = section.recorder, section.recordings, section.storage
check_true("a good read puts the real pages in the tree", frame.changed == 1
           and [p.window for p in section.pages] == [rec, takes, storage])
check("the sentence is on the page", rec.now.GetValue(), "Ready to record.")
check("the recordings are listed newest first",
      [takes.list.GetString(i) for i in range(takes.list.GetCount())],
      ["REC0005, 2 minutes, recorded Monday at 10:00",
       "REC0004, 2 minutes, recorded Monday at 10:00"])
check("the volume with something in it is chosen", storage._chosen()["label"], "USB drive")
check("the internal storage is not listed", storage.list.GetCount(), 2)
check("announcements stop for a take, as the setting says", rec.mute.box.GetValue(), True)

takes.list.SetSelection(1)
section.show(payload(takes=[take(6, "REC0006"), take(5, "REC0005"), take(4, "REC0004")]), "")
check("the chosen recording survives a read", takes._chosen()["uid"], 4)
section.show(payload(state=recorder.NO_DESTINATION, vols=[volume(0, "Memory card"),
                                                          volume(1, "USB drive")]), "")
check("no card or drive says so in the list", takes.list.GetString(0),
      "Nothing to list: there is no card or drive in the desk.")
check("and leaves nothing to delete", takes._chosen(), None)

# ---- the raw switch waits for separate tracks ------------------------------------------------
section.show(payload(multitrack=0), "")
check("the raw switch is refused on the mixed track only", rec._bypass_refused(),
      recorder.BYPASS_NEEDS_MULTITRACK)
check("and says why in its description", rec.bypass.acc.description,
      recorder.BYPASS_NEEDS_MULTITRACK)
section.show(payload(multitrack=1), "")
check("separate tracks free it", rec._bypass_refused(), None)
check("and describe what it does", rec.bypass.acc.description, recorder.BYPASS_HINT)

# ---- the transport ---------------------------------------------------------------------------
sync = (recording_section, {"call": lambda _o, fn, *a, done=None: done(fn(*a))})
commands = []


def fake_record(command):
    commands.append(command)
    state = {"start": recorder.RECORDING, "resume": recorder.RECORDING,
             "pause": recorder.PAUSED, "stop": recorder.READY}[command]
    return {"ok": True, "state": state, "recorder": payload(state=state),
            "message": {"stop": "Stopped. Saved REC0006, 1 minute."}.get(command, command)}


def press(button):
    """A press as wx sends it: the event carries the button."""
    event = wx.CommandEvent()
    event.SetEventObject(button)
    return event


def labels():
    return rec.transport.GetLabel(), rec.pause.GetLabel()


START, STOP = recording_section.START, recording_section.STOP
PAUSE, RESUME = recording_section.PAUSE, recording_section.RESUME
live = {"read_live": lambda: {"ok": True, "message": "Recording. 1 minute so far."}}
with patched(sync, (recording, {"action_record": fake_record, **live})):
    section.show(payload(state=recorder.READY), "")
    check("a ready desk offers Start recording and Pause", labels(), (START, PAUSE))
    said = len(frame.said)
    rec._on_transport(press(rec.transport))
    check("Start recording on a ready desk starts", commands[-1], "start")
    check("the reply's payload is shown", section.state, recorder.RECORDING)
    check("and the button becomes Stop recording", labels(), (STOP, PAUSE))
    check("the new label is the confirmation: nothing is spoken over it", frame.said[said:], [])
    check("the reply goes on the Now line with the listener's sentence", rec.now.GetValue(),
          "start Recording. 1 minute so far.")
    rec._on_pause(press(rec.pause))
    check("Pause during a take pauses", commands[-1], "pause")
    check("and becomes Resume", labels(), (STOP, RESUME))
    rec._on_pause(press(rec.pause))
    check("Resume on a paused desk carries on", commands[-1], "resume")
    check("and becomes Pause again", labels(), (STOP, PAUSE))
    section.show(payload(state=recorder.PAUSED), "")
    rec._on_transport(press(rec.transport))
    check("Stop recording stops a paused take too", commands[-1], "stop")
    check("and the button becomes Start recording", labels(), (START, PAUSE))
    check_in("the saved take is on the Now line", "Saved REC0006", rec.now.GetValue())
    check("still nothing spoken", frame.said[said:], [])
    count = len(commands)
    rec._on_pause(press(rec.pause))
    check("Pause with nothing recording is refused aloud", frame.said[-1], "Nothing is recording.")
    section.show(payload(state=recorder.NO_DESTINATION), "")
    rec._on_transport(press(rec.transport))
    check("Start recording with nowhere to record is refused", frame.said[-1],
          recording_section.NOWHERE)
    check("none of the refusals reached the desk", len(commands), count)

# A reply that changes no label, a failure, is spoken as before.
failed = {"ok": False, "message": "That did not take.", "recorder": payload(state=recorder.READY)}
section.show(payload(state=recorder.READY), "")
section._commanded(rec.transport, failed, False)
check("a failed press is spoken", frame.said[-1], "That did not take.")

# A state the unit's own REC button changed renames the buttons too.
section.show(payload(state=recorder.RECORDING), "")
check("a take started on the desk offers Stop recording", labels(), (STOP, PAUSE))
section.show(payload(state=recorder.PAUSED), "")
check("and a pause on the desk offers Resume", labels(), (STOP, RESUME))

# ---- the Now line's clock --------------------------------------------------------------------
ticking = {"read_live": lambda: {"ok": True, "state": recorder.RECORDING,
                                          "message": "Recording. 2 minutes so far."}}
with patched(sync, (recording, ticking)):
    section.show(payload(state=recorder.RECORDING), "Recording. 1 minute so far.")
    check("a hidden page runs no clock", rec._clock.IsRunning(), False)
    rec.Show()
    rec._run_clock()
    check("a take on a shown page runs the clock", rec._clock.IsRunning(), True)
    rec._tick()
    check("a tick puts the listener's time on the Now line", rec.now.GetValue(),
          "Recording. 2 minutes so far.")
    section.show(payload(state=recorder.PAUSED), "Paused.")
    check("a pause stops it", rec._clock.IsRunning(), False)
    rec._ticked({"ok": True, "state": recorder.RECORDING, "message": "Recording. 3 minutes so far."})
    check("and a late tick does not overwrite the paused line", rec.now.GetValue(), "Paused.")
    section.show(payload(state=recorder.RECORDING), "")
    rec.Hide()
    rec._run_clock()
    check("leaving the page stops it", rec._clock.IsRunning(), False)

section.show(payload(state=recorder.READY), "")
section.busy = True
rec._on_transport(wx.CommandEvent())
check("a second press while one runs is refused", frame.said[-1], recording_section.BUSY)
section.busy = False

# ---- a pushed state --------------------------------------------------------------------------
reads.clear()
frame.desk.listener("recorder", recorder.READY)
check("a state the page already shows reads nothing", reads, [])
frame.desk.listener("recorder", recorder.RECORDING)
check("a new one reads the recorder", reads, [False])
reads.clear()
section.busy = True
frame.desk.listener("recorder", recorder.PAUSED)
check("one pushed during a press waits for its reply", reads, [])
section._commanded(rec.transport, {"ok": True, "recorder": payload(state=recorder.RECORDING)}, False)
check("and is read after it when the reply disagrees", reads, [False])
reads.clear()
section.busy = True
frame.desk.listener("recorder", recorder.RECORDING)
section._commanded(rec.transport, {"ok": True, "recorder": payload(state=recorder.RECORDING)}, False)
check("but not when the reply agrees", reads, [])


def pushed_then_replied(command):
    """The desk's push lands while the read-back still holds the device."""
    frame.desk.listener("recorder", recorder.RECORDING)
    seen.append(labels())
    return fake_record(command)


seen = []
section.show(payload(state=recorder.READY), "")
said = len(frame.said)
with patched(sync, (recording, {"action_record": pushed_then_replied, **live})):
    rec._on_transport(press(rec.transport))
check("the buttons follow the push before the reply", seen, [(STOP, PAUSE)])
check("and the reply after it is still not spoken", frame.said[said:], [])
check("nor read again", reads, [])

# ---- deleting, ejecting, reconnecting and erasing --------------------------------------------
calls = []
questions = []
confirm = (recording_section, {"confirm": lambda _p, title, _t, _a: questions.append(title) or True})
section.show(payload(takes=[take(5, "REC0005"), take(4, "REC0004")]), "")
takes.list.SetSelection(1)
with patched(sync, confirm, (recording, {"action_delete_recording": lambda uid: calls.append(
        ("delete", uid)) or {"ok": True, "recorder": payload(takes=[take(5, "REC0005")]),
                             "message": "Deleted."}})):
    reads.clear()
    takes._on_delete(None)
check("a delete is asked about by name", questions[-1],
      "Delete REC0004, 2 minutes, recorded Monday at 10:00?")
check("and deletes the chosen recording", calls[-1], ("delete", 4))
check("the list follows the reply", takes.list.GetCount(), 1)
check_true("and the recorder is read again", reads)
section.show(payload(takes=[take(5, "REC0005")], can_delete=False, delete_note="Not here."), "")
takes._on_delete(None)
check("a desk that cannot delete says why", frame.said[-1], "Not here.")

section.show(payload(), "")
with patched(sync, (recording, {"action_storage_eject": lambda index, on: calls.append(
        ("eject", index, on)) or {"ok": True, "message": "Ejected."}})):
    storage._on_eject(None)
    check("Eject ejects the chosen volume", calls[-1], ("eject", 1, True))
    storage._on_reconnect(None)
    check("Reconnect refuses a volume that is not ejected", frame.said[-1], "It is not ejected.")
    section.show(payload(vols=[volume(1, "USB drive", inserted=True, ejected=True)]), "")
    storage._on_reconnect(None)
    check("and reconnects one that is", calls[-1], ("eject", 1, False))
    storage._on_eject(None)
    check("Eject refuses one already ejected", frame.said[-1],
          "It is already ejected. Reconnect brings it back.")
    storage._on_erase(None)
    check("Erase refuses an ejected volume", frame.said[-1], "Reconnect it before erasing.")
section.show(payload(state=recorder.RECORDING), "")
storage._on_erase(None)
check("Erase refuses mid-take", frame.said[-1], "Stop the recording first.")
section.show(payload(storage_writable=False), "")
storage._on_eject(None)
check("a desk whose storage cannot be changed says so", frame.said[-1],
      recording_section.NO_STORAGE_WRITE)


class EraseJob(FakeJob):
    def __init__(self, parent, title, text, begin, stop, closable=True):
        super().__init__(parent, title, text, begin, stop, closable)
        check("the erase cannot be closed half way", (stop, closable), (None, False))


reports = []
section.show(payload(), "")
reads.clear()
with patched(sync, confirm, (recording_section, {
        "JobDialog": EraseJob, "report": lambda _p, titles, reply: reports.append(
            titles[0] if reply.get("ok") else titles[1])}),
        (recording, {"action_storage_erase": lambda index: calls.append(("erase", index))
                  or {"ok": True, "recorder": payload(), "message": "Erased."}})):
    storage._on_erase(None)
check("Erase is asked about by name", questions[-1], "Erase the usb drive?")
check("and erases the chosen volume", calls[-1], ("erase", 1))
check("its result has a dialog of its own", reports, ["Erased"])
check_true("and the recorder is read again", reads)

frame.window.Destroy()
_a11y.check_windows("Recording")
sys.exit(_testkit.finish())
