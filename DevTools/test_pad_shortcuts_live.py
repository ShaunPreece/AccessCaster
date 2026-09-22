"""Play pads from the shortcut layer, on the attached desk, and check what it says back.

WHAT THIS IS FOR. The pad shortcut has two transports and picks between them per pad, so the thing
that can go wrong is not "does MIDI work" but "did the right branch run and did it say the right
words". Every check here is on the SPOKEN RESULT, because that sentence is the entire feedback a
shortcut gives: the window is not in front of you when you press one.

IT MAKES A NOISE, on purpose. A sound pad that returns "Airhorn playing" without playing is exactly
the bug this replaces (`padActive` over HID did that for two months), so the tracked progress state
is checked as well as the sentence - the desk pushing progress twenty times a second is the closest
thing to an ear this can have.

Runs its own announcer, because the pushed stream is the announcer's read handle and the tracking
under test lives there. Restores the bank and leaves nothing engaged or held.

    python DevTools/test_pad_shortcuts_live.py
"""
from __future__ import annotations
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401  (puts Project/ on sys.path)

import devices
import server
import soundpad

FAILURES = []


def check(what: str, got, want=None, contains=None) -> None:
    ok = (want is not None and got == want) or (contains is not None and contains in str(got))
    print(f"  {'PASS' if ok else 'FAIL'}  {what}: {got!r}")
    if not ok:
        FAILURES.append(f"{what}: got {got!r}, wanted {want if want is not None else contains!r}")


def find_pad(pads, pad_type, play_mode=None):
    """The first pad of a type (and optionally a play mode), or None."""
    for pad_idx in sorted(pads):
        _pos, name, pt, mode = pads[pad_idx]
        if pt == pad_type and name and (play_mode is None or mode == play_mode):
            return pad_idx
    return None


def silence(pads, why: str) -> None:
    """Press anything still playing until it stops, and say what had to be stopped.

    RUN AT BOTH ENDS, and the start one is not belt and braces. A playing SOUND pad survives an
    ordinary teardown - `padActive` OFF does not stop one, which is a settled negative - so the only
    stop is another press of the same pad. A 44-second music bed left over from the last run makes
    the next run's FIRST press a STOP rather than a start, and every check after it then measures
    the opposite of what it says it does. That is how this test comes to disagree with a standalone
    probe, and it looks nothing like its cause: the MIDI is perfectly correct throughout."""
    by_position = {entry[0]: pad_idx for pad_idx, entry in pads.items()}
    for _ in range(4):
        playing = server._hk_play_snapshot()
        if not playing:
            return
        for position in playing:
            pad_idx = by_position.get(position)
            if pad_idx is not None:
                print(f"  ({why}: stopping {pads[pad_idx][1]})")
                server._hk_fire_pad(pad_idx)
        time.sleep(1.2)
    print(f"  WARNING ({why}): something is still playing and will not stop")


