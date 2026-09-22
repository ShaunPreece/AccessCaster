"""DOES THE DUO'S ENGINE HONOUR THE STEREO-PAIR SOURCE CODE 4 (Mic 1 + 2)?

THE TREE CANNOT ANSWER THIS, which is why the probe exists. The A6 route - "does the property
resolve to a node of the right block?" - settled the unit-settings cap without a capture, but it
does not transfer here. The Duo publishes the Pro II's WHOLE source space verbatim: 30 INPUTSOURCE
slots and 30 matrix rows of 13, with slots 3, 4, 5, 6 and 12-16 all sitting uninitialised
(inputColour black, matrix row "-1|0"). Source 3 is MIC 4, which the Duo does not physically have,
and it looks EXACTLY like pair slot 4. So "the slot exists and is blank" is consistent both with
"the pair code works here" and with "this desk has no such source". Only the engine can tell them
apart.

SO THIS IS THE B10 METHOD-1 TEST: make the ENGINE answer. Write ONLY the channelInputSource code and
diff the whole tree. Per RODECASTER_PROTOCOL 6.5 the desk derives the rest of a link itself -
inputType goes 0 -> 2 on the pair's INPUTSOURCE slot, the pair's routing-matrix row fills in, and
both channels unmute - and NONE of that is written by the RODE app, let alone by us. If those appear
the code is live on a Duo; if our two ints are all that moved, the code is inert here and the cap
stays off.

THE ADDRESS IS ALREADY PROVEN, so this is not a stray-append hazard. channelInputSource at selector
0x1c + fader index is hardware-confirmed on this desk (the `fader_write` cap is ON) and
the vector length is 10. It is the VALUE that is unproven, not where it goes.

WHAT IT CANNOT UNDO, and this is the one real cost: if the link WORKS, unlinking does NOT return
INPUTSOURCE[4] to factory (colour, mic type, inputType and its matrix row stay initialised). Inert -
nothing carries the code once unlinked - and NOT a stray, since the property count is unchanged; but
only a factory reset clears it. Observed on the Pro II and expected here.

THE CONTROL MATTERS AS MUCH AS THE TEST, and `--control CODE` is it. "The engine filled the matrix
row" only means something if the engine does NOT do that for a source the desk has no such thing as.
Source 3 is Mic 4: seat it on the empty fader and see. If its row fills too, the response is generic
"something was put on a fader" bookkeeping and proves nothing about pair codes; if it stays -1|0,
the pair code is genuinely recognised.

Run: python DevTools/duo_stereo_probe.py [--pair N] [--control CODE] [--no-restore]
"""
from __future__ import annotations
import sys
import time

import _bootstrap  # noqa: F401
import devices
import model_tree
import rcp_read as rr
import rcp_transfer as rt

PAIR = 4          # Mic 1 + 2: the only pair the Duo could have, its two mics being adjacent.
LOWER, UPPER = 0, 1
if "--pair" in sys.argv:
    PAIR = int(sys.argv[sys.argv.index("--pair") + 1])
CONTROL = None
if "--control" in sys.argv:
    CONTROL = int(sys.argv[sys.argv.index("--control") + 1])
RESTORE = "--no-restore" not in sys.argv


def snapshot():
    """{(path, block, property): value} for every property in the tree."""
    blob = rr.read_model_stable(fresh=True)
    root, _u, _t = model_tree.parse(blob)
    out = {}
    for n in model_tree.walk(root):
        p = tuple(n.path())
        for name, val, _k in n.props:
            out[(p, n.name, name)] = val
    return out


def settled(timeout=15.0):
    """A snapshot taken once the WHOLE TREE has stopped moving. A link is a derived change, so the
    desk keeps working after our write lands; a fixed sleep would race it."""
    deadline = time.time() + timeout
    prev = snapshot()
    while time.time() < deadline:
        cur = snapshot()
        if cur == prev:
            return cur
        prev = cur
    print(f"  note: still changing after {timeout:.0f}s, using the last reading")
    return prev


def diff(a, b):
    changed = [(k, a[k], b[k]) for k in a if k in b and a[k] != b[k]]
    added = [(k, b[k]) for k in b if k not in a]
    removed = [(k, a[k]) for k in a if k not in b]
    return changed, added, removed


