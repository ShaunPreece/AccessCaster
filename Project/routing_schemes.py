"""AccessCaster - MIX SCHEMES: the output-mix matrix, saved under a name and recalled in one go.

A scheme is the routing matrix and nothing else. For each output it records the MODE (Main Mix /
Mix Minus / Custom) and, per source, whether that source is INCLUDED, whether it FOLLOWS THE FADER
(linked) and, when it does not, the manual LEVEL it sits at. That is the whole of what "where does
each output's sound come from" means on this desk, so a scheme round-trips it.

DELIBERATELY NOT A PROFILE. A profile is the entire desk (every channel, every chain, the unit's own
settings, optionally the pads' audio) and a restore takes half a minute or more, so it is a backup
you take between shows. A scheme is one screen's worth of state, it applies in seconds, and it is
meant to be recalled DURING a show ("interview", "solo", "music bed"). They are kept visibly apart
in the UI for the same reason: picking the wrong one should never be able to overwrite your mics.

KEYED BY NAME, NEVER BY INDEX. Every output and source is stored by its KEY ("Chat", "Mic1"), not
by its matrix index, for two reasons:
  - the Duo exposes a DIFFERENT set of outputs (no Headphone 3/4) at the same index space, so a
    Pro II scheme applied by index would write Headphone 3's mix onto some other output. By key, an
    output the connected desk does not have is simply skipped and named in the reply.
  - the matrix row IS the source code, so a key is stable across a fader
    reorder. Nothing about a scheme changes when you move a channel to another fader.

AND THE SAME SHAPE IN EITHER USB MODE. Capture and apply walk `MatrixSpec.all_sources`, which
includes RODE's four Expanded rows whether the desk is in Expanded or not, because the desk
publishes their cross-points either way. Walking the narrower DISPLAY list would make a scheme
depend on the mode it was captured in: one saved in Standard holds no Virtual A cell, so applying it
in Expanded leaves that cell as it was, and "Everything normal" cannot reset it.

LOCK BOUNDARY: the same rule as profiles.py, and for the same reason (no circular import). This
module never imports the server. `capture` is pure (it reads an already-read model blob), and
`apply` performs the device WRITES but ASSUMES THE CALLER HOLDS THE HID LOCK.
"""
from __future__ import annotations

import json
import os
import re
import time

import devices
import rcp_read
import rcp_transfer
from paths import data_path
from wording import count, plural

# Saved schemes are user data, beside the profiles in %LOCALAPPDATA%\AccessCaster, so they survive
# a reinstall and do not depend on where the app is installed.
SCHEMES_DIR = data_path("schemes")
SCHEME_VERSION = 1
# Seconds between device writes during an apply. Same pace as the profile restore and the
# "reset all output mixes" sweep, both of which drive this same cross-point path reliably.
WRITE_PACE = 0.03

# ---- naming + listing (pure file ops, no device) ----------------------------------------------

def safe_name(name: str):
    """The same rule as a profile name, so the two stores cannot disagree about what is nameable."""
    name = (name or "").strip()
    if not re.match(r"^[A-Za-z0-9 _-]{1,40}$", name):
        return None
    return name


def _path(safe: str) -> str:
    return os.path.join(SCHEMES_DIR, safe + ".json")


def list_names() -> list:
    """Every saved scheme name, sorted. A scheme is one flat JSON file: unlike a profile it has no
    companion files (no pad audio), so a folder would buy nothing."""
    try:
        return sorted(e[:-5] for e in os.listdir(SCHEMES_DIR) if e.endswith(".json"))
    except FileNotFoundError:
        return []


def load(name: str) -> dict:
    """{ok, safe, scheme} or {ok: False, message}."""
    safe = safe_name(name)
    if not safe or not os.path.isfile(_path(safe)):
        return {"ok": False, "message": f"Mix scheme {name} not found."}
    try:
        with open(_path(safe), "r", encoding="utf-8") as f:
            scheme = json.load(f)
    except (OSError, ValueError) as exc:
        return {"ok": False, "message": f"Could not read the mix scheme: {exc}"}
    return {"ok": True, "safe": safe, "scheme": scheme}


