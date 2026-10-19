"""Offline checks for the PID 0x0092 desk: its geometry read off its own dump, and READ ONLY.

NO DEVICE, no HID, no writes. Reads a LOCAL dump of that desk, which is not in the repo and must
never be: it carries the unit's serial number and network settings. When it is absent the suite
says so and exits 0 (skipped), so a fresh clone stays green.

    DevTools/fixtures-lokaal/pid0092_model_20261007.bin

What it pins, all measured on that unit:
  * the main-mixer runs and single blocks the layout resolves;
  * the DeviceModel built from them (bases, counts, read caps only);
  * the fader layout and its names, the input reads, the routing grid;
  * the announcer decoding pushes at THIS desk's addresses;
  * every write helper refusing before anything is sent.

Run: python DevTools/test_pid0092_layout.py
"""
from __future__ import annotations

import os
import sys

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)

FIXTURE = _bootstrap.devtools_path("fixtures-lokaal", "pid0092_model_20261007.bin")
if not os.path.isfile(FIXTURE):
    print(f"SKIPPED: the local PID 0x0092 dump is not here ({FIXTURE}).")
    print("It is a private capture (serial number, network settings) and is never committed.")
    sys.exit(0)

import devices
import layout
import model_tree
import rcp_read
import rcp_transfer
import rcp_hid
import channels

FAILED = 0


def check(label, got, want):
    global FAILED
    if got == want:
        print(f"  ok   {label}: {got!r}")
    else:
        FAILED += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


with open(FIXTURE, "rb") as fh:
    BLOB = fh.read()


def _no_device(*_a, **_k):
    raise RuntimeError("offline test: the real desk is never read")


rcp_read.read_model_raw = _no_device        # whatever a path tries, it can never reach the desk

# BYTE SPANS of each parsed node. model_tree no longer keeps them (2.62 dropped Node.start / end as
# unused by the app), and this suite compares records with the dump's own bytes, so it records them
# itself while parsing: from the node's name to one past its last child, as model_tree once did.
_SPANS: dict = {}


def parse_with_spans(blob: bytes):
    """model_tree.parse, recording every node's byte span (see span)."""
    orig = model_tree._parse_node

    def spy(b, i, parent, index):
        node, end = orig(b, i, parent, index)
        _SPANS[id(node)] = (node, i, end)       # the node is kept, so its id cannot be reused
        return node, end

    model_tree._parse_node = spy                # the recursion looks it up here, so kids are seen
    try:
        return model_tree.parse(blob)
    finally:
        model_tree._parse_node = orig


def span(node) -> tuple:
    """(start, end) of a node from parse_with_spans."""
    return _SPANS[id(node)][1:]


PID = 0x0092

print("THE DUMP")
check("fixture size", len(BLOB), 149430)
check("declared length + header", int.from_bytes(BLOB[:4], "little") + 4 <= len(BLOB), True)
check("0x02 then the root", BLOB[4:5] + BLOB[5:15], b"\x02Rodecaster")
root, _used, _tail = parse_with_spans(BLOB)
check("root children", len(root.kids), 703)

print("\nTHE LAYOUT (main runs, first occurrence)")
layout.forget()
rcp_read._INDEX_CACHE.clear()
rcp_read._index(BLOB)                       # no pid: a file is never recorded as a desk
check("a dump parsed without a pid records nothing", layout.current(PID), None)
rcp_read._index(BLOB, pid=PID)              # what read_model_raw does for this desk
LAY = layout.current(PID)
check("recorded for the pid", LAY is not None, True)
check("valid", (LAY.valid, LAY.problems), (True, ()))
for name, base, count in (("CHANNEL", 0x1A, 9), ("EFFECTS_PARAMETERS", 0x23, 10),
                          ("MIXMINUSES", 0x2D, 10), ("MIX", 0x37, 156),
                          ("INPUTSOURCE", 0xDF, 12), ("HEADPHONE", 0x07, 4)):
    check(f"{name} run", (LAY.base(name), LAY.count(name)), (base, count))
for name, base in (("PHYSICALINTERFACE", 0x00), ("RECBUTTON", 0x01), ("RECORDER", 0x02),
                   ("PLAYER", 0x03), ("ENCODER", 0x04), ("GUI", 0x05), ("APP", 0x06),
                   ("OUTPUT", 0x0B), ("MASTERCHANNEL", 0x0C), ("SYSTEM", 0x0D),
                   ("NETWORK", 0x0E), ("AUDIO", 0x0F), ("SOUNDPADS", 0xF4)):
    check(f"{name} single", LAY.single(name), base)
later = [(r.name, r.base, r.count) for r in LAY.all_runs
         if r.name in ("CHANNEL", "MIX", "INPUTSOURCE", "MIXMINUSES") and r.base > 0xF0]
check("the later blocks are seen and NOT used", later,
      [("CHANNEL", 0xFA, 1), ("MIXMINUSES", 0xFC, 3), ("MIX", 0xFF, 91), ("INPUTSOURCE", 0x161, 7),
       ("MIX", 0x17D, 143), ("INPUTSOURCE", 0x295, 11)])
check("the run refuses outside itself", LAY.run("CHANNEL").index_of(0xFA), None)
check("HEADPHONE is ONE run of 4, right after APP and right before OUTPUT",
      ([(r.base, r.count) for r in LAY.all_runs if r.name == "HEADPHONE"],
       LAY.single("APP") + 1 == LAY.base("HEADPHONE"),
       LAY.base("HEADPHONE") + LAY.count("HEADPHONE") == LAY.single("OUTPUT")),
      ([(0x07, 4)], True, True))


class _K:                                   # a stand-in tree node: name + index is all from_tree reads
    def __init__(self, name, index):
        self.name, self.index = name, index


class _R:
    def __init__(self, names):
        self.kids = [_K(n, i) for i, n in enumerate(names)]


bad = layout.from_tree(_R(["CHANNEL"] * 2 + ["INPUTSOURCE"] * 2 + ["MIX"] * 25))
check("MIX not 13 per input is invalid", bad.valid, False)
check("an invalid layout is not recorded", layout.note(0x7777, bad), False)
check("no CHANNEL is invalid",
      layout.from_tree(_R(["INPUTSOURCE"] + ["MIX"] * 13)).valid, False)

print("\nTHE DEVICE MODEL")
DEV = devices.resolve_device(PID)
check("name (fits a profile name)", DEV.name, "RODECaster Pro2 0092")
check("geometry from the layout", DEV.layout is LAY, True)
check("matrix cells / outputs / stride",
      (DEV.matrix.cell_count, DEV.matrix.output_count, DEV.matrix.row_stride), (156, 13, 13))
check("matrix base = MIX base", DEV.matrix.base, 0x37)
check("output modes base = MIXMINUSES base", DEV.matrix.modes_base, 0x2D)
check("the last three modes come from the later MIXMINUSES run", DEV.matrix.modes_extra,
      ((0xFC, 3),))
check("input table = INPUTSOURCE base", DEV.input_table_base, 0xDF)
check("strip base = CHANNEL base", DEV.proc_sel_base, 0x1A)
check("fader count = CHANNEL run", DEV.fader.count, 9)
check("read caps, the seven narrow write caps and the generalised tree_write (not matrix_write, "
      "not output_write)", DEV.caps,
      frozenset({"matrix_read", "channel_read", "input_write", "fx_write", "fader_write",
                 "strip_write", "routing_write", "headphone_write", "usbmode_write",
                 "tree_write"}))
check("tree_write stands in for output_write and unit_settings, and for nothing else",
      [c for c in ("output_write", "unit_settings", "matrix_write", "pad_write", "level_write",
                   "recorder", "shows", "network_write", "processing", "stereo_link")
       if devices.has_write_cap(DEV, c)], ["output_write", "unit_settings"])
check("the Pro II and the Duo are asked exactly `cap in caps`",
      [(devices.has_write_cap(d, c), c in d.caps) for d in (devices.PRO2, devices.DUO)
       for c in ("output_write", "unit_settings", "tree_write")],
      [(c in d.caps, c in d.caps) for d in (devices.PRO2, devices.DUO)
       for c in ("output_write", "unit_settings", "tree_write")])
check("the four headphone outputs are the HEADPHONE run, by name, not the Pro II 0x09/0x0A",
      DEV.headphones.outputs, ((0x07, "Headphone 1"), (0x08, "Headphone 2"),
                               (0x09, "Headphone 3"), (0x0A, "Headphone 4")))
check("with the Pro II amp types, all three measured here",
      DEV.headphones.values, ((0, "NTH-100"), (1, "High sensitivity"), (2, "Low sensitivity")))
check("the Pro II and Duo headphone specs are unchanged",
      (devices.PRO2.headphones.outputs, devices.DUO.headphones.outputs),
      (((0x09, "Headphone 1"), (0x0A, "Headphone 2")),) * 2)
check("a HEADPHONE run that is not four long lists no outputs",
      devices.layout_headphone_outputs(layout.from_tree(_R(
          ["APP"] + ["HEADPHONE"] * 3 + ["OUTPUT"] + ["CHANNEL"] + ["INPUTSOURCE"] + ["MIX"] * 13))),
      ())
check("writes blocked", bool(DEV.write_block), True)
# Output 0 = Headphone 1 measured by ear (7th October 2026); the rest the Pro II order, which the
# unit's default modes match, accepted by its owner. Shown plainly, no "(unconfirmed)".
check("output names are the Pro II order, plainly",
      [o["label"] for o in DEV.matrix.outputs],
      ["Headphone 1", "Headphone 2", "Headphone 3", "Headphone 4", "Monitor (speaker)",
       "Recording", "Bluetooth", "USB 1 (Main Stereo)", "Chat", "USB 2 (Secondary)", "CallMe 1",
       "CallMe 2", "CallMe 3"])
check("with the Pro II keys", [o["key"] for o in DEV.matrix.outputs][:11],
      [o["key"] for o in devices.PRO2.matrix.outputs])
check("no output label says unconfirmed",
      any("unconfirmed" in o["label"] for o in DEV.matrix.outputs), False)
check("the default modes are the Pro II order AND exactly what the unit reads",
      [o["default_mode"] for o in DEV.matrix.outputs], [0, 0, 0, 0, 0, 0, 1, 0, 1, 1, 1, 1, 1])
check("the Pro II outputs are unchanged (headphones offer no Mix Minus there)",
      [(o["key"], o["mixminus"], o["default_mode"]) for o in devices.PRO2.matrix.outputs][:2],
      [("Headphone1", False, 0), ("Headphone2", False, 0)])
check("all 12 rows of the main run, and none past it", DEV.matrix.source_indices, tuple(range(12)))
check("unconfirmed rows say so, confirmed ones do not",
      {s["index"]: s["label"] for s in DEV.matrix.sources if s["index"] in (1, 4, 6, 10)},
      {1: "Mic 2", 4: "Mic 1 + 2 (unconfirmed)", 6: "Mic 3 + 4", 10: "Bluetooth (unconfirmed)"})
check("no factory fader layout claimed", DEV.fader.default_vector, None)
other = devices.resolve_device(0x1234)
check("an unknown connected pid is NOT a Pro II", (other is devices.PRO2, other.caps),
      (False, frozenset()))
check("nothing connected is still the Pro II default",
      devices.resolve_device(None) is devices.PRO2, True)
check("the Pro II is unchanged", (devices.resolve_device(0x0037) is devices.PRO2,
                                  devices.PRO2.layout_required, devices.PRO2.matrix.base,
                                  devices.PRO2.input_table_base, devices.PRO2.proc_sel_base),
      (True, False, None, 0x26E, 0x1C))
check("the Duo is unchanged", (devices.resolve_device(0x0050) is devices.DUO,
                               devices.DUO.layout_required, devices.DUO.matrix.base), (True, False, None))

devices.force_device(DEV)
import server                               # after the pin: nothing at import may see another desk

SPOKEN: list = []
server._ann_say = lambda text: SPOKEN.append(text)
server._ann_tone = lambda name: False

print("\nTHE FADER LAYOUT")
vec = server._read_fader_vector(BLOB)
check("channelInputSource per fader", vec, [0, 1, 6, 7, 9, 8, -1, 11, 10])
names = [DEV.fader.all_labels.get(c) for c in vec[:6]]
check("faders 1-6 by name", names,
      ["Mic 1", "Mic 2", "Mic 3 + 4", "USB 1 (Main Stereo)", "USB 2 (Secondary)", "Chat"])
check("the announcer names fader 3 the same way", server._ann_channel_label(2), "Mic 3 + 4")
mutes = channels.strip_values(BLOB, "channelOutputMute", dev=DEV)
check("mutes read per strip, main run only", len(mutes), 9)

print("\nINPUT READS (INPUTSOURCE[source code])")
for code, gain, power in ((0, 51, 0), (1, 27, 1), (2, 46, 0), (3, 50, 0)):
    ch = {"source_code": code}
    check(f"source {code} mic gain", channels.input_value(BLOB, ch, "inputMicrophoneGain", DEV), gain)
    check(f"source {code} phantom", channels.input_value(BLOB, ch, "inputPower", DEV), power)
check("source 6 input type is line (2)",
      channels.input_value(BLOB, {"source_code": 6}, "inputType", DEV), 2)
chans = {c["name"]: c for c in channels.read_channels(BLOB)}
check("read_channels: mic gains", [chans[n]["level_db"] for n in ("Mic1", "Mic2", "Mic3", "Mic4")],
      [51, 27, 46, 50])
check("read_channels: phantom", [chans[n]["phantom"] for n in ("Mic1", "Mic2", "Mic3", "Mic4")],
      [False, True, False, False])
check("read_channels: Mic 1 echo read from its EFFECTS block", chans["Mic1"]["effects"]["echo"], False)

print("\nTHE ROUTING GRID (main MIX run only)")
check("cells read", len(rcp_read.matrix_cells(BLOB, DEV.matrix, "mixDisabled")), 156)
lv = rcp_read.read_matrix_levels(BLOB)
check("levels read ok", (lv["ok"], lv["reason"]), (True, ""))
check("Mic 1 to output 1 is linked at unity", lv["levels"][0][0], {"level": 1.0, "linked": True})
st = rcp_read.read_matrix_state(BLOB)
check("thirteen output modes, 10 from the main run + 3 from 0xFC",
      [st["modes"][o] for o in range(13)], [0, 0, 0, 0, 0, 0, 1, 0, 1, 1, 1, 1, 1])
check("every output has all 12 rows", {len(v) for v in st["excluded"].values()}, {12})
check("a fader at the bottom ('-3.00258e-06|0') reads as following its fader",
      lv["levels"][3][1], {"level": -3.00258e-06, "linked": True})
check("and one at its first step ('0.00787401|0.00787402')", lv["levels"][0][11]["linked"], True)

print("\nPUSHES DECODED AT THIS DESK'S ADDRESSES")


def push(rec: bytes) -> bytes:
    """A report-3 record as the desk pushes it: report id 04, same length and record."""
    return b"\x04" + rec[1:]


def push_path(frame: bytes):
    """The address the announcer decodes a push at (rcp_read.parse_push, as _ann_handle_report)."""
    rec = rcp_read.parse_push(frame)
    return rec[0] if rec else None


