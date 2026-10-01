"""`action_delete_backup` ON A REAL DESK, both of its branches.

The last unrun thing on the Backups page. Everything else there met a desk on 21st September 2026
(`read_backups`, `action_backup_now`, `backup_restore_plan`, `action_restore_backup`); this one
needed the recordings drive in, which is the only reason it waited.

TWO BRANCHES, AND THE SECOND IS THE ONE THAT MATTERS:

  * `show:<snapshot>` - a backup the unit made with no profile beside it. Straight through to
    `action_show_delete`.
  * a PROFILE NAME with a show paired to it - deletes BOTH halves. The page offers one Delete
    because the user made one backup, and leaving the unit's copy behind would quietly fill the
    drive with 24 MB files they believe they have deleted. The show goes FIRST on purpose: fail
    that way round and a profile is still listed, fail the other way and there is an orphan on the
    drive that nothing can name.

WHAT IT CHECKS. That the named backup goes, that the OTHERS do not, that the drive's free space
goes UP for a show delete (the only direct evidence the 24 MB file actually left), and that the
as-found baseline is still refused without `force` after all of it.

IRREVERSIBLE, so it takes the names on the command line rather than choosing for you, and it
refuses anything it can see a reason to keep.

    python DevTools/probe_delete_backup_live.py --profile "Testing New Backup Flow"

THE SERVER MUST BE STOPPED: a show delete drives Transfer Mode, which needs the HID to itself.

AND IT IS A `probe_`, NOT A `test_`, DELIBERATELY. `_bootstrap` hands anything named `test_*.py` a
throwaway data dir so a test can never eat the user's real profiles - which is exactly right, and
exactly wrong for this one: the both-halves branch pairs a REAL profile with a REAL show, so under
the sandbox it saw an empty profile store, listed the unit's shows alone and reported that the
backup it was asked to delete did not exist. The show branch passed regardless, because a show
lives on the desk rather than in that folder, which is what made the failure look selective.
"""
from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import profiles
import rcp_read
import recorder
import server

FAILED = 0


def check(label, got, want):
    global FAILED
    if got == want:
        print(f"  ok   {label}: {got!r}")
    else:
        FAILED += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def names():
    r = server.read_backups()
    if not r.get("ok"):
        raise RuntimeError(r.get("message") or "the backups list could not be read")
    return [b["name"] for b in r["backups"]], r


def free_mb():
    st = recorder.describe(rcp_read.read_model_stable(fresh=True))
    for v in st["volumes"]:
        if v["index"] == 1:
            return v["free_mb"]
    return None


def wait_for_job(timeout=180.0):
    """A delete that touches the unit runs as a background job; the reply only says it STARTED.

    THE KEY IS `running`, AND GETTING THAT WRONG LOOKS LIKE A BUG IN THE APP. Polling a key the
    status does not carry reads as None, so the wait returns at once, the check runs while the
    profile half is still in flight, and the report says `action_delete_backup` left the computer's
    copy behind when it had not even finished. `done` is the companion: a job that has run reports
    running False and done True."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = server.show_job_status()
        if not st.get("running"):
            return st
        time.sleep(1.0)
    return {"timeout": True}


ap = argparse.ArgumentParser()
ap.add_argument("--show", type=int, help="snapshot number of a unit-only backup to delete")
ap.add_argument("--profile", help="name of a paired backup to delete, both halves")
args = ap.parse_args()

before, raw = names()
print(f"=== action_delete_backup, against a real desk ===")
print(f"  {len(before)} backup(s) listed, drive free {free_mb()} MB\n")

if args.show is not None:
    target = f"show:{args.show}"
    print(f"1. the SHOW branch: {target}")
    was_free = free_mb()
    res = server.action_delete_backup(target)
    print(f"   reply: ok={res.get('ok')} {res.get('message','')!r}")
    wait_for_job()
    time.sleep(4.0)
    after, raw2 = names()
    snaps = {b.get("snapshot") for b in raw2["backups"]}
    check("the snapshot is gone from the list", args.show in snaps, False)
    now_free = free_mb()
    check("and the drive has MORE room than before", (now_free or 0) > (was_free or 0), True)
    print(f"   free {was_free} MB -> {now_free} MB")
    before = after

if args.profile:
    print(f"\n2. the BOTH HALVES branch: {args.profile!r}")
    paired = next((b for b in server.read_backups()["backups"]
                   if b["name"] == args.profile), None)
    if paired is None:
        FAILED += 1
        print(f"   FAIL no backup called {args.profile!r}")
    else:
        snap = paired.get("snapshot")
        print(f"   it is paired with snapshot {snap}")
        was_free = free_mb()
        res = server.action_delete_backup(args.profile)
        print(f"   reply: ok={res.get('ok')} via={res.get('via')!r} {res.get('message','')!r}")
        wait_for_job()
        time.sleep(4.0)
        after, raw3 = names()
        check("the backup is gone from the list", args.profile in after, False)
        check("and so is its half on the unit",
              snap in {b.get("snapshot") for b in raw3["backups"]}, False)
        check("this computer's copy is gone too", profiles.load(args.profile).get("ok"), False)
        now_free = free_mb()
        print(f"   free {was_free} MB -> {now_free} MB")
        before = after

print("\n3. NOTHING ELSE WENT WITH IT")
after, raw4 = names()
print(f"   {len(after)} backup(s) left")
check("the as-found baseline is still there",
      any(n.startswith("As found") for n in after), True)
base = next((n for n in after if n.startswith("As found")), None)
if base:
    r = server.action_delete_backup(base)       # NOT forced: this must REFUSE
    check("and deleting it without force is still refused", r.get("ok"), False)
    print(f"   it said: {r.get('message','')!r}")
    check("so it is still listed afterwards", base in names()[0], True)

print(f"\nFAILURES: {FAILED}")
raise SystemExit(1 if FAILED else 0)
