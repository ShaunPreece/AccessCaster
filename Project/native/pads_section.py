"""SMART pads: a page listing every pad, where one is chosen and then either its settings edited
or what it does changed, each in a dialog named with the pad (`pad_dialogs`); Voice effects
(`pad_fx`); and Pad maintenance (`pad_maint`).

The pads are not part of the window's desk state: they come from a model dump of their own
(`pad_actions.soundpad_model`, about 1.5 s), read once the desk state has first read the desk, on F5,
after a change made here, and once the desk has pushed a change to a pad (`FollowDesk`). Never on
show, like every page.
"""
import wx

import pad_actions
import soundpad
from native import a11y
from native.calls import call
from native.pad_dialogs import ChangeDialog, SettingsDialog, list_label, where
from native.pad_fx import FxPage
from native.pad_maint import MaintPage, run_job
from native.pages import (FollowDesk, MessagePage, Page, labelled, page_body, read_only, select,
                          set_items, set_text)

TITLE = "SMART pads"
PADS = "Pads"
# Every page is built from the first good read; until then each is a sentence saying why it is not
# there. As (key, title).
PAGES = [("pads", "Pads"), ("fx", "Voice effects"), ("maint", "Pad maintenance")]
# How long after a Settings dialog closes the pads are read again: past `controls._Field.DELAY_MS`,
# so a write still waiting to go out has been sent first.
REREAD_AFTER_MS = 600


class PadsSection:
    title = TITLE
    route = "pads"

    def __init__(self, frame):
        self.frame = frame
        self.model = None
        self.page = None
        self.fx = None
        self.maint = None
        self._reading = False
        self._again = None       # a read asked for while one runs: None, or whether to announce it
        self.statuses = [MessagePage(frame.host, "Starting AccessCaster.",
                                     button="Read the pads again", on_button=lambda: self.read(True))
                         for _page in PAGES]
        self.pages = [Page(key, title, status) for (key, title), status in zip(PAGES, self.statuses)]
        frame.desk.subscribe(self._desk_event)
        FollowDesk(frame, soundpad.PAD_GRID_PROPS, self.read)

    def start(self):
        self._set_status("Reading the pads from the desk.")

    def _set_status(self, text):
        for status in self.statuses:
            status.set_text(text)

    def refresh(self, announce):
        self.read(announce)

    def _desk_event(self, event, reply):
        # The first read waits for the desk state's, so Channels fill first; a desk that was not
        # there at start is read when it arrives.
        if event == "read" and reply.get("ok") and self.model is None:
            self.read()

    def read(self, announce=False):
        if self._reading:
            self._again = bool(self._again) or announce
            return
        self._reading = True
        call(self.frame, pad_actions.soundpad_model, done=lambda model: self._read_done(model, announce))

    def _read_done(self, model, announce):
        self._reading = False
        self.show(model, announce)
        if self._again is not None:
            again, self._again = self._again, None
            self.read(again)

    def show(self, model, announce=False):
        """Put a pad read on the page. A read that failed leaves the last good one in place."""
        # A failed read still answers, with no pads and the reason in its health message.
        if not model.get("pads"):
            text = model.get("health_message") or "Could not read the pads."
            if self.page is None:
                self._set_status(text)
            elif announce:
                self.frame.say(text)
            return
        self.model = model
        if self.page is None:
            self.page, self.fx, self.maint = PadsPage(self), FxPage(self), MaintPage(self)
            focus = wx.Window.FindFocus()
            if focus is not None and any(status.IsDescendant(focus) for status in self.statuses):
                self.frame.tree.SetFocus()
            self.pages = [Page(key, title, window) for (key, title), window
                          in zip(PAGES, (self.page, self.fx, self.maint))]
            self.frame.pages_changed(self)
        else:
            for window in (self.page, self.fx, self.maint):
                window.show()
        if announce:
            self.frame.say("Pads read from the desk.")


