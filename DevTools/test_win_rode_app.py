"""Windows' process lookup for RODE's app (Project/win/rode_app.py), offline.

    python DevTools/test_win_rode_app.py
"""
from __future__ import annotations

import os
import sys

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
from _testkit import check, check_true
from win import rode_app

print("THE PROCESS CHECK")
check_true("this test's own interpreter is found by name",
           rode_app.running(os.path.basename(sys.executable)))
check("a name nothing runs under is not", rode_app.running("no such program.exe"), False)

sys.exit(_testkit.finish())
