"""THE WHOLE CLEAN RESTORE, end to end, with a 6467-property diff.

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
step 3, which is exactly the state the desk was in when it started; the run reports whether the undo
came back clean. The profile is saved WITHOUT pads (a pad backup is minutes of file copying), so the
reset's factory pads stay through step 4 and pad differences there are expected - step 5 puts the
desk's own pads back with the show import.

Usage: python DevTools/test_clean_restore_live.py --yes [--keep-profile]
"""
from __future__ import annotations
import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import rcp_read
from analyse_clean_restore import snap, compare, show

BASE = "http://127.0.0.1:8770"
CAP = Path(__file__).resolve().parent / "captures"
FACTORY_DUMP = CAP / "post-showreset-raw.bin"     # a real factory dump of THIS desk
PROFILE = "Clean restore retest"
ROOT = Path(__file__).resolve().parent.parent


def say(text: str) -> None:
    """Speak a one-line status. say.ps1 self-gates on the foreground window, so this is safe."""
    try:
        subprocess.run(["powershell", "-File", str(ROOT / "Project" / "say.ps1"), "-Text", text],
                       capture_output=True, timeout=30)
    except Exception:
        pass
    print(f"** {text}")


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def post(path: str, body: dict):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode("utf-8"))


def poll(path: str, label: str, timeout: float = 900.0):
    """Poll a status endpoint until done, printing each new message."""
    last = None
    until = time.time() + timeout
    while time.time() < until:
        st = get(path)
        msg = st.get("message") or st.get("label") or ""
        if msg and msg != last:
            print(f"   {msg}")
            last = msg
        if st.get("done") or (st.get("running") is False and st.get("ok") is not None):
            return st
        time.sleep(1.5)
    return {"done": False, "ok": False, "message": f"{label} did not finish within {timeout:.0f}s"}


def dump(name: str) -> Path:
    path = CAP / f"{name}.bin"
    path.write_bytes(rcp_read.read_model_stable(fresh=True))
    print(f"   dumped {path.name} ({path.stat().st_size} bytes)")
    return path


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
    shows_before = {s["snapshot"] for s in get("/api/shows")["shows"]}

    res = post("/api/profiles/save", {"name": PROFILE, "include_pads": False})
    if not res.get("ok"):
        print(f"could not save the profile: {res.get('message')}")
        return 1
    st = poll("/api/profiles/save/status", "the profile save")
    print(f"   profile saved: {st.get('message')}")

    say("Now the clean restore. It backs the desk up, factory resets it, and writes the profile "
        "back on top.")
    res = post("/api/profiles/restore", {"name": PROFILE, "clean": True})
    if not res.get("ok"):
        print(f"could not start the restore: {res.get('message')}")
        return 1
    st = poll("/api/profiles/restore/status", "the clean restore")
    print(f"   restore finished: ok={st.get('ok')} - {st.get('message')}")
    if not st.get("ok"):
        say("The clean restore reported a failure. Stopping here so nothing else changes.")
        return 1

    time.sleep(2.0)
    say("Restore done. Dumping the desk and diffing every property against how it started.")
    after_path = dump("post-cleanretest-raw")

    factory = snap(FACTORY_DUMP) if FACTORY_DUMP.is_file() else None
    diff = compare(snap(before_path), snap(after_path), factory)
    print()
    unexplained = show(diff, factory is not None)

    # THE UNDO, always, pass or fail: the desk goes back to the state it started in.
    say("Putting the desk back now, by importing the backup the restore took.")
    new_backups = [s for s in get("/api/shows")["shows"] if s["snapshot"] not in shows_before]
    undo_ok = False
    if not new_backups:
        say("I cannot find the backup the restore took. The desk is NOT back to how it started.")
        print("*** no new show backup found; restore the desk by hand from the Shows list ***")
    else:
        # Newest first, so the first new one is the backup taken moments ago.
        target = new_backups[0]
        print(f"   importing \"{target['name']}\" (snapshot {target['snapshot']})")
        res = post("/api/shows/import", {"snapshot": int(target["snapshot"])})
        if res.get("ok"):
            st = poll("/api/shows/status", "the show import")
            print(f"   import finished: ok={st.get('ok')} - {st.get('message')}")
            time.sleep(2.0)
            undo_path = dump("post-cleanretest-undo-raw")
            undo = compare(snap(before_path), snap(undo_path), None, pads_restored=True)
            left = (len(undo["real"]) + len(undo["gone"]) + len(undo["new"]))
            print(f"\nTHE UNDO: {left} unexplained differences against the starting dump "
                  f"({len(undo['by_design'])} by design)")
            for row in undo["real"][:20]:
                (block, path, prop), was, now, _f = row
                print(f"  {block}{list(path)}.{prop}: {was!r} -> {now!r}")
            undo_ok = left == 0
        else:
            print(f"   import refused: {res.get('message')}")

    if not args.keep_profile:
        post("/api/profiles/delete", {"name": PROFILE, "force": True})
        print(f"   scratch profile '{PROFILE}' deleted")

    verdict = ("THE CLEAN RESTORE IS CLEAN." if unexplained == 0
               else f"{unexplained} DIFFERENCES ARE STILL UNEXPLAINED.")
    say(verdict + (" The desk is back to how it started."
                   if undo_ok else " Check the desk: the undo did not come back clean."))
    print(f"\n{verdict}")
    return 0 if (unexplained == 0 and undo_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