f37 = push(rcp_transfer.build_cross_point_level(0x37, 0.5))
check("0x37 mixLevelWithAnchor = (Mic 1, output 0)", server._ann_xpoint(push_path(f37)), (0, 0))
f44 = push(rcp_transfer.build_cross_point_level(0x44, 0.5))
check("0x44 mixLevelWithAnchor = source code 1", server._ann_xpoint(push_path(f44))[0], 1)
fff = push(rcp_transfer.build_cross_point_level(0xFF, 0.5))
check("a later MIX block is not a cross-point", server._ann_xpoint(push_path(fff)), None)
NOTED = []
server._ann_note_level = lambda st_, kind, src, lvl: NOTED.append((kind, src, lvl))
server._ann_levels_on = True
server._ann_handle_report(f37, {"bank": None, "pending": None, "at": 0.0})
check("the handler names the source", NOTED, [("fader", 0, 0.5)])
check("as Mic 1", server._ann_source_label(NOTED[0][1]), "Mic 1")

channels.publish_strip_vector(vec)
SPOKEN.clear()
ann = {"bank": None, "pending": None, "at": 0.0}
server._ann_handle_report(push(rcp_transfer.build_global_prop_bool("channelOutputMute", True, 0x1A)),
                          ann)
check("channelOutputMute on 0x1A is fader 1", ann.get("muted"), {0: True})
check("and is spoken as Mic 1", SPOKEN, ["Mic 1 muted"])
SPOKEN.clear()
server._ann_handle_report(push(rcp_transfer.build_global_prop_bool("channelCueEnable", True, 0x1A)),
                          ann)
check("channelCueEnable on 0x1A is fader 1", SPOKEN, ["Mic 1 solo on"])
SPOKEN.clear()
ann2 = {"bank": None, "pending": None, "at": 0.0}
server._ann_handle_report(push(rcp_transfer.build_global_prop_bool("channelOutputMute", True, 0xFA)),
                          ann2)
check("a mute on the later CHANNEL block is not a fader", (ann2.get("muted"), SPOKEN), (None, []))

print("\nPHYSICALINTERFACE: FADER and POT nodes found by name")
check("PHYSICALINTERFACE children", len(root.kids[0].kids), 105)
fr, pr = LAY.physical("FADER"), LAY.physical("POT")
check("FADER run", (fr.base, fr.count), (4, 9))
check("POT run", (pr.base, pr.count), (0, 4))
check("fader 7 path", LAY.physical_path("FADER", 6), [0, 10])
check("faderLevel per fader", rcp_read.physical_values(BLOB, "FADER", "faderLevel", DEV),
      [0, 0, 0, 0, 0, 1, 0, 1, 0])
check("potLevel per knob", rcp_read.physical_values(BLOB, "POT", "potLevel", DEV),
      [66, 127, 114, 125])
SEATED = []
real_seat = server._ann_ring_seat
server._ann_ring_seat = lambda st_, kind, idx, lvl: SEATED.append((kind, idx))
NOTED.clear()
server._ann_handle_report(push(rcp_transfer.build_path_prop_int("faderLevel", 64, [0, 10])),
                          {"bank": None, "pending": None, "at": 0.0})
check("faderLevel at [0, 10] is fader 7", SEATED, [("fader", 6)])
server._ann_handle_report(push(rcp_transfer.build_path_prop_int("potLevel", 64, [0, 3])),
                          {"bank": None, "pending": None, "at": 0.0})
check("potLevel at [0, 3] is headphone knob 4", NOTED[-1][:2] if NOTED else None, ("headphone", 3))

print("\nOUTPUT AND SYSTEM PUSHES, by this desk's own block indexes")
SEATED.clear()
NOTED.clear()
server._ann_handle_report(push(rcp_transfer.build_global_prop_float("outputMonLevel", 0x0B, 0.5)),
                          {"bank": None, "pending": None, "at": 0.0})
check("outputMonLevel on OUTPUT 0x0B is the monitor", (SEATED, NOTED),
      ([("monitor", 0)], [("monitor", 0, 0.5)]))
SEATED.clear()
NOTED.clear()
server._ann_handle_report(push(rcp_transfer.build_global_prop_float("outputMonLevel", 0x0D, 0.5)),
                          {"bank": None, "pending": None, "at": 0.0})
check("outputMonLevel at the Pro II's 0x0D (SYSTEM here) is ignored", (SEATED, NOTED), ([], []))
server._ann_ring_seat = real_seat
SPOKEN.clear()
server._ann_handle_report(push(rcp_transfer.build_global_prop_int("systemChannelSelected", 0x0D, 2)),
                          {"bank": None, "pending": None, "at": 0.0, "chan_sel": -1})
check("systemChannelSelected on SYSTEM 0x0D names fader 3", SPOKEN,
      ["Mic 3 + 4 selected. Fader level only"])
SPOKEN.clear()
server._ann_handle_report(push(rcp_transfer.build_global_prop_int("systemChannelSelected", 0x0F, 2)),
                          {"bank": None, "pending": None, "at": 0.0, "chan_sel": -1})
check("the same name on another block is not the selection", SPOKEN, [])
SPOKEN.clear()
server._ann_handle_report(push(rcp_transfer.build_path_prop_int("inputDigitalGain", -6, [0xDF + 6])),
                          {"bank": None, "pending": None, "at": 0.0})
check("a gain on the Mic 3 + 4 input is named, not dropped (2.62: the number first)", SPOKEN,
      ["-6 d B, Mic 3 + 4 input level"])

print("\nTHE ROUTING PAGE'S PRESET LIST")
try:
    rs = server.read_routing_schemes()
    check("read_routing_schemes does not crash", rs.get("ok"), True)
    check("the built-ins are offered: everything normal and the two Chat presets",
          [b["id"] for b in rs["builtins"]], ["normal", "chat-mic-only", "chat-normal"])
    check("and the presets are not read-only", rs["read_only"], False)
    nb = __import__("routing_schemes").builtin("normal")["scheme"]["outputs"]
    check("'Everything normal' puts each output on the mode the unit reads now",
          [nb[o["key"]]["mode"] for o in DEV.matrix.outputs],
          [st["modes"][o] for o in range(13)])
except Exception as exc:                      # the crash seen live on 7th October 2026
    check("read_routing_schemes does not crash", repr(exc), None)

print("\nEVERY WRITE REFUSES, AND NOTHING IS SENT")
SENT = []
real = (rcp_transfer.send_record, rcp_transfer.send_records_paced, rcp_hid.open_handle)
rcp_transfer.send_record = lambda rec: SENT.append(rec) or len(rec)
rcp_transfer.send_records_paced = lambda recs, delay=0.12: SENT.extend(recs) or 0
rcp_hid.open_handle = lambda iface, purpose: SENT.append(("open", purpose))


def refuses(label, fn):
    before = len(SENT)
    try:
        fn()
    except RuntimeError as exc:                 # devices.WriteRefused is a RuntimeError
        check(f"{label} refuses", (True, len(SENT) - before), (True, 0))
        return str(exc)
    check(f"{label} refuses", (False, len(SENT) - before), (True, 0))
    return None


refuses("cross-point exclude", lambda: rcp_transfer.set_cross_point_excluded(0, 0, True))
refuses("cross-point level", lambda: rcp_transfer.set_cross_point_level(0, 0, 0.5))
refuses("output mode", lambda: rcp_transfer.set_output_routing(0, 2))
# Fader sources are writable now, but only checked codes on main-run strips; these refuse before the
# desk is even read (nothing here may touch a connected desk).
refuses("fader source: Mic 3 alone (code 2, unproven here)", lambda: rcp_transfer.set_fader_source(0, 2))
refuses("fader source: pair code 4", lambda: rcp_transfer.set_fader_source(0, 4))
refuses("fader source: CallMe 1 (16)", lambda: rcp_transfer.set_fader_source(0, 16))
refuses("fader source: strip 9 (past the run)", lambda: rcp_transfer.set_fader_source(9, 1))
refuses("fader source: strip -1", lambda: rcp_transfer.set_fader_source(-1, 1))
res = server.action_reset_faders()
check("reset faders is refused by name, with a sentence",
      (res["ok"], "not available on the RODECaster Pro2 0092" in res["message"],
       "fader 3" in res["message"]), (False, True, True))
check("stereo link stays refused", server.action_set_stereo_link(4, True)["ok"], False)
res = server.action_set_fader(0, 2)
check("the page refuses Mic 3 on its own", (res["ok"], res["message"]),
      (False, "Channel code 2 is not assignable."))
check("the page offers only the checked codes",
      [c for c, _l in server.fader_model()["channels"]], [0, 1, 7, 8, 9, 10, 11])
check("and is no longer read-only", server.fader_model()["read_only"], False)
# (The by-name strip source setter, rcp_transfer.set_channel_input_source, is gone upstream in 2.62.)
# Enabled now (generalised gate), but checked against a FRESH dump before anything is sent - and
# there is no desk to read here, so each stops before the wire.
refuses("processing preset, with no desk to read", lambda: channels.apply_processing_preset(1, {"eqOn": True}))
refuses("preset name marker, with no desk to read", lambda: channels.set_channel_preset_marker(1, 2))
refuses("wireless mic mute, with no desk to read", lambda: channels.set_channel_wireless_mute(1, True))
refuses("processing selector", lambda: channels.proc_selector("Mic1"))
refuses("strip selector", lambda: channels.proc_selector_for_strip(0))
refuses("effects selector", lambda: channels.fx_selector_for_strip(0))
refuses("chain write", lambda: channels.write_chain(0, {"bools": {"eqOn": True}}))
refuses("instrument gain, with no desk to read", lambda: channels.set_instrument_gain(1, 20))
refuses("a monitor auto-mute, with no desk to read", lambda: rcp_transfer.set_monitor_auto_mute(True))
refuses("a pad bank select (selectedBank is a navigation, refused)",
        lambda: rcp_transfer._send_setting(rcp_transfer.build_global_prop_int(
            "selectedBank", rcp_transfer._block_sel(rcp_transfer.SEL_GUI, "GUI"), 2)))
res = __import__("profiles").apply(
    {"channels": []}, fader_count=9, fader_code_set=set(), input_strip_vec_index={},
    valid_source_codes=set(), mic_channels=(1, 2, 3, 4))
check("an empty profile restores nothing, and says the layout could not be read first",
      (res["applied"], res["failed"], "could not read" in " ".join(res["errs"])), (0, 1, True))
real_load = server.profiles.load
for saved_pid, want_ok in ((0x0037, False), (None, False)):
    server.profiles.load = lambda name, p=saved_pid: {"ok": True, "safe": "x",
                                                      "prof": {"product_id": p, "channels": []}}
    res = server.restore_profile("x")
    check(f"a profile saved from {saved_pid!r} is not restored onto this desk",
          (res["ok"], "not saved from" in res.get("message", "")), (False, True))
server.profiles.load = real_load
check("nothing was sent by any refusal", SENT, [])
rcp_transfer.send_record, rcp_transfer.send_records_paced, rcp_hid.open_handle = real

fake = rcp_hid.RodeInterface(path=b"", product_id=PID, interface_number=9, usage_page=0xFF00,
                             usage=1, product_string="", serial_number="")

print("\nINPUT WRITES: THE ADDRESS FORM")
# The record body is ADDRESS + NAME + NUL + VALUE BLOCK. The name, NUL and value block are compared
# BYTE FOR BYTE with the same property's bytes in the desk's own dump (the dump stores a property
# exactly as name, NUL, value block), written back at its current value. The address is the
# one-level form 01 01 01 01 <index>: the form every one-level push has on hardware (test_announcer
# pins the observed 01 01 01 01 1D for a mute), and the address the 0x0092 desk's own gain pushes
# decoded to on 7th October 2026 (one level, INPUTSOURCE 0xDF + source). The width byte (01 for
# an index up to 0xFF) still wants one raw INPUTSOURCE push from this unit to be seen, not derived.


def dump_prop_bytes(node, name: str) -> bytes:
    """name + NUL + value block as stored in the dump, from the node's own byte range."""
    i = BLOB.find(name.encode() + b"\x00", *span(node))
    j = i + len(name) + 1
    ln = BLOB[j]
    vlen = int.from_bytes(BLOB[j + 1:j + 1 + ln], "little")
    return BLOB[i:j + 1 + ln + vlen]


for code, prop in ((0, "inputMicrophoneGain"), (1, "inputPower"), (2, "inputPhaseFlip"),
                   (7, "inputDigitalGain")):
    node = root.kids[0xDF + code]
    cur = next(v for p, v, k in node.props if p == prop)
    build = rcp_transfer.build_path_prop_bool if isinstance(cur, bool) else rcp_transfer.build_path_prop_int
    rec = build(prop, cur, [0xDF + code])
    n = int.from_bytes(rec[1:5], "little")
    want = b"\x01\x01\x01\x01" + bytes([0xDF + code]) + dump_prop_bytes(node, prop)
    check(f"{prop} on source {code}: record == 01 01 01 01 {0xDF + code:02X} + the dump's own bytes",
          rec[5:5 + n].hex(" "), want.hex(" "))
check("Mic 1 gain 40, every byte of the record",
      rcp_transfer.build_path_prop_int("inputMicrophoneGain", 40, [0xDF]).hex(" "),
      (bytes.fromhex("03 20 00 00 00 01 01 01 01 df") + b"inputMicrophoneGain\x00"
       + bytes.fromhex("01 05 01 28 00 00 00")).ljust(256, b"\x00").hex(" "))

print("\nINPUT WRITES: THE SEND LAYER TAKES THESE AND NOTHING ELSE")


def send_ok(rec) -> bool:
    try:
        devices.refuse_unproven_pid(PID, rec)
        return True
    except devices.WriteRefused:
        return False


ok_recs = [rcp_transfer.build_path_prop_int("inputMicrophoneGain", 40, [0xDF]),
           rcp_transfer.build_path_prop_int("inputPower", 1, [0xE0]),
           rcp_transfer.build_path_prop_bool("inputPhaseFlip", True, [0xE1]),
           rcp_transfer.build_path_prop_int("inputDigitalGain", -6, [0xE6]),
           rcp_transfer.build_path_prop_int("inputDigitalGain", 12, [0xDF + 11])]
check("the four input properties on sources 0..11", [send_ok(r) for r in ok_recs], [True] * 5)

# The same unit in Expanded mode enumerates as 0x0094 (measured 7th October 2026: the dump differs
# only in outputMultiMode). It is a layout desk too, under the same name, with the same gate.
check("Expanded (0x0094) records its own layout", layout.note(devices.PID_LAYOUT_0094, LAY), True)
DEV94 = devices.resolve_device(devices.PID_LAYOUT_0094)
check("Expanded (0x0094) is the same desk, same geometry and caps",
      (DEV94.name, DEV94.matrix.base, DEV94.input_table_base, DEV94.proc_sel_base, DEV94.caps),
      (DEV.name, DEV.matrix.base, DEV.input_table_base, DEV.proc_sel_base, DEV.caps))
check("Expanded (0x0094) holds the headphone and USB-mode caps and the same four outputs",
      ({"headphone_write", "usbmode_write"} <= DEV94.caps, DEV94.headphones.outputs),
      (True, DEV.headphones.outputs))


