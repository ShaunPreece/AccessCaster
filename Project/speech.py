"""Everything the app says out loud, from one speech thread that plays one thing at a time.

`say` queues a phrase and never blocks; `say_and_wait` blocks until the phrase has been spoken;
`tone` plays a cue on the same output. The voice, output and volume are the user's choice, held here
and saved in settings.json.

SAPI RENDERS, IT DOES NOT PLAY. A phrase is spoken into memory, its silence trimmed, and the samples
played by `wave_player` on a device held open between phrases. Spoken straight to the device, SAPI
opens it for every phrase and its voices pad each one with silence (66 to 86 ms of zeros before the
word on Hazel, 650 ms after), so in a fast run of phrases, a fader moving, each new number cuts
off the last before any of it is heard.

`enabled` IS THE ANNOUNCEMENTS SWITCH, and `say` enforces it because every phrase passes through
`say`: roughly half the speech comes from outside the announcer (the recorder, CallMe, the
virtual-devices follow-up after a USB mode change). `announcer.py` sets and clears it.
"""
from __future__ import annotations

import array
import os
import threading
import wave
from dataclasses import dataclass

import diagnostics
import settings_file
import tones
import wave_player
import wording

enabled = threading.Event()

# Which Windows output device announcements go to (a name from sapi_lists()); "" = the system
# default. When the default output is the RODECaster, speech arrives on
# USB 1 and is inaudible while another channel is SOLOed (the cue bus replaces the headphone feed)
# or while USB 1 is muted. Pointing announcements at another device makes them survive both.
# IT IS THE DEVICE OUR OWN PLAYER OPENS, NOT a Windows setting: it does not move the system default
# device, and it does not touch the screen reader, which has its own.
chosen_output = str(settings_file.load().get("announcer_output", "") or "")

# Which voice speaks them ("" = whatever SAPI's default is).
chosen_voice = str(settings_file.load().get("announcer_voice", "") or "")

# How loud announcements are, 0-100. It scales our own samples, so it changes ONLY our speech:
# Windows system volume, other apps and the desk's own audio are untouched. It exists because at
# full volume the announcer talks over what you are actually listening to while riding the faders.
# Persisted, because a level you have to set again every launch is no use.
_VOLUME_DEFAULT = 100


def clamp_volume(value) -> int:
    """A percentage the UI or the settings file offered, forced into 0-100. A bad value falls back
    to full volume rather than to silence: silence would look like the announcer being broken."""
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return _VOLUME_DEFAULT


volume = clamp_volume(settings_file.load().get("announcer_volume", _VOLUME_DEFAULT))


# WHAT SPEECH IS AVAILABLE. Both lists come from SAPI itself (the engine the speech thread renders
# with, and whose output tokens it opens), so what the page offers is exactly what can be chosen:
# no guessing from the registry, and nothing offered that the speech thread would then fail to find.
# The lists are CACHED because reading them costs a COM round trip, and re-read on demand: plugging
# in headphones adds an output, and installing a voice adds a voice, without restarting the app.
_lists_cache = {"voices": None, "outputs": None}
_lists_lock = threading.Lock()


def sapi_lists(refresh: bool = False) -> dict:
    """{"voices": [...], "outputs": [...]} of SAPI names, or empty lists if speech is unavailable.

    COM has to be initialised per THREAD, and this runs on whichever HTTP worker thread took the
    request, so CoInitialize/CoUninitialize wrap the call. A machine with no speech stack at all
    reports empty lists rather than failing the request: the announcer simply has nothing to choose
    from, and the rest of the page still works."""
    with _lists_lock:
        if not refresh and _lists_cache["voices"] is not None:
            return dict(_lists_cache)
        voices, outputs = [], []
        try:
            import pythoncom  # noqa: PLC0415  (pywin32, Windows only)
            import win32com.client  # noqa: PLC0415
            pythoncom.CoInitialize()
            try:
                spv = win32com.client.Dispatch("SAPI.SpVoice")
                voices = [t.GetDescription() for t in spv.GetVoices()]
                outputs = [t.GetDescription() for t in spv.GetAudioOutputs()]
            finally:
                # EVERY COM reference has to be dropped BEFORE CoUninitialize, or the release
                # happens on a thread with no apartment left and pywin32 prints "Win32 exception
                # occurred releasing IUnknown". Harmless, but it is real noise in the log and the
                # sort of thing that gets blamed for an unrelated fault later.
                spv = None
                pythoncom.CoUninitialize()
        except Exception as exc:
            diagnostics.event("announcer.sapi_unread", "voices and outputs not listed",
                              error=str(exc))
        _lists_cache["voices"] = voices
        _lists_cache["outputs"] = outputs
        return dict(_lists_cache)


