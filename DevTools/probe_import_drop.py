"""Repeat a SHOW IMPORT with a CONTROLLED amount of work to do, until the desk leaves the USB bus.

NAMED `probe_`, NOT `test_`, AND BOTH HALVES OF THAT MATTER. It drives real hardware and asserts
nothing, so calling it a test would put it in the path of anything that sweeps the offline suites,
which is how a hardware script factory-resets a working desk ten times over. And
`DevTools/_bootstrap` redirects the DATA DIR for any script whose name starts with `test_`, which
would point this at a scratch profiles folder and make every trial a no-op.

RE-IMPORTING THE SAME BACKUP MEASURES NOTHING, and avoiding that is the whole design here. Once the
first import lands, the desk already matches that backup, so every trial after it is the "nothing
to do" case - which has never dropped a desk. It shows up as a run of trials all taking the same
suspiciously short time: 6.3 seconds each, identical to a tenth of a second, against 97 s for a real
import.

SO THE DELTA IS THE INDEPENDENT VARIABLE. Before each trial this disturbs `--delta` real settings,
proves they took, then imports the backup that holds the originals. The desk therefore has a known
and repeatable amount of rewriting to do, and the question stops being "does it drop" and becomes
"does dropping track how much the desk has to rewrite". Better than alternating between two whole
backups: the desk never leaves its owner's own setup, and the size is a dial rather than a switch.

THE JOB DURATION IS THE HONEST WORK PROXY. 6.3 s means the desk did essentially nothing whatever
the delta claimed; a real import runs far longer. Every trial prints it, so a run that quietly
degenerated into no-ops cannot be read as a clean result.

IT DRIVES THE RUNNING SERVER OVER HTTP AND OPENS NOTHING ITSELF. The app owns the device, and a
second process with its own handle would be a variable in the measurement rather than a tool for
it. Presence comes from PowerShell `Get-PnpDevice` for the same reason `test_usb_drop_live` uses it:
`hid.enumerate` OPENS each device on Windows, so asking the question would put traffic on the wire
inside the window that has to stay quiet.

EVERYTHING IT DISTURBS IS SILENT AND REVERSIBLE: mic gains and virtual fader levels, both of which
a profile and a show carry. It never touches phantom power (a pop on a live mic), the monitor
speaker level (speakers are connected) or anything on the never-sweep list.

`--via profile` IS THE ARM THAT MATTERS, AND WITHOUT IT THIS SCRIPT ASSUMES ITS OWN CONCLUSION.
A profile restore leaves the desk in the same place by writing ~148 properties one at a time: no
show command, no reload from the desk's own file, no mass-storage gadget. So it isolates SHOWS from
"talking to the desk a lot". Run the same delta and the same count both ways and compare:

  * same drop rate either way  -> shows are exonerated, and dropping the feature would cost it for
    nothing while leaving the fault exactly where it is
  * shows drop and profiles do not -> shows are finally implicated, on more than association

The arm is not optional, because the case against shows is association and nothing more: every known
drop happens around an import, but the dropout predates the shows feature by six weeks and has
happened with nothing of ours running.

Usage, with the app or `DevTools/run_dev_server.py` already serving:

    python DevTools/probe_import_drop.py --list
    python DevTools/probe_import_drop.py --backup NAME --delta 7 --trials 10
    python DevTools/probe_import_drop.py --backup NAME --delta 7 --trials 10 --via profile
    python DevTools/probe_import_drop.py --backup NAME --delta 0     # a true no-op, for comparison
"""
import _bootstrap  # noqa: F401  (puts Project/ on the path; import FIRST)

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

import diagnostics
import paths

BASE = "http://127.0.0.1:8770"
SETTLE_S = 6.0          # between trials, so one import is not still landing when the next starts
STATUS_POLL_S = 2.0
JOB_CEILING_S = 240.0   # a job that outlives this is itself the finding
NOOP_S = 8.0            # a job quicker than this did essentially nothing, whatever we asked for


def present() -> int:
    """How many VID_19F7 devices Windows has present, WITHOUT opening any of them."""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "@(Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | "
         "Where-Object { $_.InstanceId -match 'VID_19F7' }).Count"],
        capture_output=True, text=True)
    try:
        return int(out.stdout.strip())
    except ValueError:
        return -1


def api(path: str, timeout: float = 90.0, post: dict | None = None) -> dict:
    """GET, or POST with a JSON body when `post` is given. Action routes take their arguments in a
    JSON BODY and `do_POST` matches the path EXACTLY, so a query string makes it miss and 404."""
    data = json.dumps(post).encode() if post is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}


# ---- what we disturb, and how we read it back ----------------------------------------------
# Each knob is (name, read, write). They are applied in list order, so `--delta N` is always the
# same N knobs and two runs at the same delta are comparable.

def _mic_gains() -> list:
    got = api("/api/channels")
    out = []
    for c in got.get("channels") or []:
        if c.get("kind") == "mic" and isinstance(c.get("level_db"), int):
            n = int(c["name"].replace("Mic", ""))
            out.append((f"gain {c['name']}",
                        lambda name=c["name"]: _one_channel(name, "level_db"),
                        lambda v, n=n: api("/api/channel/gain", post={"channel": n, "db": v})))
    return out


