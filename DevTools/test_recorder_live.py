"""Drive the recorder from software and watch what the desk says back. Repeatable.

WHAT IT SETTLES, and each of these blocks a piece of the Recording UI:
  1. **Does the desk PUSH recorder state?** If `recordState` arrives on report-04 the app can say
     "recording started" the instant the physical REC button is pressed, and can show a live state
     without polling a 1.5 s model dump. If it pushes `recordTimeMs` too, that is the elapsed-time
     counter the model dump does NOT have (it read 0 through a 21-second recording).
  2. **Do markers work?** `requestDropMarker` sits in RECORDER, the block whose selector 0x03 came
     off the wire, so it is addressable without a capture. Never written before, so: write it, and
     judge it by what the desk does, since a request field self-clears.
  3. **What is `recordState` 1?** Presumed pause, never tested. `requestRecordState = 1` mid-take
     either pauses it or it does not.
And it hands us a fourth thing for free: the RECORDINGS block has NO confirmed selector, so
`requestDeleteUID` (the "delete a recording" field) cannot be written safely. If the desk pushes
`recordingTotalCount` when a recording finishes, that push NAMES the block's selector at no risk.

SAFETY. Every write here is `requestRecordState` or `requestDropMarker` on RECORDER 0x03, a block
anchored by two properties read straight off the wire. It leaves a short test recording on the
drive, which is the point; it deletes nothing (we cannot yet). Occurrence counts are diffed at the
end so a stray would be caught rather than assumed away.

    python DevTools/test_recorder_live.py --secs 12
"""
from __future__ import annotations
import argparse
import sys
import threading
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401  (puts Project/ on sys.path)

import hid
import rcp_read
import rcp_transfer
from selector_from_push import decode

SEL_RECORDER = 0x03

STATE_WORD = {0: "ready", 1: "paused (presumed)", 2: "recording", 3: "no destination"}


def state_now() -> tuple:
    """(recordState, recordTimeMs, recordingTotalCount) from a fresh dump."""
    b = rcp_read.read_model_stable(fresh=True)
    def one(p, kind="int"):
        v = [v for v, k in rcp_read.find_all(b, p) if k == kind]
        return v[0] if v else None
    return one("recordState"), one("recordTimeMs"), one("recordingTotalCount"), b


class Listener(threading.Thread):
    """Read-only push watcher on its own handle, so it never fights the writes."""

    def __init__(self):
        super().__init__(daemon=True)
        self.stop_at = None
        self.seen = []          # (t, kind, selector, name, value bytes)
        self.running = True

    def run(self):
        iface = rcp_transfer.find_control_interface()
        if iface is None:
            return
        dev = hid.device()
        dev.open_path(iface.path)
        try:
            while self.running:
                rep = dev.read(512, timeout_ms=100)
                if not rep:
                    continue
                b = bytes(rep)
                got = decode(b[5:])
                if got is None:
                    continue
                kind, sel, name, tail = got
                # The REPORT ID matters as much as the selector: the announcer only looks at
                # report 0x04, so a state push arriving on another report would never be spoken.
                self.seen.append((time.time(), f"{kind}/r{b[0]:02x}", sel, name, tail.hex()))
        finally:
            dev.close()


def send(name: str, value: int) -> None:
    print(f"    -> {name} = {value} on selector 0x{SEL_RECORDER:02X}")
    rcp_transfer.send_record(rcp_transfer.build_global_prop_int(name, SEL_RECORDER, value))


def send_float(name: str, value: float) -> None:
    """The same write in the property's OWN type. requestDropMarker echoes back as an 8-byte
    double (value block `01 09 06 ...`), not the int form every other request field uses, so an
    int write is a type mismatch even though the desk consumed it."""
    print(f"    -> {name} = {value} (double) on selector 0x{SEL_RECORDER:02X}")
    rcp_transfer.send_record(rcp_transfer.build_global_prop_float(name, SEL_RECORDER, value))


