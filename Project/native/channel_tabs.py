"""The tabs of a channel page. Each is built once from the channel record and the section's read,
the first time it is shown (`Tab.build`), and `show(rec)` puts a newer record's values on the same
controls.

Every control reads and records through `page.rec`, never a record captured at build time: a
re-read replaces the record.
"""
import wx
import wx.lib.scrolledpanel as scrolled

import server
from native import a11y
from native.calls import call
from native.channel_jobs import AutoGain
from native.controls import Slider, Switch, set_choices
from native.dialogs import DeletePresetDialog
from native.pages import group, labelled

# Which tab each of the server's parameter groups (server.PROCESSING_CONTROLS) goes on, so the
# strip stays one row. A group not listed here gets a tab of its own.
PARAM_TABS = {
    "Equaliser": ("eq", "Equaliser"),
    "High-pass filter": ("eq", "Equaliser"),
    "Compressor": ("dyn", "Dynamics"),
    "Noise gate": ("dyn", "Dynamics"),
    "De-esser": ("dyn", "Dynamics"),
    "Tone shaping": ("tone", "Tone and pan"),
    "Pan": ("tone", "Tone and pan"),
}

# Echo and reverb parameters in the order shown, with the words after the effect's name.
EFFECT_PARAMS = {
    "echo": [("echoMix", "level"), ("echoLowCut", "low cut"), ("echoHighCut", "high cut"),
             ("echoDelay", "delay"), ("echoDecay", "decay")],
    "reverb": [("reverbMix", "level"), ("reverbModel", "size"), ("reverbLowCut", "low cut"),
               ("reverbHighCut", "high cut")],
}


def percent_words(value):
    return f"{value} percent"


def pan_words(value):
    """A pan position, 0 to 100 with 50 the centre, in words: a bare percentage gives no
    direction."""
    if value == 50:
        return "centre"
    if value <= 0:
        return "hard left"
    if value >= 100:
        return "hard right"
    return f"{(50 - value) * 2} percent left" if value < 50 else f"{(value - 50) * 2} percent right"


def pan_note(rec):
    """A pan control's description: pan is part of the processing chain, so with processing off
    it does nothing."""
    if rec.get("processing_on") is False:
        return "Processing is off for this channel, so pan does nothing until it is on."
    return ""


def no_strip_note(rec, what):
    if rec.get("on_fader") is False:
        return (f" {rec['label']} is not on a fader, so it has no {what}. Put it on a fader on the "
                f"Routing page.")
    return ""


def build_tabs(page):
    """The tabs for `page`'s channel, in working order: the input settings, presets, the whole
    chain's switches, the parameter detail, then effects. A bus (the SMART pads mix) gets only
    the first: its strip publishes a full chain and accepts writes, but none of it reaches the
    audio."""
    rec = page.rec
    is_mic = rec["kind"] == "mic"
    is_bus = rec["kind"] == "bus"
    tabs = [SettingsTab(page)]
    if is_mic:
        tabs.append(PresetsTab(page))
    if is_bus:
        return tabs
    tabs.append(ProcessingTab(page))
    buckets = {}
    for g in page.section.data.get("groups") or []:
        key, title = PARAM_TABS.get(g["group"], ("param." + g["group"], g["group"]))
        buckets.setdefault((key, title), []).append(g)
    for (key, title), groups in buckets.items():
        tabs.append(ParamTab(page, key, title, groups))
    if is_mic:
        tabs.append(EffectsTab(page))
    return tabs


