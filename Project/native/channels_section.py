"""Channels and Processing: one page per channel, drawn from the window's desk state
(`desk_state`), so a change made on the desk shows without a re-read.

Each page is a tab control (`channel_tabs`): the channel's settings, then presets, processing, the
parameter groups and effects, as the channel has them. Auto gain and the level check are still on
the web page.
"""
import wx

import channels
from native.channel_tabs import build_tabs
from native.pages import MessagePage, Page, page_box

TITLE = "Channels and Processing"


class ChannelsSection:
    title = TITLE
    route = "channels"

    def __init__(self, frame):
        self.frame = frame
        self.desk = frame.desk
        self.tab_key = None        # the tab last chosen, which every channel page follows
        # What the next full read owes the user: a refusal if they asked for it, and with
        # `_announce`, the success too. A read the window starts itself says nothing.
        self._asked = False
        self._announce = False
        self.status = MessagePage(frame.host, TITLE, "Starting AccessCaster.",
                                  button="Read the channels again",
                                  on_button=lambda: self.refresh(True))
        self.pages = [Page("status", TITLE, self.status)]
        self.channel_pages = {}
        self.desk.subscribe(self._desk_event)

    @property
    def data(self):
        return self.desk.channels

    def start(self):
        if not self.channel_pages:
            self.status.set_text("Reading the channels from the desk.")

    def refresh(self, announce):
        self._asked = True
        self._announce = self._announce or announce
        if not self.channel_pages:
            self.status.set_text("Reading the channels from the desk.")
        self.desk.refresh()

    def _desk_event(self, event, detail):
        if event == "changed":
            for name in detail:
                page = self.channel_pages.get(name)
                if page is not None:
                    page.show(page.rec)
            return
        reply, asked, announce = detail, self._asked, self._announce
        self._asked = self._announce = False
        if not reply.get("ok"):
            text = reply.get("message") or "Could not read the channels."
            if not self.channel_pages:
                self.status.set_text(text)
            elif asked:
                self.frame.say(text)
            return
        recs = reply["channels"]
        if [r["name"] for r in recs] == list(self.channel_pages):
            for rec in recs:
                self.channel_pages[rec["name"]].show(rec)
        else:
            self._rebuild(recs)
        if announce:
            self.frame.say("Channels read from the desk.")

    def _rebuild(self, recs):
        """A different set of channels: the first read, or a USB mode change adding or removing
        Expanded's four."""
        focus = wx.Window.FindFocus()
        if focus and any(p.IsDescendant(focus) for p in self.channel_pages.values()):
            self.frame.tree.SetFocus()
        for page in self.channel_pages.values():
            page.Destroy()
        self.channel_pages = {rec["name"]: ChannelPage(self, rec) for rec in recs}
        if recs:
            self.pages = [Page(rec["name"], rec["label"], self.channel_pages[rec["name"]])
                          for rec in recs]
        else:
            self.status.set_text("The desk reports no channels.")
            self.pages = [Page("status", TITLE, self.status)]
        self.frame.pages_changed(self)

    def solos_cleared(self):
        for page in self.channel_pages.values():
            page.rec["solo"] = False
            page.show(page.rec)

    def presets_changed(self, presets):
        """The preset list changed (saved or deleted): every mic page's list shows the new one."""
        if presets is not None:
            self.data["presets"] = presets
        for page in self.channel_pages.values():
            page.show(page.rec)

    def tab_chosen(self, chosen, key):
        """Every other channel page moves to the same tab, so arrowing to the next channel keeps
        you on Dynamics. They are hidden, which matters: a notebook takes the focus when its
        selection changes while it is on screen."""
        self.tab_key = key
        for page in self.channel_pages.values():
            if page is not chosen and not page.IsShown():
                page.select_tab(key)


class ChannelPage(wx.Panel):
    """One channel: a group box named with the channel, holding its tabs."""

    def __init__(self, section, rec):
        super().__init__(section.frame.host)
        # Hidden before its notebook exists: see ChannelsSection.tab_chosen.
        self.Hide()
        self.section = section
        self.frame = section.frame
        self.name = rec["name"]
        self.rec = rec
        self.number = (channels.mic_channel_number(channels.ui_channel_map()[self.name])
                       if rec["kind"] == "mic" else None)
        sizer, parent = page_box(self, rec["label"], grow=True)
        self.book = wx.Notebook(parent)
        sizer.Add(self.book, 1, wx.EXPAND | wx.ALL, 4)
        self.tabs = build_tabs(self)
        for tab in self.tabs:
            self.book.AddPage(tab, tab.title)
        self.select_tab(section.tab_key)
        self.book.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self._on_tab)
        self.show(rec)

    def field(self, *path, scale=None):
        """(current, remember) for the value at `path` in this page's record, for a Switch or a
        Slider. `scale` turns the record's 0 to 1 into the control's whole numbers."""
        *parents, last = path

        def holder():
            node = self.rec
            for key in parents:
                node = node.get(key) or {}
            return node

        def current():
            value = holder().get(last)
            return round(value * scale) if scale and value is not None else value

        def remember(value):
            node = self.rec
            for key in parents:
                node = node.setdefault(key, {})
            node[last] = value / scale if scale else value
        return current, remember

    def show(self, rec):
        self.rec = rec
        for tab in self.tabs:
            tab.show(rec)

    def select_tab(self, key):
        for index, tab in enumerate(self.tabs):
            if tab.key == key:
                self.book.ChangeSelection(index)
                return

    def _on_tab(self, event):
        index = self.book.GetSelection()
        if index != wx.NOT_FOUND:
            self.section.tab_chosen(self, self.tabs[index].key)
        event.Skip()


