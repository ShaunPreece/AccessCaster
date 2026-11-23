"""Unit tests for what a PROFILE saves and restores. NO DEVICE, no HID, no writes.

THREE SILENT LOSSES, each of which a profile can suffer without reporting an error:
  1. The VIRTUAL FADER LEVELS and the MONITOR SPEAKER LEVEL going unsaved while the app can set
     both, so a profile claims to be a full backup and leaves two levels wherever they happen to be.
  2. HEADPHONE TYPE saved as a single value (Headphone 1's) and restored only to Headphone 1, which
     loses Headphone 2's type on every round trip.
  3. Restore validating the headphone value against a literal (0, 1), so "Low sensitivity" (2) is
     accepted by the UI, saved into the profile, and then DROPPED on restore.

The device-shaped half matters as much as the values: level writes are gated on the "level_write"
cap AND on the device having confirmed faderLevel selectors, so a Duo refuses and says so rather
than writing an unmapped selector (the stray-append hazard).

Run: python DevTools/test_profiles.py
"""
from __future__ import annotations

import contextlib
import dataclasses
import os
import shutil
import sys
import tempfile
from pathlib import Path

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
import channels
import devices
import fake_model
import profiles
import rcp_read
import rcp_transfer
import soundpad
from _testkit import as_device, check, check_true, patched

profiles.RESTORE_PACE = 0          # no need to pace writes that never leave the process


# --- a synthetic model blob, each property on its own node (see desk_dump) ---------------------

def prop_int(name, value):
    return (None, name, int(value))


def prop_float(name, value):
    return (None, name, float(value))


def prop_bool(name, value):
    return (None, name, bool(value))


def prop_str(name, value):
    return (None, name, str(value))


# Nine faders: the six physical pots read 0, the three virtual ones carry a level. Two headphone
# outputs with DIFFERENT types, the second of them "Low sensitivity" (2), the value a literal (0, 1)
# check drops. Monitor speaker at 3 percent, a realistic resting level.
FADER_LEVEL_VALUES = [0, 0, 0, 0, 0, 0, 100, 64, 127]


def desk_dump(entries, fader_levels, headphone_types):
    """A dump with each property on the node that carries it on the pinned layout, where its
    write goes: channelInputSource one per CHANNEL node, each fader level on its own FADER node
    and each headphone type on its own HEADPHONE node (a type of None is a node without one). A
    name no fixed node carries sits on the root."""
    root, blocks, strips = [], {}, []
    for _b, name, value in entries:
        if name == "channelInputSource":
            strips.append(value)
            continue
        key = next((k for k, (_p, props) in rcp_read.layout()._by_key.items()
                    if len(k) == 2 and name in props), None)
        if key is None:
            root.append((name, value))
        else:
            blocks.setdefault(key[0], []).append((name, value))
    faders = [fake_model.node("FADER", {"faderLevel": int(v)}) for v in fader_levels]
    kids = [fake_model.node("PHYSICALINTERFACE", blocks.pop("PHYSICALINTERFACE", ()), faders)]
    kids += [fake_model.node("CHANNEL", {"channelInputSource": c}) for c in strips]
    kids += [fake_model.node(block, props) for block, props in blocks.items()]
    kids += [fake_model.node("HEADPHONE", {} if t is None else {"headphoneType": int(t)})
             for t in headphone_types]
    return fake_model.node("Rodecaster", root, kids)


ROOT_PROPS = (
    [prop_int("channelInputSource", c) for c in (0, 7, 2, 3, 10, 11, 1, 8, 9, 9)]
    + [prop_bool("systemMidiControl", True)]
    + [prop_float("outputMonLevel", 0.03)]
    + [prop_bool("masterCompellorOn", False)]
    # THE UNIT'S OWN SETTINGS. hwScreenBrightness comes FIRST and holds a DIFFERENT value on
    # purpose: it is a superstring of screenBrightness, so a substring search decodes it and
    # reports 70 as the screen brightness. The save path uses the exact-name walker instead, and
    # the test below is what pins that down.
    + [prop_int("hwScreenBrightness", 70), prop_int("screenBrightness", 180),
       prop_bool("autoBrightness", False), prop_int("screenDimAfterSeconds", 1800),
       prop_int("activeButtonsBrightness", 255), prop_int("inactiveButtonsBrightness", 120),
       prop_int("metering", 1), prop_int("systemHapticSetting", 1),
       prop_int("systemRecButtonSetting", 0), prop_bool("outputPrefader", True),
       prop_bool("outputMonFixed", False),
       # The ten settings whose selectors are DERIVED rather than captured. outputBTLevel is here
       # for a specific reason: it is the first FLOAT in the address table, so a save path that
       # keeps ints and bools only drops it in silence. The two MUTES pin the opposite behaviour -
       # saved, never restored.
       prop_bool("outputMonMute", True), prop_bool("outputBTMute", False),
       prop_float("outputBTLevel", 0.5),
       prop_bool("systemDateTime24h", True), prop_bool("systemDateTimeDaylightSavings", False),
       prop_bool("systemDateTimeOnHome", True), prop_int("systemDateTimezone", 44),
       prop_bool("disableAllHeadphoneOutputs", False),
       prop_bool("disableAllLineoutOutputs", False),
       prop_bool("disableAllPhysicalButtons", False)]
    # Secrets and an undecoded property, for the full-dump snapshot below.
    + [prop_str("wifiSSID", "SomeNetwork"), prop_str("wifiPSK", "hunter2"),
       prop_str("sipAccountPassword", "s3cret"), prop_str("btPairCode", "482913"),
       prop_int("someUndecodedSetting", 42)]
)
blob = desk_dump(ROOT_PROPS, FADER_LEVEL_VALUES, [1, 2])


# --- what SAVE records ---------------------------------------------------------------------------

print("SAVE: the fader, monitor and headphone levels")
tmp = tempfile.mkdtemp(prefix="ac_profiles_test_")
saved_dir = profiles.PROFILES_DIR
profiles.PROFILES_DIR = tmp
try:
    with as_device(devices.PRO2):
        res = profiles.save("test", blob, [], "1.0.0", 10)
    check_true("save reports ok", res.get("ok"))
    prof = profiles.load("test")["prof"]
    r = prof["routing"]
    check("every fader level captured RAW (0..127)", r["fader_levels"], FADER_LEVEL_VALUES)
    check("monitor speaker level captured raw (0..1)", round(r["monitor_level"], 3), 0.03)
    check("one headphone type per output", r["headphone_types"], [1, 2])

    one_hp = dataclasses.replace(devices.PRO2, headphones=dataclasses.replace(
        devices.PRO2.headphones, outputs=devices.PRO2.headphones.outputs[:1]))
    with as_device(one_hp):
        profiles.save("test1hp", blob, [], "1.0.0", 10)
    r1 = profiles.load("test1hp")["prof"]["routing"]
    check("a device with one headphone output records one type", r1["headphone_types"], [1])

    # Each type is read on its own node, so one that will not decode cannot shift Headphone 2's
    # type onto Headphone 1.
    with as_device(devices.PRO2):
        profiles.save("testhpgap", desk_dump(ROOT_PROPS, FADER_LEVEL_VALUES, [None, 2]), [],
                      "1.0.0", 10)
    gap = profiles.load("testhpgap")["prof"]["routing"]
    check("a headphone with no type saves None, not its neighbour's", gap["headphone_types"],
          [None, 2])

    print("\nSAVE: the desk's own settings (display, buttons, haptics, REC button, listen mode)")
    unit = prof[profiles.UNIT_KEY]
    check("every desk setting in the address table is recorded",
          sorted(unit), sorted(rcp_transfer.UNIT_SETTING_SEL))
    check("screenBrightness is the SCREEN's value, not hwScreenBrightness's",
          unit["screenBrightness"], 180)
    check("and the substring trap is a genuine one (the two really do differ)",
          prof[profiles.SNAPSHOT_KEY]["properties"]["hwScreenBrightness"], [70])
    check("bools are stored as 0/1 so they round-trip through JSON unambiguously",
          (unit["autoBrightness"], unit["outputPrefader"], unit["outputMonFixed"]), (0, 1, 0))
    check("a FLOAT setting keeps its float value rather than being dropped",
          unit["outputBTLevel"], 0.5)
    check("the two live MUTES are recorded, so the snapshot still says how the desk was found",
          (unit["outputMonMute"], unit["outputBTMute"]), (1, 0))
    check("the dim timeout is kept in seconds", unit["screenDimAfterSeconds"], 1800)
    check("a value the app has no control for is saved too (activeButtonsBrightness)",
          unit["activeButtonsBrightness"], 255)

    print("\nSAVE: the full-dump snapshot (every property, not just the ones we decode)")
    snap = prof[profiles.SNAPSHOT_KEY]
    props = snap["properties"]
    check("a property no code of ours knows about is still recorded",
          props.get("someUndecodedSetting"), [42])
    check("repeated properties keep every value in stream order",
          props.get("faderLevel"), FADER_LEVEL_VALUES)
    check("the count is reported", snap["property_count"], len(props))
    print("\nSAVE: secrets are redacted from the snapshot")
    check("the wifi PSK is named as redacted and its value dropped",
          (props.get("wifiPSK"), "wifiPSK" in snap["redacted"]), ([None], True))
    check("so is the SIP account password",
          (props.get("sipAccountPassword"), "sipAccountPassword" in snap["redacted"]),
          ([None], True))
    check("so is a Bluetooth pairing code",
          (props.get("btPairCode"), "btPairCode" in snap["redacted"]), ([None], True))
    check("the SSID is kept (it identifies the setup, it does not open it)",
          props.get("wifiSSID"), ["SomeNetwork"])
    raw = (Path(tmp) / "test" / "profile.json").read_text(encoding="utf-8")
    check_true("no secret survives anywhere in the saved file",
               "hunter2" not in raw and "s3cret" not in raw)
