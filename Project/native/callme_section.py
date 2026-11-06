"""CallMe: RODE's remote guests. The call that is up or ringing, making a call and the contacts,
taking incoming calls, and the plan.

The state is the CallMe listener's (`callme_actions.watcher`), which hears every change the unit
pushes. The section reads the whole of it (`callme_actions.read`, one model dump) after the desk
state's first read, on F5 and after a fix; while one of its pages is shown it asks the listener
(`callme_actions.read_live`, no device read) every `POLL_MS`, and redraws only when its `version`
moves. Every sentence comes from `callme_actions` or `callme`.

Whether CallMe 1 is on a fader is the one fact taken from the window's desk state instead
(`problems`): the listener does not follow the faders, and the desk state does.

A call ringing or ending is spoken by the listener; with announcements off, a new ringing call is
said here instead, while a CallMe page is shown.
"""
import wx

import callme
import callme_actions
import faders
from native import a11y
from native.calls import call
from native.controls import Switch
from native.dialogs import confirm
from native.pages import Page, labelled, page_body, read_only, set_text

TITLE = "CallMe"
UNREAD = "CallMe has not been read yet."
BUSY = "One moment: the desk is still carrying out your last press."
POLL_MS = 1000
NO_LINK = "There is no guest link while incoming calls are off."


def lines(rows):
    """[{label, value}] as one sentence per line."""
    return "\n".join(f"{row['label']}: {row['value']}." for row in rows or [])


def contact_label(contact):
    return f"{contact['name']}, code {contact['spoken']}"


def problems(data, desk_faders):
    """What stops a call working. Whether CallMe 1 is on a fader comes from the window's fader
    layout, the one Fader assignment shows, which follows a change made anywhere."""
    if not data:
        return []
    on_fader = data.get("callme1_code") in desk_faders["layout"] if desk_faders else None
    return [r for r in callme.readiness(data.get("online"), on_fader) if not r["ok"]]


def _button(parent, row, label, handler):
    button = wx.Button(parent, label=label)
    button.Bind(wx.EVT_BUTTON, handler)
    row.Add(button, 0, wx.RIGHT, 8)
    return button


