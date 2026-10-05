"""Build the full from-code pad CREATE sequence from scratch - NO capture fossils.

This replaces the create9_records.pkl replay the first pad-create scripts used
(SOUNDPADS.md principle 5b: "build records from the target, do not replay
capture fossils").

WHY THIS EXISTS
---------------
The old path loaded 75 raw 256-byte records lifted from captures/padassign9.pcap
(the app editing GRID PAD 9 = padIdx 8) and patched a few fields. Every field we
forgot to overwrite leaked pad 9's identity. Decoding the template (decode_template.py)
showed two real leaks the old sound path never fixed:
  - padTriggerControl = 8  (rec 38) - the MIDI trigger number; tracks padIdx exactly
    (confirmed: pad 9 -> 8, pad 13 -> 12). Left fossilised, EVERY created pad would
    answer to pad 9's MIDI trigger - a real bug for MIDI/Access-Deck control.
  - effectsIdx = 8         (rec 65) - the effect-object index; also tracks padIdx.
Here every record is BUILT from the target pad's parameters at call time, so nothing
can leak. The captures remain only as the byte-layout spec and the selftest oracle.

PROTOCOL (decoded; see rcp_transfer.py for the value-block formats)
-------------------------------------------------------------------
A create is one held-open HID session:
  handoff "PAD"  (binds edit handle H)
  phase1: padType=0 blank, 44 pad properties on H (defaults; padIdx + padTrigger-
          Control = target)
  handoff "EFFECTS_PARAMETERS" (binds FX sub-handle S)
  effects: 20 FX-default properties on S (all effects off; effectsIdx = target)
  -- device emits padRCVSyncPadType here --
  phase2-A (commit): padType=1(sound)/2(fx), env, padName, padColourIndex (+ for FX:
          padActive=ON, padEffectTriggerMode=1)
  -- WAV written to the FAT volume here (sound only) --
  phase2-B: remountPadStorage, padFilePath (sound only), padName  (+ for FX: the
          chosen effect's on-flag on S)
  exit transfer mode

ADDRESS FRAMES (byte-for-byte from the capture):
  pad write : 01 01 02 02 9a 02 01 <H>  <name> 00 <valueblock>
  fx  write : 01 01 02 02 9d 02 01 <S>  <name> 00 <valueblock>
  handoff   : 03 01 01 02 <9a|9d> 02 01 <H> <keyword> 00 00 00
value blocks: int 01 05 01 <i32 LE> | float 01 09 04 <f64 LE> | bool 01 01 <02 on/03 off>
            | string 01 <vlen> 05 <bytes> 00  (vlen = 1 + len(bytes) + 1)
"""
from __future__ import annotations

import struct

BULK_LEN = 256
REPORT_BULK_OUT = 0x03

# Edit-handle registers. THE PAD HANDLE IS TABLE-SIZE-DEPENDENT - it must be a FREE
# slot just PAST the current pad-record count (the app uses count+2; see free_handle()
# and assign_empty.py). Writes on framing 0x9a address the pad at stream position =
# handle, so a handle <= the record count overwrites an EXISTING pad and corrupts it
# (a hardcoded 0x12 on a full 24-record table wrote onto stream position 18
# and destroyed system pad "Input 3"). 0x12 below is ONLY the value captured in
# padassign9.pcap, where the table had ~16 records so count+2 == 0x12; it is the default
# for the byte-for-byte selftest, NOT for live creates. Live callers MUST pass
# handle=free_handle(current pad count).
HANDLE = 0x12          # pad edit handle (framing 0x9a) - CAPTURE DEFAULT ONLY (count 16 -> 18)
# The effects sub-handle uses a DIFFERENT framing (0x9d = effects-parameter objects, not
# pad positions). Evidence it does NOT collide with pad positions: many sound+FX creates on
# the full table left every 0x9d-targeted effect (e.g. Monster at position 11) intact after
# reboot. Kept fixed; re-verify on the clean-table re-test before trusting at scale.
FX_SUBHANDLE = 0x0B    # effects sub-handle (framing 0x9d) - separate address space