def _one_channel(name: str, key):
    for c in api("/api/channels").get("channels") or []:
        if c.get("name") == name:
            return c.get(key)
    return None


def _fader_levels() -> list:
    out = []
    for f in api("/api/faders/levels").get("faders") or []:
        if isinstance(f.get("percent"), int):
            i = f["index"]
            out.append((f"level fader {i + 1}",
                        lambda i=i: _one_fader(i),
                        lambda v, i=i: api("/api/faders/level",
                                           post={"fader": i, "percent": v})))
    return out


def _one_fader(index: int):
    for f in api("/api/faders/levels").get("faders") or []:
        if f.get("index") == index:
            return f.get("percent")
    return None


def knobs() -> list:
    """Every setting we are willing to disturb, in a fixed order. Mic gains first because they are
    the ones a backup most obviously owns and the easiest to verify."""
    return _mic_gains() + _fader_levels()


def nudge(value: int) -> int:
    """A different value, well away from the original and inside every range these knobs have."""
    return value - 12 if value >= 25 else value + 12


# ---- restarting the server over the top of itself -------------------------------------------
# THIS IS A FAULT CONDITION, NOT A CONVENIENCE. `server._close_previous_instances` taskkills
# whatever is holding port 8770, and the incumbent is holding TWO open HID handles on MI_09 - the
# announcer and the recorder watcher, both long-lived read handles. A force-kill tears those down
# without ever closing them, so the desk is left with handles that were never released.
#
#
# That makes it a candidate in its own right: a captured drop came two minutes after exactly this,
# while fifteen clean imports all ran on a server that came up with nobody to kill.

def listening_pid() -> int:
    """The PID listening on 8770, or 0. netstat rather than anything that opens the device."""
    out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True)
    for line in out.stdout.splitlines():
        if "LISTENING" in line and line.split()[1].endswith(":8770"):
            try:
                return int(line.split()[-1])
            except ValueError:
                return 0
    return 0


def restart_over_the_top() -> bool:
    """Start a fresh dev server, which force-kills the incumbent. True once the NEW one is serving.

    Waits on the listening PID CHANGING rather than on the port answering, because the old server
    answers every endpoint right up to the moment it is killed.
    """
    was = listening_pid()
    subprocess.Popen([sys.executable, _bootstrap.devtools_path("run_dev_server.py")],
                     cwd=_bootstrap.REPO, creationflags=0x08000000)   # CREATE_NO_WINDOW
    deadline = time.time() + 90.0
    while time.time() < deadline:
        time.sleep(2.0)
        now = listening_pid()
        if now and now != was:
            time.sleep(4.0)            # let its startup read and announcer settle
            return True
    return False


