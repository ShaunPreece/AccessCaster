"""AccessCaster - LEVEL METERING, read from Windows itself rather than from the desk.

WHY THIS FILE EXISTS, and why it is not protocol work. Metering over the RODECaster's USB CONTROL
channel is settled impossible: nothing meter-shaped is ever pushed, and the RODE app itself asks for
nothing in 37 seconds of capture. So a blind operator has had no way to answer the questions a
sighted one answers with a glance - is my mic actually live, am I clipping, is that guest's fader
doing anything.

The way in is that the desk is also a USB AUDIO INTERFACE. Windows already meters every audio
endpoint it has, and it will tell us the peak of any of them for free. So this reads the desk's own
audio streams (Main Stereo, Chat, Secondary, Multitrack) as Windows sees them.

WHY IAudioMeterInformation AND NOT A CAPTURE LIBRARY. The obvious route is to open the stream with
sounddevice or pyaudio and compute the peak ourselves. That means a new dependency, PortAudio, numpy
and a few tens of megabytes in the installer, plus a capture thread per stream. Windows already
computes exactly this number for its own volume UI, and `comtypes` is already a dependency, so the
whole thing is a COM call that costs microseconds and adds nothing to the build.

WHAT IT CANNOT DO, said plainly so nobody re-discovers it as a bug:
  - It is a PEAK per ENDPOINT, not per channel and not per fader. "Is anything coming out of Main
    Stereo" is answerable; "is Mic 2 live" is not, unless Mic 2 is alone on a stream.
  - An endpoint reads 0 unless its audio engine is running. A CAPTURE endpoint that nothing is
    recording from reads 0 no matter how loud the microphone is, so `Meter` opens a silent
    shared-mode client on capture endpoints to start the engine (see `_EngineWaker`).
    NOT A THEORETICAL NOTE: a declared `_EngineWaker` that nothing ever calls leaves the mic path
    silently broken, and it is invisible whenever the feature is only exercised over Remote Desktop,
    where no desk endpoint exists at
    all. Proof of the mechanism: holding the same stream open with `ffmpeg -f dshow` made it read 42
    percent while our own reader said 0.0.
  - It is a linear 0..1 amplitude, not a calibrated dBFS reading off the desk's own meters. The two
    will not agree exactly, and the app must not pretend otherwise.
"""
from __future__ import annotations

import ctypes
import math
import threading
import time
from ctypes import POINTER, c_float, c_int32, c_uint32, c_void_p, c_wchar_p

import comtypes
import comtypes.client
from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")

# Data flow and state constants from mmdeviceapi.h.
RENDER, CAPTURE, ALL_FLOW = 0, 1, 2
STATE_ACTIVE, STATE_ALL = 0x1, 0xF
CLSCTX_ALL = 0x17

# PKEY_Device_FriendlyName: the name Windows shows, e.g. "RODECaster Pro II Main Stereo".
PKEY_Device_FriendlyName = (GUID("{a45c254e-df1c-4efd-8020-67d146a850e0}"), 14)
STGM_READ = 0


class PROPVARIANT(ctypes.Structure):
    """Only the fields we read. A friendly name comes back as VT_LPWSTR (31), so the pointer member
    is all we need; anything else is reported as empty rather than mis-decoded."""
    _fields_ = [("vt", ctypes.c_ushort), ("wReserved1", ctypes.c_ushort),
                ("wReserved2", ctypes.c_ushort), ("wReserved3", ctypes.c_ushort),
                ("pwszVal", c_wchar_p), ("padding", ctypes.c_byte * 8)]


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", ctypes.c_ulong)]


class IPropertyStore(IUnknown):
    _iid_ = GUID("{886d8eeb-8cf2-4446-8d02-cdba1dbdcf99}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(c_uint32), "cProps")),
        COMMETHOD([], HRESULT, "GetAt", (["in"], c_uint32, "iProp"),
                  (["out"], POINTER(PROPERTYKEY), "pkey")),
        COMMETHOD([], HRESULT, "GetValue", (["in"], POINTER(PROPERTYKEY), "key"),
                  (["out"], POINTER(PROPVARIANT), "pv")),
    )


class IAudioMeterInformation(IUnknown):
    _iid_ = GUID("{C02216F6-8C67-4B5B-9D00-D008E73E0064}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetPeakValue", (["out"], POINTER(c_float), "pfPeak")),
        COMMETHOD([], HRESULT, "GetMeteringChannelCount",
                  (["out"], POINTER(c_uint32), "pnChannelCount")),
        COMMETHOD([], HRESULT, "GetChannelsPeakValues",
                  (["in"], c_uint32, "u32ChannelCount"),
                  (["in"], POINTER(c_float), "afPeakValues")),
    )


