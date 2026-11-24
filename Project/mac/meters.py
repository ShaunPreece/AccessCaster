"""macOS's `meters`, on CoreAudio (`mac.core_audio`) and an AudioQueue.

An endpoint is one device in one direction, so its id is the direction and the device's UID,
which survives a replug. The system lists only the devices present.

A PEAK NEEDS A RECORDING. macOS has no meter on a device as such, so an input is metered by
recording from it with an AudioQueue whose own level metering is on, and the audio itself is
thrown away unread. An output cannot be metered at all: nothing on the system records what it
plays. Recording from an input needs the user's permission for the microphone; refused, it records
silence, so `wake_failed` is set from that permission rather than from the recording.

The AudioQueue runs its callbacks on a thread of its own, which only hands each buffer back.
"""
from __future__ import annotations

import ctypes
import time

from AVFoundation import AVCaptureDevice, AVMediaTypeAudio

from mac import cf, core_audio

PLAYBACK, RECORDING = "playback", "recording"     # as `meters` names them
PRESENT_CHECK_S = 2.0
_DIRECTIONS = {RECORDING: core_audio.INPUT, PLAYBACK: core_audio.OUTPUT}
_AUTHORISED = 3                                     # AVAuthorizationStatusAuthorized


def remote_session() -> bool:
    return False


def _endpoint_id(flow: str, uid: str) -> str:
    return f"{flow}:{uid}"


def _parse(endpoint_id: str):
    flow, _, uid = endpoint_id.partition(":")
    if flow not in _DIRECTIONS or not uid:
        raise OSError(f"{endpoint_id!r} is not an audio endpoint of this Mac.")
    return flow, uid


def _device(uid: str):
    found = next((d for d in core_audio.devices() if d.uid == uid), None)
    if found is None:
        raise OSError("That audio device is no longer available.")
    return found


def list_endpoints() -> list:
    out = []
    try:
        for device in core_audio.devices():
            alive = core_audio.alive(device.id)
            for flow, channels in ((PLAYBACK, device.outputs), (RECORDING, device.inputs)):
                if channels and device.uid:
                    out.append({"id": _endpoint_id(flow, device.uid), "name": device.name,
                                "flow": flow, "state": "active" if alive else "not present",
                                "active": alive})
    except OSError:
        pass
    return out


class _ASBD(ctypes.Structure):
    _fields_ = [("mSampleRate", ctypes.c_double), ("mFormatID", ctypes.c_uint32),
                ("mFormatFlags", ctypes.c_uint32), ("mBytesPerPacket", ctypes.c_uint32),
                ("mFramesPerPacket", ctypes.c_uint32), ("mBytesPerFrame", ctypes.c_uint32),
                ("mChannelsPerFrame", ctypes.c_uint32), ("mBitsPerChannel", ctypes.c_uint32),
                ("mReserved", ctypes.c_uint32)]


class _LevelMeterState(ctypes.Structure):
    _fields_ = [("mAveragePower", ctypes.c_float), ("mPeakPower", ctypes.c_float)]


def _code(text: str) -> int:
    return int.from_bytes(text.encode("ascii"), "big")


_LINEAR_PCM = _code("lpcm")
_FLOAT_PACKED = 0x1 | 0x8                           # kAudioFormatFlagIsFloat | IsPacked
_CURRENT_DEVICE = _code("aqcd")
_ENABLE_METERING = _code("aqme")
_LEVEL_METER = _code("aqmv")                        # linear 0 to 1, per channel
_BUFFERS = 3
_BUFFER_S = 0.05

_INPUT_CALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p)

_AQ = ctypes.CDLL("/System/Library/Frameworks/AudioToolbox.framework/AudioToolbox")
_AQ.AudioQueueNewInput.argtypes = [ctypes.POINTER(_ASBD), _INPUT_CALLBACK, ctypes.c_void_p,
                                   ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                   ctypes.POINTER(ctypes.c_void_p)]
_AQ.AudioQueueSetProperty.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                                      ctypes.c_uint32]
_AQ.AudioQueueGetProperty.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                                      ctypes.POINTER(ctypes.c_uint32)]
_AQ.AudioQueueAllocateBuffer.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                         ctypes.POINTER(ctypes.c_void_p)]
_AQ.AudioQueueEnqueueBuffer.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                        ctypes.c_void_p]
_AQ.AudioQueueStart.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_AQ.AudioQueueStop.argtypes = [ctypes.c_void_p, ctypes.c_bool]
_AQ.AudioQueueDispose.argtypes = [ctypes.c_void_p, ctypes.c_bool]
for _name in ("AudioQueueNewInput", "AudioQueueSetProperty", "AudioQueueGetProperty",
              "AudioQueueAllocateBuffer", "AudioQueueEnqueueBuffer", "AudioQueueStart",
              "AudioQueueStop", "AudioQueueDispose"):
    getattr(_AQ, _name).restype = ctypes.c_int32


