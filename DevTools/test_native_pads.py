"""The native window's SMART pads (Project/native/pads_section.py,
pad_dialogs.py, pad_fx.py, pad_maint.py),
offline: the pad list names each pad and keeps the chosen one across a read, the Settings dialog
offers only what a pad's type has and shows the read's values on it, its writes reach the server in
the desk's units, the Change dialog hands back the right plan, Voice effects builds the effect the
switches and sliders show and refuses a preview with no pad to borrow, and Pad maintenance offers
only sound pads for export and refuses its jobs on a read-only desk. Nothing is shown and nothing touches a
desk.

Run: python DevTools/test_native_pads.py
"""
from __future__ import annotations

import datetime
import sys

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
import devices
import server
import soundpad
import wx
from _testkit import as_device, check, check_in, check_true, patched
from native import dialogs, pad_dialogs, pad_fx, pad_maint, pads_section

app = wx.App()


def pad(grid, **values):
    record = {"grid": grid, "bank": (grid - 1) // 8 + 1, "pad": (grid - 1) % 8 + 1,
              "padIdx": grid - 1, "present": False, "name": None, "type": None,
              "type_label": "Empty", "replay": None, "loop": None, "play_mode": None,
              "colour": None, "gain": None, "file": None, "mixer_mode": None,
              "mixer_trigger": None, "back_channel": None, "trigger": None, "envelope": None,
              "fade": None, "censor": None}
    record.update(values)
    return record


def model_with(pads, **values):
    model = {"pads": pads, "read_only": False, "health_message": "",
             "colours": soundpad.pad_colours(),
             "play_modes": [{"value": v, "label": label}
                            for v, label in sorted(soundpad.PLAY_MODE_LABEL.items())],
             "play_mode_one_shot": soundpad.PLAY_MODE_ONE_SHOT,
             "mixer_triggers": [{"value": v, "label": label}
                                for v, label in sorted(soundpad.MIXER_TRIGGER_LABEL.items())],
             "back_channel_sources": soundpad.back_channel_sources(),
             "trigger_edit": soundpad.trigger_choices(),
             "extra_edit": soundpad.pad_extra_choices(),
             "recipes": ["Robot", "Deep"],
             "special_types": [{"value": f"mixer:{m}", "label": label}
                               for m, label in sorted(soundpad.MIXER_MODE_LABEL.items())]
             + [{"value": "trigger", "label": "MIDI Trigger"}]}
    model.update(values)
    return model


class FakeDesk:
    channels = {"channels": [{"name": "Mic1", "label": "Mic 1", "kind": "mic"},
                             {"name": "Mic2", "label": "Mic 2", "kind": "mic"},
                             {"name": "USB1", "label": "USB 1", "kind": "line"}]}

    def subscribe(self, fn):
        self.listener = fn

    def watch(self, names, fn):
        self.watched = getattr(self, "watched", []) + [(set(names), fn)]


class FakeFrame:
    def __init__(self):
        self.window = wx.Frame(None)
        self.host = wx.Panel(self.window)
        self.host.SetSizer(wx.BoxSizer(wx.VERTICAL))
        self.tree = wx.TreeCtrl(self.window)
        self.desk = FakeDesk()
        self.said = []
        self.changed = 0

    def say(self, text, recent=False):
        self.said.append(text)

    def pages_changed(self, _section):
        self.changed += 1

    def open_web_page(self, _route):
        pass


def first_focusable(window):
    for child in window.GetChildren():
        if isinstance(child, (wx.Panel, wx.StaticBox)):
            found = first_focusable(child)
            if found is not None:
                return found
        elif child.AcceptsFocus() and child.IsShown() and child.IsEnabled():
            return child
    return None


def controls_named(dialog, label):
    """The control a static text labelled `label` names: the next sibling of that text."""
    for window in dialog.GetChildren():
        for child in [window, *window.GetChildren()]:
            if isinstance(child, wx.StaticText) and child.GetLabel() == label:
                siblings = list(child.GetParent().GetChildren())
                return siblings[siblings.index(child) + 1]
    return None


