"""Unit tests for the ADDRESSED input read. NO DEVICE, no HID, no writes.

The per-input values (mic gain, phantom, polarity, mic type, line level, ...) are read and written at
ONE address on every desk: INPUTSOURCE[source_code], derived by DeviceModel.input_path.

Two earlier readings of this are what the tests pin. First, the values were read by POSITION out of a
values() list, which reported one Duo mic's gain as another's. Then they were read at a per-device
"override" address that fell back to the table - and on a Duo that override was a MIX node the audio
engine never reads, so the app wrote a copy nothing listened to, read the same copy back, and
reported success while changing nothing. Hence two groups of checks below that look like belt and
braces and are not: every retired Pro II selector is asserted against the derivation, and a stale
Duo MIX copy is asserted to be IGNORED.

These tests build synthetic model dumps rather than using a captured one: a real dump carries the
wifi PSK, and a synthetic tree can put a value in exactly the awkward place that broke the old read.

Run: python DevTools/test_input_read.py
"""
from __future__ import annotations
import sys

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import channels as c
import devices
import model_tree
import rcp_read as rr

FAILED = 0


def check(label, got, want):
    global FAILED
    if got == want:
        print(f"  ok   {label}")
    else:
        FAILED += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


# --- A minimal encoder for the model grammar --------------------------------------------------
# model_tree only parses, so the tests need the other direction. The grammar is small enough that
# this is the whole of it (see model_tree's header):
#     node := NAME \0 count(P) property x P  count(C) node x C
def _count(n: int) -> bytes:
    if n == 0:
        return b"\x00"
    ln = 1 if n <= 0xFF else 2 if n <= 0xFFFF else 4
    return bytes([ln]) + n.to_bytes(ln, "little")


def _prop(name: str, value) -> bytes:
    if isinstance(value, bool):
        payload = bytes([0x02 if value else 0x03])
    elif isinstance(value, int):
        payload = b"\x01" + value.to_bytes(4, "little", signed=True)
    elif isinstance(value, str):
        payload = b"\x05" + value.encode("latin1") + b"\x00"
    else:
        raise TypeError(f"no encoding for {value!r}")
    return name.encode("latin1") + b"\x00" + _count(len(payload)) + payload


def node(name: str, props: dict | None = None, kids: list | None = None) -> bytes:
    props, kids = props or {}, kids or []
    out = name.encode("latin1") + b"\x00" + _count(len(props))
    for k, v in props.items():
        out += _prop(k, v)
    out += _count(len(kids))
    return out + b"".join(kids)


def dump(children: dict, n_children: int) -> bytes:
    """A model blob whose root has `n_children` kids, those in `children` carrying properties.

    `children` is {child index: (block name, {property: value})}. Everything else is an empty
    placeholder, because a child's INDEX is its address and the indices have to line up.
    """
    kids = []
    for i in range(n_children):
        if i in children:
            name, props = children[i]
            kids.append(node(name, props))
        else:
            kids.append(node("FILLER"))
    # read_model strips the 5-byte stream header, but parse() finds the root by name either way.
    return b"\x53\x2f\x02\x00\x02" + node("Rodecaster", {}, kids)


def inputsource_table(dev, per_code: dict) -> dict:
    """The 30-entry INPUTSOURCE table as root children, values keyed by SOURCE CODE."""
    out = {}
    for code in range(30):
        out[dev.input_table_base + code] = ("INPUTSOURCE", per_code.get(code, {}))
    return out


PRO2, DUO = devices.PRO2, devices.DUO
N_KIDS = 0x2A0          # enough room for the table at 0x26E..0x28B


def ch_of(dev, name):
    return next(c for c in dev.ui_channels if c["name"] == name)