class IAudioClient(IUnknown):
    """Only Initialize/Start/Stop/GetMixFormat, and only to WAKE a capture endpoint.

    A capture endpoint's meter reads 0 while nothing is streaming from it, which would make an
    idle-looking desk indistinguishable from a silent one. Opening a client in shared mode starts
    the engine for that endpoint without taking exclusive use of it and without us ever reading a
    sample - we still get the level from the meter interface above."""
    _iid_ = GUID("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
    _methods_ = (
        COMMETHOD([], HRESULT, "Initialize", (["in"], c_uint32, "ShareMode"),
                  (["in"], c_uint32, "StreamFlags"), (["in"], ctypes.c_longlong, "hnsBufferDuration"),
                  (["in"], ctypes.c_longlong, "hnsPeriodicity"),
                  (["in"], c_void_p, "pFormat"), (["in"], POINTER(GUID), "AudioSessionGuid")),
        COMMETHOD([], HRESULT, "GetBufferSize", (["out"], POINTER(c_uint32), "pNumBufferFrames")),
        COMMETHOD([], HRESULT, "GetStreamLatency", (["out"], POINTER(ctypes.c_longlong), "phns")),
        COMMETHOD([], HRESULT, "GetCurrentPadding", (["out"], POINTER(c_uint32), "pNumPadding")),
        COMMETHOD([], HRESULT, "IsFormatSupported", (["in"], c_uint32, "ShareMode"),
                  (["in"], c_void_p, "pFormat"), (["out"], POINTER(c_void_p), "ppClosestMatch")),
        COMMETHOD([], HRESULT, "GetMixFormat", (["out"], POINTER(c_void_p), "ppDeviceFormat")),
        COMMETHOD([], HRESULT, "GetDevicePeriod",
                  (["out"], POINTER(ctypes.c_longlong), "phnsDefaultDevicePeriod"),
                  (["out"], POINTER(ctypes.c_longlong), "phnsMinimumDevicePeriod")),
        COMMETHOD([], HRESULT, "Start"),
        COMMETHOD([], HRESULT, "Stop"),
        COMMETHOD([], HRESULT, "Reset"),
        COMMETHOD([], HRESULT, "SetEventHandle", (["in"], c_void_p, "eventHandle")),
        COMMETHOD([], HRESULT, "GetService", (["in"], POINTER(GUID), "riid"),
                  (["out"], POINTER(POINTER(IUnknown)), "ppv")),
    )


class IAudioEndpointVolume(IUnknown):
    """Windows' own volume for the endpoint: read for the meter's ceiling, written only on request.

    It is here because it silently scales the meter, which produced a bug that could never have been
    noticed by reading the code. A microphone endpoint at -1.08 dB gives 10^(-1.08/20) = 0.883, so
    a signal at true full scale reports 0.883, a clip threshold of 0.99 is UNREACHABLE, and the
    clipping warning can never fire however hard the desk is driven. Measured twice at 8 dB apart,
    both times exactly 88.3 percent, which is the tell: a repeated identical peak is a ceiling, not
    a coincidence. At unity the same shout reads 100 percent and the warning fires.

    THE SETTER IS CALLED ONLY FROM A CONTROL THE USER OPERATES (server.action_set_input_level,
    a control the user operated), never on the app's own initiative. The distinction is the
    whole point: this level changes what EVERY application on the machine records, so the app reads
    it and adapts, and moves it only when told to.

    IT IS OFFERED IN dB, NOT PERCENT, and that is a decision rather than a convenience. On this
    endpoint 50 percent is -1.08 dB, unity is about 55 percent, 75 percent is +7.67 dB and 100
    percent is +30 dB, so the percentage tells you nothing about what it does. `SetMasterVolumeLevel`
    takes dB directly, which is also why "set it to exactly no change" is possible at all - dragging
    to "about 55 percent" cannot land on unity.

    EVERY METHOD IS DECLARED, IN ORDER, even the ones we do not use. A COM vtable is positional:
    leaving a gap does not omit a method, it makes every method after it call the wrong one. That
    is why GetVolumeRange, which is the last of the eighteen, needs the seventeen above it.
    """
    _iid_ = GUID("{5CDF2C82-841E-4546-9722-0CF74078229A}")
    _methods_ = (
        COMMETHOD([], HRESULT, "RegisterControlChangeNotify", (["in"], c_void_p, "pNotify")),
        COMMETHOD([], HRESULT, "UnregisterControlChangeNotify", (["in"], c_void_p, "pNotify")),
        COMMETHOD([], HRESULT, "GetChannelCount", (["out"], POINTER(c_uint32), "pnChannelCount")),
        COMMETHOD([], HRESULT, "SetMasterVolumeLevel", (["in"], c_float, "fLevelDB"),
                  (["in"], POINTER(GUID), "pguidEventContext")),
        COMMETHOD([], HRESULT, "SetMasterVolumeLevelScalar", (["in"], c_float, "fLevel"),
                  (["in"], POINTER(GUID), "pguidEventContext")),
        COMMETHOD([], HRESULT, "GetMasterVolumeLevel", (["out"], POINTER(c_float), "pfLevelDB")),
        COMMETHOD([], HRESULT, "GetMasterVolumeLevelScalar", (["out"], POINTER(c_float), "pfLevel")),
        COMMETHOD([], HRESULT, "SetChannelVolumeLevel", (["in"], c_uint32, "nChannel"),
                  (["in"], c_float, "fLevelDB"), (["in"], POINTER(GUID), "pguidEventContext")),
        COMMETHOD([], HRESULT, "SetChannelVolumeLevelScalar", (["in"], c_uint32, "nChannel"),
                  (["in"], c_float, "fLevel"), (["in"], POINTER(GUID), "pguidEventContext")),
        COMMETHOD([], HRESULT, "GetChannelVolumeLevel", (["in"], c_uint32, "nChannel"),
                  (["out"], POINTER(c_float), "pfLevelDB")),
        COMMETHOD([], HRESULT, "GetChannelVolumeLevelScalar", (["in"], c_uint32, "nChannel"),
                  (["out"], POINTER(c_float), "pfLevel")),
        COMMETHOD([], HRESULT, "SetMute", (["in"], c_int32, "bMute"),
                  (["in"], POINTER(GUID), "pguidEventContext")),
        COMMETHOD([], HRESULT, "GetMute", (["out"], POINTER(c_int32), "pbMute")),
        COMMETHOD([], HRESULT, "GetVolumeStepInfo", (["out"], POINTER(c_uint32), "pnStep"),
                  (["out"], POINTER(c_uint32), "pnStepCount")),
        COMMETHOD([], HRESULT, "VolumeStepUp", (["in"], POINTER(GUID), "pguidEventContext")),
        COMMETHOD([], HRESULT, "VolumeStepDown", (["in"], POINTER(GUID), "pguidEventContext")),
        COMMETHOD([], HRESULT, "QueryHardwareSupport", (["out"], POINTER(c_uint32), "pdwMask")),
        COMMETHOD([], HRESULT, "GetVolumeRange", (["out"], POINTER(c_float), "pfMin"),
                  (["out"], POINTER(c_float), "pfMax"), (["out"], POINTER(c_float), "pfIncr")),
    )


class IMMDevice(IUnknown):
    _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
    _methods_ = (
        COMMETHOD([], HRESULT, "Activate", (["in"], POINTER(GUID), "iid"),
                  (["in"], c_uint32, "dwClsCtx"), (["in"], c_void_p, "pActivationParams"),
                  (["out"], POINTER(POINTER(IUnknown)), "ppInterface")),
        COMMETHOD([], HRESULT, "OpenPropertyStore", (["in"], c_uint32, "stgmAccess"),
                  (["out"], POINTER(POINTER(IPropertyStore)), "ppProperties")),
        COMMETHOD([], HRESULT, "GetId", (["out"], POINTER(c_wchar_p), "ppstrId")),
        COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(c_uint32), "pdwState")),
    )


class IMMDeviceCollection(IUnknown):
    _iid_ = GUID("{0BD7A1BE-7A1A-44DB-8397-CC5392387B5E}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(c_uint32), "pcDevices")),
        COMMETHOD([], HRESULT, "Item", (["in"], c_uint32, "nDevice"),
                  (["out"], POINTER(POINTER(IMMDevice)), "ppDevice")),
    )