def delete(name: str) -> dict:
    safe = safe_name(name)
    if not safe or not os.path.isfile(_path(safe)):
        return {"ok": False, "message": f"Mix scheme {name} not found."}
    try:
        os.remove(_path(safe))
    except OSError as exc:
        return {"ok": False, "message": f"Could not delete the mix scheme: {exc}"}
    return {"ok": True, "message": f"Deleted the mix scheme {safe}."}


# ---- capture (pure: an already-read model blob in, a scheme dict out) --------------------------

def capture(safe: str, blob: bytes, device=None, only: str = "") -> dict:
    """Build a scheme from the model dump. Returns {ok, scheme} or {ok: False, message}.

    `only` is an output KEY, and it is what makes a one-output preset: the scheme then names that
    output alone, and `apply` therefore writes that output alone: the shape of the built-in Chat
    presets, saved from the desk instead of derived. A one-output preset is the only kind that is safe to fire mid-show, because it cannot touch a mix
    you are currently listening to.

    Both reads REFUSE when the matrix has changed shape (a live phone caller adds cross-points), so
    a scheme is never saved from a grid we cannot index reliably - a half-read matrix written back
    later would silently move somebody's mix.

    LEVELS AND LINK STATE ARE PART OF THE SCHEME. Saving only the include/exclude flags would restore a mix whose balance is wrong, which sounds like a
    working recall until you listen. A level is recorded only where the cross-point is UNLINKED: a
    linked one has no level of its own, it follows the channel fader, and writing a number there
    would quietly unlink it.

    AN EXCLUDED CROSS-POINT RECORDS NOTHING BUT "excluded". Excluding a source UNLINKS it as part of
    the same operation, and an unlink FREEZES the level at wherever that fader stood, so every
    excluded cell reads back as "unlinked at some number". That number is inert - the cross-point
    carries no audio, and re-including it relinks it to the fader anyway - but it drifts as the
    faders move, so recording it makes a scheme fail to round-trip against its own read-back for a
    difference nobody can hear.
    """
    dev = device or devices.get_active_device()
    m = dev.matrix
    wanted = [o for o in m.outputs if not only or o["key"] == only]
    if only and not wanted:
        return {"ok": False, "message": f"This device has no output called {only}."}
    st = rcp_read.read_matrix_state(blob)
    if not st.get("ok"):
        return {"ok": False, "message": st.get("reason", "The output mixes could not be read.")}
    lv = rcp_read.read_matrix_levels(blob)
    if not lv.get("ok"):
        return {"ok": False, "message": lv.get("reason", "The mix levels could not be read.")}

    outputs = {}
    for out in wanted:
        oi = out["index"]
        srcs = {}
        for src in m.all_sources:
            si = src["index"]
            excluded = bool(st["excluded"].get(oi, {}).get(si))
            if excluded:
                srcs[src["key"]] = {"excluded": True}      # see the docstring: nothing else is real
                continue
            cell = lv["levels"].get(oi, {}).get(si, {})
            linked = cell.get("linked")
            entry = {"excluded": False,
                     "linked": bool(linked) if linked is not None else None}
            if linked is False and cell.get("level") is not None:
                entry["level"] = float(cell["level"])
            srcs[src["key"]] = entry
        outputs[out["key"]] = {"mode": int(st["modes"].get(oi, 0)), "sources": srcs}

    return {"ok": True, "scheme": {
        "version": SCHEME_VERSION,
        "name": safe,
        "saved_at": time.time(),
        # Stamped for the reply wording only. A cross-device apply is ALLOWED (unlike a profile
        # restore): keying by name is exactly what makes it safe, and a Duo simply skips the
        # outputs it does not have.
        "product_id": dev.product_id,
        "device_name": dev.name,
        "outputs": outputs,
    }}


