"""Pad maintenance: the jobs that act on the whole grid (clear every pad, put a backup's pads back,
restore the factory voice effects, tidy up pad audio), each asked about first, and copying one sound
pad's file to the computer, which changes nothing on the desk and so asks nothing.
"""
import datetime

import wx

import server
import soundpad
import wording
from native import a11y
from native.calls import call
from native.dialogs import JobDialog, ListDialog, confirm, follow_job, report
from native.pad_dialogs import READ_ONLY, content, where
from native.pages import page_body, select

TITLE = "Pad maintenance"


def run_job(owner, title, working, action, *args, reread=True):
    """Run `action(*args)` in a job dialog that stays until it is done, then read the pads, so the
    page shows what the job left. Returns the action's reply and the read, as {"reply", "model"}.
    Nothing else can be started meanwhile."""
    def work():
        reply = action(*args)
        return {"reply": reply, "model": server.soundpad_model() if reread else None}

    job = JobDialog(owner, title, working, lambda dialog: call(dialog, work, done=dialog.finish),
                    stop=None, closable=False)
    with job:
        result = job.run()
    # A crash in `work` comes back from `call` as a bare refusal.
    return result if "reply" in result else {"reply": result, "model": None}


NO_BACKUPS = ("There is no backup with SMART pads in it on this computer. Back up the desk on the "
              "Backups page, and its pads can be put back from here.")


def backup_label(backup):
    """A backup as the chooser lists it: its name, how many pads it holds, and when it was saved, in
    words (`wording.spoken_datetime`)."""
    label = f"{backup['name']}. {wording.count(backup['pads'], 'pad')}"
    if backup["saved_at"]:
        label += ", saved " + wording.spoken_datetime(
            datetime.datetime.fromtimestamp(backup["saved_at"]))
    return label


def _message(reply):
    return reply.get("message") or ("Done." if reply.get("ok") else "That did not work.")


