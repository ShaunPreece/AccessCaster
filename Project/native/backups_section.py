"""Backups: one page for backing the desk up and acting on a backup (restore, rename, delete, and
the as-found backup), and one for the factory reset. The first-run offer to back the desk up as it
was found is made from here too.

One list holds every backup, whichever half holds it: a copy on this computer, the unit's own copy
on its drive, or both under one name (`server.read_backups`). Which half does the work is the
server's business; the list only says what a backup can do.

The backups are read once the desk state has first read the desk, on F5 and after every job here:
two model dumps (`read_backups`, `read_show_state`), never on show.
"""
import datetime

import wx

import server
import wording
from native import a11y
from native.backup_dialogs import BaselineDialog, ResetDialog
from native.calls import call
from native.dialogs import NameDialog, confirm, follow_job, report
from native.pages import Page, group, labelled, page_box

TITLE = "Backups"
RESET_TITLE = "Start again from factory settings"
NO_BACKUPS = "No backups yet. Back the unit up first."
HOLDS = ("A backup holds the unit's settings and your SMART pads: channels, processing, routing, "
         "faders, the unit's own settings, and every pad with its sound. Two things are left out "
         "on purpose: which channels are muted, since putting a mute back could silence someone at "
         "the worst moment, and passwords, so wifi and phone-call credentials are never stored.")
APP_SETTINGS = ("Your keyboard shortcuts and other AccessCaster preferences are not part of a "
                "backup of the unit. App settings saves them to a file.")


def _read():
    return {"backups": server.read_backups(), "shows": server.read_show_state(),
            "baseline": server.baseline_status()}


def _baseline_offer():
    """Whether to offer the as-found backup. The offer is filed under the desk's hardware id, which
    only a status read publishes."""
    status = server.baseline_status()
    if not status.get("connected"):
        server.read_device_status()
        status = server.baseline_status()
    return status


def _restore_question(name):
    """How `name` would be restored and what is in it, for the question before the restore."""
    plan = server.backup_restore_plan(name)
    info = server.profile_info(name) if plan.get("ok") and plan.get("via") != "show" else {}
    return {"plan": plan, "info": info}


def list_label(backup):
    if backup["kind"] == "unit":
        return f"{backup['name']}, made on the unit" + (
            f", {backup['modified']}" if backup.get("modified") else "")
    return f"{backup['name']}, the as-found backup" if backup["baseline"] else backup["name"]


def detail(backup):
    """What a backup holds and where, in what it can do rather than which half it is."""
    if backup["kind"] == "unit":
        text = ("Made on the unit itself, so it holds everything and puts the whole desk back in "
                "seconds. There is no copy on this computer, and it cannot be renamed.")
    else:
        pads = backup.get("pads") or 0
        text = "Held on this computer" + (f", with {wording.count(pads, 'SMART pad')}"
                                          if pads else "")
        if backup.get("saved_at"):
            text += ", saved " + wording.spoken_datetime(
                datetime.datetime.fromtimestamp(backup["saved_at"]))
        text += (". The unit has its own copy on its drive too." if backup["kind"] == "paired"
                 else ".")
    if backup["baseline"]:
        text += " This is the as-found backup: how the unit was before anything changed it."
    return text


def storage_line(backups):
    """Where a new backup goes: the one part the user can change, by putting a drive in."""
    if backups.get("note"):
        return backups["note"]
    if not backups.get("shows_capable"):
        return "Backups are saved on this computer."
    if backups.get("storage"):
        return ("Backups are saved on this computer, and the unit keeps its own copy on its drive "
                "too.")
    return "There is no card or drive in the unit, so backups are saved on this computer only."