def free_handle(pad_record_count: int) -> int:
    """Edit handle for a create = the FIRST FREE stream slot = current pad-record count.
    [CONFIRMED on a CLEAN table, ear-proven, with a TRUSTWORTHY read]

    The handle is the stream POSITION the new record is written to. Records occupy 0-based
    positions 0..count-1, so the first free slot is exactly `count`. The device requires
    that contiguous next slot: writing there makes it emit the padRCVSyncPadType handshake
    and APPEND the record at the stream end (where created pads live). A handle with a GAP
    (count+1, count+2, ...) is silently rejected - no sync, no commit.

    PROOF (clean factory table, count = 23 after clearing Megaphone, read trustworthy):
      handle 25 (count+2) -> saw_sync=False, record did NOT commit.
      handle 23 (count+0) -> saw_sync=True, Megaphone committed (appended at position 23,
                             padIdx 9), grid pad 10 ear-confirmed.

    THE "+2" WAS NEVER REAL. It was an artifact of the old read-framing bug (the 0x04
    report-id wedged at every 256-byte boundary) UNDERCOUNTING padName by ~2: the app's
    assign_known23.pcap handle 0x19=25 was read as "count 23" but the table truly held 25
    records, so the app ALSO used handle = count+0. The hardcoded 0x12 and the "+2 vs +1"
    contradiction were the same miscount. With the framing fixed (rcp_read strips the
    per-frame report-id) the count is trustworthy and the rule is simply count+0. This also
    explains the long-blamed "create never commits / handshake timing" symptom: it was a
    wrong handle landing on a gap, not timing.
    """
    return pad_record_count

FRAME_PAD = 0x9A
FRAME_FX = 0x9D

PAD_FILE_PATH_FMT = "/Application/emmc-data/pads/{n}/{name}"

# The six effect ENGINES the device offers and their on-flag property. A factory
# voice-FX recipe layers one or more of these, each with its own params (decoded from
# a clean-table snapshot; see fx_recipes.json / extract_recipes.py). These
# engine keys match fx_recipes.json (reverb/echo/pitchShift/distortion/robot/voiceDisguise).
EFFECT_FLAG = {
    "reverb": "reverbOn", "echo": "echoOn", "pitchShift": "pitchShiftOn",
    "distortion": "distortionOn", "robot": "robotOn", "voiceDisguise": "voiceDisguiseOn",
}

# --- value-block encoders ----------------------------------------------------

def _vb_int(value: int) -> bytes:
    return bytes([0x01, 0x05, 0x01]) + int(value).to_bytes(4, "little", signed=True)


def _vb_float(value: float) -> bytes:
    return bytes([0x01, 0x09, 0x04]) + struct.pack("<d", float(value))


def _vb_bool(on: bool) -> bytes:
    return bytes([0x01, 0x01, 0x02 if on else 0x03])


def _vb_str(value: str) -> bytes:
    body = value.encode("ascii") + b"\x00"
    return bytes([0x01, 1 + len(body), 0x05]) + body


_ENC = {"int": _vb_int, "float": _vb_float, "bool": _vb_bool, "str": _vb_str}


def _frame(rec_payload: bytes) -> bytes:
    rec = bytes([REPORT_BULK_OUT]) + len(rec_payload).to_bytes(4, "little") + rec_payload
    if len(rec) > BULK_LEN:
        raise ValueError(f"record too long ({len(rec)} > {BULK_LEN})")
    return rec + bytes(BULK_LEN - len(rec))


def write_record(name: str, kind: str, value, handle: int = HANDLE,
                 frame: int = FRAME_PAD) -> bytes:
    """One pad- or fx-addressed property write on the given handle."""
    addr = bytes([0x01, 0x01, 0x02, 0x02, frame, 0x02, 0x01, handle & 0xFF])
    payload = addr + name.encode("ascii") + b"\x00" + _ENC[kind](value)
    return _frame(payload)


def handoff_record(keyword: str, handle: int, frame: int) -> bytes:
    """The PAD / EFFECTS_PARAMETERS handle-binding record."""
    payload = (bytes([0x03, 0x01, 0x01, 0x02, frame, 0x02, 0x01, handle & 0xFF])
               + keyword.encode("ascii") + bytes([0x00, 0x00, 0x00]))
    return _frame(payload)


def global_record(name: str, kind: str, value, selector: int) -> bytes:
    """A global property (01 01 01 01 <selector> ...)."""
    payload = bytes([0x01, 0x01, 0x01, 0x01, selector & 0xFF]) + name.encode("ascii") + b"\x00" + _ENC[kind](value)
    return _frame(payload)


