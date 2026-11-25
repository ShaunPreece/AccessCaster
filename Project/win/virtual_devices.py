"""Windows' `virtual_devices`: RODE's driver service, Windows' audio endpoints and the default
output.

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
    announcer) wins, and the desk stays unbound until that stream goes. `park_default` moves
    Windows' default off the desk for the arrival; `usb_audio._virtual_devices_follow` retries
    when the bind is lost anyway.
  - When it does bind, its endpoints can come back DISABLED: it switches them on by writing
    Windows' audio endpoint registry, which Windows re-reads only on an audio device arrival, and
    after a mode change that write can land after the pass. `sync_endpoints` applies the service's own
    intent at once through the policy interface the Sound control panel uses, no elevation.
  - A program recording from the desk's Main Multitrack input (the Sound control panel's Recording
    tab does) holds the pin the service opens directly, so it reports connected and creates no
    Faders. `sync_endpoints` reports it.

It also puts Windows' default output back on the desk when a mode change has destroyed the
endpoint it was on.
"""
from __future__ import annotations

import contextlib
import ctypes
import re
import threading
import time
import winreg
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional

import comtypes
import comtypes.client
from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

import diagnostics
import meters
import write_guard
from win import core_audio as ca

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


# The service's status block says whether it has bound to the desk.
SEES_BINDING = True


def driver_state() -> dict:
    """Block layout: three little-endian dwords, connected / enabled / a once-a-second heartbeat,
    then ANSI strings. The desk string starts at byte 12 and is overwritten in place, so a shorter
    string leaves the tail of a longer one behind it; the version strings are found by their labels
    for the same reason."""
    st = {"installed": _installed()}
    block = _read_block()
    if block is None:
        return st
    connected = int.from_bytes(block[0:4], "little") == 1
    text = block[12:].decode("cp1252", "replace")
    firmware = re.search(r"irmware Version: ([\d.]+)", text)
    version = re.search(r"App Version: ([\d.]+)", text)
    st.update(running=True, connected=connected,
              enabled=int.from_bytes(block[4:8], "little") == 1,
              desk=text.split("\0", 1)[0].strip() if connected else "",
              firmware=firmware.group(1) if firmware else "",
              driver_version=version.group(1) if version else "")
    return st


# ---------------------------------------------------------------------------------------------
# What an unelevated process can see of the service, for the bind history.

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
    DECLARED, IN ORDER, because a COM vtable is positional; only the last two are called."""
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
    disabling an endpoint. An offline test is refused here (write_guard.refuse_in_tests)."""
    write_guard.refuse_in_tests("change Windows audio")
    return comtypes.client.CreateObject(CLSID_PolicyConfigClient, interface=IPolicyConfig)


@dataclass
class Endpoint:
    id: str
    name: str
    flow: str                       # "playback" or "recording"
    state: str                      # core_audio.STATE_NAMES
    virtual: bool                   # one of the driver's, as opposed to the desk's own USB audio
    desk: bool                      # the desk's own USB audio (Main, Chat, Secondary)


@contextlib.contextmanager
def _audio():
    """A COM apartment on this thread and Windows' endpoint enumerator, for one piece of work.
    Every caller here opens its own, because each runs on whatever thread asked."""
    with ca.apartment():
        yield ca.enumerator()


def _endpoints(enum) -> list:
    """Every endpoint Windows knows about, tagged by owner. Caller holds the COM apartment."""
    out = []
    for flow in (ca.RENDER, ca.CAPTURE):
        try:
            coll = enum.EnumAudioEndpoints(flow, ca.STATE_ALL)
            count = coll.GetCount()
        except Exception:
            continue
        for i in range(count):
            try:
                dev = coll.Item(i)
                store = dev.OpenPropertyStore(ca.STGM_READ)
                owner = ca.prop_string(store, PKEY_DeviceInterface_FriendlyName)
                enumerator = ca.prop_string(store, PKEY_Device_EnumeratorName)
                name = ca.friendly_name(dev)
                out.append(Endpoint(
                    id=dev.GetId(), name=name, flow=ca.FLOW_NAMES[flow],
                    state=ca.STATE_NAMES.get(dev.GetState(), "unknown"),
                    virtual=(enumerator.upper() == "ROOT" and meters.is_desk_endpoint(owner)),
                    desk=(enumerator.upper() == "USB" and meters.is_desk_endpoint(owner)),
                ))
            except Exception:
                continue
    return out


def _default_id(enum, role: int) -> str:
    try:
        return enum.GetDefaultAudioEndpoint(ca.RENDER, role).GetId()
    except Exception:
        return ""


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


# Leaving Expanded, the desk's Main output comes back as a playback endpoint a few seconds after the
# desk is back on the bus, so a parked default is held until it is there to move to.
DESK_MAIN_APPEARS_S = 10.0


