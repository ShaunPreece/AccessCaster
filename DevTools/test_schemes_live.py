"""MIX SCHEMES on the real desk: capture the mixes, change them, put them back, prove it is clean.

REVERSIBLE BY CONSTRUCTION. The first thing this does is capture the desk's CURRENT output mixes as
a scheme, and the last thing it does is apply that scheme back and compare the whole matrix, cell by
cell, with what it read at the start. Anything left different is a FAIL, not a note.

What it proves that the offline suite cannot:
  1. The device really does report the mixes a scheme records (mode, exclusions, link state, level).
  2. The apply path lands: the desk reads back what the scheme said.
  3. A manual (unlinked) cross-point LEVEL survives the round trip. That is the half of the format
     a flags-only scheme gets silently wrong.
  4. Nothing STRAYS: the model's length and its cross-point counts are unchanged at the end.

The test drives the CHAT output only, and never a headphone or the monitor: Chat has no caller on it
on this desk, so nothing about this is audible, and the mixes that feed Shaun's own ears are only
ever read. It refuses to run if a phone caller is connected (the matrix changes shape then, and
every read here would be indexed wrong).

THE SERVER MUST BE STOPPED FIRST: it owns the HID handle. Restart it afterwards
(python DevTools/run_dev_server.py).

Usage: python DevTools/test_schemes_live.py
"""
from __future__ import annotations
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import devices
import rcp_read
import routing_schemes

SETTLE = 1.0          # the desk needs a moment before a fresh dump shows a write
FAILED = 0


def check(label, got, want):
    global FAILED
    if got == want:
        print(f"  ok   {label}: {got!r}")
    else:
        FAILED += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def read_matrix():
    """The whole matrix as a scheme-shaped dict, plus the raw blob for the stray check."""
    blob = rcp_read.read_model_stable(fresh=True)
    cap = routing_schemes.capture("read", blob)
    return cap, blob


def counts(blob):
    """Every property NAME the model reports and how many times it occurs.

    This, not the model's byte length, is the stray test. A stray append shows up as a new name or
    an extra occurrence; the LENGTH moves on its own whenever a level changes, because
    mixLevelWithAnchor is an ASCII string and "1|1" is fourteen bytes shorter than
    "0.551181|0.551181". Measured on the desk, after a length check called a clean
    restore dirty."""
    return {n: len(v) for n, v in rcp_read.snapshot_properties(blob).items()}


def main() -> int:
    dev = devices.get_active_device()
    print(f"Device: {dev.name} (product id {dev.product_id:#06x})")
    if "matrix_write" not in dev.caps:
        print("This device has no matrix_write cap, so there is nothing to test here.")
        return 1

    print("\n1. BASELINE: capture the desk's mixes exactly as they are now")
    before, blob0 = read_matrix()
    if not before.get("ok"):
        print("  REFUSED:", before.get("message"))
        print("  (a connected phone caller changes the matrix shape; try again without one)")
        return 1
    baseline = before["scheme"]
    n0 = counts(blob0)
    chat0 = baseline["outputs"].get("Chat")
    if chat0 is None:
        print("  This desk has no Chat output; nothing to drive safely. Stopping.")
        return 1
    print(f"  captured {len(baseline['outputs'])} outputs, "
          f"{sum(len(o['sources']) for o in baseline['outputs'].values())} cross-points")
    print(f"  Chat is currently mode {chat0['mode']}, carrying "
          f"{sum(1 for c in chat0['sources'].values() if not c['excluded'])} of "
          f"{len(chat0['sources'])} sources")
    print(f"  model {len(blob0)} bytes, {len(n0)} distinct properties, "
          f"{sum(n0.values())} occurrences")

    builtins = {b["id"]: b for b in routing_schemes.builtins()}

    print("\n2. APPLY the built-in 'Chat = my mic only'")
    res = routing_schemes.apply(builtins[routing_schemes.BUILTIN_CHAT_MIC_ONLY]["scheme"])
    check("apply reports ok", res["ok"], True)
    check("and touched the Chat output only", res["outputs"], ["Chat"])
    time.sleep(SETTLE)
    after, _b = read_matrix()
    chat = after["scheme"]["outputs"]["Chat"]
    check("the desk reads back Custom mode", chat["mode"], 2)
    check("carrying Mic 1 and nothing else",
          sorted(k for k, c in chat["sources"].items() if not c["excluded"]), ["Mic1"])

    print("\n3. APPLY the built-in 'Chat back to normal'")
    res = routing_schemes.apply(builtins[routing_schemes.BUILTIN_CHAT_NORMAL]["scheme"])
    check("apply reports ok", res["ok"], True)
    time.sleep(SETTLE)
    after, _b = read_matrix()
    chat = after["scheme"]["outputs"]["Chat"]
    check("the desk reads back MIX MINUS, not Main Mix", chat["mode"], 1)
    check("with every source back in and following the faders",
          all(not c["excluded"] and c["linked"] for c in chat["sources"].values()), True)

    print("\n4. A MANUAL LEVEL round-trips (the half a flags-only scheme loses)")
    level_scheme = {"outputs": {"Chat": {"mode": 2, "sources": dict(
        {k: {"excluded": False, "linked": True} for k in chat["sources"]},
        Mic1={"excluded": False, "linked": False, "level": 0.42})}}}
    res = routing_schemes.apply(level_scheme)
    check("apply reports ok", res["ok"], True)
    time.sleep(SETTLE)
    after, _b = read_matrix()
    cell = after["scheme"]["outputs"]["Chat"]["sources"]["Mic1"]
    check("the cross-point reads back UNLINKED", cell.get("linked"), False)
    check("at the level the scheme asked for", round(cell.get("level", -1), 2), 0.42)
    check("and its neighbours are still linked",
          all(c["linked"] for k, c in after["scheme"]["outputs"]["Chat"]["sources"].items()
              if k != "Mic1"), True)

    print("\n5. AND THE SAVED SCHEME PUTS IT ALL BACK")
    res = routing_schemes.apply(baseline)
    check("apply reports ok", res["ok"], True)
    check("every output was written", len(res["outputs"]), len(baseline["outputs"]))
    check("nothing was skipped", res["skipped"], [])
    time.sleep(SETTLE)
    final, blob1 = read_matrix()
    check("the matrix reads back EXACTLY as it was found",
          final["scheme"]["outputs"] == baseline["outputs"], True)
    if final["scheme"]["outputs"] != baseline["outputs"]:
        for okey, ospec in baseline["outputs"].items():
            got = final["scheme"]["outputs"].get(okey)
            if got != ospec:
                print(f"    DIFF {okey}: was {ospec} now {got}")
    print("\n6. NO STRAYS")
    n1 = counts(blob1)
    check("no property appeared or vanished",
          sorted(set(n1) ^ set(n0)), [])
    check("and every one occurs exactly as many times as before",
          [n for n in n0 if n1.get(n) != n0[n]], [])

    print()
    print("FAILURES:", FAILED)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
