"""AccessCaster - level metering, read from Windows rather than from the desk.

The desk publishes no meter values over its USB control channel, but it is also a USB audio
interface, and Windows meters every audio endpoint. So this reads the peak of the desk's own streams
(Main Stereo, Chat, Secondary, Multitrack) through IAudioMeterInformation: one COM call, using the
`comtypes` dependency the app already has, where a capture library would add PortAudio, numpy and a
capture thread per stream.

Limits:
  - It is a PEAK per ENDPOINT, not per channel. "Is Mic 2 live" is answerable only when Mic 2 is
    alone on a stream (the per-channel check builds such a mix).
  - A capture endpoint reads 0 unless something is recording from it, so `Meter` opens a silent
    shared-mode client to start its engine (`_EngineWaker`).
  - It is linear 0..1 amplitude, not a calibrated reading of the desk's own meters, and will not
    agree with them exactly.
"""
from __future__ import annotations

import ctypes
import math
import os
import threading
import time
from ctypes import POINTER, c_float, c_int32, c_uint32, c_void_p, c_wchar_p

import comtypes
import comtypes.client
from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

# An offline test never changes Windows' audio: the same rule, under the same two flags, that
# rcp_hid applies to desk writes (DevTools/_bootstrap sets them by script name). Moving the default
# output, enabling an endpoint or setting a level or mute moves the screen reader's speech on the
# machine running the test.
_NO_AUDIO_CHANGE = bool(os.environ.get("ACCESSCASTER_NO_DEVICE_WRITE")) and not os.environ.get(
    "ACCESSCASTER_ALLOW_DEVICE_WRITE")


def refuse_audio_change_in_tests() -> None:
    """Raise if this process is an offline test. Called before every Windows audio write."""
    if _NO_AUDIO_CHANGE:
        raise RuntimeError(
            "REFUSING TO CHANGE WINDOWS AUDIO: this is an offline test "
            "(ACCESSCASTER_NO_DEVICE_WRITE). Stub the call, or set ACCESSCASTER_ALLOW_DEVICE_WRITE "
            "if it is a live suite.")


CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")

# Data flow and state constants from mmdeviceapi.h.
RENDER, CAPTURE = 0, 1
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
    """Used only to wake a capture endpoint (`_EngineWaker`): Initialize, Start, Stop and
    GetMixFormat. The rest are declared because a COM vtable is positional."""
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
    """Windows' own level for an endpoint: read for the meter's ceiling, written only from a
    control the user operates (it changes what every application on the machine records).

    It scales the meter: at -1.08 dB a true full-scale signal reads 10^(-1.08/20) = 0.883, so a
    fixed clip threshold of 0.99 is unreachable. See `Meter.refresh_ceiling`.

    Every method is declared, in order, even the unused ones: a COM vtable is positional, so a gap
    makes every later method call the wrong slot.
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
    """Windows' audio-endpoint change callback, implemented by `virtual_devices`. The method
    order is the vtable and must match mmdeviceapi.h."""
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

# The desk's own streams, matched by substring: RODE spell the product with a slashed O in some
# endpoint names and not others.
DESK_HINTS = ("rodecaster", "rødecaster")


def is_desk_endpoint(name: str) -> bool:
    low = (name or "").lower()
    return any(h in low for h in DESK_HINTS)


def _remote_session() -> bool:
    """True in a Remote Desktop session (SM_REMOTESESSION). Windows gives each session its own
    audio endpoints, so the desk's streams belong to the console session and a remote one cannot
    meter them. Every other feature talks HID, which is session-independent."""
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
    """Every audio endpoint Windows knows about: id, name, direction, state. Never raises."""
    out = []
    comtypes.CoInitialize()
    try:
        enum = comtypes.client.CreateObject(CLSID_MMDeviceEnumerator,
                                            interface=IMMDeviceEnumerator)
        # Endpoints come and go during an enumeration (an RDP session's audio device does so
        # constantly), so one failing entry is skipped rather than emptying the list.
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
    """A stream named by what it is and which way it goes: "Main Stereo, recording from the unit".

    Windows calls the desk's output mix "Microphone (RODECaster Pro II Main Stereo)" and what the PC
    sends it "Speakers (... Main Stereo)": opposite directions told apart only by a prefix. The model
    is dropped, since every entry is the same desk."""
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
    """What the app can meter right now. With nothing to meter, the reason distinguishes a Remote
    Desktop session (nothing is wrong) from a console with no desk audio (unplugged, or the
    driver). Transfer mode leaves the desk's audio devices in place."""
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
                       "the unit's USB audio is not connected.")}