def run_restore(name: str, via: str) -> dict:
    """Fire the restore and wait for the job, returning its final status.

    `via` IS THE CONTROL, AND IT IS THE POINT OF THIS SCRIPT. "show" sends `showControlImport` and
    lets the desk reload itself from its own file; "profile" passes clean=False with a profile-only
    path, writing ~148 properties one at a time and never sending a show command or touching the
    mass-storage gadget. Both leave the desk in the same place, so a drop rate that is the same
    either way exonerates shows entirely, and one that is not finally implicates them.

    Without this arm the script assumes what it is supposed to be testing. Every known drop happens
    around an import, but the dropout predates the shows feature by six weeks, so association is
    all anybody has.

    The reply's `via` says which job to poll: the two halves have separate job states.
    """
    # THE PROFILE ARM GOES STRAIGHT TO `/api/profiles/restore`, NOT to the backup chooser.
    # `/api/backup/restore` prefers the paired show whenever one is on the drive, and its `clean`
    # flag does NOT select the profile path the way it reads: clean=True forces the profile, but
    # only by FACTORY-RESETTING the desk first, which is no control at all. Passing clean=False and
    # expecting the profile runs a SHOW IMPORT instead, so the control arm becomes a second copy of
    # the arm it is meant to be compared against.
    if via == "profile":
        started = api("/api/profiles/restore", post={"name": name, "clean": False})
        status = "/api/profiles/restore/status"
        started.setdefault("via", "profile")
    else:
        started = api("/api/backup/restore", post={"name": name})
        status = ("/api/shows/status" if started.get("via") == "show"
                  else "/api/profiles/restore/status")
    if not started.get("ok"):
        return {"ok": False, "message": started.get("message") or "the restore did not start"}
    at = time.time()
    deadline = at + JOB_CEILING_S
    while time.time() < deadline:
        time.sleep(STATUS_POLL_S)
        st = api(status, timeout=15.0)
        if st.get("running"):
            continue
        if st.get("done"):
            st["seconds"] = time.time() - at
            st["ran_via"] = started.get("via")
            return st
    return {"ok": False, "seconds": time.time() - at, "ran_via": started.get("via"),
            "message": f"the job was still running after {JOB_CEILING_S:.0f}s"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backup", help="the backup name to restore, as the Backups page lists it")
    ap.add_argument("--trials", type=int, default=10)
    ap.add_argument("--delta", type=int, default=8,
                    help="how many settings to disturb before each import (0 = the control arm)")
    ap.add_argument("--via", choices=("show", "profile"), default="show",
                    help="show: the import (showControlImport). profile: the CONTROL, ~148 "
                         "property writes, no show command and no storage gadget")
    ap.add_argument("--restart", choices=("none", "kill"), default="none",
                    help="kill: start a fresh server over the top before each import, which "
                         "force-kills the incumbent and its two open HID handles")
    ap.add_argument("--list", action="store_true", help="list the backups and the knobs, then exit")
    args = ap.parse_args()

    if args.list:
        print("backups:")
        for b in api("/api/backups").get("backups") or []:
            print(f"  {b['kind']:<8} {b['name']}")
        print("\nknobs, in the order --delta takes them:")
        for i, (label, read, _w) in enumerate(knobs(), 1):
            print(f"  {i:>2}. {label:<22} currently {read()}")
        return 0
    if not args.backup:
        ap.error("--backup is required (use --list to see them)")

    baseline_devices = present()
    if baseline_devices <= 0:
        print(f"REFUSING TO START: Windows has {baseline_devices} RODE devices present.")
        return 2

    all_knobs = knobs()
    chosen = all_knobs[:max(0, args.delta)]
    if args.delta > len(all_knobs):
        print(f"Only {len(all_knobs)} knobs exist on this desk; using all of them.")

    print(f"{args.trials} x restore of {args.backup!r} via={args.via}, "
          f"delta={len(chosen)} settings, restart={args.restart}, "
          f"{baseline_devices} devices present.")
    print(f"The diagnostic log is {diagnostics.log_path()}")
    print(f"A job under {NOOP_S:.0f}s did essentially nothing: treat that trial as a no-op.")
    print("Stops on the first drop. Ctrl+C is safe between trials.\n")

    noops = 0
    for n in range(1, args.trials + 1):
        if args.restart == "kill":
            if not restart_over_the_top():
                print(f"  trial {n}: the replacement server never took the port. Stopping.")
                return 1
            if present() < baseline_devices:
                print(f"\nDROPPED ON THE RESTART ITSELF, before trial {n}'s import. That would be "
                      f"a bigger finding than the import: the kill alone did it.")
                return 1
        stamp = time.strftime("%H:%M:%S")
        # DISTURB, AND PROVE IT TOOK. A knob that silently refuses turns the trial into a no-op
        # that runs, and passes, without changing anything on the desk.
        moved = 0
        for label, read, write in chosen:
            was = read()
            if not isinstance(was, int):
                continue
            write(nudge(was))
            if read() != was:
                moved += 1
        if chosen and not moved:
            print(f"  trial {n}: SKIPPED, nothing would move, so there was nothing to import.")
            return 1

        res = run_restore(args.backup, args.via)
        after = present()
        secs = res.get("seconds") or 0.0
        verdict = "ok" if res.get("ok") else "FAILED"
        flag = "  <- NO-OP" if secs < NOOP_S and chosen else ""
        print(f"  trial {n} at {stamp}: {verdict}, {secs:5.1f}s, moved={moved}, "
              f"ran_via={res.get('ran_via')}, devices={after}{flag}")
        if not res.get("ok"):
            print(f"            {(res.get('message') or '')[:100]}")
        if secs < NOOP_S and chosen:
            noops += 1

        if after < baseline_devices:
            print(f"\nDROPPED ON TRIAL {n}, at about {stamp}, with {moved} settings to restore.")
            print(f"Windows now has {after} of {baseline_devices} devices present.")
            print("READ THE LOG AROUND THAT TIME. What to look for, in order:")
            print("  * the gap between `send showControlImport` and the first 0-byte `model.read`")
            print("  * any `hid.enumerate.slow` line, which is the 30-second hole in the first")
            print("    capture and the one thing still unexplained")
            print("  * `model.unstable`, meaning no two reads of the desk ever agreed")
            print("Then the Windows event log, for the 1010 / 1011 pair 22 seconds apart:")
            print("  Get-WinEvent -FilterHashtable @{LogName=")
            print("    'Microsoft-Windows-Kernel-PnP/Device Management'; "
                  "StartTime=(Get-Date).AddMinutes(-10)}")
            print("\nReplug the desk before running anything else.")
            return 1
        time.sleep(SETTLE_S)

    print(f"\n{args.trials} trials at delta={len(chosen)}, no drop, "
          f"{present()} devices still present.")
    if noops:
        print(f"WARNING: {noops} of them ran in under {NOOP_S:.0f}s, so the desk was not really "
              f"rewriting anything. Those trials prove nothing; raise --delta.")
    print("A clean run is NOT proof the fault is gone: the measured rate is about 1 in 6, so a "
          "run of this size can miss it by luck alone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
