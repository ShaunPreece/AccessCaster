# Soundpads & Voice-FX - Single Source of Truth

Authoritative reference for the soundpad / voice-FX WORKFLOW, architecture, and pool hygiene.
Updated 2026-06-20 (trimmed 2026-07-09: raw byte frames + value-block encodings are NOT repeated
here - they live in `RODECASTER_PROTOCOL.txt` sections 2.3 and 7.9-7.11; this doc keeps the mental
model, methods, pool rules, and key files). Tags: [CONFIRMED] ear/read-back proven; [DECODED]
understood from captures.

================================================================================
## 0. STATUS (2026-06-20) - the core problem is SOLVED

READ, CREATE, CLEAR, REASSIGN, writing exact factory recipes, and IN-PLACE realtime effect
editing all work and are CONFIRMED CLEAN on a clean table.

The long-running "creating/clearing a pad knocks out other pads' live FX until a reboot" problem
is RESOLVED. It was **orphan effect-object accumulation**, not device behaviour and not our final
code (section 5). On a clean table our clear+create stays at exactly one effect-object per pad
through repeated cycles - no orphan, no neighbour disturbance, no reboot (ear-proven + read-back).

THE POOL: one region-B object per FX pad. Our tools keep it that way automatically - the hardened
create can never leave a duplicate, and clear/create self-heal any stray orphan. If the pool is ever
polluted (e.g. a crash mid-write), clean_pool removes the orphans app-free, no factory reset (section 5).

================================================================================
## 1. FACTORY VOICE-FX RECIPES  [CONFIRMED - data in fx_recipes.json]

Real MULTI-EFFECT recipes (the app's own FX editor can't reproduce them - its "Megaphone" is
distortion only). Validated 2026-06-14 against a fresh factory show load (exact match).
extract_recipes.py regenerates fx_recipes.json from any clean model snapshot.

  Pad            grid  colour  recipe
  Voice Disguise   9     8     voiceDisguise
  Megaphone       10     9     reverb(mix .5, lowCut .3, highCut 0, model .4)
                              + echo(lowCut .4, highCut .5, delay .1, mix .1, decay .1)
                              + distortion(intensity .7)
  Large Robot     11    10     robot(mix .667, level 0)
  Pitch Up        13     0     pitchShift(+7 semitones)
  Monster         14     1     reverb(mix .7, lowCut .2, highCut .5, model .8) + pitchShift(-8)
  Small Robot     15     2     robot(mix 0, level 0)

(Censor grid 12 / Ducking grid 16 = padType 3 special pads. Bank 1 grids 1-8 = factory SOUND pads.
System/SMART pads padIdx 56-63 = padType 6.)

================================================================================
## 1b. PAD TYPES + the app's 8x8 grid  [CONFIRMED by capture 2026-06-15]

The whole pad section = "SMART pads": 8 pads x 8 banks = 64 slots (padIdx 0-63, grid N = padIdx N-1).
The app exposes ALL banks, incl. the empty middle banks 3-7 (padIdx 16-55) - nothing is locked.
padType enumeration (capture-confirmed, app type tabs Sound/FX/Mixer/MIDI/QuickDial):
  1 = Sound | 2 = Effects (FX, voice) | 3 = Mixer | 4 = MIDI (Trigger) | 5 = QuickDial
  6 = factory system/function pads (Input/Scene/Auto/Cut at 56-63) - not a user-assignable type.
Mixer (padType 3) subtypes via padMixerMode: Censor, Trash Talk, Fade In/Out, Back Channel, Ducking.
MIDI/Trigger (4): padTrigger* (default CC on Control = padIdx, Channel 1, On 127, Off 0);
  READ + WRITE + MIDI-PROVEN 2026-08-27, see the MIDI TRIGGER section below.
QuickDial (5): padSIPCallSlot / padSIPPhoneBookEntry (dials a CallMe slot / phonebook entry).
Base records for types 3/4/5 captured: captures/sp_mixer.pcap, sp_midi.pcap, sp_quickdial.pcap.

