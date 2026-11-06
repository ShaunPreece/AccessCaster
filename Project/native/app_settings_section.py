"""App settings: everything about AccessCaster itself rather than the unit. Announcements, keyboard
shortcuts, starting with Windows and moving the settings, and updates.

None of these reads the desk (bar the shortcut catalogue, read when Add is pressed), so the section
reads every page when the service is up and on F5, and a page after any write whose reply does not
carry what it changed.
"""
import json

import wx
import wx.lib.scrolledpanel as scrolled

import announcer
import app_settings_actions
import app_updates
import clip_warning
import speech
from native import a11y
from native.calls import call
from native.controls import Choice, Slider, Switch
from native.dialogs import confirm, report
from native.pages import AFTER_FOCUS_MS, Page, add_button, page_body, read_only, set_text
from native.shortcuts_page import ShortcutsPage
from native.update_dialogs import decide

TITLE = "App settings"
CHECKING_AFTER_MS = 500


class AppSettingsSection:
    title = TITLE
    route = "settings"

    def __init__(self, frame):
        self.frame = frame
        self.announcements = AnnouncementsPage(self)
        self.shortcuts = ShortcutsPage(self)
        self.general = GeneralPage(self)
        self.updates = UpdatesPage(self)
        self.views = [self.announcements, self.shortcuts, self.general, self.updates]
        self.pages = [Page("speak", "Announcements", self.announcements),
                      Page("shortcuts", "Keyboard shortcuts", self.shortcuts),
                      Page("general", "General", self.general),
                      Page("updates", "Updates", self.updates)]

    def start(self):
        for view in self.views:
            view.read()
        self.updates.take_report()

    def refresh(self, announce):
        shown = [view for view in self.views if view.IsShown()]
        for view in shown or self.views:
            view.read(announce)

    def check_updates(self):
        self.updates.check()


class AnnouncementsPage(scrolled.ScrolledPanel):
    """What the announcer says, in which voice, on which output, and how loud."""

    SWITCHES = [
        ("on", "Announce button presses aloud", announcer.action_set_announce,
         "Mutes, solos, bank changes, channel buttons and the encoder, including changes made on "
         "the desk itself."),
        ("levels", "Announce fader and monitor levels aloud", announcer.action_set_announce_levels,
         "Says the name and level of whatever you turn: a fader, the monitor, or the channel the "
         "encoder is set to."),
        ("mute_tones", "Play a tone for mute instead of speaking it",
         announcer.action_set_announce_mute_tones,
         "Falling notes mean muted, rising notes mean unmuted. Quicker than speech, but it does "
         "not say which channel."),
        ("clip_warning", "Warn when the main mix clips", clip_warning.action_set_clip_warning,
         "Says Main mix clipping when the desk's main mix reaches full scale. Like every "
         "announcement, it is silent during a recording unless announcements stay on while "
         "recording."),
        ("remember", "Start with announcements the way I left them",
         announcer.action_set_announce_remember,
         "Without this, announcements switch back on every time the app starts."),
    ]

    def __init__(self, section):
        super().__init__(section.frame.host)
        self.Hide()
        self.section = section
        self.frame = section.frame
        self.data = None             # the last `app_settings_actions.read_announce`
        sizer, parent = page_body(self)
        self.switches = [Switch(self, parent, sizer, label, send, *self.field(key),
                                description=description, guard=self._unread)
                         for key, label, send, description in self.SWITCHES]
        self.record_feedback = Choice(
            self, parent, sizer, "When recording starts, pauses or stops",
            list(announcer.RECORD_FEEDBACK.items()), announcer.action_set_announce_record_feedback,
            *self.field("record_feedback"),
            description="A tone is one long beep for recording, two for paused, three for "
                        "stopped, whether you press record here, with a shortcut or on the desk.",
            guard=self._unread)
        self.voice = Choice(self, parent, sizer, "Announcement voice", [("", "Windows default voice")],
                            speech.action_set_announce_voice, *self.field("voice"),
                            description="Choosing one plays a sample. Your screen reader keeps "
                                        "its own voice.",
                            guard=lambda: self._no_choice("voices"))
        self.output = Choice(self, parent, sizer, "Where announcements play",
                             [("", "Windows default output")], speech.action_set_announce_output,
                             *self.field("output"),
                             description="This computer's own speakers or headphones keep "
                                         "announcements out of your recording, and audible when "
                                         "the desk mutes or solos.",
                             guard=lambda: self._no_choice("outputs"))
        refresh = wx.Button(parent, label="Refresh voices and outputs")
        a11y.describe(refresh, "Use after plugging in headphones or installing a voice.")
        refresh.Bind(wx.EVT_BUTTON, self._on_refresh)
        sizer.Add(refresh, 0, wx.ALL, 6)
        # Quiet: `speech.action_set_announce_volume` says each new level in the announcer's own
        # voice, at that level.
        self.volume = Slider(self, parent, sizer, "Announcement volume", 0, 100,
                             speech.action_set_announce_volume, *self.field("volume"),
                             words=lambda v: "silent" if v == 0 else f"{v} percent", quiet=True,
                             accepted=self._volume_set)
        self.volume.slider.SetLineSize(5)
        self.volume.slider.SetPageSize(20)
        self.volume.acc.description = ("Announcements only. Windows, your screen reader and the "
                                       "desk are not affected.")
        self.SetupScrolling(scroll_x=False)

    def field(self, key):
        def current():
            return self.data.get(key) if self.data else None

        def remember(value):
            if self.data:
                self.data[key] = value
        return current, remember

    def _unread(self):
        return "The announcement settings have not been read yet." if self.data is None else None

    def _no_choice(self, kind):
        if self.data is None:
            return self._unread()
        if not self.data.get(kind):
            return f"Windows reported no {kind} to this computer, so there is nothing to choose."
        return None

    def _volume_set(self, reply):
        # At zero `speech.action_set_announce_volume` says nothing, so the window says it.
        if reply.get("volume") == 0:
            self.frame.reply(self.volume.slider, reply)

    def read(self, announce=False):
        def done(reply):
            self.show(reply)
            if announce:
                self.frame.say("Announcement settings read.")
        call(self, app_settings_actions.read_announce, done=done)

    def show(self, data):
        self.data = data
        for switch in self.switches:
            switch.show()
        self.voice.set_items([("", "Windows default voice")]
                             + [(name, name) for name in data.get("voices") or []])
        self.output.set_items([("", "Windows default output")]
                              + [(name, name) for name in data.get("outputs") or []])
        for control in (self.record_feedback, self.voice, self.output, self.volume):
            control.show()

    def _on_refresh(self, event):
        button = event.GetEventObject()

        def done(reply):
            self.frame.reply(button, reply)
            self.read()
        call(self, speech.action_refresh_speech_devices, done=done)


