"""Unit tests for MIX SCHEMES: capture, the built-ins and apply. NO DEVICE, no HID, no writes.

Every device write is intercepted, so this is safe to run with the desk plugged in and recording.
What it pins down is the part a live test cannot show cheaply: that a scheme is keyed by NAME, so a
Pro II scheme applied to a Duo writes the outputs both desks have and SKIPS the ones only the Pro II
has, instead of writing Headphone 3's mix onto whatever sits at that index. That is the whole reason
the format stores keys rather than indices, and an index bug there is silent on the wire.

Run: python DevTools/test_schemes.py
"""
from __future__ import annotations
import os
import sys
import tempfile

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import devices
import fake_model
import rcp_transfer
import routing_schemes

FAILED = 0
routing_schemes.WRITE_PACE = 0        # nothing leaves the process, so nothing needs pacing


def check(label, got, want):
    global FAILED
    if got == want:
        print(f"  ok   {label}: {got!r}")
    else:
        FAILED += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def check_true(label, got):
    check(label, bool(got), True)


class as_device:
    """Force the ACTIVE device model, so device-aware code can be tested with no desk attached."""

    def __init__(self, dev):
        self.dev = dev

    def __enter__(self):
        self.saved = devices.get_active_device
        devices.get_active_device = lambda: self.dev
        return self.dev

    def __exit__(self, *exc):
        devices.get_active_device = self.saved
        return False


class capture_writes:
    """Intercept every device write routing_schemes.apply can make, recording it instead.

    set_cross_point_excluded and set_cross_point_level are replaced WHOLE, which also removes their
    _check_cross_point guard - that guard is the subject of its own test below, and leaving it in
    here would make an out-of-range write raise instead of showing up in the log."""

    def __init__(self):
        self.log = []

    def __enter__(self):
        self.saved = {n: getattr(rcp_transfer, n) for n in
                      ("set_output_routing", "set_cross_point_excluded", "set_cross_point_level",
                       "send_record", "build_mix_link_request")}
        log = self.log
        rcp_transfer.set_output_routing = lambda o, m: log.append(("mode", o, m))
        rcp_transfer.set_cross_point_excluded = lambda o, s, ex: log.append(("xpt", o, s, ex))
        rcp_transfer.set_cross_point_level = lambda o, s, v: log.append(("level", o, s, round(v, 3)))
        rcp_transfer.build_mix_link_request = lambda sel, link: ("link", sel, link)
        rcp_transfer.send_record = lambda rec: log.append(rec)
        return self

    def __exit__(self, *exc):
        for n, fn in self.saved.items():
            setattr(rcp_transfer, n, fn)
        return False

    def modes(self):
        return [(o, m) for kind, o, m in [e for e in self.log if e[0] == "mode"]]

    def xpts(self):
        return [e for e in self.log if e[0] == "xpt"]

    def levels(self):
        return [e for e in self.log if e[0] == "level"]


# --- a synthetic model blob carrying a full matrix ------------------------------------------------
# 516 cross-points (the guard value both desks report) at read index 13*source + output, plus the
# MIXMINUSES nodes read_output_modes reads. mixLevelWithAnchor is the ASCII 'level|anchor'
# string: equal halves = LINKED (follows the fader), different = a manual level.

# The desk we pretend to read: Chat (output 8) on Custom carrying Mic 1 only, Headphone 1 (output 0)
# on Custom with USB 1 (source 7) pulled down to 0.4 by hand, everything else normal.
CHAT_OUT, HP1_OUT, MIC1_SRC, USB1_SRC = 8, 0, 0, 7
MODES = [0] * 11
MODES[CHAT_OUT] = 2
MODES[HP1_OUT] = 2
# EVERY row a scheme covers, off the device model rather than written out: that is the wide,
# mode-independent set (the nine plus RODE's four Expanded rows), and a fixture narrower than the
# reader walks fabricates "included" for the rows it leaves out.
PRO2_SOURCES = devices.PRO2.matrix.all_source_indices
VIRTUAL_A_SRC = 14


def cell(o, s):
    """(excluded, level_string) for one cross-point of the synthetic desk."""
    if o == CHAT_OUT:
        return (s != MIC1_SRC), "1|1"
    if o == HP1_OUT and s == USB1_SRC:
        return False, "0.4|0"          # unlinked: level differs from the anchor
    return False, "0.75|0.75"          # linked: level == anchor


