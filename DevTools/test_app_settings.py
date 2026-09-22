"""The app's OWN settings export and import. Offline, no device, no files touched.

WHAT THIS EXISTS TO STOP, and it is one thing above all others: `remote_token` is the credential
that grants remote access to this app, it lives in the same settings file as the keyboard shortcuts,
and an export is a file people email to themselves. A dump of settings.json would have carried it.
So the export is an ALLOW-LIST, and the first two checks here are that the token does not leave and
that an import cannot bring one in.

The second thing it pins is that IMPORT MERGES rather than replaces. The excluded keys live in the
same file, so a replace would delete this machine's token and its first-run record as a side effect
of restoring somebody's keyboard shortcuts.

Run: python DevTools/test_app_settings.py
"""
from __future__ import annotations
import json
import sys

import _bootstrap  # noqa: F401
import app_settings as a

FAILED = 0


def check(name, got, want=None):
    global FAILED
    ok = (got == want) if want is not None else bool(got)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f": got {got!r}, want {want!r}"))
    if not ok:
        FAILED += 1


# Every key the app is known to WRITE to settings.json. Keep this in step with the update_settings
# calls in server.py; the point of the check below is that a new preference cannot quietly become
# unbackupable by nobody having decided about it.
WRITTEN_BY_THE_APP = {
    "hotkeys", "announcer_levels", "announcer_volume", "announcer_voice", "announcer_output",
    "record_mute_announcer", "meter_stream", "meter_warn_clip", "meter_warn_silence",
    "meter_silence_after", "meter_speak_every", "meter_clip_sound", "meter_clip_db",
    "remote_token", "applied_presets", "baseline_asked",
}

# THE REAL STORED SHAPE, copied from a live settings.json rather than invented. An invented shape
# like {"hk1": ..., "hk2": ...} passes while the counting code is broken, because on the real shape
# len() returns 2 for ever: the two keys are "enabled" and "shortcuts". A fixture that is not the
# real shape tests nothing.
REAL_HOTKEYS = {
    "enabled": True,
    "shortcuts": [
        {"id": "sc1", "command": "app.announcer", "mods": ["ctrl", "alt", "win"], "key": "A",
         "enabled": True, "speak": True},
        {"id": "sc2", "command": "app.mics", "mods": ["ctrl", "alt", "win"], "key": "M",
         "enabled": True, "speak": True},
        {"id": "sc3", "command": "rec.toggle", "mods": ["ctrl", "alt", "win"], "key": "R",
         "enabled": True, "speak": True},
    ],
}

SAMPLE = {
    "hotkeys": REAL_HOTKEYS,
    "announcer_volume": 80,
    "announcer_levels": True,
    "meter_clip_db": -3.0,
    "announcer_voice": "Hazel",
    "announcer_output": "Speakers (Nonexistent)",
    "remote_token": "SECRET-DO-NOT-LEAK",
    "applied_presets": {"Mic1": "Broadcast"},
    "baseline_asked": ["9BF8-ED22"],
}

print("EVERY SETTING HAS BEEN DECIDED ABOUT")
check("no key the app writes is missing from all three lists",
      sorted(WRITTEN_BY_THE_APP - a.KNOWN), [])

print("\nTHE EXPORT IS AN ALLOW-LIST, AND THE CREDENTIAL NEVER LEAVES")
exported = a.build_export(SAMPLE)
blob = json.dumps(exported)
check("the remote-access token is not in the file", "SECRET-DO-NOT-LEAK" not in blob, True)
for key in a.EXCLUDED:
    check(f"{key} is excluded", key not in exported["settings"], True)
check("the shortcuts DID travel", "hotkeys" in exported["settings"], True)
check("and so did the ordinary preferences",
      all(k in exported["settings"] for k in ("announcer_volume", "meter_clip_db")), True)
check("it is marked as ours", exported["marker"], a.MARKER)
# A key absent from settings must be absent from the export, not exported as null: a null would
# stamp over a real value on the machine it is imported onto.
check("an unset preference is omitted rather than written as null",
      "meter_speak_every" not in exported["settings"], True)

