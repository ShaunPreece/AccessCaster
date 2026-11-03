"""Help and About: the help, a page per topic, read from help/help.txt.

The tree is the table of contents: a topic per `#` heading, its `##` parts kept in its text. A
topic is plain text in a read-only box, a paragraph to a line with a blank
line between, which a screen reader moves through by line or by paragraph.

The file's format, which the website's guide importer (`importAppGuide.py`) reads as well:
- `# Title` starts a topic and `## Title` a part of one.
- Blocks are separated by a blank line. A paragraph is a block, one line per paragraph; a second
  line in the same block is a line break the reader keeps.
- `- text` is a list item, a line each; consecutive items need no blank line between them.
- `* Term` names a term, and the rest of its block is the term's description.
"""
import re
import webbrowser

import wx

from native.pages import Page, read_only
from paths import resource_path
from version import __version__

TITLE = "Help and About"
SUPPORT_URL = "https://www.slightperception.com/support"
CREDIT = "Version {}. Developed by Shaun Preece."
SUPPORT_TOPIC = "Support this project"


def slug(title):
    """A topic's key: lowercase words joined by hyphens, any trailing "(Alt+1)" dropped."""
    title = re.sub(r"\s*\([^)]*\)\s*$", "", title)
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _blocks(lines, start):
    """The blocks of a run of lines, as text: a term as "Term: description", an item without its
    dash. `start` is the run's first line number, for the error."""
    out, block = [], []
    for number, line in enumerate(lines + [""], start):
        if line.strip():
            block.append((number, line.rstrip()))
            continue
        if not block:
            continue
        first_number, first = block[0]
        if first.startswith("* "):
            if len(block) < 2:
                raise ValueError(f"help.txt line {first_number}: a term with no description")
            out.append(f"{first[2:]}: " + "\n".join(text for _n, text in block[1:]))
        elif first.startswith("- "):
            for item_number, item in block:
                if not item.startswith("- "):
                    raise ValueError(f"help.txt line {item_number}: a list item is one line")
                out.append(item[2:])
        else:
            out.append("\n".join(text for _n, text in block))
        block = []
    return out


def topics(path=None):
    """[(key, title, text)] from help.txt, the first topic opening with the version."""
    with open(path or resource_path("help", "help.txt"), encoding="utf-8-sig") as fh:
        lines = fh.read().splitlines()
    # (level, title, first body line number, body lines)
    sections = []
    for number, line in enumerate(lines, 1):
        match = re.match(r"^(#{1,2}) (.+)$", line)
        if match:
            sections.append((len(match.group(1)), match.group(2).strip(), number + 1, []))
        elif sections:
            sections[-1][3].append(line)
        elif line.strip():
            raise ValueError(f"help.txt line {number}: text before the first heading")
    result = []
    for level, title, start, body in sections:
        paragraphs = _blocks(body, start)
        if level == 1:
            result.append([slug(title), title, paragraphs])
        elif not result:
            raise ValueError(f"help.txt line {start - 1}: a part before the first topic")
        else:
            result[-1][2].extend([title] + paragraphs)
    if result:
        result[0][2].insert(0, CREDIT.format(__version__))
    return [(key, title, "\n\n".join(paragraphs)) for key, title, paragraphs in result
            if paragraphs]


class HelpPage(wx.Panel):
    def __init__(self, frame, title, text, support=False):
        super().__init__(frame.host)
        self.Hide()
        # No group box: the text box carries the topic's name, and a group named the same would
        # only make the screen reader say it twice.
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.SetSizer(sizer)
        box = read_only(self, sizer, title, height=420, width=560, scroll=True)
        box.SetValue(text)
        if support:
            button = wx.Button(self, label="Open the support page in your browser")
            button.Bind(wx.EVT_BUTTON, lambda _e: webbrowser.open(SUPPORT_URL))
            sizer.Add(button, 0, wx.ALL, 6)


class HelpSection:
    title = TITLE
    route = "about"

    def __init__(self, frame):
        self.pages = [Page(key, title, HelpPage(frame, title, text, support=title == SUPPORT_TOPIC))
                      for key, title, text in topics()]
