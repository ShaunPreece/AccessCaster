"""The native window's App settings and Help (Project/native/app_settings_section.py,
shortcuts_page.py and help_section.py), offline: each shortcut listed with what it does and its
keys, the selection kept across a write and moved to a neighbour on a delete, the pad keys switch
partly ticked, the restore question counting what is lost, the voices and outputs offered from what
Windows reports, the Windows level shown only for a stream that has one, the after-update report
waiting for the window to come to the front, and the help split into
topics read from help.txt with the running version. Nothing is shown and nothing touches a
desk.

Run: python DevTools/test_native_app_settings.py
"""
from __future__ import annotations

import sys

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _a11y
import _testkit
import shortcuts
import wx
from _native_fakes import FakeFrame
from _testkit import check, check_in, check_true, patched
from native import app_settings_section, help_section, shortcuts_page
from version import __version__

app = wx.App()


MODIFIERS = [{"id": "ctrl", "label": "Ctrl"}, {"id": "alt", "label": "Alt"},
             {"id": "win", "label": "Windows"}, {"id": "shift", "label": "Shift"}]


def row(id_, label, key, enabled=True, problem=""):
    return {"id": id_, "command": label.lower(), "label": label, "key": key, "mods": [],
            "enabled": enabled, "speak": True, "combo": f"Ctrl+Alt+{key}", "problem": problem}


def state(rows, pad_set="none", enabled=True):
    return {"ok": True, "enabled": enabled, "shortcuts": rows, "mods": ["ctrl", "alt"],
            "default_mods": ["ctrl", "alt"], "modifiers": MODIFIERS, "keys": ["A", "B", "C"],
            "pad_set": pad_set, "default_count": 5, "default_pad_count": 12}


# ---- what each shortcut is called ------------------------------------------------------------
check("a shortcut is what it does and its keys", shortcuts_page.item_text(row("1", "Record", "R")),
      "Record, Ctrl+Alt+R")
check("one switched off says so", shortcuts_page.item_text(row("1", "Record", "R", enabled=False)),
      "Record, off")
check("one Windows refused says why",
      shortcuts_page.item_text(row("1", "Record", "R", problem="in use")),
      "Record, Ctrl+Alt+R, not working: in use")
check("the keys held are joined with and", shortcuts_page.mods_text(
    state([]), ["ctrl", "alt", "shift"]), "Ctrl, Alt and Shift")
check("the restore question counts what is lost", shortcuts_page.restore_text(
    state([row("1", "Record", "R")])),
      "This replaces your 1 shortcut with the 5 standard ones and the 12 SMART pad keys, all on "
      "Ctrl and Alt. Anything you have added or changed is lost.")
check("Add offers the first key nothing switched on uses",
      shortcuts_page.first_free_key(state([row("1", "Record", "A"),
                                           row("2", "Mute", "B", enabled=False)])), "B")

# ---- the shortcuts page ----------------------------------------------------------------------
frame = FakeFrame()
section = type("S", (), {"frame": frame})()
page = shortcuts_page.ShortcutsPage(section)
check("before a read Add refuses", (page._on_add(None), frame.said[-1])[1],
      "The shortcuts have not been read yet.")
rows = [row("1", "Record", "A"), row("2", "Mute", "B"), row("3", "Solo", "C")]
page.show(state(rows, pad_set="some"))
check("every shortcut is listed",
      [page.list.GetString(i) for i in range(page.list.GetCount())],
      ["Record, Ctrl+Alt+A", "Mute, Ctrl+Alt+B", "Solo, Ctrl+Alt+C"])
check("the pad keys switch is partly ticked when some are bound",
      page.pad_set.Get3StateValue(), wx.CHK_UNDETERMINED)
check("the keys button names the keys held", page.mods.GetLabel(),
      "Change the keys to hold, now Ctrl and Alt")
page.list.SetSelection(1)
page.show(state(rows + [row("4", "Bank up", "D")]))
check("the selection survives a write", page._chosen()["id"], "2")
page.show(state(rows, enabled=False))
check_in("shortcuts off is in the list's description", "none of these combinations is in use",
         page.list_acc.description)

deleted = []
said_before = len(frame.said)
sync = (shortcuts_page, {"call": lambda _o, fn, *a, done=None: done(fn(*a)),
                         "confirm": lambda *a: True})
with patched(sync, (shortcuts, {"action_delete": lambda id_: (
        deleted.append(id_), {"ok": True, "state": state([r for r in rows if r["id"] != id_])})[1]}),
        (wx, {"CallLater": lambda ms, fn, *a: fn(*a)})):
    page.show(state(rows))
    page.list.SetSelection(1)
    page._delete()