class CallMeSection:
    title = TITLE
    route = "callme"

    def __init__(self, frame):
        self.frame = frame
        self.data = None             # the last payload (callme_actions.payload)
        self.busy = False            # a press from this section has not returned
        self._seen = None            # the listener's `version` last drawn
        self._ringing = None         # the id of the call last seen ringing
        self._reading = False
        self._again = None
        self._polling = False
        self.call_page = CallPage(self)
        self.dial_page = DialPage(self)
        self.incoming_page = IncomingPage(self)
        self.plan_page = PlanPage(self)
        self.views = [self.call_page, self.dial_page, self.incoming_page, self.plan_page]
        self.pages = [Page("call", "Current call", self.call_page),
                      Page("dial", "Make a call", self.dial_page),
                      Page("incoming", "Incoming calls", self.incoming_page),
                      Page("plan", "Plan", self.plan_page)]
        self._timer = wx.Timer(self.call_page)
        self.call_page.Bind(wx.EVT_TIMER, lambda _e: self.poll(), self._timer)
        for view in self.views:
            view.Bind(wx.EVT_SHOW, self._on_show)
        self.call_page.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        frame.desk.subscribe(self._desk_event)

    def refresh(self, announce):
        self.read(announce)

    def _desk_event(self, event, _detail):
        if event == "read" and self.data is None and not self._reading:
            self.read()
        elif event in ("read", "faders") and self.data is not None:
            self.call_page.show_ready()

    def read(self, announce=False):
        if self._reading:
            self._again = bool(self._again) or announce
            return
        self._reading = True
        call(self.frame, callme_actions.read, done=lambda reply: self._read_done(reply, announce))

    def _read_done(self, reply, announce):
        self._reading = False
        if reply.get("ok"):
            self._ringing = (reply.get("incoming") or {}).get("id")
            self.show(reply)
            if announce:
                self.frame.say(reply.get("now") or "CallMe read.")
        else:
            if self.data is None:
                self.call_page.set_now(reply.get("message") or "Could not read CallMe.")
            if announce:
                self.frame.say(reply.get("message") or "Could not read CallMe.")
        if self._again is not None:
            again, self._again = self._again, None
            self.read(again)

    def show(self, data):
        self.data = data
        self._seen = data.get("version")
        for view in self.views:
            view.show()

    # ---- the listener, asked while a page is shown -----------------------------------------

    def _on_show(self, event):
        event.Skip()
        if any(view.IsShown() for view in self.views):
            if not self._timer.IsRunning():
                self._timer.Start(POLL_MS)
                self.poll()
        else:
            self._timer.Stop()

    def _on_destroy(self, event):
        event.Skip()
        self._timer.Stop()

    def poll(self):
        if self._polling or self.data is None:
            return
        self._polling = True
        call(self.frame, callme_actions.read_live, done=self._polled)

    def _polled(self, reply):
        self._polling = False
        if not reply.get("ok") or (reply.get("version") is not None
                                   and reply.get("version") == self._seen):
            return
        incoming = reply.get("incoming")
        ringing = incoming.get("id") if incoming else None
        # The listener speaks a ringing call while announcements are on.
        if ringing is not None and ringing != self._ringing and not reply.get("announcing"):
            self.frame.say(f"{incoming.get('name') or 'A guest'} is calling.")
        self._ringing = ringing
        self.show(reply)

    # ---- presses ---------------------------------------------------------------------------

    def act(self, source, action, *args, working=""):
        """A press: `action(*args)` off the UI thread, its reply spoken from `source`, then the
        listener asked at once, since the reply can change which buttons apply."""
        if self.data is None:
            self.frame.say(UNREAD)
            return
        if self.busy:
            self.frame.say(BUSY)
            return
        self.busy = True
        if working:
            self.frame.say(working)

        def done(reply):
            self.busy = False
            self.frame.reply(source, reply)
            self._seen = None
            self.poll()
        call(self.frame, action, *args, done=done)

    def field(self, key):
        """`current` and `remember` for `key` in the payload."""
        def current():
            return self.data.get(key) if self.data else None

        def remember(value):
            if self.data:
                self.data[key] = value
        return current, remember

    def unread(self):
        return UNREAD if self.data is None else None


