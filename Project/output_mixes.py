"""
AccessCaster - the output mixes on the Routing page: each output's mode and Custom cross-points, how
loud each source is in a headphone or monitor output, and mix schemes, the whole matrix saved under
a name. The matrix's layout per desk is devices.py (MatrixSpec), its writes rcp_transfer.py, and a
scheme's format and apply routing_schemes.py; this module owns the gate, the HID lock and the
wording.
"""

from __future__ import annotations

import time

import announcer
import channels
import desk_access
import devices
import rcp_read
import rcp_transfer
import routing_schemes
import wording
from desk_labels import OUTPUT_MODE_LABEL
from paths import safe_name


# ----------------------------------------------------------------------------
# Output mix matrix (the Routing > Custom cross-points), RODECASTER_PROTOCOL.txt section 5. Each
# output has a mode (OUTPUT_MODE_LABEL); under Custom, each source can be excluded from that
# output. The per-device source/output LABEL maps live in devices.py (MatrixSpec.sources /
# .outputs), so the screen adapts to the connected desk; the confirmed-index write guard lives in
# rcp_transfer.
# ----------------------------------------------------------------------------

# --- Device-aware matrix helpers: the routing screen shows the CONNECTED desk's real
# sources/outputs (source code 2 = "Headset" on the Duo, "Mic 3" on the Pro II). Writes are gated
# on the "matrix_write" cap.
def active_matrix():
    return devices.get_active_device().matrix


def _matrix_output(idx: int):
    return next((o for o in active_matrix().outputs if o["index"] == idx), None)


def _matrix_source(idx: int):
    return next((s for s in active_matrix().sources_for(True) if s["index"] == idx), None)


def write_gate():
    """Cross-point writes. Both the Pro II and the Duo have this cap today; a device whose
    cross-point selector is not verified keeps its matrix read-only rather than writing blind."""
    return desk_access.cap_gate("matrix_write", lambda name:
                     f"Changing output mixes is not enabled on the {name} yet, so the routing "
                     f"matrix is read-only here.")


def matrix_model() -> dict:
    """Static layout of the output mix matrix for the UI, for the CONNECTED device."""
    m = active_matrix()
    outputs = m.outputs or devices.PRO2.matrix.outputs
    sources = (m.sources_for(channels.usb_expanded())
               or devices.PRO2.matrix.sources_for(channels.usb_expanded()))
    read_only = write_gate() is not None
    # The read-only variant of this note reaches a desk without the cap, instead of the page
    # promising writes it cannot do.
    # ONE LINE. Do not explain Main Mix, Mix Minus and Custom here, and do not claim the source list
    # shows the unit's real current mix: that list is hidden on anything but Custom, because outside
    # Custom it shows stored cross-points the engine ignores. What each mix means is said per output,
    # about the one selected; the long version lives in Help.
    note = "Each output carries its own mix. Pick an output to see and change it."
    if read_only:
        note = ("This shows each output's live mix on this device. Changing the mix is not "
                "enabled here yet, so these controls are read-only.")
    return {
        "outputs": [{"key": o["key"], "index": o["index"], "label": o["label"],
                     "modes": [[0, "Main Mix"]] + ([[1, "Mix Minus"]] if o["mixminus"] else [])
                              + [[2, "Custom"]]}
                    for o in outputs],
        "sources": [{"key": s["key"], "index": s["index"], "label": s["label"]} for s in sources],
        "read_only": read_only,
        "note": note,
    }


def action_set_output_mode(output_index: int, mode: int) -> dict:
    gate = write_gate()
    if gate is not None:
        return gate
    out = _matrix_output(output_index)
    if out is None:
        return {"ok": False, "message": f"Output {output_index} is not valid."}
    if mode not in OUTPUT_MODE_LABEL:
        return {"ok": False, "message": f"Mode {mode} is not valid."}
    with desk_access.hid_lock:
        rcp_transfer.set_output_routing(output_index, mode)
    return {"ok": True, "message": f"{out['label']} output set to {OUTPUT_MODE_LABEL[mode]}."}


def action_set_cross_point(output_index: int, source_index: int, excluded: bool) -> dict:
    """Exclude/include one source in one output's Custom mix. The output should be in
    Custom mode first (use the mode control)."""
    gate = write_gate()
    if gate is not None:
        return gate
    try:
        with desk_access.hid_lock:
            rcp_transfer.set_cross_point_excluded(output_index, source_index, excluded)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    out = _matrix_output(output_index)
    src = _matrix_source(source_index)
    out_lab = out["label"] if out else "output"
    src_lab = src["label"] if src else "source"
    verb = "excluded from" if excluded else "included in"
    return {"ok": True, "message": f"{src_lab} {verb} the {out_lab} mix."}


