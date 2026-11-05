"""The native window's CallMe (Project/native/callme_section.py), offline: the call, its details,
the plan and the code are shown from callme_actions' payload, the Not ready group appears with only
the fixes that apply, a poll redraws only when the listener's version moves and says a new ringing
call only while announcements are off, each press calls the right action, a contact is
called, saved and deleted by its code, and hosting is refused mid-call. Nothing is shown and
nothing touches a desk.

Run: python DevTools/test_native_callme.py
"""
from __future__ import annotations

import sys

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _a11y
import _testkit
import callme_actions
import server
import wx
from _testkit import check, check_true, patched
from native import callme_section

app = wx.App()


class FakeDesk:
    def __init__(self):
        self.faders = ON_FADER

    def subscribe(self, fn):
        self.listener = fn


class FakeFrame:
    def __init__(self):
        self.window = wx.Frame(None)
        self.host = wx.Panel(self.window)
        self.host.SetSizer(wx.BoxSizer(wx.VERTICAL))
        self.desk = FakeDesk()
        self.said = []
        self.opened = []

    def say(self, text, recent=False):
        self.said.append(text)

    def reply(self, source, reply, recent=False):
        self.said.append(reply.get("message"))

    def go_to(self, route, key):
        self.opened.append((route, key))


ON_FADER = {"labels": ["Mic 1", "Mic 2", "CallMe 1"], "layout": [0, 1, 16], "stereo": [],
            "model": {}}
OFF_FADER = {"labels": ["Mic 1", "Mic 2", "Bluetooth"], "layout": [0, 1, 10], "stereo": [],
             "model": {}}


def payload(version=1, **values):
    data = {"ok": True, "version": version, "now": "No call.", "details": [], "plan": [
        {"label": "Plan", "value": "Free"}], "online": True, "hosting": True,
        "hosting_locked": False, "code": "ABC123", "link": "https://example/ABC123",
        "ringtone": True, "auto_extend": False, "incoming": None, "announcing": True,
        "callme1_code": 16, "slots": [],
        "contacts": [{"name": "Ann", "code": "AAA111", "spoken": "A A A 1 1 1"},
                     {"name": "Bob", "code": "BBB222", "spoken": "B B B 2 2 2"}]}
    data.update(values)
    return data


sync = (callme_section, {"call": lambda _o, fn, *a, done=None: done(fn(*a))})
frame = FakeFrame()
section = callme_section.CallMeSection(frame)
section._timer.Stop()
reads = []
section.read = lambda announce=False: reads.append(announce)
call_page, dial, incoming, plan = (section.call_page, section.dial_page, section.incoming_page,
                                   section.plan_page)

# ---- before a read ---------------------------------------------------------------------------
call_page._on_answer(wx.CommandEvent())
check("a press before a read is refused", frame.said[-1], callme_section.UNREAD)
check_true("and Not ready for calls is hidden", not call_page.ready.IsShown())

# ---- the payload on the pages ----------------------------------------------------------------
section.show(payload(details=[{"label": "Bitrate", "value": "64 kbps"}]))
check("the call line", call_page.now.GetValue(), "No call.")
check("the details, a sentence a line", call_page.details.GetValue(), "Bitrate: 64 kbps.")
check("the plan", plan.plan.GetValue(), "Plan: Free.")
check("the code", incoming.code.GetValue(), "ABC123")
check("the link", incoming.link.GetValue(), "https://example/ABC123")
check("hosting", incoming.hosting.box.GetValue(), True)
check("the contacts, each with its code spelt",
      [dial.list.GetString(i) for i in range(dial.list.GetCount())],
      ["Ann, code A A A 1 1 1", "Bob, code B B B 2 2 2"])
section.show(payload(details=[]))
check("no call up says so", call_page.details.GetValue(), "No call is up.")

# ---- not ready -------------------------------------------------------------------------------
frame.desk.faders = OFF_FADER
frame.desk.listener("faders", None)
check_true("CallMe 1 taken off its fader shows Not ready, from the desk state alone",
           call_page.ready.IsShown())
check_true("with the fader fix", call_page.put.IsShown())
check_true("and not the wifi one", not call_page.wifi.IsShown())
check("the faders are offered with what is on them", call_page.fader.GetString(2),
      "Fader 3, currently Bluetooth")
