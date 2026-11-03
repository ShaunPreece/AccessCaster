"""Recording: the unit's recorder (what to record, and start, pause and stop), the recordings on its
card or drive, and the card and drive themselves.

The recorder is not in the desk state. The section reads it (`server.read_recorder`, one model
dump) after the desk state's first read, on F5, after a job that rewrites the desk, and when the
desk pushes a recording state the page is not showing (the desk state's "recorder" event): the
unit's own REC button, and a card or drive going in or out. A command's reply carries the same
payload, so a press is shown from its reply without another dump.

A change made on the unit is not spoken here: the announcer speaks it, and switches itself off for
the take (`server._recorder_state_changed`).
"""
import wx

import announcer
import recorder
import server
from native import a11y
from native.calls import call
from native.controls import Choice, Switch
from native.dialogs import JobDialog, confirm, report
from native.pages import Page, group, labelled, page_box, set_items

TITLE = "Recording"
UNREAD = "The recorder has not been read yet."
BUSY = "Still waiting for the unit to answer the last press."
NOWHERE = ("The unit has nowhere to record to. Put a memory card in, or a USB drive in the second "
           "USB port.")
USB_POINTER = ("This is what the unit writes to its card or drive. What it sends this computer "
               "over USB is a separate setting, on Routing, under Master Mix.")
MUTE_HINT = ("If off, announcements may be heard in the recording, depending on where you have "
             "them routed.")
LIST_HINT = "Newest first. To copy them off, take the card or drive to a computer."
STORAGE_HINT = "Eject before pulling a card or drive out. Erase deletes everything on it."
NO_STORAGE_WRITE = "AccessCaster cannot eject or erase storage on this unit."
RUNNING = (recorder.RECORDING, recorder.PAUSED)
CLOCK_MS = 1000


def recording_label(take):
    return f"{take['name']}, {take['duration']}" + (
        f", recorded {take['when']}" if take.get("when") else "")


def volumes(data):
    """The card slot and the USB drive. Never the internal storage, which holds the pad sounds, and
    never the live view of the drive being written to, a duplicate that exists only mid-take."""
    return [v for v in (data or {}).get("volumes") or []
            if v.get("can_record") and not v.get("live_view")]


def volume_label(volume):
    if volume.get("ejected"):
        return f"{volume['label']}: ejected"
    if not volume.get("inserted"):
        return f"{volume['label']}: empty"
    return f"{volume['label']}: {volume['free_mb']} MB free of {volume['capacity_mb']} MB"


def _with_sentence(action, *args):
    """A transport command, then the state sentence from the recorder's listener (no device read),
    which the command has just brought up to date."""
    reply = action(*args)
    reply["sentence"] = server.read_recorder_live().get("message") or ""
    return reply


class RecordingSection:
    title = TITLE
    route = "recording"

    def __init__(self, frame):
        self.frame = frame
        self.data = None             # the last recorder payload (server._recorder_payload)
        self.sentence = ""           # what the unit is doing, as the server words it
        self.busy = False            # a command from this window has not returned
        self._pushed = None          # the last state pushed while busy
        self._reading = False
        self._again = None           # a read asked for while one runs: whether to announce it
        self.recorder = RecorderPage(self)
        self.recordings = RecordingsPage(self)
        self.storage = StoragePage(self)
        self.pages = [Page("recorder", "Recorder", self.recorder),
                      Page("recordings", "Recordings", self.recordings),
                      Page("storage", "Card and drive", self.storage)]
        frame.desk.subscribe(self._desk_event)

    @property
    def state(self):
        return self.data.get("state") if self.data else None

    def refresh(self, announce):
        self.read(announce)

    def _desk_event(self, event, detail):
        if event == "read":
            if self.data is None and not self._reading:
                self.read()
        elif event == "recorder":
            # A press from this window is shown from its own reply; the push it causes arrives
            # first, while the command still holds the device.
            if self.busy:
                self._pushed = detail
            elif detail != self.state:
                self.read()

    def read(self, announce=False):
        if self._reading:
            self._again = bool(self._again) or announce
            return
        self._reading = True
        call(self.frame, server.read_recorder, done=lambda reply: self._read_done(reply, announce))

    def _read_done(self, reply, announce):
        self._reading = False
        message = reply.get("message") or ("" if reply.get("ok") else
                                           "Could not read the recorder.")
        if reply.get("ok"):
            self.show(reply, message)
        elif self.data is None:
            self.recorder.set_sentence(message)
        if announce:
            self.frame.say(message)
        if self._again is not None:
            again, self._again = self._again, None
            self.read(again)

    def show(self, data, sentence):
        self.data = data
        self.sentence = sentence
        for page in (self.recorder, self.recordings, self.storage):
            page.show()

    def command(self, source, action, *args, working="", reread=False):
        """Run a command from `source`, show the payload its reply carries, and speak the reply.
        A transport command (`reread` False) takes the state sentence from the listener; anything
        else reads the recorder again, since it moves the room left and the destination."""
        if self.busy:
            self.frame.say(BUSY)
            return
        self.busy = True
        self._pushed = None
        if working:
            self.frame.say(working)
        work = (lambda: action(*args)) if reread else (lambda: _with_sentence(action, *args))
        call(self.frame, work, done=lambda reply: self._commanded(source, reply, reread))

    def _commanded(self, source, reply, reread):
        self.busy = False
        if reply.get("recorder"):
            self.show(reply["recorder"], reply.get("sentence") or self.sentence)
        self.frame.reply(source, reply)
        if reread or (self._pushed is not None and self._pushed != self.state):
            self.read()
        self._pushed = None


