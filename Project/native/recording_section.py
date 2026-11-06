"""Recording: the unit's recorder (what to record, and start, pause and stop), the recordings on its
card or drive, and the card and drive themselves.

The recorder is not in the desk state. The section reads it (`recording.read`, one model
dump) after each desk state read until one succeeds, on F5, after a job that rewrites the desk, and when the
desk pushes a recording state the page is not showing (the desk state's "recorder" event): the
unit's own REC button, and a card or drive going in or out. A command's reply carries the same
payload, so a press is shown from its reply without another dump; its buttons follow the push the
press causes, which arrives first.

A change made on the unit is not spoken here: the announcer speaks it, and switches itself off for
the take (`recording._state_changed`).
"""
import wx

import announcer
import recorder
import recording
from native import a11y
from native.calls import call
from native.controls import Choice, Switch
from native.dialogs import JobDialog, confirm, report
from native.pages import Reader, StatusPages, add_button, labelled, page_body, read_only, set_items

TITLE = "Recording"
PAGES = [("recorder", "Recorder"), ("recordings", "Recordings"), ("storage", "Card and drive")]
BUSY = "One moment: the desk is still carrying out your last press."
NOWHERE = ("The desk has nowhere to record to. Put a memory card in, or a USB drive in the second "
           "USB port.")
USB_POINTER = ("This is what the desk writes to its card or drive. What it sends this computer "
               "over USB is a separate setting, on Routing, under Master Mix.")
MUTE_HINT = ("If off, announcements may be heard in the recording, depending on where you have "
             "them routed.")
LIST_HINT = "Newest first. To copy them off, take the card or drive to a computer."
STORAGE_HINT = "Eject before pulling a card or drive out. Erase deletes everything on it."
NO_STORAGE_WRITE = "AccessCaster cannot eject or erase storage on this desk."
RUNNING = (recorder.RECORDING, recorder.PAUSED)
CLOCK_MS = 1000
START, STOP, PAUSE, RESUME = "Start recording", "Stop recording", "Pause", "Resume"


def recording_label(take):
    return f"{take['name']}, {take['duration']}" + (
        f", recorded {take['when']}" if take.get("when") else "")


