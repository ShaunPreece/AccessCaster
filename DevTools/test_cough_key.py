"""Offline test of the COUGH KEY (server._hk_mics). NO DEVICE: the layout and mutes are held.

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
import channels
import devices
import server
from _testkit import as_device, check, patched

# The Pro II's factory layout with CallMe on fader 4, so Mic 4 (source 3) is on no fader.
NO_MIC4 = [0, 1, 2, 16, 10, 11, 7, 8, 9]


def press(muted: dict) -> tuple[str, list]:
    """One press against the cached mutes `muted` ({strip: on}): what is spoken and written."""
    writes = []

    def set_mute(name, on):
        writes.append((name, on))
        return {"ok": True}

    with patched((server, {"_ann_live_state": {"muted": muted},
                           "action_set_channel_mute": set_mute})):
        said = server._hk_mics()["message"]
    return said, writes


def main() -> int:
    with as_device(devices.PRO2), patched((server, {"_hk_muted_mics": []})):
        channels.publish_strip_vector(NO_MIC4)

        print("A MIC ON NO FADER IS LEFT OUT")
        said, writes = press({0: False, 1: True, 2: False})
        check("press 1 mutes the open mics on faders, and only those",
              writes, [("Mic1", True), ("Mic3", True)])
        check("and says how many", said, "2 mics muted.")
        said, writes = press({0: True, 1: True, 2: True})
        check("press 2 opens exactly what press 1 muted", writes, [("Mic1", False), ("Mic3", False)])

        print("\nEVERY MIC ON A FADER ALREADY MUTED")
        said, writes = press({0: True, 1: True, 2: True})
        check("nothing is written", writes, [])
        check("and it says so", said, "Every mic is already muted.")
    return _testkit.finish()


if __name__ == "__main__":
    sys.exit(main())