class CallPage(wx.Panel):
    """What is wrong before a call can work, with the fix beside it; then the call that is up or
    ringing, and its details."""

    def __init__(self, section):
        super().__init__(section.frame.host)
        self.Hide()
        self.section = section
        self.frame = section.frame
        self._ready_state = None
        sizer, parent = page_body(self)

        # Shown only while something stops calls working: offline, or CallMe 1 on no fader. A
        # guest's call then never reaches the unit, and nothing on the unit says so.
        self.ready_sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.ready_sizer, 0, wx.EXPAND)
        self.ready = read_only(parent, self.ready_sizer, "Not ready for calls", height=80)
        self.wifi = wx.Button(parent, label="Go to Wireless connections")
        a11y.describe(self.wifi, "Device, Wireless connections: join a wifi network there.")
        self.wifi.Bind(wx.EVT_BUTTON, lambda _e: self.frame.go_to("status", "network"))
        self.ready_sizer.Add(self.wifi, 0, wx.ALL, 6)
        self.fader = labelled(parent, self.ready_sizer, "Fader for CallMe 1",
                              lambda p: wx.Choice(p))
        self.put = wx.Button(parent, label="Put CallMe 1 on this fader")
        a11y.describe(self.put, "The channel on that fader comes off it.")
        self.put.Bind(wx.EVT_BUTTON, self._on_put)
        self.ready_sizer.Add(self.put, 0, wx.ALL, 6)

        self.now = read_only(parent, sizer, "Now")
        self.now.SetValue("Reading the desk.")
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.answer = _button(parent, row, "Answer", self._on_answer)
        a11y.describe(self.answer, "Answers the call that is ringing. With nothing ringing, "
                                   "extends a call that is about to end.")
        # One fixed label: `callme_actions.action_end` decides at the press, so a call that began ringing a moment
        # ago is declined rather than the guest already on the line being cut off.
        self.end = _button(parent, row, "Decline or hang up", self._on_end)
        a11y.describe(self.end, "Declines the call that is ringing, or else hangs up the call "
                                "that is up.")
        self.extend = _button(parent, row, "Extend the call", self._on_extend)
        a11y.describe(self.extend, "Offered in the last five minutes of a call on a plan with an "
                                   "hour limit.")
        sizer.Add(row, 0, wx.ALL, 6)
        self.details = read_only(parent, sizer, "Call details", height=80)
        self._show_ready([])

    def set_now(self, text):
        set_text(self.now, text)

    def show(self):
        data = self.section.data
        self.set_now(data.get("now") or "")
        set_text(self.details, lines(data.get("details")) or "No call is up.")
        self.show_ready()

    def show_ready(self):
        self._show_ready(problems(self.section.data, self.section.frame.desk.faders))

    def _show_ready(self, wrong):
        keys = {r["key"] for r in wrong}
        if wrong:
            count = "One thing needs" if len(wrong) == 1 else f"{len(wrong)} things need"
            set_text(self.ready, f"{count} sorting before calls work.\n"
                     + "\n".join(r["text"] for r in wrong))
            self._fill_faders()
        state = (bool(wrong), "online" in keys, "fader" in keys)
        if state == self._ready_state:
            return
        self._ready_state = state
        shown, wifi, fader = state
        # A control about to be hidden under the focus hands it on first, or Windows drops it
        # somewhere of its own choosing.
        focus = wx.Window.FindFocus()
        going = {self.ready: shown, self.wifi: shown and wifi, self.fader: shown and fader,
                 self.put: shown and fader}
        if focus in going and not going[focus]:
            self.now.SetFocus()
        self.ready_sizer.ShowItems(shown)
        if shown:
            self.wifi.Show(wifi)
            self.fader.GetContainingSizer().ShowItems(fader)
            self.put.Show(fader)
        self.Layout()

    def _fill_faders(self):
        desk_faders = self.section.frame.desk.faders
        if not desk_faders:
            return
        labels = desk_faders["labels"]
        names = [f"Fader {i + 1}, currently {label}" for i, label in enumerate(labels)]
        if [self.fader.GetString(i) for i in range(self.fader.GetCount())] != names:
            chosen = self.fader.GetSelection()
            self.fader.Set(names)
            self.fader.SetSelection(chosen if 0 <= chosen < len(names) else wx.NOT_FOUND)

    def _on_put(self, _event):
        index = self.fader.GetSelection()
        if index == wx.NOT_FOUND:
            self.frame.say("Choose a fader first.")
            return
        desk_faders = self.section.frame.desk.faders
        code = self.section.data.get("callme1_code") if self.section.data else None
        if code is None or not desk_faders:
            self.frame.say(UNREAD)
            return
        gave_up = desk_faders["labels"][index]

        def done(reply):
            if not reply.get("ok"):
                self.frame.say(reply.get("message") or "That did not work.")
                return
            lost =("" if gave_up in (reply.get("labels") or [])
                    else f" {gave_up} is now on no fader.")
            # Worded here: `faders.action_set_fader`'s reply is the Routing page's account of the
            # layout.
            self.frame.say(f"CallMe 1 is on fader {index + 1}.{lost}")
            self.section.read()
        call(self, faders.action_set_fader, index, code, done=done)

    def _on_answer(self, event):
        self.section.act(event.GetEventObject(), callme_actions.action_answer)

    def _on_end(self, event):
        self.section.act(event.GetEventObject(), callme_actions.action_end)

    def _on_extend(self, event):
        self.section.act(event.GetEventObject(), callme_actions.action_extend)


