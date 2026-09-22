"""Join a wifi network from the PC, with no touchscreen. Reads the password from STDIN, never argv.

THE POINT. Joining wifi was one of the jobs that could only be done on the unit's own screen, which
a blind operator cannot use. NETWORK is selector 0x10 (proven), so the fields are
reachable: wifiSSID and wifiPSK are strings, `wifi` is the on switch, wifiDHCP is already True.

THE PASSWORD IS TREATED AS A SECRET THROUGHOUT, and that is not decoration:
  * it is read from STDIN, so it never appears in a command line, a shell history or a process list;
  * it is never printed, never logged, and never written to a file;
  * the read-back reports only WHETHER a password is stored, never its value. wifiPSK is readable
    off the desk, which is exactly why this file refuses to show it.

WHAT IT REPORTS AFTERWARDS, from the desk's own model: the SSID it now holds, whether a password is
stored, whether the radio is on, the IP address it was given, and wifiIncorrectPSK, which is the
desk's own way of saying the password was refused. It also diffs per-property OCCURRENCE COUNTS
before and after, so a stray would be caught rather than assumed away.

Usage:
    echo "the-password" | python DevTools/wifi_join.py --ssid "Your Network Name"
    python DevTools/wifi_join.py --status          # read only, writes nothing
    python DevTools/wifi_join.py --off             # turn the radio off again
"""
from __future__ import annotations
import argparse
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import rcp_read
import rcp_transfer

SEL = rcp_transfer.SEL_NETWORK
SETTLE = 1.2
JOIN_TIMEOUT = 45.0


def net(blob, prop):
    return rcp_read.block_value(blob, "NETWORK", prop)


def occurrences(blob) -> Counter:
    c = Counter()
    for _hdr, prop, _v, _k in rcp_read.iter_block_properties(blob):
        c[prop] += 1
    return c


def report(blob, label: str) -> None:
    psk = net(blob, "wifiPSK")
    print(f"  {label}")
    print(f"    radio on            {net(blob, 'wifi')}")
    print(f"    ssid                {net(blob, 'wifiSSID')!r}")
    print(f"    password stored     {bool(psk)}        (value deliberately not shown)")
    print(f"    dhcp                {net(blob, 'wifiDHCP')}")
    print(f"    ip                  {net(blob, 'wifiIpAddress')!r}")
    print(f"    gateway             {net(blob, 'wifiGateway')!r}")
    print(f"    password refused    {net(blob, 'wifiIncorrectPSK')}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ssid")
    ap.add_argument("--status", action="store_true", help="read and print, write nothing")
    ap.add_argument("--off", action="store_true", help="turn the wifi radio off")
    args = ap.parse_args()

    before = rcp_read.read_model_stable(fresh=True)
    report(before, "BEFORE")
    if args.status:
        return 0

    if args.off:
        print("\nturning the wifi radio off")
        rcp_transfer.send_record(rcp_transfer.build_global_prop_bool("wifi", False, SEL))
        time.sleep(SETTLE)
        report(rcp_read.read_model_stable(fresh=True), "AFTER")
        return 0

    if not args.ssid:
        print("give --ssid (and pipe the password in on stdin)")
        return 2
    if sys.stdin.isatty():
        print("the password must arrive on STDIN, so it stays out of the command line.")
        return 2
    # lstrip the BOM: piping from PowerShell prepends U+FEFF, which reaches the ascii encode in
    # build_global_prop_string and dies there, AFTER the SSID has already been written to the desk.
    psk = sys.stdin.readline().rstrip("\r\n").lstrip("﻿")
    if not psk:
        print("no password on stdin.")
        return 2

    # Credentials FIRST, then the switch: bringing the radio up before it knows where to go would
    # invite an association failure that then has to be told apart from a wrong password.
    print(f"\nwriting ssid {args.ssid!r}, then the password, then turning the radio on")
    rcp_transfer.send_record(rcp_transfer.build_global_prop_string("wifiSSID", args.ssid, SEL))
    time.sleep(0.4)
    rcp_transfer.send_record(rcp_transfer.build_global_prop_string("wifiPSK", psk, SEL))
    del psk
    time.sleep(0.4)
    rcp_transfer.send_record(rcp_transfer.build_global_prop_bool("wifi", True, SEL))

    print(f"waiting up to {JOIN_TIMEOUT:.0f}s for an IP address")
    deadline, blob, ip = time.time() + JOIN_TIMEOUT, None, None
    while time.time() < deadline:
        time.sleep(2.0)
        blob = rcp_read.read_model_stable(fresh=True)
        ip = net(blob, "wifiIpAddress")
        if net(blob, "wifiIncorrectPSK"):
            print("  the desk says the password was REFUSED.")
            break
        if ip:
            print(f"  got an address after {JOIN_TIMEOUT - (deadline - time.time()):.0f}s")
            break
    blob = blob if blob is not None else rcp_read.read_model_stable(fresh=True)
    print()
    report(blob, "AFTER")

    ob, oa = occurrences(before), occurrences(blob)
    moved = {p: (ob.get(p, 0), n) for p, n in oa.items() if n != ob.get(p, 0)}
    moved.update({p: (n, oa.get(p, 0)) for p, n in ob.items() if p not in oa})
    print("\nSTRAY CHECK (per-property occurrence counts)")
    if moved:
        print("  COUNTS MOVED - inspect before writing anything else:")
        for p, (b, a) in moved.items():
            print(f"    {p}: {b} -> {a}")
    else:
        print("  no occurrence count changed anywhere: no stray.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
