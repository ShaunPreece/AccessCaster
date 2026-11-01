"""One-command capture probe: start a USBPcap capture, drive one control in the RODE app, stop,
and decode the property WRITES out of the capture - i.e. the write SELECTOR we are hunting.

  python DevTools/cap_probe.py <name> tap <screen.target>      # click a mapped ui_map coord
  python DevTools/cap_probe.py <name> click <x> <y>            # click a raw window coord
  python DevTools/cap_probe.py <name> drag <x1> <y1> <x2> <y2> # drag (sliders)
  python DevTools/cap_probe.py <name> none                     # capture a window with no action
  python DevTools/cap_probe.py --decode <name>                 # re-decode an existing capture

WHY THIS EXISTS. A diff of model dumps around a control names WHICH property it moves, but never
the selector, which exists only in the bytes the app sends. Watching the wire while the app writes
is the one way to learn it. Never guess a selector; a wrong one appends a stray property that only
a factory reset clears.

The pipeline: the elevated cap_server task (`setup_capture_task.ps1` registers it) polls
DevTools/cap_control/cmd.txt for START <name> / STOP and writes DevTools/cap_control/status.txt;
the pcap lands in DevTools/captures/<name>.pcap. The control channel is interrupt EP 0x05 OUT / 0x85 IN, and a
property write is a report-3 record, decoded by `capture_analyze.host_writes` at any address
depth: a global selector, a mic's two-byte channel address, a cross-point or a pad.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import capture_analyze as ca

HERE = os.path.dirname(os.path.abspath(__file__))
CTRL = os.path.join(HERE, "cap_control")
CMD = os.path.join(CTRL, "cmd.txt")
STATUS = os.path.join(CTRL, "status.txt")
CAPTURES = os.path.join(HERE, "captures")
SETTLE_BEFORE = 1.2      # let the capture actually start before we touch anything
SETTLE_AFTER = 2.5       # the app can send a beat after the click; do not cut it off


def status() -> str:
    # utf-8-sig: cap_server.ps1 writes the status file with a BOM, and printing that to a cp1252
    # console raises UnicodeEncodeError - which would abort a probe mid-capture.
    try:
        with open(STATUS, encoding="utf-8-sig", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return "(no status file)"


def send(cmd: str) -> None:
    os.makedirs(CTRL, exist_ok=True)
    with open(CMD, "w", encoding="utf-8") as f:
        f.write(cmd + "\n")


def wait_for(prefix: str, timeout: float = 20.0) -> str:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        s = status()
        if s.startswith(prefix):
            return s
        time.sleep(0.25)
    return status()


def drive(action: list[str]) -> None:
    """Run one gui_drive command. Kept as a subprocess so the driver stays the single place that
    knows about window handles, DPI and the coord map."""
    if not action or action[0] == "none":
        return
    subprocess.run([sys.executable, os.path.join(HERE, "gui_drive.py")] + action, check=False)


def decode(name: str) -> None:
    """Print every property WRITE in the capture, with its selector."""
    path = os.path.join(CAPTURES, name + ".pcap")
    if not os.path.isfile(path):
        print(f"  no capture at {path}")
        return
    try:
        pkts = ca.load_capture(path)
    except Exception as exc:
        print(f"  could not read the capture: {exc}")
        return
    # HOST -> DEVICE only. The IN direction carries the desk's own state pushes, which use the same
    # record shape; counting those as writes would invent selectors we never saw the app send.
    seen = list(ca.host_writes(pkts).values())
    print(f"  ({len(pkts)} packets, {sum(1 for p in pkts if not p.from_device)} host->device)")
    if not seen:
        print("  no property writes found in this capture")
        return
    for path, prop, value in seen:
        where = "[" + ", ".join(f"0x{i:02X}" for i in path) + "]"
        print(f"  path {where:<16} {prop:<28} {value!r}")


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "--decode":
        decode(sys.argv[2])
        return
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    name, action = sys.argv[1], sys.argv[2:]
    print(f"status before: {status()}")
    send(f"START {name}")
    s = wait_for("CAPTURING", timeout=25)
    print(f"capture: {s}")
    if not s.startswith("CAPTURING"):
        print("capture did not start; aborting so we do not drive blind")
        sys.exit(1)
    time.sleep(SETTLE_BEFORE)
    drive(action)
    time.sleep(SETTLE_AFTER)
    send("STOP")
    print(f"stopped: {wait_for('STOPPED', timeout=25)}")
    print("property writes seen:")
    decode(name)


if __name__ == "__main__":
    main()
