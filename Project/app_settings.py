"""AccessCaster - export and import of THE APP'S OWN settings, which no profile carries.

WHY THIS EXISTS. A profile is a backup of the DESK: channels, processing, routing, faders, the
unit's settings, optionally the pads. Your keyboard shortcuts are not a property of the desk - they
are yours, and they live only in `%LOCALAPPDATA%\\AccessCaster\\settings.json`. So until now they
are backed up NOWHERE: reinstall, or move to another computer, and every shortcut is gone with no
way back.

IT IS DELIBERATELY NOT PART OF A PROFILE. A profile gets restored onto a desk, often someone else's
work, and quietly replacing that person's key bindings as a side effect of restoring a mixer would
be a genuinely nasty surprise. Two separate things, two separate buttons.

LOCK BOUNDARY, the same rule profiles.py follows: THIS MODULE NEVER IMPORTS THE SERVER and never
touches a file. It takes the settings dict in and hands a payload or a merged dict back, so the
server keeps ownership of reading and writing settings.json.

THE ALLOW-LIST IS THE WHOLE DESIGN. It is an allow-list and not a dump, because three of the keys in
that file must never travel - see EXCLUDED below, each with its reason. A dump would have carried
all three, and the first of them is a credential.
"""
from __future__ import annotations

from typing import Any

# The marker and version, so an import can tell OUR file from any other JSON somebody picks.
MARKER = "accesscaster-app-settings"
VERSION = 1

# WHAT TRAVELS. Preferences that are about YOU rather than about a desk or a machine.
PORTABLE = (
    "hotkeys",
    "announcer_levels",
    "announcer_volume",
    "record_mute_announcer",
    "meter_warn_clip",
    "meter_warn_silence",
    "meter_silence_after",
    "meter_speak_every",
    "meter_clip_sound",
    "meter_clip_db",
    "callme_contacts",
    "callme_auto_extend",
)

# WHAT TRAVELS BUT NAMES SOMETHING ON ONE PARTICULAR COMPUTER. A voice, an audio output and a
# capture stream are all names that may simply not exist on the machine you import onto. They are
# still worth carrying - most people move to a similar machine and it just works - but an import
# REPORTS the ones it could not match rather than applying a name that resolves to nothing and
# leaving someone wondering why their announcer went quiet.
MACHINE_SPECIFIC = (
    "announcer_voice",
    "announcer_output",
    "meter_stream",
)

# WHAT NEVER TRAVELS, and why. Each of these would do real harm in an exported file.
EXCLUDED = {
    # A CREDENTIAL. It is the token that grants remote access to this app. An export is a file
    # people email to themselves and drop in shared folders, and this must not be in one.
    "remote_token": "it is the remote-access credential for this app",
    # PER-DESK FIRST-RUN STATE. It records which desks have already been offered an as-found
    # backup. Carrying it to a new machine would silently suppress that offer for a desk that has
    # never been backed up there, and the offer only ever comes once.
    "baseline_asked": "it records which desks have already been offered a first-run backup",
    # A RECORD OF ONE DESK. Which voicing preset currently sits on which channel is a fact about
    # that mixer, not a preference, and importing it would claim presets that are not applied.
    "applied_presets": "it describes one particular desk rather than a preference",
}

# Every settings key this module has an opinion about. A key in settings.json that is in NONE of
# the three lists is a key nobody decided on, which is a bug in this file rather than in the data:
# test_app_settings.py fails on one, so a new preference cannot quietly become unbackupable.
KNOWN = frozenset(PORTABLE) | frozenset(MACHINE_SPECIFIC) | frozenset(EXCLUDED)


def build_export(settings: dict) -> dict:
    """The payload to write out, from the current settings dict. Never includes an EXCLUDED key.

    Keys that are simply absent are left out rather than written as null, so importing an export
    taken before a setting existed does not stamp a null over a real value.
    """
    values = {k: settings[k] for k in (*PORTABLE, *MACHINE_SPECIFIC) if k in settings}
    return {
        "marker": MARKER,
        "version": VERSION,
        "settings": values,
        # Named so a human reading the file can tell what it is and what it deliberately omits.
        "note": ("AccessCaster's own settings: keyboard shortcuts, announcement preferences, "
                 "level warnings and CallMe contacts. It does NOT contain any desk settings (those "
                 "are profiles), and it deliberately contains no remote-access token."),
    }


def count_shortcuts(hotkeys) -> int:
    """How many keyboard shortcuts a stored `hotkeys` value holds.

    The stored shape is {"enabled": bool, "shortcuts": [...]}, NOT a mapping of shortcuts, and
    getting that wrong is not a harmless miscount: `len()` on it returns 2 forever, whether the user
    has one shortcut or fifty. Measured against a real settings.json, after this
    reported "2 keyboard shortcuts" for a machine that had six.
    """
    if isinstance(hotkeys, dict):
        rows = hotkeys.get("shortcuts")
        if isinstance(rows, list):
            return len(rows)
    return 0


