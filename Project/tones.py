"""AccessCaster - the announcer's non-speech cues: short tones written as WAV files.

WHY A TONE AT ALL (Steven's request). Mute is the one announcement that arrives
too late to be useful as a sentence: you hit mute mid-word, and "Mic 2 muted" takes most of a second
to say. Two tones tell you the same thing in a tenth of that, and they do not talk over the thing you
muted for. Speech stays the default and stays available; this is an option, not a replacement.

WHY WAV FILES RATHER THAN A BEEP. `winsound.Beep` and `[Console]::Beep` play on the SYSTEM default
output, and on the machines this app runs on the system default IS the RODECaster. A cue that goes
down the desk is a cue your callers hear and you might not, which is the exact fault the announcer's
own output picker exists to avoid. A WAV can be played through SAPI's `SpeakStream` on the SAME
SpVoice the speech worker already owns, so it inherits the chosen output device and the announcer's
volume for free, and nothing new has to learn about audio endpoints.

WHY GENERATED RATHER THAN SHIPPED. They are two sine waves; generating them is a dozen lines and
removes two binaries from the repo, the installer and the bundle. They are written once into the
user data dir and reused, so the cost is paid on the first cue after an install.

EACH CUE IS TWO NOTES, AND THE DIRECTION CARRIES THE MEANING: falling for muted, rising for
unmuted. NOT one low tone and one high tone, which is only tellable apart if you hear both together
or already know the pitch. Direction survives a laptop speaker, a mix, and hearing one cue on its
own, which is how these are actually used. Falling means muted because down means off in every other
audible convention.
"""
from __future__ import annotations

import glob
import math
import os
import struct
import wave

from paths import data_path

# 22.05 kHz because that is SAPI's own native rate and these play through SAPI (see below). 44.1 kHz
# was tried first and also plays, so this is a preference rather than a requirement - an earlier
# version of this comment claimed 44.1 was silent, which was a wrong reading of a listening test.
SAMPLE_RATE = 22050
PEAK = 0.35                # conservative: this plays on whatever the announcer is pointed at
FADE_S = 0.008             # a click at either end is louder than the tone; this removes both

# name -> the notes to play, in order, in Hz. TWO NOTES EACH, MOVING IN OPPOSITE DIRECTIONS: a
# single low tone and a single high tone are only tellable apart if you hear both together or
# already know the pitch, while a PAIR that falls and a pair that rises is tellable apart in
# isolation, on a laptop speaker, under a mix, which is where these are actually heard.
#
# Falling means muted, in line with every other audible convention for something switching off.
TONES = {
    "muted": (660.0, 440.0),      # E5 down to A4
    "unmuted": (440.0, 660.0),    # A4 up to E5
    # JOB CUES. A cue lands before the sentence does, so the outcome of a job is known before the
    # words start, which a spoken ending alone does not manage against the pad line before it.
    # THREE NOTES, NOT TWO: two notes already mean a mute, and a job that ended must not sound like
    # a channel going quiet. Rising for finished, falling for failed, so the two pass the same
    # tellable-apart test as the mute pair.
    "done": (440.0, 587.33, 880.0),     # A4, D5, A5
    "failed": (880.0, 587.33, 440.0),   # the same three, coming down
}
# A cue is two notes and the gap between them. Kept short: the whole point is to beat the sentence
# it replaces, and the pair still lands in well under a fifth of a second.
NOTE_S = 0.07
GAP_S = 0.025
# The whole cue, for callers that need to know how long it occupies the speech worker (see
# server.action_set_announce_mute_tones, which plays both cues in turn). TWO notes: this timing is
# quoted for the mute pair, and the three-note job cues are not on anyone's critical path.
DURATION_S = 2 * NOTE_S + GAP_S
# Bumped whenever the SHAPE of a cue changes, because the file on disk is not regenerated just
# because the code changed its mind: it is part of the file name, so a new shape is simply a
# different file and the old one is cleaned up on the way past. Without it, anyone who has already
# heard an older shape keeps hearing it for good.
TONE_VERSION = 3


def _write_wav(path: str, freqs) -> None:
    frames = int(SAMPLE_RATE * NOTE_S)
    fade = max(1, int(SAMPLE_RATE * FADE_S))
    gap = bytes(2 * int(SAMPLE_RATE * GAP_S))
    out = bytearray()
    for i, freq in enumerate(freqs):
        if i:
            out += gap
        for n in range(frames):
            # Linear fade in and out, PER NOTE. Both ends matter: without the fade the step between
            # the two notes is a click, and a click is louder and more startling than the cue.
            gain = min(1.0, n / fade, (frames - n) / fade)
            sample = PEAK * gain * math.sin(2.0 * math.pi * freq * n / SAMPLE_RATE)
            out += struct.pack("<h", int(max(-1.0, min(1.0, sample)) * 32767))
    tmp = path + ".part"
    with wave.open(tmp, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(bytes(out))
    os.replace(tmp, path)          # never leave a half-written file where a player will find it


def _usable(path: str) -> bool:
    """Is there already a good file here? Checks the FORMAT as well as existence, so a file left by
    an older build - or a truncated one - is replaced rather than trusted. "It is there" is not the
    same as "it is the file this code means to play", and a wrong one fails silently."""
    try:
        if os.path.getsize(path) == 0:
            return False
        with wave.open(path, "rb") as w:
            return (w.getframerate() == SAMPLE_RATE and w.getnchannels() == 1
                    and w.getsampwidth() == 2)
    except (OSError, wave.Error):
        return False


def tone_path(name: str) -> str:
    """Absolute path to the named tone's WAV, generating it if it is not there yet.

    Written through a .part file and renamed, so an interrupted first run cannot leave a truncated
    WAV that plays as a click forever after. Returns "" for a name we do not have, rather than
    raising: a missing cue must never take the announcer down with it.
    """
    freq = TONES.get(name)
    if not freq:
        return ""
    folder = data_path("tones")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{name}-v{TONE_VERSION}.wav")
    if not _usable(path):
        try:
            _write_wav(path, freq)
        except OSError:
            return ""
        # Sweep up any earlier shape of this cue, so the folder does not accumulate one file per
        # version for ever. Best effort: a file we cannot delete is clutter, not a fault.
        # `{name}*.wav`, not `{name}-v*.wav`: the first shape shipped had no version in its name at
        # all, and a pattern that only matches versioned files leaves it behind for ever.
        for old_name in glob.glob(os.path.join(folder, f"{name}*.wav")):
            if old_name != path:
                try:
                    os.remove(old_name)
                except OSError:
                    pass
    return path