# ----------------------------------------------------------------------------
# MIX SCHEMES: the output-mix matrix saved under a name. The format, the capture, the built-ins and
# the writes all live in routing_schemes.py; these wrappers own the HID lock and the wording.
#
# The two Chat presets are BUILT-IN SCHEMES rather than buttons of their own, so there is one apply
# path, and "Chat back to normal" writes Chat's real normal state (mix minus) rather than a Main Mix
# that only looks like it.
# ----------------------------------------------------------------------------

def _apply_scheme(scheme: dict, label: str) -> dict:
    """Gate, lock, write, and say what happened in one sentence. Shared by every entry point: the
    dropdown, the built-ins and the "Reset all output mixes" button."""
    gate = write_gate()
    if gate is not None:
        return gate
    try:
        with desk_access.hid_lock:
            # Our own writes come back on the push stream as mixLevelWithAnchor on the SOURCE, which
            # the announcer speaks as a fader move - a full-desk scheme would otherwise narrate a
            # hundred of them over the top of the page's own reply. Marked per write because the
            # suppression window is 1.5s and this can run for ten.
            res = routing_schemes.apply(
                scheme, on_source_write=lambda si: announcer.mark_self_write(("fader", int(si))))
    except Exception as exc:
        return {"ok": False, "message": f"Could not apply the mix preset: {exc}"}
    outs = res["outputs"]
    if not outs:
        return {"ok": False,
                "message": (f"{label} names no output this device has, so nothing was changed."
                            + (f" Not on this device: {wording.join_and(res['skipped'])}."
                               if res["skipped"] else ""))}
    msg = f"Applied {label} to {wording.count(len(outs), 'output')}"
    msg += ": " + wording.join_and(outs) + "."
    if res["skipped"]:
        msg += (f" Skipped {wording.count(len(res['skipped']), 'part')} this device does not have: "
                f"{wording.join_and(res['skipped'])}.")
    if res["failed"]:
        msg += (f" {wording.count(res['failed'], 'write')} failed, so check the mix before you go "
                f"on air.")
    return {"ok": res["ok"], "message": msg, "outputs": outs, "skipped": res["skipped"],
            "failed": res["failed"]}


def read_routing_schemes() -> dict:
    """What the Mix schemes control offers on this desk: the built-ins, then the saved schemes.

    Each entry carries its own DESCRIPTION so the page can say what a scheme would do before it is
    applied, without a second round trip. A saved scheme's description is worked out from the file
    (routing_schemes.describe) rather than stored in it, so it can never describe a desk it no
    longer matches."""
    read_only = write_gate() is not None
    # Read once for the whole list: the source count in every description is against the rows the
    # grid is showing right now, and the mode cannot change mid-loop in any way that matters.
    expanded = channels.usb_expanded()
    saved = []
    for name in routing_schemes.list_names():
        loaded = routing_schemes.load(name)
        if not loaded.get("ok"):
            continue
        s = loaded["scheme"]
        saved.append({"name": name,
                      "description": routing_schemes.describe(s, expanded=expanded),
                      "scope": routing_schemes.scope_text(s),
                      "device_name": s.get("device_name"),
                      "saved_at": s.get("saved_at")})
    note = ("The output mixes saved under a name: which sources each output carries, and at what "
            "level. Pick one and choose Apply. Not a profile: it never touches your channels, "
            "processing or pads.")
    if read_only:
        note = ("Changing output mixes is not enabled on this device yet, so mix presets cannot be "
                "saved or applied here.")
    return {"ok": True, "read_only": read_only, "note": note,
            "builtins": [{"id": b["id"], "name": b["name"], "description": b["description"],
                          "scope": routing_schemes.scope_text(b["scheme"]),
                          "destructive": b["destructive"]}
                         for b in routing_schemes.builtins()],
            "saved": saved}


def action_apply_routing_scheme(name: str, builtin: bool = False) -> dict:
    """Apply a built-in (by id) or a saved scheme (by name). Built-ins are addressed by ID and never
    by name, so a saved scheme called "Everything normal" cannot shadow the real one."""
    if builtin:
        b = routing_schemes.builtin(name)
        if b is None:
            return {"ok": False, "message": f"There is no built-in mix preset {name}."}
        return _apply_scheme(b["scheme"], b["name"])
    loaded = routing_schemes.load(name)
    if not loaded.get("ok"):
        return loaded
    return _apply_scheme(loaded["scheme"], loaded["safe"])


