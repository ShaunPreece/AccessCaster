"""Dialogs for the desk's longer jobs: the question before one, the job running, and what it came
to, each its own dialog.

A dialog activating focuses its first focusable control, and that one focus event is what the
screen reader reads. So the control to land on is created first and nothing is focused from a
constructor: a second SetFocus is a second focus event, and the control is read twice.

A screen reader reads a dialog's text when the dialog opens and not when it changes, so a stage the
user has to hear in full (a question, a result) opens a dialog of its own. While a job runs, its
stages are spoken through UIA notifications raised from the job's dialog (`JobDialog.say`).
"""
import wx

import speech
from native import announce
from native.calls import call
from native.pages import Speaker

_WRAP_DIP = 440


class SpeakingDialog(Speaker, wx.Dialog):
    """A dialog that speaks through notifications raised from a window inside it, so what it says
    comes from the dialog the user is in. It is its own `frame`, so the controls in
    `native.controls`, which speak through `page.frame`, work in it unchanged. A subclass adds
    `self.sink` to its sizer."""

    def __init__(self, parent, title):
        super().__init__(parent, title=title)
        self.frame = self
        # Never shown: the window the notifications are raised from. Nameless, because a screen
        # reader reads it as part of the dialog's text, and with nothing else to focus, takes it as
        # the focus.
        self.sink = wx.StaticText(self, label="", size=(1, 1))
        self._notifier = announce.Notifier(self.sink.GetHandle(), "")
        self.sink.Bind(wx.EVT_WINDOW_DESTROY, self._on_sink_destroyed)

    def say(self, text, recent=False):
        self._notifier.say(text, announce.MOST_RECENT if recent else announce.ALL)

    def _on_sink_destroyed(self, event):
        self._notifier.close()
        event.Skip()


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
    order; `ShowModal` returns the id pressed. `default` is first in the tab order, so the focus
    lands on it, and takes Enter; Escape and the title bar's close answer with `escape`."""

    def __init__(self, parent, title, text, buttons, default, escape):
        super().__init__(parent, title=title)
        sizer = wx.BoxSizer(wx.VERTICAL)
        _paragraphs(self, sizer, text)
        row = wx.BoxSizer(wx.HORIZONTAL)
        made = [wx.Button(self, button_id, label) for button_id, label in buttons]
        for button in made:
            row.Add(button, 0, wx.ALL, 5)
        sizer.Add(row, 0, wx.ALIGN_RIGHT | wx.ALL, 7)
        self.SetSizerAndFit(sizer)
        # Every button ends the dialog with its own id, including ones wx has no default for.
        self.Bind(wx.EVT_BUTTON, lambda event: self.EndModal(event.GetId()))
        self.SetEscapeId(escape)
        chosen = self.FindWindow(default)
        chosen.SetDefault()
        if chosen is not made[0]:
            chosen.MoveBeforeInTabOrder(made[0])
        self.CentreOnParent()