def describe_export(payload: dict) -> str:
    """What an export caught, as a NOUN PHRASE for dropping into a sentence ("Saved <this> to ...").

    A noun phrase and not a sentence, which is a real bug this had: returning "There were no app
    settings to save yet." produced "Saved There were no app settings to save yet. to <file>".
    Empty returns "", and the CALLER decides what to say about nothing - see is_empty.
    """
    got = payload.get("settings") or {}
    if not got:
        return ""
    bits = []
    shortcuts = count_shortcuts(got.get("hotkeys"))
    if shortcuts:
        bits.append(f"{shortcuts} keyboard shortcut{'s' if shortcuts != 1 else ''}")
    contacts = len(got.get("callme_contacts") or ())
    if contacts:
        bits.append(f"{contacts} CallMe contact{'s' if contacts != 1 else ''}")
    others = len(got) - (1 if "hotkeys" in got else 0) - (1 if "callme_contacts" in got else 0)
    if others:
        bits.append(f"{others} other preference{'s' if others != 1 else ''}")
    return " and ".join(bits) if bits else f"{len(got)} settings"


def is_empty(payload: dict) -> bool:
    """True when there is genuinely nothing worth writing to a file."""
    return not (payload.get("settings") or {})


def validate(payload: Any) -> str:
    """"" if this looks like one of our exports, else the reason it does not.

    Checked before anything is merged, because the alternative is a stray JSON file quietly
    replacing somebody's shortcuts with nonsense.
    """
    if not isinstance(payload, dict):
        return "That file does not contain AccessCaster settings."
    if payload.get("marker") != MARKER:
        return ("That file is not an AccessCaster settings export. If you meant to restore the "
                "desk rather than the app, use a profile instead.")
    try:
        version = int(payload.get("version", 0))
    except (TypeError, ValueError):
        return "That settings file does not say which version it is, so it cannot be trusted."
    if version > VERSION:
        return (f"That settings file was written by a newer version of AccessCaster "
                f"(format {version}, this app understands {VERSION}). Update the app first.")
    if not isinstance(payload.get("settings"), dict):
        return "That settings file has no settings in it."
    return ""


def merge_import(payload: dict, current: dict, available: dict | None = None) -> tuple:
    """(changes, report) for an import. `changes` is what to MERGE into settings, never a replace.

    MERGE, NOT REPLACE, and that matters: the excluded keys live in the same file, so a replace
    would delete this machine's remote-access token and its first-run record as a side effect of
    restoring somebody's keyboard shortcuts.

    `available` optionally maps a MACHINE_SPECIFIC key to the list of names this computer really
    has (voices, audio outputs, capture streams). A name that is not in its list is NOT applied,
    and is named in the report - see MACHINE_SPECIFIC for why silence would be the wrong answer.
    """
    incoming = payload.get("settings") or {}
    available = available or {}
    changes, applied, unmatched, ignored = {}, [], [], []

    for key, value in incoming.items():
        if key in EXCLUDED:
            # Cannot happen from our own export; can happen from a hand-edited file.
            ignored.append(key)
            continue
        if key not in PORTABLE and key not in MACHINE_SPECIFIC:
            ignored.append(key)
            continue
        if key in MACHINE_SPECIFIC and value:
            have = available.get(key)
            if have is not None and value not in have:
                unmatched.append((key, value))
                continue
        changes[key] = value
        applied.append(key)

    return changes, {
        "applied": sorted(applied),
        "unmatched": unmatched,
        "ignored": sorted(ignored),
        "shortcuts": count_shortcuts(incoming.get("hotkeys")) if "hotkeys" in changes else 0,
    }


def describe_import(report: dict) -> str:
    """The sentence spoken after an import. Says what did NOT happen as well as what did, because a
    setting silently dropped for naming a device this machine has not got is exactly the kind of
    thing somebody would otherwise spend an evening hunting."""
    parts = []
    n = len(report.get("applied") or ())
    shortcuts = int(report.get("shortcuts") or 0)
    if shortcuts:
        parts.append(f"{shortcuts} keyboard shortcut{'s' if shortcuts != 1 else ''} restored")
    others = n - (1 if shortcuts else 0)
    if others > 0:
        parts.append(f"{others} other preference{'s' if others != 1 else ''} restored")
    if not parts:
        parts.append("Nothing was restored")
    msg = ", ".join(parts) + "."
    for key, value in report.get("unmatched") or ():
        what = {"announcer_voice": "voice", "announcer_output": "announcement output",
                "meter_stream": "level-meter input"}.get(key, key)
        msg += (f" The saved {what}, {value!r}, is not on this computer, so it was left as it is.")
    if report.get("ignored"):
        msg += (f" {len(report['ignored'])} entr"
                f"{'y was' if len(report['ignored']) == 1 else 'ies were'} not recognised "
                f"and ignored.")
    return msg
