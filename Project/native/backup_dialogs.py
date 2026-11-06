"""The Backups section's own dialogs: the factory reset's question, with its backup-first tick,
and the first-run offer to back the desk up as it was found.
"""
import wx

from native import a11y
from native.dialogs import BackupFirstDialog, paragraphs

RESET_TEXT = (
    "This puts every setting and every SMART pad back to how the desk left the factory, including "
    "RODE's eight factory pad sounds, which the desk rebuilds from its own firmware. That is the "
    "only way to get those sounds back, so if what you actually want is your own settings "
    "returned, restore a backup instead: it is quicker and it does not need the desk to "
    "reconnect.\n\n"
    "Your wifi, the language and the analytics answer are not part of it and are left alone, and "
    "the recordings on the drive are untouched. The monitor speaker level goes back to its "
    "default, so turn speakers down before you start.\n\n"
    # No rate is quoted: none is known well enough to state as a figure.
    "Resetting rebuilds the desk's USB connection, and it sometimes does not come back on its "
    "own. If the app stops finding the desk afterwards, unplug the USB cable in the socket nearest "
    "the power inlet, wait a few seconds and plug it back in. The reset itself will still have "
    "happened and nothing is lost.")


class ResetDialog(BackupFirstDialog):
    """The factory reset's question. With nowhere to save a backup (`blocked`, the reason) the tick
    is clear, a tick is refused with the reason, and the text says the reset cannot be undone,
    since that is what the dialog is read for."""

    def __init__(self, parent, blocked):
        text = RESET_TEXT
        if blocked:
            text += (f"\n\n{blocked} So this cannot be backed up first, and it cannot be undone. "
                     "Put a memory card or a USB drive in the desk first if you want a way back.")
        super().__init__(
            parent, "Start again from factory settings", text,
            "Back the desk up first, so this can be undone",
            blocked or ("Takes about a second and about 24 megabytes on the recordings drive. "
                        "Without it there is no way back to how the desk is now."),
            "Reset the desk", ticked=not blocked)
        self._blocked = blocked
        self.backup_first.Bind(wx.EVT_CHECKBOX, self._on_tick)

    def _on_tick(self, _event):
        if self._blocked and self.backup_first.GetValue():
            self.backup_first.SetValue(False)
            self.say(self._blocked)

    def wants_backup(self):
        return super().wants_backup() and not self._blocked


class BaselineDialog(wx.Dialog):
    """The first-run offer to back the desk up as it was found, before anything changes it. Lands
    on Back up now. `ShowModal` returns wx.ID_OK to back up, wx.ID_NO for Not now, which is
    remembered, and wx.ID_CANCEL for Escape, which is not, so the offer comes again next time."""

    def __init__(self, parent, status):
        super().__init__(parent, title="Back up this desk as you found it")
        sizer = wx.BoxSizer(wx.VERTICAL)
        paragraphs(self, sizer, (
            f"This looks like the first time AccessCaster has seen this "
            f"{status.get('device_name') or 'desk'}. Before anything is changed, it can save the "
            f"desk's current settings as a backup called {status.get('suggested_name') or ''}, so "
            "there is always a way back to how it is right now.\n\n"
            "Starting again from factory settings only ever gets you to how the desk left the "
            "factory, which is not the same as how it came to you, so once a setting is changed "
            "this is the only way back. It is only offered once per desk."))
        save = wx.Button(self, wx.ID_OK, label="Back up now")
        self.include_pads = wx.CheckBox(self, label="Include SMART pads (sounds and layout)")
        self.include_pads.SetValue(True)
        seconds = status.get("seconds_per_pad") or 1
        a11y.describe(self.include_pads, (
            f"About {seconds} second{'' if seconds == 1 else 's'} per pad, and the only copy of "
            "the pads as they came. They can be added later from the Backups page."))
        sizer.Add(self.include_pads, 0, wx.ALL, 12)
        skip = wx.Button(self, wx.ID_NO, label="Not now")
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(skip, 0, wx.ALL, 5)
        row.Add(save, 0, wx.ALL, 5)
        sizer.Add(row, 0, wx.ALIGN_RIGHT | wx.ALL, 7)
        save.SetDefault()
        self.SetSizerAndFit(sizer)
        self.Bind(wx.EVT_BUTTON, lambda event: self.EndModal(event.GetId()))
        # There is no Cancel button for Escape and the title bar's close to stand for.
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self.Bind(wx.EVT_CLOSE, lambda _e: self.EndModal(wx.ID_CANCEL))
        self.CentreOnParent()

    def _on_key(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
            return
        event.Skip()
