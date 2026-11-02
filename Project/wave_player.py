"""Plays 16-bit mono PCM through one Windows output, and cuts a clip off on request.

The device is held open between clips: opening it costs time on every phrase, which is heard as lag
while a fader moves. It is closed after CLOSE_AFTER_S without a clip, because an open device counts
as media playing and keeps Windows awake, and so that a device plugged in or removed meanwhile is
found again on the next open. A waveOut device id is a position in Windows' current list, not a
fixed name, which is why the caller resolves it afresh for every open.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import time

CLOSE_AFTER_S = 15

# waveOutOpen's "whatever Windows' default output is".
DEFAULT_DEVICE = 0xFFFFFFFF
_CALLBACK_EVENT = 0x50000
_WAVE_FORMAT_PCM = 1
_WHDR_DONE = 1
_WAIT_OBJECT_0 = 0

# Beyond the clip's own length, how long playback may overrun before the device is treated as stuck.
_STALL_MS = 2000


class _WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", w.WORD), ("nChannels", w.WORD), ("nSamplesPerSec", w.DWORD),
        ("nAvgBytesPerSec", w.DWORD), ("nBlockAlign", w.WORD),
        ("wBitsPerSample", w.WORD), ("cbSize", w.WORD),
    ]


class _WAVEHDR(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.c_void_p), ("dwBufferLength", w.DWORD),
        ("dwBytesRecorded", w.DWORD), ("dwUser", ctypes.c_size_t),
        ("dwFlags", w.DWORD), ("dwLoops", w.DWORD), ("lpNext", ctypes.c_void_p),
        ("reserved", ctypes.c_size_t),
    ]


_winmm = ctypes.WinDLL("winmm")
_winmm.waveOutOpen.restype = w.UINT
_winmm.waveOutOpen.argtypes = (ctypes.POINTER(w.HANDLE), w.UINT, ctypes.POINTER(_WAVEFORMATEX),
                               ctypes.c_size_t, ctypes.c_size_t, w.DWORD)
for _name in ("waveOutPrepareHeader", "waveOutWrite", "waveOutUnprepareHeader"):
    getattr(_winmm, _name).restype = w.UINT
    getattr(_winmm, _name).argtypes = (w.HANDLE, ctypes.POINTER(_WAVEHDR), w.UINT)
for _name in ("waveOutReset", "waveOutClose"):
    getattr(_winmm, _name).restype = w.UINT
    getattr(_winmm, _name).argtypes = (w.HANDLE,)

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateEventW.restype = w.HANDLE
_kernel32.CreateEventW.argtypes = (ctypes.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR)
for _name in ("SetEvent", "ResetEvent", "CloseHandle"):
    getattr(_kernel32, _name).argtypes = (w.HANDLE,)
_kernel32.WaitForMultipleObjects.restype = w.DWORD
_kernel32.WaitForMultipleObjects.argtypes = (w.DWORD, ctypes.POINTER(w.HANDLE), w.BOOL, w.DWORD)


class Signal:
    """A manual-reset Win32 event, so another thread can cut a clip off while the player waits on
    the device."""

    def __init__(self):
        self.handle = _kernel32.CreateEventW(None, True, False, None)

    def set(self):
        _kernel32.SetEvent(self.handle)

    def clear(self):
        _kernel32.ResetEvent(self.handle)

    def close(self):
        _kernel32.CloseHandle(self.handle)


class Player:
    """Plays one clip at a time, on the calling thread."""

    def __init__(self, sample_rate: int):
        self._format = _WAVEFORMATEX(
            wFormatTag=_WAVE_FORMAT_PCM, nChannels=1, nSamplesPerSec=sample_rate,
            nAvgBytesPerSec=sample_rate * 2, nBlockAlign=2, wBitsPerSample=16)
        self._device = None
        # Signalled by the device each time a clip finishes, and once when it opens. Auto-reset.
        self._done = _kernel32.CreateEventW(None, False, False, None)
        self._last_played = 0.0

    @property
    def is_open(self) -> bool:
        return self._device is not None

    def play(self, pcm: bytes, device_id: int = DEFAULT_DEVICE, cancel: Signal | None = None,
             min_ms: int = 0) -> bool:
        """Play `pcm` and return True once it has finished, or False when `cancel` cut it off.
        A cancel is honoured only once `min_ms` of the clip has played. `device_id` is used only
        when the device is not already open. Raises OSError if the device fails; the device is then
        closed, so the next clip opens afresh."""
        if not pcm:
            return True
        self._open(device_id)
        buffer = ctypes.create_string_buffer(pcm, len(pcm))
        header = _WAVEHDR(lpData=ctypes.cast(buffer, ctypes.c_void_p), dwBufferLength=len(pcm))
        size = ctypes.sizeof(header)
        try:
            _check(_winmm.waveOutPrepareHeader(self._device, header, size), "waveOutPrepareHeader")
            try:
                _check(_winmm.waveOutWrite(self._device, header, size), "waveOutWrite")
                finished = self._wait(header, len(pcm) * 1000 // self._format.nAvgBytesPerSec,
                                      cancel, min_ms)
            finally:
                # Unprepare refuses a buffer still playing, and the buffer must not be freed while
                # the device holds it.
                if not header.dwFlags & _WHDR_DONE:
                    _winmm.waveOutReset(self._device)
                _winmm.waveOutUnprepareHeader(self._device, header, size)
        except OSError:
            self.close()
            raise
        self._last_played = time.monotonic()
        return finished

    def idle(self) -> None:
        """Close the device if it has been quiet for CLOSE_AFTER_S."""
        if self._device is not None and time.monotonic() - self._last_played >= CLOSE_AFTER_S:
            self.close()

    def close(self) -> None:
        if self._device is not None:
            _winmm.waveOutReset(self._device)
            _winmm.waveOutClose(self._device)
            self._device = None

    def _open(self, device_id: int) -> None:
        if self._device is not None:
            return
        device = w.HANDLE()
        _check(_winmm.waveOutOpen(ctypes.byref(device), device_id, ctypes.byref(self._format),
                                  self._done, 0, _CALLBACK_EVENT), "waveOutOpen")
        self._device = device

    def _wait(self, header, clip_ms: int, cancel: Signal | None, min_ms: int) -> bool:
        start = time.monotonic()
        deadline = start + (clip_ms + _STALL_MS) / 1000
        cut_from = start + min_ms / 1000
        # The done event also fires when the device opens, so the header's flag decides.
        while not header.dwFlags & _WHDR_DONE:
            now = time.monotonic()
            left_ms = int((deadline - now) * 1000)
            if left_ms <= 0:
                raise OSError("The sound device stopped playing")
            if cancel is not None and now >= cut_from:
                handles = (w.HANDLE * 2)(self._done, cancel.handle)
            else:
                handles = (w.HANDLE * 1)(self._done)
                if cancel is not None:
                    left_ms = min(left_ms, max(1, int((cut_from - now) * 1000)))
            which = _kernel32.WaitForMultipleObjects(len(handles), handles, False, left_ms)
            if which == _WAIT_OBJECT_0 + 1:
                _winmm.waveOutReset(self._device)
                return False
        return True


def _check(result: int, call: str) -> None:
    if result:
        raise OSError(f"{call} failed with MMRESULT {result}")
