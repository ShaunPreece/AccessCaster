"""Offline test of the LATCHED PAD detection: does the app notice a pad left switched on?

WHY THIS EXISTS, and it is not a hypothetical. On the new "Restore the factory voice
effects" left the LAST of the six pads ENGAGED, because it called the low-level create whose default
is active=True. Shaun came back to a robot voice on his mic.

The bug is fixed at source (restore_one now passes active=False). This file guards the SECOND
failure, which is the one that let it reach him: **the after-check said everything was fine.** It
compared the grid - name, type, colour, presence - and a latched pad changes none of those. The
thing that changes is `padActive`, and nothing anywhere looked at it.

So what is pinned here is the DETECTION, not the create. It cannot be exercised on the real desk
without deliberately putting an effect on a live mic, which is the thing we are trying never to do
again, so it is driven from synthetic dumps instead.

Run: python DevTools/test_latched_pads.py
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import rcp_read
import server
import soundpad

FAILED = 0


def check(label, got, want):
    global FAILED
    if got == want:
        print(f"  ok   {label}: {got!r}")
    else:
        FAILED += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def with_pads(positions, active_flags):
    """Run the detectors against a synthetic pad table.

    `positions` is [(position, padIdx, padType, name)] as pad_positions returns it, and
    `active_flags` is the padActive vector, ONE ENTRY PER PAD, aligned with `positions`.

    NO LEADING NON-PAD ENTRY: `rcp_read.find_all` matches whole names, so nothing has to be
    skipped. A reader that searches the dump for the name as raw text matches `padActive` inside
    `padActiveEdit` and puts a junk value at the front, and a fixture that hardcodes that junk pins
    the PARSER BUG rather than the desk. The alignment is worth pinning either way, because getting
    it wrong by one blames the wrong pad, which is worse than not noticing at all."""
    real_pos, real_find = soundpad.pad_positions, rcp_read.find_all
    soundpad.pad_positions = lambda blob=None: positions
    rcp_read.find_all = lambda blob, name: (
        [(v, "bool") for v in active_flags] if name == "padActive" else real_find(blob, name))
    try:
        return soundpad.latched_pads(b""), server._pads_engaged(b"")
    finally:
        soundpad.pad_positions, rcp_read.find_all = real_pos, real_find


PADS = [(8, 0, 1, "Music_Bed"), (18, 8, 2, "Voice Disguise"), (23, 14, 2, "Small Robot")]

# The same table with the desk's OWN bank-8 pads on the end, which is what a real dump looks like.
# padType 6, padIdx 56-63: Input 1-4, Scene A/B, Auto, Cut.
PADS_WITH_SYSTEM = PADS + [(24, 56, 6, "Input 1"), (25, 58, 6, "Scene A")]


def main() -> int:
    print("THE QUIET DESK")
    latched, engaged = with_pads(PADS, [False, False, False])
    check("nothing engaged is reported as nothing", latched, [])
    check("and the overview stays silent about it", engaged, [])

    print("\nTHE FAULT THAT ACTUALLY HAPPENED: the LAST pad of a run left switched on")
    latched, engaged = with_pads(PADS, [False, False, True])
    check("the latched pad is found", latched, [(23, 14, "Small Robot")])
    check("and it is named on the overview by GRID position, not by padIdx",
          engaged, [{"grid": 15, "name": "Small Robot", "kind": "voice effect"}])

    print("\nTHE OFF-BY-ONE THIS DETECTION LIVES OR DIES BY")
    # One flag per pad, in the same order. An off-by-one here would blame Voice Disguise for Small
    # Robot being on - a confident, wrong answer, about the fault a blind operator cannot see.
    latched, _ = with_pads(PADS, [False, True, False])
    check("the MIDDLE pad being on blames the middle pad", latched, [(18, 8, "Voice Disguise")])
    latched, _ = with_pads(PADS, [True, False, False])
    check("the FIRST pad being on blames the first pad", latched, [(8, 0, "Music_Bed")])

    print("\nMORE THAN ONE AT ONCE")
    latched, engaged = with_pads(PADS, [False, True, True])
    check("both are reported", [n for _p, _i, n in latched], ["Voice Disguise", "Small Robot"])
    check("and both reach the overview", len(engaged), 2)

    print("\nTHE DESK'S OWN BANK-8 PADS ARE NOT 'LEFT SWITCHED ON'")
    # Their padActive is the desk showing which input and scene it has selected, so treating it as a
    # latched pad made unlatch_all switch off the desk's own state to "make it safe". That happened
    # The case it guards: a factory-FX rebuild finishing by announcing it had switched off Input 1
    # and Scene A. The detection is shared, so this also stopped _hk_pad_verify unlatching Input 1
    # whenever a shortcut press did not take.
    latched, engaged = with_pads(PADS_WITH_SYSTEM, [False, False, False, True, True])
    check("Input 1 and Scene A being on is not a latched pad", latched, [])
    check("and the overview does not report them either", engaged, [])

    print("\nAND SKIPPING THEM MUST NOT SHIFT THE ALIGNMENT")
    # The flag vector still carries one entry per pad INCLUDING the system ones, so the skip has to
    # happen without disturbing the index. Getting this wrong would blame the wrong pad, which the
    # off-by-one section above exists to prevent.
    latched, _ = with_pads(PADS_WITH_SYSTEM, [False, False, True, True, True])
    check("a real latched pad is still found past the system pads",
          latched, [(23, 14, "Small Robot")])
    latched, _ = with_pads(PADS_WITH_SYSTEM, [True, False, False, True, True])
    check("and so is the first one", latched, [(8, 0, "Music_Bed")])

    print("\nIT MUST NEVER TAKE THE PAGE DOWN")
    # A short or malformed padActive vector is a read problem, not a reason to fail the whole
    # Device page: the overview is the page you go to WHEN something is wrong.
    latched, engaged = with_pads(PADS, [])
    check("a truncated vector reports nothing rather than raising", latched, [])
    check("and the overview survives it", engaged, [])

    print("\nA VOICE-FX PAD WITH NO EFFECT BEHIND IT IS REFUSED, NOT SWITCHED ON")
    # The mirror image of a latched pad, and just as silent: the pad carries the right name, type
    # and colour with no region-B object bound to it, so `padActive` latches and processes nothing.
    # A shortcut that wrote the flag and said "on" would be telling the operator their voice is
    # disguised when the mic is untouched - see soundpad.heal_fx_pool for how a desk gets there.
    saved_missing, saved_on = server._hk_fx_missing, set(server._hk_pads_on)
    try:
        server._hk_fx_missing = frozenset({14})
        server._hk_pads_on.clear()
        said = server._hk_pad_toggle(14, 23, "Small Robot")
        check("it says the pad has nothing behind it",
              "no effect behind it" in said, True)
        check("it names the way out", "Restore the factory voice effects" in said, True)
        check("and it does NOT record the pad as engaged", 14 in server._hk_pads_on, False)

        # Turning OFF is never blocked: a pad that is somehow already latched must always be
        # releasable, which is the whole point of the read-out that finds it.
        #
        # THE RELEASE IS STUBBED, and it has to be: this suite runs with a desk attached as often as
        # not, and letting the real `unlatch_pad` through would open a HID handle from an OFFLINE
        # test - which segfaults the interpreter on exit (closing a handle while a read is in flight
        # on another thread). Stubbing keeps the assertion about the GUARD, which is what is on
        # trial here.
        # `_hk_pad_verify` goes too, and for a sharper reason than tidiness: it runs on a BACKGROUND
        # THREAD that reads the desk, so the interpreter exits with a HID read in flight and
        # segfaults (exit 139) after every check has already printed "ok". A suite that passes and
        # then crashes is worse than one that fails.
        released = []
        real_unlatch, real_verify = soundpad.unlatch_pad, server._hk_pad_verify
        soundpad.unlatch_pad = lambda pos: released.append(pos)
        server._hk_pad_verify = lambda *a, **k: None
        try:
            server._hk_pads_on.add(14)
            off = server._hk_pad_toggle(14, 23, "Small Robot")
        finally:
            soundpad.unlatch_pad, server._hk_pad_verify = real_unlatch, real_verify
        check("releasing one is not blocked by the same guard",
              "no effect behind it" in off, False)
        check("and the release really was sent", released, [23])
    finally:
        server._hk_fx_missing = saved_missing
        server._hk_pads_on.clear()
        server._hk_pads_on.update(saved_on)

    print("\n" + ("ALL CHECKS PASSED" if not FAILED else f"{FAILED} FAILED"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
