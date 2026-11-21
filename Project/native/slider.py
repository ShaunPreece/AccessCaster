"""A horizontal slider drawn by the app, in place of the native one (wx.Slider).

The native Windows slider (msctls_trackbar32) is read by JAWS from its own position, as a percent
of its range, whatever value text the app gives it, so "-20 decibels" is heard as "67 percent".
This control is not of that class, so every screen reader reads the value text it is given
(`a11y.slider`).

It offers the part of wx.Slider's interface the app uses (GetValue, SetValue, SetLineSize,
SetPageSize, GetMin, GetMax) and posts wx.EVT_SLIDER when the user moves it, never when the app
calls SetValue. Keys follow the ARIA slider pattern: Right and Up raise it, Left and Down lower it,
Page Up and Page Down move a page, Home and End go to the ends.
"""
import wx

from native import a11y


class SliderCtrl(wx.Control):
    def __init__(self, parent, label, lo, hi, value_words, size=(260, -1)):
        # WANTS_CHARS so the arrow keys reach the control rather than moving the focus, which
        # makes the control responsible for Tab, Ctrl+Tab and Enter (`_on_key`).
        super().__init__(parent, style=wx.WANTS_CHARS | wx.BORDER_NONE)
        self.SetLabel(label)
        self._lo, self._hi = lo, hi
        self._value = lo
        self._line = 1
        self._page = max(1, (hi - lo) // 10)
        self.SetInitialSize(wx.Size(self.FromDIP(size[0]), self.FromDIP(28)))
        self.acc = a11y.slider(self, value_words)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_KEY_DOWN, self._on_key)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_mouse_down)
        self.Bind(wx.EVT_MOTION, self._on_mouse_move)
        self.Bind(wx.EVT_LEFT_UP, self._on_mouse_up)
        self.Bind(wx.EVT_MOUSE_CAPTURE_LOST, lambda _e: None)
        self.Bind(wx.EVT_SET_FOCUS, self._on_focus)
        self.Bind(wx.EVT_KILL_FOCUS, self._on_focus)

    def AcceptsFocus(self):
        return True

    def GetValue(self):
        return self._value

    def SetValue(self, value):
        self._set(value, by_user=False)

    def GetMin(self):
        return self._lo

    def GetMax(self):
        return self._hi

    def SetLineSize(self, size):
        self._line = max(1, int(size))

    def SetPageSize(self, size):
        self._page = max(1, int(size))

    def _set(self, value, by_user):
        value = max(self._lo, min(self._hi, int(value)))
        if value == self._value:
            return
        self._value = value
        self.Refresh()
        a11y.value_changed(self)
        if by_user:
            event = wx.CommandEvent(wx.wxEVT_SLIDER, self.GetId())
            event.SetEventObject(self)
            event.SetInt(value)
            self.GetEventHandler().ProcessEvent(event)

    def _on_key(self, event):
        key = event.GetKeyCode()
        plain = not (event.ControlDown() or event.AltDown() or event.ShiftDown()
                     or event.MetaDown())
        steps = {wx.WXK_RIGHT: self._line, wx.WXK_UP: self._line,
                 wx.WXK_LEFT: -self._line, wx.WXK_DOWN: -self._line,
                 wx.WXK_PAGEUP: self._page, wx.WXK_PAGEDOWN: -self._page}
        if plain and key in steps:
            self._set(self._value + steps[key], by_user=True)
        elif plain and key == wx.WXK_HOME:
            self._set(self._lo, by_user=True)
        elif plain and key == wx.WXK_END:
            self._set(self._hi, by_user=True)
        elif key == wx.WXK_TAB and not event.AltDown():
            flags = (wx.NavigationKeyEvent.IsBackward if event.ShiftDown()
                     else wx.NavigationKeyEvent.IsForward)
            if event.ControlDown():
                flags |= wx.NavigationKeyEvent.WinChange
            self.Navigate(flags)
        elif key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and plain:
            self._press_default()
        else:
            event.Skip()

    def _press_default(self):
        """Enter presses the window's default button, as it does from a native slider."""
        top = wx.GetTopLevelParent(self)
        button = top.GetDefaultItem() if top else None
        if isinstance(button, wx.Button) and button.IsShownOnScreen() and button.IsEnabled():
            event = wx.CommandEvent(wx.wxEVT_BUTTON, button.GetId())
            event.SetEventObject(button)
            button.GetEventHandler().ProcessEvent(event)

    def _track(self):
        """The track's left end and width, in pixels."""
        margin = self.FromDIP(8)
        return margin, max(1, self.GetClientSize().width - 2 * margin)

    def _value_at(self, x):
        left, width = self._track()
        fraction = max(0.0, min(1.0, (x - left) / width))
        return round(self._lo + fraction * (self._hi - self._lo))

    def _on_mouse_down(self, event):
        self.SetFocus()
        if not self.HasCapture():
            self.CaptureMouse()
        self._set(self._value_at(event.GetX()), by_user=True)

    def _on_mouse_move(self, event):
        if event.Dragging() and event.LeftIsDown() and self.HasCapture():
            self._set(self._value_at(event.GetX()), by_user=True)

    def _on_mouse_up(self, _event):
        if self.HasCapture():
            self.ReleaseMouse()

    def _on_focus(self, event):
        self.Refresh()
        event.Skip()

    def _on_paint(self, _event):
        dc = wx.PaintDC(self)
        colour = wx.SystemSettings.GetColour
        dc.SetBackground(wx.Brush(self.GetParent().GetBackgroundColour()))
        dc.Clear()
        left, width = self._track()
        height = self.GetClientSize().height
        middle = height // 2
        text = colour(wx.SYS_COLOUR_WINDOWTEXT if self.IsEnabled() else wx.SYS_COLOUR_GRAYTEXT)
        dc.SetPen(wx.Pen(text, self.FromDIP(2)))
        dc.DrawLine(left, middle, left + width, middle)
        span = max(1, self._hi - self._lo)
        x = left + round(width * (self._value - self._lo) / span)
        thumb = self.FromDIP(5)
        fill = colour(wx.SYS_COLOUR_HIGHLIGHT if self.HasFocus() else wx.SYS_COLOUR_BTNFACE)
        dc.SetPen(wx.Pen(text, 1))
        dc.SetBrush(wx.Brush(fill))
        dc.DrawRectangle(x - thumb, self.FromDIP(4), 2 * thumb, height - self.FromDIP(8))
        if self.HasFocus():
            wx.RendererNative.Get().DrawFocusRect(self, dc, wx.Rect(0, 0, *self.GetClientSize()))
