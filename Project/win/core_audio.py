"""Windows Core Audio through COM: the declarations and helpers `win/meters` and
`win/virtual_devices` share.

Every interface lists every method, in order, even the ones never called: a COM vtable is
positional, so a gap makes every later method call the wrong slot.

COM is per thread. Each piece of work opens its own `apartment()` on whatever thread it runs on,
and no interface pointer made inside one may outlive it or travel to another thread.
"""
from __future__ import annotations

import contextlib
import ctypes
from ctypes import POINTER, c_float, c_int32, c_uint32, c_void_p, c_wchar_p

import comtypes
import comtypes.client
from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")

# Data flow and state constants from mmdeviceapi.h.
RENDER, CAPTURE = 0, 1
STATE_ACTIVE, STATE_ALL = 0x1, 0xF
CLSCTX_ALL = 0x17
STGM_READ = 0
VT_LPWSTR = 31

STATE_NAMES = {0x1: "active", 0x2: "disabled", 0x4: "not present", 0x8: "unplugged"}
# The words `meters.PLAYBACK` and `meters.RECORDING`.
FLOW_NAMES = {RENDER: "playback", CAPTURE: "recording"}

# PKEY_Device_FriendlyName: the name Windows shows, e.g. "RODECaster Pro II Main Stereo".
PKEY_Device_FriendlyName = (GUID("{a45c254e-df1c-4efd-8020-67d146a850e0}"), 14)


class PROPVARIANT(ctypes.Structure):
    """Only the fields read here: every property read is a string (VT_LPWSTR), so the pointer
    member is all that is needed, and `prop_string` reports any other type as empty rather than
    mis-decoding it."""
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
    """Used only to open a stream that is never read: Initialize, Start, Stop and GetMixFormat."""
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
    """Windows' own level and mute for an endpoint."""
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
    """Windows' audio-endpoint change callback, implemented by `win/virtual_devices`."""
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


@contextlib.contextmanager
def apartment():
    """COM on this thread for the block."""
    comtypes.CoInitialize()
    try:
        yield
    finally:
        try:
            comtypes.CoUninitialize()
        except Exception:    # an apartment already torn down at exit has nothing left to undo
            pass


def enumerator():
    """Windows' endpoint enumerator. The caller holds the apartment."""
    return comtypes.client.CreateObject(CLSID_MMDeviceEnumerator, interface=IMMDeviceEnumerator)


def activate(dev, interface):
    """`interface` on one endpoint, e.g. its IAudioEndpointVolume."""
    return dev.Activate(interface._iid_, CLSCTX_ALL, None).QueryInterface(interface)


def prop_string(store, key) -> str:
    """A string property from an endpoint's property store, or "" when it is missing or not a
    string."""
    k = PROPERTYKEY()
    k.fmtid, k.pid = key
    try:
        pv = store.GetValue(ctypes.byref(k))
        return pv.pwszVal or "" if pv.vt == VT_LPWSTR else ""
    except Exception:
        return ""


def friendly_name(dev) -> str:
    """The endpoint's name as Windows shows it, or "" when it cannot be read."""
    try:
        return prop_string(dev.OpenPropertyStore(STGM_READ), PKEY_Device_FriendlyName)
    except Exception:
        return ""
