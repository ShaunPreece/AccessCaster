"""
AccessCaster - pick the USBPcap root hub the RODECaster is on.

Given a directory of hubN.pcap scan captures, report which hub caught the most
RODECaster control traffic. Prints a single machine-readable line for the
elevated capture server to parse:

    BEST <hub-number> <device-addr> <signature-count>
    NONE

Pure analysis only; touches no hardware.
"""

import glob
import os
import re
import sys

import capture_analyze as ca


def main(scan_dir: str) -> int:
    best = None  # (count, hub, addr)
    for path in sorted(glob.glob(os.path.join(scan_dir, "hub*.pcap"))):
        m = re.search(r"hub(\d+)\.pcap$", os.path.basename(path))
        if not m:
            continue
        hub = int(m.group(1))
        try:
            pkts = ca.load_capture(path)
        except Exception:
            continue
        count = ca.rode_signature_count(pkts)
        if count == 0:
            continue
        addrs = sorted({p.device_addr for p in pkts
                        if p.from_device and p.report_id == 0x02
                        and len(p.data) >= 2 and p.data[1] == 0x41})
        addr = addrs[0] if addrs else 0
        if best is None or count > best[0]:
            best = (count, hub, addr)
    if best is None:
        print("NONE")
        return 1
    count, hub, addr = best
    print(f"BEST {hub} {addr} {count}")
    return 0


if __name__ == "__main__":
    # Derive the scan dir from this script's own location, so it works no matter
    # what working directory or (space-mangled) argument the caller passes. Only
    # honour an explicit argument if it is actually an existing directory; the
    # PowerShell caller can split a space-containing path into broken fragments.
    here = os.path.dirname(os.path.abspath(__file__))
    # cap_control/scan is where cap_server.ps1 actually puts the hub scan; captures/scan is the
    # older location. Try both, so a mangled argument degrades to the right directory rather than
    # to an empty one (which printed NONE on a perfectly good scan).
    defaults = [os.path.join(here, "cap_control", "scan"), os.path.join(here, "captures", "scan")]
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    scan_dir = arg if (arg and os.path.isdir(arg)) else next(
        (d for d in defaults if os.path.isdir(d)), defaults[0])
    raise SystemExit(main(scan_dir))