def baseline_line(status):
    """The as-found backup: whether there is one and whether its pads were taken."""
    baseline = (status or {}).get("baseline")
    if not baseline:
        if status and status.get("connected"):
            return ("No as-found backup of this unit has been taken on this computer, so there is "
                    "no way back if a change goes wrong. Back up the unit now takes one.")
        return "No as-found backup, and no unit connected to take one from."
    when = ""
    if baseline.get("saved_at"):
        when = ", taken " + wording.spoken_datetime(
            datetime.datetime.fromtimestamp(baseline["saved_at"]))
    if baseline.get("pads"):
        return (f"As-found backup: {baseline['name']}{when}, with "
                f"{wording.count(baseline['pads'], 'SMART pad')}.")
    return (f"As-found backup: {baseline['name']}{when}, settings only. Its SMART pads were not "
            "copied, so the pad sounds as they came are not backed up anywhere.")


def restore_text(label, question):
    """The question before a restore: what it will leave silent first, then what it does and how
    long it takes, all read when the dialog opens."""
    plan, info = question["plan"], question["info"]
    warnings = plan.get("warnings") or []
    warn = ("Warning. " + " ".join(warnings) + "\n\n") if warnings else ""
    if plan.get("via") == "show":
        return (warn + f"This puts the whole unit back from its own copy of {label}: every "
                "setting, the routing, the fader layout and all your SMART pads, exactly as they "
                "were. It takes a few seconds, and it moves the monitor speaker level, so turn "
                "speakers down first.\n\n"
                "The unit sometimes stops responding after restoring its own copy, and has to be "
                "unplugged from the USB socket nearest the power inlet, or have its power pulled, "
                "before it comes back. Nothing is lost when that happens: the restore itself goes "
                "through. But if your screen reader plays through the RODECaster, you will lose "
                "speech until it is back.")
    text = (warn + f"This puts your saved settings back onto the unit from this computer's copy "
            f"of {label}. Anything the unit does not accept is skipped and reported afterwards. "
            "The SMART pads saved in it are put back and any other pad on the unit is left alone; "
            "to make the grid match exactly, clear all pads first. It cannot be undone except by "
            "restoring another backup.")
    if info.get("ok"):
        text += "\n\n" + restore_time(info)
    return text


def restore_time(info):
    """How long a restore takes, from the one thing that decides it: the pad count."""
    pads = info.get("pads") or 0
    if not pads:
        return "It has no SMART pads, so it should take about half a minute."
    per_pad = info.get("seconds_per_pad") or 15
    longest = wording.describe_duration(
        (info.get("settings_seconds") or 25) + pads * per_pad)
    return (f"It has {wording.count(pads, 'SMART pad')}. A pad already as saved is left alone, "
            f"and any other takes about {per_pad} seconds, so allow up to {longest}.")


class BackupsSection:
    title = TITLE
    route = "profiles"

    def __init__(self, frame):
        self.frame = frame
        self.data = None
        self.last_name = ""          # the name the last backup was given, offered again
        self._reading = False
        self._again = None           # a read asked for while one runs: (announce, prefer)
        self._offered = False
        self.page = BackupsPage(self)
        self.reset = ResetPage(self)
        self.pages = [Page("backups", TITLE, self.page), Page("reset", "Factory settings",
                                                              self.reset)]
        frame.desk.subscribe(self._desk_event)

    def refresh(self, announce):
        self.read(announce)

    def _desk_event(self, event, reply):
        if event != "read":
            return
        # Backups on this computer are listed whether or not a desk is there.
        if self.data is None and not self._reading:
            self.read()
        if reply.get("ok") and not self._offered:
            self._offered = True
            call(self.frame, _baseline_offer, done=self._offer_baseline)

    def read(self, announce=False, prefer=None):
        """Read the backups again; `prefer` is a backup name to choose once they are shown."""
        if self._reading:
            self._again = (bool(self._again and self._again[0]) or announce, prefer)
            return
        self._reading = True
        call(self.frame, _read, done=lambda data: self._read_done(data, announce, prefer))

    def _read_done(self, data, announce, prefer):
        self._reading = False
        if "backups" not in data:
            # A crash in `_read` comes back from `call` as a bare refusal.
            if announce:
                self.frame.say(data.get("message") or "Could not read the backups.")
        else:
            self.data = data
            self.page.show(prefer)
            self.reset.show()
            if announce:
                self.frame.say("Backups read.")
        if self._again is not None:
            again, self._again = self._again, None
            self.read(*again)

    # ---- the as-found backup ---------------------------------------------------------------

    def _offer_baseline(self, status):
        # Asked once per desk, and never on top of a dialog already open: it is offered again at
        # the next start.
        if not status.get("offer") or _modal_open():
            return
        dialog = BaselineDialog(self.frame, status)
        with dialog:
            answer = dialog.ShowModal()
            include_pads = dialog.include_pads.GetValue()
        if answer == wx.ID_NO:
            call(self.frame, server.action_baseline_skip,
                 done=lambda reply: self.frame.say(reply.get("message") or "No backup was taken."))
        elif answer == wx.ID_OK:
            reply = follow_job(self.frame, "Backing up the unit as found",
                               "Saving the unit's settings, then each SMART pad in turn."
                               if include_pads else "Saving the unit's settings.",
                               lambda: server.action_baseline_save(include_pads),
                               lambda _started: server.profile_save_status)
            report(self.frame, ("Unit backed up", "Backup failed"), reply)
            self.read()


