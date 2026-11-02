"""Offline checks on the DevTools helpers that decide what a live script believes about the desk.

THE RULES THIS PINS:
  * a failed PowerShell query is an error, never a count of zero RODE devices, so a script cannot
    read it as the desk dropping off the bus;
  * a mic's chain counts as settled only once two fresh reads agree, so a voicing still applying
    is not taken for the finished chain;
  * the live schemes test applies its saved scheme even when a read part way through fails;
  * a chain that never settles, and a job that never finishes, raise rather than pass;
  * a job has finished only when its status reads `running` false;
  * the stray check counts occurrences, not names;
  * the live probes put back what they wrote even when a read after the write fails;
  * `property_list.py --check` with no list to compare against says so and writes nothing.

No device: the PowerShell call, the chain reads and the output path are stubbed.

    python DevTools/test_devtools_helpers.py
"""
from __future__ import annotations

import contextlib
import itertools
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import _bootstrap
import _desk
import _testkit
import capture_analyze
import codemap_lines
import devices
import fake_model
import probe_bind_hold
import probe_dead_props_write
import property_coverage
import property_list
import rcp_transfer
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
gains = itertools.count()
clock = itertools.count(0, 5)
with patched((_desk, {"mic_chain": lambda ch: {"eqLowGain": next(gains)}}),
             (_desk.time, {"sleep": lambda s: None, "monotonic": lambda: next(clock)})):
    try:
        _desk.settled_chain(3, timeout=8)
        raised = None
    except TimeoutError as e:
        raised = str(e)
check_true("a chain that never settles raises rather than passing its last read", raised)

print("\nA JOB HAS FINISHED ONLY WHEN `running` READS FALSE")
statuses = iter([{"running": True, "done": True},
                 {"ok": False, "message": "the request failed"},
                 {"running": False, "done": True, "ok": True, "message": "Done."}])
seen = []
with patched((_desk.time, {"sleep": lambda s: None})):
    final = _desk.wait_for_job(lambda: next(statuses), on_status=seen.append)
check("a stale `done` and a failed read are both waited past", (len(seen), final["message"]),
      (3, "Done."))
clock = itertools.count(0, 100)
with patched((_desk.time, {"sleep": lambda s: None, "monotonic": lambda: next(clock)})):
    try:
        _desk.wait_for_job(lambda: {"running": True}, timeout=150)
        raised = None
    except TimeoutError as e:
        raised = str(e)
check_true("a job still running at the timeout raises", raised)

print("\nTHE STRAY CHECK COUNTS OCCURRENCES")
before = _desk.stray_counts(fake_model.encode([("SYSTEM", "systemName", "x"),
                                               ("SYSTEM", "btVisible", True)]))
after = _desk.stray_counts(fake_model.encode([("SYSTEM", "systemName", "x"),
                                              ("SYSTEM", "systemName", "y"),
                                              ("NETWORK", "wifiSSID", "z")]))
check("an added name, a vanished one and a moved count", _desk.count_changes(before, after),
      (["wifiSSID"], ["btVisible"], ["systemName"]))

print("\nTHE BIND PROBE'S VERDICT")
check_true("a trial whose holder held nothing says so rather than raising",
           "nothing was held" in probe_bind_hold.verdict(5.0, None, None))
check("a bind after the release", probe_bind_hold.verdict(12.0, 1.0, 11.0),
      "bound 1.00s AFTER release")

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
        (test_schemes_live, {"read_matrix": lambda: next(reads), "stray_counts": lambda blob: {},
                             "on_faders": lambda blob: set()}),
        (test_schemes_live.routing_schemes, {"apply": lambda scheme: applied.append(scheme) or {
            "ok": True, "outputs": list(scheme["outputs"]), "skipped": []}}),
        (test_schemes_live.time, {"sleep": lambda s: None})):
    test_schemes_live.main()
_testkit.FAILURES[:] = own_failures
check_true("the saved scheme is applied after the failed read", applied and applied[-1] is baseline)

print("\nTHE DEAD-PROPERTY PROBE PUTS A WRITE BACK WHEN THE DUMP AFTER IT FAILS")
writes = []
snaps = iter([("root", {})])
node = SimpleNamespace(name="SYSTEM")


def _snap_then_fail():
    try:
        return next(snaps)
    except StopIteration:
        raise TimeoutError("the desk stopped answering") from None


with patched((probe_dead_props_write, {"snapshot": _snap_then_fail,
                                       "write": lambda node, prop, kind, v: writes.append(v)}),
             (probe_dead_props_write.mt, {"find_property": lambda root, prop: [(node, False, "bool")]})):
    with contextlib.suppress(TimeoutError):
        probe_dead_props_write.probe("shareAnom", "")
check("the test value, then the resting value", writes, [True, False])

print("\nTHE DEAD-PROPERTY PROBE NEVER WRITES A PADRECORDER PROPERTY")
writes = []
node = SimpleNamespace(name="PADRECORDER")
with patched((probe_dead_props_write, {"snapshot": lambda: ("root", {}),
                                       "write": lambda node, prop, kind, v: writes.append(v)}),
             (probe_dead_props_write.mt, {"find_property": lambda root, prop: [(node, False, "bool")]})):
    verdict = probe_dead_props_write.probe("padRecorderArmed", "")
check("skipped, and nothing written", (verdict, writes), ("skipped (never probed)", []))

print("\nA CAPTURE'S PROPERTY WRITES ARE FOUND AT ANY ADDRESS DEPTH")
sent = [capture_analyze.UsbPacket(n, False, 0x05, 5, 1, rec) for n, rec in enumerate((
    rcp_transfer.build_channel_prop_int("inputMicrophoneGain", 40, 0x6E),
    rcp_transfer.build_path_prop_int("padColourIndex", 3, [0x29A, 5])))]
check("a mic's two-byte channel address and a pad's two-level path",
      list(capture_analyze.host_writes(sent).values()),
      [((0x26E,), "inputMicrophoneGain", 40), ((0x29A, 5), "padColourIndex", 3)])

print("\nA PROPERTY USED THROUGH A NAME CONSTANT COUNTS AS USED")
found = property_coverage.scan(property_coverage.APP_GLOBS, ["channelWirelessMute"])
check("channelWirelessMute, written and read through WIRELESS_MUTE_PROP",
      property_coverage.coverage_of(found.get("channelWirelessMute")), "read+write")

print("\nCODEMAP'S DEVTOOLS COUNT IS WHAT GIT TRACKS")
tracked = codemap_lines.tracked_devtools_files()
ignored = Path(_bootstrap.devtools_path("_probe_stub_count_check.json"))
try:
    ignored.write_text("{}", encoding="utf-8")
    check("an ignored file on disk does not change the count",
          codemap_lines.tracked_devtools_files(), tracked)
finally:
    ignored.unlink(missing_ok=True)

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
