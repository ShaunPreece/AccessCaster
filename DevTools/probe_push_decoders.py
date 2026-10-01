"""WHICH PUSHED PROPERTIES SIT WHERE THE ANNOUNCER'S DECODERS DO NOT LOOK?

`_ann_decode_int` once read the value block at a FIXED offset after the property name, which is
wrong for `encoderSignal`: it carries four bytes of its own first, so every encoder turn decoded as
None, the caller's `or 0` made that a delta of zero, and a guard built on it was inert for as long
as it existed. That one scans a short window now. `_ann_decode_bool` and `_ann_decode_float` still
read at the fixed offset, and nobody had checked whether anything else has the same shape. The
symptom is a feature that is INERT rather than wrong, which is the kind nothing reports.

THE METHOD, AND WHY IT DOES NOT NEED TO KNOW WHICH PROPERTIES THE ANNOUNCER HANDLES.
`model_tree.parse_record` decodes a push record by the grammar, from its address downwards, and is
what every other reader in the app trusts. The announcer cannot use it: it works on a raw packet
and finds a name by scanning, because a press arrives as one report with no promise about where in
it the name sits. So the two are different code paths over the same bytes, and this runs BOTH over
every push the desk sends and compares them. Any property where they disagree is a blind spot.

TWO THINGS THE FIRST VERSION OF THIS GOT WRONG, both worth keeping in mind for any push probe:

  * A MODEL DUMP ARRIVES ON REPORT 04 TOO. A dump frame is a slice of the tree rather than one
    addressed record, so its names break at arbitrary points: `channelCueEnable` turns up as
    `CueEnable` and matches nothing. `_push_record_len` is the structural filter that drops them,
    and without it this reported "the desk pushed nothing" for properties it had pushed perfectly.
  * THE DESK DOES NOT ECHO A WRITE AS THE PROPERTY YOU WROTE. Muting Mic 1 from software pushes
    `mixMute` on each of that source's thirteen cross-points and `outputMonAutoMuteActive`, and
    never `channelOutputMute`. So a probe cannot summon a chosen property by writing it; it takes
    what the desk sends and measures that.

Every write is reversible and restored. Usage:

    python DevTools/probe_push_decoders.py
"""
from __future__ import annotations
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import channels
import model_tree
import rcp_hid
import rcp_read
import rcp_transfer
import server

FAILED = 0
NAME = server._ANN_NAME        # the announcer's own name scan, so what it would find is what we test
MARKER_WINDOW = 24             # how far past a name to look for the value marker
# kind (as model_tree reports it) -> the announcer decoder that claims to read it
DECODER = {"bool": server._ann_decode_bool,
           "float": server._ann_decode_float,
           "int": server._ann_decode_int}


class RawPushes:
    """Push records, kept WHOLE and also parsed. `rcp_read.PushListener` discards the raw frame,
    and the decoders under test work on the raw packet."""

    def __init__(self):
        self.records = []      # (frame, path, name, value, kind)
        self.seen = 0          # report-04 frames of any kind
        self.dumps = 0         # of those, dump slices, filtered out
        self._stop = threading.Event()
        self._t = None

    def __enter__(self):
        iface = rcp_hid.find_control_interface()
        if iface is None:
            raise RuntimeError("no control interface (is the unit connected?)")
        dev = rcp_hid.open_handle(iface, "decoder probe")
        self._t = threading.Thread(target=self._run, args=(dev,), daemon=True)
        self._t.start()
        time.sleep(0.3)
        return self

    def _run(self, dev):
        try:
            dev.set_nonblocking(False)
            while not self._stop.is_set():
                try:
                    rep = dev.read(rcp_read.BULK_LEN, timeout_ms=400)
                except Exception:
                    return
                if not rep or rep[0] != 0x04:
                    continue
                frame = bytes(rep)
                self.seen += 1
                n = rcp_read._push_record_len(frame)
                if n is None:
                    self.dumps += 1
                    continue
                try:
                    path, name, val, kind, _end = model_tree.parse_record(frame[5:5 + n])
                except ValueError:
                    continue
                self.records.append((frame, tuple(path), name, val, kind))
        finally:
            rcp_hid.close_handle(dev, "decoder probe")

    def __exit__(self, *exc):
        self._stop.set()
        if self._t:
            self._t.join(timeout=2.0)


def value_gap(frame: bytes, at: int):
    """Bytes between the end of a property name and its `01 <len> <type>` marker, or None.

    The marker is `01`, a length, then a type byte (01 int, 02/03 bool, 04 float, 05 string),
    which is `model_tree._value`'s grammar rather than a shape invented here."""
    v = frame[at:at + MARKER_WINDOW]
    for i in range(len(v) - 2):
        if v[i] == 0x01 and v[i + 2] in (0x01, 0x02, 0x03, 0x04, 0x05):
            return i
    return None


