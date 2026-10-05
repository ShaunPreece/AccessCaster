"""RODE's Virtual Device Driver on this computer: is it here, is it bound to the desk, and are its
endpoints switched on in Windows.

The driver is two parts. A kernel virtual audio device (enumerator ROOT, device name "RØDECaster")
owns the eleven endpoints, Fader1 to Fader9 and System as recording devices and System as the
playback device. A user-mode service, RODECasterVADService, bridges them to the desk's USB 1
multitrack stream and publishes its state in a shared-memory block any process may read. It binds
only to the desk's Expanded product ids, and it creates the Fader endpoints only when the desk is
sending every channel.

WHY THIS MODULE EXISTS. Three things go wrong between the service and Windows, and none of them
is visible from the desk:

  - The service cannot bind while anything in Windows is playing to the desk's own Main output.
    Its bind opens the KS render pin behind that output, the pin takes one user, and the
    service retries every 100 ms but never takes it back from the audio engine. Whatever has a
    stream there when the desk arrives (a screen reader on the default device, a browser, the
    announcer) wins, and the desk stays unbound until that stream goes. `park_output` moves
    Windows' default off the desk for the arrival; the server retries when it is lost anyway.
  - When it does bind, its endpoints can come back DISABLED: it switches them on by writing
    Windows' audio endpoint registry, which Windows re-reads only on an audio device arrival, and
    after a mode change that write can land after the pass. `reconcile` applies the service's own
    intent at once through the policy interface the Sound control panel uses, no elevation.
  - A program recording from the desk's Main Multitrack input (the Sound control panel's Recording
    tab does) holds the pin the service opens directly, so it reports connected and creates no
    Faders. `reconcile` says so in words.

It also puts Windows' default output back on the desk when a mode change has destroyed the
endpoint it was on.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import threading
import time
import winreg
from ctypes import wintypes
from dataclasses import dataclass, field, replace
from typing import Optional

import comtypes
import comtypes.client
from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

import diagnostics
import meters
from paths import data_path

# Names the service creates. The status block is what its tray icon reads (readable by everyone);
# the command block is what the tray writes (Enable, Latency), which the service mirrors into its
# admin-only registry key within a second. The app reads the first and never writes the second.
STATUS_MAPPING = r"Global\RODECaster_VAD{080F3A93-AC02-48d4-B1A3-0DF9178CC49A}"

# Registry key the MSI writes; present means installed, whatever the service is doing.
_DRIVER_KEY = r"SOFTWARE\RØDE Microphones\RØDECaster Virtual Device Driver"

FILE_MAP_READ = 0x0004
_BLOCK_BYTES = 512                  # the block is a page; everything it says is in the first part

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenFileMappingW.restype = wintypes.HANDLE
_k32.OpenFileMappingW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
_k32.MapViewOfFile.restype = wintypes.LPVOID
_k32.MapViewOfFile.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                               ctypes.c_size_t)
_k32.UnmapViewOfFile.argtypes = (wintypes.LPCVOID,)
_k32.CloseHandle.argtypes = (wintypes.HANDLE,)


@dataclass
class DriverStatus:
    installed: bool = False         # the MSI's registry key exists
    running: bool = False           # the service's status block exists
    connected: bool = False         # the service has bound to a desk
    enabled: bool = True            # the tray's Enable switch
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
            return "RODE's Virtual Devices driver is running but not connected to the unit."
        return f"RODE's Virtual Devices driver is connected to the {self.desk}."


def _installed() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _DRIVER_KEY):
            return True
    except OSError:
        return False


def _read_block() -> Optional[bytes]:
    h = _k32.OpenFileMappingW(FILE_MAP_READ, False, STATUS_MAPPING)
    if not h:
        return None
    try:
        view = _k32.MapViewOfFile(h, FILE_MAP_READ, 0, 0, 0)
        if not view:
            return None
        try:
            return ctypes.string_at(view, _BLOCK_BYTES)
        finally:
            _k32.UnmapViewOfFile(view)
    finally:
        _k32.CloseHandle(h)


def read_status() -> DriverStatus:
    """What the driver says about itself right now. Cheap: a registry probe and one page of
    shared memory, no COM.

    Block layout, measured against the service's own tray icon: three little-endian dwords,
    connected / enabled / a once-a-second heartbeat, then ANSI strings. The desk string starts
    at byte 12 and is overwritten in place, so a shorter string leaves the tail of a longer one
    behind it; the version strings are found by their labels for the same reason."""
    st = DriverStatus(installed=_installed())
    block = _read_block()
    if block is None:
        return st
    st.running = True
    st.connected = int.from_bytes(block[0:4], "little") == 1
    st.enabled = int.from_bytes(block[4:8], "little") == 1
    text = block[12:].decode("cp1252", "replace")
    st.desk = text.split("\0", 1)[0].strip() if st.connected else ""
    m = re.search(r"irmware Version: ([\d.]+)", text)
    st.firmware = m.group(1) if m else ""
    m = re.search(r"App Version: ([\d.]+)", text)
    st.driver_version = m.group(1) if m else ""
    return st


# The service binds 1.0 to 1.4 seconds after the desk arrives when the desk's Main output is free,
# and 0.2 seconds after that output is released when it was not. A wait longer than this only delays
# the retry (`server._virtual_devices_follow`).
CONNECT_WAIT_S = 6.0


def wait_connected(timeout_s: float = CONNECT_WAIT_S) -> DriverStatus:
    """Poll the status block until the service reports connected, or give up. Returns at once
    when the driver is absent, stopped or switched off: none of those change by waiting."""
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
# is the one party that is never restarted. It writes no log and its trace is compiled out, so the
# record is what an unelevated process can read: its pid, start time, thread and handle counts,
# private memory, and the whole status block. A missed attempt is then compared field by field with
# a bound one (`DevTools/bind_history.py`). The file is separate from the diagnostic log because
# a miss can be weeks apart from the bind it is compared with, and that log rolls in a session.

BIND_HISTORY_NAME = "bind_history.jsonl"
_BIND_HISTORY_MAX = 1024 * 1024        # then rolled to .previous: about a thousand attempts each
_SERVICE_EXE = "rodecastervadservice.exe"

# OpenProcess on a LocalSystem service is refused to an unelevated caller, even for limited
# query rights. The system process list carries the same counts and needs no access to the
# process, so it is read instead.
SystemProcessInformation = 5
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004

_ntdll = ctypes.WinDLL("ntdll")
_ntdll.NtQuerySystemInformation.restype = ctypes.c_ulong
_ntdll.NtQuerySystemInformation.argtypes = (ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                                            ctypes.POINTER(ctypes.c_ulong))

# SYSTEM_PROCESS_INFORMATION, 64-bit offsets.
_SPI_THREADS, _SPI_CREATE, _SPI_NAME_LEN, _SPI_NAME_PTR = 4, 32, 56, 64
_SPI_PID, _SPI_HANDLES, _SPI_PRIVATE = 80, 96, 200


def _service_process() -> dict:
    """The service's entry in the system process list, or {} when it is not running."""
    size = ctypes.c_ulong(1 << 20)
    while True:
        buf = ctypes.create_string_buffer(size.value)
        status = _ntdll.NtQuerySystemInformation(SystemProcessInformation, buf, size,
                                                 ctypes.byref(size))
        if status != STATUS_INFO_LENGTH_MISMATCH:
            break
        size = ctypes.c_ulong(size.value + (64 << 10))
    if status != 0:
        return {}
    base, at = ctypes.addressof(buf), 0
    u32 = lambda off: ctypes.c_uint32.from_address(base + at + off).value       # noqa: E731
    u64 = lambda off: ctypes.c_uint64.from_address(base + at + off).value       # noqa: E731
    while True:
        length = ctypes.c_uint16.from_address(base + at + _SPI_NAME_LEN).value
        name = ctypes.wstring_at(u64(_SPI_NAME_PTR), length // 2) if length else ""
        if name.lower() == _SERVICE_EXE:
            return {"pid": u64(_SPI_PID), "threads": u32(_SPI_THREADS),
                    "handles": u32(_SPI_HANDLES), "private_kb": u64(_SPI_PRIVATE) // 1024,
                    "started": round(u64(_SPI_CREATE) / 1e7 - 11644473600)}  # FILETIME to Unix
        step = u32(0)
        if not step:
            return {}
        at += step


def service_state() -> dict:
    """Everything an unelevated process can see of the service right now. The status block's
    third dword is a once-a-second heartbeat, so it is kept apart from the rest of the block,
    which is compared whole."""
    out = _service_process()
    block = _read_block()
    if block is not None:
        out["heartbeat"] = int.from_bytes(block[8:12], "little")
        out["block"] = (block[:8] + block[12:]).rstrip(b"\0").hex()
    return out


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
# Windows' side: the endpoints, their enabled state, and the default output.

# PKEY_Device_EnumeratorName: "ROOT" for the driver's endpoints, "USB" for the desk's own.
PKEY_Device_EnumeratorName = (GUID("{a45c254e-df1c-4efd-8020-67d146a850e0}"), 24)
# PKEY_DeviceInterface_FriendlyName: the owning device's name, "RØDECaster" for the driver.
PKEY_DeviceInterface_FriendlyName = (GUID("{026e516e-b814-414b-83cd-856d6fef4822}"), 2)

E_CONSOLE, E_MULTIMEDIA, E_COMMUNICATIONS = 0, 1, 2


class IPolicyConfig(IUnknown):
    """The interface the Sound control panel uses to enable an endpoint and to set the default
    device. Undocumented but stable since Vista, and it needs no elevation. EVERY METHOD IS
    DECLARED, IN ORDER, for the reason `meters.IAudioEndpointVolume` gives: a COM vtable is
    positional, and only the last two are called."""
    _iid_ = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetMixFormat", (["in"], wintypes.LPCWSTR), (["out"], ctypes.POINTER(ctypes.c_void_p))),
        COMMETHOD([], HRESULT, "GetDeviceFormat", (["in"], wintypes.LPCWSTR), (["in"], ctypes.c_int), (["out"], ctypes.POINTER(ctypes.c_void_p))),
        COMMETHOD([], HRESULT, "ResetDeviceFormat", (["in"], wintypes.LPCWSTR)),
        COMMETHOD([], HRESULT, "SetDeviceFormat", (["in"], wintypes.LPCWSTR), (["in"], ctypes.c_void_p), (["in"], ctypes.c_void_p)),
        COMMETHOD([], HRESULT, "GetProcessingPeriod", (["in"], wintypes.LPCWSTR), (["in"], ctypes.c_int), (["out"], ctypes.POINTER(ctypes.c_longlong)), (["out"], ctypes.POINTER(ctypes.c_longlong))),
        COMMETHOD([], HRESULT, "SetProcessingPeriod", (["in"], wintypes.LPCWSTR), (["in"], ctypes.POINTER(ctypes.c_longlong))),
        COMMETHOD([], HRESULT, "GetShareMode", (["in"], wintypes.LPCWSTR), (["out"], ctypes.POINTER(ctypes.c_void_p))),
        COMMETHOD([], HRESULT, "SetShareMode", (["in"], wintypes.LPCWSTR), (["in"], ctypes.c_void_p)),
        COMMETHOD([], HRESULT, "GetPropertyValue", (["in"], wintypes.LPCWSTR), (["in"], ctypes.c_void_p), (["out"], ctypes.POINTER(ctypes.c_void_p))),
        COMMETHOD([], HRESULT, "SetPropertyValue", (["in"], wintypes.LPCWSTR), (["in"], ctypes.c_void_p), (["in"], ctypes.c_void_p)),
        COMMETHOD([], HRESULT, "SetDefaultEndpoint", (["in"], wintypes.LPCWSTR), (["in"], ctypes.c_int)),
        COMMETHOD([], HRESULT, "SetEndpointVisibility", (["in"], wintypes.LPCWSTR), (["in"], wintypes.BOOL)),
    )