def send_ok_94(rec) -> bool:
    try:
        devices.refuse_unproven_pid(devices.PID_LAYOUT_0094, rec)
        return True
    except devices.WriteRefused:
        return False


check("Expanded (0x0094) takes the same input writes", [send_ok_94(r) for r in ok_recs], [True] * 5)
check("Expanded (0x0094) refuses a stray name",
      send_ok_94(rcp_transfer.build_path_prop_int("inputMadeUpName", 1, [0xDF])), False)
bad = {
    "gain above 76": rcp_transfer.build_path_prop_int("inputMicrophoneGain", 77, [0xDF]),
    "level below -60": rcp_transfer.build_path_prop_int("inputDigitalGain", -61, [0xE6]),
    "phantom 2": rcp_transfer.build_path_prop_int("inputPower", 2, [0xDF]),
    "polarity as int": rcp_transfer.build_path_prop_int("inputPhaseFlip", 1, [0xDF]),
    "mic type 11": rcp_transfer.build_path_prop_int("inputMicrophoneType", 11, [0xDF]),
    "inputType (derived: on the deny list)": rcp_transfer.build_path_prop_int("inputType", 2, [0xDF]),
    "instrument gain as a bool": rcp_transfer.build_path_prop_bool("inputInstrumentGain", True, [0xDF]),
    "source 12 (later run)": rcp_transfer.build_path_prop_int("inputMicrophoneGain", 40, [0xDF + 12]),
    "the Pro II input address": rcp_transfer.build_path_prop_int("inputMicrophoneGain", 40, [0x26E]),
    "a two-level address": rcp_transfer.build_path_prop_int("inputMicrophoneGain", 40, [0xDF, 0]),
    "a mute on the later CHANNEL 0xFA": rcp_transfer.build_global_prop_bool("channelOutputMute", True, 0xFA),
    "a cross-point in the later MIX run": rcp_transfer.build_cross_point_level(0xFF, 0.5),
    "fader source code 2": rcp_transfer.build_global_prop_int("channelInputSource", 0x1A, 2),
    "fader source code 3": rcp_transfer.build_global_prop_int("channelInputSource", 0x1A, 3),
    "fader source code 4": rcp_transfer.build_global_prop_int("channelInputSource", 0x1A, 4),
    "fader source code 12": rcp_transfer.build_global_prop_int("channelInputSource", 0x1A, 12),
    "fader source code -2": rcp_transfer.build_global_prop_int("channelInputSource", 0x1A, -2),
    "fader source at 0x19": rcp_transfer.build_global_prop_int("channelInputSource", 0x19, 1),
    "fader source at 0x23": rcp_transfer.build_global_prop_int("channelInputSource", 0x23, 1),
    "fader source, later CHANNEL 0xFA": rcp_transfer.build_global_prop_int("channelInputSource", 0xFA, 1),
    "fader source at the Pro II 0x1C+8": rcp_transfer.build_global_prop_int("channelInputSource", 0x24, 1),
    "fader source as bool": rcp_transfer.build_global_prop_bool("channelInputSource", True, 0x1A),
    "fader source two-level": rcp_transfer.build_path_prop_int("channelInputSource", 1, [0x1A, 0]),
    "another CHANNEL property": rcp_transfer.build_global_prop_int("channelCueEnable", 0x1A, 1),
    "channelInputSource on INPUTSOURCE": rcp_transfer.build_global_prop_int("channelInputSource", 0xDF, 1),
    "a monitor level at the Pro II 0x0D (SYSTEM here)":
        rcp_transfer.build_global_prop_float("outputMonLevel", 0x0D, 0.5),
    "transfer mode": rcp_transfer.build_global_prop_int("transferModeType", 0x0F, 2),
    "transfer mode on SYSTEM": rcp_transfer.build_global_prop_int("transferModeType", 0x0D, 2),
    "garbage": b"\x03" + bytes(255),
    "nothing": None,
}
check("everything else is refused", [k for k, r in bad.items() if send_ok(r)], [])
check("the instrument gain and the monitor level pass the generalised gate at their own blocks",
      [send_ok(rcp_transfer.build_path_prop_int("inputInstrumentGain", 20, [0xDF])),
       send_ok(rcp_transfer.build_global_prop_float("outputMonLevel", 0x0B, 0.5))], [True, True])
fader_ok = [rcp_transfer.build_global_prop_int("channelInputSource", 0x1A + s, c)
            for s in range(9) for c in (-1, 0, 1, 6, 7, 8, 9, 10, 11)]
check("channelInputSource, codes -1 0 1 6 7 8 9 10 11, on strips 0x1A..0x22",
      all(send_ok(r) for r in fader_ok), True)
check("the Pro II is not judged by record", devices.refuse_unproven_pid(0x0037, bad["a cross-point in the later MIX run"]), None)

print("\nINPUT WRITES: CHECKED BEFORE, CONFIRMED AFTER (fake desk, nothing leaves the process)")


def with_value(code: int, prop: str, new) -> bytes:
    """The fixture with one input property changed in place, as the desk would report it."""
    node = root.kids[0xDF + code]
    old = dump_prop_bytes(node, prop)
    i = BLOB.find(old, *span(node))
    build = rcp_transfer.build_path_prop_bool if isinstance(new, bool) else rcp_transfer.build_path_prop_int
    rec = build(prop, new, [0xDF + code])
    n = int.from_bytes(rec[1:5], "little")
    new_bytes = rec[5 + 5:5 + n]                 # drop the 5 address bytes
    assert len(new_bytes) == len(old)
    return BLOB[:i] + new_bytes + BLOB[i + len(old):]


WIRE = []


class FakeHandle:
    def write(self, rec):
        WIRE.append(bytes(rec))
        return len(rec)


saved = (rcp_transfer.find_control_interface, rcp_hid.refuse_write_in_tests, rcp_hid.open_handle,
         rcp_hid.close_handle, rcp_read.read_model, channels._confirm_reader, channels.CONFIRM_GAP_S)
rcp_transfer.find_control_interface = lambda: fake
rcp_hid.refuse_write_in_tests = lambda: None
rcp_hid.open_handle = lambda iface, purpose: FakeHandle()
rcp_hid.close_handle = lambda dev, purpose: None
rcp_read.read_model = lambda *a, **k: BLOB
channels.CONFIRM_GAP_S = 0
try:
    AFTER = with_value(0, "inputMicrophoneGain", 40)
    channels._confirm_reader = lambda: AFTER
    WIRE.clear()
    channels.set_mic_gain(1, 40)
    check("Mic 1 gain 40: one record on the wire, at INPUTSOURCE 0xDF",
          [r[5:10].hex(" ") for r in WIRE], ["01 01 01 01 df"])
    check("and it is the builder's record", WIRE[0] ==
          rcp_transfer.build_path_prop_int("inputMicrophoneGain", 40, [0xDF]), True)
    res = server.action_set_gain(1, 40)
    check("the page is told it worked", (res["ok"], res["message"]), (True, "Mic 1 gain set to 40 dB."))

    channels._confirm_reader = lambda: BLOB           # the desk keeps reporting 51
    WIRE.clear()
    res = server.action_set_gain(1, 40)
    check("a write the desk does not confirm is reported as NOT done",
          (res["ok"], "still reports 51" in res["message"]), (False, True))
    check("it was sent once, and not retried", len(WIRE), 1)

    channels._confirm_reader = lambda: with_value(1, "inputPower", 0)
    WIRE.clear()
    check("phantom off on Mic 2 confirmed", server.action_set_phantom(2, False)["ok"], True)
    channels._confirm_reader = lambda: with_value(2, "inputPhaseFlip", False)
    check("polarity normal on Mic 3 confirmed", server.action_set_polarity(3, False)["ok"], True)
    channels._confirm_reader = lambda: with_value(7, "inputDigitalGain", -6)
    check("USB 1 level -6 confirmed", server.action_set_channel_level("USB1", -6)["ok"], True)
    check("three records, all input writes",
          [model_tree.parse_record(r[5:5 + int.from_bytes(r[1:5], "little")])[:2] for r in WIRE],
          [([0xE0], "inputPower"), ([0xE1], "inputPhaseFlip"), ([0xE6], "inputDigitalGain")])

    WIRE.clear()
    try:
        channels._layout_input_write(DEV, [0xE6], "inputPower", 1)     # USB 1 reads -1: no phantom
        check("phantom on a line input is refused before sending", "sent", "refused")
    except devices.WriteRefused:
        check("phantom on a line input is refused before sending", len(WIRE), 0)
    res = server.action_set_mic_type(1, 11)
    check("mic type 11 is refused with a sentence, nothing sent", (res["ok"], len(WIRE)), (False, 0))
    res = server.action_set_gain(1, 80)
    check("gain 80 never reaches the desk", (res["ok"], len(WIRE)), (False, 0))
finally:
    (rcp_transfer.find_control_interface, rcp_hid.refuse_write_in_tests, rcp_hid.open_handle,
     rcp_hid.close_handle, rcp_read.read_model, channels._confirm_reader,
     channels.CONFIRM_GAP_S) = saved

print("\nEFFECTS: EFFECTS_PARAMETERS[strip] (strip-to-block rule PROVEN for strip 0 = Mic 1 = 0x23, heard live)")
check("read: echo/reverb per strip from the EFFECTS run", (
    channels.fx_values(BLOB, "reverbOn", None, DEV), channels.fx_values(BLOB, "reverbMix", "float", DEV)[:2]),
    ([False] * 9, [0.5, 0.5]))
mic1 = {c["name"]: c for c in channels.read_channels(BLOB)}["Mic1"]["effects"]
check("read_channels shows Mic 1's real effects", (mic1["reverb"], mic1["reverb_params"]["reverbModel"]),
      (False, next(v for p, v, k in root.kids[0x23].props if p == "reverbModel")))


def fx_record(prop, value, idx):
    if isinstance(value, bool):
        return rcp_transfer.build_path_prop_bool(prop, value, [idx])
    return rcp_transfer.build_global_prop_float(prop, idx, value)


for idx, prop in ((0x23, "reverbOn"), (0x23, "reverbMix"), (0x24, "echoOn"), (0x24, "echoDelay")):
    node = root.kids[idx]
    cur = next(v for p, v, k in node.props if p == prop)
    rec = fx_record(prop, cur, idx)
    n = int.from_bytes(rec[1:5], "little")
    check(f"{prop} on 0x{idx:02X}: record == 01 01 01 01 {idx:02X} + the dump's own bytes",
          rec[5:5 + n] == b"\x01\x01\x01\x01" + bytes([idx]) + dump_prop_bytes(node, prop), True)

ok_fx = [fx_record("reverbOn", True, 0x23), fx_record("echoOn", False, 0x2B),
         fx_record("reverbMix", 0.75, 0x23), fx_record("echoDecay", 0.0, 0x24),
         fx_record("reverbModel", 1.0, 0x25)]
check("echo/reverb on strips 0..8 pass the send layer", [send_ok(r) for r in ok_fx], [True] * 5)
bad_fx = {
    "the tenth block 0x2C": fx_record("reverbOn", True, 0x2C),
    "the Pro II fx base 0x26 + 9": fx_record("reverbOn", True, 0x2F),
    "reverbMix 1.5": fx_record("reverbMix", 1.5, 0x23),
    "reverbOn as int": rcp_transfer.build_path_prop_int("reverbOn", 1, [0x23]),
    "pitch shift as int": rcp_transfer.build_path_prop_int("pitchShiftOn", 1, [0x23]),
    "effectsIdx (a binding index: on the deny list)":
        rcp_transfer.build_path_prop_int("effectsIdx", 3, [0x23]),
    "fx preset marker on an EFFECTS block": rcp_transfer.build_global_prop_int(
        "channelCurrentFxPreset", 0x23, 2),
    "the later EFFECTS block 0xFB": fx_record("reverbOn", True, 0xFB),
    "a pad's effects block (PADEFFECTS child)": rcp_transfer.build_path_prop_bool(
        "reverbOn", True, [LAY.single("SOUNDPADS") + 2, 0]),
}
check("every other effects-shaped record is refused", [k for k, r in bad_fx.items() if send_ok(r)], [])
# What the generalised gate opens on the effects blocks: the names this dump publishes there with
# the same kind (no page writes the voice effects today; the gate allows them, the helpers don't).
check("the voice effects and the CHANNEL preset settings pass the generalised gate",
      [send_ok(r) for r in (fx_record("pitchShiftOn", True, 0x23),
                            fx_record("pitchShiftSemitones", 0.5, 0x23),
                            rcp_transfer.build_global_prop_int("channelCurrentFxPreset", 0x1A, 2),
                            rcp_transfer.build_global_prop_float("aphexBBTune", 0x1A, 0.5))],
      [True] * 4)


def with_value_at(idx, prop, new) -> bytes:
    node = root.kids[idx]
    old = dump_prop_bytes(node, prop)
    i = BLOB.find(old, *span(node))
    rec = fx_record(prop, new, idx)
    n = int.from_bytes(rec[1:5], "little")
    new_bytes = rec[10:5 + n]
    assert len(new_bytes) == len(old)
    return BLOB[:i] + new_bytes + BLOB[i + len(old):]


rcp_transfer.find_control_interface = lambda: fake
rcp_hid.refuse_write_in_tests = lambda: None
rcp_hid.open_handle = lambda iface, purpose: FakeHandle()
rcp_hid.close_handle = lambda dev, purpose: None
rcp_read.read_model = lambda *a, **k: BLOB
channels.CONFIRM_GAP_S = 0
try:
    WIRE.clear()
    channels._confirm_reader = lambda: with_value_at(0x23, "reverbOn", True)
    res = server.action_set_channel_effect("Mic1", "reverb", True)
    check("Mic 1 reverb on: confirmed, told so", (res["ok"], res["message"]),
          (True, "Mic 1 reverb turned on."))
    check("one record, reverbOn at EFFECTS 0x23 (Mic 1 is strip 0)",
          [model_tree.parse_record(r[5:5 + int.from_bytes(r[1:5], "little")])[:3] for r in WIRE],
          [([0x23], "reverbOn", True)])
    WIRE.clear()
    channels._confirm_reader = lambda: with_value_at(0x24, "echoMix", 0.37)
    res = server.action_set_channel_effect_param("Mic2", "echoMix", 0.37)
    check("Mic 2 echo mix 0.37 at 0x24, confirmed within float32 rounding",
          (res["ok"], WIRE[0][5:10].hex(" ")), (True, "01 01 01 01 24"))
    WIRE.clear()
    channels._confirm_reader = lambda: BLOB                    # the desk keeps reporting False
    res = server.action_set_channel_effect("Mic1", "echo", True)
    check("an unconfirmed effect is reported as NOT done, sent once",
          (res["ok"], "did not confirm" in res["message"], len(WIRE)), (False, True, 1))
    WIRE.clear()
    res = server.action_set_channel_effect("Mic3", "reverb", True)    # Mic 3 is in the pair, no strip
    check("a mic with no fader strip is refused, nothing sent", (res["ok"], len(WIRE)), (False, 0))
finally:
    (rcp_transfer.find_control_interface, rcp_hid.refuse_write_in_tests, rcp_hid.open_handle,
     rcp_hid.close_handle, rcp_read.read_model, channels._confirm_reader,
     channels.CONFIRM_GAP_S) = saved

