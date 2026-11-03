"""Can we START a wifi or Bluetooth scan from software, and can we PROVE it ran?

A scan that fills its result slots tells the operator what is in range, the first step of joining
a network without typing its name.

IT IS VERIFIABLE. These are ACTIONS, so a read-back of
the field itself proves nothing (the firmware consumes and clears it). But the RESULTS are ordinary
readable properties - wifiScan1..10 and btScan1..10 - so the honest test is: note what is in the
result slots, fire the scan, wait, and see whether they change. The wifi run also reads the
WIFISCANRESULT blocks. A desk joined to a network never fills either (PROJECT_GUIDE part 2), so run
it on an unjoined desk.

SAFE: both fields are in NETWORK, selector 0x10, a confirmed selector. Neither changes a setting - a scan looks around and fills a list.
Occurrence counts are diffed at the end so a stray would be caught rather than assumed away.

    python DevTools/test_network_scan_live.py --wifi
    python DevTools/test_network_scan_live.py --bt
    python DevTools/test_network_scan_live.py --wifi --bt --wait 20
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401
import rcp_read
import rcp_transfer
from _desk import count_changes, stray_counts

def slots(blob, prefix, n=10):
    """The scan-result slots, in order, blanks included so a change of shape is visible."""
    return [rcp_read.block_value(blob, "NETWORK", f"{prefix}{i}") or "" for i in range(1, n + 1)]


def result_blocks(blob):
    """Every non-blank property of the WIFISCANRESULT blocks, a NETWORK child holding one network
    per instance, as (name, value) in dump order."""
    return [(name, val) for block, name, val, _kind in rcp_read.iter_block_properties(blob)
            if block == "WIFISCANRESULT" and val not in (None, "")]


def run_scan(kind: str, prop: str, prefix: str, wait: float) -> bool:
    print(f"\n=== {kind.upper()} SCAN ===")
    before_blob = rcp_read.read_model_stable(fresh=True)
    before = slots(before_blob, prefix)
    blocks_before = result_blocks(before_blob) if kind == "wifi" else []
    print(f"  before: {[s for s in before if s] or 'nothing in the result slots'}")
    if kind == "wifi":
        print(f"  WIFISCANRESULT before: {blocks_before or 'nothing'}")

    print(f"  writing {prop} = True on NETWORK")
    rcp_transfer.write_at(rcp_transfer.NETWORK, prop, "bool", True)

    # Polls for the whole wait rather than stopping at the first change, so the timing of each
    # result form is seen.
    start = time.monotonic()
    after, blocks_after = before, blocks_before
    while time.monotonic() - start < wait:
        time.sleep(3.0)
        blob = rcp_read.read_model_stable(fresh=True)
        now, blocks_now = slots(blob, prefix), result_blocks(blob) if kind == "wifi" else []
        if now != after:
            print(f"  {time.monotonic() - start:.0f}s: slots {[s for s in now if s]}")
        if blocks_now != blocks_after:
            print(f"  {time.monotonic() - start:.0f}s: WIFISCANRESULT {blocks_now}")
        after, blocks_after = now, blocks_now
    print(f"  after:  {[s for s in after if s] or 'nothing in the result slots'}")
    if kind == "wifi":
        print(f"  WIFISCANRESULT after: {blocks_after or 'nothing'}")
    changed = after != before or blocks_after != blocks_before
    if changed:
        print(f"  RESULT: the scan RAN. {prop} is usable.")
    else:
        print(f"  RESULT: nothing moved. Either the scan did not run, or it found exactly what "
              f"was already listed - which is entirely possible on a quiet network, so this is "
              f"NOT proof that {prop} does nothing.")
    return changed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wifi", action="store_true")
    ap.add_argument("--bt", action="store_true")
    ap.add_argument("--wait", type=float, default=20.0)
    args = ap.parse_args()
    if not (args.wifi or args.bt):
        print("give --wifi, --bt, or both")
        return 2

    before = rcp_read.read_model_stable(fresh=True)
    if args.wifi:
        run_scan("wifi", "wifiScan", "wifiScan", args.wait)
    if args.bt:
        run_scan("bluetooth", "btDoScan", "btScan", args.wait)
    after = rcp_read.read_model_stable(fresh=True)

    ob, oa = stray_counts(before), stray_counts(after)
    moved = sorted(sum(count_changes(ob, oa), []))
    print("\nSTRAY CHECK (per-property occurrence counts)")
    if moved:
        print("  COUNTS MOVED - inspect before writing anything else:")
        for p in moved:
            print(f"    {p}: {ob.get(p, 0)} -> {oa.get(p, 0)}")
    else:
        print("  no occurrence count changed anywhere: no stray.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