CLSID_PolicyConfigClient = GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}")


def _policy():
    """The object every Windows endpoint change goes through: the default device and enabling or
    disabling an endpoint. An offline test is refused here (meters.refuse_audio_change_in_tests)."""
    meters.refuse_audio_change_in_tests()
    return comtypes.client.CreateObject(CLSID_PolicyConfigClient, interface=IPolicyConfig)


def _prop(store, key) -> str:
    k = meters.PROPERTYKEY()
    k.fmtid, k.pid = key
    try:
        pv = store.GetValue(ctypes.byref(k))
        return pv.pwszVal or "" if pv.vt == 31 else ""
    except Exception:
        return ""


@dataclass
class Endpoint:
    id: str
    name: str
    flow: str                       # "playback" or "recording"
    state: str                      # meters.STATE_NAMES
    virtual: bool                   # one of the driver's, as opposed to the desk's own USB audio
    desk: bool                      # the desk's own USB audio (Main, Chat, Secondary)


def _endpoints(enum) -> list:
    """Every endpoint Windows knows about, tagged by owner. Caller holds the COM apartment."""
    out = []
    for flow in (meters.RENDER, meters.CAPTURE):
        try:
            coll = enum.EnumAudioEndpoints(flow, meters.STATE_ALL)
            count = coll.GetCount()
        except Exception:
            continue
        for i in range(count):
            try:
                dev = coll.Item(i)
                store = dev.OpenPropertyStore(meters.STGM_READ)
                owner = _prop(store, PKEY_DeviceInterface_FriendlyName)
                enumerator = _prop(store, PKEY_Device_EnumeratorName)
                name = meters._friendly_name(dev)
                out.append(Endpoint(
                    id=dev.GetId(), name=name, flow=meters.FLOW_NAMES[flow],
                    state=meters.STATE_NAMES.get(dev.GetState(), "unknown"),
                    virtual=(enumerator.upper() == "ROOT" and meters.is_desk_endpoint(owner)),
                    desk=(enumerator.upper() == "USB" and meters.is_desk_endpoint(owner)),
                ))
            except Exception:
                continue
    return out