print("\nFADER WRITES: THE ADDRESS FORM")
# Same proof as the input writes: name + NUL + value block byte for byte against the dump's own
# channelInputSource on that CHANNEL node, written back at its current value, behind the one-level
# address 01 01 01 01 <0x1A + strip>.
for strip in (1, 2, 6):                      # Mic 2 (code 1), the pair (6), the empty strip (-1)
    node = root.kids[0x1A + strip]
    cur = next(v for p, v, k in node.props if p == "channelInputSource")
    rec = rcp_transfer.build_global_prop_int("channelInputSource", 0x1A + strip, cur)
    n = int.from_bytes(rec[1:5], "little")
    want = b"\x01\x01\x01\x01" + bytes([0x1A + strip]) + dump_prop_bytes(node, "channelInputSource")
    check(f"strip {strip} <- code {cur}: record == 01 01 01 01 {0x1A + strip:02X} + the dump's bytes",
          rec[5:5 + n].hex(" "), want.hex(" "))
check("strip 1 <- code 1, every byte of the record",
      rcp_transfer.build_global_prop_int("channelInputSource", 0x1B, 1).hex(" "),
      (bytes.fromhex("03 1f 00 00 00 01 01 01 01 1b") + b"channelInputSource\x00"
       + bytes.fromhex("01 05 01 01 00 00 00")).ljust(256, b"\x00").hex(" "))

print("\nFADER WRITES: CHECKED BEFORE, CONFIRMED AFTER (fake desk, nothing leaves the process)")


class FakeDesk:
    """The dump, with each channelInputSource record it receives applied in place, as the desk
    would. `ignore` = strips that silently do not take a write; `stray` = (strip, code) the desk
    changes on its own after the next write (a reshuffle we must catch)."""
    OFFS = {}
    for _s in range(9):
        _node = root.kids[0x1A + _s]
        _i = BLOB.find(b"channelInputSource\x00", *span(_node))
        assert BLOB[_i + 19:_i + 22] == b"\x01\x05\x01"
        OFFS[_s] = _i + 22

    def __init__(self):
        self.blob, self.ignore, self.stray = BLOB, set(), None

    def set(self, strip, code):
        o = self.OFFS[strip]
        self.blob = self.blob[:o] + int(code).to_bytes(4, "little", signed=True) + self.blob[o + 4:]

    def take(self, rec):
        n = int.from_bytes(rec[1:5], "little")
        path, name, value, _kind, _end = model_tree.parse_record(rec[5:5 + n])
        if name == "channelInputSource" and path[0] - 0x1A not in self.ignore:
            self.set(path[0] - 0x1A, value)
        if self.stray:
            self.set(*self.stray)
            self.stray = None


DESK = FakeDesk()


class DeskHandle:
    def write(self, rec):
        WIRE.append(bytes(rec))
        DESK.take(rec)
        return len(rec)


def wire_writes():
    return [model_tree.parse_record(r[5:5 + int.from_bytes(r[1:5], "little")])[:3] for r in WIRE]


def desk_vec():
    return channels.strip_values(DESK.blob, "channelInputSource", dev=DEV)


saved = (rcp_transfer.find_control_interface, rcp_hid.refuse_write_in_tests, rcp_hid.open_handle,
         rcp_hid.close_handle, rcp_read.read_model, channels._confirm_reader, channels.CONFIRM_GAP_S)
rcp_transfer.find_control_interface = lambda: fake
rcp_hid.refuse_write_in_tests = lambda: None
rcp_hid.open_handle = lambda iface, purpose: DeskHandle()
rcp_hid.close_handle = lambda dev, purpose: None
rcp_read.read_model = lambda *a, **k: DESK.blob
channels._confirm_reader = lambda: DESK.blob
channels.CONFIRM_GAP_S = 0
START = [0, 1, 6, 7, 9, 8, -1, 11, 10]
try:
    check("the fake desk starts at the unit's layout", desk_vec(), START)

    WIRE.clear()
    rcp_transfer.set_fader_source(1, 1)
    check("set_fader_source(strip 1, code 1): one record at 0x1B, the builder's bytes",
          [r.hex(" ") for r in WIRE],
          [rcp_transfer.build_global_prop_int("channelInputSource", 0x1B, 1).hex(" ")])

    WIRE.clear()
    res = server.action_set_fader(3, 9)            # USB 2 onto fader 4: swaps with USB 1 on fader 5
    check("a swap is reported as done", res["ok"], True)
    check("a swap is two writes in strip order, at this desk's CHANNEL addresses", wire_writes(),
          [([0x1D], "channelInputSource", 9), ([0x1E], "channelInputSource", 7)])
    check("the desk shows exactly the swapped layout", desk_vec(), [0, 1, 6, 9, 7, 8, -1, 11, 10])
    check("the page says what both faders carry", res["message"],
          "Fader 4 now carries USB 2 (Secondary). Fader 5 now carries USB 1 (Main Stereo).")
    WIRE.clear()
    check("and back", (server.action_set_fader(3, 7)["ok"], desk_vec()), (True, START))

    WIRE.clear()
    res = server.action_set_fader(6, 8)            # Chat onto the empty fader 7: fader 6 empties
    check("onto the empty strip: Chat moves, fader 6 empties, nothing else",
          (res["ok"], desk_vec()), (True, [0, 1, 6, 7, 9, -1, 8, 11, 10]))
    server.action_set_fader(5, 8)
    check("and back", desk_vec(), START)

    WIRE.clear()
    res = server.action_set_fader(2, 7)            # the user chooses fader 3 itself
    check("the pair moves only when the user chooses its fader (swap with USB 1)",
          (res["ok"], desk_vec()), (True, [0, 1, 7, 6, 9, 8, -1, 11, 10]))
    server.action_set_fader(3, 7)                  # USB 1 back onto fader 4: the pair returns
    check("and back", desk_vec(), START)

    WIRE.clear()
    try:
        channels.set_layout_faders({2: 7, 3: 6}, chosen=3)
        check("moving the pair as a side effect is refused", "sent", "refused")
    except devices.WriteRefused as exc:
        check("moving the pair as a side effect is refused, nothing sent",
              (len(WIRE), "Mic 3 + 4" in str(exc)), (0, True))
    try:
        channels.set_layout_faders({1: 0}, chosen=1, planned_from=[1, 1, 6, 7, 9, 8, -1, 11, 10])
        check("a stale plan is refused", "sent", "refused")
    except devices.WriteRefused:
        check("a stale plan is refused, nothing sent", len(WIRE), 0)
    res = server.action_set_fader(0, 3)
    check("Mic 4 alone is refused by the page, nothing sent", (res["ok"], len(WIRE)), (False, 0))

    WIRE.clear()
    DESK.ignore = {4}                              # fader 5 does not take its write
    res = server.action_set_fader(3, 9)
    check("a half-landed swap is reported as NOT done, naming the fader",
          (res["ok"], "fader 5 USB 2 (Secondary) instead of USB 1 (Main Stereo)" in res["message"]),
          (False, True))
    check("each record sent once, not retried", len(WIRE), 2)
    DESK.ignore = set()
    DESK.set(3, 7)
    check("(fake desk put back)", desk_vec(), START)

    WIRE.clear()
    DESK.stray = (7, 10)                           # the desk moves another strip on its own
    res = server.action_set_fader(3, 9)
    check("a strip that changed unexpectedly fails the confirm",
          (res["ok"], "fader 8 Bluetooth instead of SMART pads" in res["message"]), (False, True))
    for s, c in enumerate(START):
        DESK.set(s, c)
    check("(fake desk put back)", desk_vec(), START)

    WIRE.clear()
    res = server.action_reset_faders()
    check("reset faders sends nothing", (res["ok"], len(WIRE)), (False, 0))
finally:
    (rcp_transfer.find_control_interface, rcp_hid.refuse_write_in_tests, rcp_hid.open_handle,
     rcp_hid.close_handle, rcp_read.read_model, channels._confirm_reader,
     channels.CONFIRM_GAP_S) = saved

print("\nCHANNEL PROCESSING, MUTE AND SOLO ON CHANNEL[strip]; MIC TYPE ON INPUTSOURCE")


def any_record(prop, value, idx):
    if isinstance(value, bool):
        return rcp_transfer.build_path_prop_bool(prop, value, [idx])
    if isinstance(value, float):
        return rcp_transfer.build_global_prop_float(prop, idx, value)
    return rcp_transfer.build_path_prop_int(prop, value, [idx])


def patched(idx, prop, new) -> bytes:
    """The fixture with one property on root child `idx` changed in place, as the desk reports it."""
    node = root.kids[idx]
    old = dump_prop_bytes(node, prop)
    i = BLOB.find(old, *span(node))
    rec = any_record(prop, new, idx)
    new_bytes = rec[10:5 + int.from_bytes(rec[1:5], "little")]
    assert len(new_bytes) == len(old)
    return BLOB[:i] + new_bytes + BLOB[i + len(old):]


def desk_after(blob: bytes, recs) -> bytes:
    """`blob` with every one-level record in `recs` applied in place, as a desk that takes them all
    would report it (a request, or a value whose size differs, is left out)."""
    out = blob
    for r in list(recs):
        n = int.from_bytes(r[1:5], "little")
        path, name, _value, kind, _end = model_tree.parse_record(r[5:5 + n])
        if kind == "pair" or len(path) != 1 or path[0] > 0xFF:
            continue
        node = root.kids[path[0]]
        if not any(p == name for p, _v, _k in node.props):
            continue
        old = dump_prop_bytes(node, name)
        i = BLOB.find(old, *span(node))
        new_bytes = r[10:5 + n]
        if i < 0 or len(new_bytes) != len(old):
            continue
        out = out[:i] + new_bytes + out[i + len(old):]
    return out


for idx, prop in ((0x1A, "eqHighGain"), (0x1A, "compressorOn"), (0x1B, "channelOutputMute"),
                  (0x1A, "channelCueEnable"), (0x1C, "channelBypassProcessing"),
                  (0xDF, "inputMicrophoneType")):
    node = root.kids[idx]
    cur = next(v for p, v, k in node.props if p == prop)
    rec = any_record(prop, cur, idx)
    n = int.from_bytes(rec[1:5], "little")
    check(f"{prop} on 0x{idx:02X}: record == 01 01 01 01 {idx:02X} + the dump's own bytes",
          rec[5:5 + n] == b"\x01\x01\x01\x01" + bytes([idx]) + dump_prop_bytes(node, prop), True)
check("every allowed strip property exists on CHANNEL 0x1A with its type",
      sorted(p for p, (k, _lo, _hi) in devices.LAYOUT_STRIP_WRITES.items()
             if next((kk for pp, _v, kk in root.kids[0x1A].props if pp == p), None) != k), [])
check("the float allow-list is exactly the Channels page's controls",
      devices.LAYOUT_STRIP_FLOATS == frozenset(server._PROCESSING_PROP_SET), True)
check("the module switches are all in the bool allow-list",
      set(channels.PROCESSING_MODULE_ON.values()) <= devices.LAYOUT_STRIP_BOOLS, True)

ok_strip = [any_record("eqHighGain", 0.7, 0x1A), any_record("compressorOn", False, 0x22),
            any_record("channelOutputMute", True, 0x1B), any_record("channelCueEnable", True, 0x1C),
            any_record("channelBypassProcessing", True, 0x1D),
            any_record("channelAdvancedProcessing", True, 0x1E), any_record("channelPan", 0.0, 0x1F),
            any_record("inputMicrophoneType", 10, 0xDF), any_record("inputMicrophoneType", 0, 0xE2)]
check("processing, mute, solo on strips 0..8 and mic type on sources 0..3 pass",
      [send_ok(r) for r in ok_strip], [True] * len(ok_strip))
bad_strip = {
    "strip 9 (0x23 is EFFECTS)": any_record("channelOutputMute", True, 0x23),
    "the later CHANNEL 0xFA": any_record("channelOutputMute", True, 0xFA),
    "EQ value 1.5": any_record("eqHighGain", 1.5, 0x1C),
    "mute as int": any_record("channelOutputMute", 1, 0x1A),
    "EQ as bool": any_record("eqHighGain", True, 0x1A),
    "preset marker as bool": any_record("channelCurrentFxPreset", True, 0x1A),
    "wireless mute as int": any_record("channelWirelessMute", 1, 0x1A),
    "a name CHANNEL does not publish (stray)": any_record("channelMadeUpSetting", True, 0x1A),
    "mic type on source 4": any_record("inputMicrophoneType", 2, 0xE3),
    "mic type 11": any_record("inputMicrophoneType", 11, 0xDF),
    "mic type as bool": any_record("inputMicrophoneType", True, 0xDF),
}
check("everything else is refused", [k for k, r in bad_strip.items() if send_ok(r)], [])
check("the other CHANNEL settings pass the generalised gate with the dump's kind",
      [send_ok(r) for r in (any_record("aphexBBTune", 0.5, 0x1A),
                            any_record("channelCurrentFxPreset", 2, 0x1A),
                            any_record("channelWirelessMute", True, 0x1A),
                            any_record("channelPanL", 0.5, 0x1A),
                            any_record("channelListenSource", 1, 0x1A),
                            any_record("channelTalkbackEnable", True, 0x1A))], [True] * 6)

rcp_transfer.find_control_interface = lambda: fake
rcp_hid.refuse_write_in_tests = lambda: None
rcp_hid.open_handle = lambda iface, purpose: FakeHandle()
rcp_hid.close_handle = lambda dev, purpose: None
rcp_read.read_model = lambda *a, **k: BLOB
channels.CONFIRM_GAP_S = 0


def sent():
    return [model_tree.parse_record(r[5:5 + int.from_bytes(r[1:5], "little")])[:3] for r in WIRE]


