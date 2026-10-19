"""OUTPUT MIXES ON A LAYOUT DESK (PID 0x0092): checked before, confirmed after.

The fourth write family on a desk whose geometry comes from its own tree (cap "routing_write",
devices.layout_routing_write_ok). It sends exactly the records the Pro II routing code sends
(rcp_transfer.set_output_routing / set_cross_point_excluded / set_cross_point_level and the link
requests), at THIS desk's addresses, with the same three layers as the input, effects and fader
writes:
  * BEFORE: the address is a node of a MAIN run (a cross-point of the first MIX run, an output mode
    of the first MIXMINUSES run) that already carries the property with that type in a fresh dump,
    so a write can only change an existing value and never append one;
  * the send layer re-checks each finished record (devices.refuse_unproven_pid);
  * AFTER: a fresh dump must show the result, or WriteNotConfirmed says what the desk reports. The
    desk does NOT push back a change the host made, so reading is the only way to know.

ADDRESSES. A cross-point is MIX[13 * source + output] of the main run (cell_path); an output's mode
is MIXMINUSES[output] of the main run (mode_path), outputs 0..9 on PID 0x0092. The three later modes
(0xFC run) and the later MIX runs are read elsewhere and never written here.

THE OUTPUT NAMES are the Pro II order (devices._LAYOUT_OUTPUTS): output 0 = Headphone 1 was measured
by ear on 7th October 2026 (Custom, Mic 1 and Mic 2 out, the headphones on jack 1 changed); the rest
follow the Pro II order, which the unit's default modes match exactly, accepted by its owner and not
individually measured. The confirm proves the desk STORED a change; whether the audio moved is a
question for an ear or a meter (DevTools/pid0092_identify_outputs_live.py).

MIX PRESETS AND "RESET ALL OUTPUT MIXES" (apply_output): the same record forms, one output at a
time, each output checked as a batch and confirmed in one pass - the Pro II apply order. An output
in the later mode run is never written: it passes only when it already reads the wanted mode.

THE MASTER OUTPUT SETTINGS the Routing page also offers live here, each under its own narrow cap
and NOT "output_write" (which would open every Pro II global at its own selectors):
  * set_headphone_type (cap "headphone_write"): headphoneType 0/1/2 on one block of the main
    HEADPHONE run, through channels._layout_write (allow-list, exists-before, confirmed-after);
  * send_usb_mode + confirm_usb_mode (cap "usbmode_write"): outputMultiMode 1 or 4 on OUTPUT. The
    desk RE-ENUMERATES on USB when this changes and comes back under the other product id
    (devices.LAYOUT_USBMODE_PIDS), so the confirm cannot be an immediate read: it waits for the desk
    to come back under the product id that mode belongs to, then reads a fresh dump. If it is not
    back in time the answer is "sent, not confirmed yet", never a success and never a failure.

Nothing here is used on the Pro II or the Duo: their routing goes through rcp_transfer as before.
"""
from __future__ import annotations

import contextlib
import time as _time

import channels
import devices
import rcp_read as _r
import rcp_transfer as _t
from channels import WriteNotConfirmed

MODE_NAMES = {0: "Main Mix", 1: "Mix Minus", 2: "Custom"}
REQUEST_PROPS = ("mixLinkRequest", "mixUnlinkRequest")


def _confirm_reader():
    """A fresh dump for the after-write check. The SAME hook the other layout writes use
    (channels._confirm_reader), so an offline test replaces one function for all of them."""
    return channels._confirm_reader()


def _device():
    # device_for_read: in a fresh process (a script, a test console) no dump has been read yet, so
    # the desk's layout is unknown and every write would refuse; that reads one first (read only).
    dev = _r.device_for_read()
    if not dev.layout_required or dev.layout is None or "routing_write" not in dev.caps \
            or dev.matrix.base is None:
        raise devices.WriteRefused(f"Changing output mixes is not enabled on the {dev.name}.")
    return dev


def output_label(dev, output: int) -> str:
    o = next((x for x in dev.matrix.outputs if x["index"] == int(output)), None)
    return o["label"] if o else f"Output {int(output) + 1}"


def source_label(dev, source: int) -> str:
    s = next((x for x in dev.matrix.sources if x["index"] == int(source)), None)
    return s["label"] if s else f"source {int(source)}"


