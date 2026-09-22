"""End-to-end test of the AS-FOUND BASELINE, against a TEMPORARY profile store.

Reads the real desk (one model dump, read-only, safe alongside the running app) and runs the whole
first-run flow: offer, capture, mark, protect, add pads afterwards. Nothing is written to the unit,
and PROFILES_DIR and the settings file are both redirected, so the real profile store and the real
preferences are untouched - the baseline this creates lives in a temp folder and is deleted at the
end.

What it is really pinning down is the two protections, because they are the whole point: the one
profile that cannot be recreated must not be lost to a name collision or to the same click that
deletes any other profile.

Run: python DevTools/test_baseline.py
"""
from __future__ import annotations
import os
import shutil
import sys
import tempfile
import time

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import profiles
import server

FAILED = 0


def check(label, got, want):
    global FAILED
    if got == want:
        print(f"  ok   {label}: {got!r}")
    else:
        FAILED += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def check_true(label, got, why=""):
    check(label + (f" ({why})" if why else ""), bool(got), True)


def wait_for_job(timeout=180):
    """Block until the background save/attach job reports done, as the page's poll would."""
    end = time.time() + timeout
    while time.time() < end:
        st = server.profile_save_status()
        if st.get("done"):
            return st
        time.sleep(0.3)
    return {"done": False, "ok": False, "message": "timed out"}


TMP = tempfile.mkdtemp(prefix="accesscaster-baseline-test-")
profiles.PROFILES_DIR = os.path.join(TMP, "profiles")
os.makedirs(profiles.PROFILES_DIR, exist_ok=True)
SETTINGS: dict = {}
server.load_settings = lambda: dict(SETTINGS)
server.update_settings = lambda **kw: SETTINGS.update(kw)
print(f"temporary profile store: {profiles.PROFILES_DIR}\n")

try:
    print("THE OFFER: ONCE PER DESK, AND ONLY WHEN THERE IS A DESK")
    server.read_device_status()          # publishes the hardware id, as the page's status read does
    hw = server._device_hardware_id()
    check_true("a desk is connected and identified", hw, hw)
    s = server.baseline_status()
    check("offered on a store with no baseline", s["offer"], True)
    check("and it suggests a name that says what, when and which desk",
          s["suggested_name"], f"As found {time.strftime('%Y-%m-%d')} {s['device_name']}")
    check_true("which is a legal profile name", profiles.safe_name(s["suggested_name"]))

    print("\nDECLINING IS REMEMBERED, SO IT IS ASKED ONCE AND NEVER NAGS")
    check_true("skip reports ok", server.action_baseline_skip().get("ok"))
    check("so the offer is gone", server.baseline_status()["offer"], False)
    check("but the Profiles page can still capture one", server.baseline_status()["baseline"], None)

    print("\nCAPTURE (settings only, no pads: this test writes nothing to the unit)")
    started = server.action_baseline_save(include_pads=False)
    check_true("the capture starts", started.get("ok"), started.get("message"))
    job = wait_for_job()
    check_true("and finishes ok", job.get("ok"), job.get("message"))
    name = s["suggested_name"]
    check("the profile is on disk under the auto-name", profiles.list_profiles()["profiles"], [name])

    print("\nIT IS MARKED, AND THE PAGE CAN FIND IT")
    got = profiles.load(name)
    check_true("marked as the baseline", profiles.is_baseline(got["prof"]))
    found = profiles.find_baseline()
    check("found by name", found["name"], name)
    check("with no pads yet", found["pads"], 0)
    check("and the desk it came from is stamped on it", found["device_name"], s["device_name"])
    after = server.baseline_status()
    check("so it is not offered again", after["offer"], False)
    check("and the page is told what exists", after["baseline"]["name"], name)
    check_true("a real settings snapshot came back with it",
               (got["prof"].get(profiles.SNAPSHOT_KEY) or {}).get("property_count"),
               f"{(got['prof'].get(profiles.SNAPSHOT_KEY) or {}).get('property_count')} properties")

    print("\nPROTECTION 1: AN ORDINARY SAVE CANNOT REPLACE IT")
    clash = profiles.save(name, b"", [], None, 9)
    check("a same-name save is refused", clash["ok"], False)
    check_true("and says why", "as-found baseline" in clash.get("message", ""))
    check_true("the baseline is still marked afterwards",
               profiles.is_baseline(profiles.load(name)["prof"]))
    check_true("and still has its full snapshot",
               (profiles.load(name)["prof"].get(profiles.SNAPSHOT_KEY) or {}).get("property_count"))

    print("\nPROTECTION 2: DELETING IT TAKES A DELIBERATE SECOND STEP")
    first = server.delete_profile(name)
    check("the first attempt is refused", first["ok"], False)
    check("and flags itself as the baseline case, so the page can confirm", first.get("baseline"), True)
    check("nothing was deleted", profiles.list_profiles()["profiles"], [name])
    # The protection has to be specific to the baseline, or it is just an annoying extra click on
    # every profile. An ordinary one still goes on the first press.
    profiles.save("Ordinary", b"", [], None, 9)
    check_true("an ordinary profile still deletes on the first press, no force needed",
               server.delete_profile("Ordinary").get("ok"))

    print("\nADDING PADS LATER KEEPS THE AS-FOUND SETTINGS")
    # attach_pads is the "add them later" path. Called with a stub list, because copying real pad
    # audio off the unit is a many-minute transfer-mode job and what is under test here is that the
    # SETTINGS half is left exactly as captured rather than re-read from the desk as it is today.
    before_snapshot = profiles.load(name)["prof"][profiles.SNAPSHOT_KEY]["property_count"]
    res = profiles.attach_pads(name, [{"grid": 1, "name": "stub"}])
    check_true("pads attach", res.get("ok"), res.get("message"))
    prof = profiles.load(name)["prof"]
    check("the pads are recorded", len(prof["pads"]), 1)
    check("the settings snapshot is untouched",
          prof[profiles.SNAPSHOT_KEY]["property_count"], before_snapshot)
    check_true("and it is still the baseline", profiles.is_baseline(prof))
    check("the page now sees pads in it", profiles.find_baseline()["pads"], 1)
    check("so it stops offering to add them",
          server.action_baseline_add_pads().get("ok"), False)

    print("\nFORCED DELETE IS THE WAY OUT")
    forced = server.delete_profile(name, force=True)
    check_true("it deletes with force", forced.get("ok"), forced.get("message"))
    check("the store is empty again", profiles.list_profiles()["profiles"], [])
    check("and with the record gone, a new capture is offered once more",
          server.baseline_status()["baseline"], None)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("\nALL CHECKS PASSED" if not FAILED else f"\n{FAILED} CHECK(S) FAILED")
sys.exit(1 if FAILED else 0)
