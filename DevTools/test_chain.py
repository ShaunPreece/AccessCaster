"""Unit tests for the per-strip CHAIN diff and its write ORDER. NO DEVICE, no HID, no writes.

THE ORDER IS LOAD-BEARING: in SIMPLE mode the desk derives most of the chain from the three VoxLab
knobs, so a derived value written BEFORE a knob is recomputed away and the swap fails on the desk
while every write reports success. These tests pin the rules down:
  mode first -> knobs -> neutral params -> derived family -> preset marker LAST.

Run: python DevTools/test_chain.py
"""
from __future__ import annotations
import sys

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import channels as c
import devices

# PIN THE DESK AND ITS FADER LAYOUT. `proc_selector` resolves a channel to the strip it is sitting
# on RIGHT NOW, so without this the order checks below read the attached desk's live layout and
# fail on any desk that has Mic 2 off its fader - which says nothing about the write order this
# file exists to pin. Offline tests must not depend on what happens to be plugged in.
devices.force_device(devices.PRO2)
c.publish_strip_vector(list(devices.PRO2.fader.default_vector))

FAILED = 0


def check(label, got, want):
    global FAILED
    if got == want:
        print(f"  ok   {label}")
    else:
        FAILED += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def chain(**over):
    """A full chain dict with every value at a known baseline, plus overrides."""
    ch = {
        "bools": {p: False for p in c.CHAIN_BOOL_PROPS},
        "floats": {p: 0.5 for p in c.CHAIN_FLOAT_PROPS},
        "fx_bools": {p: False for p in c.CHAIN_FX_BOOL_PROPS},
        "fx_floats": {p: 0.5 for p in c.CHAIN_FX_FLOAT_PROPS},
        "marker": 1,
    }
    for group, vals in over.items():
        if group == "marker":
            ch["marker"] = vals
        else:
            ch[group].update(vals)
    return ch


print("NOTHING TO DO WHEN THE CHAINS MATCH")
a = chain()
check("identical chains produce no writes", c.chain_diff(a, a), [])
check("a full write when there is no current", len(c.chain_diff(a, None)),
      len(c.CHAIN_BOOL_PROPS) + len(c.CHAIN_FLOAT_PROPS) + len(c.CHAIN_FX_BOOL_PROPS)
      + len(c.CHAIN_FX_FLOAT_PROPS) + 1)

print("\nFLOAT EPSILON: DEVICE ROUNDING IS NOT A CHANGE")
near = chain(floats={"eqLowGain": 0.5 + 1e-9})
check("a float32-rounding difference is ignored", c.chain_diff(near, a), [])
real = chain(floats={"eqLowGain": 0.6})
check("a real difference is written", [p for _k, p, _v in c.chain_diff(real, a)], ["eqLowGain"])

print("\nWRITE ORDER")
full = c.chain_diff(chain(bools={c.CHAIN_ADVANCED_PROP: True},
                          floats={p: 0.9 for p in c.CHAIN_FLOAT_PROPS},
                          marker=2), a)
names = [p for _k, p, _v in full]
check("processing MODE is first", names[0], c.CHAIN_ADVANCED_PROP)
check("preset marker is last", names[-1], c.CHAIN_PRESET_MARKER_PROP)
for knob in c.VOXLAB_KNOB_PROPS:
    for derived in c.DERIVED_FLOAT_PROPS:
        if names.index(knob) > names.index(derived):
            check(f"{knob} before {derived}", False, True)
            break
    else:
        continue
    break
else:
    check("every VoxLab knob is written before every derived param", True, True)
check("derived params sit after the neutral ones",
      names.index("aphexAEMix") > names.index("hpfSlope"), True)

print("\nONLY THE DIFFERENCES")
one = chain(floats={"channelDepth": 0.8})
check("a single changed knob is one write", [p for _k, p, _v in c.chain_diff(one, a)],
      ["channelDepth"])
check("marker-only change is one write", [p for _k, p, _v in c.chain_diff(chain(marker=2), a)],
      [c.CHAIN_PRESET_MARKER_PROP])

print("\nMISSING VALUES ARE SKIPPED, NOT WRITTEN AS None")
holey = chain()
holey["floats"]["eqLowGain"] = None
holey["bools"][c.CHAIN_ADVANCED_PROP] = None
check("None values produce no write", c.chain_diff(holey, a), [])

print("\nTHE CHAIN COVERS WHAT PHASE 2 PROVED MOVES")
check("advanced flag is in the chain", c.CHAIN_ADVANCED_PROP in c.CHAIN_BOOL_PROPS, True)
check("bypass is in the chain", "channelBypassProcessing" in c.CHAIN_BOOL_PROPS, True)
check("the FX block is in the chain",
      ("echoOn" in c.CHAIN_FX_BOOL_PROPS and "echoMix" in c.CHAIN_FX_FLOAT_PROPS), True)
check("the 36 params are in the chain", len(c.CHAIN_FLOAT_PROPS), 36)
# Q IS AN INDEPENDENT, WRITABLE PARAMETER, so the chain has to carry it or moving a channel to
# another fader quietly reshapes its EQ. The tempting inference the other way - the RODE app never
# sends Q, yet Q moves in the model diffs, so the device must recompute it - is false. Measured on
# hardware: writing a band's gain moves no Q, writing a band's frequency moves no Q, and writing Q
# directly lands and reads back exactly.
check("all three Q values ARE in the chain, because Q is independent and writable",
      sorted(p for p in c.CHAIN_FLOAT_PROPS if p.endswith("Q")),
      ["eqHighQ", "eqLowQ", "eqMidQ"])


# ---------------------------------------------------------------------------------------------
# BOTH LAYOUT PATHS MUST CARRY THE CHAIN. action_set_fader (move one fader) is the obvious one;
# action_reset_faders (back to the default layout) is the one that gets missed, and it moves EVERY
# channel at once, so it is the worst place to lack it - a reset without it silently leaves each
# chain on its old strip and hands USB 1 a mic voicing with the gate on. No device: the layout read,
# the chain read/write and the fader write are all stubbed.
# ---------------------------------------------------------------------------------------------
import devices                                                        # noqa: E402
import rcp_read                                                       # noqa: E402
import rcp_transfer                                                   # noqa: E402
import server                                                         # noqa: E402

PRO2_DEFAULT = list(devices.PRO2_FADERS.default_vector)   # (0,1,2,3,10,11,7,8,9)
# The layout Shaun actually ran: USB 1 (code 7) pulled onto strip 1 so a real fader controls his PC
# audio, which pushed Mic 2 (code 1) down to strip 6. Resetting from here is the exact move that
# started his bed tails gating again: the default sends USB 1 to strip 6, where Mic 2's voicing was.
SHAUN_VEC = [0, 7, 2, 3, 10, 11, 1, 8, 9, 9]
GATED_STRIP = 6                                           # where Mic 2's voicing sits before the reset


class fake_desk:
    """Stand in for the desk: a layout plus one chain per strip, recording every write."""

    def __init__(self, vec, gated_strip=GATED_STRIP):
        self.vec = list(vec)
        # One strip carries a MIC voicing with the noise gate ON; the rest are neutral. If the
        # chains do NOT follow, whoever lands on that strip inherits the gate - the actual symptom.
        self.chains = [chain(bools={"noiseGateOn": True}) if i == gated_strip else chain()
                       for i in range(len(vec))]
        self.fader_writes = []
        self.chain_writes = []

    def __enter__(self):
        self.saved = {}

        def save(mod, name, fn):
            self.saved[(mod, name)] = getattr(mod, name)
            setattr(mod, name, fn)

        save(devices, "get_active_device", lambda: devices.PRO2)
        save(rcp_read, "read_model", lambda *a, **k: b"")
        save(server, "_read_fader_vector", lambda *a, **k: list(self.vec))
        save(c, "read_chains", lambda *a, **k: [dict(ch) for ch in self.chains])
        save(c, "invalidate_strip_vector", lambda *a, **k: None)
        save(rcp_transfer, "set_fader_source", self._set_fader)
        save(c, "write_chain", self._write_chain)
        return self

    def __exit__(self, *exc):
        for (mod, name), fn in self.saved.items():
            setattr(mod, name, fn)
        return False

    def _set_fader(self, i, code):
        self.fader_writes.append((i, code))
        self.vec[i] = code

    def _write_chain(self, dst, want, current):
        diff = c.chain_diff(want, current)
        self.chain_writes.append((dst, len(diff)))
        self.chains[dst] = dict(want)          # the desk now holds it, so the read-back agrees
        return {"written": len(diff)}


def gate_on(desk, code):
    """Is the noise gate on for whichever strip `code` sits on now?"""
    return desk.chains[desk.vec.index(code)]["bools"]["noiseGateOn"]


print("\nRESET FADERS CARRIES THE CHAIN")
with fake_desk(SHAUN_VEC) as desk:
    res = server.action_reset_faders()
check("the reset succeeds", res.get("ok"), True)
check("it wrote the default layout", [code for _i, code in desk.fader_writes], PRO2_DEFAULT)
check("it reports that the chains moved", "followed" in res["message"], True)
# THE POINT. USB 1 lands on strip 6, where the gated mic voicing sits. Its own chain has to arrive
# with it, or USB 1 comes up gated.
check("USB 1 landed on the strip that held the gated voicing", desk.vec.index(7), GATED_STRIP)
check("USB 1 is NOT gated: its own chain followed it", gate_on(desk, 7), False)
check("and Mic 2's voicing followed Mic 2, gate still on", gate_on(desk, 1), True)

print("\nA RESET THAT CHANGES NOTHING WRITES NO CHAIN")
with fake_desk(PRO2_DEFAULT) as desk:
    server.action_reset_faders()
check("already on the default, so nothing moves", desk.chain_writes, [])

print("\nTHE DUO CARRIES CHAINS TOO NOW (proven on hardware)")
with fake_desk(SHAUN_VEC) as desk:
    devices.get_active_device = lambda: devices.DUO      # inside the block, so it is restored
    res_duo = server.action_reset_faders()
check("a reset moves the chains on a Duo as it does on a Pro II", bool(desk.chain_writes), True)

# ---------------------------------------------------------------------------------------------
# MOVING A CHANNEL ONTO AN EMPTY FADER. A desk with more faders than sources has one spare, and the
# Duo ships that way. The naive swap reports success and does the wrong thing: the displaced
# "channel" is nothing, so the old slot keeps the source, and resolving the duplicate that makes
# overwrites the very slot the user asked for, leaving a CALLER on the empty fader.
# ---------------------------------------------------------------------------------------------
print("\nA CHANNEL MOVES ONTO AN EMPTY FADER, AND VACATES THE ONE IT LEFT")
DUO_FACTORY = [0, 1, 10, 11, 2, 7, 8, 9, -1]
duo_order = [code for code, _ in devices.DUO.fader.assignable]
saved_dev = devices.get_active_device
devices.get_active_device = lambda: devices.DUO
try:
    got = server._tidy_fader_target(DUO_FACTORY, 8, 2, duo_order)
    check("the Headset really lands on the empty fader", got[8], 2)
    check("and the fader it came from is now the empty one", got[4], -1)
    check("no caller is conjured onto a fader", 16 in got, False)
    check("still exactly one empty fader", got.count(-1), 1)
    check("every source still appears exactly once",
          sorted(x for x in got if x >= 0), sorted(x for x in DUO_FACTORY if x >= 0))
    check("an occupied-to-occupied swap is unchanged by the fix",
          server._tidy_fader_target(DUO_FACTORY, 6, 7, duo_order),
          [0, 1, 10, 11, 2, 8, 7, 9, -1])
