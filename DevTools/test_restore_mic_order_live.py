"""END-TO-END: does a real profile restore now keep the mic gain when it changes the voicing?

This drives `profiles._apply_v2` itself - the actual restore code, not a stand-in - with a
one-channel profile, from the state a factory reset leaves behind. It reproduces the clean-restore
defect on ONE channel instead of the whole desk, so it costs a few seconds rather than a reset.

THE FAILURE IT CATCHES: with the gain written first, the SM7B voicing follows it and the desk keeps
SM7B's own default of 55. In the right order, 47 survives.

REVERSIBLE. Runs on Mic 3 (nothing plugged into it; Mic 1 stays out of tests), records the
gain, type and instrument gain first and puts all three back, read-back verified. The profile it
applies carries NOTHING but that one channel, so no fader, level, master-output or unit-setting
write is made.

Usage: python DevTools/test_restore_mic_order_live.py [--channel 3]
"""
from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import channels
import devices
import profiles
import rcp_read
from model_tree import parse, walk

SETTLE = 0.9
FACTORY_TYPE = 4      # PodMic: what a software factory reset leaves on every mic
FACTORY_GAIN = 50
WANT_TYPE = 10        # SM7B, whose own default gain is 55
WANT_GAIN = 47        # a gain neither voicing defaults to, so only the profile can have set it
# A VOICING ALSO REPLACES THE WHOLE CHAIN, and takes a second or two doing it, so a restore that
# writes the chain straight after the type loses it. This value is neither voicing's either.
CHAIN_PROP = "eqLowGain"
WANT_CHAIN = 0.3125


def read_input(slot: int) -> dict:
    root, _, _ = parse(rcp_read.read_model_stable(fresh=True))
    return {p: v for p, v, _k in [x for x in walk(root) if x.name == "INPUTSOURCE"][slot].props}


def chain_of(ch: int) -> dict:
    rec = next((r for r in channels.read_channels() if r.get("name") == f"Mic{ch}"), None)
    return {} if rec is None else dict(rec.get("params") or {})


def settled_chain(ch: int, timeout: float = 8.0) -> dict:
    """The chain once the desk has finished applying a voicing, not after a fixed sleep."""
    end = time.time() + timeout
    prev = chain_of(ch)
    while time.time() < end:
        cur = chain_of(ch)
        if cur == prev:
            return cur
        prev = cur
    return prev


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", type=int, default=3)
    args = ap.parse_args()
    ch, slot = args.channel, args.channel - 1

    before = read_input(slot)
    entry = next(e for e in channels.active_ui_channels()
                 if e.get("kind") == "mic" and channels.mic_channel_number(e) == ch)
    advanced_before = bool(rcp_read.values(rcp_read.read_model_stable(fresh=True),
                                           channels.CHAIN_ADVANCED_PROP)[channels._strip_index(entry)])
    chain_before = settled_chain(ch)
    print(f"Mic {ch} as found: gain {before['inputMicrophoneGain']}, "
          f"type {before['inputMicrophoneType']}, instrument gain {before['inputInstrumentGain']}, "
          f"advanced {advanced_before}, {CHAIN_PROP} {chain_before.get(CHAIN_PROP)!r}")

    fail = 0
    try:
        print(f"\n-- putting Mic {ch} where a factory reset would leave it --")
        channels.set_mic_type(ch, FACTORY_TYPE)
        time.sleep(SETTLE)
        channels.set_mic_gain(ch, FACTORY_GAIN)
        time.sleep(SETTLE)
        start = read_input(slot)
        print(f"   gain {start['inputMicrophoneGain']}, type {start['inputMicrophoneType']}")

        print("-- restoring a one-channel profile through profiles._apply_v2 --")
        prof = {"version": 2, "routing": {},
                "channels": [{"name": f"Mic{ch}", "kind": "mic",
                              "level_db": WANT_GAIN, "mic_type": WANT_TYPE,
                              "phantom": bool(before["inputPower"]),
                              "polarity": bool(before["inputPhaseFlip"]),
                              "instrument_gain": int(before["inputInstrumentGain"]),
                              "advanced": advanced_before,
                              "params": {CHAIN_PROP: WANT_CHAIN}}]}
        res = profiles._apply_v2(prof, fader_count=devices.PRO2.fader.vector_length,
                                 fader_code_set=devices.PRO2.fader.code_set,
                                 mic_channels=(1, 2, 3, 4))
        print(f"   {res['applied']} writes, {res['failed']} failed {res['errs']}")
        time.sleep(SETTLE)
        after = read_input(slot)
        ok_type = after["inputMicrophoneType"] == WANT_TYPE
        ok_gain = after["inputMicrophoneGain"] == WANT_GAIN
        print(f"   type {after['inputMicrophoneType']} (wanted {WANT_TYPE}) "
              f"{'OK' if ok_type else 'FAILED'}")
        print(f"   gain {after['inputMicrophoneGain']} (wanted {WANT_GAIN}) "
              f"{'OK' if ok_gain else 'FAILED - the voicing default overwrote it'}")
        got_chain = settled_chain(ch).get(CHAIN_PROP)
        ok_chain = got_chain is not None and abs(got_chain - WANT_CHAIN) < 1e-4
        print(f"   {CHAIN_PROP} {got_chain!r} (wanted {WANT_CHAIN}) "
              f"{'OK' if ok_chain else 'FAILED - the voicing replaced the restored chain'}")
        if not (ok_type and ok_gain and ok_chain):
            fail += 1
        else:
            print("\nTHE RESTORE KEEPS THE GAIN AND THE CHAIN THROUGH A VOICING CHANGE.")
    finally:
        print("\n-- restoring --")
        channels.set_mic_type(ch, int(before["inputMicrophoneType"]))
        settled_chain(ch)          # the voicing has to finish before the chain goes back on top
        channels.apply_processing_preset(
            ch, dict(chain_before, **{channels.CHAIN_ADVANCED_PROP: advanced_before}))
        settled_chain(ch)
        channels.set_mic_gain(ch, int(before["inputMicrophoneGain"]))
        time.sleep(SETTLE)
        channels.set_instrument_gain(ch, int(before["inputInstrumentGain"]))
        time.sleep(SETTLE)
        end = read_input(slot)
        same = [k for k in before if end.get(k) != before[k]]
        chain_end = settled_chain(ch)
        chain_diff = sorted(k for k, v in chain_before.items() if chain_end.get(k) != v)
        if chain_diff:
            same = same + [f"chain:{k}" for k in chain_diff]
        print(f"   gain {end['inputMicrophoneGain']}, type {end['inputMicrophoneType']}, "
              f"instrument gain {end['inputInstrumentGain']}")
        print("   RESTORED CLEAN" if not same else f"   *** STILL DIFFERENT: {same} ***")
        if same:
            fail += 1
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
