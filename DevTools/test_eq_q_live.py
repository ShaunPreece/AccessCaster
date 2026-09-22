"""Settle - and keep settled - what the three EQ Q values actually are. Reversible, restores itself.

THE TWO WRONG ANSWERS, both of which have been held as fact here: that Q follows device-side when
the EQ parameters are written, so no writer is needed; and that Q cannot be written at all, offered
as the explanation for a 2e-8 float32 residue the RODE app leaves behind.

THE ANSWER, measured and re-measurable by running this file:
  - Q is NOT derived. Writing a band's GAIN moves no Q, and writing a band's FREQUENCY moves no Q.
  - Q IS directly writable, and reads back exactly.
The derivation theory rests on a true observation ("the app never writes Q, yet Q moved") plus a
false inference. What the app does or does not send says nothing about what the desk will accept.

WHY IT IS WORTH KEEPING. Q is in `channels.PROCESSING_PROPS` on the strength of this test rather
than of a capture - the RODE app has no Q control, so there is no capture to have. If a firmware
update ever makes Q derived after all, presets and profiles start writing a value the desk then
overwrites, and this is the file that catches it.

Runs on Mic 3 by default: nothing is plugged into it, and the standing rule keeps Mic 1 (the SM7B
in daily use) unwritten by tests.

Usage: python DevTools/test_eq_q_live.py [--channel 3]
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
import rcp_transfer as _t

Q_PROPS = ("eqLowQ", "eqMidQ", "eqHighQ")
GAIN_PROPS = ("eqLowGain", "eqMidGain", "eqHighGain")
FREQ_PROPS = ("eqLowShelf", "eqMidBell", "eqHighBell")
SETTLE = 0.9        # the desk needs a moment before a fresh dump shows the write


def read_strip(strip: int, props) -> dict:
    blob = rcp_read.read_model_stable(fresh=True)
    out = {}
    for p in props:
        vals = [v for v, _k in rcp_read.find_all(blob, p)]
        out[p] = vals[strip] if strip < len(vals) else None
    return out, blob


def counts(blob, props) -> dict:
    return {p: len(rcp_read.find_all(blob, p)) for p in props}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", type=int, default=3, help="mic channel to test on (default 3)")
    args = ap.parse_args()
    ch = args.channel
    strip = channels.channel_strip_index(ch)
    allp = Q_PROPS + GAIN_PROPS + FREQ_PROPS
    fails = []

    base, blob0 = read_strip(strip, allp)
    c0 = counts(blob0, allp)
    print(f"Mic {ch} is on strip {strip}")
    print("baseline: " + ", ".join(f"{p}={base[p]}" for p in allp))

    def flip(v):
        """A value far enough from `v` that a follow-on change could not be missed as rounding."""
        return 0.2 if (v or 0) > 0.5 else 0.8

    try:
        # --- 1. does Q follow a GAIN write? -------------------------------------------------
        for prop in GAIN_PROPS:
            channels.set_channel_processing(ch, prop, flip(base[prop]))
            time.sleep(SETTLE)
            now, _ = read_strip(strip, Q_PROPS)
            moved = {p: (base[p], now[p]) for p in Q_PROPS if now[p] != base[p]}
            print(f"after writing {prop:12} -> Q moved: {moved or 'no'}")
            if moved:
                fails.append(f"{prop} moved Q {moved}: Q may be derived after all, which would make "
                             f"it wrong to carry in presets and profiles")
            channels.set_channel_processing(ch, prop, base[prop])
            time.sleep(0.4)

        # --- 2. does Q follow a FREQUENCY write? --------------------------------------------
        for prop in FREQ_PROPS:
            channels.set_channel_processing(ch, prop, flip(base[prop]))
            time.sleep(SETTLE)
            now, _ = read_strip(strip, Q_PROPS)
            moved = {p: (base[p], now[p]) for p in Q_PROPS if now[p] != base[p]}
            print(f"after writing {prop:12} -> Q moved: {moved or 'no'}")
            if moved:
                fails.append(f"{prop} moved Q {moved}: see above")
            channels.set_channel_processing(ch, prop, base[prop])
            time.sleep(0.4)

        # --- 3. can Q be written DIRECTLY? ---------------------------------------------------
        # Through the public writer, not a hand-built record: the point is that the SHIPPED path
        # works, not merely that some record shape does.
        want = {"eqLowQ": 0.70, "eqMidQ": 0.60, "eqHighQ": 0.50}
        for p, v in want.items():
            channels.set_channel_processing(ch, p, v)
        time.sleep(SETTLE)
        got, _ = read_strip(strip, Q_PROPS)
        print("direct write: asked " + ", ".join(f"{p}={want[p]}" for p in Q_PROPS)
              + " | read " + ", ".join(f"{p}={got[p]}" for p in Q_PROPS))
        for p in Q_PROPS:
            if got[p] is None or abs(got[p] - want[p]) > 0.01:
                fails.append(f"{p} did not take the write: asked {want[p]}, read {got[p]}")
    finally:
        # --- restore, and prove it. -----------------------------------------------------------
        for p in allp:
            if base[p] is not None:
                channels.set_channel_processing(ch, p, base[p])
        time.sleep(SETTLE)
        back, blob3 = read_strip(strip, allp)
        drift = {p: (base[p], back[p]) for p in allp if base[p] != back[p]}
        print(f"restored: {'byte-identical' if not drift else 'DRIFT ' + str(drift)}")
        if drift:
            fails.append(f"restore left the strip changed: {drift}")
        if len(blob0) != len(blob3) or c0 != counts(blob3, allp):
            fails.append(f"model changed shape: len {len(blob0)} -> {len(blob3)}, "
                         f"counts {c0} -> {counts(blob3, allp)} (a stray may have been appended)")
        else:
            print(f"no stray: model still {len(blob3)} bytes, occurrence counts unchanged")

    print("\nRESULT: " + ("PASS - Q is independent of gain and frequency, and directly writable"
                          if not fails else "FAIL\n  " + "\n  ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
