"""INSTALLING A FIRMWARE UPDATE: the desk's own updater, driven through its SYSTEM node.

The desk does the work, exactly as from its touchscreen: it downloads RODE's image over its own
network connection and installs it. Nothing is flashed from this computer. The sequence, from the
engine (`rc_audio_mixer`), run end to end on a Pro II from 1.7.6 to 1.7.7 in 4.5 minutes:

  1. `updateCheckRequested` True: the desk reads RODE's manifest. A newer OS sets
     `osUpdateAvailable` (the full image, about 140 MB); a newer application alone sets
     `appUpdateAvailable`. `updateVersion` names it.
  2. `downloadInitiateRequested` True: the download. `updateDownloadProgress` is pushed at each
     percent, 0 to 100 (about 30 seconds for 1.7.7 on a home connection), then `updateDownloaded`.
     `downloadCancelRequested` True stops it and deletes what was staged, but sent mid-download it
     holds the engine for about two and a half minutes, every model dump coming back empty, so
     nothing here sends it.
  3. `updateInitiateRequested` True: the install. An OS install verifies the image's MD5, pushes
     `updateInstalledProgress` 5000, writes the new system partition carrying over the Bluetooth
     pairings, the time zone and the saved wifi networks, then pushes 10000 and `updateComplete`.
     It does not restart the desk (1.7.7: `updateComplete` 64 s after the request, and no progress
     push seen). An application-only install unpacks and restarts on its own.
  4. `updateRebootRequested` True: the restart onto the new firmware. 1.7.7 left USB 12 s later and
     came back on its normal identity 68 s after that.

THE REQUEST FLAGS ARE NOT CLEARED BY THE DESK: the download and its cancel stay True once written.
The engine acts on a change to True, so every request is written False first.

`updateResetAfterFWURequested` TURNS A RESTART INTO A FACTORY WIPE: with it set, the restart branch
runs the device reset instead (wifi, Bluetooth, CallMe sign-in and settings all erased). Nothing
here writes it, and nothing restarts the desk while it reads True.
"""
from __future__ import annotations

import time

import diagnostics
import model_tree
import rcp_hid
import rcp_read
import rcp_transfer

# Every property the update uses, all on the SYSTEM node.
PROPS = ("systemFirmwareVersion", "appUpdateAvailable", "osUpdateAvailable", "updateVersion",
         "updateCheckRequested", "updateChecking", "updateNoInternet",
         "downloadInitiateRequested", "downloadCancelRequested", "updateDownloadProgress",
         "updateDownloaded", "updateInitiateRequested", "updateInstalledProgress",
         "updateComplete", "updateRebootRequested", "updateResetAfterFWURequested")
# Requests written False before an update starts, so each True is a change the engine acts on.
REQUESTS = ("downloadInitiateRequested", "downloadCancelRequested", "updateInitiateRequested")
# The identity a desk takes while it is being flashed over USB. It answers nothing.
FLASHING_PID = 0x0030

CHECK_WAIT_S = 20.0
DOWNLOAD_STALL_S = 120.0       # no progress for this long is a dead download
FINISHED_QUIET_S = 15.0        # 99 percent then this long with no `updateDownloaded`: read it
INSTALL_WAIT_S = 20 * 60.0     # an OS install pushes nothing between its start and its end
LEAVES_BY_ITSELF_S = 45.0      # after an install, how long the desk is given to restart on its own
LEAVES_AFTER_ASK_S = 60.0      # a restart request takes the desk off USB in about 12 s (measured)
BACK_WITHIN_S = 10 * 60.0      # a restart onto new firmware, generously
PRESENCE_POLL_S = 1.0


class Stopped(Exception):
    """The update ended early, with the reason in words for the user."""


def read(blob: bytes) -> dict:
    """{name: (path, value)} for every update property the dump carries. A name carried twice
    keeps its first meaningful value: `systemFirmwareVersion` can be blank in one place."""
    root = model_tree.parse(blob)[0]
    out = {}
    for node in model_tree.walk(root):
        for name, value, _kind in node.props:
            if name in PROPS and (name not in out or out[name][1] in (None, "")):
                out[name] = (node.path(), value)
    return out


def values(state: dict) -> dict:
    return {name: value for name, (_path, value) in state.items()}


def offered(state: dict) -> str:
    """The version the desk offers, or "" when it offers none."""
    v = values(state)
    if v.get("osUpdateAvailable") or v.get("appUpdateAvailable"):
        return v.get("updateVersion") or ""
    return ""


