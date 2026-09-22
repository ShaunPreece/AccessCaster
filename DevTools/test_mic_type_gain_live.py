"""Does setting a mic's TYPE also move its GAIN? (the clean-restore defect)

WHY. A CLEAN profile restore left Mic 1's gain at 55 when the profile said 47, while the ordinary
MERGE restore of the same profile was perfect. The dumps say why that is not a contradiction: on the
merge the desk was ALREADY on the saved mic type, so the type write was a no-op; on the clean path
the reset had put the type back to PodMic (4) and the restore wrote SM7B (10) - and `profiles._apply_v2`
writes the GAIN FIRST and the TYPE FOURTH. If choosing a voicing carries its own default gain, the
type write lands on top of the gain we just restored, every time.

This measures it directly, and while it is at it records what gain each voicing sets, which is the
table nothing in this project has ever had.

REVERSIBLE. Runs on Mic 3 (nothing plugged into it, and the standing rule keeps Shaun's Mic 1 out of
tests), records the channel's gain and type first, and puts both back at the end, read-back verified.

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
import rcp_read
from model_tree import parse, walk

SETTLE = 0.9        # the desk needs a moment before a fresh dump shows the write
PROBE_GAIN = 47     # a value that is nobody's default, so a default overwriting it is obvious


def read_input(slot: int) -> dict:
    """Every property of one INPUTSOURCE node, by input slot (Mic 1 = 0)."""
    root, _, _ = parse(rcp_read.read_model_stable(fresh=True))
    nodes = [n for n in walk(root) if n.name == "INPUTSOURCE"]
    return {p: v for p, v, _k in nodes[slot].props}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", type=int, default=3, help="1-based mic channel (default 3)")
    args = ap.parse_args()
    ch = args.channel
    slot = ch - 1

    before = read_input(slot)
    base_gain, base_type = before["inputMicrophoneGain"], before["inputMicrophoneType"]
    print(f"Mic {ch} as found: gain {base_gain}, type {base_type} "
          f"({channels.MIC_TYPE.get(base_type, '?')})")

    fail = 0
    try:
        # THE ONE QUESTION: set a known gain, then change the voicing, and see if the gain survives.
        print(f"\n-- does a type change move the gain? --")
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
            print("The gain survived every voicing change, so the clean-restore defect is "
                  "something else.")

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
        # (that is exactly why profiles restore it), so a restore that only put the type and gain
        # back left the channel changed. Found by this file's own read-back, first run.
        channels.set_mic_type(ch, base_type)
        time.sleep(SETTLE)
        channels.set_mic_gain(ch, base_gain)
        time.sleep(SETTLE)
        channels.set_instrument_gain(ch, int(before["inputInstrumentGain"]))
        time.sleep(SETTLE)
        after = read_input(slot)
        ok = (after["inputMicrophoneGain"] == base_gain
              and after["inputMicrophoneType"] == base_type
              and after["inputInstrumentGain"] == before["inputInstrumentGain"]
              and after["inputType"] == before["inputType"])
        print(f"  gain {after['inputMicrophoneGain']} (was {base_gain}), "
              f"type {after['inputMicrophoneType']} (was {base_type}), "
              f"instrument gain {after['inputInstrumentGain']} "
              f"(was {before['inputInstrumentGain']}), "
              f"inputType {after['inputType']} (was {before['inputType']})")
        print("  RESTORED CLEAN" if ok else "  *** NOT FULLY RESTORED - check Mic %d ***" % ch)
        if not ok:
            fail += 1
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