def _check(status: int, what: str) -> None:
    if status:
        raise OSError(f"{what} failed (status {status})")


class _MeterQueue:
    """A recording from one input, kept running for its level meter."""

    def __init__(self, device):
        self._channels = max(1, min(device.inputs, 2))
        rate = core_audio.nominal_rate(device.id)
        frame = 4 * self._channels
        form = _ASBD(rate, _LINEAR_PCM, _FLOAT_PACKED, frame, 1, frame, self._channels, 32, 0)
        self._callback = _INPUT_CALLBACK(self._returned)    # kept: the queue holds only a pointer
        self._queue = ctypes.c_void_p()
        _check(_AQ.AudioQueueNewInput(ctypes.byref(form), self._callback, None, None, None, 0,
                                      ctypes.byref(self._queue)), "Opening the recording")
        try:
            uid = cf.cfstring(device.uid)
            try:
                ref = ctypes.c_void_p(uid)
                _check(_AQ.AudioQueueSetProperty(self._queue, _CURRENT_DEVICE, ctypes.byref(ref),
                                                 ctypes.sizeof(ref)), "Choosing the device")
            finally:
                cf.release(uid)
            on = ctypes.c_uint32(1)
            _check(_AQ.AudioQueueSetProperty(self._queue, _ENABLE_METERING, ctypes.byref(on),
                                             ctypes.sizeof(on)), "Turning the meter on")
            size = int(rate * _BUFFER_S) * frame
            for _ in range(_BUFFERS):
                buffer = ctypes.c_void_p()
                _check(_AQ.AudioQueueAllocateBuffer(self._queue, size, ctypes.byref(buffer)),
                       "Making a buffer")
                _check(_AQ.AudioQueueEnqueueBuffer(self._queue, buffer, 0, None),
                       "Queueing a buffer")
            _check(_AQ.AudioQueueStart(self._queue, None), "Starting the recording")
        except OSError:
            self.close()
            raise

    def _returned(self, _user, queue, buffer, _start, _packets, _descriptions):
        if self._queue:
            _AQ.AudioQueueEnqueueBuffer(queue, buffer, 0, None)

    def peak(self) -> float:
        levels = (_LevelMeterState * self._channels)()
        size = ctypes.c_uint32(ctypes.sizeof(levels))
        _check(_AQ.AudioQueueGetProperty(self._queue, _LEVEL_METER, levels, ctypes.byref(size)),
               "Reading the meter")
        return max(float(level.mPeakPower) for level in levels)

    def close(self) -> None:
        queue, self._queue = self._queue, ctypes.c_void_p()
        if queue:
            _AQ.AudioQueueStop(queue, True)
            _AQ.AudioQueueDispose(queue, True)


def _microphone_allowed() -> bool:
    return AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio) == _AUTHORISED


class EndpointReader:
    def __init__(self, endpoint_id: str):
        flow, uid = _parse(endpoint_id)
        device = _device(uid)
        self._id = device.id
        self._direction = _DIRECTIONS[flow]
        self.name = device.name
        self.is_capture = flow == RECORDING
        if not self.is_capture:
            raise OSError(f"macOS cannot meter what plays through {self.name}.")
        self._checked = time.monotonic()
        self._queue = None
        try:
            self._queue = _MeterQueue(device)
        except OSError:
            pass
        self.wake_failed = self._queue is None or not _microphone_allowed()

    def level(self):
        found = core_audio.level(self._id, self._direction)
        return None if found is None else (found[0], found[2])

    def peak(self) -> float:
        now = time.monotonic()
        if now - self._checked >= PRESENT_CHECK_S:
            self._checked = now
            if not core_audio.alive(self._id):
                raise OSError(f"{self.name} is no longer available.")
        return 0.0 if self._queue is None else self._queue.peak()

    def close(self):
        if self._queue is not None:
            self._queue.close()
            self._queue = None


def _located(endpoint_id: str):
    flow, uid = _parse(endpoint_id)
    return _device(uid), _DIRECTIONS[flow]


def read_level(endpoint_id: str) -> dict:
    device, direction = _located(endpoint_id)
    found = core_audio.level(device.id, direction)
    if found is None:
        raise OSError(f"{device.name} has no level macOS can read.")
    db, span, muted = found
    return {"name": device.name, "db": db, "range": None if span is None else (*span, 0.0),
            "muted": muted}


def write_mute(endpoint_id: str, muted: bool) -> bool:
    device, direction = _located(endpoint_id)
    core_audio.set_mute(device.id, direction, muted)
    return core_audio.muted(device.id, direction)


def write_level(endpoint_id: str, db: float) -> float:
    device, direction = _located(endpoint_id)
    core_audio.set_level(device.id, direction, db)
    return core_audio.level(device.id, direction)[0]
