"""Causal test of the bind: hold Windows' own stream on the desk's Main output from the moment it
appears, and see whether RODE's service binds only once it is released.

    python DevTools/probe_bind_hold.py 4        four trials

Per trial: Expanded with the mix only, then post-fader. The moment the new Main Multitrack
playback endpoint appears, a silent shared-mode stream is started on it and held HOLD_S seconds,
then stopped. Each trial reports whether the service bound before the hold (it won the race),
during it, or after release. Every trial is two USB arrivals, so audio through the desk drops out
each time. Needs the app's server running. Restores Standard, mix only.
"""
import json, sys, threading, time, urllib.request

import _bootstrap  # noqa: F401  (puts Project/ on the path)

import comtypes, comtypes.client, meters, virtual_devices as vd

B = "http://127.0.0.1:8770"
HOLD_S = 10.0
TRIALS = int(sys.argv[1]) if len(sys.argv) > 1 else 3

def post(path, body):
    r = urllib.request.Request(B + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=60).read())

def settle():
    time.sleep(2)
    while json.loads(urllib.request.urlopen(B + "/api/routing/driver").read())["switching"]:
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
    enum = comtypes.client.CreateObject(meters.CLSID_MMDeviceEnumerator, interface=meters.IMMDeviceEnumerator)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 20:
        for eid, (name, dev) in main_endpoints(enum).items():
            if eid in before:
                continue
            try:
                client = dev.Activate(meters.IAudioClient._iid_, meters.CLSCTX_ALL, None).QueryInterface(meters.IAudioClient)
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

comtypes.CoInitialize()
enum = comtypes.client.CreateObject(meters.CLSID_MMDeviceEnumerator, interface=meters.IMMDeviceEnumerator)
post("/api/routing/usb-expanded", {"on": True}); settle()
for trial in range(1, TRIALS + 1):
    post("/api/routing/usb-multitrack", {"mode": 0}); settle(); time.sleep(3)
    before = set(main_endpoints(enum))
    result = {}
    th = threading.Thread(target=holder, args=(before, result)); th.start()
    post("/api/routing/usb-multitrack", {"mode": 2})
    first_bind = None
    t0 = time.monotonic()
    while time.monotonic() - t0 < 25:
        if vd.read_status().connected and first_bind is None:
            first_bind = time.monotonic()
        time.sleep(0.05)
    th.join()
    held = result.get("held_at"); rel = result.get("released_at")
    if first_bind is None:
        verdict = "never bound"
    elif held and first_bind < held:
        verdict = f"bound BEFORE the hold started ({held - first_bind:.2f}s early): the service won the race"
    elif rel and first_bind >= rel:
        verdict = f"bound {first_bind - rel:.2f}s AFTER release"
    else:
        verdict = f"bound DURING the hold, {first_bind - held:.2f}s in"
    print(f"trial {trial}: {result.get('held')}; {verdict}", flush=True)
    settle()

post("/api/routing/usb-expanded", {"on": False}); settle()
post("/api/routing/usb-multitrack", {"mode": 0}); settle()
print("restored Standard, mix only", flush=True)