class GeneralPage(wx.Panel):
    """Starting with Windows, keeping running in the tray, and saving this app's settings to a
    file or restoring them."""

    def __init__(self, section):
        super().__init__(section.frame.host)
        self.Hide()
        self.section = section
        self.frame = section.frame
        self.data = None             # the last `app_settings_actions.read_general`
        sizer, parent = page_body(self)
        self.autostart = Switch(self, parent, sizer,
                                "Start AccessCaster when you sign in to Windows",
                                app_settings_actions.action_set_autostart,
                                lambda: (self.data or {}).get("enabled"), lambda _on: None,
                                guard=self._autostart_refused, accepted=lambda _r: self.read())
        self.keep_in_tray = Switch(
            self, parent, sizer, "Keep running in the system tray when the window is closed",
            app_settings_actions.action_set_keep_in_tray, lambda: (self.data or {}).get("keep_in_tray"),
            self._remember_tray,
            description="Your keyboard shortcuts and spoken announcements only work while "
                        "AccessCaster is running. Off, closing the window quits it.",
            guard=lambda: None if self.data is not None else "Not read yet.")
        self.export = add_button(parent, sizer, "Save this app's settings to a file",
                                   self._on_export)
        a11y.describe(self.export, "Your keyboard shortcuts, announcement preferences and "
                                   "CallMe contacts belong to AccessCaster rather than to the "
                                   "desk, so no backup of the desk carries them. This "
                                   "writes one small file you can copy to another computer. It "
                                   "holds nothing about the desk, so it is safe to email to "
                                   "yourself.")
        self.folder = add_button(parent, sizer, "Open the folder", self._on_folder)
        self.restore = add_button(parent, sizer, "Restore from a saved settings file",
                                    self._on_restore)
        a11y.describe(self.restore, "A voice or audio device this computer lacks is skipped, "
                                    "and said.")

    def _remember_tray(self, on):
        if self.data is not None:
            self.data["keep_in_tray"] = on

    def _autostart_refused(self):
        if self.data is None:
            return "Not read yet."
        return None if self.data.get("supported") else (
            self.data.get("message") or "This cannot be changed on this copy.")

    def read(self, announce=False):
        def done(reply):
            self.data = reply
            self.autostart.acc.description = reply.get("message") or (
                "It starts in the system tray, so your shortcuts work from the moment you sign "
                "in.")
            self.autostart.show()
            self.keep_in_tray.show()
            if announce:
                self.frame.say("General settings read.")
        call(self, app_settings_actions.read_general, done=done)

    def _on_export(self, _event):
        call(self, app_settings_actions.action_export,
             done=lambda reply: self._report(("Settings saved", "Settings not saved"), reply,
                                             self.export))

    def _on_folder(self, _event):
        call(self, app_settings_actions.action_open_export_folder,
             done=lambda reply: self.frame.reply(self.folder, reply))

    def _report(self, titles, reply, source):
        report(self, titles, reply)
        source.SetFocus()

    def _on_restore(self, _event):
        with wx.FileDialog(self, "Restore from a saved settings file",
                           defaultDir=app_settings_actions.export_dir(),
                           wildcard="AccessCaster settings (*.json)|*.json",
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as chooser:
            if chooser.ShowModal() != wx.ID_OK:
                return
            path = chooser.GetPath()
        # Restoring replaces the shortcuts in use, and is undone only by an export of what was
        # there, which is exactly what may not exist.
        if not confirm(self, "Restore this app's settings?",
                       "This replaces your keyboard shortcuts and your announcement "
                       "preferences with the ones in that file. The desk itself is "
                       "not touched at all.", "Restore settings"):
            return
        call(self, _import_file, path, done=self._restored)

    def _restored(self, reply):
        self._report(("Settings restored", "Settings not restored"), reply, self.restore)
        if reply.get("ok"):
            for view in self.section.views:
                view.read()


def _import_file(path):
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return {"ok": False, "message": "That file is not readable as settings. Choose the file "
                                        "AccessCaster saved."}
    return app_settings_actions.action_import(payload)


class UpdatesPage(wx.Panel):
    """How often AccessCaster looks for a new version of itself, and looking now (F12 anywhere)."""

    def __init__(self, section):
        super().__init__(section.frame.host)
        self.Hide()
        self.section = section
        self.frame = section.frame
        self.data = None             # the last `app_updates.state`
        self._checking = False
        sizer, parent = page_body(self)
        self.days = Choice(self, parent, sizer, "Check for updates", [], app_updates.set_interval,
                           *self.field("days"), guard=self._unread, accepted=self._interval_set)
        self.hint = read_only(parent, sizer, "Update status", height=60)
        add_button(parent, sizer, "Check for updates now", lambda _e: self.check(),
                   "F12 does the same from anywhere in the AccessCaster window.")

    def field(self, key):
        def current():
            return ((self.data or {}).get("settings") or {}).get(key)

        def remember(value):
            if self.data:
                self.data["settings"][key] = value
        return current, remember

    def _unread(self):
        return "The update settings have not been read yet." if self.data is None else None

    def _interval_set(self, reply):
        set_text(self.hint, reply.get("hint") or "")

    def read(self, announce=False):
        def done(reply):
            self.data = reply
            settings = reply.get("settings") or {}
            self.days.set_items([(c["days"], c["label"]) for c in settings.get("choices") or []])
            self.days.show()
            set_text(self.hint, settings.get("hint") or "")
            if announce:
                self.frame.say("Update settings read.")
        call(self, app_updates.state, done=done)

    def take_report(self):
        """What the last update did, from this side of its restart. Taken only once the window
        is the active one: a line said while it is still coming to the front is dropped by the
        screen reader, and taking the report cancels the notification that would say it instead
        (`app_updates._report_unclaimed`)."""
        if self.frame.IsActive():
            self._take_report()
        else:
            self.frame.Bind(wx.EVT_ACTIVATE, self._on_activate)

    def _on_activate(self, event):
        event.Skip()
        if event.GetActive():
            self.frame.Unbind(wx.EVT_ACTIVATE, handler=self._on_activate)
            self._take_report()

    def _take_report(self):
        def take():
            return {"ok": True, "report": app_updates.take_report()
                    if app_updates.report_waiting() else None}

        def done(reply):
            found = reply.get("report")
            if found:
                wx.CallLater(AFTER_FOCUS_MS, self.frame.say, found["text"])
        call(self, take, done=done)

    def check(self):
        """Check now. A version already found opens the decision without asking the network
        again; `app_updates.check_now` decides that."""
        if self._checking:
            return
        self._checking = True
        # Said only when the check is slow: a held offer or a quick answer needs no "checking".
        wx.CallLater(CHECKING_AFTER_MS, self._say_checking)
        call(self, app_updates.check_now, done=self._checked)

    def _say_checking(self):
        if self._checking and (self.data or {}).get("checking"):
            self.frame.say(self.data["checking"])

    def _checked(self, reply):
        self._checking = False
        if reply.get("offer"):
            decide(self, reply["offer"], self.frame.say)
        else:
            self.frame.say(reply.get("message") or "Done.")
        self.read()
