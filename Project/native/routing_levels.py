"""Routing's Levels page: the levels the desk keeps to itself. The virtual faders with their mute and
solo, the monitor speakers and the Bluetooth output, how loud each source is in each monitor
output, and the headphone amp types. Drawn from the window's desk state, so a turn of the encoder
or a fader shows here as it happens.
"""
import wx
import wx.lib.scrolledpanel as scrolled

import server
from native import a11y
from native.calls import call
from native.controls import Slider, Switch, set_choices
from native.dialogs import confirm
from native.pages import group, labelled, page_body
from native.desk_state import CUSTOM, MIX_MINUS

TITLE = "Levels"
MODE_WORDS = {
    CUSTOM: "Custom mix.",
    MIX_MINUS: "Mix Minus. Moving a level makes it a custom mix.",
}
FOLLOWING = "Following the faders. Moving a level makes it a custom mix."
NO_CHANNEL = "This fader has no channel on it, so there is nothing to {}."
# The two outputs with a level, a mute and an auto-mute: `key` is their group in the desk state,
# `which` and `auto` name them to the mute and auto-mute writes.
OUTPUTS = [
    {"title": "Monitor speakers", "key": "monitor", "which": "monitor", "auto": "monitor",
     "set_level": server.action_set_monitor_speaker_level, "level": "Monitor speaker level",
     "level_note": "",
     "mute": "Mute the monitor speakers",
     "mute_note": "Silences the speakers only: headphones, the recording and streams carry on."},
    {"title": "Bluetooth output", "key": "bluetooth_out", "which": "bluetooth", "auto": "bt",
     "set_level": server.action_set_bt_output_level, "level": "Bluetooth output level",
     "level_note": "What a paired phone hears from the desk.",
     "mute": "Mute the Bluetooth output",
     "mute_note": "Silences what a paired phone hears; everything else carries on."},
]
AUTO_MUTE_NOTE = "Mutes the output while a mic is live, so speakers cannot feed back into the mics."


def percent(value):
    return f"{value} percent"