class JobDialog(SpeakingDialog):
    """A job running on the desk: what it is doing now, and a Stop button.

    `begin(dialog)` runs once the dialog is up and starts the job; from then on the caller drives
    `say`, `poll` and `finish`. `stop()` runs, off the UI thread, on Stop, Escape or the title bar's
    close: it asks the job to end, and the dialog stays until the job reports that it has. A job
    that cannot be stopped passes `stop=None`: its button is Close, which ends the dialog with
    `result` None and leaves the job running. With `closable=False` the button is Please wait, and
    it, Escape and the title bar's close only say to wait: for a job whose result decides what the
    user does next.

    While it runs, a soft clock ticks (`speech.start_loop`), from `TICK_AFTER_S` so a quick job
    makes no sound. A job that measures a microphone passes `ticks=False`, and so has no progress
    bar either: NVDA beeps a progress bar's changes."""

    WAIT = "Please wait until this finishes."
    # Long enough for the screen reader to read the dialog first.
    TICK_AFTER_S = 1.5

    def __init__(self, parent, title, text, begin, stop, stop_label=None, closable=True,
                 ticks=True):
        super().__init__(parent, title)
        self._ticks = ticks
        self._begin = begin
        self._stop = stop
        self._stopping = False
        self._finished = False
        self.result = None
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.status = _paragraphs(self, sizer, text)[0]
        if closable:
            button = wx.Button(self, wx.ID_CANCEL, stop_label or ("Stop" if stop else "Close"))
            # Bound on the dialog and not skipped, so the cancel id stops the job instead of
            # ending the modal. The title bar's close arrives as the same button.
            self.Bind(wx.EVT_BUTTON, self._on_stop, id=wx.ID_CANCEL)
            self.SetEscapeId(wx.ID_CANCEL)
        else:
            # A button that only says to wait: a dialog needs a control for the focus to land on,
            # or the screen reader finds one of its own.
            button = wx.Button(self, label="Please wait")
            button.Bind(wx.EVT_BUTTON, lambda _e: SpeakingDialog.say(self, self.WAIT))
            self.SetEscapeId(wx.ID_NONE)
            self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
            self.Bind(wx.EVT_CLOSE, self._on_close)
        sizer.Add(button, 0, wx.ALIGN_RIGHT | wx.ALL, 12)
        sizer.Add(self.sink, 0)
        self.SetSizerAndFit(sizer)
        self.CentreOnParent()

    def run(self):
        """Show the dialog until `finish`, and return what was passed to it."""
        wx.CallAfter(self._begin, self)
        loop = speech.start_loop("working", self.TICK_AFTER_S) if self._ticks else None
        try:
            self.ShowModal()
        finally:
            if loop is not None:
                loop.stop()
        return self.result

    def say(self, text, recent=False):
        """Show `text` as the job's current stage and speak it. A stage already showing is not
        spoken again."""
        if not self or text == self.status.GetLabel():
            return
        self.status.SetLabel(text)
        self.status.Wrap(self.FromDIP(_WRAP_DIP))
        self.Fit()
        super().say(text, recent)

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

    def _on_key(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            SpeakingDialog.say(self, self.WAIT)
            return
        event.Skip()

    def _on_close(self, event):
        if event.CanVeto() and not self._finished:
            event.Veto()
            SpeakingDialog.say(self, self.WAIT)
            return
        event.Skip()

    def _on_stop(self, _event):
        if self._stopping or self._finished:
            return
        if self._stop is None:
            self.finish(None)
            return
        self._stopping = True
        self.say("Stopping.")
        call(self, self._stop)


class ListDialog(wx.Dialog):
    """Choose one of `names` from a list of its own, then `ok_label` or Cancel. `labels`, if given,
    is what the list shows for each name; `note` is a sentence under the list."""

    def __init__(self, parent, title, label, names, ok_label, labels=None, note=""):
        super().__init__(parent, title=title)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(self, label=label), 0, wx.LEFT | wx.TOP, 10)
        self.names = names
        self.list = wx.ListBox(self, choices=labels or names, size=(360, 120))
        self.list.SetSelection(0)
        sizer.Add(self.list, 0, wx.ALL, 10)
        if note:
            _paragraphs(self, sizer, note)
        buttons = wx.StdDialogButtonSizer()
        buttons.AddButton(wx.Button(self, wx.ID_OK, label=ok_label))
        buttons.AddButton(wx.Button(self, wx.ID_CANCEL, label="Cancel"))
        buttons.Realize()
        sizer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 10)
        self.SetSizerAndFit(sizer)

    def chosen(self):
        return self.names[self.list.GetSelection()]


class DeletePresetDialog(ListDialog):
    """Its own list of the user's presets, so what is deleted is never whatever a page's preset
    list happens to show."""

    def __init__(self, parent, names, labels=None):
        super().__init__(parent, "Delete a preset", "Preset to delete", names, "Delete", labels,
                         "Deleting a preset cannot be undone.")


def confirm(parent, title, text, action):
    """Ask before a change that wipes something: True if `action` was chosen. Cancel is the
    default, so Enter on arrival changes nothing."""
    dialog = TextDialog(parent, title, text, [(wx.ID_OK, action), (wx.ID_CANCEL, "Cancel")],
                        default=wx.ID_CANCEL, escape=wx.ID_CANCEL)
    with dialog:
        return dialog.ShowModal() == wx.ID_OK