finally:
    profiles.PROFILES_DIR = saved_dir
    shutil.rmtree(tmp, ignore_errors=True)


# --- RENAME and DELETE (pure file ops on the profile store) --------------------------------------

print("\nRENAME + DELETE: the store's own housekeeping")
tmp = tempfile.mkdtemp(prefix="ac_profiles_rd_")
outside = tempfile.mkdtemp(prefix="ac_profiles_outside_")
saved_dir = profiles.PROFILES_DIR
profiles.PROFILES_DIR = tmp
try:
    canary = Path(outside) / "do_not_touch.txt"
    canary.write_text("still here", encoding="utf-8")

    with as_device(devices.PRO2):
        profiles.save("keeper", blob, [], "1.0.0", 10)
        profiles.save("movable", blob, [], "1.0.0", 10)
    # A pad sound lives INSIDE the profile folder, so it has to travel with a rename and die with
    # a delete. Stand one in to prove the whole folder moves, not just profile.json.
    pad = Path(profiles.pads_dir("movable")) / "13"
    pad.mkdir(parents=True, exist_ok=True)
    (pad / "sound.wav").write_bytes(b"RIFF")

    check_true("rename reports ok", profiles.rename("movable", "moved")["ok"])
    check("the old name is gone and the new one is listed",
          profiles._list_profile_names(), ["keeper", "moved"])
    check_true("the profile still loads under the new name", profiles.load("moved")["ok"])
    check_true("and its pad audio came with it",
               (Path(profiles.pads_dir("moved")) / "13" / "sound.wav").is_file())

    check("renaming onto a name already in use is refused",
          profiles.rename("moved", "keeper")["ok"], False)
    check_true("and the profile it would have overwritten is untouched",
               profiles.load("keeper")["ok"])
    check("renaming to the same name is refused rather than silently doing nothing",
          profiles.rename("moved", "moved")["ok"], False)
    check("a name with characters the store does not allow is refused",
          profiles.rename("moved", "bad/name")["ok"], False)
    check("renaming something that does not exist is refused",
          profiles.rename("no such profile", "anything")["ok"], False)

    # The store is the only place either operation may reach. safe_name rejects separators and
    # dots outright, so traversal never even gets as far as the path check; assert the OUTCOME
    # (nothing outside the store moved or died) rather than which layer said no.
    for attempt in ("../../evil", "..\\..\\evil", "/etc/passwd", ".", "..",
                    "keeper/../../evil"):
        check(f"delete refuses {attempt!r}", profiles.delete(attempt)["ok"], False)
        check(f"rename refuses {attempt!r} as a source", profiles.rename(attempt, "x")["ok"], False)
        check(f"rename refuses {attempt!r} as a target",
              profiles.rename("keeper", attempt)["ok"], False)
    check_true("nothing outside the profile store was touched", canary.is_file())
    check("and the store still holds exactly what it should",
          profiles._list_profile_names(), ["keeper", "moved"])

    check_true("delete reports ok", profiles.delete("moved")["ok"])
    check("the deleted profile is gone from the list",
          profiles._list_profile_names(), ["keeper"])
    check_true("its folder, pad audio and all, is gone from disk",
               not (Path(tmp) / "moved").exists())
    check("deleting it again is refused rather than reported as done",
          profiles.delete("moved")["ok"], False)
finally:
    profiles.PROFILES_DIR = saved_dir
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(outside, ignore_errors=True)


# --- skip-if-identical: the comparison that decides a pad needs no work --------------------------
#
# The dangerous direction is a FALSE MATCH: saying a pad is already correct when it is not leaves it
# silently wrong, and nobody would know to look. So most of these assert that a difference is
# CAUGHT, one field at a time.

print("\nPAD VERIFY: metadata comparison (a false match leaves a pad silently wrong)")

SAVED_SOUND = {"grid": 5, "type": 1, "name": "Airhorn", "colour": 3, "gain": -6.0,
               "loop": False, "replay": True, "play_mode": 0, "mixer_mode": None,
               "mixer_trigger": None, "back_channel": None, "file": "airhorn.wav"}
LIVE_SOUND = {"grid": 5, "present": True, "type": 1, "name": "Airhorn", "colour": 3, "gain": -6.0,
              "loop": False, "replay": True, "play_mode": 0, "mixer_mode": None,
              "mixer_trigger": None, "back_channel": None}


def live(**over):
    return dict(LIVE_SOUND, **over)


m = soundpad._pad_metadata_matches
check_true("an identical pad matches", m(SAVED_SOUND, live()))
check("a different name is caught", m(SAVED_SOUND, live(name="Air horn")), False)
check("a different colour is caught", m(SAVED_SOUND, live(colour=4)), False)
check("a different play mode is caught", m(SAVED_SOUND, live(play_mode=2)), False)
check("a different loop flag is caught", m(SAVED_SOUND, live(loop=True)), False)
check("a different replay flag is caught", m(SAVED_SOUND, live(replay=False)), False)
check("a different pad type is caught", m(SAVED_SOUND, live(type=3)), False)
check("an empty pad on the desk is caught", m(SAVED_SOUND, live(present=False)), False)
check("a missing pad on the desk is caught", m(SAVED_SOUND, None), False)
check("a real gain difference is caught", m(SAVED_SOUND, live(gain=-5.0)), False)
check_true("but gain is compared at the 1dp the live read reports, not on raw floats",
           m(dict(SAVED_SOUND, gain=-6.04), live(gain=-6.0)))
check("a null live gain against a saved one is caught", m(SAVED_SOUND, live(gain=None)), False)
# A field the profile never recorded cannot differ: the restore would not write it either.
check_true("a field the profile did not record is ignored",
           m(dict(SAVED_SOUND, play_mode=None), live(play_mode=2)))

print("\nPAD VERIFY: the envelope, fade and censor groups the restore writes are compared (T3-257)")
ENV = {"start": 0.0, "fade_in": 0.1, "fade_out": 0.2, "stop": 1.0}
check_true("an identical envelope matches",
           m(dict(SAVED_SOUND, envelope=ENV), live(envelope=dict(ENV, fade_in=0.10000000149))))
check("a changed start point is caught",
      m(dict(SAVED_SOUND, envelope=ENV), live(envelope=dict(ENV, start=0.5))), False)