def _default_id(enum, role: int) -> str:
    try:
        return enum.GetDefaultAudioEndpoint(meters.RENDER, role).GetId()
    except Exception:
        return ""


@dataclass
class Outcome:
    status: DriverStatus
    enabled: list = field(default_factory=list)      # names switched on
    disabled: list = field(default_factory=list)     # names switched off
    faders_ready: int = 0                            # Fader endpoints active once this is done
    system_ready: bool = False                       # the System playback endpoint is active
    faders_missing: bool = False                     # connected, yet the service made no Faders
    default_moved_to: str = ""                       # new default playback name, if it moved
    error: str = ""

    def sentence(self) -> str:
        """What the operator needs to hear, for the reply and the announcer. Empty when nothing.

        THE BIND ITSELF IS NOT SAID. RODE's driver raises its own Windows notification when it
        connects to the desk and when it lets go, and a screen reader reads it, so a line of ours
        for either is the same news twice. What is said is what that notification cannot know."""
        parts = []
        if self.faders_missing:
            parts.append(FADERS_MISSING)
        if self.default_moved_to:
            parts.append(f"Windows sound output is now {self.default_moved_to}.")
        if self.error:
            parts.append(self.error)
        return " ".join(parts)


# THE ONE FAILURE THE SERVICE REPORTS AS SUCCESS. It opens the desk's Main Multitrack capture pin
# directly, and a pin the audio engine already has open cannot be opened again, so while any
# program is recording from that input (the Sound control panel's Recording tab meters every
# input, which counts) the service says connected and creates no Fader endpoints at all.
FADERS_MISSING = ("RODE's driver is connected but has not created its fader inputs. Close anything "
                  "recording from the unit's Main Multitrack input, the Sound control panel "
                  "included, then set what this computer receives to just the mix and back "
                  "again.")     # the desk ignores a write of the value it holds, so "apply
                                # again" would re-enumerate nothing