def _modal_open():
    return any(isinstance(window, wx.Dialog) and window.IsModal()
               for window in wx.GetTopLevelWindows())


class BackupsPage(wx.Panel):
    """Back up the unit now; the list of backups and the three things done to the one chosen; what
    a backup holds and the state of the as-found backup, with the button that adds its pads."""

    def __init__(self, section):
        super().__init__(section.frame.host)
        self.Hide()
        self.section = section
        self.frame = section.frame
        self.backups = []            # the backups as listed; empty when the list holds NO_BACKUPS
        sizer, parent = page_box(self, TITLE)
        self.backup = wx.Button(parent, label="Back up the unit now")
        self.backup_acc = a11y.describe(self.backup, "")
        self.backup.Bind(wx.EVT_BUTTON, self._on_backup)
        sizer.Add(self.backup, 0, wx.ALL, 6)

        box_sizer, box = group(parent, sizer, "Your backups")
        self.list = labelled(box, box_sizer, "Backup",
                             lambda p: wx.ListBox(p, choices=["Reading the backups."],
                                                  size=(420, 180)))
        self.list.Bind(wx.EVT_LISTBOX, lambda _e: self._show_detail())
        row = wx.BoxSizer(wx.HORIZONTAL)
        # The chosen backup's detail is on Restore, the press it informs, and not on the list: a
        # description that changes under a focused list is read again over the item landed on.
        self.restore = self._button(box, row, "Restore selected backup", self._on_restore)
        self.restore_acc = a11y.describe(self.restore, "")
        self.rename = self._button(box, row, "Rename selected backup", self._on_rename)
        self.delete = self._button(box, row, "Delete selected backup", self._on_delete)
        box_sizer.Add(row, 0, wx.ALL, 6)

        self.about = labelled(parent, sizer, "About backups", lambda p: wx.TextCtrl(
            p, size=(460, 150), style=wx.TE_MULTILINE | wx.TE_READONLY))
        # Hidden while the as-found backup is complete or there is none.
        self.add_pads = wx.Button(parent, label="Add SMART pads to the as-found backup")
        a11y.describe(self.add_pads, "Copies each pad's sound into the as-found backup. Its "
                                     "settings stay as they were found.")
        self.add_pads.Bind(wx.EVT_BUTTON, self._on_add_pads)
        self.add_pads.Hide()
        sizer.Add(self.add_pads, 0, wx.ALL, 6)

    def _button(self, parent, row, label, handler):
        button = wx.Button(parent, label=label)
        button.Bind(wx.EVT_BUTTON, handler)
        row.Add(button, 0, wx.RIGHT, 8)
        return button

    @property
    def data(self):
        return self.section.data

    def show(self, prefer=None):
        """Put the section's read on the page, keeping the chosen backup, or choosing `prefer`."""
        chosen = self._chosen()
        key = chosen["key"] if chosen else None
        self.backups = self.data["backups"].get("backups") or []
        labels = [list_label(b) for b in self.backups] or [NO_BACKUPS]
        if self.list.GetCount() == len(labels):
            # Item by item, so an unchanged list is not replaced under the screen reader.
            for index, label in enumerate(labels):
                if self.list.GetString(index) != label:
                    self.list.SetString(index, label)
        else:
            self.list.Set(labels)
        keys = [b["key"] for b in self.backups]
        names = [b["name"] for b in self.backups]
        if prefer in names:
            index = names.index(prefer)
        else:
            index = keys.index(key) if key in keys else 0
        self.list.SetSelection(index)
        self._show_detail()
        self.backup_acc.description = storage_line(self.data["backups"])
        status = self.data["baseline"]
        self.about.SetValue("\n\n".join([baseline_line(status), HOLDS, APP_SETTINGS]))
        baseline = status.get("baseline") if status else None
        wanted = bool(baseline and not baseline.get("pads"))
        if self.add_pads.IsShown() != wanted:
            if not wanted and wx.Window.FindFocus() is self.add_pads:
                self.about.SetFocus()
            self.add_pads.Show(wanted)
            self.Layout()

    def _chosen(self):
        index = self.list.GetSelection()
        return self.backups[index] if self.backups and index != wx.NOT_FOUND else None

    def _show_detail(self):
        chosen = self._chosen()
        self.restore_acc.description = detail(chosen) if chosen else ""

    def _none_chosen(self):
        if self._chosen() is None:
            self.frame.say(NO_BACKUPS if self.data else "The backups are still being read.")
            return True
        return False

    @staticmethod
    def _handle(backup):
        """What the server knows a backup by: a profile name, or the key of one the unit made."""
        return backup["key"] if backup["kind"] == "unit" else backup["name"]

    def _on_backup(self, _event):
        dialog = NameDialog(self, "Back up the unit", "Name for this backup",
                            self.section.last_name, "Save",
                            NameDialog.RULE + " A backup of the same name is replaced.")
        with dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            name = dialog.name()
        self.section.last_name = name
        reply = follow_job(self, "Backing up",
                           "Backing the unit up: its own copy if there is a drive in, then the "
                           "settings and each SMART pad on this computer.",
                           lambda: server.action_backup_now(name),
                           lambda _started: server.profile_save_status)
        report(self, ("Backed up", "Backup failed"), reply)
        self.backup.SetFocus()
        self.section.read(prefer=name)

    def _on_restore(self, _event):
        if self._none_chosen():
            return
        backup = self._chosen()
        call(self, _restore_question, self._handle(backup),
             done=lambda question: self._ask_restore(backup, question))

    def _ask_restore(self, backup, question):
        if not question["plan"].get("ok"):
            self.frame.say(question["plan"].get("message") or "That backup could not be read.")
            return
        if not confirm(self, f"Restore {backup['name']}?", restore_text(backup["name"], question),
                       "Restore"):
            return
        handle = self._handle(backup)
        reply = follow_job(self, "Restoring backup", f"Restoring {backup['name']} to the unit.",
                           lambda: server.action_restore_backup(handle),
                           # The two halves report through different jobs, and the start says
                           # which one is running.
                           lambda started: (server.show_job_status if started.get("via") == "show"
                                            else server.profile_restore_status))
        report(self, ("Backup restored", "Restore failed"), reply)
        self.restore.SetFocus()
        self.frame.desk_rewritten()

    def _on_rename(self, _event):
        if self._none_chosen():
            return
        backup = self._chosen()
        if not backup["renameable"]:
            self.frame.say("This backup was made on the unit, which names its own backups, so it "
                           "cannot be renamed here.")
            return
        old = backup["name"]
        dialog = NameDialog(self, "Rename backup", f"New name for {old}", old, "Rename",
                            NameDialog.RULE,
                            submit=lambda new: server.rename_profile(old, new))
        with dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            new, reply = dialog.name(), dialog.reply
        self.list.SetFocus()
        self.frame.say(reply.get("message") or "Renamed.")
        self.section.read(prefer=new)

    def _on_delete(self, _event):
        if self._none_chosen():
            return
        backup = self._chosen()
        if backup["kind"] == "unit":
            text = (f"This permanently deletes {backup['name']}, the backup the unit made, from "
                    "the unit's drive. It cannot be undone.")
        else:
            text = (f"This permanently deletes the backup {backup['name']} from this computer, "
                    "including any pad sounds saved with it"
                    + (", and the unit's own copy on its drive" if backup["kind"] == "paired"
                       else "")
                    + ". It cannot be undone, and it changes nothing else on the unit.")
        if not confirm(self, f"Delete {backup['name']}?", text, "Delete"):
            return
        self._delete(backup, force=False)

    def _delete(self, backup, force):
        call(self, server.action_delete_backup, self._handle(backup), force,
             done=lambda reply: self._deleted(backup, reply))

    def _deleted(self, backup, reply):
        if not reply.get("ok") and reply.get("baseline"):
            # The as-found backup takes a second, deliberate yes, on the server's own words.
            if confirm(self, "Delete the as-found backup?", reply.get("message") or "",
                       "Delete the as-found backup"):
                self._delete(backup, force=True)
            return
        if reply.get("ok") and reply.get("started"):
            # The unit's copy is deleted by a job on the drive, which then reports the result.
            started = reply
            result = follow_job(self, "Deleting backup", "Deleting the backup.", lambda: started,
                                lambda _started: server.show_job_status)
            report(self, ("Backup deleted", "Delete failed"), result)
            self.delete.SetFocus()
        else:
            self.list.SetFocus()
            self.frame.say(reply.get("message") or ("Deleted." if reply.get("ok")
                                                    else "That did not work."))
        self.section.read()

    def _on_add_pads(self, _event):
        reply = follow_job(self, "Adding SMART pads to the backup",
                           "Copying each pad's sound off the unit in turn.",
                           server.action_baseline_add_pads,
                           lambda _started: server.profile_save_status)
        report(self, ("Pads added to the backup", "Could not add the pads"), reply)
        self.about.SetFocus()
        self.section.read()