class IMMNotificationClient(IUnknown):
    """Windows' audio-endpoint change callback. Declared here with the rest of the MMDevice
    plumbing; `virtual_devices` implements it to hear when an endpoint is switched off.

    THE METHOD ORDER IS THE VTABLE, so it must stay exactly as mmdeviceapi.h declares it even
    though only the first three are of any interest here."""
    _iid_ = GUID("{7991EEC9-7E89-4D85-8390-6C703CEC60C0}")
    _methods_ = (
        COMMETHOD([], HRESULT, "OnDeviceStateChanged",
                  (["in"], c_wchar_p, "pwstrDeviceId"), (["in"], c_uint32, "dwNewState")),
        COMMETHOD([], HRESULT, "OnDeviceAdded", (["in"], c_wchar_p, "pwstrDeviceId")),
        COMMETHOD([], HRESULT, "OnDeviceRemoved", (["in"], c_wchar_p, "pwstrDeviceId")),
        COMMETHOD([], HRESULT, "OnDefaultDeviceChanged", (["in"], c_uint32, "flow"),
                  (["in"], c_uint32, "role"), (["in"], c_wchar_p, "pwstrDefaultDeviceId")),
        COMMETHOD([], HRESULT, "OnPropertyValueChanged", (["in"], c_wchar_p, "pwstrDeviceId"),
                  (["in"], PROPERTYKEY, "key")),
    )


class IMMDeviceEnumerator(IUnknown):
    _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    _methods_ = (
        COMMETHOD([], HRESULT, "EnumAudioEndpoints", (["in"], c_uint32, "dataFlow"),
                  (["in"], c_uint32, "dwStateMask"),
                  (["out"], POINTER(POINTER(IMMDeviceCollection)), "ppDevices")),
        COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint", (["in"], c_uint32, "dataFlow"),
                  (["in"], c_uint32, "role"), (["out"], POINTER(POINTER(IMMDevice)), "ppEndpoint")),
        COMMETHOD([], HRESULT, "GetDevice", (["in"], c_wchar_p, "pwstrId"),
                  (["out"], POINTER(POINTER(IMMDevice)), "ppDevice")),
        COMMETHOD([], HRESULT, "RegisterEndpointNotificationCallback",
                  (["in"], POINTER(IMMNotificationClient), "pClient")),
        COMMETHOD([], HRESULT, "UnregisterEndpointNotificationCallback",
                  (["in"], POINTER(IMMNotificationClient), "pClient")),
    )


STATE_NAMES = {0x1: "active", 0x2: "disabled", 0x4: "not present", 0x8: "unplugged"}
FLOW_NAMES = {RENDER: "playback", CAPTURE: "recording"}

# How we recognise the desk's own streams among everything else Windows has. Matched loosely on
# purpose: RODE spell the product with a slashed O in some places and not others, and the endpoint
# names carry a suffix ("Main Stereo", "Chat", "Secondary", "Main Multitrack") that we want to keep
# and show rather than normalise away.
DESK_HINTS = ("rodecaster", "rødecaster")


def is_desk_endpoint(name: str) -> bool:
    low = (name or "").lower()
    return any(h in low for h in DESK_HINTS)


def _remote_session() -> bool:
    """Are we in a Remote Desktop session? It matters more here than anywhere else in the app.

    Windows gives each session its own audio endpoints, and an RDP session sees only "Remote Audio":
    the RODECaster's four USB audio streams belong to the CONSOLE session and are invisible from a
    remote one. Every other feature in this app works identically over RDP because it talks to the
    desk over USB HID, which is session-independent. Metering is the single exception, so it has to
    say so rather than reporting a desk with no audio.

    SM_REMOTESESSION (0x1000) is the documented way to ask, and it is what Windows itself uses."""
    try:
        return bool(ctypes.windll.user32.GetSystemMetrics(0x1000))
    except Exception:
        return False


def _friendly_name(dev) -> str:
    try:
        store = dev.OpenPropertyStore(STGM_READ)
        key = PROPERTYKEY()
        key.fmtid, key.pid = PKEY_Device_FriendlyName
        pv = store.GetValue(ctypes.byref(key))
        return pv.pwszVal or ""
    except Exception:
        return ""


def list_endpoints(state_mask: int = STATE_ALL) -> list:
    """Every audio endpoint Windows knows about: id, name, direction, state.

    Returns [] rather than raising if the audio service is unavailable: a missing meter must never
    be able to take the app down with it."""
    out = []
    comtypes.CoInitialize()
    try:
        enum = comtypes.client.CreateObject(CLSID_MMDeviceEnumerator,
                                            interface=IMMDeviceEnumerator)
        # One failure must not empty the whole list. Endpoints come and go underneath an
        # enumeration - a Remote Desktop session's audio device does it constantly - so an
        # exception on one entry is a normal event, not a reason to report a machine with no sound
        # card. That distinction matters: "no endpoints" is a message this app shows the user.
        for flow in (RENDER, CAPTURE):
            try:
                coll = enum.EnumAudioEndpoints(flow, state_mask)
                count = coll.GetCount()
            except Exception:
                continue
            for i in range(count):
                try:
                    dev = coll.Item(i)
                    state = dev.GetState()
                    out.append({
                        "id": dev.GetId(),
                        "name": _friendly_name(dev),
                        "flow": FLOW_NAMES[flow],
                        "state": STATE_NAMES.get(state, f"unknown ({state})"),
                        "active": state == STATE_ACTIVE,
                    })
                except Exception:
                    continue
    except Exception:
        return out
    finally:
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass
    return out


def stream_label(name: str, flow: str) -> str:
    """A stream named by WHAT IT IS and WHICH WAY IT GOES, rather than by Windows' wording.

    Windows calls every input "Microphone", which means only "an audio stream coming in" - so the
    desk's main output mix is listed as "Microphone (RODECaster Pro II Main Stereo)" and the thing
    the PC SENDS the desk is listed as "Speakers (... Main Stereo)". Two nearly identical names for
    opposite directions, told apart by a prefix nobody would read as a direction. Turn up the wrong
    one of that pair and the level moved has no effect on the meter at all, because it is the
    playback half.

    The device model is dropped (every entry here is the same desk) and the direction is spelled
    out, so the list reads "Main Stereo, recording from the unit" against "Main Stereo, playback to
    the unit"."""
    inner = name
    if "(" in name and name.rstrip().endswith(")"):
        inner = name[name.index("(") + 1:name.rstrip().rindex(")")]
    for model in ("RODECaster Pro II ", "RODECaster Duo ", "RODECaster "):
        if inner.startswith(model):
            inner = inner[len(model):]
            break
    way = ("recording from the unit" if flow == FLOW_NAMES[CAPTURE]
           else "playback to the unit" if flow == FLOW_NAMES[RENDER] else flow)
    return f"{inner.strip()}, {way}"


