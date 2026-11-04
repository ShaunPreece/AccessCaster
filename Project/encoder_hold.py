"""Press and hold on the encoder while a channel button holds it: mute or unmute that channel.

The desk gives the hold a job only for the encoder's ring target: a virtual fader's channel, or the
monitor speakers. With a channel button holding the encoder its hold does nothing, so this listener
gives it the same job there. It runs whether or not announcements are on, and the mute is spoken
through the announcer like the desk's own.

It follows the encoder's press on the one listener (`desk_live`), and takes the selected channel
from the desk model (`desk_model`). The write goes through `on_hold`, which the server supplies.
"""
from __future__ import annotations

import time

import desk_live
import desk_model
import diagnostics

# The desk mutes its own hold target about 750 ms after the press edge, while still held, and a short
# press lasts up to about 475 ms; this sits between the two.
HOLD_S = 0.75
_TICK_S = 0.1


class Watcher:
    """`on_hold(strip)` runs on the follower's thread once per hold, while a channel button holds
    the encoder. While the model does not know which channel that is, a hold does nothing."""

    def __init__(self, on_hold):
        self._on_hold = on_hold
        self._down_at = None        # when the encoder went down, while it is down
        self._fired = False
        self._follower = None

    def start(self) -> None:
        self._follower = desk_live.Follower(
            "encoder hold", {"encoderPressed"},
            on_push=self._push, on_lost=self._lost, on_tick=self._check_hold,
            tick_s=_TICK_S).start()

    def _lost(self) -> None:
        self._down_at = None

    def _push(self, push) -> None:
        if push.name == "encoderPressed" and push.kind == "bool":
            self._down_at, self._fired = (time.monotonic() if push.value else None), False
        self._check_hold()

    def _check_hold(self) -> None:
        if self._down_at is None or self._fired or time.monotonic() - self._down_at < HOLD_S:
            return
        selected = desk_model.selected_channel()
        if selected is None or selected < 0:
            return
        self._fired = True
        try:
            self._on_hold(selected)
        except Exception as exc:    # a failed mute must never take the listener down
            diagnostics.event("encoder_hold.failed", str(exc))