class LevelsPage(scrolled.ScrolledPanel):
    def __init__(self, section, model, previous=None):
        super().__init__(section.frame.host)
        self.Hide()
        self.section = section
        self.frame = section.frame
        self.model = model
        self._shown = []             # every Switch and Slider, which `show` refreshes
        sizer, parent = page_body(self)
        self._build_faders(parent, sizer, previous)
        for output in OUTPUTS:
            self._build_output(parent, sizer, output)
        self._build_mix(parent, sizer, previous)
        self._build_headphones(parent, sizer)
        self.show()
        self.SetupScrolling(scroll_x=False)

    @property
    def levels(self):
        return self.section.desk.levels

    def field(self, *path):
        """`current` and `remember` for the value at `path` in the desk state's levels. Nothing is
        remembered here: the write reaches the desk model as it is sent, and the desk state is
        rebuilt from it."""
        def current():
            node = self.levels
            for key in path:
                node = node[key]
            return node
        return current, lambda _value: None

    def _add(self, control):
        self._shown.append(control)
        return control

    # ---- the virtual faders ----------------------------------------------------------------

    def _build_faders(self, parent, sizer, previous):
        """Which virtual fader, then its level, mute and solo, named with the channel on it: a
        virtual fader means whatever is on it now, and its mute and solo have no button on the
        unit."""
        self.fader_level = None
        if not self.model["faders"]:
            return
        self.fader = labelled(parent, sizer, "Virtual fader", lambda p: wx.Choice(p))
        self.fader.Bind(wx.EVT_CHOICE, lambda _e: self._show_fader())
        self._fader_position = (previous._fader_position
                                if previous is not None and previous.fader_level else 0)
        # The list names the channel only: an item that carried the level would be rewritten on
        # every encoder step, and the screen reader reads a focused list again when it changes.
        self.fader_level = Slider(
            self, parent, sizer, "Level", 0, 100,
            self._fader_send(server.action_set_fader_level),
            lambda: self._fader_value("percent"), lambda _value: None,
            words=percent)
        self.fader_mute = Switch(
            self, parent, sizer, "Mute", self._fader_send(server.action_set_fader_mute),
            lambda: self._fader_value("muted"), lambda _on: None)
        self.fader_solo = Switch(
            self, parent, sizer, "Solo", self._fader_send(server.action_set_fader_solo),
            lambda: self._fader_value("soloed"), lambda _on: None)
        clear = wx.Button(parent, label="Clear all solos")
        clear.Bind(wx.EVT_BUTTON, self._on_clear_solos)
        sizer.Add(clear, 0, wx.ALL, 6)

    def _fader_value(self, key):
        return self.levels["faders"][self._fader_position][key]

    def _fader_send(self, write):
        """`write(fader, value)` for the fader `_show_fader` last put on the page, on a worker
        thread."""
        def send(value):
            return write(self.model["faders"][self._fader_position], value)
        return send

    def _show_faders(self):
        """The list, named from the fader layout, then the chosen fader."""
        if self.fader_level is None:
            return
        labels = self.section.desk.faders["labels"]
        set_choices(self.fader, [f"Fader {index + 1}: {labels[index]}"
                                 for index in self.model["faders"]])
        self.fader.SetSelection(min(self._fader_position, len(self.model["faders"]) - 1))
        self._show_fader()

    def _show_fader(self):
        self._fader_position = position = max(self.fader.GetSelection(), 0)
        index = self.model["faders"][position]
        empty = self.levels["faders"][position]["muted"] is None
        name = f"Fader {index + 1}" if empty else self.section.desk.faders["labels"][index]
        self.fader_level.set_label(f"{name} level")
        for switch, verb in ((self.fader_mute, "Mute"), (self.fader_solo, "Solo")):
            if switch.box.GetLabel() != f"{verb} {name}":
                switch.box.SetLabel(f"{verb} {name}")
        self.fader_mute.acc.description = NO_CHANNEL.format("mute") if empty else ""
        self.fader_solo.acc.description = (NO_CHANNEL.format("solo") if empty
                                           else "Only this channel in your headphones.")
        for control in (self.fader_level, self.fader_mute, self.fader_solo):
            control.show()

    def _on_clear_solos(self, event):
        source = event.GetEventObject()
        call(self, server.action_clear_solos, done=lambda reply: self.frame.reply(source, reply))

    # ---- the monitor speakers and the Bluetooth output -------------------------------------

    def _build_output(self, parent, sizer, output):
        """One of `OUTPUTS`: its level, its mute and its auto-mute."""
        box_sizer, box = group(parent, sizer, output["title"])
        level = self._add(Slider(self, box, box_sizer, output["level"], 0, 100,
                                 output["set_level"], *self.field(output["key"], "percent"),
                                 words=percent))
        level.acc.description = output["level_note"]
        self._add(Switch(self, box, box_sizer, output["mute"],
                         lambda on: server.action_set_output_mute(output["which"], on),
                         *self.field(output["key"], "muted"), description=output["mute_note"]))
        label = dict(self.model["auto_mute"]).get(output["auto"])
        if label:
            self._add(Switch(self, box, box_sizer, label,
                             lambda on: server.action_set_auto_mute(output["auto"], on),
                             *self.field("auto_mute", output["auto"]),
                             description=AUTO_MUTE_NOTE))

    # ---- the monitor mixes -----------------------------------------------------------------

    def _build_mix(self, parent, sizer, previous):
        """An output to adjust, then a level per source in it. Moving one makes the output a
        Custom mix with that source at a level of its own."""
        mix = self.model["monitor_mix"]
        self.mix_outputs = mix["outputs"]
        self.mix_sliders = {}
        if not self.mix_outputs:
            return
        box_sizer, box = group(parent, sizer, "Monitor mix levels")
        self.mix_output = labelled(box, box_sizer, "Output to adjust", lambda p: wx.Choice(
            p, choices=[o["label"] for o in self.mix_outputs]))
        self.mix_output_acc = a11y.describe(self.mix_output)
        self.mix_output.SetSelection(previous.mix_output.GetSelection() if previous else 0)
        self.mix_output.Bind(wx.EVT_CHOICE, lambda _e: self._show_mix())
        for source in mix["sources"]:
            self.mix_sliders[source["index"]] = Slider(
                self, box, box_sizer, source["label"], 0, 100,
                lambda pct, s=source["index"]: self._send_mix(s, pct),
                lambda s=source["index"]: self._mix_percent(s), lambda _value: None,
                words=percent)
        reset = wx.Button(box, label="Reset this output to follow the faders")
        reset.Bind(wx.EVT_BUTTON, self._on_mix_reset)
        box_sizer.Add(reset, 0, wx.ALL, 6)
        self._mix_index = self.mix_outputs[self.mix_output.GetSelection()]["index"]

    def _mix_cell(self, source):
        cells = (self.levels["mix"].get("levels") or {}).get(self._mix_index) or {}
        return cells.get(source) or {}

    def _mix_percent(self, source):
        # A source slot the desk does not fill reads -1.
        level = self._mix_cell(source).get("level")
        return None if level is None or level < 0 else round(level * 100)

    def _send_mix(self, source, pct):
        """On a worker thread, so the output is the one `_show_mix` last put on the page rather
        than a read of the list."""
        return server.action_set_monitor_level(self._mix_index, source, pct / 100)

    def _show_mix(self):
        if not self.mix_sliders:
            return
        out = self.mix_outputs[max(self.mix_output.GetSelection(), 0)]
        self._mix_index = out["index"]
        self.show_mix_mode()
        for source in self.model["monitor_mix"]["sources"]:
            slider = self.mix_sliders[source["index"]]
            slider.set_label(f"{source['label']} in {out['label']}")
            slider.acc.description = ("Follows its fader."
                                      if self._mix_cell(source["index"]).get("linked") else "")
            slider.show()

    def show_mix_mode(self):
        """The shown output's mix type, on the output list's description."""
        matrix = self.section.desk.matrix
        mix = self.levels["mix"]
        if not mix.get("ok") or not matrix.get("ok"):
            self.mix_output_acc.description = (mix.get("reason") or matrix.get("reason")
                                               or "The desk's mix levels could not be read.")
            return
        self.mix_output_acc.description = MODE_WORDS.get(matrix["modes"].get(self._mix_index),
                                                         FOLLOWING)

    def _on_mix_reset(self, event):
        source = event.GetEventObject()
        out = self.mix_outputs[max(self.mix_output.GetSelection(), 0)]
        if not confirm(self, f"Reset {out['label']} to follow the faders?",
                       f"Every source in {out['label']} goes back to its fader's level, and the "
                       f"output to the Main Mix. Its own levels are lost.", "Reset"):
            self.frame.say(f"Cancelled. {out['label']} was not changed.")
            return
        self.frame.say("Resetting, please wait.")

        def done(reply):
            self.frame.reply(source, reply)
            # The desk pushes back neither the links nor the mode.
            self.section.desk.refresh()
        call(self, server.action_reset_monitor_output, out["index"], done=done)

    # ---- the headphones --------------------------------------------------------------------

    def _build_headphones(self, parent, sizer):
        """An amp type per headphone output, applied by its button: arrowing a closed list fires a
        change per item, and High sensitivity is loud."""
        hp = self.model["headphones"]
        self.hp_types = []
        if not hp["outputs"]:
            return
        labels = [label for _value, label in hp["values"]]
        for position, output in enumerate(hp["outputs"]):
            choice = labelled(parent, sizer, f"{output} type",
                              lambda p: wx.Choice(p, choices=labels))
            a11y.describe(choice, "High sensitivity is louder. Change it with care.")
            apply = wx.Button(parent, label=f"Apply {output} type")
            apply.Bind(wx.EVT_BUTTON, lambda e, p=position: self._on_apply_type(e, p))
            sizer.Add(apply, 0, wx.ALL, 6)
            self.hp_types.append(choice)

    def _show_headphones(self):
        values = [value for value, _label in self.model["headphones"]["values"]]
        for position, choice in enumerate(self.hp_types):
            now = self.levels["headphone_types"][position]
            choice.SetSelection(values.index(now) if now in values else wx.NOT_FOUND)

    def _on_apply_type(self, event, position):
        source = event.GetEventObject()
        index = self.hp_types[position].GetSelection()
        if index == wx.NOT_FOUND:
            self.frame.say("Choose a headphone type first.")
            return
        value, label = self.model["headphones"]["values"][index]
        output = self.model["headphones"]["outputs"][position]
        if value == self.levels["headphone_types"][position]:
            self.frame.say(f"{output} is already {label}.")
            return

        def applied(reply):
            self.frame.reply(source, reply)
            if reply.get("ok"):
                self.levels["headphone_types"][position] = value
            else:
                self._show_headphones()
        call(self, server.action_set_headphone_type, value, position, done=applied)

    # ---- showing the desk state ------------------------------------------------------------

    def show(self):
        """Put the desk state's levels on every control."""
        self._show_faders()
        for control in self._shown:
            control.show()
        self._show_mix()
        self._show_headphones()

    def show_outputs(self, outputs):
        """A push or a write changed the mode or mix of `outputs`."""
        if self.mix_sliders and self._mix_index in outputs:
            self.show_mix_mode()
