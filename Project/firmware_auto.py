"""AccessCaster - the desk's firmware check, made automatically: on the schedule AccessCaster keeps for
its own updates (`app_updates.check_days`), and soon after a desk connects whose check is due.

ASKING, NEVER INSTALLING. A check is the touchscreen's Check for Update (`firmware.check_unasked`):
one request, answered within a second, that changes nothing unless RODE has something newer, when
the desk records the version it is offered. Installing stays a choice made on Device > Hardware.

NOTHING IS WRITTEN TO A DESK WHOSE AS-FOUND QUESTION IS OPEN (`baseline.settled`). The as-found
backup has to record the desk before this app changes anything, so a desk met for the first time
is left alone until its offer has been taken or declined. Nor is anything written while a job is
running, or while the desk is recording or in a call.

WHO IS TOLD. A version found raises a Windows notification (`notify`, installed by `tray.py`),
once per version per desk; every other outcome is only logged, as with the app's own check.

Installed copies only (`ALLOWED`): a DevTools run starts the service too, and must not write to a
desk unasked. The desk simulator sets it, since every write there reaches a simulated desk.
"""
from __future__ import annotations

import sys
import threading
import time

import app_updates
import baseline
import desk_live
import devices
import diagnostics
import firmware
import settings_file
from selfupdate import interval

# settings.json keys, each {USB serial: value}, so every desk keeps its own.
_LAST_KEY = "firmware_check_last"       # when its check was last made, epoch seconds
_TOLD_KEY = "firmware_check_told"       # the version its last notification named

# How long after start-up or a desk connecting before a check: the reconnect's own reads and the
# as-found offer come first, and an update can wait a minute.
SETTLE_S = 60
# How often the schedule is looked at while the app runs; a desk connecting looks at once.
POLL_S = 3600

NOTIFICATION_TITLE = "RODECaster firmware"

ALLOWED = bool(getattr(sys, "frozen", False))

# Installed by tray.py: `notify(text)` raises a Windows notification that opens Device > Hardware's
# install question when chosen. None in a DevTools run, where the notice is only logged.
notify = None

_wake = threading.Event()
_started = False


def start() -> None:
    """Follow the desk connecting, and keep the schedule. Called once, from `service.start`."""
    global _started
    if _started or not ALLOWED:
        return
    _started = True
    desk_live.Follower("firmware check", (), on_connected=lambda _gap: _wake.set()).start()
    threading.Thread(target=_loop, daemon=True, name="firmware-check").start()


def _loop() -> None:
    while True:
        _wake.wait(POLL_S)
        _wake.clear()
        time.sleep(SETTLE_S)
        try:
            check_if_due()
        except Exception as exc:
            diagnostics.event("firmware.auto_failed", f"{exc}")


def _per_desk(key: str) -> dict:
    got = settings_file.load().get(key)
    return dict(got) if isinstance(got, dict) else {}


def check_if_due() -> dict | None:
    """Check the connected desk if it is due and nothing stands in the way. The check's reply, or
    None when none was made."""
    serial = desk_live.serial()
    if not baseline.settled(serial, devices.get_active_device().product_id):
        return None
    if not interval.is_due(app_updates.check_days(), _per_desk(_LAST_KEY).get(serial),
                           time.time()):
        return None
    reply = firmware.check_unasked(serial)
    if reply.get("skipped"):
        # Not stamped, so the next look tries again.
        diagnostics.event("firmware.auto_skipped", reply["skipped"])
        return reply
    # Stamped whatever the answer, as the app's own check is: a desk offline every day would
    # otherwise be asked every hour.
    settings_file.update(**{_LAST_KEY: {**_per_desk(_LAST_KEY), serial: time.time()}})
    update = reply.get("firmware_update") or {}
    diagnostics.event("firmware.auto_checked", reply.get("message") or "",
                      available=bool(update.get("available")))
    if update.get("available"):
        _tell(serial, update.get("version") or "")
    return reply


def _tell(serial: str, version: str) -> None:
    """Notify once per version per desk: the desk goes on offering an update it has not installed,
    and hearing about it every day would be a nag."""
    told = _per_desk(_TOLD_KEY)
    if told.get(serial) == version:
        return
    settings_file.update(**{_TOLD_KEY: {**told, serial: version}})
    desk = devices.get_active_device().name
    what = f"Firmware {version}" if version else "A firmware update"
    text = (f"{what} is available for your {desk}. Install it from AccessCaster's Device page, "
            f"under Hardware.")
    diagnostics.event("firmware.auto_notice", text)
    if notify is not None:
        try:
            notify(text)
        except Exception as exc:
            diagnostics.event("firmware.auto_notice_failed", f"{exc}")