class MaintPage(wx.Panel):
    def __init__(self, section):
        super().__init__(section.frame.host)
        self.Hide()
        self.section = section
        self.frame = section.frame
        self._exporting = False
        sizer, parent = page_body(self)

        self.clear_all = self._button(parent, sizer, "Clear all pads",
                                      "Empties every pad except the built-in System pads.",
                                      self._on_clear_all)
        self.restore_backup = self._button(
            parent, sizer, "Restore the pads from a backup",
            "Puts every pad back as a backup on this computer has it, sounds included. Nothing "
            "else on the desk changes.", self._on_restore_backup)
        self.restore_fx = self._button(
            parent, sizer, "Restore the factory voice effects, Censor and Ducking",
            "Overwrites those eight pads with their factory versions; the factory sounds are not "
            "restored. Takes two or three minutes.", self._on_restore_fx)
        self.tidy = self._button(parent, sizer, "Tidy up unused pad audio",
                                 "Deletes sound files no pad plays, to free pad storage. Cannot "
                                 "be undone.", self._on_tidy)

        line = wx.BoxSizer(wx.HORIZONTAL)
        line.Add(wx.StaticText(parent, label="Sound pad"), 0,
                 wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.sound_pads = []
        self.export_choice = wx.Choice(parent, size=(320, -1))
        a11y.describe(self.export_choice, "Only sound pads have a file to export: a voice effect "
                                          "is a recipe the desk builds, not a recording.")
        line.Add(self.export_choice, 0)
        sizer.Add(line, 0, wx.ALL, 6)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.export = wx.Button(parent, label="Export this pad's sound")
        a11y.describe(self.export, "Copies the sound to Documents, AccessCaster, Pad exports. The "
                                   "pad on the desk is unchanged.")
        self.export.Bind(wx.EVT_BUTTON, self._on_export)
        row.Add(self.export, 0, wx.RIGHT, 8)
        folder = wx.Button(parent, label="Open the exports folder")
        folder.Bind(wx.EVT_BUTTON, self._on_open_folder)
        row.Add(folder, 0)
        sizer.Add(row, 0, wx.ALL, 6)
        self.show()

    @property
    def model(self):
        return self.section.model

    def _button(self, parent, sizer, label, description, handler):
        button = wx.Button(parent, label=label)
        a11y.describe(button, description)
        button.Bind(wx.EVT_BUTTON, handler)
        sizer.Add(button, 0, wx.ALL, 6)
        return button

    def show(self):
        """The sound pads offered for export, keeping the chosen one."""
        index = self.export_choice.GetSelection()
        # With no sound pad, the one item is the sentence saying so.
        chosen = self.sound_pads[index]["grid"] if 0 <= index < len(self.sound_pads) else None
        self.sound_pads = [pad for pad in self.model["pads"]
                           if pad["present"] and pad["type"] == soundpad.PAD_TYPE_SOUND]
        labels = [f"{where(pad).capitalize()}. {content(pad)}" for pad in self.sound_pads]
        if labels != self.export_choice.GetItems():
            self.export_choice.Set(labels or ["No pad has a sound on it"])
        grids = [pad["grid"] for pad in self.sound_pads]
        select(self.export_choice, grids.index(chosen) if chosen in grids else 0)

    def _refused(self):
        if self.model["read_only"]:
            self.frame.say(READ_ONLY)
            return True
        return False

    def _finished(self, source, result, titles):
        """A job has ended: show the pads read after it, and what it came to (`dialogs.report`)."""
        if result.get("model"):
            self.section.show(result["model"])
        report(self, titles, result["reply"])
        source.SetFocus()

    def _on_clear_all(self, _event):
        if self._refused() or not confirm(
                self, "Clear all pads?",
                "This empties every sound, voice effect, mixer and MIDI pad on the grid. The "
                "built-in System pads are left alone.\n\nRestore the pads from a backup puts them "
                "back, so back up the desk first if you might want them. A backup made just after "
                "a factory reset brings back the factory pads the same way.", "Clear all"):
            return
        self._finished(self.clear_all, run_job(self, "Clearing all pads",
                                               "Clearing every pad. Please wait.",
                                               server.action_clear_all_pads),
                       ("Pads cleared", "Pads not cleared"))

    def _on_tidy(self, _event):
        if self._refused() or not confirm(
                self, "Delete unused pad audio?",
                "This removes sound files from the desk that no pad plays any more, freeing up "
                "space. Nothing you can play now is touched, but the files themselves are deleted "
                "and cannot be recovered.", "Delete unused audio"):
            return
        self._finished(self.tidy, run_job(self, "Tidying up pad audio",
                                          "Looking for sound files no pad plays. Please wait.",
                                          server.action_tidy_pad_audio, reread=False),
                       ("Pad audio tidied", "Pad audio not tidied"))

    def _on_restore_backup(self, _event):
        if not self._refused():
            call(self, server.pad_backups, done=self._choose_backup)

    def _choose_backup(self, reply):
        backups = reply.get("backups") or []
        if not backups:
            self.frame.say(reply.get("message") if not reply.get("ok") else NO_BACKUPS)
            return
        dialog = ListDialog(self, "Restore the pads from a backup", "Backup",
                            [b["name"] for b in backups], "Restore the pads",
                            [backup_label(b) for b in backups],
                            "Only the pads change. The newest backup is first.")
        with dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            backup = backups[dialog.list.GetSelection()]
        longest = wording.describe_duration(backup["pads"] * reply["seconds_per_pad"])
        if not confirm(self, "Restore the pads from this backup?",
                       f"Every pad goes back to how it is in {backup['name']}, and a pad that is "
                       "empty in the backup is emptied. Nothing else on the desk changes.\n\n"
                       f"It can take up to {longest}, less where pads already match.",
                       "Restore the pads"):
            return
        self._pad_job(self.restore_backup, "Restoring the pads",
                      f"Restoring the SMART pads from {backup['name']}.",
                      ("Pads restored", "Pads not restored"),
                      server.action_restore_backup_pads, backup["name"])

    def _on_restore_fx(self, _event):
        if self._refused() or not confirm(
                self, "Restore the factory voice effects, Censor and Ducking?",
                "This puts the six factory voice effects back on their own pads, and the Censor "
                "and Ducking pads with them, overwriting whatever is on those eight pads now. The "
                "factory sounds are not touched and cannot be restored from here, and neither is "
                "anything past pad 16. It takes two or three minutes, and can be undone only by "
                "setting those pads again.", "Restore those eight pads"):
            return
        self._pad_job(self.restore_fx, "Restoring the factory voice effects",
                      "Starting the restore. It takes two or three minutes.",
                      ("Factory voice effects restored", "Factory voice effects not restored"),
                      server.action_restore_factory_fx)

    def _pad_job(self, source, title, text, titles, action, *args):
        """A job on the pad-restore channel, started by `action(*args)` and followed to the end,
        then the pads read again."""
        reply = follow_job(self, title, text, lambda: action(*args),
                           lambda _started: server.pad_restore_status)
        self._finished(source, {"reply": reply}, titles)
        self.section.read()

    def _on_export(self, _event):
        if not self.sound_pads:
            self.frame.say("No pad has a sound on it to export.")
            return
        if self._exporting:
            self.frame.say("Still copying the last one.")
            return
        pad = self.sound_pads[self.export_choice.GetSelection()]
        self._exporting = True
        self.frame.say(f"Copying {where(pad)} off the desk.")

        def done(reply):
            self._exporting = False
            # Always spoken: the reply names the file, which is the point of the export.
            self.frame.say(_message(reply))
        call(self, server.action_export_pad, pad["grid"], done=done)

    def _on_open_folder(self, event):
        source = event.GetEventObject()
        call(self, server.action_open_pad_exports,
             done=lambda reply: self.frame.reply(source, reply))