def desk_streams() -> dict:
    """What the app can meter right now, and an honest reason when the answer is nothing.

    Three outcomes, and they are genuinely different problems with different fixes, so they must not
    all collapse into "no meters found":
      - streams found: the normal case.
      - none, and we are on Remote Desktop: the desk's audio belongs to the console session. Nothing
        is wrong with the desk or the app.
      - none, at the console: the desk's audio interfaces are not there (unplugged, driver, or the
        unit is in transfer mode, which takes the audio device away while it mounts as a drive)."""
    endpoints = list_endpoints()
    desk = [e for e in endpoints if is_desk_endpoint(e["name"])]
    active = [e for e in desk if e["active"]]
    if active:
        return {"ok": True, "streams": active, "reason": "",
                "all_desk": desk, "remote": _remote_session()}
    if _remote_session():
        return {"ok": False, "streams": [], "all_desk": desk, "remote": True,
                "reason": ("Level meters need the RODECaster's audio streams, and Windows gives "
                           "each Remote Desktop session its own audio devices: from a remote "
                           "session the unit's streams are not there to read. Everything else in "
                           "this app works over Remote Desktop because it talks to the unit over "
                           "USB directly. Run the app at the computer itself to use the meters.")}
    return {"ok": False, "streams": [], "all_desk": desk, "remote": False,
            "reason": ("Windows is not reporting any RODECaster audio streams. That usually means "
                       "the unit's USB audio is not connected, or the unit is in transfer mode: "
                       "while it is mounted as a drive its audio device disappears.")}


# How often a running Watcher re-reads the Windows endpoint volume. Two seconds is well inside the
# time it takes anyone to drag a slider and notice, and it is one cheap COM read.
CEILING_REFRESH_S = 2.0

# Below this, a ceiling is not believed and we fall back to 1.0. It exists because the obvious
# `max(0.05, ceiling)` guard was WRONG in a way an offline test caught immediately: a ceiling of 0
# (a failed volume read) produced a clip threshold of 0.0495, so a whisper would have been announced
# as "CLIPPING, turn it down". For a warning that interrupts a take, a MISS is much cheaper than a
# false alarm, so an untrustworthy ceiling degrades to "only call it clipping at real full scale".
MIN_TRUSTED_CEILING = 0.05


def trusted_ceiling(ceiling: float) -> float:
    """The ceiling to actually use. One place, so describe() and Watcher cannot drift apart."""
    try:
        c = float(ceiling)
    except (TypeError, ValueError):
        return 1.0
    return c if MIN_TRUSTED_CEILING < c <= 1.0 else 1.0

AUDCLNT_SHAREMODE_SHARED = 0
# 1 second, in 100-nanosecond units. Generous on purpose: in shared mode the buffer only has to be
# at least the device period, and we never read a single frame out of it, so a big buffer simply
# means Windows discards more before it wraps. Nothing here consumes the audio.
WAKE_BUFFER_100NS = 10_000_000


def _is_capture_endpoint(enum, endpoint_id: str) -> bool:
    """True if this endpoint is an INPUT. Decided by asking Windows which list it is in.

    Not inferred from the name: "Microphone (RODECaster Pro II Main Stereo)" and
    "Speakers (RODECaster Pro II Main Stereo)" differ only by a prefix Windows chose, and metering
    the wrong one of that pair is exactly the mistake that hid this bug for a session (the playback
    side was being metered and moved, but it was carrying the PC's own screen-reader speech, not the
    microphone)."""
    try:
        coll = enum.EnumAudioEndpoints(CAPTURE, STATE_ALL)
        for i in range(coll.GetCount()):
            try:
                if coll.Item(i).GetId() == endpoint_id:
                    return True
            except Exception:
                continue
    except Exception:
        return False
    return False


class _EngineWaker:
    """Hold a silent shared-mode capture client open so the endpoint's meter reports a level.

    We never read a frame from it: the point is only to make Windows run the audio engine for that
    endpoint, because IAudioMeterInformation reports 0 on an input nothing is capturing from. Shared
    mode is deliberate - exclusive mode would take the microphone away from every other application,
    which for a desk being used to record would be catastrophic.
    """

    def __init__(self, dev):
        self._client = None
        self._fmt = None
        client = dev.Activate(IAudioClient._iid_, CLSCTX_ALL, None).QueryInterface(IAudioClient)
        fmt = client.GetMixFormat()
        client.Initialize(AUDCLNT_SHAREMODE_SHARED, 0, WAKE_BUFFER_100NS, 0, fmt, None)
        client.Start()
        self._client, self._fmt = client, fmt
        # The engine takes a moment to spin up, and the first reads after Start() come back 0. Without
        # this a short sample() would report silence on a live microphone, which is the very failure
        # this class exists to remove.
        time.sleep(0.2)

    def close(self):
        if self._client is not None:
            try:
                self._client.Stop()
            except Exception:
                pass
            self._client = None
        if self._fmt is not None:
            try:
                ctypes.windll.ole32.CoTaskMemFree(self._fmt)
            except Exception:
                pass
            self._fmt = None


class Meter:
    """One endpoint's meter, held open for polling. Must be created and read on the SAME THREAD:
    COM apartments are per-thread, and an interface pointer does not travel between them.

    On a CAPTURE endpoint it also starts `_EngineWaker`, without which the peak is always 0.
    `wake_failed` says whether that could not be done, so a caller can tell "your microphone is
    silent" apart from "we could not start the stream that would let us hear it" - two sentences a
    blind operator must never have confused.
    """

    def __init__(self, endpoint_id: str):
        comtypes.CoInitialize()
        enum = comtypes.client.CreateObject(CLSID_MMDeviceEnumerator,
                                            interface=IMMDeviceEnumerator)
        dev = enum.GetDevice(endpoint_id)
        self.id = endpoint_id
        self.name = _friendly_name(dev)
        self._meter = dev.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None) \
                         .QueryInterface(IAudioMeterInformation)
        self.is_capture = _is_capture_endpoint(enum, endpoint_id)
        # What a TRUE full-scale signal will actually read on this endpoint, because Windows' own
        # endpoint volume scales the meter. 1.0 means the meter can reach the top; less than that and
        # every threshold has to come down with it or clipping is undetectable. See
        # IAudioEndpointVolume for the measurement that forced this.
        self.volume_db = 0.0
        self.ceiling = 1.0
        # WINDOWS' OWN MUTE on the endpoint, which is a different thing from a quiet input and has
        # to be said differently. It happens: both of a desk's recording endpoints
        # muted: every stream read 0.0, ffmpeg recorded six seconds of literal digital silence, and
        # nothing anywhere said why. "Your microphone is silent" would have sent the operator to
        # the desk to hunt a fault that was in Windows.
        self.muted = False
        self._vol = None
        try:
            self._vol = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None) \
                           .QueryInterface(IAudioEndpointVolume)
            self.refresh_ceiling()
        except Exception:
            pass          # no volume interface: assume unity, which is the old behaviour
        self.wake_failed = False
        self._waker = None
        if self.is_capture:
            try:
                self._waker = _EngineWaker(dev)
            except Exception:
                # Degrade to the old behaviour rather than failing the read: a meter must never be
                # able to take the app down, and a 0 with wake_failed set is still honest.
                self.wake_failed = True

    def refresh_ceiling(self) -> float:
        """Re-read the endpoint volume and recompute the ceiling. Returns it.

        It has to be re-read rather than cached at open: the Windows capture level is a live slider
        the user (or another application) can move at any moment, and a stale ceiling would silently
        move the clipping threshold to the wrong place - which is the same class of bug this whole
        ceiling business exists to fix. Cheap: one COM property read.
        """
        if self._vol is None:
            return self.ceiling
        try:
            self.volume_db = float(self._vol.GetMasterVolumeLevel())
            # Capped at 1.0: a BOOST cannot make the meter read past full scale, it just gets there
            # sooner, so the threshold must not be pushed above 0.99 by a positive dB.
            self.ceiling = min(1.0, 10.0 ** (self.volume_db / 20.0))
            self.muted = bool(self._vol.GetMute())
        except Exception:
            pass
        return self.ceiling

    def peak(self) -> float:
        try:
            return float(self._meter.GetPeakValue())
        except Exception:
            return 0.0

    def close(self):
        self._vol = None
        if self._waker is not None:
            self._waker.close()
            self._waker = None
        self._meter = None
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass


def cue(kind: str) -> None:
    """A short tone marking the start or end of something you are being asked to do.

    A CUE BEATS A SENTENCE for anything with a clock on it. "Talk now" has to be spoken, queued
    behind whatever the screen reader is already saying, and read at reading speed - by which time
    seconds of the take are gone. A tone arrives whole and at once.

    RISING for start, one low tone for stop, and both different from the clipping rasp, which is
    two LOW tones - three signals that must never be confused with each other.

    SYNCHRONOUS, unlike clip_alert, and that is deliberate - but not for the reason it first
    looks. The tone plays out of the computer's default output, which on a desk like this is the
    unit itself, and auto gain's isolated mix excludes that source, so it never reaches the
    measurement. It waits because somebody should not be talking over their own start signal.
    """
    tones = {"start": ((660, 90), (990, 130)), "stop": ((440, 220),)}.get(kind)
    if not tones:
        return
    try:
        import winsound              # noqa: PLC0415  (Windows only, and only when it fires)
        for freq, ms in tones:
            winsound.Beep(freq, ms)
    except Exception:
        pass                         # no sound device, or not Windows: the words still happen


def clip_alert() -> None:
    """A short rasp for clipping, played on a thread so the meter loop never waits for it.

    WHY A SOUND AT ALL. Clipping is the one thing on this tab you need to REACT to rather than
    think about, and a spoken sentence arrives a word at a time - by the time "clipping" has been
    said the take is already damaged. A noise arrives whole.

    Two short low tones rather than the Windows error sound: the system sounds are commonplace
    enough that the ear filters them out, and several of them are configured to nothing at all.
    `winsound.Beep` needs no asset, so nothing has to ship with the installer.

    IT PLAYS ON THE DEFAULT OUTPUT, which on this machine is the unit itself - so during a
    recording it goes into the recording. That is a real cost and the page says so; a warning you
    cannot hear in time is worth less than a mark on a take you were going to redo anyway."""
    def _beep():
        try:
            import winsound            # noqa: PLC0415  (Windows only, and only when it fires)
            winsound.Beep(320, 110)
            winsound.Beep(240, 160)
        except Exception:
            pass                       # no sound device, or not Windows: the speech still happens
    threading.Thread(target=_beep, daemon=True).start()


# The line between "silence" and "something is there". Not arbitrary: a peak this low is roughly
# -60 dBFS, which is the noise floor of a live mic in a quiet room, so anything above it means the
# channel is genuinely carrying something rather than idling.
SILENCE = 0.001


def level_note(volume_db: float) -> str:
    """A sentence about Windows' own level for this endpoint, or "" when it is close enough to unity.

    MEASURED ON THIS ENDPOINT, because the slider's percentage tells you nothing about
    what it does: 0% is -96 dB, 50% is -1.08 dB, about 55% is unity, 75% is +7.67 dB and 100% is
    +30 dB. So the top half of that slider is BOOST.

    It matters for a reason that has nothing to do with our meter. Setting a new recording device
    to about 75% is common advice, sound for a cheap laptop microphone that needs help and wrong for
    a desk that already delivers a properly levelled digital stream: at +7.67 dB, anything the desk
    sends above -7.67 dBFS clips in WINDOWS, so every recording made on this PC clips while the desk
    itself is nowhere near it. The fix is this slider, not the desk's gain, and nothing else in
    Windows says so.
    """
    if volume_db > 1.0:
        # Point at the app's own dB control rather than saying "set the slider to about 50
        # percent": "set it to no change" lands exactly on unity, which dragging a percentage
        # slider cannot.
        return (f"Windows is adding {volume_db:.1f} dB to this input, so it will clip in Windows "
                f"before the desk does. Use Set it to no change below, then set your gain on the "
                f"unit itself.")
    if volume_db < -3.0:
        return (f"Windows is taking {abs(volume_db):.1f} dB off this input, so everything reads "
                f"lower here than the desk is really sending.")
    return ""


# WHAT THE CONTROL OFFERS, which is deliberately NARROWER than what Windows allows.
#
# The driver does report a real range - measured, the RODECaster's capture endpoints say
# -96 to +30 dB in 1.5 dB steps, and its playback endpoints -96 to 0. (That corrects an earlier note
# of ours claiming the endpoints reported a range of "+0.00 to +0.00 with increment 0"; whatever
# produced that, it was not GetVolumeRange.) But -96 dB is not "quiet", it is OFF, and a control
# that can silently mute every recording on the machine is not one to hand to someone who cannot see
# what it did. The useful part of that range for a device that already delivers a properly levelled
# stream is the bit either side of unity, so the offered range is the driver's own, narrowed to
# this. The measured curve for Windows' percentage slider on this endpoint, for reference: 50% is
# -1.08 dB, about 55% is unity, 75% is +7.67 dB, 100% is +30 dB.
OFFERED_LEVEL_MIN_DB = -30.0
OFFERED_LEVEL_MAX_DB = 30.0

