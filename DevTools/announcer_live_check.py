"""Live check of the control announcer's decoder against REAL hardware bytes. READ ONLY.

Opens its own read-only HID handle, feeds every report-04 packet through the REAL decoder
(announcer.handle_report) with speech.say captured instead of spoken, and prints what the announcer WOULD have said, with the raw prefix bytes beside it.

Why not just run the app and listen: this prints what would be said, so it can be read rather
than heard.

Usage: python DevTools/announcer_live_check.py [seconds] [--speak]
       --speak also SPEAKS the instructions, for a tester at the desk rather than the screen.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _desk
import announcer
import rcp_hid
import rcp_read
import service
import speech

service.install_announcer_hooks()

SAID: list[tuple[float, str]] = []
_t0 = time.monotonic()
speech.say = lambda text: SAID.append((time.monotonic() - _t0, text))


def speak(text: str, enabled: bool) -> None:
    print(f"[say] {text}")
    if enabled:
        _desk.say(text, wait=True)


def main() -> int:
    secs = float(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1][0].isdigit() else 40.0
    do_speak = "--speak" in sys.argv

    # Read the desk once, which the desk model adopts, so the announcer can NAME channels rather
    # than say "Channel N".
    try:
        vec = [v for v, _k in rcp_read.find_all(rcp_read.read_model_stable(fresh=True),
                                                "channelInputSource")]
        print(f"fader layout: {vec}")
    except Exception as exc:
        print(f"could not read the desk ({exc}); names will fall back to Channel N")

    if rcp_hid.find_control_interface() is None:
        print("no device")
        return 1
    st = {"pending": None, "at": 0.0}
    speak(f"Press mute and solo on any channels for the next {int(secs)} seconds.", do_speak)
    print(f"listening {secs:.0f}s ...")
    seen = 0
    end = time.monotonic() + secs
    with _desk.push_handle("announcer live check") as dev:
        while time.monotonic() < end:
            rep = dev.read(256, timeout_ms=200)
            if rep:
                seen += 1
                b = bytes(rep)
                before = len(SAID)
                announcer.handle_report(b, st)
                for t, text in SAID[before:]:
                    print(f"  {t:6.2f}s  SPOKEN: {text!r}")
            before = len(SAID)
            announcer.flush_pending(st)
            for t, text in SAID[before:]:
                print(f"  {t:6.2f}s  SPOKEN (press-edge fallback): {text!r}")

    print(f"\n{seen} report-04 packets, {len(SAID)} announcement(s):")
    for t, text in SAID:
        print(f"  {t:6.2f}s  {text}")
    if not SAID:
        print("  NOTHING - either no button was pressed, or the decoder missed it.")
    speak("Listening finished.", do_speak)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