# The service creates its endpoints within about three seconds of reporting connected, one at a
# time over roughly a second, so the set is read once it has stopped growing rather than at the
# first arrival, or the announcement counts one fader of nine.
FADERS_APPEAR_S = 10.0
FADERS_SETTLE_S = 1.5


def _is_fader(e: "Endpoint") -> bool:
    return e.virtual and e.name.startswith("Fader") and e.state in ("active", "disabled")


def _wait_for_faders(enum, timeout_s: float) -> list:
    """Re-list endpoints until the Fader set has held still for FADERS_SETTLE_S, or the time is
    up with none. Returns the list."""
    at = time.monotonic()
    eps, seen, held_since = _endpoints(enum), -1, at
    while time.monotonic() - at < timeout_s:
        n = sum(1 for e in eps if _is_fader(e))
        if n != seen:
            seen, held_since = n, time.monotonic()
        elif n and time.monotonic() - held_since >= FADERS_SETTLE_S:
            break
        time.sleep(0.5)
        eps = _endpoints(enum)
    return eps


def reconcile(expect_connected: Optional[bool] = None, move_default: bool = True,
              parked: str = "") -> Outcome:
    """Make Windows agree with the driver: its endpoints enabled while the service is connected
    and disabled while it is not, and the default playback device on the desk when a mode change
    has taken away the one it was on.

    `expect_connected` True waits for the service to bind first (a mode write has just landed).
    `parked` is the endpoint id `park_output` moved the default to, if any. None trusts the service's flag as it reads now. False means
    the desk is in Standard, where the service cannot be bound: it polls, so for a few seconds
    after the desk leaves Expanded its flag still says connected, and acting on that switches the
    endpoints ON on the way out. Safe to call at any time and from any thread; it
    opens its own COM apartment. Never raises: the desk's controls must not fail because Windows'
    audio service is having a moment.
    """
    status = wait_connected() if expect_connected else read_status()
    if expect_connected is False and status.connected:
        status = replace(status, connected=False, desk="")
    out = Outcome(status=status)
    comtypes.CoInitialize()
    try:
        enum = comtypes.client.CreateObject(meters.CLSID_MMDeviceEnumerator,
                                            interface=meters.IMMDeviceEnumerator)
        policy = _policy()
        eps = (_wait_for_faders(enum, FADERS_APPEAR_S) if expect_connected and status.connected
               else _endpoints(enum))
        if status.running:
            # The service has already written what it wants into the registry; this applies it
            # now instead of at the next USB arrival. Only "disabled" and "active" are ours to
            # flip: "not present" and "unplugged" belong to the device.
            for e in eps:
                if not e.virtual:
                    continue
                if status.connected and e.state == "disabled":
                    policy.SetEndpointVisibility(e.id, True)
                    out.enabled.append(e.name)
                elif not status.connected and e.state == "active":
                    policy.SetEndpointVisibility(e.id, False)
                    out.disabled.append(e.name)
            # Counted AFTER the flips above, as what the operator now has, and "disabled" no
            # longer occurs here because it was just switched on.
            faders = [e for e in eps if _is_fader(e)]
            out.faders_ready = len(faders)
            out.system_ready = any(e.virtual and e.flow == "playback" and e.name.startswith("System")
                                   and e.state in ("active", "disabled") for e in eps)
            out.faders_missing = bool(expect_connected and status.connected and not faders)
        if move_default:
            out.default_moved_to = _restore_default(enum, policy, eps, status.connected, parked,
                                                    standard=expect_connected is False)
    except Exception as exc:
        out.error = f"Could not update Windows' audio devices: {exc}"
    finally:
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass
    if out.enabled or out.disabled or out.faders_missing or out.default_moved_to or out.error:
        diagnostics.event("virtual_devices.reconcile", status.phrase(),
                          enabled=len(out.enabled), disabled=len(out.disabled),
                          faders=out.faders_ready, missing=out.faders_missing,
                          default=out.default_moved_to or "-", error=out.error or "-")
    return out


