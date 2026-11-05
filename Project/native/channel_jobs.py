"""The two channel jobs that listen to a microphone: auto gain and the level check. The measuring
is `autogain_actions` and `level_check`; this is their buttons and dialogs.

Nothing is spoken while either listens beyond what the user needs to act on: the microphone being
measured hears whatever the computer plays.
"""
import wx

import autogain_actions
import level_check
from native import a11y
from native.calls import call
from native.dialogs import JobDialog, TextDialog


def countdown_words(left):
    """The take's countdown, coarse on purpose: a line every second would still be being read
    when the next was due."""
    if left is None or left > 10:
        return None
    if left > 5:
        return "10 seconds left."
    if left > 0:
        return "Nearly done."
    return None


class AutoGain:
    """One run, from its question to its result. `source` is the button that started it, which
    keeps the focus once the dialogs close."""

    def __init__(self, page, source):
        self.page = page
        self.source = source
        self.label = page.rec["label"]
        self._phase = None
        self._count = None

    def run(self):
        question = TextDialog(
            self.page, f"Set {self.label}'s gain automatically?",
            "You will be asked to talk normally, as you would on air, for "
            f"{int(autogain_actions.SECONDS)} seconds, then for "
            f"{int(autogain_actions.VERIFY_S)} seconds more so it can check its work. Each take "
            "starts at a tone.\n\n"
            f"This switches {self.label} processing off while it listens and puts it back "
            "afterwards, sets the Windows recording level to no change, and rewrites the gain. The "
            "gain can be undone at the end.\n\n"
            "While it runs, press Escape or Enter to cancel at any time. The gain is then left as "
            "it was.",
            [(wx.ID_OK, "Start"), (wx.ID_CANCEL, "Cancel")],
            default=wx.ID_CANCEL, escape=wx.ID_CANCEL)
        with question:
            if question.ShowModal() != wx.ID_OK:
                return
        job = JobDialog(self.page, "Setting the gain", "Getting ready. Do not talk yet.",
                        self._begin, autogain_actions.action_autogain_stop, stop_label="Cancel",
                        ticks=False)
        with job:
            st = job.run() or {}
        if not st.get("ok"):
            self.page.frame.say(st.get("message") or "Auto gain could not start.")
            return
        self._result(st)

    def _begin(self, job):
        def started(reply):
            if reply.get("ok"):
                job.poll(autogain_actions.status, lambda st: self._status(job, st))
            else:
                job.finish(reply)
        call(job, autogain_actions.action_autogain_start, self.page.name, done=started)

    def _status(self, job, st):
        phase = st.get("phase")
        if phase != self._phase:
            self._phase = phase
            self._count = None
        # Two talking phases, the take and the shorter check after it. Each one's instruction is
        # said in the "ready" phase before its start tone, so a take only counts down.
        if phase in ("listening", "verifying"):
            words = countdown_words(st.get("seconds_left"))
            if words and words != self._count:
                self._count = words
                job.say(words)
            return
        if st.get("done"):
            job.finish(st)
            return
        if phase in ("starting", "preparing", "ready", "working"):
            job.say(st.get("message") or "Working.")

    def _result(self, st):
        failed = st.get("phase") == "failed"
        changed = not failed and st.get("gain_after") != st.get("gain_before")
        buttons = ([(wx.ID_UNDO, "Undo")] if changed else []) + [(wx.ID_OK, "OK")]
        result = TextDialog(self.page, "Gain not changed" if failed else "Gain set",
                            st.get("message") or "", buttons, default=wx.ID_OK,
                            escape=wx.ID_OK)
        with result:
            chosen = result.ShowModal()
        section = self.page.section
        if chosen != wx.ID_UNDO:
            # The gain and the processing switch both moved and were put back: read the channel
            # again.
            section.refresh(False)
            return

        def undone(reply):
            self.page.frame.reply(self.source, reply)
            section.refresh(False)
        call(self.source, autogain_actions.action_autogain_undo, done=undone)


class LevelCheck:
    """The level check. One run at a time across every channel: `level_check` toggles, so any
    channel's button stops a run, and its summary names the channel it measured.

    A button's label says whether a run is going, but changes only while that button does not have
    the focus: a screen reader reads a focused button's new name aloud, over the levels the check
    is speaking. The start is a tone, the end is the summary."""

    START = "Check this channel's level"
    STOP = "Stop checking"
    DESCRIPTION = ("Talk, and it speaks this channel's level as you go, then the loudest it got. "
                   "The fader position is taken out of the reading. Press again to stop early.")

    def __init__(self, frame):
        self.frame = frame
        self.running = False
        self._buttons = []
        self._ends = None

    def button(self, parent, sizer, name):
        button = wx.Button(parent, label=self.START)
        button.SetMinSize(button.GetBestSize())
        self._label(button)
        a11y.describe(button, self.DESCRIPTION)
        button.Bind(wx.EVT_BUTTON, lambda _e: self._press(button, name))
        button.Bind(wx.EVT_KILL_FOCUS, self._on_leave)
        sizer.Add(button, 0, wx.ALL, 6)
        self._buttons = [b for b in self._buttons if b] + [button]
        return button

    def _press(self, button, name):
        call(button, level_check.action_channel_level_check, name, done=self._returned)

    def _returned(self, reply):
        self.running = bool(reply.get("running"))
        if self._ends is not None:
            self._ends.Stop()
            self._ends = None
        if self.running:
            # Only a Windows mute or level the check had to clear: the start itself is a tone.
            if reply.get("message"):
                self.frame.say(reply["message"])
            # `level_check` ends the run on its own timer and speaks the summary itself.
            self._ends = wx.CallLater(int(((reply.get("seconds") or 30) + 1) * 1000),
                                      self._ended)
        else:
            # Stopped early, or refused: either way nothing is speaking levels now.
            self.frame.say(reply.get("message") or "Done.")
        self._relabel()

    def _ended(self):
        self._ends = None
        self.running = False
        self._relabel()

    def _relabel(self):
        focus = wx.Window.FindFocus()
        for button in self._buttons:
            if button and button is not focus:
                self._label(button)

    def _on_leave(self, event):
        self._label(event.GetEventObject())
        event.Skip()

    def _label(self, button):
        want = self.STOP if self.running else self.START
        if button.GetLabel() != want:
            button.SetLabel(want)