def occurrences(blob) -> Counter:
    c = Counter()
    for _hdr, prop, _v, _k in rcp_read.iter_block_properties(blob):
        c[prop] += 1
    return c


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=12.0, help="how long to record for")
    ap.add_argument("--no-marker", action="store_true")
    ap.add_argument("--no-pause", action="store_true")
    args = ap.parse_args()

    st, ms, total, before = state_now()
    print(f"BEFORE  recordState={st} ({STATE_WORD.get(st, '?')})  "
          f"recordTimeMs={ms}  recordings={total}")
    if st == 3:
        print("\nThe unit has NO RECORD DESTINATION (recordState 3). Put the drive back in USB 2.")
        return 2
    if st != 0:
        print(f"\nNot idle (recordState {st}). Stop whatever is running first.")
        return 2

    listener = Listener()
    listener.start()
    time.sleep(0.5)
    t0 = time.time()

    print("\nSTART")
    send("requestRecordState", 2)
    time.sleep(2.0)
    st, ms, _t, _b = state_now()
    print(f"    recordState={st} ({STATE_WORD.get(st, '?')})  recordTimeMs={ms}")
    recording = st == 2
    if not recording:
        print("    IT DID NOT START. Stopping here rather than testing markers on nothing.")

    if recording and not args.no_marker:
        print("\nMARKER (requestDropMarker) - both types, so the echo can tell them apart")
        time.sleep(2.0)
        send("requestDropMarker", 1)
        time.sleep(1.5)
        send_float("requestDropMarker", 1.0)
        time.sleep(1.5)

    if recording and not args.no_pause:
        print("\nPAUSE (requestRecordState = 1, presumed pause)")
        send("requestRecordState", 1)
        time.sleep(2.0)
        st_p, ms_p, _t, _b = state_now()
        print(f"    recordState={st_p} ({STATE_WORD.get(st_p, '?')})  recordTimeMs={ms_p}")
        print("\nRESUME (requestRecordState = 2)")
        send("requestRecordState", 2)
        time.sleep(2.0)
        st_r, _m, _t, _b = state_now()
        print(f"    recordState={st_r} ({STATE_WORD.get(st_r, '?')})")

    if recording:
        left = args.secs - (time.time() - t0)
        if left > 0:
            print(f"\nrecording for another {left:.0f}s")
            time.sleep(left)

    print("\nSTOP")
    send("requestRecordState", 0)
    time.sleep(2.5)
    st, ms, total_after, after = state_now()
    print(f"AFTER   recordState={st} ({STATE_WORD.get(st, '?')})  "
          f"recordTimeMs={ms}  recordings={total_after}")

    listener.running = False
    listener.join(timeout=2.0)

    print(f"\nPUSHES ({len(listener.seen)} records, the ones the recorder cares about first)")
    interesting = [r for r in listener.seen
                   if any(w in r[3].lower() for w in
                          ("record", "storage", "marker", "recording"))]
    for t, kind, sel, name, val in interesting:
        selt = "-" if sel is None else f"0x{sel:02X}"
        print(f"  +{t - t0:6.2f}s  {kind:<6} sel {selt:>5}  {name:<28} value={val}")
    if not interesting:
        print("  NOTHING recorder-shaped was pushed. So state must be polled from the model.")
    others = Counter(r[3] for r in listener.seen if r not in interesting)
    if others:
        print("  (other pushes: " + ", ".join(f"{n} x{c}" for n, c in others.most_common(8)) + ")")

    ob, oa = occurrences(before), occurrences(after)
    moved = {p: (ob.get(p, 0), n) for p, n in oa.items() if n != ob.get(p, 0)}
    moved.update({p: (n, oa.get(p, 0)) for p, n in ob.items() if p not in oa})
    print("\nSTRAY CHECK (per-property occurrence counts)")
    if moved:
        # A finished recording ADDS a RECORDING block, so recordingContent / recordingUID going up
        # by one each is the expected shape, not a stray. Anything else is worth a look.
        expected = {"recordingContent", "recordingUID"}
        odd = {p: v for p, v in moved.items() if p not in expected}
        for p, (b_, a_) in sorted(moved.items()):
            tag = "  (expected: the new recording)" if p in expected else "  <- LOOK AT THIS"
            print(f"    {p}: {b_} -> {a_}{tag}")
        if not odd:
            print("  only the new recording's own properties moved: no stray.")
    else:
        print("  no occurrence count changed anywhere.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
