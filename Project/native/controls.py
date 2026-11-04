"""Controls that write the desk: a switch, a slider, a list and a number box, each holding its own
write path.

Each takes `current()`, what the desk last reported (None when it did not report), and
`remember(value)`, called when a write is accepted, so the caller's record stays the desk's state.
`accepted(reply)`, if given, is called after it with the accepted write's reply, for a write whose
reply carries more than the value.
A refusal is spoken and puts the control back to `current()`. A success is spoken only while the
control still has the focus (`frame.reply`).
"""
import time

import wx

from native import a11y
from native.calls import LatestWins, call
from native.pages import labelled


class Switch:
    """A checkbox. `send(on)` runs on a worker thread and returns an action's reply, which is
    spoken. `guard()`, if given, returns a sentence to refuse with before anything is sent.
    `working(on)`, if given, returns a sentence spoken as the write goes out, for one that takes
    seconds."""

    def __init__(self, page, parent, sizer, label, send, current, remember, description="",
                 guard=None, accepted=None, working=None):
        self.page = page
        self._working = working
        self._accepted = accepted
        self.box = wx.CheckBox(parent, label=label)
        self.acc = a11y.describe(self.box, description)
        self._send = send
        self._current = current
        self._remember = remember
        self._guard = guard
        self.box.Bind(wx.EVT_CHECKBOX, self._on_box)
        sizer.Add(self.box, 0, wx.ALL, 6)

    def show(self):
        on = self._current() is True
        if self.box.GetValue() != on:
            self.box.SetValue(on)

    def _on_box(self, _event):
        on = self.box.GetValue()
        refusal = self._guard() if self._guard else None
        if refusal:
            self.box.SetValue(not on)
            self.page.frame.say(refusal)
            return
        if self._working:
            self.page.frame.say(self._working(on))
        call(self.page, self._send, on, done=lambda reply: self._returned(on, reply))

    def _returned(self, on, reply):
        self.page.frame.reply(self.box, reply)
        if reply.get("ok"):
            self._remember(on)
            if self._accepted:
                self._accepted(reply)
        else:
            self.show()


class Slider:
    """A labelled slider from `lo` to `hi`. `words(value)` is its value as the screen reader reads
    it. `send(value)` goes through `LatestWins`; the reply to the last write is spoken once the
    slider has been still, unless `quiet`, when only a refusal is spoken. A value the desk did not
    report shows at `unread` and reads "not read yet" until it is moved."""

    HANDS_OFF_S = 2.0      # longer than LatestWins' settle, so the last write has returned
    # While a slider keeps moving (an arrow key held), its newest value still goes out this often.
    # Only sliders: a list sending every item arrowed past would apply each one.
    MAX_WAIT_MS = 300
    # A value the desk pushes onto the FOCUSED slider waits until the pushes have stopped this long,
    # then lands once: the screen reader reads every value set on it, so an encoder turned from 55
    # to 70 would otherwise be counted out step by step, long after the knob has stopped.
    FOCUSED_SETTLE_MS = 400

    def __init__(self, page, parent, sizer, label, lo, hi, send, current, remember, words,
                 unread=None, quiet=False, accepted=None):
        self.page = page
        self._accepted = accepted
        self.slider = labelled(parent, sizer, label, lambda p: wx.Slider(
            p, minValue=lo, maxValue=hi, size=(260, -1)))
        self._current = current
        self._remember = remember
        self._words = words
        self._unread = lo if unread is None else unread
        self._quiet = quiet
        self.acc = a11y.describe(self.slider, value=self._value_words)
        self._writer = LatestWins(page, send, self._done, settled=self._settled,
                                  max_wait_ms=self.MAX_WAIT_MS)
        self._moved_at = 0.0
        self._landing = None
        self.slider.Bind(wx.EVT_SLIDER, self._on_slider)

    def show(self):
        # Left alone while it is being moved: the desk pushes back each value written, and an
        # older one arriving would pull the slider back under the user's hand.
        if time.monotonic() - self._moved_at < self.HANDS_OFF_S:
            return
        if wx.Window.FindFocus() is self.slider:
            if self._landing is not None and self._landing.IsRunning():
                self._landing.Start(self.FOCUSED_SETTLE_MS)
            else:
                self._landing = wx.CallLater(self.FOCUSED_SETTLE_MS, self._land)
            return
        self._land()

    def _land(self):
        if not self.slider or time.monotonic() - self._moved_at < self.HANDS_OFF_S:
            return
        value = self._current()
        value = self._unread if value is None else value
        if self.slider.GetValue() != value:
            self.slider.SetValue(value)

    def set_shown(self, shown):
        """Show or hide the slider with its label."""
        self.slider.GetContainingSizer().ShowItems(shown)

    def set_label(self, label):
        """Rename the slider, through the label that names it."""
        text = self.slider.GetContainingSizer().GetItem(0).GetWindow()
        if text.GetLabel() != label:
            text.SetLabel(label)

    def _on_slider(self, _event):
        self._moved_at = time.monotonic()
        self._writer.change(self.slider.GetValue())

    def _value_words(self):
        value = self.slider.GetValue()
        if self._current() is None and value == self._unread:
            return "not read yet"
        return self._words(value)

    def _done(self, value, reply):
        if reply.get("ok"):
            self._remember(value)
            if self._accepted:
                self._accepted(reply)
            return
        self.page.frame.say(reply.get("message") or "Could not make that change.")
        if self._current() is not None:
            self.slider.SetValue(self._current())

    def _settled(self, _value, reply):
        if reply.get("ok") and not self._quiet:
            self.page.frame.reply(self.slider, reply, recent=True)


