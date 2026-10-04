"""Read the bind history the app records, and say what differs between a missed bind and a made one.

The app appends one line to `logs/bind_history.jsonl` in its data folder for every bind attempt
while the desk is in Expanded with multitrack on (`virtual_devices.record_bind`): attempt 0 is the
arrival itself, 1 onwards the follow-up's own re-enumerations. Each line carries what can be seen
of RODE's service from outside: pid, start time, threads, handles, private memory and its status
block.

    python DevTools/bind_history.py              every attempt, oldest first
    python DevTools/bind_history.py --last 20

The summary compares bound and missed attempts on each field. A field whose ranges do not overlap
is a lead; one that overlaps says nothing either way. The handle count moves by about twenty
between two reads a moment apart with nothing happening, so a gap that small is not a lead. A changed pid or start time means the service
was restarted between two attempts, which matters because a restart followed by an arrival is the
one recovery from a run of misses ever seen.

Read-only.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os

import _bootstrap  # noqa: F401  (puts Project/ on the path)

import virtual_devices
from paths import data_path

NUMERIC = ("threads", "handles", "private_kb")


def load(path: str) -> list:
    rows = []
    for name in (path + ".previous", path):
        if not os.path.exists(name):
            continue
        with open(name, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    return rows


def when(stamp) -> str:
    return datetime.datetime.fromtimestamp(stamp).strftime("%d %b %H:%M:%S") if stamp else "-"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--last", type=int, default=0, help="show only the last N attempts")
    args = ap.parse_args()

    path = os.path.join(data_path("logs"), virtual_devices.BIND_HISTORY_NAME)
    rows = load(path)
    if not rows:
        print(f"No bind attempts recorded yet ({path}).")
        return
    shown = rows[-args.last:] if args.last else rows

    print(f"{'time':16} {'try':>3} {'bound':6} {'pid':>6} {'thr':>4} {'handles':>7} "
          f"{'priv KB':>8}  block")
    previous = None
    for r in shown:
        block = r.get("block", "")
        changed = "same" if previous is not None and block == previous.get("block") else "CHANGED"
        if previous is None:
            changed = "-"
        restarted = previous is not None and r.get("started") != previous.get("started")
        print(f"{when(r.get('time')):16} {r.get('attempt', '?'):>3} "
              f"{'yes' if r.get('connected') else 'MISSED':6} {r.get('pid', '-'):>6} "
              f"{r.get('threads', '-'):>4} {r.get('handles', '-'):>7} "
              f"{r.get('private_kb', '-'):>8}  {changed}"
              + ("   CUT SHORT" if r.get("cut_short") else "")
              + ("   SERVICE RESTARTED" if restarted else ""))
        previous = r

    # A miss cut short by a mode change may be the desk leaving, not the service failing.
    bound = [r for r in rows if r.get("connected")]
    missed = [r for r in rows if not r.get("connected") and not r.get("cut_short")]
    print(f"\n{len(bound)} bound, {len(missed)} missed, across {len(rows)} attempts.")
    if not (bound and missed):
        print("Nothing to compare until there is at least one of each.")
        return
    for key in NUMERIC:
        b = [r[key] for r in bound if key in r]
        m = [r[key] for r in missed if key in r]
        if not (b and m):
            continue
        apart = max(b) < min(m) or max(m) < min(b)
        print(f"  {key:10} bound {min(b)}-{max(b)}, missed {min(m)}-{max(m)}"
              + ("   NO OVERLAP" if apart else ""))
    # The block is not summarised: it holds the connected flag and the desk's name, so a bound
    # block and a missed one always differ. Read the raw lines for what else moved.


if __name__ == "__main__":
    main()
