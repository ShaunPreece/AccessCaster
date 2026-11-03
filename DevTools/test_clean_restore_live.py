"""THE WHOLE CLEAN RESTORE, end to end, with a whole-model diff.

WHAT IT DOES, through the app's own actions in this process, so it exercises the shipping path
(quit AccessCaster first):
  1. dumps the desk as it stands, and notes every mute and solo,
  2. saves a profile of it, pads included,
  3. runs a CLEAN restore (which exports a backup, software-factory-resets the desk, then writes the
     profile back on top) and polls its progress,
  4. dumps the desk again and diffs the two, splitting every difference into by-design,
     back-at-the-factory-value and REAL,
  5. puts back what a restore deliberately never writes (mutes and solos), first re-running the
     profile as an ordinary restore if the clean one failed, and diffs THAT against the starting
     dump too, so the undo is proven rather than assumed.

**IT FACTORY-RESETS THE DESK**, so it refuses without --yes. The undo is the profile, not the show
backup the restore takes: a show import straight after a reset is the pair that leaves a mic silent,
and a desk that has refused one import refuses every later one until it is power-cycled. Once the
restore has started the undo runs however the run ends, and the run reports whether it came back
clean. The show backup stays on the drive as the last resort.

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
from _desk import wait_for_job
from analyse_clean_restore import compare, show, snap

CAP = Path(__file__).resolve().parent / "captures"
FACTORY_DUMP = CAP / "post-showreset-raw.bin"     # a real factory dump of THIS desk
PROFILE = "Clean restore retest"
OUTPUT_MUTES = (("monitor", "outputMonMute"), ("bluetooth", "outputBTMute"))
server = None


def say(text: str) -> None:
    """Speak a one-line status, unless Claudio has the focus, and print it as the record."""
    _desk.say(text, gated=True, wait=True)
    print(f"** {text}")


def poll(status, label: str, timeout: float = 900.0) -> dict:
    """Wait for the job behind the status function `status`, printing each new message. A job still
    running at `timeout` comes back as a failure, so the undo still runs."""
    said = [None]

    def print_new(st: dict) -> None:
        msg = st.get("message") or st.get("label") or ""
        if msg and msg != said[0]:
            print(f"   {msg}")
            said[0] = msg

    try:
        return wait_for_job(status, timeout, 1.5, print_new)
    except TimeoutError as e:
        return {"done": False, "ok": False, "message": f"{label}: {e}"}


def dump(name: str) -> tuple[Path, bytes]:
    path = CAP / f"{name}.bin"
    blob = rcp_read.read_model_stable(fresh=True)
    path.write_bytes(blob)
    print(f"   dumped {path.name} ({len(blob)} bytes)")
    return path, blob


def live_state(blob: bytes) -> dict:
    """Every mute and solo, the state a restore never writes. A channel the page reports neither
    for (one on no fader) is left out: the app has no route to it either."""
    chans = [(c["name"], c.get("muted"), c.get("solo"))
             for c in server.action_get_channels().get("channels") or []]
    return {
        "mute": {n: m for n, m, _s in chans if m is not None},
        "solo": {n: s for n, _m, s in chans if s is not None},
        "output": {w: rcp_read.block_value(blob, "OUTPUT", prop) for w, prop in OUTPUT_MUTES},
    }


def put_live_state_back(state: dict) -> None:
    for name, on in state["mute"].items():
        server.action_set_channel_mute(name, bool(on))
    for name, on in state["solo"].items():
        server.action_set_channel_solo(name, bool(on))
    for which, on in state["output"].items():
        if on is not None:
            server.action_set_output_mute(which, bool(on))


def put_desk_back(before_path: Path, state: dict, restored: bool) -> bool:
    """Put the desk back as it started, and diff it against the starting dump. True when it came
    back with nothing unexplained."""
    if not restored:
        say("The clean restore did not finish, so I am restoring the profile again, without the "
            "reset.")
        res = server.restore_profile(PROFILE, False)
        if not res.get("ok"):
            say(f"That restore would not start. Restore the profile called {PROFILE} by hand.")
            print(f"   restore refused: {res.get('message')}")
            return False
        st = poll(server.profile_restore_status, "the ordinary restore")
        print(f"   ordinary restore finished: ok={st.get('ok')} - {st.get('message')}")
    say("Putting the mutes and solos back as they were.")
    put_live_state_back(state)
    time.sleep(2.0)
    undo_path, _blob = dump("post-cleanretest-undo-raw")
    undo = compare(snap(before_path), snap(undo_path), None, pads_restored=True)
    left = undo["real"] + undo["gone"] + undo["new"]
    print(f"\nTHE UNDO: {len(left)} unexplained differences against the starting dump "
          f"({len(undo['by_design'])} by design)")
    for (path, block, prop), was, now, _f in left[:20]:
        print(f"  {block}{list(path)}.{prop}: {was!r} -> {now!r}")
    return not left


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="required: this factory-resets the desk")
    ap.add_argument("--keep-profile", action="store_true")
    args = ap.parse_args()
    if not args.yes:
        print("This FACTORY-RESETS the desk (and puts it back afterwards). Re-run with --yes.")
        return 2
    global server
    server = _desk.service()

    say("Starting the clean restore retest. I am dumping the desk, then saving a profile of it "
        "with its pads, which takes a few minutes.")
    before_path, before_blob = dump("pre-cleanretest-raw")
    state = live_state(before_blob)

    res = server.snapshot_profile(PROFILE, include_pads=True)
    if not res.get("ok"):
        print(f"could not save the profile: {res.get('message')}")
        return 1
    st = poll(server.profile_save_status, "the profile save")
    print(f"   profile saved: {st.get('message')}")
    if not st.get("ok"):
        return 1

    say("Now the clean restore. It backs the desk up, factory resets it, and writes the profile "
        "back on top.")
    res = server.restore_profile(PROFILE, True)
    if not res.get("ok"):
        print(f"could not start the restore: {res.get('message')}")
        return 1
    # FROM HERE THE DESK MAY BE AT FACTORY SETTINGS, so the undo runs however this ends.
    unexplained = None
    undo_ok = False
    restored = False
    try:
        st = poll(server.profile_restore_status, "the clean restore")
        print(f"   restore finished: ok={st.get('ok')} - {st.get('message')}")
        restored = bool(st.get("ok"))
        if not restored:
            say("The clean restore reported a failure, so there is nothing to diff.")
        else:
            time.sleep(2.0)
            say("Restore done. Dumping the desk and diffing every property against how it started.")
            after_path, _blob = dump("post-cleanretest-raw")
            factory = snap(FACTORY_DUMP) if FACTORY_DUMP.is_file() else None
            diff = compare(snap(before_path), snap(after_path), factory, pads_restored=True)
            print()
            unexplained = show(diff, factory is not None)
    finally:
        undo_ok = put_desk_back(before_path, state, restored)
        # The profile is the undo, so it is kept whenever the undo did not come back clean.
        if undo_ok and not args.keep_profile:
            gone = server.delete_profile(PROFILE, True)
            print(f"   scratch profile '{PROFILE}' deleted" if gone.get("ok")
                  else f"   COULD NOT DELETE the scratch profile '{PROFILE}': {gone.get('message')}")

    back = (" The desk is back to how it started." if undo_ok
            else f" Check the desk: the undo did not come back clean. The profile {PROFILE} is "
                 f"kept to restore it from.")
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