class _Field:
    """What `Choice` and `Number` share. Arrowing either fires a change per step, so the writes go
    through `LatestWins`, which sends only the newest value once the control has been still for
    `DELAY_MS`; the reply is spoken once it has settled. `guard()` refuses before anything is sent."""

    DELAY_MS = 400     # longer than the gap between arrow presses, so arrowing through writes once

    def __init__(self, page, control, send, current, remember, description, guard, accepted):
        self.page = page
        self.control = control
        self._current = current
        self._remember = remember
        self._guard = guard
        self._accepted = accepted
        self.acc = a11y.describe(control, description)
        self._writer = LatestWins(page, send, self._done, settled=self._settled,
                                  delay_ms=self.DELAY_MS)

    def show(self):
        self._put(self._current())

    def _on_change(self, _event):
        refusal = self._guard() if self._guard else None
        if refusal:
            self.show()
            self.page.frame.say(refusal)
            return
        self._writer.change(self._get())

    def _done(self, value, reply):
        if reply.get("ok"):
            self._remember(value)
            if self._accepted:
                self._accepted(reply)
            return
        self.page.frame.say(reply.get("message") or "Could not make that change.")
        self.show()

    def _settled(self, _value, reply):
        if reply.get("ok"):
            self.page.frame.reply(self.control, reply, recent=True)


class Choice(_Field):
    """A list whose choice writes, for a value that replaces nothing (a pad's colour). A value
    whose write replaces something is applied by a button instead (PROJECT_GUIDE part 2). `items`
    is (value, label) pairs."""

    def __init__(self, page, parent, sizer, label, items, send, current, remember,
                 description="", guard=None, accepted=None):
        self._values = [value for value, _label in items]
        choice = labelled(parent, sizer, label, lambda p: wx.Choice(p))
        set_choices(choice, [text for _value, text in items])
        super().__init__(page, choice, send, current, remember, description, guard, accepted)
        choice.Bind(wx.EVT_CHOICE, self._on_change)

    def set_items(self, items):
        """Replace the (value, label) pairs; `show` then puts the current value back on them."""
        self._values = [value for value, _label in items]
        set_choices(self.control, [text for _value, text in items])

    def _get(self):
        return self._values[self.control.GetSelection()]

    def _put(self, value):
        index = self._values.index(value) if value in self._values else wx.NOT_FOUND
        if self.control.GetSelection() != index:
            self.control.SetSelection(index)


class Number(_Field):
    """A number box whose value writes, for a number the user knows rather than feels their way to
    (a MIDI control number): typing takes the digits, and the arrows still step by one. A typed
    value is sent on Enter or on leaving the box."""

    def __init__(self, page, parent, sizer, label, lo, hi, send, current, remember,
                 description="", guard=None, accepted=None):
        spin = labelled(parent, sizer, label, lambda p: wx.SpinCtrl(
            p, min=lo, max=hi, size=(90, -1), style=wx.SP_ARROW_KEYS | wx.TE_PROCESS_ENTER))
        super().__init__(page, spin, send, current, remember, description, guard, accepted)
        spin.Bind(wx.EVT_SPINCTRL, self._on_change)

    def _get(self):
        return self.control.GetValue()

    def _put(self, value):
        if value is not None and self.control.GetValue() != value:
            self.control.SetValue(value)


def set_choices(choice, labels):
    """Replace a wx.Choice's items only when they differ: replacing them under the focus makes the
    screen reader read the list again."""
    if [choice.GetString(i) for i in range(choice.GetCount())] != labels:
        choice.Set(labels)
