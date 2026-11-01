"""Everything the app says out loud: one persistent SAPI worker (say.ps1) fed a phrase at a time.

`say` queues a phrase and never blocks; `say_and_wait` blocks until the worker has spoken it; `tone`
plays a cue through the same voice. The voice, output and volume are the user's choice, held here
and saved in settings.json.

`enabled` IS THE ANNOUNCEMENTS SWITCH, and `say` enforces it because every phrase passes through
`say`: roughly half the speech comes from outside the announcer (the recorder, CallMe, the
virtual-devices follow-up after a USB mode change). `announcer.py` sets and clears it.
"""
from __future__ import annotations

import os
import queue
import subprocess
import threading
import time

import diagnostics
import settings_file
import tones
import wording
from paths import resource_path

enabled = threading.Event()

# The app runs under pythonw (no console), so a console subprocess would flash a window and steal
# focus.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


# SPEECH: one PERSISTENT worker process, fed a phrase per line on stdin (say.ps1 run without
# -Text). NOT one `say.ps1 -Text` process per phrase: that costs 1.3-1.7 s each (PowerShell and
# speech-stack startup, not the speaking), and since the announcer speaks on every mute/solo press, a few quick
# presses spawn several such processes all fighting for the audio device, so announcements lag and
# then appear to stop. With the worker, only the FIRST phrase pays startup. If the worker cannot be
# started we fall back to the one-shot, so the worst case is slow rather than silent.
_queue: "queue.Queue[str]" = queue.Queue()
_SAY_QMAX = 1          # newest wins: the worker purges anyway, so queueing only adds lag
PACE_S = 0.20         # never hand the worker phrases faster than this (see the pump)
_pump_thread = None
_pump_lock = threading.Lock()
# A phrase somebody waits on (say_and_wait) travels the same queue as "!sync <id> <text>". The
# worker speaks it synchronously and replies "DONE <id>" on stdout. A sync phrase that is dropped
# or superseded before it is spoken must still release its waiter (_release).
SYNC_PREFIX = "!sync "
_SYNC_TIMEOUT_S = 4.0  # longest anyone is held up if the worker never answers
_sync_waiters = {}
_sync_lock = threading.Lock()
_sync_next = 0
# Which Windows output device announcements go to (a name from sapi_lists(), matched by the
# worker); "" = the system default. When the default output is the RODECaster, speech arrives on
# USB 1 and is inaudible while another channel is SOLOed (the cue bus replaces the headphone feed)
# or while USB 1 is muted. Pointing announcements at another device makes them survive both.
# IT IS OUR SPEECH OBJECT'S OWN OUTPUT (SAPI SpVoice.AudioOutput), NOT a Windows setting: it does not
# move the system default device, and it does not touch the screen reader, which has its own.
chosen_output = str(settings_file.load().get("announcer_output", "") or "")

# Which voice speaks them ("" = whatever SAPI's default is). Matched by name in the worker.
chosen_voice = str(settings_file.load().get("announcer_voice", "") or "")

# Bumped whenever the voice or the output device changes. The pump owns the worker process and is the
# only thread allowed to touch it, so a change cannot restart the worker directly: it raises this
# counter and the pump respawns before the next phrase. Volume does NOT need this (the worker takes
# a "!vol N" control line in place), but voice and device are set when the SpVoice is created.
worker_gen = 0

# How loud announcements are, 0-100. This is SAPI's own stream volume, so it scales ONLY our speech:
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


# WHAT SPEECH IS AVAILABLE. Both lists come from SAPI itself (the same engine the worker speaks
# through), so what the page offers is exactly what can be chosen - no guessing from the registry,
# and nothing offered that the worker would then fail to find.
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


# The queue between say and the speech worker carries plain phrases. A line starting with this
# prefix is a CUE instead: the worker plays the named WAV through the same SpVoice (see
# say.ps1), so it lands on the announcer's chosen output rather than the system default.
WAV_PREFIX = "!wav "

