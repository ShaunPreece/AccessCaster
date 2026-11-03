"""encoder_hold.Watcher's decisions: a hold while a channel button holds the encoder mutes that
channel, once per hold, and nothing else does; and our own strip writes reach the window. NO DEVICE:
the thread is never started, and pushes are fed to it directly.
"""
from __future__ import annotations

import sys
import time

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
import app_window
import encoder_hold
import rcp_transfer
import server
from _testkit import addr, check, patched, push


def v_bool(on: bool) -> bytes:
    return bytes([0x01, 0x01, 0x02 if on else 0x03])


def v_int(value: int) -> bytes:
    return bytes([0x01, 0x05, 0x01]) + int(value).to_bytes(4, "little", signed=True)


def watcher():
    held = []
    return encoder_hold.Watcher(held.append), held


def hold(w, seconds):
    """The encoder pressed `seconds` ago and still down."""
    w._push(push([6], "encoderPressed", v_bool(True)))
    w._down_at = time.monotonic() - seconds
    w._check_hold()


print("ENCODER HOLD")
w, held = watcher()
w._push(push([15], "systemChannelSelected", v_int(0)))
hold(w, 1.0)
check("a hold with a channel selected mutes that channel", held, [0])
w._check_hold()
check("once per hold, however long it lasts", held, [0])
w._push(push([6], "encoderPressed", v_bool(False)))
hold(w, 1.0)
check("the next hold toggles it again", held, [0, 0])

w, held = watcher()
w._push(push([15], "systemChannelSelected", v_int(0)))
hold(w, 0.3)
check("a short press does nothing", held, [])

w, held = watcher()
w._push(push([15], "systemChannelSelected", v_int(-1)))
hold(w, 1.0)
check("with no channel selected the desk's own hold is left alone", held, [])

w, held = watcher()
hold(w, 1.0)
check("before any selection is known, nothing happens", held, [])

w, held = watcher()
w._push(push([15], "systemChannelSelected", v_int(4)))
w._push(push([15], "systemChannelSelected", v_int(-1)))
hold(w, 1.0)
check("a channel button pressed again hands the encoder back", held, [])

print("THE WINDOW HEARS OUR OWN STRIP WRITES")
# The desk does not push our writes back, so a mute made by the hold (or a shortcut) reaches the
# window as the push the desk would have sent.
seen = []
with patched((app_window, {"pushed": lambda *p: seen.append(p)})):
    server._mirror_strip(0, "channelOutputMute", True)
check("a strip mute is handed over as that strip's push", seen,
      [(addr(rcp_transfer.channel_key(0)), "channelOutputMute", True, "bool")])
with patched((app_window, {"pushed": None})):
    server._mirror_strip(0, "channelOutputMute", True)      # no window: nothing to hand to

sys.exit(_testkit.finish())
