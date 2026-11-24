"""macOS's `speech_engine`, on the system's `say`, which renders straight to 16-bit mono PCM at
the rate asked for. The text goes in on standard input, so none of it can be read as an option.

`say` obeys speech commands embedded in the text in double square brackets, so a double bracket
in the text is split before it goes in: a channel named with one is then read as written.
"""
from __future__ import annotations

import contextlib
import os
import re
import subprocess
import tempfile
import wave

import wave_player
from mac import core_audio

SAY = "/usr/bin/say"
# Words a minute: about the pace of the Windows voice at the rate it is given there.
_RATE = 240
# "Eddy (English (UK))  en_GB    # Hello! My name is Eddy." A name may hold spaces and brackets.
_VOICE_LINE = re.compile(r"^(?P<name>.+?)\s+[a-z]{2,3}[_-][A-Za-z0-9_-]+\s+#")
_TIMEOUT_S = 20


@contextlib.contextmanager
def apartment():
    yield


class Synth:
    def __init__(self, sample_rate: int):
        self.sample_rate = sample_rate
        self._voice = None

    def voice_names(self) -> list:
        listing = subprocess.run([SAY, "-v", "?"], capture_output=True, text=True,
                                 timeout=_TIMEOUT_S, check=False).stdout
        return [m.group("name") for m in map(_VOICE_LINE.match, listing.splitlines()) if m]

    def output_names(self) -> list:
        return [d.name for d in core_audio.devices() if d.outputs and d.name]

    def use_voice(self, name: str) -> None:
        self._voice = name or None

    def output_device(self, name: str):
        uid = next((d.uid for d in core_audio.devices() if d.outputs and d.name == name), None)
        return uid or wave_player.DEFAULT_DEVICE

    def render(self, text: str) -> bytes:
        text = text.replace("[[", "[ [")
        with tempfile.TemporaryDirectory(prefix="AccessCaster-say-") as folder:
            out = os.path.join(folder, "phrase.wav")
            command = [SAY, "-r", str(_RATE), "--file-format=WAVE",
                       f"--data-format=LEI16@{self.sample_rate}", "-o", out, "-f", "-"]
            if self._voice:
                command[1:1] = ["-v", self._voice]
            done = subprocess.run(command, input=text, text=True, capture_output=True,
                                  timeout=_TIMEOUT_S, check=False)
            if done.returncode:
                raise OSError(f"say could not speak: {done.stderr.strip() or done.returncode}")
            with wave.open(out, "rb") as clip:
                if clip.getnchannels() != 1 or clip.getsampwidth() != 2:
                    raise OSError("say did not render 16-bit mono")
                return clip.readframes(clip.getnframes())
