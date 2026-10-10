"""LIVE TEST: clear every user pad, then restore them all from a backup.

`soundpad.clear_all_pads` clears with bare `clear_pad`, and clearing voice-FX pads one after another
leaves PARTIAL effect objects behind (an object removed from the middle of the list breaks the next
one). Its closing `clean_pool` must delete every one, so the pool ends with only the special object.
Needs at least two voice-FX pads on the grid for that case to arise.

  1. back up every user pad (and its sound file) to a folder under %TEMP%;
  2. clear all pads: no user pad left, the pool clean, region B down to the one special object;
  3. restore the backup in one transfer session;
  4. back up again and compare: the same pads with the same settings, the same sound files, and the
     same pool as before.

The backup folder is kept if anything fails, so the pads can be put back with --restore.

EXCLUSIVE DEVICE ACCESS: stop the running service first, then restart it with
`python DevTools/run_dev_server.py`. Destructive in the middle: it clears every user pad.

    python DevTools/test_clear_all_live.py
    python DevTools/test_clear_all_live.py --restore     # put the pads back from the kept backup
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile

import _bootstrap  # noqa: F401  - puts Project/ on sys.path

import rcp_transfer
import soundpad

BACKUP_DIR = os.path.join(tempfile.gettempdir(), "accesscaster_clear_all_backup")
VERIFY_DIR = BACKUP_DIR + "_verify"
PADS_JSON = os.path.join(BACKUP_DIR, "pads.json")
FAILED = 0


def check(label, ok, detail=""):
    global FAILED
    FAILED += not ok
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f": {detail}" if detail and not ok else ""))


def file_hashes(root):
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f.lower().endswith(".wav"):
                path = os.path.join(dirpath, f)
                with open(path, "rb") as fh:
                    out[os.path.relpath(path, root)] = hashlib.sha256(fh.read()).hexdigest()
    return out


def restore(pads):
    with rcp_transfer.transfer_session():
        return soundpad.restore_pads(pads, BACKUP_DIR, verify_first=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--restore", action="store_true", help="only restore the kept backup")
    args = ap.parse_args()

    if args.restore:
        with open(PADS_JSON, encoding="utf-8") as fh:
            pads = json.load(fh)
        res = restore(pads)
        print("restore ok:", res["ok"], " pool clean:", res["health"]["clean"])
        return 0 if res["ok"] else 1

    pool_before = soundpad.pool_health()
    if not pool_before["clean"]:
        print("the pool is not clean before the test; stopping.", pool_before)
        return 2
    if len(pool_before["fx_pads"]) < 2:
        print("needs at least two voice-FX pads for partial objects to arise; stopping.")
        return 2

    print("BACK UP")
    backup = soundpad.backup_pads(BACKUP_DIR)
    pads = backup["pads"]
    with open(PADS_JSON, "w", encoding="utf-8") as fh:
        json.dump(pads, fh, indent=1)
    print(f"  {len(pads)} pads, {backup['files_copied']} sound files, "
          f"{len(pool_before['fx_pads'])} voice-FX pads, kept in {BACKUP_DIR}")
    if backup.get("unrecognised"):
        print("  unrecognised pads, cannot be restored; stopping:", backup["unrecognised"])
        return 2
    hashes_before = file_hashes(BACKUP_DIR)

    print("CLEAR ALL")
    res = soundpad.clear_all_pads()
    print("  " + res["message"])
    left = [p["grid"] for p in soundpad.read_pads_live()["pads"]
            if p["present"] and p["type"] != soundpad.SYSTEM_PAD_TYPE]
    h = res["health"]
    check("every user pad cleared", res["cleared"] == len(pads) and not left,
          f"cleared {res['cleared']} of {len(pads)}, left {left}")
    check("pool clean", h["clean"], h)
    check("no partial object left", h["partial"] == [], h["partial"])
    check("region B is down to the special object", h["region_b_count"] == 1, h["region_b_count"])

    print("RESTORE")
    res = restore(pads)
    check("restore reported ok", res["ok"], res.get("message", ""))

    print("COMPARE")
    shutil.rmtree(VERIFY_DIR, ignore_errors=True)
    after = soundpad.backup_pads(VERIFY_DIR)["pads"]
    diffs = [(a.get("grid"), k, a.get(k), b.get(k)) for a, b in zip(pads, after)
             for k in a if a.get(k) != b.get(k)]
    check("same pads back", len(after) == len(pads), f"{len(after)} of {len(pads)}")
    check("every setting matches", not diffs, diffs)
    check("every sound file matches", file_hashes(VERIFY_DIR) == hashes_before)
    pool_after = soundpad.pool_health()
    check("pool as before", pool_after == pool_before, (pool_before, pool_after))
    shutil.rmtree(VERIFY_DIR, ignore_errors=True)

    if FAILED:
        print(f"\n{FAILED} FAILED. Backup kept in {BACKUP_DIR}.")
        return 1
    shutil.rmtree(BACKUP_DIR, ignore_errors=True)
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