ORPHAN MECHANISM (new, 2026-06-15): the app starts every new pad ("+") as SOUND, then a type tab
converts it. ONLY FX pads (padType 2) legitimately own a region-B effect-object. Creating a pad
that ends as a NON-FX type (Mixer/MIDI/QuickDial) and clearing it in the app LEAVES an empty
region-B orphan (the app's clear drops the pad record, not the block). Proven: 4 such pads -> +4
orphans (region-B 7 -> 11). We can now REMOVE such an orphan app-free (section 5), and our hardened
create scrubs the slot first so it never doubles up. DESIGN RULE for our own app: write the FINAL
padType in one record (never Sound-then-convert) and allocate a region-B block ONLY for FX pads, so
we never orphan in the first place.

MIC PROCESSING PRESETS (channel strip, not pads, captured same session): Neutral / Podcast Studio /
Broadcast. channelCurrentFxPreset index = 0 / 1 / 2. Each writes (to channel selector 0x1c..0x1f =
Mic1..4): eqLow/Mid/HighGain, aphexBBDrive, aphexAEMix, compressorThreshold+Gain, deesserThreshold,
noiseGateThreshold+Range+Release, channelDepth/Sparkle/Punch. Values in cap_control/preset_values.json.

================================================================================
## 1c. FILE FORMATS + ON-DEVICE FILENAMES  [CONFIRMED - ear-proven 2026-06-17]

FORMAT TOLERANCE (format-tolerance battery, fmt_test/, each clip speaks its own format; created
raw via clear+create onto throwaway bank-3 pads, Shaun ear-confirmed each):
  PLAYS NATIVELY, NO CONVERSION NEEDED:
    WAV 48k/16/stereo, 48k/24/stereo, 44.1k/16/stereo, 96k/24/stereo, 192k/24/stereo,
        22k/16/MONO, 48k/32-bit FLOAT/stereo  -> ALL play correctly at normal speed.
    MP3 (44.1k and 48k) -> plays, BUT ONLY when stored with a .mp3 extension (see below).
  CONCLUSION: the pad player accepts every WAV rate (22k-192k), depth (16/24/32-float) and channel
  count we tried, plus MP3. The old "force 48k / mono / 16-bit" conversion was needless and degraded
  quality. POLICY (decided 2026-06-17): write the uploaded file AS-IS, no resample/downmix/bit-depth
  change; only require it to be .wav or .mp3 (the two formats the RODE manual lists). wav_tools
  conversion stays OFF. server.action_add_sound validates the extension and writes raw.

ON-DEVICE FILENAME = the REAL name, matching the FACTORY (confirmed by clean-table padFilePath reads):
  factory pads are pads/<n>/Music_Bed.wav, Applause.wav, Crickets.wav, ... (full path
  /Application/emmc-data/pads/<n>/<Name>.<ext>) - NOT a generic "sound.wav". We now mirror that:
  soundpad.safe_dest_filename(src, dest_name) derives a FAT-safe real filename (letters/digits/space/
  underscore/hyphen/parens, whitespace collapsed, <=40 chars, extension forced to .wav/.mp3).
  PROVEN 2026-06-17: created pads/17/"Promo Bed.wav" (a SPACE in the name) -> read-back exact, played
  fine. So spaces and real names are safe. *** MP3 RULE: an MP3 written under a .wav name plays SILENT
  (the firmware trusts the extension); it MUST be stored as .mp3. *** The web threads the upload
  filename through create_sound(dest_filename=...) -> assign_create -> build_sound_create(filename=...);
  the low-level builder still DEFAULTS to "sound.wav" so the byte-for-byte selftest is unchanged.

VOLUME SAFETY (2026-06-17): rcp_transfer.find_pad_volume now requires a GENUINELY mounted volume
  (shutil.disk_usage(root).total > 0), not just isdir, matching write_pad_raw. This closes the EINVAL
  size-0-phantom bug that truncated Music_Bed via the old overwrite path: a create now either gets a
  real volume or aborts BEFORE sending any pad record. assign_create's file copy also retries on EINVAL.

================================================================================
## 1d. CREATING THE OTHER PAD TYPES - Mixer, MIDI Trigger, Quick Dial  [decoded 2026-06-17]

All three non-sound/FX types use the SAME create as a sound pad MINUS the region-B/EFFECTS block
and MINUS any file: the universal blank-pad template (build_blank with_effects=False) + a commit
that sets the final padType (+ any type-specific fields) + padName + padColourIndex. NONE of them
own a region-B object (confirmed: factory Censor/Ducking carry none), so all are POOL-SAFE BY
CONSTRUCTION (zero 0x9d writes, cannot orphan) and HID-only (no file -> no FAT mount -> silent).
build_blank reproduces each capture's phase-1 BYTE-FOR-BYTE (verified for all three pcaps).

MIXER (padType 3)  [BUILT + ear-proven: Censor, Ducking]  captures/sp_mixer.pcap
  commit = remount + padType=3 + padMixerMode + padMixerTriggerMode + padName + padColourIndex.
  padMixerMode: 0 Censor, 1 Trash Talk, 2 Fade In/Out, 3 Back Channel, 4 Ducking (0/4 confirmed
  from factory pads, 1-3 from the manual order). padMixerTriggerMode = Latching(0)/Momentary(1).
  Censor + Ducking created from our code, read-back matched the factory pads on EVERY behaviour
  field, and BOTH fired correctly by ear (Shaun). pad_records.build_mixer_create / soundpad.create_mixer
  (+ CLI `soundpad.py mixer <grid> <mode>`). create_mixer accepts all 5 modes today; Fade/BackChannel
  extra params (padMixerFadeIn/OutSeconds, padMixerFadeExcludeHost, padMixerBackChannel*) default to
  the template values - a created Fade/BackChannel pad is valid but its fine-tuning needs those props
  set (a future param-edit path, same idea as edit_effect_live for FX). The captured Censor left
  padPlayMode=0 (factory pads show 1); padPlayMode is the sound-pad Toggle/OneShot/Hold setting and is
  irrelevant to a mixer action (mixer uses padMixerTriggerMode), so we leave it at the app's value.

TRIM AND FADE ENVELOPE (padEnv*, sound pads)  [EAR-PROVEN 2026-08-27]
  FOUR POSITIONS ALONG THE SOUND, each a 0.0-1.0 fraction: play from `padEnvStart` to `padEnvStop`,
  fading UP until `padEnvFadeIn` and DOWN from `padEnvFadeOut`. NOT seconds, and the fade fields are
  absolute positions, not fade lengths.
  How it was settled, on a 44-second Music Bed copied to a spare pad (the length is the point: at 44
  seconds the two candidate meanings cannot be confused):
    start 0.5               -> playback began about halfway in, not half a second in.
    start 0.5, fadeIn 0.25  -> NO fade at all, full level from halfway to the end. So fadeIn is an
                               absolute position: it had already "finished" before playback began.
    fadeOut 0.1, stop 0.2   -> started at the beginning, faded down early and stopped. So fadeOut is
                               where the fade-down BEGINS and stop is where playback ENDS.
  That also explains the create default exactly (0.0 / 0.0 / 1.0 / 1.0 = play it all, both fade
  points sitting on the ends so neither does anything), which is what every factory sound pad reads.
  A fade point OUTSIDE the played region is accepted and silently does nothing, so the editor warns
  (soundpad.envelope_warning). The UI unit is PERCENT, because the desk never reports a file's
  duration, so seconds could not be shown honestly.
  Read + write: soundpad.ENV_FIELD / ENV_EDIT / set_envelope_percent, POST /api/pad/envelope.

FADE + CENSOR MIXER SUB-SETTINGS  [device-proven 2026-08-27]
  padMixerFadeInSeconds / OutSeconds (seconds, no proven ceiling - the app clamps at 60 as a sanity
  bound, not a device limit) and padMixerFadeExcludeHost, on padMixerMode 2 pads;
  padMixerCensorCustom + padMixerCensorFilePath on mode 0. Written and read back on a Fade pad
  created for the test (2.5 / 4.0 / True) and on the factory Censor pad (flag on, then off, clean).
  The censor FILE PATH is read and backed up but never written by us: pointing a pad at audio that
  may not be on the unit is worse than leaving it, and putting the file there is an upload job.
  soundpad.FADE_FIELD / CENSOR_FIELD / PAD_FLAG, POST /api/pad/fade and /api/pad/flag.

MIDI TRIGGER (padType 4)  [BUILT + create device-proven + MIDI OUTPUT PROVEN 2026-08-27]

  >>> WHAT A TRIGGER PAD ACTUALLY SENDS, heard on the wire 2026-08-27 (DevTools/midi_monitor.py on
  the desk's own "MIDI function 0" input port, Shaun pressing the pad). This closes the REMAINING
  item that stood here since 2026-06-17. <<<
    - The pad DOES transmit, and a custom mapping is honoured exactly: with type=Note, control=60,
      channel=2, on=100 the monitor logged `91 3c 64` / `81 3c 00`, i.e. Note On 60 velocity 100 on
      channel 2, then a real 0x80 Note Off. One message per event, no duplicates (an earlier
      "doubling" was two monitor processes writing one log, not the desk).
    - **padTriggerMode IS LATCHING vs MOMENTARY**, which was previously an unknown field (and PARITY
      wrongly filed it next to padPlayMode):
        mode 1 (what a created pad is left on) = LATCHING: press sends the ON value, the NEXT press
          sends the OFF value. Measured: Note On at 18:30:57, Note Off at 18:31:09, two presses.
        mode 0 = MOMENTARY: ON at press, OFF at release. Measured: On at 18:35:50.833, Off at
          18:35:53.138, one press held about two seconds.
      NOTE THE TRAP: a MIXER pad's padMixerTriggerMode is the OPPOSITE way round (0 Latching,
      1 Momentary). Same two words, inverted values. soundpad.TRIGGER_MODE_LABEL vs
      MIXER_TRIGGER_LABEL; never share a value between them.
    - **padTriggerCustom = False IS DESTRUCTIVE.** Writing that one flag made the DESK reset
      padTriggerControl/Channel/On/Off to its defaults (60 -> 16, ch 2 -> 1, on 100 -> 127), and
      writing the flag back to True did NOT return the custom values. So "off" discards a mapping
      rather than parking it, and the UI says so. padTriggerType did NOT reset (it stayed on Note),
      so a pad with custom off can hold a type its default mapping would not use; what such a pad
      really sends is UNPROVEN, which is why the page describes a default pad from its POSITION
      rather than from that field.
    - padTriggerSend (stamped 2 by the template) is still unexplained. Read and backed up, never
      written back, no control offered.
  READ + WRITE PATH (2026-08-27): soundpad.TRIGGER_FIELD / TRIGGER_EDIT (one spec drives the read,
  the payload, the browser controls, the write validation and the profile), read_pads_live's per-pad
  "trigger" dict, soundpad.set_trigger_mapping, POST /api/pad/trigger {pad, field, value}, and the
  "What this pad sends" fieldset on the pad editor's Settings tab. All eight padTrigger* properties
  are now in a profile (they were 8 of the 20 listed gaps in the profile-coverage report).
  Live test: DevTools/test_pad_trigger_live.py (create, read the default, write a custom mapping
  field by field, read every one back, flip the mode, then clear and check the pool).

  captures/sp_midi.pcap. commit = remount + padType=4 + padName + padColourIndex for the DEFAULT
  mapping. The firmware's per-pad default is a CC on control number = padIdx, channel 1, On 127 /
  Off 0 (manual: pad1/bank1 = CC#0 counting up); build_blank already stamps padTriggerControl=padIdx
  + the channel/on/off defaults, so the default commit sets only padType=4 (records 45/46/48/49 in
  the pcap; 47/50 are junk bulk frames). CUSTOM mapping adds padTriggerCustom=True + padTriggerType
  (0 CC / 1 Note) + padTriggerControl/Channel/On/Off. pad_records.build_trigger_create /
  soundpad.create_trigger (+ CLI `soundpad.py trigger <grid>`). Device-proven 2026-06-17: created on
  grid 19, read-back padType=4 + control=18 (=padIdx) + ch1/on127/off0/CC, pool clean=7, cleared
  clean. The 2026-06-17 "REMAINING: press the pad with a MIDI monitor" item is now DONE (above).

QUICK DIAL (padType 5)  [DECODED only - OUT OF SCOPE (SIP calling)]  captures/sp_quickdial.pcap
  commit = remount + padType=5 + padName + padColourIndex; the SIP target rides padSIPCallSlot /
  padSIPPhoneBookEntry (template defaults 0 = the captured pad). Same template/pattern as the others,
  so it is trivially buildable if SIP/CallMe ever comes into scope; no tooling built (SIP is parked).

================================================================================
## 2. ADDRESSING  [CONFIRMED]

PAD writes (frame 0x9a):
- CREATE on an empty slot: bind a fresh edit handle via a PAD handoff, set padIdx (0-based grid
  index); the device APPENDS the record. Edit handle = first free stream slot = current pad-record
  count (count+0). A handle on a gap is silently rejected (no commit).
- EDIT/CLEAR an existing pad: address by 0-based STREAM POSITION (enumerate padName in order).
  Direct int/bool/float writes and select-begin both use it. Re-read positions before each edit
  (they shift when a record drops; system pads are interleaved).

WHAT REGION B IS - plain-language mental model (Shaun's framing, recorded 2026-06-16 because it had
only ever been said out loud, never written here; that gap kept making us re-derive it):
  Region B is the store of VOICE-FX EFFECT DEFINITIONS (the DSP "functions" that make a voice effect),
  plus one special template object at effectsIdx 65. A bank-2 VFX pad does not contain its effect; it
  POINTS at a region-B definition via its effectsIdx. So "assign a voice effect to a pad" = make sure
  the definition exists in region B and point the pad at it (this is exactly what our FX restore does:
  write the definition + set the pointer - so "region B is the functions" and "we write region-B blocks
  for FX" are the SAME statement, not a conflict).
  A SOUND pad has no effect, so it must touch region B NOT AT ALL: no pointer, no write. That is why a
  clean factory table is 7 region-B objects (6 VFX + special) even with 8 sound pads - sound pads
  contribute zero. The same goes for any non-FX pad type (Mixer/MIDI/QuickDial): no region-B object.

EFFECT-OBJECT writes (frame 0x9d): effect-objects live in two clusters separated by a ~100 KB
byte-gap. REGION A = channel-strip effects (one per input channel, 11). REGION B = the special
object (effectsIdx 65) + one definition per VFX pad. The 0x9d sub-handle is a REGION-B index.
assign_create.free_effect_subhandle(padIdx) derives region-A from the gap, then REUSES the pad's
existing object (effectsIdx==padIdx) for an edit, or APPENDS at the first free region-B slot.
A clean factory table has exactly ONE region-B object per VFX pad = 7 total (6 VFX + special).

*** SOUND-create orphan bug - FIXED + EAR-PROVEN 2026-06-16 ***: the sound create used to emit a
20-record EFFECTS block on a region-B sub-handle for SOUND pads too. Per the model above a sound pad
must write NOTHING to region B, so that appended a region-B object (effectsIdx==padIdx) the pad does
not need, and clear leaves it = an orphan (removable app-free, section 5). THE FIX: build_sound_create
now takes with_effects (DEFAULTS FALSE for sound), which
drops the EFFECTS_PARAMETERS handoff + the 20 effects records, so a sound create sends ZERO 0x9d writes
and cannot orphan. PROVEN 2026-06-16 on empty bank-3 pad 2 (padIdx 17): create kept region-B at 8 (no
new object), saw_sync=True (the sync handshake was never tied to the effects block), the WAV wrote, the
pad played (Shaun ear-confirmed), and clearing it kept region-B at 8 too. So the sound create+clear
lifecycle is now pool-safe end to end. FX creates STILL write region B (with_effects defaults True for
build_fx_create_recipe) - that is correct, an FX pad's effect definition is the whole point.

Value-block encodings (int/float/bool/string) are in RODECASTER_PROTOCOL.txt section 2.3. Note:
effect floats are normalised 0..1 EXCEPT pitchShiftSemitones (rides a float64 block but is a
-12..+12 int).

================================================================================
## 3. CONFIRMED METHODS  (all clean on a clean table; never disturb neighbours)

3.1 READ  rcp_read.read_model() / read_model_stable() (strips the per-frame 0x04 report id, the old
    framing bug that made counts drift). decode_effects.py dumps every effect-object; extract_recipes.py
    pulls per-pad recipes.
3.2 CREATE SOUND  assign_create.py <padIdx> <folder> <wav> [name] [colour]. handoff + phase1 + sync +
    commit(padType=1) + file write mid-sequence + remount + padFilePath. handle=free_handle(count). NO
    region-B/EFFECTS block (build_sound_create(with_effects=False), the 2026-06-16 fix - see section 2).
    Pool-safe: a sound create+clear keeps region-B unchanged (proven). saw_sync still True without the block.
    File written under its REAL name (pads/<n>/<Name>.<ext>, factory convention) and AS-IS (no
    conversion); WAV any rate/depth/channels + MP3 all play - see section 1c. .mp3 MUST keep its extension.
3.3 CREATE VOICE-FX  padType=2, padActive ON, padEffectTriggerMode=1, no file,
    effect on-flag(s) on the fx sub-handle. THE RECIPE BUILDER IS THE ONLY PATH:
    assign_create_fx_recipe(idx,name,recipe) (pad_records.build_fx_create_recipe writes every
    on-flag + param). Appends exactly one effect-object - read-back proven clean over repeated cycles.
3.4 CLEAR  clear_pad.py pos <0-based position>. = padActive ON/OFF + select-begin(position) + exit
    transfer mode. Drops the pad record AND its effect-object cleanly (no orphan). No remount = no drive-F toast.
3.5 REASSIGN = clear (3.4) then create (3.3); re-read positions between. restore_fx.py "<name>"|all
    rebuilds factory FX from fx_recipes.json this way.
3.6 IN-PLACE EFFECT EDIT - realtime  edit_effect_live.py <padIdx> <param> <value> [int|float|bool].
    Change an EXISTING pad's effect with no clear/create/allocation: set_transfer_mode(True) ->
    padActive=True on the pad (0x9a) -> param/on-flag on the pad's effect sub-handle (0x9d) ->
    set_transfer_mode(False). PROVEN realtime (changed Monster's pitch live while talking, no reboot,
    no neighbour disturbed). Best for editing existing pads + realtime preview. (Full-recipe-in-place
    = several flags/params at once is the natural next step; only single-param is proven so far.)
3.7 EDIT A PAD-LEVEL PROPERTY IN PLACE  [generic, 2026-06-17] soundpad.set_pad_property(grid, name,
    value, kind) - the UI-agnostic editing layer. Same proven pattern as set_pad_replay: resolve the
    pad's 0-based stream position, write ONE FRAME_PAD (0x9a) record in transfer mode, exit. No
    clear/create, no region-B touch -> pool-safe + silent. A PAD_PROP registry lists the SAFE,
    value-confirmed properties (each with kind/label/min-max/applies_to) and is what a future
    "Advanced per-pad" UI renders, so the layout is decoupled from the code. Named wrappers:
    set_pad_gain / set_pad_name / set_pad_colour / set_pad_loop. CLI: `soundpad.py set <grid> <prop>
    <value>`. PROVEN by read-back on a throwaway pad (padName, padColourIndex, padGain -12->-20,
    padLoop all wrote + read back, pool stayed 7). STILL TO CONFIRM before exposing (plumbing works,
    enum/range not yet verified): padPlayMode (Toggle/OneShot/Hold), padEffectInput (which mic),
    padEffectTriggerMode (FX latching), padEnv* (trim/fade), mixer sub-params (fade secs / back-channel
    sources / censor custom). Record-to-pad + export are device recorder/file features, NOT config writes
    (out of scope). Note: set_pad_property writes any property if given an explicit kind (escape hatch).
3.8 IN-PLACE EFFECT EDIT - see 3.6 (edit_effect_live, FX effect PARAMS on the 0x9d sub-handle).

================================================================================
## 4. BUILDERS (pad_records.py)  [CONFIRMED - byte-for-byte selftests]
Every record built from the target pad's params (no fossil replay). build_sound_create
/ build_fx_create_recipe / build_blank / build_mixer_create (padType 3, all 5 padMixerMode) /
build_trigger_create (padType 4, default or custom CC/Note). build_fx_create_recipe(idx,name,colour,recipe)
takes {engine:{param:value}}, engine in reverb/echo/pitchShift/distortion/robot/voiceDisguise.
`python pad_records.py` runs THREE selftests (sound vs create9_records.pkl all 75 records; mixer vs
mixer_censor_capture.pkl; trigger vs sp_midi_capture.pkl) - all byte-for-byte. padTriggerControl +
effectsIdx track the target padIdx.

================================================================================
## 5. POOL MANAGEMENT - one effect-object per FX pad

WHY THE INVARIANT: if region B accumulates an ORPHAN (a definition no live FX pad points at) or a
DUPLICATE, a clear/create can collide with the stray object and silence another pad's live FX. So the
rule is exactly one region-B object per FX pad, plus the special object at effectsIdx 65. Our tools
hold that automatically and repair it if something external (a crash mid-write) breaks it.

TWO DEVICE FACTS this rests on (measured by region-B count, app-free, no reset):
1. CLEAR GCs THE BOUND OBJECT. Clearing an FX pad makes the firmware garbage-collect that pad's
   region-B object - the clear sends only 0x9a pad frames (zero 0x9d), yet the object vanishes.
   Measured: create FX pad -> region_b +1; clear it -> region_b -1. Persists a reboot.
2. A BARE FX CREATE DOES NOT APPEND. build_blank(with_effects=False) sends no EFFECTS_PARAMETERS
   handoff and zero 0x9d writes, so no new object is allocated; the engine binds the pad to an EXISTING
   object by the effectsIdx==padIdx rule. So an orphan can be ADOPTED without appending. (The pad
   RECORD always appends; only the EFFECTS block allocates a region-B object.)

PRIMITIVES (soundpad.py section 7b), device-proven 2026-06-20, hardened create EAR-PROVEN end to end
(safe_assign_fx_recipe on a slot first polluted with an orphan healed it AND created a Megaphone that
went live on creation, no reboot; survived a power cycle):
- safe_assign_fx_recipe  HARDENED create: scrub the slot (clear any pad, GC any
    leftover orphan for that padIdx) then the normal append-create. The pad left behind is always made
    by the proven-live normal create, so it is live. assign_fx_to_grid (web) uses this and self-heals.
- clean_pool()              recovery net: GC every stray orphan (safe, touches no live pad). Runs
    automatically inside create/clear. Live-pad DUPLICATES are reported, not auto-rebuilt (see below).
- remove_orphan_objects(idx)  GC the objects for one padIdx that have no live FX pad.
- _bind_existing_fx(idx)    bare adopt (no append); TRANSIENT helper for orphan GC only - its own live
    binding is not reboot-proven, so such a pad is never left in place.

DUPLICATE on a LIVE pad (rare): clear the pad (GCs one copy, leaves the other as an orphan), then
recreate it from its recipe with the normal live create. Not auto-run by clean_pool because rebuilding
a live effect must be ear-checked.

LIVENESS is only confirmable by ear (talk into mic + hold the VFX pad); the persistent model does not
capture the live binding, so a "clean pool" read does not prove a pad sounds right.

FACTORY RESET (reference): app gear Settings > Factory Reset -> "unplug all RODE devices" -> reconnect
-> firmware reflash. Wipes EVERYTHING to a bare empty state (no pads, no show, 109 KB settings model).
It does NOT restore the factory show; that comes from the touchscreen first-boot SETUP WIZARD (language
/ date / wifi / analytics / load-show), which is touchscreen-only and NOT USB-bypassable. So a full
from-code reset-to-clean-device is not achievable. Clean snapshot: cap_control/clean_factory_2026-06-14.bin.

================================================================================
## 6. PRACTICAL RULES
- The tools keep the pool clean and self-heal stray orphans (section 5); no manual hygiene needed.
- A user must never see drive-F "scan and fix" toasts. Only a SOUND create writes a WAV / needs the FAT
  mount; do one mount per batch, dismount cleanly. FX/clear/recolour/in-place edits are HID-only (silent).
- Created pads append at the stream end (order is not factory order); functionally identical, app-compatible.

================================================================================
## 7. KNOWN DEVICE LIMITS (real, still true - not worth re-attempting)
- Bluetooth / wifi / the first-boot setup wizard cannot be driven over USB: they are touchscreen-only
  firmware actions. Writing the model property flips the stored value but the radio/engine ignores it
  (no USB-reachable trigger).
- The device rejects shortcut pad writes: an overwrite-in-place file hack, a bare padType=0 "clear",
  and a bare padFilePath repoint are all ignored. Use the proven clear+create (section 3).

================================================================================
## 8. KEY FILES
- soundpad.py           SINGLE SOURCE OF TRUTH for every pad op (2026-06-16 consolidation):
                        read/positions, pool_health, clear, create_sound (clear+create), create
                        voice-FX (recipe only), restore factory FX, + a CLI. The website AND
                        every CLI call into this one module. The old per-operation scripts
                        (assign_create.py, assign_create_fx.py, clear_pad.py, restore_fx.py,
                        pad_ops.py) were thin wrappers and are DELETED; nothing imports them.
                        Do NOT add pad logic anywhere else - a second copy is how we ran stale code.
                        Record windowing lives in _pad_windows / _pad_field (ONE anchor pass on
                        padName); stack cleaning in _sanitise_stack.
- rcp_read.py / rcp_transfer.py / rcp_hid.py   core read / write / HID.
- pad_records.py        low-level byte-builders (sound/fx/recipe) + byte-for-byte selftest. The
                        layer underneath soundpad.py; keep it.
- edit_effect_live.py   in-place realtime effect edit.
- decode_effects.py / extract_recipes.py   decode effect-objects / pull recipes from a snapshot.
- fx_recipes.json       the factory recipes (data).
- captures/reference/   kept reference captures.
- cap_control/clean_factory_2026-06-14.bin   the true clean-factory snapshot (7 region-B objects).

PENDING VERIFICATION: a sound upload through the BROWSER end to end has not been ear-tested from the
website. The CLI create path is fully ear-proven (8 WAV formats + MP3 + spaced filenames, section 1c)
and the server path uses the same create_sound with dest_filename = the upload name, so this is a
confirmation step, not new work.
