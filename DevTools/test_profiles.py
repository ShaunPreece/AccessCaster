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
import os
import shutil
import sys
import tempfile
from pathlib import Path

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import channels
import devices
import fake_model
import profiles
import rcp_transfer

FAILED = 0
profiles.RESTORE_PACE = 0          # no need to pace writes that never leave the process


def check(label, got, want):
    global FAILED
    if got == want:
        print(f"  ok   {label}: {got!r}")
    else:
        FAILED += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def check_true(label, got):
    check(label, bool(got), True)


class as_device:
    """Force the ACTIVE device model, so device-aware code can be tested with no desk attached."""

    def __init__(self, dev):
        self.dev = dev

    def __enter__(self):
        self.saved = devices.get_active_device
        devices.get_active_device = lambda: self.dev
        return self.dev

    def __exit__(self, *exc):
        devices.get_active_device = self.saved
        return False


# --- a synthetic model blob, every property on the root node (see fake_model) -------------------

def prop_int(name, value):
    return (None, name, int(value))


def prop_float(name, value):
    return (None, name, float(value))


def prop_bool(name, value):
    return (None, name, bool(value))


def prop_str(name, value):
    return (None, name, str(value))


# Nine faders: the six physical pots read 0, the three virtual ones carry a level. Two headphone
# outputs with DIFFERENT types, the second of them "Low sensitivity" (2) - the value that used to
# be dropped. Monitor speaker at 3 percent, which is where Shaun's desk actually sits.
FADER_LEVEL_VALUES = [0, 0, 0, 0, 0, 0, 100, 64, 127]
blob = fake_model.encode(
    [prop_int("channelInputSource", c) for c in (0, 7, 2, 3, 10, 11, 1, 8, 9, 9)]
    + [prop_bool("systemMidiControl", True)]
    + [prop_int("faderLevel", v) for v in FADER_LEVEL_VALUES]
    + [prop_float("outputMonLevel", 0.03)]
    + [prop_int("headphoneType", 1), prop_int("headphoneType", 2)]
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
       prop_str("sipAccountPassword", "s3cret"), prop_int("someUndecodedSetting", 42)]
)


# --- what SAVE records ---------------------------------------------------------------------------