# remountPadStorage as the app sends it (a compact bool ON with a trailing pad byte);
# kept verbatim because its value block is non-standard (01 01 02 00).
REMOUNT_PAD_STORAGE = bytes.fromhex(
    "031a000000010101010f72656d6f756e7450616453746f726167650001010200"
).ljust(BULK_LEN, b"\x00")


# --- the blank-pad PHASE1 spec (44 records on the pad handle) -----------------
# All values are the app's defaults EXCEPT padTriggerControl and padIdx, which are
# patched to the target padIdx by build_phase1().
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
    ("padTriggerControl", "int", None),   # := target padIdx (was fossil 8)
    ("padTriggerChannel", "int", 1),
    ("padTriggerOn", "int", 127),
    ("padTriggerOff", "int", 0),
    ("padIsInternal", "bool", False),
    ("padGain", "float", -12.0),
    ("padIdx", "int", None),              # := target padIdx
]

# --- the EFFECTS_PARAMETERS defaults (20 records on the fx sub-handle) ---------
# Full-precision doubles extracted from the capture; effectsIdx := target padIdx.
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
    ("effectsIdx", "int", None),          # := target padIdx
]


def build_phase1(target_idx: int, handle: int = HANDLE) -> list[bytes]:
    out = []
    for name, kind, value in _PHASE1:
        if name in ("padTriggerControl", "padIdx"):
            value = target_idx
        out.append(write_record(name, kind, value, handle, FRAME_PAD))
    return out


def build_effects(target_idx: int, fx_subhandle: int = FX_SUBHANDLE,
                  param_overrides: dict | None = None) -> list[bytes]:
    """The 20-record EFFECTS_PARAMETERS block. `param_overrides` (name->value) replaces
    individual float params so the block carries a recipe's tuned values instead of the
    factory defaults. On-flags are NOT touched here - they are turned on in the commit
    `post` (the proven order; see build_fx_create_recipe)."""
    overrides = param_overrides or {}
    out = []
    for name, kind, value in _EFFECTS:
        if name == "effectsIdx":
            value = target_idx
        elif name in overrides:
            value = overrides[name]
        out.append(write_record(name, kind, value, fx_subhandle, FRAME_FX))
    return out


def build_blank(target_idx: int, handle: int = HANDLE,
                fx_subhandle: int = FX_SUBHANDLE,
                param_overrides: dict | None = None,
                with_effects: bool = True) -> list[bytes]:
    """pre = handoff PAD + phase1(44) [+ handoff EFFECTS + effects(20)]. 66 records with the
    effects block, 45 without.

    `with_effects` controls the REGION-B (0x9d) part. FX pads MUST keep it (the effect
    definition is the whole point). SOUND pads must NOT write region B at all (SOUNDPADS
    section 2: a sound pad owns no region-B object; writing one orphans the pool), so the
    sound path passes with_effects=False. The effects block is the ONLY 0x9d traffic in a
    create, so dropping it removes every region-B write.
    `handle` MUST be a free slot for live creates - pass free_handle(pad count).
    `param_overrides` lets an FX recipe bake tuned param values into the effects block.
    """
    recs = [handoff_record("PAD", handle, FRAME_PAD)] + build_phase1(target_idx, handle)
    if with_effects:
        recs += ([handoff_record("EFFECTS_PARAMETERS", fx_subhandle, FRAME_FX)]
                 + build_effects(target_idx, fx_subhandle, param_overrides))
    return recs