check("no live envelope against a saved one is caught",
      m(dict(SAVED_SOUND, envelope=ENV), live(envelope=None)), False)
check_true("a saved None member is skipped",
           m(dict(SAVED_SOUND, envelope=dict(ENV, start=None)),
             live(envelope=dict(ENV, start=0.5))))

print("\nPAD VERIFY: Back Channel membership (every member is written, so every member must agree)")
SAVED_BC = {"grid": 9, "type": 3, "name": "Back Channel", "colour": 2, "mixer_mode": 3,
            "mixer_trigger": 0, "back_channel": {"padBcMic1": True, "padBcMic2": False}}
LIVE_BC = {"grid": 9, "present": True, "type": 3, "name": "Back Channel", "colour": 2,
           "mixer_mode": 3, "mixer_trigger": 0, "gain": None, "loop": None, "replay": None,
           "play_mode": None, "back_channel": {"padBcMic1": True, "padBcMic2": False}}
check_true("identical membership matches", m(SAVED_BC, LIVE_BC))
check("a member that is ON when it should be OFF is caught",
      m(SAVED_BC, dict(LIVE_BC, back_channel={"padBcMic1": True, "padBcMic2": True})), False)
check("a member missing from the live read is caught",
      m(SAVED_BC, dict(LIVE_BC, back_channel={"padBcMic1": True})), False)
check("no live membership at all is caught", m(SAVED_BC, dict(LIVE_BC, back_channel=None)), False)

print("\nPAD VERIFY: a Fade pad's times and a Censor pad's own-sound switch are compared")
SAVED_FADE = dict(SAVED_BC, mixer_mode=1, back_channel=None,
                  fade={"in_seconds": 2.0, "out_seconds": 3.0, "exclude_host": True})
LIVE_FADE = dict(LIVE_BC, mixer_mode=1, back_channel=None,
                 fade={"in_seconds": 2.0, "out_seconds": 3.0, "exclude_host": True})
check_true("an identical Fade pad matches", m(SAVED_FADE, LIVE_FADE))
check("a changed fade time is caught",
      m(SAVED_FADE, dict(LIVE_FADE, fade=dict(LIVE_FADE["fade"], out_seconds=4.0))), False)
check("a changed host exclusion is caught",
      m(SAVED_FADE, dict(LIVE_FADE, fade=dict(LIVE_FADE["fade"], exclude_host=False))), False)
SAVED_CENSOR = dict(SAVED_BC, mixer_mode=0, back_channel=None,
                    censor={"custom": True, "file": "/old/path.wav"})
LIVE_CENSOR = dict(LIVE_BC, mixer_mode=0, back_channel=None,
                   censor={"custom": True, "file": "/another/path.wav"})
check_true("a Censor pad's path is not compared, since it is never restored",
           m(SAVED_CENSOR, LIVE_CENSOR))
check("but its own-sound switch is",
      m(SAVED_CENSOR, dict(LIVE_CENSOR, censor={"custom": False, "file": ""})), False)

print("\nPAD VERIFY: voice-FX pads are compared by their region-B object")
check("sound, voice FX, mixer and trigger pads are skippable",
      soundpad._SKIPPABLE_PAD_TYPES, (1, 2, 3, 4))
MEGAPHONE = {"reverb": {"reverbLowCut": 0.30000001192092896, "reverbHighCut": 0.0},
             "distortion": {"distortionIntensity": 0.699999988079071}}
SAVED_FX = {"grid": 10, "type": 2, "name": "Megaphone", "fx_recipe": "Megaphone",
            "fx_stack": MEGAPHONE}
FX_PAD = {"padIdx": 9, "padType": 2, "padActive": False, "padEffectTriggerMode": 1}
FX_OBJ = soundpad.pr.expected_effect_object(9, MEGAPHONE)
f = soundpad._fx_effect_matches
check("the object holds all twenty properties", len(FX_OBJ), 20)
check("the recipe's engines are on and the rest off",
      (FX_OBJ["reverbOn"], FX_OBJ["distortionOn"], FX_OBJ["echoOn"]), (True, True, False))
check_true("an object exactly as the create leaves it matches",
           f(SAVED_FX, FX_PAD, [FX_OBJ], True))
check_true("a param read back at double precision of the same float32 matches",
           f(SAVED_FX, FX_PAD, [dict(FX_OBJ, reverbLowCut=0.3)], True))
check("a changed param is caught",
      f(SAVED_FX, FX_PAD, [dict(FX_OBJ, reverbLowCut=0.31)], True), False)
check("a default param on an engine that is off is still compared",
      f(SAVED_FX, FX_PAD, [dict(FX_OBJ, echoMix=0.4)], True), False)
check("an extra engine switched on is caught",
      f(SAVED_FX, FX_PAD, [dict(FX_OBJ, echoOn=True)], True), False)
check("a recipe engine switched off is caught",
      f(SAVED_FX, FX_PAD, [dict(FX_OBJ, reverbOn=False)], True), False)
check("a missing property is caught",
      f(SAVED_FX, FX_PAD, [{k: v for k, v in FX_OBJ.items() if k != "robotMix"}], True), False)
check("an unknown extra property is caught",
      f(SAVED_FX, FX_PAD, [dict(FX_OBJ, futureParam=1.0)], True), False)
check("no object for the pad is caught", f(SAVED_FX, FX_PAD, [], True), False)
check("two objects for the pad are caught", f(SAVED_FX, FX_PAD, [FX_OBJ, FX_OBJ], True), False)
check("a pool that is not clean means rebuild", f(SAVED_FX, FX_PAD, [FX_OBJ], False), False)
check("a pad left engaged means rebuild",
      f(SAVED_FX, dict(FX_PAD, padActive=True), [FX_OBJ], True), False)
check("a pad on another trigger mode means rebuild",
      f(SAVED_FX, dict(FX_PAD, padEffectTriggerMode=0), [FX_OBJ], True), False)
check("a pad of another type means rebuild",
      f(SAVED_FX, dict(FX_PAD, padType=1), [FX_OBJ], True), False)
check("an unrecognised effect (no recipe) means rebuild",
      f(dict(SAVED_FX, fx_recipe=None, fx_stack=None), FX_PAD, [FX_OBJ], True), False)
check("no live pad means rebuild", f(SAVED_FX, None, [FX_OBJ], True), False)

VOL = soundpad.pr.PAD_VOLUME_ROOT
NOTHING_ELSE = (set(), set())     # no other pad plays anything

print("\nPAD VERIFY: audio comparison (name, then size, then a full content hash)")
vtmp = tempfile.mkdtemp(prefix="ac_pad_verify_")
try:
    root = Path(vtmp) / "unit"          # stands in for the mounted pad volume
    backup = Path(vtmp) / "backup"      # stands in for the profile's pads/ folder
    (root / "pads" / "5").mkdir(parents=True)
    (backup / "5").mkdir(parents=True)
    (root / "pads" / "5" / "airhorn.wav").write_bytes(b"RIFF" + b"\x01" * 4096)
    (backup / "5" / "airhorn.wav").write_bytes(b"RIFF" + b"\x01" * 4096)
    a = soundpad._pad_audio_matches
    check_true("the same file byte for byte matches",
               a(str(root), 5, "airhorn.wav", str(backup / "5" / "airhorn.wav")))

    # THE CASE ONLY A HASH CATCHES: same name, same length, different audio.
    (root / "pads" / "5" / "airhorn.wav").write_bytes(b"RIFF" + b"\x02" * 4096)
    check("same name and size but different content is caught",
          a(str(root), 5, "airhorn.wav", str(backup / "5" / "airhorn.wav")), False)

    (root / "pads" / "5" / "airhorn.wav").write_bytes(b"RIFF" + b"\x01" * 4096)
    check("a different length is caught",
          a(str(root), 5, "airhorn.wav", str(backup / "5" / "shorter.wav")), False)
    check("a name the desk does not have is caught",
          a(str(root), 5, "other.wav", str(backup / "5" / "airhorn.wav")), False)
    check("a profile entry with no filename is caught",
          a(str(root), 5, None, str(backup / "5" / "airhorn.wav")), False)
    check("a backup whose local copy is missing is caught",
          a(str(root), 5, "airhorn.wav", str(backup / "5" / "gone.wav")), False)
    check("a grid with no file on the desk is caught",
          a(str(root), 7, "airhorn.wav", str(backup / "5" / "airhorn.wav")), False)
    # Leftovers. A pad folder can hold a file from an earlier replacement. Which file the pad PLAYS
    # is what matters, and only the desk can say - padFilePath, passed in as live_path.
    (root / "pads" / "5" / "leftover.wav").write_bytes(b"RIFF" + b"\t" * 64)
    check("with no word from the desk, two files in a folder is caught",
          a(str(root), 5, "airhorn.wav", str(backup / "5" / "airhorn.wav")), False)
    check_true("but told which file the pad plays, a leftover beside it is irrelevant",
               a(str(root), 5, "airhorn.wav", str(backup / "5" / "airhorn.wav"),
                 live_path=VOL + "pads/5/airhorn.wav"))
    check("a pad that plays the leftover instead is caught",
          a(str(root), 5, "airhorn.wav", str(backup / "5" / "airhorn.wav"),
            live_path=VOL + "pads/5/leftover.wav"), False)
    check("a pad playing a file that is not there at all is caught",
          a(str(root), 5, "airhorn.wav", str(backup / "5" / "airhorn.wav"),
            live_path=VOL + "pads/5/vanished.wav"), False)
