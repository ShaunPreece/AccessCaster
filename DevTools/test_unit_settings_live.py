"""LIVE hardware check of two features: the UNIT SETTINGS and MUTE.

WHY THIS EXISTS. Both are ordinary global property writes on selectors that were captured and
read-back verified, so the risk is not the protocol: it is the PAIRING. A record whose
selector-plus-property pairing is wrong does not simply miss, it can land on some other object and
append a stray occurrence that only a factory reset clears (btDoScan on a predicted
NETWORK selector). So every write here is read back, and the run ends with the check that actually
catches a stray: OCCURRENCE COUNTS per property, diffed against a pre-test snapshot. Counting
distinct NAMES is not enough - the stray copy carries a name that already existed, which is exactly
why the earlier scan test looked clean while a stray had just been appended.

HOW IT DRIVES. Our own service on 127.0.0.1:8770, over HTTP. No GUI, no RODE app, and it never opens
the HID device itself, so it cannot fight the running server for the control interface.

WHAT IT DELIBERATELY DOES NOT TOUCH: `outputMonFixed` (Fixed Output Level). Everything else here is
visual or behavioural, but that one changes what the MONITOR OUTPUT sends, and Shaun's monitor level
sits at a few percent - a fixed full-level output into speakers is not something to try unasked. It
is READ and reported, and left alone.

Run:  python DevTools/test_unit_settings.py
"""

import json
import os
import shutil
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8770"
CHAN = "Mic2"                    # nothing is connected to Mic 2 on this desk
PRE, POST = "PRE_UNIT_TEST", "POST_UNIT_TEST"       # temporary profiles, deleted at the end
SETTLE_S = 0.8

# prop -> the value to write instead of whatever is there. Chosen so every one is a REAL change from
# the values on Shaun's desk, since a write that happens to match the current value proves nothing.
TEST_VALUE = {
    "outputPrefader": {0: 1, 1: 0},
    "systemRecButtonSetting": {0: 1, 1: 0},
    "systemHapticSetting": {0: 2, 1: 2, 2: 1},
    "metering": {0: 1, 1: 0},
    "autoBrightness": {0: 1, 1: 0},
    "screenDimAfterSeconds": {0: 1800},              # Never -> 30 minutes (and back)
    "screenBrightness": None,                        # numeric: handled by _numeric_target
    "activeButtonsBrightness": None,
    "inactiveButtonsBrightness": None,
}
SKIP = {"outputMonFixed": "changes what the monitor output sends; not tried unasked"}

FAILED = 0


def check(label, got, want):
    global FAILED
    if got == want:
        print(f"  ok   {label}: {got!r}")
    else:
        FAILED += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=120) as r:
        return json.load(r)


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)


def unit_values():
    d = get("/api/device/settings")
    if not d.get("ok"):
        raise SystemExit("could not read the unit settings: " + str(d.get("message")))
    return {s["prop"]: s["value"] for s in d["settings"]}, d


def _numeric_target(spec, current):
    """A different value inside the slider's range, well clear of both ends."""
    lo, hi = int(spec["min"]), int(spec["max"])
    mid = lo + (hi - lo) // 3
    return mid if current != mid else mid + 10


def profile_snapshot(name):
    """Save a profile and return (its unit block, {prop: occurrence count}, {prop: values}).

    WAITS FOR THE JOB. /api/profiles/save STARTS a background save and answers immediately, so
    reading the file straight afterwards is a race: it passed for as long as the save happened to
    win, then began failing with FileNotFoundError. The save's own status endpoint is the answer,
    and it is what the browser polls too.
    """
    res = post("/api/profiles/save", {"name": name, "include_pads": False})
    if not res.get("ok"):
        raise SystemExit(f"could not save the {name} profile: {res.get('message')}")
    deadline = time.time() + 180
    while time.time() < deadline:
        st = get("/api/profiles/save/status")
        if st.get("done"):
            if not st.get("ok"):
                raise SystemExit(f"the {name} profile did not save: {st.get('message')}")
            break
        time.sleep(0.5)
    else:
        raise SystemExit(f"the {name} profile save never finished")
    path = os.path.join(os.environ["LOCALAPPDATA"], "AccessCaster", "profiles", name, "profile.json")
    with open(path, encoding="utf-8") as f:
        prof = json.load(f)
    props = prof["device_snapshot"]["properties"]
    return prof.get("unit") or {}, {p: len(v) for p, v in props.items()}, props


def delete_profile(name):
    shutil.rmtree(os.path.join(os.environ["LOCALAPPDATA"], "AccessCaster", "profiles", name),
                  ignore_errors=True)


# --- baseline ------------------------------------------------------------------------------------