# How often a running Watcher re-reads the Windows endpoint volume.
CEILING_REFRESH_S = 2.0

# A ceiling at or below this is not believed and becomes 1.0. Clamping it up instead (`max(0.05,
# c)`) turns a failed volume read into a clip threshold of 0.05, announcing a whisper as clipping;
# for a warning that interrupts a take, a miss is cheaper than a false alarm.
MIN_TRUSTED_CEILING = 0.05


def trusted_ceiling(ceiling: float) -> float:
    """The ceiling to use. Shared by describe, verdict and Watcher so they cannot disagree."""
    try:
        c = float(ceiling)
    except (TypeError, ValueError):
        return 1.0
    return c if MIN_TRUSTED_CEILING < c <= 1.0 else 1.0


AUDCLNT_SHAREMODE_SHARED = 0
# 1 second, in 100-nanosecond units. Nothing reads the buffer, so its size only sets how much
# Windows discards before it wraps.
WAKE_BUFFER_100NS = 10_000_000


def _is_capture_endpoint(enum, endpoint_id: str) -> bool:
    """True if this endpoint is an input, by asking Windows which list it is in. Never inferred
    from the name: the capture and playback halves of a stream differ only by a prefix."""
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

    No frame is ever read: IAudioMeterInformation reports 0 on an input nothing is capturing from,
    and this makes Windows run its engine. Shared mode, because exclusive mode would take the
    microphone from every other application.
    """

    def __init__(self, dev):
        self._client = None
        self._fmt = None
        client = dev.Activate(IAudioClient._iid_, CLSCTX_ALL, None).QueryInterface(IAudioClient)
        self._fmt = client.GetMixFormat()
        try:
            client.Initialize(AUDCLNT_SHAREMODE_SHARED, 0, WAKE_BUFFER_100NS, 0, self._fmt, None)
            client.Start()
        except Exception:
            self.close()                 # the mix format is CoTaskMem-allocated: free it
            raise
        self._client = client
        # The first reads after Start() come back 0 while the engine spins up.
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
    """One endpoint's meter, held open for polling. Create, read and close it on ONE thread: COM
    apartments are per-thread and an interface pointer does not travel between them.

    On a capture endpoint it starts `_EngineWaker`; `wake_failed` says that could not be done, so a
    reading of 0 says nothing about the microphone.
    """

    def __init__(self, endpoint_id: str):
        comtypes.CoInitialize()
        self.id = endpoint_id
        self._meter = self._vol = self._waker = None
        try:
            self._open(endpoint_id)
        except Exception:
            self.close()                 # balances the CoInitialize on this thread
            raise

    def _open(self, endpoint_id: str):
        enum = comtypes.client.CreateObject(CLSID_MMDeviceEnumerator,
                                            interface=IMMDeviceEnumerator)
        dev = enum.GetDevice(endpoint_id)
        self.name = _friendly_name(dev)
        self._meter = dev.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None) \
                         .QueryInterface(IAudioMeterInformation)
        self.is_capture = _is_capture_endpoint(enum, endpoint_id)
        # What a true full-scale signal reads here, and Windows' own mute, which reads as 0 and must
        # be reported as a mute rather than as a silent microphone.
        self.volume_db = 0.0
        self.ceiling = 1.0
        self.muted = False
        try:
            self._vol = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None) \
                           .QueryInterface(IAudioEndpointVolume)
            self.refresh_ceiling()
        except Exception:
            pass          # no volume interface: assume unity
        self.wake_failed = False
        if self.is_capture:
            try:
                self._waker = _EngineWaker(dev)
            except Exception:
                self.wake_failed = True

    def refresh_ceiling(self) -> float:
        """Re-read the endpoint volume and mute, and return the ceiling: what a true full-scale
        signal reads, 10^(dB/20) capped at 1.0. Re-read rather than cached, because the Windows
        level is a live slider anything can move, and the clip threshold follows it."""
        if self._vol is None:
            return self.ceiling
        try:
            self.volume_db = float(self._vol.GetMasterVolumeLevel())
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
    """A short tone marking the start or end of a timed step: a tone arrives at once, where "talk
    now" queues behind the screen reader. Rising for start, one low tone for stop, both distinct
    from the two-tone clipping rasp.

    Synchronous, so nobody talks over their own start signal. It plays on the default output, often
    the desk, but auto gain's isolated mix excludes that source, so it never reaches a measurement.
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
    """A short rasp for clipping, on a thread so the meter loop never waits for it.

    Two low tones from `winsound.Beep` rather than a system sound, which may be configured to
    nothing and ships no asset. It plays on the default output, often the desk itself, so during a
    recording it lands in the take; the page says so."""
    def _beep():
        try:
            import winsound            # noqa: PLC0415  (Windows only, and only when it fires)
            winsound.Beep(320, 110)
            winsound.Beep(240, 160)
        except Exception:
            pass                       # no sound device, or not Windows: the speech still happens
    threading.Thread(target=_beep, daemon=True).start()