def _restore_default(enum, policy, eps: list, connected: bool, parked: str = "",
                     standard: bool = False) -> str:
    """Put the default playback device back on the desk if a mode change took it away.

    ONLY WHEN IT WAS ON THE DESK OR IS DEAD, OR IS WHERE `park_output` PUT IT. A default that is
    alive and somewhere else was chosen by the user and stays. Once Expanded is on, the desk's Main
    output is renamed and its old endpoint stays listed and dead, so Windows falls back to whatever
    it can find, which on a laptop is the built-in speakers; RODE's own instruction is to select
    `System (RØDECaster)` then, and that is the preference here, with the desk's Main output as the
    fallback when the driver's endpoint is not there. A PARKED default stays parked until the
    service binds, because moving it onto the desk's Main output is what keeps the service from
    binding; in Standard (`standard`) there is no bind to wait for, and it goes back to the desk.
    Console and multimedia roles move together; communications is left alone, because a call
    app's output is a choice this app knows nothing about."""
    by_id = {e.id: e for e in eps}
    current = by_id.get(_default_id(enum, E_CONSOLE))
    is_parked = bool(parked) and current is not None and current.id == parked
    if is_parked and not connected and not standard:
        return ""
    if (current is not None and current.state == "active" and not is_parked
            and not (current.desk or current.virtual)):
        return ""
    live_playback = [e for e in eps if e.flow == "playback" and e.state == "active"]
    target = None
    if connected:
        target = next((e for e in live_playback if e.virtual and e.name.startswith("System")), None)
    if target is None:
        target = next((e for e in live_playback if e.desk and "Main" in e.name), None)
    if target is None or (current is not None and current.id == target.id):
        return ""
    policy.SetDefaultEndpoint(target.id, E_CONSOLE)
    policy.SetDefaultEndpoint(target.id, E_MULTIMEDIA)
    return target.name