MIX = devices.PRO2.matrix.block
parts = []
for i in range(devices.PRO2.matrix.cell_count):   # the count the guard checks against
    s, o = divmod(i, devices.PRO2.matrix.row_stride)
    excluded, lvl = cell(o, s) if s in PRO2_SOURCES and o < 11 else (False, "0|0")
    parts.append((MIX, "mixDisabled", excluded))
    parts.append((MIX, "mixLevelWithAnchor", lvl))
# THE GRID SITS INSIDE ITS NAMED BLOCK, because the matrix read is block-scoped: RCSYNCMIX carries
# the same cross-point names, so an unscoped read returns two grids concatenated.
MODE_ROWS = [("MIXMINUSES", "outputMixMinus", m) for m in MODES]
blob = fake_model.encode(MODE_ROWS + parts)


# --- capture --------------------------------------------------------------------------------------

print("CAPTURE: what a scheme records")
tmp = tempfile.mkdtemp(prefix="ac_schemes_test_")
routing_schemes.SCHEMES_DIR = tmp
with as_device(devices.PRO2):
    cap = routing_schemes.capture("show", blob)
check_true("capture reports ok", cap.get("ok"))
scheme = cap["scheme"]
check("every output of this desk is recorded, by KEY",
      sorted(scheme["outputs"]), sorted(o["key"] for o in devices.PRO2.matrix.outputs))
check("the Chat output's mode is Custom", scheme["outputs"]["Chat"]["mode"], 2)
check("Chat carries Mic 1", scheme["outputs"]["Chat"]["sources"]["Mic1"]["excluded"], False)
check("and excludes the SMART pads (where a screen reader would go down the line)",
      scheme["outputs"]["Chat"]["sources"]["SMARTpads"]["excluded"], True)
check("an EXCLUDED cross-point records nothing but that (its level is inert and drifts; "
      "measured on the desk)",
      scheme["outputs"]["Chat"]["sources"]["Mic2"], {"excluded": True})
check("a linked cross-point records no level of its own",
      "level" in scheme["outputs"]["Headphone2"]["sources"]["USB1"], False)
check("an UNLINKED one records its manual level",
      scheme["outputs"]["Headphone1"]["sources"]["USB1"], {"excluded": False, "linked": False,
                                                           "level": 0.4})
# RODE'S EXPANDED ROWS ARE IN THE SCHEME WHATEVER MODE THE DESK IS IN. The desk publishes their
# cross-points either way, and a scheme whose shape followed the mode left a Virtual A cell behind
# when one saved in Standard was applied in Expanded.
check("a Virtual A cross-point is recorded like any other",
      scheme["outputs"]["Chat"]["sources"]["VirtualA"], {"excluded": True})
check("and the wide set is what got captured, not the display set",
      len(scheme["outputs"]["Chat"]["sources"]), len(PRO2_SOURCES))
check("the desk it was taken from is stamped on it", scheme["device_name"], devices.PRO2.name)

print("\nCAPTURE: one output only, which is what makes a preset safe to fire mid-show")
with as_device(devices.PRO2):
    one = routing_schemes.capture("chatonly", blob, only="Chat")
    bad = routing_schemes.capture("nope", blob, only="Headphone9")
check_true("capture of a single output reports ok", one.get("ok"))
check("and records that output ALONE", sorted(one["scheme"]["outputs"]), ["Chat"])
check("with the same content the whole-desk capture gave it",
      one["scheme"]["outputs"]["Chat"], scheme["outputs"]["Chat"])
check("an output this desk does not have is refused rather than saved empty", bad.get("ok"), False)
with as_device(devices.PRO2):
    # The SCOPE SUFFIX is what the dropdown reads out, so it is pinned: a preset that rewrites
    # everything and one that touches a single output must never sound alike (Michael's report).
    check("a one-output preset says so", routing_schemes.scope_text(one["scheme"]), "Chat only")
    check("a whole-desk one says so", routing_schemes.scope_text(scheme), "every output")
with as_device(devices.DUO):
    check("scope is answered for the desk in front of you, not the one it was saved on",
          routing_schemes.scope_text(scheme), "every output")

print("\nCAPTURE: a matrix that changed shape is refused, not half-saved")
with as_device(devices.PRO2):
    short = routing_schemes.capture("show", fake_model.encode(MODE_ROWS + parts[:40]))
