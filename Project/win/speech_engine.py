"""Windows' `speech_engine`, on SAPI through pywin32's COM client."""
from __future__ import annotations

import contextlib

import pythoncom
import win32com.client

import wave_player

# SAPI rate, -10 (slow) .. 10 (fast).
_RATE = 3
# SpeechAudioFormatType SAFT22kHz16BitMono, and the only rate a Synth is asked for.
_FORMATS = {22050: 22}
# Speak flag: never parse the text as SAPI XML, so a channel name containing "<" is read as written.
_IS_NOT_XML = 16


@contextlib.contextmanager
def apartment():
    """COM initialised for the calling thread. Every COM reference must be dropped before this
    exits, or the release happens on a thread with no apartment left and pywin32 prints "Win32
    exception occurred releasing IUnknown" to the log."""
    pythoncom.CoInitialize()
    try:
        yield
    finally:
        pythoncom.CoUninitialize()


class Synth:
    def __init__(self, sample_rate: int):
        self._voice = win32com.client.Dispatch("SAPI.SpVoice")
        self._voice.Rate = _RATE
        self._format = win32com.client.Dispatch("SAPI.SpAudioFormat")
        self._format.Type = _FORMATS[sample_rate]

    def voice_names(self) -> list:
        return [t.GetDescription() for t in self._voice.GetVoices()]

    def output_names(self) -> list:
        return [t.GetDescription() for t in self._voice.GetAudioOutputs()]

    def use_voice(self, name: str) -> None:
        self._voice.Voice = _token(self._voice.GetVoices(), name)

    def output_device(self, name: str):
        """SAPI's output token creates an SpMMAudioOut, whose DeviceId is the waveOut id."""
        token = _token(self._voice.GetAudioOutputs(), name)
        return wave_player.DEFAULT_DEVICE if token is None else int(token.CreateInstance().DeviceId)

    def render(self, text: str) -> bytes:
        stream = win32com.client.Dispatch("SAPI.SpMemoryStream")
        stream.Format = self._format
        self._voice.AudioOutputStream = stream
        self._voice.Speak(text, _IS_NOT_XML)
        return bytes(stream.GetData())


def _token(tokens, name: str):
    return next((t for t in tokens if t.GetDescription() == name), None)
