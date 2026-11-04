"""The pad bank shortcuts (pad_shortcuts.bank_step), offline: they step to the next or previous bank
with a pad on it, skipping empty banks and the system bank, stop at the ends with the reason said,
and change nothing when there is nowhere to go. The desk is replaced: nothing is sent.

Run: python DevTools/test_pad_banks.py
"""
from __future__ import annotations

import contextlib
import sys

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
import devices
import pad_shortcuts
import soundpad
from _testkit import as_device, check, patched

SYSTEM = soundpad.SYSTEM_PAD_TYPE


def pads(*pad_idxs):
    """A pad map with a sound pad at each padIdx, and the system bank's eight pads."""
    out = {i: (i, f"Pad {i}", 1, 0) for i in pad_idxs}
    out.update({i: (i, f"System {i}", SYSTEM, 0) for i in range(56, 64)})
    return out


@contextlib.contextmanager
def desk(bank, pad_map):
    selected = []
    with patched((pad_shortcuts, {"_pad_map": pad_map, "current_bank": lambda: bank,
                                  "_maybe_refresh_pads": lambda: None,
                                  "_release_held_pads": lambda: None}),
                 (soundpad, {"select_bank": selected.append})):
        pad_shortcuts._pads_dirty.clear()
        yield selected


def said(reply):
    return reply["message"]


with as_device(devices.PRO2):
    # Pads on banks 1, 3 and 6 (padIdx 0, 17, 40).
    with desk(0, pads(0, 17, 40)) as selected:
        check("up skips the empty bank 2", said(pad_shortcuts.bank_step("up")), "Bank 3.")
        check("and moves the desk there", selected, [2])
    with desk(2, pads(0, 17, 40)) as selected:
        check("down skips it too", said(pad_shortcuts.bank_step("down")), "Bank 1.")
    with desk(5, pads(0, 17, 40)) as selected:
        check("up at the last bank with pads stops",
              said(pad_shortcuts.bank_step("up")),
              "Bank 6 already, and that is the last bank with pads on it.")
        check("and changes nothing", selected, [])
    with desk(0, pads(0, 17, 40)) as selected:
        check("down at the first stops", said(pad_shortcuts.bank_step("down")),
              "Bank 1 already, and that is the first bank with pads on it.")
    with desk(7, pads(0, 17, 40)) as selected:
        check("from the system bank, down reaches the last bank with pads",
              said(pad_shortcuts.bank_step("down")), "Bank 6.")
    with desk(7, pads(0, 17, 40)) as selected:
        check("and up never reaches the system bank", said(pad_shortcuts.bank_step("up")),
              "No bank after this one has pads on it.")
    with desk(3, pads(0, 17, 40)) as selected:
        check("from an empty bank, up reaches the next with pads",
              said(pad_shortcuts.bank_step("up")), "Bank 6.")
    with desk(0, pads()) as selected:
        reply = pad_shortcuts.bank_step("up")
        check("with no pads anywhere it says so", (reply["ok"], said(reply)),
              (False, "There are no pads on any bank yet."))
        check("and changes nothing", selected, [])

with as_device(devices.DUO):
    # Six pads a bank: padIdx 7 is bank 2's second pad.
    with desk(0, pads(0, 7)) as selected:
        check("a Duo's banks are counted in sixes", said(pad_shortcuts.bank_step("up")), "Bank 2.")

sys.exit(_testkit.finish())
