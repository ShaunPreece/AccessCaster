"""Live check of the control announcer's decoder against REAL hardware bytes. READ ONLY.

Opens its own read-only HID handle (exactly as server._announce_loop does), feeds every report-04
packet through the REAL decoder (server._ann_handle_report) with _ann_say captured instead of
spoken, and prints what the announcer WOULD have said, with the raw prefix bytes beside it.

Why not just run the app and listen: say.ps1 self-gates when the Claude GUI has focus, so the
announcements are silent while Shaun is reading chat. This prints them instead, and it needs no
server restart (no fighting the installed build for port 8770).

Usage: python DevTools/announcer_live_check.py [seconds] [--speak]
       --speak also SPEAKS the instructions (use when Shaun is at the desk, not reading chat).
"""
from __future__ import annotations
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401  (puts Project/ on sys.path)

import hid
import server
import channels
import rcp_read
import rcp_transfer

SAID: list[tuple[float, str]] = []
_t0 = time.time()
server._ann_say = lambda text: SAID.append((time.time() - _t0, text))

SAY_PS1 = str(Path(__file__).resolve().parent.parent / "Project" / "say.ps1")


def speak(text: str, enabled: bool) -> None:
    print(f"[say] {text}")
    if enabled:
        subprocess.run(["powershell", "-ExecutionPolicy", "Bypass", "-File", SAY_PS1,
                        "-Text", text, "-Force"], capture_output=True)


def main() -> int:
    secs = float(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1][0].isdigit() else 40.0
    do_speak = "--speak" in sys.argv

    # Warm the layout cache so the announcer can NAME channels rather than say "Channel N".
    # This is the same free refresh the app gets off any read_channels dump.
    try:
        vec = [v for v, _k in rcp_read.find_all(rcp_read.read_model_stable(fresh=True),
                                                "channelInputSource")]
        channels.publish_strip_vector(vec)
        print(f"fader layout: {vec}")
    except Exception as exc:
        print(f"could not warm the layout cache ({exc}); names will fall back to Channel N")

    iface = rcp_transfer.find_control_interface()
    if iface is None:
        print("no device")
        return 1
    dev = hid.device()
    dev.open_path(iface.path)
    st = {"bank": None, "pending": None, "at": 0.0}
    speak(f"Press mute and solo on any channels for the next {int(secs)} seconds.", do_speak)
    print(f"listening {secs:.0f}s ...")
    seen = 0
    end = time.time() + secs
    try:
        while time.time() < end:
            rep = dev.read(256, timeout_ms=200)
            if rep:
                seen += 1
                b = bytes(rep)
                before = len(SAID)
                server._ann_handle_report(b, st)
                for t, text in SAID[before:]:
                    print(f"  {t:6.2f}s  SPOKEN: {text!r}")
            before = len(SAID)
            server._ann_flush_pending(st)
            for t, text in SAID[before:]:
                print(f"  {t:6.2f}s  SPOKEN (press-edge fallback): {text!r}")
    finally:
        dev.close()

    print(f"\n{seen} report-04 packets, {len(SAID)} announcement(s):")
    for t, text in SAID:
        print(f"  {t:6.2f}s  {text}")
    if not SAID:
        print("  NOTHING - either no button was pressed, or the decoder missed it.")
    speak("Listening finished.", do_speak)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
