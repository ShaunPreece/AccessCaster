"""Single source of truth for EVERY soundpad operation (read, health, clear, create sound,
create voice-FX, restore factory FX).

ALL pad logic lives HERE and the web layer calls into it, so no second copy can drift. A pad is
never overwritten in place by a create: that truncates its WAV. A new sound is always clear then
create.

LAYERING: pad_records.py stays underneath as the low-level byte-builder (it has the byte-for-byte
selftest vs the captures). soundpad.py is the operations layer over it. Nothing else holds pad logic.

THE ONE RULE (SOUNDPADS.md): keep the region-B effect-object pool clean (one object per FX pad).
Every mutating op cleans the pool first (clean_pool) and re-reports health, because a stray object
can collide with a create and silence another pad's live effect.

THE READER: pads and effect objects are read off the parsed tree (pad_nodes, effect_objects),
never by searching the dump for a property name; field order inside a pad record varies.

The choreography is documented in SOUNDPADS.md; the load-bearing facts are kept as comments at each
function (handle = count+0, REUSE-not-append the effect sub-handle, the create sync is an async ACK
not a gate, selector = 0-based stream position).

CLI:
  python soundpad.py list                     # pad grid + pool health
  python soundpad.py health                    # pool health JSON only
  python soundpad.py clean                     # remove orphan effect objects
  python soundpad.py positions                 # raw stream positions
  python soundpad.py clear <grid>              # clear grid pad (1-based)
  python soundpad.py create <grid> <folder> <wav> [name] [colour]   # create sound (clear+create)
  python soundpad.py fx <grid> <recipe-name>   # assign a voice-FX recipe
  python soundpad.py mixer <grid> <mode> [name]   # create a mixer-action pad
  python soundpad.py trigger <grid> [name]     # create a MIDI trigger pad (default mapping)
  python soundpad.py set <grid> <property> <value>   # in-place edit of a PAD_PROP property
  python soundpad.py restore <name|all|list>   # restore factory FX from fx_recipes.json
"""
from __future__ import annotations

import contextlib
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import time
from collections import Counter
from typing import NamedTuple

import diagnostics      # the app's own account: why a pad is being rebuilt, and why not
import rcp_hid          # open_handle / write_report: every handle and every write is logged
import rcp_read as rr
import rcp_transfer as t
import pad_records as pr
import devices
from paths import resource_path, data_path
from wording import count, plural


# ----------------------------------------------------------------------------------------------
# DEVICE AWARENESS - pad grid geometry + the pad-write safety gate
# ----------------------------------------------------------------------------------------------
# Pad READS are always safe on any RODECaster (the model dump is identical in shape). Pad WRITES
# (create/clear/edit) rely on the region-B effect-object pool choreography, which is only VERIFIED
# on devices carrying the "pad_write" cap. Every device-writing pad primitive calls
# _require_pad_write() first, so on a device without the cap it refuses BEFORE any HID write and
# cannot append a pad stray. The UI also disables the controls (server passes a read_only flag),
# so this is a backstop, not the only guard.

class PadWriteUnsupported(RuntimeError):
    """Raised by a pad-write primitive when the active device lacks the 'pad_write' cap."""


def active_pad_spec():
    """The active device's PadSpec (grid geometry). Pro II 8/bank, Duo 6/bank; DEFAULT (Pro II)
    when nothing is connected, so offline/test code keeps Pro II behaviour."""
    return devices.get_active_device().pad


def pad_writes_enabled() -> bool:
    """True iff the active device is verified for SMART-pad writes (has the 'pad_write' cap)."""
    return "pad_write" in devices.get_active_device().caps


def _pad_ro_message() -> str:
    dev = devices.get_active_device()
    return (f"Editing SMART pads is not enabled on the {dev.name} yet, so pads are read-only "
            "here. You can view them but not change them.")


def _require_pad_write() -> None:
    """Raise PadWriteUnsupported if the active device is not verified for pad writes."""
    if not pad_writes_enabled():
        raise PadWriteUnsupported(_pad_ro_message())


@contextlib.contextmanager
def _transfer_mode(settle: float):
    """Enter transfer mode, wait `settle` seconds, and ALWAYS leave again, even on an error. A desk
    left in transfer mode keeps its FAT volume exposed and its audio dropped until something else
    exits it. Inside rcp_transfer.transfer_session both calls do nothing; the session owns them."""
    t.set_transfer_mode(True)
    try:
        time.sleep(settle)
        yield
    finally:
        t.set_transfer_mode(False)


# ----------------------------------------------------------------------------------------------
# CONSTANTS
# ----------------------------------------------------------------------------------------------

# Factory voice-FX recipes (the validated multi-effect presets) plus the two shipped extras
# Reverb/Echo (padIdx null), keyed by display name. The user's own saved effects live in a
# separate file (FX_USER_FILE) and are merged in by all_recipes(); factory names win on clash.
with open(resource_path("fx_recipes.json"), encoding="utf-8") as _f:
    RECIPES = json.load(_f)                                                   # ships read-only
del _f
FX_USER_FILE = data_path("fx_user_presets.json")                              # user-saved, writable

# The six effect engines and which params each one owns (for validating a custom recipe). Every
# param is a normalised float 0..1 EXCEPT pitchShiftSemitones, which is -12..+12 (one octave).
FX_ENGINE_PARAMS = {
    "reverb": ["reverbMix", "reverbLowCut", "reverbHighCut", "reverbModel"],
    "echo": ["echoLowCut", "echoHighCut", "echoDelay", "echoMix", "echoDecay"],
    "pitchShift": ["pitchShiftSemitones"],
    "distortion": ["distortionIntensity"],
    "robot": ["robotMix", "robotLevel"],
    "voiceDisguise": [],
}


