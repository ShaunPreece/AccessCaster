"""LIVE hardware check of two features: the UNIT SETTINGS and MUTE.

WHY THIS EXISTS. Both are ordinary global property writes on selectors that were captured and
read-back verified, so the risk is not the protocol: it is the PAIRING. A record whose
selector-plus-property pairing is wrong does not simply miss, it can land on some other object and
append a stray occurrence that only a factory reset clears (as btDoScan does on a predicted
NETWORK selector). So every write here is read back, and the run ends with the check that actually
catches a stray: OCCURRENCE COUNTS per property, diffed against a pre-test snapshot. Counting
distinct NAMES is not enough - the stray copy carries a name that already exists, so a scan of
names looks clean while a stray has just been appended.

HOW IT DRIVES. The app's own service, in this process: no GUI and no RODE app. Quit AccessCaster
first, which holds the desk.

WHAT IT DELIBERATELY DOES NOT TOUCH: `outputMonFixed` (Fixed Output Level). Everything else here is
visual or behavioural, but that one changes what the MONITOR OUTPUT sends, and a fixed full-level
output into speakers set low is not something to try unasked. It is READ and reported, and left
alone.

EVERY WRITE IS PUT BACK HOWEVER THE RUN ENDS, and the two temporary profiles it saves into the
test data dir are deleted however it ends too.

Run:  python DevTools/test_unit_settings_live.py
"""

import json
import os
import shutil
import sys
import time

import _bootstrap  # noqa: F401  (the test data dir, like every other test)
import _desk
import _testkit
import profiles
from _desk import count_changes, wait_for_job
from _testkit import check

CHAN = "Mic2"                    # nothing is connected to Mic 2 on this desk
PRE, POST = "PRE_UNIT_TEST", "POST_UNIT_TEST"       # temporary profiles, deleted at the end
server = None
SETTLE_S = 0.8

# prop -> {current value: the value to write instead}. A write that happens to match the current
# value proves nothing, so every entry is a real change from the value it is keyed on.
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


def unit_values():
    d = server.read_unit_settings()
    if not d.get("ok"):
        raise SystemExit("could not read the desk settings: " + str(d.get("message")))
    return {s["prop"]: s["value"] for s in d["settings"]}, d


def _numeric_target(spec, current):
    """A different value inside the slider's range, well clear of both ends."""
    lo, hi = int(spec["min"]), int(spec["max"])
    mid = lo + (hi - lo) // 3
    return mid if current != mid else mid + 10


def profile_snapshot(name):
    """Save a profile and return (its unit block, {prop: occurrence count}, {prop: values}).

    WAITS FOR THE JOB. `snapshot_profile` STARTS a background save and answers immediately, so
    the file may not exist yet when the reply arrives. The save's own status says when it does, and
    it is what the window polls too.
    """
    res = server.snapshot_profile(name, include_pads=False)
    if not res.get("ok"):
        raise SystemExit(f"could not save the {name} profile: {res.get('message')}")
    st = wait_for_job(server.profile_save_status, 180.0, 0.5)
    if not st.get("ok"):
        raise SystemExit(f"the {name} profile did not save: {st.get('message')}")
    path = os.path.join(profiles.PROFILES_DIR, name, "profile.json")
    with open(path, encoding="utf-8") as f:
        prof = json.load(f)
    props = prof["device_snapshot"]["properties"]
    return prof.get("unit") or {}, {p: len(v) for p, v in props.items()}, props


def delete_profile(name):
    shutil.rmtree(os.path.join(profiles.PROFILES_DIR, name), ignore_errors=True)


def set_unit(prop, value):
    return server.action_set_unit_setting(prop, int(value))


def channel_state(name):
    d = server.action_get_channels()
    for c in d.get("channels") or []:
        if c["name"] == name:
            return c
    raise SystemExit(f"{name} not found: {d.get('message') or 'not in the channel list'}")


def set_mute(name, on):
    return server.action_set_channel_mute(name, on)


def main() -> int:
    global server
    server = _desk.service()
    try:
        run()
    finally:
        delete_profile(PRE)
        delete_profile(POST)
        print("  info temporary profiles deleted")
    print()
    return _testkit.finish()