# WINDOWS' OWN FALLBACK IS NOT THE USER'S CHOICE. When the endpoint the default was on goes away
# (the desk leaving Expanded from its touchscreen takes `System` with it, a dropout takes everything),
# Windows moves the default to whatever else is live, and that looks exactly like a device the user
# picked. The default is sampled on every endpoint change and every sync; a move away from a
# desk or driver endpoint that is no longer active is recorded as displaced, and `sync_endpoints`
# treats the displaced device as it treats a park. Any other move clears it.
_default_lock = threading.Lock()
_last_default = ""
_displaced = ""


def _note_default(enum) -> None:
    """Sample the console default and record whether its last move was Windows falling back.
    Caller holds the COM apartment."""
    global _last_default, _displaced
    with _default_lock:
        now = _default_id(enum, E_CONSOLE)
        if not now or now == _last_default:
            return
        prev = ({e.id: e for e in _endpoints(enum)}.get(_last_default) if _last_default else None)
        fell_back = prev is not None and (prev.desk or prev.virtual) and prev.state != "active"
        _displaced = now if fell_back else ""
        _last_default = now


def _desk_main(eps: list) -> Optional["Endpoint"]:
    return next((e for e in eps if e.flow == "playback" and e.state == "active"
                 and e.desk and "Main" in e.name), None)


def _system_output(eps: list) -> Optional["Endpoint"]:
    return next((e for e in eps if e.flow == "playback" and e.state == "active"
                 and e.virtual and e.name.startswith("System")), None)


def _wait_for(enum, pick, timeout_s: float, poll_s: float = 0.25) -> list:
    """Re-list endpoints until `pick(endpoints)` finds one, or the time is up. Returns the list."""
    at = time.monotonic()
    eps = _endpoints(enum)
    while pick(eps) is None and time.monotonic() - at < timeout_s:
        time.sleep(poll_s)
        eps = _endpoints(enum)
    return eps


def sync_endpoints(status, expect_connected, move_default: bool, parked: str) -> dict:
    """The Windows half of `virtual_devices.reconcile`, in a COM apartment of its own. A default
    Windows moved itself when its endpoint went away (`_note_default`) counts as parked."""
    out = {"enabled": [], "disabled": [], "faders_ready": 0, "faders_missing": False,
           "default_moved_to": ""}
    with _audio() as enum:
        _note_default(enum)
        parked = parked or _displaced
        policy = _policy()
        if expect_connected and status.connected:
            eps = _wait_for_faders(enum, FADERS_APPEAR_S)
        elif expect_connected is False and parked and move_default:
            eps = _wait_for(enum, _desk_main, DESK_MAIN_APPEARS_S)
        else:
            eps = _endpoints(enum)
        if status.running:
            # The service has already written what it wants into the registry; this applies it
            # now instead of at the next USB arrival. Only "disabled" and "active" are ours to
            # flip: "not present" and "unplugged" belong to the device.
            for e in eps:
                if not e.virtual:
                    continue
                if status.connected and e.state == "disabled":
                    policy.SetEndpointVisibility(e.id, True)
                    out["enabled"].append(e.name)
                elif not status.connected and e.state == "active":
                    policy.SetEndpointVisibility(e.id, False)
                    out["disabled"].append(e.name)
            # Counted AFTER the flips above, as what the operator now has, and "disabled" no
            # longer occurs here because it was just switched on.
            faders = [e for e in eps if _is_fader(e)]
            out["faders_ready"] = len(faders)
            out["faders_missing"] = bool(expect_connected and status.connected and not faders)
        if move_default:
            if status.connected and _system_output(eps) is None:
                # Bound before its System output exists (an arrival nobody parked for, read
                # straight after the bind), or listed before the flips above took effect.
                eps = _wait_for(enum, _system_output, FADERS_APPEAR_S)
            out["default_moved_to"] = _restore_default(enum, policy, eps, status.connected, parked,
                                                       standard=expect_connected is False)
    return out


def _restore_default(enum, policy, eps: list, connected: bool, parked: str = "",
                     standard: bool = False) -> str:
    """Put the default playback device back on the desk if a mode change took it away.

    ONLY WHEN IT WAS ON THE DESK OR IS DEAD, OR IS WHERE `park_default` PUT IT. A default that is
    alive and somewhere else was chosen by the user and stays. Once Expanded is on, the desk's Main
    output is renamed and its old endpoint stays listed and dead, so Windows falls back to whatever
    it can find, which on a laptop is the built-in speakers; RODE's own instruction is to select
    `System (RØDECaster)` then, and nothing else will do while the service is bound: it holds the
    render pin behind the desk's Main output, so a stream there opens and plays nothing. The desk's
    Main output is the target only while the service is not bound. A PARKED default stays parked
    until the service binds, because moving it onto the desk's Main output is what keeps the service from
    binding; in Standard (`standard`) there is no bind to wait for, and it goes back to the desk."""
    by_id = {e.id: e for e in eps}
    current = by_id.get(_default_id(enum, E_CONSOLE))
    is_parked = bool(parked) and current is not None and current.id == parked
    if is_parked and not connected and not standard:
        return ""
    if (current is not None and current.state == "active" and not is_parked
            and not (current.desk or current.virtual)):
        return ""
    target = _system_output(eps) if connected else _desk_main(eps)
    if target is None or (current is not None and current.id == target.id):
        return ""
    _set_default(policy, target.id)
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

