"""Offline test of the LATCHED PAD detection: does the app notice a pad left switched on?

WHY THIS EXISTS. The low-level voice-effect create defaults to active=True, so a rebuild such as
"Restore the factory voice effects" that does not pass active=False (soundpad.restore_one does)
leaves the LAST pad it makes ENGAGED, and a robot voice on a live mic.

A check that compares the grid - name, type, colour, presence - cannot see that: a latched pad
changes none of those. The thing that changes is `padActive`, and this file pins the detection
that reads it.

So what is pinned here is the DETECTION, not the create. It cannot be exercised on the real desk
without deliberately putting an effect on a live mic, which is what the detection exists to
prevent, so it is driven from synthetic dumps instead.

Run: python DevTools/test_latched_pads.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401
import _testkit
import device_page
import pad_shortcuts
import soundpad
from _testkit import check, patched


def with_pads(positions, active_flags):
    """Run the detectors against a synthetic pad table.

    `positions` is [(position, padIdx, padType, name)] as pad_positions returns it, and
    `active_flags` is each pad's padActive, aligned with `positions`. A pad past the end of
    `active_flags` carries no padActive at all."""
    pads = [soundpad.Pad(pos, idx, typ, name,
                         {"padActive": active_flags[i]} if i < len(active_flags) else {})
            for i, (pos, idx, typ, name) in enumerate(positions)]
    with patched((soundpad, {"pad_nodes": lambda blob=None: pads})):
        return soundpad.latched_pads(b""), device_page._pads_engaged(b"")


PADS = [(8, 0, 1, "Music_Bed"), (18, 8, 2, "Voice Disguise"), (23, 14, 2, "Small Robot")]

# The same table with the desk's OWN bank-8 pads on the end, which is what a real dump looks like.
# padType 6, padIdx 56-63: Input 1-4, Scene A/B, Auto, Cut.
PADS_WITH_SYSTEM = PADS + [(24, 56, 6, "Input 1"), (25, 58, 6, "Scene A")]


def main() -> int:
    print("THE QUIET DESK")
    latched, engaged = with_pads(PADS, [False, False, False])
    check("nothing engaged is reported as nothing", latched, [])
    check("and the overview stays silent about it", engaged, [])

    print("\nTHE LAST pad of a run left switched on")
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
    # Their padActive is the desk showing which input and scene it has selected. Treated as latched
    # pads, unlatch_all switches off the desk's own state to "make it safe", and a factory-FX rebuild
    # ends by announcing it switched off Input 1 and Scene A. The detection is shared, so the same
    # mistake would make pad_shortcuts._pad_verify unlatch Input 1 whenever a shortcut press did not take.
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
    with patched((pad_shortcuts, {"_fx_missing": frozenset({14}), "_pads_on": set()})):
        said = pad_shortcuts._pad_toggle(14, 23, "Small Robot")
        check("it says the pad has nothing behind it",
              "no effect behind it" in said["message"], True)
        check("it names the way out", "Restore the factory voice effects" in said["message"], True)
        check("and reports it as a failure, so a silent shortcut still speaks it", said["ok"], False)
        check("and it does NOT record the pad as engaged", 14 in pad_shortcuts._pads_on, False)

        # Turning OFF is never blocked: a pad that is somehow already latched must always be
        # releasable, which is the whole point of the read-out that finds it.
        #
        # THE RELEASE IS STUBBED, and it has to be: this suite runs with a desk attached as often as
        # not, and letting the real `unlatch_pad` through would open a HID handle from an OFFLINE
        # test - which segfaults the interpreter on exit (closing a handle while a read is in flight
        # on another thread). Stubbing keeps the assertion about the GUARD, which is what is on
        # trial here.
        # `_pad_verify` goes too, and for a sharper reason than tidiness: it runs on a BACKGROUND
        # THREAD that reads the desk, so the interpreter exits with a HID read in flight and
        # segfaults (exit 139) after every check has already printed "ok". A suite that passes and
        # then crashes is worse than one that fails.
        released = []
        with patched((soundpad, {"unlatch_pad": lambda pos: released.append(pos)}),
                     (pad_shortcuts, {"_pad_verify": lambda *a, **k: None})):
            pad_shortcuts._pads_on.add(14)
            off = pad_shortcuts._pad_toggle(14, 23, "Small Robot")
        check("releasing one is not blocked by the same guard",
              "no effect behind it" in off["message"], False)
        check("and the release really was sent", released, [23])

    print("\nA PRESS THAT FIRED THE WRONG PAD SWITCHES OFF THE ONE IT FIRED")
    # A stale map sent Robot's press to stream position 23, where pad 20 now sits. Pad 1, a music
    # bed, is also playing; it has the lower number and must be left alone.
    fresh = {1: (9, "Music bed", 1, None), 14: (30, "Robot", 2, None),
             20: (23, "Megaphone", 2, None)}
    released, said = [], []
    with patched((pad_shortcuts, {"refresh_pads": lambda: fresh, "_pads_on": {14}}),
                 (soundpad, {"latched_pads": lambda: [(9, 1, "Music bed"), (23, 20, "Megaphone")],
                             "unlatch_pad": released.append}),
                 (pad_shortcuts.speech, {"say": said.append})):
        pad_shortcuts._pad_verify(14, 23, True)
        check("the pad at the position written is released", released, [23])
        check("named from the fresh map", "Megaphone" in (said[-1] if said else ""), True)
        check("and the pad asked for is not left recorded as on", pad_shortcuts._pads_on, set())

    return _testkit.finish()


if __name__ == "__main__":
    raise SystemExit(main())
