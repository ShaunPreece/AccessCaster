"""Prove the LEVEL METERS on the real machine. RUN THIS AT THE COMPUTER, not over Remote Desktop.

WHY THAT WARNING IS THE FIRST LINE. Windows gives every login session its own audio endpoints, and a
Remote Desktop session gets exactly one, called "Remote Audio". The RODECaster's four USB audio
streams belong to the CONSOLE session, so from a remote session they are not merely quiet - they do
not exist. Every other feature in AccessCaster works identically over RDP, because everything else
talks to the desk over USB HID, which is session-independent. Metering is the one exception, and it
is the reason this tool exists as a tool rather than as a line in another test: the metering code
was written in an RDP session and could not be exercised there.

It reads only. Nothing is written to the desk, nothing is written to disk, and no setting changes.

    python DevTools/test_meters_live.py                  # list the streams, then meter for 10 s
    python DevTools/test_meters_live.py --seconds 30
    python DevTools/test_meters_live.py --speak          # also speak, exactly as the app would

WHAT TO LISTEN FOR / LOOK FOR, in order:
  1. The stream list should name the desk's own outputs (Main Stereo, Chat, Secondary, and
     Multitrack if it is switched on). If it says Remote Audio, you are still on RDP.
  2. Talk into Mic 1. The Main Stereo peak should move. If it stays at 0.0000 while you can hear
     yourself in the headphones, the meter is reading a stream that carries nothing - try another.
  3. Shout, or push a fader up hard, and check the CLIPPING line appears.
  4. Stop talking for five seconds and check it notices the silence.
"""
from __future__ import annotations
import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import meters
from paths import resource_path

BAR = 40


def speaker(enabled: bool):
    """Speak through the same one-shot the app falls back to, so the voice and the output device
    the user has chosen are honoured rather than bypassed."""
    if not enabled:
        return lambda text: None
    script = resource_path("say.ps1")

    def say(text):
        try:
            subprocess.Popen(["powershell", "-NoProfile", "-File", script, "-Text", text],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    return say


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--stream", default="", help="endpoint id, or part of its name")
    ap.add_argument("--speak", action="store_true", help="speak the warnings too")
    args = ap.parse_args()

    if meters._remote_session():
        print("!! THIS IS A REMOTE DESKTOP SESSION.")
        print("!! The desk's audio streams belong to the console session and are not visible here.")
        print("!! Run this again while logged in AT the computer. Continuing anyway, so you can")
        print("!! see what the app would see.\n")

    print("EVERY audio endpoint Windows reports")
    for e in meters.list_endpoints():
        mark = "*" if meters.is_desk_endpoint(e["name"]) else " "
        print(f" {mark} {e['flow']:9} {e['state']:12} {e['name']}")

    found = meters.desk_streams()
    print()
    if not found["ok"]:
        print("NO DESK STREAMS TO METER.")
        print(found["reason"])
        return 1

    print(f"{len(found['streams'])} desk stream(s) available:")
    for s in found["streams"]:
        print(f"   {s['name']}  ({s['flow']})")

    want = args.stream.lower()
    chosen = next((s for s in found["streams"]
                   if want and (want == s["id"].lower() or want in s["name"].lower())),
                  found["streams"][0])
    print(f"\nmetering: {chosen['name']}   (make some noise on it)\n")

    said = []

    def say(text):
        said.append(text)
        print(f"    >>> SPOKEN: {text}")
        speaker(args.speak)(text)

    w = meters.Watcher(chosen["id"], say, silence_after=5.0, speak_every=0.0)
    if not w.start():
        print("could not start the meter:", w.error)
        return 1
    peak_seen = 0.0
    try:
        end = time.time() + args.seconds
        while time.time() < end:
            v = w.peak
            peak_seen = max(peak_seen, v)
            filled = int(v * BAR)
            print(f"\r  [{'#' * filled}{'-' * (BAR - filled)}] {v * 100:5.1f}%  "
                  f"peak {peak_seen * 100:5.1f}%", end="", flush=True)
            time.sleep(0.1)
    finally:
        w.stop()
    print("\n")

    print("RESULT")
    print(f"  highest peak seen: {peak_seen * 100:.1f} percent")
    print(f"  spoken warnings:   {said or 'none'}")
    print(f"  windows endpoint:  {w.volume_db:+.2f} dB, so full scale reads "
          f"{w.ceiling * 100:.1f}% and clipping is called at "
          f"{0.99 * meters.trusted_ceiling(w.ceiling) * 100:.1f}%")
    print(f"  the app would say: "
          f"{meters.describe(peak_seen, 1.0, chosen['name'], wake_failed=w.wake_failed, ceiling=w.ceiling, volume_db=w.volume_db)}")
    note = meters.level_note(w.volume_db)
    if note:
        print(f"  ABOUT THAT LEVEL:  {note}")
    if w.wake_failed:
        print("\n  THE ENGINE WAKER FAILED on this input, so 0.0 means nothing either way. That is")
        print(" the bug fixed on regressing: see meters._EngineWaker.")
        return 1
    if peak_seen <= meters.SILENCE:
        print("\n  NOTHING WAS HEARD. Either nothing was playing on that stream, or it is not the")
        print("  one carrying your audio. Try --stream chat, or --stream secondary.")
        print("  MIND WHICH SIDE OF THE PAIR YOU PICKED: 'Speakers (... Main Stereo)' is what this")
        print("  PC SENDS TO the desk (screen-reader speech lands there); the microphone comes back")
        print("  on 'Microphone (... Main Stereo)'. Metering the wrong one hid a real bug once.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
