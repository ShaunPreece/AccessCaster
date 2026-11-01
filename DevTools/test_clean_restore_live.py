"""THE WHOLE CLEAN RESTORE, end to end, with a whole-model diff.

WHAT IT DOES, through the app's own HTTP API so it exercises the shipping path:
  1. dumps the desk as it stands,
  2. saves a profile of it,
  3. runs a CLEAN restore (which exports a backup, software-factory-resets the desk, then writes the
     profile back on top) and polls its progress,
  4. dumps the desk again and diffs the two, splitting every difference into by-design, pads,
     back-at-the-factory-value and REAL,
  5. puts the desk back by importing the backup the restore took, and diffs THAT against the
     starting dump too, so the undo is proven rather than assumed.

**IT FACTORY-RESETS THE DESK**, so it refuses without --yes. The undo is the show backup taken in
step 3, which is exactly the state the desk was in when it started. Once the restore has started the
undo runs however the run ends, a failed restore or an error included, and the run reports whether
it came back clean. The profile is saved WITHOUT pads (a pad backup is minutes of file copying), so the
reset's factory pads stay through step 4 and pad differences there are expected - step 5 puts the
desk's own pads back with the show import.

Usage: python DevTools/test_clean_restore_live.py --yes [--keep-profile]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401
import _desk
import rcp_read
from _desk import api, wait_for_job
from analyse_clean_restore import compare, show, snap

CAP = Path(__file__).resolve().parent / "captures"
FACTORY_DUMP = CAP / "post-showreset-raw.bin"     # a real factory dump of THIS desk
PROFILE = "Clean restore retest"


def say(text: str) -> None:
    """Speak a one-line status, unless Claudio has the focus, and print it as the record."""
    _desk.say(text, gated=True, wait=True)
    print(f"** {text}")


def poll(path: str, label: str, timeout: float = 900.0) -> dict:
    """Wait for the server job behind status route `path`, printing each new message. A job still
    running at `timeout` comes back as a failure, so the undo still runs."""
    said = [None]

    def print_new(st: dict) -> None:
        msg = st.get("message") or st.get("label") or ""
        if msg and msg != said[0]:
            print(f"   {msg}")
            said[0] = msg

    try:
        return wait_for_job(lambda: api(path, timeout=120.0), timeout, 1.5, print_new)
    except TimeoutError as e:
        return {"done": False, "ok": False, "message": f"{label}: {e}"}


def dump(name: str) -> Path:
    path = CAP / f"{name}.bin"
    path.write_bytes(rcp_read.read_model_stable(fresh=True))
    print(f"   dumped {path.name} ({path.stat().st_size} bytes)")
    return path


def put_desk_back(before_path: Path, shows_before: set) -> bool:
    """Import the show backup the clean restore took, and diff the desk against the starting dump.
    True when it came back with nothing unexplained."""
    say("Putting the desk back now, by importing the backup the restore took.")
    new_backups = [s for s in api("/api/shows", timeout=120.0).get("shows") or []
                   if s["snapshot"] not in shows_before]
    if not new_backups:
        say("I cannot find the backup the restore took. The desk may be at factory settings.")
        print("*** no new show backup found; restore the desk by hand from the Shows list ***")
        return False
    # Newest first, so the first new one is the backup taken moments ago.
    target = new_backups[0]
    print(f"   importing \"{target['name']}\" (snapshot {target['snapshot']})")
    res = api("/api/shows/import", post={"snapshot": int(target["snapshot"])}, timeout=600.0)
    if not res.get("ok"):
        say(f"The import was refused. The desk may be at factory settings: import the backup "
            f"called {target['name']} from the Shows list.")
        print(f"   import refused: {res.get('message')}")
        return False
    st = poll("/api/shows/status", "the show import")
    print(f"   import finished: ok={st.get('ok')} - {st.get('message')}")
    time.sleep(2.0)
    undo_path = dump("post-cleanretest-undo-raw")
    undo = compare(snap(before_path), snap(undo_path), None, pads_restored=True)
    left = (len(undo["real"]) + len(undo["gone"]) + len(undo["new"]))
    print(f"\nTHE UNDO: {left} unexplained differences against the starting dump "
          f"({len(undo['by_design'])} by design)")
    for row in undo["real"][:20]:
        (path, block, prop), was, now, _f = row
        print(f"  {block}{list(path)}.{prop}: {was!r} -> {now!r}")
    return left == 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="required: this factory-resets the desk")
    ap.add_argument("--keep-profile", action="store_true")
    args = ap.parse_args()
    if not args.yes:
        print("This FACTORY-RESETS the desk (and puts it back afterwards). Re-run with --yes.")
        return 2

    say("Starting the clean restore retest. I am dumping the desk, then saving a profile of it.")
    before_path = dump("pre-cleanretest-raw")
    shows_before = {s["snapshot"] for s in api("/api/shows", timeout=120.0).get("shows") or []}

    res = api("/api/profiles/save", post={"name": PROFILE, "include_pads": False}, timeout=600.0)
    if not res.get("ok"):
        print(f"could not save the profile: {res.get('message')}")
        return 1
    st = poll("/api/profiles/save/status", "the profile save")
    print(f"   profile saved: {st.get('message')}")

    say("Now the clean restore. It backs the desk up, factory resets it, and writes the profile "
        "back on top.")
    res = api("/api/profiles/restore", post={"name": PROFILE, "clean": True}, timeout=600.0)
    if not res.get("ok"):
        print(f"could not start the restore: {res.get('message')}")
        return 1
    # FROM HERE THE DESK MAY BE AT FACTORY SETTINGS, so the undo runs however this ends.
    unexplained = None
    undo_ok = False
    try:
        st = poll("/api/profiles/restore/status", "the clean restore")
        print(f"   restore finished: ok={st.get('ok')} - {st.get('message')}")
        if not st.get("ok"):
            say("The clean restore reported a failure, so there is nothing to diff.")
        else:
            time.sleep(2.0)
            say("Restore done. Dumping the desk and diffing every property against how it started.")
            after_path = dump("post-cleanretest-raw")
            factory = snap(FACTORY_DUMP) if FACTORY_DUMP.is_file() else None
            diff = compare(snap(before_path), snap(after_path), factory)
            print()
            unexplained = show(diff, factory is not None)
    finally:
        undo_ok = put_desk_back(before_path, shows_before)
        if not args.keep_profile:
            gone = api("/api/profiles/delete", post={"name": PROFILE, "force": True})
            print(f"   scratch profile '{PROFILE}' deleted" if gone.get("ok")
                  else f"   COULD NOT DELETE the scratch profile '{PROFILE}': {gone.get('message')}")

    back = (" The desk is back to how it started." if undo_ok
            else " Check the desk: the undo did not come back clean.")
    if unexplained is None:
        say("THE CLEAN RESTORE FAILED." + back)
        return 1
    verdict = ("THE CLEAN RESTORE IS CLEAN." if unexplained == 0
               else f"{unexplained} DIFFERENCES ARE STILL UNEXPLAINED.")
    say(verdict + back)
    print(f"\n{verdict}")
    return 0 if (unexplained == 0 and undo_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
