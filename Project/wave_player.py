"""Plays 16-bit mono PCM through one output device, and cuts a clip off on request.

`Player(sample_rate)` plays one clip at a time on the calling thread:
  - `play(pcm, device=DEFAULT_DEVICE, cancel=None, min_ms=0)` returns True once the clip has
    finished, or False when `cancel` (a `Signal`) cut it off, which it may only once `min_ms` of the
    clip has played. `device` is used only when the device is not already open. It raises OSError
    when the device fails, and closes it, so the next clip opens afresh.
  - `is_open`, `idle()` (closes the device after CLOSE_AFTER_S without a clip) and `close()`.
`Signal()` is set from another thread to cut a clip off: `set`, `clear`, `close`.
`DEFAULT_DEVICE` is the system's default output. A device is whatever `speech_engine` resolves an
output name to, and is only ever passed back here.

The device is held open between clips, because opening it costs time on every phrase, heard as lag
while a fader moves. It is closed after CLOSE_AFTER_S without a clip, because an open device keeps
the computer awake, and so that a device plugged in or removed meanwhile is found on the next open.

Each platform supplies its own.
"""
import sys

if sys.platform == "win32":
    from win.wave_player import CLOSE_AFTER_S, DEFAULT_DEVICE, Player, Signal
else:
    raise ImportError(f"No sound player for {sys.platform}: the Windows one is "
                      "win/wave_player.py")

__all__ = ["CLOSE_AFTER_S", "DEFAULT_DEVICE", "Player", "Signal"]