class DialPage(wx.Panel):
    """Calling another unit by its code or a guest link, and the contacts on this computer."""

    def __init__(self, section):
        super().__init__(section.frame.host)
        self.Hide()
        self.section = section
        self.frame = section.frame
        self.contacts = []           # as listed; empty when the list holds a note
        sizer, parent = page_body(self)
        self.code = labelled(parent, sizer, "Their CallMe code", lambda p: wx.TextCtrl(
            p, size=(260, -1), style=wx.TE_PROCESS_ENTER))
        a11y.describe(self.code, "Or a guest link they have sent you. Enter calls.")
        self.code.Bind(wx.EVT_TEXT_ENTER, self._on_dial)
        self.dial = wx.Button(parent, label="Call")
        self.dial.Bind(wx.EVT_BUTTON, self._on_dial)
        sizer.Add(self.dial, 0, wx.ALL, 6)

        self.list = labelled(parent, sizer, "Contact", lambda p: wx.ListBox(
            p, choices=["Reading the contacts."], size=(420, 160)))
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.call_contact = _button(parent, row, "Call selected contact", self._on_call_contact)
        self.edit = _button(parent, row, "Edit selected contact", self._on_edit)
        self.delete = _button(parent, row, "Delete selected contact", self._on_delete)
        self.add = _button(parent, row, "Add a contact", self._on_add)
        sizer.Add(row, 0, wx.ALL, 6)

    def show(self, prefer=None):
        chosen = self._chosen()
        code = prefer or (chosen["code"] if chosen else None)
        self.contacts = self.section.data.get("contacts") or []
        labels = [contact_label(c) for c in self.contacts] or ["No contacts yet."]
        if [self.list.GetString(i) for i in range(self.list.GetCount())] != labels:
            self.list.Set(labels)
        codes = [c["code"] for c in self.contacts]
        self.list.SetSelection(codes.index(code) if code in codes else 0)

    def _chosen(self):
        index = self.list.GetSelection()
        return self.contacts[index] if self.contacts and index != wx.NOT_FOUND else None

    def _none_chosen(self):
        if self.section.data is None:
            self.frame.say(UNREAD)
            return True
        if self._chosen() is None:
            self.frame.say("No contacts yet. Add a contact first.")
            return True
        return False

    def _on_dial(self, _event):
        if not self.code.GetValue().strip():
            self.frame.say("Type their CallMe code first.")
            return
        self.section.act(self.dial, callme_actions.action_dial, self.code.GetValue(),
                         working="Calling, please wait.")

    def _on_call_contact(self, _event):
        if self._none_chosen():
            return
        contact = self._chosen()
        self.section.act(self.call_contact, callme_actions.action_dial, contact["code"],
                         working=f"Calling {contact['name']}, please wait.")

    def _on_add(self, _event):
        if self.section.data is None:
            self.frame.say(UNREAD)
            return
        self._edit_contact(None)

    def _on_edit(self, _event):
        if self._none_chosen():
            return
        self._edit_contact(self._chosen())

    def _edit_contact(self, contact):
        dialog = ContactDialog(self, contact)
        with dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            reply = dialog.reply
        self._contacts_changed(reply, prefer=reply.get("code"))
        self.list.SetFocus()
        self.frame.say(reply.get("message") or "Saved.")

    def _on_delete(self, _event):
        if self._none_chosen():
            return
        contact = self._chosen()
        if not confirm(self, f"Delete {contact['name']}?",
                       f"This removes {contact['name']} from the contacts on this computer. A call "
                       "that is up is not affected.", "Delete"):
            return

        def done(reply):
            self.frame.reply(self.delete, reply)
            if reply.get("ok"):
                self._contacts_changed(reply)
        call(self, callme_actions.action_contact_delete, contact["code"], done=done)

    def _contacts_changed(self, reply, prefer=None):
        if reply.get("contacts") is not None and self.section.data is not None:
            self.section.data["contacts"] = reply["contacts"]
            self.show(prefer)


