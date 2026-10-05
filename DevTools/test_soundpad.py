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

import soundpad

FAILED = 0


def check(label, got, want):
    global FAILED
    if got == want:
        print(f"  ok   {label}: {got!r}")
    else:
        FAILED += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


class Stubs:
    """Replace module attributes for the length of a `with`, and put them back."""

    def __init__(self, target, **attrs):
        self.target, self.attrs, self.saved = target, attrs, {}

    def __enter__(self):
        for k, v in self.attrs.items():
            self.saved[k] = getattr(self.target, k)
            setattr(self.target, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(self.target, k, v)
        return False


print("transfer mode is left even when the work inside fails")
calls = []
with Stubs(soundpad.t, set_transfer_mode=lambda on: calls.append(on)), \
        Stubs(soundpad.time, sleep=lambda s: None):
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
    with Stubs(soundpad,
               create_mixer=lambda grid, mode: create_result,
               wait_for_pad_present=lambda idx: present,
               set_pad_colour=lambda grid, colour: colour_result,
               set_pad_name=lambda grid, name: {"ok": True},
               pool_health=lambda blob=None: CLEAN):
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

print("the CLI recipe list includes the presets that belong to no pad")
with Stubs(soundpad.sys, argv=["soundpad.py", "restore", "list"]):
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
print("ALL CHECKS PASSED" if not FAILED else f"{FAILED} CHECK(S) FAILED")
sys.exit(1 if FAILED else 0)