# What to say about an endpoint Windows cannot level at all. Worth its own sentence rather than a
# disabled control, because the person needs to know WHERE the volume went, not just that it is
# not here: on RODE's Virtual Devices output the level lives on the desk's fader, so the keyboard
# volume key does nothing and there is no software route back.
FIXED_LEVEL_NOTE = ("Windows cannot change this device's level. Use the channel's fader on the "
                    "unit, or this app's own level control.")


def _default_endpoint_name(flow: int) -> str:
    """Windows' default endpoint for one data flow, by friendly name, or "" if it cannot be read.
    COM is per-thread, so the initialise/uninitialise pair belongs here rather than at the caller."""
    comtypes.CoInitialize()
    try:
        enum = comtypes.client.CreateObject(CLSID_MMDeviceEnumerator, interface=IMMDeviceEnumerator)
        return _friendly_name(enum.GetDefaultAudioEndpoint(flow, 0))
    except Exception:
        return ""
    finally:
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass


def default_playback_name() -> str:
    """The Windows DEFAULT playback device's friendly name, or "" if it cannot be read.

    Needed because "which output does our speech use" has a silent answer: an unset choice means
    the system default, and on this machine the system default IS the desk. So announcements - and
    the screen reader, and everything else - travel down USB 1 into the mix, and into the
    recording. Nothing shows that until you play the recording back."""
    return _default_endpoint_name(RENDER)


def default_recording_name() -> str:
    """Windows' DEFAULT recording device's friendly name, or "" if it cannot be read.

    The meter defaults to this when it is one of the desk's, because it is the stream every other
    program on the computer records from - so it is the one whose level and mute actually decide
    whether a recording made here has any sound in it."""
    return _default_endpoint_name(CAPTURE)


def _volume_interface(endpoint_id: str):
    """(device, IAudioEndpointVolume) for one endpoint. COM is per-thread, so the caller owns the
    CoInitialize/CoUninitialize pair around this."""
    enum = comtypes.client.CreateObject(CLSID_MMDeviceEnumerator, interface=IMMDeviceEnumerator)
    dev = enum.GetDevice(endpoint_id)
    vol = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None) \
             .QueryInterface(IAudioEndpointVolume)
    return dev, vol


def _offered_range(vol) -> tuple:
    """(min dB, max dB, step, fixed) this control will offer: what the driver reports, narrowed to
    the sane band above. One helper, so the reader and the writer cannot disagree about what is
    allowed - which would show up as a control that accepts a value and then reports a different one.

    **`fixed` IS A DEGENERATE RANGE, NOT A MISSING ONE, AND THE TWO MUST NOT BE CONFLATED.** RODE's
    Virtual Devices endpoint `System (RØDECaster)` answers GetVolumeRange with min 0, max 0: it has
    no level control at all, the keyboard volume key does nothing to it, and SetMasterVolumeLevel
    refuses with "the parameter is incorrect". Falling back to the sane band there offers a slider
    that cannot move, which on a device whose ONLY volume control is a physical fader is worse than
    offering none. A driver that reports no range at all is the different case: nothing is known,
    so the band is still the honest answer.
    """
    lo, hi, step = OFFERED_LEVEL_MIN_DB, OFFERED_LEVEL_MAX_DB, 0.5
    try:
        r_lo, r_hi, r_step = vol.GetVolumeRange()
        if float(r_hi) <= float(r_lo):
            return float(r_lo), float(r_lo), 0.5, True
        lo = max(OFFERED_LEVEL_MIN_DB, float(r_lo))
        hi = min(OFFERED_LEVEL_MAX_DB, float(r_hi))
        if float(r_step) > 0:
            step = float(r_step)
    except Exception:
        pass                     # no range reported: the sane band is the answer
    return lo, hi, step, False


def endpoint_volume(endpoint_id: str) -> dict:
    """Windows' own recording/playback level for one endpoint, in dB, with the range it will take.

    dB rather than percent throughout, because the percentage is meaningless here: on this desk's
    capture endpoint 50 percent is -1.08 dB and unity is about 55 percent, so "set it to no change"
    is not a position anyone can find by dragging."""
    comtypes.CoInitialize()
    try:
        dev, vol = _volume_interface(endpoint_id)
        db = float(vol.GetMasterVolumeLevel())
        lo, hi, step, fixed = _offered_range(vol)
        muted = False
        try:
            muted = bool(vol.GetMute())
        except Exception:
            pass
        return {"ok": True, "id": endpoint_id, "name": _friendly_name(dev), "db": db,
                "min_db": lo, "max_db": hi, "step_db": step, "muted": muted,
                # SAY SO rather than leaving a dead slider. The caller needs to tell "this level is
                # at 0 dB" from "this device has no level", and only the second is worth a sentence.
                "fixed": fixed,
                "note": (FIXED_LEVEL_NOTE if fixed else level_note(db))}
    except Exception as exc:
        return {"ok": False, "message": f"Could not read the Windows level for that stream: {exc}"}
    finally:
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass


def set_endpoint_mute(endpoint_id: str, muted: bool) -> dict:
    """Mute or unmute the endpoint in WINDOWS, and read it back.

    Only ever from a control the user pressed, like the level setter beside it. It exists because
    a muted input is invisible: it reads exactly like a dead microphone, and the fix is in a
    Windows dialog rather than anywhere on the desk."""
    comtypes.CoInitialize()
    try:
        _dev, vol = _volume_interface(endpoint_id)
        vol.SetMute(1 if muted else 0, None)
        got = bool(vol.GetMute())
        return {"ok": got == bool(muted), "muted": got}
    except Exception as exc:
        return {"ok": False, "message": f"Could not change the Windows mute: {exc}"}
    finally:
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass


def set_endpoint_volume_db(endpoint_id: str, db: float) -> dict:
    """Set Windows' own level for one endpoint, in dB, and READ IT BACK.

    The read-back is not ceremony. This desk's `... Chat` input refuses to move from -1.08 dB, by dB
    and by scalar, SILENTLY - no error, the value simply does not change (measured, and
    it looks driver-enforced rather than a fault of ours). Without a read-back the app would report
    a change that never happened, on a control the user cannot see."""
    comtypes.CoInitialize()
    try:
        _dev, vol = _volume_interface(endpoint_id)
        lo, hi, _step, fixed = _offered_range(vol)
        if fixed:
            # REFUSE IN OUR OWN WORDS. Letting it through returns the driver's "the parameter is
            # incorrect", which names nothing the person can act on and reads like a fault here.
            return {"ok": False, "fixed": True, "message": FIXED_LEVEL_NOTE}
        want = max(lo, min(hi, float(db)))
        vol.SetMasterVolumeLevel(c_float(want), None)
        got = float(vol.GetMasterVolumeLevel())
        return {"ok": abs(got - want) < 0.2, "db": got, "wanted": want,
                "note": level_note(got)}
    except Exception as exc:
        return {"ok": False, "message": f"Could not set the Windows level: {exc}"}
    finally:
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass


def dbfs(peak: float) -> str:
    """A peak said in dBFS, which is the unit everything else about levels is said in.

    NEVER A PERCENTAGE, which is actively misleading rather than merely unhelpful: the meter
    reports LINEAR AMPLITUDE, so 50 percent is -6 dBFS, already the top of the healthy range, while
    "about half" sounds like there is plenty of room left. Every piece of advice about levels, the
    desk's own meters and the Windows level control on this same page are all in dB.

    Spoken, not written: "minus 6" rather than "-6", because a screen reader reads a bare hyphen
    unreliably and "dash six" is not a level."""
    if peak <= 0:
        return "silence"
    db = 20.0 * math.log10(peak)
    if db <= -60:
        return "below minus 60 dB"
    return (f"minus {abs(db):.0f} dB" if db < -0.5
            else "0 dB" if db < 0.5 else f"plus {db:.0f} dB")


def describe(peak: float, active_fraction: float, name: str, wake_failed: bool = False,
             ceiling: float = 1.0, volume_db: float = 0.0, muted: bool = False,
             clip_at: float = 0.99) -> str:
    """Turn a peak into the sentence a blind operator actually wants, which is not a number.

    The number is included but never leads: "minus 6 dB" tells you nothing unless you already know
    what a good number looks like. What matters is the JUDGEMENT - is it live, is it too quiet to
    use, is it about to clip - and the thresholds are chosen to match how the desk's own meters are
    read: green up to about -18 dBFS, amber to -6, red above that."""
    at = dbfs(peak)
    if muted:
        # A MUTED ENDPOINT IS NOT A QUIET MICROPHONE, and this is the sentence that would have
        # saves an hour: with a desk's recording endpoints muted in Windows, every stream reads
        # 0.0 and a capture records digital silence, and "silent, nothing is coming through" sends
        # the operator to the desk hunting a fault that is not there. It goes FIRST because a muted
        # input reads 0 whatever else is true of it.
        return f"{name}: MUTED IN WINDOWS. Nothing recorded from it will have any sound."
    if peak <= SILENCE and wake_failed:
        # NOT the same sentence as silence, and the difference matters more than it looks: telling
        # someone their microphone is dead when it is fine sends them hunting a fault in the desk.
        return (f"{name}: no level can be read. Windows only reports an input's level while "
                f"something is recording from it, and that could not be started here.")
    if peak <= SILENCE:
        return f"{name}: silent. Nothing is coming through."
    # Scaled by the endpoint's own ceiling. At a -1.08 dB capture level a true full-scale signal
    # reads 0.883, so a fixed 0.99 here means the one warning a recording cannot recover from can
    # NEVER be spoken. Only the clip line is scaled: the healthy / strong / hot boundaries below are
    # judgements by ear and belong to the user, not to this code.
    if peak >= clip_at * trusted_ceiling(ceiling):
        # Say WHICH control is at fault. "Turn it down" sends the operator to the desk's gain, and if
        # Windows is boosting the input that is the wrong knob entirely - the desk could be perfectly
        # set and the clipping still happen, so turning it down would only make the desk quiet.
        if volume_db > 1.0:
            return (f"{name}: CLIPPING at {at}, but Windows is adding {volume_db:.1f} dB to this "
                    f"input. Use Set it to no change below before touching the desk.")
        return f"{name}: CLIPPING at {at}. Turn it down."
    if peak >= 0.89:                      # about -1 dBFS
        return f"{name}: very hot, {at}, close to clipping."
    if peak >= 0.5:                       # about -6 dBFS
        return f"{name}: strong, peaking at {at}."
    if peak >= 0.126:                     # about -18 dBFS
        return f"{name}: healthy, peaking at {at}."
    if active_fraction > 0.2:
        return f"{name}: live but quiet, peaking at only {at}. It would record thin."
    return f"{name}: almost nothing, peaking at {at}."


def verdict(peak: float, active_fraction: float, volume_db: float = 0.0,
            ceiling: float = 1.0, clip_at: float = 0.99) -> str:
    """The ADVICE at the end of a check: is this usable, and what should you turn if it is not.

    Separate from `describe`, which reports. This decides. A level reading is only half an answer
    for someone who cannot see a meter - "peaking at minus 3 dB" still leaves you working out
    whether that is fine. The target is the standard one: peaks between -12 and -6 dBFS.

    IT NAMES THE RIGHT KNOB. If Windows is boosting the input, the desk's gain is the wrong control
    entirely - the desk could be set perfectly and the clipping would still happen, so turning it
    down would only make the desk quiet while the clipping continued.
    """
    boosted = volume_db > 1.0
    down = (f"Turn down the Windows level above, which is adding {volume_db:.1f} dB to this input"
            if boosted else "Turn the gain down on the unit")
    up = ("Turn up the gain on the unit" if not boosted
          else "Turn up the gain on the unit rather than the Windows level")
    if peak <= SILENCE:
        return "Nothing came through at all."
    if peak >= clip_at * trusted_ceiling(ceiling):
        return f"Audio is CLIPPING. {down}."
    if peak >= 0.89:                      # -1 dBFS
        return f"Audio is very close to clipping. {down}."
    if peak >= 0.5:                       # -6 dBFS
        return f"Audio is usable but hot. {down} a little to be safe."
    if peak >= 0.126:                     # -18 dBFS
        return "Audio is good."
    if active_fraction > 0.2:
        return f"Audio is too quiet to record well. {up}."
    return f"Almost nothing came through. Check the channel is live, then {up[0].lower()}{up[1:]}."


