"""Pad CREATE record builders: every record built from the target pad's parameters.

Nothing is a replayed byte template. A template carries its source pad's identity in every field it
does not overwrite (`padTriggerControl` and `effectsIdx` both track padIdx), so a created pad would
answer to another pad's MIDI trigger.

A create is one held-open HID session (`soundpad` sends it):
  handoff "PAD"                 binds the pad edit handle H
  phase 1                       44 pad properties on H: defaults, with padIdx and
                                padTriggerControl = target
  handoff "EFFECTS_PARAMETERS"  binds the effects sub-handle S (voice-FX pads only)
  effects                       20 effect properties on S: all off, effectsIdx = target
  -- the desk emits padRCVSyncPadType --
  mid                           the commit: padType, envelope, padName, padColourIndex
  -- the WAV is written to the FAT volume (sound pads only) --
  post                          remountPadStorage, padFilePath, padName (sound); the
                                engines' on-flags on S (voice FX)
  exit transfer mode

Records, as they read on a Pro II, whose SOUNDPADS is root child 0x29A and PADEFFECTS 0x29D. Both
are looked up on the attached desk (`rcp_transfer.SOUNDPADS` / `PADEFFECTS`), never written down:
  pad write : 01 01 02 02 9a 02 01 <H> <name> 00 <value block>    path [SOUNDPADS, H]
  fx write  : 01 01 02 02 9d 02 01 <S> <name> 00 <value block>    path [PADEFFECTS, S]
  handoff   : 03 01 01 02 <9a|9d> 02 01 <H> <keyword> 00 00 00    `child_command`
Value blocks are `rcp_transfer.value_block`.

Only a voice-FX pad writes the PADEFFECTS block. A sound, mixer or trigger pad owns no effect
object, and writing one appends an orphan that only a factory reset removes (`docs/SOUNDPADS.md`).
"""
from __future__ import annotations

import model_tree
from rcp_transfer import (PADEFFECTS, SOUNDPADS, _frame, address_of, build_record,
                          build_remount_pad_storage)

PAD_BLOCK = SOUNDPADS
FX_BLOCK = PADEFFECTS

PAD_FILE_PATH_FMT = "/Application/emmc-data/pads/{n}/{name}"


def free_handle(pad_record_count: int) -> int:
    """The edit handle for a create: the first free stream position, which is the current
    pad-record count (records occupy 0..count-1).

    The handle IS the stream position written to. At exactly `count` the desk emits the
    padRCVSyncPadType handshake and appends the record; past it (count+1, count+2) the create is
    silently dropped, with no sync and no commit; below it, the write lands on an existing pad and
    corrupts it. So the count must be read fresh, and the read must be one that counts every record.
    """
    return pad_record_count


# The six effect engines and their on-flags; the keys match fx_recipes.json.
EFFECT_FLAG = {
    "reverb": "reverbOn", "echo": "echoOn", "pitchShift": "pitchShiftOn",
    "distortion": "distortionOn", "robot": "robotOn", "voiceDisguise": "voiceDisguiseOn",
}


def child_path(handle: int, block=PAD_BLOCK) -> list:
    """The tree path of child `handle` of SOUNDPADS or PADEFFECTS on the attached desk."""
    return address_of(block) + [int(handle)]


def write_record(name: str, kind: str, value, handle: int, block=PAD_BLOCK) -> bytes:
    """One pad- or fx-addressed property write on the given handle."""
    return build_record(name, kind, value, child_path(handle, block))


# A child command names a parent and one of its children: `<op> 01 01 <parent> <child>`.
CREATE_CHILD = 0x03    # with a keyword, the PAD / EFFECTS_PARAMETERS handoff that binds a handle
REMOVE_CHILD = 0x04    # the select-begin that ends a pad clear


def child_command(op: int, handle: int, block=PAD_BLOCK, keyword: str = "",
                  cursor: bool = False) -> bytes:
    """A child command on SOUNDPADS or PADEFFECTS. `cursor` addresses the desk's edit cursor
    instead of a position, sending the child index as a zero-length count (see soundpad's cursor
    builders for when that is needed)."""
    (parent,) = address_of(block)
    head = bytes([op, 0x01, 0x01]) + model_tree.encode_index(parent)
    head += model_tree.encode_index(0 if cursor else handle, empty_zero=cursor)
    tail = keyword.encode("ascii") + bytes([0x00, 0x00, 0x00]) if keyword else b""
    return _frame(head + tail)


