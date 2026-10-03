"""Probe the ROTARY ENCODER's press edges, to decide how to trigger a custom action.

Question this answers: can we distinguish a PRESS-AND-HOLD from a quick
DOUBLE-PRESS? Both would let us bind an extra, software-only action (Shaun's idea: speak
solo / mute / routing / bank on demand). A hold is nicer if it works, because the desk
advances its own encoder ring once per press, so a double-press costs one extra ring step.

That hinges on whether the desk pushes a RELEASE edge. The live-state work of
records
`encoderPressed` as "edge only" - but that was a passing note in a broader sweep, never a
test. `screenTouched` demonstrably pushes True then False, so a release edge is plausible.

Strictly READ-ONLY: opens its own HID handle and never writes, so it is safe to run with
the server up (Windows gives each open handle its own input queue - server.py:514).

Usage: python encoder_press_probe.py [seconds]
"""
import sys
import time

import hid
import _bootstrap  # noqa: F401  - puts Project/ on sys.path
import rcp_read
import rcp_transfer as t

# Watch the state properties too, not just the encoder. A HOLD is timeable (press True at 11.95 s,
# release False at 15.27 s), but the desk announces "Chat muted" during one, so a long press may
# already have a native action and be no free trigger at all. Watching channelOutputMute /
# channelCueEnable / systemChannelSelected alongside the press edges is what tells whether the mute
# really coincides with the hold.
WATCH = ("encoderPressed", "encoderSignal", "encoderColour", "screenTouched",
         "channelOutputMute", "channelCueEnable", "systemChannelSelected", "faderLevel",
         "outputMonLevel", "mutePressed", "soloPressed")

seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 45.0

iface = t.find_control_interface()
if iface is None:
    print("no control interface (is the unit connected?)")
    sys.exit(1)

dev = hid.device()
dev.open_path(iface.path)
dev.set_nonblocking(0)
print(f"listening {seconds:.0f}s - operate the encoder now")
sys.stdout.flush()

start = time.time()
events = []
try:
    while time.time() - start < seconds:
        rep = dev.read(256, timeout_ms=200)
        rec = rcp_read.parse_push(bytes(rep)) if rep else None
        if rec is None or rec[1] not in WATCH:
            continue
        _path, hit, val, _kind = rec
        ts = time.time() - start
        events.append((ts, hit, val))
        print(f"[{ts:6.3f}s] {hit} = {val}")
        sys.stdout.flush()
finally:
    dev.close()

presses = [e for e in events if e[1] == "encoderPressed"]
print(f"\ndone. {len(events)} encoder/screen events, {len(presses)} encoderPressed edges.")
if presses:
    vals = sorted({str(v) for _t, _n, v in presses})
    print(f"encoderPressed values seen: {', '.join(vals)}")
    print("  -> RELEASE EDGE PRESENT (hold is timeable)" if "False" in vals
          else "  -> press-down only: no release edge, so a HOLD cannot be timed")
    gaps = [round(presses[i][0] - presses[i - 1][0], 3) for i in range(1, len(presses))]
    if gaps:
        print(f"gaps between consecutive press edges (s): {gaps}")
