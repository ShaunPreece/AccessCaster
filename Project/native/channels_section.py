"""Channels and Processing: one page per channel, drawn from the window's desk state
(`desk_state`), so a change made on the desk shows without a re-read.

Each page is a tab control (`channel_tabs`): the channel's settings, then presets, processing, the
parameter groups and effects, as the channel has them.
"""
import wx

import channels
from native.channel_jobs import LevelCheck
from native.channel_tabs import build_tabs
from native.pages import MessagePage, Page, page_body

TITLE = "Channels and Processing"


class ChannelsSection:
    title = TITLE
    route = "channels"

    def __init__(self, frame):
        self.frame = frame
        self.desk = frame.desk
        self.tab_key = None        # the tab last chosen, which every channel page follows
        self.level_check = LevelCheck(frame)
        # What the next full read owes the user: a refusal if they asked for it, and with
        # `_announce`, the success too. A read the window starts itself says nothing.
        self._asked = False
        self._announce = False
        self.status = MessagePage(frame.host, "Starting AccessCaster.",
                                  button="Read the channels again",
                                  on_button=lambda: self.refresh(True))
        self.pages = [Page("status", TITLE, self.status)]
        self.recs = {}             # every channel's record, by name, in the desk's order
        self.channel_pages = {}    # the pages built so far: see _build
        self.desk.subscribe(self._desk_event)

    @property
    def data(self):
        return self.desk.channels

    def start(self):
        if not self.recs:
            self.status.set_text("Reading the channels from the desk.")

    def refresh(self, announce):
        self._asked = True
        self._announce = self._announce or announce
        if not self.recs:
            self.status.set_text("Reading the channels from the desk.")
        self.desk.refresh()

    def _desk_event(self, event, detail):
        if event == "changed":
            for name in detail:
                page = self.channel_pages.get(name)
                if page is not None:
                    page.show(page.rec)
            return
        if event != "read":
            return
        reply, asked, announce = detail, self._asked, self._announce
        self._asked = self._announce = False
        if not reply.get("ok"):
            text = reply.get("message") or "Could not read the channels."
            if not self.recs:
                self.status.set_text(text)
            elif asked:
                self.frame.say(text)
            return
        recs = reply["channels"]
        if [r["name"] for r in recs] == list(self.recs):
            self.recs = {rec["name"]: rec for rec in recs}
            for name, page in self.channel_pages.items():
                page.show(self.recs[name])
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
        self.recs = {rec["name"]: rec for rec in recs}
        self.channel_pages = {}
        if recs:
            self.pages = [Page(rec["name"], rec["label"],
                               build=lambda name=rec["name"]: self._build(name))
                          for rec in recs]
        else:
            self.status.set_text("The desk reports no channels.")
            self.pages = [Page("status", TITLE, self.status)]
        self.frame.pages_changed(self)

    def _build(self, name):
        """A channel's page, built the first time the window shows it, from the channel's current
        record (`desk_state` updates records in place). Its tabs are built as they are shown."""
        page = ChannelPage(self, self.recs[name])
        self.channel_pages[name] = page
        return page

    def presets_changed(self, presets):
        """The preset list changed (saved or deleted): every mic page's list shows the new one."""
        if presets is not None:
            self.data["presets"] = presets
        for page in self.channel_pages.values():
            page.show(page.rec)

    def tab_chosen(self, key):
        """Every channel page follows the tab last chosen, so arrowing to the next channel keeps
        you on Dynamics. Each moves to it in `ChannelPage.before_show`."""
        self.tab_key = key


class ChannelPage(wx.Panel):
    """One channel: its tabs."""

    def __init__(self, section, rec):
        super().__init__(section.frame.host)
        # Hidden before its notebook exists: see before_show.
        self.Hide()
        self.section = section
        self.frame = section.frame
        self.name = rec["name"]
        self.rec = rec
        self.number = (channels.mic_channel_number(channels.ui_channel_map()[self.name])
                       if rec["kind"] == "mic" else None)
        sizer, parent = page_body(self, grow=True)
        self.book = wx.Notebook(parent)
        sizer.Add(self.book, 1, wx.EXPAND | wx.ALL, 4)
        self.tabs = build_tabs(self)
        for tab in self.tabs:
            self.book.AddPage(tab, tab.title)
        self.book.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGING, self._on_tab_changing)
        self.book.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self._on_tab)

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
            if tab.built:
                tab.show(rec)

    def before_show(self):
        """Move to the tab chosen last on any channel page. Only ever done here, while the page is
        hidden: a notebook takes the focus when its selection changes on screen. The tab shown is
        built here if it is not yet, which a page without that tab needs too."""
        self.select_tab(self.section.tab_key)
        self.tabs[self.book.GetSelection()].build()

    def select_tab(self, key):
        for index, tab in enumerate(self.tabs):
            if tab.key == key:
                tab.build()
                self.book.ChangeSelection(index)
                return

    def _on_tab_changing(self, event):
        # A tab is built before the tab control shows it, so it appears whole. A change made in
        # code (wx's Ctrl+Tab) names the new tab here and shows it before PAGE_CHANGED. One the
        # native control makes on Windows names the OLD tab here, and shows the new one only after
        # PAGE_CHANGED, so `_on_tab` builds it.
        index = event.GetSelection()
        if index != wx.NOT_FOUND:
            self.tabs[index].build()
        event.Skip()

    def _on_tab(self, event):
        index = event.GetSelection()
        if index != wx.NOT_FOUND:
            self.tabs[index].build()
            self.section.tab_chosen(self.tabs[index].key)
        event.Skip()