finally:
    shutil.rmtree(vtmp, ignore_errors=True)


print("\nTIDY PAD AUDIO: only files no pad plays, only inside pads/")
ttmp = tempfile.mkdtemp(prefix="ac_tidy_")
try:
    root = Path(ttmp)
    (root / "pads" / "1").mkdir(parents=True)
    (root / "pads" / "17").mkdir(parents=True)
    (root / "pads" / "1" / "Music_Bed.wav").write_bytes(b"A" * 100)
    (root / "pads" / "1" / "Stray.wav").write_bytes(b"B" * 50)
    (root / "pads" / "17" / "old_test.wav").write_bytes(b"C" * 30)
    # Something OUTSIDE pads/ that must never be considered, whatever else happens.
    (root / "important.wav").write_bytes(b"D" * 10)

    removed = soundpad.tidy_pad_folder(str(root), 1, "Music_Bed.wav", NOTHING_ELSE)
    check("the pad's own audio survives",
          (root / "pads" / "1" / "Music_Bed.wav").is_file(), True)
    check("the file it replaced is gone", (root / "pads" / "1" / "Stray.wav").is_file(), False)
    check("and is reported", removed, ["Stray.wav"])
    check("a folder for another pad is untouched",
          (root / "pads" / "17" / "old_test.wav").is_file(), True)
    check("nothing outside pads/ is touched", (root / "important.wav").is_file(), True)

    # No keeper means no deletions: without knowing what to keep it could delete the live sound.
    (root / "pads" / "1" / "Stray.wav").write_bytes(b"B" * 50)
    check("with no keeper named, nothing is deleted at all",
          soundpad.tidy_pad_folder(str(root), 1, "", NOTHING_ELSE), [])
    check("so both files are still there",
          sorted(f.name for f in (root / "pads" / "1").iterdir()),
          ["Music_Bed.wav", "Stray.wav"])
    check("only pads/ folders are ever listed",
          sorted(os.path.basename(d) for d in soundpad._pad_audio_dirs(str(root))), ["1", "17"])
finally:
    shutil.rmtree(ttmp, ignore_errors=True)


print("\nTIDY PAD AUDIO: a pad's file is found by its whole padFilePath (T3-243)")
ttmp = tempfile.mkdtemp(prefix="ac_tidy_path_")
try:
    root = Path(ttmp)
    for folder, name in (("3", "X.wav"), ("3", "Old.wav"), ("7", "Kept.wav"), ("9", "Gone.wav")):
        (root / "pads" / folder).mkdir(parents=True, exist_ok=True)
        (root / "pads" / folder / name).write_bytes(b"A" * 10)
    # Grid 5 plays a file in grid 3's folder; grid 7 is a sound pad that reports no padFilePath.
    pads = [soundpad.Pad(0, 4, soundpad.PAD_TYPE_SOUND, "Elsewhere",
                         {"padFilePath": VOL + "pads/3/X.wav"}),
            soundpad.Pad(1, 6, soundpad.PAD_TYPE_SOUND, "Unknown", {})]
    in_use = soundpad.pad_audio_in_use(pads)
    check("the in-use set holds the whole path and the folder it cannot locate",
          in_use, ({"pads/3/x.wav"}, {7}))
    check("a sound created on grid 3 keeps the file grid 5 plays",
          soundpad.tidy_pad_folder(str(root), 3, "New.wav", in_use), ["Old.wav"])
    check("so pads/3/X.wav is still there", (root / "pads" / "3" / "X.wav").is_file(), True)

    @contextlib.contextmanager
    def no_transfer(_settle):
        yield

    with patched((soundpad, {"pad_nodes": lambda blob=None: pads, "_transfer_mode": no_transfer}),
                 (soundpad.rr, {"read_model_stable": lambda **_k: b""}),
                 (soundpad.t, {"remount_pad_storage": lambda: None,
                               "find_pad_volume": lambda **_k: str(root),
                               "flush_volume": lambda _root: None})):
        res = soundpad.tidy_unused_pad_audio()
    check("the sweep keeps both pads' audio",
          [(root / "pads" / f / n).is_file() for f, n in (("3", "X.wav"), ("7", "Kept.wav"))],
          [True, True])
    check("and removes only the file nothing plays", res["removed"], ["pads/9/Gone.wav"])
    check("export reads the file the pad names, wherever it is",
          soundpad._pad_audio_source(str(root), 5, VOL + "pads/3/X.wav"),
          str(root / "pads" / "3" / "X.wav"))
    check("and finds nothing rather than another file when the named one is missing",
          soundpad._pad_audio_source(str(root), 7, VOL + "pads/7/Missing.wav"), None)

    # A Censor mixer pad's own sound, named by padMixerCensorFilePath, is kept by both tidies.
    (root / "pads" / "9").mkdir(parents=True, exist_ok=True)
    (root / "pads" / "9" / "Bleep.wav").write_bytes(b"C" * 10)
    pads.append(soundpad.Pad(2, 8, soundpad.PAD_TYPE_MIXER, "Censor",
                             {"padMixerCensorFilePath": VOL + "pads/9/Bleep.wav"}))
    in_use = soundpad.pad_audio_in_use(pads)
    check("the in-use set holds a Censor pad's own sound", "pads/9/bleep.wav" in in_use[0], True)
    check("a sound created on grid 9 keeps it",
          soundpad.tidy_pad_folder(str(root), 9, "New.wav", in_use), [])
    with patched((soundpad, {"pad_nodes": lambda blob=None: pads, "_transfer_mode": no_transfer}),
                 (soundpad.rr, {"read_model_stable": lambda **_k: b""}),
                 (soundpad.t, {"remount_pad_storage": lambda: None,
                               "find_pad_volume": lambda **_k: str(root),
                               "flush_volume": lambda _root: None})):
        res = soundpad.tidy_unused_pad_audio()
    check("and the sweep keeps it", res["removed"], [])
    check("every pad property naming a file is one the tidy reads",
          sorted(n for n, _k, _v in soundpad.pr._PHASE1 if n.endswith("FilePath")),
          ["padFilePath", "padMixerCensorFilePath"])
finally:
    shutil.rmtree(ttmp, ignore_errors=True)


# --- what RESTORE writes -------------------------------------------------------------------------

CALLS: list = []


RECORDED_TRANSFER = ("set_midi_control", "set_fader_source", "set_fader_level",
                     "set_monitor_level", "set_headphone_type", "set_multitrack_mode",
                     "set_monitor_auto_mute", "set_bt_auto_mute", "set_master_compellor",
                     "set_master_compellor_param", "set_output_delay", "set_unit_setting",
                     "set_recording_processing_bypass", "set_usb_multitrack_mode",
                     "set_usb_multitrack_bypass")
