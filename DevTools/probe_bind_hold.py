"""Causal test of the bind: hold Windows' own stream on the desk's Main output from the moment it
appears, and see whether RODE's service binds only once it is released.

    python DevTools/probe_bind_hold.py 4        four trials

Per trial: Expanded with the mix only, then post-fader. The moment the new Main Multitrack
playback endpoint appears, a silent shared-mode stream is started on it and held HOLD_S seconds,
then stopped. Each trial reports whether the service bound before the hold (it won the race),
during it, or after release. Every trial is two USB arrivals, so audio through the desk drops out
each time. Needs the app's server running. Restores Standard, mix only, however the run ends.
"""
import sys
import threading
import time

import _bootstrap  # noqa: F401  (puts Project/ on the path)
import comtypes
import comtypes.client
import meters
import virtual_devices as vd
from _desk import api

HOLD_S = 10.0
SETTLE_LIMIT_S = 60.0   # a driver switch takes a few seconds; one still going after this is stuck


def post(path: str, body: dict) -> None:
    """POST to the running server, stopping the run when the request fails."""
    res = api(path, post=body, timeout=60.0)
    if not res.get("ok"):
        raise RuntimeError(f"{path} failed: {res.get('message')}")


def settle() -> None:
    """Wait for the driver switch the last request started to finish."""
    time.sleep(2)
    deadline = time.monotonic() + SETTLE_LIMIT_S
    while True:
        st = api("/api/routing/driver")
        if "switching" not in st:
            raise RuntimeError(f"could not read the driver state: {st.get('message')}")
        if not st["switching"]:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(f"the driver was still switching after {SETTLE_LIMIT_S:.0f} s")
        time.sleep(1)


def main_endpoints(enum):
    out = {}
    coll = enum.EnumAudioEndpoints(meters.RENDER, 1)
    for i in range(coll.GetCount()):
        dev = coll.Item(i)
        name = meters._friendly_name(dev)
        if "RODECaster" in name and "Main" in name:
            out[dev.GetId()] = (name, dev)
    return out


def holder(before, result):
    comtypes.CoInitialize()
    enum = comtypes.client.CreateObject(meters.CLSID_MMDeviceEnumerator,
                                        interface=meters.IMMDeviceEnumerator)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 20:
        for eid, (name, dev) in main_endpoints(enum).items():
            if eid in before:
                continue
            try:
                client = dev.Activate(meters.IAudioClient._iid_, meters.CLSCTX_ALL,
                                      None).QueryInterface(meters.IAudioClient)
                fmt = client.GetMixFormat()
                client.Initialize(0, 0, 1000000, 0, fmt, None)
                client.Start()
            except Exception as exc:
                result["held"] = f"could not open {name}: {exc}"
                return
            result["held"] = f"{name} held from +{time.monotonic() - t0:.2f}s"
            result["held_at"] = time.monotonic()
            time.sleep(HOLD_S)
            client.Stop()
            result["released_at"] = time.monotonic()
            del client
            return
        time.sleep(0.03)
    result["held"] = "the endpoint never appeared"


def verdict(first_bind: float | None, held: float | None, released: float | None) -> str:
    """When the service bound, against the hold the holder thread managed (if any)."""
    if first_bind is None:
        return "never bound"
    if held is None:
        return "bound, but nothing was held, so the trial says nothing about the hold"
    if first_bind < held:
        return (f"bound BEFORE the hold started ({held - first_bind:.2f}s early): "
                "the service won the race")
    if released is not None and first_bind >= released:
        return f"bound {first_bind - released:.2f}s AFTER release"
    return f"bound DURING the hold, {first_bind - held:.2f}s in"


def trial(n: int, enum) -> None:
    post("/api/routing/usb-multitrack", {"mode": 0})
    settle()
    time.sleep(3)
    before = set(main_endpoints(enum))
    result = {}
    th = threading.Thread(target=holder, args=(before, result))
    th.start()
    post("/api/routing/usb-multitrack", {"mode": 2})
    first_bind = None
    t0 = time.monotonic()
    while time.monotonic() - t0 < 25:
        if vd.read_status().connected and first_bind is None:
            first_bind = time.monotonic()
        time.sleep(0.05)
    th.join()
    found = verdict(first_bind, result.get("held_at"), result.get("released_at"))
    print(f"trial {n}: {result.get('held')}; {found}", flush=True)
    settle()


def main() -> int:
    trials = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    comtypes.CoInitialize()
    enum = comtypes.client.CreateObject(meters.CLSID_MMDeviceEnumerator,
                                        interface=meters.IMMDeviceEnumerator)
    try:
        post("/api/routing/usb-expanded", {"on": True})
        settle()
        for n in range(1, trials + 1):
            trial(n, enum)
    finally:
        put_back()
    return 0


def put_back() -> None:
    """Standard, mix only. Each step is tried even when the one before it failed."""
    for path, body in (("/api/routing/usb-expanded", {"on": False}),
                       ("/api/routing/usb-multitrack", {"mode": 0})):
        try:
            post(path, body)
            settle()
        except (RuntimeError, TimeoutError) as e:
            print(f"COULD NOT PUT BACK {path} {body}: {e}", flush=True)
            continue
        print(f"put back {path} {body}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
