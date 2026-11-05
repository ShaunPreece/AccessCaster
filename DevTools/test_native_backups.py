"""The native window's Backups (Project/native/backups_section.py, backup_dialogs.py), offline:
each backup is listed and described by what it can do, the chosen one survives a read and a
`prefer` chooses a new one, the as-found line and its Add pads button follow the baseline, the
restore question carries the plan's warnings and the pad time, each job starts the right server
call and follows the right status, the as-found backup's delete takes a second yes, and the reset's
question lands on Cancel and refuses a backup it has nowhere to put. Nothing is shown and nothing
touches a desk.

Run: python DevTools/test_native_backups.py
"""
from __future__ import annotations

import sys

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _a11y
import _testkit
import backups
import profile_actions
import show_actions
import wx
from _testkit import check, check_in, check_true, patched
from native import backup_dialogs, backups_section, dialogs

app = wx.App()


class FakeDesk:
    def subscribe(self, fn):
        self.listener = fn

    def watch(self, names, fn):
        self.watched = getattr(self, "watched", []) + [(set(names), fn)]


class FakeFrame:
    def __init__(self):
        self.window = wx.Frame(None)
        self.host = wx.Panel(self.window)
        self.host.SetSizer(wx.BoxSizer(wx.VERTICAL))
        self.desk = FakeDesk()
        self.said = []
        self.rewritten = 0

    def say(self, text, recent=False):
        self.said.append(text)

    def desk_rewritten(self):
        self.rewritten += 1


def first_focusable(window):
    for child in window.GetChildren():
        if isinstance(child, (wx.Panel, wx.StaticBox)):
            found = first_focusable(child)
            if found is not None:
                return found
        elif child.AcceptsFocus() and child.IsShown() and child.IsEnabled():
            return child
    return None


def backup(name, kind="profile", pads=0, baseline=False, **values):
    record = {"key": ("show:7" if kind == "unit" else "profile:" + name), "name": name,
              "kind": kind, "pads": pads, "saved_at": None, "baseline": baseline,
              "renameable": kind != "unit", "snapshot": None}
    record.update(values)
    return record


def data(backups, storage=True, baseline=None, can_backup=True):
    return {"backups": {"ok": True, "backups": backups, "storage": storage, "shows_capable": True,
                        "note": ""},
            "shows": {"ok": True, "writable": True, "can_backup": can_backup,
                      "backup_blocked_reason": "" if can_backup else "No drive is in the desk."},
            "baseline": {"ok": True, "connected": True, "baseline": baseline}}


# ---- what each backup is called and what it says ---------------------------------------------
check("an ordinary backup is its name", backups_section.list_label(backup("Tuesday")), "Tuesday")
check("the as-found backup says so",
      backups_section.list_label(backup("As found", baseline=True)), "As found, the as-found backup")
check("one the desk made says so, with its date",
      backups_section.list_label(backup("Show", kind="unit", modified="Monday at 14:03")),
      "Show, made on the desk, Monday at 14:03")
check_in("a paired backup's detail names both copies", "its own copy on its drive too",
         backups_section.detail(backup("Tuesday", kind="paired", pads=3)))
check_in("and counts its pads", "with 3 SMART pads",
         backups_section.detail(backup("Tuesday", kind="paired", pads=3)))
check_in("one the desk made cannot be renamed", "cannot be renamed",
         backups_section.detail(backup("Show", kind="unit")))
check("no drive in says where backups go",
      backups_section.storage_line(data([], storage=False)["backups"]),
      "There is no card or drive in the desk, so backups are saved on this computer only.")
check_in("a baseline without pads warns", "settings only",
         backups_section.baseline_line({"baseline": {"name": "As found", "pads": 0}}))

question = {"plan": {"ok": True, "via": "profile", "warnings": ["Main Stereo goes silent."]},
            "info": {"ok": True, "pads": 4, "seconds_per_pad": 15, "settings_seconds": 25}}
text = backups_section.restore_text("Tuesday", question)
check_true("the restore question leads with its warning", text.startswith("Warning. Main Stereo"))
check_in("and says how long the pads take", "allow up to 1 minute 25 seconds", text)
check_in("a desk-made restore names the hang", "stops responding",
         backups_section.restore_text("Show", {"plan": {"ok": True, "via": "show"}, "info": {}}))

# ---- the page ----------------------------------------------------------------------------------
frame = FakeFrame()
section = backups_section.BackupsSection(frame)
page = section.page
check("before a read the list says so", page.list.GetItems(), ["Reading the backups."])
check("a desk read starts the first read", True, hasattr(frame.desk, "listener"))
check("the backups follow the shows and the drive",
      [(names, fn.__self__._read.__self__) for names, fn in frame.desk.watched],
      [(set(show_actions.SHOW_STATE_PROPS), section)])

section.data = data([])
page.show()
check("no backups is one sentence", page.list.GetItems(), [backups_section.NO_BACKUPS])
page._on_rename(None)
check("acting on it says there is nothing", frame.said[-1], backups_section.NO_BACKUPS)

section.data = data([backup("Monday"), backup("Tuesday", kind="paired", pads=2)],
                    baseline={"name": "Monday", "pads": 0})
