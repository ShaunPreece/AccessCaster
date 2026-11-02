"""The native window's desk state (Project/native/desk_state.py), offline: pushed frames, decoded
by rcp_read.parse_push as the listener decodes them, land on the right channel's record or output
mix, and only a real change notifies.

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
import recorder
import wx
from _testkit import as_device, check, check_true, patched, push
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
                                   record("SMARTpads", "bus")],
                      "matrix": {"ok": True, "modes": {8: 0, 9: 1},
                                 "excluded": {8: {0: False, 7: False}, 9: {0: False}}},
                      "matrix_model": {"outputs": [], "sources": []},
                      "faders": {"model": {}, "layout": [10, 0, 11], "labels": [], "stereo": []},
                      "levels": {
                          "faders": [{"index": 6, "percent": 50, "muted": False, "soloed": False},
                                     {"index": 7, "percent": 0, "muted": None, "soloed": None}],
                          "monitor": {"percent": 40, "muted": False},
                          "bluetooth_out": {"percent": 70, "muted": False},
                          "auto_mute": {"monitor": True, "bt": False},
                          "headphone_types": [0, 0],
                          "mix": {"ok": True, "levels": {0: {0: {"level": 0.5, "linked": True}},
                                                         8: {0: {"level": 0.5, "linked": True}}}},
                          "model": {}},
                      "master": {"usb_multitrack": 0, "usb_expanded": False, "usb_bypass": False,
                                 "compellor_on": False, "compellor_threshold": 0.67,
                                 "delay_on": False, "delay_seconds": 0.0, "model": {}}})
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

    mix = store.matrix
    feed([rcp_transfer.SEL_OUTPUT_MIXMINUS_BASE + 8], "outputMixMinus", v_int(2))
    check("an output's mode lands on its index", mix["modes"][8], 2)
    feed([rcp_transfer.cross_point_selector(8, 7)], "mixDisabled", v_bool(True))
    check("a cross-point lands on its output and source", mix["excluded"][8][7], True)
    check("and not on the same source elsewhere", mix["excluded"][8][0], False)
    feed([rcp_transfer.CROSS_POINT_BASE + devices.PRO2.matrix.cell_count + 7], "mixDisabled",
         v_bool(True))
    check("RCSYNCMIX's cross-points are not the matrix", mix["excluded"][8][0], False)
    feed([rcp_transfer.SEL_OUTPUT_MIXMINUS_BASE + 12], "outputMixMinus", v_int(2))
    check("a mode for an output not read is ignored", 12 in mix["modes"], False)
    store._flush_dirty()
    check("one notification names every output changed", events[-1], ("mix", {8}))

    reads = []
    with patched((store, {"refresh": lambda: reads.append(True)})):
        store.faders_changed({"ok": True, "faders": [0, 10, 11],
                              "labels": ["Mic 1", "Bluetooth", "SMART pads"]})
    check("a fader write's reply is the new layout", store.faders["layout"], [0, 10, 11])
    check("and is shown at once", events[-1], ("faders", None))
    check("then the channels are read again", reads, [True])

    levels = store.levels
    feed([0, devices.FADER_NODE_BASE + 6], "faderLevel", v_int(127))
    check("a virtual fader's level lands on it, in percent", levels["faders"][0]["percent"], 100)
    feed([0, devices.FADER_NODE_BASE + 2], "faderLevel", v_int(10))
    check("a fader without a level is ignored", [f["percent"] for f in levels["faders"]], [100, 0])
    feed([ch + 6], "channelOutputMute", v_bool(True))
    check("a strip's mute lands on its virtual fader", levels["faders"][0]["muted"], True)
    feed([ch + 7], "channelCueEnable", v_bool(True))
    check("an empty fader has no solo to land on", levels["faders"][1]["soloed"], None)
    out = rcp_transfer.SEL_OUTPUT_GLOBAL
    feed([out], "outputMonLevel", v_float(0.25))
    check("the monitor level, in percent", levels["monitor"]["percent"], 25)
    feed([out], "outputBTMute", v_bool(True))
    check("the Bluetooth output's mute", levels["bluetooth_out"]["muted"], True)
    feed([out], "outputBTAutoMute", v_bool(True))
    check("an auto-mute", levels["auto_mute"]["bt"], True)
    feed([out + 1], "outputMonLevel", v_float(0.9))
    check("an output value on another block is ignored", levels["monitor"]["percent"], 25)
    feed([rcp_transfer.SEL_HEADPHONE_TYPE + 1], "headphoneType", v_int(1))
    check("a headphone type lands on its output", levels["headphone_types"], [0, 1])

    def v_str(text):
        raw = text.encode("ascii")
        return bytes([0x01, len(raw) + 2, 0x05]) + raw + b"\x00"

    feed([rcp_transfer.cross_point_selector(8, 0)], "mixLevelWithAnchor", v_str("0.3|0.5"))
    check("a cross-point level lands on its output and source",
          levels["mix"]["levels"][8][0], {"level": 0.3, "linked": False})
    check("and not on the same source elsewhere", levels["mix"]["levels"][0][0]["level"], 0.5)
    feed([rcp_transfer.CROSS_POINT_BASE + devices.PRO2.matrix.cell_count], "mixLevelWithAnchor",
         v_str("0.9|0.9"))
    check("RCSYNCMIX's levels are not the matrix", levels["mix"]["levels"][0][0]["level"], 0.5)
    count = len(events)
    store._flush_dirty()
    check("one notification for the levels, and none for a strip with no channel record",
          events[count:], [("levels", None)])

    store.monitor_mix_written(0, 0, 0.8)
    check("a monitor mix write records the level of its own",
          levels["mix"]["levels"][0][0], {"level": 0.8, "linked": False})
    check("and makes the output Custom", (mix["modes"].get(0), events[-1]), (2, ("mix", {0})))
    store.solos_cleared()
    check("clearing the solos clears every channel's", bt["solo"], False)
    check("and every fader's", levels["faders"][0]["soloed"], False)

    master = store.master
    proc = rcp_transfer.SEL_MASTER_PROC
    feed([proc], "masterCompellorOn", v_bool(True))
    check("the master Compellor's switch", master["compellor_on"], True)
    feed([proc], "masterCompellorThreshold", v_float(0.5))
    check("a Compellor parameter, as the desk's own 0..1", master["compellor_threshold"], 0.5)
    feed([proc], "masterDelaySeconds", v_float(0.25))
    check("the output delay's time, in seconds", master["delay_seconds"], 0.25)
    feed([out], "outputMultiBypass", v_bool(True))
    check("the USB stream's raw switch, on the OUTPUT block", master["usb_bypass"], True)
    feed([proc + 1], "masterDelayOn", v_bool(True))
    check("a master value on another block is ignored", master["delay_on"], False)
    count = len(events)
    store._flush_dirty()
    check("one notification for the master values", events[count:], [("master", None)])
    feed([proc], "masterCompellorOn", v_bool(True))
    store._flush_dirty()
    check("a value the desk already had notifies nothing", events[count + 1:], [])

    count = len(events)
    feed([recorder.SEL_RECORDER], "recordState", v_int(recorder.RECORDING))
    check("a recording state is passed on at once", events[count:],
          [("recorder", recorder.RECORDING)])
    feed([recorder.SEL_RECORDER + 1], "recordState", v_int(recorder.READY))
    check("a recording state on another block is ignored", events[count + 1:], [])

    feed([ch + 0], "channelInputSource", v_int(1))
    check_true("a fader reassignment asks for a full read", store._reread is not None)
    check("and changes no record", bt["muted"], False)
    store.stop()

owner.Destroy()
sys.exit(_testkit.finish())
