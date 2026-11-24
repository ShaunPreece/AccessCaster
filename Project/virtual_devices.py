"""RODE's Virtual Device Driver on this computer: is it here, is it bound to the desk, and are its
devices switched on in the system.

RODE's driver gives each channel the desk carries on a fader its own audio device on the computer
(Fader1 to Fader9), plus System, a playback device the desk mixes like any other channel. It
binds only while the desk is in Expanded mode, and creates the Fader devices only when the desk is
sending every channel. Getting the driver bound, its devices switched on, and the default output
where the user can hear it is this module's job; the reasons it goes wrong are the platform's.

The platform supplies:
  - `driver_state()`: what the driver says about itself, as a dict of `DriverStatus` fields. Cheap,
    and never raises.
  - `service_state()`: everything this process can see of the driver's service right now, as a
    dict that can be stored as JSON, for the bind history.
  - `sync_endpoints(status, expect_connected, move_default, parked)`: make the system's devices
    agree with `status` (see `reconcile`), and return the `Outcome` fields it changed: "enabled"
    and "disabled" (names), "faders_ready", "faders_missing", "default_moved_to". Raises.
  - `park_default()`: move the default output off the desk onto another live device, returning
    (id, name), or None when it is already elsewhere or there is nowhere to go. Raises.
  - `hold_default(endpoint_id)`: put the default back on that device if the system has moved it
    onto the desk. True when it had to. Raises.
  - `default_to_system(timeout_s)`: make the driver's System output the default once it appears,
    returning its name, or "" when it did not appear. Raises.
  - `virtual_endpoint_counts()`: (on, listed) for the driver's devices the system lists now. Raises.
  - `watch_endpoints(on_change, stop)`: call `on_change` once each burst of device changes has
    settled, until `stop` is set. Runs on the caller's thread, and never raises.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field, replace
from typing import Optional

import diagnostics
from paths import data_path
from wording import OS_NAME

if sys.platform == "win32":
    from win.virtual_devices import (default_to_system as _default_to_system, driver_state,
                                     hold_default, park_default, service_state, sync_endpoints,
                                     virtual_endpoint_counts, watch_endpoints)
elif sys.platform == "darwin":
    from mac.virtual_devices import (default_to_system as _default_to_system, driver_state,
                                     hold_default, park_default, service_state, sync_endpoints,
                                     virtual_endpoint_counts, watch_endpoints)
else:
    raise ImportError(f"No virtual devices for {sys.platform}: the Windows ones are "
                      "win/virtual_devices.py")


@dataclass
class DriverStatus:
    installed: bool = False         # the driver is installed
    running: bool = False           # its service is running
    connected: bool = False         # the service has bound to a desk
    enabled: bool = True            # the driver's own Enable switch
    desk: str = ""                  # "RØDECaster Pro II (GV0064612)" while connected
    firmware: str = ""
    driver_version: str = ""

    def phrase(self) -> str:
        """One sentence for a person, in the order the checks fail."""
        if not self.installed:
            return "RODE's Virtual Devices driver is not installed on this computer."
        if not self.running:
            return "RODE's Virtual Devices driver is installed but its service is not running."
        if not self.enabled:
            return "RODE's Virtual Devices driver is switched off in its tray icon."
        if not self.connected:
            return "RODE's Virtual Devices driver is running but not connected to the desk."
        return f"RODE's Virtual Devices driver is connected to the {self.desk}."


def read_status() -> DriverStatus:
    """What the driver says about itself right now."""
    return DriverStatus(**driver_state())


# The service binds 1.0 to 1.4 seconds after the desk arrives when the desk's Main output is free,
# and 0.2 seconds after that output is released when it was not. A wait longer than this only delays
# the retry (`usb_audio._virtual_devices_follow`).
CONNECT_WAIT_S = 6.0


def wait_connected(timeout_s: float = CONNECT_WAIT_S) -> DriverStatus:
    """Poll the driver until it reports connected, or give up. Returns at once when the driver is
    absent, stopped or switched off: none of those change by waiting."""
    at = time.monotonic()
    while True:
        st = read_status()
        if st.connected or not (st.installed and st.running and st.enabled):
            return st
        if time.monotonic() - at >= timeout_s:
            return st
        time.sleep(0.5)


# ---------------------------------------------------------------------------------------------
# Bind history: what can be seen of the service from outside, once per bind attempt.
#
# Binds and misses come in runs, so some state persists between arrivals, and the service process
# is the one party that is never restarted. It writes no log, so the record is what an unelevated
# process can read (`service_state`). A missed attempt is then compared field by field with a bound
# one (`DevTools/bind_history.py`). The file is separate from the diagnostic log because a miss can
# be weeks apart from the bind it is compared with, and that log rolls in a session.

BIND_HISTORY_NAME = "bind_history.jsonl"
_BIND_HISTORY_MAX = 1024 * 1024        # then rolled to .previous: about a thousand attempts each


def record_bind(attempt: int, connected: bool, cut_short: bool = False) -> None:
    """Append one bind attempt to the history. Attempt 0 is the arrival itself, 1 onwards the
    follow-up's own re-enumerations. `cut_short` marks a wait during which the mode was changed
    again, so a miss there may be the desk leaving rather than the service failing. Never raises:
    a record that can break a bind is worse than none."""
    try:
        folder = data_path("logs")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, BIND_HISTORY_NAME)
        if os.path.exists(path) and os.path.getsize(path) > _BIND_HISTORY_MAX:
            os.replace(path, path + ".previous")
        line = {"time": round(time.time()), "attempt": attempt, "connected": connected}
        if cut_short:
            line["cut_short"] = True
        line.update(service_state())
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
    except Exception as exc:
        diagnostics.event("virtual_devices.history_failed", f"{exc}")


# ---------------------------------------------------------------------------------------------
# The system's side: the devices, their enabled state, and the default output.

@dataclass
class Outcome:
    status: DriverStatus
    enabled: list = field(default_factory=list)      # names switched on
    disabled: list = field(default_factory=list)     # names switched off
    faders_ready: int = 0                            # Fader devices active once this is done
    faders_missing: bool = False                     # connected, yet the service made no Faders
    default_moved_to: str = ""                       # new default playback name, if it moved
    error: str = ""

    def sentence(self) -> str:
        """What the operator needs to hear, for the reply and the announcer. Empty when nothing.

        THE BIND ITSELF IS NOT SAID. RODE's driver raises its own system notification when it
        connects to the desk and when it lets go, and a screen reader reads it, so a line of ours
        for either is the same news twice. What is said is what that notification cannot know."""
        parts = []
        if self.faders_missing:
            parts.append(FADERS_MISSING)
        if self.default_moved_to:
            # Short names: this lands among the system and the driver announcing the same arrival.
            name = self.default_moved_to
            parts.append(f"{OS_NAME} sound is on System." if name.startswith("System") else
                         f"{OS_NAME} sound is back on the desk." if "RODECaster" in name else
                         f"{OS_NAME} sound is on {name}.")
        if self.error:
            parts.append(self.error)
        return " ".join(parts)


# THE ONE FAILURE THE SERVICE REPORTS AS SUCCESS. It opens the desk's Main Multitrack capture pin
# directly, and a pin the audio engine already has open cannot be opened again, so while any
# program is recording from that input (the Sound control panel's Recording tab meters every
# input, which counts) the service says connected and creates no Fader devices at all.
FADERS_MISSING = ("RODE's driver is connected but has not created its fader inputs. Close anything "
                  "recording from the desk's Main Multitrack input, the Sound control panel "
                  "included, then set what this computer receives to just the mix and back "
                  "again.")     # the desk ignores a write of the value it holds, so "apply
                                # again" would re-enumerate nothing


def reconcile(expect_connected: Optional[bool] = None, move_default: bool = True,
              parked: str = "") -> Outcome:
    """Make the system agree with the driver: its devices enabled while the service is connected
    and disabled while it is not, and the default playback device on the desk when a mode change
    has taken away the one it was on.

    `expect_connected` True waits for the service to bind first (a mode write has just landed).
    None trusts the service's flag as it reads now. False means the desk is in Standard, where the
    service cannot be bound: it polls, so for a few seconds after the desk leaves Expanded its flag
    still says connected, and acting on that switches the devices ON on the way out.

    `parked` is the device id `park_output` moved the default to, if any.

    Safe to call at any time and from any thread. Never raises: the desk's controls must not fail
    because the system's audio service is having a moment.
    """
    status = wait_connected() if expect_connected else read_status()
    if expect_connected is False and status.connected:
        status = replace(status, connected=False, desk="")
    out = Outcome(status=status)
    try:
        out = replace(out, **sync_endpoints(status, expect_connected, move_default, parked))
    except Exception as exc:
        out.error = f"Could not update the audio devices in {OS_NAME}: {exc}"
    if out.enabled or out.disabled or out.faders_missing or out.default_moved_to or out.error:
        diagnostics.event("virtual_devices.reconcile", status.phrase(),
                          enabled=len(out.enabled), disabled=len(out.disabled),
                          faders=out.faders_ready, missing=out.faders_missing,
                          default=out.default_moved_to or "-", error=out.error or "-")
    return out


# --- Parking the default output while the service binds ----------------------------------------
# The service's bind needs the desk's own Main output to itself, and anything playing there when the
# desk arrives keeps it unbound. Parking the default output on another live device for the arrival
# keeps the output free.

@dataclass
class Parking:
    id: str                         # the device the default was moved to
    name: str


def park_output() -> Optional[Parking]:
    """Move the default playback off the desk onto another live device. None when the default is
    already elsewhere (nothing to do) or there is nowhere to park. Never raises."""
    try:
        moved = park_default()
    except Exception as exc:
        diagnostics.event("virtual_devices.park_failed", f"{exc}")
        return None
    if moved is None:
        return None
    diagnostics.event("virtual_devices.park", moved[1])
    return Parking(id=moved[0], name=moved[1])


def keep_parked(parking: Parking) -> bool:
    """Put the default back on the parking device if the system has moved it onto the desk, as it
    does when the desk re-appears. Returns whether it had to. Never raises."""
    try:
        return hold_default(parking.id)
    except Exception as exc:
        diagnostics.event("virtual_devices.park_failed", str(exc))
        return False


def default_to_system(timeout_s: float) -> str:
    """Once the service has bound, make `System (RØDECaster)` the default as soon as it appears,
    which is within about a second of the bind. Returns its name, or "" if it did not appear.
    Never raises."""
    try:
        return _default_to_system(timeout_s)
    except Exception as exc:
        diagnostics.event("virtual_devices.default_failed", str(exc))
        return ""


def describe(expanded: Optional[bool] = None) -> dict:
    """For the page: the driver's state and what its devices are doing, as words. `expanded`
    False (the desk is in Standard) overrides a connected flag the service has not cleared yet,
    exactly as `reconcile` does."""
    st = read_status()
    if expanded is False and st.connected:
        st = replace(st, connected=False, desk="")
    active = total = 0
    try:
        active, total = virtual_endpoint_counts()
    except Exception as exc:
        diagnostics.event("virtual_devices.endpoints_unread", str(exc))
    text = st.phrase()
    if st.connected:
        text += (f" {active} of {total} virtual devices are on." if total
                 else f" No virtual devices are listed in {OS_NAME}.")
    return {"installed": st.installed, "running": st.running, "connected": st.connected,
            "enabled": st.enabled, "driver_version": st.driver_version,
            "virtual_active": active, "virtual_total": total, "text": text}


__all__ = ["BIND_HISTORY_NAME", "CONNECT_WAIT_S", "FADERS_MISSING", "DriverStatus", "Outcome",
           "Parking", "default_to_system", "describe", "keep_parked", "park_output", "read_status",
           "reconcile", "record_bind", "service_state", "wait_connected", "watch_endpoints"]