def build_sound_create(target_idx: int, folder: int, name: str, colour: int,
                       handle: int = HANDLE, fx_subhandle: int = FX_SUBHANDLE,
                       with_effects: bool = False, replay: bool = True,
                       filename: str = "sound.wav"):
    """Full SOUND create, built from scratch. Returns (pre, mid, post).

    mid is sent after the device sync; the WAV is written between mid and post.
    `handle` MUST be free_handle(current pad count) for a live create.

    with_effects DEFAULTS FALSE: a SOUND pad must write NOTHING to region B (SOUNDPADS section 2).
    Defaulting it True emits a 20-record EFFECTS block on a 0x9d sub-handle, which appends a
    region-B object the pad does not need and our clear cannot remove: a permanent orphan. With
    with_effects=False the create sends ZERO 0x9d
    writes, so it cannot orphan the pool. `fx_subhandle` is then unused. Pass with_effects=True
    only to reproduce the old byte sequence (e.g. comparing against padassign9.pcap).
    """
    pre = build_blank(target_idx, handle, fx_subhandle, with_effects=with_effects)
    mid = [
        write_record("padType", "int", 1, handle, FRAME_PAD),
        # padReplay: True (default) = re-press RESTARTS from the start (factory sound-pad
        # behaviour); False = pause/resume. phase1 default is False, so we set it explicitly
        # here. ALL factory SOUND pads carry True; FX/mixer pads carry False. Per-pad in the UI.
        write_record("padReplay", "bool", replay, handle, FRAME_PAD),
        write_record("padEnvStop", "float", 1.0, handle, FRAME_PAD),
        write_record("padEnvFadeOut", "float", 1.0, handle, FRAME_PAD),
        write_record("padName", "str", name, handle, FRAME_PAD),
        write_record("padColourIndex", "int", colour, handle, FRAME_PAD),
    ]
    # filename defaults to sound.wav; pass sound.mp3 for an MP3 source so the firmware
    # player sees the right extension (an MP3 written as sound.wav plays silent).
    final_path = PAD_FILE_PATH_FMT.format(n=folder, name=filename)
    post = [
        REMOUNT_PAD_STORAGE,
        write_record("padFilePath", "str", final_path, handle, FRAME_PAD),
        write_record("padName", "str", name, handle, FRAME_PAD),
    ]
    return pre, mid, post


# Mixer-action modes (padMixerMode). 0/4 confirmed from the factory Censor/Ducking pads;
# 1-3 follow the RODE manual's listed order (Censor, Trash Talk, Fade, Back Channel, Ducking).
MIXER_MODE = {"censor": 0, "trashtalk": 1, "fade": 2, "backchannel": 3, "ducking": 4}
MIXER_MODE_LABEL = {0: "Censor", 1: "Trash Talk", 2: "Fade In/Out", 3: "Back Channel", 4: "Ducking"}


def build_mixer_create(target_idx: int, name: str, colour: int, mode: int,
                       trigger_mode: int = 0, handle: int = HANDLE,
                       fx_subhandle: int = FX_SUBHANDLE):
    """Full MIXER-action pad create (padType=3), built from scratch. Returns (pre, mid, post).

    Decoded from captures/sp_mixer.pcap: the create is the same two-phase pattern
    as a sound pad MINUS the region-B/EFFECTS block and MINUS any file. A mixer pad owns NO
    region-B object (confirmed: factory Censor/Ducking carry none), so build_blank(with_effects=
    False) is reused and the create writes ZERO 0x9d records - pool-safe by construction, it
    cannot orphan. No file means no FAT mount needed either (HID-only, silent).

    `mode` = padMixerMode: 0 Censor, 1 Trash Talk, 2 Fade In/Out, 3 Back Channel, 4 Ducking.
    `trigger_mode` = padMixerTriggerMode (0 Momentary, 1 Latching; factory Censor=0, Ducking=1).
    The app's captured Censor left padMixerMode/TriggerMode at the phase-1 default (0); we set
    both explicitly in the commit so every mode uses one code path (writing 0 is a no-op for
    Censor, so the read-back still matches the factory pad). Mode-specific extras (censor custom
    sound, fade seconds, back-channel sources) are left at defaults here - added per mode later.
    """
    pre = build_blank(target_idx, handle, fx_subhandle, with_effects=False)
    mid = [
        REMOUNT_PAD_STORAGE,
        write_record("padType", "int", 3, handle, FRAME_PAD),
        write_record("padMixerMode", "int", mode, handle, FRAME_PAD),
        write_record("padMixerTriggerMode", "int", trigger_mode, handle, FRAME_PAD),
        write_record("padName", "str", name, handle, FRAME_PAD),
        write_record("padColourIndex", "int", colour, handle, FRAME_PAD),
    ]
    return pre, mid, []


# MIDI Trigger message types (padTriggerType). 0 = Control Change (CC), 1 = Note (per the manual's
# Custom dialog: "Control Change [CC] or Note"). 0 confirmed from the captured default trigger.
TRIGGER_TYPE = {"cc": 0, "note": 1}
TRIGGER_TYPE_LABEL = {0: "Control Change", 1: "Note"}