check("capture refuses when the cross-point count is wrong", short.get("ok"), False)
check_true("and says why", "changed shape" in (short.get("message") or ""))

print("\nSAVE and LIST (flat JSON files, one per scheme)")
with as_device(devices.PRO2):
    routing_schemes.save("show", scheme)
check("the saved scheme is listed", routing_schemes.list_names(), ["show"])
check("and loads back identical", routing_schemes.load("show")["scheme"] == scheme, True)
check("a name with a path separator in it is refused", routing_schemes.safe_name("a/b"), None)
check("as is one with a dot, so no scheme can be named ..", routing_schemes.safe_name(".."), None)


# --- apply ----------------------------------------------------------------------------------------

print("\nAPPLY: the writes, and the order they go out in")
with as_device(devices.PRO2), capture_writes() as w:
    res = routing_schemes.apply({"outputs": {"Chat": scheme["outputs"]["Chat"]}})
check_true("apply reports ok", res["ok"])
check("only the output the scheme names is touched", res["outputs"], ["Chat"])
check("its mode is written FIRST (leaving Custom would discard the cross-points)",
      w.log[0], ("mode", 8, 2))
check("every source of the desk is set on that output", len(w.xpts()), len(PRO2_SOURCES))
check("Mic 1 included, the rest excluded",
      sorted({(s, ex) for _k, _o, s, ex in w.xpts()}),
      sorted([(s, s != MIC1_SRC) for s in PRO2_SOURCES]))
check("and nothing else was written", w.modes(), [(8, 2)])

print("\nAPPLY: manual levels ride with the mix")
with as_device(devices.PRO2), capture_writes() as w:
    routing_schemes.apply({"outputs": {"Headphone1": scheme["outputs"]["Headphone1"]}})
check("the one unlinked cross-point gets its level back", w.levels(), [("level", 0, 7, 0.4)])
check("and it is unlinked first, or the write would be ignored",
      ("link", rcp_transfer.cross_point_selector(0, 7), False) in w.log, True)

print("\nAPPLY: a level is never written where it would not mean anything")
main_mix = {"outputs": {"Headphone2": {"mode": 0, "sources": {
    "USB1": {"excluded": False, "linked": False, "level": 0.4}}}}}
with as_device(devices.PRO2), capture_writes() as w:
    routing_schemes.apply(main_mix)
check("an output on Main Mix takes no manual level (it follows the faders)", w.levels(), [])
excluded_level = {"outputs": {"Headphone2": {"mode": 2, "sources": {
    "USB1": {"excluded": True, "linked": False, "level": 0.4}}}}}
with as_device(devices.PRO2), capture_writes() as w:
    routing_schemes.apply(excluded_level)
check("nor does a source that is excluded from the mix", w.levels(), [])


# --- the reason it is keyed by name ----------------------------------------------------------------

print("\nCROSS-DEVICE: a Pro II scheme applied to a Duo")
with as_device(devices.DUO), capture_writes() as w:
    res = routing_schemes.apply(scheme)
duo_out_keys = [o["key"] for o in devices.DUO.matrix.outputs]
check("only outputs the Duo really has are written",
      sorted({o for _k, o, _m in [e for e in w.log if e[0] == "mode"]}),
      sorted(o["index"] for o in devices.DUO.matrix.outputs))
check("the Pro II-only outputs are named as skipped, not guessed at",
      sorted(res["skipped"]), sorted(["Headphone3", "Headphone4", "Mic3", "Mic4"]))
check("the Duo's Headphone 1 is index 0 on both desks, so its mix does carry over",
      ("mode", 0, 2) in w.log, True)
check("and nothing was written to index 2 or 3 (the Pro II's Headphone 3 and 4)",
      [e for e in w.log if e[0] in ("mode", "xpt") and e[1] in (2, 3)], [])

print("\nMIX MINUS is not written to an output that does not offer it")
mm = {"outputs": {"Headphone1": {"mode": 1, "sources": {}}}}
with as_device(devices.PRO2), capture_writes() as w:
    routing_schemes.apply(mm)
check("it falls back to that output's default mode", w.modes(), [(0, 0)])


# --- the built-ins ----------------------------------------------------------------------------------

print("\nBUILT-INS: derived from the connected desk, never stored")
with as_device(devices.PRO2):
    b = {x["id"]: x for x in routing_schemes.builtins()}
