"""LIVE TEST: the model read must survive the desk pushing at it.

WHAT IT PROVES, each of these a fault the read path is capable of:

  1. A read taken WHILE A PAD PLAYS returns in the same time as a quiet one. A push that extends
     the quiet deadline instead runs the read to its `max_reads` ceiling: 172 s measured.
  2. That read is not CONTAMINATED. Same length as a quiet read, and the same tree structure block
     for block and property for property. Push frames appended into the blob give 264180 and
     139485 byte reads against a 136170 baseline.
  3. The push filter never fires on real dump content. Every frame of a quiet dump is re-offered to
     `rcp_read._push_record_len` and none may be called a push - a false positive would silently
     punch 255 bytes out of the middle of the model.
  4. The read is bounded by the dump's DECLARED LENGTH, not by silence (`_last_read`).
  5. The filter recognises REAL pushes off the wire, not just dump content it must leave alone. A
     filter is two claims and this is the other one.
  6. THE OLD LOOP COULD NOT HAVE ESCAPED. After the burst ends, frames go on arriving for as long
     as the pad plays, so "stop when the line goes quiet" had nothing to stop on. Measured directly
     instead of inferred, and it is why the length bound is the fix rather than a bigger timeout.

WHY A HEALTHY READ NOW SKIPS ZERO PUSHES, which looks like the test missing its target and is not:
the dump burst is atomic and the length bound closes the handle the moment the last dump byte
lands, so the pushes queueing up behind it are never even read. Checks 5 and 6 are what exercise
the filter; a nonzero skip count in the runs is a bonus, not the proof.

VALUES ARE ALLOWED TO DIFFER and the test says so explicitly: `padProgress`, `padActive` and the
meters are properties INSIDE the model, so a dump taken while a pad plays legitimately carries
different numbers. Structure is the thing contamination changes, so structure is what is asserted.

Needs the pad to be reachable over MIDI, which means `systemMidiControl` on - the test turns it on
and puts it back. Nothing else is written. No one has to be at the desk.

Usage:
    python DevTools/test_read_under_push_live.py
    python DevTools/test_read_under_push_live.py --pad 3 --runs 5
"""
from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401  (puts Project/ on sys.path)

import hid
import midi_out
import model_tree
import rcp_read
from rcp_hid import find_control_interface

SLOW_S = 3.0        # a healthy dump is well under a second; anything near a minute is the old bug
TAIL_S = 2.0        # how long to watch the line AFTER the burst, for check 6


def open_watcher():
    """A read handle used only to WATCH the push stream, never to trigger a dump."""
    iface = find_control_interface()
    if iface is None:
        raise RuntimeError("no control interface (is the desk connected?)")
    dev = hid.device()
    dev.open_path(iface.path)
    dev.set_nonblocking(False)
    return dev


def drain(dev) -> None:
    """Read the handle empty.

    LOAD-BEARING BEFORE EVERY PRESS. Windows queues the pushes that arrived while nobody was
    reading, so a press that STOPPED a latched pad was immediately "confirmed playing" off frames
    from before it - which put the pre-fix demonstration on a silent desk and made it pass.
    """
    while dev.read(rcp_read.BULK_LEN, timeout_ms=1):
        pass


def progress_count(dev, secs: float) -> int:
    """How many `padProgress` / `padActive` pushes arrive on `dev` in `secs`.

    A COUNT, NOT A YES/NO, and that distinction is the whole reliability of this test. A pad that
    is STOPPING emits a last padProgress and a padActive, so "did one arrive?" says playing for a
    press that stopped it. A pad that is PLAYING pushes at about 20 a second, so the two states are
    far apart on a count and adjacent on a boolean.
    """
    n, until = 0, time.time() + secs
    while time.time() < until:
        chunk = dev.read(rcp_read.BULK_LEN, timeout_ms=rcp_read.READ_TIMEOUT_MS)
        if not chunk:
            continue
        b = bytes(chunk)
        ln = rcp_read._push_record_len(b)
        if ln and model_tree.parse_record(b[5:5 + ln])[1] in ("padProgress", "padActive"):
            n += 1
    return n


PLAYING_MIN = 5     # ~16 expected in 0.8 s at 20 pushes a second; a stop emits one or two