def action_save_routing_scheme(name: str, output: str = "") -> dict:
    """Save the desk's CURRENT output mixes under a name. One model read under the lock, no writes.

    `output` is an output KEY and makes a ONE-OUTPUT preset, the shape the built-in Chat presets
    have: applying it later writes that output and leaves every other mix alone. Empty means every
    output, the default.

    Overwriting an existing scheme is allowed and deliberate (unlike the as-found baseline, a scheme
    is cheap to re-take), but the page asks first so it is never a surprise."""
    safe = safe_name(name)
    if not safe:
        return {"ok": False, "message": "A mix preset name must be 1 to 40 letters, digits, "
                                        "spaces, dashes or underscores."}
    try:
        with desk_access.hid_lock:
            blob = rcp_read.read_model_stable()
    except Exception as exc:
        return {"ok": False, "message": f"Could not read the output mixes: {exc}"}
    cap = routing_schemes.capture(safe, blob, only=output)
    if not cap.get("ok"):
        return cap
    res = routing_schemes.save(safe, cap["scheme"])
    if not res.get("ok"):
        return res
    return {"ok": True, "name": safe,
            "message": (f"Saved the mix preset {safe}. "
                        f"{routing_schemes.describe(cap['scheme'], expanded=channels.usb_expanded())}")}


def action_delete_routing_scheme(name: str) -> dict:
    """Delete a saved scheme. Pure file work: no device access and no lock."""
    return routing_schemes.delete(name)


# --- MONITOR MIX: per-output source levels ----------------------------------------------------
# Set how loud each source is in ONE output (e.g. your headphones), independent of the recording
# and other outputs. Uses the matrix cross-point level (mixLevelWithAnchor): the output must be in
# Custom mode, then we unlink the one source and set its absolute level. Reading is reliable (the
# 13*source+output grid formula), so we prefill and can reset cleanly. Refused during a SIP call.
# The monitor-mix outputs are the headphone/monitor outputs (matrix indices 0-4); we intersect
# this with what the CONNECTED device actually exposes (below), so the Duo shows only its Headphone
# 1/2 and the Pro II shows Headphone 1-4 + Monitor. Source labels + indices come straight from the
# device matrix (so the Duo's code 2 reads "Headset", not "Mic 3").
_MONITOR_OUTPUT_INDICES = (0, 1, 2, 3, 4)


def _monitor_outputs():
    """(index, label) for each monitor-mix output the connected device exposes, device-labelled."""
    dev_out = {o["index"]: o["label"] for o in active_matrix().outputs}
    return [(i, dev_out[i]) for i in _MONITOR_OUTPUT_INDICES if i in dev_out]


def _monitor_sources():
    """(index, label) for each matrix source on the connected device (device-labelled)."""
    return [(s["index"], s["label"])
            for s in active_matrix().sources_for(channels.usb_expanded())]


def monitor_mix_model() -> dict:
    """Static layout for the Monitor mix UI (the outputs + sources the connected device exposes)."""
    read_only = write_gate() is not None
    note = ("How loud each source is in the chosen output, without changing the recording or "
            "other outputs. Moving one makes it a custom mix. Not available during a phone call.")
    if read_only:
        note = ("Shows how loud each source is in each headphone output on this device. Changing "
                "these levels is not enabled here yet, so the sliders are read-only.")
    return {
        "outputs": [{"index": i, "label": l} for i, l in _monitor_outputs()],
        "sources": [{"index": i, "label": l} for i, l in _monitor_sources()],
        "read_only": read_only,
        "note": note,
    }


