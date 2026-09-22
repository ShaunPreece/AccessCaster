"""Read a SELECTOR off the wire: decode the address path of every record the desk pushes.

WHY THIS EXISTS. Scanning a push for name-ish bytes answers "which property arrived?", which is
enough to prove the desk echoes and useless for the question that blocks a new block: what ADDRESS
was that property at? A pushed record carries its own address path in
front of the name, so a single push from a block we have never written to hands us its selector for
free - no write, no stray hazard, no touchscreen.

WHAT IT DECODES. Both framings seen so far, distinguished by the path bytes, not guessed:
    global property : 01 01 01 01 <sel>   (rcp_transfer.build_global_prop_int)
    device push     : 01 01 02 00 01 <sel> (the fader-level form the desk itself sends)
    pad-addressed   : 01 01 02 02 9a 02 01 <sel>
Anything else is printed as raw path bytes rather than forced into a shape.

STRICTLY READ-ONLY. One read handle, no writes of any kind from this file. It is safe to run while
the app is running: the Windows HID stack gives every open handle its own queue (see the announcer
note in server.py).

Usage:
    python DevTools/selector_from_push.py --secs 20
    python DevTools/selector_from_push.py --secs 30 --only storageVolume,wifi,bt
    python DevTools/selector_from_push.py --secs 20 --raw
"""
from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401  (puts Project/ on sys.path)

import hid
import rcp_transfer

PATHS = [
    (bytes([0x01, 0x01, 0x01, 0x01]), "global"),
    (bytes([0x01, 0x01, 0x02, 0x00, 0x01]), "push"),
    (bytes([0x01, 0x01, 0x02, 0x02, 0x9A, 0x02, 0x01]), "pad"),
]
NAME_OK = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")


def decode(payload: bytes):
    """(kind, selector, name, value_bytes) for one record's payload, or None if it is not one."""
    for prefix, kind in PATHS:
        if payload.startswith(prefix):
            i = len(prefix)
            sel = payload[i] if i < len(payload) else None
            # A name starts at the selector+1 for the paths that carry one. Walk the ASCII run.
            j = i + 1
            k = j
            while k < len(payload) and payload[k] in NAME_OK:
                k += 1
            name = payload[j:k].decode("latin1")
            if len(name) < 3:
                # No name after the selector: this framing may put the name where the selector is.
                j = i
                k = j
                while k < len(payload) and payload[k] in NAME_OK:
                    k += 1
                name, sel = payload[j:k].decode("latin1"), None
            return kind, sel, name, payload[k:k + 12]
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=20.0)
    ap.add_argument("--only", help="comma-separated substrings; only show names containing one")
    ap.add_argument("--raw", action="store_true", help="dump the whole record too")
    args = ap.parse_args()
    want = [s.strip().lower() for s in (args.only or "").split(",") if s.strip()]

    iface = rcp_transfer.find_control_interface()
    if iface is None:
        print("no control interface (is the unit connected on USB 1?)")
        return 1
    dev = hid.device()
    dev.open_path(iface.path)
    print(f"listening {args.secs:.0f}s, read-only. Every record decoded as kind/selector/name.")
    seen = {}
    until = time.time() + args.secs
    try:
        while time.time() < until:
            rep = dev.read(512, timeout_ms=100)
            if not rep:
                continue
            b = bytes(rep)
            rid, payload = b[0], b[5:]
            got = decode(payload)
            if got is None:
                if args.raw:
                    print(f"  [{time.time():.3f}] report {rid:#04x} unparsed {b[:32].hex()}")
                continue
            kind, sel, name, tail = got
            if want and not any(w in name.lower() for w in want):
                continue
            key = (rid, kind, sel, name)
            seen[key] = seen.get(key, 0) + 1
            if seen[key] == 1:
                selt = "-" if sel is None else f"0x{sel:02X}"
                print(f"  report {rid:#04x}  {kind:<6} sel {selt:>5}  {name}   value={tail.hex()}")
                if args.raw:
                    print(f"      raw {b[:48].hex()}")
    finally:
        dev.close()

    print("\nSUMMARY (first sighting order, with counts)")
    if not seen:
        print("  nothing arrived. The desk pushes only when something CHANGES.")
    for (rid, kind, sel, name), n in seen.items():
        selt = "-" if sel is None else f"0x{sel:02X}"
        print(f"  {name:<32} {kind:<6} sel {selt:>5}  report {rid:#04x}  x{n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