class PadsPage(wx.Panel):
    """The list of pads, each read as bank, number and what is on it, and the two things done to the
    one chosen."""

    def __init__(self, section):
        super().__init__(section.frame.host)
        self.Hide()
        self.section = section
        self.frame = section.frame
        sizer, parent = page_body(self)
        self.list = labelled(parent, sizer, "Pad", lambda p: wx.ListBox(p, size=(420, 300)))
        a11y.describe(self.list, "Press Enter to edit a pad's settings, or to give an empty pad "
                                 "something to do.")
        # On the list itself, so Enter is the list's before the page or the window sees it.
        self.list.Bind(wx.EVT_CHAR_HOOK, self._on_list_key)
        self.list.Bind(wx.EVT_LISTBOX_DCLICK, lambda _e: self._open())
        row = wx.BoxSizer(wx.HORIZONTAL)
        edit = wx.Button(parent, label="Edit this pad's settings")
        edit.Bind(wx.EVT_BUTTON, lambda _e: self._edit())
        row.Add(edit, 0, wx.RIGHT, 8)
        change = wx.Button(parent, label="Change what this pad does")
        change.Bind(wx.EVT_BUTTON, lambda _e: self._change())
        row.Add(change, 0)
        sizer.Add(row, 0, wx.ALL, 6)
        # Why pads cannot be changed, when they cannot: a read-only desk, or an effect pool the
        # desk needs tidied first. Hidden when there is nothing to say.
        self.grids = []          # the grid of each list item, as last shown
        self.note = read_only(parent, sizer, "Why the pads cannot be changed", height=80)
        self.note_label = self.note.GetPrevSibling()
        self.show()

    @property
    def model(self):
        return self.section.model

    def show(self):
        """Put the section's pad read on the list, keeping the chosen pad."""
        index = self.list.GetSelection()
        grid = self.grids[index] if index != wx.NOT_FOUND else None
        pads = self.model["pads"]
        set_items(self.list, [list_label(pad) for pad in pads])
        self.grids = [pad["grid"] for pad in pads]
        select(self.list, self.grids.index(grid) if grid in self.grids else 0)
        notes = []
        if self.model.get("read_only"):
            notes.append("Editing SMART pads is not enabled on this device yet, so pads can be "
                         "looked at here but not changed.")
        if self.model.get("health_message"):
            notes.append(self.model["health_message"])
        set_text(self.note, " ".join(notes))
        if self.note.IsShown() != bool(notes):
            self.note.Show(bool(notes))
            self.note_label.Show(bool(notes))
            self.Layout()

    def _chosen(self):
        return self.model["pads"][self.list.GetSelection()]

    def _pad(self, grid):
        return next((pad for pad in self.model["pads"] if pad["grid"] == grid), None)

    def _on_list_key(self, event):
        if event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and not event.GetModifiers():
            self._open()
            return
        event.Skip()

    def _open(self):
        """The likely one: the settings, or what an empty pad does."""
        if self._chosen()["present"]:
            self._edit()
        else:
            self._change()

    def _edit(self):
        pad = self._chosen()
        if not pad["present"]:
            self.frame.say(f"{where(pad).capitalize()} is empty, so it has no settings. Change "
                           "what this pad does puts something on it.")
            return
        self._settings(pad)

    def _settings(self, pad):
        dialog = SettingsDialog(self, pad, self.model)
        with dialog:
            dialog.ShowModal()
            wrote = dialog.wrote
        self.show()
        self.list.SetFocus()
        if wrote:
            wx.CallLater(REREAD_AFTER_MS, self.section.read)

    def _change(self):
        pad = self._chosen()
        dialog = ChangeDialog(self, pad, self.model)
        with dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            plan = dialog.plan
        self._run(pad["grid"], plan)

    def _run(self, grid, plan):
        """Make the change, then open the new pad's settings from the read made after it."""
        result = run_job(self, f"Changing {where(self._pad(grid))}", plan.working, plan.action,
                         *plan.args)
        self._changed(grid, result, plan)

    def _changed(self, grid, result, plan):
        """A change has ended: show the pads read after it, and open the new pad's settings."""
        reply = result["reply"]
        if result.get("model"):
            self.section.show(result["model"])
        message = reply.get("message") or ("Done." if reply.get("ok") else "That did not work.")
        pad = self._pad(grid)
        if not reply.get("ok") or plan.clears or pad is None or not pad["present"]:
            self.list.SetFocus()
            self.frame.say(message)
            return
        # After the job dialog has gone and the focus has settled back: opened at once, Settings
        # comes up while the focus is still moving and the screen reader never hears its first
        # control.
        wx.CallAfter(self._settings, pad)
