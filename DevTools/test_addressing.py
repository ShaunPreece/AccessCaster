"""Every write the app makes is addressed through the desk's own layout. Offline, no device.

THE ADDRESS IS LOOKED UP, NEVER WRITTEN DOWN (model_tree.Layout). This drives the app's own write
paths with the send layer replaced by a recorder, on the saved layouts of each desk and firmware
(`layout_fixture`), and checks three things:

  1. every record lands on the address the app has always used on that desk, so the lookup
     changed no byte on a firmware we have;
  2. on a layout where a block has MOVED, every record follows it;
  3. the lookup refuses a block, a copy or a property the desk does not have, rather than guess.

Run:
    python DevTools/test_addressing.py
"""
from __future__ import annotations

import pathlib
import re
import sys

import _bootstrap  # noqa: F401  (puts Project/ on sys.path; import FIRST)
import _testkit
import channels
import devices
import fake_model
import layout_fixture as lf
import model_tree
import pad_records as pr
import rcp_hid
import rcp_read
import rcp_transfer as t
import recorder
import shows
import soundpad
from _testkit import as_device, check, check_true, decode_record, on_layout, patched

# Where each block sat on every firmware these desks have run: the addresses the app used before
# the lookup existed, and the ones every capture of RODE's own app shows. A key's address is its
# block's root position plus its copy; a fader's level is the FADER node after four POTs.
LEGACY_ROOT = {
    "PHYSICALINTERFACE": 0x00, "RECORDER": 0x03, "GUI": 0x07, "HEADPHONE": 0x09, "OUTPUT": 0x0D,
    "MASTERCHANNEL": 0x0E, "SYSTEM": 0x0F, "NETWORK": 0x10, "CHANNEL": 0x1C,
    "EFFECTS_PARAMETERS": 0x26, "MIXMINUSES": 0x31, "MIX": 0x4C, "INPUTSOURCE": 0x26E,
    "SHOWCONTROL": 0x28E, "RECORDINGS": 0x28F, "SOUNDPADS": 0x29A, "PADEFFECTS": 0x29D,
}


def legacy(key) -> list:
    if len(key) == 4:
        return [LEGACY_ROOT[key[0]] + key[1], 4 + key[3]]
    return [LEGACY_ROOT[key[0]] + key[1]]


class Sent(list):
    """The records the app sends, decoded to (path, name)."""

    def record(self, rec) -> int:
        path, name, _v, _k, _end = decode_record(rec)
        self.append((tuple(path), name))
        return len(rec)

    def paced(self, recs, delay=0.0) -> int:
        return sum(self.record(r) for r in recs)


