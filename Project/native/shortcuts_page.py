"""App settings, Keyboard shortcuts: the global shortcuts, their keys, and the dialogs that change
them.

The server owns everything that can be got wrong (what commands exist, whether a combination is
legal, whether Windows accepts it): every write's reply carries the whole state (`state`), and the
page shows that.
"""
import wx

import server
from native import a11y
from native.calls import call
from native.dialogs import FormDialog, _paragraphs, confirm
from native.pages import AFTER_FOCUS_MS, labelled, page_body, set_items
from native.controls import Switch, set_choices

LIST_HINT = ("Press Enter to edit a shortcut, Delete to delete it, or the Applications key for "
             "more options.")
EMPTY = "You have no shortcuts yet. Press Add a shortcut to make one."
OFF_NOTE = " Shortcuts are off, so none of these combinations is in use."


def item_text(row):
    """What a shortcut does and what it is on, so moving down the list answers "what are my
    shortcuts"."""
    if row.get("problem"):
        return f"{row['label']}, {row['combo']}, not working: {row['problem']}"
    return f"{row['label']}, " + (row["combo"] if row.get("enabled") else "off")


def mods_text(state, mods):
    """"Ctrl, Alt and Shift", from the server's own modifier labels."""
    labels = [m["label"] for m in state.get("modifiers") or [] if m["id"] in (mods or [])]
    if len(labels) < 2:
        return "".join(labels)
    return ", ".join(labels[:-1]) + " and " + labels[-1]


def restore_text(state):
    have = len(state.get("shortcuts") or [])
    return (f"This replaces your {have} {'shortcut' if have == 1 else 'shortcuts'} with the "
            f"{state.get('default_count') or 0} standard ones and the "
            f"{state.get('default_pad_count') or 0} SMART pad keys, all on "
            f"{mods_text(state, state.get('default_mods'))}. Anything you have added or changed "
            "is lost.")


def first_free_key(state):
    """The first key no switched-on shortcut is using, which Add offers to begin with."""
    taken = {row["key"] for row in state.get("shortcuts") or [] if row.get("enabled")}
    return next((key for key in state.get("keys") or [] if key not in taken), "")


