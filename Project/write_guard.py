"""AccessCaster - an offline test must not be able to write to somebody's desk or move their audio.

A docstring saying so is not a guard. `DevTools/_bootstrap` sets ACCESSCASTER_NO_DEVICE_WRITE for
any script named `test_*`, exactly as it redirects the data dir and silences the announcer, so the
rule holds for a suite that never thought about it. Every write choke point calls `refuse_in_tests`
first: a desk write (rcp_hid), a MIDI send (midi_out), and a Windows audio change (meters and
virtual_devices), the last because moving the default output or an endpoint's level moves the
screen reader's speech on the machine running the test.

THE FAILURE IT PREVENTS IS SILENT AND AUDIBLE AT THE SAME TIME: a suite that reaches a real write
fires a pad, engages a voice effect or drops the audio on a desk somebody is working on, and the
test still passes. Raising is the only honest answer - a write that is quietly swallowed would make
the test prove something it never did.

A `test_*_live` suite that means it sets ACCESSCASTER_ALLOW_DEVICE_WRITE. This module imports
nothing of the app's, so midi_out can use it without dragging hidapi in behind it.
"""

from __future__ import annotations

import os

_REFUSING = bool(os.environ.get("ACCESSCASTER_NO_DEVICE_WRITE")) and not os.environ.get(
    "ACCESSCASTER_ALLOW_DEVICE_WRITE")


def refuse_in_tests(what: str) -> None:
    """Raise if this process is an offline test. `what` names the write, as in "play a pad"."""
    if _REFUSING:
        raise RuntimeError(
            f"REFUSING TO {what.upper()}: this is an offline test (ACCESSCASTER_NO_DEVICE_WRITE). "
            f"Stub the call, or set ACCESSCASTER_ALLOW_DEVICE_WRITE if it is a live suite.")
