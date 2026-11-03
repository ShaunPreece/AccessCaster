"""Find what blocks the window's thread while the app starts.

Runs the app as tray.py does, with a watchdog thread that posts a heartbeat to the wx thread every
50 ms and samples that thread's stack while a heartbeat is waiting. Every wait over STALL_MS is
reported with how long it lasted and the code the wx thread was in, counted across the samples.
The report goes to stdout after RUN_S seconds; the app keeps running until it is quit.

Run: python DevTools/probe_ui_stalls.py   (quit any other AccessCaster first: it is single-instance)
"""
from __future__ import annotations

import collections
import os
import sys
import threading
import time
import traceback

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)

STALL_MS = 200
BEAT_S = 0.05
SAMPLE_S = 0.02
RUN_S = 25
PROJECT = _bootstrap.PROJECT


def _where(frame) -> list[str]:
    """The stack, innermost last, as file:line function, project frames and the innermost frame."""
    stack = traceback.extract_stack(frame)
    lines = [f"{os.path.basename(f.filename)}:{f.lineno} {f.name}" for f in stack
             if PROJECT.lower() in os.path.abspath(f.filename).lower()]
    last = stack[-1]
    inner = f"{os.path.basename(last.filename)}:{last.lineno} {last.name}"
    if not lines or lines[-1] != inner:
        lines.append(f"[{inner}]")
    return lines


def watch(main_id: int, t0: float, stalls: list) -> None:
    import wx
    while wx.GetApp() is None:
        time.sleep(0.01)
    while time.monotonic() - t0 < RUN_S:
        done = threading.Event()
        sent = time.monotonic()
        try:
            wx.CallAfter(done.set)
        except Exception:
            time.sleep(BEAT_S)
            continue
        samples = []
        while not done.wait(SAMPLE_S):
            frame = sys._current_frames().get(main_id)
            if frame is not None:
                samples.append(tuple(_where(frame)))
        waited = time.monotonic() - sent
        if waited * 1000 >= STALL_MS:
            stalls.append((sent - t0, waited, samples))
        time.sleep(BEAT_S)
    report(stalls)


def report(stalls: list) -> None:
    print(f"\n==== {len(stalls)} stall(s) of {STALL_MS} ms or more in the first {RUN_S} s ====",
          flush=True)
    for at, waited, samples in stalls:
        print(f"\nat +{at:.2f}s, the wx thread was busy for {waited * 1000:.0f} ms "
              f"({len(samples)} samples)")
        by_stack = collections.Counter(samples)
        for stack, n in by_stack.most_common(4):
            print(f"  {n} samples:")
            for line in stack[-8:]:
                print(f"      {line}")
    sys.stdout.flush()


if __name__ == "__main__":
    import tray
    stalls: list = []
    t0 = time.monotonic()
    threading.Thread(target=watch, args=(threading.get_ident(), t0, stalls), name="stall-probe",
                     daemon=True).start()
    sys.exit(tray.main())