# --- Parking the default output while the service binds ----------------------------------------
# The service's bind needs the KS render pin behind the desk's own Main output, which takes one
# user (a shared stream there fails with AUDCLNT_E_DEVICE_IN_USE while the service holds it, and the
# service gets nothing while a stream holds it). Windows moves its default back onto the desk the
# moment the desk re-appears, and every program following the default reopens there, so the
# service loses that race whenever anything speaks in the first second. Parking the default on any
# other live playback device for the arrival keeps the pin free.
#
# NOT A FIX FOR EVERY SETUP. With no playback device besides the desk there is nowhere to park, and a
# program set to the desk's Main output by name ignores the default. Switching the desk's Main
# endpoint off in Windows is no way round it: the service checks that interface is enabled before it
# tries, and misses every arrival while it is off.

@dataclass
class Parking:
    id: str                         # the endpoint the default was moved to
    name: str


def _park_target(eps: list) -> Optional[Endpoint]:
    return next((e for e in eps if e.flow == "playback" and e.state == "active"
                 and not e.desk and not e.virtual), None)


def park_output() -> Optional[Parking]:
    """Move Windows' default playback (console and multimedia) off the desk onto another live
    device. None when the default is already elsewhere (nothing to do) or there is nowhere to park.
    Never raises."""
    comtypes.CoInitialize()
    try:
        enum = comtypes.client.CreateObject(meters.CLSID_MMDeviceEnumerator,
                                            interface=meters.IMMDeviceEnumerator)
        policy = _policy()
        eps = _endpoints(enum)
        current = {e.id: e for e in eps}.get(_default_id(enum, E_CONSOLE))
        if current is not None and current.state == "active" and not (current.desk or current.virtual):
            return None
        target = _park_target(eps)
        if target is None:
            return None
        policy.SetDefaultEndpoint(target.id, E_CONSOLE)
        policy.SetDefaultEndpoint(target.id, E_MULTIMEDIA)
        diagnostics.event("virtual_devices.park", target.name)
        return Parking(id=target.id, name=target.name)
    except Exception as exc:
        diagnostics.event("virtual_devices.park_failed", f"{exc}")
        return None
    finally:
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass


def keep_parked(parking: Parking) -> bool:
    """Put the default back on the parking device if Windows has moved it onto the desk, as it
    does when the desk re-appears. Returns whether it had to. Never raises."""
    comtypes.CoInitialize()
    try:
        enum = comtypes.client.CreateObject(meters.CLSID_MMDeviceEnumerator,
                                            interface=meters.IMMDeviceEnumerator)
        now = _default_id(enum, E_CONSOLE)
        if now == parking.id or not _is_desk_usb(enum, now):
            return False
        policy = _policy()
        policy.SetDefaultEndpoint(parking.id, E_CONSOLE)
        policy.SetDefaultEndpoint(parking.id, E_MULTIMEDIA)
        return True
    except Exception:
        return False
    finally:
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass


def default_to_system(timeout_s: float) -> str:
    """Once the service has bound, make `System (RØDECaster)` the default as soon as it appears,
    which is within about a second of the bind. Returns its name, or "" if it did not appear.
    Never raises."""
    comtypes.CoInitialize()
    try:
        enum = comtypes.client.CreateObject(meters.CLSID_MMDeviceEnumerator,
                                            interface=meters.IMMDeviceEnumerator)
        policy = _policy()
        at = time.monotonic()
        while time.monotonic() - at < timeout_s:
            system = next((e for e in _endpoints(enum) if e.virtual and e.flow == "playback"
                           and e.name.startswith("System") and e.state == "active"), None)
            if system is not None:
                policy.SetDefaultEndpoint(system.id, E_CONSOLE)
                policy.SetDefaultEndpoint(system.id, E_MULTIMEDIA)
                return system.name
            time.sleep(0.1)
        return ""
    except Exception:
        return ""
    finally:
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass


def _is_desk_usb(enum, endpoint_id: str) -> bool:
    """Whether one endpoint is the desk's own USB audio, as `_endpoints` tags it. Not the driver's
    System: the default landing there means the service has bound."""
    try:
        store = enum.GetDevice(endpoint_id).OpenPropertyStore(meters.STGM_READ)
        return (_prop(store, PKEY_Device_EnumeratorName).upper() == "USB"
                and meters.is_desk_endpoint(_prop(store, PKEY_DeviceInterface_FriendlyName)))
    except Exception:
        return False


def describe(expanded: Optional[bool] = None) -> dict:
    """For the page: the driver's state and what its endpoints are doing, as words. `expanded`
    False (the desk is in Standard) overrides a connected flag the service has not cleared yet,
    exactly as `reconcile` does."""
    st = read_status()
    if expanded is False and st.connected:
        st = replace(st, connected=False, desk="")
    virtual = []
    comtypes.CoInitialize()
    try:
        enum = comtypes.client.CreateObject(meters.CLSID_MMDeviceEnumerator,
                                            interface=meters.IMMDeviceEnumerator)
        # Only what the driver exposes now. Windows keeps an entry for every endpoint it has ever
        # seen, and a virtual device's endpoint lives only while its channel is on a fader, so every
        # one ever assigned lingers as not-present. Counting those reports "11 of 12" on a working
        # install. Filter by STATE, never by name: those names come back when the channel does.
        virtual = [e for e in _endpoints(enum) if e.virtual and e.state in ("active", "disabled")]
    except Exception:
        pass
    finally:
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass
    active = sum(1 for e in virtual if e.state == "active")
    text = st.phrase()
    if st.connected:
        text += (f" {active} of {len(virtual)} virtual devices are on." if virtual
                 else " No virtual devices are listed in Windows.")
    return {"installed": st.installed, "running": st.running, "connected": st.connected,
            "enabled": st.enabled, "driver_version": st.driver_version,
            "virtual_active": active, "virtual_total": len(virtual), "text": text}