finally:
    devices.get_active_device = saved_dev

# The Pro II has MORE sources than faders, so it has no empty strip and never met this. Pin it
# anyway: the fix must not disturb the desk that was already working.
print("\nAND THE PRO II IS UNCHANGED (more sources than faders, so no empty strip)")
PRO2_FACTORY = [0, 1, 2, 3, 10, 11, 7, 8, 9]
pro2_order = [code for code, _ in devices.PRO2.fader.assignable]
check("a swap still swaps", server._tidy_fader_target(PRO2_FACTORY, 0, 8, pro2_order),
      [8, 1, 2, 3, 10, 11, 7, 0, 9])
check("bringing the off-fader source on still displaces one",
      server._tidy_fader_target(PRO2_FACTORY, 0, 16, pro2_order),
      [16, 1, 2, 3, 10, 11, 7, 8, 9])


# ---------------------------------------------------------------------------------------------
# A PRESET CARRIES ITS MODULE SET. Without it, applying a preset to a strip whose modules are
# switched off writes all the values, reports the preset name, and sounds identical, because the
# chain is not engaged. So modules are preset DATA, and the master on/off
# (channelBypassProcessing) deliberately is NOT: that one is the operator's control.
# ---------------------------------------------------------------------------------------------
print("\nPRESETS CARRY THEIR MODULE SET")
factory = server._load_factory_presets()
check("all three factory presets are still there", sorted(factory), ["Broadcast", "Neutral", "Podcast Studio"])
for pname, props in factory.items():
    mods = [k for k in props if k in c.PROCESSING_MODULE_BOOLS]
    check(f"'{pname}' names every module", len(mods), len(c.PROCESSING_MODULE_BOOLS))
    check(f"'{pname}' does NOT carry the master on/off",
          "channelBypassProcessing" in props, False)
check("saving a preset records the modules too",
      all(m in server._PRESET_PROPS for m in c.PROCESSING_MODULE_BOOLS), True)
check("saving a preset still does NOT record the master on/off",
      "channelBypassProcessing" in server._PRESET_PROPS, False)


def built_order(props):
    """The property names, in the order apply_processing_preset would put them on the wire."""
    sent = []
    saved_send, saved_req = rcp_transfer.send_records_paced, c._require_processing
    rcp_transfer.send_records_paced = lambda recs, delay=0: sent.extend(recs) or 0
    c._require_processing = lambda: None
    try:
        c.apply_processing_preset(2, props)
    finally:
        rcp_transfer.send_records_paced, c._require_processing = saved_send, saved_req
    order = []
    for rec in sent:
        for name in list(props):
            if name.encode() in rec:
                order.append(name)
                break
    return order


order = built_order(factory["Podcast Studio"])
check("every property is written once", len(order), len(factory["Podcast Studio"]))
check("the preset marker is written LAST", order[-1], c.CHAIN_PRESET_MARKER_PROP)
last_module = max(i for i, n in enumerate(order) if n in c.PROCESSING_MODULE_BOOLS)
first_float = min(i for i, n in enumerate(order) if n in c.PRESET_FLOAT_PROPS)
check("modules are written BEFORE the parameters", last_module < first_float, True)

print("\nAN OLDER SAVED PRESET (no module keys) STILL APPLIES")
legacy = {k: v for k, v in factory["Neutral"].items() if k not in c.PROCESSING_MODULE_BOOLS}
legacy_order = built_order(legacy)
check("it writes only what it carries", len(legacy_order), len(legacy))
check("no module is invented for it",
      [n for n in legacy_order if n in c.PROCESSING_MODULE_BOOLS], [])

print()
if FAILED:
    print(f"{FAILED} CHECK(S) FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
