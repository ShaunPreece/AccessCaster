"""A TIMELINE of what the desk does around two MIDI presses. The definitive pad-timing tool.

WHY A THIRD ONE. Two earlier probes measured the push rate BETWEEN the presses, and both were
wrong in the same way: neither confirmed that press 1 actually STARTED the sound, so a pass that
began with a pad already playing (press 1 = stop, press 2 = start) read exactly like a pass where
the second press was ignored. That is how "a second press within a second is ignored" was concluded
from a desk that was doing something else entirely.

The fix is to stop measuring in windows. A reader thread timestamps every `padProgress` and
`padActive` continuously while the main thread presses at known times, and the timeline is read
afterwards. Nothing is inferred from a rate taken during a gap, and there is no measurement window
to fall into.

    python DevTools/probe_pad_timeline.py [padIdx] [gap seconds...]
"""
from __future__ import annotations
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401  (puts Project/ on sys.path)

import hid
import midi_out
import model_tree
import rcp_read
import rcp_transfer
import soundpad

QUIET_S = 0.5      # no progress for this long = not playing


class Reader(threading.Thread):
    """Timestamps every pad push on its own handle, so the main thread never has to stop and look."""

    def __init__(self, path):
        super().__init__(daemon=True)
        self.path, self.events, self.stop = path, [], threading.Event()
        self.t0 = None

    def run(self):
        dev = hid.device()
        dev.open_path(self.path)
        dev.set_nonblocking(False)
        try:
            while not self.stop.is_set():
                chunk = dev.read(rcp_read.BULK_LEN, timeout_ms=50)
                if not chunk:
                    continue
                b = bytes(chunk)
                ln = rcp_read._push_record_len(b)
                if not ln:
                    continue
                path, nm, val, _k, _e = model_tree.parse_record(b[5:5 + ln])
                if nm in ("padProgress", "padActive"):
                    self.events.append((time.time(), nm, path[-1], val))
        finally:
            dev.close()

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
    iface = rcp_transfer.find_control_interface()
    reader = Reader(iface.path)
    reader.start()
    time.sleep(0.3)
    try:
        with midi_out.midi_enabled():
            soundpad.select_bank(bank)
            time.sleep(0.4)
            with midi_out.MidiOut() as m:
                for gap in gaps:
                    # Get to silence, whatever it takes, and SAY how many presses that needed.
                    tries = 0
                    while reader.playing_at(time.time()) and tries < 4:
                        m.play_pad(slot)
                        tries += 1
                        time.sleep(1.2)
                    t_start = time.time()
                    m.play_pad(slot)
                    time.sleep(gap)
                    t_second = time.time()
                    m.play_pad(slot)
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
                    if not reader.playing_at(time.time()):
                        break
                    m.play_pad(slot)
                    time.sleep(1.2)
    finally:
        reader.stop.set()
        reader.join(timeout=2)
        if was_bank is not None:
            soundpad.select_bank(was_bank)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
