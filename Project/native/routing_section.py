"""Routing: one page each for the output mixes, the faders, the levels and the master mix.

Output mixes, Fader assignment and Levels draw from the window's desk state (`desk_state`), so a
change made on the desk shows without a re-read. Master Mix still opens in the web page.
"""
import wx

from native import routing_faders, routing_levels, routing_mixes
from native.pages import MessagePage, Page

TITLE = "Routing"
# The native pages, in order: (key, title, page class, its layout in the desk state). A page is
# rebuilt when its layout changes, which a USB mode change can do to each.
NATIVE = [
    ("mixes", routing_mixes.TITLE, routing_mixes.OutputMixesPage,
     lambda desk: desk.matrix["model"]),
    ("faders", routing_faders.TITLE, routing_faders.FadersPage,
     lambda desk: desk.faders["model"]),
    ("levels", routing_levels.TITLE, routing_levels.LevelsPage,
     lambda desk: desk.levels["model"]),
]
# The pages still on the web page, as (key, title), in order after the native ones.
WEB_PAGES = [("master", "Master Mix")]


class RoutingSection:
    title = TITLE
    route = "routing"

    def __init__(self, frame):
        self.frame = frame
        self.desk = frame.desk
        self._asked = False
        self._announce = False
        self.native = {}             # key -> its page, once the desk has been read
        self.status = {key: MessagePage(frame.host, title, "Starting AccessCaster.",
                                        button="Read the desk again",
                                        on_button=lambda: self.refresh(True))
                       for key, title, _cls, _model in NATIVE}
        web = [Page(key, title, MessagePage(
            frame.host, title,
            f"{title} is not in this window yet. It opens in the web page, on the Routing "
            f"page's {title} tab.",
            button="Open Routing in the browser",
            on_button=lambda: frame.open_web_page(self.route)))
            for key, title in WEB_PAGES]
        self.pages = [Page(key, title, self.status[key])
                      for key, title, _cls, _model in NATIVE] + web
        self.desk.subscribe(self._desk_event)

    def start(self):
        for key, title, _cls, _model in NATIVE:
            if key not in self.native:
                self.status[key].set_text(f"Reading the {title.lower()} from the desk.")

    def refresh(self, announce):
        self._asked = True
        self._announce = self._announce or announce
        self.desk.refresh()

    def _desk_event(self, event, detail):
        if event == "read":
            self._read(detail)
            return
        # Before the first read there are no native pages to show anything on.
        mixes, faders, levels = (self.native.get(key) for key in ("mixes", "faders", "levels"))
        if event == "mix" and mixes:
            mixes.show_outputs(detail)
            levels.show_outputs(detail)
        elif event == "faders" and faders:
            faders.show()
            levels.show()
        elif event == "levels" and levels:
            levels.show()

    def _read(self, reply):
        asked, announce = self._asked, self._announce
        self._asked = self._announce = False
        if not reply.get("ok"):
            text = reply.get("message") or "Could not read the desk."
            for key, _title, _cls, _model in NATIVE:
                if key not in self.native:
                    self.status[key].set_text(text)
            if asked and self.native:
                self.frame.say(text)
            return
        rebuilt = False
        for position, (key, title, cls, model_of) in enumerate(NATIVE):
            model = model_of(self.desk)
            page = self.native.get(key)
            if page is not None and page.model == model:
                page.show()
                continue
            self._drop_focus(page)
            self.native[key] = cls(self, model, page)
            if page is not None:
                page.Destroy()
            self.pages[position] = Page(key, title, self.native[key])
            rebuilt = True
        if rebuilt:
            self.frame.pages_changed(self)
        if announce:
            self.frame.say("Routing read from the desk.")

    def _drop_focus(self, page):
        """Put the focus in the tree before `page` is destroyed under it."""
        focus = wx.Window.FindFocus()
        if page is not None and focus is not None and page.IsDescendant(focus):
            self.frame.tree.SetFocus()
