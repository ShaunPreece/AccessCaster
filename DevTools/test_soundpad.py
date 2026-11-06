"""Offline tests of soundpad's write plumbing: transfer mode is always left, and a pad restore
reports a create or a setting that did not take instead of counting the pad as restored.

Every device call is stubbed; nothing here reaches a desk.

Run: python DevTools/test_soundpad.py
"""
from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401
import _testkit
import soundpad
from _testkit import check, patched

print("transfer mode is left even when the work inside fails")
calls = []
with patched((soundpad.t, {"set_transfer_mode": lambda on: calls.append(on)}),
             (soundpad.time, {"sleep": lambda s: None})):
    try:
        with soundpad._transfer_mode(1.0):
            raise RuntimeError("copy failed")
    except RuntimeError:
        pass
check("enter then exit", calls, [True, False])


CLEAN = {"clean": True, "duplicates": [], "orphans": [], "missing": [], "detached": [],
         "partial": [], "effects_idx": []}
MIXER_PAD = {"grid": 12, "type": soundpad.PAD_TYPE_MIXER, "name": "Censor", "colour": 11,
             "mixer_mode": 0}


def restore(create_result, colour_result, present=True):
    with patched((soundpad, {
            "create_mixer": lambda grid, mode: create_result,
            "wait_for_pad_present": lambda idx: present,
            "set_pad_property": lambda grid, name, value: (
                colour_result if name == "padColourIndex" else {"ok": True}),
            "pool_health": lambda blob=None: CLEAN})):
        return soundpad.restore_pads([dict(MIXER_PAD)], "unused", verify_first=False)


print("a restore whose create fails reports the pad as failed")
res = restore({"ok": False, "created": False, "message": "Could not create the Censor action: x"},
              {"ok": True})
check("restore ok", res["ok"], False)
check("pad ok", res["results"][0]["ok"], False)
check("reason given", res["results"][0]["message"], "Could not create the Censor action: x")

print("a restore whose pad never appears reports the pad as failed")
res = restore({"ok": True, "created": True, "message": ""}, {"ok": True}, present=False)
check("pad ok", res["results"][0]["ok"], False)

print("a restore whose create worked on an unclean pool still counts")
res = restore({"ok": False, "created": True, "message": ""}, {"ok": True})
check("pad ok", res["results"][0]["ok"], True)

print("a restore whose setting fails reports the pad as not fully restored")
res = restore({"ok": True, "created": True, "message": ""},
              {"ok": False, "message": "Pad 12 is empty; nothing to edit."})
check("restore ok", res["ok"], False)
check("names the setting", res["results"][0]["message"].endswith(
    "colour: Pad 12 is empty; nothing to edit."), True)

FX_PAD = {"grid": 5, "type": soundpad.PAD_TYPE_VOICE_FX, "name": "Radio",
          "fx_stack": {"reverb": {}}, "colour": 8}


def restore_fx(effects_idx):
    health = dict(CLEAN, clean=False, duplicates=[4], effects_idx=effects_idx)
    with patched((soundpad, {
            "safe_assign_fx_recipe": lambda *a, **k: {"ok": False, "health": health},
            "wait_for_pad_present": lambda idx: True,
            "set_pad_property": lambda grid, name, value: {"ok": True},
            "pool_health": lambda blob=None: CLEAN})):
        return soundpad.restore_pads([dict(FX_PAD)], "unused", verify_first=False)


print("a voice-effect pad whose create left it two effect objects is not counted as restored")
res = restore_fx([4, 4])
check("restore ok", res["ok"], False)
check("pad ok", res["results"][0]["ok"], False)
check("names the effect", "effect: " in res["results"][0]["message"], True)

print("one with its own object, on a pool unclean elsewhere, still counts")
check("pad ok", restore_fx([4, 2, 2])["results"][0]["ok"], True)

print("replacing a pad whose create fails says the old pad is already gone")


def fails():
    raise RuntimeError("volume did not mount")


with patched((soundpad, {"_position_of": lambda idx: (4, soundpad.PAD_TYPE_SOUND, "Applause"),
                         "clear_slot": lambda pos, ptype: None,
                         "pool_health": lambda blob=None: CLEAN}),
             (soundpad.time, {"sleep": lambda s: None})):
    res = soundpad._replace_slot(5, "sound", fails)
check("the message says so", res["message"],
      "Could not create the sound: volume did not mount. Sound Applause was already cleared from "
      "pad 5, so it is empty.")

print("a pad is named by its place on the desk, as the pad list names it")
check("first pad of bank 3", soundpad.pad_place(17), "pad 1, bank 3")
check("last pad of bank 1", soundpad.pad_place(8), "pad 8, bank 1")

print("the CLI recipe list includes the presets that belong to no pad")
with patched((soundpad.sys, {"argv": ["soundpad.py", "restore", "list"]})):
    out = io.StringIO()
    with redirect_stdout(out):
        soundpad._cli()
check("every recipe listed", len(out.getvalue().splitlines()), len(soundpad.RECIPES))

print("a preview value that is not a number is refused as a ValueError")
try:
    soundpad.sanitise_preview_edit("reverbMix", None)
    check("refused", False, True)
except ValueError:
    check("refused", True, True)

print()
sys.exit(_testkit.finish())