def start_playing(dev, midi, pad: int, tries: int = 3) -> bool:
    """Get the pad actually PLAYING, confirmed off the push stream, and say so if it will not.

    PAD 1 ON THIS DUO LATCHES: one press starts it, the NEXT STOPS IT. So "press it and assume"
    tested a quiet desk half the time, which is how the pre-fix demonstration below first came back
    passing. Press, then require a sustained push RATE; too few means that press was a stop, so
    press again.
    """
    for _ in range(tries):
        drain(dev)
        midi.play_pad(pad)
        if progress_count(dev, 0.8) >= PLAYING_MIN:
            return True
    return False


def stop_playing(dev, midi, pad: int, tries: int = 3) -> bool:
    """The other half of the latch: press until the pushes stop, so the test leaves the desk as it
    found it rather than with a music bed running."""
    for _ in range(tries):
        drain(dev)
        if progress_count(dev, 0.5) < PLAYING_MIN:
            return True
        midi.play_pad(pad)
    drain(dev)
    return progress_count(dev, 0.5) < PLAYING_MIN


def structure(blob: bytes):
    """(block, property, kind) for every property in dump order. Values excluded on purpose."""
    root, _, _ = model_tree.parse(blob)
    return [(n.name, p, k) for n in model_tree.walk(root) for p, _, k in n.props]


def timed_read():
    t0 = time.time()
    blob = rcp_read.read_model_raw()
    return blob, time.time() - t0


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pad", type=int, default=1, help="pad to play, 1-based, in the current bank")
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()
    ok = True

    print("QUIET BASELINE")
    quiet, quiet_s = timed_read()
    quiet_struct = structure(quiet)
    stats = dict(rcp_read._last_read)
    print(f"  {len(quiet)} bytes in {quiet_s:.2f} s, {len(quiet_struct)} properties, "
          f"{stats['frames']} frames, declared {stats['declared']}")
    ok &= check("bounded by the declared length, not by silence",
                stats["bounded_by"] == "length", f"({stats['bounded_by']})")
    ok &= check("declared length accounts for the blob",
                len(quiet) >= 4 + stats["declared"] > len(quiet) - 255)

    print("\nTHE PUSH FILTER ON REAL DUMP CONTENT (every frame of the quiet dump)")
    misread = [i for i in range(0, len(quiet), 255)
               if rcp_read._push_record_len(b"\x04" + quiet[i:i + 255]) is not None]
    ok &= check("no dump frame is mistaken for a push", not misread,
                f"({len(misread)} of {len(quiet) // 255} misread: {misread[:5]})")

    print("\nREADS WHILE A PAD IS PLAYING")
    with midi_out.midi_enabled() as was_on:
        print(f"  systemMidiControl was {'ON' if was_on else 'OFF'}")
        watch = open_watcher()
        try:
            with midi_out.MidiOut() as m:
                for run in range(args.runs):
                    playing = start_playing(watch, m, args.pad)
                    blob, secs = timed_read()
                    stats = dict(rcp_read._last_read)
                    got = structure(blob)
                    diffs = sum(1 for a, b in zip(blob, quiet) if a != b)
                    print(f"\n  RUN {run + 1}: {len(blob)} bytes in {secs:.2f} s, "
                          f"{stats['pushes_skipped']} pushes skipped, {diffs} bytes differ")
                    ok &= check(f"run {run + 1} pad confirmed playing", playing)
                    ok &= check(f"run {run + 1} finished promptly", secs < SLOW_S, f"({secs:.2f} s)")
                    ok &= check(f"run {run + 1} same length as the quiet read",
                                len(blob) == len(quiet), f"({len(blob)} vs {len(quiet)})")
                    ok &= check(f"run {run + 1} same tree structure", got == quiet_struct,
                                "" if got == quiet_struct else
                                f"({len(got)} properties vs {len(quiet_struct)})")
                    ok &= check(f"run {run + 1} bounded by length",
                                stats["bounded_by"] == "length", f"({stats['bounded_by']})")
        finally:
            watch.close()

        print("\nREAL PUSHES OFF THE WIRE, AND WHAT THE OLD LOOP FACED")
        ok &= tail_checks(args.pad)

        print("\nTHE PRE-FIX LOOP, RUN ON PURPOSE (quiet detection, no push filter)")
        ok &= old_loop_check(args.pad, len(quiet))

        print("\nPUTTING THE DESK BACK")
        watch = open_watcher()
        try:
            with midi_out.MidiOut() as m:
                ok &= check("the pad is stopped again", stop_playing(watch, m, args.pad))
        finally:
            watch.close()

    print("\n%s" % ("ALL CHECKS PASSED" if ok else "SOMETHING FAILED - see above"))
    return 0 if ok else 1