def save(safe: str, scheme: dict) -> dict:
    try:
        os.makedirs(SCHEMES_DIR, exist_ok=True)
        with open(_path(safe), "w", encoding="utf-8") as f:
            json.dump(scheme, f, indent=1)
    except OSError as exc:
        return {"ok": False, "message": f"Could not save the mix scheme: {exc}"}
    return {"ok": True, "name": safe}


# ---- the built-in schemes ----------------------------------------------------------------------
# Built from the CONNECTED device's matrix rather than stored, so they are correct on any desk and
# cannot go stale. Built-ins rather than buttons hardcoded on the page, including the one that
# answers what "put Chat back" really means (see CHAT_NORMAL below).
#
# They are addressed by ID, never by name: a saved scheme is allowed to be called "Everything
# normal", and a name collision must not be able to apply the wrong one.

BUILTIN_NORMAL = "normal"
BUILTIN_CHAT_MIC_ONLY = "chat-mic-only"
BUILTIN_CHAT_NORMAL = "chat-normal"

# Mic 1 is matrix source index 0 on both desks, and it is the mic the Chat preset keeps.
_CHAT_KEEP_SOURCE_KEY = "Mic1"


def _all_included(m) -> dict:
    """Every source of this desk included and following its fader."""
    return {s["key"]: {"excluded": False, "linked": True} for s in m.all_sources}


def builtins(device=None) -> list:
    """[{id, name, description, destructive, scheme}] for the connected desk, reading order.

    `destructive` marks the one that wipes mixes the user set by hand, so the page can confirm
    before applying it rather than deciding that itself.
    """
    dev = device or devices.get_active_device()
    m = dev.matrix
    out = []

    # 1. Everything normal. The whole desk back to its default modes with every source included and
    #    following the faders. This IS "Reset all output mixes" - the button and this entry run the
    #    same code, so the two can never drift apart.
    out.append({
        "id": BUILTIN_NORMAL,
        # NO SCOPE IN THE NAME, so not "Everything normal (every output)": every entry in the list
        # carries its scope from `scope_text`, and saying it here says it twice.
        "name": "Everything normal",
        # SHORT ON PURPOSE: a scheme's description is read out every time you arrow onto it, so
        # it says what happens and stops. The long version (which outputs go to which mode, and the
        # known-good provenance) is on the Reset all output mixes section below.
        "description": ("Every output back to its normal mix, every source back in and following "
                        "the faders. Wipes any mix you have set by hand."),
        "destructive": True,
        "scheme": {"outputs": {o["key"]: {"mode": int(o.get("default_mode", 0)),
                                          "sources": _all_included(m)}
                               for o in m.outputs}},
    })

    chat = next((o for o in m.outputs if o["key"] == "Chat"), None)
    if chat is not None and any(s["key"] == _CHAT_KEEP_SOURCE_KEY for s in m.all_sources):
        # 2. Chat = my mic only. The accessibility preset: a co-host on the Chat output hears you
        #    and nothing else, so your screen reader (which reaches the desk through USB 1 or the
        #    SMART pads) does not go down the line.
        out.append({
            "id": BUILTIN_CHAT_MIC_ONLY,
            "name": "Chat = my mic only",
            "description": ("Only Mic 1 goes to the Chat output, so a co-host does not hear your "
                            "screen reader. No other output is touched."),
            "destructive": False,
            # `all_sources`, so the Expanded four are excluded too. The whole point of this preset
            # is that a screen reader does not go down the line, and in Expanded the screen reader
            # arrives on one of those four rather than on USB 1.
            "scheme": {"outputs": {"Chat": {
                "mode": 2,
                "sources": {s["key"]: {"excluded": s["key"] != _CHAT_KEEP_SOURCE_KEY,
                                       "linked": True}
                            for s in m.all_sources},
            }}},
        })
        # 3. Chat back to normal: MIX MINUS, which is Chat's normal state, and only the Chat output
        #    is touched. Main Mix would carry the caller's own voice back to them.
        out.append({
            "id": BUILTIN_CHAT_NORMAL,
            "name": "Chat back to normal",
            "description": ("Chat back to its normal mix minus: everyone except the caller "
                            "themselves. No other output is touched."),
            "destructive": False,
            "scheme": {"outputs": {"Chat": {"mode": int(chat.get("default_mode", 1)),
                                            "sources": _all_included(m)}}},
        })
    return out


