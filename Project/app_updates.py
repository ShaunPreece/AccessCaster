"""Keeping AccessCaster itself up to date: the background check, the notice, the decision, the
download and the hand-off to the installer.

THE RULE THE WHOLE FEATURE IS BUILT ON: checking is automatic, installing never is. An app that
rearranges itself unannounced is worst for a screen-reader user, who finds out by walking into the
change.

The index format, the signature check, the version comparison, the download and every sentence
belong to the `selfupdate` package. This module owns WHEN to ask, WHO is told, and the job the
window follows. It never imports the window: the window shows what `state()` and `job()` hand it.

WHO IS TOLD WHAT:
  - An automatic check that finds a version raises a WINDOWS NOTIFICATION from the tray icon
    (`notify`, installed by `tray.py`), never the app's own voice: speech nobody asked for talks
    over the screen reader. Every other automatic outcome is silent.
  - A check the user asked for answers where it was asked: the window's own reply, or another
    notification for the tray's menu item.
  - The decision and the download are the window's, in its two update dialogs.

HOW THE INSTALL ENDS. Download, then `ready`, and NOTHING HAPPENS until the window says `proceed`:
the progress dialog takes its controls away and gives the closing sentence time to be heard first,
because said after the installer starts it is cut off mid-word. A dialog that never says it
(closed, or gone) leaves the verified installer on disk and installs nothing, after
`PROCEED_WITHIN_S`.
"""
from __future__ import annotations

import datetime
import os
import shutil
import subprocess
import sys
import threading
import time

import paths
import settings_file
from selfupdate import check as checks
from selfupdate import checker, downloader, interval, phrases, restart, signing
from selfupdate.versions import UpdateVersion
from version import __version__

# settings.json keys.
_DAYS_KEY = "app_update_days"
_LAST_CHECKED_KEY = "app_update_last_checked"
_SKIPPED_KEY = "app_update_skipped"
# "<version>|<installer path>", written just before the installer starts and read by the next
# launch, which is the only process that can say whether the install took.
_INSTALLING_KEY = "app_update_installing"

# How long after start-up before the first automatic check. The greeting and the window's first
# reads own those seconds, and an update is something the user can deal with whenever they like.
SETTLE_S = 60

# How often the running app asks `interval.is_due`. AccessCaster lives in the tray for days, so the
# check has to come round while it runs, not only at launch.
POLL_S = 3600

# How long the window has, once the download is ready, to say `proceed`. Generous: it waits for a
# long sentence to be read first. Past it the dialog is taken to be gone.
PROCEED_WITHIN_S = 120

# An install and restart takes well under this. Past it, with this process still running, the
# installer has not closed it, so the install has not happened. Whole minutes: the sentence says it.
NOT_BACK_WITHIN_S = 180

# How long a report from the far side of a restart waits for the window to take it before it is
# raised as a notification instead. The restart opens the window, which takes it within seconds.
REPORT_WAIT_S = 30

# /VERYSILENT rather than /SILENT: under /SILENT Setup's progress window takes focus on its Cancel
# button, JAWS reads nothing from its bar, and a Space pressed into the wait cancels the install.
# /SUPPRESSMSGBOXES is deliberately NOT given: a Setup message box still shows under /VERYSILENT,
# and an error the user can read and answer beats one that is swallowed. /RESTARTAPPLICATIONS
# brings back the process `restart.register` registered; /NORESTART because nothing here justifies
# rebooting the machine.
INSTALLER_ARGS = ["/VERYSILENT", "/RESTARTAPPLICATIONS", "/NORESTART"]

# Detached, so the installer outlives the process that started it: Restart Manager is about to
# close this one.
_DETACHED = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

# Job stages, as the window reads them.
DOWNLOADING = "downloading"
CHECKING = "checking"
READY = "ready"
INSTALLING_STAGE = "installing"
FAILED = "failed"
CANCELLED = "cancelled"
_ACTIVE = (DOWNLOADING, CHECKING, READY)