def show(title, changed, added, removed, ours):
    print(f"\n{title}")
    print(f"  changed {len(changed)}, added {len(added)}, removed {len(removed)}")
    engine = 0
    for (path, block, prop), old, new in sorted(changed, key=lambda t: (t[0][1], t[0][0])):
        tag = "OURS  " if (block, prop, path) in ours else "ENGINE"
        if tag == "ENGINE":
            engine += 1
        print(f"    {tag} {block}{list(path)}.{prop}: {old!r} -> {new!r}")
    for (path, block, prop), val in sorted(added, key=lambda t: (t[0][1], t[0][0])):
        print(f"    ADDED  {block}{list(path)}.{prop} = {val!r}")
    for (path, block, prop), val in sorted(removed, key=lambda t: (t[0][1], t[0][0])):
        print(f"    GONE   {block}{list(path)}.{prop} was {val!r}")
    return engine


def vector():
    return list(rr.values(rr.read_model(fresh=True), "channelInputSource"))


def control(code: int, spec):
    """Seat an arbitrary source code on the EMPTY fader and see what the engine does with it.

    The empty slot is used deliberately: nothing is displaced, so the only change we cause is the
    one int, and any derived movement is unambiguously the desk's answer to THAT code.
    """
    base = settled()
    vec0 = vector()
    seat = next((i for i, c in enumerate(vec0[:spec.count]) if c == -1), None)
    if seat is None:
        print("no empty fader to use as the control seat; skipping")
        return 1
    print(f"CONTROL: seating source code {code} on fader {seat + 1} (was empty)")
    rt.set_fader_source(seat, code)
    after = settled()
    print(f"vector now: {vector()}")
    changed, added, removed = diff(base, after)
    ours = {(block, prop, path) for (path, block, prop), _o, _n in changed
            if prop == "channelInputSource"}
    engine = show(f"CONTROL, source {code} on an empty fader", changed, added, removed, ours)
    row = 0x4C + 13 * code
    filled = sum(1 for (path, block, prop), _o, n in changed
                 if block == "MIX" and prop == "mixLevelWithAnchor"
                 and row <= path[0] < row + 13 and n != "-1|0")
    print(f"\n  source {code}'s own matrix row (MIX[{row}..{row + 12}]) filled: {filled}/13")
    print(f"  other properties the engine moved: {engine - filled}")
    print("\nRESTORING")
    rt.set_fader_source(seat, -1)
    back = settled()
    print(f"vector restored to: {vector()}  (matches baseline: {vector() == vec0})")
    changed, added, removed = diff(base, back)
    show("RESIDUE vs BASELINE", changed, added, removed, set())
    return 0


def mic_type_response(code: int, base: int, value: int = 1):
    """Write inputMicrophoneType to INPUTSOURCE[code] and count what the ENGINE moved as a result.

    THE ONE DISCRIMINATOR THAT HOLDS. The matrix-row fill is generic bookkeeping - source 3 (Mic 4,
    which the Duo does not have) fills its row exactly like pair 4 does - so it cannot tell a live
    source from a stored int. A mic-type write can: on a REAL input the engine
    applies that voicing's gain, its phantom setting and thirteen chain parameters, none of which we
    sent - which is what proved the corrected gain address. On a slot nothing is listening to, our
    byte moves and nothing else does.

    So: run it on the pair slot, and run the SAME write on a slot known dead and a slot known live.
    """
    before = settled()
    rt.send_record(rt.build_path_prop_int("inputMicrophoneType", value, [base + code]))
    time.sleep(0.15)
    after = settled()
    changed, added, removed = diff(before, after)
    ours = {(block, prop, path) for (path, block, prop), _o, _n in changed
            if prop == "inputMicrophoneType" and path == (base + code,)}
    engine = show(f"mic-type write to INPUTSOURCE[{code}]", changed, added, removed, ours)
    return engine, before