print("SAVE: the fader, monitor and headphone levels")
tmp = tempfile.mkdtemp(prefix="ac_profiles_test_")
saved_dir = profiles.PROFILES_DIR
profiles.PROFILES_DIR = tmp
try:
    with as_device(devices.PRO2):
        res = profiles.save("test", blob, [], "1.0.0", 10, headphone_output_count=2)
    check_true("save reports ok", res.get("ok"))
    prof = profiles.load("test")["prof"]
    r = prof["routing"]
    check("every fader level captured RAW (0..127)", r["fader_levels"], FADER_LEVEL_VALUES)
    check("monitor speaker level captured raw (0..1)", round(r["monitor_level"], 3), 0.03)
    check("one headphone type per output", r["headphone_types"], [1, 2])
    check("legacy single headphone_type is still Headphone 1's", r["headphone_type"], 1)

    with as_device(devices.PRO2):
        profiles.save("test1hp", blob, [], "1.0.0", 10, headphone_output_count=1)
    r1 = profiles.load("test1hp")["prof"]["routing"]
    check("a device with one headphone output records one type", r1["headphone_types"], [1])

    print("\nSAVE: the unit's own settings (display, buttons, haptics, REC button, listen mode)")
    unit = prof[profiles.UNIT_KEY]
    check("every unit setting in the address table is recorded",
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
        profiles.save("keeper", blob, [], "1.0.0", 10, headphone_output_count=2)
        profiles.save("movable", blob, [], "1.0.0", 10, headphone_output_count=2)
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

    # A legacy flat profiles/<name>.json has no folder, so both operations have to handle it.
    (Path(tmp) / "old style.json").write_text('{"midi": true}', encoding="utf-8")
    check_true("a legacy flat profile renames too", profiles.rename("old style", "new style")["ok"])
    check_true("and still loads afterwards", profiles.load("new style")["ok"])
    check_true("a legacy flat profile deletes too", profiles.delete("new style")["ok"])
    check("leaving only the keeper", profiles._list_profile_names(), ["keeper"])
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
import soundpad

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
check("an empty pad on the unit is caught", m(SAVED_SOUND, live(present=False)), False)
check("a missing pad on the unit is caught", m(SAVED_SOUND, None), False)
check("a real gain difference is caught", m(SAVED_SOUND, live(gain=-5.0)), False)
check_true("but gain is compared at the 1dp the live read reports, not on raw floats",
           m(dict(SAVED_SOUND, gain=-6.04), live(gain=-6.0)))
check("a null live gain against a saved one is caught", m(SAVED_SOUND, live(gain=None)), False)
# A field the profile never recorded cannot differ: the restore would not write it either.
check_true("a field the profile did not record is ignored",
           m(dict(SAVED_SOUND, play_mode=None), live(play_mode=2)))

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
    check("a name the unit does not have is caught",
          a(str(root), 5, "other.wav", str(backup / "5" / "airhorn.wav")), False)
    check("a profile entry with no filename is caught",
          a(str(root), 5, None, str(backup / "5" / "airhorn.wav")), False)
    check("a backup whose local copy is missing is caught",
          a(str(root), 5, "airhorn.wav", str(backup / "5" / "gone.wav")), False)
    check("a grid with no file on the unit is caught",
          a(str(root), 7, "airhorn.wav", str(backup / "5" / "airhorn.wav")), False)
    # Leftovers. A pad folder can hold a file from an earlier replacement (measured on the real
    # desk: pads/1 held a stray wav beside Music_Bed.wav). Which file the pad PLAYS is what matters,
    # and only the desk can say - padFilePath, passed in as live_file.
    (root / "pads" / "5" / "leftover.wav").write_bytes(b"RIFF" + b"	" * 64)
    check("with no word from the desk, two files in a folder is caught",
          a(str(root), 5, "airhorn.wav", str(backup / "5" / "airhorn.wav")), False)
    check_true("but told which file the pad plays, a leftover beside it is irrelevant",
               a(str(root), 5, "airhorn.wav", str(backup / "5" / "airhorn.wav"),
                 live_file="airhorn.wav"))
    check("a pad that plays the leftover instead is caught",
          a(str(root), 5, "airhorn.wav", str(backup / "5" / "airhorn.wav"),
            live_file="leftover.wav"), False)
    check("a pad playing a file that is not there at all is caught",
          a(str(root), 5, "airhorn.wav", str(backup / "5" / "airhorn.wav"),
            live_file="vanished.wav"), False)
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

    removed = soundpad.tidy_pad_folder(str(root), 1, "Music_Bed.wav")
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
          soundpad.tidy_pad_folder(str(root), 1, ""), [])
    check("so both files are still there",
          sorted(f.name for f in (root / "pads" / "1").iterdir()),
          ["Music_Bed.wav", "Stray.wav"])
    check("only pads/ folders are ever listed",
          sorted(os.path.basename(d) for d in soundpad._pad_audio_dirs(str(root))), ["1", "17"])
finally:
    shutil.rmtree(ttmp, ignore_errors=True)


# --- what RESTORE writes -------------------------------------------------------------------------

CALLS: list = []


