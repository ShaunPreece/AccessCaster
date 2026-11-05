"""The native window's desk state (Project/native/desk_state.py), offline: a view over the desk model.

A change in the desk model, rebuilt through `window_state.from_dump` as a read is, lands on the right
channel's record (the same record, updated in place), output mix, levels or master values, and only
a real change notifies; a structural change asks for a full read, and a recording state is passed
straight on. A page that reads the desk for itself hears the properties it watches, and
`pages.FollowDesk` reads it once per burst, never under a dialog, following only names the desk
publishes.

Run: python DevTools/test_native_desk_state.py
"""
from __future__ import annotations

import re
import sys

import _bootstrap  # puts Project/ on sys.path
import _a11y
import _testkit
import channels
import desk_live
import desk_model
import device_page
import devices
import network
import recorder
import rcp_transfer
import show_actions
import soundpad
import window_state
import wx
from _testkit import addr, as_device, check, check_true, hold, hold_desk
from native import desk_state, pages

LAYOUT = [0, 7, 2, 3, 10, 11, 1, 8, 9, 9]      # Mic 1 on strip 0, USB 1 on strip 1

app = wx.App()
owner = wx.Frame(None)
events = []


def rebuilt():
    """What the store does once a burst's rebuild comes back from its worker thread."""
    count = len(events)
    store._built(desk_state._from_model())
    return events[count:]


def pushed(key, name, value, kind):
    """A push of a value, as the listener hands it on: the desk model first."""
    path = tuple(addr(key))
    was = desk_model.apply_push(path, name, value, kind)
    store._pushed(desk_live.Push(path, name, value, kind, key, was))


with as_device(devices.PRO2):
    hold_desk(LAYOUT)
    store = desk_state.DeskState(owner)
    store.subscribe(lambda event, detail: events.append((event, detail)))
    # A full read, of the desk the model holds.
    store._read_done(desk_state._named(window_state.from_dump(desk_model.snapshot())))
    check("a full read notifies", events[-1][0], "read")
    mic = store._records["Mic1"]
    usb = store._records["USB1"]

    hold(rcp_transfer.channel_key(0), channels.MUTE_PROP, True, "bool")
    check("a mute on strip 0 is Mic1's", rebuilt(), [("changed", {"Mic1"})])
    check("on the record the page holds, updated in place", store._records["Mic1"] is mic, True)
    check_true("which now reads muted", mic["muted"])
    check("and not the channel on strip 1", usb["muted"], False)
    check("a rebuild with nothing changed says nothing", rebuilt(), [])

    hold(rcp_transfer.output_key(8), "outputMixMinus", 2, "int")
    check("an output's mode is a change to that output's mix", rebuilt(), [("mix", {8})])
    hold(rcp_transfer.fader_key(6), "faderLevel", 127, "int")
    check("a virtual fader's level is a change to the levels", rebuilt(), [("levels", None)])
    check("in percent", next(f["percent"] for f in store.levels["faders"] if f["index"] == 6), 100)
    hold(rcp_transfer.MASTER, "masterCompellorOn", True, "bool")
    check("the master Compellor is a change to the master values", rebuilt(),
          [("master", None)])

    count = len(events)
    pushed(rcp_transfer.RECORDER, "recordState", recorder.RECORDING, "int")
    check("a recording state is passed on at once", events[count:],
          [("recorder", recorder.RECORDING)])
    check_true("and asks for no rebuild", store._rebuild is None)
    pushed(rcp_transfer.channel_key(1), channels.MUTE_PROP, True, "bool")
    check_true("a shown property asks for a rebuild", store._rebuild is not None)
    check_true("but not a full read", store._reread is None)
    pushed(rcp_transfer.channel_key(0), "channelInputSource", 1, "int")
    check_true("a fader reassignment asks for a full read as well", store._reread is not None)
    store.stop()

    # A page that reads the desk for itself watches what it shows, through the same follower.
    watcher = desk_state.DeskState(owner)
    watcher._read_done(desk_state._named(window_state.from_dump(desk_model.snapshot())))
    heard = []
    watcher.watch({"screenBrightness", channels.MUTE_PROP}, lambda: heard.append(True))
    store = watcher
    pushed(rcp_transfer.SYSTEM, "screenBrightness", 120, "int")
    check("a watched property is passed to its watcher", heard, [True])
    check_true("and asks for no rebuild when the window does not show it", store._rebuild is None)
    store._pushed(desk_live.Push((0,), "padActive", True, "bool", None, None))
    check("a property nobody watches is not passed on", heard, [True])
    pushed(rcp_transfer.channel_key(0), channels.MUTE_PROP, True, "bool")
    check("a property the window shows as well reaches the watcher", heard, [True, True])
    check_true("and still rebuilds the window", store._rebuild is not None)
    watcher.stop()

# FollowDesk: one read once a burst of pushes has stopped, and none while a dialog is open.
class _Desk:
    def watch(self, names, fn):
        self.names, self.fn = names, fn


owner.desk = _Desk()
reads = []
follow = pages.FollowDesk(owner, {"padName"}, lambda: reads.append(True))
check("it watches the names it is given", owner.desk.names, {"padName"})
owner.desk.fn()
first = follow._timer
owner.desk.fn()
check_true("a push in a burst restarts the one timer", follow._timer is first and first.IsRunning())
follow._due()
check("and the read is made when it fires", reads, [True])
pages.modal_open = lambda: True
follow._due()
check("a read falling due while a dialog is open waits", reads, [True])
check_true("for the dialog to close", follow._timer.IsRunning())
follow._timer.Stop()

# Every property a page follows is one the desk publishes: a misspelt name follows nothing.
with open(_bootstrap.docs_path("PROPERTY_LIST_PRO2.md"), encoding="utf-8") as f:
    published = set(re.findall(r"`(\w+)`", f.read()))
for label, names in (("Hardware", device_page.HARDWARE_PROPS),
                     ("Desk settings", device_page.UNIT_SETTING_PROPS),
                     ("Wireless connections", network.PROPS),
                     ("Backups", show_actions.SHOW_STATE_PROPS),
                     ("SMART pads", soundpad.PAD_GRID_PROPS)):
    check(f"{label} follows only published properties", sorted(set(names) - published), [])

owner.Destroy()
_a11y.check_windows("Desk state")
sys.exit(_testkit.finish())
