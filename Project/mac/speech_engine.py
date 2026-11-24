"""A STAND-IN for macOS's text to speech: no voices, no outputs, and every phrase renders as no
sound, so the announcer is silent."""
import contextlib

import wave_player


@contextlib.contextmanager
def apartment():
    yield


class Synth:
    def __init__(self, sample_rate: int):
        self.sample_rate = sample_rate

    def voice_names(self) -> list:
        return []

    def output_names(self) -> list:
        return []

    def use_voice(self, name: str) -> None:
        pass

    def output_device(self, name: str):
        return wave_player.DEFAULT_DEVICE

    def render(self, text: str) -> bytes:
        return b""
