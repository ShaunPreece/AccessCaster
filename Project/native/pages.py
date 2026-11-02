"""The pieces every section is made of.

A SECTION is one branch of the tree. It has a `title`, a `pages` list, and optionally `start()`
(called once the service is up) and `refresh(announce)` (F5). When its pages change it calls
`frame.pages_changed(self)`, and the frame rebuilds the branch.

A PAGE is one leaf: a `key` that survives a rebuild, a `title` for the tree, and the `window` shown
on the right. Every page window is a child of `frame.host`.
"""
import wx


class Page:
    def __init__(self, key, title, window):
        self.key = key
        self.title = title
        self.window = window


def page_box(panel, title, grow=False):
    """The page's group box, named with its title so a screen reader names the page on the way in.
    Returns (sizer, parent): controls go in the sizer and are created as children of `parent`.
    `grow` makes the box fill the page's height, for a page whose content stretches (tabs)."""
    outer = wx.BoxSizer(wx.VERTICAL)
    panel.SetSizer(outer)
    return group(panel, outer, title, 1 if grow else 0)


def group(parent, sizer, title, proportion=0):
    """A named group box inside `sizer`, which a screen reader announces on the way into it.
    Returns (sizer, parent) as `page_box` does."""
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


class MessagePage(wx.Panel):
    """A page that is only a sentence and, optionally, one button: a section that is still reading,
    could not read, or is not built natively yet. The sentence is a read-only text box rather than
    static text so that it takes focus and is read when F6 moves into the page."""

    def __init__(self, host, title, text, button=None, on_button=None):
        super().__init__(host)
        sizer, parent = page_box(self, title)
        self.text = wx.TextCtrl(parent, value=text, size=(460, 80),
                                style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_NO_VSCROLL)
        sizer.Add(self.text, 0, wx.ALL, 6)
        self.button = None
        if button:
            self.button = wx.Button(parent, label=button)
            self.button.Bind(wx.EVT_BUTTON, lambda _e: on_button())
            sizer.Add(self.button, 0, wx.ALL, 6)

    def set_text(self, text):
        self.text.SetValue(text)


class WebSection:
    """An area not built natively yet: one page whose button opens it in the web page."""

    def __init__(self, frame, title, route):
        self.title = title
        self.route = route
        window = MessagePage(frame.host, title,
                             f"{title} is not in this window yet. It opens in the web page.",
                             button=f"Open {title} in the browser",
                             on_button=lambda: frame.open_web_page(route))
        self.pages = [Page(route, title, window)]
