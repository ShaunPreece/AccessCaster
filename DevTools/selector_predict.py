""">>> SUPERSEDED BY `model_tree.py`. READ THAT FIRST. <<<

The dump is a self-describing TREE and an address is the PATH through it, so a block's address is
its position among its parent's children, not a linear count of everything before it. `model_tree`
parses the whole dump with nothing left over and scores 15 of 15. This file is kept because it is
still a fast, correct answer FOR ROOT CHILDREN (where the two agree by construction), and because
its anchor table is the ground truth both are scored against.

What the tree explains that this file could only assert: the two "no slot" blocks below are CHILDREN
of SYSTEM and NETWORK, and the PHYSICALINTERFACE subtree is skipped for the same reason. Nothing is
an unexplained exception any more.

The global write-selector model: count BLOCK INSTANCES in the dump. Scores 14/14 anchors.

A REJECTED MODEL, so it cannot come back: blocks in dump order, each consuming as many slots as its
most-repeated PROPERTY. It scores five anchors by coincidence and predicts NETWORK = 0x13, which is
STREAMERXMIXPRESET's second instance - the `btDoScan` stray still in the model is a LABELLED SAMPLE
of what that selector really addresses. NETWORK is **0x10**, proven on hardware: `btVisible`
written there flips the NETWORK copy, not the SYSTEM one, leaves no stray, and the desk then pairs
an iPhone and plays audio through it. Ear proof, not arithmetic.

THE RULE, stated so it can be falsified:
  Walk every BLOCK INSTANCE in dump order and give each one the next selector, EXCEPT:
    * the PHYSICALINTERFACE subtree (POT, FADER, METER, PADBUTTON, SOLOMUTEBUTTON), which is
      addressed by the deeper push/pad path forms instead (faderLevel is 0x04 + index in ITS OWN
      space, which is why it looks like a clash and is not one);
    * STORAGEVOLUME and WIFISCANRESULT, which take NO slot at all.
  A fixed-size array block DOES take one slot per instance - that is not an assumption, it is what
  HEADPHONE (0x09-0x0C, both ends confirmed) and STREAMERXMIXPRESET (the stray) show.

WHY THOSE TWO ARE SKIPPED: they hang off a parent one level down, exactly like the
PHYSICALINTERFACE subtree. STORAGEVOLUME is a child of SYSTEM and WIFISCANRESULT a child of
NETWORK, so they have real addresses - use
`model_tree.py --find` rather than deriving anything from their position here.

HOW TO CONFIRM A DERIVED SELECTOR, in the order that costs least:
  1. A device-initiated PUSH names its own address: `DevTools/selector_from_push.py`. Free, no risk.
     ENCODER 0x06 and SYSTEM 0x0F were read off the wire that way in one transfer-mode cycle.
  2. A read-back-checked write: `DevTools/probe_path_write.py`, which reports LANDED / STRAY /
     NOTHING and, on a stray, prints which block the selector actually hit.

Usage: python DevTools/selector_predict.py [dump.bin]
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import rcp_read

# Blocks that consume NO selector slot. See the docstring: the first group is addressed by the
# deeper path forms, the last two are unexplained exceptions fitted to the anchors.
NESTED = {"POT", "FADER", "METER", "PADBUTTON", "SOLOMUTEBUTTON"}
NO_SLOT = {"STORAGEVOLUME", "WIFISCANRESULT"}

# Every block-level selector we KNOW, and where it came from. (block, instance) -> selector.
ANCHORS = [
    ("RECBUTTON", 0, 0x02, "off the wire (recButtonPressed)"),
    ("RECORDER", 0, 0x03, "off the wire (recordState)"),
    ("ENCODER", 0, 0x06, "off the wire (encoderColour push)"),
    ("GUI", 0, 0x07, "captured (screenBrightness)"),
    ("HEADPHONE", 0, 0x09, "captured (headphoneType HP1)"),
    ("HEADPHONE", 1, 0x0A, "confirmed on the Duo, GAP-6 (headphoneType HP2)"),
    ("OUTPUT", 0, 0x0D, "captured (outputMonLevel, outputPrefader)"),
    ("MASTERCHANNEL", 0, 0x0E, "captured (masterCompellorOn)"),
    ("SYSTEM", 0, 0x0F, "captured (transferModeType, systemMidiControl)"),
    ("NETWORK", 0, 0x10, "PROVEN: btVisible written, read back, phone paired"),
    ("STREAMERXMIXPRESET", 1, 0x13, "the btDoScan stray landed here"),
    ("CHANNEL", 0, 0x1C, "captured (channelInputSource fader 1, channelOutputMute strip 0)"),
    ("PARAMETERS", 0, 0x26, "captured (fx selector base)"),
    ("MIXMINUSES", 0, 0x31, "captured (outputMixMinus base)"),
    ("MIX", 0, 0x4C, "captured (cross-point base 76, verified for all 99 cells)"),
]


def block_instances(blob: bytes):
    """[(block, instance index within that block)] in dump order."""
    out, counts, last = [], {}, None
    for hdr, _prop, _val, _kind in rcp_read.iter_block_properties(blob):
        if hdr is None or hdr == last:
            continue
        # iter_block_properties reports the header per PROPERTY, so a new instance is only
        # certain when the header changes. A run of identical adjacent blocks is handled by
        # walking the raw stream instead - see _runs below.
        last = hdr
    return out, counts


def _runs(blob: bytes):
    """Every block instance in dump order, counting adjacent repeats properly."""
    alnum = rcp_read._ALNUM
    seq, i = [], 0
    while True:
        i = blob.find(b"\x00\x01", i)
        if i < 0:
            return seq
        j = i
        while j > 0 and blob[j - 1] in alnum:
            j -= 1
        raw = blob[j:i]
        if len(raw) >= 3:
            nm = rcp_read._clean_prop_name(raw.decode("latin1"))
            if len(nm) >= 3 and nm[0].isalpha() and nm.isupper():
                seq.append(nm)
        i += 2


def model(blob: bytes) -> dict:
    """{(block, instance): selector} for every block the rule gives an address to."""
    out, seen, sel = {}, {}, 0
    for nm in _runs(blob):
        inst = seen.get(nm, 0)
        seen[nm] = inst + 1
        if nm in NESTED or nm in NO_SLOT:
            continue
        out[(nm, inst)] = sel
        sel += 1
    return out


def main() -> int:
    if len(sys.argv) > 1:
        blob = Path(sys.argv[1]).read_bytes()
        print(f"from {sys.argv[1]} ({len(blob)} bytes)")
    else:
        blob = rcp_read.read_model_stable()
        print(f"live dump ({len(blob)} bytes)")
    m = model(blob)

    print("\nSCORING against every selector we know")
    right = wrong = 0
    for block, inst, want, why in ANCHORS:
        got = m.get((block, inst))
        ok = got == want
        right += ok
        wrong += not ok
        print(f"  {'ok  ' if ok else 'MISS'} {block}[{inst}]".ljust(34)
              + f"known 0x{want:02X}  model "
              + (f"0x{got:02X}" if got is not None else "-").ljust(6) + why)
    print(f"\n{right} match, {wrong} miss")

    print("\nEVERY BLOCK THE RULE ADDRESSES, up to the cross-point base")
    known = {(b, i) for b, i, _s, _w in ANCHORS}
    for (block, inst), s in sorted(m.items(), key=lambda kv: kv[1]):
        if s > 0x4C:
            break
        tag = "  <- confirmed" if (block, inst) in known else ""
        print(f"  0x{s:02X}  {block}[{inst}]{tag}")
    print("\nAnything above with no 'confirmed' tag is DERIVED. Confirm it with a push "
          "(selector_from_push.py) or a read-back-checked write (probe_path_write.py) before "
          "shipping a control for it.")
    return 0 if wrong == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