# NOTHING IS EVER PLAYED OUT LOUD UNDER THIS FLAG, set by `DevTools/_bootstrap` for any script
# named `test_*`. The announcer speaks to the default output, which on the machines
# this app is built for IS THE DESK - so an offline test that happens to exercise an announcing
# path plays tones and speech into the user's monitoring, possibly mid-recording, with nothing on
# screen to explain it. Phrases are still LOGGED (see say), so a test can still be
# diagnosed; they simply never reach an audio device.
SILENT = bool(os.environ.get("ACCESSCASTER_SILENT"))


# A phrase plays for at least this long before a newer one may cut it off, so a moving fader is
# heard as a run of numbers rather than a run of syllables. Cues and say_and_wait phrases are never
# cut off.
PACE_S = 0.20
_SAY_AND_WAIT_TIMEOUT_S = 4.0  # longest anyone is held up if the phrase is never played

# Bumped whenever the voice or the output device changes. The speech thread owns the voice and the
# open device, so a change cannot be applied from here: the thread sees the new generation and
# takes up the new pair before its next phrase.
config_gen = 0


@dataclass
class _Item:
    """One thing for the speech thread to play: a phrase, or a cue WAV with the words it stands for,
    which are spoken instead if the file cannot be played."""
    text: str
    cue_path: str = ""
    # Set once a say_and_wait phrase has played, been superseded, or failed.
    done: threading.Event | None = None

    @property
    def interruptible(self) -> bool:
        return not self.cue_path and self.done is None


# ONE SLOT, NEWEST WINS. A burst of presses lands on the latest state instead of playing out a
# backlog seconds behind the operator's fingers, which is how a screen reader behaves too.
_pending: _Item | None = None
_pending_lock = threading.Lock()
_wake = threading.Event()
# Signalled with every new item, to cut off the interruptible phrase being played.
_interrupt = wave_player.Signal()
_thread = None
_thread_lock = threading.Lock()


def _release(item: _Item | None) -> None:
    """Free whoever waits on `item`, whether it was played, superseded or failed."""
    if item is not None and item.done is not None:
        item.done.set()


class _Engine:
    """SAPI rendering into memory and a held-open player, both owned by the speech thread."""

    SAMPLE_RATE = tones.SAMPLE_RATE
    _RATE = 3                       # SAPI rate, -10 (slow) .. 10 (fast)
    # SpeechAudioFormatType SAFT22kHz16BitMono.
    _FORMAT_22K_16BIT_MONO = 22
    # Speak flag: never parse the text as SAPI XML, so a channel name containing "<" is read as
    # written.
    _IS_NOT_XML = 16
    # Peak sample level, of 32767, where a word starts (about -42 dBFS). A word opening on a plosive
    # carries 140 to 170 ms of near-silence peaking near 100 before the burst, while soft sounds
    # (the "f" of "fader", the "s" of "solo") reach several hundred.
    _ONSET = 256
    # The trailing padding is exact zeros, so any sample above this is still speech.
    _SILENCE = 16
    # Kept either side of the speech, so a soft first or last sound is not clipped.
    _LEAD_IN = SAMPLE_RATE * 20 // 1000
    _TAIL = SAMPLE_RATE * 20 // 1000

    def __init__(self):
        self._voice = None
        self._format = None
        self._gen = None
        self._output = ""
        self._device_id = wave_player.DEFAULT_DEVICE
        self._player = wave_player.Player(self.SAMPLE_RATE)
        self._cues: dict = {}

    def play(self, item: _Item) -> None:
        self._configure()
        pcm = self._cue(item.cue_path) if item.cue_path else None
        if pcm is None:
            pcm = self._render(item.text)
        pcm = _scaled(pcm, volume)
        if not pcm:
            return
        if not self._player.is_open:
            self._device_id = self._resolve_output()
        self._player.play(pcm, self._device_id,
                          _interrupt if item.interruptible else None, int(PACE_S * 1000))

    def idle(self) -> None:
        self._player.idle()

    def reset(self) -> None:
        """Drop the voice and the device after a failure, so the next phrase starts afresh."""
        self._voice = None
        self._gen = None
        self._player.close()

    def _configure(self) -> None:
        """Take up a changed voice or output before the next phrase."""
        gen = config_gen
        if self._voice is not None and self._gen == gen:
            return
        import win32com.client  # noqa: PLC0415  (pywin32, Windows only)
        voice, self._output = chosen_voice, chosen_output
        spv = win32com.client.Dispatch("SAPI.SpVoice")
        spv.Rate = self._RATE
        if voice:
            token = _token(spv.GetVoices(), voice)
            if token is not None:
                spv.Voice = token
            else:
                diagnostics.event("announcer.voice_missing", voice, using="the default voice")
        self._format = win32com.client.Dispatch("SAPI.SpAudioFormat")
        self._format.Type = self._FORMAT_22K_16BIT_MONO
        self._voice = spv
        self._gen = gen
        self._player.close()            # the next phrase opens the output now chosen

    def _resolve_output(self) -> int:
        return _output_device(self._voice, self._output)

    def _render(self, text: str) -> bytes:
        """`text` spoken into memory, with the voice's silence trimmed from both ends."""
        import win32com.client  # noqa: PLC0415
        stream = win32com.client.Dispatch("SAPI.SpMemoryStream")
        stream.Format = self._format
        self._voice.AudioOutputStream = stream
        self._voice.Speak(text, self._IS_NOT_XML)
        samples = array.array("h", bytes(stream.GetData()))
        first = next((i for i, s in enumerate(samples) if abs(s) > self._ONSET), None)
        if first is None:
            return b""
        last = next(i for i in range(len(samples) - 1, first - 1, -1)
                    if abs(samples[i]) > self._SILENCE)
        start = max(0, first - self._LEAD_IN)
        end = min(len(samples), last + 1 + self._TAIL)
        return samples[start:end].tobytes()

    def _cue(self, path: str) -> bytes | None:
        """A cue's samples, or None if the file cannot be played, in which case its words are spoken
        instead. Played untrimmed: the ring's leading silence lets the output settle before the
        sound starts."""
        if path not in self._cues:
            try:
                with wave.open(path, "rb") as w:
                    ok = (w.getframerate() == self.SAMPLE_RATE and w.getnchannels() == 1
                          and w.getsampwidth() == 2)
                    self._cues[path] = w.readframes(w.getnframes()) if ok else None
            except (OSError, wave.Error):
                return None
        return self._cues[path]


def _output_device(voice, output: str) -> int:
    """The waveOut id of `output`, through `voice`, a SAPI SpVoice. Resolved at every open, because
    the id is a position in Windows' current device list and moves when a device is plugged in or
    removed. SAPI's output token creates an SpMMAudioOut, whose DeviceId is that waveOut id."""
    if not output:
        return wave_player.DEFAULT_DEVICE
    token = _token(voice.GetAudioOutputs(), output)
    if token is None:
        diagnostics.event("announcer.output_missing", output, using="the system default")
        return wave_player.DEFAULT_DEVICE
    return int(token.CreateInstance().DeviceId)


def _token(tokens, wanted: str):
    """The SAPI token whose name is `wanted`: an exact match wins, otherwise the first
    case-insensitive substring, as `_pick` accepts."""
    tokens = list(tokens)
    for t in tokens:
        if t.GetDescription() == wanted:
            return t
    low = wanted.lower()
    for t in tokens:
        if low in t.GetDescription().lower():
            return t
    return None


def _scaled(pcm: bytes, percent: int) -> bytes:
    """16-bit samples at `percent` of their level."""
    if percent >= 100 or not pcm:
        return pcm
    if percent <= 0:
        return b""
    gain = percent / 100
    samples = array.array("h")
    samples.frombytes(pcm)
    return array.array("h", (int(s * gain) for s in samples)).tobytes()


def _speech_thread() -> None:
    """Play whatever is newest, one item at a time, and close the device once speech goes quiet."""
    import pythoncom  # noqa: PLC0415
    global _pending
    pythoncom.CoInitialize()
    engine = _Engine()
    while True:
        if not _wake.wait(1.0):
            engine.idle()
            continue
        with _pending_lock:
            item, _pending = _pending, None
            _wake.clear()
            _interrupt.clear()
        if item is None:
            continue
        try:
            engine.play(item)
        except Exception as exc:
            diagnostics.event("announcer.speech_failed", str(exc), text=item.text[:90])
            engine.reset()
        finally:
            _release(item)


def _submit(item: _Item, logged: str) -> None:
    """Log `item`, then hand it to the speech thread unless announcements are off or this is a test.
    Never blocks: a newer item replaces one still waiting, and cuts off a phrase already playing.

    LOGGED, BECAUSE SPEECH IS THIS APP'S MAIN OUTPUT. Without this line, everything sent to the
    desk is traceable and nothing said to the USER is, which is the wrong way round for an app
    whose whole purpose is telling a blind operator what the desk just did. A SUPPRESSED item is
    logged too, marked: "it said nothing" and "it was never asked to" are different faults.

    THE ANNOUNCER SWITCH IS ENFORCED HERE, because it is the only place every phrase passes
    through: roughly half the speech comes from outside the announcer's read loop (the
    virtual-devices follow-up after a USB mode change, the recorder watcher). The phrases
    announcing that speech is BACK are safe: each one sets the flag before it speaks.
    """
    global _pending, _thread
    if not enabled.is_set():
        diagnostics.event("say", logged + "  [suppressed: announcements off]")
        _release(item)
        return
    diagnostics.event("say", logged)
    if SILENT:
        _release(item)
        return                  # a test run: recorded above, never played. See SILENT.
    with _thread_lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_speech_thread, name="speech", daemon=True)
            _thread.start()
    with _pending_lock:
        _release(_pending)
        _pending = item
        _wake.set()
        _interrupt.set()


def say(text: str) -> None:
    """Speak one phrase. Never blocks the caller: the announcer's read loop must keep draining the
    HID queue."""
    _submit(_Item(text), text[:90])


def say_and_wait(text: str) -> None:
    """Speak `text` and return once it has finished playing, so the caller can act after it: the
    words "Resuming" must be over before the desk starts recording again. Nothing cuts it off once
    it starts, but a newer item queued before then replaces it. Returns at once when announcements
    are off, and after _SAY_AND_WAIT_TIMEOUT_S at worst."""
    done = threading.Event()
    _submit(_Item(text, done=done), text[:90])
    done.wait(_SAY_AND_WAIT_TIMEOUT_S)


def tone(name: str) -> bool:
    """Play the named cue instead of a phrase. True if it was queued, False if we have no such
    tone, in which case the caller must speak: a cue that silently does nothing is worse than the
    sentence it was replacing. Nothing cuts a cue off."""
    try:
        path = tones.tone_path(name)
    except Exception:
        return False
    if not path:
        return False
    _submit(_Item(name, cue_path=path), "cue " + name)
    return True


class Loop:
    """A cue repeated until `stop`, on a device handle of its own, so speech and the other cues play
    over it rather than cutting it off. It starts after `delay_s`, so something over sooner makes
    no sound."""

    def __init__(self, path: str, delay_s: float):
        self._stopped = threading.Event()
        self._cut = wave_player.Signal()
        threading.Thread(target=self._run, args=(path, delay_s), name="cue loop",
                         daemon=True).start()

    def stop(self) -> None:
        self._stopped.set()
        self._cut.set()

    def _run(self, path: str, delay_s: float) -> None:
        import pythoncom  # noqa: PLC0415
        if self._stopped.wait(delay_s):
            self._cut.close()
            return
        pythoncom.CoInitialize()
        player = wave_player.Player(tones.SAMPLE_RATE)
        try:
            with wave.open(path, "rb") as w:
                pcm = _scaled(w.readframes(w.getnframes()), volume)
            import win32com.client  # noqa: PLC0415
            device = _output_device(win32com.client.Dispatch("SAPI.SpVoice"), chosen_output)
            while pcm and not self._stopped.is_set() and player.play(pcm, device, self._cut):
                pass
        except Exception as exc:
            diagnostics.event("announcer.loop_failed", str(exc))
        finally:
            player.close()
            self._cut.close()
            pythoncom.CoUninitialize()


