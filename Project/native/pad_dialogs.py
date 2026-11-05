"""The two things done to one pad, each a dialog of its own named with the pad: its settings, which
write as they change, and what it does, which changes nothing until Apply.
"""
import os
from collections import namedtuple

import wx

import pad_actions
import soundpad
from native import a11y
from native.calls import call
from native.controls import Choice, Number, Slider, Switch
from native.dialogs import SpeakingDialog, confirm
from native.pages import group, labelled, reading_box

READ_ONLY = "Editing SMART pads is not enabled on this device yet."
DEFAULT_MAPPING = ("This pad is on the default mapping, so this does nothing. Turn on the custom "
                   "mapping first.")
# What a MIDI trigger pad sends once its custom mapping is turned off: the desk puts the numbers
# back to these itself (`soundpad.set_trigger_mapping`), the control number being the pad's padIdx.
TRIGGER_DEFAULTS = {"channel": 1, "on": 127, "off": 0}


def where(pad):
    return f"pad {pad['pad']}, bank {pad['bank']}"


def content(pad):
    """What is on the pad, as the list and the dialogs name it: "Sound: Applause"."""
    if not pad["present"]:
        return "Empty"
    name = (pad["name"] or "").replace("_", " ")
    return f"{pad['type_label']}: {name}" if name else pad["type_label"]


def list_label(pad):
    return f"Pad {pad['pad']}. Bank {pad['bank']}. {content(pad)}"


def _seconds(units):
    return f"{units / 2:g} seconds"