def cell_path(dev, output: int, source: int) -> list:
    """[root index] of the (output, source) cross-point in the MAIN MIX run. Refuses anything outside
    it: a source past the main INPUTSOURCE run, an output past the row."""
    lay, m = dev.layout, dev.matrix
    output, source = int(output), int(source)
    if not 0 <= output < m.row_stride:
        raise devices.WriteRefused(f"There is no output {output + 1} on the {dev.name}.")
    if not 0 <= source < lay.count("INPUTSOURCE") or source not in m.source_indices:
        raise devices.WriteRefused(f"Source code {source} is not a routing row on the {dev.name}.")
    sel = m.base + m.row_stride * source + output
    if lay.run("MIX") is None or lay.run("MIX").index_of(sel) is None:
        raise devices.WriteRefused(f"That cross-point lies outside the {dev.name}'s main mix grid.")
    return [sel]


def mode_path(dev, output: int) -> list:
    """[root index] of an output's mode in the MAIN MIXMINUSES run. The later run (outputs 10..12 on
    PID 0x0092) is refused: it is read, and nothing has shown that writing it is the same thing."""
    run = dev.layout.run("MIXMINUSES")
    output = int(output)
    if run is None or not 0 <= output < run.count:
        raise devices.WriteRefused(
            f"The mix mode of {output_label(dev, output)} sits outside the {dev.name}'s first "
            f"block of output modes, so it is not changed from here.")
    return [run.base + output]


def _found(blob, path, prop: str, block: str, kinds: tuple):
    """The current value of `prop` on the node at `path`, refusing unless the node is a `block` that
    carries it as one of `kinds`. The exists-before layer."""
    node = _r.node_at(blob, path)
    hit = next(((v, k) for p, v, k in (node.props if node is not None else ()) if p == prop), None)
    if node is None or node.name != block or hit is None or hit[1] not in kinds:
        raise devices.WriteRefused(
            f"The {devices.get_active_device().name} does not show {prop} at root index "
            f"0x{int(path[0]):02X}, so it is not written.")
    return hit[0]


def _record(prop: str, value, path) -> bytes:
    """The Pro II record form for one routing write, at a one-byte, one-level address."""
    sel = int(path[0])
    if not 0 <= sel <= 0xFF:
        raise devices.WriteRefused(f"Root index 0x{sel:X} needs a two-byte address; not written.")
    if prop in ("mixDisabled", "mixMute"):
        return _t.build_global_prop_bool(prop, bool(value), sel)
    if prop == "mixLevelWithAnchor":
        return _t.build_cross_point_level(sel, float(value))
    if prop in REQUEST_PROPS:
        return _t.build_mix_link_request(sel, link=(prop == "mixLinkRequest"))
    if prop == devices.LAYOUT_ROUTING_MODE_PROP:
        return _t.build_global_prop_int(prop, sel, int(value))
    raise devices.WriteRefused(f"{prop} is not a routing setting.")


def _static_check(dev, path, prop: str, value) -> None:
    """Layer one, before the desk is even read: the record this would send must pass the same
    allow-list the send layer applies."""
    if prop == "mixLevelWithAnchor":
        v, kind = f"{devices.layout_level_text(value)}|0", "str"
    elif prop in REQUEST_PROPS:
        v, kind = devices.LAYOUT_ROUTING_REQUEST_VALUE, "pair"
    elif prop == devices.LAYOUT_ROUTING_MODE_PROP:
        v, kind = value, "int"
    else:
        v, kind = value, "bool"
    why = devices.layout_routing_write_ok(dev, path, prop, v, kind)
    if why:
        raise devices.WriteRefused(why)


def _send(dev, path, prop: str, value) -> int:
    _static_check(dev, path, prop, value)
    return _t.send_record(_record(prop, value, path))


def _level_of(cell):
    """(level, anchor) floats of a mixLevelWithAnchor value, or (None, None)."""
    if isinstance(cell, str) and "|" in cell:
        a, b = cell.split("|", 1)
        try:
            return float(a), float(b)
        except ValueError:
            pass
    return None, None


def _linked(cell) -> bool:
    lv, an = _level_of(cell)
    return lv is not None and abs(lv - an) <= _r.LINK_EPS


