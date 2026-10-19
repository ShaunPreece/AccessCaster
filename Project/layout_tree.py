"""ANY SETTING ON A LAYOUT DESK (PID 0x0092 / 0x0094): checked before, confirmed after.

The generalised write path behind cap "tree_write" (devices.layout_tree_write_ok). The Pro II write
helpers build exactly the records they always have; on a layout desk they build them at THIS desk's
block for that setting (devices.layout_block_selector: found by name in its own dump, never a Pro II
constant) and hand them here instead of to the send layer. Three layers, the same as every other
layout write (channels._layout_write, layout_routing):
  * BEFORE: each record must parse as ONE property and pass devices.layout_write_ok (its narrow
    family, or the generalised gate), and a FRESH dump must show that property on that very node,
    with that kind, so a write can only change a value that is already there;
  * the send layer re-checks every finished record (devices.refuse_unproven_pid);
  * AFTER: a fresh dump must show every value written, or WriteNotConfirmed says what the desk
    reports instead. The desk does not push back a change the host made, so reading is the only
    way to know. Records are sent once and never retried; only the reading is repeated.

A batch (a processing preset, a profile section) is checked as a whole before the first record goes
out, sent on one handle, and confirmed in one pass.

Nothing here is used on the Pro II or the Duo: their helpers call rcp_transfer.send_record exactly
as before (rcp_transfer._send_setting).
"""
from __future__ import annotations

import time as _time

import devices
import model_tree
import rcp_read as _r
import rcp_transfer as _t


def parse(rec: bytes) -> tuple:
    """(path, name, value, kind) of one report-3 record, or WriteRefused if it is not exactly one
    property (the same reading the send layer applies)."""
    try:
        n = int.from_bytes(rec[1:5], "little")
        if rec[:1] != b"\x03" or not 0 < n <= len(rec) - 5 or any(rec[5 + n:]):
            raise ValueError("not one report-3 record")
        path, name, value, kind, end = model_tree.parse_record(rec[5:5 + n])
        if end != n:
            raise ValueError("trailing bytes in the record")
    except (ValueError, IndexError, TypeError):
        raise devices.WriteRefused("That record is not one setting, so it is not sent.")
    return list(path), name, value, kind


def _device():
    dev = _r.device_for_read()               # reads one dump first if the layout is not known yet
    if not dev.layout_required or dev.layout is None:
        raise devices.WriteRefused(f"The {dev.name}'s layout is not known yet, so nothing is "
                                   f"written to it.")
    return dev


def _same(got, value, kind: str) -> bool:
    if kind == "float":
        import channels                    # local: channels imports rcp_transfer, which imports us
        return isinstance(got, float) and abs(got - float(value)) <= channels.CHAIN_FLOAT_EPS
    if kind == "bool":
        return got is bool(value)
    return got == value and type(got) is type(value)


def check(recs) -> list:
    """Layer one for a batch: parse and gate every record WITHOUT reading the desk. Returns the
    parsed [(path, name, value, kind)], or raises WriteRefused naming the first refusal."""
    dev = _device()
    out = []
    for rec in recs:
        path, name, value, kind = parse(rec)
        why = devices.layout_write_ok(dev, path, name, value, kind)
        if why:
            raise devices.WriteRefused(why)
        out.append((path, name, value, kind))
    return out


def write_records(recs, pace: float = 0.0, confirm: bool = True, blob=None,
                  confirm_only=None) -> int:
    """Check, send and confirm `recs` (Pro II record forms at this desk's addresses). Returns bytes
    written. Raises devices.WriteRefused before anything is sent, or channels.WriteNotConfirmed if
    a fresh dump does not show every value afterwards. `blob` may be a FRESH dump the caller has
    just read (no write since); otherwise one is read here. `confirm_only`, a set of property
    names, narrows the read-back to those (for values the desk itself derives from others, such as
    a simple-mode processing chain); every record is still checked before it is sent."""
    import channels                        # local: see _same
    recs = list(recs)
    if not recs:
        return 0
    dev = _device()
    parsed = check(recs)
    blob = blob if blob is not None else _r.read_model(fresh=True)
    for path, name, _value, kind in parsed:
        node = _r.node_at(blob, path)
        hit = dev.layout.block_at(path)
        found = next((k for p, _v, k in (node.props if node is not None else ()) if p == name),
                     None)
        # A request slot reads bool until it is first touched and a composite after (routing).
        ok_kinds = ("bool", "pair") if kind == "pair" else (kind,)
        if node is None or hit is None or node.name != hit[0] or found not in ok_kinds:
            raise devices.WriteRefused(
                f"The {dev.name} does not show {name} there now, so it is not written.")
    if len(recs) == 1:
        n = _t.send_record(recs[0])
    else:
        n = _t.send_records_paced(recs, delay=pace)
    if not confirm:
        return n
    want = {}
    for path, name, value, kind in parsed:
        if kind != "pair" and (confirm_only is None or name in confirm_only):
            # (a request is a trigger the desk clears: nothing to read back)
            want[(tuple(path), name)] = (value, kind)
    got = {}
    bad = []
    for attempt in range(channels.CONFIRM_TRIES):
        now = channels._confirm_reader()
        bad = []
        for (path, name), (value, kind) in want.items():
            got[(path, name)] = _r.path_value(now, list(path), name, None)
            if not _same(got[(path, name)], value, kind):
                bad.append((path, name, value))
        if not bad:
            return n
        if attempt + 1 < channels.CONFIRM_TRIES:
            _time.sleep(channels.CONFIRM_GAP_S)
    said = "; ".join(f"{name} reads {got.get((path, name))!r}, not {value!r}"
                     for path, name, value in bad[:4])
    raise channels.WriteNotConfirmed(f"The {dev.name} did not confirm the change: {said}.")