class recorder:
    """Replace the device-write functions with recorders for the duration of the block."""
    _TRANSFER = ("set_midi_control", "set_fader_source", "set_fader_level", "set_monitor_level",
                 "set_headphone_type", "set_multitrack_mode", "set_monitor_auto_mute",
                 "set_bt_auto_mute", "set_master_compellor", "set_master_compellor_param",
                 "set_output_delay", "set_unit_setting",
                 "set_recording_processing_bypass", "set_usb_multitrack_mode",
                 "set_usb_multitrack_bypass")
    _CHANNELS = ("note_strip_source", "invalidate_strip_vector",
                 "set_mic_gain", "set_mic_type", "set_phantom", "set_polarity",
                 "set_instrument_gain", "set_channel_level", "set_channel_advanced",
                 "set_processing_enabled", "set_processing_module", "set_channel_processing",
                 "set_channel_preset_marker")

    def __enter__(self):
        CALLS.clear()
        self.saved = []
        for mod, names in ((rcp_transfer, self._TRANSFER), (channels, self._CHANNELS)):
            for n in names:
                self.saved.append((mod, n, getattr(mod, n)))
                setattr(mod, n, (lambda n=n: (lambda *a, **k: CALLS.append((n, a))))())
        return CALLS

    def __exit__(self, *exc):
        for mod, n, fn in self.saved:
            setattr(mod, n, fn)
        return False


def names_of(calls):
    return [n for n, _a in calls]


def args_of(calls, name):
    return [a for n, a in calls if n == name]


def profile(**routing):
    """A v2 profile carrying only routing (no channels), so the tests stay on the new keys."""
    r = {"faders": [0, 7, 2, 3, 10, 11, 1, 8, 9],
         "fader_levels": FADER_LEVEL_VALUES,
         "monitor_level": 0.03,
         "headphone_types": [1, 2]}
    r.update(routing)
    return {"version": 2, "channels": [], "routing": r}


def apply_pro2(prof):
    with as_device(devices.PRO2), recorder() as calls:
        res = profiles._apply_v2(prof, fader_count=9, fader_code_set=devices.PRO2.fader.code_set,
                                 mic_channels=(1, 2, 3, 4))
        return list(calls), res


print("\nRESTORE on the Pro II: the levels now land")
calls, res = apply_pro2(profile())
check("one level write per VIRTUAL fader (7, 8, 9)", len(args_of(calls, "set_fader_level")), 3)
check("each virtual fader's confirmed selector + its saved raw level",
      args_of(calls, "set_fader_level"), [(0x0A, 100), (0x0B, 64), (0x0C, 127)])
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
# is thrown away. It cost a clean restore: the profile said 47, SM7B followed it, the desk kept 55.
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

print("\nRESTORE: the SMART pads bus is a channel like any other")
# Its strip carries a full chain and the desk processes pads through it, measured on hardware.
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
check("both headphone outputs written, each on its own selector",
      args_of(calls, "set_headphone_type"), [(1, 0x09), (2, 0x0A)])
check_true("'Low sensitivity' (2) is applied, not silently dropped",
           (2, 0x0A) in args_of(calls, "set_headphone_type"))
calls_bad, _ = apply_pro2(profile(headphone_types=[0, 99]))
check("a value the device does not offer is skipped, not written",
      args_of(calls_bad, "set_headphone_type"), [(0, 0x09)])
calls_legacy, _ = apply_pro2(profile(headphone_types=None, headphone_type=2))
check("an older profile's single value restores to Headphone 1",
      args_of(calls_legacy, "set_headphone_type"), [(2, 0x09)])

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
# hole: they all sit on the OUTPUT block 0x0D, which is confirmed on BOTH desks (rcp_transfer's own
# self-check asserts the Duo's recordingMultitrackMode record byte-for-byte). This was true even
# while the Duo lacked the "recorder" cap, because what a recording CONTAINS is an OUTPUT setting
# and not the transport. The cap came on and nothing here needed to change,
# which is the point of keeping those two apart.
with as_device(devices.DUO), recorder() as duo_mt:
    profiles._apply_v2(profile(multitrack=2, processing_bypass=True, usb_multitrack=1,
                               usb_processing_bypass=True),
                       fader_count=10, fader_code_set=devices.DUO.fader.code_set,
                       mic_channels=(1, 2))
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
# The Duo's faderLevel addresses are DERIVED (FaderSpec.level_selectors) and the desk confirms them
# by acting on them, so a profile restores them here exactly as on a Pro II. What matters is WHICH:
# the five VIRTUAL faders, indices 4-8 -> 0x08..0x0c. The four physical ones are analog pots and
# must get nothing.
with as_device(devices.DUO), recorder() as duo_calls:
    duo_res = profiles._apply_v2(profile(), fader_count=10,
                                 fader_code_set=devices.DUO.fader.code_set,
                                 mic_channels=(1, 2))
    duo_calls = list(duo_calls)