def volumes(data):
    """The card slot and the USB drive. Never the internal storage, which holds the pad sounds, and
    never the live view of the drive being written to, a duplicate that exists only mid-take."""
    return [v for v in data.get("volumes") or []
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
    reply["sentence"] = recording.read_live().get("message") or ""
    return reply


class RecordingSection(Reader):
    title = TITLE
    route = "recording"

    def __init__(self, frame):
        self.frame = frame
        self.data = None             # the last recorder payload (recording._payload)
        self.sentence = ""           # what the unit is doing, as `recording` words it
        self.busy = False            # a command from this window has not returned
        self._pushed = None          # the last state pushed while busy
        self._label = None           # the pressed button's label at the press
        self._init_reader(owner=frame)
        # Built from the first good read (`show`).
        self.recorder = self.recordings = self.storage = None
        self.status = StatusPages(self, PAGES, "Read the recorder again")
        frame.desk.subscribe(self._desk_event)

    @property
    def state(self):
        return self.data.get("state") if self.data else None

    def start(self):
        self.status.set_text("Reading the recorder from the desk.")

    def refresh(self, announce):
        self.read(announce)

    def _desk_event(self, event, detail):
        if event == "read":
            if self.data is None and not self._reading:
                self.read()
        elif event == "recorder":
            # A press from this window: the push it causes arrives within ~30 ms, about two seconds
            # before the reply, whose read-back still holds the device. The buttons follow the push
            # at once; the reply fills in the rest. No push means the state did not change.
            if self.busy:
                self._pushed = detail
                self.data["state"] = detail
                self.recorder.show_state()
            elif detail != self.state:
                self.read()

    def fetch(self):
        return recording.read()

    def read_done(self, reply, announce):
        message = reply.get("message") or ("" if reply.get("ok") else
                                           "Could not read the recorder.")
        if reply.get("ok"):
            self.show(reply, message)
        elif self.data is None:
            self.status.set_text(message)
        if announce:
            self.frame.say(message)

    def show(self, data, sentence):
        self.data = data
        self.sentence = sentence
        if self.recorder is None:
            self.recorder = RecorderPage(self)
            self.recordings = RecordingsPage(self)
            self.storage = StoragePage(self)
            self.status.replace([self.recorder, self.recordings, self.storage])
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
        # Taken now: the push can rename the button before the reply comes back.
        self._label = source.GetLabel() if isinstance(source, wx.Button) else None
        if working:
            self.frame.say(working)
        work = (lambda: action(*args)) if reread else (lambda: _with_sentence(action, *args))
        call(self.frame, work, done=lambda reply: self._commanded(source, reply, reread))

    def _commanded(self, source, reply, reread):
        self.busy = False
        label = self._label
        if reply.get("recorder"):
            self.show(reply["recorder"], reply.get("sentence") or self.sentence)
        if reply.get("ok") and label is not None and source.GetLabel() != label:
            # The button that was pressed renamed itself, and the screen reader reads that out:
            # it is the confirmation. What the new label does not say goes on the Now line.
            message = reply.get("message") or ""
            if message and not self.sentence.startswith(message):
                self.recorder.set_sentence(f"{message} {self.sentence}".strip())
        else:
            self.frame.reply(source, reply)
        if reread or (self._pushed is not None and self._pushed != self.state):
            self.read()
        self._pushed = None


class RecorderPage(wx.Panel):
    """The transport and what the unit is doing; then the options set once: what a take contains,
    what it is saved as, and whether other announcements stop for a take."""

    def __init__(self, section):
        super().__init__(section.frame.host)
        self.Hide()
        self.section = section
        self.frame = section.frame
        self._mute = announcer.record_mutes_announcer()
        sizer, parent = page_body(self)

        # THE TRANSPORT COMES FIRST: it is what the page is opened for, and the focus lands on Start
        # recording. The options below are set once and rarely changed.
        #
        # TWO BUTTONS WHOSE LABELS FOLLOW THE STATE, whoever changed it, this window or the unit's
        # REC button. A focused button that renames itself is read out by the screen reader, so
        # after a press the new label is the confirmation and the reply is not spoken over it
        # (`RecordingSection._commanded`). Neither is ever disabled: a disabled button leaves the
        # tab order from under the user, so one that cannot act says why.
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.transport = add_button(parent, row, START, self._on_transport)
        a11y.describe(self.transport, "Starts a recording, or stops the one running.")
        self.pause = add_button(parent, row, PAUSE, self._on_pause)
        a11y.describe(self.pause, "Pauses a recording, or carries on with a paused one.")
        self.marker = add_button(parent, row, "Drop a marker", self._on_marker)
        a11y.describe(self.marker, "Puts a marker your editor can find at this point of the "
                                   "recording.")
        sizer.Add(row, 0, wx.ALL, 6)
        # Below the buttons, so one Tab after a press reaches what the unit is doing.
        self.now = read_only(parent, sizer, "Now")

        self.multitrack = Choice(
            self, parent, sizer, "Tracks", sorted(recorder.MULTITRACK_LABEL.items()),
            recording.action_set_multitrack, *self.field("multitrack"),
            description=f"{recorder.MULTITRACK_HINT} {USB_POINTER}",
            accepted=lambda _reply: self._show_bypass())
        self.format = Choice(
            self, parent, sizer, "Save recordings as",
            sorted(recorder.RECORDING_FORMAT_LABEL.items()), recording.action_set_recording_format,
            *self.field("format"), description=recorder.RECORDING_FORMAT_HINT)
        self.bypass = Switch(self, parent, sizer, "Record the separate tracks raw",
                             recording.action_set_processing_bypass,
                             *self.field("processing_bypass"), guard=self._bypass_refused)
        self.mute = Switch(self, parent, sizer, "Silence other announcements while recording",
                           announcer.action_set_record_mute_announcer, lambda: self._mute,
                           self._remember_mute, description=MUTE_HINT)
        # The Now line's running time during a take, from the push listener: no desk read.
        self._clock = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda _e: self._tick(), self._clock)
        self.Bind(wx.EVT_SHOW, self._on_show)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self.show()

    def field(self, key):
        """`current` and `remember` for `key` in the section's recorder payload."""
        def current():
            return self.section.data.get(key)

        def remember(value):
            self.section.data[key] = value
        return current, remember

    def _remember_mute(self, on):
        self._mute = on

    def _bypass_refused(self):
        # Off and unknown both mean no separate tracks are being made.
        return None if self.section.data.get("multitrack") else recorder.BYPASS_NEEDS_MULTITRACK

    def set_sentence(self, text):
        if self.now.GetValue() != text:
            self.now.SetValue(text)

    def show(self):
        self.set_sentence(self.section.sentence)
        for control in (self.multitrack, self.format, self.bypass, self.mute):
            control.show()
        self._show_bypass()
        self.show_state()

    def show_state(self):
        self._show_transport()
        self._run_clock()

    def _show_transport(self):
        state = self.section.state
        for button, label in ((self.transport, STOP if state in RUNNING else START),
                              (self.pause, RESUME if state == recorder.PAUSED else PAUSE)):
            if button.GetLabel() != label:
                button.SetLabel(label)

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
        call(self, recording.read_live, done=self._ticked)

    def _ticked(self, reply):
        # A take stopped between the tick and its reply is left to the section's own read.
        if (reply.get("ok") and reply.get("state") == recorder.RECORDING
                and self.section.state == recorder.RECORDING):
            self.set_sentence(reply["message"])

    def _show_bypass(self):
        self.bypass.acc.description = (recorder.BYPASS_HINT if self.section.data.get("multitrack")
                                       else recorder.BYPASS_NEEDS_MULTITRACK)

    # ---- the transport ---------------------------------------------------------------------

    def _on_transport(self, event):
        state = self.section.state
        if state == recorder.NO_DESTINATION:
            self.frame.say(NOWHERE)
            return
        self.section.command(event.GetEventObject(), recording.action_record,
                             "stop" if state in RUNNING else "start")

    def _on_pause(self, event):
        state = self.section.state
        if state not in RUNNING:
            self.frame.say("Nothing is recording.")
            return
        self.section.command(event.GetEventObject(), recording.action_record,
                             "resume" if state == recorder.PAUSED else "pause")

    def _on_marker(self, event):
        # `recording` refuses a marker with nothing recording, from its listener's state.
        self.section.command(event.GetEventObject(), recording.action_drop_marker)