def _confirm(dev, checks) -> None:
    """Layer three. `checks` = [(path, prop, ok(value) -> bool, what)]: every one must hold in ONE
    fresh dump, else WriteNotConfirmed names each that does not and what the desk shows instead.
    Records are sent once and never retried; only the reading is repeated."""
    bad = []
    for attempt in range(channels.CONFIRM_TRIES):
        blob = _confirm_reader()
        bad = []
        for path, prop, ok, what in checks:
            got = _r.path_value(blob, path, prop, None)
            if not ok(got):
                bad.append(f"{what} (it reports {'nothing' if got is None else repr(got)})")
        if not bad:
            return
        if attempt + 1 < channels.CONFIRM_TRIES:
            _time.sleep(channels.CONFIRM_GAP_S)
    raise WriteNotConfirmed(f"The {dev.name} did not confirm the change: " + "; ".join(bad) + ".")


# ---- the reads a writer needs --------------------------------------------------------------------

def read_cell(blob, output: int, source: int, dev=None) -> dict:
    """Every property on one cross-point of the main run, {name: value}, for a before/after diff."""
    dev = dev or _device()
    node = _r.node_at(blob, cell_path(dev, output, source))
    return {p: v for p, v, _k in (node.props if node is not None else ())}


def read_mode(blob, output: int, dev=None):
    """An output's mode as the desk reports it, main run or later run alike (the read side)."""
    dev = dev or _device()
    modes = _r.output_modes(blob, dev.matrix)
    return modes[int(output)] if 0 <= int(output) < len(modes) else None


# ---- the writes -----------------------------------------------------------------------------------

def set_output_mode(output: int, mode: int) -> int:
    """outputMixMinus on one output of the main MIXMINUSES run, checked and confirmed."""
    dev = _device()
    mode = int(mode)
    path = mode_path(dev, output)
    _static_check(dev, path, devices.LAYOUT_ROUTING_MODE_PROP, mode)
    _found(_r.read_model(fresh=True), path, devices.LAYOUT_ROUTING_MODE_PROP, "MIXMINUSES", ("int",))
    n = _send(dev, path, devices.LAYOUT_ROUTING_MODE_PROP, mode)
    _confirm(dev, [(path, devices.LAYOUT_ROUTING_MODE_PROP, lambda v: v == mode,
                    f"{output_label(dev, output)} is not on {MODE_NAMES.get(mode, mode)}")])
    return n


def set_cell(output: int, source: int, prop: str, value: bool) -> int:
    """ONE bool on ONE cross-point (mixDisabled or mixMute), checked and confirmed. No request goes
    with it: this is the single-record write the identification helper uses."""
    if prop not in ("mixDisabled", "mixMute"):
        raise devices.WriteRefused(f"{prop} is not written on its own.")
    dev = _device()
    path = cell_path(dev, output, source)
    _static_check(dev, path, prop, bool(value))
    _found(_r.read_model(fresh=True), path, prop, "MIX", ("bool",))
    n = _send(dev, path, prop, bool(value))
    _confirm(dev, [(path, prop, lambda v: v is bool(value),
                    f"{prop} on {source_label(dev, source)} in {output_label(dev, output)} is not "
                    f"{bool(value)}")])
    return n


def set_cross_point_excluded(output: int, source: int, excluded: bool) -> int:
    """Exclude or include one source in one output's mix, as the Pro II does it: the matching
    unlink / link request, then mixDisabled. Confirmed on mixDisabled (the request is a trigger the
    desk clears, so it has nothing to read back)."""
    dev = _device()
    excluded = bool(excluded)
    path = cell_path(dev, output, source)
    req = "mixUnlinkRequest" if excluded else "mixLinkRequest"
    _static_check(dev, path, req, None)
    _static_check(dev, path, "mixDisabled", excluded)
    blob = _r.read_model(fresh=True)
    # A request slot reads bool until it is first touched and a cleared composite (pair) after.
    _found(blob, path, req, "MIX", ("bool", "pair"))
    _found(blob, path, "mixDisabled", "MIX", ("bool",))
    n = _send(dev, path, req, None)
    n += _send(dev, path, "mixDisabled", excluded)
    _confirm(dev, [(path, "mixDisabled", lambda v: v is excluded,
                    f"{source_label(dev, source)} is not "
                    f"{'excluded from' if excluded else 'included in'} {output_label(dev, output)}")])
    return n


