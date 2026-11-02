"""The native window's SMART pads (Project/native/pads_section.py, pad_dialogs.py), offline: the pad
list names each pad and keeps the chosen one across a read, the Settings dialog offers only what a
pad's type has and shows the read's values on it, its writes reach the server in the desk's units,
and the Change dialog hands back the right plan. Nothing is shown and nothing touches a desk.

Run: python DevTools/test_native_pads.py
"""
from __future__ import annotations

import sys

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
import devices
import server
import soundpad
import wx
from _testkit import as_device, check, check_in, check_true, patched
from native import pad_dialogs, pads_section

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
    def subscribe(self, fn):
        self.listener = fn


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
    section.show({"pads": [], "health_message": "Could not read the pad layout."})
    check("a failed first read says why on the status page", section.status.text.GetValue(),
          "Could not read the pad layout.")
    section.show(model)
    page = section.page
    check_true("a good read puts the pads page in the tree", frame.changed == 1
               and section.pages[0].window is page)
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

    # ---- the change's job dialog stays until the change is done --------------------------------
    job = pads_section.JobDialog(frame.window, "Changing pad 1, bank 1", "Clearing.", lambda _j: None,
                                 stop=None, closable=False)
    check("its one button only says to wait",
          [c.GetLabel() for c in job.GetChildren() if isinstance(c, wx.Button)], ["Please wait"])
    waits = []
    close = wx.CloseEvent(wx.wxEVT_CLOSE_WINDOW)
    close.SetCanVeto(True)
    with patched((pads_section.JobDialog.__mro__[1], {"say": lambda _self, text, recent=False:
                                                      waits.append(text)})):
        job._on_close(close)
    check_true("closing it is refused, and says to wait",
               close.GetVeto() and waits == [job.WAIT])
    job.Destroy()

    frame.window.Destroy()

sys.exit(_testkit.finish())