def handoff_record(keyword: str, handle: int, block) -> bytes:
    """The PAD / EFFECTS_PARAMETERS handle-binding record."""
    return child_command(CREATE_CHILD, handle, block, keyword)


# Phase 1: the RODE app's defaults, in its order. None marks the two fields stamped with the
# target padIdx.
_PHASE1 = [
    ("padColourIndex", "int", 0),
    ("padActive", "bool", False),
    ("padLoop", "bool", False),
    ("padReplay", "bool", False),
    ("padType", "int", 0),
    ("padName", "str", ""),
    ("padProgress", "float", 0.0),
    ("padFilePath", "str", ""),
    ("padPlayMode", "int", 0),
    ("padEnvStart", "float", 0.0),
    ("padEnvFadeIn", "float", 0.0),
    ("padEnvFadeOut", "float", 0.0),
    ("padEnvStop", "float", 0.0),
    ("padMixerMode", "int", 0),
    ("padMixerTriggerMode", "int", 0),
    ("padMixerCensorCustom", "bool", False),
    ("padMixerCensorFilePath", "str", ""),
    ("padMixerFadeInSeconds", "float", 0.0),
    ("padMixerFadeOutSeconds", "float", 0.0),
    ("padMixerFadeExcludeHost", "bool", False),
    ("padMixerBackChannelMic2", "bool", False),
    ("padMixerBackChannelMic3", "bool", False),
    ("padMixerBackChannelMic4", "bool", False),
    ("padMixerBackChannelUsb1Comms", "bool", False),
    ("padMixerBackChannelUsb2Main", "bool", False),
    ("padMixerBackChannelBluetooth", "bool", False),
    ("padMixerBackChannelCallMe1", "bool", False),
    ("padMixerBackChannelCallMe2", "bool", False),
    ("padMixerBackChannelCallMe3", "bool", False),
    ("padEffectInput", "int", 0),
    ("padEffectTriggerMode", "int", 0),
    ("padSIPPhoneBookEntry", "int", 0),
    ("padSIPCallSlot", "int", 0),
    ("padTriggerMode", "int", 1),
    ("padTriggerSend", "int", 2),
    ("padTriggerType", "int", 0),
    ("padTriggerCustom", "bool", False),
    ("padTriggerControl", "int", None),
    ("padTriggerChannel", "int", 1),
    ("padTriggerOn", "int", 127),
    ("padTriggerOff", "int", 0),
    ("padIsInternal", "bool", False),
    ("padGain", "float", -12.0),
    ("padIdx", "int", None),
]

# The EFFECTS_PARAMETERS defaults: full-precision doubles as the RODE app sends them (float32
# values widened). effectsIdx is stamped with the target padIdx.
_EFFECTS = [
    ("reverbOn", "bool", False),
    ("reverbMix", "float", 0.5),
    ("reverbLowCut", "float", 0.6661456227302551),
    ("reverbHighCut", "float", 0.3333245515823364),
    ("reverbModel", "float", 0.6000000238418579),
    ("echoOn", "bool", False),
    ("echoLowCut", "float", 0.5),
    ("echoHighCut", "float", 0.5),
    ("echoDelay", "float", 0.16500000655651093),
    ("echoMix", "float", 0.5),
    ("echoDecay", "float", 0.5),
    ("pitchShiftOn", "bool", False),
    ("pitchShiftSemitones", "float", 7.0),
    ("distortionOn", "bool", False),
    ("distortionIntensity", "float", 0.699999988079071),
    ("robotOn", "bool", False),
    ("robotMix", "float", 0.0),
    ("robotLevel", "float", 0.0),
    ("voiceDisguiseOn", "bool", False),
    ("effectsIdx", "int", None),
]


def build_phase1(target_idx: int, handle: int) -> list[bytes]:
    return [write_record(name, kind, target_idx if value is None else value, handle, PAD_BLOCK)
            for name, kind, value in _PHASE1]


