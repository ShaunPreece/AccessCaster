"""Voice effects: build a custom voice effect from the desk's six effects, hear it live on a mic
while building it, and save it to the list a pad's voice effect is chosen from; and the saved
effects, to delete.

The live preview borrows one empty pad, puts the effect being built on it and engages it, and then
streams each change to it (`server.action_preview_*`). Nothing else touches the desk: a saved
effect lives in this app until it is put on a pad.
"""
from collections import namedtuple

import wx
import wx.lib.scrolledpanel as scrolled

import server
from native import a11y
from pad_records import EFFECT_FLAG
from native.calls import call
from native.controls import Slider, set_choices
from native.dialogs import JobDialog, TextDialog, confirm
from native.pad_dialogs import READ_ONLY
from native.pages import group, labelled, page_body

TITLE = "Voice effects"
NONE_SAVED = "You have not created any effects yet."
NO_FREE_PAD = ("Live preview needs one empty pad to borrow, and every pad is in use. You can still "
               "build and save this effect.")
DEFAULT_COLOUR = 3       # Yellow

Control = namedtuple("Control", "param label default advanced semitones")
Effect = namedtuple("Effect", "engine label controls")


def _c(param, label, default, advanced=False, semitones=False):
    return Control(param, label, default, advanced, semitones)


# Each slider is 0 to 100 percent of the desk's 0 to 1, except pitch, which is in semitones. Robot
# amount is `robotMix`, the one that is heard: the factory Large and Small Robot differ on it, and
# `robotLevel` is 0 in both and makes no audible difference.
EFFECTS = [
    Effect("reverb", "Reverb", [_c("reverbMix", "Amount", 50),
                                _c("reverbModel", "Type or size", 60, True),
                                _c("reverbLowCut", "Low cut", 67, True),
                                _c("reverbHighCut", "High cut", 33, True)]),
    Effect("echo", "Echo", [_c("echoMix", "Level", 50),
                            _c("echoDelay", "Delay, 0 to 1 second", 17, True),
                            _c("echoDecay", "Decay", 50, True),
                            _c("echoLowCut", "Low cut", 50, True),
                            _c("echoHighCut", "High cut", 50, True)]),
    Effect("distortion", "Megaphone", [_c("distortionIntensity", "Intensity", 70)]),
    Effect("robot", "Robot", [_c("robotMix", "Amount", 67),
                              _c("robotLevel", "Level", 0, True)]),
    Effect("pitchShift", "Pitch shift", [_c("pitchShiftSemitones", "Semitones", 7,
                                            semitones=True)]),
    Effect("voiceDisguise", "Voice disguise", []),
]


def semitone_words(value):
    if value == 0:
        return "no change"
    return f"{abs(value)} semitone{'s' if abs(value) != 1 else ''} {'up' if value > 0 else 'down'}"


def percent_words(value):
    return f"{value} percent"


def device_value(control, value):
    """A slider's value in the desk's units."""
    return value if control.semitones else value / 100


class _EffectBox:
    """One effect's group: its Include switch, its sliders shown while it is included, and a switch
    for the advanced ones."""

    def __init__(self, page, parent, sizer, effect):
        self.effect = effect
        box_sizer, box = group(parent, sizer, effect.label)
        self.include = wx.CheckBox(box, label=f"Include {effect.label.lower()}")
        if not effect.controls:
            a11y.describe(self.include, "No settings: it is simply on or off.")
        self.include.Bind(wx.EVT_CHECKBOX, lambda _e: page.included(self))
        box_sizer.Add(self.include, 0, wx.ALL, 6)
        self.values = {c.param: c.default for c in effect.controls}
        self.sliders = {}
        self.advanced = None
        for control in effect.controls:
            if control.advanced and self.advanced is None:
                self.advanced = wx.CheckBox(box, label=f"Advanced {effect.label.lower()} controls")
                self.advanced.Bind(wx.EVT_CHECKBOX, lambda _e: page.relayout(self.show))
                box_sizer.Add(self.advanced, 0, wx.ALL, 6)
            self.sliders[control.param] = (control, Slider(
                page, box, box_sizer, f"{effect.label} {control.label.lower()}",
                -12 if control.semitones else 0, 12 if control.semitones else 100,
                page.streamer(control), *self._field(control.param),
                words=semitone_words if control.semitones else percent_words, quiet=True))
        for _control, slider in self.sliders.values():
            slider.show()
        self.show()

    def _field(self, param):
        def remember(value):
            self.values[param] = value
        return lambda: self.values[param], remember

    def show(self):
        on = self.include.GetValue()
        if self.advanced is not None:
            self.advanced.Show(on)
        more = on and self.advanced is not None and self.advanced.GetValue()
        for control, slider in self.sliders.values():
            slider.set_shown(on and (more or not control.advanced))

    def params(self):
        """The effect's settings in the desk's units, as the sliders show them now."""
        return {param: device_value(control, slider.slider.GetValue())
                for param, (control, slider) in self.sliders.items()}