RECORDED_CHANNELS = ("set_mic_gain", "set_mic_type", "set_phantom", "set_polarity",
                     "set_instrument_gain", "set_channel_level", "set_channel_advanced",
                     "set_processing_enabled", "set_processing_module", "set_channel_processing",
                     "set_channel_preset_marker")


def _recording(name: str):
    return lambda *a, **k: CALLS.append((name, a))


@contextlib.contextmanager
def recorder():
    """Replace the device-write functions with recorders for the block, yielding the call log."""
    CALLS.clear()
    with patched((rcp_transfer, {n: _recording(n) for n in RECORDED_TRANSFER}),
                 (channels, {n: _recording(n) for n in RECORDED_CHANNELS})):
        yield CALLS


def names_of(calls):
    return [n for n, _a in calls]


def args_of(calls, name):
    return [a for n, a in calls if n == name]


def profile(**routing):
    """A version 2 profile carrying routing only, no channels."""
    r = {"faders": [0, 7, 2, 3, 10, 11, 1, 8, 9],
         "fader_levels": FADER_LEVEL_VALUES,
         "monitor_level": 0.03,
         "headphone_types": [1, 2]}
    r.update(routing)
    return {"version": 2, "channels": [], "routing": r}


def apply_pro2(prof):
    with as_device(devices.PRO2), recorder() as calls:
        res = profiles.apply(prof, fader_count=9, fader_code_set=devices.PRO2.fader.code_set)
        return list(calls), res


print("\nRESTORE on the Pro II: the levels land")
calls, res = apply_pro2(profile())
check("one level write per VIRTUAL fader (7, 8, 9)", len(args_of(calls, "set_fader_level")), 3)
check("each virtual fader + its saved raw level",
      args_of(calls, "set_fader_level"), [(6, 100), (7, 64), (8, 127)])
check("the monitor speaker level is written as its raw 0..1 float",
      [round(a[0], 3) for a in args_of(calls, "set_monitor_level")], [0.03])
check("nothing was reported as unrestorable", res["errs"], [])

print("\nRESTORE ORDER: levels follow the layout onto the strip")
# faderLevel is addressed per STRIP, so a level written before the layout would land on whatever
# channel was there before the restore.
last_source = max(i for i, n in enumerate(names_of(calls)) if n == "set_fader_source")
first_level = min(i for i, n in enumerate(names_of(calls)) if n == "set_fader_level")
check_true("every fader SOURCE write precedes the first level write", last_source < first_level)

print("\nRESTORE ORDER: the mic type goes before the gain")
# CHOOSING A VOICING SETS THAT VOICING'S OWN DEFAULT GAIN (hardware: SM7B 55,
# Condenser 25, ... only Line-in and Instrument leave it alone). So a gain written before the type
# is thrown away: a profile saying 47 with SM7B written after it leaves the desk at 55.
mic_prof = dict(profile(), channels=[{"name": "Mic1", "kind": "mic", "level_db": 47,
                                      "mic_type": 10, "phantom": False, "polarity": False,
                                      "instrument_gain": 20}])
mic_calls, _ = apply_pro2(mic_prof)
check("the gain is written once, with the saved value", args_of(mic_calls, "set_mic_gain"), [(1, 47)])
check("the mic type is written once", args_of(mic_calls, "set_mic_type"), [(1, 10)])
check_true("the mic TYPE write precedes the GAIN write",
           names_of(mic_calls).index("set_mic_type") < names_of(mic_calls).index("set_mic_gain"))
check_true("and the instrument gain still follows the type, which is what sets it to 20",
           names_of(mic_calls).index("set_mic_type")
           < names_of(mic_calls).index("set_instrument_gain"))

print("\nRESTORE: the voicing wait gives up at once on a desk that cannot be read")
chain_reads = []
with patched((profiles, {"_chain_of": lambda name: chain_reads.append(name)})):
    profiles._await_voicing("Mic1", timeout=0.5)
check("one failed read, not a stream of them until the timeout", chain_reads, ["Mic1"])
settled = iter([{"gain": 1}, {"gain": 2}, {"gain": 2}])
with patched((profiles, {"_chain_of": lambda _name: next(settled)})):
    profiles._await_voicing("Mic1", timeout=5)
check("a voicing still settles on two equal reads", next(settled, "spent"), "spent")

print("\nRESTORE: the SMART pads bus is a channel like any other")
# Its strip carries a full chain and the desk processes pads through it.
# Leaving it out of the channel list is what makes a clean restore put its custom chain back at
# factory values. It has NO level - that is its fader - so no level write may be attempted for it.
check_true("the Pro II lists the SMART pads bus as a channel",
           "SMARTpads" in [c["name"] for c in devices.PRO2.ui_channels])
bus_prof = dict(profile(), channels=[{"name": "SMARTpads", "kind": "bus", "level_db": None,
                                      "advanced": True, "processing_on": True,
                                      "modules": {"eqOn": True},
                                      "params": {"eqLowGain": 0.25},
                                      "preset_index": -1}])
bus_calls, bus_res = apply_pro2(bus_prof)
check("its processing mode is written", args_of(bus_calls, "set_channel_advanced"),
      [("SMARTpads", True)])
check("its master processing switch is written", args_of(bus_calls, "set_processing_enabled"),
      [("SMARTpads", True)])
check("its modules are written", args_of(bus_calls, "set_processing_module"),
      [("SMARTpads", "eqOn", True)])
check("its parameters are written", args_of(bus_calls, "set_channel_processing"),
      [("SMARTpads", "eqLowGain", 0.25)])
check_true("its preset marker follows the parameters (the desk blanks it on any param write)",
           names_of(bus_calls).index("set_channel_processing")
           < names_of(bus_calls).index("set_channel_preset_marker"))
check("NO level write is attempted for a bus", args_of(bus_calls, "set_channel_level"), [])
check("and nothing is reported as unrestorable", bus_res["errs"], [])

print("\nRESTORE: headphone type, one per output")
check("both headphone outputs written, each on its own output",
      args_of(calls, "set_headphone_type"), [(1, 0), (2, 1)])
check_true("'Low sensitivity' (2) is applied, not silently dropped",
           (2, 1) in args_of(calls, "set_headphone_type"))
calls_bad, _ = apply_pro2(profile(headphone_types=[0, 99]))
check("a value the device does not offer is skipped, not written",
      args_of(calls_bad, "set_headphone_type"), [(0, 0)])

print("\nRESTORE: the multitrack pair, both destinations, mode AND bypass")
# The point of these four: a profile that put the multitrack MODE back but not its processing
# bypass would restore half a decision, and the half it dropped is the one you cannot fix after
# the take - a raw pre-fader session coming back processed.
calls_mt, res_mt = apply_pro2(profile(multitrack=2, processing_bypass=True,
                                      usb_multitrack=1, usb_processing_bypass=True))
check("the recorder's mode", args_of(calls_mt, "set_multitrack_mode"), [(2,)])
check("the recorder's raw switch", args_of(calls_mt, "set_recording_processing_bypass"), [(True,)])
check("the USB mode", args_of(calls_mt, "set_usb_multitrack_mode"), [(1,)])
check("the USB raw switch", args_of(calls_mt, "set_usb_multitrack_bypass"), [(True,)])
check("nothing reported as unrestorable", res_mt["errs"], [])
calls_off, _ = apply_pro2(profile(multitrack=0, processing_bypass=False,
                                  usb_multitrack=0, usb_processing_bypass=False))
check_true("False is a VALUE, not a missing key: the raw switches are still written off",
           args_of(calls_off, "set_recording_processing_bypass") == [(False,)]
           and args_of(calls_off, "set_usb_multitrack_bypass") == [(False,)])
calls_none, res_none = apply_pro2(profile())
check("a profile taken before these existed writes none of them",
      args_of(calls_none, "set_recording_processing_bypass")
      + args_of(calls_none, "set_usb_multitrack_mode")
      + args_of(calls_none, "set_usb_multitrack_bypass"), [])
