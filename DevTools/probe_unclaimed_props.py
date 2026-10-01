"""THE SIX READABLE PROPERTIES NOTHING IN THE APP USES: are they writable, and do they react?

`appOutputDevice`, `appMonitorMix` and `appRecording` sit in an APP block the app has never
written; `eqParamModeHigh` / `Mid` / `Low` sit in GUI beside `debugMode` and `metering`, which is
why a touchscreen presentation setting was always the likelier guess than anything audible.

ADDRESSED FROM THE TREE, NOT FROM A GUESSED SELECTOR. Each node is found by walking the live model
and the property name is read off that node, so every write here goes to a confirmed member of a
confirmed node. That is the condition the write-safety rule asks for: a wrong selector does not
error, it appends a stray that only a factory reset clears.

WHAT THIS CAN AND CANNOT SETTLE. A read-back proves the desk STORED the value and nothing more.
This desk has a list of properties that accept a write, hand it back exactly, and do nothing
whatever, so "writable" is never "working". The one extra signal taken here is whether the desk
PUSHES anything of its own while the value is changed, which is a hint that something downstream
noticed. Anything audible or visible on the touchscreen still needs a person.

Every value is restored and the model's node, property and name counts are compared at the end.

    python DevTools/probe_unclaimed_props.py
"""
from __future__ import annotations
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import model_tree
import rcp_hid
import rcp_read
import rcp_transfer

FAILED = 0
# (property, the value to try). The kind comes off the live tree rather than being asserted here.
TRIALS = [("eqParamModeHigh", 1), ("eqParamModeMid", 1), ("eqParamModeLow", 1),
          ("appMonitorMix", 1), ("appRecording", 1), ("appOutputDevice", "AccessCasterProbe")]


class Pushes:
    """Push records only, so a model dump cannot be mistaken for the desk reacting."""

    def __init__(self):
        self.records = []
        self._stop = threading.Event()
        self._t = None

    def __enter__(self):
        dev = rcp_hid.open_handle(rcp_hid.find_control_interface(), "unclaimed probe")
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
                n = rcp_read._push_record_len(frame)
                if n is None:
                    continue
                try:
                    _p, name, val, _k, _e = model_tree.parse_record(frame[5:5 + n])
                except ValueError:
                    continue
                self.records.append((name, val))
        finally:
            rcp_hid.close_handle(dev, "unclaimed probe")

    def __exit__(self, *exc):
        self._stop.set()
        if self._t:
            self._t.join(timeout=2.0)


def locate(root, prop):
    """(node path, kind, current value) for a property, found by walking the tree."""
    for n in model_tree.walk(root):
        for p, v, k in n.props:
            if p == prop:
                return n.path(), k, v, n.name
    return None, None, None, None


def counts(root):
    nodes = list(model_tree.walk(root))
    return {"nodes": len(nodes), "props": sum(len(n.props) for n in nodes),
            "names": len({p for n in nodes for p, _v, _k in n.props})}


def write(prop, path, kind, value):
    if kind == "str":
        rec = rcp_transfer.build_global_prop_string(prop, value, path[0])
    elif kind == "int":
        rec = rcp_transfer.build_global_prop_int(prop, path[0], int(value))
    elif kind == "float":
        rec = rcp_transfer.build_global_prop_float(prop, path[0], float(value))
    else:
        raise RuntimeError(f"no builder for kind {kind!r}")
    rcp_transfer.send_record(rec)


root0, _c, _t = model_tree.parse(rcp_read.read_model_stable(fresh=True))
before = counts(root0)
print("=== six properties nothing in the app uses ===")
print(f"baseline: {before}\n")
print(f"  {'property':<20} {'block':<6} {'was':<20} {'wrote':<20} {'read back':<20} reaction")

for prop, trial in TRIALS:
    path, kind, was, block = locate(root0, prop)
    if path is None:
        FAILED += 1
        print(f"  {prop:<20} NOT FOUND in the live model")
        continue
    if len(path) != 1:
        FAILED += 1
        print(f"  {prop:<20} is at a nested path {path}; the global builders address root children")
        continue
    try:
        with Pushes() as pushes:
            write(prop, path, kind, trial)
            time.sleep(1.5)
        root1, _c, _t = model_tree.parse(rcp_read.read_model_stable(fresh=True))
        _p, _k, got, _b = locate(root1, prop)
    finally:
        write(prop, path, kind, was)
        time.sleep(0.8)
    root2, _c, _t = model_tree.parse(rcp_read.read_model_stable(fresh=True))
    _p, _k, back, _b = locate(root2, prop)
    took = got == trial
    restored = back == was
    others = sorted({n for n, _v in pushes.records if n != prop})
    reaction = ("nothing pushed" if not pushes.records
                else ("echoed itself only" if others == [] else f"also pushed {others}"))
    FAILED += 0 if restored else 1
    print(f"  {prop:<20} {block:<6} {was!r:<20} {trial!r:<20} {got!r:<20} "
          f"{'stored' if took else 'REFUSED'}, {reaction}"
          + ("" if restored else "  <-- DID NOT RESTORE"))

after = counts(root2)
same = after == before
FAILED += 0 if same else 1
print(f"\n  {'ok  ' if same else 'FAIL'} no stray: {after} (was {before})")
print(f"\nREMEMBER: a read-back proves the desk STORED it, never that anything acts on it.")
print(f"FAILURES: {FAILED}")
raise SystemExit(1 if FAILED else 0)
