"""LIVE round-trip of the addressed input read, on whichever desk is attached.

This is the test that would have caught the Duo mis-attribution: it writes ONE mic's gain and
asserts the OTHER mics did not move. The offline half is DevTools/test_input_read.py; this one
proves the addresses are right on real hardware, which no synthetic dump can.

SAFE AND REVERSIBLE. Every mic's gain is read first, written, read back, and restored, and the
final read is asserted to match the original. Nothing else is touched: no phantom power, no mic
type (which moves the gain), no processing.

Run: python DevTools/test_input_read_live.py
"""
from __future__ import annotations
import sys
import time

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import channels as c
import devices
import rcp_read as rr

FAILED = 0
SETTLE_S = 0.6          # the desk needs a moment before the next dump shows the write


def check(label, got, want):
    global FAILED
    if got == want:
        print(f"  ok   {label}")
    else:
        FAILED += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def gains(dev, mics):
    """{name: gain} read fresh, by address."""
    blob = rr.read_model_stable(fresh=True)
    return {m["name"]: c.input_value(blob, m, "inputMicrophoneGain", dev) for m in mics}


dev = devices.get_active_device()
mics = [e for e in dev.ui_channels if e.get("kind") == "mic"]
print(f"device: {dev.name}, {len(mics)} mic input(s): {', '.join(m['label'] for m in mics)}")
if "channel_read" not in dev.caps or not mics:
    sys.exit("this desk has no verified mic read map - nothing to test")

original = gains(dev, mics)
print(f"baseline gains: {original}")
if any(v is None for v in original.values()):
    sys.exit(f"a mic gain read as None, so the addresses are wrong before we even write: {original}")

# A test value each mic can hold that is NOT its current one, and differs BETWEEN mics so a
# mis-attribution cannot hide behind two mics happening to match.
TEST = [33, 41, 27, 46]

try:
    for n, mic in enumerate(mics):
        want = TEST[n % len(TEST)]
        if want == original[mic["name"]]:
            want = want + 1
        print(f"\nwriting {mic['label']} = {want} dB")
        c.set_mic_gain(c.mic_channel_number(mic), want)
        time.sleep(SETTLE_S)
        now = gains(dev, mics)
        check(f"{mic['label']} reads back its own new value", now[mic["name"]], want)
        for other in mics:
            if other["name"] == mic["name"]:
                continue
            check(f"  {other['label']} did NOT move",
                  now[other["name"]], original[other["name"]])
        c.set_mic_gain(c.mic_channel_number(mic), original[mic["name"]])
        time.sleep(SETTLE_S)
finally:
    # Restore unconditionally, even if an assertion above blew up.
    for mic in mics:
        c.set_mic_gain(c.mic_channel_number(mic), original[mic["name"]])
    time.sleep(SETTLE_S)

print()
check("every mic is back at its baseline", gains(dev, mics), original)

# read_mixer is the other public reader of these values and must agree with read_channels.
by_channels = {m["name"]: v for m, v in
               zip(mics, [c.input_value(rr.read_model(), m, "inputMicrophoneGain", dev) for m in mics])}
by_mixer = {m["name"]: c.read_mixer()["mics"][c.mic_channel_number(m)]["gain_db"] for m in mics}
check("read_mixer agrees with the addressed read", by_mixer, by_channels)

print()
if FAILED:
    print(f"{FAILED} CHECK(S) FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