class RecorderPage(wx.Panel):
    """What the unit is doing; what a take contains and what it is saved as, the two decisions made
    before a take; then the transport and whether announcements stop for a take."""

    def __init__(self, section):
        super().__init__(section.frame.host)
        self.Hide()
        self.section = section
        self.frame = section.frame
        self._mute = announcer.record_mutes_announcer()
        sizer, parent = page_box(self, "Recorder")
        self.now = labelled(parent, sizer, "Now", lambda p: wx.TextCtrl(
            p, value="Reading the unit.", size=(460, 60),
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_NO_VSCROLL))

        box_sizer, box = group(parent, sizer, "What to record")
        self.multitrack = Choice(
            self, box, box_sizer, "Tracks", sorted(recorder.MULTITRACK_LABEL.items()),
            server.action_set_multitrack, *self.field("multitrack"),
            description=f"{recorder.MULTITRACK_HINT} {USB_POINTER}", guard=self._unread,
            accepted=lambda _reply: self._show_bypass())
        self.format = Choice(
            self, box, box_sizer, "Save recordings as",
            sorted(recorder.RECORDING_FORMAT_LABEL.items()), server.action_set_recording_format,
            *self.field("format"), description=recorder.RECORDING_FORMAT_HINT, guard=self._unread)
        self.bypass = Switch(self, box, box_sizer, "Record the separate tracks raw",
                             server.action_set_processing_bypass,
                             *self.field("processing_bypass"), guard=self._bypass_refused)

        # Fixed labels, one command each: a focused button that renames itself is read again by
        # the screen reader, over the reply saying what the press did.
        box_sizer, box = group(parent, sizer, "Start and stop")
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.record = self._button(box, row, "Record", self._on_record)
        a11y.describe(self.record, "Starts a recording, or carries on with a paused one.")
        self.pause = self._button(box, row, "Pause", self._on_pause)
        self.stop = self._button(box, row, "Stop recording", self._on_stop)
        self.marker = self._button(box, row, "Drop a marker", self._on_marker)
        a11y.describe(self.marker, "Puts a marker your editor can find at this point of the "
                                   "recording.")
        self.status = self._button(box, row, "Recording status", self._on_status)
        box_sizer.Add(row, 0, wx.ALL, 6)
        self.mute = Switch(self, box, box_sizer, "Switch announcements off while recording",
                           announcer.action_set_record_mute_announcer, lambda: self._mute,
                           self._remember_mute, description=MUTE_HINT)
        # The Now line's running time during a take, from the push listener: no desk read.
        self._clock = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda _e: self._tick(), self._clock)
        self.Bind(wx.EVT_SHOW, self._on_show)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self.show()

    def _button(self, parent, row, label, handler):
        button = wx.Button(parent, label=label)
        button.Bind(wx.EVT_BUTTON, handler)
        row.Add(button, 0, wx.RIGHT, 8)
        return button

    def field(self, key):
        """`current` and `remember` for `key` in the section's recorder payload."""
        def current():
            return self.section.data.get(key) if self.section.data else None

        def remember(value):
            if self.section.data:
                self.section.data[key] = value
        return current, remember

    def _remember_mute(self, on):
        self._mute = on

    def _unread(self):
        return UNREAD if self.section.data is None else None

    def _bypass_refused(self):
        # Off, unknown and not read yet all mean no separate tracks are being made.
        return self._unread() or (None if self.section.data.get("multitrack")
                                  else recorder.BYPASS_NEEDS_MULTITRACK)

    def set_sentence(self, text):
        if self.now.GetValue() != text:
            self.now.SetValue(text)

    def show(self):
        if self.section.data is not None:
            self.set_sentence(self.section.sentence)
        for control in (self.multitrack, self.format, self.bypass, self.mute):
            control.show()
        self._show_bypass()
        self._run_clock()

    def _run_clock(self):
        running = self.IsShown() and self.section.state == recorder.RECORDING
        if running and not self._clock.IsRunning():
            self._clock.Start(CLOCK_MS)
        elif not running and self._clock.IsRunning():
            self._clock.Stop()

    def _on_show(self, event):
        event.Skip()
        self._run_clock()

    def _on_destroy(self, event):
        if event.GetEventObject() is self:
            self._clock.Stop()
        event.Skip()

    def _tick(self):
        call(self, server.read_recorder_live, done=self._ticked)

    def _ticked(self, reply):
        # A take stopped between the tick and its reply is left to the section's own read.
        if (reply.get("ok") and reply.get("state") == recorder.RECORDING
                and self.section.state == recorder.RECORDING):
            self.set_sentence(reply["message"])

    def _show_bypass(self):
        data = self.section.data or {}
        self.bypass.acc.description = (recorder.BYPASS_HINT if data.get("multitrack")
                                       else recorder.BYPASS_NEEDS_MULTITRACK)

    # ---- the transport ---------------------------------------------------------------------

    def _on_record(self, event):
        state = self.section.state
        refusal = self._unread() or (NOWHERE if state == recorder.NO_DESTINATION else None)
        if refusal:
            self.frame.say(refusal)
            return
        self.section.command(event.GetEventObject(), server.action_record,
                             "resume" if state == recorder.PAUSED else "start")

    def _on_pause(self, event):
        state = self.section.state
        refusal = self._unread()
        if not refusal and state == recorder.PAUSED:
            refusal = "It is already paused. Record carries on with it."
        elif not refusal and state != recorder.RECORDING:
            refusal = "Nothing is recording."
        if refusal:
            self.frame.say(refusal)
            return
        self.section.command(event.GetEventObject(), server.action_record, "pause")

    def _on_stop(self, event):
        # The server refuses a stop with nothing running, and a marker with nothing recording,
        # from its listener's state without touching the device.
        refusal = self._unread()
        if refusal:
            self.frame.say(refusal)
            return
        self.section.command(event.GetEventObject(), server.action_record, "stop")

    def _on_marker(self, event):
        refusal = self._unread()
        if refusal:
            self.frame.say(refusal)
            return
        self.section.command(event.GetEventObject(), server.action_drop_marker)

    def _on_status(self, _event):
        def said(reply):
            self.frame.say(reply.get("message") if reply.get("ok")
                           else self.section.sentence or UNREAD)
        call(self, server.read_recorder_live, done=said)


