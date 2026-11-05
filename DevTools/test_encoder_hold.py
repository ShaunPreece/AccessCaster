"""encoder_hold.Watcher's decisions: a hold while a channel button holds the encoder mutes that
channel, once per hold, and nothing else does. NO DEVICE: the follower is never started, presses are
fed to it directly, and the selected channel is pushed into the desk model.
"""
from __future__ import annotations

import sys
import time

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
import desk_live
import desk_model
import encoder_hold
from _testkit import check, push


def v_bool(on: bool) -> bytes:
    return bytes([0x01, 0x01, 0x02 if on else 0x03])


def watcher():
    held = []
    desk_model._reset()
    return encoder_hold.Watcher(held.append), held


def select(strip):
    """The desk pushing the channel a channel button has given the encoder."""
    desk_model.apply_push((15,), "systemChannelSelected", strip, "int")


def hold(w, seconds):
    """The encoder pressed `seconds` ago and still down."""
    w._push(desk_live.decode(push([6], "encoderPressed", v_bool(True))))
    w._down_at = time.monotonic() - seconds
    w._check_hold()


print("ENCODER HOLD")
w, held = watcher()
select(0)
hold(w, 1.0)
check("a hold with a channel selected mutes that channel", held, [0])
w._check_hold()
check("once per hold, however long it lasts", held, [0])
w._push(desk_live.decode(push([6], "encoderPressed", v_bool(False))))
hold(w, 1.0)
check("the next hold toggles it again", held, [0, 0])

w, held = watcher()
select(0)
hold(w, 0.3)
check("a short press does nothing", held, [])

w, held = watcher()
select(-1)
hold(w, 1.0)
check("with no channel selected the desk's own hold is left alone", held, [])

w, held = watcher()
hold(w, 1.0)
check("before any selection is known, nothing happens", held, [])

w, held = watcher()
select(4)
select(-1)
hold(w, 1.0)
check("a channel button pressed again hands the encoder back", held, [])

print("\nTHE HOLD'S WRITE TAKES THE MUTE FROM THE DESK MODEL")
import announcer  # noqa: E402
import devices  # noqa: E402
import rcp_read  # noqa: E402
import rcp_transfer  # noqa: E402
import mute_solo  # noqa: E402
import rode_app  # noqa: E402
from _testkit import as_device, hold_desk, patched  # noqa: E402
from _testkit import hold as hold_value  # noqa: E402

reads, writes = [], []
fakes = ((rcp_read, {"read_model_stable":
                     lambda fresh=False: reads.append(fresh) or desk_model.snapshot()}),
         (rcp_transfer, {"write_at": lambda key, name, kind, on: writes.append((key, on))}),
         (announcer, {"announce_mute": lambda strip, on: None}))
with as_device(devices.PRO2), patched(*fakes):
    hold_desk([0, 1, 2, 3, 10, 11, 7, 8, 9])
    hold_value(rcp_transfer.channel_key(2), "channelOutputMute", True, "bool")
    with patched((rode_app, {"is_open": lambda: False})):
        mute_solo.encoder_hold_mute(2)
    check("a muted channel is unmuted", [on for _key, on in writes], [False])
    check("without reading the desk", reads, [])
    with patched((rode_app, {"is_open": lambda: True})):
        mute_solo.encoder_hold_mute(2)
    check("while RODE's app is open the desk is read first", reads, [True])

sys.exit(_testkit.finish())