class ShortcutsPage(wx.Panel):
    def __init__(self, section):
        super().__init__(section.frame.host)
        self.Hide()
        self.section = section
        self.frame = section.frame
        self.state = None            # the last `server.hotkey_state`, or a write's `state`
        self.rows = []               # the shortcuts as listed
        sizer, parent = page_body(self)
        self.enabled = Switch(
            self, parent, sizer, "Use keyboard shortcuts", server.action_set_hotkeys_enabled,
            lambda: (self.state or {}).get("enabled"), lambda _on: None,
            description="These work anywhere in Windows, whatever program you are in and even "
                        "with this window closed, for as long as AccessCaster is running. Each "
                        "one needs Ctrl, Alt or Windows in it, so it cannot take a plain key away "
                        "from your screen reader. Turning this off releases every combination.",
            guard=self._unread, accepted=lambda reply: self.show(reply.get("state")))
        self.list = labelled(parent, sizer, "Your shortcuts", lambda p: wx.ListBox(
            p, choices=["Reading the shortcuts."], size=(460, 220)))
        self.list_acc = a11y.describe(self.list, LIST_HINT)
        # On the list itself, so Enter and Delete are the list's before the page sees them.
        self.list.Bind(wx.EVT_CHAR_HOOK, self._on_list_key)
        self.list.Bind(wx.EVT_LISTBOX_DCLICK, lambda _e: self._edit())
        # The Applications key, Shift+F10 and a right-click.
        self.list.Bind(wx.EVT_CONTEXT_MENU, self._on_menu)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.add = self._button(parent, row, "Add a shortcut", self._on_add)
        self.mods = self._button(parent, row, "Change the keys to hold", self._on_mods)
        self.restore = self._button(parent, row, "Restore default shortcuts", self._on_restore)
        sizer.Add(row, 0, wx.ALL, 6)
        # Partly ticked while some of the pad scheme is bound. Ticking adds what is missing;
        # unticking deletes it.
        self.pad_set = wx.CheckBox(parent, label="Use the SMART pad keys", style=wx.CHK_3STATE)
        a11y.describe(self.pad_set, "The number keys play the pads of the bank the desk is on, "
                                    "Dash and Equals change bank, and P says what is playing.")
        self.pad_set.Bind(wx.EVT_CHECKBOX, self._on_pad_set)
        sizer.Add(self.pad_set, 0, wx.ALL, 6)

    def _button(self, parent, row, label, handler):
        button = wx.Button(parent, label=label)
        button.Bind(wx.EVT_BUTTON, handler)
        row.Add(button, 0, wx.RIGHT, 8)
        return button

    def _unread(self):
        return "The shortcuts have not been read yet." if self.state is None else None

    def read(self, announce=False):
        def done(reply):
            self.show(reply)
            if announce:
                self.frame.say("Shortcuts read." if reply.get("ok")
                               else reply.get("message") or "Could not read the shortcuts.")
        call(self, server.hotkey_state, done=done)

    def show(self, state, keep=None):
        """Put the whole page in `state`, keeping the selection on `keep` (an id) or on the shortcut
        selected now, else on the first."""
        if not state or not state.get("ok", True):
            return
        chosen = self._chosen()
        keep = keep or (chosen["id"] if chosen else None)
        self.state = state
        self.rows = state.get("shortcuts") or []
        set_items(self.list, [item_text(row) for row in self.rows] or [EMPTY])
        ids = [row["id"] for row in self.rows]
        self.list.SetSelection(ids.index(keep) if keep in ids else 0)
        self.list_acc.description = LIST_HINT + ("" if state.get("enabled") else OFF_NOTE)
        self.enabled.show()
        label = f"Change the keys to hold, now {mods_text(state, state.get('mods'))}"
        if self.mods.GetLabel() != label:
            self.mods.SetLabel(label)
        self.pad_set.Set3StateValue({"all": wx.CHK_CHECKED, "some": wx.CHK_UNDETERMINED}.get(
            state.get("pad_set"), wx.CHK_UNCHECKED))

    def _chosen(self):
        index = self.list.GetSelection()
        return self.rows[index] if self.rows and 0 <= index < len(self.rows) else None

    def _later(self, text):
        wx.CallLater(AFTER_FOCUS_MS, self.frame.say, text)

    def _applied(self, reply, keep=None, speak=True):
        """A write's reply: the state it carries, then the sentence once the focus is back."""
        self.show(reply.get("state"), keep)
        if speak or not reply.get("ok"):
            self._later(reply.get("message") or ("Done." if reply.get("ok")
                                                 else "That did not work."))

    # ---- the list -----------------------------------------------------------------------------

    def _on_list_key(self, event):
        key, mods = event.GetKeyCode(), event.GetModifiers()
        if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and not mods:
            self._edit()
            return
        if key in (wx.WXK_DELETE, wx.WXK_NUMPAD_DELETE) and not mods:
            self._delete()
            return
        event.Skip()

    def _none_chosen(self):
        if self._chosen() is None:
            self.frame.say(self._unread() or "You have no shortcuts yet. Add one first.")
            return True
        return False

    def _on_menu(self, _event):
        row = self._chosen()
        if row is None:
            return
        menu = wx.Menu()
        edit = menu.Append(wx.ID_ANY, "Edit")
        toggle = menu.Append(wx.ID_ANY, "Turn off" if row.get("enabled") else "Turn on")
        speak = menu.AppendCheckItem(wx.ID_ANY, "Speak when pressed")
        speak.Check(row.get("speak", True))
        delete = menu.Append(wx.ID_ANY, "Delete")
        actions = {edit.GetId(): self._edit,
                   toggle.GetId(): lambda: self._send(row, enabled=not row.get("enabled")),
                   speak.GetId(): lambda: self._send(row, speak=not row.get("speak", True)),
                   delete.GetId(): self._delete}
        chosen = self.list.GetPopupMenuSelectionFromUser(menu)
        menu.Destroy()
        if chosen in actions:
            actions[chosen]()

    def _send(self, row, **changes):
        """One shortcut with `changes` applied. The keys held are shared by every shortcut and are
        never sent per shortcut."""
        values = {"key": row["key"], "enabled": row.get("enabled"),
                  "speak": row.get("speak", True), **changes}
        call(self, server.action_set_hotkey, row["id"], values["key"], values["enabled"],
             values["speak"], done=lambda reply: self._applied(reply, row["id"]))

    def _edit(self):
        if self._none_chosen():
            return
        row = self._chosen()
        dialog = EditDialog(self, self.state, row)
        with dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            reply = dialog.reply
        self._applied(reply, row["id"])

    def _delete(self):
        if self._none_chosen():
            return
        row = self._chosen()
        if not confirm(self, "Delete this shortcut?",
                       f'This removes the shortcut for "{row["label"]}" and gives its keys back to '
                       "whatever else uses them. Nothing on the desk changes, and you can add it "
                       "again.", "Delete"):
            return
        # The neighbour, chosen before the list changes, so the focus has somewhere to go.
        index = self.rows.index(row)
        others = self.rows[index + 1:index + 2] or self.rows[max(0, index - 1):index]
        neighbour = others[0]["id"] if others else None
        # A delete that worked says nothing more: it was confirmed, and the focus landing on the
        # next shortcut shows it is gone.
        call(self, server.action_delete_hotkey, row["id"],
             done=lambda reply: self._applied(reply, neighbour, speak=False))

    # ---- the buttons ----------------------------------------------------------------------

    def _on_add(self, _event):
        if self._unread():
            self.frame.say(self._unread())
            return
        # The catalogue reads the pads and presets on the unit, so it is read on every open.
        self.frame.say("Reading what can be put on a shortcut.")
        call(self, server.hotkey_commands, done=self._add_with)

    def _add_with(self, catalogue):
        if not catalogue.get("groups"):
            self.frame.say(catalogue.get("message") or "Nothing could be listed.")
            return
        dialog = AddDialog(self, self.state, catalogue)
        with dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            reply = dialog.reply
        self.list.SetFocus()
        self._applied(reply, reply.get("id"))

    def _on_mods(self, _event):
        if self._unread():
            self.frame.say(self._unread())
            return
        dialog = ModsDialog(self, self.state)
        with dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            reply = dialog.reply
        self._applied(reply)

    def _on_restore(self, _event):
        if self._unread():
            self.frame.say(self._unread())
            return
        if not confirm(self, "Restore the default shortcuts?", restore_text(self.state),
                       "Restore"):
            return
        call(self, server.action_restore_hotkey_defaults,
             done=lambda reply: self._applied(reply, keep=None))

    def _on_pad_set(self, _event):
        refusal = self._unread()
        if refusal:
            self.pad_set.Set3StateValue(wx.CHK_UNCHECKED)
            self.frame.say(refusal)
            return
        on = self.pad_set.Get3StateValue() != wx.CHK_UNCHECKED

        def done(reply):
            self.show(reply.get("state") or self.state)
            self.frame.reply(self.pad_set, reply)
        call(self, server.action_set_pad_shortcuts, on, done=done)