def build_trigger_create(target_idx: int, name: str, colour: int,
                         handle: int = HANDLE, fx_subhandle: int = FX_SUBHANDLE,
                         custom: bool = False, trigger_type: int = 0, control: int | None = None,
                         channel: int = 1, on_val: int = 127, off_val: int = 0):
    """Full MIDI TRIGGER pad create (padType=4), built from scratch. Returns (pre, mid, post).

    Decoded from captures/sp_midi.pcap: identical pattern to the Mixer create - the
    universal template (build_blank, NO region B, NO file) then a commit. A Trigger pad owns no
    region-B object, so this is pool-safe by construction and HID-only (silent).

    DEFAULT mapping (custom=False, the captured case): the firmware's per-pad default is a CC on
    control number = padIdx, channel 1, value On 127 / Off 0 (the manual: pad 1 bank 1 = CC #0,
    counting up). build_blank already stamps padTriggerControl = target_idx and the channel/on/off
    defaults, so the commit only needs to set padType=4. So the captured default Trigger commit was
    just remount + padType=4 + name + colour (records 45/46/48/49; 47/50 in the pcap are junk frames).

    CUSTOM mapping (custom=True): writes padTriggerCustom=True plus the explicit type/control/
    channel/on/off in the commit. `trigger_type` 0=CC, 1=Note. `control` defaults to target_idx.
    """
    pre = build_blank(target_idx, handle, fx_subhandle, with_effects=False)
    mid = [
        REMOUNT_PAD_STORAGE,
        write_record("padType", "int", 4, handle, FRAME_PAD),
    ]
    if custom:
        if control is None:
            control = target_idx
        mid += [
            write_record("padTriggerCustom", "bool", True, handle, FRAME_PAD),
            write_record("padTriggerType", "int", trigger_type, handle, FRAME_PAD),
            write_record("padTriggerControl", "int", control, handle, FRAME_PAD),
            write_record("padTriggerChannel", "int", channel, handle, FRAME_PAD),
            write_record("padTriggerOn", "int", on_val, handle, FRAME_PAD),
            write_record("padTriggerOff", "int", off_val, handle, FRAME_PAD),
        ]
    mid += [
        write_record("padName", "str", name, handle, FRAME_PAD),
        write_record("padColourIndex", "int", colour, handle, FRAME_PAD),
    ]
    return pre, mid, []


def build_fx_create_recipe(target_idx: int, name: str, colour: int, recipe: dict,
                           handle: int = HANDLE, fx_subhandle: int = FX_SUBHANDLE,
                           active: bool = True):
    """Full VOICE-FX create for a MULTI-EFFECT recipe. Returns (pre, mid, post).

    `recipe` = {engine: {param: value, ...}, ...} where engine is one of EFFECT_FLAG
    (reverb/echo/pitchShift/distortion/robot/voiceDisguise) and the params are the
    effect's tuned values (omit a param to keep its factory default). Every engine in
    the recipe gets its on-flag turned ON in post; its params are baked into the effects
    block. This reproduces the device's factory presets exactly (e.g. Megaphone =
    reverb+echo+distortion, Monster = reverb+pitchShift -8 semitones).

    `handle` MUST be free_handle(pad count) and `fx_subhandle` free_effect_subhandle()
    for a live create (SOUNDPADS section 0). voiceDisguise takes no params ({}).
    """
    param_overrides: dict = {}
    onflags: list[str] = []
    for engine, params in recipe.items():
        if engine not in EFFECT_FLAG:
            raise ValueError(f"unknown effect engine {engine!r}; known: {sorted(EFFECT_FLAG)}")
        onflags.append(EFFECT_FLAG[engine])
        param_overrides.update(params or {})
    pre = build_blank(target_idx, handle, fx_subhandle, param_overrides)
    # padActive is the LIVE-ENGAGE flag: True makes the effect process the target mic immediately
    # (the live preview wants that), but a freshly assigned/restored pad should REST like the
    # factory (padActive False on every clean-snapshot pad) so it does not leave a mic processing.
    mid = [
        write_record("padType", "int", 2, handle, FRAME_PAD),
        write_record("padActive", "bool", bool(active), handle, FRAME_PAD),
        write_record("padEffectTriggerMode", "int", 1, handle, FRAME_PAD),
        write_record("padEnvStop", "float", 1.0, handle, FRAME_PAD),
        write_record("padEnvFadeOut", "float", 1.0, handle, FRAME_PAD),
        write_record("padName", "str", name, handle, FRAME_PAD),
        write_record("padColourIndex", "int", colour, handle, FRAME_PAD),
    ]
    post = [write_record(flag, "bool", True, fx_subhandle, FRAME_FX) for flag in onflags]
    post.append(write_record("padName", "str", name, handle, FRAME_PAD))
    return pre, mid, post