# NOTHING IS EVER PLAYED OUT LOUD UNDER THIS FLAG, set by `DevTools/_bootstrap` for any script
# named `test_*`. The announcer speaks through SAPI to the default output, which on the machines
# this app is built for IS THE DESK - so an offline test that happens to exercise an announcing
# path plays tones and speech into the user's monitoring, possibly mid-recording, with nothing on
# screen to explain it. Phrases are still LOGGED (see say), so a test can still be
# diagnosed; they simply never reach an audio device.
SILENT = bool(os.environ.get("ACCESSCASTER_SILENT"))


def _wav_spoken_form(wav_path: str) -> str:
    """What a cue would have SAID, for the paths that cannot play it. Derived from the file name so
    a new cue cannot forget to add itself here and end up silent."""
    name = os.path.splitext(os.path.basename(wav_path or ""))[0]
    return {"muted": "muted", "unmuted": "unmuted"}.get(name, name or "")


def tone(name: str) -> bool:
    """Queue the named tone instead of a phrase. True if it was queued, False if we have no such
    tone, in which case the caller must speak - a cue that silently does nothing is worse than the
    sentence it was replacing."""
    try:
        path = tones.tone_path(name)
    except Exception:
        return False
    if not path:
        return False
    say(WAV_PREFIX + path)
    return True


def _say_oneshot(text: str) -> None:
    """One process per phrase, used only when the worker cannot take it (see _pump).
    Carries the same volume, VOICE and OUTPUT DEVICE as the worker, so degrading to this path does
    not suddenly get loud again, change voice, or (worst of the three) start speaking into the
    desk."""
    sync_id = None
    if text.startswith(SYNC_PREFIX):
        sync_id, _, text = text[len(SYNC_PREFIX):].partition(" ")
    try:
        # A CUE IS NOT A PHRASE. The queue carries "!wav <path>" for the mute tones (see tone),
        # and without this the fallback path would cheerfully read a Windows file path aloud.
        wav = ""
        if text.startswith(WAV_PREFIX):
            wav = text[len(WAV_PREFIX):]
            text = _wav_spoken_form(wav)
        cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-File",
               resource_path("say.ps1"), "-Text", text,
               "-Volume", str(volume)]
        if wav:
            cmd += ["-Wav", wav]
        if chosen_voice:
            cmd += ["-Voice", chosen_voice]
        if chosen_output:
            cmd += ["-Device", chosen_output]
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=_NO_WINDOW)
        if sync_id is not None:
            p.wait(timeout=_SYNC_TIMEOUT_S)    # say.ps1 exits when it has finished speaking
    except Exception as exc:
        diagnostics.event("announcer.oneshot_failed", str(exc))
    if sync_id is not None:
        _sync_done(sync_id)


def _sync_done(sync_id: str) -> None:
    with _sync_lock:
        ev = _sync_waiters.pop(sync_id, None)
    if ev is not None:
        ev.set()


def _release(text) -> None:
    """Free whoever waits on `text` if it is a sync phrase that will now never be spoken."""
    if isinstance(text, str) and text.startswith(SYNC_PREFIX):
        _sync_done(text[len(SYNC_PREFIX):].partition(" ")[0])


def _worker_replies(proc) -> None:
    """Read the speech worker's stdout until it exits, releasing each finished sync phrase."""
    try:
        for line in proc.stdout:
            if line.startswith("DONE "):
                _sync_done(line[5:].strip())
    except Exception as exc:
        diagnostics.event("announcer.worker_replies_lost", str(exc))


def say_and_wait(text: str) -> None:
    """Speak `text` and return once it has finished playing, so the caller can act after it: the
    words "Resuming" must be over before the desk starts recording again. Returns at once when
    announcements are off, and after _SYNC_TIMEOUT_S at worst."""
    global _sync_next
    if not enabled.is_set() or SILENT:
        say(text)                     # logged, and suppressed or silent there
        return
    ev = threading.Event()
    with _sync_lock:
        _sync_next += 1
        sync_id = str(_sync_next)
        _sync_waiters[sync_id] = ev
    say(f"{SYNC_PREFIX}{sync_id} {text}")
    ev.wait(_SYNC_TIMEOUT_S)
    with _sync_lock:
        _sync_waiters.pop(sync_id, None)


