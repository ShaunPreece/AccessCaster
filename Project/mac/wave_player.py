"""A STAND-IN for macOS's sound player: every clip finishes at once, unheard."""
import threading

CLOSE_AFTER_S = 15
DEFAULT_DEVICE = None


class Signal:
    def __init__(self):
        self._event = threading.Event()

    def set(self):
        self._event.set()

    def clear(self):
        self._event.clear()

    def close(self):
        pass


class Player:
    def __init__(self, sample_rate: int):
        self.sample_rate = sample_rate

    @property
    def is_open(self) -> bool:
        return False

    def play(self, pcm: bytes, device_id=DEFAULT_DEVICE, cancel=None, min_ms: int = 0) -> bool:
        return True

    def idle(self) -> None:
        pass

    def close(self) -> None:
        pass