call_page.fader.SetSelection(1)
with patched(sync, (server, {"action_set_fader": lambda index, code: {
        "ok": True, "labels": ["Mic 1", "CallMe 1", "Bluetooth"], "message": "Done."}})):
    reads.clear()
    call_page._on_put(None)
check("putting CallMe 1 on a fader says where, and who lost it", frame.said[-1],
      "CallMe 1 is on fader 2. Mic 2 is now on no fader.")
check_true("and CallMe is read again", reads)
frame.desk.faders = ON_FADER
frame.desk.listener("read", {"ok": True})
check_true("put back on a fader, a full read of the desk hides it",
           not call_page.ready.IsShown())
section.show(payload(online=False))
check_true("offline shows the wifi fix", call_page.wifi.IsShown())
check_true("and not the fader one", not call_page.put.IsShown())
call_page.wifi.Command(wx.CommandEvent(wx.wxEVT_BUTTON))
check("which goes to Device, Wireless connections", frame.opened, [("status", "network")])
section.show(payload())
check_true("ready hides Not ready", not call_page.ready.IsShown())

# ---- the poll --------------------------------------------------------------------------------
redraws = []
real_show = section.show
section.show = lambda data: redraws.append(data["version"]) or real_show(data)
section._polled(payload(version=1))
check("an unchanged version redraws nothing", redraws, [])
section._polled(payload(version=2, incoming={"id": 7, "name": "Ann"}))
check("a new version redraws", redraws, [2])
check("a ringing call is not said while the listener says it", frame.said[-1] == "Ann is calling.",
      False)
section._polled(payload(version=3, incoming={"id": 8, "name": "Bob"}, announcing=False))
check("with announcements off, a new ringing call is said here", frame.said[-1], "Bob is calling.")
count = len(frame.said)
section._polled(payload(version=4, incoming={"id": 8, "name": "Bob"}, announcing=False))
check("but only once", len(frame.said), count)
section.show = real_show

# ---- presses ---------------------------------------------------------------------------------
pressed = []
actions = {name: (lambda name: lambda *a: pressed.append((name,) + a) or {
    "ok": True, "message": name})(name)
    for name in ("action_answer", "action_end", "action_extend", "action_dial",
                 "action_contact_delete")}
with patched(sync, (callme_actions, dict(actions, read_live=lambda: payload(version=9)))):
    call_page._on_answer(wx.CommandEvent())
    call_page._on_end(wx.CommandEvent())
    call_page._on_extend(wx.CommandEvent())
    dial.code.SetValue("ZZZ999")
    dial._on_dial(None)
    dial.list.SetSelection(1)
    dial._on_call_contact(None)
    with patched((callme_section, {"confirm": lambda *_a: True})):
        dial._on_delete(None)
check("each press calls its action", pressed,
      [("action_answer",), ("action_end",), ("action_extend",), ("action_dial", "ZZZ999"),
       ("action_dial", "BBB222"), ("action_contact_delete", "BBB222")])
check("and the listener is asked straight after", section._seen, 9)
dial.code.SetValue("")
dial._on_dial(None)
check("an empty code is refused", frame.said[-1], "Type their CallMe code first.")
section.busy = True
call_page._on_answer(wx.CommandEvent())
check("a second press while one runs is refused", frame.said[-1], callme_section.BUSY)
section.busy = False

dial._contacts_changed({"contacts": [{"name": "Cat", "code": "CCC333", "spoken": "C C C 3 3 3"},
                                     {"name": "Dan", "code": "DDD444", "spoken": "D D D 4 4 4"}]},
                       prefer="DDD444")
check("a saved contact is chosen in the new list", dial._chosen()["name"], "Dan")

section.show(payload(hosting_locked=True))
check("hosting cannot be switched mid-call", incoming._hosting_refused(),
      "Incoming calls cannot be switched while a call is up.")
section.show(payload(link=""))
incoming._on_copy(None)
check("no link to copy says why", frame.said[-1], callme_section.NO_LINK)

frame.window.Destroy()
_a11y.check_windows("CallMe")
sys.exit(_testkit.finish())