class RecordingsPage(wx.Panel):
    """The recordings on the card or drive, and deleting one."""

    def __init__(self, section):
        super().__init__(section.frame.host)
        self.Hide()
        self.section = section
        self.frame = section.frame
        self.takes = []              # the recordings as listed; empty when the list holds a note
        sizer, parent = page_box(self, "Recordings")
        self.list = labelled(parent, sizer, "Recording", lambda p: wx.ListBox(
            p, choices=["Reading the unit."], size=(460, 220)))
        a11y.describe(self.list, LIST_HINT)
        self.delete = wx.Button(parent, label="Delete selected recording")
        a11y.describe(self.delete, "Deletes it from the card or drive. This cannot be undone; you "
                                   "are asked to confirm.")
        self.delete.Bind(wx.EVT_BUTTON, self._on_delete)
        sizer.Add(self.delete, 0, wx.ALL, 6)

    def show(self):
        data = self.section.data
        chosen = self._chosen()
        uid = chosen["uid"] if chosen else None
        self.takes = data.get("recordings") or []
        if self.takes:
            labels = [recording_label(take) for take in self.takes]
        elif data.get("state") == recorder.NO_DESTINATION:
            labels = ["Nothing to list: there is no card or drive in the unit."]
        else:
            labels = ["No recordings on the unit."]
        set_items(self.list, labels)
        uids = [take["uid"] for take in self.takes]
        self.list.SetSelection(uids.index(uid) if uid in uids else 0)

    def _chosen(self):
        index = self.list.GetSelection()
        return self.takes[index] if self.takes and index != wx.NOT_FOUND else None

    def _on_delete(self, _event):
        data = self.section.data
        take = self._chosen()
        refusal = (UNREAD if data is None else
                   "There are no recordings on the unit." if take is None else
                   None if data.get("can_delete") else
                   data.get("delete_note") or "Deleting is not available.")
        if refusal:
            self.frame.say(refusal)
            return
        if not confirm(self, f"Delete {recording_label(take)}?",
                       "This removes the recording from the unit's card or drive. It cannot be "
                       "undone, and AccessCaster has no copy of it.", "Delete"):
            return
        self.section.command(self.delete, server.action_delete_recording, take["uid"],
                             working="Deleting, please wait.", reread=True)


