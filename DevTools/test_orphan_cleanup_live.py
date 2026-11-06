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
import _testkit
import pad_records as pr
import rcp_read as rr
import rcp_transfer as t
import soundpad as sp
from _testkit import check

SLOT = 16


def tree_state():
    return _testkit.snapshot(rr.read_model_stable(fresh=True))


def make_orphan():
    handle = sp.free_pad_handle()
    sub = sp.free_effect_subhandle(SLOT)
    pre = pr.build_blank(SLOT, handle, sub)
    mid = [pr.write_record("padType", "int", 3, handle, pr.PAD_BLOCK),
           pr.write_record("padName", "str", "Orphan test", handle, pr.PAD_BLOCK)]
    with t.transfer_session():
        time.sleep(1.2)
        sp.send_create_full(pre, mid, lambda: None, [])
        time.sleep(0.4)
    time.sleep(0.8)
    pos, _, _ = sp._position_of(SLOT)
    sp.clear_pad(pos)
    time.sleep(0.8)


def main() -> int:
    for idx in (SLOT, SLOT + 1):
        if sp._position_of(idx)[0] is not None:
            print(f"padIdx {idx} is not empty; refusing.")
            return 2
    before = tree_state()
    was_clean = sp.pool_health()["clean"]
    try:
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
    finally:
        for idx in (SLOT, SLOT + 1):
            if sp._position_of(idx)[0] is not None:
                print(f"  clearing the pad left on padIdx {idx}")
                sp.clear_grid_pad(idx + 1)
        # A mixer pad's clear does not collect its effect object, so a run that failed between
        # making the orphan and cleaning it leaves the orphan behind. A pool that was not clean
        # before the run is left for a person: its orphans are not this run's.
        if was_clean and not sp.pool_health()["clean"]:
            print("  the pool is not clean: removing what this run left")
            sp.clean_pool()
    return _testkit.finish()


if __name__ == "__main__":
    raise SystemExit(main())
