"""AccessCaster's own update: the decision, then the download and the hand-off to the installer.

`app_updates` checks, downloads and starts the installer; these dialogs ask and follow. Every
sentence comes from the server. Nothing installs without Install now.

The hand-off waits for the dialog: once the download is verified, the server does nothing until
`proceed`, which is called once the dialog cannot be stopped and the closing sentence has had time
to be read, since the installer closes this app. A screen reader cannot say when it has finished,
so the wait is estimated from the sentence's length.
"""
import wx

import app_updates
from native.calls import call
from native.dialogs import JobDialog, _paragraphs, report

INSTALL, SKIP, LATER = wx.ID_OK, wx.ID_NO, wx.ID_CANCEL
POLL_MS = 400
# The closing sentence's reading time, per character: about a default speech rate, so a faster one
# is never cut off, within a floor and a ceiling.
SPEAK_MS_PER_CHAR = 60
SPEAK_MIN_MS = 3000
SPEAK_MAX_MS = 10000
# An install and restart takes well under this; past it, the install did not take.
BACK_WITHIN_MS = 180000


def reading_ms(sentence):
    return min(SPEAK_MAX_MS, max(SPEAK_MIN_MS, len(sentence) * SPEAK_MS_PER_CHAR))


class DecisionDialog(wx.Dialog):
    """What the update is and what changed, then Install now, Skip this version or Later.
    `ShowModal` returns INSTALL, SKIP or LATER. The focus lands on what changed."""

    def __init__(self, parent, offer):
        super().__init__(parent, title=offer["title"])
        sizer = wx.BoxSizer(wx.VERTICAL)
        _paragraphs(self, sizer, offer["summary"])
        sizer.Add(wx.StaticText(self, label="What changed"), 0, wx.LEFT | wx.TOP, 12)
        notes = wx.TextCtrl(self, value=offer["notes"], size=(460, 200),
                            style=wx.TE_MULTILINE | wx.TE_READONLY)
        sizer.Add(notes, 0, wx.ALL, 12)
        _paragraphs(self, sizer, offer["after"])
        row = wx.BoxSizer(wx.HORIZONTAL)
        for button_id, label in ((INSTALL, "Install now"), (SKIP, "Skip this version"),
                                 (LATER, "Later")):
            row.Add(wx.Button(self, button_id, label), 0, wx.ALL, 5)
        sizer.Add(row, 0, wx.ALIGN_RIGHT | wx.ALL, 7)
        self.SetSizerAndFit(sizer)
        self.Bind(wx.EVT_BUTTON, lambda event: self.EndModal(event.GetId()))
        self.SetEscapeId(LATER)
        self.CentreOnParent()


def decide(parent, offer, say):
    """Offer the update, and act on the answer. `say` speaks a reply on the window."""
    dialog = DecisionDialog(parent, offer)
    with dialog:
        answer = dialog.ShowModal()
    if answer == SKIP:
        call(parent, app_updates.skip, offer["version"],
             done=lambda reply: say(reply.get("message") or "Done."))
    elif answer == INSTALL:
        install(parent, offer, say)


def install(parent, offer, say):
    """Download, check and hand over, in a dialog that can be stopped until the installer is
    about to start."""
    follower = _Follower()
    job = JobDialog(parent, "Updating AccessCaster", offer["downloading"], follower.begin,
                    stop=app_updates.cancel, stop_label="Cancel")
    follower.version = offer["version"]
    with job:
        result = job.run()
    if result is None:
        return
    if result.get("cancelled"):
        say(result["message"])
        return
    report(parent, ("Update", "Update failed"), result)


class _Follower:
    """Drives the progress dialog: starts the download, follows its stages, and hands over."""

    def __init__(self):
        self.version = ""
        self.stage = None
        self.not_back = ""

    def begin(self, dialog):
        self.dialog = dialog
        call(dialog, app_updates.install, self.version, done=self._started)

    def _started(self, reply):
        if not reply.get("ok"):
            self.dialog.finish({"ok": False,
                                "message": reply.get("message") or "The update could not start."})
            return
        self.dialog.poll(app_updates.job, self._stage, every_ms=POLL_MS)

    def _stage(self, reply):
        job = reply.get("job")
        if not job or job["stage"] == self.stage:
            return
        self.stage = job["stage"]
        self.not_back = job.get("not_back") or ""
        if self.stage == app_updates.CHECKING:
            self.dialog.say(job["message"])
        elif self.stage == app_updates.READY:
            # Nothing can stop it from here: the installer is about to close this app.
            self.dialog.hold()
            self.dialog.say(job["message"])
            wx.CallLater(reading_ms(job["message"]), self._proceed)
        elif self.stage == app_updates.FAILED:
            self.dialog.finish({"ok": False, "message": job["message"]})
        elif self.stage == app_updates.CANCELLED:
            self.dialog.finish({"ok": True, "cancelled": True, "message": job["message"]})

    def _proceed(self):
        call(self.dialog, app_updates.proceed, done=self._handed_over)

    def _handed_over(self, reply):
        if not reply.get("ok"):
            job = reply.get("job") or {}
            self.dialog.finish({"ok": False,
                                "message": job.get("message") or "The update could not start."})
            return
        # The installer closes this app; still here after this long, the install did not take.
        wx.CallLater(BACK_WITHIN_MS, lambda: self.dialog and self.dialog.finish(
            {"ok": False, "message": self.not_back}))
