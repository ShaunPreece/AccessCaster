"""Repeat a SHOW IMPORT with a CONTROLLED amount of work to do, until the desk leaves the USB bus.

NAMED `probe_`, NOT `test_`, AND BOTH HALVES OF THAT MATTER. It drives real hardware and asserts
nothing, so calling it a test would put it in the path of anything that sweeps the offline suites,
which is how a hardware script factory-resets a working desk ten times over. And
`DevTools/_bootstrap` redirects the DATA DIR for any script whose name starts with `test_`, which
would point this at a scratch profiles folder and make every trial a no-op.

RE-IMPORTING THE SAME BACKUP MEASURES NOTHING, and avoiding that is the whole design here. Once the
first import lands, the desk already matches that backup, so every trial after it is the "nothing
to do" case. It shows as a run of trials that all take the same few seconds, where a real import
takes a minute or more.

SO THE DELTA IS THE INDEPENDENT VARIABLE. Before each trial this disturbs `--delta` real settings,
proves they took, then imports the backup that holds the originals. The desk therefore has a known
and repeatable amount of rewriting to do, and the question stops being "does it drop" and becomes
"does dropping track how much the desk has to rewrite". Better than alternating between two whole
backups: the desk never leaves its owner's own setup, and the size is a dial rather than a switch.

THE JOB DURATION IS THE HONEST WORK PROXY. A job under NOOP_S means the desk did essentially nothing
whatever the delta claimed. Every trial prints it, so a run that quietly degenerated into no-ops
cannot be read as a clean result.

IT RUNS THE APP'S SERVICE ITSELF, THE WATCHERS INCLUDED, so the desk sees the handles it sees
when the app is running and nothing more. Quit AccessCaster first. Presence comes from PowerShell
`Get-PnpDevice` for the same reason `test_usb_drop_live` uses it: `hid.enumerate` OPENS each device
on Windows, so asking the question would put traffic on the wire inside the window that has to
stay quiet.

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

The arm is not optional, because the case against shows is association and nothing more: the
dropout also happens with nothing of ours running.

Usage, with AccessCaster quit:

    python DevTools/probe_import_drop.py --list
    python DevTools/probe_import_drop.py --backup show:N --delta 7 --trials 10
    python DevTools/probe_import_drop.py --backup NAME --delta 7 --trials 10 --via profile
    python DevTools/probe_import_drop.py --backup show:N --delta 0     # a true no-op, for comparison
"""
import argparse
import time

import _bootstrap  # puts Project/ on the path; import FIRST
import diagnostics
import _desk
from _desk import rode_devices_present, wait_for_job

server = None

SETTLE_S = 6.0          # between trials, so one import is not still landing when the next starts
STATUS_POLL_S = 2.0
JOB_CEILING_S = 240.0   # a job that outlives this is itself the finding
NOOP_S = 8.0            # a job quicker than this did essentially nothing, whatever we asked for


# ---- what we disturb, and how we read it back ----------------------------------------------
# Each knob is (name, read, write). They are applied in list order, so `--delta N` is always the
# same N knobs and two runs at the same delta are comparable.

def _mic_gains() -> list:
    got = server.action_get_channels()
    out = []
    for c in got.get("channels") or []:
        if c.get("kind") == "mic" and isinstance(c.get("level_db"), int):
            n = int(c["name"].replace("Mic", ""))
            out.append((f"gain {c['name']}",
                        lambda name=c["name"]: _one_channel(name, "level_db"),
                        lambda v, n=n: server.action_set_gain(n, v)))
    return out


def _one_channel(name: str, key):
    for c in server.action_get_channels().get("channels") or []:
        if c.get("name") == name:
            return c.get(key)
    return None


def _fader_levels() -> list:
    out = []
    for f in server.fader_levels_live().get("faders") or []:
        if isinstance(f.get("percent"), int):
            i = f["index"]
            out.append((f"level fader {i + 1}",
                        lambda i=i: _one_fader(i),
                        lambda v, i=i: server.action_set_fader_level(i, v)))
    return out


def _one_fader(index: int):
    for f in server.fader_levels_live().get("faders") or []:
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


