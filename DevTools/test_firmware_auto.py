"""Offline checks on the per-desk as-found backup and the automatic firmware check.

THE RULES THIS PINS:
  * an as-found backup belongs to the desk whose USB serial it carries; one saved without a serial
    belongs to any desk of its model, so a desk of another model is still offered its own;
  * a desk whose as-found question is open, or that is not identified, is never checked;
  * a due check is stamped per desk, and a check skipped for a busy desk is not;
  * a version is notified once per desk, and a new version is notified again;
  * "only when I ask" stops the automatic check;
  * the check itself writes nothing while a job runs, while the desk is recording, or once the
    desk read is not the desk that was cleared.

No device: every desk call is stubbed.

    python DevTools/test_firmware_auto.py
"""
from __future__ import annotations

import sys
import types

import _bootstrap  # noqa: F401
import _testkit
import app_updates
import baseline
import desk_access
import desk_live
import devices
import firmware
import firmware_auto
import profiles
import rcp_read
import settings_file
from _testkit import check, check_true, patched
from selfupdate import interval

PRO2, DUO = 0x0037, 0x0040

print("WHOSE AS-FOUND BACKUP IS IT")


def prof(serial=None, pid=PRO2, base=True):
    out = {"baseline": base, "product_id": pid}
    if serial is not None:
        out[profiles.HARDWARE_ID_KEY] = serial
    return out


check("the desk whose serial it carries", profiles._baseline_of(prof("A"), "A", PRO2), True)
check("not another unit of the same model", profiles._baseline_of(prof("A"), "B", PRO2), False)
check("not an unidentified desk", profiles._baseline_of(prof("A"), "", PRO2), False)
check("one saved without a serial: any desk of its model",
      profiles._baseline_of(prof(), "B", PRO2), True)
check("and an unstamped model is a Pro II's", profiles._baseline_of(prof(pid=None), "B", PRO2),
      True)
check("but not a desk of another model", profiles._baseline_of(prof(), "D", DUO), False)
check("nor is an unstamped one a Duo's", profiles._baseline_of(prof(pid=None), "D", DUO), False)
check("an ordinary profile is nobody's baseline",
      profiles._baseline_of(prof("A", base=False), "A", PRO2), False)

SETTINGS: dict = {}
store = (settings_file, {"load": lambda: dict(SETTINGS), "update": SETTINGS.update})

print("\nIS THE DESK'S QUESTION SETTLED")
found = {}
with patched(store, (profiles, {"find_baseline": lambda hw, pid: found.get(hw, {})})):
    check("an unidentified desk is never offered", baseline.offer_pending("", PRO2), False)
    check("nor ever settled", baseline.settled("", PRO2), False)
    check("a new desk is offered", baseline.offer_pending("A", PRO2), True)
    check("so it is not settled", baseline.settled("A", PRO2), False)
    SETTINGS["baseline_asked"] = ["A"]
    check("once asked, it is settled", baseline.settled("A", PRO2), True)
    check("and another desk is still offered its own", baseline.offer_pending("B", PRO2), True)
    found["B"] = {"name": "As found"}
    check("a desk with a backup is settled", baseline.settled("B", PRO2), True)
    SETTINGS.clear()

print("\nTHE AUTOMATIC CHECK")
desk = types.SimpleNamespace(product_id=PRO2, name="RODECaster Pro II")
state = {"settled": False, "days": interval.DEFAULT, "now": 1_000_000.0, "reply": {}}
asked, told = [], []


def fake_check(serial):
    asked.append(serial)
    return state["reply"]


def offered(version):
    return {"ok": True, "message": "", "firmware_update": {"available": True, "version": version}}


with patched(store,
             (desk_live, {"serial": lambda: "A"}),
             (devices, {"get_active_device": lambda: desk}),
             (baseline, {"settled": lambda hw, pid: state["settled"]}),
             (app_updates, {"check_days": lambda: state["days"]}),
             (firmware, {"check_unasked": fake_check}),
             (firmware_auto, {"notify": told.append}),
             (firmware_auto.time, {"time": lambda: state["now"]})):
    check("an open as-found question: nothing is asked", firmware_auto.check_if_due(), None)
    check("and the desk is not touched", asked, [])

    state["settled"] = True
    state["reply"] = {"ok": False, "skipped": "the desk is recording"}
    firmware_auto.check_if_due()
    check("a busy desk is asked", asked, ["A"])
    check("and the skip is not stamped, so the next look tries again",
          SETTINGS.get(firmware_auto._LAST_KEY), None)

    state["reply"] = offered("1.8.0")
    firmware_auto.check_if_due()
    check("a settled, due desk is checked and stamped",
          SETTINGS[firmware_auto._LAST_KEY], {"A": state["now"]})
    check("the version found is notified", len(told), 1)
    check_true("naming it and where to install it",
               "1.8.0" in told[0] and "Device page" in told[0], told[0])

    asked.clear()
    state["now"] += 3600
    check("an hour later it is not due", firmware_auto.check_if_due(), None)
    check("so nothing is asked", asked, [])

    state["now"] += 24 * 3600
    firmware_auto.check_if_due()
    check("a day later the same version is not notified again", len(told), 1)

    state["now"] += 24 * 3600
    state["reply"] = offered("1.8.1")
    firmware_auto.check_if_due()
    check("a new version is", len(told), 2)

    asked.clear()
    state["now"] += 30 * 24 * 3600
    state["days"] = interval.NEVER
    check("only when I ask: no automatic check", firmware_auto.check_if_due(), None)
    check("nothing asked", asked, [])

print("\nWHAT THE CHECK ITSELF REFUSES")
rode = []
use = {"busy": None, "serial": "A", "recording": False}
with patched((desk_access, {"work_in_progress": lambda: use["busy"]}),
             (rcp_read, {"read_model_stable": lambda fresh=False: b"blob"}),
             (firmware, {"_live_use": lambda blob: (use["recording"], False),
                         "_ask_rode": lambda blob=None: rode.append(blob) or {"ok": True}}),
             (desk_live, {"serial": lambda: use["serial"]})):
    use["busy"] = "A restore is running."
    check("a running job skips it", firmware.check_unasked("A").get("skipped"),
          "A restore is running.")
    use["busy"] = None
    use["serial"] = "B"
    check_true("another desk skips it", firmware.check_unasked("A").get("skipped"))
    use["serial"] = "A"
    use["recording"] = True
    check("recording skips it", firmware.check_unasked("A").get("skipped"),
          "the desk is recording")
    check("none of those asked RODE", rode, [])
    use["recording"] = False
    firmware.check_unasked("A")
    check("a clear desk asks, reusing the read", rode, [b"blob"])

sys.exit(_testkit.finish())