def refusal(state: dict, online: bool, recording: bool, in_call: bool) -> str:
    """Why an update cannot start now, or "" when it can. Checked before anything is written."""
    v = values(state)
    missing = [p for p in ("downloadInitiateRequested", "updateInitiateRequested",
                           "updateRebootRequested", "updateResetAfterFWURequested")
               if p not in state]
    if missing:
        return "This unit does not offer installing a firmware update from AccessCaster."
    if v.get("updateResetAfterFWURequested"):
        return ("The unit is set to erase itself on its next restart, so AccessCaster will not "
                "install anything that restarts it. Install the update on the unit's screen.")
    if not offered(state):
        return "The unit has no firmware update waiting. Check for one first."
    if not online:
        return ("The unit is not connected to the internet, so it cannot download the update. "
                "Connect it to wifi or a network cable first.")
    if recording:
        return "The unit is recording. Stop the recording before updating the firmware."
    if in_call:
        return "A CallMe call is in progress. End it before updating the firmware."
    return ""


class Desk:
    """The desk as the installer sees it. `lock` is the app's HID lock, held for each read and
    write and never across a wait."""

    def __init__(self, lock):
        self._lock = lock

    def state(self) -> dict:
        with self._lock:
            return read(rcp_read.read_model_stable(fresh=True))

    def write(self, state: dict, name: str, value: bool) -> None:
        with self._lock:
            rcp_transfer.send_record(rcp_transfer.build_path_prop_bool(name, value, state[name][0]))
        diagnostics.event("firmware.write", name, value=value)

    def listener(self):
        return rcp_read.PushListener()

    def present(self):
        """The desk's USB product id while its control interface is on the bus, else None."""
        try:
            iface = rcp_hid.find_control_interface(fresh=True)
        except Exception:
            return None
        return iface.product_id if iface is not None else None

    def flashing(self) -> bool:
        """The desk is on the bus under its flashing identity."""
        try:
            return any(i.product_id == FLASHING_PID for i in rcp_hid.enumerate_interfaces())
        except Exception:
            return False


def install(desk, step, target: str) -> dict:
    """Download and install the update the desk offers, restart it, and confirm the version it
    comes back on. `step(message)` reports each stage. Returns the job's result."""
    try:
        state = _check(desk, step, target)
        _download(desk, step, state, target)
        restarts_itself = _install(desk, step, desk.state(), target)
        _restart(desk, step, target, restarts_itself)
        return _confirm(desk, step, target)
    except Stopped as stop:
        diagnostics.event("firmware.stopped", str(stop))
        return {"ok": False, "message": str(stop)}


def _check(desk, step, target):
    """Ask RODE again, so the install is of what the desk is offered now."""
    step("Checking with RODE for the update.")
    state = desk.state()
    for name in REQUESTS:
        if values(state).get(name):
            desk.write(state, name, False)
    with desk.listener() as listen:
        desk.write(state, "updateCheckRequested", True)
        listen.wait_for("updateChecking", CHECK_WAIT_S, match=lambda v: v is False)
    state = desk.state()
    v = values(state)
    if v.get("updateNoInternet"):
        raise Stopped("The unit could not reach RODE's update server, so nothing was downloaded "
                      "or changed. Check its network connection and try again.")
    if offered(state) != target:
        raise Stopped(f"The unit no longer offers firmware {target}, so nothing was changed. "
                      "Check for an update again.")
    return state


def _download(desk, step, state, target):
    step(f"Downloading firmware {target}.")
    said, percent = 0, 0
    with desk.listener() as listen:
        desk.write(state, "downloadInitiateRequested", True)
        while True:
            # A download that ends with `updateDownloaded` already True from an earlier one pushes
            # nothing at the end, so a finished percentage followed by quiet is read back instead.
            got = listen.wait_any(("updateDownloadProgress", "updateDownloaded",
                                   "updateNoInternet"),
                                  FINISHED_QUIET_S if percent >= 99 else DOWNLOAD_STALL_S)
            if got is None and percent >= 99 and values(desk.state()).get("updateDownloaded"):
                break
            # Not cancelled: a cancel during a download holds the engine for minutes, and a
            # download left behind installs nothing.
            if got is None:
                raise Stopped(f"The download of firmware {target} stopped making progress. "
                              "Nothing was installed. Check the unit's network connection and "
                              "try again.")
            name, value = got
            if name == "updateNoInternet" and value:
                raise Stopped("The unit lost its internet connection during the download. "
                              "Nothing was installed.")
            if name == "updateDownloaded" and value:
                break
            if name == "updateDownloadProgress" and isinstance(value, int):
                percent = value
                diagnostics.event("firmware.download", "progress", percent=value)
                if value // 10 > said:
                    said = value // 10
                    step(f"Downloading firmware {target}: {value} percent.")
    step(f"Firmware {target} has downloaded.")