# --- Watching Windows, rather than sampling it once ------------------------------------------
# RECONCILE ACTS ON ONE SAMPLE, AND THE DRIVER MOVES UNDER IT. A mode write is followed by one
# reconcile; if the service is still unbound at that instant, reconcile correctly switches the
# virtual endpoints OFF, and when the service binds a few seconds later nothing switches them back
# on. Measured: three playback endpoints left disabled with the desk holding all four channels on
# faders and the service reporting connected.
#
# So the repair cannot be a second sample at a guessed delay. Windows already says when an endpoint
# moves, through IMMNotificationClient, and that is the signal to reconcile on - whenever it
# happens, mode write or not.
_WATCH_SETTLE_S = 1.5            # endpoint changes arrive in bursts; reconcile once the burst ends
_WATCH_HEARTBEAT_S = 60.0        # a backstop, in case a change ever arrives without a callback


class _EndpointNotifier(comtypes.COMObject):
    """Sets an event when Windows reports any endpoint change. Does NO work of its own.

    The callbacks arrive on one of Windows' own audio threads, which must not be held: reconciling
    here would block the audio service while it talks to us about the devices we are changing.
    Every method takes `*args` because comtypes hands the raw `this` pointer to some builds and not
    others, and none of the arguments matter - the answer to all five is the same, go and look."""
    _com_interfaces_ = [meters.IMMNotificationClient]

    def __init__(self, wake: threading.Event):
        super().__init__()
        self._wake = wake

    def OnDeviceStateChanged(self, *args):
        self._wake.set()
        return 0

    def OnDeviceAdded(self, *args):
        self._wake.set()
        return 0

    def OnDeviceRemoved(self, *args):
        self._wake.set()
        return 0

    def OnDefaultDeviceChanged(self, *args):
        self._wake.set()
        return 0

    def OnPropertyValueChanged(self, *args):
        return 0


def watch_endpoints(on_change, stop: threading.Event) -> None:
    """Call `on_change` whenever Windows' audio endpoints move, until `stop` is set.

    Runs on its own thread with its own COM apartment. Bursts are coalesced: a change starts a
    quiet timer and `on_change` runs once the changes stop, so a mode switch that re-creates eleven
    endpoints costs one reconcile rather than eleven. Never raises - a repair path that can take the
    app down with it is worse than no repair path."""
    wake = threading.Event()
    comtypes.CoInitialize()
    enum = client = None
    try:
        enum = comtypes.client.CreateObject(meters.CLSID_MMDeviceEnumerator,
                                            interface=meters.IMMDeviceEnumerator)
        client = _EndpointNotifier(wake)
        enum.RegisterEndpointNotificationCallback(client)
        diagnostics.event("virtual_devices.watch", "listening for endpoint changes")
        while not stop.is_set():
            if not wake.wait(_WATCH_HEARTBEAT_S):
                continue
            # Let the burst finish: each re-arm pushes the settle point out again.
            while wake.is_set() and not stop.is_set():
                wake.clear()
                stop.wait(_WATCH_SETTLE_S)
            if stop.is_set():
                break
            try:
                on_change()
            except Exception as exc:
                diagnostics.event("virtual_devices.watch_failed", f"{exc}")
    except Exception as exc:
        diagnostics.event("virtual_devices.watch_failed", f"could not listen: {exc}")
    finally:
        try:
            if enum is not None and client is not None:
                enum.UnregisterEndpointNotificationCallback(client)
        except Exception:
            pass
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass
