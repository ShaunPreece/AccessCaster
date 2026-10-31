"""Offline checks on the DevTools helpers that decide what a live script believes about the desk.

THE RULES THIS PINS:
  * a failed PowerShell query is an error, never a count of zero RODE devices, so a script cannot
    read it as the desk dropping off the bus;
  * a mic's chain counts as settled only once two fresh reads agree, so a voicing still applying
    is not taken for the finished chain;
  * the live schemes test applies its saved scheme even when a read part way through fails;
  * `property_list.py --check` with no list to compare against says so and writes nothing.

No device: the PowerShell call, the chain reads and the output path are stubbed.

    python DevTools/test_devtools_helpers.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _desk
import _testkit
import devices
import fake_model
import property_list
import test_schemes_live
from _testkit import as_device, check, check_true, patched

print("A FAILED DEVICE COUNT RAISES")
empty = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="no PnP module")
with patched((subprocess, {"run": lambda *a, **k: empty})):
    try:
        _desk.rode_devices_present()
        raised = None
    except RuntimeError as e:
        raised = str(e)
check_true("an unreadable answer raises rather than counting zero", raised, "it returned a count")
counted = subprocess.CompletedProcess(args=[], returncode=0, stdout="3\n", stderr="")
with patched((subprocess, {"run": lambda *a, **k: counted})):
    check("a readable answer is the count", _desk.rode_devices_present(), 3)

print("\nA CHAIN IS SETTLED ONLY ONCE TWO FRESH READS AGREE")
reads = iter([{"eqLowGain": 0.1}, {"eqLowGain": 0.2}, {"eqLowGain": 0.3}, {"eqLowGain": 0.3}])
with patched((_desk, {"mic_chain": lambda ch: next(reads)}), (_desk.time, {"sleep": lambda s: None})):
    check("the steady chain is returned, not the first read", _desk.settled_chain(3),
          {"eqLowGain": 0.3})

print("\nTHE SCHEMES TEST PUTS THE MIXES BACK WHEN A READ FAILS PART WAY")
baseline = {"outputs": {"Chat": {"mode": 1, "sources": {}}}}
reads = iter([({"ok": True, "scheme": baseline}, b""),
              ({"ok": False, "message": "the desk stopped answering"}, b""),
              ({"ok": True, "scheme": baseline}, b"")])
applied = []
print("  (the suite's own output follows, and its failed read is the point)")
own_failures = list(_testkit.FAILURES)     # the suite's own checks fail on purpose here
with as_device(devices.PRO2), patched(
        (_testkit, {"finish": lambda: 0}),
        (test_schemes_live, {"read_matrix": lambda: next(reads), "counts": lambda blob: {},
                             "on_faders": lambda blob: set()}),
        (test_schemes_live.routing_schemes, {"apply": lambda scheme: applied.append(scheme) or {
            "ok": True, "outputs": list(scheme["outputs"]), "skipped": []}}),
        (test_schemes_live.time, {"sleep": lambda s: None})):
    test_schemes_live.main()
_testkit.FAILURES[:] = own_failures
check_true("the saved scheme is applied after the failed read", applied and applied[-1] is baseline)

print("\nproperty_list --check WITH NOTHING TO COMPARE AGAINST")
with tempfile.TemporaryDirectory(prefix="ac_property_list_") as tmp:
    blob_file = Path(tmp) / "dump.bin"
    blob_file.write_bytes(fake_model.encode([("SYSTEM", "systemName", "RODECaster Pro II")]))
    missing = Path(tmp) / "PROPERTY_LIST_PRO2.md"
    with patched((property_list, {"out_path": lambda dev: missing, "dead_properties": set}),
                 (sys, {"argv": ["property_list.py", "--check", "--blob", str(blob_file)]})):
        status = property_list.main()
    check("it fails", status, 1)
    check("and writes nothing", missing.exists(), False)

sys.exit(_testkit.finish())
