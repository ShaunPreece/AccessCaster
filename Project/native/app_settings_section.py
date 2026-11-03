"""App settings: everything about AccessCaster itself rather than the unit. Announcements, keyboard
shortcuts, level warnings, starting with Windows and moving the settings, and updates.

None of these reads the desk (bar the shortcut catalogue, read when Add is pressed), so the section
reads every page when the service is up and on F5, and a page after any write whose reply does not
carry what it changed.
"""
import json

import wx
import wx.lib.scrolledpanel as scrolled

import announcer
import app_updates
import server
import speech
from native import a11y
from native.calls import call
from native.controls import Choice, Decimal, Number, Slider, Switch
from native.dialogs import confirm, report
from native.pages import Page, group, page_box, read_only, set_text
from native.shortcuts_page import ShortcutsPage
from native.update_dialogs import decide

TITLE = "App settings"
NO_LEVEL = "Choose a stream above and its Windows level will appear here."
CHECKING_AFTER_MS = 500
LEVEL_HINT = ("Zero means no change. Set your gain on the unit. Affects every application on this "
              "computer.")


class AppSettingsSection:
    title = TITLE
    route = "settings"

    def __init__(self, frame):
        self.frame = frame
        self.announcements = AnnouncementsPage(self)
        self.shortcuts = ShortcutsPage(self)
        self.levels = LevelsPage(self)
        self.general = GeneralPage(self)
        self.updates = UpdatesPage(self)
        self.views = [self.announcements, self.shortcuts, self.levels, self.general,
                      self.updates]
        self.pages = [Page("speak", "Announcements", self.announcements),
                      Page("shortcuts", "Keyboard shortcuts", self.shortcuts),
                      Page("levels", "Level warnings", self.levels),
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
         "the unit itself."),
        ("levels", "Announce fader and monitor levels aloud", announcer.action_set_announce_levels,
         "Says the name and level of whatever you turn: a fader, the monitor, or the channel the "
         "encoder is set to."),
        ("mute_tones", "Play a tone for mute instead of speaking it",
         announcer.action_set_announce_mute_tones,
         "Falling notes mean muted, rising notes mean unmuted. Quicker than speech, but it does "
         "not say which channel."),
        ("record_tones", "Play a tone for recording instead of speaking it",
         announcer.action_set_announce_record_tones,
         "One long beep means recording, two beeps paused, three beeps stopped, whether you press "
         "record here, with a shortcut or on the unit."),
        ("remember", "Start with announcements the way I left them",
         announcer.action_set_announce_remember,
         "Without this, announcements switch back on every time the app starts."),
    ]

    def __init__(self, section):
        super().__init__(section.frame.host)
        self.Hide()
        self.section = section
        self.frame = section.frame
        self.data = None             # the last `server.read_announce`
        sizer, parent = page_box(self, "Announce Interactions on Mixer")
        self.switches = [Switch(self, parent, sizer, label, send, *self.field(key),
                                description=description, guard=self._unread)
                         for key, label, send, description in self.SWITCHES]
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
        # Quiet: the server speaks each new level in the announcer's own voice, at that level.
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
        # At zero the server says nothing, so the window says it.
        if reply.get("volume") == 0:
            self.frame.reply(self.volume.slider, reply)

    def read(self, announce=False):
        def done(reply):
            self.show(reply)
            if announce:
                self.frame.say("Announcement settings read.")
        call(self, server.read_announce, done=done)

    def show(self, data):
        self.data = data
        for switch in self.switches:
            switch.show()
        self.voice.set_items([("", "Windows default voice")]
                             + [(name, name) for name in data.get("voices") or []])
        self.output.set_items([("", "Windows default output")]
                              + [(name, name) for name in data.get("outputs") or []])
        for control in (self.voice, self.output, self.volume):
            control.show()

    def _on_refresh(self, event):
        button = event.GetEventObject()

        def done(reply):
            self.frame.reply(button, reply)
            self.read()
        call(self, speech.action_refresh_speech_devices, done=done)