print("\nA FILE THAT IS NOT OURS IS REFUSED, AND SAYS WHAT TO USE INSTEAD")
check("a random JSON file is refused", bool(a.validate({"hello": 1})), True)
check("and the refusal points at profiles", "profile" in a.validate({"hello": 1}), True)
check("a newer format is refused rather than half-read",
      bool(a.validate({"marker": a.MARKER, "version": a.VERSION + 1, "settings": {}})), True)
check("a file with no settings in it is refused",
      bool(a.validate({"marker": a.MARKER, "version": 1})), True)
check("our own export passes", a.validate(exported), "")

print("\nIMPORT MERGES, SO THIS MACHINE'S OWN SECRETS AND STATE SURVIVE")
onto = {"remote_token": "THIS-MACHINES-OWN-TOKEN", "baseline_asked": ["OTHER-DESK"]}
changes, report = a.merge_import(exported, onto, {"announcer_voice": ["Hazel", "George"],
                                                  "announcer_output": ["Remote Audio"]})
check("the local token is not in the changes", "remote_token" not in changes, True)
check("nor is the local first-run record", "baseline_asked" not in changes, True)
check("the shortcuts came across", "hotkeys" in changes, True)

print("\nA DEVICE THIS COMPUTER HAS NOT GOT IS REPORTED, NOT SILENTLY APPLIED")
check("the missing output was not applied", "announcer_output" not in changes, True)
check("and it is named in the report",
      report["unmatched"], [("announcer_output", "Speakers (Nonexistent)")])
check("a voice this machine DOES have was applied", changes.get("announcer_voice"), "Hazel")
said = a.describe_import(report)
check("the spoken result says the output was left alone", "not on this computer" in said, True)
check("and it counts the shortcuts", "3 keyboard shortcuts restored" in said, True)

print("\nAN EMPTY AVAILABILITY LIST MEANS 'CANNOT TELL', NOT 'HAS NONE'")
# Over RDP, SAPI enumeration fails and the voice list comes back empty. Refusing to restore a voice
# because we could not enumerate would be the wrong way round, and would silently drop the setting.
changes2, _ = a.merge_import(exported, {}, {})
check("with no lists at all, the machine-specific settings still apply",
      "announcer_output" in changes2 and "announcer_voice" in changes2, True)

print("\nA HAND-EDITED FILE CANNOT SMUGGLE ANYTHING IN")
evil = {"marker": a.MARKER, "version": 1,
        "settings": {"remote_token": "HACKED", "baseline_asked": ["x"],
                     "something_invented": 1, "hotkeys": {"x": 1}}}
changes3, report3 = a.merge_import(evil, {}, {})
check("the token is refused", "remote_token" not in changes3, True)
check("the first-run record is refused", "baseline_asked" not in changes3, True)
check("an unknown key is refused", "something_invented" not in changes3, True)
check("all three are reported as ignored", len(report3["ignored"]), 3)
check("and the legitimate shortcuts still came through", "hotkeys" in changes3, True)

print("\nTHE SHORTCUT COUNT IS THE REAL ONE, NOT THE NUMBER OF KEYS IN THE DICT")
# The bug this pins: `len()` on the stored value returns 2 whatever the user has, because the two
# keys are "enabled" and "shortcuts". Checked against a real settings.json,
# where it reported two shortcuts for a machine that had six.
check("three shortcuts are counted as three", a.count_shortcuts(REAL_HOTKEYS), 3)
check("an empty list is none", a.count_shortcuts({"enabled": True, "shortcuts": []}), 0)
check("a shape it does not recognise is none, not a crash", a.count_shortcuts("rubbish"), 0)
check("and the export sentence leads with three", a.describe_export(exported).split()[0], "3")

print("\nNOTHING RESTORED IS SAID PLAINLY, NOT AS A CHEERFUL SUCCESS")
_, empty = a.merge_import({"marker": a.MARKER, "version": 1, "settings": {}}, {}, {})
check("an empty import says nothing was restored",
      "Nothing was restored" in a.describe_import(empty), True)

print()
if FAILED:
    print(f"{FAILED} CHECK(S) FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
