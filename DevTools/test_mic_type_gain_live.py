"""Does setting a mic's TYPE also move its GAIN?

WHY. A voicing carries its own default gain, so a restore that writes the GAIN before the TYPE
loses the gain whenever the type changes: after a factory reset puts the type back to PodMic (4),
restoring SM7B (10) leaves the gain at SM7B's 55, not the profile's 47. A merge restore onto a desk
already on the saved type writes the type as a no-op, which is why only the clean path shows it.

This measures it directly, and records what gain each voicing sets.

REVERSIBLE. Runs on Mic 3 (nothing plugged into it, and the standing rule keeps Mic 1 out of
tests), records the whole input node and the processing chain first, and puts them back at the
end: a voicing sets the gain, the instrument gain, phantom power and the whole chain, and writing
the old type back undoes none of them. The read-back compares every one.

Usage: python DevTools/test_mic_type_gain_live.py [--channel 3]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401
import channels
from _desk import advanced_processing, put_chain_back, read_input, settled_chain

SETTLE = 0.9        # the desk needs a moment before a fresh dump shows the write
PROBE_GAIN = 47     # a value that is nobody's default, so a default overwriting it is obvious


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", type=int, default=3, help="1-based mic channel (default 3)")
    args = ap.parse_args()
    ch = args.channel
    slot = ch - 1

    before = read_input(slot)
    chain_before = settled_chain(ch)
    advanced_before = advanced_processing(ch)
    base_gain, base_type = before["inputMicrophoneGain"], before["inputMicrophoneType"]
    print(f"Mic {ch} as found: gain {base_gain}, type {base_type} "
          f"({channels.MIC_TYPE.get(base_type, '?')})")

    fail = 0
    try:
        # THE ONE QUESTION: set a known gain, then change the voicing, and see if the gain survives.
        print("\n-- does a type change move the gain? --")
        table = {}
        for tv in sorted(channels.MIC_TYPE):
            channels.set_mic_gain(ch, PROBE_GAIN)
            time.sleep(SETTLE)
            got = read_input(slot)["inputMicrophoneGain"]
            if got != PROBE_GAIN:
                print(f"  ! gain write did not land ({got}), skipping type {tv}")
                fail += 1
                continue
            channels.set_mic_type(ch, tv)
            time.sleep(SETTLE)
            after = read_input(slot)
            gain = after["inputMicrophoneGain"]
            table[tv] = gain
            verdict = "kept" if gain == PROBE_GAIN else f"OVERWRITTEN -> {gain}"
            print(f"  {tv:2d} {channels.MIC_TYPE[tv]:<12} gain {verdict}"
                  f"   (instrument gain {after['inputInstrumentGain']}, inputType {after['inputType']})")

        moved = {t: g for t, g in table.items() if g != PROBE_GAIN}
        print(f"\nVERDICT: {len(moved)} of {len(table)} voicings moved the gain.")
        if moved:
            print("PER-VOICING DEFAULT GAIN: " +
                  ", ".join(f"{channels.MIC_TYPE[t]}={g}" for t, g in sorted(moved.items())))
            print("So a restore MUST write the mic type BEFORE the gain.")
        else:
            print("The gain survived every voicing change, so a restore's write order does not "
                  "matter for the gain.")

        # And the other way round, because the fix depends on it: with the type already set, does
        # writing the gain stick?
        print("\n-- and with the type set first, does the gain stick? --")
        channels.set_mic_type(ch, base_type)
        time.sleep(SETTLE)
        channels.set_mic_gain(ch, PROBE_GAIN)
        time.sleep(SETTLE)
        got = read_input(slot)["inputMicrophoneGain"]
        print(f"  type {base_type} then gain {PROBE_GAIN} -> {got} "
              f"{'OK' if got == PROBE_GAIN else 'FAILED'}")
        if got != PROBE_GAIN:
            fail += 1
    finally:
        print("\n-- restoring --")
        # THE INSTRUMENT GAIN NEEDS PUTTING BACK TOO. Stepping through the voicings passes through
        # Instrument, which sets inputInstrumentGain to 20 and leaves it there when you move on
        # (that is exactly why profiles restore it), so putting back only the type and gain leaves
        # the channel changed.
        channels.set_mic_type(ch, base_type)
        put_chain_back(ch, chain_before, advanced_before)
        channels.set_mic_gain(ch, base_gain)
        time.sleep(SETTLE)
        channels.set_instrument_gain(ch, int(before["inputInstrumentGain"]))
        time.sleep(SETTLE)
        channels.set_phantom(ch, bool(before["inputPower"]))
        time.sleep(SETTLE)
        after = read_input(slot)
        chain_after = settled_chain(ch)
        left = [f"{k} {after.get(k)!r} (was {v!r})" for k, v in before.items() if after.get(k) != v]
        left += [f"chain {k} {chain_after.get(k)!r} (was {v!r})"
                 for k, v in chain_before.items() if chain_after.get(k) != v]
        if left:
            print(f"  *** NOT FULLY RESTORED - check Mic {ch}: " + "; ".join(left))
            fail += 1
        else:
            print("  RESTORED CLEAN: the input node and the chain are as found")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
