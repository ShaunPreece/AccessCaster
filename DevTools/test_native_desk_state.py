"""The native window's desk state (Project/native/desk_state.py), offline: pushed frames, decoded
by rcp_read.parse_push as the listener decodes them, land on the right channel's record, and only
a real change notifies.

Run: python DevTools/test_native_desk_state.py
"""
from __future__ import annotations

import struct
import sys

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
import devices
import rcp_read
import rcp_transfer
import wx
from _testkit import as_device, check, check_true, push
from native import desk_state


def v_bool(on: bool) -> bytes:
    return bytes([0x01, 0x01, 0x02 if on else 0x03])


def v_int(value: int) -> bytes:
    return bytes([0x01, 0x05, 0x01]) + int(value).to_bytes(4, "little", signed=True)


def v_float(value: float) -> bytes:
    return bytes([0x01, 0x09, 0x04]) + struct.pack("<d", value)


def record(name, kind):
    return {"name": name, "label": name, "kind": kind, "muted": False, "solo": False,
            "processing_on": True, "modules": {"eqOn": False}, "params": {"eqLowGain": 0.5},
            "level_db": 40 if kind == "mic" else (None if kind == "bus" else -12),
            "effects": {"echo": False, "echo_params": {"echoMix": 0.5}}}


app = wx.App()
owner = wx.Frame(None)
events = []

with as_device(devices.PRO2):
    store = desk_state.DeskState(owner)
    store.subscribe(lambda event, detail: events.append((event, detail)))
    # Faders: Bluetooth (source 10) on strip 0, Mic1 (0) on strip 1, the pads bus (11) on strip 2.
    store._read_done({"ok": True, "strips": [10, 0, 11],
                      "channels": [record("Mic1", "mic"), record("Bluetooth", "line"),
                                   record("SMARTpads", "bus")]})
    check("a full read notifies", events[-1][0], "read")
    mic, bt, bus = (store._records[n] for n in ("Mic1", "Bluetooth", "SMARTpads"))

    def feed(path, name, block):
        rec = rcp_read.parse_push(push(path, name, block))
        check_true(f"{name} decodes as a push", rec is not None)
        store._apply(*rec)

    ch = rcp_transfer.SEL_CHANNEL
    fx = rcp_transfer.SEL_CHANNEL_FX
    base = devices.PRO2.input_table_base

    feed([ch + 1], "channelOutputMute", v_bool(True))
    check("a mute on strip 1 lands on Mic1", mic["muted"], True)
    check("and not on the channel on strip 0", bt["muted"], False)
    feed([ch + 0], "channelCueEnable", v_bool(True))
    check("a solo on strip 0 lands on Bluetooth", bt["solo"], True)
    feed([ch + 1], "channelBypassProcessing", v_bool(True))
    check("bypass is processing off", mic["processing_on"], False)
    feed([ch + 1], "eqOn", v_bool(True))
    check("a module's state", mic["modules"]["eqOn"], True)
    feed([ch + 1], "eqLowGain", v_float(0.25))
    check("a parameter keeps the desk's own 0..1", mic["params"]["eqLowGain"], 0.25)
    feed([fx + 1], "echoOn", v_bool(True))
    check("an effect is addressed on the effects block", mic["effects"]["echo"], True)
    feed([fx + 1], "echoMix", v_float(0.75))
    check("an effect parameter", mic["effects"]["echo_params"]["echoMix"], 0.75)
    feed([base + 0], "inputMicrophoneGain", v_int(55))
    check("a mic's gain is its level, addressed at its input", mic["level_db"], 55)
    feed([base + 0], "inputPower", v_int(1))
    check("phantom", mic["phantom"], True)
    feed([base + 10], "inputDigitalGain", v_int(-6))
    check("a line channel's digital gain is its level", bt["level_db"], -6)
    feed([base + 0], "inputDigitalGain", v_int(3))
    check("a mic's digital gain is not its level", mic["level_db"], 55)
    feed([base + 11], "inputDigitalGain", v_int(3))
    check("the bus has no level", bus["level_db"], None)
    feed([ch + 1], "channelOutputMute", v_int(1))
    check("a value of the wrong kind is ignored", mic["muted"], True)

    store._flush_dirty()
    check("one notification names every channel changed", events[-1],
          ("changed", {"Mic1", "Bluetooth"}))
    count = len(events)
    feed([ch + 1], "channelOutputMute", v_bool(True))
    store._flush_dirty()
    check("an unchanged value notifies nothing", len(events), count)
    check_true("a chain change compares Mic1 with its preset again",
               "Mic1" in store._preset_timers)

    feed([ch + 0], "channelInputSource", v_int(1))
    check_true("a fader reassignment asks for a full read", store._reread is not None)
    check("and changes no record", bt["muted"], False)
    store.stop()

owner.Destroy()
sys.exit(_testkit.finish())