print("THE SANITY CHECK: the synthetic dump parses and addresses resolve")
blob = dump(inputsource_table(PRO2, {0: {"inputMicrophoneGain": 44}}), N_KIDS)
root, _used, _tail = model_tree.parse(blob)
check("the tree parses", root.name, "Rodecaster")
check("a node resolves at its address", rr.node_at(blob, [PRO2.input_table_base]).name, "INPUTSOURCE")
check("and carries its property", rr.path_value(blob, [PRO2.input_table_base], "inputMicrophoneGain"), 44)
check("an address with no node returns the default", rr.path_value(blob, [0x9999], "anything", "d"), "d")

print("\nMISSING TELLS 'ABSENT' APART FROM A REAL None / 0 / False")
b = dump({0: ("MIX", {"inputPower": 0, "inputPhaseFlip": False})}, 4)
check("a real 0 is returned, not treated as absent", rr.path_value(b, [0], "inputPower", rr.MISSING), 0)
check("a real False likewise", rr.path_value(b, [0], "inputPhaseFlip", rr.MISSING), False)
check("an absent property gives the sentinel", rr.path_value(b, [0], "inputColour", rr.MISSING), rr.MISSING)

print("\nPRO II: the write address IS the table entry, so one lookup answers")
blob = dump(inputsource_table(PRO2, {
    0: {"inputMicrophoneGain": 41}, 1: {"inputMicrophoneGain": 42},
    2: {"inputMicrophoneGain": 43}, 3: {"inputMicrophoneGain": 44},
    7: {"inputDigitalGain": -7}, 10: {"inputDigitalGain": -10},
}), N_KIDS)
for name, want in [("Mic1", 41), ("Mic2", 42), ("Mic3", 43), ("Mic4", 44)]:
    check(f"{name} reads its own gain", c.input_value(blob, ch_of(PRO2, name), "inputMicrophoneGain", PRO2), want)
check("USB1 reads its own level", c.input_value(blob, ch_of(PRO2, "USB1"), "inputDigitalGain", PRO2), -7)
check("Bluetooth reads its own level", c.input_value(blob, ch_of(PRO2, "Bluetooth"), "inputDigitalGain", PRO2), -10)
print("\nTHE RETIRED PRO II SELECTORS, REPRODUCED EXACTLY BY THE DERIVATION")
# THE SAFETY NET FOR SHAUN'S OWN DESK. Two hand-kept tables were deleted when the Duo's gain turned
# out to be addressed wrongly; on the Pro II they were RIGHT, and their every value is asserted here
# so the tidy-up provably cannot have moved a Pro II write by one byte.
RETIRED_PRO2 = {"Mic1": 0x6E, "Mic2": 0x6F, "Mic3": 0x70, "Mic4": 0x71,       # mic_channel_sel
                "USB1": 0x75, "Chat": 0x76, "USB2": 0x77, "Bluetooth": 0x78}  # digital_gain_sel
for name, sel in RETIRED_PRO2.items():
    check(f"Pro II {name} still addresses 0x{sel:02X} with the 0x02 high byte",
          PRO2.input_path(ch_of(PRO2, name)), [sel | (0x02 << 8)])

print("\nDUO: THE SAME RULE, WHICH IS THE FIX")
# 0x48 + source_code with a 0x01 high byte is real and writable on a Duo, and DEAD: the audio
# engine never reads it. Nothing may address it.
for name, dead_sel in (("Mic1", 0x48), ("Mic2", 0x49), ("Headset", 0x4A),
                       ("USB1", 0x4F), ("Chat", 0x50), ("USB2", 0x51), ("Bluetooth", 0x52)):
    ch = ch_of(DUO, name)
    check(f"Duo {name} is addressed at INPUTSOURCE, not the dead MIX node",
          DUO.input_path(ch), [DUO.input_table_base + ch["source_code"]])
    check(f"Duo {name} does NOT address 0x{dead_sel:02X}",
          DUO.input_path(ch) == [dead_sel | (0x01 << 8)], False)
check("both desks derive the SAME address for a shared source code",
      DUO.input_path(ch_of(DUO, "USB1")), PRO2.input_path(ch_of(PRO2, "USB1")))