check("and that is not a failure", res_none["errs"], [])

# EXPANDED rides in the same property, so the restore must COMPOSE the two halves into one write.
# Restoring them separately would take two USB re-enumerations and let the second undo the first,
# and dropping the flag would put the desk back to Standard without being asked to.
calls_exp, _ = apply_pro2(profile(usb_multitrack=2, usb_expanded=True))
check("Expanded composes into the output mode: post-fader plus expanded is 5",
      args_of(calls_exp, "set_usb_multitrack_mode"), [(5,)])
calls_std, _ = apply_pro2(profile(usb_multitrack=2, usb_expanded=False))
check("and Standard is the bare mode again",
      args_of(calls_std, "set_usb_multitrack_mode"), [(2,)])
# THE BACKWARDS-COMPATIBLE CASE, and it is the common one: every profile saved before Expanded was
# known holds a bare 0-2 and no flag, which is exactly "that mode, Standard".
calls_old, _ = apply_pro2(profile(usb_multitrack=1))
check("a profile with no expanded key restores as Standard",
      args_of(calls_old, "set_usb_multitrack_mode"), [(1,)])

print("\nRESTORE on the Duo: the multitrack pair DOES restore, and that is deliberate")
# The Duo HAS the output_write cap, so it gets these four. That is the right answer rather than a
# hole: they all sit on the OUTPUT block, which is confirmed on BOTH desks. It does not depend on
# the "recorder" cap, because what a recording CONTAINS is an OUTPUT setting and not the transport,
# which is the point of keeping those two apart.
with as_device(devices.DUO), recorder() as duo_mt:
    profiles.apply(profile(multitrack=2, processing_bypass=True, usb_multitrack=1,
                               usb_processing_bypass=True),
                       fader_count=10, fader_code_set=devices.DUO.fader.code_set)
    duo_mt = list(duo_mt)
check("all four land on the Duo too",
      args_of(duo_mt, "set_multitrack_mode") + args_of(duo_mt, "set_recording_processing_bypass")
      + args_of(duo_mt, "set_usb_multitrack_mode") + args_of(duo_mt, "set_usb_multitrack_bypass"),
      [(2,), (True,), (1,), (True,)])

print("\nRESTORE: an older profile with no level keys writes no levels")
old = profile()
del old["routing"]["fader_levels"]
del old["routing"]["monitor_level"]
calls_old, res_old = apply_pro2(old)
check("no level writes", args_of(calls_old, "set_fader_level") + args_of(calls_old, "set_monitor_level"), [])
check("and nothing is reported as failed", res_old["errs"], [])

print("\nRESTORE on the Duo: levels ARE written")
# The desk confirms the Duo's fader levels by acting on them, so a profile restores them here exactly
# as on a Pro II. What matters is WHICH: the five VIRTUAL faders, indices 4-8. The four physical
# ones are analog pots and must get nothing.
with as_device(devices.DUO), recorder() as duo_calls:
    duo_res = profiles.apply(profile(), fader_count=10,
                                 fader_code_set=devices.DUO.fader.code_set)
    duo_calls = list(duo_calls)
check("the five VIRTUAL faders are written, and only those",
      sorted(a[0] for a in args_of(duo_calls, "set_fader_level")), [4, 5, 6, 7, 8])
check_true("the monitor level is written too", args_of(duo_calls, "set_monitor_level"))
check("levels are not reported as unrestorable",
      [e for e in duo_res["errs"] if "level" in e], [])
check("headphone type still restores (both desks share the confirmed outputs)",
      args_of(duo_calls, "set_headphone_type"), [(1, 0), (2, 1)])

print("\nRESTORE: the desk's own settings")
UNIT = {"screenBrightness": 180, "autoBrightness": 0, "screenDimAfterSeconds": 1800,
        "outputPrefader": 1, "systemHapticSetting": 2}
calls_unit, res_unit = apply_pro2(dict(profile(), **{profiles.UNIT_KEY: UNIT}))
check("every saved desk setting is written back",
      sorted(args_of(calls_unit, "set_unit_setting")),
      sorted((k, v) for k, v in UNIT.items()))
check("nothing was reported as unrestorable", res_unit["errs"], [])
# The two output MUTES are in the address table and ARE saved, but a restore must never write them.
# A profile has to be safe to run mid-session, and silencing the speakers because that is how they
# sat when the profile was taken would be a nasty surprise with no visible cause. Same reasoning
# that keeps the channel mutes out. The FLOAT is here too: a save path that keeps ints and bools
# only drops it, so it is worth pinning that it survives the round trip and is written back.
calls_mute, res_mute = apply_pro2(dict(profile(), **{profiles.UNIT_KEY: {
    "outputMonMute": 1, "outputBTMute": 1, "outputBTLevel": 0.4, "screenBrightness": 90}}))
check("a saved output mute is NOT written back",
      [c for c in args_of(calls_mute, "set_unit_setting") if "Mute" in c[0]], [])
check("but the other settings in the same profile still are",
      sorted(args_of(calls_mute, "set_unit_setting")),
      sorted([("outputBTLevel", 0.4), ("screenBrightness", 90)]))
check("and skipping the mutes is not reported as a failure", res_mute["errs"], [])
# A profile written by a NEWER build can carry a setting this build has no address for. Writing it
# would mean guessing a selector, which is the stray-append hazard, so it is skipped.
calls_future, _ = apply_pro2(dict(profile(),
                                  **{profiles.UNIT_KEY: {"someFutureSetting": 3,
                                                         "screenBrightness": 10}}))
check("a setting with no confirmed address is SKIPPED, not guessed at",
      args_of(calls_future, "set_unit_setting"), [("screenBrightness", 10)])
old_unit = profile() # an older profile has no unit block at all
calls_nounit, res_nounit = apply_pro2(old_unit)
check("an older profile writes no desk settings and reports no failure",
      (args_of(calls_nounit, "set_unit_setting"), res_nounit["errs"]), ([], []))

print("\nRESTORE: a damaged value is one failure, not the end of the restore")
calls_bad_fmt, res_bad_fmt = apply_pro2(dict(profile(recording_format="WAV"),
                                             **{profiles.UNIT_KEY: {"screenBrightness": 90}}))
check("the settings after it are still written",
      args_of(calls_bad_fmt, "set_unit_setting"), [("screenBrightness", 90)])
check("and the bad value counts as one failure", res_bad_fmt["failed"], 1)

print("\nRESTORE on the Duo: the desk settings ARE written")
# The Duo holds the "unit_settings" cap on all 20 selectors resolving against its OWN TREE plus one
# confirmed write per block, so a profile taken on a Duo restores its Device page rather
# than skipping it. The mutes are held back, on both desks, because a restore must never silently
# mute somebody.
with as_device(devices.DUO), recorder() as duo_unit_calls:
    duo_unit_res = profiles.apply(dict(profile(), **{profiles.UNIT_KEY: UNIT}), fader_count=10,
                                      fader_code_set=devices.DUO.fader.code_set)
    duo_unit_calls = list(duo_unit_calls)
duo_unit_written = dict(args_of(duo_unit_calls, "set_unit_setting"))
check_true("the Duo restores its desk settings", bool(duo_unit_written))
check_true("and that is not reported as a failure",
           not any("desk settings" in e for e in duo_unit_res["errs"]))
check("a saved output mute is still NOT written back on the Duo either",
      sorted(p for p in duo_unit_written if "Mute" in p), [])

print("\nWRITE PATH: set_unit_setting refuses a property it has no address for")
try:
    rcp_transfer.set_unit_setting("definitelyNotAProperty", 1)
    check("it raised", False, True)
except ValueError as exc:
    check_true("ValueError, naming the property", "definitelyNotAProperty" in str(exc))

print("\nA PAD-VOLUME FLUSH THAT FAILS LEAVES A TRACE")


class _Call:
    """A kernel32 function that returns `result`, with the attributes ctypes setup assigns."""
    def __init__(self, result):
        self.result = result

    def __call__(self, *_args):
        return self.result


