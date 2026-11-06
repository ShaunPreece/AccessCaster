"""Prove the announcer's ECHO SUPPRESSION on real hardware. Reversible: restores what it moves.

THE PROBLEM IT GUARDS: the desk pushes our OWN writes back on the state stream, so a level set from
the window is announced twice - once by the window's own reply, once by the announcer speaking the
echo. announcer.mark_self_write suppresses the second.

Why the test is shaped like this: the marks live in the process that DECODES the stream, so this
script is that process. The MARKED half calls the app's own action (`faders.action_set_fader_level`
and friends), which marks its write as the window's does, the fader-index to source-code
translation included; the UNMARKED half calls the raw writer underneath, which marks nothing. The
real echo is read off the script's own HID handle. Quit AccessCaster first.

Every path is tested in PAIRS, so a silent result cannot be mistaken for a broken listener:
  MARKED   - expect NOTHING spoken (the suppression working)
  UNMARKED - the CONTROL. Whether it speaks is the measurement, not the assumption.

Three paths:
  LEVEL  a fader level, marked by announcer.mark_self_level - the desk DOES echo it.
  MUTE   a channel mute, marked by announcer.mark_self_mute - the desk does NOT echo it as
         channelOutputMute. What it pushes instead is twelve `mixMute` records, one per output mix
         (the firmware propagating the mute into the matrix), and the announcer speaks none of them.
         So there is no second announcement on this path to suppress.
  GAIN   a mic gain, marked by announcer.mark_self_gain     - the desk pushes NOTHING back at all,
         though the write itself lands and reads back.

THAT ASYMMETRY IS THE FINDING, and it narrows the claim: "the desk pushes our own writes back" is
true of LEVELS, not of every write. The suppression on the mute and gain paths stays anyway - it
costs nothing, it is right if a firmware ever starts echoing, and removing it would leave the three
writers disagreeing about their own contract - but it is a no-op today rather than a proven fix.

So a silent UNMARKED half is a NOTE, not a pass and not a failure: the check prints what each path
does and fails only when a MARKED write is spoken (suppression genuinely broken) or when a path that
IS known to echo goes quiet (the listener died, and the marked half then proves nothing).

Usage: python DevTools/announcer_echo_check.py
"""
from __future__ import annotations

import time

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import announcer
import channel_actions
import channels
import faders
import mute_solo
import rcp_hid
import rcp_read
import rcp_transfer
import speech
from _desk import push_handle, service

service()

FADER = 8          # 0-based fader index: the USB 2 virtual fader (no physical fader to fight)
MUTE_CHAN = "Mic3"  # nothing is plugged into Mic 3, and the standing rule keeps Mic 1 unwritten
GAIN_CHAN = 3       # same reason: Mic 1 carries a live microphone and is never written by a test

SAID: list[tuple[float, str]] = []
_t0 = time.monotonic()
speech.say = lambda text: SAID.append((time.monotonic() - _t0, text))
# Level and gain pushes are gated on the "announce levels" setting. This process only DECODES, it
# never speaks, so forcing it on cannot make the real announcer chattier - but leaving it off would
# make the unmarked control halves silent and report a false pass.
announcer.levels_on = True


def listen(dev, st: dict, secs: float) -> list[str]:
    """Pump the decoder for `secs` and return everything it would have spoken."""
    before = len(SAID)
    end = time.monotonic() + secs
    while time.monotonic() < end:
        rep = dev.read(256, timeout_ms=100)
        if rep:
            announcer.handle_report(bytes(rep), st)
        announcer.flush_pending(st)
    return [text for _t, text in SAID[before:]]


def raw(write):
    """A raw writer as a write for `phase`: it marks nothing, and raising is its failure."""
    def run():
        write()
        return {"ok": True}
    return run


def phase(dev, st, label: str, write, expect_speech, failures: list, notes: list) -> list[str]:
    """One half of a pair: make the write, then listen.

    `write` is a zero-argument callable returning a reply: the app's action for a MARKED half, or
    `raw(...)` round the writer underneath it for the UNMARKED control.

    `expect_speech` is True (this path echoes, so silence here means the listener is dead),
    False (a marked write: speech here is the suppression failing) or None (measured to be silent
    because the desk does not echo this path - report it, do not judge it)."""
    res = write()
    if not res.get("ok"):
        failures.append(f"{label}: the write itself failed: {res.get('message')}")
    said = listen(dev, st, 4.0)
    print(f"{label:22} -> spoken: {said or 'nothing'}")
    if expect_speech is True and not said:
        failures.append(f"{label}: no echo at all, so the marked half proves nothing "
                        f"(the listener is not hearing the desk)")
    if expect_speech is False and said:
        failures.append(f"{label}: suppression failed, announcer still said {said}")
    if expect_speech is None:
        notes.append(f"{label}: {'the desk NOW echoes this path, so the '
                                 'suppression has become a real fix rather than a no-op'
                     if said else 'silent, as measured: the desk does not echo this write'}")
    return said


