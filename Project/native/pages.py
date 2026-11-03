"""The pieces every section is made of.

A SECTION is one branch of the tree. It has a `title`, a `route` (its name for `frame.go_to`), a
`pages` list, and optionally `start()` (called once the service is up), `refresh(announce)` (F5)
and `read()` (after a job that rewrites the desk). When its pages change it calls
`frame.pages_changed(self)`, and the frame rebuilds the branch.

A PAGE is one leaf: a `key` that survives a rebuild, a `title` for the tree, and the `window` shown
on the right. Every page window is a child of `frame.host`. A window with `before_show()` has it
called while still hidden, each time the tree is about to show it.
"""
import wx

# When the focus lands somewhere (a menu or dialog closing, the window coming to the front), the
# screen reader first reads where it landed; a line said before then is cut off by it.
AFTER_FOCUS_MS = 250


class Speaker:
    """`reply` for a window that has `say(text, recent)`: the main window, and a dialog with speech
    of its own (`dialogs.SpeakingDialog`)."""

    def reply(self, source, reply, recent=False):
        """Speak an action's reply to a change made from `source`. A refusal is always spoken. A
        success only while `source` still has the focus: once the user has moved on, it would cut
        off the screen reader reading where they are now."""
        if not reply.get("ok"):
            self.say(reply.get("message") or "That did not work.", recent)
        elif wx.Window.FindFocus() is source:
            self.say(reply.get("message") or "Done.", recent)


class Page:
    """A page in the window's tree: `window`, or `build`, which makes it HIDDEN the first time the
    window is about to show it. Building every channel's page at once holds the window's thread
    for about two seconds, and nothing answers the screen reader until it is done."""

    def __init__(self, key, title, window=None, build=None):
        self.key = key
        self.title = title
        self._window = window
        self._build = build

    @property
    def built(self):
        return self._window is not None

    @property
    def window(self):
        if self._window is None:
            self._window = self._build()
        return self._window


def page_body(panel, grow=False):
    """The page's layout. Returns (sizer, parent): controls go in the sizer and are created as
    children of `parent`. `grow` makes the body fill the page's height, for a page whose content
    stretches (tabs). No group box: the tree has just named the page, so a group named the same
    repeats it on the way in. A control that moves the focus here (`MainFrame.go_to`) names the
    page in its own label instead."""
    outer = wx.BoxSizer(wx.VERTICAL)
    panel.SetSizer(outer)
    body = wx.BoxSizer(wx.VERTICAL)
    outer.Add(body, 1 if grow else 0, wx.EXPAND | wx.ALL, 4)
    return body, panel


def group(parent, sizer, title, proportion=0):
    """A named group box inside `sizer`, which a screen reader announces on the way into it.
    Returns (sizer, parent) as `page_body` does."""
    box = wx.StaticBoxSizer(wx.VERTICAL, parent, title)
    sizer.Add(box, proportion, wx.EXPAND | wx.ALL, 4)
    return box, box.GetStaticBox()


def labelled(parent, sizer, label, control_factory):
    """A static label then its control on one line. The label is created FIRST because that is what
    names the control: Windows takes a control's name from the static text just before it."""
    line = wx.BoxSizer(wx.HORIZONTAL)
    line.Add(wx.StaticText(parent, label=label), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
    control = control_factory(parent)
    line.Add(control, 0)
    sizer.Add(line, 0, wx.ALL, 6)
    return control


def read_only(parent, sizer, label, height=60, width=460, scroll=False):
    """A labelled read-only text box, for a reading: it takes the focus, so it is met in the tab
    order and read with its label, which static text is not. `height` None is one line; `scroll`
    gives a long report a scroll bar.

    Always multi-line, even for one line: wx makes a text box a tab stop only while it is editable
    or multi-line, so a single-line read-only box is never reached by Tab."""
    style = wx.TE_MULTILINE | wx.TE_READONLY | (0 if scroll else wx.TE_NO_VSCROLL)

    def make(p):
        box = wx.TextCtrl(p, size=(width, -1 if height is None else height), style=style)
        if height is None:
            box.SetInitialSize((width, box.GetSizeFromTextSize(box.GetTextExtent("Ag")).height))
        return box
    return labelled(parent, sizer, label, make)


def set_text(box, text):
    """Replace a text box's value only when it differs: a screen reader reads a focused box again
    whenever its value is set."""
    if box.GetValue() != text:
        box.SetValue(text)


def set_items(listbox, labels):
    """Put `labels` in a list box item by item, so an unchanged list is not replaced under the
    screen reader."""
    if listbox.GetCount() == len(labels):
        for index, label in enumerate(labels):
            if listbox.GetString(index) != label:
                listbox.SetString(index, label)
    else:
        listbox.Set(labels)


class MessagePage(wx.Panel):
    """A page that is only a sentence and, optionally, one button: a section that is still reading
    or could not read. The sentence is a read-only text box named Status rather than static text,
    so that it takes focus and is read when F6 moves into the page."""

    def __init__(self, host, text, button=None, on_button=None):
        super().__init__(host)
        sizer, parent = page_body(self)
        self.text = read_only(parent, sizer, "Status", height=80)
        self.text.SetValue(text)
        self.button = None
        if button:
            self.button = wx.Button(parent, label=button)
            self.button.Bind(wx.EVT_BUTTON, lambda _e: on_button())
            sizer.Add(self.button, 0, wx.ALL, 6)

    def set_text(self, text):
        self.text.SetValue(text)
