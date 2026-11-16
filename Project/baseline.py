"""
AccessCaster - the as-found baseline: a profile of the desk as it was on the day the app met it,
offered once per desk, the first time the window reads that desk.
"""

from __future__ import annotations

import datetime

import desk_access
import device_page
import devices
import profile_actions
import profiles
import settings_file
import soundpad
import wording
from paths import safe_name

# The most valuable thing this app can do on the day it is installed is record how the desk already
# was: the moment anything is written, "how it came" is gone, and the touchscreen wizard that would
# otherwise rebuild it is not reachable by a blind operator.
#
# IT IS NOT THE ONLY UNDO, and it is the better one. `backups.action_show_reset` puts the
# desk back to the factory, but it takes everything with it and it is the command implicated in the
# USB dropout; restoring this baseline puts back what the user actually had, needs no reset, and
# works on a desk with no card or drive in it.
#
# It is offered ONCE PER DESK, not once per install: plug in a second unit later and it gets its own
# offer and its own baseline, each filed under the desk's USB serial. The answer (captured, or
# declined) is remembered against that serial so the dialog does not reappear at every launch, and
# pads can be added to an existing baseline afterwards.
_BASELINE_ASKED_KEY = "baseline_asked"
# MEASURED: a full backup of a 16-pad desk, 8 of them sound pads and 19.7 MB of audio, takes 4.6
# SECONDS END TO END, settings read included. Most of that is fixed cost (the model read, then
# entering transfer mode and mounting); the copying itself is a fraction of a second per file.
#
# DO NOT QUOTE PAD_RESTORE_SECONDS HERE. That 15 s/pad is genuinely measured, but for the other
# direction: a restore WRITES over transfer mode and the desk has to take each file, while a backup
# only READS a mounted FAT volume. Reusing it tells people a 16-pad backup takes four minutes when
# it takes five seconds, which is the kind of wrong that makes somebody skip the backup.
#
# Reading off the mounted pad volume runs at ~20 MB/s (12.14 MB in 0.56 s, and seven smaller files
# at 12-21 MB/s), so the whole grid moves in about a second.
#
# SO THE HONEST SHAPE IS THROUGHPUT, NOT A PER-PAD CONSTANT. This grid's biggest pad is 12 MB; a
# ten-minute bed at 100 MB would take five seconds on its own. One second per pad is right for pads
# this size and is rounded up; if somebody's grid is all long beds it will under-quote, which costs
# little at a second a pad.
PAD_BACKUP_SECONDS = 1


def _baseline_asked() -> list:
    got = settings_file.load().get(_BASELINE_ASKED_KEY)
    return [str(x) for x in got] if isinstance(got, list) else []


def _this_desk() -> tuple:
    """(USB serial, product id) of the connected desk; the serial is "" when no desk is known."""
    return device_page.hardware_id(), devices.get_active_device().product_id


def offer_pending(hardware_id: str, product_id) -> bool:
    """Is this desk still to be offered its as-found backup? False for an unknown desk ("" serial),
    which cannot be offered anything."""
    return bool(hardware_id and hardware_id not in _baseline_asked()
                and not profiles.find_baseline(hardware_id, product_id))


def settled(hardware_id: str, product_id) -> bool:
    """Has this desk's as-found question been settled: captured, declined, or never needed? Only
    then may anything be written to it unasked. False for an unknown desk."""
    return bool(hardware_id) and not offer_pending(hardware_id, product_id)


def _note_baseline_asked() -> None:
    """Remember that this desk has had its offer, so the dialog is a first-run event and not a nag."""
    here = device_page.hardware_id()
    if here and here not in _baseline_asked():
        settings_file.update(**{_BASELINE_ASKED_KEY: [*_baseline_asked(), here]})


def _baseline_name() -> str:
    """The auto-name for a baseline: what it is, when it was taken, and which desk.

    Deliberately not "app install": a year from now that says nothing about which unit or when.
    Kept inside paths.safe_name's character set (letters, digits, spaces, dashes) and its 40
    character limit, which is why there are no brackets and the desk is named without the
    "RODECaster" every desk shares: the longest date, "30th September 2026", leaves room for no
    more. The date is spoken rather than numeric because a profile name is heard.

    Numbered when the name is taken, as it is by a second desk of the same model met the same day:
    a baseline saved under a taken name would replace that profile."""
    when = f"As found {wording.spoken_date(datetime.date.today())}"
    desk = devices.get_active_device().name.removeprefix("RODECaster ").strip()
    base = safe_name(f"{when} {desk}"[:37]) or when
    name, n = base, 1
    while profiles.load(name).get("ok"):
        n += 1
        name = f"{base} {n}"
    return name


def baseline_status() -> dict:
    """Should the page offer a first-run baseline capture, and what is there already?

    Cheap on purpose: it reads the profile store (small, local) and the settings file, and does NO
    device I/O at all. The page asks this on every load, right after the status read that publishes
    the hardware id, so "do we know this desk's id" is exactly as good as "is a desk connected" here
    and costs nothing."""
    here, product_id = _this_desk()
    existing = profiles.find_baseline(here, product_id) if here else {}
    return {
        "ok": True,
        # No desk means no offer rather than a dialog that could not do anything anyway.
        "offer": offer_pending(here, product_id),
        "connected": bool(here),
        "baseline": existing or None,
        "suggested_name": _baseline_name(),
        "device_name": devices.get_active_device().name,
        "seconds_per_pad": PAD_BACKUP_SECONDS,
    }


def action_baseline_skip() -> dict:
    """Decline the first-run capture. Remembered per desk so it is asked once, not every launch."""
    _note_baseline_asked()
    return {"ok": True, "message": "No backup was taken. You can still back the desk up from the "
                                   "Backups page at any time, though that records it as it is "
                                   "then rather than as you found it."}


def action_baseline_save(include_pads: bool = True) -> dict:
    """Capture the as-found baseline. Runs on the same worker and reports through the same progress
    dialog as an ordinary profile save; only the name, the marking and the protections differ."""
    here, product_id = _this_desk()
    if not here:
        return {"ok": False, "message": "No desk is connected to back up. Nothing was changed."}
    existing = profiles.find_baseline(here, product_id)
    if existing:
        return {"ok": False, "message": f"This desk already has an as-found backup, "
                                        f"{existing['name']}, and a desk has only one. Nothing "
                                        f"was changed."}
    _note_baseline_asked()          # asked and answered, whichever way the capture then goes
    return profile_actions.snapshot_profile(_baseline_name(), include_pads=include_pads, baseline=True)


def action_baseline_add_pads() -> dict:
    """Add the SMART pads to a baseline that was captured without them.

    The settings snapshot is left EXACTLY as it was captured (profiles.attach_pads, not a re-save):
    re-reading the desk would quietly turn an as-found record into a record of today, which is the
    one thing a baseline must never become."""
    existing = profiles.find_baseline(*_this_desk())
    if not existing:
        return {"ok": False, "message": "This desk has no as-found backup on this computer to add "
                                        "pads to."}
    if existing.get("pads"):
        return {"ok": False, "message": f"{existing['name']} already has "
                                        f"{wording.count(existing['pads'], 'SMART pad')} saved."}
    safe = existing["name"]

    def _work(job):
        with desk_access.hid_lock:
            backup = soundpad.backup_pads(profiles.pads_dir(safe), progress=job.tick)
        return profiles.attach_pads(safe, backup["pads"])

    return profile_actions.save_job.start(_work,
                                          message="Backing up SMART pads, please wait.",
                                          started="Backing up the SMART pads.",
                                          failed="Could not back up the pads")