def _install(desk, step, state, target) -> bool:
    """Start the install and wait for it to end. True if the desk restarts by itself: an
    application-only install does, and is over once the desk has gone."""
    app_only = not values(state).get("osUpdateAvailable")
    step(f"Installing firmware {target}. This takes a few minutes. Do not switch the unit off.")
    with desk.listener() as listen:
        desk.write(state, "updateInitiateRequested", True)
        deadline = time.monotonic() + INSTALL_WAIT_S
        started = False
        while time.monotonic() < deadline:
            got = listen.wait_any(("updateComplete", "updateInstalledProgress"), 5.0)
            if got is not None:
                diagnostics.event("firmware.install", got[0], value=got[1])
                # The progress goes to 5000 as the install starts and back to 10000 as it ends,
                # which also covers an `updateComplete` already True and so never pushed.
                started = started or got == ("updateInstalledProgress", 5000)
                if got == ("updateComplete", True) or (
                        started and got == ("updateInstalledProgress", 10000)):
                    return False
            if listen.lost or (app_only and desk.present() is None):
                return True
    raise Stopped(f"The unit has not said the install of firmware {target} finished. Leave it "
                  "switched on for now, and check its screen or check for an update in a few "
                  "minutes.")


def _restart(desk, step, target, restarts_itself):
    step(f"Firmware {target} is installed. The unit is restarting onto it, and its audio stops "
         "until it is back, which takes a minute or two.")
    if restarts_itself or _leaves(desk, LEAVES_BY_ITSELF_S):
        return
    state = desk.state()
    if values(state).get("updateResetAfterFWURequested"):
        raise Stopped(f"Firmware {target} is installed, but the unit is set to erase itself on "
                      "its next restart, so AccessCaster has not restarted it. Restart it from "
                      "its own screen when you are ready.")
    desk.write(state, "updateRebootRequested", True)
    if not _leaves(desk, LEAVES_AFTER_ASK_S):
        raise Stopped(f"Firmware {target} is installed, but the unit did not restart when asked. "
                      "Switch it off and on with the red button on the back to finish.")


def _leaves(desk, within: float) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if desk.present() is None:
            diagnostics.event("firmware.desk_left", "off the USB bus")
            return True
        time.sleep(PRESENCE_POLL_S)
    return False


def _confirm(desk, step, target) -> dict:
    step("Waiting for the unit to come back.")
    deadline = time.monotonic() + BACK_WITHIN_S
    seen = set()
    while time.monotonic() < deadline:
        pid = desk.present()
        if desk.flashing() and FLASHING_PID not in seen:
            seen.add(FLASHING_PID)
            diagnostics.event("firmware.flashing", "the unit is on USB as 0x0030")
            step("The unit is still updating. Leave it switched on.")
        if pid is not None and pid != FLASHING_PID:
            if pid not in seen:
                seen.add(pid)
                diagnostics.event("firmware.desk_back", "control interface", pid=f"0x{pid:04x}")
            try:
                version = values(desk.state()).get("systemFirmwareVersion")
            except Exception as exc:
                diagnostics.event("firmware.read_failed", "still waiting", error=str(exc))
                version = None
            if version:
                return _result(desk, target, version)
        time.sleep(PRESENCE_POLL_S * 2)
    raise Stopped(f"The unit has not come back from installing firmware {target}. Leave it "
                  "switched on for a few more minutes. If it still does not appear, unplug the "
                  "USB cable from the socket nearest the power inlet and plug it back in.")


def _result(desk, target, version) -> dict:
    diagnostics.event("firmware.version", version)
    if version != target:
        return {"ok": False, "version": version,
                "message": f"The unit is back but reports firmware {version}, not {target}, so "
                           "the update did not take. Its settings are as they were."}
    try:
        state = desk.state()
        for name in REQUESTS:
            if values(state).get(name):
                desk.write(state, name, False)
    except Exception as exc:
        diagnostics.event("firmware.tidy_failed", "request flags left as they were",
                          error=str(exc))
    return {"ok": True, "version": version,
            "message": f"Firmware {version} is installed, and the unit is back."}
