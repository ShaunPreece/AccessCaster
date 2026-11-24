"""Windows' `meters`: every audio endpoint's peak through IAudioMeterInformation, and its level
and mute through IAudioEndpointVolume.

One COM call per reading, where a capture library would add PortAudio, numpy and a capture thread
per stream. Two Windows behaviours shape `EndpointReader`:

  - An input's meter reads 0 unless something is recording from it, so a silent shared-mode client
    is held open on it (`_EngineWaker`).
  - A meter does not raise once its endpoint is removed: it goes on returning 0, and reads again if
    the same endpoint comes back. So the endpoint's state is asked every `PRESENT_CHECK_S`.
"""
from __future__ import annotations

import ctypes
import time
from ctypes import c_float

import comtypes

import diagnostics
from win import core_audio as ca

# How often `EndpointReader.peak` asks whether its endpoint is still there.
PRESENT_CHECK_S = 2.0

AUDCLNT_SHAREMODE_SHARED = 0
# 1 second, in 100-nanosecond units. Nothing reads the buffer, so its size only sets how much
# Windows discards before it wraps.
WAKE_BUFFER_100NS = 10_000_000

_SM_REMOTESESSION = 0x1000


def remote_session() -> bool:
    """True in a Remote Desktop session. Windows gives each session its own audio endpoints, so the
    desk's streams belong to the console session and a remote one cannot meter them."""
    try:
        return bool(ctypes.windll.user32.GetSystemMetrics(_SM_REMOTESESSION))
    except Exception:
        return False


def list_endpoints() -> list:
    out = []
    with ca.apartment():
        try:
            enum = ca.enumerator()
            # Endpoints come and go during an enumeration (an RDP session's audio device does so
            # constantly), so one failing entry is skipped rather than emptying the list.
            for flow in (ca.RENDER, ca.CAPTURE):
                try:
                    coll = enum.EnumAudioEndpoints(flow, ca.STATE_ALL)
                    count = coll.GetCount()
                except Exception:
                    continue
                for i in range(count):
                    try:
                        dev = coll.Item(i)
                        state = dev.GetState()
                        out.append({
                            "id": dev.GetId(),
                            "name": ca.friendly_name(dev),
                            "flow": ca.FLOW_NAMES[flow],
                            "state": ca.STATE_NAMES.get(state, f"unknown ({state})"),
                            "active": state == ca.STATE_ACTIVE,
                        })
                    except Exception:
                        continue
        except Exception as exc:
            diagnostics.event("meters.enumerate_failed", str(exc))
    return out


def _is_capture_endpoint(enum, endpoint_id: str) -> bool:
    """True if this endpoint is an input, by asking Windows which list it is in. Never inferred
    from the name: the capture and playback halves of a stream differ only by a prefix.

    Raises when the capture list cannot be read: an input taken for an output gets no engine waker,
    reads 0, and would be called silent."""
    coll = enum.EnumAudioEndpoints(ca.CAPTURE, ca.STATE_ALL)
    for i in range(coll.GetCount()):
        try:
            if coll.Item(i).GetId() == endpoint_id:
                return True
        except Exception:
            continue                     # an endpoint removed mid-enumeration
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
        client = ca.activate(dev, ca.IAudioClient)
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


class EndpointReader:
    """One endpoint held open for polling, inside a COM apartment of its own on the thread that
    made it."""

    def __init__(self, endpoint_id: str):
        comtypes.CoInitialize()
        self._dev = self._meter = self._vol = self._waker = None
        self._checked = time.monotonic()
        try:
            self._open(endpoint_id)
        except Exception:
            self.close()                 # balances the CoInitialize on this thread
            raise

    def _open(self, endpoint_id: str):
        enum = ca.enumerator()
        dev = enum.GetDevice(endpoint_id)
        self._dev = dev
        self.name = ca.friendly_name(dev)
        self._meter = ca.activate(dev, ca.IAudioMeterInformation)
        self.is_capture = _is_capture_endpoint(enum, endpoint_id)
        try:
            self._vol = ca.activate(dev, ca.IAudioEndpointVolume)
        except Exception:
            pass                         # no volume interface: `level` says so with None
        self.wake_failed = False
        if self.is_capture:
            try:
                self._waker = _EngineWaker(dev)
            except Exception:
                self.wake_failed = True

    def level(self):
        if self._vol is None:
            return None
        return float(self._vol.GetMasterVolumeLevel()), bool(self._vol.GetMute())

    def peak(self) -> float:
        now = time.monotonic()
        if now - self._checked >= PRESENT_CHECK_S:
            self._checked = now
            if self._dev.GetState() != ca.STATE_ACTIVE:
                raise OSError(f"{self.name} is no longer available.")
        return float(self._meter.GetPeakValue())

    def close(self):
        self._dev = None
        self._vol = None
        if self._waker is not None:
            self._waker.close()
            self._waker = None
        self._meter = None
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass


def _volume_interface(endpoint_id: str):
    """(device, IAudioEndpointVolume) for one endpoint. The caller holds the apartment."""
    dev = ca.enumerator().GetDevice(endpoint_id)
    return dev, ca.activate(dev, ca.IAudioEndpointVolume)


def read_level(endpoint_id: str) -> dict:
    with ca.apartment():
        dev, vol = _volume_interface(endpoint_id)
        db = float(vol.GetMasterVolumeLevel())
        try:
            lo, hi, step = vol.GetVolumeRange()
            level_range = (float(lo), float(hi), float(step))
        except Exception:
            level_range = None
        try:
            muted = bool(vol.GetMute())
        except Exception:
            muted = False
        return {"name": ca.friendly_name(dev), "db": db, "range": level_range, "muted": muted}


def write_mute(endpoint_id: str, muted: bool) -> bool:
    with ca.apartment():
        _dev, vol = _volume_interface(endpoint_id)
        vol.SetMute(1 if muted else 0, None)
        return bool(vol.GetMute())


def write_level(endpoint_id: str, db: float) -> float:
    with ca.apartment():
        _dev, vol = _volume_interface(endpoint_id)
        vol.SetMasterVolumeLevel(c_float(db), None)
        return float(vol.GetMasterVolumeLevel())
