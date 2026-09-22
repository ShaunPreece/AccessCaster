"""Derive a property's WRITE SELECTOR from the model dump, instead of capturing it.

  python DevTools/selector_map.py              # read the desk, print the map + what it unlocks
  python DevTools/selector_map.py <file.bin>   # same, from a saved dump

THE RULE (established). In the model stream every property sits under an ALL-CAPS
SECTION HEADER, and the header identifies the BLOCK the property belongs to. A write selector
addresses that BLOCK; the property NAME picks the field inside it. So header -> selector, and once
one property in a block has a known selector, every single-instance property under that header
shares it.

Checked against all 17 properties whose selector we already knew, captured or long-established:
  GUI -> 0x07, OUTPUT -> 0x0D, MASTERCHANNEL -> 0x0E, SYSTEM -> 0x0F.
Zero disagreements. Then CONFIRMED PREDICTIVELY on hardware: `activeButtonsBrightness` (GUI, never
captured) was written on the derived 0x07, read back as written, and restored.

WHY THIS MATTERS. Before this, every unwritable property needed its own USB capture of the RODE app
writing it - and most of them have no app control at all, so they could never be captured. This
turns those into a read-back test instead.

STILL REQUIRED, per property, before trusting a write:
  * The block must be one we have a CONFIRMED selector for. A header with no known member (e.g.
    TEST, which holds toneGeneration and allLEDSWhite) gives us nothing - do NOT guess it.
  * The property must be single-instance. Indexed blocks (CHANNEL, PAD, MIX, INPUTSOURCE) address
    per-instance as base + index, which is a different formula, already documented per block.
  * READ BACK after writing. The rule predicts the block, not that a given field is writable, and a
    write that lands nowhere can append a stray property that only a factory reset clears.
Pick something harmless for the first write on any newly-derived block.
"""
from __future__ import annotations
import sys
from collections import Counter

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import rcp_read

# Header -> selector, each anchored by at least one property whose selector is confirmed.
HEADER_SELECTOR = {
    "RECBUTTON": 0x02, # recButtonPressed (read off the wire)
    "RECORDER": 0x03, # recordState / requestRecordState (off the wire)
    "ENCODER": 0x06,        # encoderColour (device push, decoded by selector_from_push.py)
    "GUI": 0x07, # screenBrightness / metering / screenDimAfterSeconds (captured)
    "OUTPUT": 0x0D,         # recordingMultitrackMode, outputMonLevel, outputPrefader
    "MASTERCHANNEL": 0x0E,  # masterCompellorOn + its params
    "SYSTEM": 0x0F,         # systemMidiControl, transferModeType, systemHapticSetting
    # NETWORK IS 0x10, NOT 0x13: btVisible written on 0x10 reads back in the NETWORK block (not the
    # SYSTEM duplicate), leaves no stray, and a phone then pairs and plays audio through the desk.
    # DevTools/selector_predict.py holds the rejected 0x13 model and the stray that disproves it.
    # TWO CAUTIONS specific to this block, neither of them about the address:
    #   * many of its fields are ACTIONS, not settings (btDoScan, btDoPair, btDoConnect, btDoUnPair,
    #     wifiScan). A read-back may show nothing changed even when the action ran, so judge those by
    #     what the RADIO does, not by the field.
    #   * wifiPSK is a password. It is readable and must never be logged, echoed to the browser, or
    #     put in a profile.
    "NETWORK": 0x10,
}
# The properties the map is checked against, so a regression shows up as a disagreement.
ANCHORS = {
    "screenBrightness": 0x07, "autoBrightness": 0x07, "inactiveButtonsBrightness": 0x07,
    "screenDimAfterSeconds": 0x07, "metering": 0x07, "activeButtonsBrightness": 0x07,
    "outputPrefader": 0x0D, "outputMonFixed": 0x0D, "recordingMultitrackMode": 0x0D,
    "outputMonAutoMute": 0x0D, "outputBTAutoMute": 0x0D, "outputMonLevel": 0x0D,
    "masterCompellorOn": 0x0E,
    "systemMidiControl": 0x0F, "systemChannelSelected": 0x0F, "transferModeType": 0x0F,
    "systemHapticSetting": 0x0F, "systemRecButtonSetting": 0x0F,
}
_ALNUM = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")


def sections(blob: bytes) -> tuple[dict, Counter]:
    """{property: its section header} and how many times each property occurs."""
    header, where, count = None, {}, Counter()
    i = 0
    while True:
        i = blob.find(b"\x00\x01", i)
        if i < 0:
            return where, count
        j = i
        while j > 0 and blob[j - 1] in _ALNUM:
            j -= 1
        raw = blob[j:i].decode("latin1")
        if len(raw) >= 3:
            name = rcp_read._clean_prop_name(raw)
            if len(name) >= 3 and name[0].isalpha():
                if name.isupper():
                    header = name
                else:
                    where.setdefault(name, header)
                    count[name] += 1
        i += 2


def main() -> None:
    if len(sys.argv) > 1:
        blob = open(sys.argv[1], "rb").read()
    else:
        blob = rcp_read.read_model_stable()
    where, count = sections(blob)

    print("ANCHOR CHECK (every property whose selector we already know)")
    bad = 0
    for prop, sel in sorted(ANCHORS.items()):
        h = where.get(prop)
        want = HEADER_SELECTOR.get(h)
        ok = want == sel
        bad += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {prop:<28} header {str(h):<14} "
              f"selector 0x{sel:02X}" + ("" if ok else f"  (map says {want})"))
    print(f"\ndisagreements: {bad}\n")

    print("HEADER -> SELECTOR")
    for h, s in sorted(HEADER_SELECTOR.items()):
        n = sum(1 for p, hh in where.items() if hh == h and count[p] == 1)
        print(f"  {h:<16} 0x{s:02X}   {n} single-instance properties in this block")

    known = set(ANCHORS)
    print("\nDERIVABLE (single-instance, in a block with a confirmed selector, no writer yet)")
    out = []
    for prop, h in sorted(where.items()):
        if h in HEADER_SELECTOR and count[prop] == 1 and prop not in known:
            out.append((prop, h, HEADER_SELECTOR[h]))
    for prop, h, s in out:
        print(f"  {prop:<32} {h:<14} 0x{s:02X}")
    print(f"\n{len(out)} properties addressable without any further capture. READ BACK EVERY WRITE.")

    unknown = sorted({h for p, h in where.items()
                      if h and h not in HEADER_SELECTOR and count[p] == 1})
    print(f"\nBLOCKS WITH NO CONFIRMED SELECTOR ({len(unknown)}) - do not guess these:")
    print("  " + ", ".join(unknown))


if __name__ == "__main__":
    main()
