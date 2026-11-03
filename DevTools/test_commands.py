"""Unit tests for the shortcut COMMAND dispatch (commands.py). NO DEVICE: `server` is a stub.

What it pins down: a toggle shortcut writes the OPPOSITE of the channel's real current state. The
mute toggle may answer from the announcer's push-fed cache; solo, processing and phantom have no
such cache and must read the channel, even when the mute cache is populated. And the Open
AccessCaster shortcut goes to the tray's window wherever one is hosted.

Run: python DevTools/test_commands.py
"""
from __future__ import annotations

import sys
import types

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
import announcer
import channels
import commands
import devices
from _testkit import check

devices.force_device(devices.PRO2)
# The factory layout, held as if read, so the channel lookups make no read of the desk.
channels.publish_strip_vector(list(devices.PRO2_FADERS.default_vector))


# A stub server: Mic 1 is muted, soloed, processed and on phantom, and the announcer's cache knows
# only its mute.
writes = []
stub = types.ModuleType("server")
announcer.live_state = {"muted": {channels.channel_strip_index("Mic1"): True}}
stub.action_get_channels = lambda: {"channels": [
    {"name": "Mic1", "muted": True, "solo": True, "processing_on": True, "phantom": True}]}
stub.action_set_channel_mute = lambda name, on: writes.append(("mute", on)) or {}
stub.action_set_channel_solo = lambda name, on: writes.append(("solo", on)) or {}
stub.action_set_processing_enabled = lambda name, on: writes.append(("proc", on)) or {}
stub.action_set_phantom = lambda mic, on: writes.append(("phantom", on)) or {}
sys.modules["server"] = stub

print("CHANNEL TOGGLES write the opposite of the real state")
for cmd in ("mute", "solo", "proc", "phantom"):
    writes.clear()
    commands.run(f"chan.{cmd}:Mic1")
    check(f"{cmd} on a channel that has it ON turns it off", writes, [(cmd, False)])

announcer.live_state = None
writes.clear()
commands.run("chan.mute:Mic1")
check("mute with no announcer cache falls back to a read", writes, [("mute", False)])

print("\nOPEN ACCESSCASTER goes to the tray's window")
import app_window  # noqa: E402

app_window.show = lambda: True
check("a raised window is silent", commands.run("app.window")["message"], "")
app_window.show = lambda: False
check("a refused raise says where the window is", "Alt+Tab" in commands.run("app.window")["message"],
      True)
app_window.show = None
check("a run with no window says nothing", commands.run("app.window")["message"], "")

print()
sys.exit(_testkit.finish())
