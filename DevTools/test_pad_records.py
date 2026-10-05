"""The pad create builders emit the records they always have. Offline, no device.

Two kinds of check. The byte oracles are the RODE app's own records, from captures: a padFilePath
write and remountPadStorage. The digests freeze each builder's whole output at the parameters the
original capture comparisons used, so any change to any byte of any record fails here; a change
that is meant has to be proven on the desk and the digest updated with it.

The structural checks are the rules a create depends on: the target padIdx is stamped into
padTriggerControl and effectsIdx, and only a voice-FX create writes the 0x9d effects block. Run:
    python DevTools/test_pad_records.py
"""
import _bootstrap  # noqa: F401  (puts Project/ on the path; import FIRST)

import hashlib

import pad_records as pr

FAILED = []


def check(label, ok, detail=""):
    if ok:
        print(f"  ok  {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL {label}\n       {detail}")


def digest(recs):
    return hashlib.sha256(b"".join(recs)).hexdigest()[:16]


def int_after(rec, name):
    """The i32 value of the int property `name` in `rec`, or None if it is not an int block."""
    k = rec.find(name.encode()) + len(name) + 1
    if rec[k:k + 3] != bytes([0x01, 0x05, 0x01]):
        return None
    return int.from_bytes(rec[k + 3:k + 7], "little", signed=True)


def fx_records(recs):
    return [r for r in recs if r[9] == pr.FRAME_FX]


print("Captured bytes")
# A padFilePath write on handle 1, as the RODE app sent it (pad 2 -> sound.wav).
captured_path = bytes.fromhex(
    "033f000000010102029a02010170616446696c6550617468000129052f4170706c"
    "69636174696f6e2f656d6d632d646174612f706164732f322f736f756e642e7761"
    "7600").ljust(256, b"\x00")
check("write_record reproduces the captured padFilePath record",
      pr.write_record("padFilePath", "str", pr.PAD_FILE_PATH_FMT.format(n=2, name="sound.wav"),
                      1) == captured_path)
check("the remount record is the captured one, framed to 256 bytes",
      len(pr.REMOUNT_PAD_STORAGE) == 256
      and pr.REMOUNT_PAD_STORAGE[10:27] == b"remountPadStorage")

print("Frozen output")
sound = pr.build_sound_create(8, 9, "Chimes", 8, handle=0x12)
mp3 = pr.build_sound_create(8, 9, "Chimes", 8, handle=0x12, replay=False, filename="Chimes.mp3")
mixer = pr.build_mixer_create(20, "Mixer21", 8, mode=0, trigger_mode=0, handle=0x19)
trig = pr.build_trigger_create(17, "Trigger18", 5, handle=0x1a)
trig_custom = pr.build_trigger_create(17, "Cust", 5, handle=0x1a, custom=True, trigger_type=1,
                                      control=60, channel=2, on_val=100, off_val=0)
monster = pr.build_fx_create_recipe(10, "Monster", 3,
                                    {"reverb": {}, "pitchShift": {"pitchShiftSemitones": -8.0}},
                                    handle=0x17, fx_subhandle=0x05)
for label, recs, want in (
        ("blank with effects", pr.build_blank(8, 0x12, 0x0B), "68aeb2adc7a47151"),
        ("sound create", sum(sound, []), "c12e2601ce98608a"),
        ("sound create, MP3, no replay", sum(mp3, []), "1db0d574b2784c0d"),
        ("mixer create (Censor)", sum(mixer, []), "f3e26d7271a3623e"),
        ("trigger create, default mapping", sum(trig, []), "af2a233e7dd530ac"),
        ("trigger create, custom mapping", sum(trig_custom, []), "5d80f2e01ad4feac"),
        ("voice-FX create (Monster)", sum(monster, []), "241364048c77bb19")):
    got = digest(recs)
    check(f"{label} is unchanged", got == want, f"digest {got}, expected {want}")

print("Structure")
check("phase 1 is 45 records with its handoff, 66 with the effects block",
      (len(pr.build_blank(3, 0x10)), len(pr.build_blank(3, 0x10, 0x04))) == (45, 66))
phase1 = pr.build_phase1(12, 0x10)
check("phase 1 stamps padTriggerControl and padIdx with the target",
      [int_after(r, n) for n in ("padTriggerControl", "padIdx")
       for r in phase1 if f"{n}\0".encode() in r] == [12, 12])
effects = pr.build_effects(12, 0x04, {"reverbMix": 0.25})
check("the effects block stamps effectsIdx with the target",
      [int_after(r, "effectsIdx") for r in effects if b"effectsIdx" in r] == [12])
check("a param override lands in the effects block",
      any(b"reverbMix" in r and r.find(bytes([0x01, 0x09, 0x04])) > 0 for r in effects)
      and effects != pr.build_effects(12, 0x04))
for label, parts in (("sound", sound), ("mixer", mixer), ("trigger", trig)):
    check(f"a {label} create writes nothing to the effects node",
          fx_records(sum(parts, [])) == [])
check("a voice-FX create turns each engine on in post, on the sub-handle",
      [r[13:13 + r[13:].index(0)] for r in fx_records(monster[2])]
      == [b"reverbOn", b"pitchShiftOn"]
      and all(r[12] == 0x05 for r in fx_records(monster[2])))
try:
    pr.build_fx_create_recipe(1, "x", 1, {"flanger": {}}, handle=1, fx_subhandle=1)
    check("an unknown effect engine is refused", False, "no error raised")
except ValueError:
    check("an unknown effect engine is refused", True)
check("the custom trigger mapping writes the explicit control number",
      [int_after(r, "padTriggerControl") for r in trig_custom[1] if b"padTriggerControl" in r]
      == [60])
check("free_handle is the record count: the first free stream position",
      pr.free_handle(23) == 23)

print(f"\n{'ALL CHECKS PASSED' if not FAILED else str(len(FAILED)) + ' CHECK(S) FAILED'}")
raise SystemExit(1 if FAILED else 0)