class _Kernel32:
    def __init__(self, handle, flushed):
        self.CreateFileW = _Call(handle)
        self.FlushFileBuffers = _Call(flushed)
        self.CloseHandle = _Call(1)


class _PowerShellFailed:
    returncode = 1
    stderr = b"Write-VolumeCache : No MSFT_Volume objects found"


for label, handle, flushed, first in (
        ("no volume handle", 0, 0, "no volume handle"),
        ("a flush Windows refuses", 0x1234, 0, "FlushFileBuffers failed")):
    events = []
    k32 = _Kernel32(handle, flushed)
    with patched((rcp_transfer.diagnostics,
                  {"event": lambda kind, detail="", log=events.append, **f: log((kind, detail, f))}),
                 (rcp_transfer.ctypes, {"WinDLL": lambda *_a, k32=k32, **_k: k32}),
                 (rcp_transfer.subprocess, {"run": lambda *_a, **_k: _PowerShellFailed()})):
        rcp_transfer.flush_volume("Q:\\")
    check(f"{label}: the fast path's failure is logged",
          [(k, d) for k, d, _f in events[:1]], [("transfer.flush_fallback", first)])
    check(f"{label}: and so is the PowerShell exit code",
          [(k, f.get("exit_code")) for k, _d, f in events[1:]], [("transfer.flush_failed", 1)])


# --- HOW MUCH A RESTORE PUTS BACK: everything, or the setup alone ---------------------------------
import copy  # noqa: E402

import layout_fixture as lf  # noqa: E402
from _testkit import check_in  # noqa: E402

print("\nRESTORE SCOPE: the setup alone leaves the master, the monitor, MIDI and the desk settings")
MIC2 = {"name": "Mic2", "label": "Mic 2", "kind": "mic", "level_db": 30, "mic_type": 5,
        "phantom": True, "polarity": False, "instrument_gain": 20, "advanced": True,
        "processing_on": True, "modules": {"eqOn": True}, "params": {"eqLowGain": 0.25},
        "preset_index": -1, "effects": {"echo": False, "reverb": True,
                                        "echo_params": {"echoMix": 0.5}, "reverb_params": {}}}
USB1 = {"name": "USB1", "label": "USB 1 (Main Stereo)", "kind": "line", "level_db": -12,
        "processing_on": False}
whole_prof = dict(profile(multitrack=1, processing_bypass=False, recording_format=0,
                          usb_multitrack=1, usb_expanded=False, usb_processing_bypass=False,
                          monitor_auto_mute=True, bt_auto_mute=False, compellor_on=True,
                          compellor_gain=0.2, delay_on=False),
                  midi=True, channels=[MIC2, USB1],
                  **{profiles.UNIT_KEY: {"screenBrightness": 200}})
SETUP_ONLY = {"set_fader_source", "set_fader_level", "set_mic_type", "set_mic_gain", "set_phantom",
              "set_polarity", "set_instrument_gain", "set_channel_level", "set_channel_advanced",
              "set_processing_enabled", "set_processing_module", "set_channel_processing",
              "set_channel_preset_marker", "set_channel_effect", "set_channel_effect_param"}
REST = {"set_midi_control", "set_monitor_level", "set_headphone_type", "set_multitrack_mode",
        "set_recording_processing_bypass", "set_recording_format", "set_usb_multitrack_mode",
        "set_usb_multitrack_bypass", "set_monitor_auto_mute", "set_bt_auto_mute",
        "set_master_compellor", "set_master_compellor_param", "set_output_delay",
        "set_unit_setting"}


def apply_scoped(prof, scope, dev=devices.PRO2, fader_count=9):
    """Apply with every write recorded, the effects, the format and a pair's chain included.
    Returns (calls, result, the progress sentences)."""
    said = []
    with as_device(dev), recorder() as calls, patched(
            (channels, {n: _recording(n) for n in ("set_channel_effect", "set_channel_effect_param",
                                                   "write_chain")}),
            (rcp_transfer, {"set_recording_format": _recording("set_recording_format")}),
            (profiles, {"_mic_types_now": lambda: {}, "_await_voicing": lambda *_a, **_k: None})):
        res = profiles.apply(prof, fader_count=fader_count, fader_code_set=dev.fader.code_set,
                             progress=said.append, scope=scope)
        return list(calls), res, said


everything, res_all, said_all = apply_scoped(whole_prof, profiles.SCOPE_EVERYTHING)
setup, res_setup, said_setup = apply_scoped(whole_prof, profiles.SCOPE_SETUP)
check("everything writes the setup and the rest", set(names_of(everything)), SETUP_ONLY | REST)
check("the setup alone writes none of the rest", set(names_of(setup)) & REST, set())
check("and all of the setup", set(names_of(setup)), SETUP_ONLY)
check("write for write what everything sends for those", setup,
      [c for c in everything if c[0] in SETUP_ONLY])
check("the default is everything",
      names_of(apply_pro2(whole_prof)[0]),
      [n for n in names_of(everything) if n in RECORDED_TRANSFER + RECORDED_CHANNELS])
check("nothing is reported as unrestorable either way", (res_all["errs"], res_setup["errs"]),
      ([], []))
check("the setup's progress names no monitor level and no master",
      [s for s in said_setup if "monitor" in s or "master" in s or "own settings" in s], [])
check_in("but still the fader levels", "Restoring fader levels.", " ".join(said_setup))
check_in("everything still says monitor", "Restoring fader and monitor levels.",
         " ".join(said_all))
try:
    apply_scoped(whole_prof, "some")
    refused = False
except ValueError:
    refused = True
check_true("a scope that is not one is refused before anything is written", refused)

print("\nRESTORE: a linked pair, its two inputs, and an empty fader")
# The desk this pins: the Mic 3 + 4 pair on fader 3 carrying a LINE source. The pair only passes
# line audio while inputs 3 and 4 are themselves set to Line-in, and both are off-fader while
# linked, so their channel records hold the input settings and no processing.
PAIR_CHAIN = {"bools": {"channelBypassProcessing": True}, "floats": {"eqLowGain": 0.5},
              "fx_bools": {"echoOn": False}, "fx_floats": {}, "marker": -1}


def off_fader_mic(n):
    return {"name": f"Mic{n}", "label": f"Mic {n}", "kind": "mic", "level_db": 45, "mic_type": 0,
            "phantom": False, "polarity": False, "instrument_gain": 20, "advanced": None,
            "processing_on": None, "modules": {"eqOn": None}, "params": {"eqLowGain": None},
            "preset_index": None, "effects": {"echo": None, "reverb": None,
                                              "echo_params": {"echoMix": None},
                                              "reverb_params": {}}}


pair_prof = dict(profile(faders=[0, 1, 6, 9, 7, 8, -1, 11, 10],
                         **{profiles.PAIR_CHAINS_KEY: {"6": PAIR_CHAIN}}),
                 channels=[off_fader_mic(3), off_fader_mic(4)])
for scope in profiles.SCOPES:
    calls, res, _said = apply_scoped(pair_prof, scope)
    check(f"{scope}: the pair goes on its fader and the empty fader is emptied",
          [a for a in args_of(calls, "set_fader_source") if a[1] in (6, -1)], [(2, 6), (6, -1)])
    check(f"{scope}: every fader of the layout is written", len(args_of(calls, "set_fader_source")),
          9)
    check(f"{scope}: inputs 3 and 4 get their mic type back while off-fader",
          args_of(calls, "set_mic_type"), [(3, 0), (4, 0)])
    check(f"{scope}: and their gain, phantom and polarity",
          (args_of(calls, "set_mic_gain"), args_of(calls, "set_phantom"),
           args_of(calls, "set_polarity")),
          ([(3, 45), (4, 45)], [(3, False), (4, False)], [(3, False), (4, False)]))
    check(f"{scope}: nothing is written to a strip they do not have",
          [n for n in names_of(calls) if n in ("set_processing_enabled", "set_channel_processing",
                                               "set_channel_effect", "set_processing_module")], [])
    check(f"{scope}: the pair's own strip gets its chain", args_of(calls, "write_chain"),
          [(2, PAIR_CHAIN)])
    check_true(f"{scope}: after the layout and the inputs",
               names_of(calls).index("write_chain") > names_of(calls).index("set_mic_type")
               > names_of(calls).index("set_fader_source"))
    check(f"{scope}: and nothing fails", res["errs"], [])
