"""Sample Mic 1 and USB 2's mute and cue state while a Back Channel pad is pressed.

A Back Channel pad moves `channelOutputMute` and `channelCueEnable` on its members and restores
them on release, so a pad that is doing nothing and a pad that is doing its job look identical once
the finger is off. Polling the channels answers it, reading the desk and writing nothing: hold the
pad while this runs and the hold shows up as a row where the flags differ from the idle ones. Quit
AccessCaster first.

Usage:
    python DevTools/watch_backchannel.py [seconds]
"""
import sys
import time

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _desk

WATCH = ("Mic1", "USB2")
RETRY_S = 1.0       # after a failed read, so an absent desk is not asked as fast as it refuses
server = None


def sample() -> tuple[dict | None, str]:
    """({channel: (muted, cue)} for the watched channels, "") or (None, why the read failed)."""
    d = server.action_get_channels()
    if not d.get("ok"):
        return None, d.get("message") or "no reply"
    return {c["name"]: (bool(c.get("muted")), bool(c.get("solo")))
            for c in d.get("channels") or [] if c["name"] in WATCH}, ""


def main():
    global server
    server = _desk.service()
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    start = time.monotonic()
    while time.monotonic() - start < secs:
        state, why = sample()
        if state is None:
            print(f"{time.monotonic() - start:5.1f}  read failed: {why}", flush=True)
            time.sleep(RETRY_S)
            continue
        words = "  ".join(f"{n} mute={m} cue={c}" for n, (m, c) in state.items())
        print(f"{time.monotonic() - start:5.1f}  {words}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