class ContactDialog(wx.Dialog):
    """A contact's name and code, added or changed. A code `callme_actions.action_contact_save`
    cannot read is refused in a message box, and the dialog stays open until one is accepted; its
    reply is `reply`."""

    def __init__(self, parent, contact):
        super().__init__(parent, title=f"Edit {contact['name']}" if contact else "Add a contact")
        self._was = contact["code"] if contact else ""
        self._busy = False
        self.reply = None
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(self, label="Name"), 0, wx.LEFT | wx.TOP, 10)
        self.name_box = wx.TextCtrl(self, value=contact["name"] if contact else "",
                                    size=(320, -1))
        sizer.Add(self.name_box, 0, wx.ALL, 10)
        sizer.Add(wx.StaticText(self, label="CallMe code"), 0, wx.LEFT, 10)
        self.code_box = wx.TextCtrl(self, value=self._was, size=(320, -1))
        a11y.describe(self.code_box, "Or their guest link.")
        sizer.Add(self.code_box, 0, wx.ALL, 10)
        buttons = wx.StdDialogButtonSizer()
        ok = wx.Button(self, wx.ID_OK, label="Save")
        ok.SetDefault()
        buttons.AddButton(ok)
        buttons.AddButton(wx.Button(self, wx.ID_CANCEL, label="Cancel"))
        buttons.Realize()
        sizer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 10)
        self.SetSizerAndFit(sizer)
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)
        self.CentreOnParent()

    def _on_ok(self, _event):
        if self._busy:
            return
        self._busy = True
        call(self, callme_actions.action_contact_save, self.name_box.GetValue(),
             self.code_box.GetValue(), self._was, done=self._saved)

    def _saved(self, reply):
        self._busy = False
        if not reply.get("ok"):
            wx.MessageBox(reply.get("message") or "That contact could not be saved.",
                          "Contact not saved", wx.OK | wx.ICON_WARNING, self)
            return
        self.reply = reply
        self.EndModal(wx.ID_OK)


class IncomingPage(wx.Panel):
    """Taking calls: whether the unit hosts them, whether this computer rings, and the code and
    guest link to give a guest."""

    def __init__(self, section):
        super().__init__(section.frame.host)
        self.Hide()
        self.section = section
        self.frame = section.frame
        sizer, parent = page_body(self)
        self.hosting = Switch(
            self, parent, sizer, "Accept incoming calls", callme_actions.action_hosting,
            *section.field("hosting"),
            description="Switching it back on gives a new code, and a shared guest link stops "
                        "working.",
            guard=self._hosting_refused, accepted=self._hosted)
        self.ringtone = Switch(self, parent, sizer, "Ring for incoming calls",
                               callme_actions.action_ringtone, *section.field("ringtone"),
                               guard=section.unread)
        self.code = read_only(parent, sizer, "Your CallMe code", height=None, width=260)
        self.link = read_only(parent, sizer, "Guest link", height=None)
        a11y.describe(self.link, "Opens in any web browser. Nothing to install.")
        self.copy = wx.Button(parent, label="Copy the guest link")
        self.copy.Bind(wx.EVT_BUTTON, self._on_copy)
        sizer.Add(self.copy, 0, wx.ALL, 6)

    def show(self):
        data = self.section.data
        self.hosting.show()
        self.ringtone.show()
        set_text(self.code, data.get("code") or "")
        set_text(self.link, data.get("link") or "")

    def _hosting_refused(self):
        unread = self.section.unread()
        if unread:
            return unread
        if self.section.data.get("hosting_locked"):
            return "Incoming calls cannot be switched while a call is up."
        return None

    def _hosted(self, reply):
        # The reply is the whole page, with the new code.
        if reply.get("slots") is not None:
            self.section.show(reply)

    def _on_copy(self, _event):
        link = (self.section.data or {}).get("link")
        if not link:
            self.frame.say(self.section.unread() or NO_LINK)
            return
        if wx.TheClipboard.Open():
            try:
                wx.TheClipboard.SetData(wx.TextDataObject(link))
            finally:
                wx.TheClipboard.Close()
            self.frame.say("Guest link copied.")
        else:
            self.frame.say("The clipboard is in use by another program. Try again.")


class PlanPage(wx.Panel):
    """The CallMe plan's limits, and whether a call near its hour limit is extended for you."""

    def __init__(self, section):
        super().__init__(section.frame.host)
        self.Hide()
        self.section = section
        self.frame = section.frame
        sizer, parent = page_body(self)
        self.plan = read_only(parent, sizer, "Your plan", height=100)
        self.plan.SetValue("Reading the desk.")
        self.auto = Switch(self, parent, sizer, "Extend calls automatically",
                           callme_actions.action_auto_extend, *section.field("auto_extend"),
                           description="On plans with an hour limit, five minutes before the end.",
                           guard=section.unread)

    def show(self):
        set_text(self.plan, lines(self.section.data.get("plan")))
        self.auto.show()