class SettingsDialog(SpeakingDialog):
    """The pad's settings as it is now, only those its type has, each written as it changes. The
    controls read and record through `self.pad`, the pad's entry in the page's model, so the list
    names what was changed. `wrote` says whether anything was sent, for the page to read the pads
    again afterwards."""

    def __init__(self, parent, pad, model):
        super().__init__(parent, f"Settings for {where(pad)}, {content(pad)}")
        self.pad = pad
        self.model = model
        self.grid = pad["grid"]
        self.wrote = False
        self._controls = []
        sizer = wx.BoxSizer(wx.VERTICAL)
        self._build_name(sizer)
        self._add(Choice(self, self, sizer, "Colour",
                         [(c["value"], c["label"]) for c in model["colours"]],
                         self._write(pad_actions.action_set_pad_colour), *self.field("colour"),
                         guard=self._guard))
        kind = pad["type"]
        if kind == soundpad.PAD_TYPE_SOUND:
            self._build_sound(sizer)
        elif kind == soundpad.PAD_TYPE_MIXER:
            self._build_mixer(sizer)
        elif kind == soundpad.PAD_TYPE_TRIGGER:
            self._build_trigger(sizer)
        close = wx.Button(self, wx.ID_CANCEL, "Close")
        sizer.Add(close, 0, wx.ALIGN_RIGHT | wx.ALL, 10)
        sizer.Add(self.sink, 0)
        self.SetEscapeId(wx.ID_CANCEL)
        for control in self._controls:
            control.show()
        self._show_loop()
        self._show_trigger()
        self.SetSizerAndFit(sizer)
        self.CentreOnParent()

    def field(self, *path, scale=1):
        """`current` and `remember` for the value at `path` in the pad, shown as a whole number of
        1/`scale` units."""
        def holder():
            record = self.pad
            for key in path[:-1]:
                record = record[key]
            return record

        def current():
            value = holder()[path[-1]]
            return value if scale == 1 or value is None else round(value * scale)

        def remember(value):
            holder()[path[-1]] = value if scale == 1 else value / scale
        return current, remember

    def _write(self, action, *args, scale=1):
        """`send` for a control: `action(grid, *args, value)`, the value in the desk's units."""
        def send(value):
            self.wrote = True
            return action(self.grid, *args, value if scale == 1 else value / scale)
        return send

    def _add(self, control):
        self._controls.append(control)
        return control

    def _guard(self):
        return READ_ONLY if self.model["read_only"] else None

    # ---- every pad ----------------------------------------------------------------------------

    def _build_name(self, sizer):
        """The name, written by Rename or Enter rather than per keystroke."""
        line = wx.BoxSizer(wx.HORIZONTAL)
        line.Add(wx.StaticText(self, label="Name"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.name = wx.TextCtrl(self, value=(self.pad["name"] or "").replace("_", " "),
                                size=(240, -1), style=wx.TE_PROCESS_ENTER)
        self.name.SetMaxLength(40)
        line.Add(self.name, 0, wx.RIGHT, 8)
        rename = wx.Button(self, label="Rename")
        line.Add(rename, 0)
        sizer.Add(line, 0, wx.ALL, 6)
        self.name.Bind(wx.EVT_TEXT_ENTER, self._on_rename)
        rename.Bind(wx.EVT_BUTTON, self._on_rename)

    def _on_rename(self, event):
        source = event.GetEventObject()
        refusal = self._guard()
        name = self.name.GetValue().strip()
        if refusal or not name:
            self.say(refusal or "Enter a name for the pad.")
            return
        self.wrote = True

        def done(reply):
            if reply.get("ok"):
                self.pad["name"] = name
            self.reply(source, reply)
        call(self, pad_actions.action_set_pad_name, self.grid, name, done=done)

    # ---- sound pads ---------------------------------------------------------------------------

    def _build_sound(self, sizer):
        model = self.model
        self._add(Slider(self, self, sizer, "Volume", -120, 24,
                         self._write(pad_actions.action_set_pad_gain, scale=2),
                         *self.field("gain", scale=2),
                         words=lambda units: f"{units / 2:g} dB", unread=-24))
        self._add(Choice(self, self, sizer, "How the pad plays",
                         [(m["value"], m["label"]) for m in model["play_modes"]],
                         self._write(pad_actions.action_set_play_mode), *self.field("play_mode"),
                         description="Toggle: one press starts the sound and the next stops it. "
                                     "Hold: it plays only while you hold the pad. One Shot: each "
                                     "press replays it from the start, and it cannot loop.",
                         guard=self._guard, accepted=self._play_mode_set))
        self._add(Switch(self, self, sizer, "Restart from the beginning on each press",
                         self._write(pad_actions.action_set_replay), *self.field("replay"),
                         guard=self._guard))
        self.loop = self._add(Switch(self, self, sizer, "Loop: repeat until stopped",
                                     self._write(pad_actions.action_set_pad_loop),
                                     *self.field("loop"), guard=self._guard))
        box_sizer, box = group(self, sizer, "Trim and fade")
        for f in model["extra_edit"]["envelope"]:
            slider = self._add(Slider(self, box, box_sizer, f["label"], 0, 100,
                                      self._write(pad_actions.action_set_pad_envelope, f["key"]),
                                      *self.field("envelope", f["key"], scale=100),
                                      words=lambda units: f"{units} percent"))
            slider.acc.description = f["help"]

    def _play_mode_set(self, reply):
        if reply.get("loop_off"):
            self.pad["loop"] = False
            self.loop.show()
        self._show_loop()

    def _show_loop(self):
        """Loop is hidden in One Shot, which cannot loop."""
        if self.pad["type"] != soundpad.PAD_TYPE_SOUND:
            return
        shown = self.pad["play_mode"] != self.model["play_mode_one_shot"]
        if self.loop.box.IsShown() != shown:
            self.loop.box.Show(shown)
            self.Layout()
            self.Fit()

    # ---- mixer pads ---------------------------------------------------------------------------

    def _build_mixer(self, sizer):
        pad, model = self.pad, self.model
        self._add(Choice(self, self, sizer, "How the pad triggers",
                         [(t["value"], t["label"]) for t in model["mixer_triggers"]],
                         self._write(pad_actions.action_set_mixer_trigger), *self.field("mixer_trigger"),
                         guard=self._guard))
        if pad["fade"] is not None:
            box_sizer, box = group(self, sizer, "Fade times")
            for f in model["extra_edit"]["fade"]:
                slider = self._add(Slider(self, box, box_sizer, f["label"], 0, int(f["max"] * 2),
                                          self._write(pad_actions.action_set_pad_fade, f["key"], scale=2),
                                          *self.field("fade", f["key"], scale=2), words=_seconds))
                slider.acc.description = f["help"]
            self._add(Switch(self, box, box_sizer, "Leave the host's mic out of the fade",
                             self._write(pad_actions.action_set_pad_flag, "fade_exclude_host"),
                             *self.field("fade", "exclude_host"), guard=self._guard))
        if pad["censor"] is not None:
            file = (pad["censor"]["file"] or "").split("/")[-1]
            self._add(Switch(self, self, sizer, "Use my own sound instead of the beep",
                             self._write(pad_actions.action_set_pad_flag, "censor_custom"),
                             *self.field("censor", "custom"), guard=self._guard,
                             description=(f"The sound on the desk for this pad is {file}." if file
                                          else "No sound of your own is on the desk for this pad, "
                                               "so it plays nothing. Put one on the desk itself; "
                                               "this app cannot.")))
        if pad["back_channel"] is not None:
            box_sizer, box = group(self, sizer, "Who is in the Back Channel")
            for source in model["back_channel_sources"]:
                self._add(Switch(self, box, box_sizer, source["label"],
                                 self._write(pad_actions.action_set_back_channel, source["prop"]),
                                 *self.field("back_channel", source["prop"]), guard=self._guard,
                                 description="While the pad is on, Mic 1 and everyone ticked talk "
                                             "privately. With nobody ticked the pad does nothing."))

    # ---- MIDI trigger pads --------------------------------------------------------------------

    def _build_trigger(self, sizer):
        spec = self.model["trigger_edit"]
        box_sizer, box = group(self, sizer, "What this pad sends")
        self._add(Switch(self, box, box_sizer, "Use a custom mapping instead of the default",
                         self._write(pad_actions.action_set_pad_trigger, "custom"),
                         *self.field("trigger", "custom"), guard=self._guard,
                         accepted=self._custom_set,
                         description="Switching it off forgets your numbers, as the desk itself "
                                     "does."))
        for f in spec["fields"]:
            send = self._write(pad_actions.action_set_pad_trigger, f["key"])
            if f["choices"]:
                self._add(Choice(self, box, box_sizer, f["label"],
                                 [(c["value"], c["label"]) for c in spec[f["choices"]]], send,
                                 *self.field("trigger", f["key"]), description=f["help"],
                                 guard=self._trigger_guard, accepted=self._trigger_set))
            else:
                self._add(Number(self, box, box_sizer, f["label"], f["min"], f["max"], send,
                                 *self.field("trigger", f["key"]), description=f["help"],
                                 guard=self._trigger_guard, accepted=self._trigger_set))
        self.sending = labelled(box, box_sizer, "Now sending", lambda p: wx.TextCtrl(
            p, size=(420, 60), style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_NO_VSCROLL))

    def _trigger_guard(self):
        if self.model["read_only"]:
            return READ_ONLY
        return None if self.pad["trigger"]["custom"] else DEFAULT_MAPPING

    def _custom_set(self, _reply):
        trigger = self.pad["trigger"]
        if not trigger["custom"]:
            trigger.update(TRIGGER_DEFAULTS, control=self.pad["padIdx"])
            for control in self._controls:
                control.show()
        self._show_trigger()

    def _trigger_set(self, _reply):
        self._show_trigger()

    def _show_trigger(self):
        """What the pad sends, in a sentence. With the custom mapping off it is the default for the
        pad's position, never read from the record: turning custom off leaves padTriggerType
        where it was, so the record can say Note on a pad sending a control change."""
        if self.pad["type"] != soundpad.PAD_TYPE_TRIGGER:
            return
        t = self.pad["trigger"]
        if not t["custom"]:
            text = (f"The default mapping: control change {self.pad['padIdx']} on channel 1, "
                    "value 127 when pressed and 0 when released.")
        else:
            modes = {m["value"]: m["label"] for m in self.model["trigger_edit"]["modes"]}
            kind = "note" if t["type"] == 1 else "control change"
            text = (f"{kind.capitalize()} {t['control']} on channel {t['channel']}, value "
                    f"{t['on']} when pressed and {t['off']} when released, "
                    f"{modes.get(t['mode'], 'Latching').lower()}.")
        self.sending.SetValue(text)


# The plan a Change dialog hands back: what the job dialog says while it runs, the server call and
# its arguments, and whether the pad ends up empty.
Plan = namedtuple("Plan", "working action args clears")

TYPES = [("empty", "Empty"), ("sound", "Sound"), ("fx", "Voice effect"),
         ("mixer", "Mixer action"), ("trigger", "MIDI trigger")]
TYPE_KEY = {soundpad.PAD_TYPE_SOUND: "sound", soundpad.PAD_TYPE_VOICE_FX: "fx",
            soundpad.PAD_TYPE_MIXER: "mixer", soundpad.PAD_TYPE_TRIGGER: "trigger"}
APPLY_HELP = {
    "empty": "Empties the pad.",
    "sound": "Puts the chosen sound on the pad. Its settings open next.",
    "fx": "Puts the chosen voice effect on the pad. Its settings open next.",
    "mixer": "Puts the chosen mixer action on the pad. Its settings open next.",
    "trigger": "Puts a MIDI trigger on the pad, sending a control change on the pad's own number. "
               "Its settings open next.",
}


def add_sound(grid, path):
    """Put the file at `path` on the pad."""
    name = os.path.basename(path)
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as exc:
        return {"ok": False, "message": f"Could not read {name}: {exc.strerror}."}
    return pad_actions.action_add_sound(grid, name, raw)


class ChangeDialog(SpeakingDialog):
    """What the pad does: its type, then the sound, effect or mixer action. Nothing is sent from
    here: Apply asks before replacing what is there, then hands `plan` back to the page, which runs
    it."""

    def __init__(self, parent, pad, model):
        super().__init__(parent, f"Change what {where(pad)} does")
        self.pad = pad
        self.model = model
        self.plan = None
        self.path = None
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.kind = labelled(self, sizer, "What this pad does", lambda p: wx.Choice(
            p, choices=[label for _key, label in TYPES]))
        a11y.describe(self.kind, f"Now {content(pad)}. Nothing changes until you press Apply.")
        keys = [key for key, _label in TYPES]
        now = TYPE_KEY.get(pad["type"]) if pad["present"] else "empty"
        if now:
            self.kind.SetSelection(keys.index(now))
        self.kind.Bind(wx.EVT_CHOICE, lambda _e: self._show_kind())

        self.sound_line = wx.BoxSizer(wx.HORIZONTAL)
        self.sound_line.Add(wx.StaticText(self, label="Sound file"), 0,
                            wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.file = reading_box(self, width=260, height=None, value="None chosen")
        a11y.describe(self.file, "WAV or MP3, written to the pad exactly as it is.")
        self.sound_line.Add(self.file, 0, wx.RIGHT, 8)
        self.choose = wx.Button(self, label="Choose a file")
        self.choose.Bind(wx.EVT_BUTTON, self._on_choose)
        self.sound_line.Add(self.choose, 0)
        sizer.Add(self.sound_line, 0, wx.ALL, 6)

        self.recipes = list(model["recipes"])
        self.fx = labelled(self, sizer, "Voice effect", lambda p: wx.Choice(p, choices=self.recipes))
        a11y.describe(self.fx, "A factory voice effect, or one of your own from Voice effects.")
        name = (pad["name"] or "").replace("_", " ")
        if pad["type"] == soundpad.PAD_TYPE_VOICE_FX and name in self.recipes:
            self.fx.SetSelection(self.recipes.index(name))
        elif self.recipes:
            self.fx.SetSelection(0)

        self.mixer_modes = [(int(t["value"].split(":")[1]), t["label"])
                            for t in model["special_types"] if t["value"].startswith("mixer:")]
        self.mixer = labelled(self, sizer, "Mixer action", lambda p: wx.Choice(
            p, choices=[label for _mode, label in self.mixer_modes]))
        a11y.describe(self.mixer, "Censor lays a beep over everything, Trash Talk cuts your audio "
                                  "to callers, Fade fades the mix in or out, Back Channel is "
                                  "off-air talkback, and Ducking lowers everyone but the host.")
        modes = [mode for mode, _label in self.mixer_modes]
        self.mixer.SetSelection(modes.index(pad["mixer_mode"]) if pad["mixer_mode"] in modes
                                else 0)

        buttons = wx.StdDialogButtonSizer()
        self.apply = wx.Button(self, wx.ID_OK, "Apply")
        self.apply_help = a11y.describe(self.apply)
        buttons.AddButton(self.apply)
        buttons.AddButton(wx.Button(self, wx.ID_CANCEL, "Cancel"))
        buttons.Realize()
        sizer.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 10)
        sizer.Add(self.sink, 0)
        self.SetSizer(sizer)
        self.Bind(wx.EVT_BUTTON, self._on_apply, id=wx.ID_OK)
        self._show_kind()
        self.CentreOnParent()

    def _key(self):
        index = self.kind.GetSelection()
        return TYPES[index][0] if index != wx.NOT_FOUND else None

    def _show_kind(self):
        """Only the choice the type needs is shown."""
        key = self._key()
        self.sound_line.ShowItems(key == "sound")
        self.fx.GetContainingSizer().ShowItems(key == "fx")
        self.mixer.GetContainingSizer().ShowItems(key == "mixer")
        self.apply_help.description = APPLY_HELP.get(key, "")
        self.Layout()
        self.Fit()

    def _on_choose(self, _event):
        with wx.FileDialog(self, "Choose a sound for the pad",
                           wildcard="WAV or MP3 sound (*.wav;*.mp3)|*.wav;*.mp3",
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            self.path = dialog.GetPath()
        self.file.SetValue(os.path.basename(self.path))
        # The box, not the button, so the screen reader reads the file just chosen.
        self.file.SetFocus()

    def _on_apply(self, _event):
        pad, key = self.pad, self._key()
        place = where(pad)
        if self.model["read_only"]:
            self.say(READ_ONLY)
            return
        if key is None:
            self.say("Choose what the pad does.")
            return
        if key == "empty":
            if not pad["present"]:
                self.say("This pad is already empty.")
                return
            if not confirm(self, "Clear this pad?",
                           f"This empties {place}, removing {content(pad)}.", "Clear"):
                return
            self.plan = Plan(f"Clearing {place}.", pad_actions.action_clear_pad, (pad["grid"],), True)
            self.EndModal(wx.ID_OK)
            return
        if key == "sound" and not self.path:
            self.say("Choose a WAV or MP3 file first.")
            self.choose.SetFocus()
            return
        if pad["present"] and not confirm(
                self, "Replace this pad?",
                f"This replaces {content(pad)} on {place}. You can put it back by setting the pad "
                "again.", "Replace"):
            return
        if key == "sound":
            plan = Plan(f"Writing {os.path.basename(self.path)} to {place}. This takes several "
                        "seconds.", add_sound, (pad["grid"], self.path), False)
        elif key == "fx":
            effect = self.recipes[self.fx.GetSelection()]
            plan = Plan(f"Putting {effect} on {place}.", pad_actions.action_assign_fx,
                        (pad["grid"], effect), False)
        elif key == "mixer":
            mode, label = self.mixer_modes[self.mixer.GetSelection()]
            plan = Plan(f"Putting {label} on {place}.", pad_actions.action_create_special_pad,
                        (pad["grid"], f"mixer:{mode}"), False)
        else:
            plan = Plan(f"Putting a MIDI trigger on {place}.", pad_actions.action_create_special_pad,
                        (pad["grid"], "trigger"), False)
        self.plan = plan
        self.EndModal(wx.ID_OK)