class ResetPage(wx.Panel):
    """The software factory reset: one button, and the question it opens (`ResetDialog`)."""

    def __init__(self, section):
        super().__init__(section.frame.host)
        self.Hide()
        self.section = section
        self.frame = section.frame
        sizer, parent = page_box(self, RESET_TITLE)
        self.button = wx.Button(parent, label=RESET_TITLE)
        self.acc = a11y.describe(self.button, "")
        self.button.Bind(wx.EVT_BUTTON, self._on_reset)
        sizer.Add(self.button, 0, wx.ALL, 6)
        self.show()

    @property
    def shows(self):
        return self.section.data["shows"] if self.section.data else None

    def show(self):
        shows = self.shows
        # Whether it can be undone depends on there being somewhere for the backup to go.
        undo = ("The unit backs itself up first, so this can be undone by restoring that backup."
                if not shows or shows.get("can_backup", True)
                else "There is no memory card or USB drive in the unit, so it cannot back itself "
                     "up first, which means this cannot be undone.")
        self.acc.description = (
            "Puts every setting and every SMART pad back to how the unit left the factory. Your "
            "wifi, the language and the analytics answer are left alone, and so are the recordings "
            f"on the drive. {undo} It does not use the unit's touchscreen setup wizard, so "
            "nothing is left needing a sighted person to tap through.")

    def _on_reset(self, _event):
        shows = self.shows
        if shows is None:
            self.frame.say("The unit is still being read.")
            return
        if not shows.get("ok") or shows.get("writable") is False:
            self.frame.say(shows.get("message") if not shows.get("ok")
                           else "This unit cannot be reset from AccessCaster.")
            return
        blocked = "" if shows.get("can_backup") else (
            shows.get("backup_blocked_reason") or "There is nowhere to save a backup.")
        dialog = ResetDialog(self, blocked)
        with dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            backup_first = dialog.wants_backup()
        reply = follow_job(self, "Resetting the unit",
                           "Backing the unit up, then resetting it." if backup_first
                           else "Resetting the unit to factory settings.",
                           lambda: server.action_show_reset(backup_first),
                           lambda _started: server.show_job_status)
        report(self, ("Unit reset", "Reset failed"), reply)
        self.button.SetFocus()
        self.frame.desk_rewritten()