def tail_checks(pad: int) -> bool:
    """Checks 5 and 6, on one raw handle held open across the whole burst and past its end.

    Holding the handle is the point: `read_model_raw` now closes as soon as the declared length is
    in, so it never sees the tail. This is where the pushes are, and where the old quiet-detection
    loop lived until its 4000-read ceiling.
    """
    ok = True
    dev = open_watcher()
    burst, tail = [], []
    try:
        with midi_out.MidiOut() as midi:
            ok &= check("pad confirmed playing", start_playing(dev, midi, pad))
            dev.write(rcp_read.TRIGGER)
            need, got = None, 0
            t0 = time.time()
            while time.time() - t0 < TAIL_S + 4:
                chunk = dev.read(rcp_read.BULK_LEN, timeout_ms=rcp_read.READ_TIMEOUT_MS)
                done = need is not None and got >= need
                if not chunk:
                    if done:
                        break        # burst finished and the line went quiet: no tail to see
                    continue
                b = bytes(chunk)
                (tail if done else burst).append(b)
                if not done and rcp_read._push_record_len(b) is None:
                    got += len(b) - 1
                    if need is None:
                        declared = int.from_bytes(b[1:5], "little")
                        need = (4 + declared if declared >= rcp_read.MODEL_MIN_DECLARED else None)
                if done and time.time() - t0 > TAIL_S + 1:
                    break
    finally:
        dev.close()

    pushes = [b for b in tail if rcp_read._push_record_len(b) is not None]
    ok &= check("frames keep arriving after the burst, so silence was never coming",
                len(tail) > 0, f"({len(tail)} frames in the tail)")
    ok &= check("every tail frame is recognised as a push", len(pushes) == len(tail),
                f"({len(pushes)} of {len(tail)})")
    names = {model_tree.parse_record(b[5:5 + rcp_read._push_record_len(b)])[1] for b in pushes}
    print(f"    pushed properties: {sorted(names)}")
    return ok


OLD_LOOP_CEILING = 700     # the real ceiling is 4000 reads / 240 s; 700 shows the fault in seconds


def old_loop_check(pad: int, healthy_bytes: int) -> bool:
    """Run the loop AS IT WAS and show it does not stop and does not come back clean.

    This is here so the two faults stay demonstrated rather than remembered. Without it, a later
    session sees a filter with no visible reason and a length bound that "just works", and the
    cheapest-looking simplification is to take one of them out. The ceiling is cut from 4000 reads
    to 700 so the demonstration costs seconds instead of the 172 s that was measured on this desk.

    Read-only: the same trigger the app sends, and one MIDI note. Nothing is written to the model.
    """
    dev = open_watcher()
    buf = bytearray()
    playing = False
    try:
        with midi_out.MidiOut() as midi:
            playing = start_playing(dev, midi, pad)
            dev.write(rcp_read.TRIGGER)
            t0 = time.time()
            deadline = t0 + 1.5
            reads = 0
            while time.time() < deadline and reads < OLD_LOOP_CEILING:
                chunk = dev.read(rcp_read.BULK_LEN, timeout_ms=rcp_read.READ_TIMEOUT_MS)
                reads += 1
                if not chunk:
                    continue
                b = bytes(chunk)
                if b and b[0] == 0x04:
                    buf.extend(b[1:])
                    deadline = time.time() + rcp_read.QUIET_S
            secs = time.time() - t0
    finally:
        dev.close()

    print(f"  the old loop: {len(buf)} bytes in {secs:.1f} s, {reads} reads")
    ok = check("pad confirmed playing", playing)
    ok &= check("the old loop ran to its ceiling rather than finishing",
                reads >= OLD_LOOP_CEILING, f"({reads} reads, ceiling {OLD_LOOP_CEILING})")
    ok &= check("the old loop's blob is contaminated, not merely late",
                len(buf) > healthy_bytes, f"({len(buf)} bytes vs a healthy {healthy_bytes})")
    return ok


if __name__ == "__main__":
    raise SystemExit(main())
