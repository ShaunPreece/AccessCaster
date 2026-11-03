"""The question before a firmware update: what it does and costs, and whether to back up first."""
import wx

from native import a11y
from native.dialogs import SpeakingDialog, _paragraphs


def question_text(version, current):
    return (f"Firmware {version} replaces {current or 'the firmware'} on the unit. RODE offers no "
            "way back to an older version.\n\n"
            "The unit downloads the update itself, installs it and restarts, which takes about "
            "five minutes. The update keeps your settings, SMART pads, recordings, wifi and "
            "Bluetooth pairings.\n\n"
            "While the unit restarts its audio stops for a minute or two, so a screen reader "
            "speaking through it goes quiet until it is back. Do not switch the unit off while it "
            "updates.")


class FirmwareDialog(SpeakingDialog):
    """Lands on Cancel. The backup-first tick is ticked."""

    def __init__(self, parent, version, current, backup_name):
        super().__init__(parent, f"Install firmware {version}")
        sizer = wx.BoxSizer(wx.VERTICAL)
        _paragraphs(self, sizer, question_text(version, current))
        self.backup_first = wx.CheckBox(self, label="Back the unit up first")
        self.backup_first.SetValue(True)
        a11y.describe(self.backup_first,
                      f"Saves your settings and SMART pads on this computer as {backup_name}, "
                      "and a copy on the unit's drive if one is in. It takes a minute or two.")
        sizer.Add(self.backup_first, 0, wx.ALL, 12)
        go = wx.Button(self, wx.ID_OK, label=f"Install firmware {version}")
        cancel = wx.Button(self, wx.ID_CANCEL, label="Cancel")
        cancel.SetDefault()
        # The tab order starts on Cancel, so that is where the focus lands.
        cancel.MoveBeforeInTabOrder(self.backup_first)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(go, 0, wx.ALL, 5)
        row.Add(cancel, 0, wx.ALL, 5)
        sizer.Add(row, 0, wx.ALIGN_RIGHT | wx.ALL, 7)
        sizer.Add(self.sink, 0)
        self.SetSizerAndFit(sizer)
        self.Bind(wx.EVT_BUTTON, lambda _e: self.EndModal(wx.ID_OK), id=wx.ID_OK)
        self.SetEscapeId(wx.ID_CANCEL)
        self.CentreOnParent()

    def wants_backup(self):
        return self.backup_first.GetValue()