def set_cross_point_level(output: int, source: int, level: float) -> int:
    """One source's level in one output (0..1), the Pro II monitor-mix way: the output to Custom
    first if it is not (confirmed on its own), then the unlink request and the level. Confirmed on
    the level half of mixLevelWithAnchor; the desk keeps its own anchor."""
    dev = _device()
    text = devices.layout_level_text(level)
    path = cell_path(dev, output, source)
    _static_check(dev, path, "mixUnlinkRequest", None)
    _static_check(dev, path, "mixLevelWithAnchor", float(text))
    blob = _r.read_model(fresh=True)
    _found(blob, path, "mixUnlinkRequest", "MIX", ("bool", "pair"))
    _found(blob, path, "mixLevelWithAnchor", "MIX", ("str",))
    n = 0
    if read_mode(blob, output, dev) != 2:
        mode_path(dev, output)                    # refuses a later-run output before anything is sent
        n += set_output_mode(output, 2)
    n += _send(dev, path, "mixUnlinkRequest", None)
    n += _send(dev, path, "mixLevelWithAnchor", float(text))
    want = float(text)
    _confirm(dev, [(path, "mixLevelWithAnchor",
                    lambda v: _level_of(v)[0] is not None and abs(_level_of(v)[0] - want) <= 1e-6,
                    f"{source_label(dev, source)} in {output_label(dev, output)} is not at "
                    f"{round(want * 100)} percent")])
    return n


def relink_output(output: int) -> int:
    """Put one output back to following the faders, as the Pro II does: a link request on every
    source's cross-point, then Main Mix. Confirmed: the mode reads Main Mix and every source that was
    not excluded reads linked again (level equal to its anchor)."""
    dev = _device()
    mpath = mode_path(dev, output)
    paths = {s: cell_path(dev, output, s) for s in dev.matrix.source_indices}
    for s, p in paths.items():
        _static_check(dev, p, "mixLinkRequest", None)
    _static_check(dev, mpath, devices.LAYOUT_ROUTING_MODE_PROP, 0)
    blob = _r.read_model(fresh=True)
    _found(blob, mpath, devices.LAYOUT_ROUTING_MODE_PROP, "MIXMINUSES", ("int",))
    excluded = {}
    for s, p in paths.items():
        _found(blob, p, "mixLinkRequest", "MIX", ("bool", "pair"))
        excluded[s] = _found(blob, p, "mixDisabled", "MIX", ("bool",))
    n = 0
    for s, p in paths.items():
        n += _send(dev, p, "mixLinkRequest", None)
        _time.sleep(0.05)
    n += _send(dev, mpath, devices.LAYOUT_ROUTING_MODE_PROP, 0)
    checks = [(mpath, devices.LAYOUT_ROUTING_MODE_PROP, lambda v: v == 0,
               f"{output_label(dev, output)} is not on Main Mix")]
    checks += [(p, "mixLevelWithAnchor", _linked,
                f"{source_label(dev, s)} in {output_label(dev, output)} is not following its fader")
               for s, p in paths.items() if excluded[s] is not True]
    _confirm(dev, checks)
    return n


