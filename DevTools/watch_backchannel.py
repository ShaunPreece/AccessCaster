"""Sample Mic 1 and USB 2's mute and cue state while a Back Channel pad is pressed.

A Back Channel pad moves `channelOutputMute` and `channelCueEnable` on its members and restores
them on release, so a pad that is doing nothing and a pad that is doing its job look identical once
the finger is off. Polling through the running service answers it without touching the device: hold
the pad while this runs and the hold shows up as a row where the flags differ from the idle ones.

Usage:
    python DevTools/watch_backchannel.py [seconds]
"""
import json
import sys
import time
import urllib.request

WATCH = ("Mic1", "USB2")


def sample():
    with urllib.request.urlopen("http://127.0.0.1:8770/api/channels", timeout=90) as r:
        chans = json.load(r)["channels"]
    return {c["name"]: (bool(c.get("muted")), bool(c.get("solo")))
            for c in chans if c["name"] in WATCH}


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    end = time.time() + secs
    start = time.time()
    while time.time() < end:
        try:
            state = sample()
        except Exception as exc:
            print(f"{time.time() - start:5.1f}  read failed: {exc}", flush=True)
            continue
        words = "  ".join(f"{n} mute={m} cue={c}" for n, (m, c) in state.items())
        print(f"{time.time() - start:5.1f}  {words}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