check("three of them on the Pro II", sorted(b), ["chat-mic-only", "chat-normal", "normal"])
check("only 'everything normal' is marked destructive",
      [k for k in b if b[k]["destructive"]], ["normal"])
check("'everything normal' names every output", len(b["normal"]["scheme"]["outputs"]),
      len(devices.PRO2.matrix.outputs))
check("and puts each one back to its default mode",
      {k: v["mode"] for k, v in b["normal"]["scheme"]["outputs"].items()},
      {o["key"]: o["default_mode"] for o in devices.PRO2.matrix.outputs})
check("the Chat presets touch the CHAT OUTPUT ONLY",
      (sorted(b["chat-mic-only"]["scheme"]["outputs"]),
       sorted(b["chat-normal"]["scheme"]["outputs"])), (["Chat"], ["Chat"]))
check("'Chat back to normal' writes MIX MINUS, not the Main Mix the old button left behind",
      b["chat-normal"]["scheme"]["outputs"]["Chat"]["mode"], 1)
check("and re-includes every source, following the faders",
      all(not c["excluded"] and c["linked"]
          for c in b["chat-normal"]["scheme"]["outputs"]["Chat"]["sources"].values()), True)
check("'Chat = my mic only' keeps Mic 1 and nothing else",
      sorted(k for k, c in b["chat-mic-only"]["scheme"]["outputs"]["Chat"]["sources"].items()
             if not c["excluded"]), ["Mic1"])

with as_device(devices.DUO):
    duo_b = {x["id"]: x for x in routing_schemes.builtins()}
check("the Duo gets its own three, built from ITS matrix", sorted(duo_b), sorted(b))
check("and its 'everything normal' names only the Duo's outputs",
      sorted(duo_b["normal"]["scheme"]["outputs"]), sorted(duo_out_keys))
check("'Chat = my mic only' excludes the Duo's Headset, which the Pro II has no source for",
      duo_b["chat-mic-only"]["scheme"]["outputs"]["Chat"]["sources"]["Headset"]["excluded"], True)

print("\nBUILT-IN 'everything normal' == the old reset sweep, write for write")
with as_device(devices.PRO2), capture_writes() as w:
    routing_schemes.apply(b["normal"]["scheme"])
check("every output gets its default mode",
      sorted(w.modes()), sorted((o["index"], o["default_mode"]) for o in devices.PRO2.matrix.outputs))
check("every cross-point of every output is re-included",
      len(w.xpts()), len(devices.PRO2.matrix.outputs) * len(PRO2_SOURCES))
check("and none of them is excluded", [e for e in w.xpts() if e[3]], [])

print("\nDESCRIBE: what the page says before you commit to a scheme")
with as_device(devices.PRO2):
    text = routing_schemes.describe(b["chat-mic-only"]["scheme"])
check_true("names the output and the mode", "Sets Chat to a custom mix" in text)
check_true("and says how many sources survive", "carrying 1 of 9 sources" in text)
# THE COUNT FOLLOWS THE MODE while the stored scheme does not: the page shows 13 rows in Expanded
# and 9 in Standard, so a fixed denominator would name rows the user cannot see.
with as_device(devices.PRO2):
    wide = routing_schemes.describe(b["chat-mic-only"]["scheme"], expanded=True)
check_true("and counts Expanded's four when the desk is in Expanded",
           "carrying 1 of 13 sources" in wide)
with as_device(devices.PRO2):
    full = routing_schemes.describe(b["normal"]["scheme"])
check("a whole-desk scheme is summarised, not listed output by output (it is read aloud)",
      full, "Sets all 11 outputs to their normal mix.")
with as_device(devices.DUO):
    warn = routing_schemes.describe(scheme)
check_true("a scheme with outputs this desk lacks says so up front", "would be skipped" in warn)

print("\nDELETE")
check_true("delete reports ok", routing_schemes.delete("show").get("ok"))
check("the store is empty again", routing_schemes.list_names(), [])
check("deleting what is not there is an error, not a silent success",
      routing_schemes.delete("show").get("ok"), False)

for f in os.listdir(tmp):
    os.remove(os.path.join(tmp, f))
os.rmdir(tmp)

print()
print("FAILURES:", FAILED)
sys.exit(1 if FAILED else 0)
