"""A turn of the encoder that moves nothing, with no channel selected: put the encoder back on the
virtual faders and monitor.

The desk can leave the encoder bound to nothing. It still reports each turn (`encoderSignal`), but
no level follows. This listener hears every turn and every level the encoder can move on the one
listener (`desk_live`), whether or not announcements are on, and a turn that nothing follows within
`DEAD_S` runs `on_dead`, which the server supplies (`server.action_reset_encoder`).

Left alone: a turn while a channel button holds the encoder (that channel's gain is its target, and
the channel button gives it back), and a turn that may be against the end of a range, which
legitimately moves nothing.
"""
from __future__ import annotations

import time

import desk_live
import desk_model
import devices
import diagnostics

# What a working turn pushes: a virtual fader or the monitor on the levels ring, a headphone pot, a
# channel's gain, or a cross-point the fader carries.
MOVES = frozenset({"faderLevel", "outputMonLevel", "potLevel", "inputMicrophoneGain",
                   "inputDigitalGain", "mixLevelWithAnchor"})
DEAD_S = 0.45       # a turn with no level change within this was a turn against nothing
GAP_S = 4.0         # and the encoder is not put back more often than this
_TICK_S = 0.1


def _maybe_at_limit(step: int) -> bool:
    """Whether a level on the levels ring is at the end this turn goes towards. Which one the
    encoder is on cannot be known, so any of them counts."""
    levels = [desk_model.fader_level(i) for i in devices.get_active_device().fader.level_faders]
    levels.append(desk_model.monitor_level())
    known = [level for level in levels if level is not None]
    return any(level <= 0.001 for level in known) if step < 0 else \
        any(level >= 0.999 for level in known)


class Watcher:
    """`on_dead()` runs on the follower's thread after a turn that moved nothing."""

    def __init__(self, on_dead):
        self._on_dead = on_dead
        self._turn = None           # (when, step) of the turn waiting for a level to follow
        self._fixed_at = 0.0
        self._follower = None

    def start(self) -> None:
        self._follower = desk_live.Follower(
            "encoder home", MOVES | {"encoderSignal"}, on_push=self._push, on_lost=self._lost,
            on_tick=self._check, tick_s=_TICK_S).start()

    def _lost(self) -> None:
        self._turn = None

    def _push(self, push) -> None:
        if push.name == "encoderSignal":
            value = push.value
            step = value[0][0] if push.kind == "pair" and value and value[0][1] == "int" else 0
            self._turn = (time.monotonic(), step) if step else None
        else:
            self._turn = None
        self._check()

    def _check(self) -> None:
        if self._turn is None:
            return
        at, step = self._turn
        now = time.monotonic()
        if now - at < DEAD_S:
            return
        self._turn = None
        if now - self._fixed_at < GAP_S or desk_model.selected_channel() != -1:
            return
        if _maybe_at_limit(step):
            return
        self._fixed_at = now
        try:
            self._on_dead()
        except Exception as exc:    # a failed write must never take the listener down
            diagnostics.event("encoder_home.failed", str(exc))