print("BASELINE (a full profile, which is also the stray check's reference)")
pre_unit, pre_counts, pre_props = profile_snapshot(PRE)
start, payload = unit_values()
# COMPARED OVER THE PAGE'S OWN KEYS, not as whole dicts. A profile records everything WRITABLE and
# the Device page shows a deliberate subset of it (server.UNIT_SETTINGS_NOT_ON_PAGE says which is
# left off and why), so the two are not meant to be equal. Comparing them WHOLE fails on the first
# check before anything is written, and reads as a broken test rather than as the design. What
# matters is that where they overlap they AGREE: two read paths, one desk. Membership itself is
# guarded by DevTools/test_specs.py.
overlap = {k: v for k, v in pre_unit.items() if k in start}
check("the profile and the Device page agree on every setting the page shows", overlap, start)
extra = sorted(set(pre_unit) - set(start))
print(f"  info the profile also carries {len(extra)} setting(s) the page does not show: {extra}")
print(f"  info the desk right now: {json.dumps(start)}")
for prop, why in SKIP.items():
    print(f"  info NOT written: {prop} = {start.get(prop)!r} ({why})")

specs = {s["prop"]: s for s in payload["settings"]}

# --- every setting: write a different value, read it back, put it back ---------------------------

print("\nWRITE + READ BACK + RESTORE, one setting at a time")
for prop, spec in specs.items():
    if prop in SKIP:
        continue
    current = start[prop]
    if spec["kind"] == "int":
        target = _numeric_target(spec, current)
    else:
        target = (TEST_VALUE.get(prop) or {}).get(current)
    if target is None:
        print(f"  info {prop}: no test value for its current {current!r}, skipped")
        continue
    res = post("/api/device/setting", {"prop": prop, "value": target})
    time.sleep(SETTLE_S)
    got = unit_values()[0][prop]
    check(f"{prop} {current!r} -> {target!r} reads back", got, target)
    if not res.get("ok"):
        check(f"{prop} write reported ok", res.get("message"), "ok")
    post("/api/device/setting", {"prop": prop, "value": current})
    time.sleep(SETTLE_S)
    check(f"{prop} restored", unit_values()[0][prop], current)

print("\nA VALUE OUTSIDE THE SPEC IS REFUSED, NOT WRITTEN")
res = post("/api/device/setting", {"prop": "systemHapticSetting", "value": 99})
check("a choice value that is not on offer is refused", res.get("ok"), False)
res = post("/api/device/setting", {"prop": "definitelyNotASetting", "value": 1})
check("an unknown property is refused", res.get("ok"), False)

# --- mute --------------------------------------------------------------------------------------

print(f"\nMUTE {CHAN} FROM SOFTWARE, READ BACK, UNMUTE")


def channel_state(name):
    d = get("/api/channels")
    for c in d["channels"]:
        if c["name"] == name:
            return c
    raise SystemExit(f"{name} not found")


was_muted = channel_state(CHAN).get("muted")
print(f"  info {CHAN} starts {'MUTED' if was_muted else 'unmuted'}")
res = post("/api/channels/mute", {"name": CHAN, "on": not was_muted})
check("the write reports ok", res.get("ok"), True)
print(f"  info message: {res.get('message')}")
time.sleep(SETTLE_S)
check("the desk reports the new mute state", channel_state(CHAN).get("muted"), not was_muted)
# The Device page's overview lists what is muted, from a different read path: check they agree.
ov = get("/api/device/overview")
listed = [m["label"] for m in (ov.get("muted") or [])]
check_label = channel_state(CHAN)["label"]
check("and the Device overview agrees", (check_label in listed), not was_muted)
post("/api/channels/mute", {"name": CHAN, "on": bool(was_muted)})
time.sleep(SETTLE_S)
check("unmuted again", channel_state(CHAN).get("muted"), was_muted)

# --- the check that catches a stray -------------------------------------------------------------

print("\nSTRAY CHECK: occurrence counts, not names (the check that would have caught btDoScan)")
post_unit, post_counts, post_props = profile_snapshot(POST)
added = sorted(set(post_counts) - set(pre_counts))
lost = sorted(set(pre_counts) - set(post_counts))
moved = sorted(p for p in set(pre_counts) & set(post_counts) if pre_counts[p] != post_counts[p])
check("no property NAME appeared or disappeared", (added, lost), ([], []))
check("no property's OCCURRENCE COUNT changed", moved, [])
# EVERY setting the profile carries, not just the ones the page shows: this is the restore-to-
# baseline check, and a setting that drifted would matter most precisely where nobody can see it.
# Compared against the profile taken at the start, which is the like-for-like reference.
check("every unit setting is back where it started", post_unit, pre_unit)

# Value-level diff, for information: some properties move on their own (the light sensor, timers,
# telemetry), so this is printed rather than asserted.
changed = [p for p in sorted(set(pre_props) & set(post_props)) if pre_props[p] != post_props[p]]
print(f"  info {len(changed)} propert(ies) hold a different value than at the start:")
for p in changed:
    print(f"       {p}: {pre_props[p]} -> {post_props[p]}")

delete_profile(PRE)
delete_profile(POST)
print("  info temporary profiles deleted")

print()
if FAILED:
    print(f"{FAILED} CHECK(S) FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