# The line between silence and signal: -60 dBFS, about a live mic's floor in a quiet room.
SILENCE = 0.001


def level_note(volume_db: float) -> str:
    """A sentence about Windows' own level for this endpoint, or "" when it is close to unity.

    The desk's capture endpoints map Windows' percentage slider as measured: 0% is -96 dB, 50% is
    -1.08 dB, about 55% is unity, 75% is +7.67 dB, 100% is +30 dB. So the common advice of 75%
    makes anything the desk sends above -7.67 dBFS clip in Windows while the desk is fine. The
    advice points at the app's dB control, which can land exactly on unity where the slider cannot.
    """
    if volume_db > 1.0:
        return (f"Windows is adding {volume_db:.1f} dB to this input, so it will clip in Windows "
                f"before the desk does. Use Set it to no change below, then set your gain on the "
                f"unit itself.")
    if volume_db < -3.0:
        return (f"Windows is taking {abs(volume_db):.1f} dB off this input, so everything reads "
                f"lower here than the desk is really sending.")
    return ""


# The band the level control offers, narrower than the driver's. The desk's capture endpoints report
# -96 to +30 dB in 1.5 dB steps and its playback endpoints -96 to 0, but -96 dB is off, and a control
# that can silently mute every recording on the machine is not one to offer unseen.
OFFERED_LEVEL_MIN_DB = -30.0
OFFERED_LEVEL_MAX_DB = 30.0

# For an endpoint Windows cannot level (RODE's Virtual Devices outputs): says where the level is.
FIXED_LEVEL_NOTE = ("Windows cannot change this device's level. Use the channel's fader on the "
                    "unit, or this app's own level control.")


def _default_endpoint_name(flow: int) -> str:
    """Windows' default endpoint for one data flow (console role), by friendly name, or ""."""
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
    """Windows' default playback device's friendly name, or "". An unset announcer output means
    this device, and when it is the desk, speech travels into the mix and the recording."""
    return _default_endpoint_name(RENDER)


