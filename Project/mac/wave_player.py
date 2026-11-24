"""macOS's `wave_player`, on AVAudioPlayer.

A device is a CoreAudio device UID (`mac.core_audio`), which AVAudioPlayer takes as its current
device; None is the system's default output. Each clip is a player of its own, so "open" here is
the device chosen for the run of clips, kept until `idle` or `close`.
"""
from __future__ import annotations

import io
import threading
import time
import wave

from AVFoundation import AVAudioPlayer
from Foundation import NSData

CLOSE_AFTER_S = 15
DEFAULT_DEVICE = None

# How far past its own length a clip may run before the device counts as stalled.
_STALL_S = 2.0
_POLL_S = 0.01


class Signal:
    def __init__(self):
        self._event = threading.Event()

    def set(self):
        self._event.set()

    def clear(self):
        self._event.clear()

    def close(self):
        pass

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout)


class Player:
    """Plays one clip at a time, on the calling thread."""

    def __init__(self, sample_rate: int):
        self.sample_rate = sample_rate
        self._open = False
        self._device = DEFAULT_DEVICE
        self._last_played = 0.0

    @property
    def is_open(self) -> bool:
        return self._open

    def play(self, pcm: bytes, device_id=DEFAULT_DEVICE, cancel: Signal | None = None,
             min_ms: int = 0) -> bool:
        if not pcm:
            return True
        if not self._open:
            self._device, self._open = device_id, True
        player = self._player(pcm)
        try:
            if not player.play():
                raise OSError("The sound device would not start playing")
            finished = self._wait(player, len(pcm) / (2 * self.sample_rate), cancel, min_ms)
        except OSError:
            player.stop()
            self.close()
            raise
        self._last_played = time.monotonic()
        return finished

    def idle(self) -> None:
        if self._open and time.monotonic() - self._last_played >= CLOSE_AFTER_S:
            self.close()

    def close(self) -> None:
        self._open = False
        self._device = DEFAULT_DEVICE

    def _player(self, pcm: bytes):
        clip = io.BytesIO()
        with wave.open(clip, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(self.sample_rate)
            out.writeframes(pcm)
        data = clip.getvalue()
        player, error = AVAudioPlayer.alloc().initWithData_error_(
            NSData.dataWithBytes_length_(data, len(data)), None)
        if player is None:
            raise OSError(f"The sound could not be prepared: {error}")
        if self._device:
            player.setCurrentDevice_(self._device)
        player.prepareToPlay()
        return player

    def _wait(self, player, clip_s: float, cancel: Signal | None, min_ms: int) -> bool:
        start = time.monotonic()
        deadline = start + clip_s + _STALL_S
        cut_from = start + min_ms / 1000
        while player.isPlaying():
            now = time.monotonic()
            if now >= deadline:
                raise OSError("The sound device stopped playing")
            if cancel is not None and now >= cut_from:
                if cancel.wait(_POLL_S):
                    player.stop()
                    return False
            else:
                time.sleep(_POLL_S)
        return True
