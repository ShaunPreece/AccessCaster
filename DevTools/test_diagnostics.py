"""Offline: the diagnostic log's repeat folding against its flusher.

An event that recurs every two seconds, with the flusher ticking between each, must fold into one
first line and a count, not a line apiece.

    python DevTools/test_diagnostics.py
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
import diagnostics
from _testkit import check, check_in, patched

clock = [1000.0]
out = io.StringIO()
state = {"_fh": out, "_active": True, "_start_time": 1000.0, "_size": 0, "_pending": None,
         "MAX_BYTES": 10 ** 9}
with patched((diagnostics, state), (diagnostics.time, {"time": lambda: clock[0]})):
    for _ in range(15):
        diagnostics.event("device.absent", "no control interface to watch on")
        clock[0] += 1.0
        diagnostics.flush()             # the flusher's tick, between two events
        clock[0] += 1.0
    lines = out.getvalue().splitlines()
    check("a two-second repeat is not written once per event",
          sum("device.absent" in line for line in lines), 1)
    clock[0] += 10.0
    diagnostics.flush()
    lines = out.getvalue().splitlines()
    check_in("once it goes quiet its count is written", "x15", lines[-1])
    diagnostics.event("device.absent", "no control interface to watch on")
    diagnostics.flush(force=True)
    check("a single event is never followed by a count",
          out.getvalue().splitlines()[-1].endswith("no control interface to watch on"), True)

sys.exit(_testkit.finish())