check("the five VIRTUAL faders are written, and only those",
      sorted(a[0] for a in args_of(duo_calls, "set_fader_level")), [0x08, 0x09, 0x0A, 0x0B, 0x0C])
check_true("the monitor level is written too", args_of(duo_calls, "set_monitor_level"))
check("levels are no longer reported as unrestorable",
      [e for e in duo_res["errs"] if "level" in e], [])
check("headphone type still restores (both desks share the confirmed selectors)",
      args_of(duo_calls, "set_headphone_type"), [(1, 0x09), (2, 0x0A)])

print("\nRESTORE: the unit's own settings")
UNIT = {"screenBrightness": 180, "autoBrightness": 0, "screenDimAfterSeconds": 1800,
        "outputPrefader": 1, "systemHapticSetting": 2}
calls_unit, res_unit = apply_pro2(dict(profile(), **{profiles.UNIT_KEY: UNIT}))
check("every saved unit setting is written back",
      sorted(args_of(calls_unit, "set_unit_setting")),
      sorted((k, v) for k, v in UNIT.items()))
check("nothing was reported as unrestorable", res_unit["errs"], [])
# The two output MUTES are in the address table and ARE saved, but a restore must never write them.
# A profile has to be safe to run mid-session, and silencing the speakers because that is how they
# sat when the profile was taken would be a nasty surprise with no visible cause. Same reasoning
# that keeps the channel mutes out. The FLOAT is here too: it is the one type the save path used to
# drop, so it is worth pinning that it survives the round trip and is written back.
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
check("an older profile writes no unit settings and reports no failure",
      (args_of(calls_nounit, "set_unit_setting"), res_nounit["errs"]), ([], []))

print("\nRESTORE on the Duo: the unit settings ARE written")
# The Duo holds the "unit_settings" cap on all 20 selectors resolving against its OWN TREE plus one
# write per block proven on hardware, so a profile taken on a Duo restores its Device page rather
# than skipping it. The mutes are held back, on both desks, because a restore must never silently
# mute somebody.
with as_device(devices.DUO), recorder() as duo_unit_calls:
    duo_unit_res = profiles._apply_v2(dict(profile(), **{profiles.UNIT_KEY: UNIT}), fader_count=10,
                                      fader_code_set=devices.DUO.fader.code_set,
                                      mic_channels=(1, 2))
    duo_unit_calls = list(duo_unit_calls)
duo_unit_written = dict(args_of(duo_unit_calls, "set_unit_setting"))
check_true("the Duo now restores its unit settings", bool(duo_unit_written))
check_true("and that is no longer reported as a failure",
           not any("unit settings" in e for e in duo_unit_res["errs"]))
check("a saved output mute is still NOT written back on the Duo either",
      sorted(p for p in duo_unit_written if "Mute" in p), [])

print("\nWRITE PATH: set_unit_setting refuses a property it has no address for")
try:
    rcp_transfer.set_unit_setting("definitelyNotAProperty", 1)
    check("it raised", False, True)
except ValueError as exc:
    check_true("ValueError, naming the property", "definitelyNotAProperty" in str(exc))

print("\nRESTORE of a LEGACY (flat) profile: the same headphone fix")
with as_device(devices.PRO2), recorder() as calls_flat:
    profiles._apply_legacy({"routing": {"headphone_type": 2}}, input_strip_vec_index={},
                           valid_source_codes=set(), mic_channels=(1, 2, 3, 4))
    calls_flat = list(calls_flat)
check("'Low sensitivity' is no longer dropped from a legacy profile either",
      args_of(calls_flat, "set_headphone_type"), [(2, 0x09)])

print(f"\n{'ALL PASS' if not FAILED else str(FAILED) + ' FAILED'}")
sys.exit(1 if FAILED else 0)