def put_back(chosen: list, originals: dict) -> list:
    """Write each knob's original value back. Returns the labels that did not read back."""
    left = []
    for label, read, write in chosen:
        want = originals.get(label)
        if not isinstance(want, int):
            continue
        write(want)
        if read() != want:
            left.append(label)
    return left


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
    # THE PROFILE ARM GOES STRAIGHT TO `restore_profile`, NOT to the backup chooser, whose
    # `clean=True` forces the profile only by FACTORY-RESETTING the desk first.
    if via == "profile":
        started = server.restore_profile(name, False)
        status = server.profile_restore_status
        started.setdefault("via", "profile")
    else:
        started = server.action_restore_backup(name)
        status = (server.show_job_status if started.get("via") == "show"
                  else server.profile_restore_status)
    if not started.get("ok"):
        return {"ok": False, "message": started.get("message") or "the restore did not start"}
    at = time.monotonic()
    try:
        st = wait_for_job(status, JOB_CEILING_S, STATUS_POLL_S)
    except TimeoutError as e:
        st = {"ok": False, "message": str(e)}
    st["seconds"] = time.monotonic() - at
    st["ran_via"] = started.get("via")
    return st


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
    ap.add_argument("--list", action="store_true", help="list the backups and the knobs, then exit")
    args = ap.parse_args()
    global server
    server = _desk.service(start=True)

    if args.list:
        print("backups:")
        for b in server.read_backups().get("backups") or []:
            show = f"   (--via show: show:{b['snapshot']})" if b.get("snapshot") is not None else ""
            print(f"  {b['kind']:<8} {b['name']}{show}")
        print("\nknobs, in the order --delta takes them:")
        for i, (label, read, _w) in enumerate(knobs(), 1):
            print(f"  {i:>2}. {label:<22} currently {read()}")
        return 0
    if not args.backup:
        ap.error("--backup is required (use --list to see them)")
    # `action_restore_backup` restores a paired backup through its profile and never imports its show,
    # so a paired name on the show arm would run the control arm under the show arm's label.
    if args.via == "show" and not args.backup.startswith("show:"):
        ap.error(f"--via show needs a show:<snapshot> name; {args.backup!r} would restore through "
                 "its profile (use --list to see the snapshots)")

    baseline_devices = rode_devices_present()
    if baseline_devices <= 0:
        print(f"REFUSING TO START: Windows has {baseline_devices} RODE devices present.")
        return 2

    all_knobs = knobs()
    chosen = all_knobs[:max(0, args.delta)]
    if args.delta > len(all_knobs):
        print(f"Only {len(all_knobs)} knobs exist on this desk; using all of them.")
    # EVERY TRIAL NUDGES FROM THESE, not from whatever the desk holds when it starts, so a restore
    # that falls short cannot walk the settings further from the owner's own with each trial.
    originals = {label: read() for label, read, _w in chosen}

    print(f"{args.trials} x restore of {args.backup!r} via={args.via}, "
          f"delta={len(chosen)} settings, "
          f"{baseline_devices} devices present.")
    print(f"The diagnostic log is {diagnostics.log_path()}")
    print(f"A job under {NOOP_S:.0f}s did essentially nothing: treat that trial as a no-op.")
    print("Stops on the first drop. Ctrl+C is safe between trials.\n")

    noops = 0
    for n in range(1, args.trials + 1):
        stamp = time.strftime("%H:%M:%S")
        # DISTURB, AND PROVE IT TOOK. A knob that silently refuses turns the trial into a no-op
        # that runs, and passes, without changing anything on the desk.
        moved = 0
        try:
            for label, read, write in chosen:
                was = originals[label]
                if not isinstance(was, int):
                    continue
                write(nudge(was))
                if read() != was:
                    moved += 1
            if chosen and not moved:
                print(f"  trial {n}: SKIPPED, nothing would move, so there was nothing to import.")
                return 1
            res = run_restore(args.backup, args.via)
        except BaseException:
            # Stopped between the nudge and the restore's end (Ctrl+C, a request that raised):
            # the restore that would have put the knobs back has not, so put them back here.
            put_back(chosen, originals)
            raise
        after = rode_devices_present()
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
            print("\nReplug the desk before running anything else. The settings this run nudged "
                  "may still be off: their values as found were")
            for label, value in originals.items():
                print(f"  {label}: {value}")
            return 1
        if not res.get("ok"):
            left = put_back(chosen, originals)
            print("\nTHE RESTORE FAILED, so the settings it was to put back were written back "
                  "directly." + (f" These did not read back: {', '.join(left)}." if left else ""))
            print("Stopping, so the next trial does not start from a desk the restore left wrong.")
            return 1
        time.sleep(SETTLE_S)

    print(f"\n{args.trials} trials at delta={len(chosen)}, no drop, "
          f"{rode_devices_present()} devices still present.")
    if noops:
        print(f"WARNING: {noops} of them ran in under {NOOP_S:.0f}s, so the desk was not really "
              f"rewriting anything. Those trials prove nothing; raise --delta.")
    print("A clean run is NOT proof the fault is gone: it is intermittent, so a run of this size "
          "can miss it by luck alone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