def default_recording_name() -> str:
    """Windows' default recording device's friendly name, or "". The meter defaults to it when it
    is the desk's, because it is what every other program on the PC records from."""
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
    """(min dB, max dB, step, fixed) the control offers: the driver's range narrowed to the band
    above. Shared by the reader and the writer so they cannot disagree.

    `fixed` is a DEGENERATE range, not a missing one. RODE's Virtual Devices endpoint `System
    (RØDECaster)` reports min 0, max 0, has no level at all, and refuses SetMasterVolumeLevel with
    "the parameter is incorrect", so it gets no slider. A driver that reports no range at all is
    unknown, and gets the band.
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
        pass                     # no range reported: the band is the answer
    return lo, hi, step, False


def endpoint_volume(endpoint_id: str) -> dict:
    """Windows' own level for one endpoint, in dB (see `level_note` for why not percent), with
    the range the control offers."""
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
    """Mute or unmute the endpoint in Windows, from a control the user pressed, and read it back.
    A Windows-muted input reads exactly like a dead microphone."""
    comtypes.CoInitialize()
    try:
        refuse_audio_change_in_tests()
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
    """Set Windows' own level for one endpoint, in dB, and read it back: the desk's `... Chat`
    input silently stays at -1.08 dB whatever is written, by dB or by scalar."""
    comtypes.CoInitialize()
    try:
        refuse_audio_change_in_tests()
        _dev, vol = _volume_interface(endpoint_id)
        lo, hi, _step, fixed = _offered_range(vol)
        if fixed:
            # The driver's own refusal is "the parameter is incorrect", which names nothing to act on.
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
    """A peak spoken in dBFS. Never a percentage: the meter is linear amplitude, so "50 percent" is
    already -6 dBFS. "minus 6" rather than "-6", which a screen reader may read as "dash six"."""
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
    """A peak as a judgement first and a number second: live, too quiet, or about to clip. The
    bands follow the desk's own meters: green to about -18 dBFS, amber to -6, red above."""
    at = dbfs(peak)
    if muted:
        # First, because a muted endpoint reads 0 whatever the microphone is doing.
        return f"{name}: MUTED IN WINDOWS. Nothing recorded from it will have any sound."
    if peak <= SILENCE and wake_failed:
        return (f"{name}: no level can be read. Windows only reports an input's level while "
                f"something is recording from it, and that could not be started here.")
    if peak <= SILENCE:
        return f"{name}: silent. Nothing is coming through."
    # Only the clip line is scaled by the ceiling; the bands below are judgements by ear.
    if peak >= clip_at * trusted_ceiling(ceiling):
        # With Windows boosting the input, the desk's gain is the wrong knob.
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
    """The advice at the end of a check: is this usable, and which control to turn if not (the
    Windows level when Windows is boosting the input, otherwise the desk's gain). `describe`
    reports; this decides."""
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
    """A background thread that watches one stream and speaks only when something needs saying:

      CLIPPING          - at once, then held for `clip_gap` seconds so a hot passage is one warning.
      WENT SILENT/LIVE  - after `silence_after` seconds of nothing, so a breath is not an event.
      THE LEVEL ITSELF  - every `speak_every` seconds; off by default, since a running commentary
                          is unusable while talking.

    Every threshold is a setting, because they are judgements by ear."""

    def __init__(self, endpoint_id: str, say, *, clip_at=0.99, clip_gap=4.0,
                 warn_silence=True, silence_after=5.0, speak_every=0.0, poll=0.05,
                 clip_sound=True, scale=1.0, label=""):
        self.endpoint_id = endpoint_id
        self._say = say
        # scale and label serve the per-channel check (server.action_channel_level_check), which
        # hears one channel through a temporary post-fader Chat mix. scale is that cross-point's
        # level: dividing by it puts every reading, and the clip test, back in channel terms, as
        # auto gain does; a microphone clips in the preamp, ahead of the fader. label is the name
        # spoken in place of the Windows endpoint's.
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
        # Live readings for the page. peak_hold decays like a real meter; peak_max is the true
        # maximum over the run, which a check reports at the end.
        self.peak = 0.0
        self.peak_hold = 0.0
        self.peak_max = 0.0
        self.name = ""
        # Copied from the Meter once open, and the last three followed live.
        self.wake_failed = False
        self.ceiling = 1.0
        self.volume_db = 0.0
        self.muted = False

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # Long enough for an open failure (bad endpoint id, no audio service) to land in self.error.
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
                    v = min(1.0, v / self.scale)
                self.peak = v
                self.peak_hold = max(self.peak_hold * 0.995, v)   # a slow decay, like a real meter
                self.peak_max = max(self.peak_max, v)
                now = time.time()

                if (now - last_ceiling) >= CEILING_REFRESH_S:
                    last_ceiling = now
                    self.ceiling = m.refresh_ceiling()
                    self.volume_db = m.volume_db
                    self.muted = m.muted
                    clip_level = self.clip_at * trusted_ceiling(self.ceiling)

                if v >= clip_level and (now - last_clip) >= self.clip_gap:
                    last_clip = now
                    # The rasp first, since it arrives whole; the words then say which stream.
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
                    # An input whose engine could not start reads 0 forever: never call it silent.
                    was_silent = True
                    self._speak(f"{self.name} has gone silent")

                if self.speak_every > 0 and (now - last_level) >= self.speak_every:
                    last_level = now
                    # A bare level, short enough to ride a fader by.
                    self._speak(dbfs(self.peak_hold))

                self._stop.wait(self.poll)
        except Exception as exc:
            # Must set self.error: a thread that dies with no error reads as a finished run, and
            # a channel that fell over is summarised as a quiet one.
            self.error = f"The meter stopped part-way through: {exc}"
        finally:
            m.close()

    def _speak(self, text: str):
        try:
            self._say(text)
        except Exception:
            pass          # a meter must never be able to take the announcer, or the app, down