duo_calls, _res, _said = apply_scoped(pair_prof, profiles.SCOPE_SETUP, devices.DUO, 10)
check("a desk with no such pair is not handed its code, and still gets the empty fader",
      [a for a in args_of(duo_calls, "set_fader_source") if a[1] in (6, -1)], [(6, -1)])
check("nor a chain for it", args_of(duo_calls, "write_chain"), [])

# What a save records for a pair, from a dump of the Pro II's shape with the pair on fader 3.
PAIR_VEC = [0, 1, 6, 9, 7, 8, -1, 11, 10]
desk_values = {(rcp_transfer.channel_key(s), "channelInputSource"): c
               for s, c in enumerate(PAIR_VEC + [9])}
desk_values.update({
    (rcp_transfer.channel_key(2), "channelBypassProcessing"): True,
    (rcp_transfer.channel_key(2), "eqLowGain"): 0.5,
    (rcp_transfer.channel_key(1), "eqLowGain"): 0.25,
    (rcp_transfer.channel_key(1), "channelBypassProcessing"): False,
    (rcp_transfer.input_key(1), "inputMicrophoneGain"): 27,
    (rcp_transfer.input_key(1), "inputPower"): 1,
    (rcp_transfer.input_key(1), "inputMicrophoneType"): 5,
    (rcp_transfer.input_key(2), "inputMicrophoneGain"): 45,
    (rcp_transfer.input_key(3), "inputMicrophoneGain"): 45,
    (rcp_transfer.fader_key(6), "faderLevel"): 96,
    (rcp_transfer.OUTPUT, "outputMonLevel"): 0.4,
    (rcp_transfer.OUTPUT, "outputMultiMode"): 1,
})
pair_desk = lf.skeleton(lf.PRO2_177, desk_values)
with as_device(devices.PRO2):
    saved_chains = profiles._pair_chains(pair_desk, PAIR_VEC)
    desk_chans = channels.read_channels(pair_desk)
check("a save keeps the chain of the strip a pair sits on, under its code", list(saved_chains),
      ["6"])
check("as that strip holds it", (saved_chains["6"]["bools"]["channelBypassProcessing"],
                                 saved_chains["6"]["floats"]["eqLowGain"]), (True, 0.5))
by_name = {c["name"]: c for c in desk_chans}
check("the linked mics are saved off-fader, with their input settings",
      [(by_name[n]["on_fader"], by_name[n]["level_db"], by_name[n]["mic_type"])
       for n in ("Mic3", "Mic4")], [(False, 45, 0), (False, 45, 0)])
with as_device(devices.PRO2):
    check("no pair on a fader, nothing kept",
          profiles._pair_chains(pair_desk, [0, 1, 2, 3, 10, 11, 7, 8, 9]), {})

print("\nTHE CHECK AFTER A RESTORE: the desk read back against the profile")
with as_device(devices.PRO2):
    as_saved = {"version": 2, "midi": False, "channels": copy.deepcopy(desk_chans),
                "routing": {"faders": PAIR_VEC, "fader_levels": rcp_transfer.fader_levels(pair_desk),
                            "monitor_level": 0.4, **profiles._master_settings(pair_desk),
                            profiles.PAIR_CHAINS_KEY: saved_chains},
                profiles.UNIT_KEY: profiles._unit_settings(pair_desk)}


def checked(prof, scope=profiles.SCOPE_EVERYTHING):
    with as_device(devices.PRO2):
        return profiles.check(prof, pair_desk, desk_chans, fader_count=9,
                              fader_code_set=devices.PRO2.fader.code_set, scope=scope)


match = checked(as_saved)
check("a desk that is as the profile says differs in nothing", match["differ"], [])
check_true("having compared a good many settings", match["checked"] > 100, str(match["checked"]))
check_true("fewer for the setup alone", checked(as_saved, profiles.SCOPE_SETUP)["checked"]
           < match["checked"])
check("said as one sentence", profiles.check_words(match),
      f"Read the desk back: all {match['checked']} settings checked match the profile.")

off = copy.deepcopy(as_saved)
next(c for c in off["channels"] if c["name"] == "Mic2")["level_db"] = 30
one = checked(off, profiles.SCOPE_SETUP)
check("a gain the desk did not take is named, with both values", one["differ"],
      ["Mic 2 gain reads 27, the profile says 30"])
check("and the sentence counts it in the singular", profiles.check_words(one),
      f"Read the desk back: 1 setting of {one['checked']} differs. "
      "Mic 2 gain reads 27, the profile says 30.")
next(c for c in off["channels"] if c["name"] == "Mic2")["phantom"] = False
next(c for c in off["channels"] if c["name"] == "Mic3")["mic_type"] = 2
off["routing"]["faders"] = [0, 1, 2, 9, 7, 8, 3, 11, 10]
off["routing"]["fader_levels"] = list(off["routing"]["fader_levels"])
off["routing"]["fader_levels"][6] = 100
off["routing"]["monitor_level"] = 0.9
off["routing"]["usb_multitrack"] = 2
off["midi"] = True
many = checked(off, profiles.SCOPE_SETUP)
check("a fader is named by what it holds, a pair and an empty one included", many["differ"][:2],
      ["fader 3 reads Mic 3 + 4, the profile says Mic 3",
       "fader 7 reads no channel, the profile says Mic 4"])
check_in("a level by its number", "fader 7 level reads 96, the profile says 100",
         "; ".join(many["differ"]))
check_in("a switch as on or off", "Mic 2 phantom power reads on, the profile says off",
         "; ".join(many["differ"]))
check_in("a mic type by its name", "Mic 3 mic type reads Line-in, the profile says Dynamic",
         "; ".join(many["differ"]))
check("the setup's check leaves the monitor, the USB mode and MIDI out",
      [d for d in many["differ"] if "monitor" in d or "USB" in d or "MIDI" in d], [])
whole = checked(off)
check("everything's check has them",
      sorted(d for d in whole["differ"] if "monitor" in d or "USB" in d or "MIDI" in d),
      ["MIDI control reads off, the profile says on",
       "USB multitrack reads Pre-Fader, the profile says Post-Fader",
       "the monitor speaker level reads 0.4, the profile says 0.9"])
words = profiles.check_words(many)
check_true("a long list names the first few and counts the rest",
           words.count(";") == profiles.CHECK_NAMED
           and words.endswith(f"and {len(many['differ']) - profiles.CHECK_NAMED} more."), words)
chain_off = copy.deepcopy(as_saved)
chain_off["routing"][profiles.PAIR_CHAINS_KEY] = {"6": dict(
    saved_chains["6"], floats=dict(saved_chains["6"]["floats"], eqLowGain=0.9))}
check("a pair's strip is one setting, and says which value is off first",
      checked(chain_off, profiles.SCOPE_SETUP)["differ"],
      ["Mic 3 + 4 processing differs from the profile in 1 value, the first being eqLowGain"])
near = copy.deepcopy(as_saved)
next(c for c in near["channels"] if c["name"] == "Mic2")["params"]["eqLowGain"] = 0.2505
check("a float a hair off is the same setting", checked(near)["differ"], [])

RES = {"applied": 12, "failed": 0, "errs": []}
check("the result carries the check in place of the advice to go and look",
      profiles.restore_result("Show", RES, one)["message"],
      "Restored profile Show: applied 12 settings. " + profiles.check_words(one))
check_in("a setup says it was the setup", "Restored the setup from Show: applied 12 settings.",
         profiles.restore_result("Show", RES, match, profiles.SCOPE_SETUP)["message"])
check_in("with no check the advice stays", "Open Channels, Processing or Routing",
         profiles.restore_result("Show", RES)["message"])

sys.exit(_testkit.finish())