def build_effects(target_idx: int, fx_subhandle: int,
                  param_overrides: dict | None = None) -> list[bytes]:
    """The 20-record EFFECTS_PARAMETERS block. `param_overrides` (name -> value) bakes a recipe's
    tuned params in. The on-flags stay off here; they are turned on in the create's post, the
    order the RODE app uses."""
    overrides = param_overrides or {}
    out = []
    for name, kind, value in _EFFECTS:
        if name == "effectsIdx":
            value = target_idx
        elif name in overrides:
            value = overrides[name]
        out.append(write_record(name, kind, value, fx_subhandle, FX_BLOCK))
    return out


def build_blank(target_idx: int, handle: int, fx_subhandle: int | None = None,
                param_overrides: dict | None = None) -> list[bytes]:
    """handoff PAD + phase 1 (45 records), plus handoff EFFECTS + effects (66 in all) when an
    `fx_subhandle` is given. Only a voice-FX pad passes one: the effects block is the only PADEFFECTS
    traffic in a create."""
    recs = [handoff_record("PAD", handle, PAD_BLOCK)] + build_phase1(target_idx, handle)
    if fx_subhandle is not None:
        recs += ([handoff_record("EFFECTS_PARAMETERS", fx_subhandle, FX_BLOCK)]
                 + build_effects(target_idx, fx_subhandle, param_overrides))
    return recs


def build_sound_create(target_idx: int, folder: int, name: str, colour: int, *, handle: int,
                       replay: bool = True, filename: str = "sound.wav"):
    """A SOUND pad create (padType 1). Returns (pre, mid, post); the file is written between mid
    and post.

    `replay` True makes a re-press restart from the top, as every factory sound pad does; False is
    pause and resume. `filename` must carry the source's real extension: an MP3 written as
    sound.wav plays silent.
    """
    pre = build_blank(target_idx, handle)
    mid = [
        write_record("padType", "int", 1, handle),
        write_record("padReplay", "bool", replay, handle),
        write_record("padEnvStop", "float", 1.0, handle),
        write_record("padEnvFadeOut", "float", 1.0, handle),
        write_record("padName", "str", name, handle),
        write_record("padColourIndex", "int", colour, handle),
    ]
    post = [
        build_remount_pad_storage(),
        write_record("padFilePath", "str", PAD_FILE_PATH_FMT.format(n=folder, name=filename), handle),
        write_record("padName", "str", name, handle),
    ]
    return pre, mid, post


# padMixerMode. 0 and 4 are read from the factory Censor and Ducking pads; 1 to 3 follow the order
# RODE's manual lists them in.
MIXER_MODE = {"censor": 0, "trashtalk": 1, "fade": 2, "backchannel": 3, "ducking": 4}
MIXER_MODE_LABEL = {0: "Censor", 1: "Trash Talk", 2: "Fade In/Out", 3: "Back Channel", 4: "Ducking"}


def build_mixer_create(target_idx: int, name: str, colour: int, mode: int,
                       trigger_mode: int = 0, *, handle: int):
    """A MIXER-action pad create (padType 3). Returns (pre, mid, post), post always empty. No
    effect object and no file, so it needs no FAT mount.

    `trigger_mode` is padMixerTriggerMode: 0 momentary, 1 latching (factory Censor 0, Ducking 1).
    Both modes are written explicitly, although the RODE app leaves a Censor's at the phase-1
    default of 0, so every mode takes one path. Mode-specific extras (censor sound, fade times,
    back-channel sources) stay at their phase-1 defaults.
    """
    pre = build_blank(target_idx, handle)
    mid = [
        build_remount_pad_storage(),
        write_record("padType", "int", 3, handle),
        write_record("padMixerMode", "int", mode, handle),
        write_record("padMixerTriggerMode", "int", trigger_mode, handle),
        write_record("padName", "str", name, handle),
        write_record("padColourIndex", "int", colour, handle),
    ]
    return pre, mid, []


# padTriggerType: 0 Control Change (the captured default), 1 Note.
TRIGGER_TYPE = {"cc": 0, "note": 1}
TRIGGER_TYPE_LABEL = {0: "Control Change", 1: "Note"}


