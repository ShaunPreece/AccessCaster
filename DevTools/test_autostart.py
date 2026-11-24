"""Offline test of "start when you sign in". NO writes: a source run refuses before it makes one.

The `--startup` flag is pinned here because it is the whole reason a login launch lands in the
tray instead of opening the window.

Run: python DevTools/test_autostart.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401  (puts Project/ on the path; import FIRST)
import _testkit
import autostart
from _testkit import check

print("THE FLAG IS WHAT MAKES A LOGIN LAUNCH DIFFERENT")
check("a login launch is recognised", autostart.started_at_login(["--startup"]), True)
check("a launch by hand is not", autostart.started_at_login([]), False)
check("and an unrelated argument is not either", autostart.started_at_login(["--debug"]), False)

print("\nA SOURCE RUN REFUSES RATHER THAN WRITING SOMETHING THAT WOULD BREAK")
# This suite never runs frozen, so app_exe() is the refusing branch by construction.
check("app_exe() is None from source", autostart.app_exe(), None)
res = autostart.set_enabled(True)
check("turning it on is refused", res["ok"], False)
check("and the refusal says WHY, naming source", "from source" in res["message"], True)
st = autostart.state()
check("state() reports it cannot be changed here", st["supported"], False)
check("but still says whether an entry exists", isinstance(st["enabled"], bool), True)

sys.exit(_testkit.finish())