class StoragePage(wx.Panel):
    """The card slot and the USB drive: eject one before pulling it out, reconnect one ejected from
    here, or erase one."""

    def __init__(self, section):
        super().__init__(section.frame.host)
        self.Hide()
        self.section = section
        self.frame = section.frame
        self.shown = []              # the volumes as listed
        sizer, parent = page_box(self, "Card and drive")
        self.list = labelled(parent, sizer, "Card or drive", lambda p: wx.ListBox(
            p, choices=["Reading the unit."], size=(460, 80)))
        a11y.describe(self.list, STORAGE_HINT)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.eject = self._button(parent, row, "Eject", self._on_eject)
        self.reconnect = self._button(parent, row, "Reconnect", self._on_reconnect)
        a11y.describe(self.reconnect, "Brings back a card or drive ejected from here, without "
                                      "taking it out.")
        self.erase = self._button(parent, row, "Erase", self._on_erase)
        sizer.Add(row, 0, wx.ALL, 6)

    def _button(self, parent, row, label, handler):
        button = wx.Button(parent, label=label)
        button.Bind(wx.EVT_BUTTON, handler)
        row.Add(button, 0, wx.RIGHT, 8)
        return button

    def show(self):
        chosen = self._chosen()
        index = chosen["index"] if chosen else None
        self.shown = volumes(self.section.data)
        set_items(self.list, [volume_label(v) for v in self.shown]
                  or ["The unit reports no card slot or drive."])
        indices = [v["index"] for v in self.shown]
        if index in indices:
            self.list.SetSelection(indices.index(index))
        else:
            # The one with something in it, which is nearly always the one wanted.
            in_use = [i for i, v in enumerate(self.shown) if v.get("inserted") or v.get("ejected")]
            self.list.SetSelection(in_use[0] if in_use else 0)

    def _chosen(self):
        index = self.list.GetSelection()
        return self.shown[index] if self.shown and index != wx.NOT_FOUND else None

    def _refused(self, volume):
        """The refusal every storage action shares, or None."""
        data = self.section.data
        if data is None:
            return UNREAD
        if data.get("storage_writable") is False:
            return NO_STORAGE_WRITE
        if volume is None:
            return "The unit reports no card slot or drive."
        return None

    def _on_eject(self, _event):
        volume = self._chosen()
        refusal = self._refused(volume)
        if not refusal and volume.get("ejected"):
            refusal = "It is already ejected. Reconnect brings it back."
        elif not refusal and not volume.get("inserted"):
            refusal = "Nothing is in it."
        elif not refusal and self.section.state in RUNNING:
            refusal = "Stop the recording first."
        if refusal:
            self.frame.say(refusal)
            return
        self.section.command(self.eject, server.action_storage_eject, volume["index"], True,
                             working="Ejecting, please wait.", reread=True)

    def _on_reconnect(self, _event):
        volume = self._chosen()
        refusal = self._refused(volume)
        if not refusal and not volume.get("ejected"):
            refusal = "It is not ejected."
        if refusal:
            self.frame.say(refusal)
            return
        self.section.command(self.reconnect, server.action_storage_eject, volume["index"], False,
                             working="Reconnecting, please wait.", reread=True)

    def _on_erase(self, _event):
        volume = self._chosen()
        refusal = self._refused(volume)
        if not refusal and volume.get("ejected"):
            refusal = "Reconnect it before erasing."
        elif not refusal and not (volume.get("inserted") and volume.get("mounted")):
            refusal = "Nothing is in it."
        elif not refusal and self.section.state in RUNNING:
            refusal = "Stop the recording first."
        elif not refusal and self.section.busy:
            refusal = BUSY
        if refusal:
            self.frame.say(refusal)
            return
        spoken = volume["spoken"]
        if not confirm(self, f"Erase the {spoken}?",
                       "Everything on it is deleted, recordings and any other files. It cannot be "
                       "undone, and AccessCaster has no copy.", "Erase"):
            return
        job = JobDialog(self, "Erasing", f"Erasing the {spoken}. This can take a minute or more.",
                        lambda dialog: call(dialog, server.action_storage_erase, volume["index"],
                                            done=dialog.finish),
                        stop=None, closable=False)
        with job:
            reply = job.run()
        report(self, ("Erased", "Erase failed"), reply)
        self.erase.SetFocus()
        if reply.get("recorder"):
            self.section.show(reply["recorder"], self.section.sentence)
        self.section.read()
