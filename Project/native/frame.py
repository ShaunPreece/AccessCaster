"""The main window: the Categories tree on the left, the selected page on the right.

Keys:
    F6                         move between the tree and the page
    Ctrl+Tab, Ctrl+Shift+Tab   the next or previous page in the same section, or inside a page
                               with tabs, the next or previous tab
    Alt+1 to Alt+9             jump to a section
    F5                         read the current section from the desk again
    F12                        check for a new version of AccessCaster

Arrowing the tree shows each page and leaves the focus in the tree. Selecting a section that has
pages shows its first page.

The window belongs to the tray (`tray.TrayApp`), which runs the service. Closing it hides it, and
the tray shows it again with `present`; only Quit destroys it. With `server.keep_in_tray` off,
closing asks the tray to quit (`quit_app`).
"""
import wx

import rode_app
import server
from native import announce
from native.app_settings_section import AppSettingsSection
from native.backups_section import BackupsSection
from native.callme_section import CallMeSection
from native.calls import call
from native.channels_section import ChannelsSection
from native.desk_state import DeskState
from native.dialogs import TextDialog
from native.device_section import DeviceSection
from native.help_section import HelpSection
from native.pads_section import PadsSection
from native.pages import Speaker
from native.recording_section import RecordingSection
from native.routing_section import RoutingSection

# The sections in tree order, which is also their Alt+digit.
SECTIONS = [
    ChannelsSection,
    RoutingSection,
    PadsSection,
    RecordingSection,
    CallMeSection,
    BackupsSection,
    DeviceSection,
    AppSettingsSection,
    HelpSection,
]


APP_TITLE = "AccessCaster"