class EditDialog(FormDialog):
    """One shortcut's key, whether it is on, and whether it speaks when pressed."""

    def __init__(self, parent, state, row):
        super().__init__(parent, f"Edit shortcut: {row['label']}", "Save", "Shortcut not saved")
        self.row = row
        if row.get("problem"):
            _paragraphs(self, self.sizer, row["problem"])
        self.keys = state.get("keys") or []
        self.key = labelled(self, self.sizer, "Key to press", lambda p: wx.Choice(p))
        set_choices(self.key, self.keys)
        if row["key"] in self.keys:
            self.key.SetSelection(self.keys.index(row["key"]))
        # Ticked means NOT on: the box reads "Turn this shortcut off".
        self.off = wx.CheckBox(self, label="Turn this shortcut off")
        self.off.SetValue(not row.get("enabled"))
        self.sizer.Add(self.off, 0, wx.ALL, 10)
        self.speak = wx.CheckBox(self, label="Speak when pressed")
        self.speak.SetValue(row.get("speak", True))
        self.sizer.Add(self.speak, 0, wx.ALL, 10)
        self.finish_layout()

    def request(self):
        index = self.key.GetSelection()
        if index == wx.NOT_FOUND:
            return "Choose a key first."
        key, on, speak = self.keys[index], not self.off.GetValue(), self.speak.GetValue()
        return lambda: server.action_set_hotkey(self.row["id"], key, on, speak)


class ModsDialog(FormDialog):
    """The keys held for every shortcut."""

    def __init__(self, parent, state):
        super().__init__(parent, "Keys to hold for every shortcut", "Save",
                         "Keys not changed")
        self.boxes = []
        for mod in state.get("modifiers") or []:
            box = wx.CheckBox(self, label=mod["label"])
            box.SetValue(mod["id"] in (state.get("mods") or []))
            self.sizer.Add(box, 0, wx.ALL, 8)
            self.boxes.append((mod["id"], box))
        self.finish_layout()

    def request(self):
        mods = [mod for mod, box in self.boxes if box.GetValue()]
        return lambda: server.action_set_hotkey_mods(mods)


class AddDialog(FormDialog):
    """A new shortcut: the kind of command, the command, and its key."""

    def __init__(self, parent, state, catalogue):
        super().__init__(parent, "Add a shortcut", "Add", "Shortcut not added")
        self.groups = catalogue["groups"]
        self.category = labelled(self, self.sizer, "Command type", lambda p: wx.Choice(p))
        set_choices(self.category, [group["category"] for group in self.groups])
        self.category.SetSelection(0)
        self.category.Bind(wx.EVT_CHOICE, lambda _e: self._fill())
        self.command = labelled(self, self.sizer, "What it does", lambda p: wx.Choice(
            p, size=(360, -1)))
        self.keys = state.get("keys") or []
        self.key = labelled(self, self.sizer, "Key to press", lambda p: wx.Choice(p))
        set_choices(self.key, self.keys)
        free = first_free_key(state)
        if free in self.keys:
            self.key.SetSelection(self.keys.index(free))
        if not catalogue.get("complete"):
            _paragraphs(self, self.sizer, catalogue.get("message") or "")
        self._fill()
        self.finish_layout()

    def _commands(self):
        return self.groups[self.category.GetSelection()]["commands"]

    def _fill(self):
        set_choices(self.command, [command["label"] for command in self._commands()])
        self.command.SetSelection(0)

    def request(self):
        index = self.command.GetSelection()
        if index == wx.NOT_FOUND:
            return "Choose what the shortcut should do first."
        command = self._commands()[index]["id"]
        key_index = self.key.GetSelection()
        key = self.keys[key_index] if key_index != wx.NOT_FOUND else ""
        return lambda: server.action_add_hotkey(command, key)