def builtin(scheme_id: str, device=None):
    """One built-in by id, or None."""
    return next((b for b in builtins(device) if b["id"] == scheme_id), None)


# ---- apply (device writes; THE CALLER HOLDS THE HID LOCK) --------------------------------------

def apply(scheme: dict, device=None, pace: float = WRITE_PACE, on_source_write=None) -> dict:
    """Write a scheme to the desk. Returns
    {ok, outputs: [label...], skipped: [key...], applied: int, failed: int}.

    Only the outputs the scheme NAMES are touched, which is what lets a one-output scheme (the Chat
    presets) be safe to fire mid-show. An output or source key the connected desk does not expose is
    skipped and returned in `skipped`, never guessed at.

    ORDER MATTERS. The mode goes first: leaving Custom mode discards that output's cross-point set
    on the desk, so writing the sources first and the mode second would throw them away. Then each
    source is included or excluded (which also relinks it), and only after that is a manual level
    written, on the cross-points the scheme recorded as unlinked.

    `on_source_write(source_index)` is called just before each write that the desk echoes back on
    that source, so the server can suppress its announcer for it. Called per write, not once at the
    start: the echo-suppression window is short and a full-desk apply takes several seconds.
    """
    dev = device or devices.get_active_device()
    m = dev.matrix
    out_by_key = {o["key"]: o for o in m.outputs}
    src_by_key = {s["key"]: s for s in m.all_sources}
    applied = failed = 0
    touched, skipped = [], []

    def attempt(fn) -> bool:
        nonlocal applied, failed
        try:
            fn()
            applied += 1
            ok = True
        except Exception:
            failed += 1
            ok = False
        time.sleep(pace)
        return ok

    for okey, ospec in (scheme.get("outputs") or {}).items():
        out = out_by_key.get(okey)
        if out is None:
            skipped.append(okey)
            continue
        oi = out["index"]
        mode = ospec.get("mode")
        if mode not in (0, 1, 2):
            mode = None
        # A scheme saved on a desk where this output offered mix minus, applied to one where it does
        # not, would otherwise write a mode the output has no control for. Fall back to its default.
        if mode == 1 and not out.get("mixminus"):
            mode = int(out.get("default_mode", 0))
        if mode is not None:
            attempt(lambda oi=oi, mode=mode: rcp_transfer.set_output_routing(oi, mode))
        for skey, cell in (ospec.get("sources") or {}).items():
            src = src_by_key.get(skey)
            if src is None:
                if skey not in skipped:
                    skipped.append(skey)
                continue
            si = src["index"]
            excluded = bool(cell.get("excluded"))
            if on_source_write:
                on_source_write(si)
            attempt(lambda oi=oi, si=si, ex=excluded:
                    rcp_transfer.set_cross_point_excluded(oi, si, ex))
            # A manual level only exists, and is only writable, on an included cross-point of a
            # Custom output. Anywhere else the cross-point follows the fader by definition (and an
            # EXCLUDED one reads back unlinked at a frozen number that is inert - see capture), so
            # the scheme has nothing to say about its level.
            level = cell.get("level")
            if excluded or mode != 2 or cell.get("linked") is not False or level is None:
                continue
            if on_source_write:
                on_source_write(si)

            def _level(oi=oi, si=si, level=float(level)):
                rcp_transfer.set_cross_point_linked(oi, si, False)
                rcp_transfer.set_cross_point_level(oi, si, level)
            attempt(_level)
        touched.append(out["label"])

    return {"ok": failed == 0 and bool(touched), "outputs": touched, "skipped": skipped,
            "applied": applied, "failed": failed}