class RecordingsPage(wx.Panel):
    """The recordings on the card or drive, and deleting one."""

    def __init__(self, section):
        super().__init__(section.frame.host)
        self.Hide()
        self.section = section
        self.frame = section.frame
        self.takes = []              # the recordings as listed; empty when the list holds a note
        sizer, parent = page_body(self)
        self.list = labelled(parent, sizer, "Recording", lambda p: wx.ListBox(
            p, size=(460, 220)))
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
            labels = ["Nothing to list: there is no card or drive in the desk."]
        else:
            labels = ["No recordings on the desk."]
        set_items(self.list, labels)
        uids = [take["uid"] for take in self.takes]
        self.list.SetSelection(uids.index(uid) if uid in uids else 0)

    def _chosen(self):
        index = self.list.GetSelection()
        return self.takes[index] if self.takes and index != wx.NOT_FOUND else None

    def _on_delete(self, _event):
        data = self.section.data
        take = self._chosen()
        refusal = ("There are no recordings on the desk." if take is None else
                   None if data.get("can_delete") else
                   data.get("delete_note") or "Deleting is not available.")
        if refusal:
            self.frame.say(refusal)
            return
        if not confirm(self, f"Delete {recording_label(take)}?",
                       "This removes the recording from the desk's card or drive. It cannot be "
                       "undone, and AccessCaster has no copy of it.", "Delete"):
            return
        self.section.command(self.delete, recording.action_delete_recording, take["uid"],
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
        sizer, parent = page_body(self)
        self.list = labelled(parent, sizer, "Card or drive", lambda p: wx.ListBox(
            p, size=(460, 80)))
        a11y.describe(self.list, STORAGE_HINT)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.eject = add_button(parent, row, "Eject", self._on_eject)
        self.reconnect = add_button(parent, row, "Reconnect", self._on_reconnect)
        a11y.describe(self.reconnect, "Brings back a card or drive ejected from here, without "
                                      "taking it out.")
        self.erase = add_button(parent, row, "Erase", self._on_erase)
        sizer.Add(row, 0, wx.ALL, 6)

    def show(self):
        chosen = self._chosen()
        index = chosen["index"] if chosen else None
        self.shown = volumes(self.section.data)
        set_items(self.list, [volume_label(v) for v in self.shown]
                  or ["The desk reports no card slot or drive."])
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
        if self.section.data.get("storage_writable") is False:
            return NO_STORAGE_WRITE
        if volume is None:
            return "The desk reports no card slot or drive."
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
        self.section.command(self.eject, recording.action_storage_eject, volume["index"], True,
                             working="Ejecting, please wait.", reread=True)

    def _on_reconnect(self, _event):
        volume = self._chosen()
        refusal = self._refused(volume)
        if not refusal and not volume.get("ejected"):
            refusal = "It is not ejected."
        if refusal:
            self.frame.say(refusal)
            return
        self.section.command(self.reconnect, recording.action_storage_eject, volume["index"], False,
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
                        lambda dialog: call(dialog, recording.action_storage_erase, volume["index"],
                                            done=dialog.finish),
                        stop=None, closable=False)
        with job:
            reply = job.run()
        report(self, ("Erased", "Erase failed"), reply)
        self.erase.SetFocus()
        if reply.get("recorder"):
            self.section.show(reply["recorder"], self.section.sentence)
        self.section.read()