def audit(pushes) -> None:
    """Run the announcer's decoders over every push and compare with the grammar's own answer."""
    global FAILED
    rows = defaultdict(lambda: {"n": 0, "gaps": set(), "kinds": set(),
                                "agree": 0, "disagree": 0, "unreachable": 0, "example": None})
    for frame, _path, name, val, kind in pushes.records:
        r = rows[name]
        r["n"] += 1
        r["kinds"].add(kind)
        decode = DECODER.get(kind)
        # Find the name the way the announcer does. A name it cannot find at all is its own kind of
        # blind spot, and a different one from decoding at the wrong offset.
        hit = next((m for m in NAME.finditer(frame) if server._ann_is(m.group().decode(), name)),
                   None)
        if hit is None:
            r["unreachable"] += 1
            continue
        at = hit.end() + 1
        r["gaps"].add(value_gap(frame, at))
        if decode is None:
            continue                      # no announcer decoder claims this kind (str, pair, ...)
        got = decode(frame, at)
        same = (got is not None and abs(got - val) < 1e-9) if kind == "float" else (got == val)
        if same:
            r["agree"] += 1
        else:
            r["disagree"] += 1
            r["example"] = (val, got)

    print(f"\n{len(pushes.records)} push record(s) over {len(rows)} propert"
          f"{'y' if len(rows) == 1 else 'ies'} "
          f"({pushes.dumps} dump frame(s) filtered out)\n")
    print(f"  {'property':<28} {'kind':<6} {'n':>4}  {'gap':<8} verdict")
    for name in sorted(rows):
        r = rows[name]
        kinds = "/".join(sorted(r["kinds"]))
        gaps = ",".join("?" if g is None else str(g) for g in sorted(r["gaps"], key=str))
        if r["unreachable"]:
            verdict = f"NAME NOT FOUND by the announcer's scan in {r['unreachable']}"
            FAILED += 1
        elif r["disagree"]:
            verdict = f"MISMATCH: grammar {r['example'][0]!r}, decoder {r['example'][1]!r}"
            FAILED += 1
        elif r["agree"]:
            verdict = "agrees with the grammar"
        else:
            verdict = "no announcer decoder for this kind"
        print(f"  {name:<28} {kinds:<6} {r['n']:>4}  {gaps:<8} {verdict}")


print("=== the announcer's decoders against the grammar, on real pushes ===")
print("Generating pushes with reversible writes; everything is put back.\n")

restores = []
with RawPushes() as pushes:
    # 1. A channel MUTE. Pushes mixMute on each of that source's cross-points (bool) and
    #    outputMonAutoMuteActive (bool).
    sel = channels.proc_selector_for_strip(0)
    rcp_transfer.send_record(rcp_transfer.build_global_prop_bool(channels.MUTE_PROP, True, sel))
    time.sleep(1.8)
    rcp_transfer.send_record(rcp_transfer.build_global_prop_bool(channels.MUTE_PROP, False, sel))
    time.sleep(1.8)

    # 2. The MONITOR LEVEL (float), and back.
    was_mon = rcp_read.first(rcp_read.read_model_stable(fresh=True), "outputMonLevel")
    target = 0.4 if abs((was_mon or 0) - 0.4) > 0.05 else 0.6
    rcp_transfer.set_monitor_level(target)
    time.sleep(1.5)
    rcp_transfer.set_monitor_level(was_mon or 0.0)
    time.sleep(1.5)
    restores.append(("outputMonLevel", was_mon))

    # 3. A CROSS-POINT LEVEL (string: 'level|anchor'), on Headphone 1 x Mic 1, relinked after.
    xsel = rcp_transfer.cross_point_selector(0, 0)
    rcp_transfer.send_record(rcp_transfer.build_mix_link_request(xsel, link=False))
    time.sleep(0.8)
    rcp_transfer.set_cross_point_level(0, 0, 0.5)
    time.sleep(1.2)
    rcp_transfer.send_record(rcp_transfer.build_mix_link_request(xsel, link=True))
    time.sleep(1.2)

    # 4. A PROCESSING FLOAT on strip 0, and back.
    blob = rcp_read.read_model_stable(fresh=True)
    was_pan = rcp_read.path_value(blob, [sel], "pan", None)
    if isinstance(was_pan, float):
        channels.write_chain(0, {"pan": 0.6 if abs(was_pan - 0.6) > 0.05 else 0.4})
        time.sleep(1.2)
        channels.write_chain(0, {"pan": was_pan})
        time.sleep(1.2)
        restores.append(("pan", was_pan))

audit(pushes)

print("\nRESTORES, read back rather than assumed:")
blob = rcp_read.read_model_stable(fresh=True)
checks = [("channelOutputMute strip 0", rcp_read.values(blob, "channelOutputMute")[0], False)]
for name, want in restores:
    if name == "outputMonLevel":
        checks.append((name, rcp_read.first(blob, "outputMonLevel"), want))
    elif name == "pan":
        checks.append((name, rcp_read.path_value(blob, [sel], "pan", None), want))
cell = (rcp_read.read_matrix_levels(blob).get("levels") or {}).get(0, {}).get(0, {})
checks.append(("Headphone 1 x Mic 1 relinked", cell.get("linked"), True))
for label, got, want in checks:
    same = (abs(got - want) < 0.01 if isinstance(want, float) and isinstance(got, float)
            else got == want)
    FAILED += 0 if same else 1
    print(f"  {'ok  ' if same else 'FAIL'} {label}: {got!r} (wanted {want!r})")

print("\nNOT COVERED, and it needs somebody at the desk: `mutePressed`, `soloPressed`,")
print("`encoderPressed` and `encoderSignal` arrive only from a physical control, and")
print("`channelOutputMute` / `channelCueEnable` are pushed only when the DESK changes them.")
print(f"\nFAILURES: {FAILED}")
raise SystemExit(1 if FAILED else 0)
