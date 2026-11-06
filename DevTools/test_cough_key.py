"""Offline test of the COUGH KEY (commands._cough_key). NO DEVICE: the layout and mutes are held in
the desk model.

What it pins down: a mic on no fader is left out, not refused. The cough key walks every mic, and a
mic taken off its fader has no strip and so no mute, so one such mic must not stop the key working
for the others. The two presses are still a round trip: the second opens exactly what the first
muted.

Run: python DevTools/test_cough_key.py
"""
from __future__ import annotations

import sys

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
import commands
import devices
import mute_solo
import rcp_read
import rcp_transfer
import rode_app
from _testkit import as_device, check, check_not_in, hold, hold_desk, patched

# The Pro II's factory layout with CallMe on fader 4, so Mic 4 (source 3) is on no fader.
NO_MIC4 = [0, 1, 2, 16, 10, 11, 7, 8, 9]


def press(muted: dict) -> tuple[str, list]:
    """One press against the mutes `muted` ({strip: on}) in the desk model: what is spoken and
    written."""
    writes = []

    def set_mute(name, on):
        writes.append((name, on))
        return {"ok": True}

    for strip, on in muted.items():
        hold(rcp_transfer.channel_key(strip), "channelOutputMute", on, "bool")
    with patched((mute_solo, {"action_set_channel_mute": set_mute})):
        said = commands._cough_key()["message"]
    return said, writes


def main() -> int:
    with as_device(devices.PRO2), patched((commands, {"_muted_mics": []})):
        hold_desk(NO_MIC4)

        print("A MIC ON NO FADER IS LEFT OUT")
        said, writes = press({0: False, 1: True, 2: False})
        check("press 1 mutes the open mics on faders, and only those",
              writes, [("Mic1", True), ("Mic3", True)])
        check("and says how many", said, "2 mics muted.")
        said, writes = press({0: True, 1: True, 2: True})
        check("press 2 opens exactly what press 1 muted", writes, [("Mic1", False), ("Mic3", False)])

        print("\nA SECOND PRESS THAT FAILS PART-WAY KEEPS WHAT IT DID NOT OPEN")
        opened = []

        def open_until_second(name, on):
            opened.append(name)
            return {"ok": len(opened) == 1}

        commands._muted_mics = ["Mic1", "Mic2", "Mic3"]
        with patched((mute_solo, {"action_set_channel_mute": open_until_second})):
            res = commands._cough_key()
        check("the press says it failed", res["ok"], False)
        check("the mics not yet opened are held for the next press", commands._muted_mics,
              ["Mic2", "Mic3"])
        commands._muted_mics = []

        print("\nEVERY MIC ON A FADER ALREADY MUTED")
        said, writes = press({0: True, 1: True, 2: True})
        check("nothing is written", writes, [])
        check("and it says so", said, "Every mic is already muted.")

        print("\nEACH MUTE'S REPLY READS THE MODEL, NOT THE DESK")
        # The reply checks every output for one that still hears the channel. A desk read per
        # mic made a three-mic cough key take a second and a half.
        reads = []
        with patched((rcp_read, {"read_model_stable": lambda fresh=False: reads.append(fresh)}),
                     (rode_app, {"is_open": lambda: False})):
            said = mute_solo._mute_reach_message("Mic 2", [1])
        check("a trusted model answers it", reads, [])
        check("and the reply is a real one", said.startswith("Mic 2 muted"), True)
        check_not_in("not the could-not-check one", "Could not check", said)
    return _testkit.finish()


if __name__ == "__main__":
    sys.exit(main())
