"""Offline: Windows' endpoint reader. No device, no microphone.

An input endpoint delivers no peak until a capture stream is open on it, so without
`win.meters._EngineWaker` an input's meter reads 0.0 however loud the microphone is. Over Remote
Desktop the desk's endpoints do not exist at all, so a meter exercised only there cannot show the
fault; the wiring is pinned here instead, with the reader's other failures.

Usage: python DevTools/test_win_meters.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import _bootstrap  # noqa: F401
import _testkit
from _testkit import check, patched
from win import core_audio, meters

src = Path(meters.__file__).read_text(encoding="utf-8")

print("THE WAKER IS WIRED UP")
check("the reader constructs it, rather than only declaring the class",
      "_EngineWaker(dev)" in src, True)
check("it is only started for INPUTS", "if self.is_capture:" in src, True)
check("shared mode, never exclusive (exclusive would steal the mic from every other app)",
      "AUDCLNT_SHAREMODE_SHARED, 0," in src and meters.AUDCLNT_SHAREMODE_SHARED == 0, True)
check("a level write returns what READS BACK (a driver can refuse silently)",
      "GetMasterVolumeLevel()" in src.split("def write_level")[1], True)

print("\nA REMOVED ENDPOINT RAISES RATHER THAN READING SILENT")


class _Removed:
    """An endpoint Windows has taken away: its meter still answers 0, its state is not active."""
    def GetPeakValue(self):
        return 0.0

    def GetState(self):
        return 0x4          # DEVICE_STATE_NOTPRESENT


r = object.__new__(meters.EndpointReader)
r.name, r._dev, r._meter = "Desk", _Removed(), _Removed()
r._checked = time.monotonic()
check("a removed endpoint reads 0 until the next state check", r.peak(), 0.0)
r._checked -= meters.PRESENT_CHECK_S
try:
    r.peak()
    raised = False
except OSError:
    raised = True
check("then raises, so a Watcher on it ends and can be reopened", raised, True)

print("\nAN ENDPOINT READ THAT FAILS IS NOT TAKEN FOR AN ANSWER")


class _BrokenEnumerator:
    def EnumAudioEndpoints(self, *_a):
        raise OSError("enumerator gone")


try:
    meters._is_capture_endpoint(_BrokenEnumerator(), "endpoint")
    raised = False
except OSError:
    raised = True
check("an unreadable capture list raises, rather than calling an input an output", raised, True)

events = []


def _no_enumerator():
    raise OSError("class not registered")


with patched((meters.diagnostics, {"event": lambda kind, detail="", **f: events.append(kind)}),
             (core_audio, {"enumerator": _no_enumerator})):
    listed = meters.list_endpoints()
check("an enumerator that cannot be created lists nothing", listed, [])
check("and says why in the log", events, ["meters.enumerate_failed"])

r = object.__new__(meters.EndpointReader)
r._vol = None
check("an endpoint with no volume interface has no level", r.level(), None)

sys.exit(_testkit.finish())