class Watcher:
    """A background thread that watches one stream and SPEAKS when something needs saying.

    THE DESIGN QUESTION HERE IS NOT TECHNICAL. A meter is a glanceable thing; speech is not. A
    sighted operator reads a meter fifty times a minute and ignores it the rest of the time, and no
    spoken equivalent can do that - so this speaks only on the two things worth interrupting for,
    and everything else is on request:

      CLIPPING          - the one a recording cannot recover from. Spoken as soon as it happens,
                          then held for `clip_gap` seconds so a hot passage is one warning, not
                          forty. Default ON.
      WENT SILENT/LIVE  - the fault nobody notices until the take is over: a mic that was muted,
                          unplugged or faded down. Spoken only after `silence_after` seconds of
                          nothing, so a pause for breath is not an event. Default ON.
      THE LEVEL ITSELF  - spoken every `speak_every` seconds. Default OFF, deliberately: a running
                          commentary on a number is unusable while you are trying to talk, and the
                          on-request check is the better tool for the same job.

    Every threshold is a setting rather than a constant, because these are judgements made by ear
    and they belong to the user."""

    def __init__(self, endpoint_id: str, say, *, clip_at=0.99, clip_gap=4.0,
                 warn_silence=True, silence_after=5.0, speak_every=0.0, poll=0.05,
                 clip_sound=True, scale=1.0, label=""):
        self.endpoint_id = endpoint_id
        self._say = say
        # SCALE AND LABEL EXIST FOR THE PER-CHANNEL CHECK (server.action_channel_level_check), where
        # the stream being metered is not the thing being measured.
        #
        # scale: the channel is heard through a temporary Chat mix, and that cross-point is
        # POST-FADER, so a fader at -6 dB halves every reading. Dividing by the cross-point level
        # puts every number back into CHANNEL terms - the spoken level, the peak the summary
        # reports, and the clip threshold alike. Without it a check would contradict auto gain,
        # which corrects for the same thing, and riding the fader would move a number that is
        # supposed to be about the microphone. Clipping is compared against the corrected value on
        # purpose: on a microphone the clipping that matters happens in the preamp, ahead of the
        # fader.
        #
        # label: what to CALL it out loud. Everything spoken here uses self.name, which is the
        # Windows endpoint name - "Microphone (RODECaster Pro II Chat) clipping" while you are
        # checking Mic 1 names the wrong thing entirely.
        self.scale = max(0.05, float(scale or 1.0))
        self.label = str(label or "")
        self.clip_sound = bool(clip_sound)
        self.clip_at = float(clip_at)
        self.clip_gap = float(clip_gap)
        self.warn_silence = bool(warn_silence)
        self.silence_after = float(silence_after)
        self.speak_every = float(speak_every)
        self.poll = float(poll)
        self._stop = threading.Event()
        self._thread = None
        self.error = ""
        # Live readings, so the page can show a meter without a second poller of its own.
        self.peak = 0.0
        self.peak_hold = 0.0
        # The TRUE maximum over the whole run, which peak_hold cannot give because it decays like a
        # real meter. It is what a check reports at the end: "the loudest it got".
        self.peak_max = 0.0
        self.name = ""
        # Set once the thread has opened the stream: True means this is an INPUT whose engine we
        # could not start, so a reading of 0 says nothing about the microphone.
        self.wake_failed = False
        # Also set once open: what a full-scale signal can actually read here, and the Windows
        # endpoint level that decides it. Worth surfacing, because a level the user has never seen
        # changes both this meter and every recording made on this PC.
        self.ceiling = 1.0
        self.volume_db = 0.0
        # Windows' own mute on the endpoint, followed live like the ceiling: a mute switched on
        # mid-run turns every reading into 0, and a summary must not call that "silent".
        self.muted = False

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # Give the thread a moment to fail loudly (a bad endpoint id, no audio service) so the
        # caller can report the real reason instead of a cheerful "started".
        time.sleep(0.3)
        return not self.error

    def stop(self):
        self._stop.set()
        t = self._thread
        if t:
            t.join(timeout=2.0)
        self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self):
        m = None
        try:
            m = Meter(self.endpoint_id)
            self.name = self.label or m.name
            self.wake_failed = m.wake_failed
            self.ceiling = m.ceiling
            self.volume_db = m.volume_db
            self.muted = m.muted
            # The threshold that actually gets spoken, scaled to what this endpoint can reach. It is
            # recomputed in the loop below whenever the endpoint volume is re-read.
            clip_level = self.clip_at * trusted_ceiling(m.ceiling)
            last_ceiling = time.time()
        except Exception as exc:
            self.error = f"Could not open that stream: {exc}"
            return
        last_clip = 0.0
        last_level = time.time()
        last_sound = time.time()
        was_silent = False
        try:
            while not self._stop.is_set():
                v = m.peak()
                if self.scale != 1.0:
                    # Capped at full scale: a corrected reading is what the channel is doing, and
                    # nothing can be louder than full scale however far the fader is down.
                    v = min(1.0, v / self.scale)
                self.peak = v
                self.peak_hold = max(self.peak_hold * 0.995, v)   # a slow decay, like a real meter
                self.peak_max = max(self.peak_max, v)
                now = time.time()

                # Follow the Windows capture level while we run. Moving that slider changes what a
                # full-scale signal reads, so a threshold fixed at open would drift out of place.
                if (now - last_ceiling) >= CEILING_REFRESH_S:
                    last_ceiling = now
                    self.ceiling = m.refresh_ceiling()
                    self.volume_db = m.volume_db
                    self.muted = m.muted
                    clip_level = self.clip_at * trusted_ceiling(self.ceiling)

                if v >= clip_level and (now - last_clip) >= self.clip_gap:
                    last_clip = now
                    # THE SOUND FIRST, then the words. Clipping is the one thing here you need to
                    # react to rather than think about, and a rasp arrives whole while a spoken
                    # sentence arrives a word at a time. The speech still
                    # follows, because a noise on its own does not say WHICH stream.
                    if self.clip_sound:
                        clip_alert()
                    self._speak(f"{self.name} clipping")

                if v > SILENCE:
                    if was_silent and self.warn_silence:
                        was_silent = False
                        self._speak(f"{self.name} is live again")
                    last_sound = now
                elif (self.warn_silence and not was_silent and not self.wake_failed
                        and (now - last_sound) >= self.silence_after):
                    # `not self.wake_failed` is the point here: on an input whose engine we could not
                    # start, the reading is 0 forever, and announcing "has gone silent" would be a
                    # false alarm repeated every few seconds while the mic was working perfectly.
                    was_silent = True
                    self._speak(f"{self.name} has gone silent")

                if self.speak_every > 0 and (now - last_level) >= self.speak_every:
                    last_level = now
                    # In dB, like everything else about levels. A bare number every second and a
                    # half is what you ride a fader by, so it stays short: no name, no units past
                    # the "dB" itself.
                    self._speak(dbfs(self.peak_hold))

                self._stop.wait(self.poll)
        except Exception as exc:
            # A FAILURE PART-WAY THROUGH USED TO BE SILENT: the thread died, `running` went False,
            # and self.error stayed empty - so a check that broke ten seconds in reported the peak
            # it had reached as if the run had finished, and a channel that fell over read as a
            # QUIET channel. It happens when a run's watcher dies and the next press
            # started a second run instead of stopping the first.
            self.error = f"The meter stopped part-way through: {exc}"
        finally:
            m.close()

    def _speak(self, text: str):
        try:
            self._say(text)
        except Exception:
            pass          # a meter must never be able to take the announcer, or the app, down
