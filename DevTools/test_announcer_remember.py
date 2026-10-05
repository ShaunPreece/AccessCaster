"""Offline checks: "Start with announcements the way I left them".

THE RULES THIS PINS:
  * unticked (the default), the app always starts with announcements on, whatever was left;
  * ticked, a person's toggle is saved and start-up follows it;
  * ticking saves the current state at once, and a take's suspension counts as on;
  * the app switching announcements back on after a take is never saved as a choice;
  * an install whose tray saved "off" before this option existed counts as ticked.

No device and no speech: settings live in a dict and the announcer thread is stubbed.

    python DevTools/test_announcer_remember.py
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import server

FAILS = []
STORE = {}


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILS.append(f"{name}: got {got!r}, wanted {want!r}")
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")


server.load_settings = lambda: dict(STORE)
server.update_settings = lambda **kw: STORE.update(kw)
server._announce_loop = lambda: None


def fresh(saved: dict):
    STORE.clear()
    STORE.update(saved)
    server._ann_remember = server._load_ann_remember()
    server._ann_suspended_for_record = False
    server._announce_on.clear()


print("UNTICKED: always starts on")
fresh({})
check("default is unticked", server._ann_remember, False)
server.action_set_announce(False)
check("a toggle is not saved", "announcer" in STORE, False)
check("start-up is on", server.announcer_starts_on(), True)

print("TICKED: start-up follows the last choice")
fresh({})
server.action_set_announce(True)
server.action_set_announce_remember(True)
check("ticking saves the current state", STORE.get("announcer"), True)
server.action_set_announce(False)
check("a toggle off is saved", STORE.get("announcer"), False)
check("start-up is off", server.announcer_starts_on(), False)
server.action_set_announce_remember(False)
check("unticking makes start-up on again", server.announcer_starts_on(), True)

print("A TAKE IS NOT A CHOICE")
fresh({"announcer_remember": True, "announcer": True})
server._ann_suspended_for_record = True          # a recording has paused announcements
server.action_set_announce_remember(True)
check("a suspended announcer is saved as on", STORE.get("announcer"), True)
fresh({"announcer_remember": True, "announcer": True})
server._ann_suspended_for_record = True
server._restore_announcer_after_record()
check("the app's own switch-back is not saved", STORE.get("announcer"), True)
STORE["announcer"] = False
server._ann_suspended_for_record = True
server._restore_announcer_after_record()
check("nor does it overwrite a saved off", STORE.get("announcer"), False)

print("AN OLD TRAY 'OFF' IS KEPT")
fresh({"announcer": False})
check("counts as ticked", server._ann_remember, True)
check("start-up is off", server.announcer_starts_on(), False)
fresh({"announcer": True})
check("an old 'on' leaves it unticked", server._ann_remember, False)

print()
if FAILS:
    print("FAILURES:")
    for f in FAILS:
        print("  " + f)
    sys.exit(1)
print("All checks passed.")