def main() -> int:
    levels = faders.fader_levels_live()
    original = next((f["percent"] for f in levels["faders"] if f["index"] == FADER), None)
    if original is None:
        print(f"fader index {FADER} has no level control here; nothing to test")
        return 1
    print(f"fader {FADER} starts at {original} percent (restored at the end)")

    vec = [v for v, _k in rcp_read.find_all(rcp_read.read_model_stable(fresh=True),
                                            "channelInputSource")]
    print(f"fader layout: {vec}  -> fader {FADER} carries source code {vec[FADER]}")

    if rcp_hid.find_control_interface() is None:
        print("no device")
        return 1
    chans = {c["name"]: c for c in channel_actions.action_get_channels()["channels"]}
    mute_was = bool(chans[MUTE_CHAN]["muted"])
    strip = channels.channel_strip_index(MUTE_CHAN)
    gain_was = channels.mic_gain_and_processing(GAIN_CHAN)[0]
    print(f"{MUTE_CHAN} starts {'muted' if mute_was else 'unmuted'} on strip {strip}; "
          f"Mic {GAIN_CHAN} gain starts at {gain_was} dB (both restored at the end)")

    st = {"pending": None, "at": 0.0}
    announcer.live_state = st
    failures, notes = [], []
    try:
        with push_handle("announcer echo check") as dev:
            listen(dev, st, 2.0)          # let the stream settle, and swallow any startup chatter

            # ---- LEVEL: the one path the desk really does echo. This is the regression. ----
            target_a = 60 if original != 60 else 55
            phase(dev, st, "LEVEL   MARKED",
                  lambda: faders.action_set_fader_level(FADER, target_a), False, failures, notes)
            phase(dev, st, "LEVEL   UNMARKED",
                  raw(lambda: rcp_transfer.set_fader_level(
                      FADER, round(original * rcp_transfer.FADER_LEVEL_MAX / 100))),
                  True, failures, notes)

            # ---- MUTE: keyed by STRIP index, so no layout translation on either side. ----
            # Flip AWAY from where it started for the marked half and back for the unmarked one,
            # which also means the control half doubles as the restore. Both halves are expected
            # SILENT: see the module docstring - the desk answers a software mute with mixMute
            # matrix records only.
            phase(dev, st, "MUTE    MARKED",
                  lambda: mute_solo.action_set_channel_mute(MUTE_CHAN, not mute_was), False,
                  failures, notes)
            phase(dev, st, "MUTE    UNMARKED",
                  raw(lambda: channels.set_channel_mute(MUTE_CHAN, mute_was)), None,
                  failures, notes)

            # ---- GAIN: the two sides name a gain differently, which is the point of the mark. ----
            # Two DIFFERENT values, because the push side dedupes on (who, value): repeating a value
            # would be swallowed by that dedupe and the silence would prove nothing.
            base = gain_was if isinstance(gain_was, int) else 0
            g_a = base + 6 if base <= 30 else base - 6
            g_b = base + 12 if base <= 24 else base - 12
            phase(dev, st, "GAIN    MARKED",
                  lambda: channel_actions.action_set_gain(GAIN_CHAN, g_a), False, failures, notes)
            phase(dev, st, "GAIN    UNMARKED",
                  raw(lambda: channels.set_mic_gain(GAIN_CHAN, g_b)), None, failures, notes)
    finally:
        faders.action_set_fader_level(FADER, original)
        mute_solo.action_set_channel_mute(MUTE_CHAN, mute_was)
        if isinstance(gain_was, int):
            channel_actions.action_set_gain(GAIN_CHAN, gain_was)
        back = faders.fader_levels_live()
        now = next((f["percent"] for f in back["faders"] if f["index"] == FADER), None)
        chans2 = {c["name"]: c for c in channel_actions.action_get_channels()["channels"]}
        mute_now = bool(chans2[MUTE_CHAN]["muted"])
        gain_now = channels.mic_gain_and_processing(GAIN_CHAN)[0]
        for what, before, after in (("fader level", original, now),
                                    (f"{MUTE_CHAN} mute", mute_was, mute_now),
                                    (f"Mic {GAIN_CHAN} gain", gain_was, gain_now)):
            ok = before == after
            print(f"restored: {what} {before} -> {after} "
                  f"({'ok' if ok else 'MISMATCH, check the desk'})")
            if not ok:
                failures.append(f"restore: {what} came back as {after}, not {before}")

    if notes:
        print("\nMEASURED: \n  " + "\n  ".join(notes))
    print("\nRESULT: " + ("PASS - our own writes are silent, and the one path that really echoes "
                          "(level) still does"
                          if not failures else "FAIL\n  " + "\n  ".join(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