# --- selftest: prove the builders reproduce the app capture byte-for-byte ------

def _selftest() -> None:
    import pickle
    recs = [bytes(r) for r in pickle.load(open("create9_records.pkl", "rb"))]
    assert len(recs) == 75, f"template has {len(recs)} records"

    # Rebuild create9 with the capture's exact parameters: padIdx 8, folder 9,
    # colour 8, placeholder padName 'Sound 9', final padName 'Chimes'. The capture
    # used the SAME value for padTriggerControl/effectsIdx as padIdx (8), so a
    # faithful from-scratch build must match all 75 records. The default handle 0x12
    # is correct here because padassign9 was captured at 16 pad records
    # (free_handle(16) == 18 == 0x12); LIVE creates pass free_handle(current count).
    blank = build_blank(8)                      # recs 0..65
    for i, (got, exp) in enumerate(zip(blank, recs[:66])):
        assert got == exp, f"blank rec {i} mismatch\n exp {exp[:48].hex()}\n got {got[:48].hex()}"

    # phase2-A (recs 66..70): the capture's placeholder name here is 'Sound 9'.
    mid = [
        write_record("padType", "int", 1),
        write_record("padEnvStop", "float", 1.0),
        write_record("padEnvFadeOut", "float", 1.0),
        write_record("padName", "str", "Sound 9"),
        write_record("padColourIndex", "int", 8),
    ]
    for i, (got, exp) in enumerate(zip(mid, recs[66:71])):
        assert got == exp, f"mid rec {66+i} mismatch\n exp {exp[:48].hex()}\n got {got[:48].hex()}"

    # phase2-B (recs 71..73): remount, padFilePath pads/9, final name 'Chimes'.
    assert REMOUNT_PAD_STORAGE == recs[71], "remount mismatch"
    assert write_record("padFilePath", "str",
                        PAD_FILE_PATH_FMT.format(n=9, name="sound.wav")) == recs[72], "padFilePath mismatch"
    assert write_record("padName", "str", "Chimes") == recs[73], "final padName mismatch"

    # rec 74 = exit transfer mode (we emit it via set_transfer_mode normally).
    assert global_record("transferModeType", "int", 0, 0x0F) == recs[74], "exit mismatch"

    # And prove the leak is FIXED: a build for a different pad re-stamps the
    # trigger number (the old sound path left it fossilised at 8).
    trig = next(r for r in build_phase1(12) if b"padTriggerControl" in r)
    k = trig.find(b"padTriggerControl") + len("padTriggerControl") + 1
    assert trig[k:k + 3] == bytes([0x01, 0x05, 0x01]) and \
        int.from_bytes(trig[k + 3:k + 7], "little") == 12, \
        "padTriggerControl did not track target padIdx"
    eff = next(r for r in build_effects(12) if b"effectsIdx" in r)
    j = eff.find(b"effectsIdx") + len("effectsIdx") + 1
    assert int.from_bytes(eff[j + 3:j + 7], "little") == 12, "effectsIdx did not track target padIdx"

    print("selftest OK: from-scratch builders reproduce create9_records.pkl byte-for-byte "
          "(all 75 records), and padTriggerControl/effectsIdx now track the target pad.")