try:
    WIRE.clear()
    channels._confirm_reader = lambda: patched(0x1A, "eqHighGain", 0.7)
    res = server.action_set_channel_param("Mic1", "eqHighGain", 0.7)
    check("Mic 1 EQ high gain 0.7 at CHANNEL 0x1A, confirmed", (res["ok"], sent()),
          (True, [([0x1A], "eqHighGain", 0.7)]))
    WIRE.clear()
    channels._confirm_reader = lambda: patched(0x1B, "channelOutputMute", True)
    res = server.action_set_channel_mute("Mic2", True)
    check("Mic 2 mute at 0x1B (its strip 1), confirmed", (res["ok"], sent()),
          (True, [([0x1B], "channelOutputMute", True)]))
    WIRE.clear()
    channels._confirm_reader = lambda: patched(0x1D, "eqOn", False)
    res = server.action_set_channel_module("USB1", "eq", False)
    check("USB 1 EQ off at 0x1D (USB 1 sits on fader 4), confirmed", (res["ok"], sent()),
          (True, [([0x1D], "eqOn", False)]))
    WIRE.clear()
    channels._confirm_reader = lambda: patched(0x22, "channelOutputMute", False)
    res = server.action_set_fader_mute(8, False)
    check("fader 9 unmute by STRIP at 0x22, confirmed", (res["ok"], sent()),
          (True, [([0x22], "channelOutputMute", False)]))
    WIRE.clear()
    channels._confirm_reader = lambda: patched(0x1C, "channelCueEnable", True)
    res = server.action_set_fader_solo(2, True)
    check("the Mic 3 + 4 fader solo by strip at 0x1C, confirmed", (res["ok"], sent()),
          (True, [([0x1C], "channelCueEnable", True)]))
    WIRE.clear()
    channels._confirm_reader = lambda: patched(0x1A, "channelBypassProcessing", True)
    res = server.action_set_processing_enabled("Mic1", False)
    check("Mic 1 processing off = bypass True at 0x1A, confirmed", (res["ok"], sent()),
          (True, [([0x1A], "channelBypassProcessing", True)]))
    WIRE.clear()
    channels._confirm_reader = lambda: BLOB                    # the desk keeps reporting 0.5375
    res = server.action_set_channel_param("Mic1", "eqHighGain", 0.2)
    check("an unconfirmed EQ change is reported as NOT done, sent once",
          (res["ok"], "did not confirm" in res["message"], len(WIRE)), (False, True, 1))
    WIRE.clear()
    channels._confirm_reader = lambda: patched(0xDF, "inputMicrophoneType", 10)
    res = server.action_set_mic_type(1, 10)
    check("Mic 1 mic type SM7B (10) at INPUTSOURCE 0xDF, confirmed", (res["ok"], sent()),
          (True, [([0xDF], "inputMicrophoneType", 10)]))
    WIRE.clear()
    res = server.action_set_mic_type(1, 11)
    check("mic type 11 never reaches the desk", (res["ok"], len(WIRE)), (False, 0))
    WIRE.clear()
    preset = {**server._load_factory_presets(), **server._load_user_presets()}["Podcast Studio"]
    channels._confirm_reader = lambda: desk_after(BLOB, WIRE)    # a desk that takes every record
    res = server.action_apply_mic_preset(1, "Podcast Studio")
    want = channels._preset_records(0x1A, preset)
    check("a processing preset: the Pro II records in the Pro II order, at Mic 1's CHANNEL 0x1A, "
          "confirmed", (res["ok"], WIRE == want, {r[9] for r in WIRE}), (True, True, {0x1A}))
    WIRE.clear()
    channels._confirm_reader = lambda: BLOB                    # a desk that takes nothing
    res = server.action_apply_mic_preset(1, "Podcast Studio")
    check("a preset the desk does not take is reported as NOT done, each record sent once",
          (res["ok"], "did not confirm" in res["message"], len(WIRE)), (False, True, len(want)))
    WIRE.clear()
    channels._confirm_reader = lambda: desk_after(BLOB, WIRE)
    check("the preset name marker at 0x1A, confirmed",
          (channels.set_channel_preset_marker(1, 2) > 0, sent()),
          (True, [([0x1A], "channelCurrentFxPreset", 2)]))
    WIRE.clear()
    check("the wireless mic mute on Mic 2's strip 0x1B, confirmed",
          (server.action_set_channel_wireless_mute("Mic2", True)["ok"], sent()),
          (True, [([0x1B], "channelWirelessMute", True)]))
    WIRE.clear()
    check("Mic 1 instrument gain at INPUTSOURCE 0xDF, confirmed",
          (channels.set_instrument_gain(1, 20) > 0, sent()),
          (True, [([0xDF], "inputInstrumentGain", 20)]))
finally:
    (rcp_transfer.find_control_interface, rcp_hid.refuse_write_in_tests, rcp_hid.open_handle,
     rcp_hid.close_handle, rcp_read.read_model, channels._confirm_reader,
     channels.CONFIRM_GAP_S) = saved

print("\nROUTING: WHAT THE OUTPUT MIXES TAB IS GIVEN")
mm = server.matrix_model()
check("13 outputs, 12 sources, writable", (len(mm["outputs"]), len(mm["sources"]), mm["read_only"]),
      (13, 12, False))
check("reset all output mixes is no longer refused there", mm["reset_refused"], None)
check("the monitor mix is writable too", server.monitor_mix_model()["read_only"], False)
check("mix presets are writable", server.read_routing_schemes()["read_only"], False)
check("the note no longer says the names are unconfirmed", "confirmed" in mm["note"], False)
refuses("the Pro II cross-point selector", lambda: rcp_transfer.cross_point_selector(0, 1))
import layout_routing                       # noqa: E402
check("cell (output 3, Mic 2) = 0x37 + 13 * 1 + 3", layout_routing.cell_path(DEV, 3, 1), [0x47])
check("cell (output 12, source 11) = 0xD2, the last of the main run",
      layout_routing.cell_path(DEV, 12, 11), [0xD2])
check("output 9's mode = 0x36", layout_routing.mode_path(DEV, 9), [0x36])
for label, fn in (("output 13", lambda: layout_routing.cell_path(DEV, 13, 1)),
                  ("source 12 (a later run)", lambda: layout_routing.cell_path(DEV, 0, 12)),
                  ("output 10's mode (the later 0xFC run)", lambda: layout_routing.mode_path(DEV, 10))):
    try:
        fn()
        check(f"{label} refused", "allowed", "refused")
    except devices.WriteRefused:
        check(f"{label} refused", True, True)

print("\nROUTING WRITES: THE ADDRESS FORM, against the dump's own bytes")
# As for the input and fader writes: name + NUL + value block byte for byte against the same property
# on that node in the desk's own dump, written back at its current value, behind 01 01 01 01 <index>.
for idx, prop, rec in (
        (0x47, "mixDisabled", rcp_transfer.build_global_prop_bool("mixDisabled", False, 0x47)),
        (0x47, "mixMute", rcp_transfer.build_global_prop_bool("mixMute", False, 0x47)),
        (0xB9, "mixMute", rcp_transfer.build_global_prop_bool("mixMute", True, 0xB9)),   # row 10
        (0x37, "mixLevelWithAnchor", rcp_transfer.build_cross_point_level(0x37, 1.0, anchor=1)),
        (0x2D, "outputMixMinus", rcp_transfer.build_global_prop_int("outputMixMinus", 0x2D, 0)),
        (0x33, "outputMixMinus", rcp_transfer.build_global_prop_int("outputMixMinus", 0x33, 1))):
    n = int.from_bytes(rec[1:5], "little")
    want = b"\x01\x01\x01\x01" + bytes([idx]) + dump_prop_bytes(root.kids[idx], prop)
    check(f"{prop} on 0x{idx:02X}: record == 01 01 01 01 {idx:02X} + the dump's own bytes",
          rec[5:5 + n].hex(" "), want.hex(" "))
check("the request slot reads as a plain bool in the dump (never touched)",
      dump_prop_bytes(root.kids[0x47], "mixUnlinkRequest")[-3:].hex(" "), "01 01 03")
check("an unlink request on 0x47, every byte (the Pro II's constant composite)",
      rcp_transfer.build_mix_link_request(0x47, link=False).hex(" "),
      (bytes.fromhex("03 1f 00 00 00 01 01 01 01 47") + b"mixUnlinkRequest\x00"
       + bytes.fromhex("01 07 08 01 01 02 01 01 02")).ljust(256, b"\x00").hex(" "))
check("a level of 0.5 on 0x47, every byte",
      rcp_transfer.build_cross_point_level(0x47, 0.5).hex(" "),
      (bytes.fromhex("03 21 00 00 00 01 01 01 01 47") + b"mixLevelWithAnchor\x00"
       + bytes.fromhex("01 07 05") + b"0.5|0\x00").ljust(256, b"\x00").hex(" "))

print("\nROUTING WRITES: THE SEND LAYER TAKES THESE AND NOTHING ELSE")
B = rcp_transfer
ok_routing = {
    "exclude on 0x37": B.build_global_prop_bool("mixDisabled", True, 0x37),
    "include on 0xD2": B.build_global_prop_bool("mixDisabled", False, 0xD2),
    "mixMute on 0x47": B.build_global_prop_bool("mixMute", True, 0x47),
    "level 0.5": B.build_cross_point_level(0x47, 0.5),
    "level 0": B.build_cross_point_level(0x47, 0.0),
    "level 1": B.build_cross_point_level(0x47, 1.0),
    "level 0.89": B.build_cross_point_level(0x47, 0.89),
    "unlink request": B.build_mix_link_request(0x47, link=False),
    "link request": B.build_mix_link_request(0xD2, link=True),
    "the Mic 3 + 4 row (the page may)": B.build_global_prop_bool("mixDisabled", True, 0x37 + 13 * 6),
    "mode 0 on 0x2D": B.build_global_prop_int("outputMixMinus", 0x2D, 0),
    "mode 1 on 0x33": B.build_global_prop_int("outputMixMinus", 0x33, 1),
    "mode 2 on 0x36": B.build_global_prop_int("outputMixMinus", 0x36, 2),
}
check("cross-points of the main MIX run and the main run's modes pass",
      [k for k, r in ok_routing.items() if not send_ok(r)], [])
bad_routing = {
    "mixDisabled on 0x36 (a MIXMINUSES node)": B.build_global_prop_bool("mixDisabled", True, 0x36),
    "mixDisabled on 0xD3 (past the run)": B.build_global_prop_bool("mixDisabled", True, 0xD3),
    "mixDisabled on the later MIX run 0xFF": B.build_global_prop_bool("mixDisabled", True, 0xFF),
    "mixDisabled on the later MIX run 0x17D": B.build_channel_prop_bool("mixDisabled", True, 0x7D, 1),
    "a mode on the later run 0xFC": B.build_global_prop_int("outputMixMinus", 0xFC, 1),
    "a mode on a MIX node (the 0x39 stray)": B.build_global_prop_int("outputMixMinus", 0x39, 1),
    "the Pro II Chat mode 0x31 + 8": B.build_global_prop_int("outputMixMinus", 0x39, 2),
    "mode 3": B.build_global_prop_int("outputMixMinus", 0x2D, 3),
    "mode -1": B.build_global_prop_int("outputMixMinus", 0x2D, -1),
    "mode as bool": B.build_global_prop_bool("outputMixMinus", True, 0x2D),
    "mixDisabled as int": B.build_global_prop_int("mixDisabled", 0x37, 1),
    "a request as a bool": B.build_global_prop_bool("mixLinkRequest", True, 0x37),
    "mixLink as an int": B.build_global_prop_int("mixLink", 0x37, 1),
    "mixRcSyncLevelRequest": B.build_global_prop_float("mixRcSyncLevelRequest", 0x37, 0.5),
    "a two-level address": B.build_path_prop_bool("mixDisabled", True, [0x37, 0]),
    "outputMixMinus on STREAMERXSTREAMMIX 0xD6": B.build_global_prop_int("outputMixMinus", 0xD6, 1),
    "mixDisabled on the INPUTSOURCE stray 0xE3": B.build_global_prop_bool("mixDisabled", True, 0xE3),
}


def level_rec(path, text: str) -> bytes:
    """A mixLevelWithAnchor record carrying any text at any path (the builder only makes good ones)."""
    s = text.encode()
    body = (model_tree.encode_path(path) + b"mixLevelWithAnchor\x00"
            + bytes([0x01, len(s) + 2, 0x05]) + s + b"\x00")
    return (b"\x03" + len(body).to_bytes(4, "little") + body).ljust(256, b"\x00")


check("level_rec makes the builder's bytes", level_rec([0x47], "0.5|0"),
      B.build_cross_point_level(0x47, 0.5))
bad_routing["a level on the later MIX run 0x100 (two-byte address)"] = level_rec([0x100], "0.5|0")
bad_routing["a level on the later MIX run 0x17D"] = level_rec([0x17D], "0.5|0")
for text, label in (("0.555|0", "three decimals"), ("1.5|0", "above 1"), ("0.5|1", "anchor 1"),
                    ("1.0|0", "not the minimal form"), ("-0.1|0", "below 0"), ("0.5", "no anchor"),
                    ("nan|0", "not a number")):
    bad_routing[f"level {label} ({text})"] = level_rec([0x47], text)
check("every other routing-shaped record is refused", [k for k, r in bad_routing.items() if send_ok(r)],
      [])

print("\nROUTING WRITES: CHECKED BEFORE, CONFIRMED AFTER (fake desk, nothing leaves the process)")


class RoutingDesk:
    """The dump, taking routing records as the Pro II / Duo do (RODECASTER_PROTOCOL 5.3a): a value
    record replaces the stored value; an unlink request stores a cleared composite and drops
    mixLink; a link request does the same and puts the level back on its anchor; a level keeps the
    desk's own anchor. `ignore` = names the desk silently does not take; `mute_with_disable` copies
    mixDisabled into mixMute, as a Duo exclusion does."""
    CLEARED = bytes.fromhex("01 07 08 01 01 03 01 01 03")

    def __init__(self):
        self.blob, self.ignore, self.mute_with_disable = BLOB, set(), False

    def put(self, idx, prop, value_block: bytes):
        root_, _u, _t = parse_with_spans(self.blob)
        node = root_.kids[idx]
        i = self.blob.find(prop.encode() + b"\x00", *span(node))
        j = i + len(prop) + 1
        ln = self.blob[j]
        old_end = j + 1 + ln + int.from_bytes(self.blob[j + 1:j + 1 + ln], "little")
        new = self.blob[:j] + value_block + self.blob[old_end:]
        delta = len(new) - len(self.blob)
        total = int.from_bytes(new[:4], "little") + delta
        self.blob = total.to_bytes(4, "little") + new[4:]

    def value(self, idx, prop):
        return rcp_read.path_value(self.blob, [idx], prop)

    def take(self, rec):
        n = int.from_bytes(rec[1:5], "little")
        path, name, value, kind, _end = model_tree.parse_record(rec[5:5 + n])
        if name in self.ignore:
            return
        vb = rec[5 + 5 + len(name) + 1:5 + n]
        idx = path[0]
        if name in ("mixLinkRequest", "mixUnlinkRequest"):
            self.put(idx, name, self.CLEARED)
            link = name == "mixLinkRequest"
            self.put(idx, "mixLink", bytes([0x01, 0x01, 0x02 if link else 0x03]))
            if link:
                anchor = self.value(idx, "mixLevelWithAnchor").split("|", 1)[1].encode()
                self.put(idx, "mixLevelWithAnchor",
                         bytes([0x01, 2 * len(anchor) + 3, 0x05]) + anchor + b"|" + anchor + b"\x00")
            return
        if name == "mixLevelWithAnchor":
            anchor = self.value(idx, name).split("|", 1)[1].encode()
            level = value.split("|", 1)[0].encode()
            s = level + b"|" + anchor
            self.put(idx, name, bytes([0x01, len(s) + 2, 0x05]) + s + b"\x00")
            return
        self.put(idx, name, vb)
        if name == "mixDisabled" and self.mute_with_disable and value is True:
            self.put(idx, "mixMute", vb)          # and leaves it there on the way back in


RDESK = RoutingDesk()


class RoutingHandle:
    def write(self, rec):
        WIRE.append(bytes(rec))
        RDESK.take(rec)
        return len(rec)


saved = (rcp_transfer.find_control_interface, rcp_hid.refuse_write_in_tests, rcp_hid.open_handle,
         rcp_hid.close_handle, rcp_read.read_model, channels._confirm_reader, channels.CONFIRM_GAP_S,
         layout_routing._time.sleep)