def start_loop(name: str, delay_s: float = 0.0) -> Loop | None:
    """Repeat the named cue until the returned loop's `stop`. None, and nothing plays, with
    announcements off (like every cue), in a test run, or for a cue we do not have."""
    if not enabled.is_set() or SILENT:
        return None
    try:
        path = tones.tone_path(name)
    except Exception:
        return None
    return Loop(path, delay_s) if path else None


def action_set_announce_volume(percent: int) -> dict:
    """How loud announcements are. It scales our own samples, so this changes OUR speech only and never
    Windows volume, another app, or the desk's audio. The confirmation is spoken, which is the
    point: it arrives at the new level, so you hear what you have chosen straight away."""
    global volume
    volume = clamp_volume(percent)
    settings_file.update(announcer_volume=volume)
    msg = (f"Announcement volume {volume} percent." if volume
           else "Announcements are silenced.")
    if volume:
        say(msg)                     # played at the new volume
    return {"ok": True, "volume": volume, "message": msg}


def _pick(value: str, offered: list) -> str | None:
    """Resolve what the page sent against what SAPI actually offers. An exact name wins; failing
    that a case-insensitive substring is accepted, so a shortened name ("Zira", "Realtek") still
    works from a script or an older saved setting. Anything else is rejected rather than passed to
    the speech thread, where it would silently fall back to the default and look like the setting
    was ignored. "" means "leave it to Windows"."""
    value = (value or "").strip()
    if not value:
        return ""
    for name in offered:
        if name == value:
            return name
    low = value.lower()
    for name in offered:
        if low in name.lower():
            return name
    return None


def action_set_announce_voice(voice: str) -> dict:
    """Which voice speaks announcements. The confirmation is spoken IN THE NEW VOICE, which is the
    point: the sample is the first thing the new voice says."""
    return _choose("voice", voice)


def action_set_announce_output(output: str) -> dict:
    """Which Windows output announcements play through. THIS IS OUR SPEECH ONLY: it is the device our
    own player opens, so it does not move the system default device, and it does not
    touch the screen reader or any other program. It exists because the default output is often
    the RODECaster itself, and then announcements vanish the moment another channel is soloed."""
    return _choose("output", output)


def _choose(kind: str, wanted: str) -> dict:
    """Set the announcer's "voice" or "output" to one this computer offers, save it, and confirm in
    the new voice on the new output. "" is the default."""
    global chosen_voice, chosen_output, config_gen
    voice = kind == "voice"
    picked = _pick(wanted, sapi_lists()["voices" if voice else "outputs"])
    if picked is None:
        return {"ok": False, kind: chosen_voice if voice else chosen_output,
                "message": (f"This computer has no speech voice called {wanted}." if voice else
                            f"This computer has no audio output called {wanted}.")}
    if voice:
        chosen_voice = picked
        settings_file.update(announcer_voice=picked)
        msg = (f"Announcements will now be spoken by {picked}." if picked
               else "Announcements will now use the computer's default voice.")
    else:
        chosen_output = picked
        settings_file.update(announcer_output=picked)
        msg = (f"Announcements will now play through {picked}." if picked
               else "Announcements will now play through the computer's default output.")
    config_gen += 1
    say(msg)
    return {"ok": True, kind: picked, "message": msg}


def action_refresh_speech_devices() -> dict:
    """Re-read the voice and output lists from SAPI. Plugging in headphones or installing a voice
    changes them, and neither is worth restarting the app for."""
    lists = sapi_lists(refresh=True)
    return {"ok": True, "voices": lists["voices"], "outputs": lists["outputs"],
            "message": f"{wording.count(len(lists['voices']), 'voice')} and "
                       f"{wording.count(len(lists['outputs']), 'output')} found."}


def apply_settings(changes: dict) -> None:
    """Push restored speech preferences into the running speech thread, not just the file. A voice
    or output change bumps `config_gen`, which the thread takes up before its next phrase."""
    global volume, chosen_voice, chosen_output, config_gen
    if "announcer_volume" in changes:
        volume = clamp_volume(changes["announcer_volume"])
    if "announcer_voice" in changes:
        chosen_voice = str(changes["announcer_voice"] or "")
        config_gen += 1
    if "announcer_output" in changes:
        chosen_output = str(changes["announcer_output"] or "")
        config_gen += 1