print("\nDUO: every input reads from the table")
factory = inputsource_table(DUO, {
    0: {"inputMicrophoneGain": 50}, 1: {"inputMicrophoneGain": 50},
    2: {"inputDigitalGain": 0}, 7: {"inputDigitalGain": -12}, 10: {"inputDigitalGain": -12},
})
for sel in (0x48, 0x49, 0x4A, 0x4F, 0x52):
    factory[sel | (0x01 << 8)] = ("MIX", {"mixMute": False})
blob = dump(factory, N_KIDS)
check("Mic 1 reads the table", c.input_value(blob, ch_of(DUO, "Mic1"), "inputMicrophoneGain", DUO), 50)
check("Mic 2 reads the table", c.input_value(blob, ch_of(DUO, "Mic2"), "inputMicrophoneGain", DUO), 50)
check("USB 1 reads the table", c.input_value(blob, ch_of(DUO, "USB1"), "inputDigitalGain", DUO), -12)

print("\nDUO: THE REGRESSION. A STALE MIX COPY MUST NOT BE READ")
# Every Duo that this app has ever written to carries these, and they cannot be removed without a
# factory reset. They are an INERT copy: reading them back after a write reports the app's own last
# write as the desk's setting, so the display goes stale the moment anyone touches the desk. That is
# why the refusal is asserted rather than merely left undone. The numbers are measured on scottpc.
stale = dict(factory)
stale[0x148] = ("MIX", {"mixMute": False, "inputMicrophoneGain": 20, "inputMicrophoneType": 4})
stale[0x149] = ("MIX", {"mixMute": False, "inputMicrophoneGain": 33, "inputPhaseFlip": True})
stale[0x14A] = ("MIX", {"mixMute": False, "inputDigitalGain": -30})
blob = dump(stale, N_KIDS)
check("Mic 1 ignores its stale copy", c.input_value(blob, ch_of(DUO, "Mic1"), "inputMicrophoneGain", DUO), 50)
check("Mic 2 ignores its stale copy", c.input_value(blob, ch_of(DUO, "Mic2"), "inputMicrophoneGain", DUO), 50)
check("Headset ignores its stale level", c.input_value(blob, ch_of(DUO, "Headset"), "inputDigitalGain", DUO), 0)
check("a stale copy of a property the table lacks reads as absent, not as the stale value",
      c.input_value(blob, ch_of(DUO, "Mic2"), "inputPhaseFlip", DUO), None)
flat = rr.values(blob, "inputMicrophoneGain")
check("the stale copies really do come FIRST in stream order", flat[0], 20)
check("so a positional read would still be wrong", flat[0] != 50, True)

print("\nA PROPERTY THE ADDRESS DOES NOT CARRY READS AS None, NOT AS A WRONG NUMBER")
blob = dump(factory, N_KIDS)
check("no inputColour anywhere", c.input_value(blob, ch_of(DUO, "Mic1"), "inputColour", DUO), None)
check("a bus has no table gain",
      c.input_value(blob, ch_of(PRO2, "SMARTpads"), "inputDigitalGain", PRO2), None)

print("\nSOURCE CODE IS THE ONE PER-CHANNEL NUMBER")
for dev in (PRO2, DUO):
    mics = [e for e in dev.ui_channels if e["kind"] == "mic"]
    check(f"{dev.name}: mic channel numbers are source_code + 1",
          [c.mic_channel_number(e) for e in mics], [e["source_code"] + 1 for e in mics])
    check(f"{dev.name}: every channel's address round-trips back to itself",
          [(dev.channel_for_input_path(dev.input_path(e)) or {}).get("name") for e in dev.ui_channels],
          [e["name"] for e in dev.ui_channels])
    check(f"{dev.name}: no entry carries a stale input_slot",
          any("input_slot" in e for e in dev.ui_channels), False)

print()
if FAILED:
    print(f"{FAILED} CHECK(S) FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