rcp_transfer.find_control_interface = lambda: fake
rcp_hid.refuse_write_in_tests = lambda: None
rcp_hid.open_handle = lambda iface, purpose: RoutingHandle()
rcp_hid.close_handle = lambda dev, purpose: None
rcp_read.read_model = lambda *a, **k: RDESK.blob
channels._confirm_reader = lambda: RDESK.blob
channels.CONFIRM_GAP_S = 0
layout_routing._time.sleep = lambda s: None
try:
    check("the fake desk splices and still parses: output 3 mode", RDESK.value(0x30, "outputMixMinus"), 0)

    WIRE.clear()
    res = server.action_set_output_mode(3, 2)
    check("output 4 to Custom: one record at MIXMINUSES 0x30, confirmed, told so",
          (res["ok"], wire_writes(), RDESK.value(0x30, "outputMixMinus")),
          (True, [([0x30], "outputMixMinus", 2)], 2))
    check("and the page names the output plainly", res["message"],
          "Headphone 4 output set to Custom.")

    WIRE.clear()
    res = server.action_set_cross_point(3, 1, True)
    check("Mic 2 out of output 4: the unlink request then mixDisabled, both at 0x47, confirmed",
          (res["ok"], wire_writes()),
          (True, [([0x47], "mixUnlinkRequest", [(True, "bool"), (True, "bool")]),
                  ([0x47], "mixDisabled", True)]))
    check("the desk shows Mic 2 excluded there and nowhere else",
          [o for o in range(13) if RDESK.value(0x37 + 13 + o, "mixDisabled")], [3])
    WIRE.clear()
    res = server.action_set_cross_point(3, 1, False)
    check("and back in: link request then mixDisabled False",
          (res["ok"], [w[1:] for w in wire_writes()]),
          (True, [("mixLinkRequest", [(True, "bool"), (True, "bool")]), ("mixDisabled", False)]))

    WIRE.clear()
    RDESK.ignore = {"mixDisabled"}
    res = server.action_set_cross_point(3, 1, True)
    check("an exclusion the desk does not show is reported as NOT done, each record sent once",
          (res["ok"], "did not confirm" in res["message"], len(WIRE)), (False, True, 2))
    RDESK.ignore = set()

    WIRE.clear()
    res = server.action_set_output_mode(10, 2)
    check("the mode of output 11 (later run) is refused, nothing sent",
          (res["ok"], "outside" in res["message"], len(WIRE)), (False, True, 0))

    server.action_set_output_mode(3, 0)
    WIRE.clear()
    res = server.action_set_monitor_level(3, 1, 0.5)
    check("a monitor level on a Main Mix output: Custom first, then unlink, then the level",
          (res["ok"], wire_writes()),
          (True, [([0x30], "outputMixMinus", 2),
                  ([0x47], "mixUnlinkRequest", [(True, "bool"), (True, "bool")]),
                  ([0x47], "mixLevelWithAnchor", "0.5|0")]))
    check("the desk keeps its own anchor", RDESK.value(0x47, "mixLevelWithAnchor"), "0.5|0")

    WIRE.clear()
    RDESK.ignore = {"mixLevelWithAnchor"}
    res = server.action_set_monitor_level(3, 1, 0.25)
    check("a level the desk does not show is reported as NOT done",
          (res["ok"], "not at 25 percent" in res["message"]), (False, True))
    RDESK.ignore = set()

    WIRE.clear()
    res = server.action_reset_monitor_output(3)
    check("reset output 4: a link request on all 12 rows, then Main Mix, confirmed",
          (res["ok"], [w[1] for w in wire_writes()].count("mixLinkRequest"),
           wire_writes()[-1], RDESK.value(0x30, "outputMixMinus")),
          (True, 12, ([0x30], "outputMixMinus", 0), 0))
    check("every link request on output 4's own cells",
          sorted(w[0][0] for w in wire_writes()[:-1]), [0x37 + 13 * s + 3 for s in range(12)])
    check("Mic 2 follows its fader again", RDESK.value(0x47, "mixLevelWithAnchor"), "0|0")

    # A MIX PRESET: "Chat = my mic only" - Chat (output 8, MIXMINUSES 0x35) to Custom, Mic 1 in,
    # every other source out, one output only.
    WIRE.clear()
    res = server.action_apply_routing_scheme("chat-mic-only", builtin=True)
    chat_mode = 0x2D + 8
    check("Chat = my mic only: applied, Chat only, told so",
          (res["ok"], res.get("outputs"), res.get("failed")), (True, ["Chat"], 0))
    check("the mode first, at MIXMINUSES 0x35, then the 12 cells of output 8 only",
          (wire_writes()[0], sorted({w[0][0] for w in wire_writes()[1:]})),
          (([chat_mode], "outputMixMinus", 2), [0x37 + 13 * s + 8 for s in range(12)]))
    check("the desk shows Chat on Custom with only Mic 1 in it",
          (RDESK.value(chat_mode, "outputMixMinus"),
           [s for s in range(12) if not RDESK.value(0x37 + 13 * s + 8, "mixDisabled")]),
          (2, [0]))
    WIRE.clear()
    res = server.action_apply_routing_scheme("chat-normal", builtin=True)
    check("Chat back to normal: Mix Minus again, every source back in",
          (res["ok"], RDESK.value(chat_mode, "outputMixMinus"),
           [s for s in range(12) if RDESK.value(0x37 + 13 * s + 8, "mixDisabled")]),
          (True, 1, []))

    # RESET ALL OUTPUT MIXES: output 4 on Custom with Mic 2 out first, then the sweep.
    server.action_set_output_mode(3, 2)
    server.action_set_cross_point(3, 1, True)
    WIRE.clear()
    res = server.action_reset_all_outputs()
    modes_sent = [w for w in wire_writes() if w[1] == "outputMixMinus"]
    check("reset all output mixes: applied to all 13 outputs, nothing failed",
          (res["ok"], len(res.get("outputs") or []), res.get("failed")), (True, 13, 0))
    check("only the one mode that differs is written (output 4 back to Main Mix at 0x30), never "
          "the later run 0xFC..0xFE", modes_sent, [([0x30], "outputMixMinus", 0)])
    check("a link request and an include on every cell of the main grid, nothing past it",
          (sum(1 for w in wire_writes() if w[1] == "mixLinkRequest"),
           sum(1 for w in wire_writes() if w[1] == "mixDisabled" and w[2] is False),
           max(w[0][0] for w in wire_writes())), (156, 156, 0xD2))
    check("the desk then reads every output on its default mode, Mic 2 back in output 4",
          ([rcp_read.read_matrix_state(RDESK.blob)["modes"][o] for o in range(13)],
           RDESK.value(0x47, "mixDisabled")),
          ([0, 0, 0, 0, 0, 0, 1, 0, 1, 1, 1, 1, 1], False))
    WIRE.clear()
    res = server._apply_scheme({"outputs": {"CallMe2": {"mode": 2, "sources": {}}}}, "x")
    check("a preset that would change a later-run output's mode fails that output, nothing sent",
          (res["ok"], res["failed"], "outside" in res["message"], len(WIRE)), (False, 1, True, 0))

    WIRE.clear()
    try:
        layout_routing.set_cell(3, 1, "mixLevelWithAnchor", True)
        check("set_cell takes only the two bools", "allowed", "refused")
    except devices.WriteRefused:
        check("set_cell takes only the two bools, nothing sent", len(WIRE), 0)
finally:
    (rcp_transfer.find_control_interface, rcp_hid.refuse_write_in_tests, rcp_hid.open_handle,
     rcp_hid.close_handle, rcp_read.read_model, channels._confirm_reader, channels.CONFIRM_GAP_S,
     layout_routing._time.sleep) = saved

print("\nHEADPHONE TYPE AND USB MODE: THE ADDRESS FORM, against the dump's own bytes")
# Measured 7th October 2026 by dumps around changes in RODE's app: headphone 1 = HEADPHONE 0x07
# (headphoneType 0 -> 2 -> 1), USB 1 Input Standard / Expanded = OUTPUT 0x0B outputMultiMode 1 / 4.
for idx, prop in ((0x07, "headphoneType"), (0x08, "headphoneType"), (0x09, "headphoneType"),
                  (0x0A, "headphoneType"), (0x0B, "outputMultiMode")):
    node = root.kids[idx]
    cur = next(v for p, v, k in node.props if p == prop)
    rec = rcp_transfer.build_global_prop_int(prop, idx, cur)
    n = int.from_bytes(rec[1:5], "little")
    check(f"{prop} on 0x{idx:02X} ({node.name}): record == 01 01 01 01 {idx:02X} + the dump's bytes",
          rec[5:5 + n].hex(" "),
          (b"\x01\x01\x01\x01" + bytes([idx]) + dump_prop_bytes(node, prop)).hex(" "))
check("headphone 1 to Low sensitivity (2), every byte of the record",
      rcp_transfer.build_global_prop_int("headphoneType", 0x07, 2).hex(" "),
      (bytes.fromhex("03 1a 00 00 00 01 01 01 01 07") + b"headphoneType\x00"
       + bytes.fromhex("01 05 01 02 00 00 00")).ljust(256, b"\x00").hex(" "))
check("USB mode Expanded (4), every byte of the record",
      rcp_transfer.build_global_prop_int("outputMultiMode", 0x0B, 4).hex(" "),
      (bytes.fromhex("03 1c 00 00 00 01 01 01 01 0b") + b"outputMultiMode\x00"
       + bytes.fromhex("01 05 01 04 00 00 00")).ljust(256, b"\x00").hex(" "))

print("\nHEADPHONE TYPE AND USB MODE: THE SEND LAYER TAKES THESE AND NOTHING ELSE (0x0092 and 0x0094)")
ok_out = {f"headphoneType {v} on 0x{i:02X}": B.build_global_prop_int("headphoneType", i, v)
          for i in (0x07, 0x08, 0x09, 0x0A) for v in (0, 1, 2)}
ok_out.update({f"outputMultiMode {v}": B.build_global_prop_int("outputMultiMode", 0x0B, v)
               for v in (1, 4)})
ok_out["headphoneType as a path record"] = B.build_path_prop_int("headphoneType", 1, [0x07])
check("0x0092: headphoneType 0..2 on 0x07..0x0A and outputMultiMode 1 / 4 on 0x0B pass",
      [k for k, r in ok_out.items() if not send_ok(r)], [])
check("0x0094: the same records pass", [k for k, r in ok_out.items() if not send_ok_94(r)], [])
bad_out = {
    "headphoneType 3": B.build_global_prop_int("headphoneType", 0x07, 3),
    "headphoneType -1": B.build_global_prop_int("headphoneType", 0x07, -1),
    "headphoneType as bool": B.build_global_prop_bool("headphoneType", True, 0x07),
    "headphoneType as float": B.build_global_prop_float("headphoneType", 0x07, 1.0),
    "headphoneType on APP 0x06": B.build_global_prop_int("headphoneType", 0x06, 1),
    "headphoneType on OUTPUT 0x0B": B.build_global_prop_int("headphoneType", 0x0B, 1),
    "headphoneType two-level": B.build_path_prop_int("headphoneType", 1, [0x07, 0]),
    "headphoneColour": B.build_global_prop_int("headphoneColour", 0x07, 1),
    "a made-up headphone property": B.build_global_prop_int("headphoneVolume", 0x07, 1),
    "outputMultiMode 0 (Pro II Off, unmeasured here)": B.build_global_prop_int("outputMultiMode", 0x0B, 0),
    "outputMultiMode 2 (Pro II Post-Fader)": B.build_global_prop_int("outputMultiMode", 0x0B, 2),
    "outputMultiMode 3": B.build_global_prop_int("outputMultiMode", 0x0B, 3),
    "outputMultiMode 5": B.build_global_prop_int("outputMultiMode", 0x0B, 5),
    "outputMultiMode as bool": B.build_global_prop_bool("outputMultiMode", True, 0x0B),
    "outputMultiMode at the Pro II 0x0D (SYSTEM here)": B.build_global_prop_int("outputMultiMode", 0x0D, 4),
    "outputMultiMode on HEADPHONE 0x07": B.build_global_prop_int("outputMultiMode", 0x07, 4),
    "outputMultiMode on MASTERCHANNEL 0x0C": B.build_global_prop_int("outputMultiMode", 0x0C, 4),
    "outputMultiMode two-level": B.build_path_prop_int("outputMultiMode", 4, [0x0B, 0]),
    "a made-up OUTPUT property": B.build_global_prop_int("outputMultiModeX", 0x0B, 4),
    "outputMultiBypass as int": B.build_global_prop_int("outputMultiBypass", 0x0B, 1),
    "recordingMultitrackMode at the Pro II 0x0D (SYSTEM here)":
        B.build_global_prop_int("recordingMultitrackMode", 0x0D, 1),
    "outputMonAutoMute two-level": B.build_path_prop_bool("outputMonAutoMute", False, [0x0B, 0]),
    "the auto-mute's status flag (outputMonAutoMuteActive)":
        B.build_global_prop_bool("outputMonAutoMuteActive", False, 0x0B),
}
check("0x0092: every near miss is refused", [k for k, r in bad_out.items() if send_ok(r)], [])
check("0x0094: every near miss is refused", [k for k, r in bad_out.items() if send_ok_94(r)], [])
check("the rest of the OUTPUT block passes the generalised gate at 0x0B, on 0x0092 and 0x0094",
      [(send_ok(r), send_ok_94(r)) for r in (
          B.build_global_prop_bool("outputMultiBypass", True, 0x0B),
          B.build_global_prop_int("recordingMultitrackMode", 0x0B, 1),
          B.build_global_prop_bool("outputMonAutoMute", False, 0x0B))], [(True, True)] * 3)
check("the Pro II still takes its own headphone record unjudged",
      devices.refuse_unproven_pid(0x0037, B.build_global_prop_int("headphoneType", 0x09, 1)), None)

print("\nHEADPHONE TYPE AND USB MODE: WHAT THE ROUTING PAGE IS GIVEN")
rm = server.routing_model()
check("the rest of the master output block is writable now (generalised gate)",
      rm["output_write_read_only"], False)
check("the output names carry no (unconfirmed)",
      any("unconfirmed" in o["label"] for o in rm["matrix"]["outputs"]), False)
check("the USB mode and the headphones are writable under their own caps",
      (rm["usb_mode_read_only"], rm["headphones"]["read_only"]), (False, False))
# 2.62 shows outputMultiMode as its two halves (recorder.split_usb_multitrack): the output mode and
# USB 1 Input Standard / Expanded. Measured here: 1 and 4, i.e. pre-fader with Standard or Expanded.
check("only the measured output mode is offered (pre-fader)",
      [m for m, _l in rm["usb_multitrack_modes"]], [1])
check("both USB 1 Input choices are offered", [v for v, _l in rm["usb_expanded_labels"]],
      [False, True])
check("Expanded asks first, with the measured warning",
      ("Virtual Device Driver" in (rm["usb_expanded_confirm"] or ""),
       "default output" in (rm["usb_expanded_confirm"] or "")), (True, True))
check("four headphone outputs, three types",
      ([o["label"] for o in rm["headphones"]["outputs"]], [v for v, _l in rm["headphones"]["values"]]),
      (["Headphone 1", "Headphone 2", "Headphone 3", "Headphone 4"], [0, 1, 2]))
