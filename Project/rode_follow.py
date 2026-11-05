"""
AccessCaster - following what RODE's own app changes on the desk, which the desk never pushes.

While RODE's app is open, every dump any reader takes is compared with the desk model
(`desk_model.reveal`), so a change it shows reaches whatever follows the desk as a push would.
NOTHING IS READ ON A TIMER: every reader is sent every dump's frames, and ours arriving every few
seconds keep RODE's app on "Synchronizing device" for good. The window reads once when it comes
back to the front (`read_now`), and the desk is read once more as RODE's app closes.
"""

from __future__ import annotations

import threading

import announcer
import desk_access
import desk_live
import desk_model
import diagnostics
import rcp_read
import rode_app

_busy = threading.Lock()
# How long a read waits for a write or a job's read to let the desk go before it skips this look;
# the next comes `rode_app.POLL_S` later.
_WAIT_S = 1.0

_was_open = False


def follow(open_now: bool) -> None:
    """`rode_app.on_look`: compare reads with the desk model while RODE's app is open, gather the
    announcer's changes, and read once more as it closes, for what it changed since."""
    global _was_open
    closed, _was_open = _was_open and not open_now, open_now
    if closed:
        _read_unheard()
    desk_model.reveal(open_now)
    announcer.gather_changes(open_now)


def read_now() -> None:
    """One read of the desk while RODE's app is open, for what it has changed, which the
    announcer then says with what it has gathered."""
    if rode_app.is_open():
        _read_unheard()
        announcer.speak_gathered()


def _read_unheard() -> None:
    """One read, compared with the desk model. Skipped while a job has the desk, while the desk is
    gone, and while another such read runs."""
    if not desk_live.connected() or desk_access.work_in_progress() is not None:
        return
    if not _busy.acquire(blocking=False):
        return
    try:
        if not desk_access.hid_lock.acquire(timeout=_WAIT_S):
            return
        try:
            rcp_read.read_model(fresh=True)
        finally:
            desk_access.hid_lock.release()
    except Exception as exc:
        diagnostics.event("rode_app.read_failed", str(exc))
    finally:
        _busy.release()