def apply_output(output: int, mode, sources: dict, pace: float = 0.03) -> int:
    """One output of a MIX PRESET (or "reset all output mixes"), the routing_schemes.apply order -
    mode first, then each source included (link request + mixDisabled False) or excluded (unlink
    request + mixDisabled True), then any manual level (unlink request + level) - as ONE checked
    batch: every record passes the routing allow-list and every property is found on its node in
    ONE fresh dump before the first record goes out; the records go out on one handle; ONE pass of
    fresh dumps then confirms the mode, every source's include/exclude, every level, and that every
    included source without a level follows its fader again.

    `mode` None leaves the mode alone. A mode the output ALREADY reads is not written, so an output
    in the later MIXMINUSES run (outputs 10..12 on PID 0x0092, never written) passes when it
    already has the wanted mode and is refused, before anything is sent, when it does not.
    `sources` = {source index: (excluded bool, level 0..1 or None)}. Returns bytes written."""
    dev = _device()
    output = int(output)
    blob = _r.read_model(fresh=True)
    recs, found, checks = [], [], []
    if mode is not None and read_mode(blob, output, dev) != int(mode):
        mpath = mode_path(dev, output)             # refuses a later-run output
        _static_check(dev, mpath, devices.LAYOUT_ROUTING_MODE_PROP, int(mode))
        found.append((mpath, devices.LAYOUT_ROUTING_MODE_PROP, "MIXMINUSES", ("int",)))
        recs.append(_record(devices.LAYOUT_ROUTING_MODE_PROP, int(mode), mpath))
    if mode is not None:
        checks.append((None, None, int(mode),
                       f"{output_label(dev, output)} is not on {MODE_NAMES.get(int(mode), mode)}"))
    levels = []
    for si, (excluded, level) in sorted(sources.items()):
        path = cell_path(dev, output, si)
        excluded = bool(excluded)
        req = "mixUnlinkRequest" if excluded else "mixLinkRequest"
        _static_check(dev, path, req, None)
        _static_check(dev, path, "mixDisabled", excluded)
        found.append((path, req, "MIX", ("bool", "pair")))
        found.append((path, "mixDisabled", "MIX", ("bool",)))
        recs.append(_record(req, None, path))
        recs.append(_record("mixDisabled", excluded, path))
        what = (f"{source_label(dev, si)} is not "
                f"{'excluded from' if excluded else 'included in'} {output_label(dev, output)}")
        checks.append((path, "mixDisabled", (lambda v, ex=excluded: v is ex), what))
        if not excluded and level is not None:
            levels.append((si, path, float(devices.layout_level_text(level))))
        elif not excluded:
            checks.append((path, "mixLevelWithAnchor", _linked,
                           f"{source_label(dev, si)} in {output_label(dev, output)} is not "
                           f"following its fader"))
    for si, path, want in levels:
        _static_check(dev, path, "mixUnlinkRequest", None)
        _static_check(dev, path, "mixLevelWithAnchor", want)
        found.append((path, "mixLevelWithAnchor", "MIX", ("str",)))
        recs.append(_record("mixUnlinkRequest", None, path))
        recs.append(_record("mixLevelWithAnchor", want, path))
        checks.append((path, "mixLevelWithAnchor",
                       (lambda v, w=want: _level_of(v)[0] is not None
                        and abs(_level_of(v)[0] - w) <= 1e-6),
                       f"{source_label(dev, si)} in {output_label(dev, output)} is not at "
                       f"{round(want * 100)} percent"))
    for path, prop, block, kinds in found:
        _found(blob, path, prop, block, kinds)
    n = _t.send_records_paced(recs, delay=pace) if recs else 0
    _confirm_with_mode(dev, output, checks)
    return n


def _confirm_with_mode(dev, output: int, checks) -> None:
    """_confirm, where a check whose prop is None is the output's MODE (its `ok` is the wanted
    mode), read as the page reads it (rcp_read.output_modes), so a later-run output is confirmed
    too."""
    bad = []
    for attempt in range(channels.CONFIRM_TRIES):
        blob = _confirm_reader()
        bad = []
        for path, prop, ok, what in checks:
            if prop is None:
                modes = _r.output_modes(blob, dev.matrix)
                got = modes[output] if 0 <= output < len(modes) else None
                good = got == ok
            else:
                got = _r.path_value(blob, path, prop, None)
                good = ok(got)
            if not good:
                bad.append(f"{what} (it reports {'nothing' if got is None else repr(got)})")
        if not bad:
            return
        if attempt + 1 < channels.CONFIRM_TRIES:
            _time.sleep(channels.CONFIRM_GAP_S)
    raise WriteNotConfirmed(f"The {dev.name} did not confirm the change: " + "; ".join(bad[:4])
                            + (f"; and {len(bad) - 4} more" if len(bad) > 4 else "") + ".")


# ---- the master output settings: headphone type and USB mode ----------------------------------------

# How long confirm_usb_mode waits for the desk to come back on USB after a mode change, and how
# often it looks. The switch drops the whole desk off the bus for a few seconds (measured on PID
# 0x0092: it returns under the other product id); past this the answer is "not confirmed yet".
USBMODE_RECONNECT_S = 20.0
USBMODE_POLL_S = 0.5


def headphone_path(dev, output: int) -> list:
    """[root index] of headphone output `output` (0-based) in the MAIN HEADPHONE run. Refuses unless
    that run is exactly the four outputs (devices.layout_headphone_outputs)."""
    outs = devices.layout_headphone_outputs(dev.layout)
    output = int(output)
    if not 0 <= output < len(outs):
        raise devices.WriteRefused(f"There is no headphone output {output + 1} the {dev.name} may "
                                   f"change.")
    return [outs[output][0]]