class FxPage(scrolled.ScrolledPanel):
    def __init__(self, section):
        super().__init__(section.frame.host)
        self.Hide()
        self.section = section
        self.frame = section.frame
        self.preview_grid = None      # the pad the live preview is on, while it is on
        self._preview_busy = False    # a preview stop has not returned
        sizer, parent = page_body(self)
        box_sizer, box = group(parent, sizer, "Create a custom voice effect")
        self._build_preview(box, box_sizer)
        self.effects = [_EffectBox(self, box, box_sizer, effect) for effect in EFFECTS]
        self.name = labelled(box, box_sizer, "Effect name", lambda p: wx.TextCtrl(
            p, size=(240, -1), style=wx.TE_PROCESS_ENTER))
        self.name.SetMaxLength(40)
        self.name.Bind(wx.EVT_TEXT_ENTER, self._on_save)
        self.colour = labelled(box, box_sizer, "Pad colour", lambda p: wx.Choice(p))
        save = wx.Button(box, label="Save effect")
        a11y.describe(save, "Adds it to the voice effects a pad can be given. Saving changes no "
                            "pad.")
        save.Bind(wx.EVT_BUTTON, self._on_save)
        box_sizer.Add(save, 0, wx.ALL, 6)

        saved_sizer, saved_box = group(parent, sizer, "Your saved effects")
        self.saved = labelled(saved_box, saved_sizer, "Saved effect", lambda p: wx.ListBox(
            p, size=(300, 120)))
        delete = wx.Button(saved_box, label="Delete this effect")
        a11y.describe(delete, "Removes it from the voice effects a pad can be given. A pad already "
                              "using it keeps it.")
        delete.Bind(wx.EVT_BUTTON, self._on_delete)
        saved_sizer.Add(delete, 0, wx.ALL, 6)
        self.show()
        self.SetupScrolling(scroll_x=False)

    @property
    def model(self):
        return self.section.model

    def _build_preview(self, parent, sizer):
        self.preview = wx.CheckBox(parent, label="Live preview: hear changes on your mic as you "
                                                 "build")
        a11y.describe(self.preview, "Puts the effect on one empty pad and switches it on, so you "
                                    "hear each change as you make it. The pad is emptied again "
                                    "when you turn this off.")
        self.preview.Bind(wx.EVT_CHECKBOX, self._on_preview)
        sizer.Add(self.preview, 0, wx.ALL, 6)
        self.mic = labelled(parent, sizer, "Preview through", lambda p: wx.Choice(p))
        a11y.describe(self.mic, "Talk into this mic to hear the effect.")
        self.mic.Bind(wx.EVT_CHOICE, self._on_mic)
        self.mics = []

    def show(self):
        """The colours, mics and saved effects from the latest reads, keeping each choice."""
        colours = self.model["colours"]
        chosen = self.colour.GetSelection()
        set_choices(self.colour, [c["label"] for c in colours])
        self.colour.SetSelection(chosen if chosen != wx.NOT_FOUND else
                                 next((i for i, c in enumerate(colours)
                                       if c["value"] == DEFAULT_COLOUR), 0))
        channels = (self.frame.desk.channels or {}).get("channels") or []
        mics = [(int(c["name"][3:]), c["label"]) for c in channels
                if c.get("kind") == "mic" and c["name"][3:].isdigit()] or [(1, "Mic 1")]
        chosen = self.mics[self.mic.GetSelection()][0] if self.mic.GetSelection() >= 0 else 1
        self.mics = mics
        set_choices(self.mic, [label for _n, label in mics])
        numbers = [n for n, _label in mics]
        self.mic.SetSelection(numbers.index(chosen) if chosen in numbers else 0)
        self._show_saved()

    def _show_saved(self, keep=None):
        names = list(self.model.get("custom_fx") or [])
        index = self.saved.GetSelection()
        keep = keep if keep is not None else max(index, 0)
        labels = names or [NONE_SAVED]
        if [self.saved.GetString(i) for i in range(self.saved.GetCount())] != labels:
            self.saved.Set(labels)
        self.saved.SetSelection(min(keep, len(labels) - 1))

    def relayout(self, change=None):
        if change:
            change()
        self.Layout()
        self.SetupScrolling(scroll_x=False, scrollToTop=False)

    def stack(self):
        """The effect as built: {engine: {param: value}} for each one included."""
        return {box.effect.engine: box.params() for box in self.effects if box.include.GetValue()}

    def _colour(self):
        index = self.colour.GetSelection()
        return self.model["colours"][index]["value"] if index != wx.NOT_FOUND else DEFAULT_COLOUR

    def _mic(self):
        return self.mics[self.mic.GetSelection()][0]

    # ---- live preview ---------------------------------------------------------------------------

    def streamer(self, control):
        """A slider's `send`: the change goes to the preview pad while there is one, and nowhere
        otherwise."""
        def send(value):
            grid = self.preview_grid
            if grid is None:
                return {"ok": True}
            return server.action_preview_update(grid, control.param, device_value(control, value))
        return send

    def included(self, box):
        self.relayout(box.show)
        grid = self.preview_grid
        if grid is None:
            return
        on = box.include.GetValue()
        # The effect's settings follow its switch, so it is heard at the sliders' values at once.
        self._stream(grid, [(EFFECT_FLAG[box.effect.engine], on)]
                     + (list(box.params().items()) if on else []))

    def _stream(self, grid, edits):
        """Send (param, value) edits to the preview pad in order, stopping at the first refused."""
        def stream():
            for param, value in edits:
                reply = server.action_preview_update(grid, param, value)
                if not reply.get("ok"):
                    return reply
            return {"ok": True}
        call(self, stream, done=self._streamed)

    def _streamed(self, reply):
        if not reply.get("ok"):
            self.frame.say(reply.get("message") or "The preview did not take that change.")

    def _on_mic(self, _event):
        if self.preview_grid is not None:
            call(self, server.action_preview_update, self.preview_grid, "padEffectInput",
                 self._mic() - 1, done=self._streamed)

    def _on_preview(self, _event):
        on = self.preview.GetValue()
        refusal = None
        if self._preview_busy:
            refusal = "Still stopping the last preview."
        elif on and self.model["read_only"]:
            refusal = READ_ONLY
        elif on and not any(not pad["present"] for pad in self.model["pads"]):
            refusal = NO_FREE_PAD
        if refusal:
            self.preview.SetValue(not on)
            self.frame.say(refusal)
            return
        if on:
            self._start_preview()
        else:
            self._stop_preview(announce=True)

    def _start_preview(self):
        """Start the preview in a job dialog that stays until it is live: the start takes several
        seconds, and a change made meanwhile has no pad to go to."""
        stack, colour, mic = self.stack(), self._colour(), self._mic()
        job = JobDialog(self, "Starting the live preview",
                        "Finding an empty pad and putting the effect on it.",
                        lambda dialog: call(dialog, server.action_preview_start, stack, colour,
                                            mic, done=dialog.finish),
                        stop=None, closable=False)
        with job:
            reply = job.run()
        self._preview_started(reply)

    def _preview_started(self, reply):
        """The focus is back on the switch, and a switch left ticked says the preview is on. A
        failure opens a dialog of its own, which nothing on its way back can talk over."""
        if reply.get("ok"):
            self.preview_grid = reply.get("grid")
            self.section.read()
            return
        self.preview.SetValue(False)
        dialog = TextDialog(self, "Live preview not started",
                            reply.get("message") or "Could not start the live preview.",
                            [(wx.ID_OK, "OK")], default=wx.ID_OK, escape=wx.ID_OK)
        with dialog:
            dialog.ShowModal()

    def _stop_preview(self, announce):
        grid, self.preview_grid = self.preview_grid, None
        self.preview.SetValue(False)
        if grid is None:
            self._preview_busy = False
            return
        self._preview_busy = True

        def stopped(reply):
            self._preview_busy = False
            if announce or not reply.get("ok"):
                self.frame.say(reply.get("message") or "Live preview stopped.")
            self.section.read()
        call(self, server.action_preview_stop, grid, done=stopped)

    # ---- saving and deleting --------------------------------------------------------------------

    def _lists(self, reply):
        """A save or delete's reply carries the lists it changed."""
        for key in ("recipes", "custom_fx"):
            if key in reply:
                self.model[key] = reply[key]

    def _on_save(self, _event):
        name = self.name.GetValue().strip()
        if not name:
            self.frame.say("Give the effect a name first.")
            self.name.SetFocus()
            return
        stack = self.stack()
        if not stack:
            self.frame.say("Include at least one effect before saving.")
            return

        def done(reply):
            self.frame.say(reply.get("message") or ("Saved." if reply.get("ok")
                                                    else "Could not save the effect."))
            if not reply.get("ok"):
                return
            self._lists(reply)
            self._show_saved(keep=self.model["custom_fx"].index(name)
                             if name in self.model["custom_fx"] else None)
            self.name.SetValue("")
            # The borrowed pad goes back once the effect is saved.
            self._stop_preview(announce=False)
        call(self, server.action_save_fx_preset, name, stack, self._colour(), done=done)

    def _on_delete(self, _event):
        names = list(self.model.get("custom_fx") or [])
        index = self.saved.GetSelection()
        if not names or index == wx.NOT_FOUND:
            self.frame.say(NONE_SAVED)
            return
        name = names[index]
        if not confirm(self, "Delete this effect?",
                       f"The {name} effect is deleted from this app. Pads already using it keep "
                       "it. This cannot be undone.", "Delete"):
            return

        def done(reply):
            self.frame.say(reply.get("message") or "Done.")
            if reply.get("ok"):
                self._lists(reply)
                self._show_saved(keep=max(0, index - 1) if index >= len(self.model["custom_fx"])
                                 else index)
                self.saved.SetFocus()
        call(self, server.action_delete_fx_preset, name, done=done)