# A scheme that names more outputs than this is SUMMARISED rather than listed. It is read aloud
# through the page's live region, and a full-desk scheme listed output by output is a 60-word
# sentence nobody can hold in their head - the custom mixes are the part worth hearing.
_DESCRIBE_LIST_LIMIT = 3


def scope_text(scheme: dict, device=None) -> str:
    """How much of the desk this preset writes, in three or four words, for the option LABEL.

    WHY THE LABEL AND NOT THE DESCRIPTION: the description hangs off the Apply button, so it is
    heard after the choice is made rather than while it is being made, and whether a preset leaves
    every other output alone is exactly what the choice turns on. A suffix on the option is read as you arrow
    past it, and it cannot be put on the dropdown as a description without a screen reader
    re-announcing it over the option name (see index.html, the deliberate omission there).

    DERIVED, NEVER STORED. A preset saved on a Pro 2 and read on a Duo names outputs that desk does
    not have, and a stored phrase would then be a lie."""
    dev = device or devices.get_active_device()
    have = {o["key"]: o for o in dev.matrix.outputs}
    named = [have[k] for k in (scheme.get("outputs") or {}) if k in have]
    if len(named) == 1:
        return f"{named[0]['label']} only"
    if not named:
        return "nothing on this device"
    if len(named) >= len(have):
        return "every output"
    return f"{len(named)} outputs only"


def describe(scheme: dict, device=None, expanded: bool = False) -> str:
    """A sentence saying what applying this scheme would do, for the page to read out BEFORE the
    user commits to it. Built from the scheme, not stored with it, so it is never out of date.

    `expanded` is the desk's USB mode, and it changes nothing but the SOURCE COUNT. A scheme stores
    every row the grid carries, while the page shows RODE's Expanded four only in Expanded, so
    "1 of 13" in Standard would quote a denominator nobody can see. The same filter drops a row
    belonging to another desk, which `apply` would skip anyway."""
    dev = device or devices.get_active_device()
    shown = {s["key"] for s in dev.matrix.sources_for(expanded)}
    out_by_key = {o["key"]: o for o in dev.matrix.outputs}
    found, missing = [], []
    for okey, ospec in (scheme.get("outputs") or {}).items():
        out = out_by_key.get(okey)
        if out is None:
            missing.append(okey)
        else:
            found.append((out, ospec))

    def one(out, ospec) -> str:
        mode = ospec.get("mode")
        if mode == 2:
            srcs = {k: c for k, c in (ospec.get("sources") or {}).items() if k in shown}
            kept = [k for k, c in srcs.items() if not c.get("excluded")]
            what = f"a custom mix carrying {len(kept)} of {len(srcs)} sources"
        elif mode == 1:
            what = "mix minus"
        elif mode == 0:
            what = "the main mix"
        else:
            what = "its current mix"
        return f"{out['label']} to {what}"

    if not found:
        return "This scheme names no output this device has, so applying it would do nothing."
    if len(found) <= _DESCRIBE_LIST_LIMIT:
        text = "Sets " + ", and ".join(one(out, spec) for out, spec in found) + "."
    else:
        # Only the outputs NOT going to their own default mode are worth naming one by one.
        unusual = [(out, spec) for out, spec in found
                   if spec.get("mode") != int(out.get("default_mode", 0))]
        normal = len(found) - len(unusual)
        everything = len(found) >= len(out_by_key)
        text = f"Sets {'all ' if everything else ''}{len(found)} outputs"
        if not unusual:
            text += " to their normal mix."
        else:
            text += (f": {normal} to their normal mix, and " if normal else ": ")
            text += ", and ".join(one(out, spec) for out, spec in unusual) + "."
    if missing:
        text += (f" {count(len(missing), 'part')} of this scheme "
                 f"{plural(len(missing), 'is', 'are')} not on this device and would be "
                 f"skipped: {', '.join(missing)}.")
    return text
