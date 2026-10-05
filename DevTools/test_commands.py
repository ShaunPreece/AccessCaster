"""Unit tests for the shortcut COMMAND dispatch (commands.py). NO DEVICE: `server` is a stub.

What it pins down: a toggle shortcut writes the OPPOSITE of the channel's real current state. The
mute toggle may answer from the announcer's push-fed cache; solo, processing and phantom have no
such cache and must read the channel, even when the mute cache is populated.

Run: python DevTools/test_commands.py
"""
from __future__ import annotations
import sys
import types

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import channels
import commands
import devices

devices.force_device(devices.PRO2)

FAILED = 0


def check(label, got, want):
    global FAILED
    if got == want:
        print(f"  ok   {label}: {got!r}")
    else:
        FAILED += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


# A stub server: Mic 1 is muted, soloed, processed and on phantom, and the announcer's cache knows
# only its mute.
writes = []
stub = types.ModuleType("server")
stub._ann_live_state = {"muted": {channels.channel_strip_index("Mic1"): True}}
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

stub._ann_live_state = None
writes.clear()
commands.run("chan.mute:Mic1")
check("mute with no announcer cache falls back to a read", writes, [("mute", False)])

print()
print("FAILURES:", FAILED)
sys.exit(1 if FAILED else 0)