class MainFrame(Speaker, wx.Frame):
    def __init__(self, quit_app):
        super().__init__(None, title=APP_TITLE, size=(980, 680))
        self._quit_app = quit_app
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
        self._notifier = announce.Notifier(sink, APP_TITLE)

        self.desk = DeskState(self)
        self.desk.subscribe(self._desk_event)
        self.sections = [section(self) for section in SECTIONS]
        tree_root = self.tree.AddRoot(APP_TITLE)
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
        # The desk summary (DeviceSection): a native status bar, which a screen reader reads on
        # demand and does not speak as it changes. Without wx's default ellipsizing, which hands the
        # native control a copy cut to the window's width, and that copy is what SB_GETTEXT, and so
        # the screen reader, returns.
        self.status = self.CreateStatusBar(style=wx.STB_SIZEGRIP | wx.FULL_REPAINT_ON_RESIZE)
        self.status.SetStatusText("Reading the unit.")

        self.tree.Bind(wx.EVT_TREE_SEL_CHANGED, self._on_select)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_ACTIVATE, self._on_activate)
        self.tree.SelectItem(self._branches[0])
        self._fit_to_screen()

    def _fit_to_screen(self):
        """Keep the whole window on its display. NVDA+End finds the status bar at the window's
        bottom-left corner; on a display shorter than the window that corner is under the taskbar,
        and NVDA falls back to its own copy of the bottom line, which keeps the tail of longer text
        drawn there before."""
        index = wx.Display.GetFromWindow(self)
        area = wx.Display(index if index != wx.NOT_FOUND else 0).GetClientArea()
        width = min(self.GetSize().width, area.width)
        height = min(self.GetSize().height, area.height)
        self.SetSize(area.x + (area.width - width) // 2, area.y + (area.height - height) // 2,
                     width, height)

    # ---- the service -----------------------------------------------------------------------

    def start(self):
        """The service is up: read the device status, then let each section read the desk. The
        device status publishes the desk's hardware id, which every per-desk setting is filed
        under."""
        call(self, server.read_device_status, done=self._started)

    def _started(self, _reply):
        self.desk.start()
        for section in self.sections:
            if hasattr(section, "start"):
                section.start()

    def desk_rewritten(self):
        """A job has put the whole desk back (a restore, a factory reset): the desk state, and every
        section that reads the desk for itself, read it again."""
        self.desk.refresh()
        for section in self.sections:
            if hasattr(section, "read"):
                section.read()

    def go_to(self, route, key):
        """Show the page `key` of the section `route`, and move the focus into it. Nothing names
        the page on the way in, so the control that calls this names it ("Go to Wireless
        connections")."""
        section = next(s for s in self.sections if s.route == route)
        branch = self._branches[self.sections.index(section)]
        item = next((child for child in self._children(branch)
                     if self.tree.GetItemData(child).key == key), branch)
        self.tree.SelectItem(item)
        self._focus_page()

    def reread(self, route):
        """Read the section `route` again, after a change made outside the window."""
        next(s for s in self.sections if s.route == route).refresh(False)

    def _on_activate(self, event):
        event.Skip()
        if event.GetActive():
            wx.CallAfter(self.warn_rode_app)

    def warn_rode_app(self):
        """RODE's app is open: say why that matters, once per run of it, and only while this window
        is in front, where the dialog gets the focus (`rode_app.take_warning`)."""
        if not self.IsActive() or not rode_app.take_warning():
            return
        with TextDialog(self, rode_app.DIALOG_TITLE, rode_app.DIALOG_TEXT, [(wx.ID_OK, "OK")],
                        default=wx.ID_OK, escape=wx.ID_OK) as dialog:
            dialog.ShowModal()

    def check_updates(self):
        for section in self.sections:
            if hasattr(section, "check_updates"):
                section.check_updates()

    def say(self, text, recent=False):
        # A closed window says nothing: the user has left it.
        if self.IsShown():
            self._notifier.say(text, recent)

    def _desk_event(self, event, reply):
        """The title names the desk while one is connected."""
        if event == "read":
            name = reply.get("device_name") if reply.get("ok") else None
            title = f"{APP_TITLE}, {name}" if name else APP_TITLE
        elif event == "lost":
            title = APP_TITLE
        else:
            return
        if self.GetTitle() != title:
            self.SetTitle(title)

    def set_status(self, text):
        if self.status.GetStatusText() != text:
            self.status.SetStatusText(text)

    # ---- the tree --------------------------------------------------------------------------

    def _fill_branch(self, section):
        branch = self._branches[self.sections.index(section)]
        for page in section.pages:
            if page.built:
                self._place(page.window)
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
            self._show(self.tree.GetItemData(current))

    def _on_select(self, event):
        # The tree changes selection as its items are deleted on close and during a rebuild.
        if not self or self.IsBeingDeleted() or self._rebuilding:
            return
        item = event.GetItem()
        if item.IsOk():
            self._show(self.tree.GetItemData(item))

    def _place(self, window):
        """Put a page's window in the host, hidden, once."""
        if window.GetContainingSizer() is None:
            window.Hide()
            self.host.GetSizer().Add(window, 1, wx.EXPAND)

    def _show(self, page):
        window = page.window
        if window is self._shown:
            return
        self._place(window)
        if hasattr(window, "before_show"):
            window.before_show()
        self.host.Freeze()
        if self._shown:
            self._shown.Hide()
        # While the window is closed its page stays hidden too; `present` shows it.
        window.Show(self.IsShown())
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
        # NavigateIn does not reach controls that sit inside a group box.
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
        if key == wx.WXK_F12 and mods == wx.MOD_NONE:
            self.check_updates()
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

    # ---- open and closed --------------------------------------------------------------------

    def present(self):
        """Show the window as it was closed. The page goes first, so that activating the window
        puts the focus back on the control it was on."""
        if self._shown and not self._shown.IsShown():
            self._shown.Show()
            self.host.Layout()
        if self.IsIconized():
            self.Iconize(False)
        self.Show()

    def _on_close(self, event):
        if event.CanVeto():
            if not server.keep_in_tray():
                # Quit closes the window with force, which comes back here unvetoable.
                event.Veto()
                wx.CallAfter(self._quit_app)
                return
            # To the tray. The window goes first, so Windows records where the focus was as it
            # deactivates; hiding the page then stops its polls (CallMe, the driver line, the
            # recording clock), which start and stop with their page's EVT_SHOW.
            event.Veto()
            self.Hide()
            if self._shown:
                self._shown.Hide()
            return
        self.desk.stop()
        self._notifier.close()
        event.Skip()
