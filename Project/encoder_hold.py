"""Press and hold on the encoder while a channel button holds it: mute or unmute that channel.

The desk gives the hold a job only for the encoder's ring target: a virtual fader's channel, or the
monitor speakers. With a channel button holding the encoder its hold does nothing, so this listener
gives it the same job there. It runs whether or not announcements are on, and the mute is spoken
through the announcer like the desk's own.

Its own read-only handle, like recorder.Watcher, so it never takes another listener's packets. The
write goes through `on_hold`, which the server supplies.
"""
from __future__ import annotations

import threading
import time

import diagnostics
import rcp_hid
import rcp_read
import rcp_transfer

# The desk mutes its own hold target about 750 ms after the press edge, while still held, and a short
# press lasts up to about 475 ms; this sits between the two.
HOLD_S = 0.75


class Watcher(threading.Thread):
    """`on_hold(strip)` runs on this thread once per hold, while a channel button holds the encoder.
    Until a `systemChannelSelected` push says which channel that is, a hold does nothing."""

    def __init__(self, on_hold):
        super().__init__(daemon=True, name="encoder-hold")
        self._on_hold = on_hold
        self._selected = None       # the strip a channel button gave the encoder; -1 none
        self._down_at = None        # when the encoder went down, while it is down
        self._fired = False
        self.running = True

    def run(self) -> None:
        dev = None
        while self.running:
            if dev is None:
                iface = rcp_transfer.find_control_interface(fresh=True)
                if iface is None:
                    time.sleep(2.0)
                    continue
                try:
                    dev = rcp_hid.open_handle(iface, "encoder hold")
                except Exception as exc:
                    diagnostics.event("device.open_failed", "encoder hold", error=f"{exc}")
                    time.sleep(2.0)
                    continue
            try:
                rep = dev.read(256, timeout_ms=100)
            except Exception:
                rcp_hid.close_handle(dev, "encoder hold")
                dev, self._down_at = None, None
                time.sleep(0.5)
                continue
            if rep:
                self._push(bytes(rep))
            self._check_hold()
        if dev is not None:
            rcp_hid.close_handle(dev, "encoder hold")

    def _push(self, report: bytes) -> None:
        rec = rcp_read.parse_push(report)
        if rec is None:
            return
        _path, name, value, kind = rec
        if name == "systemChannelSelected" and kind == "int":
            self._selected = value
        elif name == "encoderPressed" and kind == "bool":
            self._down_at, self._fired = (time.monotonic() if value else None), False

    def _check_hold(self) -> None:
        if (self._down_at is None or self._fired or self._selected is None or self._selected < 0
                or time.monotonic() - self._down_at < HOLD_S):
            return
        self._fired = True
        try:
            self._on_hold(self._selected)
        except Exception as exc:    # a failed mute must never take the listener down
            diagnostics.event("encoder_hold.failed", str(exc))
