"""Live test of the orphan cleanup, leaving the whole tree as it started after each part.

ATTACHED: a pad created WITH an effects block, typed as a mixer, then cleared. A mixer pad's clear
does not collect the object, so it is left with no pad; clean_pool must remove it.

PARTIAL: two FX pads, then the first cleared, which removes an object from the middle of the list,
then the second, whose clear leaves a partial object; clear_grid_pad must delete it.

Uses padIdx 16 and 17 (bank 3 pads 1 and 2), which must be empty. A DETACHED object cannot be made
from here (only RODE's app has been seen to leave one), so that branch is not covered.

    python DevTools/test_orphan_cleanup_live.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import model_tree as mt
import pad_records as pr
import rcp_read as rr
import rcp_transfer as t
import soundpad as sp

SLOT = 16
FAILED = 0


def check(label, got, want):
    global FAILED
    ok = got == want
    FAILED += not ok
    print(f"  {'ok  ' if ok else 'FAIL'} {label}: {got!r}" + ("" if ok else f", want {want!r}"))


def tree_state():
    root = mt.parse(rr.read_model_stable(fresh=True))[0]
    return {tuple(n.path()): [(p, v) for p, v, _ in n.props] for n in mt.walk(root)}


def make_orphan():
    handle = pr.free_handle(sp.current_pad_count())
    sub = sp.free_effect_subhandle(SLOT)
    pre = pr.build_blank(SLOT, handle, sub, with_effects=True)
    mid = [pr.write_record("padType", "int", 3, handle, pr.FRAME_PAD),
           pr.write_record("padName", "str", "Orphan test", handle, pr.FRAME_PAD)]
    t.set_transfer_mode(True); time.sleep(1.2)
    sp.send_create_full(pre, mid, lambda: None, [])
    time.sleep(0.4); t.set_transfer_mode(False); time.sleep(0.8)
    pos, _, _ = sp._position_of(SLOT)
    sp.clear_pad(pos)
    time.sleep(0.8)


def main() -> int:
    for idx in (SLOT, SLOT + 1):
        if sp._position_of(idx)[0] is not None:
            print(f"padIdx {idx} is not empty; refusing.")
            return 2
    before = tree_state()
    make_orphan()
    h = sp.pool_health()
    check("an attached orphan was made", (h["orphans"], h["detached"]), ([SLOT], []))
    res = sp.clean_pool()
    check("clean_pool removed it", res["removed_orphans"], 1)
    check("the pool is clean", res["health"]["clean"], True)
    check("the whole tree is as it started", tree_state() == before, True)

    print("\nPARTIAL")
    for idx in (SLOT, SLOT + 1):
        sp.assign_fx_to_grid(idx + 1, "Megaphone")
    sp.clear_grid_pad(SLOT + 1)
    res = sp.clear_grid_pad(SLOT + 2)
    check("no partial object is left", res["health"]["partial"], [])
    check("the pool is clean", res["health"]["clean"], True)
    check("the whole tree is as it started", tree_state() == before, True)
    print("\n" + ("ALL CHECKS PASSED" if not FAILED else f"{FAILED} FAILED"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
