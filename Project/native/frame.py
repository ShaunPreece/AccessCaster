"""The main window: the Categories tree on the left, the selected page on the right.

Keys:
    F6                         move between the tree and the page
    Ctrl+Tab, Ctrl+Shift+Tab   the next or previous page in the same section, or inside a page
                               with tabs, the next or previous tab
    Alt+1 to Alt+9             jump to a section
    F5                         read the current section from the desk again

Arrowing the tree shows each page and leaves the focus in the tree. Selecting a section that has
pages shows its first page.
"""
import wx

import server
from native import announce
from native.calls import call
from native.channels_section import ChannelsSection
from native.desk_state import DeskState
from native.pads_section import PadsSection
from native.pages import Speaker, WebSection
from native.routing_section import RoutingSection

# The sections still on the web page, as (title, web route), in tree order after SMART pads.
WEB_SECTIONS = [
    ("Recording", "recording"),
    ("CallMe", "callme"),
    ("Backups", "profiles"),
    ("Device", "status"),
    ("App settings", "settings"),
    ("Help and About", "about"),
]


class MainFrame(Speaker, wx.Frame):
    def __init__(self):
        super().__init__(None, title="AccessCaster", size=(980, 680))
        root = wx.Panel(self)
        # A static text created just before a control names it, the standard Win32 labelling.
        tree_label = wx.StaticText(root, label="Categories")
        self.tree = wx.TreeCtrl(root, style=wx.TR_HIDE_ROOT | wx.TR_HAS_BUTTONS
                                | wx.TR_LINES_AT_ROOT | wx.TR_SINGLE)
        # The pages share one host and are shown and hidden directly. wx.Simplebook would do the
        # same job, but it moves the focus into each page it switches to, which pulls focus out of
        # the tree on every arrow press.
        self.host = wx.Panel(root)
        self.host.SetSizer(wx.BoxSizer(wx.VERTICAL))
        self._shown = None
        self._rebuilding = False

        # Never shown: the window the screen-reader notifications are raised from.
        sink = wx.StaticText(root, label="", size=(1, 1))
        self._notifier = announce.Notifier(sink.GetHandle(), "AccessCaster")

        self.desk = DeskState(self)
        self.sections = [ChannelsSection(self), RoutingSection(self), PadsSection(self)] + [
            WebSection(self, title, route) for title, route in WEB_SECTIONS]
        tree_root = self.tree.AddRoot("AccessCaster")
        self._branches = []
        for section in self.sections:
            self._branches.append(self.tree.AppendItem(tree_root, section.title))
            self._fill_branch(section)

        left = wx.BoxSizer(wx.VERTICAL)
        left.Add(tree_label, 0, wx.BOTTOM, 4)
        left.Add(self.tree, 1, wx.EXPAND)
        left.SetMinSize((260, -1))
        outer = wx.BoxSizer(wx.HORIZONTAL)
        outer.Add(left, 0, wx.EXPAND | wx.ALL, 8)
        outer.Add(self.host, 1, wx.EXPAND | wx.ALL, 8)
        outer.Add(sink, 0)
        root.SetSizer(outer)

        self.tree.Bind(wx.EVT_TREE_SEL_CHANGED, self._on_select)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.tree.SelectItem(self._branches[0])

    # ---- the service -----------------------------------------------------------------------

    def start(self):
        """Bring the service up off the UI thread, then let each section read the desk."""
        def bring_up():
            # No "AccessCaster started": the window opening in front already says so.
            started = server.start_server_managed(open_browser=False, greeting="")
            if started is None:
                return {"ok": False, "message": "AccessCaster could not start: another copy is "
                                                "already running on this computer."}
            return {"ok": True}
        call(self, bring_up, done=self._started)

    def _started(self, reply):
        if not reply.get("ok"):
            self.say(reply["message"])
            return
        self.desk.start()
        for section in self.sections:
            if hasattr(section, "start"):
                section.start()

    def open_web_page(self, route):
        server.open_ui(f"http://localhost:{server.HTTP_PORT}/#/{route}")

    def say(self, text, recent=False):
        self._notifier.say(text, announce.MOST_RECENT if recent else announce.ALL)

    # ---- the tree --------------------------------------------------------------------------

    def _fill_branch(self, section):
        branch = self._branches[self.sections.index(section)]
        for page in section.pages:
            if page.window.GetContainingSizer() is None:
                page.window.Hide()
                self.host.GetSizer().Add(page.window, 1, wx.EXPAND)
        self.tree.SetItemData(branch, section.pages[0])
        if len(section.pages) > 1:
            for page in section.pages:
                self.tree.AppendItem(branch, page.title, data=page)

    def pages_changed(self, section):
        """Rebuild `section`'s branch, keeping the selection on the same page where it survives."""
        branch = self._branches[self.sections.index(section)]
        selected = self.tree.GetSelection()
        was_inside = selected.IsOk() and self.tree.GetItemParent(selected) == branch
        key = self.tree.GetItemData(selected).key if was_inside else None
        self._rebuilding = True
        self.tree.DeleteChildren(branch)
        self._fill_branch(section)
        if was_inside:
            match = next((child for child in self._children(branch)
                          if self.tree.GetItemData(child).key == key), branch)
            self.tree.SelectItem(match)
        self._rebuilding = False
        current = self.tree.GetSelection()
        if current.IsOk():
            self._show(self.tree.GetItemData(current).window)

    def _on_select(self, event):
        # The tree changes selection as its items are deleted on close and during a rebuild.
        if not self or self.IsBeingDeleted() or self._rebuilding:
            return
        item = event.GetItem()
        if item.IsOk():
            self._show(self.tree.GetItemData(item).window)

    def _show(self, window):
        if window is self._shown:
            return
        self.host.Freeze()
        if self._shown:
            self._shown.Hide()
        window.Show()
        self._shown = window
        self.host.Layout()
        self.host.Thaw()

    def _children(self, item):
        children = []
        child, cookie = self.tree.GetFirstChild(item)
        while child.IsOk():
            children.append(child)
            child, cookie = self.tree.GetNextChild(item, cookie)
        return children

    def _branch_of(self, item):
        parent = self.tree.GetItemParent(item)
        return item if parent == self.tree.GetRootItem() else parent

    # ---- keys ------------------------------------------------------------------------------

    def _tree_has_focus(self):
        return wx.Window.FindFocus() is self.tree

    def _focus_page(self):
        # NavigateIn does not reach controls that sit inside the page's group box.
        target = self._first_focusable(self._shown) if self._shown else None
        if target:
            target.SetFocus()

    def _in_tabs(self):
        """Whether the focus is in a page's tab control, where Ctrl+Tab is the tab control's."""
        window = wx.Window.FindFocus()
        while window is not None and window is not self:
            if isinstance(window, wx.Notebook):
                return True
            window = window.GetParent()
        return False

    def _first_focusable(self, window):
        for child in window.GetChildren():
            if not child.IsShown() or not child.IsEnabled():
                continue
            if child.AcceptsFocus() and not isinstance(child, wx.Panel):
                return child
            found = self._first_focusable(child)
            if found:
                return found
        return None

    def _step_page(self, step):
        item = self.tree.GetSelection()
        branch = self._branch_of(item)
        siblings = self._children(branch)
        if not siblings:
            return
        if item == branch:
            item = siblings[0]
        index = next(i for i, s in enumerate(siblings) if s == item)
        in_page = not self._tree_has_focus()
        self.tree.SelectItem(siblings[(index + step) % len(siblings)])
        if in_page:
            self._focus_page()

    def _on_key(self, event):
        key, mods = event.GetKeyCode(), event.GetModifiers()
        if key == wx.WXK_F6 and mods == wx.MOD_NONE:
            if self._tree_has_focus():
                self._focus_page()
            else:
                self.tree.SetFocus()
            return
        if key == wx.WXK_F5 and mods == wx.MOD_NONE:
            section = self.sections[self._branches.index(
                self._branch_of(self.tree.GetSelection()))]
            if hasattr(section, "refresh"):
                section.refresh(True)
            return
        if (key == wx.WXK_TAB and mods in (wx.MOD_CONTROL, wx.MOD_CONTROL | wx.MOD_SHIFT)
                and not self._in_tabs()):
            self._step_page(-1 if mods & wx.MOD_SHIFT else 1)
            return
        if mods == wx.MOD_ALT and ord("1") <= key <= ord("9"):
            index = key - ord("1")
            if index < len(self._branches):
                self.tree.SelectItem(self._branches[index])
                self.tree.SetFocus()
                return
        event.Skip()

    def _on_close(self, event):
        self.desk.stop()
        self._notifier.close()
        server.stop_server()
        event.Skip()
