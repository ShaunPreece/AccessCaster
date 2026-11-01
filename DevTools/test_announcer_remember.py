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
import _testkit
import announcer
import settings_file
import speech
from _testkit import check

STORE = {}


settings_file.load = lambda: dict(STORE)
settings_file.update = lambda **kw: STORE.update(kw)
announcer._loop = lambda *_: None


def fresh(saved: dict):
    STORE.clear()
    STORE.update(saved)
    announcer.remember = announcer._load_remember()
    announcer.suspended_for_record = False
    speech.enabled.clear()


print("UNTICKED: always starts on")
fresh({})
check("default is unticked", announcer.remember, False)
announcer.action_set_announce(False)
check("a toggle is not saved", "announcer" in STORE, False)
check("start-up is on", announcer.starts_on(), True)

print("TICKED: start-up follows the last choice")
fresh({})
announcer.action_set_announce(True)
announcer.action_set_announce_remember(True)
check("ticking saves the current state", STORE.get("announcer"), True)
announcer.action_set_announce(False)
check("a toggle off is saved", STORE.get("announcer"), False)
check("start-up is off", announcer.starts_on(), False)
announcer.action_set_announce_remember(False)
check("unticking makes start-up on again", announcer.starts_on(), True)

print("A TAKE IS NOT A CHOICE")
fresh({"announcer_remember": True, "announcer": True})
announcer.suspended_for_record = True          # a recording has paused announcements
announcer.action_set_announce_remember(True)
check("a suspended announcer is saved as on", STORE.get("announcer"), True)
fresh({"announcer_remember": True, "announcer": True})
announcer.suspended_for_record = True
announcer.restore_after_record()
check("the app's own switch-back is not saved", STORE.get("announcer"), True)
STORE["announcer"] = False
announcer.suspended_for_record = True
announcer.restore_after_record()
check("nor does it overwrite a saved off", STORE.get("announcer"), False)

print("AN OLD TRAY 'OFF' IS KEPT")
fresh({"announcer": False})
check("counts as ticked", announcer.remember, True)
check("start-up is off", announcer.starts_on(), False)
fresh({"announcer": True})
check("an old 'on' leaves it unticked", announcer.remember, False)

print()
sys.exit(_testkit.finish())
