"""Does Windows' default output sitting on the driver's own endpoint stop the service binding?

RODE's service binds to the desk on some arrivals and not others. One candidate nobody has ruled
out: while `System (RØDECaster)` is the default playback device, Windows' audio engine holds a
render stream open on the very endpoint the service is trying to build, and the desk's audio
interfaces are in flux at exactly that moment.

This moves the default output to a device that is neither the desk nor the driver, drives the USB
output mode off and back to make arrivals, and counts how many arrivals produce a bind. Compare
the rate against the same count with the default left on `System`.

    python DevTools/probe_bind_default_output.py --cycles 6
    python DevTools/probe_bind_default_output.py --cycles 6 --keep-default

It restores the default output it found, whatever happens, and every flip is a mode the desk
already supports. The app's server must be running: the flips go through its API so that one
process keeps the HID handle.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.request

import _bootstrap  # noqa: F401

import comtypes
import comtypes.client

import meters
import virtual_devices as vd
from bind_timeline import interfaces

API = "http://127.0.0.1:8770/api/routing/usb-multitrack"
EXPANDED_PID = "pid_0094"       # post-fader multitrack AND Expanded: the mode with fader inputs


def post_mode(mode: int, timeout: float = 150.0) -> str:
    body = json.dumps({"mode": mode}).encode()
    req = urllib.request.Request(API, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode()).get("ok")


class Sampler(threading.Thread):
    """Count arrivals of the desk in the Expanded multitrack mode, and binds of the service.

    An arrival is the HID interface going ACTIVE, which is the interface the service polls for.
    Counting arrivals rather than flips is what makes the rate comparable: the app's own follow-up
    makes extra arrivals whenever it misses, and every one of those is another trial."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.stop = threading.Event()
        self.arrivals = 0
        self.binds = 0
        self.events: list = []

    def run(self) -> None:
        at = time.monotonic()
        was_here = False
        was_bound = vd.read_status().connected
        while not self.stop.is_set():
            here = any(EXPANDED_PID in path and active
                       for path, active in interfaces("hid").items())
            bound = vd.read_status().connected
            now = time.monotonic() - at
            if here and not was_here:
                self.arrivals += 1
                self.events.append(f"{now:7.1f}s  arrival {self.arrivals}")
            if bound and not was_bound:
                self.binds += 1
                self.events.append(f"{now:7.1f}s  BOUND after arrival {self.arrivals}")
            if was_bound and not bound:
                self.events.append(f"{now:7.1f}s  unbound")
            was_here, was_bound = here, bound
            time.sleep(0.2)


def pick_target(eps: list) -> "vd.Endpoint | None":
    """A live playback device that is neither the desk's nor the driver's."""
    return next((e for e in eps if e.flow == "playback" and e.state == "active"
                 and not e.desk and not e.virtual), None)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cycles", type=int, default=6)
    ap.add_argument("--keep-default", action="store_true",
                    help="leave the default output where this probe put it")
    ap.add_argument("--control", action="store_true",
                    help="THE MATCHED CONTROL: move nothing, so the same cycles run with the "
                         "default output wherever it already is. Without this run the rate means "
                         "nothing, because a run of failures alone would produce the same drop.")
    args = ap.parse_args()

    comtypes.CoInitialize()
    enum = comtypes.client.CreateObject(meters.CLSID_MMDeviceEnumerator,
                                        interface=meters.IMMDeviceEnumerator)
    policy = comtypes.client.CreateObject(vd.CLSID_PolicyConfigClient, interface=vd.IPolicyConfig)
    eps = vd._endpoints(enum)
    by_id = {e.id: e for e in eps}
    was_default_id = vd._default_id(enum, vd.E_CONSOLE)
    was_default = by_id.get(was_default_id)
    target = pick_target(eps)
    if target is None:
        print("No playback device outside the desk and the driver. Nothing to move to.")
        return
    print(f"default was : {was_default.name if was_default else '?'}")
    if args.control:
        print("control run : the default output is not moved")
    else:
        print(f"moving to   : {target.name}")
        policy.SetDefaultEndpoint(target.id, vd.E_CONSOLE)
        policy.SetDefaultEndpoint(target.id, vd.E_MULTIMEDIA)

    sampler = Sampler()
    sampler.start()
    try:
        for i in range(1, args.cycles + 1):
            print(f"cycle {i}: mix only ...", flush=True)
            post_mode(0)
            time.sleep(4)
            print(f"cycle {i}: post-fader ...", flush=True)
            post_mode(2)
            # The service binds within about three seconds of an arrival or not at all, but the
            # app's own follow-up may still be making arrivals of its own, so give the cycle room
            # rather than calling a miss early.
            time.sleep(25)
            st = vd.read_status()
            print(f"cycle {i}: connected={st.connected}  "
                  f"arrivals so far={sampler.arrivals} binds={sampler.binds}", flush=True)
    finally:
        sampler.stop.set()
        sampler.join(timeout=3)
        if not args.control and not args.keep_default and was_default is not None:
            policy.SetDefaultEndpoint(was_default_id, vd.E_CONSOLE)
            policy.SetDefaultEndpoint(was_default_id, vd.E_MULTIMEDIA)
            print(f"default restored to: {was_default.name}")

    print()
    for line in sampler.events:
        print(" ", line)
    rate = (sampler.binds / sampler.arrivals) if sampler.arrivals else 0.0
    where = "ON the driver (control)" if args.control else "off the driver"
    print(f"\n{sampler.binds} binds in {sampler.arrivals} arrivals  ({rate:.0%}), "
          f"default output {where}")


if __name__ == "__main__":
    main()
