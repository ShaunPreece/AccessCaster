"""Dialogs for the desk's longer jobs: the question before one, the job running, and what it came
to, each its own dialog.

A screen reader reads a dialog's text when the dialog opens and not when it changes, so a stage the
user has to hear in full (a question, a result) opens a dialog of its own. While a job runs, its
stages are spoken through UIA notifications raised from the job's dialog (`JobDialog.say`).
"""
import wx

from native import announce
from native.calls import call

_WRAP_DIP = 440


def _paragraphs(dialog, sizer, text):
    """One static text per paragraph, so a screen reader hears the breaks."""
    items = []
    for para in text.split("\n\n"):
        item = wx.StaticText(dialog, label=para)
        item.Wrap(dialog.FromDIP(_WRAP_DIP))
        sizer.Add(item, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)
        items.append(item)
    return items


class TextDialog(wx.Dialog):
    """Some text and a row of buttons, for a question or a result. `buttons` is (id, label) pairs in
    order; `ShowModal` returns the id pressed. `default` takes the focus and Enter; Escape and the
    title bar's close answer with `escape`."""

    def __init__(self, parent, title, text, buttons, default, escape):
        super().__init__(parent, title=title)
        sizer = wx.BoxSizer(wx.VERTICAL)
        _paragraphs(self, sizer, text)
        row = wx.BoxSizer(wx.HORIZONTAL)
        for button_id, label in buttons:
            row.Add(wx.Button(self, button_id, label), 0, wx.ALL, 5)
        sizer.Add(row, 0, wx.ALIGN_RIGHT | wx.ALL, 7)
        self.SetSizerAndFit(sizer)
        # Every button ends the dialog with its own id, including ones wx has no default for.
        self.Bind(wx.EVT_BUTTON, lambda event: self.EndModal(event.GetId()))
        self.SetEscapeId(escape)
        chosen = self.FindWindow(default)
        chosen.SetDefault()
        chosen.SetFocus()
        self.CentreOnParent()


class JobDialog(wx.Dialog):
    """A job running on the desk: what it is doing now, and a Stop button.

    `begin(dialog)` runs once the dialog is up and starts the job; from then on the caller drives
    `say`, `poll` and `finish`. `stop()` runs, off the UI thread, on Stop, Escape or the title bar's
    close: it asks the job to end, and the dialog stays until the job reports that it has.

    No progress bar: NVDA beeps a progress bar's changes, and a job that measures a microphone must
    make no sound of its own."""

    def __init__(self, parent, title, text, begin, stop, stop_label="Stop"):
        super().__init__(parent, title=title)
        self._begin = begin
        self._stop = stop
        self._stopping = False
        self._finished = False
        self.result = None
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.status = _paragraphs(self, sizer, text)[0]
        button = wx.Button(self, wx.ID_CANCEL, stop_label)
        sizer.Add(button, 0, wx.ALIGN_RIGHT | wx.ALL, 12)
        # Never shown: the window this dialog's notifications are raised from.
        sink = wx.StaticText(self, label="", size=(1, 1))
        sizer.Add(sink, 0)
        self._notifier = announce.Notifier(sink.GetHandle(), title)
        sink.Bind(wx.EVT_WINDOW_DESTROY, self._on_sink_destroyed)
        self.SetSizerAndFit(sizer)
        # Bound on the dialog and not skipped, so the cancel id stops the job instead of ending
        # the modal. The title bar's close arrives as the same button.
        self.Bind(wx.EVT_BUTTON, self._on_stop, id=wx.ID_CANCEL)
        self.SetEscapeId(wx.ID_CANCEL)
        button.SetFocus()
        self.CentreOnParent()

    def run(self):
        """Show the dialog until `finish`, and return what was passed to it."""
        wx.CallAfter(self._begin, self)
        self.ShowModal()
        return self.result

    def say(self, text):
        """Show `text` as the job's current stage and speak it. A stage already showing is not
        spoken again."""
        if not self or text == self.status.GetLabel():
            return
        self.status.SetLabel(text)
        self.status.Wrap(self.FromDIP(_WRAP_DIP))
        self.Fit()
        self._notifier.say(text)

    def poll(self, status, handle, every_ms=400):
        """Ask `status()` off the UI thread, hand its reply to `handle(reply)` on it, and ask again
        `every_ms` after each reply, until `finish`. One question at a time, so two replies never
        both finish the job."""
        def ask():
            if self and not self._finished:
                call(self, status, done=returned)

        def returned(reply):
            if self._finished:
                return
            handle(reply)
            if not self._finished:
                wx.CallLater(every_ms, ask)
        ask()

    def finish(self, result):
        """The job is over: close the dialog, with `result` as what `run` returns."""
        self._finished = True
        self.result = result
        if self.IsModal():
            self.EndModal(wx.ID_OK)

    def _on_stop(self, _event):
        if self._stopping or self._finished:
            return
        self._stopping = True
        self.say("Stopping.")
        call(self, self._stop)

    def _on_sink_destroyed(self, event):
        self._notifier.close()
        event.Skip()
