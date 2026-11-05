"""
AccessCaster - the shared core every feature uses to reach the desk: the one lock around it, the
desk as it is now, the capability gate on a risky write, and the shape of a long job.

It imports none of the modules that use it (of the features, only the announcer and speech, which
a long job holds and sounds), so any feature module can import it.
"""

from __future__ import annotations

import threading
import time

import announcer
import desk_model
import devices
import diagnostics
import rcp_read
import rcp_transfer
import rode_app
import speech


# One lock around ALL device access. The window, the shortcuts and the watchers call in from
# their own threads and the unit must never be opened twice at once; this also serialises the
# slow transfer-mode pad write against status reads.
hid_lock = threading.Lock()


def desk_now() -> bytes:
    """The desk as a model dump: the desk model's picture while it is trusted and RODE's app, whose
    changes are not pushed, is closed; otherwise a read, which the model adopts. CALLER HOLDS THE
    HID LOCK."""
    if desk_model.trusted() and not rode_app.is_open():
        snap = desk_model.snapshot()
        if snap:
            return snap
    return rcp_read.read_model_stable(fresh=True)


def cap_gate(cap: str, refusal) -> dict | None:
    """None if the CONNECTED device has `cap`, else a refusal dict carrying `refusal(device name)`.

    THE ONE SHAPE EVERY RISKY WRITE FAMILY IS GATED BY. The rule behind all of them: a selector
    proven on a RODECaster Pro II has NOT been proven on a Duo, and a wrong selector does not simply
    fail - it can append a stray property that only a factory reset clears. So a device without
    the cap REFUSES BY NAME and says why, rather than writing blind and hoping.

    Each gate keeps its own sentence, because what the user can and cannot do differs per family
    and a generic "not supported" would be useless to them.
    """
    dev = devices.get_active_device()
    if cap in dev.caps:
        return None
    return {"ok": False, "message": refusal(dev.name)}


# Every BackgroundJob, with the sentence that says it is running, in the order they were made.
_jobs: list = []


def work_in_progress() -> str | None:
    """What is part-way through, as a sentence, or None. Quitting in the middle of one of these can
    leave the desk half written: a pad's sound cut off mid-copy leaves an entry only a factory
    reset clears."""
    for job in _jobs:
        if job.busy():
            return job.running
    if rcp_transfer.transfer_active():
        return "SMART pad sounds are being copied."
    return None


