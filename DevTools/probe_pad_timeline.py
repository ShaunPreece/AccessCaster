"""A TIMELINE of what the desk does around two MIDI presses. The definitive pad-timing tool.

A push rate measured BETWEEN the presses cannot tell a pass that began with the pad already
playing (press 1 stops it, press 2 starts it) from one where the second press was ignored. So
nothing is measured in windows: a reader thread timestamps every `padProgress` and `padActive`
while the main thread presses at known times, each row first checks that press 1 started the
sound, and the timeline is read afterwards.

    python DevTools/probe_pad_timeline.py [padIdx] [gap seconds...]
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import midi_out
import soundpad
from _desk import push_handle, pushes

QUIET_S = 0.5      # no progress for this long = not playing


class Reader(threading.Thread):
    """Timestamps every pad push on its own handle, so the main thread never has to stop and look.
    Each event is (`time.monotonic()` when it arrived, name, pad index, value)."""

    def __init__(self):
        super().__init__(daemon=True)
        self.events, self.stop = [], threading.Event()

    def run(self):
        with push_handle("pad timeline") as dev:
            for _at, path, nm, val in pushes(dev, float("inf"), stop=self.stop.is_set,
                                             poll_ms=50):
                if nm in ("padProgress", "padActive"):
                    self.events.append((time.monotonic(), nm, path[-1], val))

    def playing_at(self, when: float) -> bool:
        """Was progress arriving around `when`? Judged on a push within QUIET_S either side."""
        return any(nm == "padProgress" and abs(t - when) <= QUIET_S
                   for t, nm, _p, _v in self.events)

    def since(self, when: float):
        return [(round(t - when, 2), nm, p, round(v, 3) if isinstance(v, float) else v)
                for t, nm, p, v in self.events if t >= when]


def main() -> int:
    pad_idx = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    gaps = [float(g) for g in sys.argv[2:]] or [0.3, 0.6, 1.0, 2.0]
    pads = {pi: (pos, nm, pt) for pos, pi, pt, nm in soundpad.pad_positions()}
    pos, name, _pt = pads[pad_idx]
    modes = soundpad.pad_play_modes()
    bank, slot = pad_idx // 8, pad_idx % 8 + 1
    print(f"{name}: padIdx {pad_idx}, position {pos}, bank {bank + 1} slot {slot}, "
          f"mode {soundpad.PLAY_MODE_LABEL.get(modes.get(pad_idx))}")

    was_bank = soundpad.current_bank()
    reader = Reader()
    reader.start()
    time.sleep(0.3)
    try:
        with midi_out.midi_enabled():
            soundpad.select_bank(bank)
            time.sleep(0.4)
            for gap in gaps:
                # Get to silence, whatever it takes, and SAY how many presses that needed.
                tries = 0
                while reader.playing_at(time.monotonic()) and tries < 4:
                    midi_out.play_pad(slot)
                    tries += 1
                    time.sleep(1.2)
                t_start = time.monotonic()
                midi_out.play_pad(slot)
                time.sleep(gap)
                t_second = time.monotonic()
                midi_out.play_pad(slot)
                time.sleep(1.5)
                started = reader.playing_at(t_start + 0.35)
                still = reader.playing_at(t_second + 1.2)
                verdict = ("press 1 never started it - IGNORE THIS ROW" if not started
                           else "second press STOPPED it" if not still
                           else "second press was IGNORED, still playing")
                print(f"  gap {gap:4.1f}s: {verdict}")
                prog = [e for e in reader.since(t_start) if e[1] == "padProgress"]
                print(f"            {len(prog)} progress pushes, "
                      f"first at +{prog[0][0] if prog else float('nan'):.2f}s, "
                      f"last at +{prog[-1][0] if prog else float('nan'):.2f}s")
                time.sleep(0.5)
            # Leave it silent.
            for _ in range(4):
                if not reader.playing_at(time.monotonic()):
                    break
                midi_out.play_pad(slot)
                time.sleep(1.2)
    finally:
        midi_out.close()
        reader.stop.set()
        reader.join(timeout=2)
        if was_bank is not None:
            soundpad.select_bank(was_bank)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