def drive(dev) -> tuple:
    """Run every keyed write path once for `dev`. Returns (records sent, keys expected), in step."""
    sent, want = Sent(), []

    def expect(key, name, n=1):
        want.extend([(tuple(key), name)] * n)

    with patched((t, {"send_record": sent.record, "send_records_paced": sent.paced}),
                 (rcp_hid, {"wait_for_reenumeration": lambda before: None}),
                 (t, {"find_control_interface": lambda: None})):
        t.set_midi_control(True); expect(t.SYSTEM, "systemMidiControl")
        for o in range(dev.matrix.output_count):
            t.set_output_routing(o, 2); expect(t.output_key(o), "outputMixMinus")
        for o in sorted({x["index"] for x in dev.matrix.outputs}):
            for src in dev.matrix.all_source_indices:
                key = t.cross_point_key(o, src)
                t.set_cross_point_linked(o, src, False); expect(key, "mixUnlinkRequest")
                t.set_cross_point_excluded(o, src, True)
                expect(key, "mixUnlinkRequest"); expect(key, "mixDisabled")
                t.set_cross_point_level(o, src, 0.5); expect(key, "mixLevelWithAnchor")
        for i in range(dev.fader.vector_length):
            t.set_fader_source(i, -1); expect(t.channel_key(i), "channelInputSource")
        for fn, name in ((t.set_multitrack_mode, "recordingMultitrackMode"),
                         (t.set_recording_format, "recordingCompressionQuality"),
                         (t.set_usb_multitrack_mode, "outputMultiMode")):
            fn(0); expect(t.OUTPUT, name)
        for fn, name in ((t.set_recording_processing_bypass, "recordingProcessingBypass"),
                         (t.set_usb_multitrack_bypass, "outputMultiBypass"),
                         (t.set_monitor_auto_mute, "outputMonAutoMute"),
                         (t.set_bt_auto_mute, "outputBTAutoMute")):
            fn(True); expect(t.OUTPUT, name)
        t.set_monitor_level(0.5); expect(t.OUTPUT, "outputMonLevel")
        t.set_master_compellor(True); expect(t.MASTER, "masterCompellorOn")
        for param, prop in t.MASTER_COMPELLOR_PARAM.items():
            t.set_master_compellor_param(param, 0.5); expect(t.MASTER, prop)
        t.set_output_delay(True, 0.5)
        expect(t.MASTER, "masterDelayOn"); expect(t.MASTER, "masterDelaySeconds")
        for i in dev.fader.level_faders:
            t.set_fader_level(i, 64); expect(t.fader_key(i), "faderLevel")
        for i, _label in dev.headphones.outputs:
            t.set_headphone_type(1, i); expect(t.headphone_key(i), "headphoneType")
        for prop, (key, _kind) in t.UNIT_SETTING_SEL.items():
            t.set_unit_setting(prop, 0); expect(key, prop)
        t.set_transfer_mode(True); expect(t.SYSTEM, "transferModeType")
        t.remount_pad_storage(); expect(t.SYSTEM, "remountPadStorage")

        for ch in dev.ui_channels:
            if ch.get("kind") != "mic":
                continue
            n, key = channels.mic_channel_number(ch), dev.input_key(ch)
            channels.set_mic_gain(n, 40); expect(key, "inputMicrophoneGain")
            channels.set_phantom(n, False); expect(key, "inputPower")
            channels.set_polarity(n, False); expect(key, "inputPhaseFlip")
            channels.set_mic_type(n, 0); expect(key, "inputMicrophoneType")
            channels.set_instrument_gain(n, 20); expect(key, "inputInstrumentGain")

        chain = {"bools": {p: True for p in channels.CHAIN_BOOL_PROPS},
                 "floats": {p: 0.5 for p in channels.CHAIN_FLOAT_PROPS},
                 "fx_bools": {p: True for p in channels.CHAIN_FX_BOOL_PROPS},
                 "fx_floats": {p: 0.5 for p in channels.CHAIN_FX_FLOAT_PROPS},
                 "marker": -1}
        for strip in range(dev.fader.vector_length):
            for kind, prop, _v in channels.chain_diff(channels.fit_chain(chain, strip), None):
                fx = kind.startswith("fx_")
                expect(t.channel_fx_key(strip) if fx else t.channel_key(strip), prop)
            channels.write_chain(strip, chain)
            for prop in (channels.MUTE_PROP, channels.CUE_PROP, channels.WIRELESS_MUTE_PROP):
                t.write_at(channels.proc_key_for_strip(strip), prop, "bool", False)
                expect(t.channel_key(strip), prop)

        recorder.send_command(0); expect(t.RECORDER, "requestRecordState")
        recorder.drop_marker(); expect(t.RECORDER, "requestDropMarker")
        sent.record(recorder.build_delete_request(3)); expect(t.RECORDINGS, "requestDeleteUID")
        shows.send_export(); expect(t.SHOWCONTROL, "showControlExport")
        shows.send_import(1); expect(t.SHOWCONTROL, "showControlImport")
        shows.send_new_from_default(); expect(t.SHOWCONTROL, "showControlNewFromDefault")
        shows.send_delete(1); expect(t.SHOWCONTROL, "showControlDelete")
        soundpad.select_bank(2); expect(t.GUI, "selectedBank")
    return sent, want