class BackgroundJob:
    """One long job running off the request thread: its progress, and its result.

    ONE SHAPE FOR EVERY LONG JOB (the profile save and restore, the desk's own show commands, the
    pad restore, the firmware update, the driver install), so there is one lock, one state dict and
    one finish block. Each registers itself, so `work_in_progress` sees every job without knowing
    the features that own them.

    WHY BACKGROUND AT ALL. These take from a few seconds to well over a minute (a pad backup copies
    every sound file off the unit over transfer mode). A window that can poll for progress speaks
    each step; one long silent call just hangs, which for a screen-reader user is
    indistinguishable from a crash.

    `start` claims the job, refusing plainly if one is already running, and runs `work(job)` on a
    daemon thread. `work` reports with `job.step` (a message) or `job.tick` (a message AND a count),
    and RETURNS its result dict. This class writes the finish, including turning an exception into
    an honest failure message rather than a dialog that never closes.
    """

    def __init__(self, busy: str, failed: str, running: str, rewrites_desk: bool = False):
        self._lock = threading.Lock()
        self.running = running   # "A backup is running.", for work_in_progress
        # A job that rewrites the desk holds the announcer's desk-change speech for its run
        # (announcer.hold_desk_speech): every write it makes would otherwise be read out.
        self._rewrites_desk = rewrites_desk
        self._busy = busy        # "A profile save is already running."
        self._failed = failed    # "Could not save the profile"
        self._state = {"running": False, "step": 0, "total": 0, "label": "",
                       "done": False, "ok": None, "message": ""}
        self._n = 0
        self._phrase = None
        _jobs.append(self)

    def status(self) -> dict:
        """The whole state, copied, for the window's progress dialog to poll."""
        with self._lock:
            return dict(self._state)

    def busy(self) -> bool:
        with self._lock:
            return self._state["running"]

    def step(self, message: str) -> None:
        """Say what is happening now WITHOUT advancing the count. For a phase that reports its own
        sections rather than a countable list of items (the settings half of a restore)."""
        diagnostics.event("job.step", message)
        with self._lock:
            self._state["message"] = message

    def tick(self, message: str) -> None:
        """Advance the step count and say what is happening now."""
        diagnostics.event("job.step", message)
        with self._lock:
            self._n += 1
            self._state["step"] = self._n
            self._state["label"] = message
            self._state["message"] = (self._phrase(message, self._n, self._state["total"])
                                      if self._phrase else message)

    def start(self, work, message: str, total: int = 0, phrase=None,
              started: str | None = None, failed: str | None = None) -> dict:
        """Claim the job and run it. `message` is what the dialog says until the first report;
        `phrase(msg, n, total)` words a counted step ("Pad 3 (3 of 16)."); `started` overrides the
        sentence handed straight back to the caller; `failed` overrides the wording of a crash for
        a job that shares this state with another (the pad backup on a baseline is not "could not
        save the profile")."""
        with self._lock:
            if self._state["running"]:
                return {"ok": False, "message": self._busy}
            self._n = 0
            self._phrase = phrase
            self._state.update({"running": True, "step": 0, "total": total, "label": "",
                                "done": False, "ok": None, "message": message})
        crashed = failed or self._failed
        label = (started or message).rstrip(".")

        def _worker():
            # EVERY JOB IS BRACKETED IN THE DIAGNOSTIC LOG, because the fault this log was built
            # for happens AFTER a job reports success: the interesting question is what the app did
            # in the minute following the end line, and that can only be asked once the end line
            # has a timestamp of its own.
            at = time.monotonic()
            diagnostics.event("job.start", label)
            if self._rewrites_desk:
                announcer.hold_desk_speech()
            try:
                res = work(self) or {}
            except Exception as exc:
                res = {"ok": False, "message": f"{crashed}: {exc}"}
            finally:
                if self._rewrites_desk:
                    announcer.release_desk_speech()
            diagnostics.event("job.end", label, ok=bool(res.get("ok")),
                              took=f"{time.monotonic() - at:.2f}s",
                              said=res.get("message") or "Done.")
            with self._lock:
                self._state.update({"running": False, "done": True,
                                    "ok": bool(res.get("ok")),
                                    "message": res.get("message") or "Done."})
            # A CUE AT THE END OF EVERY JOB, rising for finished and falling for failed. Every step
            # a job reports is spoken the same way, so the closing sentence arrives sounding
            # exactly like the twenty before it and "Backup complete" is heard as one more pad
            # name. A tone is not a sentence and cannot be mistaken for one, and it lands BEFORE
            # the words, so the outcome is known first.
            # ONE PLACE, because this class already owns the finish block for every long job: the
            # backup, the restore, the reset and the pad jobs all get it without knowing it exists.
            # NOT GATED ON THE ANNOUNCER. The announcer speaks for the DESK's own controls and can
            # reasonably be off; this answers a button the user just pressed in this app and is
            # waiting on.
            try:
                speech.tone("done" if res.get("ok") else "failed")
            except Exception:
                pass                    # a missing cue must never fail the job that just succeeded

        # NAMED, so the diagnostic log's thread column says which job a line came from rather than
        # "Thread-7". Every other line in a job's window is written from this thread.
        threading.Thread(target=_worker, daemon=True, name=f"job:{label[:14]}").start()
        reply = {"ok": True, "started": True, "message": started or message}
        if total:
            reply["total"] = total
        return reply