with as_device(devices.PRO2):
    sources = soundpad.back_channel_sources()
    sound = pad(1, present=True, type=1, type_label="Sound", name="Big_Applause", gain=-10.0,
                play_mode=soundpad.PLAY_MODE_ONE_SHOT, replay=True, loop=False, colour=3,
                envelope={"start": 0.0, "fade_in": 0.1, "fade_out": 0.9, "stop": 1.0})
    fade = pad(2, present=True, type=3, type_label="Mixer", name="Fade", colour=1, mixer_mode=2,
               mixer_trigger=1, fade={"in_seconds": 2.0, "out_seconds": 3.5, "exclude_host": True})
    room = pad(3, present=True, type=3, type_label="Mixer", name="Back Channel", colour=1,
               mixer_mode=3, mixer_trigger=0,
               back_channel={s["prop"]: False for s in sources})
    trigger = pad(4, present=True, type=4, type_label="MIDI", name="MIDI", colour=5,
                  trigger={"custom": False, "type": 1, "control": 60, "channel": 2, "on": 100,
                           "off": 0, "mode": 1, "send": 2})
    empty = pad(5)
    model = model_with([sound, fade, room, trigger, empty])

    # ---- the list ------------------------------------------------------------------------------
    frame = FakeFrame()
    section = pads_section.PadsSection(frame)
    check("three pages under SMART pads", [p.title for p in section.pages],
          ["Pads", "Voice effects", "Pad maintenance"])
    check("the pads follow every pad property the grid is built from",
          [(names, fn.__self__._read.__self__) for names, fn in frame.desk.watched],
          [(set(soundpad.PAD_GRID_PROPS), section)])
    section.show({"pads": [], "health_message": "Could not read the pad layout."})
    check("a failed first read says why on every status page",
          [status.text.GetValue() for status in section.statuses],
          ["Could not read the pad layout."] * 3)
    section.show(model)
    page = section.page
    check_true("a good read puts all three pages in the tree", frame.changed == 1
               and [p.window for p in section.pages] == [page, section.fx, section.maint])
    check("the list names bank, pad and what is on it", page.list.GetString(0),
          "Pad 1. Bank 1. Sound: Big Applause")
    check("an empty pad reads as empty", page.list.GetString(4), "Pad 5. Bank 1. Empty")
    check_true("no note while the pads can be changed", not page.note.IsShown())
    page.list.SetSelection(3)
    renamed = model_with([dict(p) for p in model["pads"]], read_only=True)
    renamed["pads"][3]["name"] = "Clap"
    section.show(renamed)
    check("a read keeps the chosen pad", page.list.GetSelection(), 3)
    check("a read relabels a renamed pad", page.list.GetString(3), "Pad 4. Bank 1. MIDI: Clap")
    check_true("a read-only desk says so", page.note.IsShown()
               and "not enabled" in page.note.GetValue())
    section.show(model)

    page.list.SetSelection(4)
    page._edit()
    check_in("Edit on an empty pad points at Change", "Change what this pad does", frame.said[-1])

    # ---- Settings: a sound pad -----------------------------------------------------------------
    dialog = pad_dialogs.SettingsDialog(frame.window, sound, model)
    check("the title names the pad", dialog.GetTitle(),
          "Settings for pad 1, bank 1, Sound: Big Applause")
    check_true("the focus lands on the name", first_focusable(dialog) is dialog.name)
    check("the volume is in half decibels", controls_named(dialog, "Volume").GetValue(), -20)
    check("the colour is the pad's", controls_named(dialog, "Colour").GetSelection(), 3)
    check_true("Loop is hidden in One Shot", not dialog.loop.box.IsShown())
    starts = [controls_named(dialog, f["label"]).GetValue()
              for f in model["extra_edit"]["envelope"]]
    check("the trim and fade points are percent", starts, [0, 10, 90, 100])
    check_true("a sound pad has no trigger mode",
               controls_named(dialog, "How the pad triggers") is None)

    sent = []
    with patched((server, {"action_set_pad_gain": lambda *a: sent.append(a) or {"ok": True}})):
        dialog._write(server.action_set_pad_gain, scale=2)(-15)
    check("a volume write goes in decibels", sent, [(1, -7.5)])
    check_true("a write is remembered for the re-read", dialog.wrote)
    current, remember = dialog.field("envelope", "fade_in", scale=100)
    remember(25)
    check("a remembered value lands on the pad's record", sound["envelope"]["fade_in"], 0.25)
    check("and reads back in the control's units", current(), 25)
    dialog._play_mode_set({"ok": True})
    sound["play_mode"] = 0
    dialog._play_mode_set({"ok": True})
    check_true("Loop comes back out of One Shot", dialog.loop.box.IsShown())
    dialog.Destroy()

    # ---- Settings: mixer pads ------------------------------------------------------------------
    dialog = pad_dialogs.SettingsDialog(frame.window, fade, model)
    check("the fade times are in half seconds",
          [controls_named(dialog, f["label"]).GetValue() for f in model["extra_edit"]["fade"]],
          [4, 7])
    check_true("a mixer pad has no volume", controls_named(dialog, "Volume") is None)
    host = [c for c in dialog._controls if isinstance(c, pad_dialogs.Switch)]
    check_true("leaving the host out shows ticked", host and host[0].box.GetValue())
    dialog.Destroy()

    dialog = pad_dialogs.SettingsDialog(frame.window, room, model)
    members = [c.box.GetLabel() for c in dialog._controls if isinstance(c, pad_dialogs.Switch)]
    check("a Back Channel pad lists this desk's members", members,
          [s["label"] for s in sources])
    dialog.Destroy()

    # ---- Settings: a MIDI trigger pad ----------------------------------------------------------
    dialog = pad_dialogs.SettingsDialog(frame.window, trigger, model)
    check_in("the default mapping is the pad's position, not the record",
             "control change 3 on channel 1", controls_named(dialog, "Now sending").GetValue())
    check("a field refuses on the default mapping", dialog._trigger_guard(),
          pad_dialogs.DEFAULT_MAPPING)
    trigger["trigger"]["custom"] = True
    check("and takes a value on a custom one", dialog._trigger_guard(), None)
    dialog._trigger_set({"ok": True})
    check_in("the sentence follows the record", "Note 60 on channel 2",
             controls_named(dialog, "Now sending").GetValue())
    trigger["trigger"]["custom"] = False
    dialog._custom_set({"ok": True})
    check("turning custom off puts the desk's defaults on the record",
          [trigger["trigger"][k] for k in ("control", "channel", "on", "off")], [3, 1, 127, 0])
    check("and on the number boxes",
          controls_named(dialog, "Control or note number").GetValue(), 3)
    dialog.Destroy()

    # ---- Change --------------------------------------------------------------------------------
    dialog = pad_dialogs.ChangeDialog(frame.window, sound, model)
    check("the title names the pad", dialog.GetTitle(), "Change what pad 1, bank 1 does")
    check("the pad's type is chosen", dialog._key(), "sound")
    check_true("the focus lands on the type", first_focusable(dialog) is dialog.kind)
    check_true("only the file chooser is shown for a sound",
               dialog.file.IsShown() and not dialog.fx.IsShown() and not dialog.mixer.IsShown())
    said = []
    with patched((dialog, {"say": lambda text, recent=False: said.append(text)})):
        dialog._on_apply(None)
    check("Apply with no file refuses", (dialog.plan, said), (None, ["Choose a WAV or MP3 file first."]))
    dialog.Destroy()

    dialog = pad_dialogs.ChangeDialog(frame.window, empty, model)
    check("an empty pad starts on Empty", dialog._key(), "empty")
    dialog.kind.SetSelection(4)
    dialog._show_kind()
    with patched((dialog, {"EndModal": lambda _code: None})):
        dialog._on_apply(None)
    check("a MIDI trigger on an empty pad asks nothing and plans the create",
          (dialog.plan.action, dialog.plan.args, dialog.plan.clears),
          (server.action_create_special_pad, (5, "trigger"), False))
    dialog.kind.SetSelection(3)
    dialog._show_kind()
    dialog.mixer.SetSelection(4)
    with patched((dialog, {"EndModal": lambda _code: None})):
        dialog._on_apply(None)
    check("a mixer action plans its mode", dialog.plan.args, (5, "mixer:4"))
    dialog.Destroy()

    # ---- Voice effects -------------------------------------------------------------------------
    fx = section.fx
    check("nothing is included to start with", fx.stack(), {})
    check("the preview mics are the desk's", fx.mic.GetItems(), ["Mic 1", "Mic 2"])
    check("a new effect starts on yellow", fx.colour.GetStringSelection(), "Yellow")
    reverb, pitch = fx.effects[0], fx.effects[4]
    check_true("an effect's sliders are hidden until it is included",
               not reverb.sliders["reverbMix"][1].slider.IsShown())
    streamed = []
    with patched((server, {"action_preview_update": lambda *a: streamed.append(a) or {"ok": True}})):
        reverb.include.SetValue(True)
        fx.included(reverb)
        pitch.include.SetValue(True)
        fx.included(pitch)
        sent_alone = fx.streamer(reverb.sliders["reverbMix"][0])(80)
    check_true("with no preview nothing is streamed", not streamed and sent_alone["ok"])
    check_true("including shows the simple sliders and keeps the advanced ones hidden",
               reverb.sliders["reverbMix"][1].slider.IsShown()
               and not reverb.sliders["reverbLowCut"][1].slider.IsShown())
    reverb.advanced.SetValue(True)
    fx.relayout(reverb.show)
    check_true("the advanced switch shows the rest", reverb.sliders["reverbLowCut"][1].slider.IsShown())
    check("the stack is in the desk's units, pitch in semitones", fx.stack(),
          {"reverb": {"reverbMix": 0.5, "reverbModel": 0.6, "reverbLowCut": 0.67,
                      "reverbHighCut": 0.33}, "pitchShift": {"pitchShiftSemitones": 7}})
    check("pitch reads in semitones", pad_fx.semitone_words(-1), "1 semitone down")
    fx._on_save(None)
    check("Save with no name refuses", frame.said[-1], "Give the effect a name first.")
    full = model_with([dict(sound, grid=g) for g in (1, 2)], custom_fx=["Mine"])
    section.show(full)
    fx.preview.SetValue(True)
    fx._on_preview(None)
    check_true("a preview with every pad in use is refused and switched back",
               frame.said[-1] == pad_fx.NO_FREE_PAD and not fx.preview.GetValue())
    check("the saved effects are listed", fx.saved.GetStrings(), ["Mine"])
    started, jobs, shown = [], [], []

    class Job:
        """The wait dialog: runs its begin, which the patched call answers at once."""
        def __init__(self, _parent, title, _text, begin, **kwargs):
            jobs.append((title, kwargs.get("closable")))
            self.begin, self.result = begin, None

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            pass

        def finish(self, result):
            self.result = result

        def run(self):
            self.begin(self)
            return self.result

    class Result:
        def __init__(self, _parent, title, text, *_a, **_k):
            shown.append((title, text))

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            pass

        def ShowModal(self):
            return wx.ID_OK

    section.show(model)
    with patched((pad_fx, {"JobDialog": Job, "TextDialog": Result,
                           "call": lambda _o, fn, *a, done=None: started.append((fn, a))
                           or done({"ok": True, "grid": 5})})):
        fx.preview.SetValue(True)
        fx._on_preview(None)
    check("a preview starts with the effect as built, on the chosen mic",
          started, [(server.action_preview_start, (fx.stack(), 3, 1))])
    check("in a wait dialog that stays until it is live", jobs,
          [("Starting the live preview", False)])
    check_true("and once live it says nothing over the switch",
               fx.preview_grid == 5 and fx.preview.GetValue() and not shown)
    fx.preview_grid = None
    with patched((pad_fx, {"JobDialog": Job, "TextDialog": Result,
                           "call": lambda _o, fn, *a, done=None: done(
                               {"ok": False, "message": "No room."})})):
        fx.preview.SetValue(True)
        fx._on_preview(None)
    check_true("a start that fails says why in a dialog and switches back off",
               shown == [("Live preview not started", "No room.")] and not fx.preview.GetValue())
    fx.preview_grid = 5
    with patched((server, {"action_preview_update": lambda *a: streamed.append(a) or {"ok": True}})):
        fx.streamer(reverb.sliders["reverbMix"][0])(80)
    check("while it runs a slider streams to the preview pad", streamed[-1], (5, "reverbMix", 0.8))
    fx.preview_grid, fx._preview_busy = None, False
    fx.preview.SetValue(False)
    check("with no saved effects the list says so", fx.saved.GetStrings(), [pad_fx.NONE_SAVED])

    # ---- Pad maintenance -----------------------------------------------------------------------
    maint = section.maint
    check("only sound pads are offered for export", maint.export_choice.GetItems(),
          ["Pad 1, bank 1. Sound: Big Applause"])
    exported = []
    with patched((pad_maint, {"call": lambda _o, fn, *a, done=None: exported.append((fn, a))})):
        maint._on_export(None)
    check("Export copies the chosen pad", exported, [(server.action_export_pad, (1,))])
    maint._exporting = False
    section.show(model_with([empty]))
    check("with no sound pad the list says so", maint.export_choice.GetItems(),
          ["No pad has a sound on it"])
    maint._on_export(None)
    check("and Export says there is nothing to copy", frame.said[-1],
          "No pad has a sound on it to export.")
    section.show(model_with([dict(p) for p in model["pads"]], read_only=True))
    asked = []
    with patched((pad_maint, {"confirm": lambda *a: asked.append(a) or False})):
        maint._on_clear_all(None)
        maint._on_tidy(None)
        maint._on_restore_fx(None)
        maint._on_restore_backup(None)
    check_true("a read-only desk refuses every job before asking",
               not asked and frame.said[-4:] == [pad_dialogs.READ_ONLY] * 4)
    section.show(model)
    ran = []
    with patched((pad_maint, {"confirm": lambda *a: False,
                              "run_job": lambda *a, **k: ran.append(a)})):
        maint._on_clear_all(None)
    check("a job not confirmed is not run", ran, [])

    maint._choose_backup({"ok": True, "backups": [], "seconds_per_pad": 15})
    check("with no backup holding pads it says how to make one", frame.said[-1],
          pad_maint.NO_BACKUPS)
    saved = datetime.datetime(2026, 11, 2, 19, 40)
    check("a backup is listed with its pads and when it was saved",
          pad_maint.backup_label({"name": "Show night", "pads": 16, "saved_at": saved.timestamp()}),
          "Show night. 16 pads, saved " + pad_maint.wording.spoken_datetime(saved))

    class ChosenList:
        def __init__(self, *_a):
            self.list = wx.ListBox(frame.window, choices=["a", "b"])
            self.list.SetSelection(1)

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            self.list.Destroy()

        def ShowModal(self):
            return wx.ID_OK

    jobs, questions = [], []
    with patched((pad_maint, {"ListDialog": ChosenList,
                              "confirm": lambda _p, _t, text, _a: questions.append(text) or True}),
                 (maint, {"_pad_job": lambda *a: jobs.append(a[4:])})):
        maint._choose_backup({"ok": True, "seconds_per_pad": 15, "backups": [
            {"name": "Newest", "pads": 4, "saved_at": None},
            {"name": "Show night", "pads": 16, "saved_at": None}]})
    check("the chosen backup's pads are restored", jobs,
          [(server.action_restore_backup_pads, "Show night")])
    check_in("the question says how long it can take", "up to 4 minutes", questions[0])

    shown = []

    class Result:
        def __init__(self, _parent, title, text, *_a, **_k):
            shown.append((title, text))

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            pass

        def ShowModal(self):
            return wx.ID_OK

    with patched((dialogs, {"TextDialog": Result})):
        maint._finished(maint.restore_backup, {"reply": {"ok": True, "message": "Back."}},
                        ("Pads restored", "Pads not restored"))
        maint._finished(maint.restore_backup, {"reply": {"ok": False, "message": "No."}},
                        ("Pads restored", "Pads not restored"))
    check("a job's result opens in a dialog titled by how it went", shown,
          [("Pads restored", "Back."), ("Pads not restored", "No.")])

    # ---- the change's job dialog stays until the change is done --------------------------------
    job = pad_maint.JobDialog(frame.window, "Changing pad 1, bank 1", "Clearing.", lambda _j: None,
                                 stop=None, closable=False)
    check("its one button only says to wait",
          [c.GetLabel() for c in job.GetChildren() if isinstance(c, wx.Button)], ["Please wait"])
    waits = []
    close = wx.CloseEvent(wx.wxEVT_CLOSE_WINDOW)
    close.SetCanVeto(True)
    with patched((pad_maint.JobDialog.__mro__[1], {"say": lambda _self, text, recent=False:
                                                      waits.append(text)})):
        job._on_close(close)
    check_true("closing it is refused, and says to wait",
               close.GetVeto() and waits == [job.WAIT])
    job.Destroy()

    frame.window.Destroy()

sys.exit(_testkit.finish())