check("leaving High sensitivity (1) asks first, nothing else does",
      (rm["headphones"]["louder"], "louder" in (rm["headphones"]["louder_warning"] or "")),
      ([[1, 0], [1, 2]], True))
saved_pin = devices.force_device(devices.PRO2)
try:
    rp = server.routing_model()
    check("the Pro II page is upstream's: its three USB modes, output_write for all, no warnings",
          ([m for m, _l in rp["usb_multitrack_modes"]], rp["output_write_read_only"],
           rp["usb_mode_read_only"], rp["headphones"]["read_only"], rp["usb_expanded_confirm"],
           rp["headphones"]["louder"], rp["headphones"]["note"],
           len(rp["headphones"]["outputs"])),
          ([0, 1, 2], False, False, False, None, [], None, 2))
finally:
    devices.force_device(saved_pin)

print("\nHEADPHONE TYPE: CHECKED BEFORE, CONFIRMED AFTER (fake desk, nothing leaves the process)")
saved = (rcp_transfer.find_control_interface, rcp_hid.refuse_write_in_tests, rcp_hid.open_handle,
         rcp_hid.close_handle, rcp_read.read_model, channels._confirm_reader, channels.CONFIRM_GAP_S)
rcp_transfer.find_control_interface = lambda: fake
rcp_hid.refuse_write_in_tests = lambda: None
rcp_hid.open_handle = lambda iface, purpose: FakeHandle()
rcp_hid.close_handle = lambda dev, purpose: None
rcp_read.read_model = lambda *a, **k: BLOB
channels.CONFIRM_GAP_S = 0
try:
    WIRE.clear()
    channels._confirm_reader = lambda: patched(0x07, "headphoneType", 2)
    res = server.action_set_headphone_type(2, 0)
    check("headphone 1 to Low sensitivity: one record at HEADPHONE 0x07, confirmed, told so",
          (res["ok"], sent(), res["message"]),
          (True, [([0x07], "headphoneType", 2)], "Headphone 1 type set to Low sensitivity."))
    WIRE.clear()
    channels._confirm_reader = lambda: patched(0x0A, "headphoneType", 1)
    res = server.action_set_headphone_type(1, 3)
    check("headphone 4 to High sensitivity at 0x0A, confirmed", (res["ok"], sent()),
          (True, [([0x0A], "headphoneType", 1)]))
    WIRE.clear()
    channels._confirm_reader = lambda: BLOB                    # the desk keeps reporting 0
    res = server.action_set_headphone_type(1, 1)
    check("a type the desk does not show is reported as NOT done, sent once",
          (res["ok"], "did not confirm" in res["message"], len(WIRE)), (False, True, 1))
    WIRE.clear()
    res = server.action_set_headphone_type(3, 0)
    check("type 3 is refused, nothing sent", (res["ok"], len(WIRE)), (False, 0))
    res = server.action_set_headphone_type(1, 4)
    check("a fifth headphone output is refused, nothing sent", (res["ok"], len(WIRE)), (False, 0))
    rcp_read.read_model = lambda *a, **k: patched(0x07, "headphoneType", 2)
    live = server.read_routing_live()
    check("the live read gives one type per output, by HEADPHONE address, and the USB halves",
          (live["ok"], live["headphone_types"], live["usb_multitrack"], live["usb_expanded"],
           [m for m, _l in live["usb_multitrack_modes"]]), (True, [2, 0, 0, 0], 1, False, [1]))
    rcp_read.read_model = lambda *a, **k: BLOB
    refuses("headphone type 3 straight at the helper",
            lambda: layout_routing.set_headphone_type(0, 3))
finally:
    (rcp_transfer.find_control_interface, rcp_hid.refuse_write_in_tests, rcp_hid.open_handle,
     rcp_hid.close_handle, rcp_read.read_model, channels._confirm_reader,
     channels.CONFIRM_GAP_S) = saved

print("\nUSB MODE: SENT, THEN WAITED FOR UNDER THE OTHER PRODUCT ID (fake desk on a fake bus)")


class UsbDesk:
    """A desk that takes outputMultiMode, drops off the bus for `drop` polls, then comes back under
    the product id that mode belongs to (as measured), reading the new mode. `ignore`: it keeps its
    mode and never drops; `stay_away`: it never comes back; `lie`: it comes back reading this."""

    def __init__(self):
        self.mode, self.pid, self.gone = 1, PID, 0
        self.ignore, self.stay_away, self.lie, self.drop = False, False, None, 2

    def iface(self):
        if self.gone:
            if not self.stay_away:
                self.gone -= 1
            return None
        return rcp_hid.RodeInterface(path=b"", product_id=self.pid, interface_number=9,
                                     usage_page=0xFF00, usage=1, product_string="", serial_number="")

    def blob(self):
        return patched(0x0B, "outputMultiMode", self.mode)

    def take(self, rec):
        n = int.from_bytes(rec[1:5], "little")
        _path, name, value, _k, _e = model_tree.parse_record(rec[5:5 + n])
        if name != "outputMultiMode" or self.ignore:
            return
        self.mode = value if self.lie is None else self.lie
        self.pid = devices.LAYOUT_USBMODE_PIDS[value]
        self.gone = self.drop


UDESK = UsbDesk()


class UsbHandle:
    def write(self, rec):
        WIRE.append(bytes(rec))
        UDESK.take(rec)
        return len(rec)


saved = (rcp_transfer.find_control_interface, rcp_hid.refuse_write_in_tests, rcp_hid.open_handle,
         rcp_hid.close_handle, rcp_read.read_model, layout_routing._time.sleep,
         layout_routing.USBMODE_RECONNECT_S)
rcp_transfer.find_control_interface = UDESK.iface
rcp_hid.refuse_write_in_tests = lambda: None
rcp_hid.open_handle = lambda iface, purpose: UsbHandle()
rcp_hid.close_handle = lambda dev, purpose: None
rcp_read.read_model = lambda *a, **k: UDESK.blob()
layout_routing._time.sleep = lambda s: None
try:
    check("the fake desk reads Standard (1) on 0x0092",
          (rcp_read.block_value(UDESK.blob(), "OUTPUT", "outputMultiMode"), UDESK.pid), (1, PID))
    WIRE.clear()
    res = server.action_set_usb_expanded(True)
    check("Expanded: one record at OUTPUT 0x0B, sent while the desk was 0x0092",
          sent(), [([0x0B], "outputMultiMode", 4)])
    check("confirmed only after the desk came back as 0x0094 reading 4, and the reply says what "
          "Expanded does", (res["ok"], "0x0094" in res["message"],
                            "Main Multitrack" in res["message"], UDESK.pid),
          (True, True, True, devices.PID_LAYOUT_0094))
    WIRE.clear()
    res = server.action_set_usb_expanded(True)
    check("Expanded again: already there, nothing sent (no needless dropout)",
          (res["ok"], "already" in res["message"], len(WIRE)), (True, True, 0))
    res = server.action_set_usb_multitrack(1)
    check("pre-fader while in Expanded keeps Expanded: already there, nothing sent",
          (res["ok"], "already" in res["message"], len(WIRE)), (True, True, 0))
    WIRE.clear()
    res = server.action_set_usb_expanded(False)
    check("back to Standard: confirmed as 0x0092 (the record passed the 0x0094 send gate)",
          (res["ok"], sent(), "0x0092" in res["message"], UDESK.pid),
          (True, [([0x0B], "outputMultiMode", 1)], True, PID))

    WIRE.clear()
    for mode in (0, 2):
        res = server.action_set_usb_multitrack(mode)
        check(f"output mode {mode} is refused as not measured, nothing sent",
              (res["ok"], "measured" in res["message"], len(WIRE)), (False, True, 0))
    for mode in (3, -1):
        res = server.action_set_usb_multitrack(mode)
        check(f"output mode {mode} is refused as not valid, nothing sent",
              (res["ok"], "not valid" in res["message"], len(WIRE)), (False, True, 0))
    refuses("mode 2 straight at the helper", lambda: layout_routing.send_usb_mode(2))

    layout_routing.USBMODE_RECONNECT_S = 0      # the deadline is the first look
    UDESK.stay_away = True
    WIRE.clear()
    res = server.action_set_usb_expanded(True)
    check("a desk that is not back in time: sent, NOT claimed either way",
          (res["ok"], res.get("pending"), res["message"].startswith("Sent, not confirmed yet"),
           len(WIRE)), (False, True, True, 1))
    UDESK.__init__()
    UDESK.ignore = True
    WIRE.clear()
    res = server.action_set_usb_expanded(True)
    check("a desk that ignores it and stays 0x0092 on Standard: reported as NOT done",
          (res["ok"], res.get("pending"), "reports Standard" in res["message"], len(WIRE)),
          (False, None, True, 1))
    UDESK.__init__()
    UDESK.lie, UDESK.drop = 1, 0
    layout_routing.USBMODE_RECONNECT_S = 20
    WIRE.clear()
    res = server.action_set_usb_expanded(True)
    check("back as 0x0094 but reading another mode: reported as NOT done",
          (res["ok"], "0x0094" in res["message"] and "reports Standard" in res["message"]),
          (False, True))
finally:
    (rcp_transfer.find_control_interface, rcp_hid.refuse_write_in_tests, rcp_hid.open_handle,
     rcp_hid.close_handle, rcp_read.read_model, layout_routing._time.sleep,
     layout_routing.USBMODE_RECONNECT_S) = saved

print("\nTHE IDENTIFICATION HELPER (DevTools/pid0092_identify_outputs_live.py), never on a desk here")
import pid0092_identify_outputs_live as ident  # noqa: E402

CELL = {"mixLevelWithAnchor": "1|1", "mixLink": True, "mixMute": False, "mixDisabled": False}


def plan_refused(label, *a, **k):
    try:
        ident.plan(DEV, *a, **k)
        check(f"plan refuses {label}", "allowed", "refused")
    except ident.Refused as exc:
        check(f"plan refuses {label}", True, True)
        return str(exc)


check("source 6 is refused by name", "line input" in (plan_refused(
    "source 6 (the Mic 3 + 4 line pair)", 3, 6, 2, CELL, True, False) or ""), True)
plan_refused("Mic 1 without --allow-mic1", 3, 0, 2, CELL, True, False)
plan_refused("output 13", 13, 1, 2, CELL, True, False)
plan_refused("source 12 (a later MIX run)", 3, 12, 2, CELL, True, False)
plan_refused("a cell already excluded", 3, 1, 2, dict(CELL, mixDisabled=True), True, False)
plan_refused("a Main Mix output without --custom or --as-is", 3, 1, 0, CELL, False, False)
plan_refused("--custom on output 11 (mode in the later run)", 10, 1, 1, CELL, True, False)
p = ident.plan(DEV, 3, 1, 0, CELL, True, False)
check("output 4, Mic 2, --custom: one cell, mode switched",
      (p["path"], p["switch_mode"], p["mode_before"]), ([0x47], True, 0))
check("--as-is leaves the mode", ident.plan(DEV, 3, 1, 0, CELL, False, True)["switch_mode"], False)
check("an output already on Custom needs neither", ident.plan(DEV, 3, 1, 2, CELL, False, False)
      ["switch_mode"], False)
check("Mic 1 with --allow-mic1", ident.plan(DEV, 3, 0, 2, CELL, False, False, True)["path"], [0x3A])


class FakeStreams:
    """The two capture streams: Chat is loud while Mic 2 is in output 9 (index 8), else quiet."""
    meters = [("Chat", "", None), ("Main Multitrack (USB 1)", "", None)]

    def __init__(self, out):
        pass

    def sample(self):
        chat = 0.02 if RDESK.value(0x37 + 13 + 8, "mixDisabled") else 0.4
        return {"Chat": (chat, None), "Main Multitrack (USB 1)": (0.5, 0.3)}

    def close(self):
        pass


RDESK = RoutingDesk()
saved = (rcp_transfer.find_control_interface, rcp_hid.refuse_write_in_tests, rcp_hid.open_handle,
         rcp_hid.close_handle, rcp_read.read_model, channels._confirm_reader, channels.CONFIRM_GAP_S)
rcp_transfer.find_control_interface = lambda: fake
rcp_hid.refuse_write_in_tests = lambda: None
rcp_hid.open_handle = lambda iface, purpose: RoutingHandle()
rcp_hid.close_handle = lambda dev, purpose: None
rcp_read.read_model = lambda *a, **k: RDESK.blob
channels._confirm_reader = lambda: RDESK.blob
channels.CONFIRM_GAP_S = 0
LOG = []
try:
    WIRE.clear()
    rc = ident.run(3, 1, custom=True, hold_fn=lambda s, tick: tick(), out=LOG.append)
    check("a full run on output 4: back exactly as it was", (rc, RDESK.blob == BLOB), (0, True))
    check("four records: Custom, exclude, include, mode back - one cell, one output",
          wire_writes(), [([0x30], "outputMixMinus", 2), ([0x47], "mixDisabled", True),
                          ([0x47], "mixDisabled", False), ([0x30], "outputMixMinus", 0)])
    check("it says so", LOG[-1].startswith("RESTORED EXACTLY"), True)

    WIRE.clear()
    LOG.clear()
    RDESK.mute_with_disable = True             # the desk mutes the cell with the exclusion
    rc = ident.run(3, 1, custom=True, hold_fn=lambda s, tick: tick(), out=LOG.append)
    check("a mixMute the desk moved is put back on the same cell",
          (rc, RDESK.blob == BLOB, {w[0][0] for w in wire_writes() if w[1] != "outputMixMinus"},
           ([0x47], "mixMute", False) in wire_writes()),
          (0, True, {0x47}, True))
    RDESK.mute_with_disable = False

    WIRE.clear()
    LOG.clear()
    rc = ident.run(8, 1, custom=False, as_is=False, hold_fn=lambda s, tick: tick(),
                   out=LOG.append)
    check("output 9 is on Mix Minus: refused without a choice, nothing sent",
          (rc, len(WIRE)), (2, 0))
    rc = ident.run(8, 1, custom=True, measure=True, hold_fn=lambda s, tick: tick(),
                   streams_factory=FakeStreams, out=LOG.append)
    check("--measure on output 9: Chat dropped, Main Multitrack did not, restored",
          (rc, any("Chat: included 0.400, excluded 0.020" in x and "DROPPED" in x for x in LOG),
           any("Main Multitrack (USB 1)" in x and "no drop" in x for x in LOG), RDESK.blob == BLOB),
          (0, True, True, True))

    WIRE.clear()
    LOG.clear()
    rc = ident.run(3, 6, custom=True, hold_fn=lambda s, tick: tick(), out=LOG.append)
    check("source 6 through run(): refused, nothing sent", (rc, len(WIRE)), (2, 0))
    rc = ident.run(3, 1, custom=True, dry_run=True, out=LOG.append)
    check("--dry-run sends nothing", (rc, len(WIRE)), (0, 0))

    RDESK.ignore = {"mixDisabled"}
    rc = ident.run(3, 1, custom=True, hold_fn=lambda s, tick: tick(), out=LOG.append)
    check("an exclusion the desk does not take: reported, mode still put back",
          (rc, RDESK.value(0x30, "outputMixMinus"), any("WRITE PROBLEM" in x for x in LOG)),
          (1, 0, True))
    RDESK.ignore = set()