def _selftest_mixer() -> None:
    """Verify build_mixer_create reproduces the app's captured Censor create byte-for-byte.

    mixer_censor_capture.pkl = the OUT report-3 records from captures/sp_mixer.pcap (the app
    creating a default Censor on padIdx 20, handle 0x19, name 'Mixer21', colour 8), minus the
    leading select. Layout: [0:45] handoff PAD + 44 phase1, [45] remount, [46] padType=3,
    [47] padName, [48] padColourIndex. Our build sets padMixerMode/TriggerMode explicitly (the
    app left them at the phase-1 default 0), so those two records are extra but must decode to 0.
    """
    import pickle
    cap = [bytes(r) for r in pickle.load(open("mixer_censor_capture.pkl", "rb"))]
    assert len(cap) == 49, f"capture has {len(cap)} records"
    pre, mid, post = build_mixer_create(20, "Mixer21", 8, mode=0, trigger_mode=0, handle=0x19)
    assert post == [], "mixer create should have no post records"
    # phase 1: byte-for-byte against the capture
    assert len(pre) == 45, f"pre has {len(pre)} records"
    for i, (got, exp) in enumerate(zip(pre, cap[:45])):
        assert got == exp, f"mixer pre rec {i} mismatch\n exp {exp[:48].hex()}\n got {got[:48].hex()}"
    # commit: remount, padType, (padMixerMode, padMixerTriggerMode = ours), padName, padColourIndex
    assert mid[0] == cap[45], "remount mismatch"
    assert mid[1] == cap[46], "padType=3 mismatch"
    assert mid[4] == cap[47], "padName mismatch"
    assert mid[5] == cap[48], "padColourIndex mismatch"
    # the two explicit-mode records decode to 0 (Censor / Latching)
    for rec, nm in ((mid[2], "padMixerMode"), (mid[3], "padMixerTriggerMode")):
        d = rec.find(nm.encode()) + len(nm) + 1
        assert rec[d:d + 3] == bytes([0x01, 0x05, 0x01]) and \
            int.from_bytes(rec[d + 3:d + 7], "little") == 0, f"{nm} record malformed"
    # a Ducking build re-stamps mode/trigger to 4/1 and otherwise matches
    _, dmid, _ = build_mixer_create(20, "Ducking", 3, mode=4, trigger_mode=1, handle=0x19)
    dm = dmid[2].find(b"padMixerMode") + len("padMixerMode") + 1
    dt = dmid[3].find(b"padMixerTriggerMode") + len("padMixerTriggerMode") + 1
    assert int.from_bytes(dmid[2][dm + 3:dm + 7], "little") == 4, "Ducking padMixerMode != 4"
    assert int.from_bytes(dmid[3][dt + 3:dt + 7], "little") == 1, "Ducking padMixerTriggerMode != 1"
    print("mixer selftest OK: build_mixer_create reproduces the app's Censor create byte-for-byte "
          "(phase1 + commit), padMixerMode/TriggerMode encode correctly, Ducking sets mode=4 trigger=1.")


def _selftest_trigger() -> None:
    """Verify build_trigger_create reproduces the app's captured default Trigger byte-for-byte.

    sp_midi_capture.pkl = the create records from captures/sp_midi.pcap (default MIDI Trigger on
    padIdx 17, handle 0x1a, name 'Trigger18', colour 5). Layout: [0:45] handoff PAD + 44 phase1,
    [45] remount, [46] padType=4, [48] padName, [49] padColourIndex. Records 47 and 50 are junk
    bulk frames (rid 0x04, 3840 bytes), not config records, so they are skipped.
    """
    import pickle
    cap = [bytes(r) for r in pickle.load(open("sp_midi_capture.pkl", "rb"))]
    pre, mid, post = build_trigger_create(17, "Trigger18", 5, handle=0x1a)  # default mapping
    assert post == [], "trigger create should have no post records"
    assert len(pre) == 45, f"pre has {len(pre)} records"
    for i, (got, exp) in enumerate(zip(pre, cap[:45])):
        assert got == exp, f"trigger pre rec {i} mismatch\n exp {exp[:48].hex()}\n got {got[:48].hex()}"
    assert mid[0] == cap[45], "remount mismatch"
    assert mid[1] == cap[46], "padType=4 mismatch"
    assert mid[2] == cap[48], "padName mismatch"
    assert mid[3] == cap[49], "padColourIndex mismatch"
    # a custom-mapping build adds the trigger params (Note on control 60, channel 2)
    _, cmid, _ = build_trigger_create(17, "Cust", 5, handle=0x1a, custom=True,
                                      trigger_type=1, control=60, channel=2, on_val=100, off_val=0)
    names = [r[8:].split(b"\x00", 1)[0] for r in cmid]
    for need in (b"padTriggerCustom", b"padTriggerType", b"padTriggerControl", b"padTriggerChannel"):
        assert any(need in r for r in cmid), f"custom trigger missing {need.decode()}"
    print("trigger selftest OK: build_trigger_create reproduces the app's default Trigger create "
          "byte-for-byte (phase1 + commit padType=4), and custom mapping adds the padTrigger* records.")


if __name__ == "__main__":
    _selftest()
    _selftest_mixer()
    _selftest_trigger()