def action_set_monitor_level(output_index: int, source_index: int, level01: float) -> dict:
    """Set one source's level in one output. Switches the output to Custom mode if needed, unlinks
    that source, then writes the level. Only affects this output."""
    gate = write_gate()
    if gate is not None:
        return gate
    mon_out = dict(_monitor_outputs())
    mon_src = dict(_monitor_sources())
    if output_index not in mon_out:
        return {"ok": False, "message": "That output is not adjustable here."}
    if source_index not in mon_src:
        return {"ok": False, "message": "That source is not adjustable here."}
    try:
        v = max(0.0, min(1.0, float(level01)))
        with desk_access.hid_lock:
            # Ensure Custom mode + the source unlinked. Both writes are idempotent (no-ops if
            # already so), so they are sent unconditionally: a ~1.5 s model read to check first
            # would lag every slider step.
            rcp_transfer.set_output_routing(output_index, 2)       # Custom
            rcp_transfer.set_cross_point_linked(output_index, source_index, False)
            rcp_transfer.set_cross_point_level(output_index, source_index, v)
    except Exception as exc:
        return {"ok": False, "message": f"Could not set the level: {exc}"}
    # A cross-point write comes back as mixLevelWithAnchor on that SOURCE, which is exactly what the
    # announcer speaks as a fader move - so without this, every matrix slider is announced twice.
    # Source index and source code are the same space here (both address the cross-point grid).
    announcer.mark_self_write(("fader", int(source_index)))
    return {"ok": True,
            "message": f"{mon_src[source_index]} in {mon_out[output_index]} "
                       f"set to {round(v * 100)} percent."}


def action_reset_monitor_output(output_index: int) -> dict:
    """Reset one output to follow the faders: re-link every source (level back to the fader value)
    and put the output back to Main Mix mode."""
    gate = write_gate()
    if gate is not None:
        return gate
    mon_out = dict(_monitor_outputs())
    if output_index not in mon_out:
        return {"ok": False, "message": "That output is not adjustable here."}
    try:
        with desk_access.hid_lock:
            for si, _ in _monitor_sources():
                rcp_transfer.set_cross_point_linked(output_index, si, True)
                time.sleep(0.05)
            rcp_transfer.set_output_routing(output_index, 0)       # Main Mix
    except Exception as exc:
        return {"ok": False, "message": f"Could not reset the output: {exc}"}
    return {"ok": True,
            "message": f"{mon_out[output_index]} reset to follow the faders."}


def action_reset_all_outputs() -> dict:
    """Reset EVERY output mix on the desk: re-include and re-link every source on every output,
    then put each output back to its default mode (devices.py, MatrixSpec.outputs default_mode).

    A wider sweep than the per-output "Reset to faders". It exists because a hand-set level silently
    breaks channel Mute: a mute only reaches cross-points that follow the fader, so an output with
    that source unlinked keeps carrying it and you still hear a muted mic in your own cans.

    HONESTY: the default modes are a KNOWN-GOOD set taken from a saved profile, NOT a capture of a
    factory-reset unit (see the provenance comment on PRO2_MATRIX_OUTPUTS). So this is called
    "defaults", never "factory". Every write is reversible and read-back verifiable, and the UI
    tells the user to save a profile first.

    THIS IS THE "Everything normal" BUILT-IN MIX SCHEME, applied through the one scheme path
    (routing_schemes.apply). The button stays because it is where people look for it and it carries
    its own confirmation, but there is no second copy of the sweep to drift."""
    b = routing_schemes.builtin(routing_schemes.BUILTIN_NORMAL)
    if b is None:                       # no matrix outputs on this device model at all
        return {"ok": False, "message": "This device exposes no output mixes to reset."}
    return _apply_scheme(b["scheme"], b["name"])


def apply_matrix(routing: dict) -> dict:
    """Re-apply a profile's output mix matrix. CALLER HOLDS THE HID LOCK. Returns
    {applied, failed, skipped}. Skips cleanly for one saved while the matrix was unreadable (a
    phone caller).

    `mix_scheme` is the WHOLE matrix, keyed by name, applied through routing_schemes.apply - the
    same proven path the Mix schemes page uses. Every output's cross-point levels come back, not
    just the monitor ones, and an output the connected desk does not have is skipped by name rather
    than written by index.
    """
    # A desk without the matrix_write cap must not have cross-points written to it, so the matrix
    # re-apply is skipped there.
    if write_gate() is not None:
        return {"applied": 0, "failed": 0, "skipped": True}
    scheme = routing.get("mix_scheme")
    if scheme and scheme.get("outputs"):
        # Same announcer suppression as _apply_scheme: our own cross-point writes come back on the
        # push stream as mixLevelWithAnchor on the SOURCE, which the announcer would otherwise
        # narrate as a hundred fader moves over the top of the restore's own progress.
        res = routing_schemes.apply(
            scheme, on_source_write=lambda si: announcer.mark_self_write(("fader", int(si))))
        return {"applied": res["applied"], "failed": res["failed"], "skipped": False}
    return {"applied": 0, "failed": 0, "skipped": True}