def build_trigger_create(target_idx: int, name: str, colour: int, *, handle: int,
                         custom: bool = False, trigger_type: int = 0, control: int | None = None,
                         channel: int = 1, on_val: int = 127, off_val: int = 0):
    """A MIDI TRIGGER pad create (padType 4). Returns (pre, mid, post), post always empty.

    The default mapping is the firmware's own: CC on control number = padIdx, channel 1, on 127,
    off 0, all of which phase 1 already stamps, so the commit only sets padType. `custom` writes
    padTriggerCustom and the explicit mapping; `control` defaults to the padIdx.
    """
    pre = build_blank(target_idx, handle)
    mid = [
        build_remount_pad_storage(),
        write_record("padType", "int", 4, handle),
    ]
    if custom:
        mid += [
            write_record("padTriggerCustom", "bool", True, handle),
            write_record("padTriggerType", "int", trigger_type, handle),
            write_record("padTriggerControl", "int",
                         target_idx if control is None else control, handle),
            write_record("padTriggerChannel", "int", channel, handle),
            write_record("padTriggerOn", "int", on_val, handle),
            write_record("padTriggerOff", "int", off_val, handle),
        ]
    mid += [
        write_record("padName", "str", name, handle),
        write_record("padColourIndex", "int", colour, handle),
    ]
    return pre, mid, []


def _recipe_parts(recipe: dict) -> tuple[list[str], dict]:
    """(on-flags, param overrides) for a recipe of {engine: {param: value}}."""
    param_overrides: dict = {}
    onflags: list[str] = []
    for engine, params in recipe.items():
        if engine not in EFFECT_FLAG:
            raise ValueError(f"unknown effect engine {engine!r}; known: {sorted(EFFECT_FLAG)}")
        onflags.append(EFFECT_FLAG[engine])
        param_overrides.update(params or {})
    return onflags, param_overrides


def expected_effect_object(target_idx: int, recipe: dict) -> dict:
    """{property: value} for every property of the region-B object that build_fx_create_recipe
    leaves behind: the effects block in full (defaults with the recipe's params baked in) plus the
    recipe's on-flags. A create writes all twenty, so this is the whole object, not a subset."""
    onflags, overrides = _recipe_parts(recipe)
    out = {}
    for name, _kind, value in _EFFECTS:
        if name == "effectsIdx":
            value = target_idx
        elif name in overrides:
            value = overrides[name]
        out[name] = value
    for flag in onflags:
        out[flag] = True
    return out


def build_fx_create_recipe(target_idx: int, name: str, colour: int, recipe: dict, *,
                           handle: int, fx_subhandle: int, active: bool = True):
    """A VOICE-FX pad create (padType 2) for a recipe of one or more engines. Returns
    (pre, mid, post).

    `recipe` is {engine: {param: value}} with engine a key of EFFECT_FLAG; an omitted param keeps
    its default, and voiceDisguise takes none. Each engine's params go into the effects block and
    its on-flag is turned on in post, which reproduces the factory presets (Megaphone is reverb +
    echo + distortion, Monster is reverb + pitchShift at -8 semitones).

    `fx_subhandle` comes from `soundpad.free_effect_subhandle`. `active` is padActive, the
    live-engage flag: True processes the target mic at once (a preview wants that); a restored pad
    rests like a factory one, with it False.
    """
    onflags, param_overrides = _recipe_parts(recipe)
    pre = build_blank(target_idx, handle, fx_subhandle, param_overrides)
    mid = [
        write_record("padType", "int", 2, handle),
        write_record("padActive", "bool", bool(active), handle),
        write_record("padEffectTriggerMode", "int", 1, handle),
        write_record("padEnvStop", "float", 1.0, handle),
        write_record("padEnvFadeOut", "float", 1.0, handle),
        write_record("padName", "str", name, handle),
        write_record("padColourIndex", "int", colour, handle),
    ]
    post = [write_record(flag, "bool", True, fx_subhandle, FX_BLOCK) for flag in onflags]
    post.append(write_record("padName", "str", name, handle))
    return pre, mid, post