def _pump() -> None:
    """Own the speech worker: start it, feed it phrases, restart it if it dies.

    COALESCE AND PACE. The worker speaks with purge-before-speak, so if we hand it five phrases it
    dutifully starts and cancels five utterances, and each stop-and-restart costs real time, which is
    heard as the announcer pausing for a second before it says anything. So: whenever more than one
    phrase is waiting, only the NEWEST is sent (older ones are already obsolete by definition),
    and we never hand over faster than PACE_S. A moving fader then speaks about five times a
    second, every phrase audible, none of them queued behind a cancelled one."""
    proc = None
    last_sent = 0.0
    sent_vol = None          # the volume the LIVE worker was last told, so a change is sent once
    spawned_gen = -1         # the voice/device generation the LIVE worker was started with
    while True:
        text = _queue.get()
        if text is None:                                   # shutdown sentinel
            break
        # Anything already waiting supersedes what we just took.
        while True:
            try:
                newer = _queue.get_nowait()
            except queue.Empty:
                break
            if newer is None:
                return
            _release(text)
            text = newer
        # Pace the hand-off, preferring anything that arrives while we wait.
        gap = PACE_S - (time.monotonic() - last_sent)
        while gap > 0:
            try:
                newer = _queue.get(timeout=gap)
            except queue.Empty:
                break
            if newer is None:
                return
            _release(text)
            text = newer
            gap = PACE_S - (time.monotonic() - last_sent)
        last_sent = time.monotonic()
        if proc is not None and proc.poll() is not None:    # worker died; drop it and respawn
            proc = None
        gen = worker_gen                               # read once, with the values below
        voice, device = chosen_voice, chosen_output
        if proc is not None and spawned_gen != gen:
            # The voice or the output device changed. Both are fixed when the worker creates its
            # SpVoice, so the running one has to go; the replacement is started immediately below and
            # speaks the confirmation the action queued, which is how you hear the new voice.
            try:
                proc.stdin.write("__QUIT__\n"); proc.stdin.flush()
            except Exception:
                try: proc.kill()
                except Exception: pass
            proc = None
        vol = volume                                   # read once; it can change under us
        if proc is None:
            cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-File",
                   resource_path("say.ps1"), "-Volume", str(vol)]
            if voice:
                cmd += ["-Voice", voice]
            if device:
                cmd += ["-Device", device]
            try:
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW,
                                        text=True, bufsize=1)
                # Drained for as long as the worker lives: it writes a line per phrase, and an
                # unread pipe fills and blocks it mid-sentence.
                threading.Thread(target=_worker_replies, args=(proc,), daemon=True).start()
                sent_vol = vol
                spawned_gen = gen
            except Exception:
                _say_oneshot(text)                      # no worker: degrade, do not go silent
                continue
        try:
            # A volume change rides in front of the next phrase rather than being pushed to the
            # worker the moment the slider moves: the pump is the only thread that owns proc, so
            # nothing else may write to its stdin. Nothing is lost by waiting, since volume only
            # matters when something is about to be spoken.
            if sent_vol != vol:
                proc.stdin.write(f"!vol {vol}\n")
                sent_vol = vol
            proc.stdin.write(text + "\n")
            proc.stdin.flush()
        except Exception:
            try: proc.kill()
            except Exception: pass
            proc = None
            _say_oneshot(text)
    if proc is not None:
        try:
            proc.stdin.write("__QUIT__\n"); proc.stdin.flush()
        except Exception:
            try: proc.kill()
            except Exception: pass