def by_key(want, address) -> list:
    """The expected (path, name) list, each key turned into an address by `address`."""
    return [(tuple(address(k)), name) for k, name in want]


# ---- 1. Every record lands where it always has, on every saved desk and firmware --------------
for fixture, dev in ((lf.PRO2_177, devices.PRO2), (lf.PRO2_176, devices.PRO2),
                     (lf.DUO_173, devices.DUO)):
    print(f"{fixture}: every write on the address the app has always used")
    with as_device(dev), on_layout(fixture):
        sent, want = drive(dev)
    check(f"{fixture}: as many records as writes", len(sent), len(want))
    wrong = [(got, exp) for got, exp in zip(sent, by_key(want, legacy)) if got != exp]
    check(f"{fixture}: {len(sent)} records, none moved", wrong[:3], [])

# ---- The pad records: SOUNDPADS and PADEFFECTS, and the byte forms only the hardware proved -----
print("pad records")
with on_layout(lf.PRO2_177):
    recs = pr.build_blank(5, 7, fx_subhandle=2)
    paths = {tuple(decode_record(r)[0]) for r in recs if r[5] == 0x01}
    check("a create writes only on PAD[7] and PADEFFECTS[2]", paths, {(0x29A, 7), (0x29D, 2)})
    check("the PAD handoff, byte for byte", recs[0][5:16].hex(),
          "0301010" + "29a020107" + "504144")
    check("select-begin on a pad", soundpad.build_select_begin(9)[5:13].hex(), "040101029a020109")
    check("select-begin on an effect object", soundpad.build_select_begin(4, pr.FX_BLOCK)[5:13].hex(),
          "040101029d020104")
    check("the cursor select-begin sends no position", soundpad.build_select_begin_cursor()[:12].hex(),
          "0307000000040101029a0200")
    check("a cursor pad bool", soundpad.build_pad_bool_cursor("padActive", False)[5:22].hex(),
          "010102029a020070616441637469766500")
    check("the pad edit's padIdx", pr.write_record("padIdx", "int", 15, 0x17)[5:13].hex(),
          "010102029a020117")

# ---- The strips are not alike -------------------------------------------------------------------
print("a chain is written only where the strip can hold it, and read per node")
with as_device(devices.DUO), on_layout(lf.DUO_173):
    full = {"bools": {}, "floats": {}, "fx_bools": {p: True for p in channels.CHAIN_FX_BOOL_PROPS},
            "fx_floats": {p: 0.5 for p in channels.CHAIN_FX_FLOAT_PROPS}, "marker": None}
    check("a Duo's tenth strip keeps echo and reverb on/off and drops their parameters",
          (sorted(channels.fit_chain(full, 9)["fx_bools"]), channels.fit_chain(full, 9)["fx_floats"]),
          (sorted(channels.CHAIN_FX_BOOL_PROPS), {}))
    check("its ninth strip keeps them all", channels.fit_chain(full, 8)["fx_floats"], full["fx_floats"])
# The pads' effect objects are EFFECTS_PARAMETERS blocks as well, so a positional read of strip N's
# reverb returns a pad's once a strip's node is short.
dump = fake_model.node("Rodecaster", (), [
    fake_model.node("CHANNEL", {"channelOutputMute": False}),
    fake_model.node("CHANNEL", {"channelOutputMute": True}),
    fake_model.node("EFFECTS_PARAMETERS", {"echoOn": True, "reverbMix": 0.25}),
    fake_model.node("EFFECTS_PARAMETERS", {"echoOn": False}),
    fake_model.node("PADEFFECTS", (), [fake_model.node("EFFECTS_PARAMETERS", {"reverbMix": 0.9})]),
])
read = channels.read_chains(dump)
check("two strips read", len(read), 2)
check("strip 2's reverb is its own (none), not the pad's", read[1]["fx_floats"].get("reverbMix"), None)
check("strip 1's reverb is read", read[0]["fx_floats"].get("reverbMix"), 0.25)

