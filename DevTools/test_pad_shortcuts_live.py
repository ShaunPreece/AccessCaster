"""Play pads from the shortcut layer, on the attached desk, and check what it says back.

WHAT THIS IS FOR. The pad shortcut has two transports and picks between them per pad, so the thing
that can go wrong is not "does MIDI work" but "did the right branch run and did it say the right
words". Every check here is on the SPOKEN RESULT, because that sentence is the entire feedback a
shortcut gives: the window is not in front of you when you press one.

IT MAKES A NOISE, on purpose. A sound pad that returns "Airhorn playing" without playing is the
failure this guards against (a `padActive` write over HID gives exactly that), so the tracked
progress state is checked as well as the sentence - the desk pushing progress twenty times a second
is the closest thing to an ear this can have.

Starts the shortcuts' own follower of the desk (`pad_shortcuts.follow_desk`), where the tracking
under test lives. Restores the bank and leaves nothing engaged or held.

    python DevTools/test_pad_shortcuts_live.py
"""
from __future__ import annotations

import time

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
import devices
import pad_shortcuts
import soundpad
from _testkit import check, check_in


def check_said(what: str, got, want=None, contains=None) -> bool:
    """A check on a shortcut's result, which is judged by its sentence."""
    if isinstance(got, dict):
        got = got.get("message")
    if contains is not None:
        return check_in(what, contains, str(got))
    return check(what, got, want)


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
        playing = pad_shortcuts.play_snapshot()
        if not playing:
            return
        for position in playing:
            pad_idx = by_position.get(position)
            if pad_idx is not None:
                print(f"  ({why}: stopping {pads[pad_idx][1]})")
                pad_shortcuts.fire_pad(pad_idx)
        time.sleep(1.2)
    print(f"  WARNING ({why}): something is still playing and will not stop")


def main() -> int:
    dev = devices.get_active_device()
    per_bank = dev.pad.pads_per_bank
    pads = pad_shortcuts.refresh_pads()
    was_bank = pad_shortcuts.current_bank()
    print(f"{dev.name}: {len(pads)} pads, {per_bank} per bank, on bank {was_bank + 1}")

    pad_shortcuts.follow_desk()
    time.sleep(1.0)                       # the listener opening, so the first press is not racing it

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
            time.sleep(0.4)

            check_said("pad.fire on a sound pad says playing",
                       pad_shortcuts.fire(str(sound)), contains="playing")
            check_said("it selected the pad's own bank", pad_shortcuts.current_bank(), want=bank)
            time.sleep(0.6)
            playing = pad_shortcuts.play_snapshot()
            check_said("the desk is pushing progress for it", pos in playing, want=True)
            check_said("what is playing names it", pad_shortcuts.playing_now(), contains=name)

            # pad.slot fires by PLACE, so the same pad answers to its slot number now that the desk
            # is on its bank. A One Shot restarts, a Toggle stops: both are "the pad's own setting".
            said = pad_shortcuts.slot(str(slot))
            check_said("pad.slot on the same pad", said,
                       contains="stopped" if mode == soundpad.PLAY_MODE_TOGGLE else "playing")
            # AND THE DESK HAS TO AGREE. Asserting our own sentence proves only that the branch
            # ran: the word "stopped" comes from the pad's stored play mode, which is a read-back.
            # The engine is the judge - a stopped pad stops pushing progress, so it falls out of
            # the tracked state. (A second press on a One Shot pad restarts it, so this check only
            # applies to Toggle.) If this fails while somebody is at the desk, ask whether they
            # pressed the pad: a physical press pushes exactly like a MIDI one and restarts it.
            if mode == soundpad.PLAY_MODE_TOGGLE:
                time.sleep(1.5)
                check_said("and the desk really did stop it",
                           pad_shortcuts.playing_now(), want="Nothing is playing.")
            else:
                time.sleep(1.0)

        if fx is None:
            print("\nno voice-FX pad on this desk; skipping the HID half")
        else:
            pos, name, _pt, _mode = pads[fx]
            print(f"\nVOICE-FX pad {name!r}: padIdx {fx}, position {pos}")
            check_said("pad.fire on a voice-FX pad switches it on",
                       pad_shortcuts.fire(str(fx)), want=f"{name} on.")
            check_said("what is playing says it is switched on",
                       pad_shortcuts.playing_now(), contains="switched on")
            check_said("a second press switches it off",
                       pad_shortcuts.fire(str(fx)), want=f"{name} off.")

        print("\nBANK STEPPING")
        filled = pad_shortcuts._filled_banks()
        soundpad.select_bank(filled[0])
        time.sleep(0.4)
        check_said("bank down at the first bank with pads refuses",
                   pad_shortcuts.bank_step("down"), contains="first bank with pads")
        if len(filled) > 1:
            check_said("bank up moves to the next bank with pads",
                       pad_shortcuts.bank_step("up"), want=f"Bank {filled[1] + 1}.")
            check_said("and the desk model followed it", pad_shortcuts.current_bank(),
                       want=filled[1])
        else:
            print("only one bank has pads on it; skipping the step up")

        print("\nREFUSALS")
        check_said("a slot the desk does not have",
                   pad_shortcuts.slot(str(per_bank + 1)), contains=f"{per_bank} pads in a bank")
        check_said("a pad that is not a number", pad_shortcuts.fire("banana"), contains="not a pad")
    finally:
        # Silence while the follower still runs: it is what says what is still playing.
        silence(pads, "left playing by this run")
        pad_shortcuts.release_held()
        for pos, _pi, _nm in soundpad.latched_pads():
            soundpad.unlatch_pad(pos)
        if was_bank is not None:
            soundpad.select_bank(was_bank)
        print(f"\nrestored: bank {(was_bank or 0) + 1}, nothing engaged or held")

    return _testkit.finish()


if __name__ == "__main__":
    raise SystemExit(main())
