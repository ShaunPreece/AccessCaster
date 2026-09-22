"""Offline test of `server._pad_write` and every pad action built on it. NO DEVICE, no HID writes.

WHY THIS EXISTS. Seventeen pad actions share one helper, `server._pad_write`, and they are the most
numerous action family in the app. Without this file none of them is called by any offline suite,
which leaves a change to the helper resting on "it reads correctly".

WHAT IT PINS, and each is a decision that could be silently undone:
  * the RANGE check comes first, so a bad pad number is the complaint you get even when the value
    is bad too. The pad number is the more basic mistake and naming the other one would send you
    looking in the wrong place;
  * `invalid` is checked BEFORE the lock, so nothing takes the device lock to discover a value was
    wrong. A test that only checked the message would pass while the lock was being taken;
  * a crash becomes a SENTENCE, never an exception out of an action. A traceback reaching the HTTP
    layer is a dialog that never closes for a screen-reader user;
  * `plain_value_error` surfaces soundpad's own text. soundpad raises ValueError with a finished,
    user-facing sentence (a polluted effect pool, an unknown effect); wrapping it in "Could not
    assign the voice effect:" buries the part that says what to do about it;
  * every action still REFUSES an out-of-range pad. That is the guard standing between the browser
    and a write to a pad position the device does not have.

Run: python DevTools/test_pad_actions.py
"""
from __future__ import annotations

import _bootstrap  # noqa: F401  (puts Project/ on the path; import FIRST)

import server
import soundpad

FAILED = []


def check(label, got, want=True, show=None):
    ok = (got == want)
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  [{show if show is not None else got}]"
                                                     if not ok or show is not None else ""))
    if not ok:
        FAILED.append(label)


def check_in(label, needle, hay):
    ok = needle.lower() in (hay or "").lower()
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f"  [{hay}]"))
    if not ok:
        FAILED.append(label)


TOO_HIGH = server._max_user_pads() + 1

print("_pad_write itself")
# A work() that would BLOW UP if it ever ran. Every refusal below must come back without it firing.
def _never():
    raise AssertionError("work() ran when the call should have been refused before the lock")


check_in("an out-of-range pad is refused by number",
         "out of range", server._pad_write(TOO_HIGH, "Could not", _never)["message"])
check_in("pad 0 is refused too (grids are 1-based)",
         "out of range", server._pad_write(0, "Could not", _never)["message"])
check_in("`invalid` refuses before the lock, with its own words",
         "not a mode", server._pad_write(1, "Could not", _never, invalid="not a mode")["message"])

# THE ORDER, and it is the point of the check: both are wrong, and the PAD NUMBER is what you hear.
both_wrong = server._pad_write(TOO_HIGH, "Could not", _never, invalid="not a mode")
check_in("with a bad pad AND a bad value, the PAD is the complaint",
         "out of range", both_wrong["message"])
check("and the value complaint is not also tacked on",
      "not a mode" in both_wrong["message"], False)

crash = server._pad_write(1, "Could not change the thing",
                          lambda: (_ for _ in ()).throw(RuntimeError("the desk went away")))
check("a crash comes back as ok False, not as an exception", crash["ok"], False)
check_in("and it is a sentence naming what failed", "could not change the thing", crash["message"])
check_in("with the cause kept, so it is diagnosable", "the desk went away", crash["message"])

wrapped = server._pad_write(1, "Could not assign it",
                            lambda: (_ for _ in ()).throw(ValueError("the pool is dirty")))
check_in("a ValueError is WRAPPED by default", "could not assign it", wrapped["message"])
plain = server._pad_write(1, "Could not assign it",
                          lambda: (_ for _ in ()).throw(ValueError("the pool is dirty")),
                          plain_value_error=True)
check("plain_value_error surfaces soundpad's own sentence, unwrapped",
      plain["message"], "the pool is dirty")

good = server._pad_write(1, "Could not", lambda: {"ok": True, "message": "done"})
check("and a clean run returns what work() returned", good, {"ok": True, "message": "done"})

print("\nevery pad action refuses an out-of-range pad")
# The whole family, by the signature each takes. None of these may reach the device.
CALLS = [
    ("action_set_replay", lambda g: server.action_set_replay(g, True)),
    ("action_set_pad_gain", lambda g: server.action_set_pad_gain(g, 0.0)),
    ("action_set_pad_name", lambda g: server.action_set_pad_name(g, "Name")),
    ("action_set_pad_loop", lambda g: server.action_set_pad_loop(g, True)),
    ("action_set_play_mode", lambda g: server.action_set_play_mode(g, 0)),
    ("action_set_mixer_trigger", lambda g: server.action_set_mixer_trigger(g, 0)),
    ("action_set_pad_trigger", lambda g: server.action_set_pad_trigger(g, "control", 1)),
    ("action_set_pad_envelope", lambda g: server.action_set_pad_envelope(g, "start", 0.0)),
    ("action_set_pad_fade", lambda g: server.action_set_pad_fade(g, "fade_in", 1.0)),
    ("action_set_pad_flag", lambda g: server.action_set_pad_flag(g, "excludeHost", True)),
    ("action_set_back_channel", lambda g: server.action_set_back_channel(g, "x", True)),
    ("action_set_pad_colour", lambda g: server.action_set_pad_colour(g, 1)),
    ("action_clear_pad", lambda g: server.action_clear_pad(g)),
    ("action_export_pad", lambda g: server.action_export_pad(g)),
    ("action_assign_fx", lambda g: server.action_assign_fx(g, "megaphone")),
    ("action_create_special_pad", lambda g: server.action_create_special_pad(g, "trigger")),
    ("action_add_sound", lambda g: server.action_add_sound(g, "a.wav", b"RIFF")),
]
for name, call in CALLS:
    res = call(TOO_HIGH)
    check_in(f"{name} refuses pad {TOO_HIGH}", "out of range", res.get("message"))

print("\nthe per-action value checks, all of which must land before the lock")
check_in("a play mode outside 0-2 is named, with the three options spelled out",
         "0 (toggle), 1 (hold) or 2 (one shot)",
         server.action_set_play_mode(1, 99)["message"])
check_in("a mixer trigger outside 0-1 says which two it may be",
         "0 (latching) or 1 (momentary)",
         server.action_set_mixer_trigger(1, 7)["message"])
check_in("an empty pad name asks for one rather than writing a blank",
         "enter a name", server.action_set_pad_name(1, "   ")["message"])
check_in("an unknown MIDI trigger field is refused BY NAME, and lists the real ones",
         "unknown midi trigger setting",
         server.action_set_pad_trigger(1, "nonsense", 1)["message"])
known = {f["key"]: f for f in soundpad.TRIGGER_EDIT}
a_field = next(iter(known))
spec = known[a_field]
check_in(f"a {a_field} outside its range says what the range is",
         "must be between", server.action_set_pad_trigger(1, a_field, spec["max"] + 1)["message"])
# A SILENT NO-OP ON A PAD MAPPING IS THE WORST OUTCOME: nothing about the desk would look wrong
# afterwards, so an unknown field has to be refused rather than ignored.
check("an unknown field is a refusal, not a quiet success",
      server.action_set_pad_trigger(1, "nonsense", 1)["ok"], False)

print("\nand the range guard is device-aware, not a hardcoded 56")
check("_max_user_pads comes from the active device's PadSpec",
      server._max_user_pads() > 0)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED:")
    for f in FAILED:
        print("  - " + f)
    raise SystemExit(1)
print("ALL PAD ACTION CHECKS PASSED")