check("Delete deletes the shortcut chosen", deleted, ["2"])
check("and the next one is chosen", page._chosen()["id"], "3")
check("a delete that worked says nothing more", len(frame.said), said_before)
with patched(sync, (shortcuts, {"action_delete": lambda id_: {
        "ok": True, "state": state([r for r in rows if r["id"] != id_])}}),
        (wx, {"CallLater": lambda ms, fn, *a: fn(*a)})):
    page.show(state(rows))
    page.list.SetSelection(2)
    page._delete()
check("deleting the last one chooses the one before", page._chosen()["id"], "2")
page.show(state([]))
check("an empty list says how to start", page.list.GetString(0), shortcuts_page.EMPTY)
check("and Enter on it refuses", (page._edit(), frame.said[-1])[1],
      "You have no shortcuts yet. Add one first.")

# ---- announcements ---------------------------------------------------------------------------
frame = FakeFrame()
section = type("S", (), {"frame": frame})()
announcements = app_settings_section.AnnouncementsPage(section)
check("before a read a switch refuses", announcements._unread(),
      "The announcement settings have not been read yet.")
announcements.show({"on": True, "levels": False, "mute_tones": False, "clip_warning": True,
                    "record_feedback": "tone", "remember": False, "volume": 60, "voice": "Zira",
                    "output": "", "voices": ["David", "Zira"], "outputs": []})
check("each switch shows its setting", [s.box.GetValue() for s in announcements.switches],
      [True, False, False, True, False])
check("recording's sound is a choice of three, showing the setting",
      (announcements.record_feedback.control.GetCount(),
       announcements.record_feedback.control.GetStringSelection()), (3, "Play a tone"))
check("the voices are Windows' own, after the default",
      [announcements.voice.control.GetString(i)
       for i in range(announcements.voice.control.GetCount())],
      ["Windows default voice", "David", "Zira"])
check("the chosen voice is selected", announcements.voice.control.GetSelection(), 2)
check("no outputs refuses a choice and says why", announcements._no_choice("outputs"),
      "Windows reported no outputs to this computer, so there is nothing to choose.")
check("the volume is read in percent", announcements.volume._value_words(), "60 percent")

# ---- the report after an update --------------------------------------------------------------
class InactiveFrame(wx.Frame):
    """A window that has not yet come to the front."""

    def __init__(self):
        super().__init__(None)
        self.host = wx.Panel(self)
        self.host.SetSizer(wx.BoxSizer(wx.VERTICAL))
        self.active = False
        self.said = []

    def IsActive(self):
        return self.active

    def say(self, text, recent=False):
        self.said.append(text)


def run_timers(ms):
    wx.CallLater(ms, app.ExitMainLoop)
    app.MainLoop()


waiting = [{"kind": "updated", "text": "AccessCaster has updated to version 9.9.9."}]
reports = (app_settings_section.app_updates,
           {"report_waiting": lambda: bool(waiting), "take_report": lambda: waiting.pop()})
inactive = InactiveFrame()
updates = app_settings_section.UpdatesPage(type("S", (), {"frame": inactive})())
with patched(reports, (app_settings_section, {"call": lambda _o, fn, *a, done=None: done(fn(*a))})):
    updates.take_report()
    check("the report waits while the window is not in front", (len(waiting), inactive.said),
          (1, []))
    inactive.active = True
    inactive.ProcessEvent(wx.ActivateEvent(wx.wxEVT_ACTIVATE, True))
    check("coming to the front takes it", len(waiting), 0)
    check("but says it only once the screen reader has read the window", inactive.said, [])
    run_timers(app_settings_section.AFTER_FOCUS_MS + 150)
    check("then says it", inactive.said, ["AccessCaster has updated to version 9.9.9."])
    inactive.ProcessEvent(wx.ActivateEvent(wx.wxEVT_ACTIVATE, True))
    run_timers(app_settings_section.AFTER_FOCUS_MS + 150)
    check("and only once", len(inactive.said), 1)
inactive.Destroy()

# ---- help ------------------------------------------------------------------------------------
topics = help_section.topics()
keys = [key for key, _title, _text in topics]
check("About comes first", topics[0][1], "About")
check_true("with the version running", topics[0][2].startswith(f"Version {__version__}."))
check_true("each named by its heading",
           any(title.startswith("Channels and Processing") for _k, title, _t in topics))
check("a key drops its shortcut", "channels-and-processing" in keys, True)
check_in("Troubleshooting keeps its parts in its text",
         "My desk has stopped responding",
         dict((key, text) for key, _title, text in topics)["troubleshooting"])
check_true("a term reads with its description",
           any("The landmark: The touchscreen." in text for _k, _t, text in topics))
check_true("no list item keeps its dash", not any(line.startswith("- ")
                                                  for _k, _t, text in topics
                                                  for line in text.splitlines()))
check_true("no topic is empty", all(text for _k, _t, text in topics))
check_true("no heading line reaches the text", not any(line.startswith("#")
                                                       for _k, _t, text in topics
                                                       for line in text.splitlines()))

_a11y.check_windows("App settings")
sys.exit(_testkit.finish())