def load_user_fx_presets() -> dict:
    """The user's saved custom voice-FX presets (same shape as RECIPES entries)."""
    try:
        with open(FX_USER_FILE, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_user_fx_presets(d: dict) -> None:
    with open(FX_USER_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)


def all_recipes() -> dict:
    """Every offered voice effect: factory + shipped extras + the user's custom presets.
    Factory/extra names take precedence so a stray user entry can never shadow a built-in."""
    return {**load_user_fx_presets(), **RECIPES}


def _sanitise_stack(stack: dict) -> dict:
    """Drop unknown engines and params from an effect stack and clamp every value to its range.

    An empty result is allowed: a preview pad starts silent and effects are switched into it.
    validate_fx_stack is the strict form. An engine that keeps no params is NOT an error:
    voiceDisguise legitimately has none.
    """
    clean = {}
    for engine, params in (stack or {}).items():
        if engine not in FX_ENGINE_PARAMS:
            continue
        allowed = FX_ENGINE_PARAMS[engine]
        kept = {}
        for p, v in (params or {}).items():
            if p not in allowed:
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            kept[p] = (max(-12.0, min(12.0, v)) if p == "pitchShiftSemitones"
                       else max(0.0, min(1.0, v)))
        clean[engine] = kept
    return clean


def validate_fx_stack(stack: dict) -> dict:
    """Clean + range-check a custom effect stack ({engine: {param: value}}) that MUST end up with
    something in it. Returns the sanitised stack; raises ValueError if nothing valid remains."""
    if not isinstance(stack, dict) or not stack:
        raise ValueError("the effect has no engines selected")
    clean = _sanitise_stack(stack)
    if not clean:
        raise ValueError("none of the selected effects are recognised")
    return clean


def save_user_fx_preset(name: str, stack: dict, colour: int = 8) -> str:
    """Validate + persist a custom voice-FX preset. Returns the stored name. Refuses to overwrite
    a built-in (factory/extra) name so the originals can't be clobbered."""
    name = (name or "").strip()
    if not name:
        raise ValueError("the effect needs a name")
    if name in RECIPES:
        raise ValueError(f"'{name}' is a built-in effect name; choose another")
    clean = validate_fx_stack(stack)
    colour = max(0, min(11, int(colour)))
    user = load_user_fx_presets()
    user[name] = {"padIdx": None, "colour": colour, "stack": clean}
    _save_user_fx_presets(user)
    return name


def delete_user_fx_preset(name: str) -> bool:
    """Remove a saved custom preset. Returns True if it existed."""
    user = load_user_fx_presets()
    if name in user:
        del user[name]
        _save_user_fx_presets(user)
        return True
    return False


# Every field a live preview edit is allowed to write: each engine's params + its on-flag, plus the
# mic-input selector. Anything else is rejected so a stray write can never create cruft.
_PREVIEW_FLOAT_PARAMS = {p for ps in FX_ENGINE_PARAMS.values() for p in ps if p != "pitchShiftSemitones"}
_PREVIEW_ONFLAGS = set(pr.EFFECT_FLAG.values())


def sanitise_preview_edit(param: str, value):
    """Validate + clamp one live-preview edit. Returns (param, value, kind) or raises ValueError.
    Accepts an engine param (float 0..1), pitchShiftSemitones (-12..12), an engine on-flag (bool),
    or padEffectInput (int 0..3 = Mic 1..4)."""
    try:
        if param in _PREVIEW_ONFLAGS:
            return param, bool(value), "bool"
        if param == "pitchShiftSemitones":
            return param, max(-12.0, min(12.0, float(value))), "float"
        if param == "padEffectInput":
            return param, max(0, min(3, int(value))), "int"
        if param in _PREVIEW_FLOAT_PARAMS:
            return param, max(0.0, min(1.0, float(value))), "float"
    except (TypeError, ValueError):
        raise ValueError(f"{value!r} is not a valid value for {param}") from None
    raise ValueError(f"{param!r} is not a previewable control")


# ----------------------------------------------------------------------------------------------
# LIVE PREVIEW (temp pad + in-place edits; both primitives ear-proven)
# A scratch FX pad carries the in-progress effect. Creating it sets padActive ON, which engages
# the effect on the live mic hands-free (ear-proven). Each control change is streamed
# as an in-place edit (padActive + the one field on the FX sub-handle; ear-proven), so
# nothing is reallocated. Stopping clears the pad. padEffectInput 0 = Mic 1 (the create default).
# ----------------------------------------------------------------------------------------------

PREVIEW_NAME = "Preview"


def find_empty_grid(banks: int | None = None, pads_per_bank: int | None = None, prefer_bank: int = 3):
    """Grid number (1-based) of an empty pad to use as the preview scratch pad, preferring the
    given bank (3 = the spare bank), else any empty user pad across the user banks. Returns
    None if every user pad is full. Geometry defaults to the ACTIVE DEVICE (Pro II 8/bank,
    Duo 6/bank) unless explicitly overridden."""
    spec = active_pad_spec()
    if banks is None:
        banks = spec.user_banks
    if pads_per_bank is None:
        pads_per_bank = spec.pads_per_bank
    present = {pidx for _pos, pidx, _t, _n in pad_positions()}
    total = banks * pads_per_bank
    order = list(range((prefer_bank - 1) * pads_per_bank, prefer_bank * pads_per_bank))
    order += [i for i in range(total) if i not in order]
    for pidx in order:
        if pidx not in present:
            return pidx + 1
    return None


def preview_start(stack: dict, colour: int = 8, mic: int = 1) -> dict:
    """Create the in-progress effect on an empty scratch pad and engage it live. `mic` is 1..4
    (Mic 1 is the proven default). Returns {ok, grid} or {ok:False, no_pad:True} if the grid
    is full. The effect is audible immediately (padActive ON); stream edits via preview_update."""
    grid = find_empty_grid()
    if grid is None:
        return {"ok": False, "no_pad": True,
                "message": "Live preview needs one free pad to play through. Clear a pad first."}
    pidx = grid - 1
    # Build the effect (or a silent shell if nothing is on yet). safe_assign_fx_recipe scrubs the
    # slot first so the pool can never double up. Empty stack -> a VFX pad with every engine off.
    safe_assign_fx_recipe(pidx, PREVIEW_NAME, _sanitise_stack(stack),
                          colour=max(0, min(11, int(colour))))
    time.sleep(0.4)
    if int(mic) != 1:
        preview_update(grid, "padEffectInput", max(0, min(3, int(mic) - 1)), "int")
    return {"ok": True, "grid": grid,
            "message": f"Live preview on. Talk into Mic {int(mic)} to hear it."}


def preview_update(grid: int, param: str, value, kind: str = "float") -> dict:
    """Stream one in-place edit to the live preview pad, re-asserting padActive so the effect stays
    engaged while you tweak. padEffectInput lives on the PAD frame; every effect param and on-flag
    lives on the pad's FX sub-handle. Pass values through sanitise_preview_edit first."""
    if not pad_writes_enabled():
        return {"ok": False, "message": _pad_ro_message()}
    padidx = grid - 1
    pos = next((p for p, pi, _t, _n in pad_positions() if pi == padidx), None)
    if pos is None:
        return {"ok": False, "message": "The preview pad is no longer there. Start preview again."}
    if param == "padEffectInput":
        field = pr.write_record(param, kind, value, pos, pr.FRAME_PAD)
    else:
        field = pr.write_record(param, kind, value, free_effect_subhandle(padidx), pr.FRAME_FX)
    recs = [pr.write_record("padActive", "bool", True, pos, pr.FRAME_PAD), field]
    with _transfer_mode(0.4):
        t.send_records_paced(recs, delay=0.1)
        time.sleep(0.2)
    return {"ok": True}


def preview_stop(grid: int) -> dict:
    """Clear the preview scratch pad, removing the effect from the live mic."""
    pos = next((p for p, pi, _t, _n in pad_positions() if pi == grid - 1), None)
    if pos is None:
        return {"ok": True, "message": "Preview already stopped."}
    clear_grid_pad(grid)
    return {"ok": True, "message": "Live preview stopped."}


PAD_TYPE_LABEL = {
    1: "Sound", 2: "Voice effect", 3: "Mixer", 4: "MIDI", 5: "Quick Dial", 6: "System",
}
# NOTE THAT 3 IS MIXER, NOT A VOICE EFFECT, which is why "Censor" and "Ducking" read as 3; getting
# that backwards sends a mixer pad down the sound path.
PAD_TYPE_SOUND = 1        # plays audio: MIDI only, HID sets the flag and plays nothing
PAD_TYPE_VOICE_FX = 2     # processes a live mic: HID, and the flag IS the state
PAD_TYPE_MIXER = 3        # performs a mixer action: HID, same as voice FX
PAD_TYPE_TRIGGER = 4      # sends a MIDI message
# Bank 8 (padIdx 56-63): Input 1-4, Scene A/B, Auto, Cut. The desk owns these - we never create,
# clear, back up or unlatch one.
SYSTEM_PAD_TYPE = 6

# The one non-pad region-B effect-object present on a clean table (SOUNDPADS.md section 2).
SPECIAL_EFFECTS_IDX = 65

# Clear addressing: selector = the pad's 0-based stream position + OFFSET. On a CLEAN table the
# 0-based padName position equals the device selector (verified on clean bank-1). Keep 0.
SELECTOR_OFFSET = 0


# ----------------------------------------------------------------------------------------------
# READ / STREAM POSITIONS
# ----------------------------------------------------------------------------------------------

class Pad(NamedTuple):
    """One PAD node under SOUNDPADS, as the tree parser reads it."""
    position: int           # child index under SOUNDPADS: the pad write selector
    idx: int | None         # padIdx, the grid slot (0-based)
    type: int | None        # padType, see PAD_TYPE_LABEL
    name: str | None
    props: dict             # every property on the node, by name


class EffectObject(NamedTuple):
    """One EFFECTS_PARAMETERS node under PADEFFECTS (region B)."""
    index: int              # child index under PADEFFECTS: the 0x9d sub-handle
    effects_idx: int | None
    detached: bool          # see effect_objects

    @property
    def partial(self) -> bool:
        """A node with no effectsIdx: the 13-property shell (distortion, echo and reverb only) that
        an FX pad's clear leaves instead of collecting its object, when an object earlier in the
        list has been removed since. Binds nothing."""
        return self.effects_idx is None


def _top_node(blob: bytes, name: str):
    """The root child called `name`, or None if the dump has none (an empty dump has none).
    rcp_read raises ModelUnreadable for a dump that does not parse."""
    root = rr.node_at(blob, [])
    return next((n for n in root.kids if n.name == name), None) if root else None


def pad_nodes(blob=None) -> list:
    """Every pad record as a Pad, in stream order. `blob` reuses a dump; default is a fresh read."""
    if blob is None:
        blob = rr.read_model(fresh=True)
    pads = _top_node(blob, "SOUNDPADS")
    out = []
    for node in (pads.kids if pads else ()):
        props = {p: v for p, v, _k in node.props}
        out.append(Pad(node.index, props.get("padIdx"), props.get("padType"),
                       props.get("padName"), props))
    return out


def pad_positions(blob=None):
    """[(position, padIdx, padType, name)] for every pad record, in stream order."""
    return [(p.position, p.idx, p.type, p.name) for p in pad_nodes(blob)]


def _fields_by_padidx(blob: bytes, fields) -> dict:
    """{field: {padIdx: value}} for several fields. A pad that does not carry a field (padMixerMode
    exists on mixer pads only) is left out of that field's map."""
    out = {f: {} for f in fields}
    for p in pad_nodes(blob):
        if p.idx is None:
            continue
        for f in fields:
            if f in p.props:
                out[f][p.idx] = p.props[f]
    return out


def _field_by_padidx(blob: bytes, field: str) -> dict:
    """Map padIdx -> value of one `field` (see _fields_by_padidx)."""
    return _fields_by_padidx(blob, (field,))[field]


# ----------------------------------------------------------------------------------------------
# POOL HEALTH (the clean-pool gate)
# ----------------------------------------------------------------------------------------------

def effect_objects(blob=None) -> list:
    """Every region-B effect object (PADEFFECTS' children) as an EffectObject, in stream order.

    DETACHED means the node stores `effectsIdx` FIRST. A live pad's object stores it last; the
    special object 65 stores it first but belongs to no pad, so it is not counted as detached. RODE's
    app leaves detached objects behind (seen after a pad was retyped). No pad binds to one, so a pad
    clear never collects it, and the only removal is a delete by tree path (_delete_effect_object).
    """
    if blob is None:
        blob = rr.read_model_stable(fresh=True)
    fx = _top_node(blob, "PADEFFECTS")
    out = []
    for node in (fx.kids if fx else ()):
        props = {p: v for p, v, _k in node.props}
        eidx = props.get("effectsIdx")
        first = node.props[0][0] if node.props else None
        out.append(EffectObject(node.index, eidx,
                                first == "effectsIdx" and eidx != SPECIAL_EFFECTS_IDX))
    return out


def pool_health(blob: bytes | None = None) -> dict:
    """Verdict on the region-B effect-object pool: clean iff there is exactly one object per
    voice-FX pad (effectsIdx == its padIdx) plus the single special object, with no duplicate
    effectsIdx, no orphan (object with no matching FX pad), no missing object and no detached one.

    Detached objects are listed on their own (`detached`, by effectsIdx) and kept out of the other
    three counts: they bind nothing, so one sharing a live pad's padIdx is not a duplicate. Partial
    objects carry no effectsIdx, so they are listed by node index (`partial`)."""
    if blob is None:
        blob = rr.read_model_stable(fresh=True)
    fx_pad_idxs = sorted(p.idx for p in pad_nodes(blob)
                         if p.type == PAD_TYPE_VOICE_FX and p.idx is not None)
    objects = effect_objects(blob)
    attached = [o.effects_idx for o in objects if not o.detached and not o.partial]
    counts = Counter(attached)
    expected = set(fx_pad_idxs) | {SPECIAL_EFFECTS_IDX}
    duplicates = sorted(k for k, c in counts.items() if c > 1)
    orphans = sorted(k for k in counts if k not in expected)
    missing = sorted(i for i in fx_pad_idxs if i not in counts)
    detached = [o.effects_idx for o in objects if o.detached]
    partial = [o.index for o in objects if o.partial]
    return {
        "clean": not duplicates and not orphans and not missing and not detached and not partial,
        "region_b_count": len(objects),
        "fx_pads": fx_pad_idxs,
        "effects_idx": sorted(attached),
        "duplicates": duplicates,
        "orphans": orphans,
        "missing": missing,
        "detached": detached,
        "partial": partial,
    }


def _health_message(health: dict) -> str:
    # Silent in the normal (clean) case - users never see effect-object/orphan wording. Orphans, and
    # a detached or partial object at the END of the list, are removed by clean_pool during
    # create/clear. What can survive: a detached or partial object in the middle of the list, and a
    # live-pad DUPLICATE, which needs an ear-checked clear and recreate (SOUNDPADS 5b).
    if health["clean"]:
        return ""
    if health.get("detached"):
        pads = ", ".join(str(i + 1) for i in sorted(set(health["detached"])))
        # It becomes last, and is deleted, once the voice-effect pads added after it are cleared.
        return (f"A leftover voice-effect definition for pad {pads} is stored on the desk, so a "
                "voice effect cannot go on that pad yet. It is removed automatically once the "
                "voice-effect pads added after it are cleared.")
    if health.get("partial"):
        # Harmless while it waits: an FX pad whose object sits after it was heard working. It
        # becomes last, and is deleted, once the pads after it are cleared.
        return ("An empty leftover voice-effect entry is stored on the desk. No pad uses it and "
                "your pads still work; it is removed automatically once the voice-effect pads "
                "added after it are cleared.")
    if health["missing"]:
        # Named separately from a duplicate, which is a different fault with a different repair:
        # this is a state the desk reaches ON ITS OWN (see heal_fx_pool), not only after a crash
        # mid-write.
        n = len(health["missing"])
        return (f"{count(n, 'voice-effect pad')} {plural(n, 'has', 'have')} no effect to play, so "
                f"{plural(n, 'it does', 'they do')} nothing. Use \"Restore the factory voice "
                f"effects, Censor and Ducking\" on the SMART pads page to rebuild.")
    return ("One voice-effect pad has a duplicate effect definition that needs a manual rebuild "
            "(clear and recreate that pad). Other pads are unaffected.")



def _resolve_recipe(name: str) -> str:
    key = _safe_match_recipe(name)
    if key is None:
        raise ValueError(f"unknown voice effect {name!r}; known: {list(all_recipes())}")
    return key


def _position_of(pidx: int, blob=None):
    """Stream position / type / name of the pad currently at padIdx `pidx` (or None,None,None)."""
    for pos, p, ptype, name in pad_positions(blob):
        if p == pidx:
            return pos, ptype, name
    return None, None, None


# ----------------------------------------------------------------------------------------------
# LIVE PAD GRID (what the website renders)
# ----------------------------------------------------------------------------------------------

def read_pads_live(min_banks: int = 3, max_banks: int | None = None,
                   pads_per_bank: int | None = None) -> dict:
    """Live pad grid (one model read) + pool health + the recipe names the UI offers.

    Geometry is DEVICE-AWARE (defaults from the active device's PadSpec when not overridden):
    the Pro II has 8 banks of 8 (padIdx 0-63, system pads 56-63); the Duo has 8 banks of 6
    (padIdx 0-47, system pads 42-47). Banks 1..user_banks are user-assignable; the top bank holds
    the factory system pads. Rather than a fixed size, the visible grid GROWS with content: it
    shows every filled user bank plus one spare empty bank to add to (floor min_banks, cap
    user_banks). So creating a pad on a new bank reveals the next one, like the device itself."""
    spec = active_pad_spec()
    if pads_per_bank is None:
        pads_per_bank = spec.pads_per_bank
    if max_banks is None:
        max_banks = spec.user_banks
    blob = rr.read_model_stable(fresh=True)
    # bc_props is what THIS desk offers, so the per-pad "back_channel" map below always has exactly
    # the keys the UI will render.
    bc_props = [s["prop"] for s in back_channel_sources()]
    mixer_fields = _fields_by_padidx(
        blob, ["padMixerMode", "padMixerTriggerMode", "padPlayMode", "padFilePath"]
        + bc_props + TRIGGER_PROPS + EXTRA_PAD_PROPS)
    mixer_modes = mixer_fields["padMixerMode"]
    mixer_triggers = mixer_fields["padMixerTriggerMode"]
    play_modes = mixer_fields["padPlayMode"]
    # WHICH FILE A SOUND PAD ACTUALLY PLAYS, straight from the desk (padFilePath, e.g.
    # "/Application/emmc-data/pads/1/Music_Bed.wav"). This is the ONLY authoritative answer: a pad
    # folder can hold more than one wav (leftovers from earlier replacements), so picking a file by
    # listing the folder can pick the wrong one.
    file_paths = mixer_fields["padFilePath"]
    by_idx = {}
    for p in pad_nodes(blob):
        if p.idx is not None:
            by_idx[p.idx] = {"position": p.position, "type": p.type, "name": p.name,
                             "replay": p.props.get("padReplay"),
                             "gain": p.props.get("padGain"),
                             "loop": p.props.get("padLoop"),
                             "colour": p.props.get("padColourIndex")}
    # How many banks to show: highest USER bank that has real content, plus one spare. System
    # pads (padType 6, padIdx 56-63) and anything past the user range are ignored for this.
    max_user_pidx = max_banks * pads_per_bank
    filled = [pidx for pidx, info in by_idx.items()
              if pidx < max_user_pidx and info["type"] != SYSTEM_PAD_TYPE]
    highest_bank = (max(filled) // pads_per_bank + 1) if filled else 0
    banks = max(min_banks, min(max_banks, highest_bank + 1))
    grid = []
    for n in range(1, banks * pads_per_bank + 1):
        pidx = n - 1
        info = by_idx.get(pidx)
        # replay + loop only apply to sound pads (type 1); null elsewhere so the UI shows no control.
        is_sound = bool(info and info["type"] == PAD_TYPE_SOUND)
        replay = info["replay"] if is_sound else None
        loop = info["loop"] if is_sound else None
        # Mixer pads (type 3) carry a trigger mode; Back Channel pads (mode 3) also carry the
        # member flags. Null on every other pad type so the UI shows no control at all.
        is_mixer = bool(info and info["type"] == PAD_TYPE_MIXER)
        members = ({p: bool(mixer_fields[p].get(pidx)) for p in bc_props}
                   if is_mixer and mixer_modes.get(pidx) == MIXER_MODE["backchannel"] else None)
        # MIDI trigger pads (type 4) carry the eight padTrigger* fields: WHAT the pad sends. Null on
        # every other type, so the UI shows no control at all. Without reading them, the page can
        # only offer the firmware's default mapping and a profile cannot back a custom one up.
        is_trigger = bool(info and info["type"] == PAD_TYPE_TRIGGER)
        trigger = ({k: mixer_fields[p].get(pidx) for k, p in TRIGGER_FIELD.items()}
                   if is_trigger else None)
        if trigger is not None:
            trigger["custom"] = bool(trigger["custom"])
        # Three groups a profile easily drops. Each is null on a pad it does not apply to, so the
        # editor and the backup both see "not applicable" rather than a zero that looks like a
        # setting. ENVELOPE is sound pads; FADE and CENSOR are the two mixer MODES that own them.
        mixer_mode = mixer_modes.get(pidx) if is_mixer else None
        envelope = ({k: mixer_fields[p].get(pidx) for k, p in ENV_FIELD.items()}
                    if is_sound else None)
        fade = ({k: mixer_fields[p].get(pidx) for k, p in FADE_FIELD.items()}
                if mixer_mode == MIXER_MODE["fade"] else None)
        if fade is not None:
            fade["exclude_host"] = bool(fade["exclude_host"])
        censor = ({k: mixer_fields[p].get(pidx) for k, p in CENSOR_FIELD.items()}
                  if mixer_mode == MIXER_MODE["censor"] else None)
        if censor is not None:
            censor["custom"] = bool(censor["custom"])
        grid.append({
            "grid": n,
            "bank": (n - 1) // pads_per_bank + 1,
            "pad": (n - 1) % pads_per_bank + 1,
            "padIdx": pidx,
            "present": info is not None,
            "name": info["name"] if info else None,
            "type": info["type"] if info else None,
            "type_label": PAD_TYPE_LABEL.get(info["type"], "Other") if info else "Empty",
            "replay": replay,
            "loop": loop,
            # Toggle(0)/Hold(1)/OneShot(2), sound pads only (see PLAY_MODE_LABEL).
            "play_mode": (play_modes.get(pidx) if is_sound else None),
            "colour": (info["colour"] if info else None),
            "gain": (round(info["gain"], 1) if (info and info["gain"] is not None) else None),
            # Filename the pad plays (basename of padFilePath), sound pads only; null elsewhere.
            "file": (os.path.basename(file_paths.get(pidx) or "") or None) if is_sound else None,
            # which mixer action (0-4), for mixer pads only; lets the UI pre-select the mode.
            "mixer_mode": (mixer_modes.get(pidx) if is_mixer else None),
            # Latching(0)/Momentary(1), mixer pads only.
            "mixer_trigger": (mixer_triggers.get(pidx) if is_mixer else None),
            # {prop: bool} for Back Channel pads only; null for anything else.
            "back_channel": members,
            # What a MIDI trigger pad sends; null for anything else. See TRIGGER_FIELD.
            "trigger": trigger,
            # A sound pad's trim/fade envelope, a Fade pad's times, a Censor pad's own sound. Each
            # null unless it applies to this pad (see ENV_FIELD / FADE_FIELD / CENSOR_FIELD).
            "envelope": envelope,
            "fade": fade,
            "censor": censor,
        })
    health = pool_health(blob)
    # Creatable "special" pad types for the Create-a-special-pad UI: the 5 mixer-action modes
    # plus a MIDI trigger. All are pool-safe (no region-B writes), so not gated on a clean pool.
    special_types = [{"value": f"mixer:{m}", "label": MIXER_MODE_LABEL[m]}
                     for m in sorted(MIXER_MODE_LABEL)]
    special_types.append({"value": "trigger", "label": "MIDI Trigger"})
    return {
        "banks": banks,
        "pads_per_bank": pads_per_bank,
        "pads": grid,
        "health": health,
        "health_message": _health_message(health),
        "recipes": list(all_recipes().keys()),
        "custom_fx": list(load_user_fx_presets().keys()),
        "special_types": special_types,
        # The Back Channel members this desk offers, and the two trigger modes, so the pad editor
        # renders whatever the device supports instead of a hardcoded list.
        "back_channel_sources": back_channel_sources(),
        "mixer_triggers": [{"value": v, "label": MIXER_TRIGGER_LABEL[v]}
                           for v in sorted(MIXER_TRIGGER_LABEL)],
        # The MIDI trigger mapping fields and their ranges, so the editor's labels, help text and
        # limits come from the same spec the write validation uses.
        "trigger_edit": trigger_choices(),
        # The trim/fade envelope (sound pads) and the fade times (Fade mixer pads).
        "extra_edit": pad_extra_choices(),
        "play_modes": [{"value": v, "label": PLAY_MODE_LABEL[v]} for v in sorted(PLAY_MODE_LABEL)],
        "play_mode_one_shot": PLAY_MODE_ONE_SHOT,
        # The colour palette with real names, so the UI offers "Red" rather than the number 0.
        "colours": pad_colours(),
        # True on a device whose pad WRITES are not verified (no pad_write cap): the UI shows the
        # pads but disables create/clear/edit so nothing is a silent no-op.
        "read_only": not pad_writes_enabled(),
    }


# ----------------------------------------------------------------------------------------------
# EFFECT-OBJECT ADDRESSING (region-B sub-handle)
# ----------------------------------------------------------------------------------------------

def current_pad_count() -> int:
    """Number of pad records currently in the device model (= padName occurrences).

    Read FRESH right before a create: the free edit handle is derived from this count, and using
    a stale/too-low handle makes the create overwrite an existing pad's stream position (this
    corrupted a system pad on a full table - see SOUNDPADS.md / pad_records.HANDLE).
    """
    return len(pad_nodes())


def wait_for_pad_present(target_idx: int, timeout: float = 6.0, poll: float = 0.3) -> bool:
    """Poll until grid pad `target_idx` (0-based padIdx) appears in the device model, or timeout.
    Returns True if it appeared. Used BETWEEN rapid creates: a fast create (e.g. a voice-FX pad,
    which copies no file) can finish before the model reflects the new pad, so the NEXT create
    would read a stale, too-low pad count and reuse the same free handle, OVERWRITING the pad just
    made. Waiting for the pad to actually register means the next create reads a settled count."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(pi == target_idx for _p, pi, _ty, _n in pad_positions()):
            return True
        time.sleep(poll)
    return False


def free_effect_subhandle(target_idx: int) -> int:
    """Effects sub-handle for a pad = REUSE the pad's FIRST existing effect object's index under
    PADEFFECTS; only APPEND (the object count) if the pad has no object yet.

    WHY REUSE, NOT APPEND (decoded + ear-proven): the LIVE FX engine binds a pad to the
    FIRST region-B effect-object whose effectsIdx == padIdx. Appending our effect at the end left
    the first object empty, so the engine bound silence and the pad went dead after a reboot. The
    RODE app writes to the pad's FIRST matching object. So we reuse that index. (Dead ends: hard-
    coded 0x0B; sub=total count; sub=region-B append - all silenced this/other pad's FX.)
    """
    objects = effect_objects()
    return next((o.index for o in objects if o.effects_idx == target_idx), len(objects))


# ----------------------------------------------------------------------------------------------
# LOW-LEVEL HID RECORD BUILDERS + THE ONE-SESSION CREATE SENDER
# ----------------------------------------------------------------------------------------------

def _pad256(payload: bytes) -> bytes:
    rec = bytes([0x03]) + len(payload).to_bytes(4, "little") + payload
    return rec + bytes(256 - len(rec))


def build_pad_bool(name: str, on: bool, sel: int, frame: int = pr.FRAME_PAD) -> bytes:
    """Compact bool on child `sel` of the pad (0x9a) or effects (0x9d) node:
    01 01 02 02 <frame> 02 01 <sel> <name> 00 01 01 <02=on/03=off>."""
    return _pad256(bytes([0x01, 0x01, 0x02, 0x02, frame, 0x02, 0x01, sel & 0xFF]) + name.encode()
                   + bytes([0x00, 0x01, 0x01, 0x02 if on else 0x03]))


def build_select_begin(sel: int, frame: int = pr.FRAME_PAD) -> bytes:
    """select-begin on child `sel`: 04 01 01 02 <frame> 02 01 <sel>."""
    return _pad256(bytes([0x04, 0x01, 0x01, 0x02, frame, 0x02, 0x01, sel & 0xFF]))


# CURSOR-addressed variants (addressing mode 0x00 = the device's current model-edit cursor,
# no stream-position byte). The position-addressed select-begin above over-deletes the
# NEIGHBOUR when the target is the FIRST stream record (a firmware edge case the app never
# hit). The app clears the first record via the cursor instead: a model dump (rcp_read.TRIGGER)
# parks the edit cursor at position 0, then these cursor-addressed records remove exactly that
# one record. Decoded from the app clearing grid pad 10 while it sat at stream pos 0
# (captures/clearpos0.pcap), and verified byte-for-byte on the device.
def build_pad_bool_cursor(name: str, on: bool) -> bytes:
    """Pad bool on the edit CURSOR: 01 01 02 02 9a 02 00 <name> 00 01 01 <02/03>."""
    return _pad256(bytes([0x01, 0x01, 0x02, 0x02, 0x9a, 0x02, 0x00]) + name.encode()
                   + bytes([0x00, 0x01, 0x01, 0x02 if on else 0x03]))


def build_select_begin_cursor() -> bytes:
    """select-begin on the edit CURSOR: 04 01 01 02 9a 02 00 (no position byte)."""
    return _pad256(bytes([0x04, 0x01, 0x01, 0x02, 0x9a, 0x02, 0x00]))


def send_create_full(pre, mid, file_copy_fn, post, delay=0.06,
                     sync_marker=b"padRCVSyncPadType", sync_timeout=6.0, debug=False):
    """One held-open HID session: pre -> (drain sync) -> mid -> file write -> post.

    The device emits padRCVSyncPadType DURING the effects block, so it is buffered by the OS by
    the time we finish `pre`; we clear stale backlog first, then drain to catch the buffered sync.
    The WAV file is copied in the middle (via file_copy_fn) with the edit handle still bound,
    exactly as the app does. Returns (bytes_written, saw_sync).

    DO NOT block waiting for padRCVSyncPadType: it arrives mid-commit as an async ACK, NOT a gate.
    At a brand-new slot the device does not emit the sync until it receives the commit, so a wait
    deadlocks and the create never commits. So: short settle, send the commit, drain opportunistically.
    """
    _require_pad_write()   # backstop: no create HID write on a device lacking the pad_write cap
    iface = t.find_control_interface()
    if iface is None:
        raise RuntimeError("no control interface (close the RODE app)")
    dev = rcp_hid.open_handle(iface, "pad create")
    n = 0
    saw = False
    seen = []

    def drain(acc, want_sync):
        nonlocal saw
        for _ in range(80):
            data = dev.read(256, timeout_ms=20)
            if not data:
                break
            b = bytes(data)
            if debug:
                seen.extend(mm.decode() for mm in re.findall(rb"pad[A-Za-z0-9]{3,40}", b))
            acc.extend(b)
            if want_sync and acc.find(sync_marker) >= 0:
                saw = True
            if len(acc) > 2048:
                del acc[:-128]

    try:
        drain(bytearray(), False)               # clear stale backlog
        for r in pre:
            n += rcp_hid.write_report(dev, r); time.sleep(delay)
        acc = bytearray()
        time.sleep(0.15)
        drain(acc, True)                          # opportunistic: sync may already be buffered
        for r in mid:
            n += rcp_hid.write_report(dev, r); time.sleep(delay)
        drain(acc, True)                          # the sync typically fires during the commit
        # WAV file write happens HERE, edit handle still bound
        file_copy_fn()
        time.sleep(0.2)
        for r in post:
            n += rcp_hid.write_report(dev, r); time.sleep(delay)
        drain(acc, True)
        if debug:
            print(f"    [create] saw_sync={saw}; report-4 names: {sorted(set(seen))}")
    finally:
        rcp_hid.close_handle(dev, "pad create")
    return n, saw


# ----------------------------------------------------------------------------------------------
# CLEAR
# ----------------------------------------------------------------------------------------------

def clear_pad(position: int, debug: bool = False) -> str:
    """Clear the pad at 0-based stream `position`, exactly as the app does (decoded from
    captures/padclear.pcap). The ENTIRE clear is: padActive ON -> padActive OFF -> select-begin,
    then exit transfer mode, and NOTHING else. It sends NO remountPadStorage (writes no files),
    so it does NOT mount drive F and produces NO Windows "drive has a problem" toast.

    Re-read positions first - they shift when a record drops.

    POSITION 0 IS SPECIAL. A position-addressed select-begin (mode 0x01 + selector) over-deletes
    the NEIGHBOUR at position 1 when the target is the FIRST stream record - a firmware edge case
    the RODE app never triggered (its clears were always mid-table). The app clears the first
    record via the model-edit CURSOR instead: dump the model (parks the cursor at position 0),
    then send cursor-addressed (mode 0x00) padActive OFF + select-begin, which removes exactly
    that one record. Decoded from the app clearing grid pad 10 at stream pos 0 and verified on
    device (captures/clearpos0.pcap). So: cursor method for position 0, position method for >=1.
    """
    _require_pad_write()   # backstop: no clear HID write on a device lacking the pad_write cap
    iface = t.find_control_interface()
    if iface is None:
        raise RuntimeError("no control interface (close the RODE app)")

    # NO remountPadStorage -> no drive-F mount/toast. Leaving transfer mode is what completes the clear.
    with _transfer_mode(1.0):
        dev = rcp_hid.open_handle(iface, "pad clear")
        n = 0
        try:
            for _ in range(20):                        # drain stale input
                if not dev.read(256, timeout_ms=10):
                    break
            if position == 0:
                # park the edit cursor at position 0, then clear via the cursor (mode 0x00)
                dev.set_nonblocking(False)
                dev.write(rr.TRIGGER)                   # full model dump -> cursor at position 0
                for _ in range(60):
                    if not dev.read(256, timeout_ms=30):
                        break
                seq = [build_pad_bool_cursor("padActive", False), build_select_begin_cursor()]
                how = "cursor (mode 0x00, dump-parked)"
            else:
                sel = position + SELECTOR_OFFSET
                seq = [build_pad_bool("padActive", True, sel),    # select
                       build_pad_bool("padActive", False, sel),   # confirm-clear part 1
                       build_select_begin(sel)]                   # confirm-clear part 2
                how = f"position (sel=0x{sel:02x})"
            for r in seq:
                n += rcp_hid.write_report(dev, r)
                time.sleep(0.08)
            time.sleep(0.3)
        finally:
            rcp_hid.close_handle(dev, "pad clear")
        time.sleep(0.2)
    return f"cleared position {position} via {how}; sent {n} bytes"


def clear_slot(position: int, ptype) -> str:
    """clear_pad for a pad being cleared IN USE, as opposed to inside the orphan cleanup. A voice-
    effect pad's clear leaves a partial object at the end of the list whenever an earlier object
    has been removed since, so that clear is followed by remove_partial_objects."""
    out = clear_pad(position)
    if ptype == PAD_TYPE_VOICE_FX:
        time.sleep(0.6)
        remove_partial_objects()
    return out


def clear_grid_pad(grid: int) -> dict:
    """Clear the pad at grid position `grid` (1-based), app-style. Reports pool health after.

    A SOUND pad clear is region-B-inert. A non-sound clear can interact with region-B bindings, so
    it AUTO-HEALS any stray orphan objects first (clean_pool, a no-op on a clean pool) rather than
    refuse: pool hygiene is plumbing, not something the user should ever have to act on."""
    pidx = grid - 1
    pos, ptype, name = _position_of(pidx)
    if pos is None:
        return {"ok": True, "message": f"Pad {grid} is already empty.", "health": pool_health()}
    if ptype != PAD_TYPE_SOUND:                    # non-sound clear can touch region-B binding
        clean_pool()                               # silently GC stray orphans first (no-op if clean)
    clear_slot(pos, ptype)
    health = pool_health()
    msg = f"Cleared pad {grid} ({PAD_TYPE_LABEL.get(ptype, 'pad')} '{name}'). " + _health_message(health)
    return {"ok": health["clean"], "message": msg, "health": health}


def clear_all_pads() -> dict:
    """Clear EVERY user pad (sound / voice-FX / mixer / MIDI / quick-dial), leaving the System pads
    (the fixed fader + scene controls, padType 6) untouched. Re-reads positions each pass because
    they shift as records drop. Clearing FX pads GCs their region-B objects; a final clean_pool
    mops up any stray orphan, so the pool ends minimal-clean (just the special object). Returns the
    count cleared, the names, and final health. Destructive - the caller must confirm first."""
    cleared, names = 0, []
    # Safety bound: 96 comfortably exceeds any device's user-pad count (Pro II 8x7=56, Duo 6x7=42),
    # so the loop always drains the whole grid; it is device-agnostic (it clears whatever it reads).
    for _ in range(96):
        target = None
        for pos, pidx, ptype, name in pad_positions():
            if pidx is not None and ptype != SYSTEM_PAD_TYPE:   # skip System pads
                target = (pos, name)
                break
        if target is None:
            break
        clear_pad(target[0])
        cleared += 1
        names.append(target[1])
        time.sleep(0.3)
    clean_pool()                                       # mop up any stray orphan left in the pool
    health = pool_health()
    msg = (f"Cleared {count(cleared, 'pad')}." if cleared else "There were no user pads to clear.")
    return {"ok": health["clean"], "cleared": cleared, "names": names,
            "message": msg + " " + _health_message(health), "health": health}


# Time spent INSIDE transfer mode for an in-place pad edit. Transfer mode is the fragile part (it
# is when the unit exposes its FAT volume, so a long or repeated dwell is what makes Windows throw
# a "problem with drive F" toast), so we sit in it as briefly as the device tolerates and write
# every record for the edit in ONE session rather than one session per record.
# Device-verified on a Pro II by change-read-back-restore: 0.25/0.12 writes padColourIndex, padLoop
# and padGain correctly 15/15 times (518 ms per edit against 1048 ms for 0.6/0.3), pool health
# clean, and even 0.10/0.05 passes 4/4 - so 0.25/0.12 keeps a healthy margin over the point where it
# starts to matter.
PAD_EDIT_SETTLE_S = 0.25    # after entering transfer mode, before the first record
PAD_EDIT_DRAIN_S = 0.12     # after the last record, before leaving transfer mode


def _commit_pad_records(recs: list) -> int:
    """Write pad-table records in ONE transfer-mode session and leave again. Returns bytes written.
    Every in-place pad edit goes through here so the dwell time is defined in one place."""
    _require_pad_write()
    with _transfer_mode(PAD_EDIT_SETTLE_S):
        n = t.send_records_paced(recs, delay=0.12)
        time.sleep(PAD_EDIT_DRAIN_S)
    return n


def set_pad_replay(grid: int, replay: bool) -> dict:
    """Sound pads: padReplay True = a re-press RESTARTS the sound from the start (factory
    behaviour); False = the second press acts as pause/resume. FX and mixer pads carry padReplay
    False and the flag has no meaning for them."""
    res = set_pad_property(grid, "padReplay", bool(replay))
    if res.get("ok"):
        state = "restart from the start" if replay else "pause/resume"
        res["replay"] = bool(replay)
        res["message"] = f"Pad {grid} will {state} on the next press."
    return res


# ----------------------------------------------------------------------------------------------
# EDIT A PAD PROPERTY IN PLACE  (generic; the UI-agnostic editing layer)
# ----------------------------------------------------------------------------------------------

# PAD_PROP (below) is the registry of per-pad properties that are SAFE to edit in place (value
# semantics confirmed). Each entry: kind (int/float/bool/str), label, optional min/max (clamped),
# applies_to (a tuple of padTypes the property is meaningful for, or None = any), and help text.
# set_pad_property() can write an unregistered property given an explicit kind, but only registry
# entries are clamped and type-gated. padEffectInput, padEffectTriggerMode and the mixer
# sub-params other than those registered here have no confirmed value mapping, so they stay out.

# --- Back Channel participants (mixer pads in mode 3) -----------------------------------------
# A Back Channel pad opens a private off-air talkback room: while it is engaged the member
# channels are taken OUT of the main mix (so nothing they say is recorded or streamed) and routed
# to each other instead, and their Listen/Mute buttons glow yellow. Membership is stored as one
# bool per channel ON THE PAD RECORD (padMixerBackChannel*, present on both desks). There is NO
# padMixerBackChannelMic1: Mic 1 (the host) is always in the room, so only the OTHER channels are
# selectable, which matches the RODE app's own page. A pad created with none of these ticked links
# nothing and does nothing when pressed.
# `channel` = the device-side name this entry needs the connected desk to have (its ui_channels
# name or its matrix source key), so a 2-mic desk is not offered Mic 3 / Mic 4.
BACK_CHANNEL_SOURCES = (
    {"prop": "padMixerBackChannelMic2", "label": "Mic 2", "channel": "Mic2"},
    {"prop": "padMixerBackChannelMic3", "label": "Mic 3", "channel": "Mic3"},
    {"prop": "padMixerBackChannelMic4", "label": "Mic 4", "channel": "Mic4"},
    {"prop": "padMixerBackChannelUsb1Comms", "label": "Chat (USB 1 comms)", "channel": "Chat"},
    {"prop": "padMixerBackChannelUsb2Main", "label": "USB 2 (Secondary)", "channel": "USB2"},
    {"prop": "padMixerBackChannelBluetooth", "label": "Bluetooth", "channel": "Bluetooth"},
    {"prop": "padMixerBackChannelCallMe1", "label": "CallMe 1", "channel": "CallMe1"},
    {"prop": "padMixerBackChannelCallMe2", "label": "CallMe 2", "channel": "CallMe2"},
    {"prop": "padMixerBackChannelCallMe3", "label": "CallMe 3", "channel": "CallMe3"},
)

# padMixerTriggerMode, shared by every mixer action (Censor/Trash Talk/Fade/Back Channel/Ducking).
# 1 latches and 0 is momentary, ear-proven on a Back Channel pad. That is the SAME way round as
# padTriggerMode on a MIDI pad, so the two share one convention: do not invert this pair.
MIXER_TRIGGER_LABEL = {0: "Momentary (only while held)", 1: "Latching (press on, press again off)"}

# padPlayMode on SOUND pads, EAR-PROVEN one value at a time: 0 press to play / press to stop /
# press to play from the start; 1 plays only while held, stops on release, restarts from the start
# next hold; 2 press to play, press again restarts from the beginning, CANNOT be stopped.
# NOTE THE ORDER. The RODE guide lists these as "Toggle / One Shot / Hold", so the obvious guess is
# 0/1/2 in that order - and it is WRONG: 1 is Hold and 2 is One Shot. Do not "tidy" it.
PLAY_MODE_LABEL = {
    0: "Toggle (press to play, press again to stop)",
    1: "Hold (plays only while you hold the pad)",
    2: "One Shot (press replays from the start, cannot be stopped)",
}
PLAY_MODE_TOGGLE = 0     # the only mode where a second press STOPS the sound
PLAY_MODE_HOLD = 1       # plays only while the pad is held: a tap gives a blip
PLAY_MODE_ONE_SHOT = 2   # the mode where Loop is unavailable (RODE guide)


def back_channel_sources() -> list[dict]:
    """The Back Channel members THIS desk can offer, in reading order: [{prop, label}].

    Device-aware. A channel is offered only if the active device actually has it, judged by the
    same tables the rest of the app labels channels from: ui_channels for the input strips, and
    the matrix keys for a CallMe slot (which the matrix carries as an OUTPUT, not a source, so
    both sides are checked). That keeps this list consistent with the Channels and Routing
    screens: the Pro II therefore offers CallMe 1 but not the inferred CallMe 2 / 3 slots,
    exactly as the routing matrix does."""
    dev = devices.get_active_device()
    have = {c["name"] for c in dev.ui_channels}
    for side in ("sources", "outputs"):
        have |= {e["key"] for e in getattr(dev.matrix, side, ()) or ()}
    return [{"prop": s["prop"], "label": s["label"]}
            for s in BACK_CHANNEL_SOURCES if s["channel"] in have]


# padColourIndex 0-11 -> the palette's real colour names, which the RODE user guide does not list.
# Method: the RODE app's Customize SMART Pads screen draws each pad's tile icon in that pad's colour
# at FULL saturation, so the swatches were sampled straight off the app UI (no Transfer Mode needed,
# nothing written). Measured hues, in index order: 0, 28.6, 43.1, 54.1, 83.1, 120.0, 165.3, 190.9,
# 223.5, 266.3, 280.8, 306.3 - an even sweep round the wheel from red to magenta.
# CROSS-CHECKED three ways: (1) pad bank 2 carries indices 0, 1, 2 and 6 again and sampled
# byte-identically to bank 1; (2) a webcam run that stepped ONE pad through all 12 indices and
# photographed the physical LED agreed on every index it could resolve (the white pad diffuser
# washes the pale ones out, which is why the camera alone was not enough); (3) that same run proves
# the index-to-pad alignment, so these names are pinned to the real values, not inferred order.
PAD_COLOUR_NAME = {
    0: "Red", 1: "Orange", 2: "Amber", 3: "Yellow", 4: "Lime", 5: "Green",
    6: "Teal", 7: "Cyan", 8: "Blue", 9: "Violet", 10: "Purple", 11: "Magenta",
}


def pad_colours() -> list[dict]:
    """The colour palette as [{"value": 0, "label": "Red"}, ...] for the UI to render as a list."""
    return [{"value": v, "label": PAD_COLOUR_NAME[v]} for v in sorted(PAD_COLOUR_NAME)]


PAD_PROP = {
    "padName": {
        "kind": "str", "label": "Pad name", "applies_to": None,
        "help": "The on-screen name of the pad.",
    },
    "padColourIndex": {
        "kind": "int", "label": "Pad colour", "applies_to": None, "min": 0, "max": 11,
        "help": "The pad's display colour (0-11; the app's fixed palette, see PAD_COLOUR_NAME).",
    },
    "padGain": {
        "kind": "float", "label": "Pad volume (dB)", "applies_to": None, "min": -60.0, "max": 12.0,
        "help": "Per-pad output level in dB. Factory pads sit at -12.0. (Range provisional.)",
    },
    "padReplay": {
        "kind": "bool", "label": "Restart on re-press", "applies_to": (1,),
        "help": "Sound pads: a second press restarts the sound; off, it pauses and resumes.",
    },
    "padLoop": {
        "kind": "bool", "label": "Loop", "applies_to": (1,),
        "help": "Sound pads: play repeatedly until stopped (not available in One-Shot mode).",
    },
    "padPlayMode": {
        "kind": "int", "label": "How the pad plays", "applies_to": (1,), "min": 0, "max": 2,
        "help": "Sound pads: 0 Toggle, 1 Hold, 2 One Shot (see PLAY_MODE_LABEL).",
    },
    "padMixerTriggerMode": {
        "kind": "int", "label": "Trigger mode", "applies_to": (3,), "min": 0, "max": 1,
        "help": "Mixer pads: 0 Momentary (only while held), 1 Latching (press on, press again off). "
                "Confirmed from the factory pads (Censor = Momentary, Ducking = Latching).",
    },
}

# MIDI TRIGGER MAPPING (padType 4). All eight padTrigger* fields, exposed under short names so the
# payload, the writer and the profile share ONE definition. The firmware's default, which is what a
# pad is stuck with unless these are written, is a Control Change on control = padIdx (bank 1 pad 1
# = CC 0, counting up), channel 1, value 127 on press and 0 on release.
# padTriggerMode IS latching versus momentary, PROVEN ON HARDWARE on the desk's own MIDI output
# (DevTools/midi_monitor.py):
#   mode 1 (the app's default): press sends the ON value, the NEXT press sends the OFF value.
#     LATCHING.
#   mode 0: the ON value at press and the OFF value at release, one message each. MOMENTARY.
# A mixer pad's padMixerTriggerMode agrees with this: 0 Momentary, 1 Latching.
# `send` is still read and backed up but NOT offered as a control: the template stamps it 2 and
# nothing has shown what it does, so a label would be a guess dressed as knowledge.
TRIGGER_MODE_LABEL = {0: "Momentary", 1: "Latching"}
# What a freshly created trigger pad is left on, so a restore only writes the mode when the
# profile actually differs from it (see restore_pads).
TRIGGER_CREATE_MODE = 1
TRIGGER_FIELD = {
    "custom": "padTriggerCustom",
    "type": "padTriggerType",
    "control": "padTriggerControl",
    "channel": "padTriggerChannel",
    "on": "padTriggerOn",
    "off": "padTriggerOff",
    "mode": "padTriggerMode",
    "send": "padTriggerSend",
}
TRIGGER_PROPS = list(TRIGGER_FIELD.values())
TRIGGER_TYPE_LABEL = pr.TRIGGER_TYPE_LABEL          # 0 Control Change, 1 Note

# ------------------------------------------------------------------------------------------------
# THE OTHER PER-PAD SETTINGS, read here so they can be backed up and, where the meaning is
# settled, edited. Same one-spec-drives-everything shape as the
# trigger fields above.
#
# ENVELOPE (sound pads). Four floats, and they are FOUR POSITIONS ALONG THE SOUND, 0.0 to 1.0.
# EAR-PROVEN on a 44-second sound, where a position and a length in seconds cannot be confused:
#   start 0.5              -> playback began about halfway in, NOT half a second in. So a POSITION.
#   start 0.5, fadeIn 0.25 -> NO fade at all, full level from halfway to the end. So fadeIn is an
#                             ABSOLUTE POSITION too (the fade had already "finished" before playback
#                             began), not a fade LENGTH measured from the start point.
#   fadeOut 0.1, stop 0.2  -> started at the beginning, faded down early and stopped. So fadeOut is
#                             where the fade-down BEGINS and stop is where playback ENDS.
# Hence: play from `start` to `stop`, fading up until `fade_in` and down from `fade_out`. That also
# explains the create default (0.0 / 0.0 / 1.0 / 1.0) exactly: play it all, with both fade points
# sitting on the ends so neither does anything.
# A fade point OUTSIDE the played region is accepted by the desk and simply does nothing, which is a
# silent no-op a blind operator cannot see, so the editor says so rather than leaving it quiet.
ENV_FIELD = {
    "start": "padEnvStart",
    "fade_in": "padEnvFadeIn",
    "fade_out": "padEnvFadeOut",
    "stop": "padEnvStop",
}
# The editor works in PERCENT THROUGH THE SOUND. The desk's own unit is a 0-1 fraction, but "start at
# 50 percent" is something a screen reader reads usefully and a slider arrows through sensibly, while
# seconds would need the file's duration, which the desk does not report.
ENV_EDIT = [
    {"key": "start", "label": "Start playing at",
     "help": "How far into the sound playback begins, as a percentage. 0 is the very beginning."},
    {"key": "fade_in", "label": "Fade in until",
     "help": "The sound fades up from silence until this point. Set it to the same as Start playing "
             "at for no fade in."},
    {"key": "fade_out", "label": "Fade out from",
     "help": "The sound fades down to silence from this point. Set it to the same as Stop playing at "
             "for no fade out."},
    {"key": "stop", "label": "Stop playing at",
     "help": "How far into the sound playback ends, as a percentage. 100 is the very end."},
]
# FADE mixer pads (padMixerMode 2). The names say seconds, so the unit is not in doubt. The upper
# limit is not proven, so the editor's 60 seconds is a SANITY bound (a minute-long fade is already
# absurd for a show), not a device limit: write validation clamps to it, and the profile restore
# writes the saved value through the same clamp.
FADE_FIELD = {
    "in_seconds": "padMixerFadeInSeconds",
    "out_seconds": "padMixerFadeOutSeconds",
    "exclude_host": "padMixerFadeExcludeHost",
}
FADE_MAX_SECONDS = 60.0
FADE_EDIT = [
    {"key": "in_seconds", "label": "Fade in over, in seconds",
     "help": "How long the mix takes to come up when the pad fades in."},
    {"key": "out_seconds", "label": "Fade out over, in seconds",
     "help": "How long the mix takes to go down when the pad fades out."},
]
# CENSOR mixer pads (padMixerMode 0): whether the pad lays your OWN sound over the mix instead of the
# factory beep, and which file. The path is read and backed up but never written by us: putting a
# custom censor sound ON the unit is a file upload, a separate job from restoring a setting.
CENSOR_FIELD = {
    "custom": "padMixerCensorCustom",
    "file": "padMixerCensorFilePath",
}
EXTRA_PAD_PROPS = (list(ENV_FIELD.values()) + list(FADE_FIELD.values())
                   + list(CENSOR_FIELD.values()))
# One flat {key: property} map for the restore path. The censor FILE PATH is left out on purpose:
# restoring a path whose audio may not be on the unit would point a pad at nothing, and putting the
# file there is an upload, not a setting. It is still saved, so nothing is lost.
_EXTRA_PROP_OF = {}
_EXTRA_PROP_OF.update(ENV_FIELD)
_EXTRA_PROP_OF.update(FADE_FIELD)
_EXTRA_PROP_OF["custom"] = CENSOR_FIELD["custom"]

# The six fields the editor offers, in the order they are read out. Each carries its own range, so
# the browser control, the write validation and the refusal message all come from here.
TRIGGER_EDIT = [
    {"key": "mode", "prop": "padTriggerMode", "kind": "int", "min": 0, "max": 1,
     "label": "How the pad triggers", "choices": "modes",
     "help": "Latching means the first press sends the pressed value and the next press sends the "
             "released value. Momentary means the pressed value goes as you press and the released "
             "value as you let go. Heard on the desk's own MIDI output, not assumed."},
    {"key": "type", "prop": "padTriggerType", "kind": "int", "min": 0, "max": 1,
     "label": "MIDI message type", "choices": "types",
     "help": "Control Change sends a CC. Note sends a note, and with Latching the note stays on "
             "until the next press."},
    {"key": "control", "prop": "padTriggerControl", "kind": "int", "min": 0, "max": 127,
     "label": "Control or note number",
     "help": "0 to 127. The default is the pad's own position, so bank 1 pad 1 is 0, counting up."},
    {"key": "channel", "prop": "padTriggerChannel", "kind": "int", "min": 1, "max": 16,
     "label": "MIDI channel",
     "help": "1 to 16. Most software listens on channel 1."},
    {"key": "on", "prop": "padTriggerOn", "kind": "int", "min": 0, "max": 127,
     "label": "Value when pressed",
     "help": "0 to 127. The default is 127, which most software reads as fully on."},
    {"key": "off", "prop": "padTriggerOff", "kind": "int", "min": 0, "max": 127,
     "label": "Value when released",
     "help": "0 to 127. The default is 0, which most software reads as off."},
]


def pad_extra_choices() -> dict:
    """The editable fields for a sound pad's trim/fade envelope and a Fade pad's times, so the page's
    labels, help text and limits come from the same spec as the write validation and the profile.
    Percent is the envelope's UI unit; the desk's own unit is the 0-1 fraction underneath it."""
    return {
        "envelope": [dict(f, min=0, max=100) for f in ENV_EDIT],
        "fade": [dict(f, min=0, max=FADE_MAX_SECONDS) for f in FADE_EDIT],
    }


def trigger_choices() -> dict:
    """What the pad editor may offer for a MIDI trigger, straight off the spec above.

    A field carrying `choices` is a dropdown over that named list; the rest are numbers within their
    own min and max. The editor therefore renders whatever the spec says, and a field added here
    appears without touching the page."""
    return {
        "fields": [{k: f.get(k) for k in ("key", "label", "help", "min", "max", "choices")}
                   for f in TRIGGER_EDIT],
        "types": [{"value": v, "label": TRIGGER_TYPE_LABEL[v]} for v in sorted(TRIGGER_TYPE_LABEL)],
        "modes": [{"value": v, "label": TRIGGER_MODE_LABEL[v]} for v in sorted(TRIGGER_MODE_LABEL)],
    }


# The Back Channel member flags are per-pad bools on mixer pads, so they edit in place exactly like
# padLoop. Registered here (rather than written out by hand) so the list and the editor cannot drift.
for _bc in BACK_CHANNEL_SOURCES:
    PAD_PROP[_bc["prop"]] = {
        "kind": "bool", "label": "Back Channel member: " + _bc["label"], "applies_to": (3,),
        "help": f"Back Channel pads: include {_bc['label']} in the off-air talkback room.",
    }
del _bc

# The MIDI trigger mapping, registered from the ONE spec above so the editor, the write validation
# and the profile cannot drift apart. applies_to (4,) is what stops a mapping being written onto a
# sound or mixer pad, where the field exists in the record but means nothing.
for _tf in TRIGGER_EDIT:
    PAD_PROP[_tf["prop"]] = {
        "kind": _tf["kind"], "label": _tf["label"], "applies_to": (4,),
        "min": _tf["min"], "max": _tf["max"],
        "help": "MIDI trigger pads: " + _tf["help"],
    }
del _tf
# The envelope, fade and censor settings. The envelope's 0.0-1.0 range is EAR-PROVEN (see ENV_FIELD),
# so it is clamped; the fade seconds are clamped to a sanity bound rather than a device limit.
for _e in ENV_EDIT:
    PAD_PROP[ENV_FIELD[_e["key"]]] = {
        "kind": "float", "label": _e["label"], "applies_to": (1,), "min": 0.0, "max": 1.0,
        "help": "Sound pads: " + _e["help"] + " Stored as a fraction of the whole sound.",
    }
del _e
for _f in FADE_EDIT:
    PAD_PROP[FADE_FIELD[_f["key"]]] = {
        "kind": "float", "label": _f["label"], "applies_to": (3,),
        "min": 0.0, "max": FADE_MAX_SECONDS,
        "help": "Fade mixer pads: " + _f["help"],
    }
del _f
PAD_PROP[FADE_FIELD["exclude_host"]] = {
    "kind": "bool", "label": "Fade excludes the host", "applies_to": (3,),
    "help": "Fade mixer pads: leave the host's own mic out of the fade, so you can still be heard "
            "while everything else fades.",
}
PAD_PROP[CENSOR_FIELD["custom"]] = {
    "kind": "bool", "label": "Censor uses your own sound", "applies_to": (3,),
    "help": "Censor pads: lay your own sound over the mix instead of the factory beep. The sound "
            "file itself has to already be on the unit.",
}

PAD_PROP["padTriggerCustom"] = {
    "kind": "bool", "label": "Custom MIDI mapping", "applies_to": (4,),
    "help": "MIDI trigger pads: send the mapping set here rather than the firmware's default "
            "(a Control Change on the pad's own number, channel 1, 127 on and 0 off).",
}


def _coerce_pad_value(name: str, value, kind: str, spec):
    """Validate/normalise one pad-property value. Returns (value, None) or (None, error message)."""
    try:
        if kind == "int":
            value = int(value)
        elif kind == "float":
            value = float(value)
        elif kind == "bool":
            # accept real bools AND CLI strings (bool('False') would be True otherwise)
            value = (value.strip().lower() in ("1", "true", "yes", "on")
                     if isinstance(value, str) else bool(value))
        elif kind == "str":
            value = str(value).replace("\x00", "")[:40]
        else:
            return None, f"Unsupported kind {kind!r}."
    except (ValueError, TypeError):
        return None, f"Invalid value {value!r} for {name} ({kind})."
    if spec and kind in ("int", "float"):
        if "min" in spec and value < spec["min"]:
            value = spec["min"]
        if "max" in spec and value > spec["max"]:
            value = spec["max"]
    return value, None


def set_pad_properties(grid: int, updates, kinds: dict | None = None, blob=None) -> dict:
    """Generic IN-PLACE editor for pad-level properties on an EXISTING pad (no clear/create, no
    region-B touch): resolve the pad's 0-based stream position, write the records in ONE transfer-
    mode session, exit. Nothing is allocated or shifted, so it is pool-safe and silent (no FAT
    mount). Verify by read-back.

    `updates` is a list of (name, value) pairs (or a dict) applied in order. Writing them together
    matters: transfer mode is the fragile, drive-F-mounting part, so two settings that always change
    together (play mode + loop) cost one short session instead of two.

    `kinds` overrides the kind per property; otherwise it comes from the PAD_PROP registry.
    Registry properties are clamped to their min/max and gated by applies_to. An unregistered
    property needs an explicit kind (an escape hatch for properties whose semantics we are still
    confirming - use with care).

    `blob` reuses a model dump the caller has ALREADY taken (position lookup) instead of paying for
    another. Only pass one that is fresh and taken with no write since, or the stream position could
    be stale - which is a corruption hazard, not a display glitch.
    """
    if not pad_writes_enabled():
        return {"ok": False, "message": _pad_ro_message()}
    items = list(updates.items()) if isinstance(updates, dict) else list(updates)
    if not items:
        return {"ok": False, "message": "Nothing to change on the pad."}
    kinds = kinds or {}
    pos, ptype, padname = _position_of(grid - 1, blob)
    if pos is None:
        return {"ok": False, "message": f"Pad {grid} is empty; nothing to edit."}

    recs, labels, values = [], [], {}
    for name, value in items:
        spec = PAD_PROP.get(name)
        kind = kinds.get(name) or (spec["kind"] if spec else None)
        if kind is None:
            return {"ok": False,
                    "message": f"Unknown pad property {name!r}. Pass kind= to write it anyway."}
        if spec and spec.get("applies_to") and ptype not in spec["applies_to"]:
            return {"ok": False, "message": f"{spec['label']} does not apply to this pad type."}
        value, err = _coerce_pad_value(name, value, kind, spec)
        if err:
            return {"ok": False, "message": err}
        recs.append(pr.write_record(name, kind, value, pos, pr.FRAME_PAD))
        labels.append(f"{spec['label'] if spec else name} to {value!r}")
        values[name] = value
    _commit_pad_records(recs)
    first = items[0][0]
    return {"ok": True, "name": first, "value": values[first], "values": values,
            "message": f"Set {', '.join(labels)} on pad {grid} '{padname}'."}


def set_pad_property(grid: int, name: str, value, kind: str | None = None, blob=None) -> dict:
    """Set ONE pad-level property in place (see set_pad_properties, which does the work)."""
    return set_pad_properties(grid, [(name, value)],
                              kinds={name: kind} if kind else None, blob=blob)


# Thin, named convenience wrappers over set_pad_property (ergonomics + CLI; the UI can call either).
def set_pad_gain(grid: int, db: float) -> dict:
    return set_pad_property(grid, "padGain", db)


def set_pad_name(grid: int, name: str) -> dict:
    return set_pad_property(grid, "padName", name)


def set_pad_colour(grid: int, colour: int) -> dict:
    return set_pad_property(grid, "padColourIndex", colour)


def set_pad_loop(grid: int, on: bool) -> dict:
    return set_pad_property(grid, "padLoop", on)


def set_play_mode(grid: int, mode: int) -> dict:
    """Sound pads: Toggle (0), Hold (1) or One Shot (2). See PLAY_MODE_LABEL - the order is NOT the
    one the RODE guide prints. Loop is meaningless in One Shot, so switching to One Shot also turns
    padLoop off rather than leaving a setting on that the pad cannot honour."""
    # Both records go in ONE transfer-mode session: half the dwell, and the pad is never left
    # momentarily in One Shot WITH loop still set.
    try:
        one_shot = int(mode) == PLAY_MODE_ONE_SHOT
    except (TypeError, ValueError):
        return {"ok": False, "message": f"Invalid play mode {mode!r}."}
    updates = [("padPlayMode", mode)] + ([("padLoop", False)] if one_shot else [])
    res = set_pad_properties(grid, updates)
    if res.get("ok") and one_shot:
        res["message"] += " Loop turned off, One Shot cannot loop."
        res["loop_off"] = True
    return res


def set_mixer_trigger(grid: int, mode: int) -> dict:
    """Mixer pads: Momentary (0) or Latching (1). See MIXER_TRIGGER_LABEL."""
    return set_pad_property(grid, "padMixerTriggerMode", mode)


def set_trigger_mapping(grid: int, **fields) -> dict:
    """MIDI trigger pads: change WHAT the pad sends (see TRIGGER_EDIT for the fields and ranges).

    Pass any of type / control / channel / on / off by name, plus optional custom=. Everything goes
    in ONE transfer-mode session, so the pad is never left momentarily half-mapped, and
    `padTriggerCustom` is set TRUE alongside unless the caller says otherwise: the firmware only
    honours a mapping when that flag is on, so writing the numbers without it would report success
    and change nothing audible.
    MEASURED: passing custom=False is DESTRUCTIVE. Writing that one flag makes the desk itself
    reset padTriggerControl / Channel / On / Off back to the firmware defaults (60 -> 16, channel
    2 -> 1, on 100 -> 127), and switching the flag straight back on does NOT bring the custom
    values back. So "off" discards the mapping rather than parking it, and any UI offering that
    switch has to say so. padTriggerType does NOT reset (it stays on Note), which
    means a pad with custom off can hold a type its default mapping would not use; what such a pad
    actually sends is UNPROVEN, so describe the default from the pad's position rather than from that
    field."""
    by_key = {f["key"]: f["prop"] for f in TRIGGER_EDIT}
    updates = []
    for key, prop in by_key.items():
        if fields.get(key) is not None:
            updates.append((prop, fields[key]))
    custom = fields.get("custom")
    if custom is None:
        custom = True if updates else None
    if custom is not None:
        # Custom LAST when switching on (so the mapping is complete before it takes effect) and
        # FIRST when switching off (so nothing is sent under the old flag while values move).
        rec = ("padTriggerCustom", bool(custom))
        updates = updates + [rec] if custom else [rec] + updates
    if not updates:
        return {"ok": False, "message": "Nothing to change in the MIDI trigger mapping."}
    return set_pad_properties(grid, updates)


def set_envelope_percent(grid: int, field: str, percent: float) -> dict:
    """Sound pads: move one point of the trim/fade envelope, given as a PERCENTAGE through the sound.

    The desk stores a 0-1 fraction (ear-proven, see ENV_FIELD); percent is the editor's unit because
    it reads and arrows sensibly and needs no file duration. A point outside the played region is
    accepted by the desk and does nothing, so the caller is TOLD when that is the result rather than
    left with a control that silently achieves nothing."""
    prop = ENV_FIELD.get(field)
    if not prop:
        return {"ok": False, "message": f"Unknown envelope point {field!r}. Expected one of: "
                                       + ", ".join(ENV_FIELD) + "."}
    try:
        percent = float(percent)
    except (TypeError, ValueError):
        return {"ok": False, "message": f"Invalid value {percent!r} for {field}."}
    percent = max(0.0, min(100.0, percent))
    blob = rr.read_model_stable(fresh=True)
    res = set_pad_property(grid, prop, percent / 100.0, blob=blob)
    if not res.get("ok"):
        return res
    # Re-read the four points off the SAVED blob plus the value we just wrote, so the warning below
    # describes the envelope as it now is without paying for a second dump.
    env = {k: _field_by_padidx(blob, p).get(grid - 1) for k, p in ENV_FIELD.items()}
    env[field] = percent / 100.0
    note = envelope_warning(env)
    if note:
        res["message"] = res.get("message", "") + " " + note
        res["warning"] = note
    return res


def envelope_warning(env: dict) -> str:
    """The one thing about this envelope that a listener would notice and a reader could not: a fade
    point that sits outside the part of the sound being played, so it has no effect at all."""
    def val(key, default):
        v = env.get(key)
        return default if v is None else float(v)
    start, stop = val("start", 0.0), val("stop", 1.0)
    fin, fout = val("fade_in", 0.0), val("fade_out", 1.0)
    bad = []
    if start >= stop:
        return ("Nothing will play: the start point is at or after the stop point.")
    if fin <= start:
        bad.append("no fade in (its point is at or before the start)")
    if fout >= stop:
        bad.append("no fade out (its point is at or after the stop)")
    if fin > stop or fout < start:
        bad.append("a fade point outside the part being played, which does nothing")
    return ("This pad now has " + " and ".join(bad) + ".") if bad else ""


def set_fade_seconds(grid: int, field: str, seconds: float) -> dict:
    """Fade mixer pads: how long the fade in or the fade out takes."""
    prop = FADE_FIELD.get(field)
    if field not in ("in_seconds", "out_seconds") or not prop:
        return {"ok": False, "message": f"Unknown fade time {field!r}. Expected in_seconds or "
                                        "out_seconds."}
    return set_pad_property(grid, prop, seconds)


# The two per-pad switches that are not on the main editor rows: a Fade pad leaving the host out of
# the fade, and a Censor pad using your own sound instead of the beep. Named rather than passed as a
# raw property so the browser cannot ask for an arbitrary write.
PAD_FLAG = {
    "fade_exclude_host": FADE_FIELD["exclude_host"],
    "censor_custom": CENSOR_FIELD["custom"],
}


def set_pad_flag(grid: int, name: str, on: bool) -> dict:
    prop = PAD_FLAG.get(name)
    if not prop:
        return {"ok": False, "message": f"Unknown pad switch {name!r}. Expected one of: "
                                       + ", ".join(PAD_FLAG) + "."}
    res = set_pad_property(grid, prop, bool(on))
    # A Censor pad set to use your own sound with no file on the unit would beep nothing at all, and
    # we do not upload censor audio, so say it plainly instead of reporting a bare success.
    if res.get("ok") and name == "censor_custom" and on:
        path = _field_by_padidx(rr.read_model_stable(fresh=True),
                               CENSOR_FIELD["file"]).get(grid - 1)
        if not path:
            res["message"] = (res.get("message", "") + " NOTE: this pad has no custom sound on the "
                              "unit yet, so it has nothing to play until one is set on the unit "
                              "itself. Switch this back off to use the built-in beep.")
    return res


def set_back_channel_member(grid: int, prop: str, on: bool) -> dict:
    """Add/remove one channel from a Back Channel pad's off-air room (see BACK_CHANNEL_SOURCES).

    `prop` is validated against what THIS desk offers, so a caller cannot write a member flag for
    a channel the unit does not have (harmless on the wire, but it would read back as a silent
    no-op setting, which is worse than an honest refusal). The pad must also actually BE a Back
    Channel pad: PAD_PROP's applies_to only knows padType, so it would happily accept a member
    flag on a Censor or Ducking pad, where the field exists in the record but means nothing."""
    offered = {s["prop"]: s["label"] for s in back_channel_sources()}
    if prop not in offered:
        return {"ok": False, "message": f"{prop} is not a Back Channel member on this device."}
    # One dump serves both the mixer-mode check and the stream-position lookup inside the write
    # (it is fresh, and nothing is written in between), so ticking a member costs one read, not two.
    blob = rr.read_model_stable(fresh=True)
    mode = _field_by_padidx(blob, "padMixerMode").get(grid - 1)
    if mode != MIXER_MODE["backchannel"]:
        what = MIXER_MODE_LABEL.get(mode, "not a mixer action")
        return {"ok": False,
                "message": f"Pad {grid} is {what}, not a Back Channel pad, so it has no members."}
    return set_pad_property(grid, prop, on, blob=blob)


# ----------------------------------------------------------------------------------------------
# CREATE SOUND  (the held-open one-session create + the website's clear+create entry)
# ----------------------------------------------------------------------------------------------

def safe_dest_filename(src_path: str, dest_name: str | None = None) -> str:
    """The on-device filename to store this sound under, matching the FACTORY convention
    (the factory pads are pads/<n>/Music_Bed.wav, Applause.wav, ... - the real descriptive
    name, NOT a generic 'sound.wav'; confirmed by clean-table reads). We mirror that.

    `dest_name` (e.g. the original upload filename) wins; else we use the source basename.
    The stem is sanitised to a FAT-safe set (letters/digits/space/underscore/hyphen/parens),
    whitespace collapsed, capped at 40 chars, defaulting to 'Sound'. The extension is forced
    to .wav or .mp3 (the only formats the device plays); an MP3 MUST keep .mp3 or it plays
    silent (the firmware trusts the extension - proven)."""
    base = os.path.basename(dest_name or src_path or "")
    stem, ext = os.path.splitext(base)
    ext = ext.lower()
    if ext not in (".wav", ".mp3"):
        ext = ".mp3" if (src_path or "").lower().endswith(".mp3") else ".wav"
    clean = "".join(c if (c.isalnum() or c in " _-()") else " " for c in stem)
    clean = " ".join(clean.split()).strip()[:40]
    return (clean or "Sound") + ext


def assign_create(target_idx: int, folder: int, src_wav: str, name="Sound", colour=5,
                  replay=True, debug=False, dest_filename: str | None = None):
    """Low-level CREATE of a SOUND pad on an EMPTY slot, faithful to the app (padassign9.pcap).

    Sequence (one held-open HID session):
      pre  = handoff PAD + phase1(padType=0..padIdx) + handoff EFFECTS + effects defaults
      -- device sync (padRCVSyncPadType, async) --
      mid  = phase2-A (padType=1, padEnvStop, padEnvFadeOut, padName, padColourIndex)
      -- file written to pads/<folder>/<real name> on the FAT volume here, handle still bound --
      post = remountPadStorage, padFilePath, padName ; then exit transfer mode
    Records are BUILT FROM SCRATCH by pad_records (no capture replay); handle = free slot past the
    pad records; fx sub-handle = REUSE the pad's effect-object or append.

    The on-device filename uses the REAL name (factory convention pads/<n>/Name.ext), derived
    from `dest_filename` if given else the source basename - see safe_dest_filename.
    """
    handle = pr.free_handle(current_pad_count())   # FREE slot past the pad records
    fx_sub = free_effect_subhandle(target_idx)     # REUSE pad's effect-object (or append)
    on_device = safe_dest_filename(src_wav, dest_filename)
    pre, mid, post = pr.build_sound_create(target_idx, folder, name, colour, handle=handle,
                                           fx_subhandle=fx_sub, replay=replay,
                                           filename=on_device)
    print(f"  target padIdx={target_idx} (grid pad {target_idx + 1})  folder={folder}  "
          f"handle=0x{handle:02x}  fx_sub=0x{fx_sub:02x} (region-B reuse/append)  name={name!r}")
    print(f"  records: {len(pre)} pre (handoff+phase1, NO region-B for a sound pad) -> sync -> "
          f"{len(mid)} phase2A -> FILE -> {len(post)} phase2B")

    def file_copy():
        dst_dir = os.path.join(root, "pads", str(folder)); os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, on_device)
        last = None
        for attempt in range(6):               # EINVAL can persist for a beat after a fresh mount
            try:
                shutil.copyfile(src_wav, dst)
                last = None
                break
            except OSError as exc:
                last = exc
                print(f"    copy attempt {attempt + 1} failed ({exc}); retrying")
                time.sleep(1.0)
        if last is not None:
            raise last
        t.flush_volume(root)
        print(f"  wrote {os.path.basename(src_wav)} -> pads/{folder}/{on_device} (mid-sequence)")

    with _transfer_mode(1.0):
        t.remount_pad_storage()
        root = t.find_pad_volume()
        if root is None:
            raise RuntimeError("volume did not mount")
        n, saw = send_create_full(pre, mid, file_copy, post, debug=debug)
        print(f"  sent {n} bytes; saw device sync = {saw}")
        time.sleep(0.4)
        # Remove the audio this pad USED to play, before the session closes. Free, because the
        # volume is already mounted and we know exactly which file to keep: the one just written.
        # `post` has already written padFilePath, so the pad is pointing at the keeper by now.
        stale = tidy_pad_folder(root, folder, on_device)
        if stale:
            print(f"  removed the previous audio for pad {folder}: {', '.join(stale)}")
            t.flush_volume(root)
    # Report the REAL on-device filename (on_device), NOT os.path.basename(src_wav): the server
    # uploads via a tempfile (accesscaster_*.wav), so the source basename is the temp name.
    return f"created grid pad {target_idx + 1} -> {on_device}"


def create_sound(grid: int, src_wav: str, name="Sound", colour=5, folder=None,
                 replay=True, debug=False, dest_filename=None) -> dict:
    """High-level: put a sound on grid pad `grid` (1-based) via the PROVEN clear+create path
    (NEVER overwrite-in-place). This is what the website calls.

    NOT gated on the clean pool: a sound create+clear writes NOTHING to region B
    (SOUNDPADS section 2), so it cannot collide with an effect-object orphan.

    If the grid pad is already occupied it is cleared first (app-style), then a fresh sound pad is
    created at padIdx = grid-1, writing pads/<folder>/<real name> (factory convention - see
    safe_dest_filename). `folder` defaults to the grid number, matching the app's FAT layout
    (grid pad N -> pads/N/). `dest_filename` (e.g. the original upload name) sets the on-device
    filename; without it the source basename is used. `replay` True = re-press restarts from the
    start (factory behaviour); False = pause/resume.
    """
    return _replace_slot(grid, "sound", lambda: assign_create(
        grid - 1, grid if folder is None else folder, src_wav, name=name, colour=colour,
        replay=replay, debug=debug, dest_filename=dest_filename))


def _replace_slot(grid: int, what: str, create, note: str = "") -> dict:
    """Clear grid pad `grid` if it is occupied, then run `create` (a low-level assign_create_*),
    and report pool health. `created` is True only when the create itself ran to the end; `ok`
    additionally needs a clean pool, so a caller asking "is the pad there?" reads `created`.
    `note` is said after a successful create."""
    pos, ptype, oldname = _position_of(grid - 1)
    cleared = None
    if pos is not None:
        clear_slot(pos, ptype)
        cleared = f"{PAD_TYPE_LABEL.get(ptype, 'pad')} '{oldname}'"
        time.sleep(1.2)                            # let the record drop before the create reads count
    try:
        result = create()
    except Exception as exc:
        return {"ok": False, "created": False, "message": f"Could not create the {what}: {exc}",
                "health": pool_health()}
    health = pool_health()
    lead = f"Replaced {cleared} on pad {grid}. " if cleared else f"Created {what} on pad {grid}. "
    msg = lead + result + "." + note + " " + _health_message(health)
    return {"ok": health["clean"], "created": True, "message": msg, "health": health}


def _send_create(pre, mid, post, debug=False) -> None:
    """One create session for a pad with no file to copy (voice FX, mixer action, MIDI trigger)."""
    with _transfer_mode(1.2):
        n, saw = send_create_full(pre, mid, lambda: None, post, debug=debug)
        print(f"  sent {n} bytes; saw device sync = {saw}")
        time.sleep(0.4)


# ----------------------------------------------------------------------------------------------
# CREATE VOICE-FX
# ----------------------------------------------------------------------------------------------

def assign_create_fx_recipe(target_idx: int, name: str, recipe: dict, colour=8, debug=False,
                            active: bool = True):
    """Low-level multi-effect VOICE-FX create from a factory/custom recipe ({engine:{param:value}},
    see pad_records.build_fx_create_recipe / fx_recipes.json). Reproduces the exact factory presets.
    `active` True engages it live at once (live preview); False leaves it at rest like the factory."""
    handle = pr.free_handle(current_pad_count())
    fx_sub = free_effect_subhandle(target_idx)
    pre, mid, post = pr.build_fx_create_recipe(target_idx, name, colour, recipe,
                                               handle=handle, fx_subhandle=fx_sub, active=active)
    stack = " + ".join(recipe.keys())
    print(f"  target padIdx={target_idx} (grid pad {target_idx + 1})  name={name!r}  "
          f"recipe=[{stack}]  handle=0x{handle:02x}  fx_sub=0x{fx_sub:02x} (region-B reuse/append)")
    print(f"  {len(pre)} pre -> sync -> {len(mid)} commit -> {len(post)} effect-on")
    _send_create(pre, mid, post, debug=debug)
    return f"created FX (recipe [{stack}]) on grid pad {target_idx + 1}"


# ----------------------------------------------------------------------------------------------
# POOL SELF-HEAL + HARDENED CREATE
#
# An orphan is an effect object with no live FX pad. There are three kinds, and each has exactly one
# removal that works (device-proven on 1.7.6):
#   * ATTACHED (effectsIdx stored last, like a live pad's object). Clearing an FX pad makes the
#     firmware collect the object bound to it, and a BARE FX create (build_blank with_effects=False,
#     zero 0x9d writes) binds a pad to an existing object without appending one. So: bare-bind a
#     pad, clear it, and the object goes. A delete by tree path does NOT remove this kind.
#   * DETACHED (effectsIdx stored first, see effect_objects). Nothing binds to it, so the bare-bind
#     route never collects it. A delete by tree path removes exactly that node. RODE's app leaves
#     one when an FX pad made in RODE's app is retyped to MIDI.
#   * PARTIAL (no effectsIdx, 13 properties). Once ANY object has left the middle of the list, by
#     a clear, a retype in RODE's app or a path delete, the next FX pad's clear leaves this shell
#     instead of collecting its object. The delete by tree path removes it too.
# Only the LAST object is ever deleted by path: a delete from the middle is itself a middle removal,
# so the next clear would leave a partial. A partial in the middle does not stop the FX pad after it
# working, and becomes last (and deletable) once the pads after it are cleared.
#
# The bare-bind pad is used only transiently (cleared at once). Every pad we leave behind is made
# by the normal create (assign_create_fx_recipe), which is the proven live path.
# ----------------------------------------------------------------------------------------------

def _delete_effect_object(index: int) -> None:
    """Delete PADEFFECTS child `index` by tree path: the pad clear's shape (a bool ON, OFF, then
    select-begin) aimed at the effects node, inside transfer mode. Only for a DETACHED or PARTIAL
    object at the END of the list; the caller checks both."""
    _require_pad_write()
    iface = t.find_control_interface()
    if iface is None:
        raise RuntimeError("no control interface (close the RODE app)")
    with _transfer_mode(1.0):
        dev = rcp_hid.open_handle(iface, "effect object delete")
        try:
            for r in (build_pad_bool("voiceDisguiseOn", True, index, pr.FRAME_FX),
                      build_pad_bool("voiceDisguiseOn", False, index, pr.FRAME_FX),
                      build_select_begin(index, pr.FRAME_FX)):
                rcp_hid.write_report(dev, r)
                time.sleep(0.08)
            time.sleep(0.3)
        finally:
            rcp_hid.close_handle(dev, "effect object delete")
        time.sleep(0.2)


def _bind_existing_fx(target_idx: int, name: str = "ZZheal") -> None:
    """Create an FX pad bound to an EXISTING region-B object, writing NO new object
    (build_blank with_effects=False). Transient helper for orphan GC only - do not leave
    this pad in place as a user pad (its live binding is not reboot-proven)."""
    _require_pad_write()   # backstop: orphan-GC bind is a device write too
    handle = pr.free_handle(current_pad_count())
    fx_sub = free_effect_subhandle(target_idx)     # reuse index of the existing object
    pre = pr.build_blank(target_idx, handle, fx_sub, with_effects=False)
    mid = [
        pr.write_record("padType", "int", 2, handle, pr.FRAME_PAD),
        pr.write_record("padActive", "bool", True, handle, pr.FRAME_PAD),
        pr.write_record("padEffectTriggerMode", "int", 1, handle, pr.FRAME_PAD),
        pr.write_record("padName", "str", name, handle, pr.FRAME_PAD),
        pr.write_record("padColourIndex", "int", 8, handle, pr.FRAME_PAD),
    ]
    _send_create(pre, mid, [])


def _fx_pad_idxs() -> set:
    return {pi for _p, pi, pt, _n in pad_positions() if pt == PAD_TYPE_VOICE_FX and pi is not None}


def remove_orphan_objects(target_idx: int, max_iter: int = 8) -> int:
    """Remove every effect object for `target_idx` while no live FX pad owns it, last first: an
    attached one by bare bind and clear, a detached one by path delete if it is the last object.
    Returns the number removed. Stops, rather than retrying, on a detached object in the middle of
    the list or on any pass that removes nothing; pool_health then reports what is left."""
    removed = 0
    for _ in range(max_iter):
        if target_idx in _fx_pad_idxs():
            break
        objects = effect_objects()
        mine = [o for o in objects if o.effects_idx == target_idx]
        if not mine:
            break
        last = mine[-1]
        if last.detached:
            if last.index != len(objects) - 1:
                break
            _delete_effect_object(last.index)
        else:
            _bind_existing_fx(target_idx)
            time.sleep(0.6)
            # Found by padIdx whatever its type: a bind that does not take can leave a blank
            # (padType 0) record, and that must be cleared too.
            pos = next((p for p, pi, _t, _n in pad_positions() if pi == target_idx), None)
            if pos is not None:
                clear_pad(pos)
        time.sleep(0.6)
        if sum(o.effects_idx == target_idx for o in effect_objects()) >= len(mine):
            break
        removed += 1
    return removed


def remove_partial_objects(max_iter: int = 8) -> int:
    """Delete partial objects (no effectsIdx) from the END of the list by tree path, one at a time.
    Returns the number removed. A partial one in the middle is left for pool_health to report, for
    the same reason a middle detached one is."""
    removed = 0
    for _ in range(max_iter):
        objects = effect_objects()
        if not objects or not objects[-1].partial:
            break
        _delete_effect_object(objects[-1].index)
        time.sleep(0.6)
        if len(effect_objects()) >= len(objects):
            break
        removed += 1
    return removed


def clean_pool() -> dict:
    """Recovery net: remove every orphan, detached and partial effect object that has no live FX
    pad (remove_orphan_objects, remove_partial_objects). Touches no live pad and leaves no pad
    behind. Live-pad DUPLICATES (a pad plus an extra copy of its object) are NOT repaired here
    because rebuilding a live pad's effect must be ear-checked; they are reported for a manual fix,
    as is a detached or partial object that is not last in the list. Returns the post-heal
    pool_health plus what it did."""
    h = pool_health()
    removed = 0
    for idx in sorted(set(h["orphans"]) | set(h["detached"])):
        removed += remove_orphan_objects(idx)
    if h["partial"]:
        removed += remove_partial_objects()
    h2 = pool_health()
    unhealed = sorted(set(h2["duplicates"]))
    return {
        "ok": h2["clean"],
        "removed_orphans": removed,
        "health": h2,
        "unhealed_live_duplicates": unhealed,
        "message": ("Pool is clean." if h2["clean"]
                    else f"Removed {count(removed, 'orphan object')}; left: duplicates {unhealed}, "
                         f"orphans {h2['orphans']}, detached {h2['detached']}, "
                         f"partial {h2['partial']}."),
    }


def safe_assign_fx_recipe(target_idx: int, name: str, recipe: dict, colour: int = 8,
                          active: bool = True) -> dict:
    """HARDENED multi-effect (recipe) FX create. Scrubs the slot (clears any pad there and GCs its
    effect object) so a create never lands on top of a live one, then runs the
    normal recipe create (proven live). `active` True engages it live (live preview); False leaves
    the pad at rest like the factory so it does not leave a mic processing.

    REFUSES, before any write, a slot that has a detached effect object stuck in the middle of the
    list: a new FX pad there may bind the leftover instead of its own effect, and nothing here can
    remove it safely."""
    objects = effect_objects()
    if any(o.detached and o.effects_idx == target_idx and o.index != len(objects) - 1
           for o in objects):
        raise ValueError(_health_message({"clean": False, "detached": [target_idx]}))
    pos, ptype, _name = _position_of(target_idx)
    if pos is not None:
        clear_slot(pos, ptype); time.sleep(0.8)
    remove_orphan_objects(target_idx)
    assign_create_fx_recipe(target_idx, name, recipe, colour=colour, active=active)
    time.sleep(0.6)
    h = pool_health()
    dup = [e for e in h["effects_idx"] if e == target_idx]
    return {"ok": h["clean"] and len(dup) == 1, "health": h, "objects_for_pad": len(dup)}


def assign_fx_to_grid(grid: int, effect_name: str, active: bool = False) -> dict:
    """High-level: put a factory voice effect on grid pad `grid` (1-based). SELF-HEALING: instead
    of refusing on a polluted pool, safe_assign_fx_recipe scrubs the target slot (clears any pad,
    GCs any leftover orphan object for this padIdx) and then does the proven create, so it cannot
    leave a duplicate. Reports health after. `active` defaults False so assigning an effect leaves
    the pad at REST (matching the factory) and never leaves a mic processing; the live preview path
    passes active=True itself."""
    pidx = grid - 1
    key = _resolve_recipe(effect_name)
    spec = all_recipes()[key]
    recipe, colour = spec["stack"], spec["colour"]
    res = safe_assign_fx_recipe(pidx, key, recipe, colour=colour, active=active)
    health = res["health"]
    msg = f"Assigned voice effect '{key}' to pad {grid}. " + _health_message(health)
    return {"ok": health["clean"], "message": msg, "health": health, "effect": key}


# ----------------------------------------------------------------------------------------------
# CREATE MIXER ACTION  (padType=3; no file, no region B -> pool-safe)
# ----------------------------------------------------------------------------------------------

MIXER_MODE = pr.MIXER_MODE
MIXER_MODE_LABEL = pr.MIXER_MODE_LABEL
# Per-mode defaults. colour is cosmetic (the pad LED); trigger_mode is BEHAVIOURAL and matches the
# factory grid where known: Censor is momentary (0, hold to bleep) and Ducking latches (1).
_MIXER_DEFAULTS = {
    0: {"colour": 11, "trigger_mode": 0},   # Censor
    1: {"colour": 9,  "trigger_mode": 0},   # Trash Talk
    2: {"colour": 5,  "trigger_mode": 0},   # Fade In/Out
    3: {"colour": 6,  "trigger_mode": 0},   # Back Channel
    4: {"colour": 3,  "trigger_mode": 1},   # Ducking
}


def _resolve_mixer_mode(mode) -> int:
    """Accept an int 0-4 or a name (censor/trashtalk/fade/backchannel/ducking, any case/spacing)."""
    if isinstance(mode, int):
        if mode not in MIXER_MODE_LABEL:
            raise ValueError(f"unknown mixer mode {mode}; valid 0-4 ({MIXER_MODE_LABEL})")
        return mode
    key = str(mode).strip().lower().replace(" ", "").replace("_", "")
    if key not in MIXER_MODE:
        raise ValueError(f"unknown mixer mode {mode!r}; known: {list(MIXER_MODE)}")
    return MIXER_MODE[key]


def assign_create_mixer(target_idx: int, mode, name="Mixer", colour=None,
                        trigger_mode=None, debug=False):
    """Low-level MIXER-action create (padType=3). No file, no region-B block (mirrors the FX
    create's single held-open session with a no-op file copy)."""
    mode = _resolve_mixer_mode(mode)
    d = _MIXER_DEFAULTS.get(mode, {})
    if colour is None:
        colour = d.get("colour", 8)
    if trigger_mode is None:
        trigger_mode = d.get("trigger_mode", 0)
    handle = pr.free_handle(current_pad_count())
    pre, mid, post = pr.build_mixer_create(target_idx, name, colour, mode,
                                           trigger_mode=trigger_mode, handle=handle)
    print(f"  target padIdx={target_idx} (grid pad {target_idx + 1})  name={name!r}  "
          f"mode={mode} ({MIXER_MODE_LABEL[mode]})  trigger={trigger_mode}  handle=0x{handle:02x}  "
          f"[no file, no region B]")
    print(f"  {len(pre)} pre -> sync -> {len(mid)} commit")
    _send_create(pre, mid, post, debug=debug)
    return f"created mixer action ({MIXER_MODE_LABEL[mode]}) on grid pad {target_idx + 1}"


def create_mixer(grid: int, mode, name=None, colour=None, trigger_mode=None, debug=False) -> dict:
    """High-level: put a MIXER-action pad on grid pad `grid` (1-based) via clear+create.

    NOT gated on the clean pool: a mixer pad writes NOTHING to region B (SOUNDPADS 1c/2), so it
    cannot collide with an effect-object orphan. Clears the slot first if occupied. Reports pool
    health after - it MUST stay unchanged (region-B count identical) since no 0x9d records are sent.
    `mode` = 0-4 or a name (censor/trashtalk/fade/backchannel/ducking).
    """
    mode_i = _resolve_mixer_mode(mode)
    if name is None:
        name = MIXER_MODE_LABEL[mode_i]
    # A fresh Back Channel pad has NO members (every padMixerBackChannel* defaults False), so it
    # links nothing and pressing it does nothing at all. Say so, or it reads as a broken pad.
    todo = (" Now choose who is in the Back Channel under Pad settings; until you tick someone,"
            " the pad has nobody to talk to and pressing it does nothing."
            if mode_i == MIXER_MODE["backchannel"] else "")
    res = _replace_slot(grid, f"{MIXER_MODE_LABEL[mode_i]} action", lambda: assign_create_mixer(
        grid - 1, mode_i, name=name, colour=colour, trigger_mode=trigger_mode, debug=debug),
        note=todo)
    res["mode"] = mode_i
    return res


# ----------------------------------------------------------------------------------------------
# CREATE MIDI TRIGGER  (padType=4; no file, no region B -> pool-safe)
# ----------------------------------------------------------------------------------------------

TRIGGER_TYPE = pr.TRIGGER_TYPE


def assign_create_trigger(target_idx: int, name="Trigger", colour=5, custom=False,
                          trigger_type=0, control=None, channel=1, on_val=127, off_val=0,
                          debug=False):
    """Low-level MIDI TRIGGER create (padType=4). No file, no region-B block. Mirrors
    assign_create_mixer. Default mapping = a CC on control=padIdx, channel 1, on 127, off 0."""
    handle = pr.free_handle(current_pad_count())
    pre, mid, post = pr.build_trigger_create(target_idx, name, colour, handle=handle,
                                             custom=custom, trigger_type=trigger_type,
                                             control=control, channel=channel,
                                             on_val=on_val, off_val=off_val)
    kind = (f"custom {TRIGGER_TYPE_LABEL.get(trigger_type, trigger_type)} ctrl="
            f"{control if control is not None else target_idx} ch={channel}") if custom else "default CC"
    print(f"  target padIdx={target_idx} (grid pad {target_idx + 1})  name={name!r}  "
          f"MIDI trigger ({kind})  handle=0x{handle:02x}  [no file, no region B]")
    print(f"  {len(pre)} pre -> sync -> {len(mid)} commit")
    _send_create(pre, mid, post, debug=debug)
    return f"created MIDI trigger on grid pad {target_idx + 1}"


def create_trigger(grid: int, name=None, colour=5, custom=False, trigger_type=0,
                   control=None, channel=1, on_val=127, off_val=0, debug=False) -> dict:
    """High-level: put a MIDI TRIGGER pad on grid pad `grid` (1-based) via clear+create.

    NOT gated on the clean pool: a trigger pad writes NOTHING to region B (same as Mixer/Sound),
    so it cannot orphan. Clears the slot first if occupied. Reports pool health (must stay clean).
    Default = the factory per-pad CC mapping; pass custom=True with type/control/channel/on/off
    for a custom MIDI message.
    """
    if name is None:
        name = "MIDI Trigger"
    return _replace_slot(grid, "MIDI trigger", lambda: assign_create_trigger(
        grid - 1, name=name, colour=colour, custom=custom, trigger_type=trigger_type,
        control=control, channel=channel, on_val=on_val, off_val=off_val, debug=debug))


# ----------------------------------------------------------------------------------------------
# RESTORE FACTORY FX
# ----------------------------------------------------------------------------------------------

def _match_recipe(name: str) -> str:
    """Resolve a user-typed name to a FACTORY recipe key, for the CLI."""
    key = _safe_match_recipe(name, RECIPES)
    if key is None:
        raise SystemExit(f"unknown pad {name!r}; known: {list(RECIPES)}")
    return key


def restore_one(pad_name: str, live: bool):
    """Restore ONE factory FX pad EXACTLY (clear the slot if present, then create the factory
    recipe with factory name + colour). `pad_name` must be a key in fx_recipes.json."""
    spec = RECIPES[pad_name]
    padidx, colour, recipe = spec["padIdx"], spec["colour"], spec["stack"]
    stack = " + ".join(recipe)
    pos, ptype, nm = _position_of(padidx)
    print(f"\n{pad_name}  padIdx {padidx} (grid pad {padidx + 1})  colour {colour}")
    print(f"  recipe: {stack}")
    print(f"  current slot: pos={pos} type={ptype} name={nm!r}")
    if not live:
        print("  [dry-run] would clear (if present) then create the factory recipe")
        return
    if pos is not None:
        print("  clearing existing pad...")
        print("   ", clear_slot(pos, ptype))
        time.sleep(1.2)
    print("  creating factory recipe...")
    # active=False IS LOAD-BEARING: assign_create_fx_recipe defaults to active=True (for the live
    # preview), which ENGAGES the effect on the live mic the moment the pad is made. A restored
    # factory pad must be AT REST. In a run of restores each clear switches the previous pad off,
    # so only the last one would stay latched, and nothing on the grid shows it.
    print("   ", assign_create_fx_recipe(padidx, pad_name, recipe, colour=colour, active=False))
    time.sleep(1.0)


def restore_factory_fx(name: str = "all", live: bool = True):
    """Restore one named factory FX pad, or 'all' six. Returns nothing (prints progress)."""
    if name == "all":
        # Only the real factory PADS have a padIdx; shipped extra presets (Reverb/Echo,
        # padIdx null) are offered in the dropdown but are not part of the factory pad set.
        factory = [nm for nm in RECIPES if RECIPES[nm].get("padIdx") is not None]
        for nm in factory:
            restore_one(nm, live=live)
        print(f"\nAll {len(factory)} factory FX pads restored. Ear-check each.")
        return
    restore_one(_match_recipe(name), live=live)


def factory_fx_names() -> list:
    """The factory VOICE-FX pads, in pad order: (name, grid position). The shipped extra presets
    (Reverb, Echo) are excluded - they have no padIdx, so they are effects you can PUT on a pad, not
    pads the desk ships with."""
    named = [(nm, RECIPES[nm]["padIdx"]) for nm in RECIPES
             if RECIPES[nm].get("padIdx") is not None]
    return [(nm, idx + 1) for nm, idx in sorted(named, key=lambda x: x[1])]


def restore_factory_fx_pads(live: bool = True, progress=None) -> dict:
    """Restore the factory VOICE EFFECTS and the two factory MIXER pads, and RETURN a result.

    `restore_factory_fx` above is the CLI shape - it prints and returns nothing - so the web UI
    cannot use it. Same work, wrapped: this reports each step through `progress` and hands back what
    was done.

    THE MIXER PADS RIDE WITH THE EFFECTS because they are the same KIND of job: Censor and Ducking
    are settings, not audio, so both can be rebuilt from the tables in this file with no file to
    find. That is exactly what separates them from the eight factory SOUNDS, which need a WAV this
    app deliberately does not carry - those come back with a factory reset, and nothing here
    pretends otherwise.

    DESTRUCTIVE, and the caller must have confirmed: each of these eight grid positions is cleared
    and recreated from the factory definition, so anything the user has put there is gone. It does
    NOT touch the factory SOUND pads or anything at padIdx 16 and above. CALLER HOLDS THE HID
    LOCK."""
    names = factory_fx_names()
    jobs = ([(nm, grid, "vfx", None) for nm, grid in names]
            + [(MIXER_MODE_LABEL[mode], grid, "mixer", mode) for grid, mode in FACTORY_MIXER_PADS])
    jobs.sort(key=lambda j: j[1])
    results = []
    for n, (nm, grid, kind, mode) in enumerate(jobs, start=1):
        if progress:
            progress(f"Restoring {nm} on pad {grid} ({n} of {len(jobs)})")
        try:
            if kind == "vfx":
                restore_one(nm, live=live)
            elif live:
                res = create_mixer(grid, mode)
                if not res.get("created"):
                    raise RuntimeError(res.get("message"))
            results.append({"name": nm, "grid": grid, "ok": True})
        except Exception as exc:
            # Carry on rather than stopping: five restored pads and one named failure is a far
            # better outcome than an abort that leaves the grid half-cleared with no report.
            results.append({"name": nm, "grid": grid, "ok": False, "error": str(exc)})
    done = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]

    # THE BACKSTOP. Creating an FX pad can leave it ENGAGED on a live mic, and that is invisible:
    # the pad's name, type and colour are all correct. `active=False` in restore_one prevents it;
    # this checks that it did, and catches a pad left latched by anything else.
    if progress:
        progress("Checking nothing was left switched on")
    stuck = unlatch_all()
    note = ""
    if stuck:
        note = (" NOTE: " + ", ".join(nm for _p, _i, nm in stuck)
                + " " + plural(len(stuck), "was", "were")
                + " left switched on and processing a live channel; that has been switched off.")

    return {
        "ok": not failed,
        "results": results,
        "unlatched": [nm for _p, _i, nm in stuck],
        "message": (f"All {len(done)} factory effect and mixer pads restored on pads "
                    f"{', '.join(str(r['grid']) for r in done)}. Worth an ear-check on each."
                    if not failed else
                    f"{len(done)} of {len(results)} restored. Failed: "
                    + "; ".join(f"{r['name']} (pad {r['grid']}): {r['error']}"
                                for r in failed)) + note,
    }


def heal_fx_pool(progress=None) -> dict:
    """Rebuild any voice-FX pad whose effect DEFINITION is missing, and touch nothing else.

    THE DESK REACHES THIS STATE BY ITSELF, which is why this is a repair rather than a diagnostic.
    Proven against model dumps: `pre-showreset-raw.bin` holds all seven region-B objects, `post-showreset-raw.bin` holds only the
    special one at 65 with all six FX pad records still present, and `default-show-raw.bin` shows
    why - RODE's factory default show ships the FX pad records with their `effectsIdx` pointers and
    no objects for them to point at. The firmware garbage-collects an object the moment no pad claims
    it but never allocates one (SOUNDPADS.md section 5), and only an explicit effects block (a 0x9d
    frame) allocates. So `newFromDefault` drops the pad table, the GC takes all six objects with it,
    the default show writes the records back, and nothing re-allocates. The user gets six voice-FX
    pads with the right name, type and colour that make no sound and process no mic - the exact class
    of fault nothing on screen can show. Object 65 survives because it belongs to no pad, so the
    pad-keyed GC never reaches it.

    A show IMPORT is NOT affected (`post-showimport-raw.bin` holds all seven): an exported show
    carries the objects. It is the DEFAULT show alone that does not.

    IDEMPOTENT AND CHEAP WHEN HEALTHY: one dump, and no writes at all when nothing is missing. That
    is what lets it sit at the end of every path that can rewrite the pad grid rather than being a
    button somebody has to know to press.

    Rebuilds BY NAME, via the same recipe match `backup_pads` uses, so a user's own saved preset heals
    as well as a factory one. A pad whose name matches no known effect is REPORTED, never guessed at:
    we would have to invent a recipe, and a wrong effect is worse than an honest gap.
    CALLER HOLDS THE HID LOCK."""
    blob = rr.read_model_stable(fresh=True)
    health = pool_health(blob)
    missing = list(health["missing"])
    if not missing:
        return {"ok": True, "healed": [], "unrecognised": [], "failed": [],
                "health": health, "message": ""}

    fx_pads = {p.idx: p for p in pad_nodes(blob) if p.type == PAD_TYPE_VOICE_FX}
    recipes = all_recipes()
    healed, unrecognised, failed = [], [], []
    for n, pidx in enumerate(sorted(missing), start=1):
        pad = fx_pads.get(pidx)
        name = pad.name if pad else ""
        key = _safe_match_recipe(name)
        if key is None:
            unrecognised.append({"grid": pidx + 1, "name": name})
            continue
        if progress:
            progress(f"Rebuilding {name} on pad {pidx + 1} ({n} of {len(missing)})")
        spec = recipes[key]
        # The pad's OWN colour wins over the recipe's: a recoloured pad must not silently go back to
        # factory red because its effect needed rebuilding. The recipe colour is the fallback for a
        # pad whose colour we could not read.
        colour = spec.get("colour")
        if pad and isinstance(pad.props.get("padColourIndex"), int):
            colour = pad.props["padColourIndex"]
        try:
            safe_assign_fx_recipe(pidx, name or key, spec["stack"],
                                  colour=int(colour), active=False)
            healed.append({"grid": pidx + 1, "name": name, "effect": key})
        except Exception as exc:
            # Carry on: five rebuilt pads and one named failure beats an abort half way through.
            failed.append({"grid": pidx + 1, "name": name, "error": str(exc)})

    bits = []
    if healed:
        bits.append(f"{count(len(healed), 'voice-effect pad')} had no effect "
                    f"to play and {plural(len(healed), 'has', 'have')} been rebuilt: "
                    + ", ".join(f"{h['name']} (pad {h['grid']})" for h in healed)
                    + ". Worth an ear-check on each.")
    if unrecognised:
        bits.append("Could not rebuild "
                    + ", ".join(f"{u['name'] or 'the pad'} (pad {u['grid']})" for u in unrecognised)
                    + ": the name matches no voice effect this app knows, so there is nothing to "
                      "rebuild it from. Assign an effect to it on the SMART pads page.")
    if failed:
        bits.append("Failed to rebuild "
                    + "; ".join(f"{f['name']} (pad {f['grid']}): {f['error']}" for f in failed))
    return {"ok": not failed and not unrecognised, "healed": healed,
            "unrecognised": unrecognised, "failed": failed,
            "health": pool_health(), "message": " ".join(bits)}


def latched_pads(blob=None) -> list:
    """[(position, padIdx, name)] for every pad the desk currently has ENGAGED (padActive true).

    A latched pad changes nothing on the grid (name, type, colour, presence), only `padActive`. A
    latched VOICE-FX pad is the dangerous case: it is silent in itself and processes a live mic, so
    the only symptom is that you sound wrong, with nothing on screen to explain it.

    SYSTEM PADS (padType 6, bank 8: Input 1-4, Scene A/B, Auto, Cut) ARE EXCLUDED. Their
    `padActive` is the desk showing which input and scene it has selected, not a pad somebody left
    engaged, so unlatching one switches off the desk's own state."""
    if blob is None:
        blob = rr.read_model_stable()
    return [(p.position, p.idx, p.name) for p in pad_nodes(blob)
            if p.type != SYSTEM_PAD_TYPE and p.props.get("padActive") is True]


def pad_play_modes(blob=None) -> dict:
    """padIdx -> padPlayMode (see PLAY_MODE_LABEL) for every pad record.

    WHY A SHORTCUT NEEDS THIS. A second press on a sound pad does whatever the PAD's own mode says -
    Toggle stops it, One Shot restarts it from the beginning, and there is no global stop - so a
    shortcut that always says "playing" is wrong half the time. This is what lets it say which."""
    if blob is None:
        blob = rr.read_model_stable()
    return {p.idx: p.props["padPlayMode"] for p in pad_nodes(blob)
            if isinstance(p.props.get("padPlayMode"), int)}


def engage_pad(position: int) -> None:
    """Switch ONE pad on: a bare `padActive` ON at its position. The mirror of `unlatch_pad`.

    This is how a pad is fired from software, and it is the SAME write that create sends to leave a
    new voice-FX pad engaged. Measured: a voice-FX pad engages and releases; a MIXER pad performs its
    whole action (Back Channel moved cue, talkback and two mutes together); a SOUND pad sets the
    flag and moves nothing else in the model, so whether it makes a noise is an ear question.

    NOT the select-begin sequence, deliberately: it is the select-begin that DELETES a record. This
    sends the one flag and nothing else, exactly like `unlatch_pad`.

    No transfer mode: a live state toggle, not a record edit."""
    _require_pad_write()
    iface = t.find_control_interface()
    if iface is None:
        raise RuntimeError("no control interface (close the RODE app)")
    dev = rcp_hid.open_handle(iface, "pad engage")
    try:
        for _ in range(20):
            if not dev.read(256, timeout_ms=10):
                break
        rcp_hid.write_report(dev, build_pad_bool("padActive", True, position + SELECTOR_OFFSET))
        time.sleep(0.3)
    finally:
        rcp_hid.close_handle(dev, "pad engage")


def unlatch_pad(position: int) -> None:
    """Disengage ONE pad without clearing it: a bare `padActive` OFF on its position selector.

    The full clear is padActive ON -> padActive OFF -> select-begin, and it is the SELECT-BEGIN that
    removes the record. Sending only the OFF therefore switches the effect off and leaves the pad
    exactly where it was, which is what you want when a create left something engaged.

    No transfer mode, deliberately: this is a live state toggle, not a record edit, and the
    disengage proven on hardware is sent exactly like this."""
    _require_pad_write()
    iface = t.find_control_interface()
    if iface is None:
        raise RuntimeError("no control interface (close the RODE app)")
    dev = rcp_hid.open_handle(iface, "pad unlatch")
    try:
        for _ in range(20):
            if not dev.read(256, timeout_ms=10):
                break
        rcp_hid.write_report(dev, build_pad_bool("padActive", False,
                                                 position + SELECTOR_OFFSET))
        time.sleep(0.3)
    finally:
        rcp_hid.close_handle(dev, "pad unlatch")


def select_bank(bank: int) -> None:
    """Move the desk to a pad bank. `bank` is 0-BASED, exactly as the desk numbers them.

    AN ORDINARY HID WRITE, and that is the whole answer to the MIDI bank-select question. MIDI
    reaches only the bank the desk is currently on, and RODE's MIDI spec offers CC 0 / CC 1 for the
    bank marked "verify" - which is not needed, because `selectedBank` is an int on the GUI block
    (selector 0x07), the same mapped node as `metering`. Proven with the ENGINE as the judge rather
    than a read-back: with the
    bank at 0 a MIDI press fired padIdx 0, and after this write with bank 1 the identical message
    fired padIdx 6, which is bank 2's first pad on a Duo.

    Not gated on the `pad_write` cap: this changes a display setting, not a pad record, so it cannot
    leave a stray. It IS visible on the desk, and the desk pushes the change back, which is how the
    announcer says "Bank 2" without us telling it.

    No transfer mode: a live state change, not a record edit."""
    banks = devices.get_active_device().pad.system_bank
    if not 0 <= int(bank) < banks:
        raise ValueError(f"bank {bank} is outside this desk's {banks} banks")
    t.send_record(t.build_global_prop_int("selectedBank", t.SEL_GUI, int(bank)))


def current_bank(blob=None):
    """The bank the desk is on, 0-BASED, or None if the model does not say.

    Pass a blob you already have: this is read on the way to firing a pad, and a dump of its own
    would put 1.5 seconds in front of a keypress."""
    if blob is None:
        blob = rr.read_model()
    found = [v for v in rr.values(blob, "selectedBank") if isinstance(v, int)]
    return found[0] if found else None


def unlatch_all(blob=None) -> list:
    """Switch off every engaged pad, and report what was switched off. [] when there was nothing."""
    found = latched_pads(blob)
    for position, _pi, _nm in found:
        unlatch_pad(position)
        time.sleep(0.2)
    return found


def export_pad_audio(grid: int, dest_dir: str) -> dict:
    """Copy ONE pad's sound off the unit into `dest_dir`. Read-only as far as the desk is concerned.

    The same copy backup_pads makes for every sound pad, for one pad, to a folder the user can find.
    The file is named by the desk (padFilePath), NOT by whatever the directory listing returns
    first: a pad folder can hold leftovers from earlier replacements, and picking the wrong one
    would hand the user a sound their pad does not play. CALLER HOLDS THE HID LOCK."""
    live = read_pads_live()
    pad = next((p for p in live["pads"] if p["grid"] == int(grid)), None)
    if pad is None:
        return {"ok": False, "message": f"There is no pad {grid} on this unit."}
    if not pad["present"]:
        return {"ok": False, "message": f"Pad {grid} is empty, so there is no sound to export."}
    if pad["type"] != PAD_TYPE_SOUND:
        return {"ok": False,
                "message": (f"Pad {grid} is {pad['name']!r}, a {pad['type_label']} pad. Only sound "
                            f"pads have an audio file; a voice effect is a recipe, not a recording.")}
    wanted = pad.get("file")
    os.makedirs(dest_dir, exist_ok=True)
    with _transfer_mode(1.0):
        t.remount_pad_storage()
        root = t.find_pad_volume()
        if root is None:
            return {"ok": False, "message": "The unit's pad storage did not appear, so nothing "
                                            "was exported. Try again in a moment."}
        src = _pad_audio_source(root, int(grid), wanted)
        if src is None:
            return {"ok": False,
                    "message": f"Pad {grid} says it plays {wanted or 'a file'}, but that file is "
                               f"not on the unit's storage."}
        # Name the copy after the PAD, not after the desk's internal filename, and keep the real
        # extension. A folder of Music_Bed.wav and audio_3.wav tells you nothing about which pad
        # each came from.
        ext = os.path.splitext(src)[1] or ".wav"
        safe = "".join(c for c in str(pad["name"]) if c.isalnum() or c in " ._-").strip() or "pad"
        dst = os.path.join(dest_dir, f"Pad {int(grid)} - {safe}{ext}")
        shutil.copyfile(src, dst)
        size_mb = round(os.path.getsize(dst) / (1024 * 1024), 1)
        return {"ok": True, "path": dst, "folder": dest_dir, "size_mb": size_mb,
                "message": (f"Pad {grid}, {pad['name']}, exported to {dst} ({size_mb} MB). "
                            f"The pad on the unit is unchanged.")}


def _pad_audio_source(root: str, grid: int, wanted: str | None):
    """Path on the mounted volume of the file grid pad `grid` plays, or None. `wanted` is the
    desk's own answer (padFilePath's basename); the first file in the pad's folder is the fallback
    only for a pad whose padFilePath the model does not report."""
    folder = os.path.join(root, "pads", str(grid))
    if wanted:
        cand = os.path.join(folder, wanted)
        if os.path.isfile(cand):
            return cand
    srcs = [s for s in glob.glob(os.path.join(folder, "*")) if os.path.isfile(s)]
    return srcs[0] if srcs else None


# ----------------------------------------------------------------------------------------------
# THE FACTORY PADS THIS APP CAN PUT BACK, which is not the sounds
# ----------------------------------------------------------------------------------------------
# THE EIGHT FACTORY SOUNDS ARE THE DESK'S TO RESTORE, NOT OURS, and the app ships none of RODE's
# audio. Putting a factory SOUND back needs the WAV itself, and there is no copy this app can rely
# on:
#   * shipping them republishes RODE's samples, which is what keeps the repo private;
#   * the desk's own copy under /Application/emmc-data/pads/<n>/ is deleted the moment that pad is
#     replaced (tidy_pad_folder), so it is there exactly until somebody needs it;
#   * the as-found baseline holds whatever was on the desk WHEN IT WAS TAKEN, which is the factory
#     set only if the user had never customised before installing.
# A "restore the defaults" built on any of those works for some people and silently restores
# somebody else's sounds for others, which is worse than not offering it.
#
# **A FACTORY RESET IS THE ANSWER, and it is the same answer for every user.** The desk keeps its
# own copies and `showControlNewFromDefault` restores them - proven on hardware, files and all
# (DevTools/test_factory_sfx_live.py). A user with a backup should restore that first, because it
# needs no reset; see server.action_show_reset, which says both.
#
# WHAT IS LEFT HERE NEEDS NO FILE AT ALL: the six voice effects are recipes and the two mixer pads
# are settings, so both can be rebuilt from nothing but the tables below. That is
# `restore_factory_fx_pads`, and it is device-independent - a Duo ships the same recipes at the same
# padIdx, only spread over more banks.

# grid, mixer mode (Censor=0, Ducking=4). Colour and trigger come from the per-mode factory defaults
# in _MIXER_DEFAULTS, which already match the factory grid.
FACTORY_MIXER_PADS = [(12, 0), (16, 4)]


# ----------------------------------------------------------------------------------------------
# PROFILE PAD BACKUP / RESTORE  (Profiles "Include SMART pads")
# ----------------------------------------------------------------------------------------------
# backup_pads reads the user grid (banks 1-7) and copies each SOUND pad's audio file off the unit
# into a profile folder; restore_pads rebuilds that exact set. Both assume THE CALLER HOLDS THE HID
# LOCK (the server owns it). Voice-FX pads are fingerprinted to a known recipe BY NAME (factory +
# user presets) - the device stores an FX pad as a recipe stack, not a simple label, so a renamed
# or custom FX that matches nothing is recorded with fx_recipe=None and reported (restore skips it
# rather than guessing). System pads (bank 8, padType 6) are never touched.

def _safe_match_recipe(name, recipes=None):
    """Resolve a pad name to a known recipe key (default: factory or user), case and underscore
    insensitive. Returns the key or None (no raise), so backup can flag unrecognised FX."""
    norm = (name or "").strip().lower().replace("_", " ")
    for key in (all_recipes() if recipes is None else recipes):
        if key.lower().replace("_", " ") == norm:
            return key
    return None


def backup_pads(dest_dir: str, progress=None) -> dict:
    """Back up the current user pad grid for a profile. Returns {pads, files_copied, unrecognised}
    and copies each SOUND pad's audio file into dest_dir/<grid>/. CALLER HOLDS THE HID LOCK."""
    shutil.rmtree(dest_dir, ignore_errors=True)       # clean slate so a re-save leaves no stale files
    live = read_pads_live()
    pads = []
    unrecognised = []
    for p in live["pads"]:
        if not p["present"] or p["type"] == SYSTEM_PAD_TYPE:   # skip empty + system pads
            continue
        entry = {
            "grid": p["grid"], "type": p["type"], "type_label": p["type_label"],
            "name": p["name"], "colour": p["colour"], "gain": p["gain"],
            "loop": p["loop"], "replay": p["replay"], "mixer_mode": p["mixer_mode"],
            # Three settings the app can SET and read, and a backup that drops any of them brings
            # the pad back on the create path's default instead: the sound pad's play mode, a mixer
            # pad's latching/momentary trigger, and which channels a Back Channel pad carries.
            "play_mode": p["play_mode"], "mixer_trigger": p["mixer_trigger"],
            "back_channel": p["back_channel"],
            # A MIDI trigger pad's mapping (what it actually sends). Without it a restored trigger
            # pad comes back on the firmware default and a custom mapping is silently lost. All
            # eight fields are kept, including `send`, whose meaning is unknown: a backup records
            # what the pad reads.
            "trigger": p["trigger"],
            # A sound pad's trim/fade envelope, a Fade pad's times and host exclusion, a Censor
            # pad's own sound. Kept verbatim.
            "envelope": p["envelope"], "fade": p["fade"], "censor": p["censor"],
            "file": None, "fx_recipe": None,
            # What the desk says this pad plays, used below to copy the RIGHT file out of a folder
            # that may hold leftovers. Popped before the entry is saved: "file" is the profile's
            # record, and two keys naming the same thing would be one to drift.
            "live_file": p.get("file"),
        }
        if p["type"] == PAD_TYPE_VOICE_FX:            # voice FX: match the pad name to a known
            key = _safe_match_recipe(p["name"])       # recipe (factory OR a saved custom preset)
            entry["fx_recipe"] = key
            if key is not None:
                # Store the actual recipe stack + colour so the profile is self-contained: a
                # restore rebuilds the exact effect even if that preset is later deleted or the
                # profile is restored on another machine.
                spec = all_recipes().get(key) or {}
                entry["fx_stack"] = spec.get("stack")
                entry["fx_colour"] = spec.get("colour")
            else:
                unrecognised.append(p["grid"])        # a renamed pad matching no preset
        pads.append(entry)

    # Copy every sound pad's file in ONE transfer-mode session (reads only; non-destructive).
    sound = [e for e in pads if e["type"] == PAD_TYPE_SOUND]
    copied = 0
    if sound:
        with _transfer_mode(1.0):
            t.remount_pad_storage()
            root = t.find_pad_volume()
            if root is None:
                raise RuntimeError("pad storage volume did not mount for the backup")
            for e in sound:
                # NO PER-PAD PROGRESS LINE, deliberately. Copying a pad takes about a second, so a
                # line each is speech that interrupts itself. The caller says how many are coming
                # and when they are done.
                # quiet_drive_errors: if a read fails, the caller reports it in the progress dialog,
                # so Windows must not ALSO throw its own modal "problem with this drive" box.
                with t.quiet_drive_errors():
                    src = _pad_audio_source(root, e["grid"], e.get("live_file"))
                    if src is None:
                        continue
                    fn = os.path.basename(src)
                    dst_dir = os.path.join(dest_dir, str(e["grid"]))
                    os.makedirs(dst_dir, exist_ok=True)
                    try:
                        shutil.copyfile(src, os.path.join(dst_dir, fn))
                    except OSError as exc:
                        raise RuntimeError(
                            f"could not read pad {e['grid']} ({e['name']}) from the unit's "
                            f"storage: {fn}: {exc}") from exc
                e["file"] = fn
                copied += 1
    for e in pads:
        e.pop("live_file", None)          # working field only; never goes in the profile
    return {"pads": pads, "files_copied": copied, "unrecognised": unrecognised}


# ---- tidy up pad audio nothing points at -------------------------------------------------------
#
# CLEARING A PAD DELETES NO AUDIO, and that is not an oversight in clear_pad: a clear is HID-only ON
# PURPOSE (no remountPadStorage, no file writes), which is why clearing a pad raises no Windows
# "drive F has a problem" toast. It drops the pad RECORD and its region-B effect object and leaves
# the wav on the card, and a new sound create writes a NEW file beside it under its own name. So
# without this sweep every replacement and every restore-to-defaults leaks the previous audio:
# measured on a working desk, 53.3 MB of 73.0 MB was files no pad pointed at.
#
# DELETED OUTRIGHT, NOT ARCHIVED, and for a better reason than tidiness: these pads get played into
# calls and voicemails, so the owner has to know where that audio ends up, and a hidden copy of it
# on the computer is the wrong answer. Overwriting a pad means the old sound is no longer wanted.
#
# WHAT IS AUTHORITATIVE: a live pad's padFilePath (read_pads_live's "file"). Never the directory
# listing - a folder can hold several files and only the desk knows which one is played.

def _pad_audio_dirs(root: str):
    """Every pads/<n>/ folder on the mounted volume. Confined to root/pads by construction: this is
    the only place either tidy function is allowed to touch."""
    base = os.path.join(root, "pads")
    return [d for d in glob.glob(os.path.join(base, "*")) if os.path.isdir(d)]


def tidy_pad_folder(root: str, grid: int, keep: str) -> list:
    """Delete everything in pads/<grid>/ EXCEPT the file named `keep`. For use INSIDE an existing
    transfer-mode session, which is why it costs a pad assignment nothing: no extra mount, no model
    read, and the caller already knows the filename it just wrote. Returns the names removed.

    A FACTORY SOUND IS DELETED LIKE ANY OTHER and that is deliberate: the desk keeps its own copy
    and a software factory reset restores all eight, so the file here is not the only copy and
    nothing is lost by tidying it."""
    removed = []
    if not keep:
        return removed                       # without a keeper we cannot safely delete anything
    folder = os.path.join(root, "pads", str(grid))
    for f in glob.glob(os.path.join(folder, "*")):
        if not os.path.isfile(f) or os.path.basename(f) == keep:
            continue
        try:
            os.remove(f)
            removed.append(os.path.basename(f))
        except OSError:
            pass                             # locked or gone; the full sweep will get it later
    return removed


def tidy_unused_pad_audio(progress=None, allow_empty: bool = False) -> dict:
    """Sweep the whole pad volume: delete every file under pads/ that no live pad plays.

    One model read plus ONE transfer-mode session, so the cost does not grow with the number of
    files. Returns {ok, removed, bytes_freed, kept, message}.

    allow_empty guards the one genuinely dangerous case. If the model read comes back with no pads
    at all, that is EITHER a desk with an empty grid (where deleting all the audio is right) or a
    read that went wrong (where it would be a disaster). Callers that know the grid is meant to be
    empty - clear-all-pads - pass True; everything else refuses and says so."""
    live = read_pads_live()["pads"]
    keep = {(p["grid"], p["file"]) for p in live if p.get("file")}
    if not keep and not allow_empty:
        return {"ok": False, "removed": [], "bytes_freed": 0, "kept": 0,
                "message": "No pad reported a sound file, so nothing was deleted. That is either an "
                           "empty pad grid or a failed read, and the two are indistinguishable from "
                           "here."}
    if progress:
        progress("Tidying up pad audio nothing points at.")
    removed, freed, kept = [], 0, 0
    with _transfer_mode(1.0):
        t.remount_pad_storage()
        root = t.find_pad_volume()
        if root is None:
            return {"ok": False, "removed": [], "bytes_freed": 0, "kept": 0,
                    "message": "The pad storage volume did not mount, so nothing was deleted."}
        for d in _pad_audio_dirs(root):
            folder = os.path.basename(d)
            grid = int(folder) if folder.isdigit() else None
            for f in sorted(glob.glob(os.path.join(d, "*"))):
                if not os.path.isfile(f):
                    continue
                if grid is not None and (grid, os.path.basename(f)) in keep:
                    kept += 1
                    continue
                try:
                    size = os.path.getsize(f)
                    os.remove(f)
                    removed.append(f"pads/{folder}/{os.path.basename(f)}")
                    freed += size
                except OSError:
                    pass
            try:
                if not os.listdir(d):        # the folder's pad is gone too; leave nothing behind
                    os.rmdir(d)
            except OSError:
                pass
        if removed:
            t.flush_volume(root)
    mb = freed / 1024 / 1024
    msg = (f"Removed {count(len(removed), 'unused sound file')}, freeing {mb:.1f} MB. "
           f"Kept the {kept} in use." if removed
           else f"No unused sound files: all {kept} on the card belong to a pad.")
    return {"ok": True, "removed": removed, "bytes_freed": freed, "kept": kept, "message": msg}


# ---- skip-if-identical: don't spend 15 seconds putting a pad back the way it already is --------
#
# WHY. A pad restore costs about 15 seconds EACH, and almost none of that is the audio: every pad
# runs its own transfer-mode cycle (assign_create: enter, 1s settle, remount, find volume, copy,
# exit), a clear_pad with a 1.2s settle, a wait_for_pad_present, then separate colour/gain/play-mode/
# loop writes. A pad that is already correct pays all of it to arrive where it already was.
#
# The check is cheap and, crucially, done ONCE for the whole grid rather than per pad: the metadata
# comes free with one read_pads_live dump (which carries exactly the fields a profile stores, since
# backup_pads builds its entries from that same read), and the audio is compared inside ONE
# transfer-mode session, the pattern backup_pads already uses. So the cost is one transfer-mode
# cycle plus some fast reads no matter how many pads there are.
#
# THE RULE, because a wrong skip leaves a silently wrong pad and nobody would know: skip ONLY on a
# total match. Anything missing, unreadable, or not understood means rebuild. Decided per pad, so a
# mismatch on one can never affect another.

# Fields a profile stores that are also readable live. A saved None is skipped: restore_pads would
# not write it either, so it cannot be a difference.
_PAD_COMPARE_FIELDS = ("name", "colour", "gain", "loop", "replay", "play_mode",
                       "mixer_mode", "mixer_trigger", "back_channel", "trigger")

# Types we are willing to skip. Voice FX (2) is deliberately EXCLUDED: its identity lives in the
# effect object in region B, which we cannot verify from a model dump, and region B is where the
# orphan damage happens. An FX pad is also cheaper to rebuild than a sound pad (no file copy).
_SKIPPABLE_PAD_TYPES = (PAD_TYPE_SOUND, PAD_TYPE_MIXER, PAD_TYPE_TRIGGER)


def _file_sha256(path: str) -> str | None:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _pad_metadata_matches(saved: dict, live: dict) -> bool:
    """True when every field this profile RECORDED already reads back the same off the unit."""
    if not live or not live.get("present"):
        return False
    if saved.get("type") != live.get("type"):
        return False
    for field in _PAD_COMPARE_FIELDS:
        want = saved.get(field)
        if want is None:
            continue                          # not recorded, so the restore would not write it
        got = live.get(field)
        if field == "gain":
            # read_pads_live rounds gain to 1dp; compare on the same basis rather than on raw floats.
            if got is None or round(float(want), 1) != round(float(got), 1):
                return False
        elif field == "back_channel":
            # Every member is written on restore, so every member must already agree AND must
            # actually be reported. A key the live read does not carry is a member we cannot
            # verify, not a member that is off, so it means rebuild.
            if not isinstance(got, dict):
                return False
            if any(k not in got or bool(want[k]) != bool(got[k]) for k in want):
                return False
        elif field == "trigger":
            # A MIDI trigger's mapping. Only the fields the restore would WRITE are compared, which
            # is the custom mapping: with custom off, the create path reproduces the firmware default
            # and the numbers underneath it are not written, so a difference there would force a
            # rebuild that changes nothing the pad sends.
            if not isinstance(got, dict):
                return False
            if bool(want.get("custom")) != bool(got.get("custom")):
                return False
            if want.get("custom") and any(want.get(k) != got.get(k)
                                          for k in ("type", "control", "channel", "on", "off")):
                return False
            # Latching versus momentary is restored either way, so it is compared either way.
            if want.get("mode") is not None and want.get("mode") != got.get("mode"):
                return False
        elif field == "name":
            if str(want) != str(got):
                return False
        elif isinstance(want, bool) or isinstance(got, bool):
            # loop and replay can come back as 0/1 through JSON, so booleans compare as booleans.
            # Checked BEFORE the numeric case because in Python a bool IS an int.
            if bool(want) != bool(got):
                return False
        elif want != got:
            # colour, play_mode, mixer_mode, mixer_trigger: exact.
            return False
    return True


def _pad_audio_matches(root: str, grid: int, saved_file: str, local_path: str,
                       live_file: str | None = None) -> bool:
    """True when the sound this pad PLAYS is byte-for-byte the backup's copy.

    live_file is the filename the desk reports for the pad (padFilePath). It is what makes this
    safe in a folder holding leftovers from earlier replacements: what matters is the file the pad
    points AT, not what else happens to sit beside it. Given no live_file we fall back to requiring
    exactly one file in the folder, since then we cannot tell which one is played.

    Name, then size, then a full content hash. The hash is the only one that actually proves it, and
    reading a couple of megabytes off the mounted volume costs a fraction of one pad rebuild."""
    if not saved_file or not os.path.isfile(local_path):
        return False
    if live_file:
        if live_file != saved_file:
            return False                      # the pad plays something else entirely
        on_unit_path = os.path.join(root, "pads", str(grid), live_file)
        if not os.path.isfile(on_unit_path):
            return False
    else:
        on_unit = [f for f in glob.glob(os.path.join(root, "pads", str(grid), "*"))
                   if os.path.isfile(f)]
        if len(on_unit) != 1 or os.path.basename(on_unit[0]) != saved_file:
            return False                      # no file, or no way to know which one is played
        on_unit_path = on_unit[0]
    try:
        if os.path.getsize(on_unit_path) != os.path.getsize(local_path):
            return False
    except OSError:
        return False
    device_hash = _file_sha256(on_unit_path)
    return device_hash is not None and device_hash == _file_sha256(local_path)


def unchanged_pads(pads: list, src_dir: str, progress=None) -> set:
    """Which of these saved pads are ALREADY on the unit exactly as saved, and can be left alone.
    CALLER HOLDS THE HID LOCK. Never raises: any trouble means an empty set, i.e. restore everything,
    which is the same work as before this optimisation existed."""
    try:
        if progress:
            progress("Checking which SMART pads already match.")
        live_by_grid = {p["grid"]: p for p in read_pads_live()["pads"]}
        diagnostics.event("pads.verify.start", "checking which pads already match",
                          saved=len(pads))
        candidates = [e for e in pads
                      if e.get("type") in _SKIPPABLE_PAD_TYPES
                      and _pad_metadata_matches(e, live_by_grid.get(e.get("grid")))]
        sound = [e for e in candidates if e.get("type") == PAD_TYPE_SOUND]
        unchanged = {e["grid"] for e in candidates if e.get("type") != PAD_TYPE_SOUND}
        if sound:
            # ONE transfer-mode session for every audio comparison (reads only, nothing written).
            with _transfer_mode(1.0):
                t.remount_pad_storage()
                root = t.find_pad_volume()
                if root is None:
                    # THE VOLUME DID NOT MOUNT, so no sound pad can be compared and every one of
                    # them is about to be rewritten. SAY SO: silently doing the maximum work looks
                    # identical to the check deciding they all differ, and the user is left watching
                    # a restore rebuild pads that were already correct with no idea why.
                    diagnostics.event("pads.verify.no_volume",
                                      "pad storage did not mount, so no sound pad could be "
                                      "compared", sound_pads=len(sound))
                    if progress:
                        progress("Could not read the unit's pad storage, so every sound pad will "
                                 "be rebuilt.")
                else:
                    for e in sound:
                        local = os.path.join(src_dir, str(e["grid"]), e.get("file") or "")
                        live_name = (live_by_grid.get(e["grid"]) or {}).get("file")
                        if _pad_audio_matches(root, e["grid"], e.get("file"), local,
                                              live_file=live_name):
                            unchanged.add(e["grid"])
        # WHY EACH PAD IS BEING REBUILT, in one line, because "it still finds pads to restore" is
        # otherwise unanswerable from outside. Voice FX is ALWAYS here: it is excluded from the skip by
        # `_SKIPPABLE_PAD_TYPES` because its identity lives in region B, which a model dump cannot
        # show, and the rule in this file is to skip only on a total match.
        diagnostics.event("pads.verify.end", "pads already matching", unchanged=len(unchanged),
                          rebuilding=len(pads) - len(unchanged),
                          fx_always_rebuilt=sum(1 for e in pads
                                                if e.get("type") == PAD_TYPE_VOICE_FX))
        return unchanged
    except Exception as exc:
        # NEVER LET THE OPTIMISATION BREAK THE RESTORE - but never let it fail quietly either. An
        # empty set means EVERY pad gets rewritten, which on a full grid is minutes of work and a
        # transfer-mode cycle per pad, so the log has to record that the check gave up and why.
        diagnostics.event("pads.verify.failed",
                          "the match check gave up, so every pad will be rebuilt", error=f"{exc}")
        if progress:
            progress("Could not check which pads already match, so all of them will be rebuilt.")
        return set()


def restore_pads(pads: list, src_dir: str, progress=None, verify_first: bool = True) -> dict:
    """Rebuild a backed-up pad set onto the unit (clear+create each, lowest grid first). Sound
    files come from src_dir/<grid>/<file>. CALLER HOLDS THE HID LOCK, and should also hold ONE
    `rcp_transfer.transfer_session()` around this: without it every
    create enters transfer mode, remounts the volume and leaves again for itself - sixteen cycles
    for sixteen pads, each one dropping the desk's audio and popping a Windows device sound. The
    per-pad calls are unchanged; while a session is held they simply do nothing. Returns {ok,
    results, skipped, health}. Waits for each pad to register before the next create (the stale-handle
    guard against a stale free handle)."""
    results = []
    skipped = []
    ok = True
    # Pads already exactly as saved are left alone (see unchanged_pads). This is a decision per pad,
    # never a blanket one, and it is skipped entirely with verify_first=False.
    unchanged = unchanged_pads(pads, src_dir, progress=progress) if verify_first else set()
    todo = [e for e in sorted(pads, key=lambda x: x.get("grid", 0))
            if e.get("grid") not in unchanged]
    for e in sorted(pads, key=lambda x: x.get("grid", 0)):
        if e.get("grid") in unchanged:
            # Counted as a success, because the pad IS in the state the profile asked for.
            results.append({"grid": e.get("grid"), "label": e.get("name") or "",
                            "ok": True, "unchanged": True})
    # The count reflects the pads actually being written, so "3 of 4" cannot promise work that the
    # verify pass has already established is unnecessary.
    total = len(todo)
    done = 0
    # Every create and setting write below reports failure in its return rather than raising, so
    # each one is checked: an unchecked failure would count the pad as restored.
    problems = []

    def check(res, what):
        if not res.get("ok"):
            problems.append(f"{what}: {res.get('message') or 'failed'}")

    def created(res):
        if not res.get("created"):
            raise RuntimeError(res.get("message") or "the pad was not created")

    for e in todo:
        grid = e.get("grid")
        ptype = e.get("type")
        label = e.get("name") or ""
        done += 1
        if progress:
            progress(f"Pad {done} of {total}: {label}")
        problems.clear()
        try:
            if ptype == PAD_TYPE_SOUND:
                fn = e.get("file")
                src = os.path.join(src_dir, str(grid), fn) if fn else None
                if not src or not os.path.isfile(src):
                    skipped.append(f"pad {grid} (sound file missing)"); ok = False; continue
                replay = e.get("replay")
                created(create_sound(grid, src, name=label or "Sound", colour=e.get("colour") or 5,
                                     replay=bool(replay) if replay is not None else True))
            elif ptype == PAD_TYPE_VOICE_FX:
                stack = e.get("fx_stack")
                key = e.get("fx_recipe")
                if stack:                             # self-contained: rebuild from the saved stack
                    colour = e.get("fx_colour")
                    if colour is None:
                        colour = e.get("colour") if e.get("colour") is not None else 8
                    safe_assign_fx_recipe(grid - 1, label or key or "Effect", stack,
                                          colour=int(colour), active=False)
                elif key:                             # older backup: fall back to the live recipe
                    assign_fx_to_grid(grid, key)
                else:
                    skipped.append(f"pad {grid} ('{label}': unrecognised effect)"); continue
            elif ptype == PAD_TYPE_MIXER:
                if e.get("mixer_mode") is None:
                    skipped.append(f"pad {grid} (mixer mode missing)"); ok = False; continue
                created(create_mixer(grid, e["mixer_mode"]))
            elif ptype == PAD_TYPE_TRIGGER:
                created(create_trigger(grid, name=label or None, colour=e.get("colour") or 5))
            else:
                skipped.append(f"pad {grid} (type {ptype} not restorable)"); continue
            if not wait_for_pad_present(grid - 1):
                raise RuntimeError("the pad did not appear on the unit after it was created")
            # Re-apply the saved cosmetics/behaviour the create path does not already set.
            if e.get("colour") is not None:
                check(set_pad_colour(grid, int(e["colour"])), "colour")
            if ptype != PAD_TYPE_SOUND and label:     # sound create already set the name
                check(set_pad_name(grid, label), "name")
            if e.get("gain") is not None and ptype in (PAD_TYPE_SOUND, PAD_TYPE_VOICE_FX):
                check(set_pad_gain(grid, float(e["gain"])), "volume")
            # PLAY MODE before loop: set_play_mode turns padLoop off when it selects One Shot
            # (loop is meaningless there), so restoring loop afterwards keeps the saved pair
            # consistent instead of having the play-mode write clear a loop just restored.
            if ptype == PAD_TYPE_SOUND and e.get("play_mode") is not None:
                check(set_play_mode(grid, int(e["play_mode"])), "play mode")
            if ptype == PAD_TYPE_SOUND and e.get("loop") is not None:
                check(set_pad_loop(grid, bool(e["loop"])), "loop")
            if ptype == PAD_TYPE_MIXER and e.get("mixer_trigger") is not None:
                check(set_mixer_trigger(grid, int(e["mixer_trigger"])), "trigger mode")
            # A MIDI trigger's mapping. Written in ONE session, and only when the saved pad actually
            # HAD a custom mapping: the create path already reproduces the firmware default exactly,
            # so writing the default numbers back would be five seconds of transfer mode to change
            # nothing. A profile with no "trigger" key at all is the same case.
            trig = e.get("trigger") or {}
            if ptype == PAD_TYPE_TRIGGER and trig:
                want = {}
                if trig.get("custom"):
                    want.update({k: trig.get(k) for k in
                                 ("type", "control", "channel", "on", "off")})
                # The mode is restored WHETHER OR NOT the mapping is custom, because it is not
                # known that the flag gates it, and a pad that came back latching when it was saved
                # momentary would be wrong in a way nothing on screen would show. 1 is what the
                # create path leaves behind, so only a saved 0 costs a write.
                if trig.get("mode") is not None and int(trig["mode"]) != TRIGGER_CREATE_MODE:
                    want["mode"] = int(trig["mode"])
                if want:
                    check(set_trigger_mapping(grid, custom=bool(trig.get("custom")), **want),
                          "MIDI mapping")
            # The envelope / fade / censor groups, each written only for the pad type that owns it
            # and only where the profile actually recorded a value. set_pad_properties puts a whole
            # group in ONE transfer-mode session, so a sound pad's four envelope floats cost one
            # session rather than four.
            for what, group, applies in (("trim and fade", e.get("envelope"), ptype == PAD_TYPE_SOUND),
                                         ("fade times", e.get("fade"), ptype == PAD_TYPE_MIXER),
                                         ("censor sound", e.get("censor"), ptype == PAD_TYPE_MIXER)):
                if not applies or not group:
                    continue
                items = [(_EXTRA_PROP_OF[k], v) for k, v in group.items()
                         if v is not None and k in _EXTRA_PROP_OF]
                if items:
                    check(set_pad_properties(grid, items), what)
            # Back Channel membership. The saved dict is non-null only for a Back Channel pad, and
            # every member is written (not just the ticked ones) so the restore is authoritative
            # rather than additive - a member the create path turns on but the backup had off must
            # come back off. set_back_channel_member reads a dump per call and validates against
            # what this desk offers, so this costs a second or two on the one pad that has it.
            for prop, on in (e.get("back_channel") or {}).items():
                check(set_back_channel_member(grid, prop, bool(on)), "Back Channel member")
            if problems:
                results.append({"grid": grid, "label": label, "ok": False,
                                "message": "the pad was created but not every setting went back: "
                                           + "; ".join(problems)})
                ok = False
            else:
                results.append({"grid": grid, "label": label, "ok": True})
        except Exception as exc:
            results.append({"grid": grid, "label": label, "ok": False, "message": str(exc)})
            ok = False
    return {"ok": ok, "results": sorted(results, key=lambda r: r.get("grid") or 0),
            "skipped": skipped, "unchanged": sorted(unchanged), "health": pool_health()}


# ----------------------------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------------------------

def _cli():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    dbg = os.environ.get("AC_DEBUG") == "1"
    if cmd == "list":
        data = read_pads_live()
        print(json.dumps(data["health"], indent=2))
        for p in data["pads"]:
            print(f"  grid {p['grid']:2}  {p['type_label']:12} {p['name'] or ''}")
    elif cmd == "health":
        print(json.dumps(pool_health(), indent=2))
    elif cmd == "clean":
        # GC orphan region-B effect objects. The one implementation: do not add a DevTools shim
        # over it.
        print(json.dumps(clean_pool(), indent=2))
    elif cmd == "positions":
        for pos, pidx, ptype, name in pad_positions():
            print(f"  pos {pos:2}  padIdx={str(pidx):>6}  padType={str(ptype):>4}  {name}")
    elif cmd == "clear":
        print(json.dumps(clear_grid_pad(int(sys.argv[2])), indent=2))
    elif cmd == "create":
        grid = int(sys.argv[2]); folder = int(sys.argv[3]); wav = sys.argv[4]
        name = sys.argv[5] if len(sys.argv) > 5 else "Sound"
        colour = int(sys.argv[6]) if len(sys.argv) > 6 else 5
        print(json.dumps(create_sound(grid, wav, name=name, colour=colour, folder=folder,
                                       debug=dbg), indent=2))
    elif cmd == "fx":
        print(json.dumps(assign_fx_to_grid(int(sys.argv[2]), sys.argv[3]), indent=2))
    elif cmd == "mixer":
        # mixer <grid> <mode 0-4 or name> [name]
        grid = int(sys.argv[2]); mode = sys.argv[3]
        name = sys.argv[4] if len(sys.argv) > 4 else None
        print(json.dumps(create_mixer(grid, mode, name=name, debug=dbg), indent=2))
    elif cmd == "trigger":
        # trigger <grid> [name]   (default CC mapping)
        grid = int(sys.argv[2])
        name = sys.argv[3] if len(sys.argv) > 3 else None
        print(json.dumps(create_trigger(grid, name=name, debug=dbg), indent=2))
    elif cmd == "set":
        # set <grid> <property> <value>   (in-place pad-property edit; property from PAD_PROP)
        grid = int(sys.argv[2]); prop = sys.argv[3]; val = sys.argv[4]
        print(json.dumps(set_pad_property(grid, prop, val), indent=2))
    elif cmd == "restore":
        arg = sys.argv[2] if len(sys.argv) > 2 else "list"
        if arg == "list":
            for nm, v in RECIPES.items():
                where = " -" if v.get("padIdx") is None else f"{v['padIdx'] + 1:2}"
                print(f"  {nm:16} grid {where}  colour {v['colour']:2}  "
                      f"{' + '.join(v['stack'])}")
        else:
            restore_factory_fx(arg, live=True)
    else:
        print(__doc__)


if __name__ == "__main__":
    _cli()
