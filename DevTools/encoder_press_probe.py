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
import re
import sys
import time

import hid
import _bootstrap  # noqa: F401  - puts Project/ on sys.path
import rcp_transfer as t

# Watch the state properties too, not just the encoder. A HOLD is timeable (press True at 11.95 s,
# release False at 15.27 s), but the desk announces "Chat muted" during one, so a long press may
# already have a native action and be no free trigger at all. Watching channelOutputMute /
# channelCueEnable / systemChannelSelected alongside the press edges is what tells whether the mute
# really coincides with the hold.
WATCH = ("encoderPressed", "encoderSignal", "encoderColour", "screenTouched",
         "channelOutputMute", "channelCueEnable", "systemChannelSelected", "faderLevel",
         "outputMonLevel", "mutePressed", "soloPressed")
NAME_RE = re.compile(rb"[A-Za-z][A-Za-z0-9_]{3,}")

seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 45.0


def decode_bool(data: bytes, after: int):
    """01 <len> <02 true | 03 false> after the name. Mirrors server._ann_decode_bool."""
    v = data[after:]
    if v[:1] == b"\x00":
        v = v[1:]
    if len(v) >= 3 and v[0] == 0x01 and v[2] in (0x02, 0x03):
        return v[2] == 0x02
    return None


def decode_int(data: bytes, after: int):
    b = data[after:after + 7]
    if b[:3] == b"\x01\x05\x01":
        return int.from_bytes(b[3:7], "little", signed=True)
    return None


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
        if not rep or rep[0] != 0x04:
            continue
        b = bytes(rep)
        for m in NAME_RE.finditer(b):
            name = m.group().decode("latin-1")
            hit = next((w for w in WATCH if name.endswith(w)), None)
            if not hit:
                continue
            val = decode_bool(b, m.end() + 1)
            if val is None:
                val = decode_int(b, m.end() + 1)
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