def say(text: str) -> None:
    """Queue one phrase for the speech worker. Never blocks the caller (the announcer's read loop
    must keep draining the HID queue), and never lets a burst build a backlog: past _SAY_QMAX
    the OLDEST pending phrase is dropped, because the newest state is the one worth hearing.

    LOGGED, BECAUSE SPEECH IS THIS APP'S MAIN OUTPUT. Without this line, everything sent to the
    desk is traceable and nothing said to the USER is, which is the wrong way round for an app
    whose whole purpose is telling a blind operator what the desk just did: a burst of repeating
    cues then arrives with the log silent either side, and nothing says what fired them or how many
    there were. A cue is logged by NAME rather than as a file path, so a run of them reads as a run
    of them. A SUPPRESSED phrase is logged too, marked: "it said nothing" and "it was never asked
    to" are different faults and the log has to tell them apart.

    THE ANNOUNCER SWITCH IS ENFORCED HERE, because it is the only place every phrase passes
    through: roughly half the speech comes from outside the announcer's read loop (the
    virtual-devices follow-up after a USB mode change, the recorder watcher). The phrases
    announcing that speech is BACK are safe: each one sets the flag before it speaks.
    """
    if text.startswith(WAV_PREFIX):
        what = "cue " + os.path.basename(text[len(WAV_PREFIX):]).split("-")[0]
    elif text.startswith(SYNC_PREFIX):
        what = text[len(SYNC_PREFIX):].partition(" ")[2][:90]
    else:
        what = text[:90]
    if not enabled.is_set():
        diagnostics.event("say", what + "  [suppressed: announcements off]")
        _release(text)
        return
    diagnostics.event("say", what)
    if SILENT:
        _release(text)
        return                  # a test run: recorded above, never played. See SILENT.
    global _pump_thread
    with _pump_lock:
        if _pump_thread is None or not _pump_thread.is_alive():
            _pump_thread = threading.Thread(target=_pump, daemon=True)
            _pump_thread.start()
    while _queue.qsize() >= _SAY_QMAX:
        try:
            _release(_queue.get_nowait())
        except queue.Empty:
            break
    _queue.put_nowait(text)


def action_set_announce_volume(percent: int) -> dict:
    """How loud announcements are. SAPI stream volume, so this changes OUR speech only and never
    Windows volume, another app, or the desk's audio. The confirmation is spoken, which is the
    point: it arrives at the new level, so you hear what you have chosen straight away."""
    global volume
    volume = clamp_volume(percent)
    settings_file.update(announcer_volume=volume)
    msg = (f"Announcement volume {volume} percent." if volume
           else "Announcements are silenced.")
    if volume:
        say(msg)                     # rides in behind the new volume; see _pump
    return {"ok": True, "volume": volume, "message": msg}


def _pick(value: str, offered: list) -> str | None:
    """Resolve what the page sent against what SAPI actually offers. An exact name wins; failing
    that a case-insensitive substring is accepted, so a shortened name ("Zira", "Realtek") still
    works from a script or an older saved setting. Anything else is rejected rather than passed to
    the worker, where it would silently fall back to the default and look like the setting was
    ignored. "" means "leave it to Windows"."""
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
    point: the worker is restarted with it and the sample is the first thing it says."""
    return _choose("voice", voice)


def action_set_announce_output(output: str) -> dict:
    """Which Windows output announcements play through. THIS IS OUR SPEECH ONLY: it sets the audio
    output on our own SAPI voice, so it does not move the system default device, and it does not
    touch the screen reader or any other program. It exists because the default output is often
    the RODECaster itself, and then announcements vanish the moment another channel is soloed."""
    return _choose("output", output)


def _choose(kind: str, wanted: str) -> dict:
    """Set the announcer's "voice" or "output" to one this computer offers, restart the speech
    worker with it, save it, and confirm through the restarted worker. "" is the default."""
    global chosen_voice, chosen_output, worker_gen
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
    worker_gen += 1
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
    """Push restored speech preferences into the running worker, not just the file. The voice and
    the output are fixed when the worker starts, so a change bumps `worker_gen` and the pump
    restarts it before the next phrase."""
    global volume, chosen_voice, chosen_output, worker_gen
    if "announcer_volume" in changes:
        volume = clamp_volume(changes["announcer_volume"])
    if "announcer_voice" in changes:
        chosen_voice = str(changes["announcer_voice"] or "")
        worker_gen += 1
    if "announcer_output" in changes:
        chosen_output = str(changes["announcer_output"] or "")
        worker_gen += 1