def set_headphone_type(output: int, value: int) -> int:
    """headphoneType on one headphone output of a layout desk, checked and confirmed: the Pro II
    record form at this desk's HEADPHONE block, through channels._layout_write (allow-list, the
    property already there as an int in a fresh dump, a fresh dump showing the value after)."""
    dev = devices.get_active_device()
    if not dev.layout_required or dev.layout is None or "headphone_write" not in dev.caps:
        raise devices.WriteRefused(f"Changing the headphone type is not enabled on the {dev.name}.")
    path = headphone_path(dev, output)
    why = devices.layout_headphone_write_ok(dev, path, devices.LAYOUT_HEADPHONE_PROP, value, "int")
    if why:                                    # refused before the desk is even read
        raise devices.WriteRefused(why)
    return channels._layout_write(dev, path, devices.LAYOUT_HEADPHONE_PROP, int(value), "int",
                                  "HEADPHONE", devices.layout_headphone_write_ok,
                                  blob=_r.read_model(fresh=True))


def usb_mode_path(dev) -> list:
    """[root index] of the OUTPUT block, read off the desk's own layout (0x0B on PID 0x0092)."""
    sel = dev.layout.single("OUTPUT") if dev.layout is not None else None
    if sel is None:
        raise devices.WriteRefused(f"The {dev.name}'s OUTPUT block is not known.")
    return [sel]


def send_usb_mode(mode: int) -> dict:
    """Check and send outputMultiMode on a layout desk. NO CONFIRM HERE: the desk drops off USB as
    it takes the change (confirm_usb_mode does the waiting, without the HID lock held).

    Returns {"sent": bytes, "before": the mode the desk reported}. "sent" is 0 when the desk already
    reports `mode`: nothing is written then, because a write would only cost a USB dropout."""
    dev = devices.get_active_device()
    if not dev.layout_required or dev.layout is None or "usbmode_write" not in dev.caps:
        raise devices.WriteRefused(f"Changing what this computer receives over USB is not enabled "
                                   f"on the {dev.name}.")
    mode = int(mode)
    path = usb_mode_path(dev)
    why = devices.layout_usbmode_write_ok(dev, path, devices.LAYOUT_USBMODE_PROP, mode, "int")
    if why:
        raise devices.WriteRefused(why)
    before = _found(_r.read_model(fresh=True), path, devices.LAYOUT_USBMODE_PROP, "OUTPUT", ("int",))
    if before == mode:
        return {"sent": 0, "before": before}
    return {"sent": _t.send_record(_t.build_global_prop_int(devices.LAYOUT_USBMODE_PROP,
                                                             path[0], mode)),
            "before": before}


def confirm_usb_mode(mode: int, timeout: float | None = None, lock=None) -> dict:
    """After send_usb_mode: wait for the desk to come back under the product id `mode` belongs to
    (devices.LAYOUT_USBMODE_PIDS), then read a fresh dump (which records its layout for that id) and
    look at outputMultiMode in its OUTPUT block. `lock` is held for each read only, never across
    the wait.

    Returns {"state": "confirmed", "pid": ...} when it reads `mode`, or {"state": "reconnecting",
    "pid": what is on the bus now or None} when the desk is not back under that id (or not readable)
    within `timeout` - sent, but neither done nor failed as far as anyone can tell. Raises
    WriteNotConfirmed when the desk answers with another value: back under the expected id but
    reading something else, or still under the old id at the deadline and reading the old mode."""
    mode = int(mode)
    want_pid = devices.LAYOUT_USBMODE_PIDS[mode]
    timeout = USBMODE_RECONNECT_S if timeout is None else float(timeout)
    lock = lock if lock is not None else contextlib.nullcontext()
    deadline = _time.monotonic() + timeout
    pid, got = None, None
    while True:
        try:
            iface = _t.find_control_interface()
        except Exception:
            iface = None
        pid = None if iface is None else iface.product_id
        last = _time.monotonic() >= deadline
        if pid == want_pid or (last and pid in devices.LAYOUT_PIDS):
            try:
                with lock:
                    got = _r.block_value(_r.read_model(fresh=True), "OUTPUT",
                                         devices.LAYOUT_USBMODE_PROP)
            except Exception:
                got = None                 # back on the bus but not answering yet: keep waiting
            if got == mode and pid == want_pid:
                return {"state": "confirmed", "pid": pid}
            if got is not None and (pid == want_pid or last):
                name = devices.LAYOUT_USBMODE_LABEL.get(got, f"mode {got}")
                raise WriteNotConfirmed(
                    f"The unit did not confirm the change: it is connected as PID 0x{pid:04X} and "
                    f"reports {name} ({got}).")
        if last:
            return {"state": "reconnecting", "pid": pid}
        _time.sleep(USBMODE_POLL_S)