def run() -> None:
    print("BASELINE (a full profile, which is also the stray check's reference)")
    pre_unit, pre_counts, pre_props = profile_snapshot(PRE)
    start, payload = unit_values()
    # COMPARED OVER THE PAGE'S OWN KEYS, not as whole dicts. A profile records everything WRITABLE
    # and the Device page shows a deliberate subset of it (server.UNIT_SETTINGS_NOT_ON_PAGE says
    # which is left off and why), so the two are not meant to be equal. What matters is that where
    # they overlap they AGREE: two read paths, one desk. Membership itself is guarded by
    # DevTools/test_specs.py.
    overlap = {k: v for k, v in pre_unit.items() if k in start}
    check("the profile and the Device page agree on every setting the page shows", overlap, start)
    extra = sorted(set(pre_unit) - set(start))
    print(f"  info the profile also carries {len(extra)} setting(s) the page does not show: {extra}")
    print(f"  info the desk right now: {json.dumps(start)}")
    for prop, why in SKIP.items():
        print(f"  info NOT written: {prop} = {start.get(prop)!r} ({why})")

    specs = {s["prop"]: s for s in payload["settings"]}

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
        try:
            res = set_unit(prop, target)
            time.sleep(SETTLE_S)
            got = unit_values()[0][prop]
            check(f"{prop} {current!r} -> {target!r} reads back", got, target)
            if not res.get("ok"):
                check(f"{prop} write reported ok", res.get("message"), "ok")
        finally:
            set_unit(prop, current)
            time.sleep(SETTLE_S)
        check(f"{prop} restored", unit_values()[0][prop], current)

    print("\nA VALUE OUTSIDE THE SPEC IS REFUSED, NOT WRITTEN")
    res = set_unit("systemHapticSetting", 99)
    check("a choice value that is not on offer is refused", res.get("ok"), False)
    res = set_unit("definitelyNotASetting", 1)
    check("an unknown property is refused", res.get("ok"), False)

    print(f"\nMUTE {CHAN} FROM SOFTWARE, READ BACK, PUT BACK")
    was_muted = channel_state(CHAN).get("muted")
    print(f"  info {CHAN} starts {'MUTED' if was_muted else 'unmuted'}")
    try:
        res = set_mute(CHAN, not was_muted)
        check("the write reports ok", res.get("ok"), True)
        print(f"  info message: {res.get('message')}")
        time.sleep(SETTLE_S)
        check("the desk reports the new mute state", channel_state(CHAN).get("muted"),
              not was_muted)
        # The Device page's overview lists what is muted, from a different read path.
        ov = server.device_overview()
        listed = [m["label"] for m in (ov.get("muted") or [])]
        check("and the Device overview agrees", channel_state(CHAN)["label"] in listed,
              not was_muted)
    finally:
        set_mute(CHAN, bool(was_muted))
        time.sleep(SETTLE_S)
    check("mute back where it started", channel_state(CHAN).get("muted"), was_muted)

    print("\nSTRAY CHECK: occurrence counts, not names (the check that catches a stray like "
          "btDoScan's)")
    post_unit, post_counts, post_props = profile_snapshot(POST)
    added, lost, moved = count_changes(pre_counts, post_counts)
    check("no property NAME appeared or disappeared", (added, lost), ([], []))
    check("no property's OCCURRENCE COUNT changed", moved, [])
    # EVERY setting the profile carries, not just the ones the page shows: a setting that drifted
    # matters most where nobody can see it. Compared against the profile taken at the start, which
    # is the like-for-like reference.
    check("every desk setting is back where it started", post_unit, pre_unit)

    # Value-level diff, for information: some properties move on their own (the light sensor,
    # timers, telemetry), so this is printed rather than asserted.
    changed = [p for p in sorted(set(pre_props) & set(post_props)) if pre_props[p] != post_props[p]]
    print(f"  info {len(changed)} propert(ies) hold a different value than at the start:")
    for p in changed:
        print(f"       {p}: {pre_props[p]} -> {post_props[p]}")


if __name__ == "__main__":
    sys.exit(main())
