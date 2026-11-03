"""Help and About: the help, a page per topic, read from static/help.html, the file the web page
shows, so the two say the same.

The tree is the table of contents: About, What it can control, each part of Help (one per `h3`),
Known issues, and Support this project. A topic is plain text in a read-only box, a paragraph to a
line with a blank line between, which a screen reader moves through by line or by paragraph.
"""
import webbrowser
from html.parser import HTMLParser

import wx

from native.pages import Page, page_box, read_only
from paths import resource_path
from version import __version__

TITLE = "Help and About"
SUPPORT_URL = "https://www.slightperception.com/support"
# A paragraph that is a block of its own, and so a line of its own in the text.
BLOCKS = {"p", "li", "h1", "h2", "h3", "h4", "dd", "section", "div", "ul", "dl"}
# The tab whose `h3`s become topics of their own; the other tabs are one topic each.
SPLIT_TAB = "help"
# A <br>, kept apart from the source's own line breaks, which are whitespace.
BREAK = "\x00"
# Written for a page that has no support link set up; this one has.
SKIP_IDS = {"support-not-yet"}


class _HelpParser(HTMLParser):
    """help.html as topics: [(key, title, [paragraph, ...])]. Comments and hidden elements are
    left out, bar the tab panes, which are hidden only until the web page shows one."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.topics = []
        self._text = []
        self._term = ""              # a <dt> waiting for its <dd>
        self._skip = 0               # depth inside an element left out
        self._tab = None             # the key of the tab pane being read
        self._heading = None         # the tag whose text names the next topic, while it is open
        self._version = False

    def _topic(self, key, title):
        self.topics.append((key, title, []))

    def _flush(self):
        # Whitespace collapses as a browser would, except a <br>'s line break.
        lines = (" ".join(line.split()) for line in "".join(self._text).split(BREAK))
        text = "\n".join(line for line in lines if line)
        self._text = []
        if not text or not self.topics:
            return
        if self._term:
            text, self._term = f"{self._term}: {text}", ""
        self.topics[-1][2].append(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self._skip:
            if tag not in ("br",):
                self._skip += 1
            return
        if tag == "div" and "cat-tab" in (attrs.get("class") or ""):
            self._flush()
            self._tab = attrs.get("data-tab-key")
            self._topic(self._tab, attrs.get("data-tab-title") or self._tab)
            return
        if (("hidden" in attrs and tag != "section") or attrs.get("id") in SKIP_IDS
                or tag == "h1"):
            self._skip = 1
            return
        if tag == "section":
            self._topic("about", "About")
        if tag in BLOCKS or tag == "dt":
            self._flush()
        if tag == "h3" and self._tab == SPLIT_TAB:
            self._heading = tag
            self._topic(None, "")
        if tag == "br":
            self._text.append(BREAK)
        if attrs.get("id") == "app-version":
            self._version = True

    def handle_endtag(self, tag):
        if self._skip:
            self._skip -= 1
            return
        if tag == "dt":
            self._term = " ".join("".join(self._text).split())
            self._text = []
            return
        if tag == self._heading:
            title = " ".join("".join(self._text).split())
            self._text = []
            self._heading = None
            self.topics[-1] = (f"{SPLIT_TAB}-{len(self.topics)}", title, [])
            return
        if tag == "span" and self._version:
            self._version = False
        if tag in BLOCKS:
            self._flush()

    def handle_data(self, data):
        if self._skip:
            return
        self._text.append(__version__ if self._version else data)


def topics(path=None):
    """[(key, title, text)] from help.html."""
    parser = _HelpParser()
    with open(path or resource_path("static", "help.html"), encoding="utf-8-sig") as fh:
        parser.feed(fh.read())
    parser.close()
    return [(key, title, "\n\n".join(paragraphs))
            for key, title, paragraphs in parser.topics if paragraphs]


class HelpPage(wx.Panel):
    def __init__(self, frame, title, text, support=False):
        super().__init__(frame.host)
        self.Hide()
        sizer, parent = page_box(self, title)
        box = read_only(parent, sizer, title, height=420, width=560, scroll=True)
        box.SetValue(text)
        if support:
            button = wx.Button(parent, label="Open the support page in your browser")
            button.Bind(wx.EVT_BUTTON, lambda _e: webbrowser.open(SUPPORT_URL))
            sizer.Add(button, 0, wx.ALL, 6)


class HelpSection:
    title = TITLE
    route = "about"

    def __init__(self, frame):
        self.pages = [Page(key, title, HelpPage(frame, title, text, support=key == "support"))
                      for key, title, text in topics()]