page.show()
page.list.SetSelection(1)
page._show_detail()
check_in("Restore describes the chosen backup", "with 2 SMART pads", page.restore_acc.description)
check_true("Add pads shows for a baseline without pads", page.add_pads.IsShown())
section.data = data([backup("Sunday"), backup("Monday"), backup("Tuesday", kind="paired")],
                    baseline={"name": "Monday", "pads": 5})
page.show()
check("the chosen backup survives a read", page._chosen()["name"], "Tuesday")
check_true("Add pads hides once the baseline has pads", not page.add_pads.IsShown())
page.show(prefer="Sunday")
check("a preferred name is chosen", page._chosen()["name"], "Sunday")
check("the storage line is on Back up", page.backup_acc.description,
      backups_section.storage_line(section.data["backups"]))

# ---- the jobs start the right call and follow the right status --------------------------------
# A read after a job would read the real store and the desk.
reads = []
section.read = lambda announce=False, prefer=None: reads.append(prefer)
jobs, reports, questions = [], [], []


def fake_follow(_parent, title, _text, start, status):
    started = start()
    jobs.append((title, status(started)))
    return {"ok": True, "message": "Done."}


fakes = (backups_section, {"follow_job": fake_follow,
                           "report": lambda _p, titles, reply: reports.append(titles[0]),
                           "confirm": lambda _p, title, text, _a: questions.append(text) or True})

page.list.SetSelection(2)
with patched(fakes, (backups, {"action_restore_backup": lambda name: {"ok": True, "via": "profile",
                                                                     "started": True}})):
    page._ask_restore(page._chosen(), question)
check("a restore follows the profile restore",
      jobs[-1], ("Restoring backup", profile_actions.profile_restore_status))
check("and the whole desk is read again", frame.rewritten, 1)
with patched(fakes, (backups, {"action_restore_backup": lambda name: {"ok": True, "via": "show",
                                                                     "started": True}})):
    page._ask_restore(page._chosen(), question)
check("one through the desk's copy follows the show job",
      jobs[-1], ("Restoring backup", show_actions.show_job_status))

deletes = []
answers = iter([{"ok": False, "baseline": True, "message": "This is the as-found backup."},
                {"ok": True, "message": "Deleted Monday."}])
page.list.SetSelection(1)
with patched(fakes, (backups_section, {"call": lambda _o, fn, *a, done=None: done(fn(*a))}),
             (backups, {"action_delete_backup": lambda name, force: deletes.append((name, force))
                       or next(answers)})):
    page._deleted(page._chosen(), backups.action_delete_backup("Monday", False))
check("the as-found backup is deleted only on a second, forced yes",
      deletes, [("Monday", False), ("Monday", True)])
check_in("asked in the server's own words", "This is the as-found backup.", questions[-1])
check("and its result is spoken", frame.said[-1], "Deleted Monday.")
check_true("and the backups are read again", reads)

page.list.SetSelection(2)
with patched(fakes):
    page._deleted(page._chosen(), {"ok": True, "started": True, "message": "Deleting."})
check("a delete that started a job on the drive follows it",
      jobs[-1], ("Deleting backup", show_actions.show_job_status))

# ---- the factory reset -------------------------------------------------------------------------
section.data = data([], can_backup=False)
section.reset.show()
check_in("the reset says it cannot be undone without a drive", "cannot be undone",
         section.reset.acc.description)
dialog = backup_dialogs.ResetDialog(frame.window, "No drive is in the desk.")
check("the reset's question lands on Cancel", first_focusable(dialog).GetLabel(), "Cancel")
check_true("with nowhere to put a backup it is not ticked", not dialog.backup_first.GetValue())
said = []
with patched((dialogs.SpeakingDialog, {"say": lambda _s, text, recent=False: said.append(text)})):
    dialog.backup_first.SetValue(True)
    dialog._on_tick(None)
check_true("a tick is refused with the reason",
           not dialog.backup_first.GetValue() and said == ["No drive is in the desk."])
check_true("and no backup is asked for", not dialog.wants_backup())
dialog.Destroy()
dialog = backup_dialogs.ResetDialog(frame.window, "")
check_true("with a drive in, backing up first is ticked", dialog.wants_backup())
dialog.Destroy()

dialog = backup_dialogs.BaselineDialog(frame.window, {"device_name": "RODECaster Pro II",
                                                      "suggested_name": "As found Pro II"})
check("the as-found offer lands on Back up now", first_focusable(dialog).GetLabel(), "Back up now")
check_true("with the pads included", dialog.include_pads.GetValue())
dialog.Destroy()

dialog = dialogs.NameDialog(frame.window, "Rename backup", "New name for Monday", "Monday",
                            "Rename", dialogs.NameDialog.RULE)
check_true("a name dialog lands on the name", first_focusable(dialog) is dialog.name_box)
refused = []
with patched((dialogs.NameDialog, {"_refuse": lambda _s, text: refused.append(text)})):
    dialog.name_box.SetValue("bad/name")
    dialog._on_ok(wx.CommandEvent())
check("a name outside the rule is refused, and the dialog stays", refused,
      [dialogs.NameDialog.RULE])
check_true("nothing was handed over", dialog.reply is None)
dialog.Destroy()

frame.window.Destroy()
_a11y.check_windows("Backups")
sys.exit(_testkit.finish())
