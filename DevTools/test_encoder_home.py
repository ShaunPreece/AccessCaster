"""A lost encoder put back on the virtual faders and monitor (Project/encoder_home.py), offline: a
turn nothing follows does it, a turn a level follows does not, nor one while a channel button holds
the encoder, nor one that may be against the end of a range, and it is not repeated within the gap.
Nothing touches a desk.

Run: python DevTools/test_encoder_home.py
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
import desk_model
import devices
import encoder_home
from _testkit import as_device, check, patched

NOW = [100.0]
FIXES = []


def turn(step):
    return SimpleNamespace(name="encoderSignal", kind="pair", value=[(step, "int"), (True, "bool")])


def level():
    return SimpleNamespace(name="outputMonLevel", kind="float", value=0.5)


def watcher():
    return encoder_home.Watcher(lambda: FIXES.append(1))


def later(w, seconds):
    NOW[0] += seconds
    w._check()


def desk(selected=-1, faders=0.5, monitor=0.5):
    return patched(
        (encoder_home.time, {"monotonic": lambda: NOW[0]}),
        (desk_model, {"selected_channel": lambda: selected,
                      "fader_level": lambda _i: faders, "monitor_level": lambda: monitor}))


with as_device(devices.PRO2):
    with desk():
        w = watcher()
        w._push(turn(1))
        later(w, 0.2)
        check("nothing is done inside the wait", FIXES, [])
        later(w, 0.3)
        check("a turn nothing follows puts the encoder back", FIXES, [1])
        w._push(turn(1))
        later(w, 0.5)
        check("not again within the gap", FIXES, [1])
        NOW[0] += encoder_home.GAP_S
        w._push(turn(-1))
        later(w, 0.5)
        check("but again after it", FIXES, [1, 1])

    FIXES.clear()
    with desk():
        w = watcher()
        w._push(turn(1))
        w._push(level())
        later(w, 0.5)
        check("a turn a level follows is left alone", FIXES, [])

    with desk(selected=0):
        w = watcher()
        w._push(turn(1))
        later(w, 0.5)
        check("a turn while a channel button holds the encoder is left alone", FIXES, [])

    with desk(selected=None):
        w = watcher()
        w._push(turn(1))
        later(w, 0.5)
        check("and one before the selection is known", FIXES, [])

    with desk(monitor=0.0):
        w = watcher()
        w._push(turn(-1))
        later(w, 0.5)
        check("a turn down with a level at 0 may be against the stop", FIXES, [])
        w._push(turn(1))
        later(w, 0.5)
        check("a turn up from there is not", FIXES, [1])

    FIXES.clear()
    with desk(faders=1.0):
        w = watcher()
        w._push(turn(1))
        later(w, 0.5)
        check("a turn up with a level at full may be against the stop", FIXES, [])

    with desk():
        w = watcher()
        w._push(turn(1))
        w._lost()
        later(w, 0.5)
        check("a turn before the desk went away is forgotten", FIXES, [])

sys.exit(_testkit.finish())