# ---- 2. A moved block is followed ---------------------------------------------------------------
# A firmware that inserts one node ahead of CHANNEL moves CHANNEL and everything after it by one,
# which is the case the lookup exists for. Every record must follow its block.
print("a block moved by a firmware: every write follows it")


def _shifted_layout() -> model_tree.Layout:
    import json
    import os
    with open(os.path.join(lf.FIXTURES, f"layout_{lf.PRO2_177}.json"), encoding="utf-8") as fh:
        runs = json.load(fh)
    at = next(i for i, r in enumerate(runs) if r[0] == "CHANNEL")
    runs.insert(at, ["NEWBLOCK", 1, ["somethingNew"], []])
    root, _u, _t = model_tree.parse(fake_model.node("Rodecaster", (), lf._encode(runs)))
    return model_tree.Layout(root)


moved = _shifted_layout()
previous = rcp_read.pin_layout(moved)
try:
    with as_device(devices.PRO2):
        sent, want = drive(devices.PRO2)
finally:
    rcp_read.pin_layout(previous)


def shifted(key) -> list:
    path = legacy(key)
    if len(key) == 2 and LEGACY_ROOT[key[0]] >= LEGACY_ROOT["CHANNEL"]:
        path[0] += 1
    return path


wrong = [(got, exp) for got, exp in zip(sent, by_key(want, shifted)) if got != exp]
check(f"{len(sent)} records, each on its block's new position", wrong[:3], [])
check("CHANNEL[0] is one further on", moved.path(t.channel_key(0)), [0x1D])
check("a block ahead of the insert stays put", moved.path(t.OUTPUT), [0x0D])

# ---- 3. The lookup refuses what the desk does not have -----------------------------------------
print("the lookup refuses rather than guesses")
book = lf.load(lf.PRO2_177)


def refuses(key, prop=None) -> bool:
    try:
        book.path(key, prop)
    except model_tree.AddressUnknown:
        return True
    return False


check_true("a block the desk lacks", refuses(("NOSUCHBLOCK", 0)))
check_true("a copy past the last (MIX has 390)", refuses(("MIX", 390)))
check_true("a property the node does not carry: the stray guard",
           refuses(t.OUTPUT, "channelInputSource"))
check_true("a property in the right block passes", not refuses(t.OUTPUT, "outputMonLevel"))
check("the reverse lookup names a pushed address", book.locate([0x26E + 2]), ("INPUTSOURCE", 2))
check("a fader level's path names its FADER", book.locate([0, 10]),
      ("PHYSICALINTERFACE", 0, "FADER", 6))
check("a pad's position is not a fixed node", book.locate([0x29A, 3]), None)
check("a cross-point key gives back its output and source", t.cross_point_of(("MIX", 13 * 14 + 4)),
      (4, 14))
check("RCSYNCMIX is not the routing matrix", t.cross_point_of(("RCSYNCMIX", 4)), None)

print("with no layout and no desk, a write is refused, not sent to a remembered position")
previous = rcp_read.pin_layout(None)
try:
    with patched((rcp_read, {"read_model_stable": lambda **kw: b""}),
                 (rcp_read, {"_layout_held": {"book": None, "epoch": -1}})):
        try:
            t.build_at(t.OUTPUT, "outputMonLevel", "float", 0.5)
            refused = False
        except RuntimeError:
            refused = True
finally:
    rcp_read.pin_layout(previous)
check_true("refused", refused)

# ---- No position is written down in the app ---------------------------------------------------
print("no app code writes to a selector it names itself")
PROJECT = pathlib.Path(_bootstrap.PROJECT)
named = []
for f in sorted(PROJECT.rglob("*.py")):
    if f.name == "rcp_transfer.py":
        continue
    for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
        if re.search(r"build_record\([^)]*\[0x", line):
            named.append(f"{f.name}:{n}")
check("no literal-path record outside rcp_transfer", named, [])

sys.exit(_testkit.finish())
