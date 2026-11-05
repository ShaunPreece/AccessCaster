"""
AccessCaster - the App settings pages: announcements, General, and the export and import of the
app's own settings. `app_settings` is the pure half (what travels in an export, and how an import
merges); this module reads and writes the settings file and applies what changed.
"""

from __future__ import annotations

import datetime
import json
import os

import announcer
import app_settings
import autostart  # the HKCU Run entry, shared with installer.iss
import callme
import clip_warning
import folders
import meters
import settings_file
import shortcuts
import speech
import wording


def read_announce() -> dict:
    """Every announcement setting, and the voices and outputs this computer offers."""
    lists = speech.sapi_lists()
    return {"ok": True, "on": speech.enabled.is_set(), "levels": announcer.levels_on,
            "mute_tones": announcer.mute_tones, "record_feedback": announcer.record_feedback,
            "remember": announcer.remember,
            "volume": speech.volume,
            "voice": speech.chosen_voice, "output": speech.chosen_output,
            "voices": lists["voices"], "outputs": lists["outputs"],
            "clip_warning": clip_warning.clip_warning_on(),
            "running": bool(announcer._thread and announcer._thread.is_alive())}


def export_dir() -> str:
    """Where an app-settings export goes. The point of this one is to survive a reinstall."""
    return folders.documents_folder("App settings")


def _available() -> dict:
    """What this computer actually HAS, for the machine-specific keys - see app_settings.merge_import.

    Best-effort: if a list cannot be read, the key is left out entirely, which merge_import treats
    as "cannot check, so apply it" rather than as "not available". Refusing to restore a setting
    because we failed to enumerate would be the wrong way round."""
    out = {}
    try:
        lists = speech.sapi_lists()
        # Only offer a list we actually got. Over Remote Desktop, SAPI enumeration fails with
        # 0x80045039 and both come back empty, and an empty list must NOT be read as "this
        # computer has no voices, so refuse the setting" - it means we could not tell.
        if lists.get("voices"):
            out["announcer_voice"] = list(lists["voices"])
        if lists.get("outputs"):
            out["announcer_output"] = list(lists["outputs"])
    except Exception:
        pass
    try:
        found = meters.desk_streams()
        if found.get("streams"):
            out["meter_stream"] = [s["name"] for s in found["streams"] if s.get("name")]
    except Exception:
        pass
    return out


def action_export() -> dict:
    """Write this app's own settings to a file: shortcuts, announcements, level warnings.

    NOT the desk - that is a profile. This is the half no profile has ever carried, so before it
    existed a reinstall lost every keyboard shortcut with no way back."""
    payload = app_settings.build_export(settings_file.load())
    # Writing an empty file would be a cheerful success for a no-op, and the user would carry it to
    # the new machine believing their shortcuts were in it.
    if app_settings.is_empty(payload):
        return {"ok": False,
                "message": "There is nothing to save yet: no keyboard shortcuts are set up and no "
                           "announcement or level-warning preference has been changed from its "
                           "default."}
    # A filename somebody will hear rather than see, so the date is spoken, not numeric.
    name = f"AccessCaster settings {wording.spoken_date(datetime.date.today())}.json"
    path = os.path.join(export_dir(), name)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    except OSError as exc:
        return {"ok": False, "message": f"Could not write the settings file: {exc}"}
    return {"ok": True, "path": path, "folder": export_dir(),
            "message": (f"Saved {app_settings.describe_export(payload)} to {name}, in your "
                        f"Documents folder under AccessCaster, App settings. It holds none of "
                        f"the desk's settings.")}


def action_import(payload) -> dict:
    """Restore this app's own settings from an export. MERGES, never replaces (see merge_import).

    The shortcuts are re-registered here rather than at the next restart, because a shortcut that
    only works tomorrow reads exactly like one that does not work."""
    why = app_settings.validate(payload)
    if why:
        return {"ok": False, "message": why}
    changes, report = app_settings.merge_import(payload, settings_file.load(),
                                                _available())
    if not changes:
        return {"ok": False, "message": app_settings.describe_import(report)}
    settings_file.update(**changes)
    if "callme_contacts" in changes:
        callme.set_contacts(changes["callme_contacts"])

    notes = []
    if "hotkeys" in changes:
        # Re-register NOW. mgr.load validates and registers each combination, and saveable() is
        # written back so anything the manager refused (a key another program already owns) does
        # not sit in the file pretending to work.
        try:
            mgr = shortcuts.manager()
            mgr.load(changes["hotkeys"], defaults=shortcuts.defaults())
            settings_file.update(hotkeys=mgr.saveable())
        except Exception as exc:
            notes.append(f" The shortcuts were saved but could not all be registered: {exc}")
    # Speech and the announcer hold their settings in module state, so a restored preference has to
    # be pushed into them or it would not take effect until a restart.
    speech.apply_settings(changes)
    announcer.apply_settings(changes)
    return {"ok": True, "report": report,
            "message": app_settings.describe_import(report) + "".join(notes)}


def read_general() -> dict:
    """The General settings: whether AccessCaster starts when this user signs in (see autostart.py
    for why that is the installer's own registry value rather than one of ours), and
    `keep_in_tray`."""
    return {"ok": True, **autostart.state(), "keep_in_tray": keep_in_tray()}


def keep_in_tray() -> bool:
    """Whether closing the window leaves the app running in the tray, the default, rather than
    quitting it."""
    return settings_file.load().get("keep_in_tray", True) is not False


def action_set_keep_in_tray(on: bool) -> dict:
    if on is None:
        return {"ok": False, "message": "Say whether to keep running in the tray or not."}
    settings_file.update(keep_in_tray=bool(on))
    return {"ok": True, "keep_in_tray": bool(on),
            "message": ("Closing the window will leave AccessCaster running in the tray." if on
                        else "Closing the window will quit AccessCaster.")}


def action_set_autostart(on: bool) -> dict:
    """Turn "start when you sign in" on or off, for THIS user only."""
    if on is None:
        return {"ok": False, "message": "Say whether to start at sign-in or not."}
    res = autostart.set_enabled(bool(on))
    return {**res, "ok": bool(res.get("ok"))}


def action_open_export_folder() -> dict:
    """Open the folder the export went to, so the file can actually be copied somewhere safe."""
    return folders.open_folder_reply(export_dir(), "The folder")