def main() -> int:
    dev = devices.get_active_device()
    per_bank = dev.pad.pads_per_bank
    pads = server._hk_refresh_pads()
    was_bank = server._hk_current_bank()
    print(f"{dev.name}: {len(pads)} pads, {per_bank} per bank, on bank {was_bank + 1}")

    # The announcer owns the pushed stream, and the play tracking under test lives in its handler.
    server._ann_quiet_start.set()
    server._announce_on.set()
    threading.Thread(target=server._announce_loop, daemon=True).start()
    time.sleep(2.5)                       # the seed read, so the first press is not racing it

    silence(pads, "left over from a previous run")
    sound = find_pad(pads, soundpad.PAD_TYPE_SOUND)
    fx = find_pad(pads, soundpad.PAD_TYPE_VOICE_FX)
    try:
        if sound is None:
            print("no sound pad on this desk; skipping the MIDI half")
        else:
            pos, name, _pt, mode = pads[sound]
            bank, slot = sound // per_bank, sound % per_bank + 1
            print(f"\nSOUND pad {name!r}: padIdx {sound}, bank {bank + 1} slot {slot}, "
                  f"position {pos}, mode {soundpad.PLAY_MODE_LABEL.get(mode)}")

            # Move OFF the pad's bank first, so the fire has to select it. That is the step a
            # read-back can never check and the one a wrong bank silently breaks.
            other = 1 if bank == 0 else 0
            soundpad.select_bank(other)
            server._hk_note_bank(other)
            time.sleep(0.4)

            check("pad.fire on a sound pad says playing",
                  server._hk_pad(str(sound)), contains="playing")
            check("it selected the pad's own bank", server._hk_current_bank(), want=bank)
            time.sleep(0.6)
            playing = server._hk_play_snapshot()
            check("the desk is pushing progress for it", pos in playing, want=True)
            check("what is playing names it", server._hk_playing_now(), contains=name)

            # pad.slot fires by PLACE, so the same pad answers to its slot number now that the desk
            # is on its bank. A One Shot restarts, a Toggle stops: both are "the pad's own setting".
            said = server._hk_pad_slot(str(slot))
            check("pad.slot on the same pad", said,
                  contains="stopped" if mode == soundpad.PLAY_MODE_TOGGLE else "playing")
            # AND THE DESK HAS TO AGREE. Asserting our own sentence proves only that the branch
            # ran: the word "stopped" comes from the pad's stored play mode, which is a read-back.
            # The engine is the judge - a stopped pad stops pushing progress, so it falls out of
            # the tracked state. (A second press on a One Shot pad restarts it, so this check only
            # applies to Toggle.) If this fails while somebody is at the desk, ask whether they
            # pressed the pad: a physical press pushes exactly like a MIDI one and restarts it.
            if mode == soundpad.PLAY_MODE_TOGGLE:
                time.sleep(1.5)
                check("and the desk really did stop it",
                      server._hk_playing_now(), want="Nothing is playing.")
            else:
                time.sleep(1.0)

        if fx is None:
            print("\nno voice-FX pad on this desk; skipping the HID half")
        else:
            pos, name, _pt, _mode = pads[fx]
            print(f"\nVOICE-FX pad {name!r}: padIdx {fx}, position {pos}")
            check("pad.fire on a voice-FX pad switches it on",
                  server._hk_pad(str(fx)), want=f"{name} on.")
            check("what is playing says it is switched on",
                  server._hk_playing_now(), contains="switched on")
            check("a second press switches it off",
                  server._hk_pad(str(fx)), want=f"{name} off.")

        print("\nBANK STEPPING")
        server._hk_note_bank(0)
        soundpad.select_bank(0)
        time.sleep(0.4)
        check("bank down at the first bank refuses",
              server._hk_bank_step("down"), contains="first bank")
        check("bank up moves", server._hk_bank_step("up"), want="Bank 2.")
        check("and the cache followed it", server._hk_current_bank(), want=1)

        print("\nREFUSALS")
        check("a slot the desk does not have",
              server._hk_pad_slot(str(per_bank + 1)), contains=f"{per_bank} pads in a bank")
        check("a pad that is not a number", server._hk_pad("banana"), contains="not a pad")
    finally:
        # Silence BEFORE the announcer goes: the tracking that says what is still playing is its
        # read loop, and without it this can only guess.
        silence(pads, "left playing by this run")
        server._announce_on.clear()
        server._hk_release_held()
        for pos, _pi, _nm in soundpad.latched_pads():
            soundpad.unlatch_pad(pos)
        if was_bank is not None:
            soundpad.select_bank(was_bank)
        print(f"\nrestored: bank {(was_bank or 0) + 1}, nothing engaged or held")

    print("\n" + ("ALL PASSED" if not FAILURES else f"{len(FAILURES)} FAILED"))
    for f in FAILURES:
        print(f"  - {f}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