finally:
    (rcp_transfer.find_control_interface, rcp_hid.refuse_write_in_tests, rcp_hid.open_handle,
     rcp_hid.close_handle, rcp_read.read_model, channels._confirm_reader,
     channels.CONFIRM_GAP_S) = saved

print("\nTHE PAIR'S CHANNEL SELECTION IS WORDED BY ITS INPUT TYPE")
server._ann_input_types.clear()
server._ann_input_types[6] = 2                 # what the dump and the live encoder both said
SPOKEN.clear()
server._ann_handle_report(push(rcp_transfer.build_global_prop_int("systemChannelSelected", 0x0D, 2)),
                          {"bank": None, "pending": None, "at": 0.0, "chan_sel": -1})
check("Mic 3 + 4 on line input", SPOKEN,
      ["Mic 3 + 4 selected. Encoder sets its input level in decibels"])
server._ann_handle_report(push(rcp_transfer.build_path_prop_int("inputType", 0, [0xE5])),
                          {"bank": None, "pending": None, "at": 0.0})
SPOKEN.clear()
server._ann_handle_report(push(rcp_transfer.build_global_prop_int("systemChannelSelected", 0x0D, 2)),
                          {"bank": None, "pending": None, "at": 0.0, "chan_sel": -1})
check("an inputType push moves it to the mic wording", SPOKEN,
      ["Mic 3 + 4 selected. Encoder sets its gain"])

print("\nTHE GENERALISED GATE (tree_write): every expectation derived from the fixture dump")
OUT, MASTER, SYSTEM_, GUI_ = (LAY.single("OUTPUT"), LAY.single("MASTERCHANNEL"), LAY.single("SYSTEM"),
                             LAY.single("GUI"))
SOUND = LAY.single("SOUNDPADS")
LATER_CHANNEL = next(r.base for r in LAY.all_runs if r.name == "CHANNEL" and r.base != LAY.base("CHANNEL"))
NETWORK_ = LAY.single("NETWORK")


def kind_on(idx, prop):
    """The kind the dump has for `prop` on root child `idx` (None if it is not there)."""
    return next((k for p, _v, k in root.kids[idx].props if p == prop), None)


def gate(path, name, kind, value=None):
    value = {"bool": False, "int": 0, "float": 0.0, "str": ""}.get(kind) if value is None else value
    return devices.layout_tree_write_ok(DEV, path, name, value, kind)


check("the blocks come from the dump by name (OUTPUT, MASTERCHANNEL, SYSTEM, GUI, SOUNDPADS)",
      (root.kids[OUT].name, root.kids[MASTER].name, root.kids[SYSTEM_].name, root.kids[GUI_].name,
       root.kids[SOUND].name), ("OUTPUT", "MASTERCHANNEL", "SYSTEM", "GUI", "SOUNDPADS"))
check("accepts a real property on its real block with the dump's kind",
      [gate([OUT], "outputMonAutoMute", kind_on(OUT, "outputMonAutoMute")),
       gate([MASTER], "masterCompellorThreshold", kind_on(MASTER, "masterCompellorThreshold")),
       gate([SYSTEM_], "systemHapticSetting", kind_on(SYSTEM_, "systemHapticSetting")),
       gate([GUI_], "screenBrightness", kind_on(GUI_, "screenBrightness"))], [None] * 4)
refusals = {
    "a name absent from that block in the dump (stray)": gate([OUT], "masterCompellorOn", "bool"),
    "a name the desk publishes elsewhere, at the Pro II's OUTPUT 0x0D (SYSTEM here)":
        gate([0x0D], "outputMonAutoMute", "bool"),
    "the wrong kind": gate([OUT], "outputMonAutoMute", "int", 1),
    "a later-run duplicate block (CHANNEL 0x%02X)" % LATER_CHANNEL:
        gate([LATER_CHANNEL], "channelListenSource", kind_on(LATER_CHANNEL, "channelListenSource")),
    "a deny-list name (a command)": gate([SYSTEM_], "updateRebootRequested",
                                         kind_on(SYSTEM_, "updateRebootRequested")),
    "a deny-list name (a status flag)": gate([OUT], "outputMonAutoMuteActive", "bool"),
    "a deny-list name (transfer mode)": gate([SYSTEM_], "transferModeType", "int"),
    "an address that resolves to no block": gate([len(root.kids) + 5], "outputMonAutoMute", "bool"),
    "a block outside the setting blocks (NETWORK)": gate([NETWORK_], "btVisible", "bool"),
    "a pad setting on SOUNDPADS (pads are not opened)":
        gate([SOUND, 0], "padLoop", next(k for p, _v, k in root.kids[SOUND].kids[0].props
                                         if p == "padLoop")),
    "a composite value": gate([OUT], "outputMonAutoMute", "pair", []),
}
check("refuses each of these, with a sentence",
      {k: bool(v) for k, v in refusals.items()}, {k: True for k in refusals})
check("and send_record judges the same: a stray name never leaves",
      send_ok(rcp_transfer.build_global_prop_bool("masterCompellorOn", True, OUT)), False)
check("the narrow families keep their own, stricter checks (outputMultiMode 2 still refused)",
      send_ok(rcp_transfer.build_global_prop_int("outputMultiMode", OUT, 2)), False)
check("the routing requests keep their composite value through the routing family",
      send_ok(rcp_transfer.build_mix_link_request(0x37, link=False)), True)

print("\nTHE PRO II HELPERS AT THIS DESK'S BLOCKS (fake desk takes every record; confirmed after)")


class TreeHandle:
    def write(self, rec):
        WIRE.append(bytes(rec))
        TDESK[0] = desk_after(TDESK[0], [rec])
        return len(rec)


TDESK = [BLOB]
saved = (rcp_transfer.find_control_interface, rcp_hid.refuse_write_in_tests, rcp_hid.open_handle,
         rcp_hid.close_handle, rcp_read.read_model, rcp_read.read_model_stable,
         channels._confirm_reader, channels.CONFIRM_GAP_S, server._AUTO_MUTE_SETTLE_S)
rcp_transfer.find_control_interface = lambda: fake
rcp_hid.refuse_write_in_tests = lambda: None
rcp_hid.open_handle = lambda iface, purpose: TreeHandle()
rcp_hid.close_handle = lambda dev, purpose: None
rcp_read.read_model = lambda *a, **k: TDESK[0]
rcp_read.read_model_stable = lambda *a, **k: TDESK[0]
channels._confirm_reader = lambda: TDESK[0]
channels.CONFIRM_GAP_S = 0
server._AUTO_MUTE_SETTLE_S = 0
try:
    for label, fn, want in (
            ("monitor auto-mute off: OUTPUT", lambda: server.action_set_auto_mute("monitor", False),
             [([OUT], "outputMonAutoMute", False)]),
            ("Bluetooth auto-mute on: OUTPUT", lambda: server.action_set_auto_mute("bt", True),
             [([OUT], "outputBTAutoMute", True)]),
            ("the master Compellor off: MASTERCHANNEL", lambda: server.action_set_master_compellor(False),
             [([MASTER], "masterCompellorOn", False)]),
            ("Compellor threshold 0.5: MASTERCHANNEL",
             lambda: server.action_set_compellor_param("threshold", 0.5),
             [([MASTER], "masterCompellorThreshold", 0.5)]),
            ("output delay on, 250 ms: MASTERCHANNEL, two records",
             lambda: server.action_set_output_delay(True, 250),
             [([MASTER], "masterDelayOn", True), ([MASTER], "masterDelaySeconds", 0.25)]),
            ("the recorder's multitrack Off: OUTPUT", lambda: server.action_set_multitrack(0),
             [([OUT], "recordingMultitrackMode", 0)]),
            ("recording format MP3: OUTPUT", lambda: server.action_set_recording_format(1),
             [([OUT], "recordingCompressionQuality", 1)]),
            ("the USB raw switch: OUTPUT", lambda: server.action_set_usb_bypass(True),
             [([OUT], "outputMultiBypass", True)]),
            ("the monitor speaker level 40 percent: OUTPUT",
             lambda: server.action_set_monitor_speaker_level(40),
             [([OUT], "outputMonLevel", 0.4)]),
            ("the Bluetooth output level 30 percent: OUTPUT",
             lambda: server.action_set_bt_output_level(30), [([OUT], "outputBTLevel", 0.3)]),
            ("the monitor speakers muted: OUTPUT", lambda: server.action_set_output_mute("monitor", True),
             [([OUT], "outputMonMute", True)]),
            ("screen brightness 200: GUI", lambda: server.action_set_unit_setting("screenBrightness", 200),
             [([GUI_], "screenBrightness", 200)]),
            ("haptics 2: SYSTEM", lambda: server.action_set_unit_setting("systemHapticSetting", 2),
             [([SYSTEM_], "systemHapticSetting", 2)])):
        WIRE.clear()
        res = fn()
        check(f"{label} 0x{want[0][0][0]:02X}, confirmed", (res["ok"], sent()), (True, want))
    WIRE.clear()
    res = server.action_set_unit_setting("autoBrightness", 1)
    check("a unit setting this desk publishes as another kind (autoBrightness is an int here) is "
          "refused with a sentence, nothing sent",
          (res["ok"], "not bool" in res["message"], len(WIRE)), (False, True, 0))
    check("the Device page is told which settings this desk refuses, and why",
          sorted(s["prop"] for s in server.read_unit_settings()["settings"] if s.get("write_refused")),
          ["autoBrightness", "screenDimAfterSeconds"])
    check("and the MIDI switch (an int on this desk, a bool record)",
          bool(server.read_unit_settings().get("midi_refused")), True)
    WIRE.clear()
    res = server.action_set_midi(True)
    check("so MIDI on is refused with that sentence, nothing sent", (res["ok"], len(WIRE)), (False, 0))
    TDESK[0] = BLOB
    WIRE.clear()
    res = server.action_set_master_compellor(True)                  # the fixture already reads True
    channels._confirm_reader = lambda: patched(MASTER, "masterCompellorOn", False)
    WIRE.clear()
    res = server.action_set_master_compellor(True)
    check("a setting the desk does not take is reported as NOT done, sent once",
          (res["ok"], "did not confirm" in res["message"], len(WIRE)), (False, True, 1))
    channels._confirm_reader = lambda: TDESK[0]
    rm = server._recorder_payload(BLOB)
    check("the Recording page: multitrack and format writable, the transport still not",
          (rm["multitrack_writable"], rm["format_writable"], rm["writable"]), (True, True, False))
    lv = server.fader_levels_live()
    check("the Levels tab: monitor and Bluetooth output open, no fader-level sliders",
          (lv["read_only"], lv["faders"]), (False, []))
    check("fader levels stay refused (which faders are virtual is not measured here)",
          server.action_set_fader_level(6, 50)["ok"], False)

    # A PROFILE RESTORE through the same helpers, at this desk's own blocks.
    TDESK[0] = BLOB
    WIRE.clear()
    prof = {"product_id": PID,
            "channels": [{"name": "Mic1", "kind": "mic", "level_db": 40}],
            "routing": {"compellor_on": False, "monitor_auto_mute": False, "usb_multitrack": 4,
                        "fader_levels": [0] * 9, "monitor_level": 0.3},
            "unit": {"screenBrightness": 180, "autoBrightness": 1}}
    import profiles                          # noqa: E402
    real_pace = profiles.RESTORE_PACE
    profiles.RESTORE_PACE = 0
    res = profiles.apply(prof, fader_count=9, fader_code_set=set(), input_strip_vec_index={},
                         valid_source_codes=set(), mic_channels=(1, 2, 3, 4))
    profiles.RESTORE_PACE = real_pace
    got = {(tuple(p), n): v for p, n, v in sent()}
    check("a profile restore writes each setting at this desk's block, confirmed",
          (got.get(((0xDF,), "inputMicrophoneGain")), got.get(((MASTER,), "masterCompellorOn")),
           got.get(((OUT,), "outputMonAutoMute")), got.get(((OUT,), "outputMonLevel")),
           got.get(((GUI_,), "screenBrightness"))), (40, False, False, 0.3, 180))
    check("and says what it did not restore: the USB mode, the fader levels, autoBrightness",
          (res["failed"], any("USB" in e for e in res["errs"]),
           any("fader levels" in e for e in res["errs"]),
           any("autoBrightness" in e for e in res["errs"]),
           any(n == "outputMultiMode" for _p, n, _v in sent())), (3, True, True, True, False))
finally:
    (rcp_transfer.find_control_interface, rcp_hid.refuse_write_in_tests, rcp_hid.open_handle,
     rcp_hid.close_handle, rcp_read.read_model, rcp_read.read_model_stable,
     channels._confirm_reader, channels.CONFIRM_GAP_S, server._AUTO_MUTE_SETTLE_S) = saved

print("\nTHE PRO II AND THE DUO: the same helpers send exactly the bytes they always did")
CAUGHT = []
real_send = rcp_transfer.send_record
rcp_transfer.send_record = lambda rec: CAUGHT.append(rec) or len(rec)
try:
    for desk in (devices.PRO2, devices.DUO):
        devices.force_device(desk)
        CAUGHT.clear()
        rcp_transfer.set_monitor_auto_mute(False)
        rcp_transfer.set_master_compellor(True)
        rcp_transfer.set_master_compellor_param("gain", 0.25)
        rcp_transfer.set_output_delay(True, 0.5)
        rcp_transfer.set_multitrack_mode(1)
        rcp_transfer.set_monitor_level(0.5)
        rcp_transfer.set_unit_setting("screenBrightness", 200)
        rcp_transfer.set_unit_setting("systemHapticSetting", 1)
        rcp_transfer.set_unit_setting("outputBTLevel", 0.5)
        rcp_transfer.set_midi_control(True)
        rcp_transfer.set_headphone_type(2, 0x0A)
        B_ = rcp_transfer
        check(f"{desk.name}: every record at its Pro II selector, byte for byte", CAUGHT, [
            B_.build_global_prop_bool("outputMonAutoMute", False, 0x0D),
            B_.build_global_prop_bool("masterCompellorOn", True, 0x0E),
            B_.build_global_prop_float("masterCompellorGain", 0x0E, 0.25),
            B_.build_global_prop_bool("masterDelayOn", True, 0x0E),
            B_.build_global_prop_float("masterDelaySeconds", 0x0E, 0.5),
            B_.build_global_prop_int("recordingMultitrackMode", 0x0D, 1),
            B_.build_global_prop_float("outputMonLevel", 0x0D, 0.5),
            B_.build_global_prop_int("screenBrightness", 0x07, 200),
            B_.build_global_prop_int("systemHapticSetting", 0x0F, 1),
            B_.build_global_prop_float("outputBTLevel", 0x0D, 0.5),
            B_.build_global_prop_bool("systemMidiControl", True, 0x0F),
            B_.build_global_prop_int("headphoneType", 0x0A, 2)])
finally:
    rcp_transfer.send_record = real_send
    devices.force_device(DEV)

devices.force_device(None)
layout.forget()
print()
if FAILED:
    print(f"{FAILED} CHECK(S) FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