class LevelsPage(scrolled.ScrolledPanel):
    """The stream the background watch listens to, Windows' own level for it, and the watch."""

    def __init__(self, section):
        super().__init__(section.frame.host)
        self.Hide()
        self.section = section
        self.frame = section.frame
        self.data = None             # the last `server.read_meters`
        sizer, parent = page_box(self, "Level warnings")
        self._sizer = sizer
        self.status = read_only(parent, sizer, "Level warnings status", height=60)
        self.status.SetValue("Reading the unit's audio streams.")

        box_sizer, box = group(parent, sizer, "Which stream to watch")
        self.stream = Choice(self, box, box_sizer, "Audio device to watch", [],
                             server.action_set_meter_stream, *self.field("chosen"),
                             description="To check one channel, use Check this channel's level "
                                         "on the Channels page.",
                             guard=self._no_streams, accepted=lambda _r: self.read())

        box_sizer, box = group(parent, sizer, "Windows level")
        self._level_sizer = box_sizer
        self.no_level = read_only(box, box_sizer, "Windows level status", height=None)
        self.no_level.SetValue(NO_LEVEL)
        self.level = Decimal(self, box, box_sizer, "Windows level, dB", -96.0, 12.0, 0.5, 1,
                             server.action_set_input_level, *self.level_field("db"),
                             guard=self._level_refused, accepted=self._level_set)
        self.unity = wx.Button(box, label="Set it to no change")
        self.unity.Bind(wx.EVT_BUTTON, self._on_unity)
        box_sizer.Add(self.unity, 0, wx.ALL, 6)
        self.muted = Switch(self, box, box_sizer, "Muted in Windows",
                            server.action_set_input_mute, *self.level_field("muted"),
                            description="A muted input reads zero everywhere, and nothing "
                                        "recorded from it has any sound.",
                            guard=self._no_level)

        box_sizer, box = group(parent, sizer, "Warn me while I work")
        self.watch = Switch(self, box, box_sizer, "Watch this stream and warn me",
                            server.action_meter_watch,
                            lambda: ((self.data or {}).get("live") or {}).get("running"),
                            lambda _on: None, guard=self._no_streams,
                            accepted=lambda _r: self.read())
        self.options = [
            self._option(box, box_sizer, "warn_clip", "Say when it is clipping"),
            self._option(box, box_sizer, "clip_sound", "Play a sound when it clips"),
            Number(self, box, box_sizer, "Warn me at, dB", -20, 0,
                   lambda v: server.action_set_meter_option("clip_db", float(v)),
                   *self.setting("clip_db", whole=True),
                   description="Zero is where sound is actually damaged. The unit's own limiter "
                               "holds processed speech just under that, so a lower setting will "
                               "fire on every sentence."),
            self._option(box, box_sizer, "warn_silence",
                         "Say when it goes silent, and when it comes back"),
            Number(self, box, box_sizer, "Seconds of silence before it says so", 1, 60,
                   lambda v: server.action_set_meter_option("silence_after", float(v)),
                   *self.setting("silence_after", whole=True),
                   description="Five is long enough to ignore a pause for breath."),
            Number(self, box, box_sizer, "Speak the level every, seconds, 0 for never", 0, 600,
                   lambda v: server.action_set_meter_option("speak_every", float(v)),
                   *self.setting("speak_every", whole=True),
                   description="Leave at 0 while you are talking. Try 10 or 20 while setting "
                               "gains."),
        ]
        self._show_level()
        self.SetupScrolling(scroll_x=False)

    def _option(self, box, box_sizer, name, label):
        return Switch(self, box, box_sizer, label,
                      lambda on: server.action_set_meter_option(name, 1.0 if on else 0.0),
                      *self.setting(name))

    def field(self, key):
        def current():
            return self.data.get(key) if self.data else None

        def remember(value):
            if self.data:
                self.data[key] = value
        return current, remember

    def setting(self, key, whole=False):
        def current():
            value = ((self.data or {}).get("settings") or {}).get(key)
            return round(value) if whole and value is not None else value

        def remember(value):
            if self.data:
                self.data["settings"][key] = value
        return current, remember

    def level_field(self, key):
        def current():
            return ((self.data or {}).get("input_level") or {}).get(key)

        def remember(value):
            if self.data and self.data.get("input_level"):
                self.data["input_level"][key] = value
        return current, remember

    def _no_streams(self):
        if self.data is None:
            return "The audio streams have not been read yet."
        if not self.data.get("available"):
            return self.data.get("reason") or "There is nothing to meter at the moment."
        return None

    def _no_level(self):
        if self._no_streams():
            return self._no_streams()
        return None if self.data.get("input_level") else NO_LEVEL

    def _level_refused(self):
        refusal = self._no_level()
        if refusal:
            return refusal
        level = self.data["input_level"]
        if level.get("fixed"):
            return level.get("note") or "Windows cannot change this device's level."
        return None

    def _level_set(self, reply):
        # The level shown is the one read back: a driver can refuse a level silently.
        if reply.get("level") and self.data:
            self.data["input_level"].update(reply["level"])
            self.level.show()

    def _on_unity(self, _event):
        refusal = self._level_refused()
        if refusal:
            self.frame.say(refusal)
            return

        def done(reply):
            self._level_set(reply)
            self.frame.reply(self.unity, reply)
        call(self, server.action_set_input_level, 0.0, done=done)

    def read(self, announce=False):
        def done(reply):
            self.show(reply)
            if announce:
                self.frame.say("Level warnings read.")
        call(self, server.read_meters, done=done)

    def show(self, data):
        self.data = data
        # Unavailable is a normal state: over Remote Desktop the unit's audio belongs to another
        # session, and while the unit is a drive its audio device is gone.
        text = (data.get("mix_note") or "") if data.get("available") else (
            data.get("reason") or "No streams to meter.")
        set_text(self.status, text)
        self._sizer.Show(self.status.GetContainingSizer(), bool(text))
        self.stream.set_items([(s["id"], s.get("label") or s["name"])
                               for s in data.get("streams") or []])
        if not data.get("chosen_present"):
            data["chosen"] = None
        self.stream.show()
        self._show_level()
        live = data.get("live") or {}
        self.watch.acc.description = (
            "Watches in the background and speaks only when something needs saying. "
            + (live.get("error") or (f"Watching {live['name']}." if live.get("running")
                                     else "Not watching.")))
        self.watch.show()
        for option in self.options:
            option.show()
        self.Layout()

    def _show_level(self):
        level = (self.data or {}).get("input_level")
        self.no_level.GetContainingSizer().ShowItems(not level)
        self.level.control.GetContainingSizer().ShowItems(bool(level))
        self._level_sizer.Show(self.unity, bool(level))
        self._level_sizer.Show(self.muted.box, bool(level))
        if level:
            self.level.set_range(level.get("min_db", -96.0), level.get("max_db", 12.0))
            self.level.acc.description = LEVEL_HINT + (
                f" {level['note']}" if level.get("note") else "")
            self.level.show()
            self.muted.show()
        self.Layout()
        self.FitInside()


