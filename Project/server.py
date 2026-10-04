"""
AccessCaster - accessible local web server.

A screen-reader and keyboard friendly controller for the RODECaster Pro II.
It talks to the unit over the same USB control channel RODE Central uses and
presents the controls we have reverse-engineered and verified on hardware:

  - System: enable/disable MIDI control (the unlock for the Access Deck app).
  - Mic channels 1-4: input gain (dB) and 48V phantom power.
  - SMART pads: replace a configured pad's sound (overwrite-in-place).

READ-BACK (session 12): the device DOES report its current settings. One command
(report-3 payload ad10a7b0) makes it stream its whole property model; rcp_read
parses it. So the mixer/system controls now show the unit's REAL current state,
read live over USB, not just the last command we sent.

Design (matches the sibling Access Deck / LightSwitch apps):
- stdlib http.server only, no web framework
- real native HTML controls, ARIA live regions, true modal <dialog> for any
  decision UI
- built to WCAG 2.2 AA and the ARIA APG rather than to one screen reader's quirks, which is
  what should make it behave the same across them. EXERCISED on NVDA; JAWS and Narrator
  have had some testing and VoiceOver none, so "should" is doing real work in that sentence

Run:  python server.py
Then open http://localhost:8770  (also reachable from another device on the LAN
at http://<this-pc-ip>:8770, though the USB control happens on this PC).
"""

from __future__ import annotations

import json
import os
import queue
import secrets                    # remote-access token (see access_token)
import shutil
import socket                     # to report this machine's LAN address for the remote link
import subprocess
import tempfile
import threading
import urllib.parse               # to read ?t=<token> off a request path
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import time

import diagnostics            # the app's own account of what it did, in %LOCALAPPDATA%\\...\\logs
import rcp_hid
import rcp_read
import model_tree
import rcp_transfer
import channels
import devices                  # per-device model (matrix labels, caps) keyed by product id
import soundpad                 # single source of truth for ALL pad operations
import midi_out                 # the desk's own MIDI port: the ONLY way to play a sound pad
import autostart               # the HKCU Run entry, shared with installer.iss
import app_settings            # THIS APP's own settings (shortcuts, announcements) - export /
                               # import. Pure, like profiles: it never touches a file.
import profiles                 # settings backup/restore (save+restore live here, not in server)
import routing_schemes          # saved output-mix schemes (the matrix under a name) + the built-ins
import meters                   # level metering, read from Windows' audio endpoints (not over USB)
import virtual_devices          # RODE's Virtual Device Driver: its state, and switching its endpoints on
import driver_install           # fetching that driver from RODE: the app's ONLY outbound call
import autogain                # sets a mic's gain by measuring it
import recorder                 # the transport: start/pause/stop, storage volumes, recordings
import callme                   # RODE's remote guests: hosting, answering, call state
import shows                    # the DESK'S own backup: export/import/factory reset (not profiles)
import tones                    # the announcer's non-speech cues (the mute tones), generated WAVs
import hotkeys                  # system-wide keyboard shortcuts: the Win32 half (keys, no meaning)
import commands                 # ...and the CATALOGUE of what a shortcut can do, generated from
                                # the app's own spec tables
import page                     # assembles index.html from static/index.html + static/help.html
from paths import resource_path, data_path   # frozen-build-safe asset/data locations
from version import __version__ as APP_VERSION   # single source of truth (Project/version.py)

HTTP_HOST = "0.0.0.0"     # bind all interfaces so a phone on the LAN can view the UI
HTTP_PORT = 8770
STATIC_DIR = resource_path("static")

# One lock around ALL device access. ThreadingHTTPServer serves requests on
# separate threads and the unit must never be opened twice at once; this also
# serialises the slow transfer-mode pad write against status reads.
_hid_lock = threading.Lock()

# --- App lifecycle: the server lives only as long as a webpage is open. ---------------------
# The page heartbeats /api/ping; closing it fires /api/quit (sendBeacon) and stops sending pings.
# A watchdog shuts the server down when no client has pinged for _CLIENT_TIMEOUT, so the server
# behaves like an app (open = running, close = stopped). DISABLED in headless mode
# (ACCESS_CASTER_NO_UI) so our background/tooling runs are not auto-killed.
_httpd = None                      # set in main(); the running ThreadingHTTPServer
_last_ping = time.time()           # updated by every /api/ping
# Which run of the server this is. The ping reply carries it with the version, so an open page can
# tell that the server it was loaded from has gone and a different one answers in its place.
_INSTANCE_ID = secrets.token_hex(8)
_preview_grid = None               # grid of the live-preview scratch pad, if one is engaged
_client_seen = False               # True once any page has pinged at least once
_quit_at = None                    # set by /api/quit; a deadline a ping can still cancel
_CLIENT_TIMEOUT = 600.0            # seconds with no ping before the watchdog stops the server.
                                   # Generous so a backgrounded/throttled tab (browsers clamp
                                   # hidden timers to ~1/min) is not mistaken for a closed page;
                                   # the page also re-pings on visibility/focus to reset this.
_STARTUP_GRACE = 120.0             # seconds to wait for the first page before giving up
_QUIT_GRACE = 5.0                  # after a quit beacon, wait this long so a RELOAD can cancel it


def _request_shutdown():
    """Stop the HTTP server from a worker/watchdog thread (shutdown() blocks the calling
    thread until serve_forever returns, so it must not run on a request thread)."""
    # Let the endpoint watcher unregister its callback rather than leaving Windows' audio service
    # holding a pointer into a process that is going away.
    _virtual_devices_watch_stop.set()
    if _httpd is not None:
        threading.Thread(target=_httpd.shutdown, daemon=True).start()


def _lifecycle_stop_reason(now, start, client_seen, last_ping, quit_at, job_running=False):
    """Pure decision for the lifecycle watchdog (unit-testable). Returns a stop reason string,
    or None to keep running. Triggers, in priority order:
      - a /api/quit beacon set quit_at and its grace has elapsed (a reload's ping clears quit_at),
      - no page ever connected within _STARTUP_GRACE (browser failed to open),
      - the connected page went silent for _CLIENT_TIMEOUT (closed, crashed, lost network).

    A RUNNING JOB OUTRANKS ALL THREE. Without this, a page that goes away mid-backup stops the HTTP
    server while the desk is healthy and still on the bus and the process is still alive, and from
    the outside that is indistinguishable from the desk dropping off the bus.
    The server exists to serve the page, but a job is not the page's work, it is the DESK's: a
    backup is mid-read of the model and a restore is part way through writing settings and pads. A
    window closing must not decide to abandon that. Once the job ends, every trigger applies as
    before - including the quit beacon, which is still pending and fires on the next pass."""
    if job_running:
        return None
    if quit_at is not None and now >= quit_at:
        return "Webpage closed; stopping AccessCaster."
    if not client_seen:
        if now - start > _STARTUP_GRACE:
            return "No webpage connected; stopping."
        return None
    if now - last_ping > _CLIENT_TIMEOUT:
        return "Webpage heartbeat lost; stopping AccessCaster."
    return None


def _client_watchdog():
    """Background: stop the server once every webpage has closed. See _lifecycle_stop_reason."""
    start = time.time()
    while True:
        time.sleep(1.0)
        if _httpd is None:
            continue
        # Both job states, because either can be the one holding the desk.
        busy = _profile_job.busy() or _show_job.busy()
        reason = _lifecycle_stop_reason(time.time(), start, _client_seen, _last_ping, _quit_at,
                                        job_running=busy)
        if reason:
            diagnostics.event("app.watchdog_stop", reason)
            print(reason)
            _request_shutdown()
            return

# Channel / pad layout of the RODECaster Pro II SMART pads.
MIC_CHANNELS = (1, 2, 3, 4)
# Pad grid geometry is now DEVICE-AWARE (devices.PadSpec): the Pro II is 8 pads/bank, the Duo 6.
# These module constants are the Pro II DEFAULTS, kept only as the floor for the dynamic bank
# growth (PAD_MIN_BANKS) and the offline fallback in soundpad_model(). The per-device pads/bank,
# user-bank count and the per-action range limit come from the active device: read_pads_live()
# resolves the geometry, and _max_user_pads() gives the hard range limit for a pad action.
# The visible grid is DYNAMIC - it grows with content (every filled bank plus one spare), floor
# PAD_MIN_BANKS - so a new bank appears as you fill the previous one, like the device itself.
PAD_MIN_BANKS = 3
PADS_PER_BANK = 8      # Pro II default (fallback); active device's PadSpec.pads_per_bank wins
GAIN_MIN_DB = 0
GAIN_MAX_DB = 76
# Non-mic channel "Level" (inputDigitalGain) range. The device clamps; these are sane UI bounds
# (the app's default is -12 dB). Pending a captured min/max, kept conservative.
LEVEL_MIN_DB = -60
LEVEL_MAX_DB = 12

# Channel-processing controls exposed in the UI. Every parameter is a normalised
# 0..1 float on the device (we present it as 0-100 percent); the property name
# drives it (see channels.set_channel_processing / PROCESSING_PROPS). Grouped
# the way the RODE app's per-channel processing panel groups them.
PROCESSING_CONTROLS = [
    # Each band is gain, then frequency, then WIDTH (Q), so the three controls for one band sit
    # together rather than the six-then-three arrangement the property list would suggest. "Width" is
    # the label, with Q in brackets: Q is the engineer's word and width is what it does, and a
    # screen-reader user arrowing a slider needs the second one first. Q is a control at all only
    # because it is independently writable (see channels.PROCESSING_PROPS).
    {"group": "Equaliser", "controls": [
        {"prop": "eqLowGain", "label": "Low gain"},
        {"prop": "eqLowShelf", "label": "Low frequency"},
        {"prop": "eqLowQ", "label": "Low width (Q)"},
        {"prop": "eqMidGain", "label": "Mid gain"},
        {"prop": "eqMidBell", "label": "Mid frequency"},
        {"prop": "eqMidQ", "label": "Mid width (Q)"},
        {"prop": "eqHighGain", "label": "High gain"},
        {"prop": "eqHighBell", "label": "High frequency"},
        {"prop": "eqHighQ", "label": "High width (Q)"},
    ]},
    {"group": "High-pass filter", "controls": [
        {"prop": "hpfFrequency", "label": "Frequency"},
        {"prop": "hpfSlope", "label": "Slope"},
    ]},
    {"group": "Compressor", "controls": [
        {"prop": "compressorThreshold", "label": "Threshold"},
        {"prop": "compressorRatio", "label": "Ratio"},
        {"prop": "compressorAttack", "label": "Attack"},
        {"prop": "compressorRelease", "label": "Release"},
        {"prop": "compressorGain", "label": "Makeup gain"},
    ]},
    {"group": "Noise gate", "controls": [
        {"prop": "noiseGateThreshold", "label": "Threshold"},
        {"prop": "noiseGateAttack", "label": "Attack"},
        {"prop": "noiseGateHold", "label": "Hold"},
        {"prop": "noiseGateRelease", "label": "Release"},
        {"prop": "noiseGateRange", "label": "Range"},
        {"prop": "noiseGateHysteresis", "label": "Hysteresis"},
    ]},
    {"group": "De-esser", "controls": [
        {"prop": "deesserThreshold", "label": "Threshold"},
        {"prop": "deesserRatio", "label": "Ratio"},
        {"prop": "deesserAttack", "label": "Attack"},
        {"prop": "deesserRelease", "label": "Release"},
        {"prop": "deesserGain", "label": "Gain"},
        {"prop": "deesserFrequency", "label": "Frequency"},
    ]},
    {"group": "Tone shaping", "controls": [
        {"prop": "channelDepth", "label": "Depth"},
        {"prop": "channelSparkle", "label": "Sparkle"},
        {"prop": "channelPunch", "label": "Punch"},
        {"prop": "aphexBBDrive", "label": "Big bottom (bass)"},
        {"prop": "aphexAEMix", "label": "Aural exciter (air)"},
    ]},
    # A mono channel gets channelPan; a stereo one gets the two legs instead (see devices.Channel
    # .stereo), so "stereo" says which kind of channel a control is for.
    {"group": "Pan", "controls": [
        {"prop": "channelPan", "label": "Pan", "pan": True, "stereo": False},
        {"prop": "channelPanL", "label": "Left side position", "pan": True, "stereo": True},
        {"prop": "channelPanR", "label": "Right side position", "pan": True, "stereo": True},
    ]},
]
_PROCESSING_CONTROL_BY_PROP = {c["prop"]: c for g in PROCESSING_CONTROLS for c in g["controls"]}
# How a write is reported: "Threshold" alone could be the compressor's, the gate's or the de-esser's,
# so the group goes in front wherever the label does not already name its module.
_PROCESSING_SPOKEN = {c["prop"]: (c["label"] if g["group"] in ("Pan", "Tone shaping")
                                  else f"{g['group']} {c['label'].lower()}")
                      for g in PROCESSING_CONTROLS for c in g["controls"]}
_PROCESSING_PROP_SET = set(_PROCESSING_CONTROL_BY_PROP)

# Current known state of the mixer. We CAN read these live from the device now
# (rcp_read), and every value we set is sent to the device, so this dict holds
# the unit's real state. "_read_at" is when we last did a full live read; None
# until the first read/set, so the UI can say "unknown" honestly before then.
_session_state_lock = threading.Lock()
_session_state = {
    "midi": None,                                  # bool or None (unknown)
    "channels": {c: {"gain_db": None, "phantom": None, "polarity": None, "mic_type": None}
                 for c in MIC_CHANNELS},
    "firmware": None,
    "read_at": None,                               # epoch seconds of last live read
}


def _state_snapshot() -> dict:
    with _session_state_lock:
        read_at = _session_state["read_at"]
        if read_at:
            when = time.strftime("%H:%M:%S", time.localtime(read_at))
            # No "Read from device" button exists; each page re-reads the unit when you open it.
            note = (f"Read live from the device at {when}. Values you change here "
                    f"are sent to the unit immediately, and the unit is read again "
                    f"each time you open a page.")
        else:
            note = ("Not read from the device yet. Opening a page reads the unit's "
                    "current settings.")
        return {
            "midi": _session_state["midi"],
            "channels": {
                str(c): dict(_session_state["channels"][c]) for c in MIC_CHANNELS
            },
            "firmware": _session_state["firmware"],
            "read_at": read_at,
            "live_supported": True,
            "gain_min_db": GAIN_MIN_DB,
            "gain_max_db": GAIN_MAX_DB,
            # mic input type/voicing options, as [value, name] pairs. Only the confirmed values:
            # the full 0..10 map excludes Wireless, which needs a wireless device to capture.
            "mic_types": sorted([list(kv) for kv in channels.MIC_TYPE.items()]),
            "mic_types_partial": False,
            "note": note,
        }


def read_live_settings() -> dict:
    """Live-read mic gain/phantom + MIDI from the device and update session state.

    Read-only on the device. Returns {"ok": bool, ...snapshot...}.
    """
    try:
        with _hid_lock:
            m = channels.read_mixer()
    except Exception as exc:
        return {"ok": False,
                "message": f"Could not read settings from the device: {exc}"}
    with _session_state_lock:
        _session_state["midi"] = m.get("midi")
        _session_state["firmware"] = m.get("firmware")
        # read_mixer returns only the CONNECTED device's mics (4 on the Pro II, 2 on the Duo);
        # iterate those, leaving any mic slots the desk does not have as their initialised None.
        for c, mc in m.get("mics", {}).items():
            if c in _session_state["channels"]:
                _session_state["channels"][c]["gain_db"] = mc.get("gain_db")
                _session_state["channels"][c]["phantom"] = mc.get("phantom")
                _session_state["channels"][c]["polarity"] = mc.get("polarity")
                _session_state["channels"][c]["mic_type"] = mc.get("mic_type")
        _session_state["read_at"] = time.time()
    snap = _state_snapshot()
    snap["ok"] = True
    snap["message"] = "Read current settings from the device."
    return snap


def _wireless_mics(blob) -> dict:
    """RODE wireless transmitters (Series IV etc.) as the unit reports them.

    SHARED by the Hardware state read and the Wireless connections read so the two can never
    disagree. The readout lives on Wireless connections: a radio link to another device belongs
    with the other radio links, not in the card-and-recorder report.

    PRESENT and CONNECTED are different facts and the page needs both. An empty slot list means the
    dump carried no txConnected at all, which is not the same as "no mic is on" and must not be
    read out as if it were. Nothing has ever been paired here, so this is structure-only until it is.
    """
    def col(name, kind=None):
        return rcp_read.values(blob, name, kind)

    conn = col("txConnected")
    batt = col("txBatteryLevel", "int")
    rssi = col("txRssi", "int")
    tx = [{"index": i, "connected": bool(c),
           "battery_pct": batt[i] if i < len(batt) else None,
           "rssi": rssi[i] if i < len(rssi) else None}
          for i, c in enumerate(conn)]
    return {"transmitters": tx,
            "present": bool(tx),
            "any_connected": any(t["connected"] for t in tx),
            "unproven": True}


def read_device_state() -> dict:
    """READ-ONLY live state a blind operator cannot otherwise know: the memory card and its free
    space, the recorder, the monitor speaker level, the firmware and the wired network.

    The "Live state / readable feedback" class in PARITY.md, which a feature tracker built from a
    user guide does not ask about, because a user guide only describes controls. One model dump,
    no writes.

    HONEST GAPS, reported as such rather than shown as zeroes: the recorder has never been exercised
    with a card in the desk (recordState reads 3 = idle either way), and no wireless mic has ever been
    paired, so `unproven` is set for those and the UI says so. `storageVolumeState` is the usable
    source for card space - the numeric storageVolumeFree/Capacity read 0 even when a volume is
    mounted (measured)."""
    def val(prop, kind=None):
        return rcp_read.values(blob, prop, kind)

    try:
        with _hid_lock:
            blob = rcp_read.read_model_stable()
    except Exception as exc:
        return {"ok": False, "message": f"Could not read the unit's live state: {exc}"}

    out = {"ok": True}

    # ---- storage. Parsed in ONE place (recorder.read_volumes) so this tab, the overview's
    # nearly-full warning and the Recording page's destination all name the same volume the same
    # way. SLOT 1 IS THE USB PORT, not "Card slot 2", and the difference matters the moment anyone
    # is told where their recording went.
    cards = recorder.read_volumes(blob)
    out["storage"] = {"volumes": cards,
                      "any_card_inserted": any(c["inserted"] for c in cards),
                      "low": [c["label"] for c in cards if c["low"]]}

    # ---- recorder. The whole transport is driveable from software and reads back (see
    # recorder.py). This tab reports it; the Recording page drives it.
    rs = val("recordState", "int")
    state = rs[0] if rs else None
    out["recorder"] = {
        "state": state,
        "state_label": recorder.STATE_LABEL.get(state, "Unknown"),
        "idle": (state == recorder.READY) if state is not None else None,
        # recordTimeMs is reported for completeness and is NOT the live counter: it reads 0 right
        # through a recording. Elapsed time is counted by recorder.Watcher instead.
        "elapsed_ms": (val("recordTimeMs", "int") or [None])[0],
        "total_recordings": (val("recordingTotalCount", "int") or [None])[0],
        "bytes_per_second": (val("recordBytesPerSecond", "int") or [None])[0],
    }

    # ---- monitor speaker level. Readable, pushed live, AND WRITABLE: the selector comes from a
    # device push. "The RODE app has no control for it, so there is nothing to capture, so it must
    # be unwritable" is reasoning that has been wrong five times - never file a property unreachable
    # on it.
    mon = val("outputMonLevel", "float")
    out["monitor"] = {"level_pct": (round(mon[0] * 100) if mon else None), "writable": True}

    # ---- wireless mics. Still in the payload, but the Hardware tab no longer SHOWS them: they read
    # out on Wireless connections now, beside wifi and Bluetooth (see _wireless_mics).
    out["wireless"] = _wireless_mics(blob)

    # ---- firmware and the wired network. Neither is a connection you can make or change, so they
    # sit with the other facts the unit reports about itself rather than on a tab of wireless
    # controls.
    with _session_state_lock:
        fw = _session_state.get("firmware")
    if not fw:
        # As in read_network_status: systemFirmwareVersion sits near the tail of a dump whose length
        # varies, so the value the startup read already captured is the reliable one.
        fw = next((v for v in val("systemFirmwareVersion") if v not in (None, "")), None)
    out["firmware"] = fw
    out["firmware_update"] = _firmware_update(blob)
    wired = next((v for v in val("wiredConnected") if v is not None), None)
    out["wired_connected"] = bool(wired) if wired is not None else None
    return out


# ----------------------------------------------------------------------------
# FIRMWARE UPDATES: telling the user one exists. Installing is NOT offered: an update reboots the
# desk, which is the unit the user's screen reader is probably speaking through, and the install
# could not be tested until RODE ship something newer.
#
# All on the SYSTEM node. `updateCheckRequested` is the touchscreen's Check for Update: the desk
# raises `updateChecking` and clears both within about a second. What an AVAILABLE update looks like
# has not been seen yet; `appUpdateAvailable` / `osUpdateAvailable` and `updateVersion` are the
# fields the desk publishes for it.
_UPDATE_CHECK_WAIT_S = 20.0
_UPDATE_HOW = ("Install it on the unit's screen under Settings, System, Information, or with "
               "RODE's own app.")


def _firmware_update(blob: bytes) -> dict:
    def sys_val(name):
        return rcp_read.block_value(blob, "SYSTEM", name)
    available = bool(sys_val("appUpdateAvailable")) or bool(sys_val("osUpdateAvailable"))
    version = sys_val("updateVersion") or ""
    return {"available": available, "version": version if available else "",
            "no_internet": bool(sys_val("updateNoInternet"))}


def _firmware_update_sentence(upd: dict) -> str:
    if upd.get("available"):
        what = f"Firmware {upd['version']}" if upd.get("version") else "A firmware update"
        return f"{what} is available. {_UPDATE_HOW}"
    if upd.get("no_internet"):
        return ("The unit could not reach RODE's update server. It needs wifi or a network cable "
                "to check.")
    return ""


def action_check_firmware() -> dict:
    """Ask the desk to check RODE's server for a firmware update, and say what it found."""
    listener = rcp_read.PushListener()
    listening = False
    try:
        with _hid_lock:
            blob = rcp_read.read_model_stable(fresh=True)
            found = model_tree.find_property(model_tree.parse(blob)[0], "updateCheckRequested")
            if not found:
                return {"ok": False, "message": "This unit does not offer a firmware check."}
            path = found[0][0].path()
            # Opened before the write: the desk answers within a second.
            try:
                listener.start()
                listening = True
            except Exception:
                pass
            rcp_transfer.send_record(
                rcp_transfer.build_path_prop_bool("updateCheckRequested", True, path))
        if listening:
            listener.wait_for("updateChecking", _UPDATE_CHECK_WAIT_S, match=lambda v: v is False)
        else:
            time.sleep(3.0)
        with _hid_lock:
            blob = rcp_read.read_model_stable(fresh=True)
    except Exception as exc:
        return {"ok": False, "message": f"Could not check for a firmware update: {exc}"}
    finally:
        if listening:
            listener.stop()
    upd = _firmware_update(blob)
    with _session_state_lock:
        fw = _session_state.get("firmware")
    fw = fw or rcp_read.block_value(blob, "SYSTEM", "systemFirmwareVersion") or "unknown"
    msg = (_firmware_update_sentence(upd)
           or f"Your unit is up to date. Firmware {fw} is the latest.")
    return {"ok": True, "firmware": fw, "firmware_update": upd, "message": msg}


# ----------------------------------------------------------------------------
# THE RECORDER. Start, pause and stop from software, and say what the unit is doing.
#
# The protocol side is in recorder.py; this is the app side of it. TWO THINGS SHAPE THIS CODE:
#
#   * **The state comes from a PUSH, not a poll.** recorder.Watcher holds one read-only HID handle
#     and hears `recordState` change within ~30 ms, from our writes AND from the unit's own REC
#     button. So `/api/recorder/live` costs nothing and can be polled while the page is open,
#     while `/api/recorder` (the full page: volumes and the recordings list) does a ~1.5 s dump.
#   * **A write is never trusted.** `requestRecordState` is a request the firmware consumes and
#     resets, so the only honest confirmation is reading `recordState` back, which is what
#     action_record does before it tells anyone the recording started.

_recorder_watch = None


def _recorder_state_changed(was, now) -> None:
    """Speak a recording state change, and SWITCH ANNOUNCEMENTS OFF FOR THE TAKE. On the Watcher's
    thread, so this runs whether the recording was started from here or from the unit's own button.

    ANNOUNCEMENTS ARE SUSPENDED WHILE RECORDING because there is no other way to keep them out of
    the recording. Sending the speech to the computer's own speakers instead of the desk is NOT a
    fix: it moves the announcements off the USB return and they are picked up by the microphones
    anyway. Anything audible in the room reaches the mics, so silence is the only fix that works.

    THE NOTICE IS SPOKEN BEFORE THE SILENCE, and only when the recording was started ON THE UNIT.
    Started from this app, the reply carries the same words into the page's live region instead,
    which makes no sound at all and therefore cannot land in the take. Started on the unit there is
    no reply to carry it, so one short phrase at the top of the recording is the price of the
    operator knowing what just happened.

    A PAUSE COUNTS AS RECORDING here: the take is not finished, and switching announcements on and
    off around every pause would be its own kind of noise.

    WHAT IS SAID COMES FROM THE TRANSITION, NOT FROM `now` ALONE (recorder.spoken_change), which is
    what tells a drive being plugged in apart from a recording stopping. This function always had
    `was` and simply did not use it for the wording. The SUSPENSION decisions below are unchanged
    and still keyed on the new state, correctly: a take is over once the desk leaves RECORDING and
    PAUSED, whatever the reason it left."""
    ours = _ann_is_self_echo(("record",))
    # The last destination we knew about, which on a REMOVAL is the thing that just left. Best
    # effort by design: it is empty until a full page read has happened, and spoken_change falls
    # back to naming no medium at all rather than guessing one.
    medium = _recorder_context.get("where") or ""
    phrase = recorder.spoken_change(was, now, medium)
    if now in (recorder.RECORDING, recorder.PAUSED):
        going_quiet = _announce_on.is_set() and _record_mutes_announcer()
        if _announce_on.is_set() and not ours and phrase:
            # One phrase, not two: the speech pump coalesces, so a second would silence the first.
            _ann_say(f"{phrase}."
                     + (" Announcements off until you stop." if going_quiet else ""))
        _suspend_announcer_for_record()
        return
    if now in (recorder.READY, recorder.NO_DESTINATION):
        # Unconditional, and before the speech: a take that ended because the drive was pulled must
        # give announcements back just the same as one that was stopped. It is a no-op when we did
        # not switch them off in the first place, so an idle plug costs nothing here.
        came_back = _restore_announcer_after_record()
        if not ours and _announce_on.is_set() and phrase:
            _ann_say(phrase + (". Announcements are back on." if came_back else "."))
        return
    if _announce_on.is_set() and not ours and phrase:
        _ann_say(phrase)


def _recorder_watcher():
    """The running Watcher, started on first use. Returns None if it could not be started, and the
    callers then fall back to reading the state out of a model dump."""
    global _recorder_watch
    if _recorder_watch is None:
        try:
            w = recorder.Watcher(on_change=_recorder_state_changed)
            w.start()
            _recorder_watch = w
        except Exception:
            return None
    return _recorder_watch


def _announcer_into_desk() -> dict:
    """Is this app's spoken output going INTO the unit, and therefore into every recording?

    THE CASE THAT MATTERS IS THE INVISIBLE ONE. The announcer's output setting defaults to "the
    system default device", and on a machine with a RODECaster plugged in that default IS the
    RODECaster - so "Recording started" is spoken down USB 1, into the mix, onto the USB 1 track
    and into the stereo mix. Nothing anywhere says so until the recording is played back.

    OUR OWN SPEECH IS HANDLED: announcements are switched off for the duration of a recording and
    switched back on afterwards (_suspend_announcer_for_record). What this reports is what we
    CANNOT switch off - the screen reader, and anything else the computer plays through that
    output. Moving our speech to another device is not a fix either: it only takes the sound off
    the USB return, and anything audible in the room reaches the microphones."""
    try:
        chosen = _ann_say_device or ""
        effective = chosen or meters.default_playback_name()
        default_name = meters.default_playback_name()
        if not effective and not default_name:
            return {"known": False}
        return {"known": True,
                # Whether OUR speech would go into the unit. Handled by the suspend, reported so
                # the page can say that it is handled.
                "into_desk": bool(effective and meters.is_desk_endpoint(effective)),
                "output": effective,
                # The part we cannot do anything about: the computer's default output IS the unit,
                # so everything else the machine says goes into the mix and into the recording.
                "default_into_desk": bool(default_name
                                          and meters.is_desk_endpoint(default_name)),
                "default_output": default_name,
                "announcing": _announce_on.is_set(),
                "mute_while_recording": _record_mutes_announcer()}
    except Exception:
        return {"known": False}


def _recorder_payload(blob: bytes) -> dict:
    """The whole Recording page from one dump. ONE builder, used by the page read AND by every
    transport command's reply.

    That is not tidiness. When the command replied with a SMALLER payload, the page rebuilt the
    multitrack dropdown from a missing option list and emptied it - so pressing Start wiped a
    control, which is exactly the "nothing may replace a subtree a screen reader may be sitting in"
    fault this project has been bitten by three times."""
    dev = devices.get_active_device()
    out = recorder.describe(blob)
    out["writable"] = "recorder" in dev.caps
    out["device_name"] = dev.name
    # Multitrack lives here rather than on Routing: it decides what a recording CONTAINS, which is
    # a question you ask on this page and nowhere else. The write still goes to
    # /api/routing/multitrack, which owns the selector.
    mt = rcp_read.values(blob, "recordingMultitrackMode", "int")
    out["multitrack"] = mt[0] if mt else None
    out["multitrack_modes"] = [[k, v] for k, v in sorted(recorder.MULTITRACK_LABEL.items())]
    out["multitrack_hint"] = recorder.MULTITRACK_HINT
    out["multitrack_writable"] = "output_write" in dev.caps

    # WHAT THE FILE IS WRITTEN AS, beside what it contains: the two questions you answer before a
    # take, and neither can be fixed afterwards. Reported as the raw value AND normalised, because
    # the desk stores out-of-range values (see recorder.RECORDING_FORMAT_LABEL): anything non-zero
    # IS an MP3, so the control must show MP3 rather than showing nothing for a stray 3.
    fmt = rcp_read.values(blob, "recordingCompressionQuality", "int")
    raw_fmt = fmt[0] if fmt else None
    out["format"] = None if raw_fmt is None else (0 if raw_fmt == 0 else 1)
    out["format_raw"] = raw_fmt
    out["format_options"] = [[k, v] for k, v in sorted(recorder.RECORDING_FORMAT_LABEL.items())]
    out["format_hint"] = recorder.RECORDING_FORMAT_HINT
    out["format_writable"] = "output_write" in dev.caps

    # The recorder's processing bypass, reported WITH the mode it depends on so the page can say
    # "this does nothing yet" rather than offering a switch that looks dead. The USB destination's
    # equivalent pair is NOT here: it is an output, and it lives on Routing > USB audio.
    pb = rcp_read.values(blob, "recordingProcessingBypass", "bool")
    out["processing_bypass"] = bool(pb[0]) if pb else None
    out["bypass_hint"] = recorder.BYPASS_HINT
    out["bypass_needs_multitrack"] = recorder.BYPASS_NEEDS_MULTITRACK

    # Deleting a take. The page needs to know BEFORE it draws the control, so a button that cannot
    # work is disabled with the reason attached rather than failing when it is pressed.
    out["can_delete"] = bool(out["writable"] and recorder.DELETE_PROVEN)
    out["delete_note"] = ("" if out["can_delete"] else
                          (recorder.DELETE_UNPROVEN_MESSAGE if out["writable"] else
                           f"AccessCaster can read the recorder on the {dev.name} but not drive "
                           f"it, so it cannot delete a recording."))

    out["storage_writable"] = "storage_write" in dev.caps
    out["announcer"] = _announcer_into_desk()
    return out


def read_recorder() -> dict:
    """The Recording page, read from the unit.

    The dump is authoritative, so its state is fed back into the Watcher. That is a self-correction
    rather than a formality: if the listener ever missed a push (unplugged cable, a packet lost
    while the handle was reopening) this puts it right, and `note_state` leaves an unchanged state
    - and therefore the elapsed-time count - completely alone."""
    try:
        with _hid_lock:
            blob = rcp_read.read_model_stable()
    except Exception as exc:
        return {"ok": False, "message": f"Could not read the recorder: {exc}"}
    out = _recorder_payload(blob)
    w = _recorder_watcher()
    if w is not None and out.get("state") is not None:
        # timed=False: a state we learned from a dump says nothing about WHEN it started, so no
        # elapsed time is claimed for a recording we did not see begin.
        w.note_state(out["state"], timed=False)
        out["live"] = w.snapshot()
    _note_recorder_context(out)
    out["ok"] = True
    out["message"] = _recorder_sentence(out)
    return out


# WHERE A RECORDING WOULD GO and HOW LONG THERE IS ROOM FOR, from the last full read.
#
# Kept so the CHEAP endpoint can answer in full sentences too. /api/recorder/live costs no device
# read at all (it is answered from the push listener), and without this it could only say the bare
# state - so "Recording status" would give you less than arriving on the page did.
_recorder_context = {"where": "", "room": "", "low": None}


def _note_recorder_context(payload: dict) -> None:
    dest = payload.get("destination")
    _recorder_context["where"] = dest["spoken"] if dest else ""
    _recorder_context["room"] = (payload.get("room") or {}).get("words") or ""
    _recorder_context["low"] = dest if (dest and dest.get("low")) else None


def _recorder_sentence(state, elapsed: str = "") -> str:
    """What the unit is doing, said in the order it matters while you are working.

    THE ORDER IS THE WHOLE DESIGN OF THIS SENTENCE: while a
    recording is running the only things worth hearing are WHETHER it is running and HOW LONG it
    has been going, with the time left at the END. Where it is recording to is not repeated - you
    were told that when you pressed record - and the storage detail is not here at all; that lives
    on the Device page, where you go to look at it deliberately.

    `state` may be the state code or the whole payload dict, so the page read and the cheap live
    read produce identical wording rather than two versions that drift."""
    if isinstance(state, dict):
        elapsed = elapsed or ((state.get("live") or {}).get("elapsed") or "")
        state = state.get("state")
    where = f" to the {_recorder_context['where']}" if _recorder_context["where"] else ""
    # ROOM LEFT IN TIME, not megabytes: whether a two-hour show fits is the actual question, and
    # nobody can answer it from a free-space figure and a bitrate while a recording is running.
    room = _recorder_context["room"]
    room_left = f" {room} left." if room else ""
    if state == recorder.RECORDING:
        so_far = f" {elapsed} so far." if elapsed else (
            " It started before AccessCaster was watching, so there is no time to give.")
        return f"Recording.{so_far}{room_left}"
    if state == recorder.PAUSED:
        so_far = f" {elapsed} recorded." if elapsed else ""
        return f"Paused.{so_far}{room_left}"
    if state == recorder.READY:
        low = _recorder_context["low"]
        if low:
            room_left += (f" Careful: only {low['free_pct']} percent of the drive is free, "
                          f"about {low['free_mb']} megabytes.")
        return f"Ready to record{where}.{room_left}"
    if state == recorder.NO_DESTINATION:
        return ("The unit has nowhere to record to. Put a memory card in, or a USB drive in the "
                "second USB port, and this will say Ready.")
    return "The unit did not report a recording state."


def read_recorder_live() -> dict:
    """The cheap one: what the unit is doing right now, with NO device read at all.

    Answered entirely from the Watcher's last push, so the page can poll it while it is open
    without costing a 1.5 s dump or fighting anything else for the HID lock."""
    w = _recorder_watcher()
    if w is None:
        return {"ok": False, "message": "The recording state listener is not running."}
    snap = w.snapshot()
    snap["ok"] = True
    # The same sentence the page read produces, built from the listener's state plus the
    # destination and room noted at the last full read. One wording, two endpoints.
    snap["message"] = _recorder_sentence(snap.get("state"), snap.get("elapsed") or "")
    return snap


def action_record(command: str) -> dict:
    """Start, pause, resume or stop, then READ THE STATE BACK before saying what happened.

    The read-back is the whole point. `requestRecordState` is consumed and reset by the firmware,
    so a write that lands on a unit with no destination looks exactly like one that started a
    recording. Reporting "recording" to someone who cannot see the unit, when it is not recording,
    is the single worst thing this page could do."""
    cmd = (command or "").strip().lower()
    if cmd not in recorder.COMMANDS:
        return {"ok": False, "message": f"Unknown recording command: {command}."}
    dev = devices.get_active_device()
    if "recorder" not in dev.caps:
        return {"ok": False,
                "message": (f"AccessCaster can read the recorder on the {dev.name} but not drive "
                            f"it: how to do that has only ever been confirmed on a RODECaster "
                            f"Pro 2, so rather than risk the wrong thing on this model, it "
                            f"refuses.")}

    w = _recorder_watcher()
    before = w.snapshot().get("state") if w else None
    if cmd == "pause" and before == recorder.PAUSED:
        return {"ok": True, "message": "It is already paused. Resume carries on with it."}
    if cmd in ("start", "resume") and before == recorder.RECORDING:
        return {"ok": True, "message": "It is already recording."}
    if cmd == "stop" and before in (recorder.READY, recorder.NO_DESTINATION):
        return {"ok": True, "message": "Nothing is recording."}

    # Mark it BEFORE the write: the unit pushes the new state back in about 30 ms, which can beat
    # this function's own reply to the page.
    _ann_mark_self_write(("record",))
    try:
        with _hid_lock:
            recorder.send_command(recorder.COMMANDS[cmd])
            time.sleep(_RECORD_SETTLE_S)
            blob = rcp_read.read_model_stable(fresh=True)
    except Exception as exc:
        return {"ok": False, "message": f"Could not {cmd} the recording: {exc}"}

    state = recorder.read_state(blob)
    if w is not None:
        w.note_state(state, timed=(cmd in ("start", "resume")))
    # Idempotent, and belt and braces: the Watcher has almost certainly done this already off the
    # unit's push, but a missed push must not leave announcements talking into a take, or leave
    # them off after one.
    announcer_back = False
    if state in (recorder.RECORDING, recorder.PAUSED):
        _suspend_announcer_for_record()
    elif state in (recorder.READY, recorder.NO_DESTINATION):
        announcer_back = _restore_announcer_after_record()
    info = _recorder_payload(blob)
    _note_recorder_context(info)
    wanted = {"start": recorder.RECORDING, "resume": recorder.RECORDING,
              "pause": recorder.PAUSED, "stop": recorder.READY}[cmd]
    if state == wanted:
        msg = {"start": "Recording.", "resume": "Recording again.",
               "pause": "Paused.", "stop": "Stopped."}[cmd]
        dest = info.get("destination")
        if cmd == "start" and dest:
            msg = f"Recording to the {dest['spoken']}."
        if cmd == "stop":
            newest = (info.get("recordings") or [None])[0]
            if newest:
                msg = f"Stopped. Saved {newest['name']}, {newest['duration']}."
        # SAID IN THE REPLY, not spoken: this is the one path where the notice can reach the
        # operator without making a sound, and therefore without landing in the recording.
        if cmd in ("start", "resume") and _ann_suspended_for_record:
            msg += " Announcements are off until you stop."
        elif cmd == "stop" and announcer_back:
            msg += " Announcements are back on."
        return {"ok": True, "state": state, "recorder": info, "message": msg}
    # It did not do what was asked. Say what it IS doing, and why that is usually the answer.
    return {"ok": False, "state": state, "recorder": info,
            "message": f"That did not take. The unit says: {_recorder_sentence(info)}"}


def action_drop_marker() -> dict:
    """Put a marker at the current point of the recording.

    THE REPLY IS DELIBERATELY NOT "DONE". Nothing over USB confirms a marker: the desk consumes the
    write and pushes the field back cleared, and no readable property changes, so the app can only
    honestly say it was sent and where it believes it landed. The proof lives in the file, as a
    standard WAV cue point - which is how this was confirmed at all (two markers at exactly 4.0 s
    and 8.0 s in a take read off the drive on a PC, against a marker-free take with no cue chunk).

    Refused when nothing is running, because a marker has no meaning without a position. The
    ELAPSED TIME is what makes the reply useful: "marker at 2 minutes 5 seconds" is something you
    can find again in an editor, where "marker sent" is not."""
    dev = devices.get_active_device()
    if "recorder" not in dev.caps:
        return {"ok": False,
                "message": (f"AccessCaster can read the recorder on the {dev.name} but not drive "
                            f"it, so it cannot drop a marker.")}
    w = _recorder_watcher()
    state = w.snapshot().get("state") if w else None
    if state != recorder.RECORDING:
        return {"ok": False,
                "message": ("Markers can only be dropped while something is recording."
                            if state != recorder.PAUSED else
                            "Nothing is being recorded while it is paused, so there is nowhere to "
                            "put a marker.")}
    try:
        with _hid_lock:
            recorder.drop_marker()
    except Exception as exc:
        return {"ok": False, "message": f"Could not drop a marker: {exc}"}
    where = (w.snapshot().get("elapsed") if w else "") or ""
    return {"ok": True,
            "message": (f"Marker dropped at {where}." if where else "Marker dropped.")
                       + " It will show up in the recording as a marker your editor can find."}


# The unit takes a moment to act on a request and settle its own state. Measured at well under a
# second on every transition (start, pause, resume and stop), so this is a comfortable margin
# rather than a guess, and the read-back that follows is what actually decides the answer.
_RECORD_SETTLE_S = 1.2


def action_delete_recording(uid: int) -> dict:
    """Delete one recording from the unit's card or drive, by UID.

    THE READ-BACK IS THE ANSWER, not the write. `requestDeleteUID` self-clears whether or not it did
    anything (the same pattern as the record command and the marker), so this re-reads the
    recordings list and reports what it finds: gone, or still there. It never says "deleted"
    because a write was sent.

    Refuses while a recording is running: deleting a take from under the recorder is the one version
    of this that could cost somebody real material rather than a test file."""
    dev = devices.get_active_device()
    if "recorder" not in dev.caps:
        return {"ok": False,
                "message": (f"AccessCaster can read the recorder on the {dev.name} but not drive "
                            f"it, so it cannot delete a recording.")}
    if not recorder.DELETE_PROVEN:
        return {"ok": False, "message": recorder.DELETE_UNPROVEN_MESSAGE}
    w = _recorder_watcher()
    if (w.snapshot().get("state") if w else None) in (recorder.RECORDING, recorder.PAUSED):
        return {"ok": False,
                "message": "Stop the recording first. Nothing is deleted while the recorder is "
                           "running."}
    try:
        with _hid_lock:
            before = recorder.read_recordings(rcp_read.read_model_stable(fresh=True))
            target = next((r for r in before if r.get("uid") == int(uid)), None)
            if target is None:
                return {"ok": False, "message": f"There is no recording with id {uid} on the unit."}
            recorder.delete_recording(int(uid))
            time.sleep(_RECORD_SETTLE_S)
            blob = rcp_read.read_model_stable(fresh=True)
    except Exception as exc:
        return {"ok": False, "message": f"Could not delete the recording: {exc}"}
    info = _recorder_payload(blob)
    still = any(r.get("uid") == int(uid) for r in (info.get("recordings") or []))
    if still:
        return {"ok": False, "recorder": info,
                "message": (f"{target['name']} is still on the unit. The request was sent and the "
                            f"unit did not act on it.")}
    return {"ok": True, "recorder": info,
            "message": (f"Deleted {target['name']}, {target['duration']}. "
                        f"{len(info.get('recordings') or [])} recordings left on the unit.")}


# ----------------------------------------------------------------------------
# EJECTING AND ERASING THE CARD OR DRIVE. Per-volume switches on the STORAGEVOLUME nodes, found
# by walking the tree; section 7.2a of the protocol spec. The third volume is the unit's INTERNAL
# storage, which holds the pad sounds, so only a volume the desk marks as a recording destination
# may be touched, and the guard is checked against the live dump on every call.
_ERASE_WAIT_S = 90.0        # a 117 GB drive took about 20 s


def _storage_gate():
    return _cap_gate("storage_write", lambda name:
                     f"AccessCaster can show the {name}'s storage but not eject or erase it: that "
                     f"has only been confirmed on a RODECaster Pro 2.")


def _storage_target(index: int):
    """(volume, node path, blob) for a card or drive this may act on, or (None, refusal, None)."""
    blob = rcp_read.read_model_stable(fresh=True)
    vols = recorder.read_volumes(blob)
    vol = next((v for v in vols if v["index"] == int(index)), None)
    if vol is None or not vol.get("can_record"):
        return None, "That is not a card slot or drive this can change.", None
    nodes = [n for n in model_tree.walk(model_tree.parse(blob)[0]) if n.name == "STORAGEVOLUME"]
    if int(index) >= len(nodes):
        return None, "The unit does not publish that slot.", None
    return vol, nodes[int(index)].path(), blob


def _storage_reply(blob: bytes, ok: bool, message: str) -> dict:
    return {"ok": ok, "recorder": _recorder_payload(blob), "message": message}


def action_storage_eject(index: int, on: bool = True) -> dict:
    """Eject the card or drive (`on`), or reconnect one ejected from here (`on=False`).

    An ejected volume reads as empty, so it can be pulled out safely, and clearing the flag mounts
    it again with no replug. Refused while recording: the take is on that volume."""
    gate = _storage_gate()
    if gate is not None:
        return gate
    on = bool(on)
    try:
        with _hid_lock:
            vol, path, blob = _storage_target(index)
            if vol is None:
                return {"ok": False, "message": path}
            if on and recorder.read_state(blob) in (recorder.RECORDING, recorder.PAUSED):
                return {"ok": False, "message": f"The {vol['spoken']} is being recorded to. Stop "
                                                f"the recording first."}
            if on and not vol["inserted"]:
                return {"ok": False, "message": f"Nothing is in the {vol['spoken']}."}
            rcp_transfer.send_record(
                rcp_transfer.build_path_prop_bool("storageVolumeEject", on, path))
            time.sleep(1.5 if on else 3.0)
            blob = rcp_read.read_model_stable(fresh=True)
        now = next(v for v in recorder.read_volumes(blob) if v["index"] == vol["index"])
    except Exception as exc:
        return {"ok": False, "message": f"Could not change the {recorder.volume_spoken(index)}: "
                                        f"{exc}"}
    if on:
        if now["mounted"]:
            return _storage_reply(blob, False, f"The {vol['spoken']} is still in use: the unit "
                                               f"did not eject it.")
        return _storage_reply(blob, True, f"The {vol['spoken']} is ejected. You can unplug it "
                                          f"now, or reconnect it here to use it again.")
    if not now["mounted"]:
        return _storage_reply(blob, False, f"The {vol['spoken']} did not come back. If it has "
                                           f"been unplugged, plug it in again.")
    return _storage_reply(blob, True, f"The {vol['spoken']} is back, with {now['free_mb']} MB "
                                      f"free.")


def action_storage_erase(index: int) -> dict:
    """Erase (format) the card or drive. Everything on it is deleted; the page asks first.

    The desk clears `storageVolumeErase` itself when the format is done, which is the
    confirmation; the free space then reads the whole capacity."""
    gate = _storage_gate()
    if gate is not None:
        return gate
    listener = rcp_read.PushListener()
    listening = False
    try:
        with _hid_lock:
            vol, path, blob = _storage_target(index)
            if vol is None:
                return {"ok": False, "message": path}
            if recorder.read_state(blob) in (recorder.RECORDING, recorder.PAUSED):
                return {"ok": False, "message": "A recording is running. Stop it before erasing."}
            if not (vol["inserted"] and vol["mounted"]):
                return {"ok": False, "message": f"Nothing usable is in the {vol['spoken']}."}
            try:
                listener.start()
                listening = True
            except Exception:
                pass
            rcp_transfer.send_record(
                rcp_transfer.build_path_prop_bool("storageVolumeErase", True, path))
        if listening:
            listener.wait_for("storageVolumeErase", _ERASE_WAIT_S, match=lambda v: v is False)
        else:
            time.sleep(25.0)
        with _hid_lock:
            blob = rcp_read.read_model_stable(fresh=True)
    except Exception as exc:
        return {"ok": False, "message": f"Could not erase the {recorder.volume_spoken(index)}: "
                                        f"{exc}"}
    finally:
        if listening:
            listener.stop()
    now = next((v for v in recorder.read_volumes(blob) if v["index"] == vol["index"]), None)
    if not now or now["free_mb"] < now["capacity_mb"] - 64:
        return _storage_reply(blob, False, f"The erase was sent, but the {vol['spoken']} does not "
                                           f"read as empty yet.")
    return _storage_reply(blob, True, f"Erased the {vol['spoken']}. {now['free_mb']} MB free.")


# ----------------------------------------------------------------------------
# CALLME - RODE's remote guests: hosting, answering and call state.
#
# The protocol and the wording are callme.py; this is the app side. Like the recorder, the state
# comes from a PUSH heard by its own Watcher, so `/api/callme/live` costs no device read and the
# page can poll it, while `/api/callme` does a dump and re-seeds the Watcher.

_callme_watch = None
# How long an answer is given to reach Active before the reply goes out without it. Measured at
# about 1.3 s from the write.
_CALLME_ANSWER_WAIT_S = 4.0


def _callme_watcher():
    """The running Watcher, started on first use, or None if it could not be started."""
    global _callme_watch
    if _callme_watch is None:
        try:
            w = callme.Watcher(on_say=_ann_say, read_model=_callme_dump,
                               on_expiring=_callme_on_expiring)
            w.start()
            _callme_watch = w
        except Exception:
            return None
    return _callme_watch


def _callme_gate():
    return _cap_gate("callme", lambda name:
                     f"AccessCaster can show CallMe on the {name} but not answer calls or change "
                     f"hosting: that has only been confirmed on a RODECaster Pro 2.")


def _callme_dump() -> bytes:
    with _hid_lock:
        return rcp_read.read_model()


def _callme_note_dump(blob: bytes) -> None:
    """Re-seed the Watcher from a dump this app has just taken."""
    w = _callme_watcher()
    if w is not None:
        w.seed(blob)


def _callme_payload(d: dict) -> dict:
    """The page's reply: the state, plus the sentences the page shows, all worded here."""
    d = dict(d)
    w = _callme_watcher()
    for s in d.get("slots", []):
        # A page read straight off a dump has no length: the Watcher holds it, keyed by the call.
        if "since" not in s and w is not None:
            s["since"], s["since_exact"] = w.since_for(s.get("uuid"))
    d["announcing"] = _announce_on.is_set()
    d["plan"] = [{"label": k, "value": v} for k, v in callme.plan_lines(d)]
    d["now"] = callme.call_now(d)
    d["details"] = [{"label": k, "value": v} for k, v in callme.call_details(d)]
    d["end_label"] = callme.end_label(d)
    d["status"] = callme.status_sentence(d)
    d["fader_note"] = ("No fader carries CallMe 1, so a call can be answered but you will not hear "
                       "the guest. Put CallMe 1 on a fader on Routing, Alt plus 2, under Faders."
                       if d.get("on_fader") is False else "")
    d["contacts"] = _callme_contact_rows(_callme_contacts())
    d["auto_extend"] = bool(load_settings().get("callme_auto_extend"))
    return d


def _callme_contacts() -> list:
    return callme.clean_contacts(load_settings().get("callme_contacts"))


def _callme_contact_rows(contacts: list) -> list:
    return [dict(c, spoken=callme.spell(c["code"])) for c in contacts]


def _callme_store_contacts(contacts: list, message: str) -> dict:
    update_settings(callme_contacts=contacts)
    callme.set_contacts(contacts)
    return {"ok": True, "message": message, "contacts": _callme_contact_rows(contacts)}


def action_callme_contact_save(name: str, code: str, was: str = "") -> dict:
    """Add a contact, or replace the one saved under the code `was`."""
    contacts, message = callme.save_contact(_callme_contacts(), name, code, was)
    if contacts is None:
        return {"ok": False, "message": message}
    return dict(_callme_store_contacts(contacts, message), code=callme.normalise_code(code))


def action_callme_contact_delete(code: str) -> dict:
    contacts, message = callme.delete_contact(_callme_contacts(), code)
    if contacts is None:
        return {"ok": False, "message": message}
    return _callme_store_contacts(contacts, message)


def read_callme() -> dict:
    """The whole CallMe page from one dump, which also re-seeds the Watcher."""
    try:
        blob = _callme_dump()
    except Exception as exc:
        return {"ok": False, "message": f"Could not read the unit: {exc}"}
    d = callme.read(blob)
    if not d.get("ok"):
        return d
    _callme_note_dump(blob)
    return _callme_payload(d)


def read_callme_live() -> dict:
    """The cheap one, from the Watcher alone: no device read. `version` changes whenever anything
    the page shows has changed, so the page redraws only then."""
    w = _callme_watcher()
    snap = w.snapshot() if w is not None else None
    if snap is None:
        return {"ok": False, "message": "The CallMe listener has not read the unit yet."}
    return _callme_payload(snap)


def _callme_state_now():
    """(state, control path) from the Watcher once it is seeded, else from a fresh dump. A hotkey
    answer should not wait a second and a half for a dump the Watcher makes unnecessary."""
    w = _callme_watcher()
    snap = w.snapshot() if w is not None else None
    if snap is not None and w.control_path() is not None:
        return snap, w.control_path()
    blob = _callme_dump()
    _callme_note_dump(blob)
    return callme.read(blob), callme.paths(blob)[0]


def action_callme_hosting(on: bool) -> dict:
    """Switch call hosting, then wait for RODE's service to issue the code before saying so."""
    gate = _callme_gate()
    if gate is not None:
        return gate
    on = bool(on)
    try:
        d, path = _callme_state_now()
        if path is None:
            return {"ok": False, "message": "This unit does not publish CallMe."}
        if d.get("hosting_locked"):
            return {"ok": False, "message": "Hosting cannot be changed while a call is up."}
        with _hid_lock:
            rcp_transfer.send_record(callme.build_hosting(on, path))
            # The code comes from RODE's service over the network, about a second after the switch.
            deadline = time.time() + (6.0 if on else 2.0)
            while True:
                time.sleep(1.0)
                blob = rcp_read.read_model_stable(fresh=True)
                d = callme.read(blob)
                settled = d.get("hosting") == on and (not on or d.get("code"))
                if settled or time.time() > deadline:
                    break
        _callme_note_dump(blob)
    except Exception as exc:
        return {"ok": False, "message": f"Could not change hosting: {exc}"}
    out = _callme_payload(d)
    if d.get("hosting") != on:
        out.update(ok=False, message="The unit did not take that: hosting is still "
                                     + ("on." if d.get("hosting") else "off."))
    elif not on:
        out.update(ok=True, message="Hosting is off. Nobody can call in.")
    elif d.get("code"):
        out.update(ok=True, message=(f"Hosting is on. Your CallMe code is "
                                     f"{callme.spell(d['code'])}."))
    else:
        out.update(ok=True, message="Hosting is on, but RODE's service has not sent a code yet. "
                                    "Check the unit is online.")
    return out


def _callme_respond(answer: bool) -> dict:
    """Answer the ringing call into the first free slot, or decline it."""
    gate = _callme_gate()
    if gate is not None:
        return gate
    try:
        d, path = _callme_state_now()
    except Exception as exc:
        return {"ok": False, "message": f"Could not read the unit: {exc}"}
    call = d.get("incoming")
    if not call:
        return {"ok": False, "message": "No CallMe call is ringing."}
    if path is None:
        return {"ok": False, "message": "This unit does not publish CallMe."}
    slot = -1
    if answer:
        free = [s["slot"] - 1 for s in d.get("slots", []) if s.get("state") == "Idle"]
        if not free:
            return {"ok": False, "message": callme.sentence(
                f"{call['name']} is calling, but every call this plan allows is already in use.")}
        slot = free[0]
    w = _callme_watcher()
    if w is not None:
        w.note_handled(call["id"])
        if answer:
            w.quiet_connect(slot, True)
    try:
        with _hid_lock:
            rcp_transfer.send_record(callme.build_answer(call["id"], slot, path))
    except Exception as exc:
        if w is not None and answer:
            w.quiet_connect(slot, False)
        return {"ok": False, "message": f"Could not send that to the unit: {exc}"}
    if not answer:
        return {"ok": True, "message": f"Declined the call from {call['name']}."}
    # WAIT FOR THE CALL TO GO ACTIVE, which is the only honest confirmation: a write the unit
    # stores and ignores looks exactly like one that worked, and so does a call whose audio never
    # opens until it fails a minute later.
    deadline = time.time() + _CALLME_ANSWER_WAIT_S
    state = None
    while time.time() < deadline and w is not None:
        time.sleep(0.25)
        snap = w.snapshot() or {}
        state = next((s.get("state") for s in snap.get("slots", [])
                      if s.get("slot") == slot + 1), None)
        if state == "Active":
            msg = f"Connected to {call['name']}."
            if snap.get("on_fader") is False:
                msg += " No fader carries CallMe 1, so you will not hear them until one does."
            return {"ok": True, "message": msg}
    if w is not None:
        w.quiet_connect(slot, False)
    if state == "Connecting":
        return {"ok": True, "message": f"Answered {call['name']}. Still connecting."}
    return {"ok": False, "message": (f"Sent the answer for {call['name']}'s call, but the unit has "
                                     f"not started it.")}


def action_callme_hangup(slot: int = 0) -> dict:
    """End a call. `slot` is the call's number, 1 to 3, or 0 for "the one call that is up"."""
    gate = _callme_gate()
    if gate is not None:
        return gate
    w = _callme_watcher()
    snap = w.snapshot() if w is not None else None
    if snap is None:
        return {"ok": False, "message": "The CallMe listener has not read the unit yet."}
    up = [s for s in snap.get("slots", []) if s.get("state") in callme.UP]
    if slot:
        up = [s for s in up if s.get("slot") == int(slot)]
    if not up:
        return {"ok": False, "message": "No CallMe call is up."}
    if len(up) > 1:
        return {"ok": False, "message": "More than one call is up, so say which one to end."}
    target = up[0]
    i = target["slot"] - 1
    path = w.slot_path(i)
    if path is None:
        return {"ok": False, "message": "This unit does not publish that call."}
    who = target.get("peer") or "the guest"
    w.quiet_end(i, True)
    try:
        with _hid_lock:
            for rec in callme.build_hangup(path):
                rcp_transfer.send_record(rec)
                time.sleep(0.15)
    except Exception as exc:
        w.quiet_end(i, False)
        return {"ok": False, "message": f"Could not send that to the unit: {exc}"}
    deadline = time.time() + _CALLME_ANSWER_WAIT_S
    while time.time() < deadline:
        time.sleep(0.25)
        snap = w.snapshot() or {}
        state = next((s.get("state") for s in snap.get("slots", [])
                      if s.get("slot") == target["slot"]), None)
        if state == "Idle":
            return {"ok": True, "message": f"Hung up on {who}."}
    w.quiet_end(i, False)
    return {"ok": False, "message": f"Sent the hang-up, but the call with {who} is still up."}


# How long a dial is given to show it is ringing or has failed. A wrong code fails in 0.4 s.
_CALLME_DIAL_WAIT_S = 3.0


def action_callme_dial(code: str) -> dict:
    """Call another RODECaster by its code. Replies once the call is RINGING, or with why it is
    not; whether it connects comes later, from the Watcher, because the other end may take up to
    two minutes to answer."""
    gate = _callme_gate()
    if gate is not None:
        return gate
    code = callme.normalise_code(code)
    if not code:
        return {"ok": False, "message": "Type the other unit's CallMe code, letters and numbers "
                                        "only."}
    try:
        d, path = _callme_state_now()
    except Exception as exc:
        return {"ok": False, "message": f"Could not read the unit: {exc}"}
    if path is None:
        return {"ok": False, "message": "This unit does not publish CallMe."}
    if code == (d.get("code") or ""):
        return {"ok": False, "message": "That is this unit's own code."}
    if d.get("incoming"):
        return {"ok": False, "message": callme.sentence(
            f"{d['incoming']['name']} is calling you. Answer or decline that first.")}
    free = [s for s in d.get("slots", []) if s.get("state") == "Idle"]
    if not free:
        return {"ok": False, "message": "Every call this plan allows is already in use. Hang up "
                                        "first."}
    slot = free[0]
    i = slot["slot"] - 1
    who = callme.peer_name(callme.RODE_PREFIX + code)
    w = _callme_watcher()
    # A failure inside the wait is said by this reply, so the Watcher is kept from saying it too.
    w.quiet_end(i, True)
    try:
        with _hid_lock:
            for rec in callme.build_dial(code, i, path):
                rcp_transfer.send_record(rec)
                time.sleep(0.15)
    except Exception as exc:
        w.quiet_end(i, False)
        return {"ok": False, "message": f"Could not send that to the unit: {exc}"}
    ended_before = slot.get("ended", 0)
    started = time.time()
    while time.time() - started < _CALLME_DIAL_WAIT_S:
        time.sleep(0.1)
        s = next((x for x in (w.snapshot() or {}).get("slots", [])
                  if x.get("slot") == slot["slot"]), {})
        if s.get("ended", 0) > ended_before:
            msg = f"Could not call {who}: {callme.end_reason(s.get('reason'))}."
            if callme.contact_name(code) and s.get("reason", "").startswith("404"):
                msg += (f" {who}'s code changes whenever they switch hosting off and on. Ask "
                        f"them for the new one and edit the contact.")
            return {"ok": False, "message": msg}
        # An unknown code goes through Connecting before it fails, so Connecting only counts as
        # ringing once it has lasted a second.
        if s.get("state") in callme.UP and time.time() - started >= 1.0:
            w.quiet_end(i, False)
            return {"ok": True, "message": f"Calling {who}."}
    w.quiet_end(i, False)
    return {"ok": False, "message": f"Sent the call to {who}, but the unit has not started it."}


def _callme_on_expiring(_slot_index: int) -> bool:
    """The Watcher's hook when a call goes Expiring. Runs under the Watcher's lock, so the extend
    itself goes to a thread."""
    if not load_settings().get("callme_auto_extend"):
        return False
    threading.Thread(target=_callme_auto_extend, daemon=True, name="callme-auto-extend").start()
    return True


def _callme_auto_extend() -> None:
    res = action_callme_extend(auto=True)
    _ann_say(res.get("message") or "Could not extend the call.")


def action_callme_auto_extend(on: bool) -> dict:
    update_settings(callme_auto_extend=bool(on))
    return {"ok": True, "auto_extend": bool(on),
            "message": ("Calls will be extended automatically before the hour runs out." if on
                        else "Calls will no longer be extended automatically.")}


def action_callme_extend(auto: bool = False) -> dict:
    """Extend the call that is about to reach the plan's one-hour limit."""
    gate = _callme_gate()
    if gate is not None:
        return gate
    w = _callme_watcher()
    snap = w.snapshot() if w is not None else None
    if snap is None:
        return {"ok": False, "message": "The CallMe listener has not read the unit yet."}
    expiring = [s for s in snap.get("slots", []) if s.get("state") == "Expiring"]
    if not expiring:
        return {"ok": False, "message": "No call is about to end, so there is nothing to extend."}
    target = expiring[0]
    i = target["slot"] - 1
    path = w.slot_path(i)
    if path is None:
        return {"ok": False, "message": "This unit does not publish that call."}
    who = target.get("peer") or "the guest"
    w.quiet_connect(i, True)
    try:
        with _hid_lock:
            for rec in callme.build_extend(path):
                rcp_transfer.send_record(rec)
                time.sleep(0.15)
    except Exception as exc:
        w.quiet_connect(i, False)
        return {"ok": False, "message": f"Could not send that to the unit: {exc}"}
    deadline = time.time() + _CALLME_ANSWER_WAIT_S
    while time.time() < deadline:
        time.sleep(0.25)
        snap = w.snapshot() or {}
        state = next((s.get("state") for s in snap.get("slots", [])
                      if s.get("slot") == target["slot"]), None)
        if state == "Active":
            return {"ok": True, "message": (f"Extended the call with {who}"
                                            + (" automatically." if auto else "."))}
    w.quiet_connect(i, False)
    return {"ok": False, "message": (f"Sent Extend, but the call with {who} is still about to "
                                     f"end in five minutes.")}


def action_callme_end() -> dict:
    """Decline the call that is ringing, or else hang up the one that is up. A ringing call comes
    first, so a second call arriving mid-conversation is turned away rather than the guest you are
    talking to being cut off."""
    w = _callme_watcher()
    snap = (w.snapshot() if w is not None else None) or {}
    if snap.get("incoming"):
        return action_callme_decline()
    if any(s.get("state") in callme.UP for s in snap.get("slots", [])):
        return action_callme_hangup()
    return {"ok": False, "message": "No CallMe call to end."}


def action_callme_answer() -> dict:
    """Answer the ringing call; with nothing ringing, extend a call that is about to end. A ringing
    call comes first, because its two minutes run out sooner than an expiring call's five."""
    w = _callme_watcher()
    snap = (w.snapshot() if w is not None else None) or {}
    if not snap.get("incoming") and any(s.get("state") == "Expiring"
                                        for s in snap.get("slots", [])):
        return action_callme_extend()
    return _callme_respond(True)


def action_callme_decline() -> dict:
    return _callme_respond(False)


def callme_status_sentence() -> str:
    """For the status shortcut: from the Watcher, falling back to a dump."""
    try:
        d, _path = _callme_state_now()
    except Exception as exc:
        return f"Could not read CallMe: {exc}"
    if not d.get("ok", True):
        return d.get("message") or "CallMe could not be read."
    return callme.status_sentence(d)


# ----------------------------------------------------------------------------
# UNIT SETTINGS - the desk's own display, buttons and listen behaviour.
#
# These ten are readable from any dump; what makes them WRITABLE is the write SELECTOR, captured and
# read-back verified on hardware (see rcp_transfer.UNIT_SETTING_SEL for the provenance of each one,
# including the single derived-selector entry). Without them the whole group is reachable ONLY from
# the touchscreen, which is exactly the class of setting this project exists to replace.
#
# ONE SPEC DRIVES FOUR THINGS: the read, the write's value validation, the control the browser
# builds, and the profile's restore set. Adding a setting here adds it to all four, so the Device
# page's copy and what a profile actually carries cannot drift apart.
#   kind    : "bool" -> a checkbox, "choice" -> a select, "int" -> a range slider
#   detail  : what the user hears. Written for someone who cannot see the unit's screen, so it says
#             what the setting DOES rather than naming the touchscreen control it mirrors.
UNIT_SETTINGS = [
    {"prop": "outputPrefader", "label": "Listen mode", "kind": "choice",
     "choices": [[1, "Pre-fader listen"], [0, "After-fader listen"]],
     "detail": "What you hear when you solo a channel. Pre-fader listen hears it at its own level "
               "whatever its fader is doing, which is what you want for checking a mic. After-fader "
               "listen hears it as the fader has it set, so a fader down means silence."},
    {"prop": "outputMonFixed", "label": "Fixed output level on the monitor output", "kind": "bool",
     "detail": "With this on, the monitor output ignores the monitor level and sends a fixed level "
               "instead. Use it when the thing you are feeding has its own volume control and you "
               "do not want the unit's monitor level in the way."},
    {"prop": "systemRecButtonSetting", "label": "What the record button does when pressed",
     "kind": "choice", "choices": [[0, "Press to pause"], [1, "Press to stop"]],
     "detail": "Whether a press of the record button pauses the recording or stops it. Press and "
               "hold is unaffected."},
    {"prop": "systemHapticSetting", "label": "Haptic feedback", "kind": "choice",
     "choices": [[0, "Off"], [1, "On press and hold"], [2, "Always on"]],
     "detail": "Whether the unit buzzes when you touch a control. Worth knowing before you rely on "
               "it: we have confirmed the setting itself, but not whether the physical buttons buzz "
               "or only the touchscreen does."},
    {"prop": "metering", "label": "Meter style", "kind": "choice",
     "choices": [[0, "Default"], [1, "Broadcast"]],
     "detail": "How the level meters are drawn on the unit's screen. This changes nothing you can "
               "hear; it is here for a sighted co-host who reads the meters."},
    {"prop": "screenBrightness", "label": "Screen brightness", "kind": "int",
     "min": 0, "max": 255, "step": 5,
     "detail": "How bright the unit's touchscreen is, from 0 to 255."},
    {"prop": "autoBrightness", "label": "Adjust screen brightness automatically", "kind": "bool",
     "detail": "Let the unit set its own screen brightness from the light in the room. With this "
               "on, the brightness above is overridden by the unit."},
    {"prop": "screenDimAfterSeconds", "label": "Dim the screen after", "kind": "choice",
     "choices": [[0, "Never"], [30, "30 seconds"], [60, "1 minute"], [120, "2 minutes"],
                 [300, "5 minutes"], [600, "10 minutes"], [1800, "30 minutes"]],
     "detail": "How long the unit waits, with nothing touched, before dimming its screen."},
    {"prop": "activeButtonsBrightness", "label": "Button brightness when lit", "kind": "int",
     "min": 0, "max": 255, "step": 5,
     "detail": "How bright a button's light is when that button is active, from 0 to 255."},
    {"prop": "inactiveButtonsBrightness", "label": "Button brightness when not lit", "kind": "int",
     "min": 0, "max": 255, "step": 5,
     "detail": "How bright a button's light is when that button is inactive, from 0 to 255. Turn it "
               "right down to darken the unit without losing the lit buttons."},
    # ---- THE CLOCK (selectors derived and read-back proven). -------------------------------------
    # The timezone and daylight saving are NOT offered: both store a write and leave the clock
    # alone, because the touchscreen applies them. See UNIT_SETTINGS_NOT_ON_PAGE.
    {"prop": "systemDateTime24h", "label": "24-hour clock", "kind": "bool",
     "detail": "Show the unit's clock as 24-hour rather than 12-hour with am and pm."},
    {"prop": "systemDateTimeOnHome", "label": "Show the clock on the unit's home screen",
     "kind": "bool",
     "detail": "Whether the time appears on the unit's own screen. Nothing you can hear; it is here "
               "for a sighted co-host, and because turning it off is otherwise touchscreen-only."},
    # ---- THE LOCKOUTS (same session, same method). ------------------------------------------------
    # Each was set and cleared on the desk and left it byte-clean. They are last on the page on
    # purpose: they are the ones that can make a working desk feel broken, so the wording has to say
    # exactly what goes quiet and that this app can always undo it.
    {"prop": "disableAllPhysicalButtons", "label": "Lock the unit's physical buttons", "kind": "bool",
     "detail": "Stops the buttons on the unit itself doing anything, so a knock or a curious guest "
               "cannot change your mix. The faders and the touchscreen still work, and this app "
               "still controls everything, including switching this back off."},
    {"prop": "disableAllHeadphoneOutputs", "label": "Silence every headphone output", "kind": "bool",
     "detail": "Turns off all four headphone sockets at once. Your mixes, the recording and "
               "anything you are streaming carry on as normal; only the headphones go quiet. Switch "
               "it back off here to get them back."},
    {"prop": "disableAllLineoutOutputs", "label": "Silence every line output", "kind": "bool",
     "detail": "Turns off the line outputs on the back, which is what usually feeds studio "
               "speakers. The recording and your USB outputs are unaffected."},
]
UNIT_SETTING_BY_PROP = {s["prop"]: s for s in UNIT_SETTINGS}
# Every one of these is in rcp_transfer's address table, or a write here would raise at the very
# last moment instead of at import. Cheap, and it is the pairing that has to stay honest.
assert all(p in rcp_transfer.UNIT_SETTING_SEL for p in UNIT_SETTING_BY_PROP), \
    "a UNIT_SETTINGS entry has no selector in rcp_transfer.UNIT_SETTING_SEL"

# THE OTHER DIRECTION IS THE ONE THAT DRIFTS. The assert above only says the page never names a
# setting we cannot write. It says nothing about a setting we CAN write never reaching the page,
# which is what happens every time a name is added to the address table and no Device page entry
# follows. Nothing fails, because nothing is checking: the only symptom is the unit-settings suite
# comparing the two sets and finding them unequal, which reads as a broken test rather than drift.
#
# So membership now has ONE source: the address table. A writable setting either appears on the
# Device page or is named here with the reason, and `unit_settings_off_page` reports anything that
# is neither. Same shape as profile_coverage.NOT_SAVED, which is the pattern that works in this
# codebase: an omission has to be a decision somebody wrote down, not a gap nobody noticed.
UNIT_SETTINGS_NOT_ON_PAGE = {
    "outputMonMute": "Has its own control: Routing > Levels, 'Mute the monitor speakers'. A second "
                     "copy on the Device page would be two controls for one setting, which is worse "
                     "than none.",
    "outputBTMute": "Has its own control beside the monitor mute, for the same reason.",
    "outputBTLevel": "Has its own control: the Bluetooth output level, next to the two mutes. It is "
                     "a level rather than a unit preference, so it belongs with the other levels.",
    "systemDateTimezone": "DELIBERATELY NOT OFFERED. Measured display-only on this desk (three "
                          "indices, three takes, one UTC second), and at the touchscreen sitting "
                          "the unit's own Time Zone field would not open either. Writing it would "
                          "change nothing a user could see, so offering it would be a lie. Still "
                          "saved and restored by a profile, because it is real stored state.",
    "systemDateTimeDaylightSavings": "NOT OFFERED, for the same reason as the timezone: a write is "
                                     "stored and reads back, and the clock on the unit's screen "
                                     "stays where it was (watched on camera). The touchscreen "
                                     "applies it. Still saved and restored by a profile.",
}


def unit_settings_off_page() -> list:
    """Writable unit settings that are neither on the Device page nor deliberately excluded.

    Empty is the only correct answer. Asserted by DevTools/test_specs.py rather than at import, so
    a mismatch fails a test run instead of stopping the app from starting for a user."""
    return sorted(p for p in rcp_transfer.UNIT_SETTING_SEL
                  if p not in UNIT_SETTING_BY_PROP and p not in UNIT_SETTINGS_NOT_ON_PAGE)


def _unit_setting_values(blob: bytes) -> dict:
    """{prop: value} for every unit setting, read from an ALREADY-READ dump.

    Reads through rcp_read.snapshot_properties, which gets EVERY property in one pass: this wants
    forty-odd names at once, so one walk beats forty lookups.

    Bools are normalised to 0/1 so a "choice" control can compare them numerically; a property the
    dump does not carry comes back None and the UI says it could not be read rather than showing a
    zero."""
    props = rcp_read.snapshot_properties(blob)
    out = {}
    for spec in UNIT_SETTINGS:
        vals = props.get(spec["prop"]) or []
        v = vals[0] if vals else None
        if isinstance(v, bool):
            v = int(v)
        out[spec["prop"]] = v if isinstance(v, int) else None
    return out


def read_unit_settings() -> dict:
    """The Device page's "Unit settings" group: one dump, every setting, and whether this desk can
    write them.

    `writable` is the device's "unit_settings" cap. All ten selectors were captured on the Pro II
    and none has ever been seen on a Duo, so the Duo reads them and refuses to write - the same
    capture-wider-than-write rule the fader levels use, and the refusal that keeps a wrong selector
    (the stray-append hazard) off an unproven desk."""
    dev = devices.get_active_device()
    try:
        with _hid_lock:
            blob = rcp_read.read_model_stable()
    except Exception as exc:
        return {"ok": False, "message": f"Could not read the unit's settings: {exc}"}
    values = _unit_setting_values(blob)
    settings = [dict(s, value=values.get(s["prop"])) for s in UNIT_SETTINGS]
    # MIDI LIVES ON THIS TAB, not on one of its own for a single switch, so its value is reported
    # here and the tab still makes exactly ONE call. It is NOT a UNIT_SETTINGS
    # entry and deliberately so: that spec drives the profile's restore set as well as the page, and
    # MIDI already has its own captured write (rcp_transfer.set_midi_control, byte-verified against
    # midi_on/midi_off) and its own key in a profile. Adding it there would write it twice on every
    # restore. Read off the dump we already have, by exact name for the reason in
    # _unit_setting_values.
    midi = (rcp_read.snapshot_properties(blob).get("systemMidiControl") or [None])[0]
    return {"ok": True, "settings": settings, "device_name": dev.name,
            "midi": bool(midi) if midi is not None else None,
            "writable": "unit_settings" in dev.caps}


def action_set_unit_setting(prop: str, value: int) -> dict:
    """Write one unit setting, validating the value against its own spec first.

    The validation is the safety here, not ceremony: these are ordinary global property writes, and
    the thing that makes them safe is that both the selector AND the property name are confirmed
    pairings. So an unknown property is refused outright, a choice must be one of its listed values,
    and a slider is clamped to its range."""
    spec = UNIT_SETTING_BY_PROP.get(prop)
    if spec is None:
        return {"ok": False, "message": f"Unknown unit setting: {prop}."}
    dev = devices.get_active_device()
    if "unit_settings" not in dev.caps:
        return {"ok": False,
                "message": (f"These settings can be read but not changed on the {dev.name}: "
                            f"changing them has only ever been confirmed on a RODECaster Pro 2, "
                            f"so AccessCaster leaves them alone rather than risk changing the "
                            f"wrong one.")}
    try:
        v = int(value)
    except (TypeError, ValueError):
        return {"ok": False, "message": f"{spec['label']} needs a whole number."}
    if spec["kind"] == "choice":
        allowed = {int(c[0]) for c in spec["choices"]}
        if v not in allowed:
            return {"ok": False, "message": f"{v} is not one of the choices for {spec['label']}."}
        said = next(c[1] for c in spec["choices"] if int(c[0]) == v)
    elif spec["kind"] == "bool":
        v = 1 if v else 0
        said = "on" if v else "off"
    else:
        v = max(int(spec["min"]), min(int(spec["max"]), v))
        said = str(v)
    # WRITING outputMonFixed CLEARS outputMonMute, so switching to fixed level OPENS THE MONITOR
    # SPEAKERS - with a live mic in the room, which is feedback (measured on a Pro II).
    # Writing outputMonFixed back does not re-mute, so the mute is read first
    # and PUT BACK after, and the user is told. Verified rather than assumed: the mute is only
    # rewritten if the desk actually moved it, so if a firmware stops doing this the code goes quiet
    # on its own instead of writing a value nobody asked for.
    guard_mute = prop == "outputMonFixed"
    extra = ""
    try:
        with _hid_lock:
            mute_before = (rcp_read.values(rcp_read.read_model_stable(fresh=True),
                                           "outputMonMute") or [None])[0] if guard_mute else None
            rcp_transfer.set_unit_setting(prop, v)
            if mute_before is not None:
                mute_after = (rcp_read.values(rcp_read.read_model_stable(fresh=True),
                                              "outputMonMute") or [None])[0]
                if mute_after is not None and bool(mute_after) != bool(mute_before):
                    rcp_transfer.set_unit_setting("outputMonMute", bool(mute_before))
                    extra = (" The unit un-muted the monitor speakers when it did that, so the mute "
                             "has been put back on."
                             if mute_before else
                             " The unit muted the monitor speakers when it did that, so the mute has "
                             "been taken back off.")
    except Exception as exc:
        return {"ok": False, "message": f"Could not set {spec['label']}: {exc}"}
    return {"ok": True, "value": v, "message": f"{spec['label']}: {said}.{extra}"}


def _overview_routing(blob: bytes) -> dict:
    """Routing reported BY EXCEPTION: only what differs from the usual, never the full grid.

    Reading eleven outputs against ten sources aloud is 110 facts to get through before reaching
    the one that matters, which defeats the point of a troubleshooting summary. So we report an
    output only when its MODE differs from the default, or when it EXCLUDES a source.

    Two things shape the wording. First, the defaults in devices.py are a known-good set rather
    than a factory capture (see the provenance comment there), so this says "usually" and never
    "wrong". Second:50 - a Mix Minus output carries no exclusion flags at all,
    because the self-exclusion is implicit in the mode, so mode is reported before exclusions."""
    dev = devices.get_active_device()
    state = rcp_read.read_matrix_state(blob)
    if not state.get("ok"):
        return {"ok": False, "reason": state.get("reason") or "The routing could not be read.",
                "notes": []}
    mode_names = {0: "Main Mix", 1: "Mix Minus", 2: "Custom"}
    # NAMED FROM EVERY SOURCE, COUNTED FROM THE LIVE ONES. The desk keeps a cross-point for Game,
    # Music, A and B in both modes, but in Standard nothing can feed them, so a note that counts them
    # says "USB 2 carries only Game, Music, A and B" about an output that carries nothing at all.
    src_label = {s["index"]: s["label"] for s in (dev.matrix.all_sources or ())}
    live = {s["index"] for s in (dev.matrix.sources_for(channels.usb_expanded()) or ())}
    notes = []
    for out in (dev.matrix.outputs or ()):
        o = out["index"]
        mode = state["modes"].get(o)
        if mode is None:
            continue
        default = out.get("default_mode")
        if default is not None and mode != default:
            notes.append(f"{out['label']} is set to {mode_names.get(mode, mode)}, "
                         f"usually {mode_names.get(default, default)}.")
        cells = sorted((s, off) for s, off in (state["excluded"].get(o) or {}).items()
                       if s in live)
        excluded = [src_label.get(s, f"source {s}") for s, off in cells if off]
        included = [src_label.get(s, f"source {s}") for s, off in cells if not off]
        # Say whichever list is shorter. A typical Chat preset excludes nine of ten sources, and
        # "Chat does not include Mic 2, Mic 3, Mic 4, USB 1, Chat, USB 2, Bluetooth and SMART pads"
        # is a mouthful that hides the one fact that matters: Chat carries Mic 1 only.
        if excluded and len(included) < len(excluded):
            notes.append(f"{out['label']} carries only {_join_and(included)}."
                         if included else f"{out['label']} carries nothing at all.")
        elif excluded:
            notes.append(f"{out['label']} does not include {_join_and(excluded)}.")
    return {"ok": True, "reason": "", "notes": notes}


def _join_and(items) -> str:
    """'a', 'a and b', 'a, b and c'. Spoken lists read badly with a trailing comma."""
    items = list(items)
    if len(items) <= 1:
        return items[0] if items else ""
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _pads_engaged(blob: bytes) -> list:
    """Pads the desk currently has ENGAGED, named, from a dump already read. [] when none are.

    A VOICE-FX pad is the one worth catching: it processes a live channel and is otherwise silent,
    so the only symptom is sounding wrong. Never raises - this decorates the overview, and an
    unreadable pad table is not worth failing the whole page for."""
    try:
        return [{"grid": pi + 1, "name": nm, "kind": "voice effect" if pi is not None else "pad"}
                for _pos, pi, nm in soundpad.latched_pads(blob)]
    except Exception:
        return []


def _storage_low(blob: bytes) -> list:
    """Volumes with less than a fifth of their space left, named and quantified, from a dump we have
    already read. [] when everything has room, which is what lets the overview stay by-exception.

    The parsing and the "nearly full" judgement both live in `recorder.read_volumes`, so this
    warning, the Hardware tab and the Recording page's destination cannot disagree about which
    volume is which or when one is short of room. Never raises: an unreadable volume is reported as
    nothing rather than taking the whole overview down with it."""
    try:
        return [{"label": v["label"], "free_pct": v["free_pct"], "free_mb": v["free_mb"]}
                for v in recorder.read_volumes(blob) if v["low"]]
    except Exception:
        return []


def _overview_recording(blob: bytes):
    """A word for the overview when the unit is recording or paused, else None.

    Deliberately thin: the overview reports EXCEPTIONS, and a unit sitting ready to record is not
    one. Never raises, for the same reason as the rest of this page's helpers."""
    try:
        state = recorder.read_state(blob)
        if state in (recorder.RECORDING, recorder.PAUSED):
            return {"state": state, "label": recorder.STATE_LABEL[state]}
    except Exception:
        pass
    return None


def device_overview() -> dict:
    """The Device page's opening summary: what is muted, what is soloed, where every fader sits,
    anything unusual about the routing, and the current bank.

    Why it exists: these facts otherwise live on four different pages, and mute and solo have no
    read path at all outside the announcer, so a desk left with two channels muted gives the app no
    way to find out which. This is the one place to look when something sounds wrong.

    ONE model dump feeds everything here. Parsing each section from its own read would cost four
    round trips of about half a second each on the page the app opens on.

    Fader positions come from the ANCHOR half of `mixLevelWithAnchor`, not from `faderLevel`:
    faderLevel is NOT the physical position (it does not move while fader 1 travels top to halfway)
    and only the virtual slots carry a value there. The
    anchor is the channel fader's own position and is present on every cross-point in that
    source's row, physical faders included, so we read it from the row's first cell."""
    dev = devices.get_active_device()
    try:
        with _hid_lock:
            blob = rcp_read.read_model_stable()
    except Exception as exc:
        return {"ok": False, "device_name": dev.name,
                "message": f"Could not read the unit: {exc}"}

    spec = dev.fader
    vec = rcp_read.values(blob, "channelInputSource")
    channels.publish_strip_vector(vec)
    mutes = rcp_read.values(blob, "channelOutputMute")
    cues = rcp_read.values(blob, "channelCueEnable")

    # The anchor lookup, guarded on the matrix keeping its expected shape - the same guard
    # rcp_read uses, because a connected SIP caller shifts the grid and every index would be wrong.
    cells = rcp_read.block_values(blob, dev.matrix.block, "mixLevelWithAnchor")
    shape_ok = len(cells) == dev.matrix.cell_count

    def anchor_pct(source_code):
        if not shape_ok or source_code is None or source_code < 0:
            return None
        cell = cells[dev.matrix.row_stride * source_code] \
            if dev.matrix.row_stride * source_code < len(cells) else None
        if not isinstance(cell, str) or "|" not in cell:
            return None
        try:
            return max(0, min(100, int(round(float(cell.split("|", 1)[1]) * 100))))
        except ValueError:
            return None

    def label(i):
        code = vec[i] if i < len(vec) else None
        # labels_for: a fader holding a linked pair must be named, not fall through to "Fader N",
        # and USB 1 is named after what the computer actually sends in the mode the desk is in.
        return spec.labels_for(channels.usb_expanded()).get(code) or f"Fader {i + 1}"

    faders, muted, soloed = [], [], []
    for i in range(spec.count):
        code = vec[i] if i < len(vec) else None
        empty = code is None or code < 0
        is_muted = bool(mutes[i]) if (i < len(mutes) and not empty) else False
        faders.append({"fader": i + 1,
                       "label": "empty" if empty else label(i),
                       "percent": None if empty else anchor_pct(code),
                       "muted": is_muted,
                       "virtual": i >= spec.physical})
        if is_muted:
            muted.append({"fader": i + 1, "label": label(i)})
        if not empty and i < len(cues) and cues[i]:
            soloed.append({"fader": i + 1, "label": label(i)})

    mon = [v for v in rcp_read.values(blob, "outputMonLevel") if isinstance(v, float)]
    bank = [v for v in rcp_read.values(blob, "selectedBank") if isinstance(v, int)]
    return {"ok": True,
            "device_name": dev.name,
            "muted": muted,
            "soloed": soloed,
            # STORAGE, BY EXCEPTION like everything else here: this reports a volume that is nearly
            # full and nothing at all when there is room. It belongs on this
            # page rather than only on Hardware because "the card is nearly full" is the fact you
            # need BEFORE you press record, not after, and a blind operator has no bar to glance at.
            # No card at all is deliberately NOT an exception: this desk usually has no card in, and
            # a warning that is always there is a warning nobody hears.
            "storage_low": _storage_low(blob),
            # THE RECORDER, by exception like everything else here: reported only when it is
            # actually recording or paused. "No destination" is deliberately NOT an exception -
            # this desk usually has no drive in it, and a warning that is always there is one
            # nobody hears (the same rule the missing memory card follows). A recording running
            # when you thought it was not is the fact worth interrupting for.
            "recording": _overview_recording(blob),
            # PADS LEFT SWITCHED ON, which is the single best example of what this overview is
            # FOR: a latched voice-FX pad sits on a live mic, makes no sound of its own, changes
            # nothing about the pad's
            # name, type or colour, and simply makes you sound wrong with nothing on screen to say
            # why. Reported by exception like everything else here, so it costs nothing when the
            # desk is fine.
            "pads_engaged": _pads_engaged(blob),
            # By exception, like the rest: empty unless the desk says an update is waiting.
            "firmware_update": _firmware_update(blob),
            "faders": faders,
            "monitor_percent": max(0, min(100, int(round(mon[0] * 100)))) if mon else None,
            "bank": (bank[0] + 1) if bank else None,       # the desk numbers banks from 0
            "levels_ok": shape_ok,
            "routing": _overview_routing(blob),
            "message": ""}


# The USB SERIAL of the desk we are talking to, published from every status read. It TAGS
# per-device preferences (which preset a strip is on, whether the as-found baseline has been
# offered) so that plugging in a second unit does not inherit the first one's claims. Kept as
# (device name, serial) so a desk swap mid-session replaces it rather than keeping a stale
# pairing. Nothing here opens the device: see _device_hardware_id for why that matters.
_device_id_cache = ("", "")


def _note_device_id(device_name, serial) -> None:
    global _device_id_cache
    if serial:
        _device_id_cache = (str(device_name or ""), str(serial))


def read_device_status() -> dict:
    """Open the control interface, run the one safe status command, return a status dict.

    Read-only. Never changes device state. See the comment at the command for why it is only `I`.
    """
    with _hid_lock:
        interfaces = [i.describe() for i in rcp_hid.enumerate_interfaces()]
        iface = rcp_hid.find_control_interface()
        # WHICH KIND OF NOTHING IS IT? The finder returns MI_09 or None and never guesses (see its
        # docstring), so telling "no desk at all" from "a desk on the wrong port" is done here, where
        # the answer is a sentence for a person rather than a value for a caller.
        #
        # The desk has TWO USB ports and this protocol works over exactly one interface, MI_09, which
        # is on USB 1, the port nearest the power inlet. USB 2 presents its own device (PID 0x0026,
        # MI_03) that carries the audio and answers no read: every report-1 command comes back
        # empty. So a cable in USB 2 alone looks like a connected unit to anyone watching the
        # lights while the app can do nothing with it, and that state invites a whole causal theory
        # about the protocol when the real answer is which cable is plugged in.
        if iface is None:
            others = [i for i in rcp_hid.enumerate_interfaces() if i.is_control]
            if others:
                device_name = devices.resolve_device(others[0].product_id).name
                return {
                    "connected": False,
                    "app_version": APP_VERSION,
                    "device_name": device_name,
                    "control_interface": others[0].describe(),
                    "interfaces": interfaces,
                    "headline": f"{device_name} half connected",
                    "message": (f"The {device_name} is half connected: its audio is here but the "
                                f"connection AccessCaster controls it over is not. That is almost "
                                f"always the USB cable in the port nearest the power socket, which "
                                f"is the one this app needs. Plug that one back in and everything "
                                f"comes back straight away."),
                }
            return {
                "connected": False,
                "app_version": APP_VERSION,
                "device_name": None,
                # NAMING THE PORT IS THE POINT OF THIS MESSAGE. It is what someone setting up for
                # the first time reads, and a cable in the wrong port produces exactly this and
                # nothing else to go on.
                "headline": "No RODECaster found",
                "message": "No RODECaster found. Check it is powered on, and that the USB cable "
                           "goes into the socket nearest the power inlet. That is the only one "
                           "this app can control the unit over; the other USB socket carries "
                           "audio only.",
                "interfaces": interfaces,
            }
        # GAP-10: name the actual connected desk (Pro II or Duo), not a hardcoded model, so the
        # status line and the UI chrome (title/brand/About, updated in app.js) reflect reality.
        device_name = devices.resolve_device(iface.product_id).name
        try:
            # ONE COMMAND, `I`, AND NEVER `VER` OR THE `U` PING. With the desk in Expanded, either
            # of those two makes it drop RODE's virtual `System (RODECaster)` playback endpoint
            # about 17 seconds later; Windows surprise-removes and re-adds it, and a screen reader
            # on that output loses its speech for a few seconds. Measured one command at a time:
            # `I` and a model dump are clean. `I` answering is proof enough that the desk is
            # responding, and the identity below comes from the USB serial, so nothing is lost.
            with rcp_hid.RodeCaster.open_path(iface, "device status") as dev:
                info = dev.get_info()
            online = bool(info)
            # THE UNIT'S IDENTITY IS ITS USB SERIAL. It survives factory resets and power cycles
            # unchanged, and comes free with the enumeration without opening anything. `VER` was
            # never a substitute: it changes across a reboot, so anything filed under it is orphaned.
            _note_device_id(device_name, iface.serial_number)
            return {
                "connected": True,
                "app_version": APP_VERSION,
                "device_name": device_name,
                "online": online,
                "hardware_id": iface.serial_number or "",
                "info_hex": info[:16].hex(),
                "control_interface": iface.describe(),
                "interfaces": interfaces,
                "headline": f"{device_name} connected",
                "message": f"Connected to {device_name} (serial "
                           f"{iface.serial_number or 'unknown'}).",
            }
        except Exception as exc:  # surfaced to the UI
            return {
                "connected": False,
                "app_version": APP_VERSION,
                "device_name": device_name,
                "headline": f"{device_name} found but not answering",
                "message": f"Found the control interface but could not talk to "
                           f"it: {exc}",
                "interfaces": interfaces,
            }


def read_network_status() -> dict:
    """Live-read wifi, Bluetooth and firmware from the device model. Read-only.

    These all live in the property model (rcp_read), so one model dump answers
    everything. Never changes device state.
    """
    try:
        with _hid_lock:
            blob = rcp_read.read_model()
    except Exception as exc:
        return {"ok": False,
                "message": f"Could not read network info from the device: {exc}"}

    def first(name):
        return rcp_read.first(blob, name)

    def net(name):
        """A NETWORK-block value, by block rather than by "first in the stream".

        `first()` cannot be used for a name that might occur in more than one block: False is a
        meaningful value, so it has no way to tell a real answer from the wrong block's.

        THE CASE THAT TAUGHT US THIS WAS A STRAY, NOT A SCHEMA. A second `btVisible` sat in SYSTEM
        reading False, so `first()` reported "not discoverable" while the radio was discoverable
        with a phone connected. It accepted writes, kept the value, changed nothing on the unit and
        never tracked the radio - which is the signature of a stray, and it vanished when the desk
        was factory reset. An earlier guess that it belonged to a sibling RODE product sharing this
        schema was wrong.

        READING BY BLOCK IS STILL RIGHT, and that is why this stays: a stray can be appended to any
        block by one malformed write, so a name is only unambiguous when the block is named too.
        """
        return rcp_read.block_value(blob, "NETWORK", name)

    def slots(prefix, count, start=1):
        """The non-blank entries of a numbered slot array (btScan1..10, wifiScan1..10)."""
        out = []
        for n in range(start, start + count):
            v = first(f"{prefix}{n}")
            if v:
                out.append(str(v))
        return out

    ssid = first("wifiSSID")
    ip = first("wifiIpAddress")
    gateway = first("wifiGateway")
    dhcp = first("wifiDHCP")
    incorrect = first("wifiIncorrectPSK")
    bt_addr = first("btConnectedAddress")
    auto = first("autoReconnect")
    midi = first("systemMidiControl")
    bt_visible = net("btVisible")
    # The 5 paired-device slots (btPairedNumber1..5); keep only non-blank ones.
    bt_paired = []
    for n in range(1, 6):
        v = first(f"btPairedNumber{n}")
        if v:
            bt_paired.append(v)
    # systemFirmwareVersion sits near the tail of the dump (whose length varies),
    # so prefer the value the startup/mixer read already captured into session state.
    with _session_state_lock:
        fw = _session_state.get("firmware")
    if not fw:
        fw = first("systemFirmwareVersion")
    wired = first("wiredConnected")
    return {
        "ok": True,
        "firmware": fw,
        "midi": bool(midi) if midi is not None else None,
        "wifi": {
            # The RADIO, which is not the same fact as "connected": it can be on and not yet
            # joined to anything. Read BY BLOCK, like everything else here that has a duplicate.
            "on": bool(net("wifi")),
            "ssid": ssid or "",
            "ip": ip or "",
            "gateway": gateway or "",
            "dhcp": bool(dhcp) if dhcp is not None else None,
            "connected": bool(ip),               # has an IP => joined a network
            "incorrect_psk": bool(incorrect) if incorrect is not None else None,
            # Whatever a scan last found. WE CAN NOW START ONE (action_scan_wifi, proven 29th
            # August 2026), so this is the result of the last scan by either us or the touchscreen.
            # An empty list still means "nothing has been scanned for yet", not "nothing in range".
            "scan": slots("wifiScan", 10) or slots("wifiScanResultSSID", 15),
            "can_scan": "network_write" in devices.get_active_device().caps,
            # Never returned, and this is deliberate rather than an oversight: wifiPSK IS readable
            # off this desk, and putting a password into a JSON payload, a browser page and a log
            # line is not a thing to do casually. Whether the network HAS a password is enough.
            "has_password": bool(first("wifiPSK")),
        },
        "bluetooth": {
            "connected_address": bt_addr or "",
            "connected": bool(bt_addr),
            "auto_reconnect": bool(auto) if auto is not None else None,
            "visible": bool(bt_visible) if bt_visible is not None else None,
            "paired": bt_paired,
            "scan": slots("btScan", 10),
        },
        # The third radio on the Wireless connections tab. Same helper as the Hardware state read,
        # so the two readings cannot drift apart.
        "wireless_mics": _wireless_mics(blob),
        # Still returned, though the page now shows both of these on the Hardware tab: they are read
        # from the same dump for free, and callers other than that tab may want them.
        "wired_connected": bool(wired) if wired is not None else None,
        # WHAT CAN BE CHANGED HERE. Three things on this block are proven and writable; everything
        # else on this page is read-only, and the page says WHICH is which rather than leaving a
        # user to find out by pressing things.
        "writable": "network_write" in devices.get_active_device().caps,
        # PARKED, NOT WRITTEN OFF. The page says what is missing and what to do instead.
        # THE TWO REMAINING CASES ARE DIFFERENT AND MUST NOT BE BLURRED. Bluetooth scanning is
        # TESTED and does nothing: `btDoScan` written on both blocks that carry it, with a phone in
        # pairing mode, never returns a result. Pairing and a fixed IP are simply NOT BUILT YET,
        # which is not the same claim at all - and telling someone a thing is impossible when it is
        # merely unfinished is the mistake this project has made seven times over (see PARITY.md).
        "still_read_only": (
            "Scanning for Bluetooth devices does not work: it was tried and the unit reports "
            "nothing back. Make the unit discoverable above and pair from the phone instead. "
            "Bluetooth pairing from here, and a fixed IP address, are still being worked on."),
        "message": "Read network and firmware from the device.",
    }


# ----------------------------------------------------------------------------
# NETWORK WRITES. Joining wifi and making the unit discoverable, with no touchscreen.
#
# NETWORK IS SELECTOR 0x10, proven on hardware: `btVisible` written there reads back in the NETWORK
# block (not the SYSTEM duplicate), leaves no stray, and a phone then finds the unit, pairs, and
# plays audio through it. `wifiSSID` and `wifiPSK` write as strings, and with the radio on the unit
# holds a DHCP address 15 seconds later - confirmed by PINGING it, not by the unit reporting success.
# "The RODE app's own Network screen is read-only, so there is no capture to learn from" says
# nothing whatsoever about the bus. Never file a block unreachable on that reasoning.
#
# WHAT IS DELIBERATELY NOT OFFERED, and it is not caution:
#   * **Bluetooth scanning.** `btDoScan` is addressable and written on both blocks that carry it,
#     with a phone in pairing mode: it latches True and never returns a result. Beware the SYSTEM
#     occurrence, which is a STRAY, so any "the write lands but nothing happens" reading taken there
#     is a write into a stray; the NETWORK one is genuinely dead. Making the unit discoverable and
#     pairing FROM the phone is the route that works, and it is offered.
#     WIFI scanning is NOT on this list: see action_scan_wifi. "No push back means not consumed" is
#     a bad heuristic, not a measurement, and it is what kept wifi scanning here.
#   * **Pairing, connecting and unpairing.** `btDoPair` / `btDoConnect` / `btDoDisconnect` /
#     `btDoUnPair` are addressable, but they are ACTIONS taking an address we have never seen the
#     format of, and nothing is paired here to test against. Making the unit DISCOVERABLE and
#     pairing from the phone is the route that is proven and needs no address at all.
#   * **Static IP and DNS.** Derived but untried, and a bad write drops the unit off the network,
#     which is exactly the state that then needs the touchscreen to fix.

# The password is the reason this endpoint is not like the others. wifiPSK is READABLE off the unit,
# and this app serves its pages on 0.0.0.0 so a phone on the LAN can use it - which means a password
# typed into that page would cross the very network it is for, in clear. So the join is refused
# unless the request came from this machine.
LOCAL_ONLY_ROUTES = {"/api/network/wifi/join", "/api/access", "/api/access/new"}
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}

# ---------------------------------------------------------------------------
# REMOTE ACCESS: a token, because this server binds 0.0.0.0 and had no authentication at all.
#
# THE HAZARD IT CLOSES. The page is meant to be reachable from a phone in another room, which is a
# real feature and stays. But until now anyone who could reach port 8770 had FULL control of the
# desk - mutes, routing, Clear all pads - with nothing asked of them. Fine on a home LAN or a
# tailnet, not at a venue, a shared studio or a university network. The app is for everyone, not
# just for the machine it happens to be installed on.
#
# THE SHAPE, and each part is a deliberate trade:
#   - REQUESTS FROM THIS MACHINE ARE ALWAYS ALLOWED. Anything already running here can drive the
#     desk anyway, so a token would protect nothing and would break every DevTools script and the
#     tray's own browser launch. It is the same trust boundary the app itself has.
#   - EVERYTHING ELSE NEEDS THE TOKEN, once. It arrives as `?t=...` on the page URL, and a
#     successful page load sets a cookie, so the API calls that follow carry it without every
#     fetch in app.js having to know. Reloading, deep-linking and leaving the tab open all work.
#   - IT IS A SHARED SECRET, NOT A LOGIN. There is no user to name and nobody to lock out; the
#     threat is "a stranger on this network", and a link you can send to your own phone answers it
#     without a password to type on a phone keyboard with a screen reader.
#   - HTTP, so the token is visible to anyone who can already read your LAN traffic. Said plainly
#     in the UI rather than implied: this raises the bar from "anyone" to "anyone with the link",
#     which is the honest claim.
_ACCESS_TOKEN_KEY = "remote_token"
_token_lock = threading.Lock()


def access_token() -> str:
    """The remote-access token, made on first use and kept in settings.json."""
    with _token_lock:
        tok = (load_settings().get(_ACCESS_TOKEN_KEY) or "").strip()
        if not tok:
            tok = secrets.token_urlsafe(18)
            update_settings(**{_ACCESS_TOKEN_KEY: tok})
        return tok


def new_access_token() -> str:
    """Replace the token, so a link that has been shared stops working."""
    with _token_lock:
        tok = secrets.token_urlsafe(18)
        update_settings(**{_ACCESS_TOKEN_KEY: tok})
        return tok


def _lan_address() -> str:
    """This machine's address on the LAN, for the link we show. No traffic is sent: connecting a
    UDP socket only picks the route, which is enough for the OS to tell us which address it would
    use. Falls back to the hostname if there is no route at all."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))     # TEST-NET-1: routable-looking, never actually reached
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "this-computer"


def read_access() -> dict:
    """What the Device page shows: the link to open on another device, and the plain warnings."""
    return {
        "url": f"http://{_lan_address()}:{HTTP_PORT}/?t={access_token()}",
        "port": HTTP_PORT,
        "note": ("Open this link on a phone or another computer on the same network to control the "
                 "unit from there. Anyone with the link has full control, so treat it like a "
                 "password. This machine itself never needs it."),
        "warning": ("The link travels unencrypted, so it protects you from a stranger guessing the "
                    "address, not from someone who can already read your network's traffic."),
    }


def action_new_access_token() -> dict:
    new_access_token()
    return {"ok": True,
            "message": "A new link has been made. Any link you shared before will no longer work.",
            "access": read_access()}


def _wifi_state(blob) -> dict:
    """Just enough of the wifi state to report a join honestly, read BY BLOCK.

    Never returns the password. `wifiPSK` is readable, and "there is a password stored" is the most
    that should ever leave this process - not in a JSON payload, not in the page, not in a log."""
    def net(name):
        return rcp_read.block_value(blob, "NETWORK", name)
    return {"on": bool(net("wifi")), "ssid": net("wifiSSID") or "",
            "ip": net("wifiIpAddress") or "", "has_password": bool(net("wifiPSK")),
            "wrong_password": bool(net("wifiIncorrectPSK"))}


def action_set_wifi(on: bool) -> dict:
    """Turn the wifi radio on or off. `wifi` is an ordinary bool on NETWORK 0x10."""
    gate = _network_write_gate()
    if gate is not None:
        return gate
    try:
        with _hid_lock:
            rcp_transfer.send_record(
                rcp_transfer.build_global_prop_bool("wifi", bool(on), rcp_transfer.SEL_NETWORK))
            time.sleep(1.0)
            state = _wifi_state(rcp_read.read_model_stable(fresh=True))
    except Exception as exc:
        return {"ok": False, "message": f"Could not change the wifi radio: {exc}"}
    if bool(on) != state["on"]:
        return {"ok": False, "wifi": state,
                "message": "The unit did not take that: the wifi radio is still "
                           + ("on" if state["on"] else "off") + "."}
    if not on:
        return {"ok": True, "wifi": state, "message": "Wifi is off."}
    return {"ok": True, "wifi": state,
            "message": ("Wifi is on." if not state["ssid"]
                        else f"Wifi is on, set to {state['ssid']}." +
                             (f" It has the address {state['ip']}." if state["ip"] else
                              " It has not been given an address yet; check again in a moment."))}


def action_join_wifi(ssid: str, password: str) -> dict:
    """Join a wifi network: SSID, then password, then the radio on, then wait for an address.

    THE ORDER IS LOAD-BEARING. Bringing the radio up before it knows where to go invites an
    association failure that then has to be told apart from a wrong password.

    THE PASSWORD IS HANDLED AS A SECRET THROUGHOUT: it is used once, never stored by us, never
    logged, never echoed back, and never put in a profile. The unit keeps it (it is readable there,
    which is exactly why this code refuses to repeat it).

    The answer is the unit's own IP address or `wifiIncorrectPSK`, not "the write was sent". A blind
    operator has no other way to find out whether it worked."""
    gate = _network_write_gate()
    if gate is not None:
        return gate
    ssid = (ssid or "").strip()
    if not ssid:
        return {"ok": False, "message": "Give the name of the network to join."}
    if not ssid.isascii():
        return {"ok": False,
                "message": "The unit only takes plain ASCII in a network name, so this one cannot "
                           "be sent from here."}
    if password and not password.isascii():
        return {"ok": False,
                "message": "The unit only takes plain ASCII in a password, so this one cannot be "
                           "sent from here."}
    try:
        with _hid_lock:
            sel = rcp_transfer.SEL_NETWORK
            rcp_transfer.send_record(rcp_transfer.build_global_prop_string("wifiSSID", ssid, sel))
            time.sleep(0.4)
            if password:
                rcp_transfer.send_record(
                    rcp_transfer.build_global_prop_string("wifiPSK", password, sel))
                time.sleep(0.4)
            rcp_transfer.send_record(rcp_transfer.build_global_prop_bool("wifi", True, sel))
    except Exception as exc:
        return {"ok": False, "message": f"Could not send the network details: {exc}"}
    finally:
        del password           # not kept a moment longer than the write needs it

    # Wait for an ADDRESS, which is the only honest evidence of a join. Polls rather than sleeping
    # the whole timeout so a quick join answers quickly.
    deadline = time.time() + WIFI_JOIN_TIMEOUT_S
    state = {}
    while time.time() < deadline:
        time.sleep(2.5)
        try:
            with _hid_lock:
                state = _wifi_state(rcp_read.read_model_stable(fresh=True))
        except Exception:
            continue
        if state.get("wrong_password") or state.get("ip"):
            break
    if state.get("wrong_password"):
        return {"ok": False, "wifi": state,
                "message": f"{ssid} refused the password. The name and password are still stored "
                           f"on the unit, so try again with the password corrected."}
    if state.get("ip"):
        return {"ok": True, "wifi": state,
                "message": f"Joined {ssid}. The unit's address is {state['ip']}."}
    return {"ok": False, "wifi": state,
            "message": f"The unit took the details for {ssid} but has no address yet. It did not "
                       f"say the password was wrong, so it may still be connecting, or the network "
                       f"may be out of range. Check again in a moment."}


# How long to wait for an address before answering. The measured join took about 15 seconds, so
# this is roughly three times it: long enough not to report a failure that is really a slow DHCP
# server, short enough that a page is not left hanging with nothing to say.
WIFI_JOIN_TIMEOUT_S = 45.0


def action_set_bt_visible(on: bool) -> dict:
    """Make the unit discoverable, or stop.

    THE DUPLICATE MATTERS HERE. `btVisible` exists in SYSTEM as well as NETWORK, and only the
    NETWORK copy drives the radio - writing the SYSTEM one flips a stored value and leaves the unit
    invisible, with nothing in the read-back to say so. So the write goes to 0x10 and the read-back
    is taken BY BLOCK, never by "first occurrence in the stream"."""
    gate = _network_write_gate()
    if gate is not None:
        return gate
    try:
        with _hid_lock:
            rcp_transfer.send_record(
                rcp_transfer.build_global_prop_bool("btVisible", bool(on),
                                                   rcp_transfer.SEL_NETWORK))
            time.sleep(1.0)
            blob = rcp_read.read_model_stable(fresh=True)
        now = bool(rcp_read.block_value(blob, "NETWORK", "btVisible"))
    except Exception as exc:
        return {"ok": False, "message": f"Could not change Bluetooth discoverability: {exc}"}
    if now != bool(on):
        return {"ok": False, "visible": now,
                "message": "The unit did not take that: it is still "
                           + ("discoverable" if now else "not discoverable") + "."}
    if on:
        return {"ok": True, "visible": True,
                "message": "The unit is now discoverable. On your phone or computer, look for "
                           "RODECaster in its Bluetooth list and pair from there. It stops being "
                           "discoverable on its own after a while, so do it now."}
    return {"ok": True, "visible": False,
            "message": "The unit is no longer discoverable. Anything already paired still works."}


# ----------------------------------------------------------------------------
# SCANNING FOR WIFI NETWORKS, proven on hardware.
#
# `wifiScan` written on the NETWORK selector pushes NOTHING back. What it does is fill in
# `wifiScan1` to `wifiScan10` SILENTLY, so the results appear only in a later model dump. Format of
# each slot: `<SSID>----<security>`.
#
# PUSH-MEANS-CONSUMED IS A BAD HEURISTIC, and this is where it costs most: `requestRecordState` and
# `requestDropMarker` both push a cleared value when consumed, which makes silence look like proof
# that nothing happened. Some fields just do the work and say nothing, so the only honest test is to
# look for the RESULT rather than the acknowledgement.
WIFI_SCAN_SLOTS = 10
WIFI_SCAN_SETTLE_S = 6.0        # results arrive within a few seconds; this is comfortably past that


def parse_scan_slot(raw: str) -> dict:
    """One `wifiScanN` value: `<SSID>----<security>`.

    An SSID may legitimately contain a hyphen, so the split is on the four-hyphen separator from the
    RIGHT and only once. Anything that does not carry a separator is treated as a bare SSID rather
    than discarded, because a network we cannot classify is still one you may want to join."""
    text = str(raw or "").strip()
    if not text:
        return {}
    ssid, sep, security = text.rpartition("----")
    if not sep:
        return {"ssid": text, "security": "", "open": False, "spoken": text}
    ssid, security = ssid.strip(), security.strip()
    # "the security type is blank or reads as none" is the only claim made here. The exact strings
    # this firmware uses beyond that have not been catalogued, so they are passed through as read.
    is_open = security.lower() in ("", "none", "open", "0")
    return {"ssid": ssid, "security": security, "open": is_open,
            "spoken": f"{ssid}, open network" if is_open
                      else f"{ssid}, needs a password" + (f", {security}" if security else "")}


def action_scan_wifi() -> dict:
    """Scan for wifi networks and return what the unit found.

    Writes `wifiScan` = True on NETWORK, waits, then READS the result slots, because the unit
    reports a scan by filling them in rather than by pushing anything (see the comment above)."""
    gate = _network_write_gate()
    if gate is not None:
        return gate
    try:
        with _hid_lock:
            rcp_transfer.send_record(
                rcp_transfer.build_global_prop_bool("wifiScan", True, rcp_transfer.SEL_NETWORK))
        time.sleep(WIFI_SCAN_SETTLE_S)
        with _hid_lock:
            blob = rcp_read.read_model_stable(fresh=True)
    except Exception as exc:
        return {"ok": False, "message": f"Could not scan for networks: {exc}"}

    found, seen = [], set()
    for n in range(1, WIFI_SCAN_SLOTS + 1):
        entry = parse_scan_slot(rcp_read.block_value(blob, "NETWORK", f"wifiScan{n}"))
        # The unit can report the same SSID twice (two bands, or two access points on one network),
        # and a picker listing a name twice is unusable by ear.
        if entry.get("ssid") and entry["ssid"] not in seen:
            seen.add(entry["ssid"])
            found.append(entry)
    if not found:
        return {"ok": True, "networks": [],
                "message": ("The scan finished but found nothing. The unit only reports ten "
                            "networks at most, and it can come back empty if the radio was just "
                            "switched on, so try once more before assuming there is nothing "
                            "in range.")}
    current = str(rcp_read.block_value(blob, "NETWORK", "wifiSSID") or "")
    for entry in found:
        entry["current"] = bool(current and entry["ssid"] == current)
    return {"ok": True, "networks": found, "current": current,
            "message": (f"Found {len(found)} network{'' if len(found) == 1 else 's'}. "
                        f"The unit lists ten at most, so a network in range can still be missing "
                        f"from this list; you can always type the name instead.")}


def _cap_gate(cap: str, refusal) -> dict:
    """None if the CONNECTED device has `cap`, else a refusal dict carrying `refusal(device name)`.

    THE ONE SHAPE EVERY RISKY WRITE FAMILY IS GATED BY. The rule behind all of them: a selector
    proven on a RODECaster Pro II has NOT been proven on a Duo, and a wrong selector does not simply
    fail - it can append a stray property that only a factory reset clears, which has happened on
    hardware here. So a device without the cap REFUSES BY NAME and says why, rather than writing
    blind and hoping.

    Each gate below keeps its own sentence, because what the user can and cannot do differs per
    family and a generic "not supported" would be useless to them.
    """
    dev = devices.get_active_device()
    if cap in dev.caps:
        return None
    return {"ok": False, "message": refusal(dev.name)}


def _network_write_gate():
    """The NETWORK block, 0x10. Proven on both the Pro II and the Duo, so this gate refuses no desk
    we know; it stays for the next one. See _cap_gate."""
    return _cap_gate("network_write", lambda name:
                     f"AccessCaster can read the network settings on the {name} but not change "
                     f"them: changing them has only ever been confirmed on a RODECaster Pro 2, so "
                     f"it refuses rather than risk the wrong setting.")


def soundpad_model() -> dict:
    """Live pad grid the UI renders: what is on each pad now, plus the effect-object pool
    health (which gates clear / assign-effect for safety) and the voice-effect choices.

    Reads the device model (~1.5s) under the HID lock. On a read failure it returns the
    static layout with an error note so the page still renders."""
    base = {
        "replace_supported": True,
        "reason": "Add or replace a pad's sound with your own WAV or MP3, clear a pad, "
                  "or put a factory voice effect on a pad.",
    }
    try:
        with _hid_lock:
            # max_banks / pads_per_bank default to the ACTIVE DEVICE geometry (Pro II 8/bank,
            # Duo 6/bank) - do NOT force the Pro II numbers here. read_pads_live also returns a
            # read_only flag (True on a device without the pad_write cap, e.g. the Duo).
            live = soundpad.read_pads_live(min_banks=PAD_MIN_BANKS)
        base.update(live)
        base["can_edit"] = bool(live.get("health", {}).get("clean"))
        # A preview scratch pad the server still tracks but no page owns (e.g. the browser was
        # closed mid-preview) - the freshly loaded page uses this to clear it on load.
        base["preview_grid"] = _preview_grid
        return base
    except Exception as exc:
        base.update({
            "banks": PAD_MIN_BANKS,
            "pads_per_bank": PADS_PER_BANK,
            "pads": [],
            "recipes": list(soundpad.all_recipes().keys()),
            "custom_fx": list(soundpad.load_user_fx_presets().keys()),
            "can_edit": False,
            "preview_grid": _preview_grid,
            "health_message": f"Could not read the pad layout from the device: {exc}",
        })
        return base


# ----------------------------------------------------------------------------
# Control actions. Each returns {"ok": bool, "message": str}. All device access
# goes through _hid_lock so the unit is only ever opened by one request at once.
# ----------------------------------------------------------------------------

def action_set_midi(on: bool) -> dict:
    with _hid_lock:
        rcp_transfer.set_midi_control(on)
    with _session_state_lock:
        _session_state["midi"] = on
    return {"ok": True, "message": f"MIDI control turned {'on' if on else 'off'}."}


# --- Control announcer: speak physical button presses aloud (accessibility aid) -------------
# A background thread listens to report-04 HID events and speaks bank changes / mute / solo /
# encoder presses via SAPI (say.ps1), so a screen-reader user knows what a physical press did
# without seeing the touchscreen (the announce_controls.py logic, server-integrated).
#
# DEVICE-AGNOSTIC (GAP-7, verified): the decoder keys off the report-04 PROPERTY NAMES
# (selectedBank / cmutePressed / csoloPressed / encoderPressed), which are protocol-level and shared
# across both desks, NOT a Pro II physical control-map or channel-index table. It speaks generic
# "Bank N" / "Mute" / "Solo" / "Select" (no per-channel index), so the Duo's fewer faders/banks need
# no special-casing. It is also strictly READ-ONLY (one read handle, no writes), so even if a name
# ever differs on a future desk the worst case is a missed announcement, never a mis-write.
#
# HID SHARING: the server uses the same control interface for pad/mixer ops. The announcer keeps
# ONE read-only handle open for its whole lifetime and reads continuously, WITHOUT taking _hid_lock.
# This is safe because the Windows HID stack delivers every input report (the report-04 control
# events) to each open handle's own queue, so the listener never misses a press and never disturbs
# the writes, which open their own handle under _hid_lock. (Verified on this unit: a second handle
# opens fine and a command still ACKs while it is open, see tools/probe_concurrent_hid.py.)
# DO NOT go back to opening and closing the handle around each poll to avoid two concurrent handles:
# closing FLUSHES the input queue, so every press landing in the gap is dropped and bank/mute/solo
# announcements go intermittent. A persistent handle has no deaf windows and no open/close churn.
#
# THE HANDLE IS REOPENED ON A FRESHLY RESOLVED INTERFACE, never on a remembered one. The desk
# re-enumerates on `outputMultiMode`, `engineMode` and `systemMainMode`, all of which the app itself
# writes, and comes back under a different USB product id and path.
_announce_on = threading.Event()
_announce_thread = None

# Reconnect pacing. Quick at first, so a momentarily busy handle costs nothing audible and a
# re-enumeration (about 2.5 s) is picked up as soon as the desk is back; slow after that, because
# every attempt re-enumerates the USB HID tree and a desk off the bus may never return without a
# replug.
_ANN_RETRY_S = 0.3
_ANN_RETRY_SLOW_S = 2.0
_ANN_RETRY_FAST_TRIES = 15
# A reconnect is only worth speaking about when the gap was long enough to have swallowed a press.
# Coming straight back on the next pass is not news.
_ANN_RECONNECT_SPEAK_AFTER_S = 1.0

# The server runs under pythonw (no console), so any console subprocess we spawn would flash a
# window and steal focus. CREATE_NO_WINDOW keeps these helpers invisible. Does NOT apply to the
# browser launch (open_ui), which is the app window we DO want shown.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


# SPEECH: one PERSISTENT worker process, fed a phrase per line on stdin (say_worker.ps1).
# NOT one `say.ps1` process per phrase: that costs 1.3-1.7 s each (PowerShell + speech-stack
# startup, not the speaking), and since the announcer speaks on every mute/solo press, a few quick
# presses spawn several such processes all fighting for the audio device, so announcements lag and
# then appear to stop. With the worker, only the FIRST phrase pays startup. If the worker cannot be
# started we fall back to the one-shot, so the worst case is slow rather than silent.
_ann_speak_q: "queue.Queue[str]" = queue.Queue()
_ANN_SAY_QMAX = 1          # newest wins: the worker purges anyway, so queueing only adds lag
_ANN_PACE_S = 0.20         # never hand the worker phrases faster than this (see the pump)
_ann_speak_thread = None
_ann_speak_lock = threading.Lock()
# Which Windows output device announcements go to (a name from _ann_audio_outputs(), matched by the
# worker); "" = the system default. This desk's default output IS the RODECaster, so speech arrives
# on USB 1 and is inaudible while another channel is SOLOed (the cue bus replaces the headphone feed)
# or while USB 1 is muted. Pointing announcements at another device makes them survive both.
# IT IS OUR SPEECH OBJECT'S OWN OUTPUT (SAPI SpVoice.AudioOutput), NOT a Windows setting: it does not
# move the system default device, and it does not touch the screen reader, which has its own.
_ann_say_device = ""

# Which voice speaks them ("" = whatever SAPI's default is). Matched by name in the worker.
_ann_voice = ""

# Bumped whenever the voice or the output device changes. The pump owns the worker process and is the
# only thread allowed to touch it, so a change cannot restart the worker directly: it raises this
# counter and the pump respawns before the next phrase. Volume does NOT need this (the worker takes
# a "!vol N" control line in place), but voice and device are set when the SpVoice is created.
_ann_worker_gen = 0

# How loud announcements are, 0-100. This is SAPI's own stream volume, so it scales ONLY our speech:
# Windows system volume, other apps and the desk's own audio are untouched. It exists because at
# full volume the announcer talks over what you are actually listening to while riding the faders.
# Persisted, because a level you have to set again every launch is no use.
_ANN_VOLUME_DEFAULT = 100
_ann_volume = _ANN_VOLUME_DEFAULT

# Persisted preferences live in ONE small file in %LOCALAPPDATA%\AccessCaster. Reads and writes go
# through here so a writer can never clobber a key it does not know about: a writer that dumps
# {"announcer": ...} over the whole file silently eats the announcer volume the first time the tray
# toggles the announcer. update_settings MERGES.
_SETTINGS_FILE = data_path("settings.json")
_settings_lock = threading.Lock()


def load_settings() -> dict:
    try:
        with open(_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def update_settings(**changes) -> None:
    """Merge changes into the settings file. Best-effort: a preference that cannot be written is
    not worth failing an action over, it just will not survive a restart."""
    with _settings_lock:
        data = load_settings()
        data.update(changes)
        try:
            with open(_SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError:
            pass


def _clamp_volume(value) -> int:
    """A percentage the UI or the settings file offered, forced into 0-100. A bad value falls back
    to full volume rather than to silence: silence would look like the announcer being broken."""
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return _ANN_VOLUME_DEFAULT


_ann_volume = _clamp_volume(load_settings().get("announcer_volume", _ANN_VOLUME_DEFAULT))
_ann_voice = str(load_settings().get("announcer_voice", "") or "")
_ann_say_device = str(load_settings().get("announcer_output", "") or "")
callme.set_contacts(load_settings().get("callme_contacts"))


# WHAT SPEECH IS AVAILABLE. Both lists come from SAPI itself (the same engine the worker speaks
# through), so what the page offers is exactly what can be chosen - no guessing from the registry,
# and nothing offered that the worker would then fail to find.
# The lists are CACHED because reading them costs a COM round trip, and re-read on demand: plugging
# in headphones adds an output, and installing a voice adds a voice, without restarting the app.
_ann_speech_cache = {"voices": None, "outputs": None}
_ann_speech_lock = threading.Lock()


def _sapi_lists(refresh: bool = False) -> dict:
    """{"voices": [...], "outputs": [...]} of SAPI names, or empty lists if speech is unavailable.

    COM has to be initialised per THREAD, and this runs on whichever HTTP worker thread took the
    request, so CoInitialize/CoUninitialize wrap the call. A machine with no speech stack at all
    reports empty lists rather than failing the request: the announcer simply has nothing to choose
    from, and the rest of the page still works."""
    with _ann_speech_lock:
        if not refresh and _ann_speech_cache["voices"] is not None:
            return dict(_ann_speech_cache)
        voices, outputs = [], []
        try:
            import pythoncom                      # noqa: PLC0415  (pywin32, Windows only)
            import win32com.client                # noqa: PLC0415
            pythoncom.CoInitialize()
            try:
                spv = win32com.client.Dispatch("SAPI.SpVoice")
                voices = [t.GetDescription() for t in spv.GetVoices()]
                outputs = [t.GetDescription() for t in spv.GetAudioOutputs()]
            finally:
                # EVERY COM reference has to be dropped BEFORE CoUninitialize, or the release
                # happens on a thread with no apartment left and pywin32 prints "Win32 exception
                # occurred releasing IUnknown". Harmless, but it is real noise in the log and the
                # sort of thing that gets blamed for an unrelated fault later.
                spv = None
                pythoncom.CoUninitialize()
        except Exception:
            pass
        _ann_speech_cache["voices"] = voices
        _ann_speech_cache["outputs"] = outputs
        return dict(_ann_speech_cache)


# The queue between _ann_say and the speech worker carries plain phrases. A line starting with this
# prefix is a CUE instead: the worker plays the named WAV through the same SpVoice (see
# say_worker.ps1), so it lands on the announcer's chosen output rather than the system default.
_ANN_WAV_PREFIX = "!wav "

# NOTHING IS EVER PLAYED OUT LOUD UNDER THIS FLAG, set by `DevTools/_bootstrap` for any script
# named `test_*`. The announcer speaks through SAPI to the default output, which on the machines
# this app is built for IS THE DESK - so an offline test that happens to exercise an announcing
# path plays tones and speech into the user's monitoring, with nothing on screen to explain it.
# possibly mid-recording. Phrases are still LOGGED (see _ann_say), so a test can still be
# diagnosed; they simply never reach an audio device.
_ANN_SILENT = bool(os.environ.get("ACCESSCASTER_SILENT"))


def _ann_wav_spoken_form(wav_path: str) -> str:
    """What a cue would have SAID, for the paths that cannot play it. Derived from the file name so
    a new cue cannot forget to add itself here and end up silent."""
    name = os.path.splitext(os.path.basename(wav_path or ""))[0]
    return {"muted": "muted", "unmuted": "unmuted"}.get(name, name or "")


def _ann_tone(name: str) -> bool:
    """Queue the named tone instead of a phrase. True if it was queued, False if we have no such
    tone, in which case the caller must speak - a cue that silently does nothing is worse than the
    sentence it was replacing."""
    try:
        path = tones.tone_path(name)
    except Exception:
        return False
    if not path:
        return False
    _ann_say(_ANN_WAV_PREFIX + path)
    return True


def _ann_say_oneshot(text: str) -> None:
    """The original one-process-per-phrase path. Fallback only (see _ann_say). Carries the same
    volume, VOICE and OUTPUT DEVICE as the worker, so degrading to this path does not suddenly get
    loud again, change voice, or (worst of the three) start speaking into the desk."""
    try:
        # A CUE IS NOT A PHRASE. The queue carries "!wav <path>" for the mute tones (see _ann_tone),
        # and without this the fallback path would cheerfully read a Windows file path aloud.
        wav = ""
        if text.startswith(_ANN_WAV_PREFIX):
            wav = text[len(_ANN_WAV_PREFIX):]
            text = _ann_wav_spoken_form(wav)
        cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-File",
               resource_path("say.ps1"), "-Text", text,
               "-Volume", str(_ann_volume), "-Force"]
        if wav:
            cmd += ["-Wav", wav]
        if _ann_voice:
            cmd += ["-Voice", _ann_voice]
        if _ann_say_device:
            cmd += ["-Device", _ann_say_device]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=_NO_WINDOW)
    except Exception:
        pass


def _ann_speak_pump() -> None:
    """Own the speech worker: start it, feed it phrases, restart it if it dies.

    COALESCE AND PACE. The worker speaks with purge-before-speak, so if we hand it five phrases it
    dutifully starts and cancels five utterances, and each stop-and-restart costs real time, which is
    heard as the announcer pausing for a second before it says anything. So: whenever more than one
    phrase is waiting, only the NEWEST is sent (older ones are already obsolete by definition),
    and we never hand over faster than _ANN_PACE_S. A moving fader then speaks about five times a
    second, every phrase audible, none of them queued behind a cancelled one."""
    proc = None
    last_sent = 0.0
    sent_vol = None          # the volume the LIVE worker was last told, so a change is sent once
    spawned_gen = -1         # the voice/device generation the LIVE worker was started with
    while True:
        text = _ann_speak_q.get()
        if text is None:                                   # shutdown sentinel
            break
        # Anything already waiting supersedes what we just took.
        while True:
            try:
                newer = _ann_speak_q.get_nowait()
            except queue.Empty:
                break
            if newer is None:
                return
            text = newer
        # Pace the hand-off, preferring anything that arrives while we wait.
        gap = _ANN_PACE_S - (time.time() - last_sent)
        while gap > 0:
            try:
                newer = _ann_speak_q.get(timeout=gap)
            except queue.Empty:
                break
            if newer is None:
                return
            text = newer
            gap = _ANN_PACE_S - (time.time() - last_sent)
        last_sent = time.time()
        if proc is not None and proc.poll() is not None:    # worker died; drop it and respawn
            proc = None
        gen = _ann_worker_gen                               # read once, with the values below
        voice, device = _ann_voice, _ann_say_device
        if proc is not None and spawned_gen != gen:
            # The voice or the output device changed. Both are fixed when the worker creates its
            # SpVoice, so the running one has to go; the replacement is started immediately below and
            # speaks the confirmation the action queued, which is how you hear the new voice.
            try:
                proc.stdin.write("__QUIT__\n"); proc.stdin.flush()
            except Exception:
                try: proc.kill()
                except Exception: pass
            proc = None
        vol = _ann_volume                                   # read once; it can change under us
        if proc is None:
            cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-File",
                   resource_path("say_worker.ps1"), "-NoGate", "-Volume", str(vol)]
            if voice:
                cmd += ["-Voice", voice]
            if device:
                cmd += ["-Device", device]
            try:
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW,
                                        text=True, bufsize=1)
                sent_vol = vol
                spawned_gen = gen
            except Exception:
                _ann_say_oneshot(text)                      # no worker: degrade, do not go silent
                continue
        try:
            # A volume change rides in front of the next phrase rather than being pushed to the
            # worker the moment the slider moves: the pump is the only thread that owns proc, so
            # nothing else may write to its stdin. Nothing is lost by waiting, since volume only
            # matters when something is about to be spoken.
            if sent_vol != vol:
                proc.stdin.write(f"!vol {vol}\n")
                sent_vol = vol
            proc.stdin.write(text + "\n")
            proc.stdin.flush()
        except Exception:
            try: proc.kill()
            except Exception: pass
            proc = None
            _ann_say_oneshot(text)
    if proc is not None:
        try:
            proc.stdin.write("__QUIT__\n"); proc.stdin.flush()
        except Exception:
            try: proc.kill()
            except Exception: pass


def _ann_say(text: str) -> None:
    """Queue one phrase for the speech worker. Never blocks the caller (the announcer's read loop
    must keep draining the HID queue), and never lets a burst build a backlog: past _ANN_SAY_QMAX
    the OLDEST pending phrase is dropped, because the newest state is the one worth hearing.

    LOGGED, BECAUSE SPEECH IS THIS APP'S MAIN OUTPUT. Without this line, everything sent to the
    desk is traceable and nothing said to the USER is, which is the wrong way round for an app
    whose whole purpose is telling a blind operator what the desk just did: a burst of repeating
    cues then arrives with the log silent either side, and nothing says what fired them or how many
    there were. A cue is logged by NAME rather than as a file path, so a run of them reads as a run
    of them. A SUPPRESSED phrase is logged too, marked: "it said nothing" and "it was never asked
    to" are different faults and the log has to tell them apart.

    THE ANNOUNCER SWITCH IS ENFORCED HERE, because it is the only place every phrase passes
    through. Roughly half the speech in this app comes from outside the announcer's read loop - the
    virtual-devices follow-up after a USB mode change, the recorder watcher - so a switch that only
    stopped the loop left all of that talking to somebody who had asked for silence. The phrases
    announcing that speech is BACK are safe: each one sets the flag before it speaks.
    """
    what = ("cue " + os.path.basename(text[len(_ANN_WAV_PREFIX):]).split("-")[0]
            if text.startswith(_ANN_WAV_PREFIX) else text[:90])
    if not _announce_on.is_set():
        diagnostics.event("say", what + "  [suppressed: announcements off]")
        return
    diagnostics.event("say", what)
    if _ANN_SILENT:
        return                  # a test run: recorded above, never played. See _ANN_SILENT.
    global _ann_speak_thread
    with _ann_speak_lock:
        if _ann_speak_thread is None or not _ann_speak_thread.is_alive():
            _ann_speak_thread = threading.Thread(target=_ann_speak_pump, daemon=True)
            _ann_speak_thread.start()
    while _ann_speak_q.qsize() >= _ANN_SAY_QMAX:
        try:
            _ann_speak_q.get_nowait()
        except queue.Empty:
            break
    try:
        _ann_speak_q.put_nowait(text)
    except Exception:
        pass


# MUTE / SOLO STATE, hardware-observed.
# Pressing mute or solo pushes, unasked and about 70 ms after the button edge, the STATE property
# `channelOutputMute` / `channelCueEnable`, addressed at SEL_CHANNEL_PROC + strip index. So the
# announcer can say WHICH channel and WHETHER it is now muted, instead of the bare word "Mute" - and
# it catches a mute from any source (button, touchscreen, MIDI), not just a press edge. Still
# strictly read-only. The press edge (`mutePressed` / `soloPressed`) is addressed at the physical
# BUTTON, which names no channel, so it is only the fallback when no state follows.
_ANN_STATE_SEL_BASE = channels.SEL_CHANNEL_PROC      # 0x1C + strip index
_ANN_STATE_PROPS = {"channelOutputMute": "mute", "channelCueEnable": "solo"}
# HOW LONG A MIXER PAD OWNS THE MUTE AND CUE LINES. One press of a Back Channel pad moves four of
# these at once (both members' mute and cue), and each edge of a momentary pad brings its own burst,
# so speaking them turns a two-second talkback push into eight announcements describing a state the
# operator just asked for. A DEADLINE, not a switch: every mixer `padActive` pushes it further out,
# so repeated presses cannot leave it stuck on, and a missed window costs only today's behaviour.
# Generous against the burst (the pushes arrive together, within tens of ms) and short enough that a
# real mute a moment later still speaks.
_ANN_MIXER_QUIET_S = 0.75
_ann_mixer_quiet_until = 0.0
# PRESS AND HOLD ON THE ENCODER MUTES THE SELECTED CHANNEL. Hardware-measured: a short press runs
# 475 ms down-to-up and mutes nothing; a hold
# pushes `channelOutputMute = True` 765 ms after the press edge, WHILE STILL HELD, and mutes the
# channel selected BEFORE the press. So the desk does not advance its encoder ring on a hold. RODE's
# user guide documents press-and-hold only on the REC button, so it is not there either.
#
# Two consequences, both of which this handling exists for:
#  1. ANNOUNCING THE NEXT RING POSITION ON THE PRESS EDGE IS WRONG ON A HOLD: you hear the next
#     channel named and then the previous one announced as muted, and our ring position silently
#     desyncs from the desk's, so every later prediction is one out until a real movement re-seats
#     it. So we wait for the RELEASE edge, which the desk does push, and only advance on a short
#     press.
#  2. What tells the two apart is the mute push itself, not a stopwatch: any `channelOutputMute`
#     arriving between our press and release means the desk took the gesture. The duration below is
#     only a backstop for a desk that pushes no state (the Duo is unobserved here), set well clear of
#     both measurements.
_ANN_ENC_HOLD_S = 0.9
# How long to wait for the STATE push after a press edge before falling back to the generic word.
# Measured gap on this desk is ~70 ms; this is deliberately generous. The fallback keeps the
# announcer device-agnostic (GAP-7): a desk that pushes no state still says "Mute" / "Solo".
_ANN_PENDING_S = 0.5


# SPOKEN POSITIONS. The desk pushes a moving fader's position as
# `mixLevelWithAnchor` on every one of that source's 13 cross-points, 8-10 times a second, and the
# monitor encoder as `outputMonLevel`. Speaking each push would be a torrent, so we hold the latest
# value and speak ONE phrase once the movement stops (_ANN_SETTLE_S of quiet). Separately switchable
# from the button announcements: some sessions want mute/solo spoken but not a running commentary on
# every fader.
# Persisted, like the volume and the on/off: a bare default means turning levels OFF comes silently
# back ON at the next launch.
_ann_levels_on = bool(load_settings().get("announcer_levels", True))
# MUTE AS A TONE INSTEAD OF A SENTENCE. Off by default: speech is what everyone gets until they
# choose otherwise, and the tones only make sense to someone who has been told what they mean.
_ann_mute_tones = bool(load_settings().get("announcer_mute_tones", False))
# SPEAK THE DESK'S STATE WHEN THE APP OPENS. It is an ANNOUNCEMENT, not a page, so it is a setting
# of its own rather than a side effect of which page the app opens on. Default ON: knowing the
# desk's state without looking is the point of the app, and on a normal desk it is one sentence.
_ann_startup_summary = bool(load_settings().get("announcer_startup_summary", True))
_ANN_SETTLE_S = 0.12       # only a safety net now: guarantees the closing value is spoken
_ANN_LEVEL_GAP_S = 0.10    # minimum spacing between spoken level updates while moving
_ANN_RUN_S = 1.5           # within this of the last update on the same target, say just the number
# WHAT THE ENCODER PRESS SELECTS, tracked by us because the desk never says.
#
# We DO see every press, so since the press CYCLES through a fixed order we keep our own position in
# that order and say what has just been selected BEFORE it is turned - which matters, because the
# first click already moves the value by 1/127.
#
# The order is HARDWARE-OBSERVED, not assumed: pressing walks fader 7 -> fader 8 -> fader 9 ->
# monitor level -> round again, i.e. exactly the things with no physical fader. Evidence from one
# capture: consecutive press-then-click pairs revealed 0x0d, 0x0a, 0x0b, 0x0c in that order, and a run
# of FIVE presses with no turn in between landed one step on from where it started, which is what a
# four-step ring does. Selectors: faderLevel 0x04 + fader index (7 -> 0x0a, 8 -> 0x0b, 9 -> 0x0c, all
# observed), outputMonLevel 0x0d.
#
# We never TRUST the prediction blindly: the first movement reports the real target, which re-seats
# our position, so a wrong guess self-corrects and costs one spoken phrase.
#
# DEVICE-AWARE: the ring is the faders with NO physical pot, then the monitor,
# so it is derived from devices.FaderSpec rather than hardcoded. Pro II (physical 6, count 9) yields
# exactly the observed ring - faders 7/8/9 then monitor - byte for byte. The Duo (physical 4, count 9)
# yields faders 5-9 then monitor, which is the same RULE but an UNOBSERVED order on that desk; a wrong
# guess costs one spoken phrase because the first real movement re-seats us (see _ann_ring_seat).
_ANN_ENCODER_RING_PRO2 = (("fader", 6), ("fader", 7), ("fader", 8), ("monitor", 0))


def _ann_encoder_ring() -> tuple:
    """The encoder's press-cycle order for the CONNECTED desk: virtual faders, then monitor level."""
    try:
        spec = devices.get_active_device().fader
        ring = tuple(("fader", i) for i in range(spec.physical, spec.count)) + (("monitor", 0),)
        return ring if len(ring) > 1 else _ANN_ENCODER_RING_PRO2
    except Exception:
        return _ANN_ENCODER_RING_PRO2


_ANN_FADER_LEVEL_MAX = 127.0     # faderMax; faderLevel/127 IS the cross-point level, checked exactly
# The headphone pots. Each POT node under PHYSICALINTERFACE[0] publishes its OWN potMin and potMax
# (0 and 127 on a Pro II, read off the desk), so this constant is the desk's figure rather than an
# assumed one - check those two properties before trusting it on a new device.
# Four pots, one per headphone output.
_ANN_POT_LEVEL_MAX = 127.0
_ANN_POT_COUNT = 4
# TURNING WITH NOTHING BOUND. The encoder only adjusts a level while the desk has one bound to it,
# and pressing the BANK buttons moves the screen to the pads page, which unbinds it (webcam-confirmed:
# 2x4 pad grid on screen, and a full model diff over ten clicks changed 0 of 459 properties).
# In that state the desk still reports the rotation as `encoderSignal` but nothing moves,
# so silence is technically correct and infuriating in practice. Say so instead: if a turn is followed
# by no level change, announce that it is not linked. `encoderColour` corroborates it - it read 7 while
# the encoder was driving a fader and -1 once it went inert - so we use that when the desk offers it.
_ANN_ENC_DEAD_S = 0.45     # a turn with no level change within this was a turn against nothing
_ANN_ENC_DEAD_GAP_S = 4.0  # and do not repeat that message more often than this
# A pushed cross-point selector is CROSS_POINT_BASE + 13*source + output (rcp_transfer), and the
# matrix row IS the source code (ProjectMemory/), so one selector byte gives us the
# source directly.
_ANN_XPOINT_BASE = rcp_transfer.CROSS_POINT_BASE
_ANN_XPOINT_STRIDE = rcp_transfer.CROSS_POINT_SOURCE_STRIDE


def _ann_source_label(source_code: int) -> str:
    """A speakable name for a matrix SOURCE code, e.g. 'Mic 1'. Never touches the device.

    `all_labels`, NOT `code_label`: a fader can HOLD a stereo-linked pair (codes 4, 5, 6) even though
    a pair is never something you assign, and code_label covers only the assignable set. With the
    narrow one, a linked Mic 1 + 2 is announced as "Source 4" out loud."""
    try:
        lbl = devices.get_active_device().fader.labels_for(channels.usb_expanded()).get(source_code)
        if lbl:
            return lbl
        for s in devices.get_active_device().matrix.sources_for(True):
            if s.get("index") == source_code:
                return s.get("label") or f"source {source_code}"
    except Exception:
        pass
    return f"Source {source_code}"


def _ann_channel_label(strip_idx: int) -> str:
    """A speakable name for a strip index, e.g. 'Mic 2', from the known fader layout.

    Never touches the device: the announcer thread holds no HID lock, so a model dump from here
    would fight the writes. Uses the LAST KNOWN layout rather than the TTL'd cache: nothing refreshes
    that cache while the operator is only pressing buttons, so every announcement past the 15 s TTL
    degrades to "Channel 4". The layout is seeded once when
    the announcer starts and kept current from the stream (see channels.note_strip_source), so a
    stale name is only possible if the layout changed in a way the desk never reported. Falls back to
    'Channel N' whenever we have never seen a layout or it does not name that slot."""
    try:
        vec = channels.cached_strip_vector() or channels.last_known_strip_vector()
        if vec and 0 <= strip_idx < len(vec):
            # labels_for, so a linked pair is named rather than spoken as a bare code, and the
            # Expanded four are named rather than announced as "Source 12".
            label = devices.get_active_device().fader.labels_for(
                channels.usb_expanded()).get(vec[strip_idx])
            if label:
                return label
    except Exception:
        pass
    return f"Channel {strip_idx + 1}"


def _ann_handle_report(b: bytes, st: dict) -> None:
    """Decode one report-04 frame and speak it, updating the announcer state dict `st`
    ({"bank", "pending", "at"}). Only a PUSH is acted on: a slice of a model dump decodes as None
    and is ignored, so a page reading the desk never sounds like the desk changing."""
    global _ann_mixer_quiet_until
    rec = rcp_read.parse_push(b)
    if rec is None:
        return
    path, name, val, kind = rec
    # Typed views of the value, so a branch that wants an int never acts on a string or a bool.
    ival = val if kind == "int" else None
    bval = val if kind == "bool" else None
    fval = val if kind == "float" else None
    # The strip a one-level push at SEL_CHANNEL_PROC + strip is addressed at, else -1.
    strip = path[0] - _ANN_STATE_SEL_BASE if len(path) == 1 else -1
    if not 0 <= strip < 16:
        strip = -1

    if name == "selectedBank":
        if ival is None:
            return
        # Keep the shortcut layer's copy current whether or not this is a change: the bank decides
        # which pad a number key fires, so a stale one plays a stranger's sound.
        _hk_note_bank(ival)
        if ival != st["bank"]:
            st["bank"] = ival
            _ann_say(f"Bank {ival + 1}")
        return
    # PAD PLAYBACK, TRACKED AND NEVER SPOKEN. A playing pad pushes `padProgress` about twenty times a
    # second, so this is the one branch where saying anything at all would be wrong: the "what is
    # playing" shortcut reads the state out on demand instead, rather than talking over the pad the
    # whole time it plays. Both are addressed at the pad's STREAM POSITION, the last path element.
    if name == "padProgress":
        if fval is not None and path:
            _hk_note_pad_progress(path[-1], fval)
        return
    if name == "padActive":
        if bval is not None and path:
            _hk_note_pad_active(path[-1], bval)
            # BOTH EDGES. A momentary pad restores every channel it moved when it is released, and
            # that burst says no more than the one on the way in.
            if path[-1] in _hk_mixer_pos:
                _ann_mixer_quiet_until = time.time() + _ANN_MIXER_QUIET_S
        return
    if name == "mixLevelWithAnchor":
        # A fader is moving. One phrase per gesture, not per push: hold the latest value and let
        # _ann_flush_pending speak it once the pushes stop. A cross-point selector is
        # CROSS_POINT_BASE + 13*source + output, which passes 255 at source 14, so Virtual A and
        # Virtual B are addressed with a two-byte index: use the whole path element, never a byte.
        if not _ann_levels_on or kind != "str" or "|" not in val or len(path) != 1:
            return
        if path[0] < _ANN_XPOINT_BASE:
            return
        try:
            lvl = float(val.split("|", 1)[0])
        except ValueError:
            return
        _ann_note_level(st, "fader", (path[0] - _ANN_XPOINT_BASE) // _ANN_XPOINT_STRIDE, lvl)
        return
    if name == "encoderSignal":
        # Raw rotation, a COMPOSITE whose first member is the signed step (+1 clockwise, -1 anti).
        # We do not announce the turn itself - the level change that follows says everything - but
        # if NOTHING follows, _ann_flush_pending speaks up (see _ANN_ENC_DEAD_S). EXCEPT at the ends
        # of the range: turning down at 0, or up at maximum, legitimately changes nothing.
        if _ann_levels_on:
            delta = val[0][0] if kind == "pair" and val and val[0][1] == "int" else 0
            st["enc_turn_at"] = None if _ann_at_limit(st, delta) else time.time()
        return
    if name == "systemChannelSelected":
        # -1 = nothing selected, and the encoder is back on the levels ring. Anything else is a STRIP
        # INDEX: that channel is selected and the encoder now moves its gain. The channel buttons
        # emit no press edge of their own - this state change IS the event.
        if ival is None or ival == st.get("chan_sel"):
            return
        st["chan_sel"] = ival
        _ann_say("Levels. Encoder sets fader and monitor levels" if ival < 0
                 else _ann_selected_phrase(ival))
        return
    if name in ("inputMicrophoneGain", "inputDigitalGain"):
        # The gain the encoder moves while a channel is selected. BOTH kinds matter: a MIC strip
        # moves inputMicrophoneGain, a line strip (USB 1, Chat, USB 2, Bluetooth) moves
        # inputDigitalGain. Miss the second and a working turn on USB 1 announces "not linked" while
        # the system audio audibly changes. Pushed at the channel's INPUTSOURCE node, whose index is
        # two bytes (0x26E + source_code).
        if not _ann_levels_on or ival is None:
            return
        st["enc_turn_at"] = None              # the turn DID do something: it moved a gain
        who = _ann_gain_label(path, name == "inputMicrophoneGain")
        if who and st.get("gain_said") != (who, ival):
            # Record it either way, so a suppressed echo is not spoken by a later duplicate push.
            st["gain_said"] = (who, ival)
            if not _ann_is_self_echo(("gain", who)):
                _ann_say(f"{who} {ival} d B")
        return
    if name == "encoderColour":
        # -1 = the encoder is bound to nothing; anything else is the colour of what it drives.
        if ival is not None:
            st["enc_bound"] = (ival >= 0)
        return
    if name == "faderLevel":
        # The VIRTUAL fader level, 0-127, hardware-observed. This is the encoder's own report of
        # what it just moved, and its address names the FADER directly
        # (PHYSICALINTERFACE.FADER[base + index]), so it is the cleanest signal for re-seating the
        # ring - the cross-point pushes that follow say the same thing by source.
        idx = (path[1] - devices.FADER_NODE_BASE) if len(path) == 2 and path[0] == 0 else -1
        if ival is not None and 0 <= idx < 16:
            _ann_ring_seat(st, "fader", idx, max(0.0, min(1.0, ival / _ANN_FADER_LEVEL_MAX)))
        return
    if name == "outputMonLevel":
        if _ann_levels_on and fval is not None:
            _ann_ring_seat(st, "monitor", 0, fval)
            _ann_note_level(st, "monitor", 0, fval)
        return
    # THE HEADPHONE KNOBS. They are physical pots with NO TACTILE INDICATOR, so there is no way to
    # know where one is set by touch, which is exactly the case software has to answer. The desk
    # pushes `potLevel` per step as the knob turns: measured 199 pushes in 12 seconds, one per
    # increment, with the pot identified by the path. They go through the ordinary level machinery,
    # so a sweep settles into ONE closing phrase rather than a torrent, exactly as a fader does. NOT
    # seated on the encoder ring: the ring cycles what the ENCODER selects, and a headphone knob is
    # not on it.
    if name == "potLevel":
        idx = path[1] if len(path) == 2 and path[0] == 0 else -1
        if _ann_levels_on and ival is not None and 0 <= idx < _ANN_POT_COUNT:
            _ann_note_level(st, "headphone", idx, max(0.0, min(1.0, ival / _ANN_POT_LEVEL_MAX)))
        return
    if name == "channelInputSource":
        # A fader source changed. Keep the naming layout current and drop the TTL'd cache, so a
        # change made on the desk itself is picked up at once instead of after up to 15 s. We do NOT
        # speak it: our own fader writes push this too, and the UI already reports those.
        if ival is not None and strip >= 0:
            channels.note_strip_source(strip, ival)
        return
    if name in _ANN_STATE_PROPS:
        if bval is None or strip < 0:
            return
        on, what = bval, _ANN_STATE_PROPS[name]
        st["pending"] = None            # the state supersedes the press edge
        who = _ann_channel_label(strip)
        if what == "mute":
            # Track it: the encoder ring quotes this instead of a level (a muted channel's level is
            # not the useful fact), and a mute landing mid-press is what identifies the gesture as
            # the desk's press-and-hold rather than a selection change.
            st.setdefault("muted", {})[strip] = on
        # Our own write, echoed back: the UI announced it already (see _ann_mark_self_write). After
        # the cache, so the ring still quotes the truth, and before the press-and-hold inference,
        # because a software mute is not a gesture on the desk. Solo is marked too
        # (_ann_publish_solo), though this desk pushes nothing back for a software solo, so that
        # branch is insurance rather than an observed fix.
        if _ann_is_self_echo((what, strip)):
            return
        # A mixer pad is mid-action, so this mute or cue is the pad doing its job. After the cache
        # above, which must stay true whether or not anything is said.
        if time.time() < _ann_mixer_quiet_until:
            return
        if what == "mute":
            if st.get("enc_down_at"):
                st["enc_hold"] = True
            # A TONE IF THEY HAVE ASKED FOR ONE, and the sentence if the tone cannot be played. Mute
            # is the announcement that arrives too late as words: you press it mid-sentence, and by
            # the time "Mic 2 muted" has been said the moment has gone. The cue does not name the
            # channel, which is the trade and is why this is an option rather than the default.
            if not (_ann_mute_tones and _ann_tone("muted" if on else "unmuted")):
                _ann_say(f"{who} {'muted' if on else 'unmuted'}")
        else:
            _ann_say(f"{who} solo {'on' if on else 'off'}")
        return
    # Press EDGES. Do not speak yet: hold the generic word for _ANN_PENDING_S in case the state
    # property follows (it names the channel, which is strictly better).
    if name == "mutePressed" and bval is True:
        st["pending"], st["at"] = "Mute", time.time()
        return
    if name == "soloPressed" and bval is True:
        st["pending"], st["at"] = "Solo", time.time()
        return
    if name == "encoderPressed" and bval is True:
        # Say NOTHING yet. A short press advances the ring; a hold mutes instead and leaves the
        # selection alone (see _ANN_ENC_HOLD_S), and at the press edge we cannot yet tell which this
        # is. The cost is announcing on release, about 400 ms later on a measured press, which is
        # far cheaper than naming the wrong channel and desyncing the ring.
        st["enc_at"], st["enc_named"] = time.time(), False
        st["enc_down_at"], st["enc_hold"] = time.time(), False
        return
    if name == "encoderPressed" and bval is False:
        down_at = st.get("enc_down_at")
        held = bool(st.get("enc_hold")) or (
            down_at is not None and (time.time() - down_at) >= _ANN_ENC_HOLD_S)
        st["enc_down_at"], st["enc_hold"] = None, False
        if held:
            return          # the desk muted; that announcement already named the channel
        # A CHANNEL BUTTON HOLDS THE ENCODER, AND AN ENCODER PRESS DOES NOT TAKE IT BACK. Measured
        # on the desk: with `systemChannelSelected` at 0, two encoder presses leave it at 0, and only
        # the channel button pressed again returns it to -1. Advancing the ring here announces USB 1
        # and then Chat while the desk has never left Mic 1 - and worse than the wrong words, it
        # leaves `enc_pos` pointing at a ring entry the encoder is not on, which is the same value
        # `_ann_at_limit` reads to decide whether a turn is against a stop. Say what the encoder is
        # still on, and leave the ring position alone.
        sel_now = st.get("chan_sel")
        if sel_now is not None and sel_now >= 0:
            _ann_say(_ann_selected_phrase(sel_now))
            return
        # A short press advances the fixed ring (_ann_encoder_ring), so once we know where we are we
        # can say WHAT was just selected and where it sits - before a click moves it. Until the first
        # movement seats us, all we can honestly say is "Select". A release with no press behind it
        # (the announcer started mid-press) counts as a short press: a possibly-wrong name
        # self-corrects, silence just leaves you guessing.
        pos = st.get("enc_pos")
        if pos is None:
            _ann_say("Select")
        else:
            st["enc_pos"] = (pos + 1) % len(_ann_encoder_ring())
            _ann_say(_ann_ring_phrase(st, st["enc_pos"]))
        return


def _ann_seed_layout(st: dict) -> None:
    """Read the fader layout ONCE when the announcer starts, so the very first mute press can say
    'Mic 2 muted' rather than 'Channel 7 muted'. Fills in st["known"] (the encoder-ring level cache)
    and st["muted"].

    Seeding the mutes matters because the pushed stream only ever reports CHANGES: without this, a
    channel muted before the announcer started would have the ring quote its level as though it were
    audible, and stay wrong until someone happened to toggle it.

    Takes _hid_lock for the one dump (unlike the read loop, which deliberately never does), and
    gives up quietly on any failure - a missing layout only costs us the channel NAME. Every later
    refresh is free: any page read republishes it, and note_strip_source follows changes on the
    pushed stream."""
    known = {}
    try:
        with _hid_lock:
            blob = rcp_read.read_model()
        vec = rcp_read.values(blob, "channelInputSource")
        channels.publish_strip_vector(vec)
        # Seed the encoder ring's level cache too, so the FIRST press can already say "Chat 97
        # percent" instead of just naming it. faderLevel is 0-127 and only the virtual faders hold a
        # value; outputMonLevel is a 0-1 float.
        fl = rcp_read.values(blob, "faderLevel")
        for i, v in enumerate(fl):
            if isinstance(v, int):
                known[("fader", i)] = max(0.0, min(1.0, v / _ANN_FADER_LEVEL_MAX))
        mon = rcp_read.values(blob, "outputMonLevel")
        if mon and isinstance(mon[0], float):
            known[("monitor", 0)] = mon[0]
        # The headphone pots, for the same reason and on the same contract: seeded so a resting
        # value can never be announced as a change. They are read positionally because the four POT
        # nodes appear in pot order under PHYSICALINTERFACE[0] - the same order the pushed path
        # gives - and that is the only place `potLevel` occurs.
        for i, v in enumerate(rcp_read.values(blob, "potLevel")):
            if isinstance(v, int) and i < _ANN_POT_COUNT:
                known[("headphone", i)] = max(0.0, min(1.0, v / _ANN_POT_LEVEL_MAX))
        mutes = rcp_read.values(blob, "channelOutputMute")
        st["muted"] = {i: v for i, v in enumerate(mutes) if isinstance(v, bool)}
        # SEED THE SELECTED CHANNEL for the same reason as the mutes: the stream reports CHANGES,
        # and the handler speaks whenever the pushed value differs from what it last saw. With no
        # seed, `st["chan_sel"]` starts absent, so the FIRST push of systemChannelSelected - which
        # is normally the resting -1, nothing selected - reads as a change and announces
        # "Levels. Encoder sets fader and monitor levels" out of nowhere, some time after startup,
        # as though the touchscreen had done something. Seeding makes an unchanged value silent,
        # which is the whole contract.
        sel = rcp_read.values(blob, "systemChannelSelected")
        if sel and isinstance(sel[0], int):
            st["chan_sel"] = sel[0]
        # ...and the SELECTED BANK, for exactly the same reason: with st["bank"] left at None, the
        # desk's first push of the resting bank announces "Bank 1" out of nowhere. Same root cause
        # as the phantom "Levels" above, second property.
        # Stored RAW (the desk numbers banks from 0); the announcement adds 1.
        bank = [v for v in rcp_read.values(blob, "selectedBank") if isinstance(v, int)]
        if bank:
            st["bank"] = bank[0]
            _hk_note_bank(bank[0])
        # ...and THE USB AUDIO MODE, which is seeded for the opposite reason to the three above:
        # not to keep a resting value quiet, but so a reconnect can tell that it MOVED. The desk
        # can drop out of Expanded on its own (measured: off the bus unprompted, back two seconds
        # later in Standard with the setting reset), and the change is invisible - the operator's
        # extended inputs stop carrying anything and their computer's audio has rearranged
        # itself. The push cannot carry this: the desk leaves the bus as it changes, so
        # any push is sent into the gap. Comparing across the reconnect is the only way to see it.
        st["usb_mode"] = rcp_read.first(blob, "outputMultiMode", "int")
        channels.publish_usb_expanded(recorder.split_usb_multitrack(st["usb_mode"])[1])
    except Exception:
        pass
    st["known"] = known
    # ...and SEED WHAT COUNTS AS ALREADY SPOKEN, which is the same fix as the two above and the
    # third property to need it. `_ann_flush_pending` speaks a level unless it matches st["said"],
    # an empty st["said"] makes the desk's first push of a RESTING level read as a change, so the
    # announcer opens with "Monitor 95 percent" as though something had moved it. It would fire on
    # every reconnect too, which after a crash is when a spurious announcement is least welcome.
    # Seeding makes an unchanged value silent, which is the whole contract of a CHANGE stream.
    st["said"] = dict(known)


def _ann_pct(level):
    """A level as the whole percent we would SAY, or None. Comparing what we would say (rather than
    the raw float) is what stops the same number being announced twice."""
    return None if level is None else max(0, min(100, int(round(level * 100))))


def _ann_selected_phrase(strip_idx: int) -> str:
    """What to say when a channel button hands the encoder to a strip.

    Says up front whether there is anything to adjust, rather than letting the operator turn the
    encoder and get silence: the SMART pads strip has no gain and no processing (RODE's own guide:
    "You can add processing to any audio channel except the SMART pads"), so its set-up screen leaves
    the encoder with nothing to move. We work that out from the channel itself - whether its source
    has a mic-gain or line-level selector - so it is known at press time, no waiting.

    "NOTHING TO ADJUST" IS WRONG FOR A BUS, and the SMART pads strip is the case that shows it: it
    rides a fader like anything else. The phrase is true only of the CHANNEL SETUP screen the button
    opens, where the encoder moves a gain and a bus has none. The channel still HAS a level, its
    fader.

    SO SAY WHAT THE CHANNEL HAS, NEVER WHAT IT LACKS. A bus gets "Fader level only", which is true
    whether that fader is
    physical or virtual, so there is no device branch here and nothing to keep in step. Only a strip
    with NOTHING on it gets "nothing to adjust", because only then is it true."""
    who = _ann_channel_label(strip_idx)
    dev = devices.get_active_device()
    try:
        vec = channels.cached_strip_vector() or channels.last_known_strip_vector() or []
        code = vec[strip_idx] if 0 <= strip_idx < len(vec) else None
    except Exception:
        code = None
    # Match on the SOURCE CODE only, never on the strip index. They are different numbers the moment
    # the faders are reassigned, so an index match announces "gain" for a line channel as soon as a
    # line source sits on a strip whose index belongs to a mic.
    entry = next((c for c in (dev.ui_channels or ()) if code is not None
                  and c.get("source_code") == code), None)
    if entry and entry.get("kind") == "mic":
        return f"{who} selected. Encoder sets its gain"
    if entry and entry.get("kind") == "line":
        return f"{who} selected. Encoder sets its input level in decibels"
    # No gain of its own: a bus, or a source this desk's read map does not list (CallMe 1 on either
    # desk). It still has a fader, so say so.
    if code is not None and code >= 0:
        return f"{who} selected. Fader level only"
    return f"{who} selected. Nothing here for the encoder to adjust"


def _ann_gain_label(path, is_mic: bool):
    """Name the channel whose GAIN just moved, from the ADDRESS the change was pushed at.

    Mic strips carry 'gain'; line strips carry 'level', which is what the app and the desk call them,
    so the spoken phrase matches the control the operator knows.

    THE ADDRESS IS THE WHOLE PATH, not the low selector byte. Taking that byte alone and looking it
    up in a per-device table breaks on a Duo, whose table holds the dead MIX selectors: a gain turned
    ON THE DESK then pushes at an address the lookup does not recognise, returns None, and is never
    spoken at all. Matching the path against DeviceModel.input_path cannot drift, because the
    announcer and the writer read the same rule."""
    dev = devices.get_active_device()
    entry = dev.channel_for_input_path(path)
    if entry is None:
        return None
    if is_mic:
        if entry.get("kind") != "mic":
            return None
        return f"Mic {channels.mic_channel_number(entry)} gain"
    if entry.get("kind") != "line":
        return None
    # RESOLVED from the wide set above, NAMED from the current mode: the entry that an address
    # matches is fixed, what it is called is not.
    label = (dev.fader.labels_for(channels.usb_expanded()).get(entry.get("source_code"))
             or entry.get("label") or entry.get("name"))
    return f"{label} input level"


def _ann_at_limit(st: dict, delta: int) -> bool:
    """Would a turn in this direction do nothing because we are already at the end of the range?

    Uses the ring's own cache of where things sit, so it only ever suppresses the "not linked"
    message when we actually know the level is against a stop."""
    pos = st.get("enc_pos")
    if pos is None or not delta:
        return False
    ring = _ann_encoder_ring()
    lvl = (st.get("known") or {}).get(ring[pos % len(ring)])
    if lvl is None:
        return False
    return (delta < 0 and lvl <= 0.001) or (delta > 0 and lvl >= 0.999)


def _ann_level_words(name: str, level: float, *, name_first: bool, say_percent: bool = True) -> str:
    """THE one place a level is turned into words. Both announcements below come through here.

    WHY THE WORD ORDER IS A PARAMETER AND NOT A CONSTANT. The two callers want opposite orders and
    both are right, which is the whole reason this is one function with a flag rather than two
    functions that drifted apart:

      * A LEVEL MOVED -> number first. Speaking the name first is what makes a turn feel laggy:
        "USB 2 (Main Stereo)" takes about a second, and each new update purges and restarts the
        speech, so the number never arrives until the movement stops. Number-first means the
        changing fact lands even when the next update cuts the phrase mid-word, and the name
        completes on the first pause.
      * SOMETHING WAS SELECTED -> name first. Nothing has moved yet; the question the press asks is
        "what have I got", so the answer has to lead.

    `say_percent` is dropped only during a RUN of updates, where the unit is obvious and the extra
    syllables just eat into the name."""
    pct = _ann_pct(level)
    if name_first:
        return f"{name} {pct} percent" if say_percent else f"{name} {pct}"
    return f"{pct} percent, {name}" if say_percent else f"{pct}, {name}"


def _ann_moved_phrase(kind: str, source: int, level: float, short: bool = False) -> str:
    """A level that just MOVED. `source` is a SOURCE CODE, never a fader index.

    Source code is the right space here and a fader index is not, because a channel can be OFF
    FADER entirely - the Pro II always has one - and its level still changes and still deserves a
    name. `_ann_ring_phrase` works in the other space, and the two are easy to confuse because both
    speak of a "fader" kind; check which space you are in before calling either."""
    name = ("Monitor" if kind == "monitor"
            else f"Headphone {source + 1}" if kind == "headphone"
            else _ann_source_label(source))
    return _ann_level_words(name, level, name_first=False, say_percent=not short)


def _ann_fader_label(fader_idx: int) -> str:
    """Name the channel sitting on a FADER (by index), from the known layout - so the encoder ring
    announces "Chat" rather than "fader 8", and follows the layout if a source is moved."""
    try:
        vec = channels.cached_strip_vector() or channels.last_known_strip_vector()
        if vec and 0 <= fader_idx < len(vec) and vec[fader_idx] is not None and vec[fader_idx] >= 0:
            return _ann_source_label(vec[fader_idx])
    except Exception:
        pass
    return f"Fader {fader_idx + 1}"


def _ann_ring_phrase(st: dict, pos: int) -> str:
    """What to say the moment the encoder is pressed: the thing now selected and, when we know it,
    where it currently sits. The level comes from our own tracking (seeded at startup and updated
    from every push), so it costs no device read and no turning.

    THIS SIDE WORKS IN FADER INDICES, because that is what the encoder cycles through and what
    `st["known"]` and `st["muted"]` are keyed by. `_ann_moved_phrase` works in SOURCE CODES. Keep
    them straight: the translation between the two lives in `_ann_note_level` and nowhere else."""
    ring = _ann_encoder_ring()
    kind, idx = ring[pos % len(ring)]
    name = "Monitor level" if kind == "monitor" else _ann_fader_label(idx)
    # A muted channel's level is not the useful fact about it, and quoting a healthy-sounding
    # percentage for something you cannot hear is actively misleading. Say the
    # mute instead. The map is seeded from the startup dump and kept current from every pushed
    # `channelOutputMute`, so it costs no device read. Fader index and strip index are the same
    # space here: both index the channelInputSource vector.
    if kind == "fader" and (st.get("muted") or {}).get(idx):
        return f"{name}, muted"
    lvl = (st.get("known") or {}).get((kind, idx))
    if lvl is None:
        return name
    return _ann_level_words(name, lvl, name_first=True)


def _ann_ring_seat(st: dict, kind: str, idx: int, level: float) -> None:
    """A real movement just told us what is selected: re-seat our ring position and cache the level.
    This is what makes a wrong prediction self-correcting."""
    st.setdefault("known", {})[(kind, idx)] = level
    for i, entry in enumerate(_ann_encoder_ring()):
        if entry == (kind, idx):
            st["enc_pos"] = i
            return


def _ann_note_level(st: dict, kind: str, source: int, level: float) -> None:
    """Record a level change, and speak it AT ONCE when it is the first one after an encoder press.

    `source` is a SOURCE CODE (0 for the monitor). THIS IS THE ONE PLACE the two index spaces meet:
    everything spoken is keyed by source, because a source can be off-fader and still have a level,
    while the encoder ring's cache is keyed by FADER INDEX because that is what the encoder cycles.
    The translation below is the only crossing point; do not add another.

    We have always detected the PRESS itself (`encoderPressed`, announced as "Select"), but the press
    carries nothing that says WHICH channel it selected, and the target is chosen by touch. So the
    first movement has to do that job: speaking it immediately means ONE click names the channel,
    instead of nudging repeatedly and waiting for each nudge to settle to find out what is being
    held. Later movements in the same gesture still wait for the settle, so a full sweep is one
    closing phrase rather than a torrent."""
    key = (kind, source)
    now = time.time()
    st.setdefault("levels", {})[key] = level
    st["levels_at"] = now
    st["enc_turn_at"] = None        # something moved, so that turn was not against nothing
    # THE CROSSING POINT. The ring's cache is keyed by FADER INDEX (what faderLevel reports and what
    # the encoder cycles); cross-point pushes arrive keyed by SOURCE CODE. Translate through the
    # layout, or a level changed from our own sliders leaves the ring quoting an old number on the
    # next press. A source that is on NO fader simply has no ring entry, which
    # is correct - and it is still spoken, because speech is keyed by source.
    known = st.setdefault("known", {})
    if kind == "monitor":
        known[("monitor", 0)] = level
    elif kind == "headphone":
        # A headphone pot is its own space: it is not a source and it is not on a fader, so there
        # is nothing to translate and the crossing point below must not be asked to try.
        known[("headphone", source)] = level
    else:
        try:
            vec = channels.cached_strip_vector() or channels.last_known_strip_vector() or []
            fidx = vec.index(source)
        except (ValueError, AttributeError):
            fidx = None
        if fidx is not None:
            known[("fader", fidx)] = level
    if st.get("enc_at") and not st.get("enc_named"):
        st["enc_named"] = True
    said = st.setdefault("said", {})
    at = st.setdefault("said_at", {})
    # OUR OWN write, echoed back by the desk (see _ann_mark_self_write). The UI has already announced
    # it, so stay quiet - but record it as spoken and drop the held value, or the settle flush would
    # speak it a moment later and we would be back where we started.
    if _ann_is_self_echo(key):
        said[key], at[key] = level, now
        st["levels"].pop(key, None)
        return
    # Speak straight away unless we have just spoken this same target: identical percentages are not
    # worth repeating (each update arrives 13 times, once per cross-point), and back-to-back updates
    # are spaced so a sweep sounds like a count instead of a stutter.
    if _ann_pct(said.get(key)) != _ann_pct(level) and (now - at.get(key, 0.0)) >= _ANN_LEVEL_GAP_S:
        # Bare number while the SAME thing keeps moving; full name when the target changes or after a
        # pause, so you always know what you grabbed without hearing the name over and over.
        short = (st.get("run_key") == key and (now - st.get("run_at", 0.0)) < _ANN_RUN_S)
        said[key], at[key] = level, now
        st["run_key"], st["run_at"] = key, now
        st["levels"].pop(key, None)
        _ann_say(_ann_moved_phrase(kind, source, level, short=short))


def _ann_flush_pending(st: dict) -> None:
    """Speak anything being held: a press edge with no STATE behind it (_ANN_PENDING_S), and the
    settled position of a fader or the monitor encoder (_ANN_SETTLE_S)."""
    if st.get("pending") and (time.time() - st.get("at", 0.0)) > _ANN_PENDING_S:
        _ann_say(st["pending"])
        st["pending"] = None
    # A turn that moved nothing: tell the operator, once per burst.
    turn_at = st.get("enc_turn_at") if _ann_levels_on else None
    if turn_at and (time.time() - turn_at) > _ANN_ENC_DEAD_S:
        st["enc_turn_at"] = None
        if (time.time() - st.get("enc_dead_said", 0.0)) > _ANN_ENC_DEAD_GAP_S:
            st["enc_dead_said"] = time.time()
            # Name what is holding the encoder. That is the whole of the original "the encoder has
            # stopped working" mystery.
            #
            # SAY WHAT THE CHANNEL HAS, NEVER WHAT IT LACKS, the same rule _ann_selected_phrase
            # follows. A flat "has nothing for the encoder to adjust" fires on ANY turn the ring
            # does not see, including a mic gain that is moving perfectly well, so Mic 1 gets told
            # it has nothing to adjust while the encoder is adjusting it. The phrase builder works
            # out what the strip actually has, so it cannot make that claim wrongly.
            #
            # The way out is MEASURED, not assumed: the channel button TOGGLES
            # (systemChannelSelected 0, then -1 on the second press), so that is what is named here.
            sel = st.get("chan_sel")
            if sel is not None and sel >= 0:
                _ann_say(_ann_selected_phrase(sel)
                         + ". Press its channel button again for the fader and monitor levels")
            else:
                _ann_say("Encoder not linked to anything")
    levels = st.get("levels")
    if levels and (time.time() - st.get("levels_at", 0.0)) > _ANN_SETTLE_S:
        said = st.setdefault("said", {})
        for key, lvl in list(levels.items()):
            if _ann_pct(said.get(key)) == _ann_pct(lvl):
                continue                       # this position was already spoken while moving
            said[key] = lvl
            st.setdefault("said_at", {})[key] = time.time()
            st["run_key"] = None            # the gesture is over; name it again next time
            _ann_say(_ann_moved_phrase(key[0], key[1], lvl))
        levels.clear()


# The LIVE announcer state, published so the rest of the server can keep it current, and the record
# of what WE have just written so the desk's echo of it is not read back at us.
#
# OUR OWN WRITES *ARE* ECHOED ON THE PUSH STREAM. Hardware proof, against the running app with no
# hand on the desk: three level writes over the HTTP API come straight back and are announced -
# "60 percent, USB 2 (Secondary)", "80 percent, Monitor". What the echo is NOT is a
# reliable cache: it can lag, and it is absent while the announcer is off, which is why the publish
# helpers below still exist.
#
# Without the suppression below, every change is said TWICE: the screen reader reads our live region
# and the announcer speaks the echo. The fix is narrow on purpose: suppress the SPEECH for the exact
# target just written, for a moment, and never the cache updates. A change made ON the desk is
# unaffected and still speaks at once, which is the whole point of the announcer and why "turn it
# off while the app has focus" is the wrong shape. The window is refreshed by each write, so dragging
# a slider stays quiet all the way down, and a hand on that same fader is audible again
# _ANN_SELF_ECHO_S after the last write.
#
# NOT covered, deliberately: a profile RESTORE, which writes the whole desk at once. Narrating that
# is a different problem (it wants one "restoring" phrase, not 40 suppressions) and it has its own
# progress reporting already.
_ann_live_state = None
_ANN_SELF_ECHO_S = 1.5
_ann_self_writes: dict = {}


def _ann_mark_self_write(key) -> None:
    """Remember that WE just wrote `key`, so the desk's echo of it is not spoken back at us.

    Keys are in the space the PUSH arrives in, not the space the writer thinks in: a fader is keyed
    by its SOURCE CODE (what the cross-point selector carries), not its fader index. Marked whether
    or not the announcer is running, since it costs nothing and the two can be toggled apart."""
    _ann_self_writes[key] = time.time()


def _ann_is_self_echo(key) -> bool:
    """Is this push the echo of our own write to the same target, close enough behind it to be that
    write rather than a fresh change on the desk?"""
    at = _ann_self_writes.get(key)
    return at is not None and (time.time() - at) < _ANN_SELF_ECHO_S


def _ann_mark_self_gain(key, is_mic: bool) -> None:
    """Mark a gain/level write of ours under the SAME label the push side builds for it.

    The two sides name a gain differently - the writer has a mic number or a channel name, the push
    has an ADDRESS - so we resolve ours to that same address and let _ann_gain_label do the naming,
    rather than keeping two naming schemes in step. `key` is the mic number (mic strips) or the
    channel name (line strips)."""
    try:
        dev = devices.get_active_device()
        if is_mic:
            def matches(c):
                return c.get("kind") == "mic" and int(c["source_code"]) + 1 == key
        else:
            def matches(c):
                return c.get("kind") == "line" and c["name"] == key
        entry = next((c for c in (dev.ui_channels or ()) if matches(c)), None)
        if entry is None:
            return
        who = _ann_gain_label(dev.input_path(entry), is_mic)
        if who:
            _ann_mark_self_write(("gain", who))
    except Exception:
        pass


def _ann_publish_level(kind: str, idx: int, level01: float) -> None:
    """Tell the announcer about a level WE just wrote, so the encoder ring quotes the new value.

    Cache only, deliberately SILENT: the web UI already announces the result in its live region, and
    speaking as well would say everything twice. Keyed exactly as the ring is - ("fader", fader
    index) or ("monitor", 0) - so no layout translation is needed (that is only for cross-point
    pushes, which arrive keyed by source code). Safe no-op when the announcer is off.

    It also marks the echo, which DOES need that translation: the desk pushes this write back as a
    cross-point keyed by source code, and speaking it would be the second announcement of a change
    the UI has already reported."""
    if kind == "monitor":
        _ann_mark_self_write(("monitor", 0))
    else:
        try:
            vec = channels.cached_strip_vector() or channels.last_known_strip_vector() or []
            if 0 <= int(idx) < len(vec) and vec[int(idx)] is not None:
                _ann_mark_self_write(("fader", vec[int(idx)]))
        except (TypeError, ValueError, AttributeError):
            pass
    st = _ann_live_state
    if st is None:
        return
    try:
        st.setdefault("known", {})[(kind, int(idx))] = max(0.0, min(1.0, float(level01)))
    except (TypeError, ValueError):
        pass


def _ann_publish_mute(strip_idx: int, on: bool) -> None:
    """Tell the announcer about a mute WE just wrote, so the encoder ring keeps quoting the truth.

    Same contract as _ann_publish_level: cache only, deliberately SILENT (the web UI announces the
    mute itself in its live region), and it marks the echo so the announcer does not say it a second
    time. The ring says "Mic 2 muted" instead of a level for a muted channel, so without the cache a
    mute set from here would leave the ring reading out a level nobody can hear, the mirror image of
    a stale level. Keyed by STRIP index, exactly as the desk's own pushes are,
    so no translation is needed here. Safe no-op when the announcer is off."""
    try:
        _ann_mark_self_write(("mute", int(strip_idx)))
    except (TypeError, ValueError):
        pass
    st = _ann_live_state
    if st is None:
        return
    try:
        st.setdefault("muted", {})[int(strip_idx)] = bool(on)
    except (TypeError, ValueError):
        pass


def _ann_usb_mode_change(was, now) -> str:
    """What to say when the USB audio mode is not what it was before the desk left the bus.

    Empty when it has not moved, so the caller falls back to its own wording.

    IT NAMES THE HALF THAT CHANGED, not both. A re-enumeration resets the whole property, but
    reciting two settings when one moved buries the one that matters, and Expanded going away is
    the consequential half: the operator's Game, Music, A and B inputs go dead and every
    RODECaster device on the computer is renamed under it. Spoken rather than shown because
    the desk does this unprompted, when nobody is looking at the app.
    """
    if was is None or now is None or was == now:
        return ""
    was_mode, was_exp = recorder.split_usb_multitrack(was)
    now_mode, now_exp = recorder.split_usb_multitrack(now)
    if was_exp != now_exp:
        # Leaving it, the faders are tidied and spoken about separately (_tidy_after_standard),
        # so this says only what is certain.
        return "USB input is now Expanded." if now_exp else "The unit has left Expanded USB input."
    # A value outside the six measured ones is still worth SAYING, just not naming: going silent on
    # an unknown would hide exactly the case nobody has seen before.
    label = recorder.USB_MULTITRACK_LABEL.get(now_mode)
    return (f"USB output to this computer is now {label.lower()}." if label else
            "The unit's USB audio mode has changed.")


def _announce_loop() -> None:
    global _ann_live_state
    iface = rcp_transfer.find_control_interface()
    if iface is None:
        _ann_say("The control announcer could not open the device.")
        _announce_on.clear()
        return
    # One persistent read-only handle for the whole session. No _hid_lock, no per-cycle
    # close: input reports are delivered to this handle's own queue regardless of the
    # pad/mixer writes, so there are no deaf windows where a press gets dropped.
    dev = None
    misses = 0                        # consecutive failed opens, for the backoff below
    lost_at = 0.0
    st = {"bank": None, "pending": None, "at": 0.0, "levels": {}, "levels_at": 0.0,
          "known": {}, "muted": {}, "enc_pos": None, "enc_down_at": None, "enc_hold": False,
          "said": {}}
    announced_on = False
    _ann_seed_layout(st)
    _ann_live_state = st              # publish it so our own writes can keep the caches current
    while _announce_on.is_set():
        if dev is None:
            if iface is None:
                iface = rcp_transfer.find_control_interface()
            try:
                dev = rcp_hid.open_handle(iface, "announcer") if iface is not None else None
            except Exception as exc:
                diagnostics.event("device.open_failed", "announcer", error=f"{exc}")
                dev = None
            if dev is None:
                # THE CACHED PATH GOES STALE ON A RE-ENUMERATION, so it is dropped after any failed
                # open and re-resolved on the next pass. The desk returns as a DIFFERENT USB product
                # id - the id encodes the audio configuration, so `outputMultiMode` changes it - and
                # a handle re-opened on the old path fails for ever. Holding the path was what left
                # the announcer retrying silently until the app was restarted, which to a
                # screen-reader user is indistinguishable from the app having died.
                iface = None
                misses += 1
                time.sleep(_ANN_RETRY_S if misses <= _ANN_RETRY_FAST_TRIES
                           else _ANN_RETRY_SLOW_S)
                continue
            misses = 0
            if not announced_on:
                # Quiet when the RECORDER is putting announcements back: the stop phrase already
                # says they are back on, and the pump keeps only the newest phrase, so a greeting
                # here would silence it.
                if not _ann_quiet_start.is_set():
                    _ann_say("Control announcer is on.")
                _ann_quiet_start.clear()
                announced_on = True
            elif time.time() - lost_at >= _ANN_RECONNECT_SPEAK_AFTER_S:
                # Back after a real gap. Re-seed first: mutes, levels and the fader layout can all
                # have moved while we were deaf, and Expanded mode reassigns sources as it goes.
                was_usb = st.get("usb_mode")
                _ann_seed_layout(st)
                if _virtual_devices_retrying.is_set():
                    pass        # our own flip of the mode, already announced once as a retry
                else:
                    _ann_say(_ann_usb_mode_change(was_usb, st.get("usb_mode"))
                             or "Control announcer reconnected.")
                    # Out of Expanded with nobody using the app's switch (the touchscreen tidies
                    # its own faders; the desk dropping out on its own does not). On a thread: it
                    # writes, and this loop must keep reading.
                    if (was_usb is not None and st.get("usb_mode") is not None
                            and recorder.split_usb_multitrack(was_usb)[1]
                            and not recorder.split_usb_multitrack(st["usb_mode"])[1]):
                        threading.Thread(target=_tidy_after_standard, args=(True,),
                                         daemon=True, name="expanded-tidy").start()
                    # The desk re-enumerated, whoever caused it: the touchscreen, a profile, or
                    # the desk leaving Expanded on its own. RODE's virtual devices need the same
                    # follow-up either way, and this is the one place that sees every case.
                    _virtual_devices_follow(st.get("usb_mode"), True)
        try:
            rep = dev.read(256, timeout_ms=200)
        except Exception as exc:
            diagnostics.event("device.lost", "announcer read failed", error=f"{exc}")
            rcp_hid.close_handle(dev, "announcer")
            dev = None                                     # reopen on the next pass
            lost_at = time.time()
            time.sleep(0.3)
            continue
        try:
            if rep:
                _ann_handle_report(bytes(rep), st)
            # Runs every read cycle (the read above blocks at most 200 ms), so a held press edge is
            # spoken promptly when the desk sends no state to supersede it.
            _ann_flush_pending(st)
        except Exception:
            # Decoding is best-effort: a malformed or unexpected packet costs us ONE announcement,
            # never the announcer. Silently carrying on beats a thread that dies and leaves the UI
            # claiming announcements are on.
            pass
    if dev is not None:
        rcp_hid.close_handle(dev, "announcer")
    _ann_live_state = None            # stale state must not outlive the announcer
    # A SUSPEND MUST NOT SPEAK. The sign-off is right when the user switched announcements off; it
    # is exactly wrong when the recorder switched them off, because that phrase would be the first
    # thing in the recording - the very thing the suspend exists to prevent.
    if not _ann_quiet_stop.is_set():
        _ann_say("Control announcer is off.")
    _ann_quiet_stop.clear()


# Set while the RECORDER is switching announcements off or back on, so the announcer thread leaves
# and returns without a greeting of its own (see _recorder_state_changed for why that matters).
_ann_quiet_stop = threading.Event()
_ann_quiet_start = threading.Event()
# True when WE suspended announcements for a recording, so they are only ever switched back on if
# we were the ones who switched them off. A user who had them off keeps them off.
_ann_suspended_for_record = False


# ON BY DEFAULT, and it is a CHOICE rather than a policy. Most people do not
# want announcements in the recording, so the default has to be off-while-recording. But somebody
# whose announcements already go to a separate output - a second pair of earbuds, say - is not
# putting anything into the take and would rather keep hearing the unit. Assuming that away would
# silence the operator for the whole show for no reason.
_RECORD_MUTE_DEFAULT = True


def _record_mutes_announcer() -> bool:
    return bool(load_settings().get("record_mute_announcer", _RECORD_MUTE_DEFAULT))


def action_set_record_mute_announcer(on: bool) -> dict:
    """Whether a recording switches announcements off for its duration."""
    on = bool(on)
    update_settings(record_mute_announcer=on)
    if not on:
        # Turning the option off mid-take must give the announcements back straight away, or the
        # switch would appear to do nothing until the recording ended.
        _restore_announcer_after_record()
    return {"ok": True, "mute_announcer": on,
            "message": ("Announcements will be switched off while recording."
                        if on else "Announcements will keep playing while recording.")}


def _suspend_announcer_for_record() -> bool:
    """Switch announcements off for the duration of a recording. Returns True if it did anything.

    WHY SILENCE RATHER THAN A DIFFERENT OUTPUT: sending the speech to the computer's own speakers
    only takes it off the USB return, and anything audible in the room reaches the microphones
    anyway."""
    global _ann_suspended_for_record
    if not _record_mutes_announcer():
        return False                       # the user keeps their announcements; see above
    if not _announce_on.is_set():
        return False                       # already off: leave the user's own choice alone
    _ann_quiet_stop.set()
    _announce_on.clear()
    _ann_suspended_for_record = True
    return True


def _restore_announcer_after_record() -> bool:
    """Switch announcements back on, but ONLY if we were the ones who switched them off."""
    global _ann_suspended_for_record
    if not _ann_suspended_for_record:
        return False
    _ann_suspended_for_record = False
    if _announce_on.is_set():
        return False                       # the user turned them back on mid-take; leave it
    _ann_quiet_start.set()
    action_set_announce(True)
    return True


def action_set_announce(on: bool) -> dict:
    global _announce_thread, _ann_suspended_for_record
    # A DELIBERATE toggle cancels our claim on it either way, so a recording stopping later never
    # overrides what the user has since chosen.
    _ann_suspended_for_record = False
    if on:
        if not _announce_on.is_set():
            _announce_on.set()
            _announce_thread = threading.Thread(target=_announce_loop, daemon=True)
            _announce_thread.start()
        return {"ok": True, "announce": True,
                "message": "Button-press announcements are on."}
    _announce_on.clear()
    return {"ok": True, "announce": False,
            "message": "Button-press announcements are off."}


def action_set_announce_levels(on: bool) -> dict:
    """Whether the announcer also speaks a fader or monitor level once you stop moving it.

    Separate from the button announcements on purpose: mute and solo are events you always want
    spoken, whereas a running commentary on every fader move suits some sessions and not others.
    One phrase per gesture, not per update (see _ANN_SETTLE_S)."""
    global _ann_levels_on
    _ann_levels_on = bool(on)
    update_settings(announcer_levels=_ann_levels_on)
    return {"ok": True, "levels": _ann_levels_on,
            "message": ("Fader and monitor levels will be spoken when you stop moving them."
                        if on else "Fader and monitor levels will not be spoken.")}


def action_set_announce_mute_tones(on: bool) -> dict:
    """Whether a mute or unmute is a TONE rather than a spoken sentence.

    The reasoning is timing: mute is pressed in the middle of something, and a sentence finishes
    after the moment it was describing. A cue of two notes,
    falling for muted and rising for unmuted, lands in a sixth of a second.

    THE TRADE IS THE CHANNEL NAME, which a tone cannot carry. That is why it is an option and why it
    is off by default. Everything else the announcer says is unaffected.

    The confirmation plays the tones themselves, in order, after the sentence: hearing them is the
    only way to learn them, and being told "two tones" teaches nobody which is which.
    """
    global _ann_mute_tones
    _ann_mute_tones = bool(on)
    update_settings(announcer_mute_tones=_ann_mute_tones)
    if on:
        # PLAYED WITH A GAP, and it has to be. The pump coalesces whatever is waiting down to the
        # newest item, which is right for speech (the latest state is the only one worth hearing) and
        # wrong for a pair of cues: queued back to back, the low one was dropped every time and the
        # confirmation taught the wrong half of the lesson. Long enough for the first tone to play
        # (110 ms) and to clear the pump's own pacing.
        _ann_tone("muted")
        time.sleep(max(0.45, _ANN_PACE_S * 2 + tones.DURATION_S))
        _ann_tone("unmuted")
    return {"ok": True, "mute_tones": _ann_mute_tones,
            "message": ("Mutes will play a tone instead of being spoken: two notes falling means "
                        "muted, two notes rising means unmuted."
                        if on else "Mutes will be spoken, naming the channel.")}


def action_set_announce_startup_summary(on: bool) -> dict:
    """Whether the desk's state is spoken when the app opens.

    Only the EXCEPTIONS: what is muted or soloed, a pad left switched on, a recording already
    running, a nearly-full drive, and any output not on its usual mix. On a desk with nothing to
    report it is a single sentence saying so. The full picture stays on the Device page, which is
    where you go when the sentence tells you something is wrong.
    """
    global _ann_startup_summary
    _ann_startup_summary = bool(on)
    update_settings(announcer_startup_summary=_ann_startup_summary)
    return {"ok": True, "startup_summary": _ann_startup_summary,
            "message": ("The desk's state will be spoken when the app opens."
                        if on else "The desk's state will not be spoken when the app opens.")}


def action_set_announce_volume(percent: int) -> dict:
    """How loud announcements are. SAPI stream volume, so this changes OUR speech only and never
    Windows volume, another app, or the desk's audio. The confirmation is spoken, which is the
    point: it arrives at the new level, so you hear what you have chosen straight away."""
    global _ann_volume
    _ann_volume = _clamp_volume(percent)
    update_settings(announcer_volume=_ann_volume)
    msg = (f"Announcement volume {_ann_volume} percent." if _ann_volume
           else "Announcements are silenced.")
    if _ann_volume:
        _ann_say(msg)                     # rides in behind the new volume; see _ann_speak_pump
    return {"ok": True, "volume": _ann_volume, "message": msg}


def _ann_pick(value: str, offered: list) -> str:
    """Resolve what the page sent against what SAPI actually offers. An exact name wins; failing
    that a case-insensitive substring is accepted, so a shortened name ("Zira", "Realtek") still
    works from a script or an older saved setting. Anything else is rejected rather than passed to
    the worker, where it would silently fall back to the default and look like the setting was
    ignored. "" means "leave it to Windows"."""
    value = (value or "").strip()
    if not value:
        return ""
    for name in offered:
        if name == value:
            return name
    low = value.lower()
    for name in offered:
        if low in name.lower():
            return name
    return None


def action_set_announce_voice(voice: str) -> dict:
    """Which voice speaks announcements. The confirmation is spoken IN THE NEW VOICE, which is the
    point: the worker is restarted with it and the sample is the first thing it says."""
    global _ann_voice, _ann_worker_gen
    offered = _sapi_lists()["voices"]
    picked = _ann_pick(voice, offered)
    if picked is None:
        return {"ok": False, "voice": _ann_voice,
                "message": f"This computer has no speech voice called '{voice}'."}
    _ann_voice = picked
    _ann_worker_gen += 1
    update_settings(announcer_voice=_ann_voice)
    msg = (f"Announcements will now be spoken by {picked}." if picked
           else "Announcements will now use the computer's default voice.")
    _ann_say(msg)
    return {"ok": True, "voice": _ann_voice, "message": msg}


def action_set_announce_output(output: str) -> dict:
    """Which Windows output announcements play through. THIS IS OUR SPEECH ONLY: it sets the audio
    output on our own SAPI voice, so it does not move the system default device, and it does not
    touch the screen reader or any other program. It exists because this desk's default output is
    the RODECaster itself, so announcements vanish the moment another channel is soloed."""
    global _ann_say_device, _ann_worker_gen
    offered = _sapi_lists()["outputs"]
    picked = _ann_pick(output, offered)
    if picked is None:
        return {"ok": False, "output": _ann_say_device,
                "message": f"This computer has no audio output called '{output}'."}
    _ann_say_device = picked
    _ann_worker_gen += 1
    update_settings(announcer_output=_ann_say_device)
    msg = (f"Announcements will now play through {picked}." if picked
           else "Announcements will now play through the computer's default output.")
    _ann_say(msg)
    return {"ok": True, "output": _ann_say_device, "message": msg}


def action_refresh_speech_devices() -> dict:
    """Re-read the voice and output lists from SAPI. Plugging in headphones or installing a voice
    changes them, and neither is worth restarting the app for."""
    lists = _sapi_lists(refresh=True)
    return {"ok": True, "voices": lists["voices"], "outputs": lists["outputs"],
            "message": f"{len(lists['voices'])} voices and {len(lists['outputs'])} outputs found."}


def action_set_gain(channel: int, db: int) -> dict:
    if channel not in MIC_CHANNELS:
        return {"ok": False, "message": f"Mic channel {channel} is not valid."}
    if db < GAIN_MIN_DB or db > GAIN_MAX_DB:
        return {"ok": False, "message": f"Gain must be between {GAIN_MIN_DB} and "
                                        f"{GAIN_MAX_DB} dB."}
    with _hid_lock:
        channels.set_mic_gain(channel, db)
    _ann_mark_self_gain(channel, True)     # the desk echoes this; the UI already reports it
    with _session_state_lock:
        _session_state["channels"][channel]["gain_db"] = db
    return {"ok": True, "message": f"Mic {channel} gain set to {db} dB."}


def action_set_phantom(channel: int, on: bool) -> dict:
    if channel not in MIC_CHANNELS:
        return {"ok": False, "message": f"Mic channel {channel} is not valid."}
    with _hid_lock:
        channels.set_phantom(channel, on)
    with _session_state_lock:
        _session_state["channels"][channel]["phantom"] = on
    return {"ok": True,
            "message": f"Mic {channel} 48V phantom power turned {'on' if on else 'off'}."}


def action_set_polarity(channel: int, on: bool) -> dict:
    if channel not in MIC_CHANNELS:
        return {"ok": False, "message": f"Mic channel {channel} is not valid."}
    with _hid_lock:
        channels.set_polarity(channel, on)
    with _session_state_lock:
        _session_state["channels"][channel]["polarity"] = on
    return {"ok": True,
            "message": f"Mic {channel} polarity {'inverted' if on else 'normal'}."}


def _mic_processing_snapshot(channel: int) -> dict:
    """{param/module name: value} for one mic's strip, off one dump, as the Channels page shows it.

    Deliberately the SAME reader the page uses (channels.read_channels), so a before/after diff here
    can never disagree with what the user is looking at."""
    try:
        recs = channels.read_channels()
    except Exception:
        return {}
    entry = next((c for c in channels.active_ui_channels()
                  if c.get("kind") == "mic" and channels.mic_channel_number(c) == channel), None)
    rec = next((r for r in recs if entry and r.get("name") == entry.get("name")), None)
    if rec is None:
        return {}
    out = dict(rec.get("params") or {})
    out.update(rec.get("modules") or {})
    return out


def action_set_mic_type(channel: int, type_value: int) -> dict:
    """Set a mic's voicing, AND SAY WHAT IT DID TO THE GAIN AND THE PROCESSING CHAIN.

    CHOOSING A VOICING SETS THAT VOICING'S OWN DEFAULT GAIN - the desk does it, not us (measured:
    Dynamic 45, Condenser 25, PodMic 50, Broadcaster 27, Procaster 50, NT1 30, NT1-A 31, RE20 51,
    SM7B 55; Line-in and Instrument leave it alone).
    Without the read-back below, the gain control keeps showing the OLD number and nothing is
    announced, so a screen-reader user is told the voicing changed and never learns their preamp
    moved by up to 30 dB. Costs one dump on a deliberate, rare action.

    IT ALSO REPLACES THE WHOLE PROCESSING CHAIN on a Pro II - 13 params measured (Depth, Sparkle,
    Punch, compressor threshold + gain, de-esser, gate threshold + range, all three EQ gains, both
    Aphex) - and writing the old type back does NOT undo it. A Duo mic moves the gain only. None of
    it is undone here on purpose: the voicing's own chain is what the user asked for by naming their
    microphone, and it is what the desk and the RODE app both do. What must not happen is saying
    nothing, which leaves a screen-reader user with a panel full of stale numbers and no idea their
    compressor moved. So it is COUNTED and ANNOUNCED, exactly as the gain is, and the client re-reads
    the panel on success."""
    if channel not in MIC_CHANNELS:
        return {"ok": False, "message": f"Mic channel {channel} is not valid."}
    with _session_state_lock:
        was = _session_state["channels"][channel].get("gain_db")
    # ALL THREE READS INSIDE THE LOCK, like every other read that has to line up with a write of
    # ours: the readers do not take _hid_lock themselves, the caller does.
    with _hid_lock:
        proc_before = _mic_processing_snapshot(channel)
        channels.set_mic_type(channel, type_value)
        gain, _proc = _read_gain_and_processing(channel)
        proc_after = _mic_processing_snapshot(channel)
    with _session_state_lock:
        _session_state["channels"][channel]["mic_type"] = type_value
        if gain is not None:
            _session_state["channels"][channel]["gain_db"] = gain
    name = channels.MIC_TYPE.get(type_value, f"type {type_value}")
    msg = f"Mic {channel} input type set to {name}."
    if gain is not None and gain != was:
        msg += f" The unit moved the gain to {gain} dB for this microphone."
    moved = sorted(k for k, v in proc_after.items()
                   if k in proc_before and proc_before[k] != v)
    if moved:
        msg += (f" The unit also replaced this channel's processing with the {name} voicing: "
                f"{len(moved)} setting{'' if len(moved) == 1 else 's'} changed, so anything you had "
                f"set here by hand is gone.")
    return {"ok": True, "message": msg, "gain_db": gain, "processing_changed": len(moved)}


# GAP-8: there are NO Pro II-shaped, mic-1..4-only processing endpoints. app.js drives the
# device-aware unified Channels view (/api/channels + /api/channels/param|module|...), which
# resolves per-channel selectors from the active device (channels.proc_selector). The shared helpers
# (PROCESSING_CONTROLS, _PROCESSING_PROP_SET, preset_list) belong to that view.


# --- Unified Channels view (every processing-capable strip the device exposes: on the Pro II the
# --- four mics, BT/USB1/Chat/USB2, and the SMART pads bus) --------------------------------------
# One adaptive panel per channel, mirroring the RODE app: mic channels add gain/phantom/polarity/
# mic-type/effects; line channels (BT/USB1/Chat/USB2) have Level + processing. Processing (modules
# + params) applies to every channel here (selectors decoded and device-proven).
# Resolve a channel name against the CONNECTED device each call (not a static Pro II map): the
# Duo exposes "Headset"/"USB1"/... instead of "Mic 3"/"Mic 4", so a fixed map built at import time
# would reject the Duo's channels. channels.ui_channel_map() reads the active device's ui_channels.
def _ui_chan(name):
    return channels.ui_channel_map().get(name)
# Module on/off toggles shown in the UI, with screen-reader labels.
PROCESSING_MODULES = [
    {"key": "hpf", "label": "High-pass filter"}, {"key": "eq", "label": "Equaliser"},
    {"key": "compressor", "label": "Compressor"}, {"key": "noiseGate", "label": "Noise gate"},
    {"key": "deesser", "label": "De-esser"}, {"key": "aphex", "label": "Aphex exciter"},
    {"key": "pan", "label": "Pan"},
]


def action_get_channels() -> dict:
    """Read every UI channel's full adaptive state (one model dump). Params are 0-100 percent ints."""
    try:
        with _hid_lock:
            # ONE DUMP FOR BOTH. The wireless-mic slots ride the same read as the channels so the
            # "Mute the wireless mic" switches can be made unavailable when the unit reports no
            # transmitter, without a second trip to the desk for a fact we already have in hand.
            blob = rcp_read.read_model_stable()
            chans = channels.read_channels(blob)
    except Exception as exc:
        return {"ok": False, "message": f"Could not read channels from the device: {exc}"}
    # Which preset each mic strip is on. BEFORE the percent conversion below, because the comparison
    # is against the preset's own normalised 0..1 values (see _preset_now).
    for c in chans:
        if c.get("kind") == "mic":
            ui = _ui_chan(c.get("name"))
            if ui is not None:
                c["preset_now"] = _preset_now(channels.mic_channel_number(ui), c)
    for c in chans:
        c["params"] = {p: (round(v * 100) if v is not None else None)
                       for p, v in c["params"].items()}
    return {
        "ok": True, "channels": chans, "groups": PROCESSING_CONTROLS,
        "wireless_mics": _wireless_mics(blob),
        "modules": PROCESSING_MODULES, "mic_types": sorted([list(kv) for kv in channels.MIC_TYPE.items()]),
        "presets": preset_list(),
        "gain_min_db": GAIN_MIN_DB, "gain_max_db": GAIN_MAX_DB,
        "level_min_db": LEVEL_MIN_DB, "level_max_db": LEVEL_MAX_DB,
        "message": "Read all channels from the device.",
    }


def action_set_channel_level(name: str, db: int) -> dict:
    """Set a channel's Level: mics use mic gain (0-76 dB); line channels use inputDigitalGain."""
    ch = _ui_chan(name)
    if ch is None:
        return {"ok": False, "message": f"Unknown channel: {name}."}
    if ch["kind"] == "mic":
        return action_set_gain(channels.mic_channel_number(ch), db)
    if db < LEVEL_MIN_DB or db > LEVEL_MAX_DB:
        return {"ok": False, "message": f"Level must be between {LEVEL_MIN_DB} and {LEVEL_MAX_DB} dB."}
    with _hid_lock:
        channels.set_channel_level(name, db)
    _ann_mark_self_gain(name, False)       # line strips push inputDigitalGain back at us
    return {"ok": True, "message": f"{ch['label']} level set to {db} dB."}


def action_set_channel_mute(name: str, on: bool) -> dict:
    """Mute or unmute a channel from software (channelOutputMute, proven writable on hardware).

    The real accessibility win here is reach: this is the same mute as the button on the unit, so it
    can now be pressed from a phone or another room instead of by finding the right button by touch.

    Two details matter. It silences the channel in EVERY mix, not just the monitor (the firmware
    propagates it into the mix matrix), so the message says so plainly rather than implying it is a
    local monitor mute. And the announcer is told about the write: without _ann_publish_mute the
    encoder ring would keep quoting a level for a channel that is now silent, and the desk's echo of
    the mute would be spoken on top of the UI's own announcement."""
    ch = _ui_chan(name)
    if ch is None:
        return {"ok": False, "message": f"Unknown channel: {name}."}
    try:
        with _hid_lock:
            channels.set_channel_mute(name, on)
            strip = channels.channel_strip_index(name)
    except Exception as exc:
        return {"ok": False, "message": f"Could not change the mute: {exc}"}
    _ann_publish_mute(strip, on)
    return {"ok": True,
            "message": (f"{ch['label']} muted; it is silent in every mix."
                        if on else f"{ch['label']} unmuted.")}


def _strip_toggle_gate(what: str):
    """Per-strip MUTE and SOLO writes, gated on "processing" and NOT on the level gate.

    They are compact bools on the channel's own processing selector, so the capability that decides
    them is the same one that decides every other module toggle. Tying them to _level_write_gate
    conflates two unrelated things: the Duo's faderLevel selectors are unconfirmed, which says
    nothing about its mute. It also leaves the UI hiding a control the HTTP endpoint still accepts,
    which is the wrong way round on a device where the write is unproven.

    The Channels page's by-name mute reaches the same gate through channels._require_processing;
    this is the by-fader route's half, so the two cannot drift apart."""
    return _cap_gate("processing", lambda name:
                     f"{what} a channel is not enabled on the {name} yet, so this is read-only "
                     f"here. You can still see which channels are muted or soloed.")


def action_set_fader_mute(fader: int, on: bool) -> dict:
    """Mute whatever is on one fader, addressed by FADER rather than by channel name.

    Same property and the same announcer publish as action_set_channel_mute; only the way in
    differs. It exists because the Levels tab's rows ARE strips, and two of the things a strip can
    hold have no channel name to look up: an EMPTY fader, and a stereo-linked PAIR (its own source
    code, deliberately absent from the assignable channel list). Routing those through a by-name
    call would mean either refusing to mute a linked pair or teaching the channel table about pairs,
    and the strip is what the desk mutes anyway.

    Named for the fader because that is what the user pressed. The reply names the CHANNEL, because
    a virtual fader is only ever meaningful as whatever is currently sitting on it."""
    gate = _strip_toggle_gate("Muting")
    if gate is not None:
        return gate
    spec = _active_faders()
    if not 0 <= int(fader) < spec.count:
        return {"ok": False, "message": f"Fader {int(fader) + 1} is not valid."}
    try:
        with _hid_lock:
            vec = _read_fader_vector(spec=spec)
            code = vec[int(fader)] if int(fader) < len(vec) else None
            if _is_empty_fader(code):
                return {"ok": False,
                        "message": (f"{_fader_label(int(fader), spec)} has no channel on it, so "
                                    f"there is nothing to mute. Put a channel on it first.")}
            rcp_transfer.send_record(rcp_transfer.build_global_prop_bool(
                channels.MUTE_PROP, bool(on), channels.proc_selector_for_strip(int(fader))))
    except Exception as exc:
        return {"ok": False, "message": f"Could not change the mute: {exc}"}
    _ann_publish_mute(int(fader), bool(on))
    who = f"{_channel_name(code, spec)} on {_fader_label(int(fader), spec)}"
    return {"ok": True, "muted": bool(on),
            "message": (f"{who} muted; it is silent in every mix." if on else f"{who} unmuted.")}


def action_set_fader_solo(fader: int, on: bool) -> dict:
    """Solo ("Listen") whatever is on one fader. The solo twin of action_set_fader_mute, and it
    exists for the same reason: a Levels row IS a strip, and an empty fader or a stereo-linked pair
    has no channel name to look up.

    IT KEEPS THE SAFETY WORDING, which is the whole condition on offering solo in a second place. A
    solo replaces the headphone feed, so left on by accident the desk sounds broken with nothing
    saying why - and a switch that only knew about its own fader would be exactly that trap. The
    reply names everything the headphones now carry, not just this one, and the Levels tab carries
    the same one-press Clear all solos that the Channels page has."""
    gate = _strip_toggle_gate("Solo")
    if gate is not None:
        return gate
    spec = _active_faders()
    if not 0 <= int(fader) < spec.count:
        return {"ok": False, "message": f"Fader {int(fader) + 1} is not valid."}
    try:
        with _hid_lock:
            vec = _read_fader_vector(spec=spec)
            code = vec[int(fader)] if int(fader) < len(vec) else None
            if _is_empty_fader(code):
                return {"ok": False,
                        "message": (f"{_fader_label(int(fader), spec)} has no channel on it, so "
                                    f"there is nothing to solo.")}
            rcp_transfer.send_record(rcp_transfer.build_global_prop_bool(
                channels.CUE_PROP, bool(on), channels.proc_selector_for_strip(int(fader))))
    except Exception as exc:
        return {"ok": False, "message": f"Could not change the solo: {exc}"}
    _ann_publish_solo(int(fader), bool(on))
    who = f"{_channel_name(code, spec)} on {_fader_label(int(fader), spec)}"
    return {"ok": True, "soloed": bool(on),
            "message": (f"{who} soloed; the headphones now carry "
                        f"{_solo_summary(int(fader), True)}. The mixes, the recording and anything "
                        f"you are streaming are untouched."
                        if on else
                        f"{who} solo off. {_solo_after_clear(int(fader))}")}


def action_set_channel_solo(name: str, on: bool) -> dict:
    """Solo ("Listen") a channel from software, or clear it (channelCueEnable, proven on hardware).

    The reach argument is the same as the mute's, but the SAFETY argument is stronger and shapes the
    message. A solo replaces the headphone feed: leave one on by accident and the desk sounds broken
    with nothing on screen to say why, and it is exactly the sort of state a blind operator cannot
    spot by glancing at a lit button. So the reply always says how many solos are now active and
    names them, and there is a one-press way out (action_clear_solos)."""
    ch = _ui_chan(name)
    if ch is None:
        return {"ok": False, "message": f"Unknown channel: {name}."}
    try:
        with _hid_lock:
            channels.set_channel_solo(name, on)
            strip = channels.channel_strip_index(name)
    except Exception as exc:
        return {"ok": False, "message": f"Could not change the solo: {exc}"}
    _ann_publish_solo(strip, on)
    return {"ok": True, "message": (f"{ch['label']} soloed; the headphones now carry "
                                    f"{_solo_summary(strip, True)}. Nothing else has changed: the "
                                    f"mixes, the recording and anything you are streaming are "
                                    f"untouched."
                                    if on else
                                    f"{ch['label']} solo off. {_solo_after_clear(strip)}")}


def action_set_channel_wireless_mute(name: str, on: bool) -> dict:
    """Mute or unmute a RODE wireless mic paired to a channel (channelWirelessMute).

    NOT the same control as action_set_channel_mute, and the reply has to keep them apart or it is
    worse than no control at all: that one silences the strip inside the desk, this one asks the
    TRANSMITTER to stop sending. Mute the strip and the desk still receives; mute the transmitter and
    nothing arrives at all, which is the one you want when somebody walks off still wearing it.

    THE REPLY SAYS WHAT WAS SENT, NOT WHAT HAPPENED, and that is deliberate. The write is addressed
    correctly (the property sits on the same CHANNEL node as the mute, measured) but nobody here owns
    a transmitter, so it has never been heard to work, and a channel with nothing paired has nothing
    to obey it. Claiming "the mic is muted" would be the app reading its own write back as an
    outcome, which is the fault that hid the Duo's dead gain address for two months."""
    ch = _ui_chan(name)
    if ch is None:
        return {"ok": False, "message": f"Unknown channel: {name}."}
    if ch.get("kind") != "mic":
        return {"ok": False, "message": f"{ch['label']} is not a mic channel, so no wireless mic "
                                        f"can be paired to it."}
    try:
        with _hid_lock:
            channels.set_channel_wireless_mute(name, on)
    except Exception as exc:
        return {"ok": False, "message": f"Could not change the wireless mic mute: {exc}"}
    return {"ok": True,
            "message": (f"Mute sent to the wireless mic on {ch['label']}. If a RODE wireless "
                        f"transmitter is paired there it should now be silent; the channel itself "
                        f"is not muted."
                        if on else
                        f"Unmute sent to the wireless mic on {ch['label']}.")}


def action_clear_solos() -> dict:
    """Clear every solo in one go. The way out of the state above without having to find which
    channel caused it - and the reply says plainly when there was nothing to clear, rather than
    reporting a cheerful success for a no-op."""
    try:
        with _hid_lock:
            cleared = channels.clear_all_solos()
    except Exception as exc:
        return {"ok": False, "message": f"Could not clear the solos: {exc}"}
    if not cleared:
        return {"ok": True, "message": "Nothing was soloed; the headphones already carry the "
                                       "full monitor mix."}
    _ann_forget_solos()
    return {"ok": True, "message": (f"{cleared} solo{'s' if cleared != 1 else ''} cleared; the "
                                    f"headphones carry the full monitor mix again.")}


def _solo_live() -> dict:
    """{strip: bool} for every strip the desk reports, or {} if it cannot be read. Never raises:
    this only ever decorates a message, and a message is not worth failing a write over."""
    try:
        with _hid_lock:
            return {i: bool(v) for i, v in enumerate(channels.read_solos())}
    except Exception:
        return {}


def _solo_summary(strip: int, on: bool) -> str:
    """Name what the headphones carry, with the write we just made folded in - the desk's own read
    can lag a write by a moment, so trusting it alone would sometimes report the state we just left."""
    live = _solo_live()
    live[strip] = on
    names = [_ann_channel_label(i) for i, v in sorted(live.items()) if v]
    if not names:
        return "the full monitor mix"
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _solo_after_clear(strip: int) -> str:
    left = _solo_summary(strip, False)
    return ("The headphones are back to the full monitor mix."
            if left == "the full monitor mix"
            else f"Still soloed: {left}.")


def _ann_publish_solo(strip_idx: int, on: bool) -> None:
    """Mark a solo WE wrote, so a future firmware that echoes it does not get it spoken twice.

    Measured: this desk pushes NOTHING back for a software solo, so the mark is a no-op on it. It is
    here anyway because the announcer's solo branch speaks every channelCueEnable push it sees, so a
    firmware that starts echoing would have a channel announcing its own solo on top of the UI, the
    same bug the level path already carries a fix for."""
    try:
        _ann_mark_self_write(("solo", int(strip_idx)))
    except (TypeError, ValueError):
        pass


def _ann_forget_solos() -> None:
    """Mark every strip's solo as ours, for the clear-all path: one call can flip many strips, and
    each would otherwise be a separate push to speak over the single message the UI gives."""
    for strip in range(16):
        _ann_mark_self_write(("solo", strip))


# ----------------------------------------------------------------------------
# AUTO GAIN. Set a microphone's gain by measuring it.
#
# The one studio job that is purely visual - talk, watch a meter, turn the knob - which is why it is
# worth building rather than another spoken meter that a listener has to interpret. The measurement
# rules, and why each one exists, are in autogain.py.
#
# IT RUNS ON A THREAD with a status endpoint, like the profile jobs, because it takes half a minute
# and the page has to stay answerable throughout. Every device change it makes is recorded so it
# can be put back: the channel's processing bypass is ALWAYS restored, and the gain can be undone
# from the result.

_AUTOGAIN_SECONDS = 30.0
# The second, shorter take that checks the new gain. Ten seconds is enough for a few sentences and
# short enough that nobody minds being asked twice.
_AUTOGAIN_VERIFY_S = 10.0
_autogain_lock = threading.Lock()
_autogain = None            # the running or finished job, as a plain dict


def _autogain_state() -> dict:
    with _autogain_lock:
        return dict(_autogain) if _autogain else {"phase": "idle"}


def _autogain_set(**changes) -> None:
    with _autogain_lock:
        if _autogain is not None:
            _autogain.update(changes)


def _mic_number(name: str):
    """The 1-based mic number for a UI channel key ("Mic2" -> 2), or None if it is not a mic.
    Only microphone channels have a preamp gain to set."""
    if not str(name).startswith("Mic"):
        return None
    try:
        n = int(str(name)[3:])
    except ValueError:
        return None
    return n if n in MIC_CHANNELS else None


def _read_gain_and_processing(mic: int):
    """(gain dB, processing on) for one mic, read from the unit in one dump.

    The gain comes from the mic's own ADDRESS (channels.input_value), not from position N-1 in the
    inputMicrophoneGain list. Processing stays positional because it is per-STRIP, but the strip is
    resolved from the live fader vector rather than assumed to be mic-1, or autogain reports the
    wrong strip's bypass state the moment the faders are reassigned.
    """
    blob = rcp_read.read_model_stable(fresh=True)
    entry = next((c for c in channels.active_ui_channels()
                  if c.get("kind") == "mic" and channels.mic_channel_number(c) == mic), None)
    if entry is None:
        return None, None
    gain = channels.input_value(blob, entry, "inputMicrophoneGain")
    bypass = rcp_read.values(blob, "channelBypassProcessing")
    # Resolve the strip from THIS dump, so the gain and the bypass state describe the same moment.
    strip = entry.get("proc_idx")
    if strip is None:
        vec = rcp_read.values(blob, "channelInputSource")
        channels.publish_strip_vector(vec)
        try:
            strip = vec.index(entry.get("source_code"))
        except ValueError:
            strip = None            # this mic is not on a fader right now
    on = (not bypass[strip]) if (strip is not None and 0 <= strip < len(bypass)) else None
    return gain, on


def _autogain_run(name: str, mic: int, seconds: float) -> None:
    """The whole job: prepare, listen in silence, decide, apply, restore, report."""
    restore_processing = False
    try:
        _autogain_set(phase="preparing", message="Getting ready. Do not talk yet.")
        # 1. WINDOWS OUT OF THE WAY. Its level scales everything we are about to measure, and a
        #    Windows mute would make the microphone look dead - which is exactly what it did on
        #    this machine today.
        win = ""
        stream = _meter_stream()
        if stream:
            try:
                if meters.endpoint_volume(stream).get("muted"):
                    meters.set_endpoint_mute(stream, False)
                    win = "Cleared the Windows mute. "
                if meters.set_endpoint_volume_db(stream, 0.0).get("ok"):
                    win += "Windows level set to no change. "
            except Exception:
                pass

        with _hid_lock:
            gain_before, proc_on = _read_gain_and_processing(mic)
        if gain_before is None:
            _autogain_set(phase="failed", done=True,
                          message="Could not read that microphone's gain from the unit.")
            return
        _autogain_set(gain_before=gain_before, processing_before=proc_on)

        # 2. PROCESSING OFF. The meter reads the unit's output, which is AFTER the compressor and
        #    limiter, so with processing on we would be measuring the safety net and not the gain.
        if proc_on:
            with _hid_lock:
                channels.set_processing_enabled(mic, False)
            restore_processing = True

        endpoint = autogain.chat_endpoint()
        if endpoint is None:
            raise RuntimeError("the unit's Chat audio device is not available on this computer")
        # Windows out of the way on the endpoint we ACTUALLY measure. Its own level scales the
        # reading and its own mute would make the microphone look dead.
        try:
            meters.set_endpoint_mute(endpoint["id"], False)
            meters.set_endpoint_volume_db(endpoint["id"], 0.0)
        except Exception:
            pass
        check = None
        with _hid_lock:
            with _ChatIsolation(mic - 1) as iso:
                # THE CUE COMES FIRST, AND IT FINISHES FIRST - not because it would otherwise be
                # measured (it plays out on USB 1, and the isolated mix excludes everything except
                # the microphone, so it cannot reach the measurement) but because the person should
                # not start talking over their own start signal. Only an
                # acoustic path through monitor speakers could put it in the take, which is a
                # different problem and not one to slow this down for.
                meters.cue("start")
                _autogain_set(phase="listening", seconds=seconds, started=time.time(),
                              message="Speak now.")
                result = autogain.measure_endpoint(
                    endpoint["id"], seconds,
                    should_stop=lambda: _autogain_state().get("cancel"))
                meters.cue("stop")
                # POST-FADER, corrected: the cross-point carries the channel at its own level, so
                # the gain we derive must not move with the fader.
                result = autogain.correct_for_level(result, iso.level)

                decision = autogain.verdict(result)
                target = gain_before
                if decision["ok"]:
                    target = max(GAIN_MIN_DB, min(GAIN_MAX_DB,
                                                  int(round(gain_before + decision["delta_db"]))))
                    if target != gain_before:
                        channels.set_mic_gain(mic, target)
                        _ann_mark_self_gain(mic, True)
                        with _session_state_lock:
                            _session_state["channels"][mic]["gain_db"] = target
                        time.sleep(0.4)
                    # CHECK ITS OWN WORK: set the gain from the results, then measure again.
                    # Without the second take the result is a PREDICTION rather than a measurement,
                    # and nothing tells the user whether it landed. The isolation is still open, so
                    # it costs nothing but the ten seconds.
                    meters.cue("start")
                    _autogain_set(phase="verifying", seconds=_AUTOGAIN_VERIFY_S,
                                  started=time.time(),
                                  message="Speak again, to check.")
                    check = autogain.correct_for_level(
                        autogain.measure_endpoint(
                            endpoint["id"], _AUTOGAIN_VERIFY_S,
                            should_stop=lambda: _autogain_state().get("cancel")),
                        iso.level)
                    meters.cue("stop")
            if not iso.restored:
                iso.restore()
        _autogain_set(phase="working", message="Working it out.")

        if not decision["ok"]:
            _autogain_set(phase="failed", done=True, result=result, message=decision["reason"])
            return
        _autogain_set(phase="done", done=True, gain_after=target, result=result, check=check,
                      message=_autogain_words(name, gain_before, target, result, win, check))
    except Exception as exc:
        _autogain_set(phase="failed", done=True, message=f"Auto gain stopped: {exc}")
    finally:
        # THE PROCESSING IS ALWAYS PUT BACK, whatever went wrong, and the restore is VERIFIED.
        # It was best-effort and silent in the first version, and on the very first run it failed
        # and left Mic 1 unprocessed with nothing said about it. That is the worst possible way for
        # this to fail: the operator sounds different and there is no sign of why. So it reads back,
        # retries, and if it still cannot be put back it says so LOUDLY in the result rather than
        # swallowing the error.
        if restore_processing and not _autogain_restore_processing(mic):
            st = _autogain_state()
            _autogain_set(phase="failed", done=True,
                          message=((st.get("message") or "") + " WARNING: processing could not be "
                                   "switched back on for that microphone. Switch it on yourself on "
                                   "this page, or that channel will sound unprocessed.").strip())


class _ChatIsolation:
    """Make the Chat output carry ONE source for the length of a measurement, then put it back.

    SHAUN'S IDEA, and it replaced a much worse one. The first version isolated the channel with the
    Main Multitrack USB device, and switching that does not merely add an endpoint - it makes the
    unit RE-ENUMERATE ITS WHOLE USB AUDIO FUNCTION. Measured: all four desk endpoints go inactive
    for a second or two, Windows falls back to the laptop's own sound card (which is where his
    screen reader suddenly came from), and on the way back Windows picks a different default.
    Twice per run. Unusable before a show.

    A custom mix costs none of that, because nothing about the USB audio function changes. It is
    the same operation as the "Chat carries only Mic 1" preset this app already has, so it uses
    exactly the same calls, plus a snapshot and a verified restore.

    CHAT because it returns to the computer as its own capture endpoint, it is not the main mix, and
    a mix that is not the main one is what that output is for.

    IT IS POST-FADER, and that is corrected rather than ignored: the cross-point level is read and
    subtracted from the measurement, so the gain we work out does not move with the fader. Reading
    it is safer than writing the cross-point to unity and having that to put back as well.
    """

    def __init__(self, keep_source: int):
        self.keep = keep_source
        self.chat = rcp_transfer.CROSS_POINT_OUTPUT_INDEX["Chat"]
        # `all_sources`: isolating one channel means every OTHER row out, and in Expanded four of
        # them carry the computer's own audio. Leaving those in measures the screen reader.
        self.sources = [x["index"] for x in _active_matrix().all_sources]
        self.level = 1.0
        self.mode_before = None
        self.excluded_before = {}
        self.restored = False

    def _snapshot(self):
        blob = rcp_read.read_model_stable(fresh=True)
        m = devices.get_active_device().matrix
        excl = [bool(v) for v in rcp_read.block_values(blob, m.block, "mixDisabled", "bool")]
        if len(excl) != m.cell_count:
            raise RuntimeError("the unit's mix grid is not the shape this expects "
                               "(a phone caller may be connected)")
        got = {src: excl[m.row_stride * src + self.chat] for src in self.sources}
        lv = rcp_read.read_matrix_levels(blob)
        if not lv.get("ok"):
            raise RuntimeError(lv.get("reason") or "could not read the mix levels")
        return lv["modes"].get(self.chat), got, lv["levels"].get(self.chat, {})

    def __enter__(self):
        self.mode_before, self.excluded_before, levels = self._snapshot()
        cell = levels.get(self.keep) or {}
        self.level = float(cell.get("level") or 0.0)
        if self.level < _AUTOGAIN_MIN_CROSSPOINT:
            raise RuntimeError("that channel is not reaching the Chat mix - its fader is down, or "
                               "it is excluded there - so nothing could be measured through it")
        if self.mode_before != 2:
            rcp_transfer.set_output_routing(self.chat, 2)      # Custom
            time.sleep(0.3)
        for src in self.sources:
            want = (src != self.keep)
            if self.excluded_before.get(src) != want:
                rcp_transfer.set_cross_point_excluded(self.chat, src, want)
                time.sleep(0.05)
        time.sleep(0.6)                # let the mix settle before anything is measured
        return self

    def __exit__(self, *exc):
        self.restore()
        return False

    def restore(self) -> bool:
        """Put the Chat mix back exactly, and CHECK that it went back.

        Verified rather than assumed, for the same reason the processing restore is: leaving
        somebody's Chat output carrying one microphone would break the next call they took, and
        they would have no way of knowing why."""
        if self.restored or self.mode_before is None:
            return True
        try:
            for src in self.sources:
                rcp_transfer.set_cross_point_excluded(self.chat, src,
                                                      self.excluded_before.get(src, False))
                time.sleep(0.05)
            if self.mode_before != 2:
                rcp_transfer.set_output_routing(self.chat, self.mode_before)
            time.sleep(0.6)
            mode_now, excl_now, _levels = self._snapshot()
            self.restored = (mode_now == self.mode_before
                             and excl_now == self.excluded_before)
            return self.restored
        except Exception:
            return False


# A cross-point quieter than this cannot be measured through: correcting for it would multiply the
# noise floor up rather than the voice.
_AUTOGAIN_MIN_CROSSPOINT = 0.05


def _autogain_restore_processing(mic: int) -> bool:
    """Switch a mic's processing back on and CHECK it took. Returns True when it is really on.

    Read back rather than assumed: the write is an ordinary property write and can fail while the
    unit is busy, which is precisely what happened on the first run of this feature.
    """
    for attempt in range(3):
        try:
            with _hid_lock:
                channels.set_processing_enabled(mic, True)
                time.sleep(0.4)
                blob = rcp_read.read_model_stable(fresh=True)
            bypass = rcp_read.values(blob, "channelBypassProcessing")
            if len(bypass) >= mic and not bypass[mic - 1]:
                return True
        except Exception:
            pass
        time.sleep(0.5 * (attempt + 1))
    return False


def _autogain_words(name: str, before: int, after: int, result: dict, win: str,
                    check=None) -> str:
    """The result, said the way it would be said out loud.

    IT REPORTS THE SECOND TAKE, not a prediction. A closing line like "talk again and it should sit
    around minus 9" promises a result nothing has measured AND reads as an instruction to speak while
    nothing is listening. By the time this runs the second take has happened, so it says what was
    actually heard."""
    label = (_ui_chan(name) or {}).get("label", name)
    was = f"You were peaking at {meters.dbfs(result['loud'])}."
    if after == before:
        head = f"{label} was already right, so the gain is still {before} dB. {was}"
    else:
        way = "down" if after < before else "up"
        head = f"{label} gain turned {way} from {before} to {after} dB. {was}"
    if not check:
        return f"{win}{head}"
    now = check["loud_db"]
    at = f"Checking again, you peaked at {meters.dbfs(check['loud'])}."
    if check["loud_db"] < autogain.MIN_USABLE_DBFS:
        return f"{win}{head} {at} That was too quiet to confirm - nothing was heard on the check."
    off = now - autogain.TARGET_DBFS
    if abs(off) <= 3.0:
        return f"{win}{head} {at} That is right."
    way = "loud" if off > 0 else "quiet"
    return (f"{win}{head} {at} Still about {abs(off):.0f} dB too {way}. Run it again, speaking as "
            f"you did the first time, or set the gain yourself.")


def action_autogain_start(name: str, seconds: float = 0.0) -> dict:
    """Begin an auto-gain run on one mic channel."""
    global _autogain
    dev = devices.get_active_device()
    if "input_mic_write" not in dev.caps:
        return {"ok": False, "message": f"Mic gain cannot be set on the {dev.name} from here."}
    mic = _mic_number(name)
    if mic is None:
        return {"ok": False, "message": "Only microphone channels have a gain to set."}
    with _autogain_lock:
        if _autogain and not _autogain.get("done"):
            return {"ok": False, "message": "Auto gain is already running."}
        _autogain = {"phase": "starting", "channel": name, "done": False, "cancel": False,
                     "message": "Starting."}
    secs = float(seconds) if seconds and seconds > 0 else _AUTOGAIN_SECONDS
    threading.Thread(target=_autogain_run, args=(name, mic, max(5.0, min(120.0, secs))),
                     daemon=True).start()
    return {"ok": True, "message": "Auto gain started.", "seconds": secs}


def autogain_status() -> dict:
    st = _autogain_state()
    st["ok"] = True
    started, secs = st.get("started"), st.get("seconds")
    if st.get("phase") == "listening" and started and secs:
        st["seconds_left"] = max(0, int(round(secs - (time.time() - started))))
    return st


def action_autogain_stop() -> dict:
    """Stop the take early. The measurement SO FAR is still used: somebody who has said enough has
    said enough, and throwing it away would make the button pointless."""
    _autogain_set(cancel=True)
    return {"ok": True, "message": "Stopping."}


def action_autogain_undo() -> dict:
    """Put the gain back. ONLY the gain: the processing bypass is restored by the run itself, and
    the Windows level is left at no change, because that is where it should be anyway."""
    st = _autogain_state()
    name, before = st.get("channel"), st.get("gain_before")
    mic = _mic_number(name) if name else None
    if mic is None or before is None:
        return {"ok": False, "message": "There is no gain change to undo."}
    try:
        with _hid_lock:
            channels.set_mic_gain(mic, int(before))
        _ann_mark_self_gain(mic, True)
        with _session_state_lock:
            _session_state["channels"][mic]["gain_db"] = int(before)
    except Exception as exc:
        return {"ok": False, "message": f"Could not put the gain back: {exc}"}
    _autogain_set(gain_after=int(before))
    label = (_ui_chan(name) or {}).get("label", name)
    return {"ok": True, "message": f"{label} gain put back to {before} dB."}


def action_set_processing_enabled(name: str, on: bool) -> dict:
    """Master per-channel processing on/off (the 'Processing' toggle)."""
    ch = _ui_chan(name)
    if ch is None:
        return {"ok": False, "message": f"Unknown channel: {name}."}
    with _hid_lock:
        channels.set_processing_enabled(name, on)
    return {"ok": True,
            "message": f"{ch['label']} processing turned {'on' if on else 'off'}."}


def action_set_channel_advanced(name: str, on: bool) -> dict:
    """Switch a channel between ADVANCED processing (the modules and their parameters) and SIMPLE
    (the three tone-shaping controls only). channelAdvancedProcessing, per fader strip.

    Worth knowing, and why the message says so: in SIMPLE mode the unit DERIVES most of the chain
    from Depth/Sparkle/Punch, so the module parameters below can be recomputed from those three. The
    RODE app clears them outright when you leave advanced mode; we do not touch them, so switching
    back and forth here is non-destructive (hardware-verified)."""
    ch = _ui_chan(name)
    if ch is None:
        return {"ok": False, "message": f"Unknown channel: {name}."}
    try:
        with _hid_lock:
            channels.set_channel_advanced(name, on)
    except Exception as exc:
        return {"ok": False, "message": f"Could not change the processing mode: {exc}"}
    return {"ok": True,
            "message": (f"{ch['label']} set to advanced processing; the modules and their "
                        f"parameters are in charge." if on else
                        f"{ch['label']} set to simple processing; the unit works the chain out "
                        f"from Depth, Sparkle and Punch.")}


def action_set_channel_module(name: str, module: str, on: bool) -> dict:
    """Enable/disable one processing module (hpf/eq/compressor/noiseGate/deesser/aphex/pan)."""
    ch = _ui_chan(name)
    if ch is None:
        return {"ok": False, "message": f"Unknown channel: {name}."}
    try:
        with _hid_lock:
            channels.set_processing_module(name, module, on)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True,
            "message": f"{ch['label']} {module} turned {'on' if on else 'off'}."}


def action_set_channel_effect(name: str, effect: str, on: bool) -> dict:
    """Toggle a mic channel's always-on effect (echo / reverb). Mic channels only (the app shows
    Echo/Reverb only on mics)."""
    ch = _ui_chan(name)
    if ch is None or ch["kind"] != "mic":
        return {"ok": False, "message": "Echo and reverb are available on mic channels only."}
    try:
        with _hid_lock:
            channels.set_channel_effect(channels.mic_channel_number(ch), effect, on)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": f"{ch['label']} {effect} turned {'on' if on else 'off'}."}


def action_set_channel_effect_param(name: str, param: str, value01: float) -> dict:
    """Set one channel-effect param (echo*/reverb*) on a mic channel. value01 is 0.0..1.0
    (decoded: global float64 on selector 0x26+(ch-1), byte-for-byte the RODE app's write)."""
    ch = _ui_chan(name)
    if ch is None or ch["kind"] != "mic":
        return {"ok": False, "message": "Echo and reverb are available on mic channels only."}
    try:
        v = max(0.0, min(1.0, float(value01)))
        with _hid_lock:
            channels.set_channel_effect_param(channels.mic_channel_number(ch), param, v)
    except (ValueError, TypeError) as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": f"{ch['label']} {param} set to {round(v * 100)} percent."}


def action_set_channel_param(name: str, prop: str, value01: float) -> dict:
    """Set one processing parameter (0..1 normalised) on any UI channel."""
    ch = _ui_chan(name)
    if ch is None:
        return {"ok": False, "message": f"Unknown channel: {name}."}
    if prop not in _PROCESSING_PROP_SET:
        return {"ok": False, "message": f"Unknown processing control: {prop}."}
    try:
        v = max(0.0, min(1.0, float(value01)))
    except (TypeError, ValueError):
        return {"ok": False, "message": "Value must be a number from 0 to 1."}
    with _hid_lock:
        channels.set_channel_processing(name, prop, v)
    ctrl = _PROCESSING_CONTROL_BY_PROP[prop]
    pct = round(v * 100)
    if ctrl.get("pan"):
        where = ("centre" if pct == 50 else "hard left" if pct == 0 else "hard right" if pct == 100
                 else f"{(50 - pct) * 2} percent left" if pct < 50 else f"{(pct - 50) * 2} percent right")
    else:
        where = f"{pct} percent"
    return {"ok": True,
            "message": f"{ch['label']} {_PROCESSING_SPOKEN[prop].lower()} set to {where}."}


# --- Mic processing PRESETS (whole-chain, captured from the RODE app + user-saved) -----
_FACTORY_PRESET_FILE = resource_path("mic_presets.json")   # ships with the app (read-only)
_USER_PRESET_FILE = data_path("user_presets.json")         # user-saved (writable, %LOCALAPPDATA%)


def _load_factory_presets() -> dict:
    try:
        with open(_FACTORY_PRESET_FILE, "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("factory", {})
    except (OSError, ValueError):
        return {}


def _load_user_presets() -> dict:
    try:
        with open(_USER_PRESET_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def _save_user_presets(d: dict) -> None:
    with open(_USER_PRESET_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)


# The property set a preset stores = the keys of a factory preset (floats + the index).
_PRESET_PROPS = ("eqLowGain", "eqMidGain", "eqHighGain", "aphexBBDrive", "aphexAEMix",
                 "compressorThreshold", "compressorGain", "deesserThreshold",
                 "noiseGateThreshold", "noiseGateRange", "noiseGateRelease",
                 "channelDepth", "channelSparkle", "channelPunch",
                 # The processing MODE belongs to a voicing (see channels.CHAIN_ADVANCED_PROP).
                 # Without it, a saved advanced voicing applied to a strip in simple
                 # mode gets regenerated from the VoxLab knobs. Written FIRST by apply_processing_
                 # preset; the marker below is written LAST.
                 channels.CHAIN_ADVANCED_PROP,
                 # The MODULE on/off flags. A voicing is not just parameter values, it is which parts
                 # of the chain are engaged: without these, applying a preset to a strip with its
                 # modules off sets every value, changes nothing audible, and reports success.
                 # channelBypassProcessing is deliberately NOT here: the master
                 # on/off is the operator's control, not part of the voicing (see
                 # channels.apply_processing_preset).
                 *channels.PROCESSING_MODULE_BOOLS,
                 "channelCurrentFxPreset")


def preset_list() -> list:
    """[{name, custom}] for the UI combo box, factory first then user presets."""
    out = [{"name": n, "custom": False} for n in _load_factory_presets()]
    out += [{"name": n, "custom": True} for n in _load_user_presets()]
    return out


# --- WHICH PRESET A STRIP IS ON ----------------------------------------------------------------
# The device cannot really answer this, so without the record below the Presets tab cannot either:
# apply Podcast Studio, leave the channel, come back, and nothing says what it is on.
#
# `channelCurrentFxPreset` is a NAME-only marker with three factory slots (0/1/2, -1 = none) that
# the unit blanks the moment any parameter is written. So it cannot name a custom preset at all, and
# it stops naming a factory one at the first tweak. Reading the device harder does not fix that.
#
# Instead we keep our OWN record of what we last applied and then CHECK it against the live values
# rather than trusting it. A record saying "Podcast Studio" on a chain that no longer matches is
# reported as edited, not as the preset. That covers custom presets, stays honest through tweaks,
# and self-corrects when the strip is changed from the RODE app or the desk, because the values
# simply stop matching. The device marker is still preferred when it HAS an answer (see _preset_now).
#
# The record is per-machine and per-device: it carries the hardware id it was written against, and a
# record from another desk is ignored rather than claimed. On a fresh install there is no record, so
# the marker is all we have until something is applied - which the UI says plainly.
_APPLIED_PRESETS_KEY = "applied_presets"
_PRESET_MATCH_TOL = 0.005     # normalised 0..1 floats: a read-back is never bit-identical
# The factory marker slots, in channelCurrentFxPreset order, confirmed against the RODE app's own
# list (app.js keeps the same list for its dropdown).
_FACTORY_MARKER_NAMES = ("Neutral", "Podcast Studio", "Broadcast")


def _device_hardware_id() -> str:
    """The connected desk's USB SERIAL, or "" if we do not know it yet. It is what every per-device
    preference is filed under, so it has to be the same value tomorrow: see read_device_status for
    why `VER` is not.

    PURE LOOKUP, deliberately: it reads the value read_device_status already published (see
    _note_device_id) and never opens the device itself. Two reasons. Reading it is a full device
    round trip - enumerate, open, get_version, ping - and this is called from a channel read that
    the UI does on every visit to the page. And read_device_status takes the HID lock, which is not
    reentrant, so calling it from anywhere that already holds the lock would deadlock the app rather
    than merely be slow. "" is a perfectly good answer here: it only means a preference goes untagged
    until the first status read, which the page does on load."""
    return _device_id_cache[1]


def _applied_preset_record() -> dict:
    """{mic channel as str: preset name} for THIS desk. Empty if the record belongs to another one -
    two desks would otherwise inherit each other's claims."""
    rec = load_settings().get(_APPLIED_PRESETS_KEY)
    if not isinstance(rec, dict):
        return {}
    here = _device_hardware_id()
    if rec.get("device") and here and rec.get("device") != here:
        return {}
    chans = rec.get("channels")
    return chans if isinstance(chans, dict) else {}


def _note_applied_preset(channel: int, name: str) -> None:
    """Remember that we just applied `name` to a mic channel. Best-effort, like every other setting:
    losing it costs a name in the UI, never a device state."""
    update_settings(**{_APPLIED_PRESETS_KEY: {
        "device": _device_hardware_id(),
        "channels": {**_applied_preset_record(), str(int(channel)): name},
    }})


def _preset_matches(props: dict, ch: dict) -> bool:
    """Does the live chain still hold every value this preset writes?

    Read straight off the channel dump the UI already asked for, so this costs no device access.
    Floats compare within _PRESET_MATCH_TOL; the module flags and the processing mode compare
    exactly, because those are what make a voicing audible. The preset-name marker is skipped: it is
    a name, not a value, and the unit blanks it as soon as the first parameter lands."""
    params, modules = (ch.get("params") or {}), (ch.get("modules") or {})
    for prop, want in props.items():
        if prop == channels.CHAIN_PRESET_MARKER_PROP:
            continue
        if prop == channels.CHAIN_ADVANCED_PROP:
            got = ch.get("advanced")
        elif prop in channels.PROCESSING_MODULE_BOOLS:
            got = modules.get(prop)
        else:
            got = params.get(prop)
            if got is None:
                return False
            try:
                if abs(float(got) - float(want)) > _PRESET_MATCH_TOL:
                    return False
            except (TypeError, ValueError):
                return False
            continue
        if got is None or bool(got) != bool(want):
            return False
    return True


def _preset_now(channel: int, ch: dict):
    """What this mic strip is on: {name, edited, source}, or None when nothing can be claimed.

    Order of trust, most reliable first:
      1. The DEVICE marker, when it has an answer. A non-negative marker means a factory preset was
         applied and nothing has been written since, which beats any record of ours - it is also the
         only thing that knows about a preset applied from the RODE app.
      2. OUR record, checked against the live values, which is the only way a custom preset or an
         edited factory one can be named at all.
      3. Nothing. The strip is on settings we have no name for, and the UI says exactly that rather
         than inventing "custom"."""
    idx = ch.get("preset_index")
    if isinstance(idx, int) and 0 <= idx < len(_FACTORY_MARKER_NAMES):
        return {"name": _FACTORY_MARKER_NAMES[idx], "edited": False, "source": "device"}
    name = _applied_preset_record().get(str(int(channel)))
    if not name:
        return None
    props = {**_load_factory_presets(), **_load_user_presets()}.get(name)
    if not props:
        # Applied, then deleted from the preset list: we can still name it, but there is nothing left
        # to compare against, so we do not claim either way.
        return {"name": name, "edited": None, "source": "record"}
    return {"name": name, "edited": not _preset_matches(props, ch), "source": "record"}


def _read_channel_preset_props(channel: int) -> dict:
    """Read the current preset-property values for one mic channel (for 'save as').

    NEVER INDEX THE PROCESSING ARRAYS WITH `channel - 1`: that assumes a mic's chain sits at its own
    slot, and processing is per fader STRIP (hardware-confirmed). On a reordered layout - a line
    channel on fader 2 pushing Mic 2 to strip 6 - "save Mic 2's settings as a preset" then silently
    saves whatever chain sits on strip 1. The strip is resolved the same way every other processing
    read and write does it (channels._channel_strip, off the live fader vector). The processing MODE
    is saved too, so applying a saved advanced voicing restores advanced mode rather than landing on
    a strip that re-derives it from the VoxLab knobs."""
    blob = rcp_read.read_model_stable()
    idx = channels._channel_strip(channel)
    out = {}
    for p in _PRESET_PROPS:
        vals = rcp_read.values(blob, p)
        if idx < len(vals):
            out[p] = vals[idx]
    return out


# Plain names for the module flags, for the "switched on" sentence. Spelled as the Channels page
# spells them so the two are not read aloud as if they were different controls.
_MODULE_LABEL = {"hpfOn": "high-pass filter", "eqOn": "equaliser", "compressorOn": "compressor",
                 "noiseGateOn": "noise gate", "deesserOn": "de-esser", "aphexOn": "tone shaping",
                 "channelPanOn": "pan", "eqHighOn": "EQ high band", "eqMidOn": "EQ mid band",
                 "eqLowOn": "EQ low band"}


def _read_channel_modules(channel: int) -> dict:
    """The module on/off flags for one mic channel's STRIP, plus its master processing state.

    Same strip resolution as every other processing read (channels._channel_strip off the live
    fader vector), so it is correct on a reordered layout. CALLER HOLDS THE HID LOCK."""
    blob = rcp_read.read_model(fresh=True)
    idx = channels._channel_strip(channel)
    out = {}
    for p in channels.PROCESSING_MODULE_BOOLS:
        vals = rcp_read.values(blob, p)
        out[p] = vals[idx] if idx < len(vals) else None
    bypass = rcp_read.values(blob, "channelBypassProcessing")
    out["_processing_on"] = (None if idx >= len(bypass) else not bypass[idx])
    return out


def action_apply_mic_preset(channel: int, name: str) -> dict:
    if channel not in MIC_CHANNELS:
        return {"ok": False, "message": f"Mic channel {channel} is not valid."}
    presets = {**_load_factory_presets(), **_load_user_presets()}
    props = presets.get(name)
    if not props:
        return {"ok": False, "message": f"Preset '{name}' was not found."}
    # Read the modules FIRST so we can say what actually changed. A preset that silently switched
    # part of the chain on would be as bad as the silent no-op it replaces: the operator has to be
    # told, because nothing on screen otherwise shows a module moving.
    try:
        with _hid_lock:
            before = _read_channel_modules(channel)
            channels.apply_processing_preset(channel, props)
            after = _read_channel_modules(channel)
    except Exception as exc:
        return {"ok": False, "message": f"Could not apply the preset: {exc}"}
    # Remember it, so coming back to this channel can still say what it is on. The device marker
    # cannot do that job for a custom preset, or for a factory one once a slider moves (_preset_now).
    _note_applied_preset(channel, name)
    msg = f"Mic {channel} set to the '{name}' preset."
    turned_on = [_MODULE_LABEL.get(p, p) for p in channels.PROCESSING_MODULE_BOOLS
                 if after.get(p) and not before.get(p)]
    turned_off = [_MODULE_LABEL.get(p, p) for p in channels.PROCESSING_MODULE_BOOLS
                  if before.get(p) and not after.get(p)]
    if turned_on:
        msg += f" Switched on: {', '.join(turned_on)}."
    if turned_off:
        msg += f" Switched off: {', '.join(turned_off)}."
    # The master on/off is never part of a preset, so say so when it would mask the whole thing.
    if after.get("_processing_on") is False:
        msg += (" Note: processing is still off for this channel, so the preset is loaded but not "
                "heard. Turn processing on to use it.")
    return {"ok": True, "message": msg}


def action_save_mic_preset(channel: int, name: str) -> dict:
    name = (name or "").strip()
    if channel not in MIC_CHANNELS:
        return {"ok": False, "message": f"Mic channel {channel} is not valid."}
    if not name:
        return {"ok": False, "message": "Please enter a name for the preset."}
    if name in _load_factory_presets():
        return {"ok": False, "message": f"'{name}' is a built-in preset name; choose another."}
    try:
        with _hid_lock:
            props = _read_channel_preset_props(channel)
    except Exception as exc:
        return {"ok": False, "message": f"Could not read the current settings: {exc}"}
    if not props:
        return {"ok": False, "message": "Could not read the current settings to save."}
    user = _load_user_presets()
    user[name] = props
    _save_user_presets(user)
    # The channel now IS this preset by definition - it was saved from these very values - so the
    # strip adopts the new name straight away rather than still claiming whatever it was based on.
    _note_applied_preset(channel, name)
    return {"ok": True, "message": f"Saved Mic {channel}'s current settings as '{name}'.",
            "presets": preset_list()}


def action_delete_mic_preset(name: str) -> dict:
    user = _load_user_presets()
    if name not in user:
        return {"ok": False, "message": f"'{name}' is not a custom preset (built-ins can't be deleted)."}
    del user[name]
    _save_user_presets(user)
    return {"ok": True, "message": f"Deleted the '{name}' preset.", "presets": preset_list()}


# ----------------------------------------------------------------------------
# Routing / output settings (decoded sessions 16-17, read-back verifiable).
# ----------------------------------------------------------------------------

# Flexible channel strips that can be reassigned (the by-name routing view) + the source palette
# now live per-device in devices.InputStripSpec (GAP-3), resolved via _active_input_strips(). Before
# GAP-3 these were Pro II-hardcoded here as _INPUT_STRIP_VEC_INDEX / _INPUT_STRIP_LABEL /
# _INPUT_SOURCE_LABEL / _VALID_SOURCE_CODES; the Duo uses the index-based fader_model instead, so its
# spec is empty and the by-name endpoints below report nothing and refuse on it.


def _active_input_strips():
    """The InputStripSpec for the currently-connected device (devices.py). See GAP-3."""
    return devices.get_active_device().input_strips


# Import-time consistency guard: every strip key must be a real channelInputSource selector whose
# selector equals 0x1C + vec_index, and every palette code a real source code, so a future spec typo
# fails loudly here instead of silently mis-writing (or dropping) a strip. devices.py cannot import
# rcp_transfer (import-cycle), so the cross-check lives here where both modules are visible.
def _check_input_strip_specs():
    for dev in devices.DEVICES.values():
        spec = dev.input_strips
        for key, idx, _label in spec.strips:
            assert key in rcp_transfer.CHANNEL_INPUT_SOURCE_SELECTOR, (
                f"{dev.name}: input strip '{key}' is not a channelInputSource selector key")
            assert (rcp_transfer.CHANNEL_INPUT_SOURCE_SELECTOR[key]
                    == rcp_transfer.FADER_SELECTOR_BASE + idx), (
                f"{dev.name}: input strip '{key}' vec_index {idx} disagrees with its selector")
        assert set(spec.valid_source_codes) <= set(rcp_transfer.INPUT_SOURCE_CODE.values()), (
            f"{dev.name}: an input-source palette code is not a known INPUT_SOURCE_CODE value")


_check_input_strip_specs()

# --- FADER ASSIGNMENT (which channel sits on which fader) ---------------------
# The fader layout IS the channelInputSource vector (see RODECASTER_PROTOCOL.txt section 6):
# index 0 = fader 1 ... value = channel code on that fader.
# We read the layout from the vector and write it with rcp_transfer.set_fader_source (selector
# 0x1c + index). The per-device SHAPE (vector length, physical/virtual split, assignable channel
# list + labels, factory-default vector) lives in devices.FaderSpec (GAP-1): the Pro II has 9
# faders and Mic 1-4; the Duo has a 10-entry vector, code 2 = Headset (not Mic 3), no Mic3/Mic4,
# and NO confirmed default so its 'reset faders' refuses instead of writing a guessed layout.


def _active_faders():
    """The FaderSpec for the currently-connected device (devices.py)."""
    return devices.get_active_device().fader


def _chain_follow_on() -> bool:
    """Whether "the chain follows the channel" is enabled for the connected device.

    Gated behind its own cap rather than riding on "fader_write", because the Duo has swap cases the
    Pro II does not: its default layout contains an EMPTY strip (-1) and a DUPLICATED source (USB 2
    twice), and index() on a duplicate finds only the first. Until the same read-back test has been
    run on a Duo, it keeps the old behaviour (layout moves, chain stays) rather than an untested one."""
    return "chain_follow" in devices.get_active_device().caps


def _follow_chains(old_vec, new_vec, chains_before) -> str:
    """Move each channel's processing/effects chain to the strip it landed on, then VERIFY.

    Called with the HID lock held, immediately after the layout write. `chains_before` is the
    per-strip chain snapshot taken from the dump BEFORE that write, so for every source that moved we
    know exactly which chain belongs to it. Only the DIFFERENCES are written (a real swap is about
    13 writes and a second); a blind full write would be ~110.

    THREE CASES, not one. A channel can move between strips, LEAVE the layout, or RETURN to it. A
    channel that leaves has its chain parked (channels.park_chain), because the strip it vacated is
    about to be overwritten by whoever moves in and the desk keeps no per-channel copy. A channel
    that returns gets that parked chain written onto its new strip, or it silently inherits the
    previous occupant's voicing. Leaving and returning is not an edge case: it is the ordinary path
    for stereo link, which evicts the upper mic of a pair and hands it back on unlink.

    Returns a sentence for the user: what moved, or precisely what did not. Never raises - a fader
    move that succeeded must not be reported as a failure because the chain step had a problem."""
    try:
        # Where did each source live, and where does it live now? Sources present in both layouts and
        # sitting on a DIFFERENT strip are the ones whose chain has to travel.
        old_at = {code: i for i, code in enumerate(old_vec) if code is not None and code >= 0}
        new_at = {code: i for i, code in enumerate(new_vec) if code is not None and code >= 0}
        moves = []
        for code, i in new_at.items():
            src = old_at.get(code)
            if src is not None and src != i and src < len(chains_before) and i < len(chains_before):
                moves.append((code, src, i))
        # PARK FIRST, off the pre-write snapshot: once the moves below start writing, the vacated
        # strip's chain is gone for good.
        # Falls back to the model name because _device_hardware_id is "" until the first status read:
        # parking under one key and looking under another would lose the chain we just saved.
        device_id = _device_hardware_id() or devices.get_active_device().name
        parked = []
        for code, src in old_at.items():
            if code not in new_at and src < len(chains_before):
                channels.park_chain(device_id, code, chains_before[src])
                parked.append(code)
        # A channel with no strip before and a strip now: hand back what we parked when it left.
        returns = []
        homeless = []
        for code, dst in new_at.items():
            if code in old_at or dst >= len(chains_before):
                continue
            chain = channels.take_chain(device_id, code)
            if chain is None:
                homeless.append(code)      # never saw it leave (parked before this feature, or
            else:                          # moved off-fader by the RODE app, which we cannot see)
                returns.append((code, dst, chain))
        if not moves and not returns:
            return _chain_note(spec=_active_faders(), parked=parked, homeless=homeless)
        written = 0
        for _code, src, dst in moves:
            # `current` is what physically sits on the destination strip right now, i.e. the previous
            # occupant's chain, so the diff is exactly the handful of values that actually differ.
            res = channels.write_chain(dst, chains_before[src], chains_before[dst])
            written += res["written"]
        for _code, dst, chain in returns:
            res = channels.write_chain(dst, chain, chains_before[dst])
            written += res["written"]
        # Read back and check every moved chain landed. This is the half of the job that makes a
        # partial write safe to report honestly.
        after = channels.read_chains(rcp_read.read_model(fresh=True))
        spec = _active_faders()
        bad = []
        for code, src, dst in moves:
            if dst >= len(after) or channels.chain_diff(chains_before[src], after[dst]):
                bad.append(_channel_name(code, spec))
        for code, dst, chain in returns:
            if dst >= len(after) or channels.chain_diff(chain, after[dst]):
                bad.append(_channel_name(code, spec))
                channels.park_chain(device_id, code, chain)   # put it back; it did not land
        if bad:
            return (f"WARNING: the processing chain did not fully follow {', '.join(bad)} "
                    f"({written} of the writes landed). Set that channel's processing again, or "
                    f"restore a profile.")
        return _chain_note(spec=spec, parked=parked, homeless=homeless, written=written,
                           moved=[c for c, _s, _d in moves],
                           restored=[c for c, _d, _c in returns])
    except Exception as exc:
        return (f"WARNING: the fader layout changed, but moving the processing chain failed ({exc}). "
                f"Check the affected channels' processing.")


def _chain_note(spec, parked=(), homeless=(), written=0, moved=(), restored=()) -> str:
    """The one sentence the user hears about what happened to the processing. Says what was KEPT for
    an off-fader channel as well as what moved: "off-fader" otherwise reads as "deleted", and the
    whole point of parking is that it is not."""
    def names(codes):
        return ", ".join(_channel_name(c, spec) for c in codes)

    out = ""
    if moved and restored:
        out = (f"Processing and effects followed {names(moved)}, and {names(restored)} got its own "
               f"processing back ({written} values).")
    elif moved:
        out = f"Processing and effects followed {names(moved)} ({written} values)."
    elif restored:
        out = f"{names(restored)} got its own processing back ({written} values)."
    if parked:
        # Says only what happened to the PROCESSING. action_set_fader already reports the layout
        # side ("X is now off-fader"), and hearing the same clause twice in one announcement is
        # worse than terse.
        many = len(parked) > 1
        out += (f" The processing for {names(parked)} is kept, and comes back when "
                f"{'they get faders' if many else 'it gets a fader'} again.")
    if homeless:
        out += (f" {names(homeless)} had no stored processing, so it is using whatever was "
                f"already on that fader; set its processing to taste.")
    return out.strip()


def _fader_write_gate():
    """Fader-assignment WRITES. Reads and labels are always device-aware and safe; writes gate
    because a device whose fader write model is only partly known (the Duo: empty strips encoded
    as -1, no confirmed default vector) must refuse rather than write a guessed layout (GAP-1)."""
    return _cap_gate("fader_write", lambda name:
                     f"Changing the fader layout is not enabled on the {name} yet, so the faders "
                     f"are read-only here. You can still see the current layout.")


def _is_empty_fader(code) -> bool:
    """True for an empty/unsourced fader strip. The Duo's vector carries -1 for an empty
    logical strip; None appears when the read vector is shorter than the fader count."""
    return code is None or code == -1


def _fader_label(i: int, spec=None) -> str:
    spec = spec or _active_faders()
    return f"Fader {i + 1}" + ("" if i < spec.physical else " (virtual)")


def _channel_name(code, spec=None) -> str:
    # all_labels, not code_label: a fader can HOLD a stereo-linked pair even though a pair is never
    # something you assign, and "channel code 4" is no use to anyone reading the layout aloud.
    spec = spec or _active_faders()
    if _is_empty_fader(code):
        return "empty (no channel)"
    return spec.labels_for(channels.usb_expanded()).get(code, f"channel code {code}")


def _follow_levels(old_vec, new_vec, levels_before, spec) -> str:
    """Move each channel's LEVEL to the fader it landed on, the way _follow_chains moves its chain.

    WHY IT EXISTS. A fader's level belongs to the STRIP, not to the channel sitting on it, so a
    reassignment silently hands a channel whatever level that strip happens to be at. A backup that
    moves Main Stereo from fader 8 onto fader 7 lands it on whatever 7 was sitting at, zero in the
    worst case, while the channel that took fader 8 carries on at 95. Every reading is then clean -
    unmuted, in the matrix, on a fader, layout exactly as the backup says - because nothing IS
    wrong. The level has simply not travelled with the channel, and a level is not visible as a
    fault; it is just silence.

    The app already accepts that a channel's settings should follow the channel: that is the whole
    reason `_follow_chains` and the off-fader chain store exist. The level is the only one of them
    that can silence somebody without saying so.

    ONLY BETWEEN FADERS THAT BOTH HAVE A LEVEL CONTROL, i.e. the virtual ones. A physical fader's
    position is physical: it cannot be read and it cannot be written, so a channel arriving from one
    brings no level with it and putting a number there would be inventing state. Those cases are the
    restore warning's job, not this one's.

    Written from the PRE-WRITE snapshot, so a swap works: each channel takes the level it had, not
    whatever the other one has just been given.

    Never raises. A layout change that succeeded must not be reported as a failure because this
    step had a problem - the same rule `_follow_chains` follows, and for the same reason.
    """
    try:
        old_at = {code: i for i, code in enumerate(old_vec) if code is not None and code >= 0}
        new_at = {code: i for i, code in enumerate(new_vec) if code is not None and code >= 0}
        moved = []
        for code, dst in new_at.items():
            src = old_at.get(code)
            if src is None or src == dst:
                continue
            if src not in spec.level_selectors or dst not in spec.level_selectors:
                continue
            was = levels_before[src] if src < len(levels_before) else None
            now = levels_before[dst] if dst < len(levels_before) else None
            if not isinstance(was, int) or was == now:
                continue
            rcp_transfer.set_fader_level(spec.level_selectors[dst], was)
            moved.append((code, dst, was))
        if not moved:
            return ""
        pct = [(c, d, round(v * 100 / rcp_transfer.FADER_LEVEL_MAX)) for c, d, v in moved]
        return " " + " ".join(
            f"{_channel_name(c, spec)} kept its level of {p} percent on "
            f"{_fader_label(d, spec)}." for c, d, p in pct)
    except Exception as exc:
        return f" The channel moved but its level did not follow it: {exc}"


def _read_fader_vector(blob=None, spec=None):
    """The current channel code on each fader, from channelInputSource (device-length).

    PUBLISHES what it read, and that is load-bearing. Invalidating the TTL'd cache alone is not
    enough: that is the cache that ADDRESSES processing writes, while the separate no-expiry NAMING
    layout (channels.last_known_strip_vector) keeps the pre-write layout, because our own writes are
    not echoed on the pushed stream the way a change made on the desk is. The symptom is an announcer
    naming the OLD channel on an encoder press after 'reset faders' while a TURN names it correctly:
    the turn decodes the source code straight off the cross-point selector, but the press is a
    prediction that has to look the layout up.
    Every layout write re-reads through here immediately afterwards, so publishing here keeps both
    caches honest with no extra dump. The FULL vector is published, not the per-fader-count slice,
    so the spare 10th strip still names its source."""
    spec = spec or _active_faders()
    if blob is None:
        blob = rcp_read.read_model()
    cis = rcp_read.values(blob, "channelInputSource")
    channels.publish_strip_vector(cis)
    return [cis[i] if i < len(cis) else None for i in range(spec.count)]


def fader_model() -> dict:
    """Static layout the Fader assignment UI renders: the connected device's faders + the
    channels that can be placed on them (device-aware, see devices.FaderSpec)."""
    spec = _active_faders()
    if spec.physical < spec.count:
        phys_note = (f" Faders 1 to {spec.physical} are the physical faders; the rest are "
                     f"virtual faders.")
    else:
        phys_note = ""
    return {
        "faders": [{"index": i, "label": _fader_label(i, spec), "physical": i < spec.physical}
                   for i in range(spec.count)],
        # THE DROPDOWN AND THE WRITE GATE MUST OFFER THE SAME SET, or Expanded's four inputs are
        # either refused after being chosen or missing from the list that can choose them. Both
        # come off `code_..._for`, so there is one answer to "what may go on a fader".
        "channels": [[code, spec.labels_for(channels.usb_expanded()).get(code, str(code))]
                     for code in spec.code_order_for(channels.usb_expanded())],
        "read_only": _fader_write_gate() is not None,
        # The PAIRS are static (which links exist, and what to call them); whether each is currently
        # linked is live state and arrives with the layout read, not from here.
        "stereo_pairs": [{"code": code, "label": label,
                          "lower": _channel_name(lo, spec), "upper": _channel_name(up, spec)}
                         for code, lo, up, label in spec.stereo_pairs],
        "stereo_read_only": _stereo_link_gate() is not None,
        "stereo_note": ("Linking two inputs puts both on one fader, panned hard left and right, so "
                        "one fader controls the pair. Only inputs next to each other can link. The "
                        "fader the second input leaves becomes empty, and unlinking gives that input "
                        "its fader and its processing back."),
        # NEVER WRITE A FADER ON `change`. A closed select fires on EVERY arrow press, and arrowing
        # a closed select is how a screen-reader user reads one, so writing on change means simply
        # browsing the list performs the swap. Each fader has its own Apply.
        "note": ("Choose which channel sits on each fader." + phys_note + " Putting a channel "
                 "on a fader that already holds it elsewhere swaps the two. Nothing is sent to the "
                 "unit until you press that fader's own Apply button."),
    }


def _carry_mute(new_vec, codes, muted: bool) -> str:
    """Re-mute `codes` on the strips they now sit on, if the desk opened them.

    THE DESK UNMUTES WHAT IT REARRANGES. Linking a pair clears channelOutputMute on both inputs, and
    unlinking clears it on the mic it hands back (measured), and the RODE app does not put it back
    either. A mic that the user deliberately silenced going live because they tried a
    stereo link is the worst failure this feature could have, so we carry the state across.

    ONE DIRECTION ONLY: it re-mutes, it never unmutes. Muting something the user has open is the
    same class of surprise in reverse, which is exactly why a profile restore refuses to write mutes
    at all (profiles.UNIT_NEVER_RESTORE). Restoring what our own write disturbed is a different thing
    from applying somebody's old snapshot, but only in this direction."""
    if not muted:
        return ""
    spec = _active_faders()
    mutes = rcp_read.values(rcp_read.read_model(fresh=True), "channelOutputMute")
    done = []
    for code in codes:
        strip = next((i for i, c in enumerate(new_vec) if c == code), None)
        if strip is None or strip >= len(mutes) or mutes[strip]:
            continue
        rcp_transfer.send_record(rcp_transfer.build_global_prop_bool(
            channels.MUTE_PROP, True, channels.proc_selector_for_strip(strip)))
        done.append(_channel_name(code, spec))
    return f" {', '.join(done)} stayed muted." if done else ""


def _stereo_link_gate():
    """Stereo-link WRITES. Two conditions, not one: the device must claim the cap AND carry a proven
    pair table, so a future device that gets the cap by copy-paste still refuses until its codes are
    captured rather than writing a Pro II code at a desk that means something else by it."""
    gate = _cap_gate("stereo_link", lambda name:
                     f"Linking two inputs into a stereo pair is not enabled on the {name} yet, so "
                     f"this is read-only here.")
    if gate is not None:
        return gate
    if not _active_faders().stereo_pairs:
        dev = devices.get_active_device()
        return {"ok": False,
                "message": (f"Linking two inputs into a stereo pair is not available on the "
                            f"{dev.name}: its pair codes have not been confirmed.")}
    return None


def stereo_link_state(vec=None, spec=None) -> list:
    """Each legal pair and whether it is currently linked, for the UI. Read-only, and it takes the
    already-read vector when there is one so opening the page costs no extra dump."""
    spec = spec or _active_faders()
    vec = vec if vec is not None else _read_fader_vector(spec=spec)
    out = []
    for code, lo, up, label in spec.stereo_pairs:
        out.append({"code": code, "label": label, "linked": code in (vec or []),
                    "lower": _channel_name(lo, spec), "upper": _channel_name(up, spec)})
    return out


def action_set_stereo_link(pair_code: int, on: bool) -> dict:
    """Link or unlink one adjacent mic pair (RODECASTER_PROTOCOL 6.5).

    LINKING is two vacates and one write: the pair code goes onto the LOWER mic's fader and the other
    fader is emptied, because a linked pair occupies one fader rather than two. That is the desk's
    own shape, captured from the RODE app; the app's other two records are cosmetic and writing the
    source code alone leaves the desk byte-identical (verified).

    UNLINKING is where we deliberately DIVERGE from the desk. It returns only the lower mic and
    abandons the upper one off-fader, which on the RODE app leaves you with a mic you cannot hear and
    no indication why. We put the upper mic back on the fader the link emptied when that fader is
    still free, so a link followed by an unlink is a true round trip. With its processing restored by
    the off-fader chain store, the desk ends up where it started. When there is no free fader we say
    so rather than displacing something the user chose.

    Processing follows both ways through _follow_chains: linking parks BOTH mics' chains (the pair
    keeps the lower mic's strip voicing, which is what the desk does), and unlinking hands each one
    back."""
    gate = _stereo_link_gate()
    if gate is not None:
        return gate
    spec = _active_faders()
    pair = spec.pair_map.get(int(pair_code))
    if pair is None:
        return {"ok": False, "message": f"{pair_code} is not a stereo pair on this unit."}
    lower, upper = pair
    label = spec.pair_label.get(int(pair_code), f"pair {pair_code}")
    chain_note = mute_note = ""
    try:
        with _hid_lock:
            blob = rcp_read.read_model()
            vec = _read_fader_vector(blob, spec)
            target = list(vec)
            if on:
                if int(pair_code) in vec:
                    return {"ok": True, "faders": vec,
                            "labels": [_channel_name(c, spec) for c in vec],
                            "stereo": stereo_link_state(vec, spec),
                            "message": f"{label} is already linked."}
                # The pair lands where the LOWER mic sits. If neither mic is on a fader (both were
                # displaced earlier) the first empty strip will do; with none we refuse rather than
                # evict a channel the user put there on purpose.
                seat = next((i for i, c in enumerate(target) if c == lower), None)
                if seat is None:
                    seat = next((i for i, c in enumerate(target) if c == upper), None)
                if seat is None:
                    seat = next((i for i, c in enumerate(target) if _is_empty_fader(c)), None)
                if seat is None:
                    return {"ok": False,
                            "message": (f"There is no free fader for {label}. Move a channel off a "
                                        f"fader first, then link.")}
                for i, c in enumerate(target):
                    if c in (lower, upper):
                        target[i] = -1
                target[seat] = int(pair_code)
            else:
                seat = next((i for i, c in enumerate(target) if c == int(pair_code)), None)
                if seat is None:
                    return {"ok": True, "faders": vec,
                            "labels": [_channel_name(c, spec) for c in vec],
                            "stereo": stereo_link_state(vec, spec),
                            "message": f"{label} is not linked."}
                target[seat] = lower
                spare = next((i for i, c in enumerate(target) if _is_empty_fader(c)), None)
                if spare is not None:
                    target[spare] = upper
            # MUTE TRAVELS WITH THE INPUTS, the same principle as the processing chain. Read before
            # the write, since a linked mic has no strip of its own to read afterwards. A pair counts
            # as muted only when BOTH its inputs were, so linking never silences something that was
            # open, and unlinking hands the pair's own state to both mics.
            mutes = rcp_read.values(blob, "channelOutputMute")

            def muted_now(code):
                i = next((j for j, c in enumerate(vec) if c == code), None)
                return bool(mutes[i]) if i is not None and i < len(mutes) else False

            if on:
                keep_muted = muted_now(lower) and muted_now(upper)
                carry_to = [int(pair_code)]
            else:
                keep_muted = muted_now(int(pair_code))
                carry_to = [lower, upper]
            chains_before = channels.read_chains(blob) if _chain_follow_on() else None
            for i in range(spec.count):
                if target[i] != vec[i]:
                    rcp_transfer.set_fader_source(i, target[i])
            channels.invalidate_strip_vector()
            new = _read_fader_vector(spec=spec)
            if chains_before is not None:
                chain_note = _follow_chains(vec, new, chains_before)
            mute_note = _carry_mute(new, carry_to, keep_muted)
    except Exception as exc:
        verb = "link" if on else "unlink"
        return {"ok": False, "message": f"Could not {verb} {label}: {exc}"}
    if on:
        msg = (f"{label} linked, on {_fader_label(new.index(int(pair_code)), spec)}. "
               f"One fader now carries both inputs.")
    else:
        msg = f"{label} unlinked."
        if upper in new:
            msg += f" {_channel_name(upper, spec)} is back on {_fader_label(new.index(upper), spec)}."
        else:
            msg += (f" {_channel_name(upper, spec)} has no free fader to go back to, so it is "
                    f"off-fader; put it on a fader yourself when you have one spare.")
    if chain_note:
        msg += " " + chain_note
    msg += mute_note
    return {"ok": True, "faders": new, "labels": [_channel_name(c, spec) for c in new],
            "stereo": stereo_link_state(new, spec), "message": msg}


def read_faders_live() -> dict:
    """Live-read the channel on each fader (the channelInputSource vector order)."""
    spec = _active_faders()
    try:
        with _hid_lock:
            vec = _read_fader_vector(spec=spec)
    except Exception as exc:
        return {"ok": False, "message": f"Could not read the fader layout: {exc}"}
    return {"ok": True, "faders": vec,
            "labels": [_channel_name(c, spec) for c in vec],
            "stereo": stereo_link_state(vec, spec),
            "message": "Read the fader layout from the device."}


def _tidy_fader_target(vec, fader_index, code, order):
    """Return the target fader vector that places `code` on `fader_index` while keeping a
    VALID layout: no duplicate assignable source, no orphaned channel that is off-fader yet
    still in the mix. The channel displaced from `fader_index` moves onto wherever `code` used
    to sit (a true swap, matching a hardware drag). If the prior layout was already messy (code
    on 2+ faders, or pre-existing duplicates), each leftover duplicate slot is filled with an
    assignable source currently missing, so the result is always clean. EMPTY strips (Duo -1 /
    None) are legitimate and left untouched: they are never treated as duplicates nor forced to
    hold a source (a device can have more strips than assignable sources). `order` is the
    device's assignable-code display order (the gap-fill order)."""
    target = list(vec)
    old = target[fader_index]
    target[fader_index] = code
    # The swap: the channel we displaced goes to the first slot that still holds `code`.
    #
    # AND WHEN WE DISPLACED NOTHING, THAT SLOT IS VACATED, which is what `old` already is. Do NOT
    # guard this with `and not _is_empty_fader(old)`: moving a channel onto an EMPTY fader then
    # leaves it on its old strip as well, and the dedup loop below resolves that duplicate by
    # overwriting the slot the user just asked for - "put the Headset on fader 9" leaves the Headset
    # on fader 5 and puts another channel on fader 9. Only a desk with more faders than sources can
    # reach it, which is why it is invisible on a Pro II.
    dup_positions = [j for j in range(len(target)) if j != fader_index and target[j] == code]
    if dup_positions:
        target[dup_positions[0]] = old
    # Clean up any remaining duplicates (extra copies, or a pre-existing mess) by handing each
    # later duplicate an assignable source that is currently absent. Empties are skipped.
    #
    # A mic inside a STEREO-LINKED PAIR is not missing, it is on the fader carrying the pair. Without
    # this, linking 1+2 and then moving any other fader would see Mic 1 and Mic 2 as absent and
    # gap-fill one of them onto a spare strip, so a mic would be both linked and on its own fader.
    spec = _active_faders()
    seen = set()
    for j in range(len(target)):
        if _is_empty_fader(target[j]):
            continue
        if target[j] in seen:
            missing = [c for c in order
                       if c not in target and spec.pair_swallowing(c, target) is None]
            target[j] = missing[0] if missing else -1
            if _is_empty_fader(target[j]):
                continue
        seen.add(target[j])
    return target


def action_set_fader(fader_index: int, code: int) -> dict:
    """Put channel `code` on fader `fader_index` (0-based) on the connected device. Always
    produces a valid layout: the displaced channel swaps onto wherever `code` used to sit, and
    any duplicate/orphan left by an already-messy layout is tidied (see _tidy_fader_target).
    Re-reads to report the real result. Device-aware (devices.FaderSpec); refused on a device
    without the "fader_write" cap (the Duo)."""
    gate = _fader_write_gate()
    if gate is not None:
        return gate
    spec = _active_faders()
    if not 0 <= fader_index < spec.count:
        return {"ok": False, "message": f"Fader {fader_index + 1} is not valid."}
    # THE OFFERED SET WIDENS IN EXPANDED, where Game, Music, A and B become real inputs. In
    # Standard they are nameable but not assignable: putting one on a fader there gives somebody a
    # channel that can never carry audio.
    if code not in spec.code_set_for(channels.usb_expanded()):
        return {"ok": False, "message": f"Channel code {code} is not assignable."}
    chain_note = ""
    try:
        with _hid_lock:
            blob = rcp_read.read_model()
            vec = _read_fader_vector(blob, spec)
            old = vec[fader_index]
            if code == old:
                return {"ok": True, "faders": vec,
                        "labels": [_channel_name(c, spec) for c in vec],
                        "message": f"{_fader_label(fader_index, spec)} already carries "
                                   f"{_channel_name(code, spec)}."}
            target = _tidy_fader_target(vec, fader_index, code,
                                        spec.code_order_for(channels.usb_expanded()))
            # THE CHAIN FOLLOWS THE CHANNEL. Processing and effects are per fader STRIP, so moving a
            # channel without moving its chain leaves it being processed by whatever voicing sits on
            # the strip it lands on. The RODE app does exactly this on its own
            # drag - proven by diffing it. Snapshot BEFORE the layout write, off the dump we already
            # have, so a failure can be reported precisely.
            chains_before = channels.read_chains(blob) if _chain_follow_on() else None
            # THE LEVEL FOLLOWS THE CHANNEL TOO, and for the same reason the chain does. Snapshot
            # before the write, off the dump we already have; see _follow_levels for the silence
            # this prevents. Gated, because a desk with no confirmed faderLevel selectors must not
            # be written to at all.
            levels_before = (rcp_read.values(blob, "faderLevel")
                             if _level_write_gate() is None else None)
            # Only write the faders that actually change.
            for i in range(spec.count):
                if target[i] != vec[i]:
                    rcp_transfer.set_fader_source(i, target[i])
            # The layout IS the processing/effects strip map (channels._strip_index), so a stale
            # cached vector here would address the next processing write to the old strip.
            channels.invalidate_strip_vector()
            new = _read_fader_vector(spec=spec)
            if chains_before is not None:
                chain_note = _follow_chains(vec, new, chains_before)
            if levels_before is not None:
                chain_note += _follow_levels(vec, new, levels_before, spec)
    except Exception as exc:
        return {"ok": False, "message": f"Could not change the fader layout: {exc}"}
    changed = [i for i in range(spec.count) if i != fader_index and vec[i] != new[i]]
    # SAY WHAT THE FADER ACTUALLY CARRIES, read back, not what was asked for. Reporting `code`
    # unconditionally means that whenever the tidy step overrides the request, the app confidently
    # announces the opposite of what it did, which is what keeps the empty-fader bug above invisible.
    landed = new[fader_index] if fader_index < len(new) else None
    if landed == code:
        parts = [f"{_fader_label(fader_index, spec)} now carries {_channel_name(code, spec)}."]
    else:
        parts = [f"{_fader_label(fader_index, spec)} carries {_channel_name(landed, spec)}, "
                 f"not {_channel_name(code, spec)}."]
    moved_to = next((j for j in changed if new[j] == old), None)
    if moved_to is not None:
        parts.append(f"{_fader_label(moved_to, spec)} now carries {_channel_name(old, spec)}.")
    elif not _is_empty_fader(old) and old not in new:
        # More assignable sources than faders: the displaced channel now sits on no fader.
        parts.append(f"{_channel_name(old, spec)} is now off-fader; no fader carries it.")
    if any(j != moved_to for j in changed):
        parts.append("Tidied the layout so no channel is duplicated or off-fader.")
    if chain_note:
        parts.append(chain_note)
    # `stereo` rides along on every layout write: a swap can seat a channel on top of a linked pair,
    # so the link switches have to move with the dropdowns or they start claiming a stale state.
    return {"ok": True, "faders": new, "stereo": stereo_link_state(new, spec),
            "labels": [_channel_name(c, spec) for c in new], "message": " ".join(parts)}


# --- Virtual fader levels + monitor level --------------------------------------------------------
# The two levels the desk normally keeps to itself: the VIRTUAL faders (7-9 on the Pro II) have no
# physical fader, and the monitor speaker level has no control in the RODE app either, so without
# these writes both are reachable only from the desk, by encoder or touchscreen. The encoder is not
# usable without sight (its target is chosen by touch and it silently unbinds), which is why these
# matter. Writes PROVEN on hardware, read back and restored; see RODECASTER_PROTOCOL.txt 6.1.
def _level_write_gate():
    """Fader and monitor LEVEL writes. The one gate with a second condition beyond the cap: the
    device must also carry confirmed faderLevel selectors, so it cannot use _cap_gate alone."""
    dev = devices.get_active_device()
    if "level_write" not in dev.caps or not dev.fader.level_selectors:
        return {"ok": False,
                "message": (f"Setting fader and monitor levels is not verified on the {dev.name} "
                            f"yet, so it is disabled here rather than writing an unconfirmed "
                            f"selector.")}
    return None


def fader_levels_live() -> dict:
    """Read the levels we can set: each writable (virtual) fader, plus the monitor speaker.

    faderLevel is 0..127 and only the virtual slots carry a value; we present percent, which is what
    the announcements say too. `channel` names whatever sits on that fader right now."""
    dev = devices.get_active_device()
    spec = dev.fader
    gate = _level_write_gate()
    try:
        with _hid_lock:
            blob = rcp_read.read_model_stable()
        raw = rcp_read.values(blob, "faderLevel")
        # A virtual fader's MUTE and SOLO have no control on the unit, so software is the only way to
        # reach them, which is why they sit beside its level here. Its LEVEL is not in that position:
        # the encoder cycles onto the virtual faders (_ann_encoder_ring).
        mutes = rcp_read.values(blob, "channelOutputMute")
        solos = rcp_read.values(blob, channels.CUE_PROP)
        mon = rcp_read.values(blob, "outputMonLevel")
        # The two output MUTES and the Bluetooth output LEVEL, all proven writable.
        # They ride on this read rather than on /api/routing/live because they belong beside the
        # monitor level on the Levels tab, and this is the read that tab already makes.
        outs = rcp_read.snapshot_properties(blob)
        vec = _read_fader_vector(blob, spec)
    except Exception as exc:
        return {"ok": False, "message": f"Could not read the levels: {exc}"}
    faders = []
    for idx in sorted(spec.level_selectors):
        val = raw[idx] if idx < len(raw) else None
        faders.append({
            "index": idx,
            "label": _fader_label(idx, spec),
            "channel": _channel_name(vec[idx], spec) if idx < len(vec) else None,
            "percent": (None if val is None
                        else max(0, min(100, round(val * 100 / rcp_transfer.FADER_LEVEL_MAX)))),
            # None for an empty fader, so the UI can say why the switch is unavailable rather than
            # offering a mute for nothing.
            "muted": (None if idx >= len(vec) or _is_empty_fader(vec[idx])
                      else bool(mutes[idx]) if idx < len(mutes) else None),
            "soloed": (None if idx >= len(vec) or _is_empty_fader(vec[idx])
                       else bool(solos[idx]) if idx < len(solos) else None),
        })
    def one(prop):
        vals = outs.get(prop) or []
        return vals[0] if vals else None

    bt_level = one("outputBTLevel")
    # TWO gates, deliberately: the LEVELS can be unverified on a device whose mute and solo are
    # fine, and the UI has to be able to say which is which.
    toggle_gate = _strip_toggle_gate("Muting")
    return {"ok": True, "read_only": gate is not None,
            "message": (gate or {}).get("message", ""),
            "toggles_read_only": toggle_gate is not None,
            "toggles_message": (toggle_gate or {}).get("message", ""),
            "faders": faders,
            "monitor": {"percent": (round(mon[0] * 100) if mon else None),
                        "muted": one("outputMonMute")},
            # The Bluetooth OUTPUT: what a paired phone hears from the desk. Nothing to do with the
            # Bluetooth INPUT channel on the Channels page, which is what the phone sends US, and the
            # labels have to keep saying so or the two are impossible to tell apart by ear.
            "bluetooth_out": {"percent": (None if bt_level is None else round(bt_level * 100)),
                              "muted": one("outputBTMute")}}


def action_set_fader_level(fader: int, percent: int) -> dict:
    """Set one VIRTUAL fader's level, 0-100 percent (faderLevel, 0-127 on the wire)."""
    gate = _level_write_gate()
    if gate is not None:
        return gate
    spec = _active_faders()
    sel = spec.level_selectors.get(int(fader))
    if sel is None:
        return {"ok": False,
                "message": (f"{_fader_label(int(fader), spec)} has no level control here. Only the "
                            f"virtual faders do: the others are physical faders you move by hand.")}
    pct = max(0, min(100, int(percent)))
    try:
        with _hid_lock:
            rcp_transfer.set_fader_level(sel, round(pct * rcp_transfer.FADER_LEVEL_MAX / 100))
    except Exception as exc:
        return {"ok": False, "message": f"Could not set the level: {exc}"}
    # Tell the announcer: the echo is not a dependable cache, so without this the next encoder press
    # can quote the level this fader had BEFORE the slider moved, and the echo that DOES arrive would
    # otherwise be spoken over the top of the UI's own announcement (see _ann_publish_level).
    _ann_publish_level("fader", int(fader), pct / 100.0)
    who = _channel_name(_read_fader_vector(spec=spec)[int(fader)], spec)
    return {"ok": True, "percent": pct,
            "message": f"{_fader_label(int(fader), spec)} ({who}) set to {pct} percent."}


def action_set_monitor_speaker_level(percent: int) -> dict:
    """Set the monitor SPEAKER level, 0-100 percent (outputMonLevel).

    Named for the speaker deliberately: action_set_monitor_level further down is a different thing
    entirely, one source's level inside a monitor MIX (a matrix cross-point). The first version of
    this shared that name, was silently shadowed by it, and the endpoint called the wrong function."""
    gate = _level_write_gate()
    if gate is not None:
        return gate
    pct = max(0, min(100, int(percent)))
    try:
        with _hid_lock:
            rcp_transfer.set_monitor_level(pct / 100.0)
    except Exception as exc:
        return {"ok": False, "message": f"Could not set the monitor level: {exc}"}
    _ann_publish_level("monitor", 0, pct / 100.0)      # same reason as the fader level above
    return {"ok": True, "percent": pct, "message": f"Monitor speaker level set to {pct} percent."}


# ---- LEVEL METERING ----------------------------------------------------------------------------
# The one big accessibility gap that needed NO protocol work at all. Metering over the desk's USB
# control channel is settled impossible (nothing meter-shaped is ever pushed, and the RODE app asks
# for nothing in 37 s of capture), but the desk is also a USB AUDIO INTERFACE, and Windows meters
# every audio endpoint it has. See Project/meters.py for why that route rather than a capture
# library, and for the Remote Desktop caveat that makes this the one feature in the app that does
# not work over RDP.
_meter_watcher = None
_meter_lock = threading.Lock()
def _meter_prefs() -> dict:
    s = load_settings()
    return {
        "stream": s.get("meter_stream") or "",
        "warn_clip": bool(s.get("meter_warn_clip", True)),
        "warn_silence": bool(s.get("meter_warn_silence", True)),
        "silence_after": float(s.get("meter_silence_after", 5.0)),
        "speak_every": float(s.get("meter_speak_every", 0.0)),
        # A SOUND for clipping as well as the words. Clipping is the one thing here you react to
        # rather than think about, and a spoken sentence arrives a word at a time.
        "clip_sound": bool(s.get("meter_clip_sound", True)),
        # WHERE THE WARNING FIRES, as a peak level in dBFS.
        #
        # THE DEFAULT IS 0, TRUE FULL SCALE. Do not lower it to catch more: this desk's compressor
        # and limiter are DESIGNED to park peaks just under full scale, so -1 dB is where normal
        # processed speech lives, not where damage begins, and a warning there fires on every
        # sentence and is worth nothing.
        #
        # The setting exists for anyone who wants warning earlier: on an UNPROCESSED channel, where
        # peaks are not held down, -1 or -2 is a sensible place to put it.
        "clip_db": float(s.get("meter_clip_db", 0.0)),
        # How long a level check runs. THIRTY SECONDS, not two: a check is what you use while
        # riding a fader, and two seconds is only long enough to take a snapshot. It speaks the
        # level as it goes, and pressing the button again ends it.
        "check_seconds": float(s.get("meter_check_seconds", 30.0)),
    }


def _meter_stream(found=None) -> str:
    """The stream the meter controls actually act on: the one chosen, or the first desk stream.

    THE FALLBACK IS THE MICROPHONE, and that is a fix rather than a preference. Windows lists the
    desk's PLAYBACK endpoint first - "Speakers (RODECaster Pro II Main Stereo)" - which is what this
    PC SENDS the desk, so a meter defaulting to it moves convincingly on your own screen reader's
    speech while telling you nothing about your microphone: a healthy-looking 61 percent peak that
    is really NVDA. The two endpoints differ only by a prefix Windows chose. So the default is a
    CAPTURE endpoint, preferring Main Stereo.

    The fallback matters at all because the page's dropdown SHOWS a stream when nothing has been
    chosen; without one, "check the level" and "set the Windows level" would act on a stream the
    user was not looking at. One helper, so the read and both actions cannot disagree."""
    chosen = _meter_prefs().get("stream") or ""
    found = found if found is not None else meters.desk_streams()
    streams = found.get("streams") or []
    known = {s["id"] for s in streams}
    if chosen and chosen in known:
        return chosen
    inputs = [s for s in streams if s.get("flow") == "recording"]   # meters.FLOW_NAMES
    # WINDOWS' OWN DEFAULT RECORDING DEVICE FIRST, when it is one of the desk's. That is the stream
    # everything else on this computer records from, so it is the one worth being told about, and
    # it is a fact rather than a guess of ours.
    default_in = meters.default_recording_name()
    by_default = [s for s in inputs if default_in and s["name"] == default_in]
    main = [s for s in inputs if "main stereo" in s["name"].lower()]
    for candidates in (by_default, main, inputs, streams):
        if candidates:
            return candidates[0]["id"]
    return ""


def read_meters() -> dict:
    """What can be metered, what is being metered, and the live reading. Read-only."""
    found = meters.desk_streams()
    prefs = _meter_prefs()
    with _meter_lock:
        w = _meter_watcher
        live = {"running": bool(w and w.running),
                "name": (w.name if w else ""),
                "peak": (round(w.peak, 4) if w else None),
                "peak_hold": (round(w.peak_hold, 4) if w else None),
                "error": (w.error if w else "")}
    # A stream the user chose that is no longer there is worth saying out loud: it is the normal
    # result of unplugging the desk, and a silently-cleared setting would look like the app
    # forgetting rather than the unit being away.
    chosen = prefs["stream"]
    known = {s["id"]: s for s in found["streams"]}
    return {"ok": True,
            "available": found["ok"],
            "reason": found["reason"],
            "remote_session": found["remote"],
            # `label` says which DIRECTION each one goes, because Windows' own names do not:
            # "Microphone (... Main Stereo)" is the desk's output mix and "Speakers (... Main
            # Stereo)" is what this PC sends it. `name` is kept alongside so a user can still match
            # an entry against what their Windows audio manager shows.
            "streams": [{"id": s["id"], "name": s["name"], "flow": s["flow"],
                         "label": meters.stream_label(s["name"], s["flow"])}
                        for s in found["streams"]],
            "chosen": chosen,
            "chosen_present": bool(chosen and chosen in known),
            "settings": prefs,
            "live": live,
            "mix_note": _meter_mix_note(found),
            # WINDOWS' OWN LEVEL for the chosen stream, in dB. It belongs on this tab because it is
            # the thing that silently scales everything else here: an input Windows is boosting
            # clips in Windows while the desk's meters look fine, and one Windows was attenuating
            # made our clipping warning unreachable for the whole life of the feature.
            "input_level": _input_level(_meter_stream(found)),
            "message": found["reason"] or ""}


def _meter_mix_note(found=None) -> str:
    """A warning when the metered stream contains THIS COMPUTER'S OWN AUDIO. "" when it does not.

    MEASURED, and it is why a perfectly healthy setting reads as clipping: with nothing deliberately
    playing, "Main Stereo, recording from the unit" idles at -10 dB while "Chat, recording from the
    unit" carrying Mic 1 alone idles at -45 dB. Thirty-five decibels apart, and the difference is the
    screen reader. This PC's audio arrives at the unit on USB 1, USB 1 is in the main mix, and the
    main mix is what that stream carries back.

    It is worse than a distraction on this tab: the level check SPEAKS, and if our speech goes to
    the unit then a check on the main mix is partly measuring its own voice.

    Only said when it is actually true - the main mix is being metered AND our speech goes to the
    unit - because a warning that is always there is one nobody hears."""
    stream_id = _meter_stream(found)
    if not stream_id:
        return ""
    found = found if found is not None else meters.desk_streams()
    picked = next((s for s in found.get("streams", []) if s["id"] == stream_id), None)
    if not picked or picked.get("flow") != "recording":
        return ""
    if "main stereo" not in picked["name"].lower():
        return ""
    ann = _announcer_into_desk()
    if not (ann.get("known") and ann.get("into_desk")):
        return ""
    return ("This is the unit's whole mix, so this computer's own audio, including your screen "
            "reader, is measured with it and can look like clipping.")


def _input_level(endpoint_id: str):
    """Windows' level for one endpoint, or None if it cannot be read. Never raises: this decorates
    the meters tab and an unreadable endpoint volume is not worth failing the whole read for."""
    if not endpoint_id:
        return None
    try:
        got = meters.endpoint_volume(endpoint_id)
        return got if got.get("ok") else None
    except Exception:
        return None


def action_set_input_mute(on: bool) -> dict:
    """Mute or unmute the metered stream in WINDOWS.

    Here because a muted input is invisible from everywhere else: it reads exactly like a dead
    microphone, and the fix is in a Windows dialog rather than anywhere on the desk. With both of a
    unit's recording endpoints muted, every meter reads 0.0 and a capture comes back as digital
    silence, with nothing anywhere saying why."""
    chosen = _meter_stream()
    if not chosen:
        return {"ok": False, "message": "There is no stream to mute."}
    res = meters.set_endpoint_mute(chosen, bool(on))
    if not res.get("ok"):
        return {"ok": False,
                "message": res.get("message", "Windows would not change that mute.")}
    return {"ok": True, "muted": res["muted"],
            "message": ("This stream is muted in Windows. Nothing can be read from it, and nothing "
                        "recorded from it on this computer will have any sound."
                        if res["muted"] else
                        "This stream is no longer muted in Windows.")}


def action_set_input_level(db: float) -> dict:
    """Set WINDOWS' recording or playback level for the metered stream, in dB.

    THIS IS THE ONE CONTROL IN THE APP THAT CHANGES SOMETHING OUTSIDE THE UNIT, and it earns its
    place: Windows' level for the desk's microphone endpoint is a hidden multiplier on everything.
    At +7.67 dB, the "75 percent" a lot of advice recommends, anything the desk sends above
    -7.67 dBFS clips IN WINDOWS while the desk's own meters show nothing wrong, so every recording
    made on this PC is damaged by a setting nowhere near the desk. It is offered in dB, not percent,
    because 50 percent is -1.08 dB on this endpoint and unity is about 55 percent, a percentage
    nobody can reason about.

    THE APP NEVER MOVES IT ON ITS OWN. The meter reads it and adapts its clipping threshold; only
    this action, from a control the user operated, writes it.

    The value is READ BACK before anything is reported, because a driver can refuse SILENTLY: an
    endpoint can sit at -1.08 dB and refuse to move by dB or by scalar with no error at all, and
    then accept the same write later. That intermittency is the argument for reading back rather
    than trusting the write."""
    chosen = _meter_stream()
    if not chosen:
        return {"ok": False, "message": "There is no stream to set a level on."}
    try:
        res = meters.set_endpoint_volume_db(chosen, float(db))
    except Exception as exc:
        return {"ok": False, "message": f"Could not set the Windows level: {exc}"}
    if not res.get("ok"):
        got = res.get("db")
        if got is None:
            return {"ok": False, "message": res.get("message", "Could not set the Windows level.")}
        return {"ok": False, "level": res,
                "message": (f"Windows would not move that level: it is still {got:+.1f} dB. Some "
                            f"drivers refuse it, and this unit's Chat input has done exactly that "
                            f"before.")}
    now = res["db"]
    note = res.get("note") or ""
    said = ("no change, which is what you want with this unit" if abs(now) < 0.2
            else f"{now:+.1f} dB")
    return {"ok": True, "level": res,
            "message": f"Windows level for this stream set to {said}." + (f" {note}" if note else "")}


def action_set_meter_stream(stream: str) -> dict:
    """Choose which of the desk's audio streams the meter watches, and restart it if it is running."""
    found = meters.desk_streams()
    ids = {s["id"]: s for s in found["streams"]}
    if stream and stream not in ids:
        return {"ok": False, "message": ("That stream is not one the unit is offering now. "
                                         + (found["reason"] or "Read the list again."))}
    update_settings(meter_stream=stream)
    name = ids[stream]["name"] if stream else ""
    with _meter_lock:
        running = bool(_meter_watcher and _meter_watcher.running)
    if running:
        action_meter_watch(False)
        return action_meter_watch(True)
    return {"ok": True, "message": (f"Meter set to {name}." if stream
                                    else "No stream chosen for the meter.")}


def _clip_level(prefs_warn_clip: bool, clip_db: float) -> float:
    """The peak the warning fires at, as a 0..1 amplitude. ONE conversion, used everywhere.

    Warning switched off is expressed as an UNREACHABLE threshold rather than a branch, so there is
    no second code path that could forget to check it."""
    if not prefs_warn_clip:
        return 2.0
    return min(1.0, 10.0 ** (max(-20.0, min(0.0, float(clip_db))) / 20.0))


def action_set_meter_option(name: str, value: float) -> dict:
    """One meter preference. Kept as one endpoint over five because they are all the same shape and
    all mean the same thing: how much should this thing interrupt you."""
    spec = {
        "warn_clip": ("meter_warn_clip", bool, "Warn me when it clips"),
        "warn_silence": ("meter_warn_silence", bool, "Warn me when it goes silent"),
        "silence_after": ("meter_silence_after", float, "Seconds of silence before warning"),
        "speak_every": ("meter_speak_every", float, "Speak the level every"),
        "clip_sound": ("meter_clip_sound", bool, "Play a sound when it clips"),
        "clip_db": ("meter_clip_db", float, "Warn me when it reaches"),
    }
    if name not in spec:
        return {"ok": False, "message": f"Unknown meter setting: {name}."}
    key, cast, label = spec[name]
    try:
        v = cast(value)
    except (TypeError, ValueError):
        return {"ok": False, "message": f"{label} needs a number."}
    if name == "clip_db":
        # dBFS: never above 0 (there is nothing above full scale to warn about) and never so low
        # that ordinary speech trips it, which would make the warning worthless.
        v = max(-20.0, min(0.0, v))
    elif cast is float:
        v = max(0.0, min(600.0, v))
    update_settings(**{key: (int(v) if cast is bool else v)})
    with _meter_lock:
        w = _meter_watcher
    if w:                                  # apply live, no restart needed for any of these
        if name == "warn_clip":
            w.clip_at = _clip_level(bool(v), _meter_prefs()["clip_db"])
        elif name == "warn_silence":
            w.warn_silence = bool(v)
        elif name == "silence_after":
            w.silence_after = float(v)
        elif name == "speak_every":
            w.speak_every = float(v)
        elif name == "clip_sound":
            w.clip_sound = bool(v)
        elif name == "clip_db":
            w.clip_at = _clip_level(prefs_warn_clip=True, clip_db=float(v))
    # A running CHECK has its own Watcher, so the setting has to reach that too or turning the
    # sound off mid-check would appear to do nothing. That check now lives on the Channels page
    # (action_channel_level_check); the Watcher it holds takes the same two settings.
    with _chan_check_lock:
        c = _chan_check
    if c is not None:
        if name == "clip_sound":
            c.clip_sound = bool(v)
        elif name == "clip_db":
            c.clip_at = _clip_level(prefs_warn_clip=True, clip_db=float(v))
    if cast is bool:
        return {"ok": True, "message": f"{label}: {'on' if v else 'off'}."}
    if name == "clip_db":
        return {"ok": True,
                "message": f"{label}: {'minus ' + format(abs(v), '.0f') if v < 0 else '0'} dB."}
    if name == "speak_every":
        return {"ok": True, "message": ("The level will not be spoken on its own." if not v
                                        else f"The level will be spoken every {v:g} seconds.")}
    return {"ok": True, "message": f"{label}: {v:g} seconds."}


def action_meter_watch(on: bool) -> dict:
    """Start or stop the spoken meter."""
    global _meter_watcher
    prefs = _meter_prefs()
    with _meter_lock:
        if not on:
            if _meter_watcher:
                _meter_watcher.stop()
                _meter_watcher = None
            return {"ok": True, "message": "Meter warnings off."}
        if _meter_watcher and _meter_watcher.running:
            return {"ok": True, "message": "The meter is already running."}
        found = meters.desk_streams()
        if not found["ok"]:
            return {"ok": False, "message": found["reason"]}
        stream = prefs["stream"] or found["streams"][0]["id"]
        # THE LEVEL WATCH FOLLOWS THE ANNOUNCER. It speaks unasked, in the background, for as long
        # as it is on, which is exactly the kind of voice that must go quiet when the announcer
        # does, recordings included.
        w = meters.Watcher(stream, _speak_if_announcing,
                           clip_at=_clip_level(prefs["warn_clip"], prefs["clip_db"]),
                           warn_silence=prefs["warn_silence"],
                           silence_after=prefs["silence_after"],
                           speak_every=prefs["speak_every"],
                           clip_sound=prefs["clip_sound"])
        if not w.start():
            return {"ok": False, "message": w.error or "The meter could not start."}
        _meter_watcher = w
    return {"ok": True, "message": f"Watching {w.name}. "
                                   + ("Clipping will be announced. " if prefs["warn_clip"] else "")
                                   + ("Going quiet will be announced. "
                                      if prefs["warn_silence"] else "")}


def _level_check_summary(w) -> str:
    """What the run came to: the measurement, then the ADVICE.

    The reading alone is only half an answer for somebody who cannot see a meter - "peaking at
    minus 3 dB" still leaves you working out whether that is fine. So a check ends with a verdict
    and, when there is something to do about it, which control to turn.
    The maximum comes from `peak_max`, the true peak over the run, not the decaying hold."""
    live = 1.0 if w.peak_max > meters.SILENCE else 0.0
    # The special cases (muted, engine not started, nothing at all) are whole answers already, with
    # their own instruction in them, so they are reported as `describe` puts them.
    if w.muted or w.wake_failed or w.peak_max <= meters.SILENCE:
        return meters.describe(w.peak_max, live, w.name or "That channel",
                               wake_failed=w.wake_failed, ceiling=w.ceiling,
                               volume_db=w.volume_db, muted=w.muted, clip_at=w.clip_at)
    # Otherwise: the measurement, then the advice. NOT `describe` plus a verdict - describe ends
    # with its own instruction, so the two together said "turn it down" twice.
    return (f"{w.name or 'That channel'}: peaked at {meters.dbfs(w.peak_max)}. "
            + meters.verdict(w.peak_max, live, volume_db=w.volume_db,
                             ceiling=w.ceiling, clip_at=w.clip_at))


# How often the level is spoken during a check. Fast enough to ride a fader by, slow enough that
# the phrases do not run into each other.
_LEVEL_CHECK_SPEAK_S = 1.5


# ---------------------------------------------------------------------------
# THE SAME CHECK, ON ONE CHANNEL. Channels > any channel.
#
# WHY IT MOVED HERE FROM DEVICE > LEVELS. The Device check meters a Windows STREAM, and every stream
# this desk offers is a MIX: "Microphone (RODECaster Pro II Main Stereo)" is everything at once. So
# it could answer "is something reaching the computer" and never "is MIC 2 right", which is the
# question you actually have while setting up. The channel is the unit of work, so the check belongs
# on the channel.
#
# IT HEARS ONE CHANNEL by the same means auto gain does: a temporary custom Chat mix carrying that
# source alone (_ChatIsolation), snapshotted and restored with the restore VERIFIED. Everything that
# class documents applies here - above all that the multitrack device is not an option, because
# switching it re-enumerates the desk's whole USB audio function.
#
# IT AGREES WITH AUTO GAIN BY CONSTRUCTION: the cross-point is post-fader, so the Watcher is given
# `scale=iso.level` and every number it speaks is in channel terms. A check run straight after auto
# gain should confirm auto gain, not argue with it - and it would have argued the moment a fader was
# anywhere but unity.
_chan_check_lock = threading.Lock()
_chan_check = None            # the Watcher
_chan_check_iso = None        # the Chat isolation it is listening through
_chan_check_label = ""        # what it is called out loud ("Mic 2")
_chan_check_ends = 0.0
# Each run gets a number, and its end-timer carries that number. Without it, a timer belonging to a
# run that has already been stopped fires later and stops whatever is running THEN - which is a real
# sequence, not a theoretical one: press stop at 8 s, start again at 10 s, and the first run's
# 30-second timer arrives 20 s into the second run and kills it.
_chan_check_id = 0


def _chan_check_state() -> dict:
    """Is a run in progress? A LIVE ISOLATION COUNTS, even with no watcher left alive.

    Answering on the watcher alone leaves a Chat mix isolated: if the watcher thread dies mid-run
    this says "not running", the next press STARTS a second run instead of stopping the first, and
    the first run's isolation - the thing that has to be put back - is dropped on the floor with
    nobody holding a reference to it. Anything that still needs restoring means a run is still in
    progress."""
    with _chan_check_lock:
        w, iso = _chan_check, _chan_check_iso
        live = w is not None or iso is not None
        return {"running": live,
                "channel": _chan_check_label if live else "",
                "seconds_left": (max(0.0, round(_chan_check_ends - time.time(), 1))
                                 if live else 0.0)}


def _chan_check_stop(speak: bool = True, run_id: int = None) -> str:
    """End the check, PUT THE CHAT MIX BACK, and say what the run came to.

    The restore is the part that must not be quiet about failing. Auto gain's processing restore was
    best-effort and silent once, it failed, and it left Mic 1 unprocessed with nothing said
    (ProjectMemory/Completed.md, auto gain). A Chat output left carrying one microphone would break
    the next call taken on this desk with nothing on screen to explain it, so a failed restore is
    spoken and
    returned, ahead of the measurement.

    `run_id` is how the end-timer says WHICH run it is ending. A timer with a stale number does
    nothing at all, rather than stopping a run somebody has only just started."""
    global _chan_check, _chan_check_iso
    with _chan_check_lock:
        if run_id is not None and run_id != _chan_check_id:
            return ""
        w, iso, _chan_check, _chan_check_iso = _chan_check, _chan_check_iso, None, None
    if w is None and iso is None:
        return ""
    try:
        if w is not None:
            w.stop()
    except Exception:
        pass
    trouble = ""
    if iso is not None:
        try:
            with _hid_lock:
                if not iso.restore():
                    trouble = ("WARNING: the Chat mix could not be put back. It may still be "
                               "carrying one channel only. Check Routing, Output mixes, Chat. ")
        except Exception as exc:
            trouble = (f"WARNING: the Chat mix could not be put back ({exc}). It may still be "
                       "carrying one channel only. Check Routing, Output mixes, Chat. ")
    # A WATCHER THAT FELL OVER MID-RUN MUST NOT BE SUMMARISED AS A QUIET CHANNEL. It reports its own
    # failure now (meters.Watcher._run), and that answer replaces the measurement rather than
    # decorating it: the peak it reached before it broke is not what the channel did.
    broke = getattr(w, "error", "") if w is not None else ""
    if broke:
        msg = trouble + broke + " Nothing was measured, so no level is being reported."
    else:
        msg = trouble + (_level_check_summary(w) if w is not None else "")
    if w is not None:
        meters.cue("stop")           # the same pair of tones auto gain uses: stop talking now
    if speak and msg:
        _ann_say(msg)
    return msg


def _chan_check_source(name: str):
    """The matrix source a channel name refers to, or (None, reason)."""
    src = next((s for s in _active_matrix().sources_for(True) if s["key"] == name), None)
    if src is None:
        return None, f"{name} is not a channel this unit exposes to the mixes."
    # CHAT IS THE ONE CHANNEL THIS CANNOT DO, and the reason is the mechanism rather than a rule:
    # the isolation listens on the Chat OUTPUT, and the Chat output is mix-minus - it deliberately
    # excludes the Chat source so a caller does not hear themselves. So there is nothing to hear.
    if src["key"] == "Chat":
        return None, ("The Chat channel cannot be checked this way: the check listens on the Chat "
                      "output, which deliberately leaves the caller out of their own mix.")
    return src, ""


def action_channel_level_check(name: str = "", seconds: float = 0.0) -> dict:
    """Talk and hear this channel's level, for as long as it takes to ride a fader. A toggle.

    It speaks the level every 1.5 s and ends with the loudest it got plus what to do about it, the
    same wording the Device check used, because the judgement is the same judgement. What is new is
    that the number is about ONE channel."""
    if _chan_check_state()["running"]:
        return {"ok": True, "running": False, "message": _chan_check_stop(speak=False)}

    global _chan_check, _chan_check_iso, _chan_check_label, _chan_check_ends, _chan_check_id
    src, why = _chan_check_source(name)
    if src is None:
        return {"ok": False, "running": False, "message": why}
    # Not while auto gain is running: it owns the same isolation and is writing a gain from it.
    ag = _autogain_state()
    if ag.get("phase") in ("preparing", "listening", "verifying", "working"):
        return {"ok": False, "running": False,
                "message": "Auto gain is running. Wait for it to finish, or stop it first."}
    gate = _matrix_write_gate()      # None when writes are allowed; a refusal dict otherwise
    if gate is not None:
        return {"ok": False, "running": False, "message": gate["message"]}

    endpoint = autogain.chat_endpoint()
    if endpoint is None:
        return {"ok": False, "running": False,
                "message": "The unit's Chat audio device is not available on this computer, so "
                           "there is no way to listen to one channel on its own."}
    # Windows out of the way on the endpoint actually being metered: its level scales every reading
    # and its mute would make a working microphone read as silence. Said in the reply, not silently.
    win = ""
    try:
        if meters.endpoint_volume(endpoint["id"]).get("muted"):
            meters.set_endpoint_mute(endpoint["id"], False)
            win = "Cleared the Windows mute on the Chat device. "
        meters.set_endpoint_volume_db(endpoint["id"], 0.0)
    except Exception:
        pass

    prefs = _meter_prefs()
    secs = float(seconds) if seconds and seconds > 0 else prefs["check_seconds"]
    secs = max(2.0, min(300.0, secs))
    try:
        with _hid_lock:
            iso = _ChatIsolation(src["index"])
            iso.__enter__()
    except Exception as exc:
        # THE COMMON REFUSAL, and it needs an instruction rather than a diagnosis. A channel whose
        # fader is right down does not reach the Chat mix at all, so there is nothing to listen to,
        # and on a desk whose Chat preset carries one mic that is most of them. The isolation's own
        # wording explains the mixer; this adds what to DO.
        why = str(exc)
        if "not reaching the Chat mix" in why:
            return {"ok": False, "running": False,
                    "message": (f"{src['label']} is not reaching the Chat mix, which is what the "
                                f"check listens on, so there is nothing to measure. Bring that "
                                f"channel's fader up, or check it is not excluded from Chat under "
                                f"Routing, Output mixes.")}
        return {"ok": False, "running": False, "message": f"Could not listen to {src['label']} on "
                                                          f"its own: {why}"}
    w = meters.Watcher(endpoint["id"], _ann_say,
                       clip_at=_clip_level(True, prefs["clip_db"]), clip_gap=4.0,
                       warn_silence=False,        # a check is not a watch: no went-silent nagging
                       speak_every=_LEVEL_CHECK_SPEAK_S,
                       clip_sound=prefs["clip_sound"],
                       scale=iso.level, label=src["label"])
    if not w.start():
        err = w.error or "Could not open the Chat stream."
        with _chan_check_lock:
            _chan_check, _chan_check_iso = None, iso
        _chan_check_stop(speak=False)             # the mix still has to go back
        return {"ok": False, "running": False, "message": err}
    with _chan_check_lock:
        _chan_check_id += 1
        run_id = _chan_check_id
        _chan_check, _chan_check_iso = w, iso
        _chan_check_label, _chan_check_ends = src["label"], time.time() + secs
    # The timer carries the run's number, so a timer left over from a run that has already been
    # stopped cannot end the one running now.
    threading.Timer(secs, _chan_check_stop, kwargs={"run_id": run_id}).start()
    # A BEEP RATHER THAN A SENTENCE to say it has started. The check speaks a level within a second
    # and a half, so anything said here is read over the first reading - and this is the moment the
    # user starts talking, which is not a moment to be listening to the computer.
    meters.cue("start")
    return {"ok": True, "running": True, "seconds": secs, "channel": src["label"],
            "message": win, "level_note": meters.level_note(w.volume_db)}


def channel_level_check_status() -> dict:
    st = _chan_check_state()
    st["ok"] = True
    return st


def action_set_output_mute(which: str, on: bool) -> dict:
    """Mute or unmute a whole OUTPUT: the monitor speakers, or the Bluetooth send.

    Both selectors come from the block rule and are proven by a read-back-verified write. The
    accessibility case is the channel mute's, only stronger: killing the speakers is what you reach
    for when somebody walks in, and the only other way to do it is a control on the unit you have to
    find by touch.

    The two are one endpoint rather than two because they are the same idea with a different target,
    and because the message has to draw the SAME distinction in both directions: muting an output
    changes what somebody HEARS, never what is recorded or streamed."""
    targets = {
        "monitor": ("outputMonMute", "Monitor speakers",
                    "your speakers are silent; headphones, the recording and your streams are not"),
        "bluetooth": ("outputBTMute", "Bluetooth output",
                      "a paired phone hears nothing from the desk; everything else is unaffected"),
    }
    if which not in targets:
        return {"ok": False, "message": f"Unknown output: {which}."}
    prop, label, effect = targets[which]
    gate = _level_write_gate()
    if gate is not None:
        return gate
    try:
        with _hid_lock:
            rcp_transfer.set_unit_setting(prop, bool(on))
    except Exception as exc:
        return {"ok": False, "message": f"Could not change the {label.lower()} mute: {exc}"}
    return {"ok": True, "message": (f"{label} muted: {effect}." if on
                                    else f"{label} unmuted.")}


def action_set_bt_output_level(percent: int) -> dict:
    """Set the BLUETOOTH OUTPUT level, 0-100 percent (outputBTLevel).

    This is what a paired phone HEARS from the desk. It is not the Bluetooth channel on the Channels
    page, which is what the phone SENDS us; the two have almost the same name and opposite meanings,
    so every label and message about this one says "output" out loud."""
    gate = _level_write_gate()
    if gate is not None:
        return gate
    pct = max(0, min(100, int(percent)))
    try:
        with _hid_lock:
            rcp_transfer.set_unit_setting("outputBTLevel", pct / 100.0)
    except Exception as exc:
        return {"ok": False, "message": f"Could not set the Bluetooth output level: {exc}"}
    return {"ok": True, "percent": pct,
            "message": f"Bluetooth output level set to {pct} percent: that is what a paired phone "
                       f"hears from the desk."}


# THERE IS NO "Free the encoder" ACTION, and it must not come back on the reasoning that a channel
# button taking the encoder can only be undone by a POWER CYCLE. The channel button TOGGLES:
# measured, systemChannelSelected goes 0 on the press and -1 on a second press of the same button,
# while two encoder presses in between leave it at 0. The announcer names the holding channel and
# names that way out, so a systemChannelSelected = -1 write earns nothing.


def action_reset_faders() -> dict:
    """Restore the factory-default fader layout in one go (direct set per position, no swap
    logic so it lands exactly on the default). Device-aware: a device whose clean default vector
    is NOT confirmed (devices.FaderSpec.default_vector is None) REFUSES rather than writing a
    guessed layout. Both shipped desks HAVE a confirmed vector, so neither refuses today; the
    guard is for the next desk added.

    THE CHAIN FOLLOWS THE CHANNEL HERE TOO, exactly as in action_set_fader. Without it, a reset
    drops USB 1 onto a strip holding a mic voicing with the noise gate on and music beds start
    gating. Processing is per fader STRIP and a reset moves EVERY channel at once, so it is the
    single move most likely to hand a channel the wrong chain."""
    gate = _fader_write_gate()
    if gate is not None:
        return gate
    spec = _active_faders()
    if not spec.default_vector:
        dev = devices.get_active_device()
        return {"ok": False,
                "message": (f"Resetting the faders to a default layout is not available on the "
                            f"{dev.name} yet: its factory-default fader layout has not been "
                            f"confirmed, so this control stays disabled rather than write a "
                            f"guessed layout.")}
    chain_note = ""
    try:
        with _hid_lock:
            # One dump serves both jobs: the layout we are about to replace, and the per-strip
            # chains as they stand BEFORE the write, which is what tells us whose chain is whose.
            blob = rcp_read.read_model()
            old = _read_fader_vector(blob, spec)
            chains_before = channels.read_chains(blob) if _chain_follow_on() else None
            for i, code in enumerate(spec.default_vector):
                rcp_transfer.set_fader_source(i, code)
            channels.invalidate_strip_vector()   # the layout is the processing strip map
            new = _read_fader_vector(spec=spec)
            if chains_before is not None:
                chain_note = _follow_chains(old, new, chains_before)
    except Exception as exc:
        return {"ok": False, "message": f"Could not reset the faders: {exc}"}
    labels = ", ".join(_channel_name(c, spec) for c in spec.default_vector)
    msg = f"Faders reset to the default layout: {labels}."
    if chain_note:
        msg += " " + chain_note
    return {"ok": True, "faders": new, "labels": [_channel_name(c, spec) for c in new],
            "stereo": stereo_link_state(new, spec), "message": msg}


# --- MONITOR MIX: per-output source levels (decoded and proven on hardware) ------------------
# Set how loud each source is in ONE output (e.g. your headphones), independent of the recording
# and other outputs. Uses the matrix cross-point level (mixLevelWithAnchor): the output must be in
# Custom mode, then we unlink the one source and set its absolute level. Reading is reliable (the
# 13*source+output grid formula), so we prefill and can reset cleanly. Refused during a SIP call.
# The monitor-mix outputs are the headphone/monitor outputs (matrix indices 0-4); we intersect
# this with what the CONNECTED device actually exposes (below), so the Duo shows only its Headphone
# 1/2 and the Pro II shows Headphone 1-4 + Monitor. Source labels + indices come straight from the
# device matrix (so the Duo's code 2 reads "Headset", not "Mic 3").
_MONITOR_OUTPUT_INDICES = (0, 1, 2, 3, 4)
# Spelled exactly as the mode dropdown a few groups up the same page spells them (matrix_model),
# so the two are not read aloud as if they were different settings.
OUTPUT_MODE_LABEL = {0: "Main Mix", 1: "Mix Minus", 2: "Custom"}


def _monitor_outputs():
    """(index, label) for each monitor-mix output the connected device exposes, device-labelled."""
    dev_out = {o["index"]: o["label"] for o in _active_matrix().outputs}
    return [(i, dev_out[i]) for i in _MONITOR_OUTPUT_INDICES if i in dev_out]


def _monitor_sources():
    """(index, label) for each matrix source on the connected device (device-labelled)."""
    return [(s["index"], s["label"])
            for s in _active_matrix().sources_for(channels.usb_expanded())]


def monitor_mix_model() -> dict:
    """Static layout for the Monitor mix UI (the outputs + sources the connected device exposes)."""
    read_only = _matrix_write_gate() is not None
    note = ("Set how loud each source is in a chosen output, for example your own headphones, "
            "WITHOUT changing the recording or other people's outputs. Adjusting a slider "
            "switches that output to a custom mix; \"Reset this output to follow the faders\" "
            "returns it to following the faders. Not available while a phone caller is connected.")
    if read_only:
        note = ("Shows how loud each source is in each headphone output on this device. Changing "
                "these levels is not enabled here yet, so the sliders are read-only.")
    return {
        "outputs": [{"index": i, "label": l} for i, l in _monitor_outputs()],
        "sources": [{"index": i, "label": l} for i, l in _monitor_sources()],
        "read_only": read_only,
        "note": note,
    }


def read_monitor_mix_live() -> dict:
    """Live per-output, per-source levels (0-100 percent) + each output's mode."""
    try:
        with _hid_lock:
            r = rcp_read.read_matrix_levels()
    except Exception as exc:
        return {"ok": False, "message": f"Could not read the mix levels: {exc}"}
    if not r.get("ok"):
        return {"ok": False, "message": r.get("reason", "Mix levels unavailable.")}
    lv, modes = r["levels"], r["modes"]
    outs = {}
    for oi, _l in _monitor_outputs():
        srcs = {}
        for si, _sl in _monitor_sources():
            cell = lv.get(oi, {}).get(si, {})
            pct = cell.get("level")
            srcs[str(si)] = {"percent": (round(pct * 100) if pct is not None else None),
                             "linked": cell.get("linked")}
        outs[str(oi)] = {"mode": modes.get(oi),
                         "mode_label": OUTPUT_MODE_LABEL.get(modes.get(oi), "?"),
                         "sources": srcs}
    return {"ok": True, "outputs": outs}


def action_set_monitor_level(output_index: int, source_index: int, level01: float) -> dict:
    """Set one source's level in one output. Switches the output to Custom mode if needed, unlinks
    that source, then writes the level. Only affects this output."""
    gate = _matrix_write_gate()
    if gate is not None:
        return gate
    mon_out = dict(_monitor_outputs())
    mon_src = dict(_monitor_sources())
    if output_index not in mon_out:
        return {"ok": False, "message": "That output is not adjustable here."}
    if source_index not in mon_src:
        return {"ok": False, "message": "That source is not adjustable here."}
    try:
        v = max(0.0, min(1.0, float(level01)))
        with _hid_lock:
            # Ensure Custom mode + the source unlinked. Both writes are idempotent (no-ops if
            # already so), so we send them unconditionally rather than pay a ~1.5s model read
            # to check first - that read was the cause of the per-slider lag.
            sel = rcp_transfer.cross_point_selector(output_index, source_index)
            rcp_transfer.set_output_routing(output_index, 2)       # Custom
            rcp_transfer.send_record(rcp_transfer.build_mix_link_request(sel, link=False))
            rcp_transfer.set_cross_point_level(output_index, source_index, v)
    except Exception as exc:
        return {"ok": False, "message": f"Could not set the level: {exc}"}
    # A cross-point write comes back as mixLevelWithAnchor on that SOURCE, which is exactly what the
    # announcer speaks as a fader move - so without this, every matrix slider is announced twice.
    # Source index and source code are the same space here (both address the cross-point grid).
    _ann_mark_self_write(("fader", int(source_index)))
    return {"ok": True,
            "message": f"{mon_src[source_index]} in {mon_out[output_index]} "
                       f"set to {round(v * 100)} percent."}


def action_reset_monitor_output(output_index: int) -> dict:
    """Reset one output to follow the faders: re-link every source (level back to the fader value)
    and put the output back to Main Mix mode."""
    gate = _matrix_write_gate()
    if gate is not None:
        return gate
    mon_out = dict(_monitor_outputs())
    if output_index not in mon_out:
        return {"ok": False, "message": "That output is not adjustable here."}
    try:
        with _hid_lock:
            for si, _ in _monitor_sources():
                sel = rcp_transfer.cross_point_selector(output_index, si)
                rcp_transfer.send_record(rcp_transfer.build_mix_link_request(sel, link=True))
                time.sleep(0.05)
            rcp_transfer.set_output_routing(output_index, 0)       # Main Mix
    except Exception as exc:
        return {"ok": False, "message": f"Could not reset the output: {exc}"}
    return {"ok": True,
            "message": f"{mon_out[output_index]} reset to follow the faders."}


def action_reset_all_outputs() -> dict:
    """Reset EVERY output mix on the desk: re-include and re-link every source on every output,
    then put each output back to its default mode (devices.py, MatrixSpec.outputs default_mode).

    A wider sweep than the per-output "Reset to faders". It exists because a Custom headphone mix
    silently breaks channel Mute: mute takes a channel out of the MAIN mix, so a Custom output keeps
    carrying it and you still hear a muted mic in your own cans.

    HONESTY: the default modes are a KNOWN-GOOD set taken from a saved profile, NOT a capture of a
    factory-reset unit (see the provenance comment on PRO2_MATRIX_OUTPUTS). So this is called
    "defaults", never "factory". Every write is reversible and read-back verifiable, and the UI
    tells the user to save a profile first.

    THIS IS THE "Everything normal" BUILT-IN MIX SCHEME, applied through the one scheme path
    (routing_schemes.apply). The button stays because it is where people look for it and it carries
    its own confirmation, but there is no second copy of the sweep to drift."""
    b = routing_schemes.builtin(routing_schemes.BUILTIN_NORMAL)
    if b is None:                       # no matrix outputs on this device model at all
        return {"ok": False, "message": "This device exposes no output mixes to reset."}
    return _apply_scheme(b["scheme"], b["name"])


def _apply_matrix(routing: dict) -> dict:
    """Re-apply a v2 profile's output mix matrix. CALLER HOLDS THE HID LOCK. Returns
    {applied, failed, skipped}. Skips cleanly for a legacy profile or one saved while the matrix
    was unreadable (a phone caller).

    TWO SHAPES, because a profile saved by an older build has only the legacy keys:
      - `mix_scheme`, what a current build saves: the WHOLE matrix, keyed by name, applied
        through routing_schemes.apply - the same proven path the Mix schemes page uses. Every
        output's cross-point levels come back, not just the monitor ones, and an output the
        connected desk does not have is skipped by name rather than written by index.
      - `output_modes` + `matrix_excluded` + `monitor_unlinked`: the old index-keyed form, below.
    """
    # The saved matrix is Pro II-shaped; a desk that cannot write the matrix (e.g. the Duo, cap off)
    # must not have Pro II cross-points written to it, so skip the matrix re-apply there.
    if _matrix_write_gate() is not None:
        return {"applied": 0, "failed": 0, "skipped": True}
    scheme = routing.get("mix_scheme")
    if scheme and scheme.get("outputs"):
        # Same announcer suppression as _apply_scheme: our own cross-point writes come back on the
        # push stream as mixLevelWithAnchor on the SOURCE, which the announcer would otherwise
        # narrate as a hundred fader moves over the top of the restore's own progress.
        res = routing_schemes.apply(
            scheme, on_source_write=lambda si: _ann_mark_self_write(("fader", int(si))))
        return {"applied": res["applied"], "failed": res["failed"], "skipped": False}
    modes = routing.get("output_modes")
    if not modes:
        return {"applied": 0, "failed": 0, "skipped": True}
    output_count = _active_matrix().output_count
    applied = 0
    failed = 0

    def attempt(fn):
        nonlocal applied, failed
        try:
            fn(); applied += 1
        except Exception:
            failed += 1
        time.sleep(0.03)

    # ---- LEGACY FORM ONLY, for profiles saved before the mix_scheme capture. -----------------
    # 1. Per-output mode (0 Main / 1 Mix Minus / 2 Custom).
    for o, mode in enumerate(modes):
        if 0 <= o < output_count and mode in (0, 1, 2):
            attempt(lambda o=o, mode=mode: rcp_transfer.set_output_routing(o, int(mode)))
    # 2. Custom-mix excludes.
    for pair in (routing.get("matrix_excluded") or []):
        try:
            o, s = int(pair[0]), int(pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        attempt(lambda o=o, s=s: rcp_transfer.set_cross_point_excluded(o, s, True))
    # 3. Monitor mix: relink the exposed monitor cross-points (reset to faders), then re-apply any
    #    saved manual (unlinked) levels so the restore is authoritative, not additive.
    for oi, _l in _monitor_outputs():
        for si, _sl in _monitor_sources():
            attempt(lambda oi=oi, si=si: rcp_transfer.send_record(
                rcp_transfer.build_mix_link_request(rcp_transfer.cross_point_selector(oi, si), link=True)))
    for cell in (routing.get("monitor_unlinked") or []):
        try:
            o, s, lvl = int(cell["o"]), int(cell["s"]), float(cell["level"])
        except (TypeError, ValueError, KeyError):
            continue

        def _set(o=o, s=s, lvl=lvl):
            sel = rcp_transfer.cross_point_selector(o, s)
            rcp_transfer.send_record(rcp_transfer.build_mix_link_request(sel, link=False))
            rcp_transfer.set_cross_point_level(o, s, lvl)
        attempt(_set)
    return {"applied": applied, "failed": failed, "skipped": False}


def routing_model() -> dict:
    """Static layout of the routing controls the UI renders.

    NOTE: output routing MODE (outputMixMinus, Main Mix/Mix-Minus/Custom per output)
    is intentionally NOT exposed yet. Its per-output read is unreliable (the property
    recurs ~23x in the model dump so stream position drifts) and the output ordering
    is not fully pinned, so writing it can hit the wrong output. Re-add once decoded.
    """
    return {
        # Still sent, but no page reads it any more: the multitrack control moved to the Recording
        # page, which builds its own list from recorder.MULTITRACK_LABEL. Kept because the routing
        # payload is also what the profile-coverage tooling reads.
        "multitrack_modes": [[k, v] for k, v in sorted(recorder.MULTITRACK_LABEL.items())],
        # USB audio's option list. The STRUCTURE endpoint carries it because loadRouting builds the
        # controls once from this payload; the VALUES arrive with /api/routing/live.
        "usb_multitrack_modes": [[k, v] for k, v in sorted(recorder.USB_MULTITRACK_LABEL.items())],
        # EXPANDED'S OPTIONS BELONG HERE TOO, for the same reason and with the same consequence if
        # they are missing: `setStagedValue` invents a "Value true" option for a device value it has
        # no label for, so the control renders with ONE option it cannot be changed away from. The
        # live payload sends these as well, which is what hid it.
        "usb_expanded_labels": [[k, v] for k, v in sorted(recorder.USB_EXPANDED_LABEL.items())],
        "usb_expanded_hint": recorder.USB_EXPANDED_HINT,
        "input_strips": [{"key": k, "label": lab}
                         for k, _idx, lab in _active_input_strips().strips],
        "input_sources": _active_input_strips().source_palette,
        "fader_model": fader_model(),
        # Headphone-type controls, device-aware (GAP-6): one select per addressable headphone output,
        # populated with that device's amp-type value list. Both desks expose HP1 + HP2.
        "headphones": {
            "outputs": [{"index": i, "label": lab}
                        for i, (_sel, lab) in enumerate(_active_headphones().outputs)],
            "values": [[code, lab] for code, lab in _active_headphones().values],
        },
        # Master/global output controls (multitrack, auto-mute, headphone type, Compellor, delay)
        # are read-only on a device without the "output_write" cap (the Duo, pending capture).
        "output_write_read_only": _output_write_gate() is not None,
        # Faders 1 to 4 are NOT fixed mic inputs: fader_model offers every fader the full assignable
        # list, mic faders included. Do not describe them as fixed here.
        "note": ("Everything about where sound goes: which channel sits on each fader, what each "
                 "output can hear, the levels the unit normally keeps to itself, and how it "
                 "records. Changes are sent to the unit immediately, and this page reads the "
                 "unit's current values every time you open it."),
        "matrix": matrix_model(),
        "monitor_mix": monitor_mix_model(),
    }


def read_usb_driver() -> dict:
    """The Routing page's driver line on its own, for its poll. No device read: the service's
    status block and Windows' endpoint list, with the desk's USB mode as last published (every
    re-enumeration republishes it), so the line follows a bind landing, a retry, or the desk
    leaving Expanded without the page being re-read."""
    known = channels.usb_expanded_known()
    return {"ok": True,
            "usb_driver": virtual_devices.describe(channels.usb_expanded() if known else None)}


def read_routing_live() -> dict:
    """Live-read routing-related properties for the UI."""
    try:
        with _hid_lock:
            blob = rcp_read.read_model()
    except Exception as exc:
        return {"ok": False, "message": f"Could not read routing from the device: {exc}"}

    def first(name):
        return rcp_read.first(blob, name)

    cis = rcp_read.values(blob, "channelInputSource")
    input_sources = {strip: (cis[idx] if idx < len(cis) else None)
                     for strip, idx in _active_input_strips().strip_vec_index.items()}
    # All headphoneType values in dump order (GAP-6), mapped positionally to the device's outputs.
    headphone_types = [v for v in rcp_read.values(blob, "headphoneType")
                       if v is not None and v != ""]
    usb_mode, usb_expanded = recorder.split_usb_multitrack(
        rcp_read.first(blob, "outputMultiMode", "int"))
    # Publish it exactly as the strip vector above is published: this is the routing read every
    # page view makes, so the announcer and the fader labels get the mode for free rather than
    # dumping the model from a thread that must not.
    channels.publish_usb_expanded(usb_expanded)
    return {
        "ok": True,
        "input_sources": input_sources,
        "faders": [cis[i] if i < len(cis) else None for i in range(_active_faders().count)],
        # Off the vector we already have, so the arrival read still costs one dump.
        "stereo": stereo_link_state(list(cis), _active_faders()),
        # NAMES as well as codes, because a fader can hold a stereo pair and the dropdown's option
        # list only carries the ASSIGNABLE channels. Without this the browser would have to invent a
        # label and would get "Channel code 6" for a linked pair.
        "fader_labels": [_channel_name(cis[i] if i < len(cis) else None, _active_faders())
                         for i in range(_active_faders().count)],
        "multitrack": first("recordingMultitrackMode"),
        # USB audio, this page's own tab. Read by KIND as well as by name:
        # both fields are single-instance, and matching the type is what stops a same-named field
        # elsewhere in the dump being read as this one.
        # SPLIT, because the one property holds the output mode and the input mode (recorder.py).
        # Sending the raw value would make the browser show "Just the mix" for a desk that is
        # actually in Expanded.
        "usb_multitrack": usb_mode,
        "usb_expanded": usb_expanded,
        "usb_expanded_labels": [[k, v] for k, v in sorted(recorder.USB_EXPANDED_LABEL.items())],
        "usb_expanded_hint": recorder.USB_EXPANDED_HINT,
        # The driver's side of Expanded, in words, so the page can say whether the four inputs
        # will actually carry anything. Read from Windows, not the desk.
        "usb_driver": virtual_devices.describe(usb_expanded),
        "usb_multitrack_modes": [[k, v] for k, v in sorted(recorder.USB_MULTITRACK_LABEL.items())],
        "usb_processing_bypass": rcp_read.first(blob, "outputMultiBypass", "bool"),
        "bypass_hint": recorder.BYPASS_HINT,
        "bypass_needs_multitrack": recorder.BYPASS_NEEDS_MULTITRACK,
        # READ BY BLOCK, and these two specifically must not go back to a by-name read. The reason
        # is NOT prefix matching: the tree index matches names whole, so "outputMonAutoMute" cannot
        # collide with "outputMonAutoMuteActive". It is that a name can occur in MORE THAN ONE
        # BLOCK, and then "first in the stream" is a coin toss - the btVisible case, where the
        # SYSTEM copy and the NETWORK copy are different facts and only one drives the radio. Say
        # which block you mean and the question does not arise.
        "monitor_auto_mute": rcp_read.block_value(blob, "OUTPUT", "outputMonAutoMute"),
        "bt_auto_mute": rcp_read.block_value(blob, "OUTPUT", "outputBTAutoMute"),
        # Per-output headphone types (GAP-6): the model dump carries one headphoneType per output
        # (4 on the Duo: HP1,HP2,HP3,HP4); we map them POSITIONALLY to the device's exposed headphone
        # outputs (HP1 then HP2). This dump->output ordering is HARDWARE-CONFIRMED on the Duo:
        # HP1=0x09 moves dump position 0, HP2=0x0A moves position 1, value 2 accepted and read back,
        # restored byte-clean. headphone_type is kept as the HP1 value for compatibility.
        "headphone_type": headphone_types[0] if headphone_types else None,
        "headphone_types": [headphone_types[i] if i < len(headphone_types) else None
                            for i in range(len(_active_headphones().outputs))],
        **rcp_read.master_processing(blob),
        "message": "Read routing from the device.",
    }



def action_set_input_source(strip: str, source_code: int) -> dict:
    spec = _active_input_strips()
    if strip not in spec.strip_vec_index:
        return {"ok": False, "message": f"Channel strip {strip} is not assignable."}
    if source_code not in spec.valid_source_codes:
        return {"ok": False, "message": f"Source {source_code} is not valid."}
    # This writes channelInputSource (the fader vector), so it is a fader-assignment write and gates
    # on the same "fader_write" cap as action_set_fader (GAP-1): a device whose fader-write model is
    # unconfirmed (the Duo) must refuse rather than drive an unconfirmed layout. Pro II: cap ON, so
    # this returns None and behaviour is unchanged.
    gate = _fader_write_gate()
    if gate is not None:
        return gate
    with _hid_lock:
        rcp_transfer.set_channel_input_source(strip, source_code)
        channels.invalidate_strip_vector()   # the layout is the processing strip map
    src = spec.source_label.get(source_code, str(source_code))
    return {"ok": True, "message": f"{spec.strip_label[strip]} source set to {src}."}


def action_set_multitrack(mode: int) -> dict:
    gate = _output_write_gate()
    if gate is not None:
        return gate
    if mode not in rcp_transfer.MULTITRACK_MODES:
        return {"ok": False, "message": f"Multitrack mode {mode} is not valid."}
    with _hid_lock:
        rcp_transfer.set_multitrack_mode(mode)
    # Answered in the same words the control offers (recorder.MULTITRACK_LABEL), not in the unit's
    # own "Pre-Fader" / "Post-Fader", which never say whether you are getting multitrack at all.
    return {"ok": True,
            "message": f"Recording set to {recorder.MULTITRACK_LABEL[mode].lower()}."}


def action_set_recording_format(value: int) -> dict:
    """WAV or MP3 for the unit's own recordings. Refuses anything else, deliberately.

    The desk would ACCEPT a 2 or a 99 and store it, so the validation here is the only thing that
    keeps it on a value its own touchscreen can represent (see rcp_transfer.set_recording_format).
    """
    gate = _output_write_gate()
    if gate is not None:
        return gate
    if value not in recorder.RECORDING_FORMAT_LABEL:
        return {"ok": False, "message": f"Recording format {value} is not valid."}
    with _hid_lock:
        rcp_transfer.set_recording_format(value)
    # NOT lowercased, unlike the multitrack reply: these labels open with an initialism, and
    # "written as mp3, smaller files" reads as a typo and is spoken as a word by some voices.
    return {"ok": True,
            "message": f"Recordings will be saved as "
                       f"{recorder.RECORDING_FORMAT_LABEL[value]}."}


def action_set_usb_multitrack(mode: int) -> dict:
    """What the desk sends this computer over USB: the mix pair, or every channel.

    THE WARNING IS PART OF THE REPLY, not an aside. Changing this makes the desk tear down and
    re-advertise its whole USB audio function (measured): every RODECaster input and
    output on this machine disappears for a second or two, and Windows can move the default device
    while they are gone. Someone listening THROUGH the unit hears it stop, so the reply says so
    rather than leaving a silence to be interpreted."""
    gate = _output_write_gate()
    if gate is not None:
        return gate
    if mode not in recorder.USB_MULTITRACK_LABEL:
        return {"ok": False, "message": f"USB multitrack mode {mode} is not valid."}
    with _hid_lock:
        # READ FIRST so the input half is carried through. The desk holds both in one property, so
        # writing the mode alone would take a desk out of Expanded without being asked to.
        _, expanded = recorder.split_usb_multitrack(
            rcp_read.first(rcp_read.read_model_stable(fresh=True), "outputMultiMode", "int"))
        value = recorder.join_usb_multitrack(mode, expanded)
        arrived = _write_usb_mode(value)
    return {"ok": True,
            "message": (f"This computer will now receive "
                        f"{recorder.USB_MULTITRACK_LABEL[mode].lower()}. The unit's audio devices "
                        f"drop out for a couple of seconds while it changes over."
                        + _virtual_devices_follow(value, arrived))}


def _clear_extended_faders() -> list:
    """Take Game, Music, A and B off every fader that carries one, and return their names.

    THE TOUCHSCREEN DOES THIS AND THE DESK DOES NOT. Leaving Expanded on the unit's screen asks
    "All extended inputs will be unassigned" and empties those faders; the same change written over
    USB leaves them assigned to inputs nothing can feed, so the faders go dead. Each channel's
    processing is parked by `_follow_chains`, exactly as when a channel is moved off a fader by
    hand. Holds the HID lock. Writes nothing when no fader carries one of the four."""
    spec = _active_faders()
    extended = {code for code, _label in spec.expanded_sources}
    if not extended or _fader_write_gate() is not None:
        return []
    with _hid_lock:
        blob = rcp_read.read_model()
        old = _read_fader_vector(blob, spec)
        dead = [i for i, code in enumerate(old) if code in extended]
        if not dead:
            return []
        chains_before = channels.read_chains(blob) if _chain_follow_on() else None
        for i in dead:
            rcp_transfer.set_fader_source(i, -1)
        channels.invalidate_strip_vector()
        new = _read_fader_vector(spec=spec)
        if chains_before is not None:
            _follow_chains(old, new, chains_before)
    labels = spec.labels_for(True)
    return [labels.get(old[i], f"source {old[i]}") for i in dead]


def _tidy_after_standard(spoken: bool) -> str:
    """Empty the faders the Expanded four left behind once the desk is in Standard, and word it.

    For the cases the app did not cause: the desk dropping out of Expanded on its own, or being
    found in Standard at start-up with the four still on faders. `spoken` sends the sentence to the
    announcer as well as returning it."""
    try:
        cleared = _clear_extended_faders()
    except Exception as exc:
        diagnostics.event("usb_mode.tidy_failed", f"{exc}")
        return ""
    if not cleared:
        return ""
    msg = (f"USB input is Standard, so {_join_and(cleared)} cannot carry anything. Took them off "
           f"their faders; their processing is kept.")
    if spoken:
        _ann_say(msg)
    return msg


def action_set_usb_expanded(on: bool) -> dict:
    """The USB 1 INPUT half of the same property: Standard, or Expanded with four more app inputs.

    THE REPLY CARRIES BOTH COSTS, because neither is visible from the app. Like the output mode
    above, the change re-advertises the desk's whole USB audio function and the audio devices
    vanish for a second or two. Turning it OFF also takes Game, Music, A and B off their faders,
    as the unit's own screen does, and that is done FIRST, while the desk is still on the bus: the
    mode change takes it off USB for a couple of seconds."""
    gate = _output_write_gate()
    if gate is not None:
        return gate
    if on is None:
        return {"ok": False, "message": "Expanded needs on or off."}
    cleared = [] if on else _clear_extended_faders()
    with _hid_lock:
        mode, _ = recorder.split_usb_multitrack(
            rcp_read.first(rcp_read.read_model_stable(fresh=True), "outputMultiMode", "int"))
        value = recorder.join_usb_multitrack(mode or 0, bool(on))
        arrived = _write_usb_mode(value)
    tail = ("" if on or not cleared else
            f" {_join_and(cleared)} came off their faders, as the unit's own screen does; their "
            f"processing is kept.")
    return {"ok": True,
            "message": (f"USB 1 will now send {recorder.USB_EXPANDED_LABEL[bool(on)].lower()}. "
                        f"The unit's audio devices drop out for a couple of seconds while it "
                        f"changes over.{tail}" + _virtual_devices_follow(value, arrived))}


def _seed_usb_expanded() -> None:
    """Publish the desk's USB mode once at startup, so no page has to be opened first.

    Takes the HID lock like any other read and never raises: a desk that is not there yet simply
    leaves the flag unpublished, and the next routing read or announcer seed fills it in."""
    try:
        if not channels.usb_expanded_known():
            with _hid_lock:
                raw = rcp_read.first(rcp_read.read_model_stable(fresh=True), "outputMultiMode",
                                     "int")
            if raw is not None:
                channels.publish_usb_expanded(recorder.split_usb_multitrack(raw)[1])
        # A desk found in Standard with the Expanded four still on faders: tidy it now.
        if channels.usb_expanded_known() and not channels.usb_expanded():
            _tidy_after_standard(spoken=True)
    except Exception as exc:
        diagnostics.event("usb_mode.seed_failed", f"{exc}")


_virtual_devices_busy = threading.Lock()
_virtual_devices_watch_stop = threading.Event()


def _virtual_devices_repair() -> None:
    """Windows' endpoints moved: make them agree with the driver again. Silent, and never speaks.

    THE LOCK IS WHY THIS IS SAFE DURING A MODE CHANGE. `_virtual_devices_follow` holds it for the
    whole of its wait and its retries, and those retries re-enumerate the desk on purpose, so every
    endpoint change they cause is skipped here rather than fought over. What reaches this is a
    change with no follow-up behind it, which is exactly the case that used to go unrepaired.

    `move_default` stays off: a disable is not a reason to take the output device away from
    wherever the user has put it, and the mode-change path already moves it when it has to.

    A repair is itself an endpoint change, so it wakes the watcher again. That settles rather than
    runs away because `reconcile` is idempotent: the second pass finds Windows already agreeing and
    changes nothing, so nothing wakes a third."""
    if not _virtual_devices_busy.acquire(blocking=False):
        return
    try:
        out = virtual_devices.reconcile(move_default=False)
        if out.enabled or out.disabled:
            diagnostics.event("virtual_devices.repaired", out.status.phrase(),
                              enabled=len(out.enabled), disabled=len(out.disabled))
    except Exception as exc:
        diagnostics.event("virtual_devices.repair_failed", f"{exc}")
    finally:
        _virtual_devices_busy.release()
# Set while the follow-up is re-enumerating the desk on purpose, so the announcer's reconnect
# does not narrate each flip of the mode as news.
_virtual_devices_retrying = threading.Event()

# RODE'S SERVICE BINDS ON SOME OF THE DESK'S ARRIVALS, and never later. Measured: twelve
# arrivals, six bound, in every mode and with nothing of ours open; a quiet desk stayed unbound
# for five minutes; nothing writable from user mode (Enable, its command block) re-runs the
# bind. What does is another arrival, so the follow-up makes one itself by flipping the output
# mode away and back.
#
# THE ODDS ARE NOT EVEN, so three attempts are not the nine in ten they look like: failures come
# in runs, and one measured nine arrivals for two binds. A replug is one more arrival at whatever
# the current odds are, which is why nothing here asks for one.
_VIRTUAL_DEVICES_RETRIES = 3
_VIRTUAL_DEVICES_RETRY_HOLD_S = 3.0     # at the other mode: long enough for the service to let go


def _desk_pid():
    """The desk's USB product id now, or None when it is off the bus."""
    iface = rcp_hid.find_control_interface()
    return iface.product_id if iface is not None else None


def _write_usb_mode(value: int) -> bool:
    """Write `outputMultiMode` and return whether the desk came back as a different USB device.

    Only that is an ARRIVAL for RODE's service, which matches the desk by product id. Pre-fader
    and post-fader in Expanded are the same device (0x0094) and the desk does not leave the bus
    between them, so that switch gives the service nothing to bind to and is not a bind attempt.
    Caller holds the HID lock."""
    before = _desk_pid()
    rcp_transfer.set_usb_multitrack_mode(value)
    return _desk_pid() != before


def _desk_usb_mode():
    """`outputMultiMode` as the desk holds it now, or None when it cannot be read. Takes the HID
    lock."""
    try:
        with _hid_lock:
            return rcp_read.first(rcp_read.read_model_stable(fresh=True), "outputMultiMode", "int")
    except Exception:
        return None


def _virtual_devices_expectation(raw_mode):
    """What `virtual_devices.reconcile` should expect of the service in this mode. True: the
    desk is sending every channel in Expanded, so a bind should come and is waited for. None:
    the mode is unknown, or Expanded with the mix alone, where the service may bind and its flag
    is trusted as read. False: Standard, where it cannot be bound at all."""
    mode, expanded = recorder.split_usb_multitrack(raw_mode)
    if expanded is None:
        return None
    if not expanded:
        return False
    return True if mode else None


def _virtual_devices_follow(raw_mode, arrived: bool) -> str:
    """Bring RODE's virtual devices into line with the USB mode the desk now holds, and say so.

    The work runs on a thread and its outcome is SPOKEN, because the service takes seconds to
    bind after the desk re-enumerates, longer with retries, and a reply held that long reads as
    a hang. The string returned is the reply's tail: what the user should know now, which is
    only the case where nothing is coming (no driver, or a mode it will not be waited for in).

    THE DESK'S MODE IS RE-READ BEFORE EVERY RETRY AND BEFORE ANYTHING IS SAID. A follow-up with
    its retries runs for over a minute, and only one runs at a time, so a mode the person
    changes meanwhile gets no follow-up of its own: this one moves on to it instead. Retrying
    with the mode it started with puts the desk back into a mode the person has just left, and
    judging the outcome against it reports fader inputs missing from a mode that has none.

    `arrived` says the desk has just come back as a different USB device, which is the only
    case where the service gets a chance to bind; only then is the attempt recorded in the bind
    history (`virtual_devices.record_bind`)."""
    mode, expanded = recorder.split_usb_multitrack(raw_mode)

    def rearrive(value: int):
        """Re-enumerate the desk on purpose: the output mode off and back, Expanded kept. The
        lock is held across the hold, so a person's own write waits and lands after this one."""
        with _hid_lock:
            rcp_transfer.set_usb_multitrack_mode(recorder.join_usb_multitrack(0, True))
            time.sleep(_VIRTUAL_DEVICES_RETRY_HOLD_S)
            rcp_transfer.set_usb_multitrack_mode(value)

    def run():
        # ONE FOLLOW-UP PER RE-ENUMERATION. A mode write and the announcer's reconnect both see
        # the same event a second apart, and two threads waiting on the same bind would announce
        # the same inputs twice. The one already running covers it, retries included.
        if not _virtual_devices_busy.acquire(blocking=False):
            return
        try:
            value, fresh, pid = raw_mode, arrived, _desk_pid()

            def moved() -> bool:
                """Whether someone has changed the mode since `value`; if so, follow theirs."""
                nonlocal value, fresh, pid
                held = _desk_usb_mode() if value is not None else None
                if held is None or held == value:
                    return False
                now = _desk_pid()
                value, fresh, pid = held, now != pid, now
                return True

            # A move that is not an arrival (pre-fader to post-fader in Expanded) gives the
            # service nothing new to bind to, so the retries it was owed carry on rather than
            # starting again, and the miss is not announced twice.
            attempts = 0
            while True:
                expect, arrival = _virtual_devices_expectation(value), fresh
                # Waiting for a bind only makes sense after an arrival; otherwise read it as is.
                out = virtual_devices.reconcile(
                    expect_connected=expect if (arrival or not expect) else None)
                left = moved()
                # Recorded even when the mode has since moved: a wait that ran out before the
                # desk left the bus is still a missed bind, and one it left during is cut short.
                if expect and arrival and out.status.running:
                    virtual_devices.record_bind(0, out.status.connected, cut_short=left and fresh)
                if left:
                    attempts = 0 if fresh else attempts
                    continue
                while (expect and out.status.running and out.status.enabled
                       and not out.status.connected and attempts < _VIRTUAL_DEVICES_RETRIES):
                    attempts += 1
                    if attempts == 1:
                        _ann_say("RODE's driver missed the unit. Reconnecting it; the audio drops "
                                 "again for a moment.")
                    _virtual_devices_retrying.set()
                    try:
                        rearrive(int(value))
                    except Exception as exc:
                        diagnostics.event("virtual_devices.retry_failed", f"{exc}")
                        break
                    finally:
                        _virtual_devices_retrying.clear()
                    diagnostics.event("virtual_devices.retry", "re-enumerated the desk",
                                      attempt=attempts)
                    out = virtual_devices.reconcile(expect_connected=True)
                    left = moved()
                    virtual_devices.record_bind(attempts, out.status.connected,
                                                cut_short=left and fresh)
                    if left:
                        break
                if left:
                    attempts = 0 if fresh else attempts
                    continue
                break
            if out.sentence():
                _ann_say(out.sentence())
            elif expect and out.status.running and not out.status.connected:
                # STATE, NEVER AN INSTRUCTION. A replug is one more arrival at the same odds, and
                # in a failing run it binds no better than the retries just did, so asking for one
                # reads as "this will fix it" when it mostly will not.
                _ann_say("RODE's driver has not connected to the unit, so the extra inputs are "
                         "not there.")
        finally:
            _virtual_devices_busy.release()

    threading.Thread(target=run, daemon=True, name="virtual-devices").start()
    if not expanded:
        return ""
    st = virtual_devices.read_status()
    if not (st.installed and st.running and st.enabled):
        return f" {st.phrase()} Game, Music, A and B will be silent."
    if not mode:
        # The service can bind here too, but the mix alone carries no fader inputs to expose,
        # so nothing is waited for and the user is told what the four need.
        return (" Game, Music, A and B only reach their own inputs while this computer receives "
                "every channel, so pick pre-fader or post-fader for that.")
    if not arrived and st.connected:
        return ""       # the desk stayed on the bus and the service stays bound: nothing to wait for
    return (" RODE's driver connects in a few seconds, and the virtual devices are announced "
            "when they are ready.")


def _bypass_sentence(where: str, on: bool) -> str:
    """Said as what you GET, not as the state of a flag: "bypass is on" needs translating in your
    head every time, and both of these are read aloud."""
    return (f"{where}: separate tracks will now be "
            f"{'raw, with no processing' if on else 'processed as you hear them'}. "
            f"The mixed track is unchanged.")


def action_set_processing_bypass(on: bool) -> dict:
    """The RECORDER's raw switch: what the unit writes to its card or drive.

    Two actions rather than one with a `which`, because the two destinations now live on different
    pages and each endpoint belongs to the page that calls it. They share the sentence, not the
    route."""
    gate = _output_write_gate()
    if gate is not None:
        return gate
    with _hid_lock:
        rcp_transfer.set_recording_processing_bypass(on)
    return {"ok": True, "message": _bypass_sentence("Recordings on the card or drive", on)}


def action_set_usb_bypass(on: bool) -> dict:
    """The USB stream's raw switch: what this computer receives. Routing > USB audio."""
    gate = _output_write_gate()
    if gate is not None:
        return gate
    with _hid_lock:
        rcp_transfer.set_usb_multitrack_bypass(on)
    return {"ok": True, "message": _bypass_sentence("The tracks sent to this computer", on)}


# The two auto-mutes, by name: the writer, the property to read back, and what to call it.
# Both live in the OUTPUT block and neither is duplicated anywhere else in the model (checked on
# hardware), and the read-back is still taken BY BLOCK rather than by first occurrence: that costs
# nothing and is the habit that stops the next duplicate biting.
# Each entry carries its own two sentences rather than a shared template, because one phrase cannot
# serve a plural subject and a singular one ("the monitor speakers will not mute on its own"), and
# wording a blind operator relies on is the wrong place to save four lines.
_AUTO_MUTE = {
    "monitor": {
        "label": "Monitor auto-mute",
        "prop": "outputMonAutoMute",
        "writer": "set_monitor_auto_mute",
        "on": ("Monitor auto-mute is on. The monitor speakers mute themselves whenever a "
               "microphone fader is brought up, which is what stops feedback."),
        "off": ("Monitor auto-mute is off, so the monitor speakers stay live when a microphone "
                "fader is brought up. With speakers connected, that is how you get feedback."),
    },
    "bt": {
        "label": "Bluetooth auto-mute",
        "prop": "outputBTAutoMute",
        "writer": "set_bt_auto_mute",
        "on": ("Bluetooth auto-mute is on, so what a paired phone hears mutes by itself when a "
               "microphone fader is brought up."),
        "off": ("Bluetooth auto-mute is off, so a paired phone keeps hearing the mix when a "
                "microphone fader is brought up."),
    },
}


def action_set_auto_mute(which: str, on: bool) -> dict:
    """Turn one of the two auto-mutes on or off, and REPORT WHAT THE DESK ACTUALLY DID.

    NEVER REPORT SUCCESS FROM THE WRITE RETURNING. The failure mode is a tick box reading ON while
    the desk reads False - here, an auto-mute the operator believes is on, and feedback through the
    speakers - and nothing in the app notices, because nothing asks. Every other write in this app
    reads back before it claims anything.

    The state is returned as well as reported, so the page can correct its own switch instead of
    showing a setting the desk does not have.
    """
    gate = _output_write_gate()
    if gate is not None:
        return gate
    spec = _AUTO_MUTE.get(which)
    if spec is None:
        return {"ok": False, "message": f"Unknown auto-mute target {which}."}
    try:
        with _hid_lock:
            getattr(rcp_transfer, spec["writer"])(bool(on))
            time.sleep(_AUTO_MUTE_SETTLE_S)
            now = rcp_read.block_value(rcp_read.read_model_stable(fresh=True),
                                       "OUTPUT", spec["prop"])
    except Exception as exc:
        return {"ok": False, "message": f"Could not change {spec['label'].lower()}: {exc}"}
    now = bool(now)
    if now != bool(on):
        return {"ok": False, "on": now,
                "message": (f"The unit did not take that: {spec['label'].lower()} is still "
                            f"{'on' if now else 'off'}.")}
    return {"ok": True, "on": now, "message": spec["on" if now else "off"]}


# The desk needs a moment before the read-back is meaningful; the same settle the other output
# writes use. Long enough that a slow desk is not read as a refusal.
_AUTO_MUTE_SETTLE_S = 0.6


def _active_headphones():
    """The HeadphoneTypeSpec for the currently-connected device (devices.py). See GAP-6."""
    return devices.get_active_device().headphones


def action_set_headphone_type(value: int, output: int = 0) -> dict:
    """Set a headphone output's amp type. `output` is an INDEX into the active device's headphone
    outputs (0 = Headphone 1, 1 = Headphone 2), `value` an amp-type code from that device's
    HeadphoneTypeSpec (0=NTH-100, 1=High sensitivity, 2=Low sensitivity). The server resolves the
    real selector from the spec so the client never passes an unmapped selector (GAP-6)."""
    gate = _output_write_gate()
    if gate is not None:
        return gate
    spec = _active_headphones()
    if not 0 <= output < len(spec.outputs):
        return {"ok": False, "message": f"Headphone output {output} is not available on this device."}
    selector, out_label = spec.outputs[output]
    if value not in spec.value_codes:
        return {"ok": False, "message": "That headphone type is not a valid value for this device."}
    with _hid_lock:
        rcp_transfer.set_headphone_type(value, selector)
    return {"ok": True, "message": f"{out_label} type set to {spec.value_label[value]}."}


def action_set_master_compellor(on: bool) -> dict:
    """Master output Compellor on/off. Affects all Main-Mix outputs (master audio)."""
    gate = _output_write_gate()
    if gate is not None:
        return gate
    with _hid_lock:
        rcp_transfer.set_master_compellor(on)
    return {"ok": True, "message": f"Master Compellor turned {'on' if on else 'off'}."}


def action_set_compellor_param(param: str, value01: float) -> dict:
    """Master Compellor parameter (threshold/attack/release/gain) as a normalised 0..1 float."""
    gate = _output_write_gate()
    if gate is not None:
        return gate
    if param not in rcp_transfer.MASTER_COMPELLOR_PARAM:
        return {"ok": False, "message": f"Unknown Compellor parameter {param!r}."}
    v = max(0.0, min(1.0, float(value01)))
    with _hid_lock:
        rcp_transfer.set_master_compellor_param(param, v)
    return {"ok": True, "message": f"Master Compellor {param} set to {round(v * 100)} percent."}


def action_set_output_delay(on: bool, ms: float | None) -> dict:
    """Output delay on/off + time in milliseconds (0..2000), for A/V sync. Master audio."""
    gate = _output_write_gate()
    if gate is not None:
        return gate
    seconds = None if ms is None else max(0.0, min(2.0, float(ms) / 1000.0))
    with _hid_lock:
        rcp_transfer.set_output_delay(on, seconds)
    if not on:
        return {"ok": True, "message": "Output delay turned off."}
    if seconds is None:
        return {"ok": True, "message": "Output delay turned on."}
    return {"ok": True, "message": f"Output delay on, {int(round(seconds * 1000))} milliseconds."}


# ----------------------------------------------------------------------------
# Output mix matrix (the Routing > Custom cross-points). Decoded, see
# RODECASTER_PROTOCOL.txt section 5. Each output has a mode (Main Mix / Mix Minus / Custom); under
# Custom, each source can be excluded from that output. The per-device source/output
# LABEL maps now live in devices.py (MatrixSpec.sources / .outputs), so the screen adapts
# to the connected desk; the confirmed-index write guard still lives in rcp_transfer.
# ----------------------------------------------------------------------------

_OUTPUT_MODE_NAMES = {0: "Main Mix", 1: "Mix Minus", 2: "Custom"}


# --- Device-aware matrix helpers. The label maps now live per-device (devices.py), so the routing
# screen shows the CONNECTED desk's real sources/outputs (e.g. source code 2 = "Headset" on the Duo,
# "Mic 3" on the Pro II). Writes are gated on the "matrix_write" cap, which BOTH desks now hold -
# the Duo since its matrix scheme was verified on hardware, so its matrix is no longer read-only.
def _active_matrix():
    return devices.get_active_device().matrix


def _matrix_output(idx: int):
    return next((o for o in _active_matrix().outputs if o["index"] == idx), None)


def _matrix_source(idx: int):
    return next((s for s in _active_matrix().sources_for(True) if s["index"] == idx), None)


def _matrix_write_gate():
    """Cross-point writes. Both the Pro II and the Duo have this cap today; a device whose
    cross-point selector is not verified keeps its matrix read-only rather than writing blind."""
    return _cap_gate("matrix_write", lambda name:
                     f"Changing output mixes is not enabled on the {name} yet, so the routing "
                     f"matrix is read-only here.")


def _output_write_gate():
    """Master/global output writes: multitrack, monitor and BT auto-mute, headphone type, master
    Compellor, output delay. All are global named-property writes at fixed selectors
    (0x0D / 0x09 / 0x0E) decoded on the Pro II and not yet verified on the Duo."""
    return _cap_gate("output_write", lambda name:
                     f"Changing the master output settings is not enabled on the {name} yet, so "
                     f"these controls are read-only here.")


def _max_user_pads() -> int:
    """Highest 1-based grid number a pad action may target on the ACTIVE device (Pro II = 56 on
    a 8/bank x 7-user-bank grid; Duo = 42 on a 6/bank grid). Device-aware so a Duo action cannot
    address a slot beyond its 48-slot model. Pad WRITES are also gated by soundpad's pad_write cap."""
    return devices.get_active_device().pad.max_user_pads


def matrix_model() -> dict:
    """Static layout of the output mix matrix for the UI, for the CONNECTED device."""
    m = _active_matrix()
    outputs = m.outputs or devices.PRO2.matrix.outputs
    sources = (m.sources_for(channels.usb_expanded())
               or devices.PRO2.matrix.sources_for(channels.usb_expanded()))
    read_only = _matrix_write_gate() is not None
    # This note IS rendered (app.js buildMatrix overwrites the static hint with it), so the
    # read-only variant actually reaches the Duo instead of the page promising writes it cannot do.
    # ONE LINE. Do not explain Main Mix, Mix Minus and Custom here, and do not claim the source list
    # shows the unit's real current mix: that list is hidden on anything but Custom, because outside
    # Custom it shows stored cross-points the engine ignores. What each mix means is said per output,
    # about the one selected, by `matrix-mix-summary`; the long version lives on Help & About.
    note = "Each output carries its own mix. Pick an output to see and change it."
    if read_only:
        note = ("This shows each output's live mix on this device. Changing the mix is not "
                "enabled here yet, so these controls are read-only.")
    return {
        "outputs": [{"key": o["key"], "index": o["index"], "label": o["label"],
                     "modes": [[0, "Main Mix"]] + ([[1, "Mix Minus"]] if o["mixminus"] else [])
                              + [[2, "Custom"]]}
                    for o in outputs],
        "sources": [{"key": s["key"], "index": s["index"], "label": s["label"]} for s in sources],
        "read_only": read_only,
        "note": note,
    }


def action_set_output_mode(output_index: int, mode: int) -> dict:
    gate = _matrix_write_gate()
    if gate is not None:
        return gate
    out = _matrix_output(output_index)
    if out is None:
        return {"ok": False, "message": f"Output {output_index} is not valid."}
    if mode not in _OUTPUT_MODE_NAMES:
        return {"ok": False, "message": f"Mode {mode} is not valid."}
    with _hid_lock:
        rcp_transfer.set_output_routing(output_index, mode)
    return {"ok": True, "message": f"{out['label']} output set to {_OUTPUT_MODE_NAMES[mode]}."}


def action_set_cross_point(output_index: int, source_index: int, excluded: bool) -> dict:
    """Exclude/include one source in one output's Custom mix. The output should be in
    Custom mode first (use the mode control)."""
    gate = _matrix_write_gate()
    if gate is not None:
        return gate
    try:
        with _hid_lock:
            rcp_transfer.set_cross_point_excluded(output_index, source_index, excluded)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    out = _matrix_output(output_index)
    src = _matrix_source(source_index)
    out_lab = out["label"] if out else "output"
    src_lab = src["label"] if src else "source"
    verb = "excluded from" if excluded else "included in"
    return {"ok": True, "message": f"{src_lab} {verb} the {out_lab} mix."}


def read_matrix_live() -> dict:
    """Live per-output, per-source EXCLUDED state for the matrix UI (read-back)."""
    try:
        with _hid_lock:
            st = rcp_read.read_matrix_state()
    except FileNotFoundError:
        return {"ok": False,
                "message": "The matrix read map is not calibrated on this machine yet."}
    except Exception as exc:
        return {"ok": False, "message": f"Could not read the output mixes: {exc}"}
    if not st["ok"]:
        return {"ok": False, "message": st["reason"]}
    # Map source code -> UI source key using the CONNECTED device's matrix labels, so the Duo's
    # code 2 resolves to "Headset" (its own key) rather than the Pro II's "Mic3".
    idx2key = {s["index"]: s["key"] for s in _active_matrix().sources_for(True)}
    excluded = {str(o): {idx2key.get(s, str(s)): bool(v) for s, v in srcs.items()}
                for o, srcs in st["excluded"].items()}
    modes = {str(o): v for o, v in st.get("modes", {}).items()}
    return {"ok": True, "excluded": excluded, "modes": modes,
            "message": "Read the live output mixes from the device."}


# ----------------------------------------------------------------------------
# MIX SCHEMES: the output-mix matrix saved under a name. The format, the capture, the built-ins and
# the writes all live in routing_schemes.py; these wrappers own the HID lock and the wording.
#
# The two Chat presets are BUILT-IN SCHEMES rather than buttons of their own, so there is one apply
# path, and "Chat back to normal" writes Chat's real normal state (mix minus) rather than a Main Mix
# that only looks like it.
# ----------------------------------------------------------------------------

def _apply_scheme(scheme: dict, label: str) -> dict:
    """Gate, lock, write, and say what happened in one sentence. Shared by every entry point: the
    dropdown, the built-ins and the "Reset all output mixes" button."""
    gate = _matrix_write_gate()
    if gate is not None:
        return gate
    try:
        with _hid_lock:
            # Our own writes come back on the push stream as mixLevelWithAnchor on the SOURCE, which
            # the announcer speaks as a fader move - a full-desk scheme would otherwise narrate a
            # hundred of them over the top of the page's own reply. Marked per write because the
            # suppression window is 1.5s and this can run for ten.
            res = routing_schemes.apply(
                scheme, on_source_write=lambda si: _ann_mark_self_write(("fader", int(si))))
    except Exception as exc:
        return {"ok": False, "message": f"Could not apply the mix preset: {exc}"}
    outs = res["outputs"]
    if not outs:
        return {"ok": False,
                "message": (f"'{label}' names no output this device has, so nothing was changed."
                            + (f" Not on this device: {', '.join(res['skipped'])}."
                               if res["skipped"] else ""))}
    msg = f"Applied '{label}' to {len(outs)} output" + ("s" if len(outs) != 1 else "")
    msg += ": " + ", ".join(outs) + "."
    if res["skipped"]:
        msg += (f" Skipped {len(res['skipped'])} part(s) this device does not have: "
                f"{', '.join(res['skipped'])}.")
    if res["failed"]:
        msg += f" {res['failed']} write(s) failed, so check the mix before you go on air."
    return {"ok": res["ok"], "message": msg, "outputs": outs, "skipped": res["skipped"],
            "failed": res["failed"]}


def read_routing_schemes() -> dict:
    """What the Mix schemes control offers on this desk: the built-ins, then the saved schemes.

    Each entry carries its own DESCRIPTION so the page can say what a scheme would do before it is
    applied, without a second round trip. A saved scheme's description is worked out from the file
    (routing_schemes.describe) rather than stored in it, so it can never describe a desk it no
    longer matches."""
    read_only = _matrix_write_gate() is not None
    # Read once for the whole list: the source count in every description is against the rows the
    # grid is showing right now, and the mode cannot change mid-loop in any way that matters.
    expanded = channels.usb_expanded()
    saved = []
    for name in routing_schemes.list_names():
        loaded = routing_schemes.load(name)
        if not loaded.get("ok"):
            continue
        s = loaded["scheme"]
        saved.append({"name": name,
                      "description": routing_schemes.describe(s, expanded=expanded),
                      "scope": routing_schemes.scope_text(s),
                      "device_name": s.get("device_name"),
                      "saved_at": s.get("saved_at")})
    note = ("The output mixes saved under a name: which sources each output carries, and at what "
            "level. Pick one and choose Apply. Not a profile: it never touches your channels, "
            "processing or pads.")
    if read_only:
        note = ("Changing output mixes is not enabled on this device yet, so mix presets cannot be "
                "saved or applied here.")
    return {"ok": True, "read_only": read_only, "note": note,
            "builtins": [{"id": b["id"], "name": b["name"], "description": b["description"],
                          "scope": routing_schemes.scope_text(b["scheme"]),
                          "destructive": b["destructive"]}
                         for b in routing_schemes.builtins()],
            "saved": saved}


def action_apply_routing_scheme(name: str, builtin: bool = False) -> dict:
    """Apply a built-in (by id) or a saved scheme (by name). Built-ins are addressed by ID and never
    by name, so a saved scheme called "Everything normal" cannot shadow the real one."""
    if builtin:
        b = routing_schemes.builtin(name)
        if b is None:
            return {"ok": False, "message": f"There is no built-in mix preset '{name}'."}
        return _apply_scheme(b["scheme"], b["name"])
    loaded = routing_schemes.load(name)
    if not loaded.get("ok"):
        return loaded
    return _apply_scheme(loaded["scheme"], loaded["safe"])


def action_save_routing_scheme(name: str, output: str = "") -> dict:
    """Save the desk's CURRENT output mixes under a name. One model read under the lock, no writes.

    `output` is an output KEY and makes a ONE-OUTPUT preset, the shape the built-in Chat presets
    have always had: applying it later writes that output and leaves every other mix alone. Empty
    means every output, which is the older behaviour and still the default.

    Overwriting an existing scheme is allowed and deliberate (unlike the as-found baseline, a scheme
    is cheap to re-take), but the page asks first so it is never a surprise."""
    safe = routing_schemes.safe_name(name)
    if not safe:
        return {"ok": False, "message": "A mix preset name must be 1 to 40 letters, digits, "
                                        "spaces, dashes or underscores."}
    try:
        with _hid_lock:
            blob = rcp_read.read_model_stable()
    except Exception as exc:
        return {"ok": False, "message": f"Could not read the output mixes: {exc}"}
    cap = routing_schemes.capture(safe, blob, only=output)
    if not cap.get("ok"):
        return cap
    res = routing_schemes.save(safe, cap["scheme"])
    if not res.get("ok"):
        return res
    return {"ok": True, "name": safe,
            "message": (f"Saved the mix scheme '{safe}'. "
                        f"{routing_schemes.describe(cap['scheme'], expanded=channels.usb_expanded())}")}


def action_delete_routing_scheme(name: str) -> dict:
    """Delete a saved scheme. Pure file work: no device access and no lock."""
    return routing_schemes.delete(name)


# ----------------------------------------------------------------------------
# Settings profiles. The save/restore logic lives in profiles.py; these are thin wrappers that
# own the HID lock (read the model under it for a save, hold it across the writes for a restore)
# and pass the server's validation constants in. See profiles.py for the format + field detail.
# ----------------------------------------------------------------------------

# Measured on this desk, and the ONLY honest basis for the waiting-time the page quotes. A pad
# restore is dominated by moving its audio over transfer mode, and it is per pad, so the total grows
# with the pad count without limit - the page must work it out, not guess a phrase.
PAD_RESTORE_SECONDS = 15
SETTINGS_RESTORE_SECONDS = 25


def list_profiles() -> dict:
    return profiles.list_profiles()


def _profile_file_job_busy():
    """Message if a save or restore is in flight, else None. Renaming or deleting a profile is
    pure file work, but either job is mid-way through reading or writing that same folder, so the
    file operations stand aside rather than pulling it out from under them."""
    if _profile_job.busy():
        return "A profile save is running. Try again when it finishes."
    if _restore_job.busy():
        return "A profile restore is running. Try again when it finishes."
    return None


def profile_info(name: str) -> dict:
    """What the page needs to describe a profile BEFORE restoring it, above all its pad count: pads
    dominate the time (about 15 seconds each, measured), so a 40-pad profile is a ten-minute job
    rather than the minute or so a settings-only restore takes. Reads the one profile, not the whole
    store.

    IT ALSO ANSWERS WHETHER A CLEAN RESTORE IS POSSIBLE, because the restore dialog is where that
    choice is offered and this is the call it already waits for. A clean restore backs the desk up
    first and refuses to go on without it, so with nowhere to write it cannot be done at all. Gating
    the option on the shows CAPABILITY alone is not enough: a Duo has that cap and no removable
    storage, so the option would be offered and then refused. Asked HERE rather than on page entry
    so the answer is fresh at the moment the
    dialog is built, and it is cheap because `_removable_volume_ready` reads through the cache."""
    loaded = profiles.load(name)
    if not loaded.get("ok"):
        return loaded
    prof = loaded["prof"]
    can_clean = _removable_volume_ready()
    return {"ok": True, "name": loaded["safe"], "pads": len(prof.get("pads") or []),
            "saved_at": prof.get("saved_at"), "device_name": prof.get("device_name"),
            "seconds_per_pad": PAD_RESTORE_SECONDS, "settings_seconds": SETTINGS_RESTORE_SECONDS,
            "can_clean": can_clean,
            "clean_blocked_reason": ("" if can_clean else
                                     "Resetting first needs the desk backed up, and there is no "
                                     "memory card or USB drive in it to save a backup to.")}


def delete_profile(name: str, force: bool = False) -> dict:
    """Delete a saved profile from this computer. No HID lock and no device access.

    `force` is the second step for the as-found baseline only: without it, deleting the baseline
    comes back with baseline: True and a message for the page to confirm against."""
    busy = _profile_file_job_busy()
    return {"ok": False, "message": busy} if busy else profiles.delete(name, force=force)


def rename_profile(name: str, new_name: str) -> dict:
    """Rename a saved profile on this computer. No HID lock and no device access."""
    busy = _profile_file_job_busy()
    return {"ok": False, "message": busy} if busy else profiles.rename(name, new_name)


class _BackgroundJob:
    """One long job running off the request thread: its progress, and its result.

    WHY IT EXISTS. Four jobs need this shape - the profile save, the profile restore, the desk's own
    show commands and the pad restore - and hand-rolling each one means four locks, four identical
    state dicts, four `_worker` closures and four finish blocks. Two of the four are started from
    two different actions, so that is six copies of the finish block, and a bug fixed in one of them
    is fixed in exactly one.

    WHY BACKGROUND AT ALL. These take from a few seconds to well over a minute (a pad backup copies
    every sound file off the unit over transfer mode). A page that can poll for progress announces
    each step into a live region; one long silent POST just hangs, which for a screen-reader user is
    indistinguishable from a crash.

    `start` claims the job, refusing plainly if one is already running, and runs `work(job)` on a
    daemon thread. `work` reports with `job.step` (a message) or `job.tick` (a message AND a count),
    and RETURNS its result dict. This class writes the finish, including turning an exception into
    an honest failure message rather than a dialog that never closes.
    """

    def __init__(self, busy: str, failed: str):
        self._lock = threading.Lock()
        self._busy = busy        # "A profile save is already running."
        self._failed = failed    # "Could not save the profile"
        self._state = {"running": False, "step": 0, "total": 0, "label": "",
                       "done": False, "ok": None, "message": ""}
        self._n = 0
        self._phrase = None

    def status(self) -> dict:
        """The whole state, copied, for the status endpoint the page polls."""
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

    def start(self, work, message: str, total: int = 0, phrase=None, started: str = None,
              failed: str = None) -> dict:
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
            at = time.time()
            diagnostics.event("job.start", label)
            try:
                res = work(self) or {}
            except Exception as exc:
                res = {"ok": False, "message": f"{crashed}: {exc}"}
            diagnostics.event("job.end", label, ok=bool(res.get("ok")),
                              took=f"{time.time() - at:.2f}s",
                              said=res.get("message") or "Done.")
            with self._lock:
                self._state.update({"running": False, "done": True,
                                    "ok": bool(res.get("ok")),
                                    "message": res.get("message") or "Done."})
            # A CUE AT THE END OF EVERY JOB, rising for finished and falling for failed. Every step
            # a job reports lands in one live region, so the closing sentence arrives sounding
            # exactly like the twenty before it and "Backup complete" is heard as one more pad
            # name. A tone is not a sentence and cannot be mistaken for one, and it lands BEFORE
            # the words, so the outcome is known first.
            # ONE PLACE, because this class already owns the finish block for every long job: the
            # backup, the restore, the reset and the pad jobs all get it without knowing it exists.
            # NOT GATED ON THE ANNOUNCER. The announcer speaks for the DESK's own controls and can
            # reasonably be off; this answers a button the user just pressed in this app and is
            # waiting on, and silence there is the fault being fixed.
            try:
                _ann_tone("done" if res.get("ok") else "failed")
            except Exception:
                pass                    # a missing cue must never fail the job that just succeeded

        # NAMED, so the diagnostic log's thread column says which job a line came from rather than
        # "Thread-7". Every other line in a job's window is written from this thread.
        threading.Thread(target=_worker, daemon=True, name=f"job:{label[:14]}").start()
        reply = {"ok": True, "started": True, "message": started or message}
        if total:
            reply["total"] = total
        return reply


# A settings-only profile save is quick, but include_pads copies every pad's sound file off the unit
# over transfer mode and takes a minute or more, so it runs as a job and the page polls
# /api/profiles/save/status.
_profile_job = _BackgroundJob("A profile save is already running.", "Could not save the profile")


# RODE'S DRIVER, FETCHED ON REQUEST. Its own job state rather than a shared one: it is the only
# job in the app that touches the network instead of the desk, so it can run while a desk job is
# going and must not be refused by one.
_driver_job = _BackgroundJob("The driver install is already running.",
                             "The driver could not be installed")


def driver_install_status() -> dict:
    return _driver_job.status()


def action_install_driver() -> dict:
    """Fetch RODE's Virtual Devices driver, check its signature and hand it to Windows.

    **THE APP'S ONLY OUTBOUND INTERNET CALL, and only ever from this press.** Never at startup,
    never on a timer, never a check for a newer version. See driver_install.py for why that is kept
    deliberate, and for why the signature rather than a hash is what gets checked.

    REFUSED WHEN IT IS ALREADY INSTALLED, because the honest answer there is not "installing" but
    "you already have it": a second install of the same version tells the user nothing and costs
    them a UAC prompt and a reboot prompt. Repairing a broken install is RODE's installer's job,
    reachable through the page button beside this one.
    """
    if virtual_devices.read_status().installed:
        return {"ok": False,
                "message": ("RODE's Virtual Devices driver is already installed on this computer. "
                            "If it is not working, use \"Open RODE's software page\" to reinstall "
                            "or repair it.")}

    def _work(job):
        try:
            got = driver_install.install(on_step=job.step)
        except driver_install.DriverInstallError as exc:
            diagnostics.event("driver.install_failed", f"{exc}")
            return {"ok": False, "message": str(exc)}
        diagnostics.event("driver.handed_over", got["signer"])
        return {"ok": True,
                "message": ("RODE's installer is now running. Follow its prompts, allow the "
                            "Windows permission request, and restart the computer if it asks. "
                            "This page will show the driver once it is in.")}

    return _driver_job.start(_work,
                             message="Downloading RODE's driver, please wait.",
                             started="Install RODE's driver",
                             failed="The driver install failed")


def action_open_driver_page() -> dict:
    """Open RODE's own software page in the browser: the manual route, offered as its own button
    rather than only after a failure, so nobody has to make something go wrong to find it."""
    try:
        open_ui(driver_install.PAGE_URL)
    except Exception as exc:
        return {"ok": False,
                "message": (f"The page could not be opened ({exc}). Go to {driver_install.PAGE_URL} "
                            f"in your browser and look for the RODECaster Virtual Device Driver.")}
    return {"ok": True,
            "message": ("RODE's software page is opening in your browser. Look for the RODECaster "
                        "Virtual Device Driver, download it and run the installer inside.")}




def profile_save_status() -> dict:
    return _profile_job.status()


def _do_snapshot(safe: str, include_pads: bool, prog, baseline: bool = False, show=None) -> dict:
    """Read the whole desk under the HID lock, then hand it to profiles.save. With include_pads,
    also back up the SMART pad grid (metadata + copy each sound file off the unit) in the same
    locked session. The device reads stay here because the lock lives here."""
    try:
        with _hid_lock:
            # PHASES, NOT ITEMS. Everything spoken during a job lands in one live region, so each
            # line cuts off the one before it; with a line per pad you hear the first two words of
            # each and nothing else, and the closing line sounds like one more pad. Three lines for
            # the whole job, and the last word of the last one cannot be mistaken for a pad name.
            prog("Backing up the settings.")
            blob = rcp_read.read_model_stable()
            chans = channels.read_channels(blob)
            if include_pads:
                pad_backup = soundpad.backup_pads(profiles.pads_dir(safe), progress=None)
                n = len(pad_backup.get("pads") or [])
                prog(f"Settings done. Backed up {n} SMART pad{'' if n == 1 else 's'}.")
            else:
                pad_backup = None
    except Exception as exc:
        return {"ok": False, "message": f"Could not read settings from the device: {exc}"}
    with _session_state_lock:
        firmware = _session_state.get("firmware")
    dev = devices.get_active_device()
    # capture_length, not count: a profile must capture the WHOLE channelInputSource vector,
    # including any slot the desk does not expose as a fader (both known desks have a 10th, measured),
    # so the saved layout is the real one rather than a truncated view.
    # Capture is deliberately WIDER than write: profiles._apply_v2 only writes the first
    # vector_length entries, so the extra slot round-trips into the JSON and is never written back.
    res = profiles.save(safe, blob, chans, firmware, _active_faders().capture_length,
                        pads=(pad_backup["pads"] if pad_backup else None),
                        product_id=dev.product_id, device_name=dev.name,
                        monitor_output_indices=[i for i, _l in _monitor_outputs()],
                        headphone_output_count=len(_active_headphones().outputs),
                        baseline=baseline, show=show)
    if pad_backup and pad_backup.get("unrecognised"):
        grids = ", ".join(str(g) for g in pad_backup["unrecognised"])
        res["message"] += (f" Note: the voice-FX pad(s) at grid {grids} carry a name that matches no "
                           f"factory or saved effect (likely renamed), so the effect could not be "
                           f"identified and they will be skipped on restore.")
    return res


def _removable_volume_ready() -> bool:
    """Is there a card or USB drive in the unit with room for a show?

    A show can ONLY go to removable storage: measured, every export takes about 24 MB off the USB
    drive and leaves the internal volume untouched, and that internal volume is the SMART pad
    partition, which reports it cannot even record. So with nothing inserted there is nowhere for a
    backup to go, and asking for one times out rather than failing cleanly.

    ANSWERED BY `recorder.destination`, LIKE EVERY OTHER CALLER. A hand-rolled inserted/mounted/
    can_record loop here would be a SECOND OPINION on the one question `read_show_state` says must
    have only one, so the Shows page, the Recording page and this could disagree about whether a
    backup is possible. Deliberately NOT fresh: the short-lived cache is what keeps this cheap
    enough to ask on a page load."""
    try:
        with _hid_lock:
            blob = rcp_read.read_model()
        return bool(recorder.destination(recorder.read_volumes(blob)))
    except Exception:
        return False


def action_backup_now(name: str) -> dict:
    """BACK THE UNIT UP: one name, one button, and both mechanisms underneath.

    THE OBVIOUS ALTERNATIVE LOOKS TIDIER AND IS WORSE: offering shows OR profiles depending on
    whether a drive is in. It fails two ways: pull the drive to offload recordings and the user's
    whole backup history vanishes from the page (and dies for good if they format it), and a
    shows-only list cannot be named, because the unit names every export after the loaded show and
    offers no rename, so it reads as a column of identical lines.

    So: export a show when there is somewhere to put one, ALWAYS save a profile, and pair them. The
    user types one name and never learns there are two mechanisms. With no removable storage the
    profile goes on alone and nothing announces a missing feature.

    The show is exported FIRST, while the desk still holds exactly what is being named."""
    safe = profiles.safe_name(name)
    if not safe:
        return {"ok": False, "message": "Backup name must be 1 to 40 letters, digits, "
                                        "spaces, dashes or underscores."}

    def work(job):
        show = None
        note = ""
        if "shows" in devices.get_active_device().caps and _removable_volume_ready():
            got = _export_and_wait(job.step, "Backing the unit up to its own drive...")
            if got.get("ok"):
                s = got["show"]
                show = {"snapshot": s.get("snapshot"), "uuid": s.get("uuid"), "name": s.get("name")}
            else:
                # The profile is still worth having, so carry on rather than losing the lot. Say so
                # plainly: a backup that quietly did half of what was asked is worse than a slow one.
                note = (" The unit's own backup did not appear on the drive, so this is a settings "
                        "and pads backup only. Check there is a drive in with room on it.")
        res = _do_snapshot(safe, True, job.tick, show=show)
        if not res.get("ok"):
            return res
        res["message"] = _backup_wording(safe, show) + note
        return res
    return _profile_job.start(lambda job: work(job),
                              message="Backing up, please wait.", started="Backing up.")


def _backup_wording(safe: str, show) -> str:
    """What a finished backup says. Names what it can restore FROM, not which mechanisms ran."""
    if show:
        return (f"Backed up as '{safe}'. It holds the unit's own complete backup, which restores "
                f"everything in seconds, plus a copy of your settings and pads on this computer in "
                f"case the drive is removed or erased.")
    return (f"Backed up as '{safe}', on this computer. There is no card or drive in the unit, so "
            f"the unit's own backup could not be made; this holds your settings and pads.")


def snapshot_profile(name: str, include_pads: bool = False, baseline: bool = False) -> dict:
    """Validate the name, then start the save on a worker thread. The name check stays synchronous
    so a bad name comes straight back as an error instead of through the progress dialog."""
    safe = profiles.safe_name(name)
    if not safe:
        return {"ok": False, "message": "Profile name must be 1 to 40 letters, digits, "
                                        "spaces, dashes or underscores."}
    return _profile_job.start(
        lambda job: _do_snapshot(safe, include_pads, job.tick, baseline=baseline),
        message="Reading the unit's settings, please wait.",
        started="Saving the profile.")


# --- THE AS-FOUND BASELINE, offered on first run -----------------------------------------------
# The most valuable thing this app can do on the day it is installed is record how the desk already
# was: the moment anything is written, "how it came" is gone, and the touchscreen wizard that would
# otherwise rebuild it is not reachable by a blind operator.
#
# IT IS NOT THE ONLY UNDO ANY MORE, and it is still the better one. `action_show_reset` puts the
# desk back to the factory, but it takes everything with it and it is the command implicated in the
# USB dropout; restoring this baseline puts back what the user actually had, needs no reset, and
# works on a desk with no card or drive in it.
#
# It is offered ONCE PER DESK, not once per install: plug in a second unit later and it gets its own
# offer. The answer (captured, or declined) is remembered against the hardware id so the dialog does
# not reappear at every launch, and declining does not close the door - the Profiles page can still
# capture one, and pads can be added to an existing baseline afterwards.
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
# at 12-21 MB/s), so the whole grid moves in about a second. The copy is real rather than a cache
# trick or an empty file: every WAV was md5'd on the volume and again in the backup folder, and all
# eight matched byte for byte.
#
# SO THE HONEST SHAPE IS THROUGHPUT, NOT A PER-PAD CONSTANT. This grid's biggest pad is 12 MB; a
# ten-minute bed at 100 MB would take five seconds on its own. One second per pad is right for pads
# this size and is rounded up; if somebody's grid is all long beds it will under-quote, which is the
# safe direction now that the number is small.
PAD_BACKUP_SECONDS = 1


def _baseline_asked() -> list:
    got = load_settings().get(_BASELINE_ASKED_KEY)
    return [str(x) for x in got] if isinstance(got, list) else []


def _note_baseline_asked() -> None:
    """Remember that this desk has had its offer, so the dialog is a first-run event and not a nag."""
    here = _device_hardware_id()
    if here and here not in _baseline_asked():
        update_settings(**{_BASELINE_ASKED_KEY: [*_baseline_asked(), here]})


def _baseline_name() -> str:
    """The auto-name for a baseline: what it is, when it was taken, and which desk.

    Deliberately not "app install": a year from now that says nothing about which unit or when.
    Kept inside profiles.safe_name's character set (letters, digits, spaces, dashes) and its 40
    character limit, which is why the date is numeric and there are no brackets."""
    name = f"As found {time.strftime('%Y-%m-%d')} {devices.get_active_device().name}"
    return profiles.safe_name(name[:40]) or f"As found {time.strftime('%Y-%m-%d')}"


def baseline_status() -> dict:
    """Should the page offer a first-run baseline capture, and what is there already?

    Cheap on purpose: it reads the profile store (small, local) and the settings file, and does NO
    device I/O at all. The page asks this on every load, right after the status read that publishes
    the hardware id, so "do we know this desk's id" is exactly as good as "is a desk connected" here
    and costs nothing."""
    existing = profiles.find_baseline()
    here = _device_hardware_id()
    connected = bool(here)
    asked = here in _baseline_asked() if here else False
    return {
        "ok": True,
        # Only offer when there is a desk to read, nothing captured yet, and we have not already
        # asked. No desk means no offer rather than a dialog that could not do anything anyway.
        "offer": bool(connected and not existing and not asked),
        "connected": connected,
        "baseline": existing or None,
        "suggested_name": _baseline_name(),
        "device_name": devices.get_active_device().name,
        "seconds_per_pad": PAD_BACKUP_SECONDS,
    }


def action_baseline_skip() -> dict:
    """Decline the first-run capture. Remembered per desk so it is asked once, not every launch."""
    _note_baseline_asked()
    return {"ok": True, "message": "No baseline was captured. You can still capture one from the "
                                   "Profiles page at any time, though it will then record the unit "
                                   "as it is now rather than as you found it."}


def action_baseline_save(include_pads: bool = True) -> dict:
    """Capture the as-found baseline. Runs on the same worker and reports through the same progress
    dialog as an ordinary profile save; only the name, the marking and the protections differ."""
    existing = profiles.find_baseline()
    if existing:
        return {"ok": False, "message": f"This unit already has a baseline, '{existing['name']}', "
                                        f"and there can only be one. Nothing was changed."}
    _note_baseline_asked()          # asked and answered, whichever way the capture then goes
    return snapshot_profile(_baseline_name(), include_pads=include_pads, baseline=True)


def action_baseline_add_pads() -> dict:
    """Add the SMART pads to a baseline that was captured without them.

    The settings snapshot is left EXACTLY as it was captured (profiles.attach_pads, not a re-save):
    re-reading the desk would quietly turn an as-found record into a record of today, which is the
    one thing a baseline must never become."""
    existing = profiles.find_baseline()
    if not existing:
        return {"ok": False, "message": "There is no baseline on this computer to add pads to."}
    if existing.get("pads"):
        return {"ok": False, "message": f"'{existing['name']}' already has "
                                        f"{existing['pads']} SMART pad(s) saved."}
    safe = existing["name"]

    def _work(job):
        with _hid_lock:
            backup = soundpad.backup_pads(profiles.pads_dir(safe), progress=job.tick)
        return profiles.attach_pads(safe, backup["pads"])

    return _profile_job.start(_work,
                              message="Backing up SMART pads, please wait.",
                              started="Backing up the SMART pads.",
                              failed="Could not back up the pads")


# Profile restore runs in a background thread (settings ~13s + an optional pad rebuild ~a minute),
# so the page is not blocked and can poll progress for live-region announcements.
_restore_job = _BackgroundJob("A profile restore is already running.",
                              "Could not restore the profile")


def profile_restore_status() -> dict:
    return _restore_job.status()


def restore_profile(name: str, clean: bool = False) -> dict:
    """Start a background restore: apply the settings, then (if the profile carries pads) rebuild
    the SMART pad grid. The page polls /api/profiles/restore/status for progress. profiles.apply
    and soundpad.restore_pads do the device writes and assume the caller holds the HID lock.

    `clean` PICKS BETWEEN TWO GENUINELY DIFFERENT OPERATIONS, and the page names both rather than
    choosing for the user:

      * MERGE (the default, and what this has always done): write the profile's values over
        whatever the desk holds now. Everything the profile does not cover is LEFT ALONE. That is
        the right choice for "put my show back", and it is the only one that preserves settings this
        app cannot write - a paired Bluetooth phone, a pad's custom censor sound, the strip colours.

      * CLEAN: software-factory-reset the desk first (shows.send_new_from_default), then restore on
        top. The desk then matches the profile EXACTLY, because the ~300 properties a profile cannot
        write are at factory defaults rather than at some unknown leftover value. It also clears
        strays and orphan pads, which no profile restore can do. The cost is real and is stated on
        the page: anything the profile does not carry is DESTROYED rather than left, and the pad
        phase gets slower because the reset puts 16 factory pads back for it to replace.

    An export is taken before the reset as the undo, for the reason action_show_reset gives.
    """
    loaded = profiles.load(name)
    if not loaded.get("ok"):
        return loaded
    safe, prof = loaded["safe"], loaded["prof"]
    # GAP-4: refuse a cross-device restore. A profile stamps the product id of the desk it was saved
    # on; its fader vector, matrix shape and channel set are device-specific, so applying a Pro II
    # profile to a Duo (or vice versa) would mis-write. Older profiles carry no product id (all saved
    # on the Pro II historically) - those are allowed, with the per-write guards still protecting.
    saved_pid = prof.get("product_id")
    active = devices.get_active_device()
    if saved_pid is not None and saved_pid != active.product_id:
        saved_name = prof.get("device_name") or devices.resolve_device(saved_pid).name
        return {"ok": False,
                "message": (f"This profile was saved from a {saved_name}, but a {active.name} is "
                            f"connected. Profiles are device-specific and cannot be restored across "
                            f"different RODECaster models.")}
    pad_list = prof.get("pads") or []
    # Refuse a clean restore we cannot actually do CLEANLY, rather than silently doing a merge and
    # calling it clean. Someone who asked for an exact match needs to know they did not get one.
    if clean:
        gate = _shows_write_gate()
        if gate is not None:
            return {"ok": False,
                    "message": ("A clean restore resets the desk first, and that is not available "
                                "on this unit. " + gate["message"] + " Restore without the reset "
                                "instead: it puts your settings back but leaves anything the "
                                "profile does not cover as it is.")}
    def _work(job):
        # TWO PROGRESS CHANNELS, deliberately. The settings phase reports its own sections (it is
        # ~20s of paced writes) so it uses job.step, which does not touch the count; the pad phase
        # is a countable list, so it uses job.tick. The pad phase formats its OWN counts too
        # (soundpad.restore_pads knows how many pads are actually being written, which is not the
        # same as how many the profile holds once the verify pass has spared the ones already
        # correct), so no `phrase` is passed and the message goes through as it comes.
        reset_note = ""
        if clean:
            # THE RESET, AND ITS UNDO, BEFORE ANY PROFILE WRITE. Order is load-bearing: the export
            # has to be taken while the desk still holds the state being replaced, and the reset has
            # to land before profiles.apply, or the reset would wipe the values we just wrote.
            undo = _clean_restore_reset(job.step)
            if not undo.get("ok"):
                return undo
            reset_note = undo["message"]
        # AN OLD PROFILE REARRANGES THE FADERS AND THEN SAYS NOTHING ABOUT THE LEVELS, which is how
        # it can leave a channel silent. A fader's level belongs to the STRIP, so a channel moved to
        # a different fader inherits whatever that strip was sitting at - and profiles saved before
        # `fader_levels` existed carry the layout without the levels, so a restore can put a channel
        # on a fader at zero with every reading clean, because nothing IS wrong, and leave the
        # silence with no explanation.
        # So when the profile has no levels of its own, the levels FOLLOW THE CHANNELS instead, the
        # same way `action_set_fader` already carries them on a manual reassignment. When the
        # profile DOES carry levels they are authoritative and this must not run: `profiles.apply`
        # writes them after the layout, and overwriting them with the pre-restore values would throw
        # away the very thing the backup recorded.
        spec = _active_faders()
        carry_levels = not (prof.get("routing") or {}).get("fader_levels")
        vec_before = levels_before = None
        with _hid_lock:
            if carry_levels and _level_write_gate() is None:
                try:
                    blob0 = rcp_read.read_model()
                    vec_before = _read_fader_vector(blob0, spec)
                    levels_before = rcp_read.values(blob0, "faderLevel")
                except Exception:
                    vec_before = levels_before = None
            res = profiles.apply(prof, fader_count=_active_faders().vector_length,
                                 fader_code_set=_active_faders().code_set,
                                 input_strip_vec_index=_active_input_strips().strip_vec_index,
                                 valid_source_codes=_active_input_strips().valid_source_codes,
                                 mic_channels=MIC_CHANNELS, progress=job.step)
            if levels_before is not None:
                _follow_levels(vec_before, _read_fader_vector(spec=spec), levels_before, spec)
        # A PROFILE CARRIES `systemMidiControl`, so one taken while MIDI was off has just switched
        # the MIDI port back off and every sound-pad shortcut with it. Forget that we armed it and
        # the next pad press arms it again - which is why arming is per-press-until-done rather than
        # once at startup.
        _hk_rearm_midi()
        # A restore can also rewrite the whole pad grid, so the shortcut layer's map is wrong.
        _hk_pads_stale()
        # Output mix matrix + monitor-mix levels (v2 profiles only).
        job.step("Settings done. Restoring output routing...")
        with _hid_lock:
            mx_res = _apply_matrix(prof.get("routing") or {})
        pad_res = None
        pad_tidy = None
        if pad_list:
            job.step("Restoring SMART pads...")
            with _hid_lock:
                # ONE TRANSFER SESSION FOR THE WHOLE PAD RESTORE. Without it each create enters
                # transfer mode, remounts the volume and leaves again for itself: sixteen cycles for
                # sixteen pads, every one dropping the desk's audio, popping a Windows device sound,
                # and costing about a second per pad. The session exits in its own __exit__, so a
                # failure on any pad still closes it.
                with rcp_transfer.transfer_session():
                    pad_res = soundpad.restore_pads(pad_list, profiles.pads_dir(safe),
                                                    progress=job.tick)
                # Pads replaced by the restore left their old audio behind; clear it out.
                pad_tidy = soundpad.tidy_unused_pad_audio(progress=job.tick)

        # AFTER THE PAD PHASE, AND ONLY ON A CLEAN RESTORE. The reset puts sixteen factory pads back,
        # six of them voice-FX pads with no effect definition behind them (soundpad.heal_fx_pool).
        # A profile that CARRIES pads has just rebuilt its own, so this finds nothing missing and
        # costs one dump; a profile saved without pads leaves the factory six needing the rebuild.
        # Proven by `post-cleanrestore-raw.bin`, which holds only region-B object 65.
        healed = None
        if clean:
            job.step("Checking the voice effects survived the reset...")
            with _hid_lock:
                healed = soundpad.heal_fx_pool(progress=job.step)

        msg = profiles.restore_result(safe, res)["message"]
        if reset_note:
            msg = reset_note + " " + msg
        if mx_res and not mx_res.get("skipped"):
            msg += f" Re-applied output routing ({mx_res['applied']} matrix writes)."
        if pad_res is not None:
            unchanged = pad_res.get("unchanged") or []
            rebuilt = sum(1 for r in pad_res["results"] if r.get("ok") and not r.get("unchanged"))
            total_pads = len(pad_res["results"])
            # Say what was left alone as well as what was written. A restore that finishes in
            # seconds because everything already matched must not read like it did nothing.
            if unchanged:
                msg += (f" SMART pads: {len(unchanged)} of {total_pads} were already exactly as "
                        f"saved and were left alone, {rebuilt} rebuilt.")
            else:
                msg += f" Restored {rebuilt} of {total_pads} SMART pads."
            if pad_res.get("skipped"):
                msg += " Could not restore: " + "; ".join(pad_res["skipped"]) + "."
        if pad_tidy and pad_tidy.get("removed"):
            msg += " " + pad_tidy["message"]
        if healed and healed.get("message"):
            msg += " " + healed["message"]
        return {"ok": True, "message": msg}

    return _restore_job.start(_work, total=len(pad_list),
                              message=("Backing the desk up before resetting it..." if clean
                                       else "Applying settings, please wait."),
                              started="Restoring the profile.")


# ----------------------------------------------------------------------------
# THE DESK'S OWN SHOWS: export, import, software factory reset, delete.
#
# The mechanism, the traps and why this sits alongside profiles rather than replacing them are all
# in shows.py. What lives HERE is the part that must not be in a module: the gate, the wording, and
# the rule that no command is ever reported as having worked until the desk has been read back.
#
# ALL FOUR RUN AS ONE BACKGROUND JOB, sharing one state block, because they are mutually exclusive
# by nature (you cannot import while a reset runs) and because every one of them needs a progress
# dialog: an import and a reset take seconds, and a blind operator staring at a page that has not
# changed cannot tell "working" from "died".
_show_job = _BackgroundJob("A show command is already running.", "The show command failed")


def _shows_write_gate():
    """SHOWCONTROL, where the consequence of a wrong selector is the worst of any gate: the address
    carries a FACTORY RESET, so a guess could land a reset, or a stray, on some other block.

    BOTH KNOWN DESKS HOLD THIS CAP, so nothing refuses here today. The gate stays because the
    hazard is a property of the ADDRESS and not of any one desk: the
    old reason - "0x28E is a position in the Pro II's block walk and the Duo's has never been done
    that far" - was retired by reading the Duo's own tree, and the next unknown desk gets the same
    treatment. `DevTools/test_shows_sim.py` proves it with a synthetic capless device."""
    return _cap_gate("shows", lambda name:
                     f"AccessCaster can list the shows on the {name} but not export, restore or "
                     f"reset them: doing that has only ever been confirmed on a RODECaster Pro 2, "
                     f"and the same control carries the factory reset, so it refuses rather than "
                     f"risk it.")


def read_show_state() -> dict:
    """The Shows section of the Profiles page: what is loaded, and what is on the drive."""
    try:
        with _hid_lock:
            blob = rcp_read.read_model()
    except Exception as exc:
        return {"ok": False, "message": f"Could not read the shows from the device: {exc}"}
    state = shows.describe(blob)
    state["ok"] = True
    state["writable"] = "shows" in devices.get_active_device().caps
    current_uuid = (state.get("current") or {}).get("uuid") or ""
    # `is_current` says this backup holds the SHOW that is loaded. It is NOT a bar on restoring:
    # the unit happily imports one of these and puts the desk back, which is the ORDINARY case,
    # since a uuid only changes on a factory reset. It stays
    # because it is still worth knowing which backups belong to the loaded show, and because
    # several can be true at once - a uuid names the show, not the file.
    for show in state["shows"]:
        show["is_current"] = bool(current_uuid and show.get("uuid") == current_uuid)
    # Backups of one desk state are identical in name and uuid, so without this they read as several
    # copies of the same line with no way to tell which is which. The snapshot index is what makes
    # them distinguishable, and it is ALSO the handle, so the label and the handle cannot drift.
    total = len(state["shows"])
    for n, show in enumerate(state["shows"], start=1):
        # TWO labels, because one cannot do both jobs. `label` is the dropdown option, which is read
        # on its own and must carry every qualification. `short_label` is for embedding in a
        # sentence, where the qualifications turn into nonsense: quoting the full label inside a
        # restore confirmation produced "restore the backup ... so it cannot be restored".
        # THE DATE IS THE USEFUL DISCRIMINATOR: "backup 1 of 2" only says where a thing sits in a
        # list, while a date and time says which one you actually want. It comes from
        # `showLastModification`, a 64-bit epoch that needs the tree parser to read. The position is
        # kept as a fallback for a backup whose date did not read, so two entries can never come out
        # reading the same.
        show["short_label"] = (f"{show['name']}, {show['modified']}" if show.get("modified")
                               else f"{show['name']}, backup {n} of {total}")
        show["label"] = (show["short_label"]
                         + (", the newest" if n == 1 and total > 1 else ""))
    state["message"] = ("Nothing has been backed up to the drive yet." if not state["shows"]
                        else f"{total} backup{'' if total == 1 else 's'} on the recordings drive.")

    # CAN A BACKUP BE MADE AT ALL? Answered HERE because this function already has the model blob,
    # and answered with recorder.destination so the Shows page and the Recording page agree about
    # what counts as somewhere to write - one helper, not two opinions.
    #
    # WHY THE PAGE NEEDS IT UP FRONT. The factory reset offers "back the unit up first", ticked by
    # default. With no card or drive in, that cannot work, so without this the reset runs, the
    # backup fails, and the user lands in a failure dialog they have to tab out of. Offering a
    # choice that cannot succeed is the bug; the failure dialog is only the symptom.
    dest = recorder.destination(recorder.read_volumes(blob))
    state["can_backup"] = bool(dest)
    state["backup_to"] = dest["spoken"] if dest else ""
    state["backup_blocked_reason"] = ("" if dest else
                                      "There is no memory card or USB drive in the unit, so there "
                                      "is nowhere to save a backup.")
    return state


def show_job_status() -> dict:
    """Progress for whichever show command is running. Polled by the page."""
    return _show_job.status()


def _run_show_job(label: str, work) -> dict:
    """Start `work` on a worker thread under the one job state, which all four show commands share
    because they are mutually exclusive. `work` reports through the `step` it is handed."""
    gate = _shows_write_gate()
    if gate is not None:
        return gate
    return _show_job.start(lambda job: work(job.step),
                           message=f"{label}, please wait.",
                           started=f"{label}.",
                           failed=f"{label} failed")


def _locked_dump():
    """One fresh model dump, taking the HID lock only for the dump itself.

    Passed to shows.wait_for so a forty-second wait does not hold the lock through its sleeps. See
    the note on shows.wait_for: holding it across the wait blocked the announcer and read as a
    frozen app."""
    with _hid_lock:
        return rcp_read.read_model_stable(fresh=True)


def _read_shows_now() -> tuple:
    """A FRESH shows list plus the blob it came from. Every command resolves its uid through this
    immediately before writing, because `showUID` is positional and an export renumbers the lot
    (shows.py trap 1)."""
    blob = rcp_read.read_model_stable(fresh=True)
    return shows.read_shows(blob), blob


def _export_and_wait(step, label: str) -> dict:
    """Export the desk and WAIT FOR THE FILE TO APPEAR. Shared by every path that needs a backup.

    Identifies the new show by a NEW SNAPSHOT INDEX appearing. It cannot be done by uuid: three
    exports of one desk state all carry the same uuid, so "a uuid we have not seen" never fires on
    the second export and every repeat backup is silently reported as a failure. It is not done by
    "uid 0" either, which is true but would also be satisfied by an export somebody else triggered
    from the touchscreen mid-wait.

    IT REFUSES BEFORE SENDING ANYTHING IF THERE IS NOWHERE TO WRITE, and this is the only place that
    has to, because it is the only place an export is sent from. `read_show_state` computes the same
    fact for the PAGE, so the reset dialog can grey out its tick, but a check on that side alone
    leaves every non-UI route asking the impossible: the command goes out, the desk refuses it
    silently, and we poll the whole 136 kB model for up to 40 s waiting for a file that can never
    appear.

    THE LIVE CASE IS A DUO. `_clean_restore_reset` calls this unconditionally and the clean-restore
    gate only checks the shows CAPABILITY, which a Duo has, while a Duo has no removable storage at
    all - so a Duo owner choosing a clean profile restore gets a pointless command, a 40 second wait
    and a refusal.

    It also matters for the USB fault: a real export is the prime suspect for taking a desk off the
    bus, so not sending one that cannot succeed removes the risk from every case that was never going
    to work anyway.
    """
    with _hid_lock:
        before, blob = _read_shows_now()
        dest = recorder.destination(recorder.read_volumes(blob))
        if not dest:
            # `no_storage` so callers can word their own refusal for this specific cause - a clean
            # restore wants to point at the merge restore, which needs no backup at all.
            return {"ok": False, "count": len(before), "no_storage": True,
                    "message": ("There is no memory card or USB drive in the unit, so there is "
                                "nowhere to save a backup.")}
        known = {s["snapshot"] for s in before if s.get("snapshot") is not None}
        step(label)
        shows.send_export()

    def appeared(blob):
        for s in shows.read_shows(blob):
            if s.get("snapshot") is not None and s["snapshot"] not in known:
                return s
        return None
    found, _ = shows.wait_for(appeared, read=_locked_dump)
    if not found:
        return {"ok": False, "count": len(before),
                "message": ("The unit took the command but no new backup has appeared on the "
                            "drive. Check there is a drive in and that it has room.")}
    return {"ok": True, "show": found, "count": len(before) + 1}


# How long to listen for the reset's own confirmation. The whole push burst finishes by t+4.96 s on
# a Duo (measured), so this is generous rather than tight - the cost of it being too short is a
# needless fallback dump, and no Pro II figure has been taken yet.
_RESET_PUSH_WAIT_S = 20.0


def _reset_and_wait(step) -> dict:
    """Software-factory-reset the desk and wait for the evidence.

    The evidence is the LOADED SHOW BECOMING A DIFFERENT ONE: `newFromDefault` mints a new uuid, so
    a changed uuid is the desk confirming it in the only way it does.

    WE LISTEN FOR IT RATHER THAN ASK FOR IT. Re-dumping the whole property model every 1.5 s until
    the uuid changes is about 136 kB a time, six or seven times over, and it puts our traffic on the
    wire for the whole time the desk is rewriting its configuration and re-enumerating its USB audio
    function. The desk pushes `currentShowUUID` by itself 0.02 s after the command, and its whole
    burst is done by t+4.96 s (measured on a Duo), so the answer arrives sooner with nothing of ours
    in the way.

    THE HID LOCK IS HELD FOR THE WRITE ONLY, not across the wait: the listener is read-only on its
    own handle, so an announcement or a page read does not queue behind this.

    A READ IS STILL THE FALLBACK. A missed push is not a failed reset, and hidapi makes no promise
    about a frame arriving, so a timeout costs one dump before anything is reported as wrong.
    """
    listener = rcp_read.PushListener()
    listening = False
    try:
        with _hid_lock:
            before_uuid = shows.read_current(rcp_read.read_model_stable(fresh=True)).get("uuid")
            step("Resetting the desk to factory settings and pads...")
            # OPENED AFTER OUR OWN DUMP AND BEFORE THE WRITE. After, so it does not sit through 536
            # frames of the baseline read; before, because the push burst starts 20 ms later and a
            # listener opened afterwards would race it.
            #
            # AND FAILING TO OPEN IT IS NOT A FAILED RESET. Listening is an optimisation over
            # reading, never a requirement, so if a handle cannot be had we do the reset anyway and
            # fall through to the read below. This was found by the offline sim, which has no HID
            # interface at all: the exception killed the whole job and reported "never finished".
            try:
                listener.start()
                listening = True
            except Exception:
                pass
            shows.send_new_from_default()
        # NEVER LONGER THAN THE SHOW-COMMAND TIMEOUT ITSELF. Two independent waits for one command is
        # one too many: `shows.COMMAND_TIMEOUT_S` is the ceiling for "how long we give the desk", so
        # this listens for the shorter of the two and the offline sim's single knob still governs it.
        after = (listener.wait_for("currentShowUUID",
                                   timeout=min(_RESET_PUSH_WAIT_S, shows.COMMAND_TIMEOUT_S),
                                   match=lambda v: v and v != before_uuid)
                 if listening else None)
        listener_lost = listener.lost
        if after:
            # DO NOT ACT ON THE ANSWER THE INSTANT IT ARRIVES. The uuid lands at about t+1.8 s and
            # the desk carries on rewriting itself until t+4.96 s, so the wake below - which WRITES
            # to every mic channel - would otherwise land in the middle of that, and on a reset that
            # is also while the desk re-enumerates USB.
            step("Waiting for the desk to finish...")
            listener.wait_until_quiet()
    finally:
        listener.stop()

    # THE FALLBACK READ ALSO ANSWERS A SECOND QUESTION: is the desk still THERE? "It took the command
    # and did nothing" and "it has left the USB bus" are different failures needing different words,
    # and an earlier draft of this collapsed them - telling anyone whose reset merely did not apply
    # to go and unplug their cable. The offline sim caught it.
    answered = None
    if not after:
        step("Checking the desk...")
        try:
            with _hid_lock:
                got = shows.read_current(rcp_read.read_model_stable(fresh=True)).get("uuid")
            answered = bool(got)
            after = got if (got and got != before_uuid) else None
        except Exception:
            answered = False

    if not after:
        if answered:
            # The desk is present and talking, so nothing needs replugging and nothing is lost.
            return {"ok": False,
                    "message": ("The unit took the command but its show has not changed, so the "
                                "reset does not look like it happened. Nothing has been lost; "
                                "check the Shows list in a moment.")}
        # Not answering. The old wording claimed "the desk does not look reset" and "nothing has
        # been lost" HERE too, and both are wrong once the desk is off the bus: the command has
        # almost certainly applied, and nothing about it can be verified at all.
        return {"ok": False,
                "message": ("The desk stopped answering before it could confirm the reset"
                            + (", and the connection dropped. " if listener_lost else ". ")
                            + "The reset itself was almost certainly carried out, but nothing can "
                              "be checked while the unit is not responding. Unplug the USB cable "
                              "in the socket nearest the power inlet, wait a few seconds and plug "
                              "it back in, then look at the Shows list. If it still does not come "
                              "back, try that once or twice more, and if it will not appear at "
                              "all, disconnect the power, hold the red button on the back for "
                              "sixty seconds, then reconnect the power supply on its own and "
                              "switch the unit on. Nothing is lost either way.")}
    wake_channels()              # a reset leaves the same silent-channel fault; see wake_channels
    # THE OTHER THING A RESET LEAVES BROKEN - the voice-FX pads' effect definitions - IS HEALED BY
    # THE CALLER, NOT HERE, and deliberately. soundpad.heal_fx_pool rebuilds six pads at about
    # fifteen seconds each, and a CLEAN PROFILE RESTORE is about to rewrite the whole pad grid
    # anyway, so healing here would spend ninety seconds building pads that are cleared moments
    # later. Each caller runs it once its own pad phase is finished; it is a no-op when nothing is
    # missing, so the ordering costs nothing when the profile did carry the pads.
    return {"ok": True}


def _clean_restore_reset(step) -> dict:
    """The reset half of a CLEAN profile restore: back the desk up, then reset it.

    REFUSES TO RESET IF THE BACKUP DID NOT LAND. A clean restore is chosen precisely because it
    destroys what it does not replace, so proceeding without the undo would be the one outcome
    nobody asked for."""
    saved = _export_and_wait(step, "Backing the desk up, so the reset can be undone...")
    if not saved.get("ok"):
        if saved.get("no_storage"):
            # NAME THE OTHER OPTION. This is the Duo's permanent case, not a "put a drive in and try
            # again" case, so telling them to find storage would be telling them to buy hardware.
            return {"ok": False,
                    "message": (saved["message"] + " A clean restore has to back the desk up first, "
                                "because it wipes anything the profile does not cover, so it cannot "
                                "be done. Choose the ordinary restore instead: it puts your settings "
                                "back and leaves everything else as it is.")}
        return {"ok": False,
                "message": ("Stopped before resetting anything, and nothing on the desk has "
                            "changed: the backup did not appear on the drive, so there would have "
                            "been no way back. Check there is a drive in with room on it.")}
    done = _reset_and_wait(step)
    if not done.get("ok"):
        return done
    return {"ok": True,
            "message": (f"Reset the desk to factory settings first, after backing it up as "
                        f"\"{saved['show']['name']}\".")}


def action_show_export() -> dict:
    """Back the whole desk up to a new show file on the recordings drive.

    The evidence is a NEW SHOW APPEARING, not the write returning: the command field self-clears
    either way. Since each export renumbers the list, the new one is identified by its uuid being
    one we had not seen a moment earlier, rather than by assuming it is uid 0."""
    def work(step):
        # About 24 MB, measured at roughly a second. Polled rather than slept so it answers quickly.
        res = _export_and_wait(step, "Writing the show to the recordings drive...")
        if not res.get("ok"):
            return res
        count = res["count"]
        return {"ok": True,
                "message": (f"Backed up as \"{res['show']['name']}\". There "
                            f"{'is' if count == 1 else 'are'} now {count} "
                            f"backup{'' if count == 1 else 's'} on the drive. Each one is a new "
                            f"copy of about 24 megabytes, so delete the ones you no longer want.")}
    return _run_show_job("Exporting the show", work)


def action_show_import(snapshot: int, as_name: str = "") -> dict:
    """Restore the whole desk from an exported show.

    `as_name` IS THE NAME THE USER GAVE IT, and it exists because the unit does not keep one: every
    export is named after whichever show was loaded, so a backup saved as "Testing New Backup Flow"
    is called "Default Show" on the drive. Reporting the desk's name therefore tells the user they
    have restored something they never chose, which is the worst sentence a restore can produce.
    The Backups page passes the name it holds; the Shows list has none to pass and falls back to
    the desk's.

    TAKES A SNAPSHOT INDEX, NOT A UID AND NOT A UUID. A uid is a position that any export renumbers,
    so a page holding one is holding something that may already mean a different file; a uuid names
    the SHOW rather than the file, so several backups can share one. The snapshot index is the only
    stable per-file handle (shows.py trap 2), and it is resolved to a uid against a read taken
    moments before the write, because the protocol only accepts a uid."""
    try:
        snapshot = int(snapshot)
    except (TypeError, ValueError):
        return {"ok": False, "message": "Choose which backup to restore."}

    def work(step):
        with _hid_lock:
            current, blob = _read_shows_now()
            match = shows.find_by_snapshot(current, snapshot)
            if match is None:
                return {"ok": False,
                        "message": "That backup is no longer on the drive. The list has been "
                                   "refreshed."}
            # NO UUID PRE-CHECK. Refusing a backup whose uuid matches the loaded show assumes the
            # unit would reject it, and THE UNIT DOES NOT: proven twice on hardware, where a backup
            # sharing the loaded uuid restored Mic 4's gain from 30 to 50 and again from 42 to 50,
            # with showControlLastError staying 0. Since a uuid only changes on a factory reset,
            # that refusal blocks the ordinary case this feature exists for - change something, put
            # it back - and leaves shows usable only straight after a reset. The unit's own refusal
            # (error 1) is reported below; we simply never guess it in advance.
            before = shows.restore_fingerprint(blob)
            shown = as_name or match["name"]     # see the docstring: the user's name wins
            step(f"Restoring \"{shown}\". This also moves the monitor level.")
            shows.send_import(match["uid"])
        target_uuid = match.get("uuid")
        loaded_uuid = shows.read_current(blob).get("uuid")

        # THE EVIDENCE IS THE STATE MOVING, not the uuid: see shows.restore_fingerprint for why
        # every other signal fails. The uuid test is kept as a second route because it is the one
        # that fires when the backup IS a different show (the undo for a factory reset).
        def settled(blob):
            err = shows.read_last_error(blob)
            if err:
                return {"error": err}
            if target_uuid != loaded_uuid and shows.read_current(blob).get("uuid") == target_uuid:
                return {"done": True}
            if shows.restore_fingerprint(blob) != before:
                return {"done": True}
            return None
        got, blob_after = shows.wait_for(settled, read=_locked_dump)
        if got and got.get("error"):
            return {"ok": False, "message": shows.error_message(got["error"])}
        tail = ("Everything is back: settings, routing, the fader layout and the SMART pads. The "
                "monitor speaker level will have moved, so check it before turning speakers up.")
        if not got:
            # THE DESK NEVER ANSWERED AT ALL, which is a different outcome from "it did nothing"
            # and has to be said differently. `shows.wait_for` hands back no blob when not one
            # usable dump arrived in the whole wait, and that is what a desk leaving the bus
            # mid-restore looks like from here: it stops answering about 30 s after the import
            # command and Windows drops it a minute later. Without this branch the same case
            # reports a cheerful success.
            if blob_after is None:
                return {"ok": False,
                        "message": ("The unit stopped responding during the restore and is no "
                                    "longer answering. Unplug its USB cable and plug it back in, "
                                    "then check the Device page before trusting any setting.")}
            # Nothing moved and the unit raised no error. That is only benign for a backup of the
            # SAME show, where "nothing to do" is a real and reportable outcome rather than a
            # failure - saying "it did not work" would send someone hunting a problem that is not
            # there. For a DIFFERENT show it is a genuine failure, because a successful import
            # would have changed the loaded uuid and it did not.
            if (target_uuid == loaded_uuid and shows.read_last_error(blob_after) in (None, 0)):
                return {"ok": True,
                        "message": (f"The unit already matched \"{shown}\", so nothing "
                                    f"needed to change.")}
            return {"ok": False,
                    "message": (f"The unit took the command but has not loaded "
                                f"\"{shown}\" yet. Check the list in a moment.")}
        # THE ENGINE CAN COME BACK WITH A CHANNEL SILENT. See wake_channels: the model looks
        # perfect, so this cannot be conditional on detecting anything.
        wake_channels()
        return {"ok": True, "message": f"Restored \"{shown}\". {tail}"}
    return _run_show_job("Restoring the show", work)


def wake_channels() -> dict:
    """Force the audio engine to re-latch every mic channel, after a reset or an import.

    THE FAULT THIS EXISTS FOR, reproduced deliberately on hardware: after a software factory reset
    followed by a show import, a mic channel can be COMPLETELY SILENT while the control model is
    byte-identical to its working state.
    Measured: 472 properties compared, 8 differed, and all 8 were the new backup's own list entry and
    the drive's free space. Level 1|1, chain matching the working reference exactly, unmuted, gain
    and phantom correct - and no audio. So NOTHING CAN DETECT THIS BY READING THE DESK, and any fix
    that displays a value is wasted effort.

    WHAT WAKES IT IS A REAL VALUE CHANGE, and only that. Writing the gain back at its CURRENT value
    does nothing at all (measured), which is why the desk's own encoder was the
    only thing that ever recovered it: an encoder sends a genuine change. So this nudges the gain by
    a step and puts it straight back.

    Kept to MIC channels because that is where a dead input is silently catastrophic and where we
    have a cheap real-value write. Whether the fault can hit a non-mic channel is UNKNOWN: nothing
    was plugged into the others when it was reproduced, so it could not be heard either way.
    """
    dev = devices.get_active_device()
    if "input_mic_write" not in dev.caps:
        return {"ok": False, "message": f"Cannot wake the channels on a {dev.name}."}
    # THE GAIN COMES FROM THE DESK, NOT FROM `_session_state`. The "put it straight back" half of
    # the nudge has to write the value the desk currently holds: the only callers are a reset and
    # an import, i.e. exactly the two moments the cache is guaranteed stale, because changing the
    # desk is what they are for. Sourcing it from the cache puts every mic gain back to whatever
    # the app last displayed, so a backup holding Mic 1 at 55 dB restores correctly and then has 45
    # written over it. The damage is invisible whenever the desk is off the bus, because the write
    # goes nowhere and the restored value survives by accident.
    try:
        with _hid_lock:
            blob = rcp_read.read_model_stable(fresh=True)
    except Exception:
        blob = None
    if not rcp_read.is_model_dump(blob):
        return {"ok": False, "woken": [],
                "message": "The unit is not answering, so its mic channels could not be woken."}
    woken = []
    for ch in MIC_CHANNELS:
        try:
            now = channels.mic_gain(blob, ch)
            if now is None:
                continue
            # A step DOWN unless that would fall off the bottom, so the nudge is always a real
            # change. Putting the original back immediately means the user never hears the step.
            other = now - 1 if now > GAIN_MIN_DB else now + 1
            with _hid_lock:
                channels.set_mic_gain(ch, other)
                channels.set_mic_gain(ch, now)
            _ann_mark_self_gain(ch, True)
            woken.append(ch)
        except Exception:
            continue                      # one channel failing must not leave the rest unwoken
    return {"ok": True, "woken": woken,
            "message": (f"Nudged {len(woken)} mic channel(s) so the unit's audio engine picks them "
                        f"up again." if woken else "No mic channels needed waking.")}


def _restore_silence_warnings(prof, blob) -> list:
    """What this backup would leave you unable to hear, worked out BEFORE anything is sent.

    THE ONE WARNING THAT HAS TO ARRIVE EARLY. Everything the app says after a restore is spoken
    through the desk, so a restore that silences an output silences its own explanation: lose Main
    Stereo and the announcement telling you so goes with it. The confirmation dialog is read while
    the audio still works, and it can still be declined, so this is the only place a warning about
    silence is any use.

    TWO WAYS A BACKUP LEAVES A CHANNEL SILENT, and both are invisible afterwards:
      * it MOVES the channel to a different fader, and the level belongs to the fader rather than
        the channel, so it inherits whatever that strip was sitting at;
      * it sets that fader's level to zero outright.

    A backup with no `fader_levels` at all is the worse case, and most older ones are like that: it
    rearranges the layout and then has nothing to say about the levels it scrambled.
    """
    out = []
    try:
        spec = _active_faders()
        routing = prof.get("routing") or {}
        want = routing.get("faders")
        want_levels = routing.get("fader_levels")
        now_vec = _read_fader_vector(blob, spec)
        now_levels = rcp_read.values(blob, "faderLevel")

        def level_of(i, table):
            return table[i] if table and i < len(table) and isinstance(table[i], int) else None

        for i in sorted(spec.level_selectors):
            code = want[i] if want and i < len(want) else (now_vec[i] if i < len(now_vec) else None)
            if code is None or code < 0:
                continue
            after = level_of(i, want_levels) if want_levels else level_of(i, now_levels)
            if after == 0:
                out.append(f"{_channel_name(code, spec)} will be silent: it ends up on "
                           f"{_fader_label(i, spec)}, which is at zero.")
        if want and list(want[:len(now_vec)]) != list(now_vec[:len(want)]):
            movers = [_channel_name(c, spec) for i, c in enumerate(want)
                      if i < len(now_vec) and c != now_vec[i] and c is not None and c >= 0]
            if movers:
                out.append("It rearranges the faders, moving " + ", ".join(sorted(set(movers))) + ".")
            if not want_levels:
                out.append("It does not set fader levels, so each channel keeps whatever level "
                           "the fader it lands on is already at.")
    except Exception:
        return out          # a warning that crashes must never block the restore it describes
    return out


def backup_restore_plan(name: str) -> dict:
    """How this backup WOULD be restored, without restoring it. The page asks so its confirmation
    dialog can say which of the two is about to happen, in the user's terms rather than ours.

    Split out from the restore itself because the dialog needs it BEFORE the user commits, and
    because "is the paired show still on the drive" is a device read that must not happen twice."""
    if name.startswith("show:"):
        # One the unit made, with no profile beside it: there is only one way to put it back, and
        # NOTHING CAN BE SAID ABOUT WHAT IS IN IT. The show file is the desk's own and we cannot
        # read it, so the honest answer is to say so rather than to imply we checked.
        return {"ok": True, "name": name, "via": "show", "paired": True,
                "seconds_per_pad": PAD_RESTORE_SECONDS, "pads": 0,
                "warnings": ["This backup was made by the unit itself, so there is no way to "
                             "tell you what it contains before it is restored. It may move "
                             "channels between faders or change their levels."]}
    loaded = profiles.load(name)
    if not loaded.get("ok"):
        return loaded
    prof = loaded["prof"]
    paired = prof.get(profiles.SHOW_KEY) or None
    on_drive = None
    blob = None
    try:
        with _hid_lock:
            blob = rcp_read.read_model()
    except Exception:
        blob = None
    # `on_drive` IS NO LONGER WHAT THE RESTORE USES. It is still read because the page says whether
    # a backup has the unit's own copy beside it, but the restore itself always takes the profile
    # now - see action_restore_backup for the crash rate that decided it.
    if paired and blob is not None and "shows" in devices.get_active_device().caps:
        try:
            on_drive = shows.find_by_snapshot(shows.read_shows(blob), paired.get("snapshot"))
        except Exception:
            on_drive = None
    # THE PROFILE IS READ EVEN WHEN THE SHOW WILL BE USED. The two halves were saved in the same
    # instant, so the profile is the only readable description of what the show is about to do.
    warnings = _restore_silence_warnings(prof, blob) if blob is not None else []
    return {
        "ok": True,
        "name": loaded["safe"],
        "via": "profile",             # always, now; see action_restore_backup
        "has_show": bool(on_drive),   # the unit's own copy is still there, and still not used
        "paired": bool(paired),
        "seconds_per_pad": PAD_RESTORE_SECONDS,
        "pads": len(prof.get("pads") or []),
        "warnings": warnings,
    }


def read_backups() -> dict:
    """THE ONE LIST BEHIND THE BACKUPS PAGE: every backup this desk can be put back from, whichever
    mechanism happens to hold it.

    The user types one name, presses one button and never learns there are two mechanisms; this is
    the read that lets the page keep that promise. Three kinds come out of it, and the kind is the
    page's business, not the user's:
      * a PAIRED backup - a profile on this computer plus the unit's own show on the drive. Restores
        in seconds while the drive is in, and still restores from the profile when it is not.
      * a PROFILE-ONLY backup - either there was no storage when it was taken, or the show has since
        been deleted or the drive erased. Slower and misses what a profile cannot write.
      * a UNIT-MADE show with no profile beside it, which is one taken at the touchscreen or by
        RODE's own software. Named by the desk, not by anyone here, so it cannot be renamed.

    ONE DEVICE READ, NOT ONE PER BACKUP. `backup_restore_plan` answers "is the paired show still
    there" for a single name and reads the model to do it; asking it per profile would cost a dump
    each. The shows are read once and matched by snapshot."""
    names = profiles._list_profile_names()
    dev = devices.get_active_device()
    on_drive, storage, note = [], False, ""
    if "shows" in dev.caps:
        try:
            with _hid_lock:
                blob = rcp_read.read_model()
            on_drive = shows.read_shows(blob)
            storage = bool(recorder.destination(recorder.read_volumes(blob)))
        except Exception as exc:
            note = f"The unit's own backups could not be read: {exc}"
    by_snapshot = {s.get("snapshot"): s for s in on_drive}

    out, claimed = [], set()
    for safe in names:
        loaded = profiles.load(safe)
        if not loaded.get("ok"):
            continue
        prof = loaded["prof"]
        paired = prof.get(profiles.SHOW_KEY) or None
        snap = paired.get("snapshot") if paired else None
        show = by_snapshot.get(snap) if snap is not None else None
        if show is not None:
            claimed.add(snap)
        out.append({
            "key": "profile:" + safe,
            "name": safe,
            "kind": "paired" if show else "profile",
            "pads": len(prof.get("pads") or []),
            "saved_at": prof.get("saved_at"),
            "baseline": bool(prof.get("baseline")),
            "renameable": True,
            "snapshot": snap if show else None,
        })
    for s in on_drive:
        if s.get("snapshot") in claimed:
            continue
        out.append({
            # The DESK named this one, after whichever show was loaded, and it offers no rename, so
            # several can share a name. The modification date is the only thing that tells them
            # apart, which is why it is part of the label rather than a detail line.
            "key": "show:%s" % s.get("snapshot"),
            "name": s.get("name") or "Made on the unit",
            "kind": "unit",
            "pads": None,
            "saved_at": None,
            "modified": s.get("modified"),
            "baseline": False,
            "renameable": False,
            "snapshot": s.get("snapshot"),
        })
    return {"ok": True, "backups": out, "storage": storage, "shows_capable": "shows" in dev.caps,
            "note": note}


def action_delete_backup(name: str, force: bool = False) -> dict:
    """Delete a backup, BOTH HALVES OF IT. `name` is a profile name, or "show:<snapshot>" for one
    the unit made with no profile beside it.

    The page offers one Delete because the user made one backup; leaving the unit's copy behind
    would quietly fill the drive with 24 MB files they believe they have deleted. The show goes
    first: if that fails there is still a profile listed, whereas the reverse leaves an orphan
    nobody can name."""
    if name.startswith("show:"):
        try:
            snap = int(name.split(":", 1)[1])
        except ValueError:
            return {"ok": False, "message": "That backup could not be identified."}
        return action_show_delete(snap)
    loaded = profiles.load(name)
    if not loaded.get("ok"):
        return loaded
    # THE AS-FOUND BACKUP'S TWO-STEP SURVIVES THIS ROUTE, and it is asked BEFORE the unit's own
    # copy is touched. `profiles.delete` refuses the baseline without `force` and the page arms the
    # second press from that refusal; deleting the show first and then hitting the refusal would
    # destroy half of the one record of how the desk arrived, while reporting that nothing happened.
    if not force and profiles.is_baseline(loaded["prof"]):
        return profiles.delete(name, force=False)
    paired = (loaded["prof"].get(profiles.SHOW_KEY) or {}).get("snapshot")
    if paired is not None and "shows" in devices.get_active_device().caps:
        try:
            with _hid_lock:
                blob = rcp_read.read_model()
            still_there = shows.find_by_snapshot(shows.read_shows(blob), paired)
        except Exception:
            still_there = None
        if still_there is not None:
            # BOTH HALVES IN ONE JOB, so the page shows one progress dialog and one result. `via`
            # tells it which job state to watch, exactly as the restore does.
            res = _delete_show_then_profile(paired, name)
            res["via"] = "show"
            return res
    res = delete_profile(name, force=True)
    res["via"] = "profile"
    return res


def action_restore_backup(name: str, clean: bool = False) -> dict:
    """RESTORE A BACKUP, from this computer's copy. See RESTORE_USES_THE_PROFILE below.

    THE SHOW IMPORT CRASHES THE DESK ABOUT ONE TIME IN SIX, so this does not use it. Measured on a
    Pro II with the diagnostic log running:

        PROFILE restores :   9   failed: 0   median 126s
        SHOW imports     :  31   failed: 5   median   6s

    Five crashes, on three different backups, with Bluetooth connected and disconnected. The desk
    hangs inside its own firmware, keeps a stuck audio buffer looping on the headphone output, stays
    electrically present on USB for another minute, and then needs its MAINS pulled - a USB replug
    does not bring it back, because the link is not the problem. Nothing we send is wrong and no
    change to our code prevents it; avoiding the path is the only fix available to us.

    WHY THAT IS WORTH THE COST, and the cost is real: the profile writes back 148 of 182 writable
    properties one at a time and takes two minutes against six seconds. What it misses that anyone
    would notice is the monitor output level and the auto-duck depth. Bluetooth pairing, the
    headline reason for shows existing at all, is NOT carried by a show either (proven on hardware).
    Against that: the desk IS the screen reader's audio device for the people this app is for, so a
    desk that stops responding takes their speech with it, and an NVDA add-on that can escape to
    another output is not something everyone has.

    THE EXPORT HALF STAYS. It is the import that crashes; backing up to a show has never
    misbehaved, costs seconds, and leaves a complete copy on the drive if it is ever wanted.

    `name` may also be "show:<snapshot>", which is a backup the unit made with no profile beside it.
    That one has no profile to restore from, so it is the single remaining case that must use the
    import, and it says so before it runs."""
    # THE REPLY SAYS WHICH HALF RAN, in `via`, and the page needs it rather than merely liking it:
    # the two halves report progress through DIFFERENT job states (the show commands share one, the
    # profile save/restore another), so a page that guessed wrong would poll a job that is not
    # running and watch a finished restore in silence.
    if name.startswith("show:"):
        try:
            res = action_show_import(int(name.split(":", 1)[1]))
        except ValueError:
            return {"ok": False, "message": "That backup could not be identified."}
        res["via"] = "show"
        return res
    # "RESET THE UNIT TO FACTORY FIRST" IS A PROFILE-RESTORE MODE, so asking for it forces the
    # profile path even where a show is available. The show is an exact copy of the desk, which is
    # the opposite of what this mode is for: it exists to clear everything the profile does NOT
    # cover, so that what is left matches the profile and nothing else survives underneath.
    if clean:
        res = restore_profile(name, clean=True)
        res["via"] = "profile"
        return res
    # RESTORE_USES_THE_PROFILE, never the paired show, however available the show is; the docstring
    # holds the measurement behind that. The pairing is still made and still exported, so backing up
    # is unaffected and a paired backup is not stranded - only which half is read back.
    plan = backup_restore_plan(name)
    if not plan.get("ok"):
        return plan
    res = restore_profile(name)
    res["via"] = "profile"
    return res


def action_show_reset(backup_first: bool = True) -> dict:
    """THE SOFTWARE FACTORY RESET: settings and pads back to factory, without the setup wizard.

    `backup_first` DEFAULTS TO TRUE and the UI does not offer False lightly, because an export is
    the only real undo this has: it costs about a second, and without it there is no way back to
    what the desk was. It is also what MAKES the undo legal - the reset mints a new uuid, and an
    import is refused while the uuids match (shows.py trap 2).

    What survives: wifi, language and the analytics answer all live outside the show. Recordings on
    the drive are untouched.
    """
    def work(step):
        saved = None
        if backup_first:
            # REFUSE RATHER THAN CARRY ON if the backup does not land. Asked for a reset WITH a
            # backup, doing the reset anyway would destroy the desk's state having promised not to.
            # The user can still choose the unbacked reset explicitly.
            res = _export_and_wait(step, "Backing the desk up first, so the reset can be undone...")
            if not res.get("ok"):
                if res.get("no_storage"):
                    return {"ok": False,
                            "message": (res["message"] + " Nothing on the desk has changed. Either "
                                        "put a card or drive in, or choose the reset without a "
                                        "backup, which cannot be undone.")}
                return {"ok": False,
                        "message": ("Stopped before resetting anything, and nothing on the desk "
                                    "has changed: the backup did not appear on the drive, so there "
                                    "would have been no way back. Check there is a drive in with "
                                    "room on it, and try again.")}
            saved = res["show"]
        done = _reset_and_wait(step)
        if not done.get("ok"):
            return done
        # REBUILD THE VOICE EFFECTS THE DEFAULT SHOW DOES NOT BRING WITH IT. Without this the reset
        # hands back six voice-FX pads with the right name, type and colour that do nothing at all,
        # and says "factory SMART pads" while it does so - see soundpad.heal_fx_pool for the
        # mechanism and the captures that prove it.
        step("Rebuilding the factory voice effects...")
        with _hid_lock:
            healed = soundpad.heal_fx_pool(progress=step)
        _hk_pads_stale()          # the pad grid was rewritten; the shortcut layer's map is stale
        msg = ("The desk is back to factory settings and factory SMART pads. Your wifi, language "
               "and the analytics answer were not part of it and are untouched, and so are the "
               "recordings on the drive. The monitor speaker level has gone back to its default, "
               "so check it before turning speakers up.")
        if healed.get("message"):
            msg += " " + healed["message"]
        if saved:
            msg += f" To undo this, restore \"{saved['name']}\" from the Shows list."
        return {"ok": True, "message": msg}
    return _run_show_job("Resetting the desk", work)


def action_show_delete(snapshot: int) -> dict:
    """Delete one exported backup from the drive. By snapshot index (see action_show_import).

    Deleting the backup that happens to hold the LOADED show is ALLOWED. Refusing it treats the uuid
    as a file identity, which it is not (shows.py trap 2), so with several backups of the current
    state such a rule refuses to delete ANY of them - exactly the case where the drive fills up. A
    show FILE is not the loaded state, and deleting one does not disturb the desk."""
    try:
        snapshot = int(snapshot)
    except (TypeError, ValueError):
        return {"ok": False, "message": "Choose which backup to delete."}

    return _run_show_job("Deleting the backup",
                         lambda step: _show_delete_work(snapshot, step))


def _show_delete_work(snapshot: int, step) -> dict:
    """Take one backup off the drive and wait for it to be gone.

    RUNS INSIDE A SHOW JOB THAT THE CALLER OWNS, which is the whole reason it is not just the body
    of the function above: the unified delete has to remove the unit's copy AND this computer's in
    ONE job, so the user gets one progress dialog and one result for one backup."""
    with _hid_lock:
        current, blob = _read_shows_now()
        match = shows.find_by_snapshot(current, snapshot)
        if match is None:
            return {"ok": False,
                    "message": "That backup is no longer on the drive. The list has been "
                               "refreshed."}
        step(f"Deleting \"{match['name']}\"...")
        shows.send_delete(match["uid"])
    # The evidence is that SNAPSHOT INDEX leaving the list. A count drop alone would be fooled
    # by anything else removing a backup at the same moment.
    def gone(blob):
        return True if shows.find_by_snapshot(shows.read_shows(blob), snapshot) is None else None
    done, _ = shows.wait_for(gone, read=_locked_dump)
    if not done:
        return {"ok": False,
                "message": f"The unit took the command but \"{match['name']}\" is still on the "
                           f"drive."}
    return {"ok": True, "message": f"Deleted \"{match['name']}\" and gave the space back."}


def _delete_show_then_profile(snapshot: int, name: str):
    """Both halves of one backup, in one job: the unit's copy first, then this computer's.

    THE ORDER IS THE POINT. If the show delete fails, the profile is still listed and the user can
    try again; the other way round leaves 24 MB on the drive under a name nothing on this page knows
    about any more."""
    def work(step):
        res = _show_delete_work(snapshot, step)
        step("Deleting this computer's copy...")
        gone = delete_profile(name, force=True)
        if not gone.get("ok"):
            return gone
        tail = (" The unit's own copy went too." if res.get("ok")
                else " The unit's own copy could not be deleted, so it is still on the drive.")
        return {"ok": True, "message": (gone.get("message") or "Deleted.") + tail}
    return _run_show_job("Deleting the backup", work)


def _pad_write(grid: int, failed: str, work, invalid: str = None,
               plain_value_error: bool = False) -> dict:
    """Range-check a grid pad, take the HID lock, run `work()`, and turn a crash into a sentence.

    THE SHAPE EVERY LIVE PAD EDIT HAS. Seventeen actions need these eight lines, and only one
    sentence differs between them: `failed`, the "Could not ..." wording, which is the only part
    worth reading at a call site.

    `invalid` is a refusal message for a value this function cannot check itself (a play mode, a
    trigger field), and it is checked AFTER the range and BEFORE the lock: the pad number is the
    more basic complaint, and nothing should take the device lock to find out a value was wrong.

    `plain_value_error` surfaces a ValueError's own text as the message instead of wrapping it.
    soundpad raises those with a finished, user-facing sentence in them (a polluted effect pool,
    an unknown effect name), and wrapping that in "Could not assign the voice effect:" buries the
    part that says what to do about it.
    """
    if grid < 1 or grid > _max_user_pads():
        return {"ok": False, "message": f"Pad {grid} is out of range."}
    if invalid:
        return {"ok": False, "message": invalid}
    # THE SHORTCUT LAYER'S PAD MAP IS NOW WRONG, whatever this edit turns out to be. It caches the
    # name, type, play mode and stream POSITION of every pad on a timer, and every one of those can
    # move here - clearing a pad shifts the positions of the ones after it. Marked stale rather than
    # re-read: this is the wrong moment to spend a model dump, and the next press reads it anyway.
    _hk_pads_stale()
    try:
        with _hid_lock:
            return work()
    except ValueError as exc:
        if plain_value_error:
            return {"ok": False, "message": str(exc)}
        return {"ok": False, "message": f"{failed}: {exc}"}
    except Exception as exc:
        return {"ok": False, "message": f"{failed}: {exc}"}


def _pad_name_from_filename(filename: str) -> str:
    """Derive a tidy pad name from an uploaded filename (stem, alnum/space/underscore, capped)."""
    stem = os.path.splitext(os.path.basename(filename or "Sound"))[0]
    clean = "".join(c if (c.isalnum() or c in " _-") else " " for c in stem).strip()
    clean = " ".join(clean.split())            # collapse runs of whitespace
    return (clean[:24] or "Sound")


def action_add_sound(pad_n: int, filename: str, raw: bytes, replay: bool = True) -> dict:
    """Put the uploaded sound on the pad via the PROVEN clear+create path (soundpad.create_sound),
    NEVER an overwrite-in-place. create_sound clears the slot if occupied, then creates a fresh
    sound pad writing pads/<grid>/sound.<ext>, and is gated on a clean effect-object pool.

    NO CONVERSION, ear-proven: the unit's pad player accepts WAV at every rate (22k-192k), bit depth
    (16/24/32-float) and channel count tested, plus MP3, all played natively. So we write the file
    AS-IS (no resample / downmix / bit-depth change) and never degrade quality. We only require the
    file to be WAV or MP3 (the two
    formats the RODE manual lists) and preserve its extension on the device, because an MP3 written
    under a .wav name plays as silence (the firmware trusts the extension).
    """
    if pad_n < 1 or pad_n > _max_user_pads():
        return {"ok": False, "message": f"Pad {pad_n} is out of range."}
    if not raw:
        return {"ok": False, "message": "The uploaded file was empty."}

    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in (".wav", ".mp3"):
        return {"ok": False,
                "message": "Only WAV or MP3 files can be added to a pad. "
                           "Please choose a .wav or .mp3 file."}

    name = _pad_name_from_filename(filename)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=ext, prefix="accesscaster_")
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        with _hid_lock:
            # dest_filename = the real upload name so the device stores it as the factory does
            # (pads/<n>/RealName.ext), not a generic sound.wav. name = the on-screen pad label.
            return soundpad.create_sound(pad_n, tmp_path, name=name, replay=replay,
                                         dest_filename=filename)
    except Exception as exc:
        return {"ok": False, "message": f"Could not add the pad sound: {exc}"}
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def action_set_replay(grid: int, on: bool) -> dict:
    """Toggle padReplay on an existing sound pad in place (restart-on-re-press vs pause/resume)."""
    return _pad_write(grid, "Could not change the pad's replay setting",
                      lambda: soundpad.set_pad_replay(grid, on))


def action_set_pad_gain(grid: int, db: float) -> dict:
    """Set per-pad output volume (padGain, dB) on an existing pad, in place. Read-back verifiable.

    THE NAME MUST STAY DISTINCT from the mic action_set_gain above. Give both the same name and
    Python keeps the last def, so this pad version silently shadows the mic one and every mic
    Level/gain write is routed into soundpad.set_pad_gain."""
    return _pad_write(grid, "Could not change the pad's volume",
                      lambda: soundpad.set_pad_gain(grid, db))


def action_set_pad_name(grid: int, name: str) -> dict:
    """Rename an existing pad in place (padName). Read-back verifiable."""
    name = (name or "").strip()
    return _pad_write(grid, "Could not rename the pad",
                      lambda: soundpad.set_pad_name(grid, name),
                      invalid=None if name else "Enter a name for the pad.")


def action_set_pad_loop(grid: int, on: bool) -> dict:
    """Toggle loop (padLoop) on an existing sound pad. Read-back verifiable."""
    return _pad_write(grid, "Could not change the pad's loop setting",
                      lambda: soundpad.set_pad_loop(grid, on))


def action_set_play_mode(grid: int, mode: int) -> dict:
    """Sound pads: Toggle (0), Hold (1) or One Shot (2). Values ear-proven on hardware: the stored
    order is NOT the guide's printed "Toggle / One Shot / Hold" order."""
    return _pad_write(grid, "Could not change how the pad plays",
                      lambda: soundpad.set_play_mode(grid, mode),
                      invalid=None if mode in soundpad.PLAY_MODE_LABEL else
                      "Play mode must be 0 (Toggle), 1 (Hold) or 2 (One Shot).")


def action_set_mixer_trigger(grid: int, mode: int) -> dict:
    """Mixer pads: Momentary (0, only while held) or Latching (1, press on / press again off)."""
    return _pad_write(grid, "Could not change the pad's trigger mode",
                      lambda: soundpad.set_mixer_trigger(grid, mode),
                      invalid=None if mode in (0, 1) else
                      "Trigger mode must be 0 (Momentary) or 1 (Latching).")


def action_set_pad_trigger(grid: int, field: str, value: int) -> dict:
    """MIDI trigger pads: change ONE field of what the pad sends (type, control, channel, on, off),
    or turn the custom mapping off again with field="custom", value=0.

    ONE field per request, like every other live pad edit, so each change is read back and spoken on
    its own. `field` is validated against soundpad.TRIGGER_EDIT (the same spec that builds the
    browser controls and the write ranges), so an unknown field is REFUSED rather than quietly
    ignored - a silent no-op on a pad mapping is the worst outcome here, since nothing about the desk
    would look wrong afterwards."""
    known = {f["key"]: f for f in soundpad.TRIGGER_EDIT}
    if field == "custom":
        def _custom():
            res = soundpad.set_trigger_mapping(grid, custom=bool(value))
            # Switching custom OFF is destructive: the desk resets the control number, channel and
            # values to its own defaults and does not give them back if you switch it on again
            # (measured). Say so in the message, because a switch reads as reversible.
            if res.get("ok") and not value:
                res["message"] = (res.get("message", "") + " The pad is back on its default "
                                  "mapping, and the numbers you had set are not kept.")
            return res

        return _pad_write(grid, "Could not change the MIDI mapping", _custom)
    spec = known.get(field)
    if not spec:
        bad = ("Unknown MIDI trigger setting %r. Expected one of: %s."
               % (field, ", ".join(known)))
    elif value < spec["min"] or value > spec["max"]:
        bad = "%s must be between %d and %d." % (spec["label"], spec["min"], spec["max"])
    else:
        bad = None
    return _pad_write(grid, "Could not change the MIDI trigger",
                      lambda: soundpad.set_trigger_mapping(grid, **{field: value}),
                      invalid=bad)


def action_set_pad_envelope(grid: int, field: str, percent: float) -> dict:
    """Sound pads: one point of the trim/fade envelope, as a percentage through the sound.

    The four points are start / fade_in / fade_out / stop, and they are POSITIONS along the sound
    (ear-proven, see soundpad.ENV_FIELD). soundpad validates the field name and the range
    and reports when the result is a fade that cannot be heard."""
    return _pad_write(grid, "Could not change the pad's trim and fade",
                      lambda: soundpad.set_envelope_percent(grid, field, percent))


def action_set_pad_fade(grid: int, field: str, seconds: float) -> dict:
    """Fade mixer pads: how long the fade in or the fade out takes, in seconds."""
    return _pad_write(grid, "Could not change the fade time",
                      lambda: soundpad.set_fade_seconds(grid, field, seconds))


def action_set_pad_flag(grid: int, name: str, on: bool) -> dict:
    """The two named per-pad switches: a Fade pad excluding the host, and a Censor pad using your own
    sound instead of the beep. Named (soundpad.PAD_FLAG) rather than taking a property, so the
    browser cannot ask for an arbitrary pad write."""
    return _pad_write(grid, "Could not change that pad setting",
                      lambda: soundpad.set_pad_flag(grid, name, on))


def action_set_back_channel(grid: int, source: str, on: bool) -> dict:
    """Add or remove one channel from a Back Channel pad's off-air talkback room.

    `source` is the padMixerBackChannel* property name, as handed to the UI in the pads payload
    (soundpad.back_channel_sources); soundpad validates it against what this desk actually has.
    A Back Channel pad with no members links nothing and does nothing when pressed, so this is
    the setting that makes the pad work at all."""
    return _pad_write(grid, "Could not change the Back Channel members",
                      lambda: soundpad.set_back_channel_member(grid, source, on))


def action_set_pad_colour(grid: int, colour: int) -> dict:
    """Set a pad's display colour index (padColourIndex 0-11). Read-back verifiable."""
    return _pad_write(grid, "Could not change the pad's colour",
                      lambda: soundpad.set_pad_colour(grid, colour))


def action_clear_pad(grid: int) -> dict:
    """Clear the pad at grid position `grid`. Gated on a clean effect-object pool by soundpad."""
    return _pad_write(grid, "Could not clear the pad",
                      lambda: soundpad.clear_grid_pad(grid))


def action_clear_all_pads() -> dict:
    """Clear every user pad (sound/FX/mixer/MIDI/quick-dial), leaving the System pads untouched.
    Destructive - the web UI confirms with a modal before calling this."""
    try:
        with _hid_lock:
            res = soundpad.clear_all_pads()
            # allow_empty: with every pad cleared, NO pad points at any file, so all of it is
            # genuinely unused. This is the only caller that may say so - see tidy_unused_pad_audio.
            tidy = soundpad.tidy_unused_pad_audio(allow_empty=True)
        if tidy.get("removed"):
            res["message"] = (res.get("message") or "Cleared every user pad.") + " " + tidy["message"]
        return res
    except Exception as exc:
        return {"ok": False, "message": f"Could not clear all pads: {exc}"}


# Pad-defaults restore runs in a background thread (it takes ~a minute) so the page is not blocked
# and can poll progress for live-region announcements. This state is the single source the status
# endpoint reports.
_pad_restore_job = _BackgroundJob("A pad restore is already running.",
                                  "The pad restore failed")


def pad_restore_status() -> dict:
    return _pad_restore_job.status()


def action_restore_factory_fx() -> dict:
    """Put the six factory VOICE EFFECTS back on their original pads.

    Decoded a long time ago and never wired up, because `soundpad.restore_factory_fx` was written
    CLI-shaped: it printed and returned nothing, so there was nothing for a page to report. The
    wrapper it needed is `restore_factory_fx_pads`.

    DESTRUCTIVE and narrower than it sounds, which is what the confirm dialog has to get across: it
    clears and recreates those six grid positions only. The eight factory SOUND pads are untouched,
    and so is everything past pad 16. Reuses the pad-restore progress channel, so the page shows the
    same progress dialog it already knows."""
    def _work(job):
        with _hid_lock:
            return soundpad.restore_factory_fx_pads(live=True, progress=job.tick)

    return _pad_restore_job.start(
        _work, total=len(soundpad.factory_fx_names()),
        message="Starting the factory voice effect restore.",
        phrase=lambda msg, _n, _total: f"{msg}.",
        started="Restoring the factory voice effects.",
        failed="Could not restore the factory voice effects")


def pad_export_dir() -> str:
    """Where an exported pad sound goes: Documents\\AccessCaster\\Pad exports.

    Documents rather than the app's own data folder (%LOCALAPPDATA%), and that is the whole point of
    the feature: an export you cannot find is not an export. There is no folder picker because the
    page is a browser page and a native picker is not something a screen reader user should have to
    fight for a fixed destination anyway - the reply says the full path, and Open folder takes you
    there."""
    base = os.path.join(os.path.expanduser("~"), "Documents", "AccessCaster", "Pad exports")
    os.makedirs(base, exist_ok=True)
    return base


def action_export_pad(pad: int) -> dict:
    """Copy one pad's sound off the unit into the exports folder. Reads the unit; changes nothing."""
    return _pad_write(pad, f"Could not export pad {pad}",
                      lambda: soundpad.export_pad_audio(int(pad), pad_export_dir()))


def _open_folder_focused(folder: str) -> None:
    """Open a folder in Explorer AND PUT THE USER IN IT. Raises if it cannot be opened at all.

    `os.startfile` alone opens the window BEHIND the browser: measured, with no window already at
    that path, the foreground two seconds after the call is still the browser. A window you were not
    moved to is, to a screen-reader user, indistinguishable from nothing having happened.

    Windows will not let a background process take the foreground on its own, so this borrows the
    input state of the window that HAS it (AttachThreadInput), which is the documented way and the
    one that survives a browser being frontmost. `AllowSetForegroundWindow` is called first because
    when it does work it is cheaper and needs no window handle.

    FAILING TO FOCUS IS NOT FAILING TO OPEN. The folder is what was asked for; the focus is the
    courtesy. So everything after `startfile` is best-effort and swallows its own errors - reporting
    "could not be opened" for a window that opened perfectly well would be worse than the silence
    this exists to fix.
    """
    import ctypes
    from ctypes import wintypes

    u32 = ctypes.windll.user32
    try:
        u32.AllowSetForegroundWindow(-1)          # ASFW_ANY, before the window exists
    except Exception:
        pass
    os.startfile(folder)                          # noqa: S606 - app-owned folder, never user input

    want = os.path.basename(os.path.normpath(folder))
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _visit(hwnd, _lparam):
        cls = ctypes.create_unicode_buffer(64)
        u32.GetClassNameW(hwnd, cls, 64)
        # Explorer's folder windows only. Anything else with this title is not ours to raise.
        if cls.value not in ("CabinetWClass", "ExploreWClass"):
            return True
        n = u32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        u32.GetWindowTextW(hwnd, buf, n + 1)
        # PREFIX, NOT EQUALITY. Explorer titles the window with the folder name and then appends
        # " - File Explorer" a moment later, so which of the two you see depends on how fast the poll
        # got there. Matching only the bare name worked on the machine it was written on and would
        # have failed silently on a slower one.
        title = buf.value
        if title == want or title.startswith(want + " - "):
            found.append(hwnd)
            return False
        return True

    # Explorer takes a moment to put the window up; poll rather than guess a sleep.
    for _ in range(20):
        found.clear()
        try:
            u32.EnumWindows(_visit, 0)
        except Exception:
            break
        if found:
            break
        time.sleep(0.1)
    if not found:
        return
    hwnd = found[0]
    try:
        fg = u32.GetForegroundWindow()
        ours = u32.GetWindowThreadProcessId(hwnd, None)
        theirs = u32.GetWindowThreadProcessId(fg, None)
        u32.ShowWindow(hwnd, 9)                   # SW_RESTORE, in case it opened minimised
        if ours != theirs:
            u32.AttachThreadInput(theirs, ours, True)
            u32.SetForegroundWindow(hwnd)
            u32.AttachThreadInput(theirs, ours, False)
        else:
            u32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def action_open_pad_exports() -> dict:
    """Open the exports folder in Explorer, so the file can actually be used once it is copied."""
    folder = pad_export_dir()
    try:
        _open_folder_focused(folder)
    except Exception as exc:
        return {"ok": False, "message": f"The exports folder is {folder}, but it could not be "
                                        f"opened: {exc}"}
    return {"ok": True, "message": f"Opened {folder}."}


def action_tidy_pad_audio() -> dict:
    """Manual sweep of pad audio nothing plays. The destructive pad operations now do this for
    themselves; this covers what earlier versions left behind, and clearing individual pads (a clear
    is HID-only by design - it mounts nothing and writes no files, which is why it raises no drive
    toast - so it cannot tidy up after itself without giving that up)."""
    try:
        with _hid_lock:
            return soundpad.tidy_unused_pad_audio()
    except Exception as exc:
        return {"ok": False, "message": f"Could not tidy up the pad audio: {exc}"}


def action_assign_fx(grid: int, effect: str) -> dict:
    """Put a factory voice effect on the pad at grid position `grid`. Gated on a clean pool."""
    return _pad_write(grid, "Could not assign the voice effect",
                      lambda: soundpad.assign_fx_to_grid(grid, effect),
                      plain_value_error=True)


def action_create_special_pad(grid: int, type_str: str) -> dict:
    """Create a Mixer-action pad ("mixer:0".."mixer:4") or a MIDI Trigger pad ("trigger") on `grid`.
    Both are pool-safe (no region-B writes). Clears the slot first if occupied (soundpad does this)."""
    def _make():
        if type_str.startswith("mixer:"):
            return soundpad.create_mixer(grid, int(type_str.split(":", 1)[1]))
        if type_str == "trigger":
            return soundpad.create_trigger(grid)
        return {"ok": False, "message": f"Unknown pad type {type_str!r}."}

    return _pad_write(grid, "Could not create the pad", _make, plain_value_error=True)


def action_save_fx_preset(name: str, stack: dict, colour: int) -> dict:
    """Save a user-built custom voice effect (no device access; just persists to the user file).
    soundpad.validate_fx_stack sanitises engines/params and clamps every value to range."""
    try:
        saved = soundpad.save_user_fx_preset(name, stack, colour=colour)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    except Exception as exc:
        return {"ok": False, "message": f"Could not save the effect: {exc}"}
    return {"ok": True,
            "message": f"Saved '{saved}'. It is now in the Assign a voice effect list.",
            "recipes": list(soundpad.all_recipes().keys()),
            "custom_fx": list(soundpad.load_user_fx_presets().keys())}


def action_delete_fx_preset(name: str) -> dict:
    """Delete a user-built custom voice effect. Built-in effects cannot be deleted (not in the
    user file), so this only ever removes the user's own presets."""
    try:
        existed = soundpad.delete_user_fx_preset(name)
    except Exception as exc:
        return {"ok": False, "message": f"Could not delete the effect: {exc}"}
    if not existed:
        return {"ok": False, "message": f"'{name}' is not one of your custom effects."}
    return {"ok": True,
            "message": f"Deleted '{name}'.",
            "recipes": list(soundpad.all_recipes().keys()),
            "custom_fx": list(soundpad.load_user_fx_presets().keys())}


def action_preview_start(stack: dict, colour: int, mic: int) -> dict:
    """Create a scratch preview pad carrying the in-progress effect and engage it on the live
    mic. Returns the grid used, or no_pad:True if every pad is full."""
    global _preview_grid
    try:
        with _hid_lock:
            res = soundpad.preview_start(stack or {}, colour=colour, mic=mic)
        if res.get("ok"):
            _preview_grid = res.get("grid")
        return res
    except Exception as exc:
        return {"ok": False, "message": f"Could not start live preview: {exc}"}


def action_preview_update(grid: int, param: str, value) -> dict:
    """Stream one control change to the live preview pad (an effect param, on-flag, or mic input).
    The value is validated + clamped before any write."""
    try:
        p, v, kind = soundpad.sanitise_preview_edit(param, value)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    try:
        with _hid_lock:
            if p == "padEffectInput":
                soundpad._preview_pad_field(grid - 1, p, v, kind)
                return {"ok": True}
            return soundpad.preview_update(grid, p, v, kind)
    except Exception as exc:
        return {"ok": False, "message": f"Could not update live preview: {exc}"}


def action_preview_stop(grid: int) -> dict:
    """Clear the preview scratch pad, removing the effect from the live mic."""
    global _preview_grid
    try:
        with _hid_lock:
            res = soundpad.preview_stop(grid)
        _preview_grid = None
        return res
    except Exception as exc:
        return {"ok": False, "message": f"Could not stop live preview: {exc}"}


def _cleanup_preview_on_exit() -> None:
    """If the server stops (watchdog/quit/Ctrl-C) while a live preview is engaged, clear the
    scratch pad so no effect is left on the mic. Best effort; failures are ignored."""
    if _preview_grid is None:
        return
    try:
        with _hid_lock:
            soundpad.preview_stop(_preview_grid)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# GLOBAL KEYBOARD SHORTCUTS. The Windows plumbing is in hotkeys.py, which knows nothing about this
# desk; what follows is the list of what a shortcut can DO, and it is the only place that list
# lives. Each action returns the sentence to speak, because when a hotkey fires the app's window is
# not in front of you: the spoken reply IS the feedback.
#
# WHAT BELONGS HERE. A shortcut is for something you want without stopping what you are doing, that
# takes ONE press and needs no decision: an on/off, a transport press, a spoken read-out. Anything
# that needs a choice, a number or a confirmation belongs on a page, where it can be read properly
# first. Nothing destructive gets one (no Clear all pads, no restore, no factory reset): a global
# hotkey is by definition pressable by accident, from any application, with no dialog in the way.
# ----------------------------------------------------------------------------

def _hk_announcer() -> str:
    """Toggle the spoken announcer, and the case the whole shortcut layer exists for: the announcer
    talking over a guest is something you fix NOW, not after finding the window and the tab.

    TURNING IT OFF IS SILENT, ON PURPOSE. The case this shortcut exists
    for is realising mid-take that announcements are going into the mix, and "announcements off"
    spoken into that mix is the exact thing the key was pressed to stop. Turning them back ON still
    speaks, so the pair always leaves you knowing where you are. The silence is safe here in a way
    it would not be elsewhere: what the shortcut did is immediately audible as the announcer going
    quiet. (Speech would otherwise work either way - _ann_say goes to the speech worker directly and
    never asks whether the announcer is on.) Every OTHER shortcut can be silenced too, per shortcut,
    on the Keyboard shortcuts tab.

    NOT persisted, deliberately: this is the same toggle as the Announcements tab's switch, which
    does not persist either, and one control having two lifetimes depending on how you reached it is
    a bug waiting to be reported. The tray menu's copy is the one that saves the preference."""
    on = not _announce_on.is_set()
    res = action_set_announce(on)
    return res.get("message", "") if on else ""


def _hk_record() -> str:
    """Start or stop the recording. Reads the live state first (free: the watcher's last push, no
    device read) so one key both starts and stops, and a PAUSED take is stopped rather than
    restarted. action_record reads the state back before it reports, so what this speaks is what the
    unit is really doing, not what we asked for."""
    live = read_recorder_live()
    state = live.get("state")
    going = state in (recorder.RECORDING, recorder.PAUSED)
    res = action_record("stop" if going else "start")
    return res.get("message", "")


# The mics THIS shortcut muted, held so the second press can undo exactly that set - see _hk_mics.
_hk_muted_mics = []


def _hk_mics() -> str:
    """The cough key: mute every mic that is open, and on the next press open again ONLY those.

    IT UNDOES ITSELF RATHER THAN UNMUTING EVERYTHING, and the difference is not cosmetic. Tested on
    hardware with Mic 1 open and Mics 2-4 muted, an "if any is open, mute all, else unmute all"
    toggle leaves all four OPEN after two presses, having silently un-muted three channels somebody
    deliberately shut. A cough key that puts a guest's mic live is worse than no cough key.
    Remembering what it muted makes the pair of presses a true round trip whatever mixture the desk
    is in.

    THE STATE COMES FROM THE ANNOUNCER'S LIVE CACHE, seeded from one dump when the announcer starts
    and kept current by the desk's own pushes (_ann_seed_layout, _ann_publish_mute). That matters:
    reading the mutes off the desk costs about 1.5 seconds, and a cough key that takes 1.5 seconds
    is not a cough key. With the announcer off there is no cache, so it falls back to the dump: still
    correct, just slower, and only on the FIRST press of a pair.

    With nothing latched and every mic already muted it says so and writes nothing, rather than
    treating "all muted" as an invitation to open them."""
    global _hk_muted_mics
    mics = [c for c in channels.active_ui_channels() if c.get("kind") == "mic"]
    if not mics:
        return "This unit has no mic channels."
    if _hk_muted_mics:
        names, _hk_muted_mics = _hk_muted_mics, []
        opened = []
        for name in names:
            res = action_set_channel_mute(name, False)
            if not res.get("ok"):
                return res.get("message", "The mics could not be opened.")
            opened.append(name)
        return f"{len(opened)} mic{'s' if len(opened) != 1 else ''} open again."
    st = _ann_live_state
    muted_now = dict((st or {}).get("muted") or {})
    if not muted_now:
        try:
            with _hid_lock:
                blob = rcp_read.read_model_stable()
            muted_now = {i: bool(v) for i, v
                         in enumerate(v for v in rcp_read.values(blob, "channelOutputMute"))}
        except Exception as exc:
            return f"Could not read the mics: {exc}"
    open_mics = [c for c in mics
                 if not muted_now.get(channels.channel_strip_index(c["name"]), False)]
    if not open_mics:
        return "Every mic is already muted."
    done = []
    for c in open_mics:
        res = action_set_channel_mute(c["name"], True)
        if not res.get("ok"):
            _hk_muted_mics = done          # keep what DID land, so the undo is still exact
            return res.get("message", "The mics could not be muted.")
        done.append(c["name"])
    _hk_muted_mics = done
    return f"{len(done)} mic{'s' if len(done) != 1 else ''} muted."


def _hk_state() -> str:
    """Speak the short version of the Device page's current-state report, from anywhere.

    Costs a model read, so it speaks "Reading the unit" first: a key that appears to do nothing for
    a second and a half is a key you press again. By exception like the page's own report - what is
    muted, what is soloed, whether it is recording, what pads are latched - so "nothing to report"
    is itself the answer and takes one sentence to hear."""
    _speak_if_announcing("Reading the unit.")   # silent whenever the announcer is
    ov = device_overview()
    if not ov.get("ok"):
        return ov.get("message", "The unit could not be read.")
    bits = []
    rec = ov.get("recording")
    if rec:
        bits.append(f"{rec.get('label', 'Recording')}.")
    muted = [m["label"] for m in ov.get("muted") or []]
    if muted:
        bits.append(f"Muted: {_join_and(muted)}.")
    soloed = [s["label"] for s in ov.get("soloed") or []]
    if soloed:
        bits.append(f"Soloed: {_join_and(soloed)}.")
    latched = [p["name"] for p in ov.get("pads_engaged") or []]
    if latched:
        bits.append(f"Still switched on: {_join_and(latched)}.")
    for vol in ov.get("storage_low") or []:
        bits.append(f"{vol['label']} is nearly full, {vol['free_pct']} percent free.")
    # ROUTING, by exception like everything else here, and the reason it is worth the words: an
    # output left on a scheme you have forgotten about is exactly the fault this read-out is for:
    # it sounds like a dead channel and there is nothing to see.
    for note in ((ov.get("routing") or {}).get("notes") or []):
        bits.append(str(note))
    mon = ov.get("monitor_percent")
    if isinstance(mon, int):
        bits.append(f"Monitor {mon} percent.")
    if not bits:
        return "Nothing to report: nothing muted or soloed, and not recording."
    return " ".join(bits)


# WHERE EACH PAD SITS IN THE POOL, cached, because firing a pad has to be INSTANT.
#
# A pad is engaged by writing `padActive` at its STREAM POSITION, and position is only knowable from
# a model dump - about 1.5 seconds, which is not a cough-key or an airhorn. So the map is cached, and
# kept honest three ways: it is built on first use, REFRESHED IN THE BACKGROUND after every press
# (so the next press is both instant and current), and the same background pass CHECKS that the pad
# now engaged is the one asked for. If a pad was created or cleared since the last refresh, the
# positions can have shifted; the check catches that, switches the wrong pad off again and says so,
# rather than leaving a stranger's voice effect on a live mic. `padIdx` is the stable identity (a
# pad's place in the grid), and it is what a shortcut stores; position is the moving part.
_hk_pad_map = {}                # padIdx -> (position, name, pad type, play mode)
_hk_pad_lock = threading.Lock()
_hk_pads_on = set()             # padIdx of pads THIS app engaged, so a second press releases them
_hk_pads_held = set()           # padIdx of HOLD-mode sound pads whose MIDI note we are still holding

# WHICH BANK THE DESK IS ON, cached for the same reason the pad map is: MIDI reaches only the
# current bank, so firing a pad in another one needs this before it can do anything, and a model
# dump in front of a keypress is not an airhorn. Kept honest from three directions - the background
# refresh after every press reads it off the same dump, the announcer notes the desk's own pushes,
# and our own bank writes note themselves. None means we have genuinely never seen one, which a
# caller must not silently read as bank 1: that fires the wrong pad.
_hk_bank = None


def _hk_note_bank(bank) -> None:
    """Record the bank the desk is on. Safe to call with anything; non-banks are ignored."""
    global _hk_bank
    if isinstance(bank, int) and 0 <= bank < devices.get_active_device().pad.system_bank:
        _hk_bank = bank


def _hk_current_bank():
    """The bank the desk is on (0-based), or None if we cannot find out."""
    if _hk_bank is None:
        try:
            _hk_refresh_pads()
        except Exception:
            return None
    return _hk_bank


# VOICE-FX PADS WITH NOTHING BEHIND THEM, by padIdx. A voice-FX pad's sound lives in a region-B
# effect object, and the grid record does NOT: a pad can carry the right name, type and colour with
# no object bound to it, which is what a factory reset leaves behind (soundpad.heal_fx_pool). Such a
# pad takes `padActive` and processes nothing, so a shortcut that writes the flag and reports
# success is telling the operator their voice is being disguised when it is not.
#
# READ OFF THE REFRESH DUMP, never asked for separately: `pool_health` costs a full model read on
# its own, and a press cannot pay 1.5 s to find out whether it is allowed.
_hk_fx_missing = frozenset()

# STREAM POSITIONS OF THE MIXER PADS (padType 3), read off the same refresh dump. A mixer pad's whole
# job is to move channel state - Back Channel mutes and cues its members, Censor mutes the mics - so
# its `padActive` is the announcer's signal that the mute and cue pushes about to arrive are the
# pad doing what it was pressed to do, not a change worth speaking. See _ANN_MIXER_QUIET_S.
_hk_mixer_pos = frozenset()


def _hk_refresh_pads() -> dict:
    """Re-read the pad pool and replace the cache. Returns the new map.

    One dump answers four questions - where each pad sits, what its play mode is, which bank the
    desk is on, and which voice-FX pads have lost their effect object - so the press that follows
    needs no read of its own."""
    global _hk_pad_map, _hk_pads_at, _hk_fx_missing, _hk_mixer_pos
    with _hid_lock:
        blob = rcp_read.read_model_stable()
    modes = soundpad.pad_play_modes(blob)
    placed = list(soundpad.pad_positions(blob))
    fresh = {pi: (pos, nm, pt, modes.get(pi)) for pos, pi, pt, nm in placed}
    _hk_mixer_pos = frozenset(pos for pos, _pi, pt, _nm in placed if pt == 3)
    try:
        _hk_fx_missing = frozenset(soundpad.pool_health(blob).get("missing") or ())
    except Exception:
        _hk_fx_missing = frozenset()    # a health read that fails must never break a press
    with _hk_pad_lock:
        _hk_pad_map = fresh
    _hk_pads_at = time.time()
    _hk_note_bank(soundpad.current_bank(blob))
    return fresh


# What to say about a voice-FX pad whose effect object is gone. One wording, because the shortcut
# that refuses and the read-out that describes it must not disagree about what is wrong or what to
# do about it.
_HK_FX_EMPTY = ("{name} has no effect behind it, so it would change nothing. Use Restore the "
                "factory voice effects on the SMART pads page to rebuild it.")


def _hk_pad_entry(pad_idx: int):
    """The cached entry for a pad, reading the desk again only when we must.

    TWO KINDS OF OUT OF DATE, and they deserve different answers. A pad EDIT makes the map wrong
    NOW - a cleared pad shifts every later position - so `_hk_pads_dirty` forces a read before this
    press, and that cost is worth paying once. Merely OLD is different: nothing is known to have
    changed, so the read happens in the background and this press does not wait for it."""
    if _hk_pads_dirty.is_set():
        _hk_pads_dirty.clear()
        try:
            return _hk_refresh_pads().get(pad_idx)
        except Exception:
            pass                # fall back to what we have; a stale name beats a dead shortcut
    with _hk_pad_lock:
        found = _hk_pad_map.get(pad_idx)
    if found is None:
        found = _hk_refresh_pads().get(pad_idx)
    return found


# HOW STALE THE PAD MAP MAY GET BEFORE A PRESS PAYS FOR A NEW ONE.
#
# NEVER REFRESH AFTER EVERY PRESS: that is what makes pad firing flaky. A refresh is a FULL MODEL
# DUMP, one and a half to three seconds holding `_hid_lock`, so a row of number keys pressed at
# performance speed queues a dump per press - and `HotkeyManager._fire` DROPS a re-press while the
# last one is still running, so presses go missing rather than merely arriving late.
#
# It is also unnecessary on the MIDI path. A dump is only needed to know where a pad SITS, and MIDI
# addresses a pad by bank and slot, which does not move. What the map still supplies - the name and
# the play mode - changes only when somebody edits a pad, so a TTL is the honest shape. The HID
# toggle path keeps its own verification, which is a different job: it catches a stale POSITION
# firing a stranger's voice effect onto a live mic.
_HK_PAD_CACHE_S = 90
_hk_pads_at = 0.0
_hk_refreshing = threading.Event()


_hk_pads_dirty = threading.Event()


def _hk_pads_stale() -> None:
    """Mark the pad map KNOWN WRONG, so the next press reads it again before doing anything. Called
    by anything that edits a pad: the TTL alone would leave a renamed or cleared pad wrong for up to
    a minute and a half, and a cleared one moves every later pad's stream position."""
    global _hk_pads_at
    _hk_pads_at = 0.0
    _hk_pads_dirty.set()


def _hk_maybe_refresh_pads() -> None:
    """Read the pad pool again in the background IF it is stale, and never twice at once."""
    if time.time() - _hk_pads_at < _HK_PAD_CACHE_S or _hk_refreshing.is_set():
        return

    def run():
        try:
            _hk_refresh_pads()
        except Exception:
            pass                # a refresh that cannot run must not break the next press
        finally:
            _hk_refreshing.clear()

    _hk_refreshing.set()
    threading.Thread(target=run, daemon=True).start()


def _hk_pad_name(position: int):
    """The name of the pad at a stream POSITION, or None. The pushed stream addresses a pad by
    position, so this is what turns a `padActive` / `padProgress` push back into a name."""
    with _hk_pad_lock:
        for _pi, entry in _hk_pad_map.items():
            if entry[0] == position:
                return entry[1]
    return None


# WHETHER WE HAVE TURNED MIDI ON YET THIS SESSION. `systemMidiControl` is the gate on the whole
# MIDI port, it is NOT on by default, and a profile restore can switch it off again - so anything
# that fires a sound pad has to arm it at the point of use rather than once at startup.
#
# UNCONDITIONALLY, AND NEVER READ-THEN-WRITE. Reading first costs a model dump (about 1.5 s) in
# front of a keypress, and `_session_state["midi"]` does not follow a change made on the desk
# itself, so the read can be stale in the one direction that matters. The write is idempotent and
# measured at 0.004 s, so there is nothing to save. Said out loud ONCE, because the user did not ask
# for it and would otherwise have a setting changed silently.
_hk_midi_armed = False


def _hk_rearm_midi() -> None:
    """Forget that MIDI was armed, so the next pad press turns it on again. Called when a profile is
    applied: a profile carries `systemMidiControl`, so restoring one taken while MIDI was off
    silently kills every sound-pad shortcut."""
    global _hk_midi_armed
    _hk_midi_armed = False


def _hk_arm_midi() -> None:
    global _hk_midi_armed
    if _hk_midi_armed:
        return
    rcp_transfer.set_midi_control(True)
    _hk_midi_armed = True
    _ann_say("MIDI control switched on, so the desk will play sound pads.")


# WHAT IS PLAYING, tracked SILENTLY off the pushed stream and read out only when asked.
#
# ON DEMAND, NOT ON PRESS, which is what keeps it cheap as well as quiet: a playing pad pushes
# `padProgress` about twenty times a second, and speaking that would mean throttling twenty
# announcements a second and talking over the pad. As STATE that nobody speaks, the rate costs a
# dict update.
#
# ADDRESSED BY STREAM POSITION, path [666, position] - the same address our own `padActive` write
# uses. Measured on a pad chosen so the two candidate readings disagree: Pitch Up is padIdx 12 at
# position 19 and is pushed at 19, not at 8 + padIdx. On a factory Pro II every user pad happens to
# sit at 8 + padIdx, so a bank 1 pad proves nothing here.
#
# ONLY SOUND PADS PUBLISH PROGRESS. A voice-FX pad pushes `padActive` and no progress at all
# (measured), so "how far through" is a sound-pad answer and nothing else.
_hk_play_lock = threading.Lock()
_hk_playing = {}                # stream position -> {"first", "first_at", "last", "last_at"}
# A playing pad pushes about 20 times a second, so a second of silence means it has stopped without
# us seeing the `padActive` that said so - which happens whenever the announcer starts mid-sound.
_HK_PLAY_STALE_S = 1.0


def _hk_note_pad_progress(position: int, progress: float) -> None:
    """One `padProgress` push. Keeps the FIRST sample as well as the latest, because time remaining
    is worked out from the RATE between two samples: progress is a 0-1 fraction and the desk
    publishes no duration anywhere, so a single sample says how far through but never how long."""
    now = time.time()
    with _hk_play_lock:
        seen = _hk_playing.get(position)
        if seen is None or progress < seen["last"] or now - seen["last_at"] > _HK_PLAY_STALE_S:
            # A new sound, or the same pad restarted from the beginning (One Shot), or one we lost
            # track of. Either way the old rate is meaningless, so start again from here.
            _hk_playing[position] = {"first": progress, "first_at": now,
                                     "last": progress, "last_at": now}
            return
        seen["last"], seen["last_at"] = progress, now


def _hk_note_pad_active(position: int, on: bool) -> None:
    """One `padActive` push. Off means the sound has finished or been stopped."""
    with _hk_play_lock:
        if on:
            _hk_playing.setdefault(position, {"first": 0.0, "first_at": time.time(),
                                              "last": 0.0, "last_at": time.time()})
        else:
            _hk_playing.pop(position, None)


def _hk_play_snapshot() -> dict:
    """position -> (progress, seconds remaining or None), for pads pushing progress RIGHT NOW."""
    now, out = time.time(), {}
    with _hk_play_lock:
        for position, seen in list(_hk_playing.items()):
            if now - seen["last_at"] > _HK_PLAY_STALE_S:
                _hk_playing.pop(position, None)
                continue
            span, moved = seen["last_at"] - seen["first_at"], seen["last"] - seen["first"]
            left = None
            if span > 0.2 and moved > 0.0:
                left = max(0.0, (1.0 - seen["last"]) * span / moved)
            out[position] = (seen["last"], left)
    return out


def _hk_pad_verify(pad_idx: int, want_on: bool) -> None:
    """After a press, re-read and confirm the RIGHT pad reacted. Runs on its own thread."""
    try:
        fresh = _hk_refresh_pads()
        latched = {pi for _pos, pi, _nm in soundpad.latched_pads()}
        if want_on and pad_idx not in latched and latched:
            wrong = sorted(latched)[0]
            entry = fresh.get(wrong)
            if entry:
                soundpad.unlatch_pad(entry[0])
                _ann_say(f"That fired the wrong pad, {entry[1]}, and it has been switched off "
                         f"again. The pad list was out of date and has been read again.")
    except Exception:
        pass                    # a verification that cannot run must not break the press


# TWO TRANSPORTS, AND THEY COVER DIFFERENT PAD TYPES. Dispatch is PER PAD and never per bank,
# because the kinds are mixed inside a bank.
#
#   SOUND pad  -> MIDI, CC 35 on the pad's own channel. It is a PLAY, and what a second press does
#                 is the pad's own `padPlayMode`. Over HID a sound pad sets `padActive` and plays
#                 nothing: an ear-proven negative, so do not route sound pads down the HID path.
#   VOICE-FX
#   and MIXER  -> HID, `padActive` at the pad's stream position. It is a TOGGLE and we hold the
#                 state in `_hk_pads_on`, because for those two the flag IS the state and the desk
#                 pushes nothing back for our own write.
#
# So one key says "Megaphone on" for one pad and "Airhorn playing" for the next. Do not collapse
# them into one word: "on" for a sound that has already finished would be a lie, and "playing" for a
# voice effect that is sitting on a live mic hides the thing you most need to know.
def _hk_fire_pad(pad_idx: int) -> str:
    """Fire ONE pad by its padIdx, choosing the transport from its type. The spoken result says
    which of the two things happened."""
    entry = _hk_pad_entry(pad_idx)
    if entry is None:
        return "That pad is empty now. Choose another on the Keyboard shortcuts tab."
    position, name, pad_type, play_mode = entry
    if pad_type == soundpad.PAD_TYPE_SOUND:
        return _hk_pad_play(pad_idx, position, name, play_mode)
    return _hk_pad_toggle(pad_idx, position, name)


def _hk_pad_play(pad_idx: int, position: int, name: str, play_mode) -> str:
    """Play a SOUND pad over MIDI, selecting its bank first if the desk is on another one.

    A STALE PAD MAP CANNOT FIRE THE WRONG SOUND HERE, which is why there is no verification pass
    like the HID toggle has. MIDI addresses a pad by bank and slot - that is padIdx, the pad's fixed
    place in the grid - so it does not care where the record sits in the stream. Position is only
    used to name what is playing."""
    dev = devices.get_active_device()
    per_bank = dev.pad.pads_per_bank
    bank, slot = pad_idx // per_bank, pad_idx % per_bank + 1
    try:
        _hk_arm_midi()
        if _hk_current_bank() != bank:
            _hk_release_held_pads()
            soundpad.select_bank(bank)
            _hk_note_bank(bank)
            time.sleep(_HK_BANK_SETTLE_S)
        # HOLD MODE PLAYS ONLY WHILE THE PAD IS HELD, and a hotkey has no release event to hang a
        # release on: Windows delivers WM_HOTKEY on the press and says nothing when the key comes
        # up. A tap would therefore give a blip. So the two presses of the shortcut become the two
        # ends of one hold, which is the only shape that makes a Hold pad usable from a keyboard.
        if play_mode == soundpad.PLAY_MODE_HOLD:
            if pad_idx in _hk_pads_held:
                midi_out.release_pad(slot)
                _hk_pads_held.discard(pad_idx)
                return f"{name} released."
            midi_out.press_pad(slot)
            _hk_pads_held.add(pad_idx)
            return f"{name} playing. It is set to Hold, so press again to stop it."
        was_playing = position in _hk_play_snapshot()
        midi_out.play_pad(slot)
    except Exception as exc:
        _hk_pads_held.discard(pad_idx)
        return f"{name} would not play: {exc}"
    _hk_maybe_refresh_pads()
    if was_playing and play_mode == soundpad.PLAY_MODE_TOGGLE:
        return f"{name} stopped."
    if was_playing and play_mode == soundpad.PLAY_MODE_ONE_SHOT:
        return f"{name} playing from the start."
    return f"{name} playing."


def _hk_pad_toggle(pad_idx: int, position: int, name: str) -> str:
    """Switch a VOICE-FX or MIXER pad on, and off again on the next press.

    Writing `padActive` at a pad's position ENGAGES it, and that is the same mechanism a latched
    voice-FX pad uses, the one that can put a robot voice on a live mic. Proven on hardware: a
    voice-FX pad engages and releases, and a MIXER pad performs its whole action (Back Channel moves
    `channelCueEnable`, `channelTalkbackEnable`, `mixMute` and `channelOutputMute` together).

    INJECTING THE BUTTON DOES NOT WORK, and that was the obvious route: `padButtonPressed` at the
    PADBUTTON node reads back exactly as written and moves NOTHING else in the whole 6000-property
    model, on either address encoding, for a sound pad or a mixer pad. See the padActive entry in
    PROJECT_GUIDE.md; do not spend another evening on it.

    A VOICE-FX PAD WITH NO EFFECT OBJECT IS REFUSED RATHER THAN SWITCHED ON. `padActive` latches on
    one of those perfectly happily and processes nothing, so writing it would leave the desk holding
    a flag the operator has been told is doing something. Only the ON half is guarded: releasing a
    pad that is somehow already latched must always be possible."""
    on = pad_idx not in _hk_pads_on
    if on and pad_idx in _hk_fx_missing:
        return _HK_FX_EMPTY.format(name=name)
    try:
        if on:
            soundpad.engage_pad(position)
            _hk_pads_on.add(pad_idx)
        else:
            soundpad.unlatch_pad(position)
            _hk_pads_on.discard(pad_idx)
    except Exception as exc:
        _hk_pads_on.discard(pad_idx)
        return f"{name} would not respond: {exc}"
    threading.Thread(target=_hk_pad_verify, args=(pad_idx, on), daemon=True).start()
    return f"{name} on." if on else f"{name} off."


def _hk_release_held_pads() -> None:
    """Let go of any HOLD-mode pad we are still holding down.

    THE SAME CLASS OF FAULT AS A LATCHED VOICE-FX PAD, which is why it is called from the shutdown
    path AND from every bank change: a held note nobody releases leaves the desk playing with
    nothing on screen to explain it, and a MIDI channel means "pad N of the current bank", so
    moving the bank strands the hold on a pad we can no longer name. Never raises."""
    dev = devices.get_active_device()
    for pad_idx in list(_hk_pads_held):
        try:
            midi_out.release_pad(pad_idx % dev.pad.pads_per_bank + 1)
        except Exception:
            pass
    _hk_pads_held.clear()


def _hk_release_held() -> None:
    """The shutdown half: release the notes, then drop the MIDI handle."""
    _hk_release_held_pads()
    try:
        midi_out.close()
    except Exception:
        pass


def _hk_pad(target: str) -> str:
    """Fire the pad this shortcut names. The target is a padIdx: the pad's FIXED place in the grid,
    so the shortcut keeps pointing at the same sound however the pool is rearranged."""
    target = (target or "").strip()
    if not target:
        return "No pad is assigned to this shortcut yet. Choose one on the Keyboard shortcuts tab."
    try:
        pad_idx = int(target)
    except ValueError:
        return f"{target} is not a pad."
    return _hk_fire_pad(pad_idx)


# How long to let a bank change land before sending the MIDI press. The desk publishes no
# acknowledgement, and a press that arrives first plays the pad in the OLD bank - which is a
# stranger's sound, not a missing one, so this is a correctness dwell rather than a politeness one.
_HK_BANK_SETTLE_S = 0.25


def _hk_pad_slot(target: str) -> str:
    """Fire pad N of whatever bank the desk is on. The number-row half of the pad scheme.

    NOT the same command as `pad.fire`, and the difference is the whole point: `pad.fire` is pinned
    to one sound for ever, while this one moves with the desk, so eight keys reach every pad on it.
    N IS PER-DEVICE - eight on a Pro II, six on a Duo - and it comes from the device table, so a Duo
    is never offered a seventh."""
    try:
        slot = int((target or "").strip())
    except ValueError:
        return f"{target} is not a pad number."
    per_bank = devices.get_active_device().pad.pads_per_bank
    if not 1 <= slot <= per_bank:
        return f"This desk has {per_bank} pads in a bank, so there is no pad {slot}."
    bank = _hk_current_bank()
    if bank is None:
        return "I cannot tell which pad bank the desk is on. Is it plugged in?"
    return _hk_fire_pad(bank * per_bank + slot - 1)


def _hk_bank_step(target: str) -> str:
    """Move down or up a pad bank, and say where you have landed.

    CLAMPS AT THE ENDS RATHER THAN WRAPPING. Wrapping from the last bank to the first is silent and
    disorienting when you cannot see the desk: you press once more expecting nothing and every
    number key is suddenly a different sound."""
    step = -1 if str(target).strip() == "down" else 1
    banks = devices.get_active_device().pad.system_bank
    bank = _hk_current_bank()
    if bank is None:
        return "I cannot tell which pad bank the desk is on. Is it plugged in?"
    want = max(0, min(banks - 1, bank + step))
    if want == bank:
        return (f"Bank {bank + 1} already, and that is the "
                f"{'first' if step < 0 else 'last'} bank.")
    _hk_release_held_pads()
    try:
        soundpad.select_bank(want)
    except Exception as exc:
        return f"The bank would not change: {exc}"
    _hk_note_bank(want)
    _hk_maybe_refresh_pads()
    # The system bank is the desk's own function pads, not sounds anyone assigned, so say so: the
    # number keys still work there and would otherwise fire "Scene A" with no warning.
    if want == banks - 1:
        return f"Bank {want + 1}, the desk's own function pads."
    return f"Bank {want + 1}."


def _hk_playing_now(_target: str = "") -> str:
    """Say what is playing, on demand.

    ON DEMAND AND NEVER ON PRESS, or it talks over the pad it is describing. Reads the state the
    announcer keeps silently, so twenty progress pushes a second cost nothing until somebody asks.

    FALLS BACK TO A MODEL READ WHEN THE ANNOUNCER IS OFF, because the push stream is the announcer's
    handle: without this the shortcut would answer "nothing" on a desk that is audibly playing, and
    a wrong answer is worse than a slow one. The read costs about 1.5 s and cannot give time
    remaining, which needs two samples of a moving progress figure.

    THE THREE SOURCES ARE MERGED BY STREAM POSITION, NOT CONCATENATED. They overlap: a voice effect
    this app engaged is in `_hk_pads_on` AND in the desk's own `padActive`, so appending both says
    "Voice Disguise is switched on. Voice Disguise is switched on." Position is the one identity all
    three share."""
    said = {}                       # stream position -> the sentence about it, first source wins
    for position, (progress, left) in _hk_play_snapshot().items():
        name = _hk_pad_name(position) or f"The pad at position {position}"
        said[position] = (f"{name}, {round(progress * 100)} percent through, "
                          f"{_hk_seconds_phrase(left)} left." if left is not None
                          else f"{name}, {round(progress * 100)} percent through.")
    if not said and _ann_live_state is None:
        said.update(_hk_playing_from_model())
    # HID-ENGAGED PADS ARE NOT IN THE STREAM AT ALL. The desk does not echo our own `padActive`
    # write (measured: engaging a voice-FX pad over HID pushes nothing whatever),
    # so a voice effect this app switched on is only knowable from what we remember doing.
    for pad_idx in sorted(_hk_pads_on):
        entry = _hk_pad_entry(pad_idx)
        if entry:
            # SAY WHEN THERE IS NOTHING BEHIND IT. `_hk_pads_on` records what this app WROTE, not
            # what the desk is doing, so a voice-FX pad that lost its effect object reads as
            # switched on here and is inaudible on the mic. That gap is the whole reason this line
            # cannot just report the flag.
            said.setdefault(entry[0], _HK_FX_EMPTY.format(name=entry[1])
                            if pad_idx in _hk_fx_missing else f"{entry[1]} is switched on.")
    for pad_idx in sorted(_hk_pads_held):
        entry = _hk_pad_entry(pad_idx)
        if entry:
            said[entry[0]] = f"{entry[1]} is being held down."
    bits = [said[p] for p in sorted(said)]
    return " ".join(bits) if bits else "Nothing is playing."


def _hk_playing_from_model() -> list:
    """What the DESK says is active, read fresh. The answer when the announcer is off.

    Returns {stream position: sentence}, keyed the same way as the tracked state so the caller can
    merge the two rather than say the same pad twice."""
    try:
        with _hid_lock:
            blob = rcp_read.read_model_stable()
        pads = soundpad.pad_nodes(blob)
    except Exception:
        return {-1: "I could not ask the desk what is playing."}
    out = {}
    for p in pads:
        if p.props.get("padActive") is not True:
            continue
        frac = p.props.get("padProgress")
        if p.type == soundpad.PAD_TYPE_SOUND and isinstance(frac, float):
            out[p.position] = f"{p.name}, {round(frac * 100)} percent through."
        else:
            out[p.position] = f"{p.name} is switched on."
    return out


def _hk_seconds_phrase(seconds: float) -> str:
    """A duration as it is spoken: "8 seconds", "1 minute 5 seconds"."""
    whole = int(round(seconds))
    if whole < 60:
        return f"{whole} second" + ("" if whole == 1 else "s")
    mins, secs = divmod(whole, 60)
    return (f"{mins} minute" + ("" if mins == 1 else "s")
            + (f" {secs} second" + ("" if secs == 1 else "s") if secs else ""))


def _hk_window() -> str:
    """Bring the app's own window up. The one shortcut that is not about the desk: it is how you get
    to everything that is deliberately NOT on a hotkey."""
    # SILENT ON SUCCESS, and that is right ONLY because the page speaks for itself: it moves focus
    # to the current section's heading when the window is raised, so a screen reader announces
    # where you have landed. Speaking here as well would talk over that in a second voice.
    # SILENT ON SUCCESS. Bringing a window to the front is a WINDOWS action, and Windows tells the
    # screen reader about it - so an announcement from here would be this app speaking over the
    # screen reader doing its own job, in a second voice, about something it has already said. The
    # app's own announcer is for the DESK. What this needs instead is a real activation, which is
    # `_focus_app_window`'s problem and is documented there.
    if _focus_app_window():
        return ""
    # A REFUSED RAISE MUST SAY SO. Before this it fell through to open_ui and said "Opening
    # AccessCaster" whether or not a window already existed, which is fine, but a raise that
    # failed with the window still sitting there behind everything said nothing at all.
    open_ui(f"http://localhost:{HTTP_PORT}/")
    return "Opening AccessCaster."


# WHICH PAD TYPES A SHORTCUT MAY BE POINTED AT lives in `commands.PAD_TYPES`, and since MIDI
# arrived it is all three that do something: sound, voice FX and mixer.

# WHAT A FRESH INSTALL GETS. (command, modifiers, key, speak). Only laid down when nothing has been
# saved, so deleting them stays deleted.
#
# THE NUMBER ROW IS THE PAD SCHEME, and it is one row you can find by touch: 1 to N play the pads of
# the current bank, and Dash and Equals - which sit at the END of that same row - step the bank down
# and up. Deliberately NO NUMPAD KEYS, which rules out the otherwise obvious numpad pair for bank
# down/up; brackets lose to Dash and Equals because they are not on the number row.
#
# N IS PER-DEVICE. Eight on a Pro II, six on a Duo, taken from the device table and never
# hard-coded, so a Duo owner is not given two keys that can only ever say "there is no pad 7".
def _pad_hotkey_scheme() -> list:
    """The pad half, on its own, because it can also be added to a list that already exists
    (`action_add_pad_shortcuts`) rather than only shipped to a fresh install."""
    per_bank = devices.get_active_device().pad.pads_per_bank
    rows = ([("pad.playing", hotkeys.DEFAULT_MODS, "P"),
             ("pad.bank:down", hotkeys.DEFAULT_MODS, hotkeys.KEY_DASH),
             ("pad.bank:up", hotkeys.DEFAULT_MODS, hotkeys.KEY_EQUALS)]
            + [(f"pad.slot:{n}", hotkeys.DEFAULT_MODS, str(n)) for n in range(1, per_bank + 1)])
    # Which of these speak is `commands.speaks_by_default`, so the shipped set and a hand-added
    # shortcut agree. The pad keys arrive silent: the pad IS the feedback.
    return [(cid, mods, key, commands.speaks_by_default(cid)) for cid, mods, key in rows]


def _hotkey_defaults() -> list:
    return [
        ("app.announcer", hotkeys.DEFAULT_MODS, "A", True),
        ("app.mics", hotkeys.DEFAULT_MODS, "M", True),
        ("rec.toggle", hotkeys.DEFAULT_MODS, "R", True),
        ("app.state", hotkeys.DEFAULT_MODS, "S", True),
        ("app.window", hotkeys.DEFAULT_MODS, "O", True),
        ("callme.answer", hotkeys.DEFAULT_MODS, "C", True),
        ("callme.end", hotkeys.DEFAULT_MODS, "D", True),
    ] + _pad_hotkey_scheme()

# THE FIRST FORMAT, kept only to carry an existing settings file across. Version one bound a fixed
# set of ten named actions; version two binds a user-built list to catalogue commands. Without this,
# anyone who had already set their keys would silently get the defaults back.
_HOTKEY_V1_COMMANDS = {"announcer": "app.announcer", "mics": "app.mics", "record": "rec.toggle",
                       "state": "app.state", "window": "app.window"}

_hotkeys = None


def _hotkeys_migrate(saved: dict) -> dict:
    """Turn a version-one `hotkeys` settings block into the current shape. Returns it unchanged if
    it is already current, or if there is nothing to carry."""
    if not isinstance(saved, dict) or "bindings" not in saved or "shortcuts" in saved:
        return saved if isinstance(saved, dict) else {}
    rows = []
    for name, b in (saved.get("bindings") or {}).items():
        if not isinstance(b, dict):
            continue
        command = _HOTKEY_V1_COMMANDS.get(name)
        if command is None and name.startswith("pad"):
            # A version-one pad SLOT carried its pad in `target`; an empty slot was a placeholder
            # and there is nothing to carry.
            target = str(b.get("target", "") or "")
            command = f"pad.fire:{target}" if target else None
        if command is None:
            continue
        rows.append({"id": f"sc{len(rows) + 1}", "command": command,
                     "mods": list(b.get("mods") or hotkeys.DEFAULT_MODS),
                     "key": str(b.get("key", "") or "A"),
                     "enabled": bool(b.get("enabled", True)),
                     "speak": bool(b.get("speak", True))})
    return {"enabled": bool(saved.get("enabled", True)), "shortcuts": rows}


def _speak_if_announcing(text: str) -> None:
    """Speak something the app decided to say, but ONLY when the control announcer is on.

    **THE CONTROL ANNOUNCER IS THE LEADER, AND EVERY OTHER VOICE IN THE APP FOLLOWS IT.** One switch
    means one answer to "will this machine talk", instead of a set of voices each with its own
    opinion.

    THE FAILURE IT PREVENTS: a recording switches the announcer off for the duration of the take so
    nothing of ours lands in it, but a hotkey reply sent straight to `_ann_say` never asks whether
    the announcer is on. So the one thing the suspension exists to prevent happens anyway,
    from the very keys you reach for mid-take. Gating on `_announce_on` fixes that case and the
    manual case together, because a record suspension IS the announcer being switched off.

    TWO CONSEQUENCES, both deliberate. Turning announcements back ON still speaks, because
    `action_set_announce(True)` has already run by the time the reply is handed over. And with
    announcements off, a shortcut that FAILS is silent too: the leader rule is the whole point, and a
    machine told to be quiet should not start talking because something went wrong."""
    if not _announce_on.is_set():
        return
    _ann_say(text)


def hotkey_manager():
    """The one manager, built on first use and given the saved shortcuts. Built lazily so importing
    server.py (our DevTools scripts all do) never registers a system-wide hotkey by surprise: only
    _bring_up_server asks for it, and only when the app is really running.

    The manager holds keys and knows nothing about the desk; `commands` holds what a shortcut can
    DO. The two meet here, and only here."""
    global _hotkeys
    if _hotkeys is None:
        mgr = hotkeys.HotkeyManager(speak=_speak_if_announcing,
                                    runner=commands.run,
                                    labeller=_hotkey_label)
        mgr.load(_hotkeys_migrate(load_settings().get("hotkeys") or {}),
                 defaults=_hotkey_defaults())
        _hotkeys = mgr
    return _hotkeys


# The catalogue is generated from live spec tables, and generating it can touch the desk (pads,
# presets), so labels are CACHED: `state()` asks for one per shortcut and the page reads state
# often. Cleared whenever the catalogue is re-read.
_hotkey_labels = {}


def _hotkey_label(command_id: str) -> str:
    label = _hotkey_labels.get(command_id)
    if label is None:
        try:
            label = commands.label_for(command_id)
        except Exception:
            label = command_id
        _hotkey_labels[command_id] = label
    return label


def hotkey_state() -> dict:
    return hotkey_manager().state()


def hotkey_commands() -> dict:
    """The whole catalogue of bindable commands, grouped by category for the Add dialog.

    THIS ONE READS THE DESK (pads and presets), which is why it is not part of `/api/hotkeys`: the
    shortcut list has to answer at once and with the unit unplugged, and only the Add dialog needs
    the catalogue. A read that fails still returns the device-free half rather than nothing."""
    try:
        items = commands.catalogue(include_device=True)
        complete = True
    except Exception:
        items = commands.catalogue(include_device=False)
        complete = False
    _hotkey_labels.clear()
    for c in items:
        _hotkey_labels[c["id"]] = c["label"]
    groups = []
    for cat in commands.CATEGORIES:
        rows = [c for c in items if c["category"] == cat]
        if rows:
            groups.append({"category": cat, "commands": rows})
    msg = f"{len(items)} things can be put on a shortcut."
    if not complete:
        msg += " The unit could not be read, so pads and presets are not listed."
    return {"ok": True, "groups": groups, "count": len(items), "complete": complete,
            "message": msg}


def app_settings_dir() -> str:
    """Where an app-settings export goes: Documents\\AccessCaster\\App settings.

    Documents rather than %LOCALAPPDATA%, for the reason pad_export_dir gives: an export you cannot
    find is not an export, and the whole point of this one is to survive a reinstall - which wipes
    the app's own data folder."""
    base = os.path.join(os.path.expanduser("~"), "Documents", "AccessCaster", "App settings")
    os.makedirs(base, exist_ok=True)
    return base


def _app_settings_available() -> dict:
    """What this computer actually HAS, for the machine-specific keys - see app_settings.merge_import.

    Best-effort: if a list cannot be read, the key is left out entirely, which merge_import treats
    as "cannot check, so apply it" rather than as "not available". Refusing to restore a setting
    because we failed to enumerate would be the wrong way round."""
    out = {}
    try:
        lists = _sapi_lists()
        # Only offer a list we actually got. On this machine over RDP, SAPI enumeration fails and
        # both come back empty (see ProjectNotes), and an empty list must NOT be read as "this
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


def action_export_app_settings() -> dict:
    """Write this app's own settings to a file: shortcuts, announcements, level warnings.

    NOT the desk - that is a profile. This is the half no profile has ever carried, so before it
    existed a reinstall lost every keyboard shortcut with no way back."""
    payload = app_settings.build_export(load_settings())
    # Writing an empty file would be a cheerful success for a no-op, and the user would carry it to
    # the new machine believing their shortcuts were in it.
    if app_settings.is_empty(payload):
        return {"ok": False,
                "message": "There is nothing to save yet: no keyboard shortcuts are set up and no "
                           "announcement or level-warning preference has been changed from its "
                           "default."}
    # Day, month NAME, year - never all digits. "02-09" is read aloud as the wrong date in half the
    # world, and this is a filename somebody will hear rather than see.
    now = time.localtime()
    day = now.tm_mday
    suffix = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    stamp = f"{day}{suffix} {time.strftime('%B %Y', now)}"
    name = f"AccessCaster settings {stamp}.json"
    path = os.path.join(app_settings_dir(), name)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    except OSError as exc:
        return {"ok": False, "message": f"Could not write the settings file: {exc}"}
    return {"ok": True, "path": path, "folder": app_settings_dir(),
            "message": (f"Saved {app_settings.describe_export(payload)} to {name}, in your "
                        f"Documents folder under AccessCaster, App settings. It holds none of "
                        f"the desk's settings and no remote-access token.")}


def action_import_app_settings(payload) -> dict:
    """Restore this app's own settings from an export. MERGES, never replaces (see merge_import).

    The shortcuts are re-registered here rather than at the next restart, because a shortcut that
    only works tomorrow reads exactly like one that does not work."""
    why = app_settings.validate(payload)
    if why:
        return {"ok": False, "message": why}
    changes, report = app_settings.merge_import(payload, load_settings(),
                                                _app_settings_available())
    if not changes:
        return {"ok": False, "message": app_settings.describe_import(report)}
    update_settings(**changes)
    if "callme_contacts" in changes:
        callme.set_contacts(changes["callme_contacts"])

    notes = []
    if "hotkeys" in changes:
        # Re-register NOW. mgr.load validates and registers each combination, and saveable() is
        # written back so anything the manager refused (a key another program already owns) does
        # not sit in the file pretending to work.
        try:
            mgr = hotkey_manager()
            mgr.load(_hotkeys_migrate(changes["hotkeys"]), defaults=_hotkey_defaults())
            update_settings(hotkeys=mgr.saveable())
        except Exception as exc:
            notes.append(f" The shortcuts were saved but could not all be registered: {exc}")
    # The announcer holds its settings in module state, so a restored preference has to be pushed
    # into the running announcer or it would not take effect until a restart.
    _apply_announcer_settings(changes)
    return {"ok": True, "report": report,
            "message": app_settings.describe_import(report) + "".join(notes)}


def _apply_announcer_settings(changes: dict) -> None:
    """Push restored announcer preferences into the RUNNING announcer, not just the file."""
    global _ann_levels_on, _ann_volume, _ann_voice, _ann_say_device
    if "announcer_levels" in changes:
        _ann_levels_on = bool(changes["announcer_levels"])
    if "announcer_volume" in changes:
        try:
            _ann_volume = int(changes["announcer_volume"])
        except (TypeError, ValueError):
            pass
    if "announcer_voice" in changes:
        _ann_voice = str(changes["announcer_voice"] or "")
    if "announcer_output" in changes:
        _ann_say_device = str(changes["announcer_output"] or "")


def read_autostart() -> dict:
    """Whether AccessCaster starts when this user signs in. See autostart.py for why it is the
    installer's own registry value rather than one of ours."""
    st = autostart.state()
    return {"ok": True, **st}


def action_set_autostart(on: bool) -> dict:
    """Turn "start when you sign in" on or off, for THIS user only."""
    if on is None:
        return {"ok": False, "message": "Say whether to start at sign-in or not."}
    res = autostart.set_enabled(bool(on))
    return {**res, "ok": bool(res.get("ok"))}


def action_open_app_settings_folder() -> dict:
    """Open the folder the export went to, so the file can actually be copied somewhere safe."""
    folder = app_settings_dir()
    try:
        _open_folder_focused(folder)
    except Exception as exc:
        return {"ok": False, "message": f"The folder is {folder}, but it could not be "
                                        f"opened: {exc}"}
    return {"ok": True, "message": f"Opened {folder}."}


def action_set_hotkey(id: str, mods, key: str, enabled: bool, speak: bool = True) -> dict:
    """Change one shortcut, and persist it only if it was accepted. The manager validates and
    registers first, so the settings file can never hold a binding that does not work."""
    mgr = hotkey_manager()
    res = mgr.set_binding(str(id or ""), mods or [], str(key or ""), bool(enabled), bool(speak))
    if res.get("ok"):
        update_settings(hotkeys=mgr.saveable())
    res["state"] = mgr.state()
    return res


def action_add_hotkey(command: str) -> dict:
    """Add a shortcut for a catalogue command. It arrives on a free key and already working."""
    mgr = hotkey_manager()
    res = mgr.add(str(command or ""), speak=commands.speaks_by_default(command))
    if res.get("ok"):
        update_settings(hotkeys=mgr.saveable())
    res["state"] = mgr.state()
    return res


def action_add_pad_shortcuts() -> dict:
    """Put the whole pad scheme onto this machine's shortcut list, on request.

    THE ONE THING A SHIPPED DEFAULT CANNOT DO. Defaults land only on a machine that has saved
    nothing, so anybody who had already chosen their keys before the pad scheme existed would never
    see it. This is the same set behind a button, so it stays an explicit choice rather than a
    start-up surprise, and it can be pressed again after switching desks - a Duo has six pads in a
    bank where a Pro II has eight, and the missing two are added without disturbing anything."""
    mgr = hotkey_manager()
    res = mgr.add_scheme(_pad_hotkey_scheme())
    update_settings(hotkeys=mgr.saveable())
    added, had, clashed = res["added"], res["already_there"], res["clashed"]
    bits = []
    if added:
        bits.append(f"Added {len(added)} pad shortcut{'' if len(added) == 1 else 's'}.")
    if had:
        bits.append(f"{len(had)} {'was' if len(had) == 1 else 'were'} already there.")
    for command, key, taken_by in clashed:
        bits.append(f"{hotkeys.combo_text(hotkeys.DEFAULT_MODS, key)} is already used for "
                    f"{_hotkey_label(taken_by)}, so {_hotkey_label(command)} was not added.")
    if not bits:
        bits.append("Every pad shortcut was already there.")
    return {"ok": True, "message": " ".join(bits), "state": mgr.state()}


def action_delete_hotkey(id: str) -> dict:
    mgr = hotkey_manager()
    res = mgr.remove(str(id or ""))
    if res.get("ok"):
        update_settings(hotkeys=mgr.saveable())
    res["state"] = mgr.state()
    return res


def action_set_hotkeys_enabled(on: bool) -> dict:
    """The master switch. Off unregisters every combination, so nothing is held from other
    programs: the point of an off switch is that it really lets go."""
    mgr = hotkey_manager()
    res = mgr.set_enabled(bool(on))
    update_settings(hotkeys=mgr.saveable())
    res["state"] = mgr.state()
    return res


# ----------------------------------------------------------------------------
# Declarative route tables. Most endpoints share one shape, so they live in a
# table instead of a long if/elif chain (the chain was ~17 GET + ~45 POST near-
# identical branches). do_GET/do_POST dispatch through these; genuinely special
# routes (raw-body upload, None-aware fields, global-state mutators, dict bodies)
# stay explicit in the handler. Behaviour is unchanged: same functions, same
# field names, same defaults, same coercions.
# ----------------------------------------------------------------------------

# GET: path -> zero-arg callable whose result is sent as JSON.
GET_ROUTES = {
    # THE APP'S OWN VERSION, and nothing else. It is also in /api/status, but that one opens the
    # USB device and only the Device page takes it, so a page relying on it shows "unknown" to
    # anyone who goes straight there. This costs no device access at all, so the Help & About page
    # can have its version without the About tabs reading the unit.
    "/api/version": lambda: {"app_version": APP_VERSION},
    # The remote-access link. LOCAL ONLY, like the wifi join: handing the token out over the very
    # network it protects would defeat it, so a remote page can use the app but cannot read or
    # replace the key that let it in.
    "/api/access": read_access,
    "/api/status": read_device_status,
    "/api/device/overview": device_overview,
    "/api/appsettings/autostart": read_autostart,
    "/api/announce": lambda: {"on": _announce_on.is_set(), "levels": _ann_levels_on,
                              "mute_tones": _ann_mute_tones,
                              "startup_summary": _ann_startup_summary,
                              "volume": _ann_volume,
                              "voice": _ann_voice, "output": _ann_say_device,
                              "voices": _sapi_lists()["voices"],
                              "outputs": _sapi_lists()["outputs"],
                              "running": bool(_announce_thread and _announce_thread.is_alive())},
    "/api/device/state": read_device_state,
    "/api/device/settings": read_unit_settings,
    "/api/faders/levels": fader_levels_live,
    # The pad-restore progress channel. Named for a restore that no longer exists;
    # the factory-effects restore is its only user and the path is kept because the
    # page and the browser checks both address it by name.
    "/api/pads/restore-defaults/status": pad_restore_status,
    "/api/soundpads": soundpad_model,
    "/api/network": read_network_status,
    "/api/channels": action_get_channels,
    "/api/routing": routing_model,
    "/api/routing/live": read_routing_live,
    "/api/routing/driver": read_usb_driver,
    "/api/driver/status": driver_install_status,
    "/api/faders/live": read_faders_live,
    "/api/routing/matrix/live": read_matrix_live,
    "/api/routing/schemes": read_routing_schemes,
    "/api/monitormix/live": read_monitor_mix_live,
    "/api/profiles": list_profiles,
    "/api/backups": read_backups,
    "/api/profiles/save/status": profile_save_status,
    "/api/profiles/restore/status": profile_restore_status,
    "/api/shows": read_show_state,
    "/api/shows/status": show_job_status,
    "/api/baseline/status": baseline_status,
    "/api/meters": read_meters,
    # The full Recording page: one ~1.5 s dump. /api/recorder/live is the cheap companion, answered
    # from the push listener with no device read at all, so a page can poll THAT.
    "/api/recorder": read_recorder,
    "/api/recorder/live": read_recorder_live,
    "/api/callme": read_callme,
    "/api/callme/live": read_callme_live,
    "/api/channels/autogain/status": autogain_status,
    "/api/channels/level-check/status": channel_level_check_status,
    "/api/hotkeys": hotkey_state,
    "/api/hotkeys/commands": hotkey_commands,
}

# POST: path -> (action, argspecs). Each argspec is (cast, json_key, default) read
# from the JSON body and passed positionally to the action. An empty argspec list
# means "no request body; call action() with no arguments".
POST_ROUTES = {
    "/api/refresh": (read_device_status, []),
    "/api/access/new": (action_new_access_token, []),   # LOCAL ONLY (see LOCAL_ONLY_ROUTES)
    "/api/faders/reset": (action_reset_faders, []),
    "/api/pads/clear-all": (action_clear_all_pads, []),
    "/api/pads/tidy-audio": (action_tidy_pad_audio, []),
    "/api/pads/restore-fx": (action_restore_factory_fx, []),
    "/api/pad/export": (action_export_pad, [(int, "pad", 0)]),
    "/api/pads/exports/open": (action_open_pad_exports, []),
    "/api/announce": (action_set_announce, [(bool, "on", None)]),
    "/api/announce/levels": (action_set_announce_levels, [(bool, "on", None)]),
    "/api/announce/mute-tones": (action_set_announce_mute_tones, [(bool, "on", None)]),
    "/api/announce/startup-summary": (action_set_announce_startup_summary, [(bool, "on", None)]),
    "/api/announce/volume": (action_set_announce_volume, [(int, "percent", 100)]),
    "/api/announce/voice": (action_set_announce_voice, [(str, "voice", "")]),
    "/api/announce/output": (action_set_announce_output, [(str, "output", "")]),
    "/api/announce/devices": (action_refresh_speech_devices, []),
    # Global keyboard shortcuts. `mods` is a list of modifier ids ("ctrl", "alt", "win", "shift");
    # the manager validates and registers before anything is persisted.
    "/api/hotkeys/set": (action_set_hotkey, [(str, "id", ""), (list, "mods", []),
                                             (str, "key", ""), (bool, "enabled", True),
                                             (bool, "speak", True)]),
    "/api/hotkeys/add": (action_add_hotkey, [(str, "command", "")]),
    "/api/hotkeys/add-pads": (action_add_pad_shortcuts, []),
    "/api/hotkeys/delete": (action_delete_hotkey, [(str, "id", "")]),
    "/api/hotkeys/enabled": (action_set_hotkeys_enabled, [(bool, "on", None)]),
    "/api/faders/level": (action_set_fader_level,
                          [(int, "fader", -1), (int, "percent", 0)]),
    "/api/monitor/level": (action_set_monitor_speaker_level, [(int, "percent", 0)]),
    "/api/meters/stream": (action_set_meter_stream, [(str, "stream", "")]),
    "/api/meters/option": (action_set_meter_option, [(str, "name", ""), (float, "value", 0)]),
    "/api/meters/watch": (action_meter_watch, [(bool, "on", None)]),
    # 0 = "use the saved length" (30 s). The old default of 2.0 here silently overrode the
    # setting, so a check ran for two seconds however it was configured.
    "/api/channels/level-check": (action_channel_level_check,
                                 [(str, "name", ""), (float, "seconds", 0.0)]),
    "/api/meters/input-level": (action_set_input_level, [(float, "db", 0.0)]),
    "/api/meters/input-mute": (action_set_input_mute, [(bool, "on", None)]),
    "/api/recorder/format": (action_set_recording_format, [(int, "value", -1)]),
    "/api/recorder/command": (action_record, [(str, "command", "")]),
    "/api/device/firmware-check": (action_check_firmware, []),
    "/api/callme/hosting": (action_callme_hosting, [(bool, "on", None)]),
    "/api/callme/answer": (action_callme_answer, []),
    "/api/callme/decline": (action_callme_decline, []),
    "/api/callme/hangup": (action_callme_hangup, [(int, "slot", 0)]),
    "/api/callme/end": (action_callme_end, []),
    "/api/callme/extend": (action_callme_extend, []),
    "/api/callme/auto-extend": (action_callme_auto_extend, [(bool, "on", None)]),
    "/api/callme/dial": (action_callme_dial, [(str, "code", "")]),
    "/api/callme/contact/save": (action_callme_contact_save,
                                 [(str, "name", ""), (str, "code", ""), (str, "was", "")]),
    "/api/callme/contact/delete": (action_callme_contact_delete, [(str, "code", "")]),
    "/api/recorder/marker": (action_drop_marker, []),
    "/api/recorder/delete": (action_delete_recording, [(int, "uid", -1)]),
    "/api/storage/eject": (action_storage_eject, [(int, "index", -1), (bool, "on", True)]),
    "/api/storage/erase": (action_storage_erase, [(int, "index", -1)]),
    "/api/channels/autogain": (action_autogain_start,
                               [(str, "name", ""), (float, "seconds", 0.0)]),
    "/api/channels/autogain/stop": (action_autogain_stop, []),
    "/api/channels/autogain/undo": (action_autogain_undo, []),
    "/api/recorder/announcer-mute": (action_set_record_mute_announcer, [(bool, "on", None)]),
    "/api/network/wifi": (action_set_wifi, [(bool, "on", None)]),
    "/api/network/wifi/scan": (action_scan_wifi, []),
    # LOCAL ONLY (see LOCAL_ONLY_ROUTES): this one carries a wifi password.
    "/api/network/wifi/join": (action_join_wifi, [(str, "ssid", ""), (str, "password", "")]),
    "/api/network/bt-visible": (action_set_bt_visible, [(bool, "on", None)]),
    "/api/output/mute": (action_set_output_mute, [(str, "which", ""), (bool, "on", None)]),
    "/api/output/bt-level": (action_set_bt_output_level, [(int, "percent", 0)]),
    "/api/midi": (action_set_midi, [(bool, "on", None)]),
    "/api/channel/gain": (action_set_gain, [(int, "channel", 0), (int, "db", 0)]),
    "/api/channel/phantom": (action_set_phantom, [(int, "channel", 0), (bool, "on", None)]),
    "/api/channel/polarity": (action_set_polarity, [(int, "channel", 0), (bool, "on", None)]),
    "/api/channel/mic-type": (action_set_mic_type, [(int, "channel", 0), (int, "type", 0)]),
    "/api/device/setting": (action_set_unit_setting, [(str, "prop", ""), (int, "value", 0)]),
    "/api/channels/mute": (action_set_channel_mute, [(str, "name", ""), (bool, "on", None)]),
    "/api/channels/solo": (action_set_channel_solo, [(str, "name", ""), (bool, "on", None)]),
    "/api/channels/solo/clear": (action_clear_solos, []),
    "/api/appsettings/export": (action_export_app_settings, []),
    "/api/appsettings/open": (action_open_app_settings_folder, []),
    "/api/appsettings/autostart": (action_set_autostart, [(bool, "on", None)]),
    "/api/channels/wireless-mute": (action_set_channel_wireless_mute,
                                    [(str, "name", ""), (bool, "on", None)]),
    "/api/channels/level": (action_set_channel_level, [(str, "name", ""), (int, "db", 0)]),
    "/api/channels/advanced": (action_set_channel_advanced,
                               [(str, "name", ""), (bool, "on", None)]),
    "/api/channels/processing-onoff": (action_set_processing_enabled,
                                       [(str, "name", ""), (bool, "on", None)]),
    "/api/channels/module": (action_set_channel_module,
                             [(str, "name", ""), (str, "module", ""), (bool, "on", None)]),
    "/api/channels/param": (action_set_channel_param,
                            [(str, "name", ""), (str, "prop", ""), (float, "value", 0)]),
    "/api/channels/effect": (action_set_channel_effect,
                             [(str, "name", ""), (str, "effect", ""), (bool, "on", None)]),
    "/api/channels/effect-param": (action_set_channel_effect_param,
                                   [(str, "name", ""), (str, "param", ""), (float, "value", 0)]),
    "/api/processing/preset": (action_apply_mic_preset, [(int, "channel", 0), (str, "preset", "")]),
    "/api/processing/preset/save": (action_save_mic_preset, [(int, "channel", 0), (str, "name", "")]),
    "/api/processing/preset/delete": (action_delete_mic_preset, [(str, "name", "")]),
    "/api/routing/inputsource": (action_set_input_source, [(str, "strip", ""), (int, "source", -1)]),
    "/api/faders/set": (action_set_fader, [(int, "fader", -1), (int, "code", -1)]),
    "/api/faders/stereo-link": (action_set_stereo_link, [(int, "pair", -1), (bool, "on", None)]),
    "/api/faders/mute": (action_set_fader_mute, [(int, "fader", -1), (bool, "on", None)]),
    "/api/faders/solo": (action_set_fader_solo, [(int, "fader", -1), (bool, "on", None)]),
    "/api/monitormix/set": (action_set_monitor_level,
                            [(int, "output", -1), (int, "source", -1), (float, "value", 0)]),
    "/api/monitormix/reset": (action_reset_monitor_output, [(int, "output", -1)]),
    "/api/routing/multitrack": (action_set_multitrack, [(int, "mode", -1)]),
    # USB audio is ROUTING's, not the recorder's: it is an output, and it never
    # touches the drive, the transport or the recordings list. Same OUTPUT selector either way.
    "/api/routing/usb-multitrack": (action_set_usb_multitrack, [(int, "mode", -1)]),
    "/api/routing/usb-expanded": (action_set_usb_expanded, [(bool, "on", None)]),
    "/api/driver/install": (action_install_driver, []),
    "/api/driver/page": (action_open_driver_page, []),
    "/api/routing/usb-bypass": (action_set_usb_bypass, [(bool, "on", None)]),
    "/api/recorder/processing-bypass": (action_set_processing_bypass, [(bool, "on", None)]),
    "/api/routing/automute": (action_set_auto_mute, [(str, "which", ""), (bool, "on", None)]),
    "/api/routing/headphone": (action_set_headphone_type, [(int, "value", -1), (int, "output", 0)]),
    "/api/routing/compellor": (action_set_master_compellor, [(bool, "on", None)]),
    "/api/routing/compellor-param": (action_set_compellor_param,
                                     [(str, "param", ""), (float, "value", 0)]),
    "/api/routing/outputmode": (action_set_output_mode, [(int, "output", -1), (int, "mode", -1)]),
    "/api/routing/crosspoint": (action_set_cross_point,
                                [(int, "output", -1), (int, "source", -1), (bool, "excluded", None)]),
    "/api/routing/reset-outputs": (action_reset_all_outputs, []),
    # Mix schemes. The Chat presets are built-in schemes (ids chat-mic-only and chat-normal) and go
    # through this one apply path rather than an endpoint of their own.
    "/api/routing/schemes/apply": (action_apply_routing_scheme,
                                   [(str, "name", ""), (bool, "builtin", False)]),
    "/api/routing/schemes/save": (action_save_routing_scheme,
                                  [(str, "name", ""), (str, "output", "")]),
    "/api/routing/schemes/delete": (action_delete_routing_scheme, [(str, "name", "")]),
    "/api/profiles/restore": (restore_profile, [(str, "name", ""), (bool, "clean", False)]),
    # The desk's OWN backup. Import and delete take a SNAPSHOT INDEX, which is the only stable
    # per-file handle: a uid is a position any export renumbers, and a uuid names the show rather
    # than the file, so several backups share one (shows.py traps 1 and 2).
    "/api/shows/export": (action_show_export, []),
    "/api/shows/import": (action_show_import, [(int, "snapshot", -1)]),
    "/api/shows/delete": (action_show_delete, [(int, "snapshot", -1)]),
    "/api/shows/reset": (action_show_reset, [(bool, "backup_first", True)]),
    # ONE backup: exports a show when there is a drive to hold one, always saves a profile, and
    # pairs them. Restore prefers the show and falls back to the profile. The four granular routes
    # above stay: they are what a Duo (no removable storage) and the clean-restore path still use.
    "/api/backup": (action_backup_now, [(str, "name", "")]),
    "/api/backup/restore": (action_restore_backup, [(str, "name", ""), (bool, "clean", False)]),
    "/api/backup/plan": (backup_restore_plan, [(str, "name", "")]),
    "/api/backup/delete": (action_delete_backup, [(str, "name", "")]),
    # Manual recovery for the silent-channel fault, for the times it happens with no restore
    # involved. Safe to press at any time: it nudges each mic gain and puts it straight back.
    "/api/channels/wake": (wake_channels, []),
    "/api/profiles/info": (profile_info, [(str, "name", "")]),
    "/api/profiles/delete": (delete_profile, [(str, "name", ""), (bool, "force", False)]),
    "/api/baseline/save": (action_baseline_save, [(bool, "include_pads", True)]),
    "/api/baseline/skip": (action_baseline_skip, []),
    "/api/baseline/add-pads": (action_baseline_add_pads, []),
    "/api/profiles/rename": (rename_profile, [(str, "name", ""), (str, "new_name", "")]),
    "/api/pad/clear": (action_clear_pad, [(int, "pad", 0)]),
    "/api/pad/replay": (action_set_replay, [(int, "pad", 0), (bool, "on", None)]),
    "/api/pad/gain": (action_set_pad_gain, [(int, "pad", 0), (float, "db", 0)]),
    "/api/pad/name": (action_set_pad_name, [(int, "pad", 0), (str, "name", "")]),
    "/api/pad/loop": (action_set_pad_loop, [(int, "pad", 0), (bool, "on", None)]),
    "/api/pad/colour": (action_set_pad_colour, [(int, "pad", 0), (int, "colour", 0)]),
    "/api/pad/playmode": (action_set_play_mode, [(int, "pad", 0), (int, "mode", -1)]),
    "/api/pad/mixer-trigger": (action_set_mixer_trigger, [(int, "pad", 0), (int, "mode", -1)]),
    "/api/pad/trigger": (action_set_pad_trigger,
                         [(int, "pad", 0), (str, "field", ""), (int, "value", 0)]),
    "/api/pad/envelope": (action_set_pad_envelope,
                          [(int, "pad", 0), (str, "field", ""), (float, "percent", 0.0)]),
    "/api/pad/fade": (action_set_pad_fade,
                      [(int, "pad", 0), (str, "field", ""), (float, "seconds", 0.0)]),
    "/api/pad/flag": (action_set_pad_flag,
                      [(int, "pad", 0), (str, "name", ""), (bool, "on", None)]),
    "/api/pad/backchannel": (action_set_back_channel,
                             [(int, "pad", 0), (str, "source", ""), (bool, "on", None)]),
    "/api/pad/create-special": (action_create_special_pad, [(int, "pad", 0), (str, "type", "")]),
    "/api/pad/assign-fx": (action_assign_fx, [(int, "pad", 0), (str, "effect", "")]),
    "/api/fx/delete": (action_delete_fx_preset, [(str, "name", "")]),
    "/api/fx/preview/stop": (action_preview_stop, [(int, "grid", 0)]),
}


class Handler(BaseHTTPRequestHandler):
    # Quieter logging; the user is on a screen reader and console noise is not
    # useful to them.
    def log_message(self, fmt, *args):
        pass

    # --- helpers -----------------------------------------------------------
    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type, extra_headers=None):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self.send_error(404)
            return
        self._send_body(body, content_type, extra_headers)

    def _send_body(self, body: bytes, content_type, extra_headers=None):
        """Send bytes we already hold, with the same headers a static file gets.

        Split out from _send_file for index.html, which is assembled rather than read (page.py
        splices static/help.html into it). The no-cache headers below are the reason this is one
        function and not two: a second copy would drift and start serving a stale page."""
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or []):
            self.send_header(name, value)
        # No caching: this is a local single-user app and the static files change between
        # sessions/updates. Without this the browser serves a STALE app.js/index.html after an
        # update, which silently breaks features (a stale app.js against fresh index.html).
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def _is_local(self) -> bool:
        """Did this request come from the machine the unit is plugged into?

        Used by LOCAL_ONLY_ROUTES. Judged on the peer address rather than a header, since a header
        is whatever the caller says it is."""
        host = (self.client_address[0] if self.client_address else "") or ""
        return host in _LOCAL_HOSTS or host.startswith("::ffff:127.")

    # --- remote access ------------------------------------------------------
    def _token_offered(self) -> str:
        """The token this request carries, from the query string or the cookie we set."""
        q = urllib.parse.urlparse(self.path).query
        got = urllib.parse.parse_qs(q).get("t", [""])[0]
        if got:
            return got
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "ac_token":
                return value
        return ""

    def _authorised(self) -> bool:
        """Local requests always pass; anything else needs the token.

        `compare_digest` rather than `==` so a wrong token cannot be narrowed down by timing. It is
        a small thing on a LAN, but it costs one call."""
        if self._is_local():
            return True
        return secrets.compare_digest(self._token_offered(), access_token())

    def _refuse(self) -> None:
        """Say NO in a way a person can act on.

        A bare 401 tells a screen-reader user nothing, so this is a real page with a heading and an
        explanation. It deliberately does NOT hint at the token or the address: someone who has
        reached this page without the link is exactly who should learn nothing from it."""
        body = ("<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                "<title>AccessCaster - link needed</title></head><body>"
                "<h1>You need the access link</h1>"
                "<p>AccessCaster is running on another computer and will not accept control from "
                "this device without its access link.</p>"
                "<p>On the computer the RODECaster is plugged into, open AccessCaster, go to the "
                "Device page and then the Hardware tab, and find \"Remote access from another "
                "device\". Send that link to this device and open it.</p>"
                "</body></html>").encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    def _read_json(self) -> dict:
        raw = self._read_body()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    # --- routing -----------------------------------------------------------
    def do_GET(self):
        # THE GATE. Local requests pass untouched, so nothing on this machine changes; a remote one
        # needs the token. Checked before ANY route, including the static files, so a stranger
        # cannot read the page and see the desk's state without control of it.
        if not self._authorised():
            self._refuse()
            return
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            # THE COOKIE IS SET HERE AND NOWHERE ELSE. It is what lets every fetch in app.js stay
            # ignorant of the token: the page is loaded once with ?t=..., and the browser carries
            # the cookie on the API calls that follow. SameSite=Lax so another site cannot ride it,
            # and no Secure flag because this is plain HTTP on a LAN - stated in the UI rather than
            # pretended otherwise.
            extra = None
            if not self._is_local():
                extra = [("Set-Cookie",
                          "ac_token=%s; Path=/; SameSite=Lax; Max-Age=31536000"
                          % access_token())]
            # ASSEMBLED, NOT READ: page.render_index splices static/help.html in at the marker.
            self._send_body(page.render_index(), "text/html; charset=utf-8", extra_headers=extra)
            return
        # LOCAL ONLY applies to reads too, and it did not before: `/api/access` hands back the
        # token itself, so a remote page that is legitimately signed in must still not be able to
        # read it out (or replace it, which is the POST half). Only the machine the desk is plugged
        # into may see the key.
        if path in LOCAL_ONLY_ROUTES and not self._is_local():
            self._send_json({"ok": False,
                             "message": "The access link can only be seen on the computer the "
                                        "unit is plugged into."})
            return
        fn = GET_ROUTES.get(path)
        if fn is not None:
            # /live and /network routes do a slower (~1.5s) device read; the rest are cheap.
            self._send_json(fn())
            return
        if path.startswith("/static/"):
            name = path[len("/static/"):]
            safe = os.path.normpath(name).replace("\\", "/")  # block traversal
            if safe.startswith("..") or safe.startswith("/"):
                self.send_error(403)
                return
            full = os.path.join(STATIC_DIR, safe)
            ctype = (
                "text/css; charset=utf-8" if name.endswith(".css")
                else "application/javascript; charset=utf-8" if name.endswith(".js")
                # Any extra page served straight out of static/. Added for the tab-layout
                # prototype (uitest.html, deleted once every page was converted) and kept
                # because without it an .html here arrives as text/plain and the browser
                # shows the source instead of rendering it.
                else "text/html; charset=utf-8" if name.endswith(".html")
                else "text/plain; charset=utf-8"
            )
            self._send_file(full, ctype)
            return
        self.send_error(404)

    def do_POST(self):
        global _last_ping, _client_seen, _quit_at
        # Same gate as do_GET, and it matters more here: every write to the desk is a POST.
        if not self._authorised():
            self._refuse()
            return
        try:
            if self.path == "/api/ping":
                # App-lifecycle heartbeat from the open webpage. Cheap, no device access.
                # A ping also CANCELS a pending quit, so a page reload does not stop the server.
                _last_ping = time.time()
                _client_seen = True
                _quit_at = None
                self._send_json({"ok": True, "instance": _INSTANCE_ID, "version": APP_VERSION})
            elif self.path == "/api/quit":
                # The webpage is closing (sendBeacon on pagehide). Schedule shutdown after a
                # short grace; a reloaded page's ping arrives first and cancels it.
                _quit_at = time.time() + _QUIT_GRACE
                diagnostics.event("app.quit_beacon", "the webpage says it is closing")
                self._send_json({"ok": True})
            elif self.path == "/api/focus":
                # A duplicate page asks us to raise the already-open app window.
                self._send_json({"ok": _focus_app_window()})
            elif self.path.startswith("/api/pad/replace"):
                # pad number in query (?pad=N), filename in a header, body = raw WAV.
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(self.path).query)
                pad_n = int((q.get("pad") or ["0"])[0])
                # replay defaults ON (factory behaviour); ?replay=0 makes the pad pause/resume.
                replay = (q.get("replay") or ["1"])[0] not in ("0", "false", "off")
                filename = self.headers.get("X-Filename", "sound.wav")
                raw = self._read_body()
                self._send_json(action_add_sound(pad_n, filename, raw, replay))
            elif self.path == "/api/routing/delay":
                # ms is None-aware (None keeps the current delay; only on/off changes).
                data = self._read_json()
                ms = data.get("ms", None)
                self._send_json(action_set_output_delay(bool(data.get("on")),
                                                        None if ms is None else float(ms)))
            elif self.path == "/api/profiles/save":
                # include_pads is a keyword arg on snapshot_profile.
                data = self._read_json()
                self._send_json(snapshot_profile(str(data.get("name", "")),
                                                 include_pads=bool(data.get("include_pads"))))
            elif self.path == "/api/appsettings/import":
                # The WHOLE body is the exported file, not a field in it: the page reads the file
                # the user picked and posts it verbatim, so nothing has to be re-shaped on the way.
                self._send_json(action_import_app_settings(self._read_json()))
            elif self.path == "/api/fx/save":
                # stack is a dict body, not a scalar field.
                data = self._read_json()
                self._send_json(action_save_fx_preset(
                    str(data.get("name", "")),
                    data.get("stack") or {},
                    int(data.get("colour", 8))))
            elif self.path == "/api/fx/preview/start":
                # stack is a dict body, not a scalar field.
                data = self._read_json()
                self._send_json(action_preview_start(
                    data.get("stack") or {},
                    int(data.get("colour", 8)),
                    int(data.get("mic", 1))))
            elif self.path == "/api/fx/preview/update":
                # value is passed through untyped (number or string).
                data = self._read_json()
                self._send_json(action_preview_update(
                    int(data.get("grid", 0)),
                    str(data.get("param", "")),
                    data.get("value")))
            elif self.path in LOCAL_ONLY_ROUTES and not self._is_local():
                # A wifi password must not cross the network it is for. The app serves on 0.0.0.0
                # so a phone can drive the desk from another room, and that is exactly why this
                # one route refuses a remote caller instead of quietly accepting one.
                self._send_json({
                    "ok": False,
                    "message": ("Joining a wifi network can only be done from the computer the "
                                "unit is plugged into. The password would otherwise travel across "
                                "the network in the clear, which is the network you are joining.")})
            elif self.path in POST_ROUTES:
                action, specs = POST_ROUTES[self.path]
                if specs:
                    data = self._read_json()
                    args = [cast(data.get(key, default)) for (cast, key, default) in specs]
                    self._send_json(action(*args))
                else:
                    self._send_json(action())
            else:
                self.send_error(404)
        except (ValueError, TypeError) as exc:
            self._send_json({"ok": False, "message": f"Bad request: {exc}"}, status=400)
        except Exception as exc:
            self._send_json({"ok": False, "message": f"Server error: {exc}"}, status=500)


def _browser_exe():
    """Locate a Chromium browser (Edge or Chrome) for 'app mode'. Edge ships with
    Windows 11 so it is the reliable default; Chrome is used if it is found first."""
    names = []
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if not base:
            continue
        names.append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
        names.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
    for path in names:
        if os.path.isfile(path):
            return path
    return shutil.which("msedge") or shutil.which("chrome")


def open_ui(url: str) -> None:
    """Open the UI in a Chromium 'app mode' window (no tabs, address bar or toolbar)
    so keyboard focus stays inside the page and Tab never lands on browser chrome.
    Falls back to the default browser if no Chromium browser is found."""
    exe = _browser_exe()
    if exe:
        try:
            subprocess.Popen([exe, f"--app={url}"])
            return
        except Exception:
            pass
    try:
        webbrowser.open(url)
    except Exception:
        pass


# The app sets its window/tab title to "<page> - AccessCaster" (app.js APP_TITLE), so we match a
# title ENDING in "- AccessCaster". That is precise: it catches every app page but not a folder in
# File Explorer ("AccessCaster - File Explorer") or an editor ("... - Visual Studio Code"). The
# startswith clause covers the brief initial load before the SPA sets the per-page title.
_APP_WINDOW_TITLE_SUFFIX = " - AccessCaster"
_APP_WINDOW_TITLE_PREFIX = "AccessCaster - RODECaster"


def _is_app_window_title(title: str) -> bool:
    return title.endswith(_APP_WINDOW_TITLE_SUFFIX) or title.startswith(_APP_WINDOW_TITLE_PREFIX)


def _focus_app_window() -> bool:
    """Bring the existing AccessCaster browser window to the foreground (Windows only). Returns
    True if a matching top-level window was found and raised. Used so a relaunch (or a duplicate
    page) raises the window that is already open instead of opening another one."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False
    user32 = ctypes.windll.user32
    # 64-bit HWNDs must not be truncated to a C int, so declare the signatures explicitly.
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [EnumProc, wintypes.LPARAM]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    # Declared like the rest: an undeclared restype is a C int, which TRUNCATES a 64-bit HWND and
    # would make the success check below compare two different halves of the same window.
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND

    found = []

    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if _is_app_window_title(buf.value):
                found.append(hwnd)
        return True

    try:
        user32.EnumWindows(EnumProc(_cb), 0)
    except Exception:
        return False
    if not found:
        return False
    hwnd = found[0]
    SW_RESTORE = 9
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)

    # A PLAIN SetForegroundWindow, AND NOTHING ELSE. Windows grants this process the right to set
    # the foreground while the hotkey it registered is being handled, so the call is allowed and
    # produces a NORMAL activation - which is the whole point, because a normal activation is what
    # tells a screen reader the window came forward.
    #
    # DO NOT ADD A WORKAROUND HERE IF IT FAILS. Every one of them - a minimize/restore flip, an
    # Alt tap, AttachThreadInput - puts the window in front WITHOUT a real activation: measured,
    # the window became foreground and held it, GetGUIThreadInfo agreed the keyboard focus was
    # ours, and NVDA went on reading the app the user came from. Alt-Tabbing to the same window
    # announced it perfectly, which is what proved the window and the screen reader were both
    # fine. A refusal here means the CALLER has lost the foreground right, and the fix is at that
    # end - see hotkeys._FOREGROUND_COMMANDS, where dispatching to a worker thread was spending it.
    user32.SetForegroundWindow(hwnd)
    # ASK WINDOWS WHAT HAPPENED RATHER THAN ASSUMING IT WORKED. This returned True unconditionally,
    # so a refused raise and a successful one were indistinguishable - and the caller stays silent
    # on a success, which makes a refusal silent too. It also made the fault impossible to
    # diagnose: a note recorded "the raise is refused" when the raise was in fact working and the
    # real fault was focus landing nowhere announceable.
    got = user32.GetForegroundWindow()
    ok = bool(got) and got == hwnd
    # LOG THE FOCUS WINDOW, NOT JUST THE FOREGROUND ONE. They are different things and only the
    # second is what a screen reader follows, so "the window came up but my screen reader still
    # says I am in the other app" has two possible causes that look identical from outside: a
    # window that is merely in front without holding focus, or a screen reader reporting a stale
    # object because nothing generated a focus event. This one line separates them, and the
    # question has come round three times without it. GetGUIThreadInfo(0) reports the foreground
    # thread's own focus window.
    try:
        class _GTI(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                        ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                        ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                        ("rcCaret", wintypes.RECT)]
        gti = _GTI()
        gti.cbSize = ctypes.sizeof(_GTI)
        user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(_GTI)]
        user32.GetGUIThreadInfo(0, ctypes.byref(gti))

        def _title(h):
            if not h:
                return "none"
            n = user32.GetWindowTextLengthW(h)
            b = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(h, b, n + 1)
            return (b.value or "")[:40]

        diagnostics.event("window.raise", "app window", activated=ok,
                          wanted=f"{hwnd}", foreground=f"{got}",
                          fg_title=_title(got),
                          active=f"{gti.hwndActive}", active_title=_title(gti.hwndActive),
                          focus=f"{gti.hwndFocus}", focus_title=_title(gti.hwndFocus))
    except Exception as exc:
        diagnostics.event("window.raise", "app window", activated=ok, error=f"{exc}")
    return ok


def _app_already_serving(port: int) -> bool:
    """True if an AccessCaster server is already answering on `port`. A lightweight GET to a known
    endpoint, so we can decide to raise the existing window rather than start a second server."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/api/announce", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def _pids_listening_on(port: int) -> set:
    """The PIDs of every process LISTENING on `port`, this one excluded. Pure-stdlib (netstat), so
    it reads processes belonging to other Windows accounts without any privilege of its own; an
    empty set on non-Windows or any failure."""
    if os.name != "nt":
        return set()
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                             capture_output=True, text=True, timeout=5,
                             creationflags=_NO_WINDOW).stdout
    except Exception:
        return set()
    me = os.getpid()
    pids = set()
    for line in out.splitlines():
        if "LISTENING" not in line:
            continue
        parts = line.split()
        if len(parts) < 5 or not parts[1].endswith(f":{port}"):
            continue
        try:
            pid = int(parts[-1])
        except ValueError:
            continue
        if pid and pid != me:
            pids.add(pid)
    return pids


def _session_of_pid(pid: int):
    """The Windows session a process belongs to, or None if it cannot be read. `ProcessIdToSessionId`
    takes a bare PID and opens nothing, so it works on another account's process where reading that
    process's owner would need admin. Session numbers are all this needs: the point is only whether
    a holder is US or SOMEBODY ELSE."""
    if os.name != "nt":
        return None
    import ctypes
    sid = ctypes.c_ulong()
    try:
        if not ctypes.windll.kernel32.ProcessIdToSessionId(ctypes.c_ulong(pid), ctypes.byref(sid)):
            return None
    except Exception:
        return None
    return sid.value


def _other_account_holds_port(port: int) -> bool:
    """True if `port` is held by a process in a DIFFERENT Windows session, i.e. another signed-in
    account on this PC has AccessCaster running.

    THE WHOLE APP IS NOT MACHINE-WIDE, ONLY THE PORT IS. The announcer speaks into the session that
    started it, the global shortcuts are registered there, the tray icon lives there and the
    settings written are that account's. So a second account served by the first account's process
    gets a page that looks right, says nothing and answers no key - which is why this is a refusal
    rather than a share."""
    mine = _session_of_pid(os.getpid())
    if mine is None:
        return False
    return any(_session_of_pid(pid) not in (None, mine) for pid in _pids_listening_on(port))


def _refuse_other_account(interactive: bool) -> None:
    """Report that another Windows account already has the app, and stop. `interactive` is "were we
    about to open a window for you": if so we owe an explanation in a dialog, and if not (a login
    start, or a headless tooling run) nobody is watching a screen and a blocking dialog would hang
    the run. A native MessageBox rather than anything of our own, because it is a real modal dialog
    to every screen reader without a line of work."""
    text = ("AccessCaster is already running in another Windows account on this PC.\n\n"
            "Only one account can use it at a time. There is a single RODECaster on the USB, and "
            "the spoken announcements and keyboard shortcuts belong to the account that started "
            "the app.\n\n"
            "Quit AccessCaster in the other account, or sign that account out, then start it "
            "again here.\n\n"
            "To use the app from a different machine instead, turn on Remote access on the Device "
            "page of the copy that is already running.")
    print("AccessCaster is already running in another Windows account on this PC.")
    if not interactive or os.name != "nt":
        return
    import ctypes
    MB_OK, MB_ICONWARNING, MB_SETFOREGROUND = 0x0, 0x30, 0x10000
    try:
        ctypes.windll.user32.MessageBoxW(0, text, "AccessCaster",
                                         MB_OK | MB_ICONWARNING | MB_SETFOREGROUND)
    except Exception:
        pass


def _close_previous_instances(port: int) -> bool:
    """Single-instance: terminate any OTHER process of OURS already listening on `port` so this new
    server cleanly takes over, which stops a stale zombie server answering with old code.

    **OUR OWN SESSION ONLY.** The holder of the port can belong to another signed-in account, and
    killing that is taking away somebody else's running app; it fails today only because doing so
    needs admin, which is luck rather than a design. `_other_account_holds_port` handles that case
    properly, well before this runs.

    Best-effort and pure-stdlib (netstat + taskkill); no-op on non-Windows or any failure. Returns
    True if it killed at least one."""
    if os.name != "nt":
        return False
    mine = _session_of_pid(os.getpid())
    pids = {pid for pid in _pids_listening_on(port) if _session_of_pid(pid) == mine}
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                          capture_output=True, timeout=5, creationflags=_NO_WINDOW)
            print(f"Closed a previous AccessCaster instance (PID {pid}).")
        except Exception:
            pass
    if pids:
        time.sleep(1.0)            # let the socket(s) release before we bind
    return bool(pids)


class _ExclusiveHTTPServer(ThreadingHTTPServer):
    """THE BIND IS THE SINGLE-INSTANCE LOCK. `HTTPServer` sets `allow_reuse_address = 1`, and on
    Windows that flag lets a second process bind a port another process already holds - so two
    servers could sit on 8770 at once, in different Windows sessions, both reaching for the one
    desk on the USB. False makes the second bind fail instead, which every entry point already
    handles as "somebody else is serving"."""
    allow_reuse_address = False


def _bring_up_server(open_browser=True, watchdog=True, announcer=True, shortcuts=True):
    """Bind the server, wire its lifecycle, and (optionally) open the UI. Returns the running
    ThreadingHTTPServer (already stored in _httpd) or None when another instance is already
    serving or the port could not be bound. Does NOT call serve_forever - the caller decides
    whether to block (main) or run it on a thread (the tray app).

      - open_browser: open the accessible web UI app-window after binding.
      - watchdog:     run the auto-quit watchdog (stops the server once the page closes). The
                      tray app passes False so closing the window leaves the server in the tray.
      - announcer:    turn the control announcer on at startup.
      - shortcuts:    register the global keyboard shortcuts. OFF in headless mode: a background
                      tooling run must never take keys away from the whole machine.
    """
    url = f"http://localhost:{HTTP_PORT}/"
    # THE ONE PLACE THE DIAGNOSTIC LOG IS STARTED, because it is the one place every entry point
    # goes through: the tray, `python server.py` and the dev server all land here. Starting it in
    # each entry point instead would put two processes on one file the moment a second launch
    # raced the first; this way only the process that actually owns the server ever writes to it.
    diagnostics.start("AccessCaster server starting", version=APP_VERSION,
                      watchdog=watchdog, announcer=announcer, shortcuts=shortcuts)
    # ANOTHER SIGNED-IN ACCOUNT ON THIS PC HAS IT: refuse. This comes first because both branches
    # below would otherwise mistake that instance for our own - `_app_already_serving` is a plain
    # HTTP GET and localhost is machine-wide, while `_focus_app_window` enumerates windows and
    # cannot see another session's, so the pair of them ends up opening somebody else's UI.
    if _other_account_holds_port(HTTP_PORT):
        _refuse_other_account(open_browser)
        diagnostics.stop("another Windows account is running AccessCaster")
        return None
    # If the app is already running with a window open, just raise THAT window and exit. This is
    # the normal "launched it again" case: never open a second browser window.
    if open_browser and _app_already_serving(HTTP_PORT) and _focus_app_window():
        print("AccessCaster is already running; brought its window to the front.")
        diagnostics.stop("another instance is already serving")
        return None
    _close_previous_instances(HTTP_PORT)   # otherwise (zombie/headless server) take over the port
    try:
        server = _ExclusiveHTTPServer((HTTP_HOST, HTTP_PORT), Handler)
    except OSError:
        # Could not bind (a launch race, or a process we could not kill). If another account took
        # the port in the moment since the check above, say so rather than opening their UI.
        if _other_account_holds_port(HTTP_PORT):
            _refuse_other_account(open_browser)
            diagnostics.stop("another Windows account is running AccessCaster")
            return None
        # Otherwise raise the existing window rather than opening another; only fall back to a
        # fresh window if none is found.
        if open_browser and _focus_app_window():
            print("AccessCaster is already running; brought its window to the front.")
        else:
            print(f"AccessCaster is already running; opening {url}")
            open_ui(url)
        diagnostics.stop("could not bind the port")
        return None
    global _httpd, _last_ping
    _httpd = server
    _last_ping = time.time()
    diagnostics.event("app.serving", url)
    print(f"AccessCaster serving on {url}")
    print(f"(LAN: http://<this-pc-ip>:{HTTP_PORT}/ - USB control runs on this PC)")
    if watchdog:
        # App lifecycle: when the watchdog is on, the server lives only while a webpage is open.
        threading.Thread(target=_client_watchdog, daemon=True).start()
    if announcer:
        # The control announcer has its own read-only HID handle, so it never blocks the writes.
        # The Device page can still turn it off.
        action_set_announce(True)
    # SEED THE EXPANDED FLAG BEFORE ANY PAGE ASKS FOR IT. It is a published cache, and unpublished
    # it answers False: the fader dropdown then omits Game, Music, A and B and the write gate
    # refuses them, so on a desk that IS in Expanded the four cannot be assigned until something
    # reads the Routing page. The announcer seeds it too, which is why this was invisible - but
    # the announcer only runs while announcements are ON, and they are a user setting.
    # Skipped when something has already published, so this costs one dump at most.
    threading.Thread(target=_seed_usb_expanded, daemon=True, name="usb-mode-seed").start()
    # RODE's virtual devices may have been left switched off in Windows by a mode change made
    # while this app was not running; apply the driver's current state now. Delayed so the
    # announcer's greeting comes first, and silent unless something actually changed.
    threading.Thread(target=lambda: (time.sleep(4), _virtual_devices_follow(None, False)),
                     daemon=True, name="virtual-devices-start").start()
    # And keep agreeing with it. The pass above, and the one after every mode write, each act on a
    # single sample, so a bind that lands a moment later leaves Windows holding endpoints this app
    # switched off itself. Windows announces every endpoint change; this listens and repairs.
    threading.Thread(target=virtual_devices.watch_endpoints,
                     args=(_virtual_devices_repair, _virtual_devices_watch_stop),
                     daemon=True, name="virtual-devices-watch").start()
    if shortcuts:
        # Global shortcuts, started whatever the announcer is doing (one of them turns it on).
        # Failures are reported and never fatal: no shortcut is worth refusing to start the app.
        try:
            mgr = hotkey_manager()
            mgr.start()
            live = [a["combo"] for a in mgr.state()["shortcuts"]
                    if a["enabled"] and not a["problem"]]
            print(f"Keyboard shortcuts registered: {len(live)}.")
        except Exception as exc:
            print(f"Keyboard shortcuts unavailable: {exc}")
    # The recording state listener, started whatever the announcer is doing: it is what makes the
    # Recording page free to poll, and it is how a press of the unit's own REC button is noticed.
    # Read-only, its own handle, and idle unless the recorder changes state.
    _recorder_watcher()
    # The CallMe listener, for the same reasons: a call rings whatever page is open, and it has to
    # be heard whether or not anyone is announcing. It seeds itself from a dump once it opens.
    _callme_watcher()
    # Best-effort live read at startup so the first page load shows real state.
    try:
        seed = read_live_settings()
        if seed.get("ok"):
            print(f"Seeded live settings from the device (MIDI "
                  f"{'on' if _session_state['midi'] else 'off'}, firmware "
                  f"{_session_state['firmware']}).")
        else:
            print(f"Startup read skipped: {seed.get('message')}")
    except Exception as exc:
        print(f"Startup read skipped: {exc}")
    if open_browser:
        open_ui(url)
    return server


def start_server_managed(announcer=True, shortcuts=True, open_browser=True):
    """Start the server for the tray app: there is NO auto-quit watchdog, so closing the web page
    leaves the server running (it lives in the system tray until Quit). The server runs on a
    background daemon thread. Returns the server, or None if it did not start (another instance was
    already serving).

    `open_browser` FALSE IS THE LOGIN CASE (`tray.main`, `--startup`). Started at sign-in you want
    the tray, the announcer and the global shortcuts ready, and emphatically NOT a browser window
    stealing the focus of whatever you actually signed in to do. Nothing else changes: the watchdog
    is already off on this path, so a tray instance with no page open sits there quite happily
    rather than deciding after two minutes that the browser failed to start."""
    server = _bring_up_server(open_browser=open_browser, watchdog=False, announcer=announcer,
                              shortcuts=shortcuts)
    if server is not None:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def stop_server():
    """Stop the server cleanly (used by the tray's Quit). Shuts down serve_forever, then clears
    any live preview still engaged on a mic. Safe to call more than once."""
    diagnostics.event("app.stopping", "stop_server called")
    if _httpd is not None:
        _httpd.shutdown()
    # Let go of the global shortcuts before we go: a quit that left them registered would keep
    # taking those keys from every other program with nothing left running to act on them.
    if _hotkeys is not None:
        _hotkeys.stop()
    _hk_release_held()
    _cleanup_preview_on_exit()
    diagnostics.stop("quit from the tray")


def main():
    headless = bool(os.environ.get("ACCESS_CASTER_NO_UI"))
    # In headless mode (ACCESS_CASTER_NO_UI, our tooling/background runs) the UI, watchdog and
    # announcer are all off. Otherwise this is the classic foreground launcher (the .bat /
    # python server.py): watchdog ON, so closing the page stops the server.
    server = _bring_up_server(open_browser=not headless,
                              watchdog=not headless,
                              announcer=not headless,
                              shortcuts=not headless)
    if server is None:
        return
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        server.shutdown()
    finally:
        # If a live preview was still engaged when the server stops, clear it off the mic.
        _cleanup_preview_on_exit()
        diagnostics.stop("serve_forever returned")


if __name__ == "__main__":
    main()