# Installed by tray.py. `notify(text)` raises a Windows notification; `open_window()` shows the
# window, which then finds `_open_request` set and opens the decision. Both are None in a DevTools
# run, where there is no tray: the notice is then only logged.
notify = None
open_window = None

_lock = threading.Lock()
# A check or a download is in flight. COVERS THE NETWORK CALL ONLY, never the decision: held across
# the dialog, Install would find it set and do nothing at all.
_busy = False
# The version the last check found and the user has not acted on, so the key and the tray item open
# the decision without going back to the network.
_offer = None
_job = None
# The tray asked the window to open the decision.
_open_request = False
# What the last update did, from the far side of its restart: {"kind": "updated" | "failed", "text"}.
_report = None
_started = False


def running_version():
    """The version actually running, or None for a build whose own version cannot be read, which
    stops every check: offering an update on the strength of "we do not know what this is" would be
    the one way this feature could talk somebody into replacing a working app."""
    return UpdateVersion.parse(__version__)


def updates_dir() -> str:
    return paths.data_path("updates")


def _setting(key, default=None):
    return settings_file.load().get(key, default)


class _Job:
    """One download and hand-off. `cancel` is what the downloader asks after every block."""

    def __init__(self, offer):
        self.offer = offer
        self.stage = DOWNLOADING
        self.message = phrases.DOWNLOADING
        self.installer = None
        self.cancel = threading.Event()

    def payload(self) -> dict:
        return {"stage": self.stage, "message": self.message, "version": str(self.offer.version),
                "not_back": phrases.not_back(self.offer.version, NOT_BACK_WITHIN_S // 60)}


class _Watcher(downloader.Watcher):
    def __init__(self, job):
        self.job = job

    def checking(self):
        checker.log("checking the download")
        with _lock:
            self.job.stage, self.job.message = CHECKING, phrases.CHECKING_STAGE

    def cancelled(self):
        return self.job.cancel.is_set()


def _later(seconds, fn, *args) -> None:
    """Run `fn` once after `seconds`, on a daemon thread: a pending timer must not hold the process
    open when the app quits."""
    timer = threading.Timer(seconds, fn, args=args)
    timer.daemon = True
    timer.start()


# ---- start-up -----------------------------------------------------------------------------------

def start() -> None:
    """Report what the last update did, then keep the automatic check running. Called once, from
    `service.start`."""
    global _started
    if _started:
        return
    _started = True
    _read_outcome()
    threading.Thread(target=_auto_loop, daemon=True, name="update-check").start()


def _auto_loop() -> None:
    time.sleep(SETTLE_S)
    while True:
        if _auto_check_allowed() and interval.is_due(_setting(_DAYS_KEY, interval.DEFAULT),
                                                     _setting(_LAST_CHECKED_KEY), time.time()):
            result = _run_check()
            if result is not None and result.offer is not None:
                _notify(phrases.available(result.offer.version))
        time.sleep(POLL_S)


def _auto_check_allowed() -> bool:
    """A dev run is not the user's installed copy, and an update started from one would replace
    the installed app from a process that is not it. Check for updates now still works there, which
    is how a published build gets tested. No key, no check: logged rather than silent, because "no
    release has happened" and "this build cannot check" look identical from outside."""
    if not getattr(sys, "frozen", False):
        return False
    if not signing.is_configured():
        checker.log("no public key is built in, so no check was made")
        return False
    return True


def _notify(text: str) -> None:
    checker.log(f"notice: {text}")
    if notify is not None:
        try:
            notify(text)
        except Exception as exc:
            checker.log(f"the notification could not be raised: {exc}")


# ---- the check ----------------------------------------------------------------------------------

def _run_check():
    """One check, guarded so two never overlap: None when another is already running."""
    global _busy, _offer
    with _lock:
        if _busy:
            return None
        _busy = True
    try:
        result = checker.run(running_version(), _setting(_SKIPPED_KEY))
        # WRITTEN EVEN WHEN THE CHECK FAILED. Without that, a machine offline every morning would
        # ask on every launch and never get an answer. The cost is one missed day.
        settings_file.update(**{_LAST_CHECKED_KEY: time.time()})
        with _lock:
            _offer = result.offer
        return result
    finally:
        with _lock:
            _busy = False


def check_now() -> dict:
    """Check for updates now, from the window: its button and F12. Opens the decision straight from
    a held offer rather than asking the network an answer it already has. A skipped version is
    offered like any other here: the skip silences the automatic check only."""
    global _offer
    if not signing.is_configured():
        return {"ok": False, "message": phrases.CANNOT_CHECK}
    with _lock:
        if _job is not None and _job.stage in _ACTIVE:
            return {"ok": False, "message": phrases.ALREADY_CHECKING}
    held = _decision_payload()
    if held is not None:
        return {"ok": True, "offer": held}
    result = _run_check()
    if result is None:
        return {"ok": False, "message": phrases.ALREADY_CHECKING}
    running = running_version()
    if result.verdict in (checks.AVAILABLE, checks.SKIPPED):
        with _lock:
            _offer = result.index
        return {"ok": True, "offer": _decision_payload()}
    if result.verdict == checks.UP_TO_DATE and running is not None:
        return {"ok": True, "message": phrases.up_to_date(running)}
    return {"ok": False, "message": phrases.COULD_NOT_CHECK}


def check_from_tray() -> None:
    """The tray menu's item, on a thread of its own so the menu does not wait on the network. An
    offer opens the decision in the window; anything else is answered in a notification, which is
    where the user asked from."""
    def work():
        reply = check_now()
        if reply.get("offer"):
            request_decision()
        else:
            _notify(reply.get("message") or phrases.COULD_NOT_CHECK)
    threading.Thread(target=work, daemon=True, name="update-check-tray").start()


def held_version():
    """The version waiting to be offered, or None. The tray's menu label reads it."""
    with _lock:
        return _offer.version if _offer is not None else None


def request_decision() -> None:
    """Have the window open the decision: the tray's Install item and the notification's click."""
    global _open_request
    with _lock:
        _open_request = True
    if open_window is not None:
        open_window()


def take_request() -> bool:
    """True once per request; the window asks as it opens."""
    global _open_request
    with _lock:
        asked, _open_request = _open_request, False
    return asked


def _decision_payload():
    with _lock:
        offer = _offer
    running = running_version()
    if offer is None or running is None:
        return None
    return {"version": str(offer.version),
            "title": phrases.confirm_title(offer.version),
            "summary": phrases.confirm_summary(running, offer, datetime.date.today()),
            "notes": offer.notes,
            "after": phrases.CONFIRM_AFTER,
            # The progress dialog's first line, in place before it opens.
            "downloading": phrases.DOWNLOADING}


# ---- the decision -------------------------------------------------------------------------------

def skip(version: str) -> dict:
    """ONE EXACT VERSION, not "nothing newer than this": the next release is a new decision."""
    global _offer
    turned_down = UpdateVersion.parse(version)
    if turned_down is None:
        return {"ok": False, "message": phrases.OFFER_CHANGED}
    settings_file.update(**{_SKIPPED_KEY: str(turned_down)})
    with _lock:
        if _offer is not None and _offer.version == turned_down:
            _offer = None
    return {"ok": True, "message": phrases.skipped(turned_down)}


def set_interval(days: int) -> dict:
    settings_file.update(**{_DAYS_KEY: interval.normalise(days)})
    return {"ok": True, **_settings_payload()}


# ---- the download -------------------------------------------------------------------------------

def install(version: str) -> dict:
    """Start downloading the held offer. `version` is the one the decision showed, so an
    offer replaced in between cannot be installed under the old one's notes."""
    global _busy, _job
    with _lock:
        offer = _offer
        if offer is None or str(offer.version) != version:
            return {"ok": False, "message": phrases.OFFER_CHANGED}
        if _busy or (_job is not None and _job.stage in _ACTIVE):
            return {"ok": False, "message": phrases.ALREADY_CHECKING}
        _busy = True
        job = _job = _Job(offer)
    checker.log(f"downloading {offer.version} from {offer.url}")

    def work():
        global _busy
        outcome, path = downloader.download(offer, updates_dir(), watcher=_Watcher(job))
        with _lock:
            _busy = False
        _downloaded(job, outcome, path)

    threading.Thread(target=work, daemon=True, name="update-download").start()
    return {"ok": True, "job": job.payload()}


def _downloaded(job: _Job, outcome: str, path) -> None:
    if outcome == downloader.READY:
        with _lock:
            job.installer = path
            if job.cancel.is_set():
                job.stage, job.message = CANCELLED, phrases.CANCELLED_DOWNLOAD
                return
            job.stage, job.message = READY, phrases.READY_STAGE
        checker.log(f"{path} is ready to run")
        _later(PROCEED_WITHIN_S, _proceed_timed_out, job)
        return
    if outcome == downloader.CANCELLED:
        with _lock:
            job.stage, job.message = CANCELLED, phrases.CANCELLED_DOWNLOAD
        return
    # A complete file that hashes wrong is the one case the signing scheme exists to catch, and
    # calling it a network problem would hide it.
    sentence = (phrases.WRONG_CHECKSUM if outcome == downloader.WRONG_CHECKSUM
                else phrases.COULD_NOT_DOWNLOAD)
    _forget_the_offer()
    with _lock:
        job.stage, job.message = FAILED, sentence


def _forget_the_offer() -> None:
    """After a download that went wrong, throw away BOTH the held offer and the last-checked stamp.
    A held offer opens the decision without re-reading the index, so keeping one after a failure
    asks for the same URL on every press, and a republished index could never reach a running app.
    Dropping the stamp makes the next automatic check due at once."""
    global _offer
    with _lock:
        _offer = None
    settings_file.update(**{_LAST_CHECKED_KEY: ""})


def job() -> dict:
    """The download's progress, for the progress dialog to poll."""
    with _lock:
        return {"ok": True, "job": _job.payload() if _job is not None else None}


def cancel() -> dict:
    """Cancel while there is still something to cancel: the download, the check of it, or the
    pause before the hand-off. A verified installer is kept, so the next attempt skips the
    download. Too late once the installer is running."""
    with _lock:
        job = _job
        if job is None or job.stage not in _ACTIVE:
            return {"ok": False}
        job.cancel.set()
        if job.stage == READY:
            job.stage, job.message = CANCELLED, phrases.CANCELLED_DOWNLOAD
    checker.log("the update was cancelled")
    return {"ok": True}


def _proceed_timed_out(job: _Job) -> None:
    with _lock:
        if job.stage != READY:
            return
        job.stage, job.message = CANCELLED, phrases.CANCELLED_DOWNLOAD
    checker.log("the window did not ask to install, so nothing was installed")


# ---- the hand-off -------------------------------------------------------------------------------

def proceed() -> dict:
    """The dialog has taken its controls away and the closing sentence has been read: start the
    installer and leave it to close the app.

    THE APP DOES NOT EXIT ITSELF HERE. The installer's Restart Manager asks it to close, and
    /RESTARTAPPLICATIONS brings back the process registered in `restart`. Exiting first would leave
    Restart Manager nothing to restart, so the app would install and never come back."""
    with _lock:
        job = _job
        if job is None or job.stage != READY or job.cancel.is_set():
            return {"ok": False, "job": job.payload() if job is not None else None}
        job.stage = INSTALLING_STAGE
    installer = job.installer
    # Recorded BEFORE the installer starts, because this process will be gone before it finishes.
    settings_file.update(**{_INSTALLING_KEY: f"{job.offer.version}|{installer}"})
    restart.register()
    try:
        subprocess.Popen([installer] + INSTALLER_ARGS, cwd=os.path.dirname(installer),
                         creationflags=_DETACHED, close_fds=True)
    except OSError as exc:
        checker.log(f"could not start {installer}: {exc}")
        settings_file.update(**{_INSTALLING_KEY: ""})
        restart.unregister()
        _forget_the_offer()
        with _lock:
            job.stage = FAILED
            job.message = phrases.update_did_not_take(running_version(), installer)
        return {"ok": False, "job": job.payload()}
    checker.log(f"started {installer}")
    return {"ok": True, "job": job.payload()}


# ---- the report from the other side of the restart ----------------------------------------------

def _read_outcome() -> None:
    """On the first launch after this app started an installer, work out whether it took. Clears
    the record as it reads it, so one failed update does not announce itself on every launch."""
    global _report
    record = _setting(_INSTALLING_KEY) or ""
    if not record:
        return
    settings_file.update(**{_INSTALLING_KEY: ""})
    wanted_text, _, installer = record.partition("|")
    wanted = UpdateVersion.parse(wanted_text)
    running = running_version()
    if wanted is None or running is None:
        return
    if running < wanted:
        checker.log(f"restarted still on {running}, wanted {wanted}")
        _report = {"kind": "failed",
                   "text": phrases.update_did_not_take(running, installer or updates_dir())}
    else:
        # A skip is forgotten once an update has landed: the version turned down is behind us.
        settings_file.update(**{_SKIPPED_KEY: ""})
        checker.log(f"updated to {running}")
        _report = {"kind": "updated", "text": phrases.just_updated(running)}
        _discard_downloads()
    _later(REPORT_WAIT_S, _report_unclaimed)


def report_waiting() -> bool:
    """Whether a report is waiting, without taking it, so the window asks to take one only when
    there is one."""
    with _lock:
        return _report is not None


def take_report():
    """The report, once. The window takes it when the restart opens it."""
    global _report
    with _lock:
        report, _report = _report, None
    return report


def _report_unclaimed() -> None:
    """The window did not take the report in time. Said as a notification instead, so the user
    hears the outcome of what they started."""
    report = take_report()
    if report is not None:
        _notify(report["text"])


def _discard_downloads() -> None:
    """Throw away the downloaded installer, on the first launch after an update CONFIRMED to have
    landed and on no other path. Every other one has to keep it, because a failed install tells the
    user to run the file by hand."""
    folder = updates_dir()
    shutil.rmtree(folder, ignore_errors=True)
    if os.path.isdir(folder):
        # Setup can still hold the file open seconds after restarting us. The next download
        # replaces it, and the launch after that update clears it.
        checker.log(f"could not remove {folder}")


# ---- what the window reads ----------------------------------------------------------------------

def _settings_payload() -> dict:
    days = interval.normalise(_setting(_DAYS_KEY, interval.DEFAULT))
    try:
        stamp = float(_setting(_LAST_CHECKED_KEY) or 0)
    except (TypeError, ValueError):
        stamp = 0
    last = datetime.date.fromtimestamp(stamp) if stamp > 0 else None
    return {"days": days,
            "choices": [{"days": d, "label": label}
                        for d, label in zip(interval.CHOICES, interval.LABELS, strict=True)],
            "hint": phrases.settings_status(interval.describe(days), last,
                                            _setting(_SKIPPED_KEY) or "", datetime.date.today())}


def state() -> dict:
    """Everything the window needs: the settings, any offer waiting, and the job in flight."""
    with _lock:
        current = _job.payload() if _job is not None else None
    return {"ok": True, "running": __version__, "settings": _settings_payload(),
            "offer": _decision_payload(), "job": current, "checking": phrases.CHECKING,
            "report_waiting": report_waiting()}
