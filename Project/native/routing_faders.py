"""Routing's Fader assignment page: which channel sits on each fader, the desk's default layout,
and the stereo links. Drawn from the window's desk state; a write's reply shows at once and a full
read follows, since processing and levels move with the channels.
"""
import wx
import wx.lib.scrolledpanel as scrolled

import server
from native import a11y
from native.calls import call
from native.controls import Switch, set_choices
from native.pages import group, labelled, page_box

TITLE = "Fader assignment"
READ_ONLY = "Changing the fader layout is not enabled on this device yet."
LINK_READ_ONLY = "Linking two inputs is not enabled on this device yet."


class FadersPage(scrolled.ScrolledPanel):
    def __init__(self, section, model, previous=None):
        super().__init__(section.frame.host)
        self.Hide()
        self.section = section
        self.frame = section.frame
        self.model = model
        self.read_only = model["read_only"]
        self._codes = []
        sizer, parent = page_box(self, TITLE)

        # One list of the faders, each item naming the channel on it, so arrowing it reads the
        # whole layout.
        self.fader = labelled(parent, sizer, "Fader", lambda p: wx.Choice(p))
        self.fader.Bind(wx.EVT_CHOICE, lambda _e: self._show_fader())
        self.channel = labelled(parent, sizer, "Channel on this fader", lambda p: wx.Choice(p))
        a11y.describe(self.channel, "Choosing a channel already on another fader swaps the two. "
                                    "Processing moves with the channel.")
        # Confirmed by the button, never by the list: arrowing a closed list fires a change per
        # item, and each change would move a channel and its processing.
        change = wx.Button(parent, label="Confirm fader change")
        change.Bind(wx.EVT_BUTTON, self._on_change)
        sizer.Add(change, 0, wx.ALL, 6)
        reset = wx.Button(parent, label="Reset faders to default")
        a11y.describe(reset, "The unit's default channel on each fader.")
        reset.Bind(wx.EVT_BUTTON, self._on_reset)
        sizer.Add(reset, 0, wx.ALL, 6)

        self.links = []
        pairs = model.get("stereo_pairs") or []
        if pairs:
            links_sizer, links = group(parent, sizer, "Stereo linked inputs")
            for pair in pairs:
                self.links.append(Switch(
                    self, links, links_sizer, f"Link {pair['label']}",
                    send=lambda on, code=pair["code"]: server.action_set_stereo_link(code, on),
                    current=lambda code=pair["code"]: self._linked(code),
                    remember=lambda _on: None,
                    accepted=self.section.desk.faders_changed,
                    description=model.get("stereo_note") or "",
                    guard=lambda: LINK_READ_ONLY if model["stereo_read_only"] else None))

        self.show()
        if previous is not None:
            self.fader.SetSelection(previous.fader.GetSelection())
            self._show_fader()
        self.SetupScrolling(scroll_x=False)

    @property
    def faders(self):
        return self.section.desk.faders

    def _linked(self, code):
        return next((p["linked"] for p in self.faders["stereo"] if p["code"] == code), None)

    def show(self):
        """Put the desk state's layout on every control."""
        names = [f"{fader['label']}, {self.faders['labels'][i]}"
                 for i, fader in enumerate(self.model["faders"])]
        chosen = self.fader.GetSelection()
        set_choices(self.fader, names)
        self.fader.SetSelection(chosen if 0 <= chosen < len(names) else 0)
        self._show_fader()
        for link in self.links:
            link.show()

    def _show_fader(self):
        """The chosen fader's channel, selected in a list of what it may take. A channel it holds
        that is not offered (a linked pair, or nothing) is added, so the list still shows what is
        there."""
        index = self.fader.GetSelection()
        code = self.faders["layout"][index]
        options = [(c, label) for c, label in self.model["channels"]]
        if all(c != code for c, _label in options):
            options.append((code, self.faders["labels"][index]))
        self._codes = [c for c, _label in options]
        set_choices(self.channel, [label for _c, label in options])
        self.channel.SetSelection(self._codes.index(code))

    def _on_change(self, event):
        source = event.GetEventObject()
        if self.read_only:
            self.frame.say(READ_ONLY)
            return
        index = self.fader.GetSelection()
        code = self._codes[self.channel.GetSelection()]
        if code == self.faders["layout"][index]:
            self.frame.say(f"{self.model['faders'][index]['label']} already has "
                           f"{self.channel.GetStringSelection()}.")
            return
        call(self, server.action_set_fader, index, code,
             done=lambda reply: self._done(source, reply))

    def _on_reset(self, event):
        source = event.GetEventObject()
        if self.read_only:
            self.frame.say(READ_ONLY)
            return
        self.frame.say("Resetting faders, please wait.")
        call(self, server.action_reset_faders, done=lambda reply: self._done(source, reply))

    def _done(self, source, reply):
        self.frame.reply(source, reply)
        if reply.get("ok"):
            self.section.desk.faders_changed(reply)
        else:
            self._show_fader()