class GeneralPage(wx.Panel):
    """Starting with Windows, and saving this app's settings to a file or restoring them."""

    def __init__(self, section):
        super().__init__(section.frame.host)
        self.Hide()
        self.section = section
        self.frame = section.frame
        self.data = None             # the last `server.read_autostart`
        sizer, parent = page_box(self, "General")
        self.autostart = Switch(self, parent, sizer,
                                "Start AccessCaster when you sign in to Windows",
                                server.action_set_autostart,
                                lambda: (self.data or {}).get("enabled"), lambda _on: None,
                                guard=self._autostart_refused, accepted=lambda _r: self.read())
        box_sizer, box = group(parent, sizer, "Move this app's settings")
        self.export = self._button(box, box_sizer, "Save this app's settings to a file",
                                   self._on_export)
        a11y.describe(self.export, "Your keyboard shortcuts, announcement preferences, level "
                                   "warnings and CallMe contacts belong to AccessCaster rather "
                                   "than to the unit, so no backup of the unit carries them. This "
                                   "writes one small file you can copy to another computer. It "
                                   "holds nothing about the desk, so it is safe to email to "
                                   "yourself.")
        self.folder = self._button(box, box_sizer, "Open the folder", self._on_folder)
        self.restore = self._button(box, box_sizer, "Restore from a saved settings file",
                                    self._on_restore)
        a11y.describe(self.restore, "A voice or audio device this computer lacks is skipped, "
                                    "and said.")

    def _button(self, parent, sizer, label, handler):
        button = wx.Button(parent, label=label)
        button.Bind(wx.EVT_BUTTON, handler)
        sizer.Add(button, 0, wx.ALL, 6)
        return button

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
            if announce:
                self.frame.say("General settings read.")
        call(self, server.read_autostart, done=done)

    def _on_export(self, _event):
        call(self, server.action_export_app_settings,
             done=lambda reply: self._report(("Settings saved", "Settings not saved"), reply,
                                             self.export))

    def _on_folder(self, _event):
        call(self, server.action_open_app_settings_folder,
             done=lambda reply: self.frame.reply(self.folder, reply))

    def _report(self, titles, reply, source):
        report(self, titles, reply)
        source.SetFocus()

    def _on_restore(self, _event):
        with wx.FileDialog(self, "Restore from a saved settings file",
                           defaultDir=server.app_settings_dir(),
                           wildcard="AccessCaster settings (*.json)|*.json",
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as chooser:
            if chooser.ShowModal() != wx.ID_OK:
                return
            path = chooser.GetPath()
        # Restoring replaces the shortcuts in use, and is undone only by an export of what was
        # there, which is exactly what may not exist.
        if not confirm(self, "Restore this app's settings?",
                       "This replaces your keyboard shortcuts and your announcement and "
                       "level-warning preferences with the ones in that file. The unit itself is "
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
    return server.action_import_app_settings(payload)


class UpdatesPage(wx.Panel):
    """How often AccessCaster looks for a new version of itself, and looking now (F12 anywhere)."""

    def __init__(self, section):
        super().__init__(section.frame.host)
        self.Hide()
        self.section = section
        self.frame = section.frame
        self.data = None             # the last `app_updates.state`
        self._checking = False
        sizer, parent = page_box(self, "Updates")
        self.days = Choice(self, parent, sizer, "Check for updates", [], app_updates.set_interval,
                           *self.field("days"), guard=self._unread, accepted=self._interval_set)
        self.hint = read_only(parent, sizer, "Update status", height=60)
        self.check_button = wx.Button(parent, label="Check for updates now")
        a11y.describe(self.check_button, "F12 does the same from anywhere in the window.")
        self.check_button.Bind(wx.EVT_BUTTON, lambda _e: self.check())
        sizer.Add(self.check_button, 0, wx.ALL, 6)

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
        """What the last update did, from this side of its restart."""
        def take():
            return {"ok": True, "report": app_updates.take_report()
                    if app_updates.report_waiting() else None}

        def done(reply):
            found = reply.get("report")
            if found:
                self.frame.say(found["text"])
        call(self, take, done=done)

    def check(self):
        """Check now. A version already found opens the decision without asking the network
        again; the server decides that."""
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
