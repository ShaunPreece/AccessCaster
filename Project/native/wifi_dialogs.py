"""Joining a wifi network: choose one the scan found, then give its password.

Each opens straight after another dialog closes, so neither has a default button: the Enter that
closed the one before can land on it. Enter in a list or a field still acts, through its own key
handling, and a stray one can do no harm: a network that needs a password refuses to join without
one.
"""
import wx

from native.dialogs import _paragraphs

# `NetworksDialog.choice` for "Network not listed": a network the user names themselves.
NOT_LISTED = {}
NOT_LISTED_ID = wx.NewIdRef()
ENTER = (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER)


class NetworksDialog(wx.Dialog):
    """The networks a scan found, then Join, Network not listed or Cancel. `choice` is the chosen
    scan entry, `NOT_LISTED`, or None for Cancel. With nothing found there is no list, and the
    focus lands on Network not listed."""

    def __init__(self, parent, networks, note):
        super().__init__(parent, title="Choose a network")
        self.networks = networks
        self.choice = None
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.list = None
        if networks:
            sizer.Add(wx.StaticText(self, label="Networks found"), 0, wx.LEFT | wx.TOP, 10)
            self.list = wx.ListBox(self, choices=[n["spoken"] for n in networks], size=(360, 120))
            self.list.SetSelection(0)
            self.list.Bind(wx.EVT_CHAR_HOOK, self._on_list_key)
            self.list.Bind(wx.EVT_LISTBOX_DCLICK, lambda _e: self._chosen())
            sizer.Add(self.list, 0, wx.ALL, 10)
        if note:
            _paragraphs(self, sizer, note)
        row = wx.BoxSizer(wx.HORIZONTAL)
        buttons = [(NOT_LISTED_ID, "Network not listed"), (wx.ID_CANCEL, "Cancel")]
        if networks:
            buttons.insert(0, (wx.ID_OK, "Join"))
        for button_id, label in buttons:
            row.Add(wx.Button(self, button_id, label), 0, wx.ALL, 5)
        sizer.Add(row, 0, wx.ALIGN_RIGHT | wx.ALL, 7)
        self.SetSizerAndFit(sizer)
        self.Bind(wx.EVT_BUTTON, lambda _e: self._chosen(), id=wx.ID_OK)
        self.Bind(wx.EVT_BUTTON, self._not_listed, id=NOT_LISTED_ID)
        self.CentreOnParent()

    def _on_list_key(self, event):
        if event.GetKeyCode() in ENTER and not event.GetModifiers():
            self._chosen()
            return
        event.Skip()

    def _chosen(self):
        self.choice = self.networks[self.list.GetSelection()]
        self.EndModal(wx.ID_OK)

    def _not_listed(self, _event):
        self.choice = NOT_LISTED
        self.EndModal(wx.ID_OK)


class JoinDialog(wx.Dialog):
    """The name and password of the network to join, then Join or Cancel. `network` is a scan entry
    or `NOT_LISTED`. A listed network has its name filled in and lands on the password; an open
    one has no password box; one not listed lands on its name. `ssid` and `password` hold what was
    typed once Join closes it."""

    def __init__(self, parent, network):
        name = network.get("ssid") or ""
        super().__init__(parent, title=f"Join {name}" if name else "Join a network not listed")
        self.ssid = self.password = ""
        self._listed = name
        self._needs_password = bool(name) and not network.get("open")
        sizer = wx.BoxSizer(wx.VERTICAL)
        grid = wx.FlexGridSizer(2, 8, 8)
        # Windows names a box after the static text created just before it, and the dialog lands
        # on the first box created, so for a listed network the password's pair is created first
        # and only laid out second.
        name_pair = password_pair = None
        if self._needs_password:
            password_pair = self._pair("Password", "", wx.TE_PASSWORD)
            name_pair = self._pair("Network name", name)
        else:
            name_pair = self._pair("Network name", name)
            if not name:
                password_pair = self._pair("Password", "", wx.TE_PASSWORD)
        self.name_box = name_pair[1]
        self.password_box = password_pair[1] if password_pair else None
        for pair in (name_pair, password_pair):
            if pair:
                grid.Add(pair[0], 0, wx.ALIGN_CENTER_VERTICAL)
                grid.Add(pair[1], 0)
        sizer.Add(grid, 0, wx.ALL, 10)
        if not name:
            _paragraphs(self, sizer, "Type the name exactly, capitals included, and leave the "
                                     "password empty for an open network.")
        elif not self._needs_password:
            _paragraphs(self, sizer, f"{name} is an open network, so it needs no password.")
        buttons = wx.StdDialogButtonSizer()
        buttons.AddButton(wx.Button(self, wx.ID_OK, label="Join"))
        buttons.AddButton(wx.Button(self, wx.ID_CANCEL, label="Cancel"))
        buttons.Realize()
        sizer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 10)
        self.SetSizerAndFit(sizer)
        self.Bind(wx.EVT_BUTTON, self._on_join, id=wx.ID_OK)
        self.Bind(wx.EVT_TEXT_ENTER, self._on_join)
        self.CentreOnParent()

    def _pair(self, label, value, style=0):
        text = wx.StaticText(self, label=label)
        box = wx.TextCtrl(self, value=value, size=(280, -1), style=style | wx.TE_PROCESS_ENTER)
        return text, box

    def _refuse(self, text, box):
        wx.MessageBox(text, "Not joined", wx.OK | wx.ICON_WARNING, self)
        box.SetFocus()

    def _on_join(self, _event):
        ssid = self.name_box.GetValue().strip()
        password = self.password_box.GetValue() if self.password_box else ""
        if not ssid:
            self._refuse("Type the name of the network first.", self.name_box)
            return
        if self._needs_password and ssid == self._listed and not password:
            self._refuse(f"{ssid} needs a password. Type it first.", self.password_box)
            return
        self.ssid, self.password = ssid, password
        self.EndModal(wx.ID_OK)
