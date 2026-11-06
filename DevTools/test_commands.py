"""Unit tests for the shortcut COMMAND dispatch (commands.py). NO DEVICE: the channel actions are stubs.

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
_testkit.hold_desk(list(devices.PRO2_FADERS.default_vector))


# Stub channel actions: Mic 1 is muted, soloed, processed and on phantom, and the announcer's cache
# knows only its mute.
writes = []
stub = types.ModuleType("channel_actions")
mutes = types.ModuleType("mute_solo")
announcer.live_state = {"muted": {channels.channel_strip_index("Mic1"): True}}
stub.action_get_channels = lambda: {"ok": True, "channels": [
    {"name": "Mic1", "muted": True, "solo": True, "processing_on": True, "phantom": True}]}
mutes.action_set_channel_mute = lambda name, on: writes.append(("mute", on)) or {}
mutes.action_set_channel_solo = lambda name, on: writes.append(("solo", on)) or {}
stub.action_set_processing_enabled = lambda name, on: writes.append(("proc", on)) or {}
stub.action_set_phantom = lambda mic, on: writes.append(("phantom", on)) or {}
commands.channel_actions = stub
commands.mute_solo = mutes

print("CHANNEL TOGGLES write the opposite of the real state")
for cmd in ("mute", "solo", "proc", "phantom"):
    writes.clear()
    commands.run(f"chan.{cmd}:Mic1")
    check(f"{cmd} on a channel that has it ON turns it off", writes, [(cmd, False)])

announcer.live_state = None
writes.clear()
commands.run("chan.mute:Mic1")
check("mute with no announcer cache falls back to a read", writes, [("mute", False)])

print("\nA TOGGLE WHOSE READ FAILS refuses rather than guessing")
import device_page  # noqa: E402
import network  # noqa: E402

failed_read = {"ok": False, "message": "Could not read the desk."}
good_read = stub.action_get_channels
stub.action_get_channels = lambda: failed_read
for cmd in ("mute", "solo", "proc", "phantom"):
    writes.clear()
    res = commands.run(f"chan.{cmd}:Mic1")
    check(f"{cmd} with a failed read writes nothing", writes, [])
    check(f"{cmd} with a failed read says why", (res["ok"], res["message"]),
          (False, failed_read["message"]))
stub.action_get_channels = good_read
with _testkit.patched((device_page, {"read_unit_settings": lambda: failed_read}),
                      (network, {"read_status": lambda: failed_read})):
    for cid in ("out.mute:monitor", "unit.midi", "unit.wifi", "unit.bt"):
        check(f"{cid} with a failed read fails", commands.run(cid)["ok"], False)

print("\nOPEN ACCESSCASTER goes to the tray's window")
import app_window  # noqa: E402

app_window.show = lambda: True
check("a raised window is silent", commands.run("app.window")["message"], "")
app_window.show = lambda: False
check("a refused raise says where the window is", "Alt+Tab" in commands.run("app.window")["message"],
      True)
app_window.show = None
check("a run with no window says nothing", commands.run("app.window")["message"], "")

print("\nA LEVEL SHORTCUT moves the virtual fader it names, from where it is")
import desk_model  # noqa: E402
import faders  # noqa: E402
import rcp_transfer  # noqa: E402

offered = [c["id"] for c in commands._level_commands() if c["id"].startswith("level.fader.up")]
check("only the virtual faders are offered", offered,
      ["level.fader.up:7", "level.fader.up:8", "level.fader.up:9"])
levels = []
_testkit.hold(rcp_transfer.fader_key(6), "faderLevel", 64, "int")
_testkit.hold(rcp_transfer.fader_key(7), "faderLevel", 0, "int")
with _testkit.patched((faders, {"action_set_fader_level":
                                lambda index, pct: levels.append((index, pct)) or {}})):
    commands.run("level.fader.up:7")
expected = round(desk_model.fader_level(6) * 100) + commands.LEVEL_STEP
check("fader 7 is index 6, stepped from its own level", levels, [(6, expected)])

print()
sys.exit(_testkit.finish())