def _park_target(eps: list) -> Optional[Endpoint]:
    return next((e for e in eps if e.flow == "playback" and e.state == "active"
                 and not e.desk and not e.virtual), None)


def _set_default(policy, endpoint_id: str) -> None:
    """Console and multimedia together; communications is a call app's choice."""
    policy.SetDefaultEndpoint(endpoint_id, E_CONSOLE)
    policy.SetDefaultEndpoint(endpoint_id, E_MULTIMEDIA)


def park_default() -> Optional[tuple]:
    with _audio() as enum:
        eps = _endpoints(enum)
        current = {e.id: e for e in eps}.get(_default_id(enum, E_CONSOLE))
        if (current is not None and current.state == "active"
                and not (current.desk or current.virtual)):
            return None
        target = _park_target(eps)
        if target is None:
            return None
        _set_default(_policy(), target.id)
        return target.id, target.name


def _is_desk_usb(enum, endpoint_id: str) -> bool:
    """Whether one endpoint is the desk's own USB audio, as `_endpoints` tags it. Not the driver's
    System: the default landing there means the service has bound."""
    try:
        store = enum.GetDevice(endpoint_id).OpenPropertyStore(ca.STGM_READ)
        return (ca.prop_string(store, PKEY_Device_EnumeratorName).upper() == "USB"
                and meters.is_desk_endpoint(ca.prop_string(store, PKEY_DeviceInterface_FriendlyName)))
    except Exception:
        return False


def hold_default(endpoint_id: str) -> bool:
    with _audio() as enum:
        now = _default_id(enum, E_CONSOLE)
        if now == endpoint_id or not _is_desk_usb(enum, now):
            return False
        _set_default(_policy(), endpoint_id)
        return True


def default_to_system(timeout_s: float) -> str:
    with _audio() as enum:
        policy = _policy()
        system = _system_output(_wait_for(enum, _system_output, timeout_s, poll_s=0.1))
        if system is None:
            return ""
        _set_default(policy, system.id)
        return system.name


def virtual_endpoint_counts() -> tuple:
    """Only what the driver exposes now. Windows keeps an entry for every endpoint it has ever
    seen, and a virtual device's endpoint lives only while its channel is on a fader, so every one
    ever assigned lingers as not-present. Counting those reports "11 of 12" on a working install.
    Filter by STATE, never by name: those names come back when the channel does."""
    with _audio() as enum:
        virtual = [e for e in _endpoints(enum) if e.virtual and e.state in ("active", "disabled")]
    return sum(1 for e in virtual if e.state == "active"), len(virtual)


# --- Watching Windows, rather than sampling it once ------------------------------------------
# RECONCILE ACTS ON ONE SAMPLE, AND THE DRIVER MOVES UNDER IT. A mode write is followed by one
# reconcile; if the service is still unbound at that instant, reconcile correctly switches the
# virtual endpoints OFF, and when the service binds a few seconds later nothing switches them back
# on: they stay disabled while the desk holds its channels on faders and the service reports
# connected.
#
# A second sample at a guessed delay cannot repair that. Windows already says when an endpoint
# moves, through IMMNotificationClient, and that is the signal to reconcile on, whenever it
# happens, mode write or not.
_WATCH_SETTLE_S = 1.5            # endpoint changes arrive in bursts; reconcile once the burst ends
_WATCH_POLL_S = 1.0              # how often an idle watcher looks at `stop`, so Quit unregisters


class _EndpointNotifier(comtypes.COMObject):
    """Sets an event when Windows reports any endpoint change. Does NO work of its own.

    The callbacks arrive on one of Windows' own audio threads, which must not be held: reconciling
    here would block the audio service while it talks to us about the devices we are changing.
    Every method takes `*args` because comtypes hands the raw `this` pointer to some builds and not
    others, and none of the arguments matter - the answer to all five is the same, go and look."""
    _com_interfaces_ = [ca.IMMNotificationClient]

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
    enum = client = None
    with ca.apartment():
        try:
            enum = ca.enumerator()
            client = _EndpointNotifier(wake)
            enum.RegisterEndpointNotificationCallback(client)
            diagnostics.event("virtual_devices.watch", "listening for endpoint changes")
            _note_default(enum)
            while not stop.is_set():
                if not wake.wait(_WATCH_POLL_S):
                    continue
                # Before the settle, while the endpoint the default left is still the last one seen.
                try:
                    _note_default(enum)
                except Exception as exc:    # a missed note costs one default change, not the watch
                    diagnostics.event("virtual_devices.note_failed", str(exc))
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