class Tab(scrolled.ScrolledPanel):
    """One tab's page. It scrolls, and brings the focused control into view, because Dynamics
    alone holds seventeen sliders. `controls` holds every Switch and Slider for `show`.

    The panel exists from the start, so the tab control lists every tab, but its controls are made
    by `build`, the first time the tab is about to be shown: a mic page's tabs together hold the
    window's thread for about half a second. A subclass makes them in `fill`."""

    key = ""
    title = ""

    def __init__(self, page):
        super().__init__(page.book)
        self.page = page
        self.sizer = None
        self.controls = []
        self.built = False

    def build(self):
        if self.built:
            return
        self.built = True
        self.sizer = wx.BoxSizer(wx.VERTICAL)
        self.SetSizer(self.sizer)
        self.fill()
        self.SetupScrolling(scroll_x=False)
        self.show(self.page.rec)

    def fill(self):
        raise NotImplementedError

    def add(self, control):
        self.controls.append(control)
        return control

    def show(self, rec):
        for control in self.controls:
            control.show()

    def reply(self, source, reply):
        """Speak the reply to what the `source` control started (`frame.reply`)."""
        self.page.frame.reply(source, reply)


class SettingsTab(Tab):
    """Mute, listen, the level, auto gain and the level check, then a mic's input settings."""

    key = "settings"
    title = "Channel settings"

    def fill(self):
        page = self.page
        rec = page.rec
        name, label = page.name, rec["label"]
        data = page.section.data
        is_mic = rec["kind"] == "mic"
        field = page.field

        self.mute = self.add(Switch(
            page, self, self.sizer, f"Mute {label}",
            lambda on: server.action_set_channel_mute(name, on), *field("muted")))
        self.solo = self.add(Switch(
            page, self, self.sizer, f"Listen (solo) {label}",
            lambda on: server.action_set_channel_solo(name, on), *field("solo")))
        clear = wx.Button(self, label="Clear every solo")
        clear.Bind(wx.EVT_BUTTON, self._on_clear_solos)
        self.sizer.Add(clear, 0, wx.ALL, 6)

        self.wireless = None
        if is_mic:
            self.wireless = self.add(Switch(
                page, self, self.sizer, f"Mute the wireless mic on {label}",
                lambda on: server.action_set_channel_wireless_mute(name, on),
                *field("wireless_muted"),
                guard=lambda: "No wireless mics detected." if self._no_wireless() else None))

        # A bus has no level here: its level is its fader.
        if rec["kind"] != "bus":
            lo, hi = ((data["gain_min_db"], data["gain_max_db"]) if is_mic
                      else (data["level_min_db"], data["level_max_db"]))
            self.add(Slider(page, self, self.sizer, "Level", lo, hi,
                            lambda db: server.action_set_channel_level(name, db),
                            *field("level_db"), words=lambda db: f"{db} decibels"))
        # Auto gain sets a mic's preamp gain, which nothing else has. The level check works on
        # every channel but Chat: it listens on the Chat output, which leaves the caller out.
        if is_mic:
            auto = wx.Button(self, label="Set gain automatically")
            auto.Bind(wx.EVT_BUTTON, lambda _e: AutoGain(page, auto).run())
            self.sizer.Add(auto, 0, wx.ALL, 6)
        if name != "Chat":
            page.section.level_check.button(self, self.sizer, name)

        self.mic_type = None
        if is_mic:
            number = page.number
            self.add(Switch(page, self, self.sizer, "48V phantom power",
                            lambda on: server.action_set_phantom(number, on), *field("phantom")))
            self.add(Switch(page, self, self.sizer, "Invert polarity (phase)",
                            lambda on: server.action_set_polarity(number, on), *field("polarity")))
            self._mic_types = [(value, text) for value, text in data.get("mic_types") or []]
            if self._mic_types:
                self.mic_type = labelled(self, self.sizer, "Mic type", lambda p: wx.Choice(p))
                # Applied by the button, never by arrowing the list: a voicing change makes the
                # desk move the gain and replace the whole processing chain.
                apply = wx.Button(self, label="Apply mic type")
                apply.Bind(wx.EVT_BUTTON, self._on_apply_mic_type)
                self.sizer.Add(apply, 0, wx.ALL, 6)

    def show(self, rec):
        super().show(rec)
        self.mute.acc.description = (
            "Silences this channel everywhere, including the recording."
            + (no_strip_note(rec, "mute") or (
                " The desk did not report this channel's mute state, so this shows unmuted."
                if rec.get("muted") is None else "")))
        self.solo.acc.description = (
            "Only this channel in your headphones. Announcements go quiet while anything is "
            "soloed."
            + (no_strip_note(rec, "solo") or (
                " The desk did not report this channel's solo state, so this shows off."
                if rec.get("solo") is None else "")))
        if self.wireless is not None:
            self.wireless.acc.description = (
                "No wireless mics detected." if self._no_wireless() else
                "Mutes a paired RODE wireless transmitter at the mic, not the channel here."
                + (" The desk did not report this, so the switch shows unmuted."
                   if rec.get("wireless_muted") is None else ""))
        if self.mic_type is not None:
            self._fill_mic_types(rec.get("mic_type"))

    def _no_wireless(self):
        wireless = (self.page.section.data or {}).get("wireless_mics") or {}
        return bool(wireless.get("present") and not wireless.get("any_connected"))

    def _on_clear_solos(self, event):
        source = event.GetEventObject()
        call(self, server.action_clear_solos, done=lambda reply: self.reply(source, reply))

    def _fill_mic_types(self, current):
        choices = list(self._mic_types)
        if current is not None and all(value != current for value, _ in choices):
            choices.append((current, f"Other model (value {current})"))
        self._mic_type_values = [value for value, _ in choices]
        set_choices(self.mic_type, [text for _, text in choices])
        if current in self._mic_type_values:
            self.mic_type.SetSelection(self._mic_type_values.index(current))

    def _on_apply_mic_type(self, event):
        source = event.GetEventObject()
        index = self.mic_type.GetSelection()
        if index == wx.NOT_FOUND:
            return
        value = self._mic_type_values[index]
        if value == self.page.rec.get("mic_type"):
            # Rewriting the same voicing still replaces the processing chain, so it is not sent.
            self.page.frame.say(f"{self.page.rec['label']} is already set to "
                                f"{self.mic_type.GetString(index)}.")
            return
        call(self, server.action_set_mic_type, self.page.number, value,
             done=lambda reply: self._mic_type_done(source, reply))

    def _mic_type_done(self, source, reply):
        self.reply(source, reply)
        if reply.get("ok"):
            # The desk moved the gain and the processing with the voicing: read it all again.
            self.page.section.refresh(False)
        else:
            self._fill_mic_types(self.page.rec.get("mic_type"))


class PresetsTab(Tab):
    """Apply a whole processing preset, or save or delete one of the user's own. The list's first
    item says what the channel is on when that is not a preset as it stands."""

    key = "presets"
    title = "Presets"

    def fill(self):
        self.choice = labelled(self, self.sizer, "Processing preset", lambda p: wx.Choice(p))
        a11y.describe(self.choice, "Sets this channel's processing values, not whether "
                                   "processing is on.")
        self._names = []
        # Applied by the button for the same reason as mic type: arrowing a closed list fires a
        # change per item.
        apply = wx.Button(self, label="Apply preset")
        apply.Bind(wx.EVT_BUTTON, self._on_apply)
        self.sizer.Add(apply, 0, wx.ALL, 6)

        self.new_name = labelled(self, self.sizer, "Save current settings as a preset",
                                 lambda p: wx.TextCtrl(p, size=(260, -1),
                                                       style=wx.TE_PROCESS_ENTER))
        self.new_name.Bind(wx.EVT_TEXT_ENTER, self._on_save)
        save = wx.Button(self, label="Save preset")
        save.Bind(wx.EVT_BUTTON, self._on_save)
        self.sizer.Add(save, 0, wx.ALL, 6)
        delete = wx.Button(self, label="Delete a preset")
        delete.Bind(wx.EVT_BUTTON, self._on_delete)
        self.sizer.Add(delete, 0, wx.ALL, 6)

    def show(self, rec):
        now = rec.get("preset_now")
        matches = bool(now) and (now.get("source") == "device" or now.get("edited") is False)
        if not now or matches:
            first = "Not on a preset"
        elif now.get("edited") is None:
            first = f"{now['name']}, a preset since deleted"
        else:
            first = f"{now['name']}, adjusted since"
        presets = self.page.section.data.get("presets") or []
        self._names = [None] + [p["name"] for p in presets]
        set_choices(self.choice, [first] + [p["name"] + (" (custom)" if p["custom"] else "")
                                            for p in presets])
        on = now["name"] if matches else None
        self.choice.SetSelection(self._names.index(on) if on in self._names else 0)

    def _on_apply(self, event):
        source = event.GetEventObject()
        name = self._names[self.choice.GetSelection()] if self._names else None
        if name is None:
            self.page.frame.say("Choose a preset to apply.")
            return

        def applied(reply):
            self.reply(source, reply)
            if reply.get("ok"):
                # The whole chain moved: read it all again, which also shows the preset as on.
                self.page.section.refresh(False)
        call(self, server.action_apply_mic_preset, self.page.number, name, done=applied)

    def _on_save(self, event):
        source = event.GetEventObject()
        name = self.new_name.GetValue().strip()
        if not name:
            self.page.frame.say("Please enter a name for the preset.")
            self.new_name.SetFocus()
            return

        def saved(reply):
            self.reply(source, reply)
            if reply.get("ok"):
                self.new_name.SetValue("")
                # The channel is now this preset by definition: it was saved from these values.
                self.page.rec["preset_now"] = {"name": name, "edited": False, "source": "record"}
                self.page.section.presets_changed(reply.get("presets"))
        call(self, server.action_save_mic_preset, self.page.number, name, done=saved)

    def _on_delete(self, event):
        source = event.GetEventObject()
        own = [p["name"] for p in self.page.section.data.get("presets") or [] if p["custom"]]
        if not own:
            self.page.frame.say("There are no presets of your own to delete.")
            return
        with DeletePresetDialog(self, own) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            name = dialog.chosen()

        def deleted(reply):
            self.reply(source, reply)
            if reply.get("ok"):
                self.page.section.presets_changed(reply.get("presets"))
        call(self, server.action_delete_mic_preset, name, done=deleted)


class ProcessingTab(Tab):
    """The whole chain's switches: processing on, advanced or simple, and each module."""

    key = "processing"
    title = "Processing"

    def fill(self):
        page = self.page
        name = page.name
        self.add(Switch(page, self, self.sizer, "Processing on",
                        lambda on: server.action_set_processing_enabled(name, on),
                        *page.field("processing_on")))
        if page.rec.get("advanced") is not None:
            self.add(Switch(
                page, self, self.sizer, "Advanced processing (use the modules below)",
                lambda on: server.action_set_channel_advanced(name, on), *page.field("advanced"),
                description="Off, Tone shaping sets the modules for you. Your settings are kept "
                            "either way."))
        for module in page.section.data.get("modules") or []:
            self.add(Switch(
                page, self, self.sizer, module["label"],
                lambda on, key=module["key"]: server.action_set_channel_module(name, key, on),
                *page.field("modules", module["prop"])))


class ParamTab(Tab):
    """One or more parameter groups. Several groups each get a named group box, so a screen
    reader says which module a Threshold belongs to; a single group named like the tab does not."""

    def __init__(self, page, key, title, groups):
        super().__init__(page)
        self.key, self.title = key, title
        self.groups = groups
        self.pan_controls = []

    def fill(self):
        page, groups = self.page, self.groups
        name = page.name
        stereo = bool(page.rec.get("stereo"))
        single = len(groups) == 1 and groups[0]["group"] == self.title
        for g in groups:
            sizer, parent = ((self.sizer, self) if single
                             else group(self, self.sizer, g["group"]))
            # Pan comes in a mono and a stereo kind; a channel gets only its own.
            legs = []
            for ctrl in g["controls"]:
                if ctrl.get("pan") and bool(ctrl.get("stereo")) != stereo:
                    continue
                if stereo and ctrl["prop"] in server._PAN_LEGS:
                    legs.append(ctrl)
                    continue
                slider = self.add(Slider(
                    page, parent, sizer, ctrl["label"], 0, 100,
                    lambda pct, prop=ctrl["prop"]: server.action_set_channel_param(
                        name, prop, pct / 100),
                    *page.field("params", ctrl["prop"], scale=100),
                    words=pan_words if ctrl.get("pan") else percent_words, unread=50))
                if ctrl.get("pan"):
                    self.pan_controls.append(slider.acc)
            if legs:
                stereo_pan = self.add(StereoPan(self, parent, sizer, legs))
                self.pan_controls += stereo_pan.accs()

    def show(self, rec):
        super().show(rec)
        note = pan_note(rec)
        for acc in self.pan_controls:
            acc.description = note


class StereoPan:
    """A stereo channel's pan: its two legs, as the desk has them, or with Link on, one Pan
    control that shifts both from where they were when linked (`server.action_set_linked_pan`).
    All three sliders are built once and the link shows one kind or the other, so linking never
    moves the focus off the switch."""

    def __init__(self, tab, parent, sizer, legs):
        self.tab = tab
        self.page = page = tab.page
        name = page.name
        self._legs_now = self._legs()
        self.link = Switch(page, parent, sizer, "Link both sides into one pan control",
                           lambda on: server.action_link_pan(name, on, *self._legs_now),
                           self._linked, lambda _on: None, accepted=self._links_changed)
        self.leg_sliders = [Slider(
            page, parent, sizer, ctrl["label"], 0, 100,
            lambda pct, prop=ctrl["prop"]: server.action_set_channel_param(name, prop, pct / 100),
            *page.field("params", ctrl["prop"], scale=100), words=pan_words, unread=50)
            for ctrl in legs]
        self.linked = Slider(page, parent, sizer, "Pan", -100, 100,
                             lambda off: server.action_set_linked_pan(name, off, *self._legs_now),
                             self._offset, lambda _off: None, words=server.pan_offset_words,
                             unread=0, accepted=self._moved)
        self._shown_linked = None

    def accs(self):
        """The accessibility objects of every control here, for a description."""
        return [self.link.acc] + [slider.acc for slider in (*self.leg_sliders, self.linked)]

    def _legs(self):
        """The legs as the record has them, in percent; centre for one not read."""
        params = self.page.rec.get("params") or {}
        return tuple(50 if params.get(prop) is None else round(params[prop] * 100)
                     for prop in server._PAN_LEGS)

    def _links(self):
        return self.page.section.data.get("pan_links") or {}

    def _linked(self):
        return self.page.name in self._links()

    def _offset(self):
        link = self._links().get(self.page.name)
        return None if link is None else server.linked_pan_offset(link, *self._legs())

    def _links_changed(self, reply):
        self.page.section.data["pan_links"] = reply.get("pan_links") or {}
        self.show()

    def _moved(self, reply):
        params = self.page.rec.setdefault("params", {})
        for prop, pct in (reply.get("legs") or {}).items():
            params[prop] = pct / 100
        self._links_changed(reply)

    def show(self):
        # A snapshot for the writes, which run off the UI thread.
        self._legs_now = self._legs()
        self.link.show()
        for slider in (*self.leg_sliders, self.linked):
            slider.show()
        linked = self._linked()
        if linked != self._shown_linked:
            self._shown_linked = linked
            for slider in self.leg_sliders:
                slider.set_shown(not linked)
            self.linked.set_shown(linked)
            self.tab.Layout()
            self.tab.FitInside()


class EffectsTab(Tab):
    """Echo and reverb, mic channels only. A parameter's success is not spoken: the slider's value
    already is."""

    key = "fx"
    title = "Effects"

    def fill(self):
        page = self.page
        name = page.name
        for effect, params in EFFECT_PARAMS.items():
            title = effect.capitalize()
            sizer, parent = group(self, self.sizer, title)
            self.add(Switch(page, parent, sizer, title,
                            lambda on, fx=effect: server.action_set_channel_effect(name, fx, on),
                            *page.field("effects", effect)))
            for param, words in params:
                self.add(Slider(
                    page, parent, sizer, f"{title} {words}", 0, 100,
                    lambda pct, p=param: server.action_set_channel_effect_param(
                        name, p, pct / 100),
                    *page.field("effects", effect + "_params", param, scale=100),
                    words=percent_words, unread=50, quiet=True))