def discriminate(spec):
    """The pair slot against a known-dead slot and a known-live one, all three seated on a fader."""
    dev = devices.get_active_device()
    base = dev.input_table_base
    vec0 = vector()
    print(f"baseline vector: {vec0}")
    seat = next((i for i, c in enumerate(vec0[:spec.count]) if c == -1), None)
    if seat is None:
        print("no empty fader to seat the codes on; refusing")
        return 1
    results = {}
    # 1 = Mic 2, a real input already on a fader: the POSITIVE control, so a null result elsewhere
    #     cannot be blamed on the write itself failing.
    # 3 = Mic 4, which this desk does not have: the NEGATIVE control.
    # PAIR = the question.
    for code in (1, 3, PAIR):
        placed = False
        if code not in vec0:
            print(f"\n=== seating source {code} on fader {seat + 1} ===")
            rt.set_fader_source(seat, code)
            settled()
            placed = True
        else:
            print(f"\n=== source {code} is already on fader {vec0.index(code) + 1} ===")
        engine, _b = mic_type_response(code, base)
        results[code] = engine
        if placed:
            rt.set_fader_source(seat, -1)
            settled()
    print("\n--- DISCRIMINATION ---")
    for code, engine in results.items():
        what = {1: "Mic 2, a real input (positive control)",
                3: "Mic 4, which this desk lacks (negative control)"}.get(code, f"pair code {code}")
        print(f"  INPUTSOURCE[{code}] {what}: engine moved {engine} properties")
    live, dead, pair = results.get(1, 0), results.get(3, 0), results.get(PAIR, 0)
    if pair > dead and pair >= live * 0.5:
        print(f"  => pair {PAIR} behaves like a REAL input on this desk.")
    elif pair <= dead:
        print(f"  => pair {PAIR} behaves like the DEAD slot. The Duo does not honour it.")
    else:
        print("  => inconclusive; read the diffs above rather than this line.")
    return 0


def main():
    dev = devices.get_active_device()
    if dev.product_id != devices.PID_DUO:
        print(f"This probe is for the Duo; {dev.name} is attached. Refusing.")
        return 2
    spec = dev.fader
    print(f"{dev.name}, {spec.count} faders, vector length {spec.vector_length}")
    if CONTROL is not None:
        return control(CONTROL, spec)
    if "--discriminate" in sys.argv:
        return discriminate(spec)

    base = settled()
    vec0 = vector()
    print(f"baseline vector: {vec0}")
    if PAIR in vec0:
        print(f"pair code {PAIR} is ALREADY on a fader; nothing to prove. Stopping.")
        return 1

    lo_at = vec0.index(LOWER) if LOWER in vec0 else None
    up_at = vec0.index(UPPER) if UPPER in vec0 else None
    if lo_at is None:
        print(f"source {LOWER} is not on a fader; cannot seat the pair where the desk would.")
        return 1
    print(f"Mic 1 on fader {lo_at + 1}, Mic 2 on fader {(up_at + 1) if up_at is not None else '-'}")

    # The captured RODE-app order (PROTOCOL 6.5): vacate both mics, then seat the pair on the
    # LOWER mic's fader. Writing only these ints is what leaves the desk byte-identical to the
    # app's fuller sequence, proven on the Pro II.
    writes = []
    if up_at is not None:
        writes.append((up_at, -1))
    writes.append((lo_at, -1))
    writes.append((lo_at, PAIR))

    print(f"\nWRITING: {[(i + 1, v) for i, v in writes]}")
    for idx, val in writes:
        rt.set_fader_source(idx, val)
        time.sleep(0.05)

    after = settled()
    vec1 = vector()
    print(f"vector now: {vec1}")

    # OURS = the channelInputSource slots we actually wrote. Everything else in the diff is the
    # desk's own doing, which is the whole question.
    changed, added, removed = diff(base, after)
    ours = {(block, prop, path) for (path, block, prop), _o, _n in changed
            if prop == "channelInputSource"}
    engine = show("AFTER THE WRITE", changed, added, removed, ours)

    linked = PAIR in vec1
    print("\n--- VERDICT ---")
    print(f"  pair code {PAIR} sits in the vector: {linked}")
    print(f"  properties the ENGINE moved that we did not write: {engine}")
    if linked and engine:
        print("  => THE DUO HONOURS THE PAIR CODE. The engine derived state we never sent.")
    elif linked and not engine:
        print("  => INERT. The int was stored and nothing else happened, so the code means")
        print("     nothing to this desk's engine. Same shape as the mic-gain dead address.")
    else:
        print("  => the desk REFUSED the value outright.")

    if RESTORE:
        print("\nRESTORING the baseline vector")
        for i in range(spec.vector_length):
            if i < len(vec1) and i < len(vec0) and vec1[i] != vec0[i]:
                rt.set_fader_source(i, vec0[i])
                time.sleep(0.05)
        back = settled()
        vec2 = vector()
        print(f"vector restored to: {vec2}  (matches baseline: {vec2 == vec0})")
        changed, added, removed = diff(base, back)
        show("RESIDUE vs BASELINE", changed, added, removed, set())
    return 0


if __name__ == "__main__":
    sys.exit(main())
