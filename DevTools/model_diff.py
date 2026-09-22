"""Snapshot every property in the model, and diff two snapshots. Block-aware.

WHY THIS EXISTS. Some things the desk does have no discoverable write on our side, only a
touchscreen gesture: opening the recordings PLAYBACK screen is the standing example, because the
user guide says every other audio function is muted while that screen is open, so the screen itself
is what opens the player's audio path. We cannot press it from software, but we CAN read the whole
model before and during, and the difference names the property.

That makes this the general tool for "the desk just did something, what changed?", and it beats a
USBPcap capture for this class of question: a capture shows what the RODE APP writes, and the app
never touches the unit's player at all (its Transfer Recordings screen plays the file on the
COMPUTER over Transfer Mode).

BLOCK-AWARE ON PURPOSE. Keys are `BLOCK.name#n`, not bare names. `btVisible` lives in both SYSTEM
and NETWORK and only the NETWORK copy drives the radio, so a diff that reported a bare name would
point at the wrong write (see PROJECT_GUIDE).

NOISE. Meters, clocks and progress figures move on their own. `--quiet` drops the properties known
to be self-moving so a real change is not buried; the dropped keys are counted, never hidden.

    python DevTools/model_diff.py snap before.json
    python DevTools/model_diff.py snap during.json
    python DevTools/model_diff.py diff before.json during.json [--quiet]
    python DevTools/model_diff.py watch during.json --seconds 20   # snap repeatedly, keep them all
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import rcp_read

# Properties that move by themselves, so a difference in them means nothing. Kept as a prefix/
# substring list rather than exact names because the meter and clock families are large.
NOISY = ("Meter", "meter", "Progress", "CurrentPosition", "elapsed", "Elapsed",
         "systemTime", "systemDate", "recordTime", "recordElapsed", "Rms", "Peak",
         "batteryLevel", "freeSpace", "FreeSpace", "remaining", "Remaining")


def is_noisy(key: str) -> bool:
    name = key.split(".", 1)[-1]
    return any(n in name for n in NOISY)


def snapshot() -> dict:
    """{'BLOCK.name#n': value} for every property occurrence in a fresh model read."""
    blob = rcp_read.read_model_stable(fresh=True)
    seen: dict[str, int] = {}
    out: dict[str, object] = {}
    for block, name, val, kind in rcp_read.iter_block_properties(blob):
        base = f"{block or '?'}.{name}"
        n = seen.get(base, 0)
        seen[base] = n + 1
        out[f"{base}#{n}"] = val if kind != "bytes" else repr(val)
    out["_modelLength"] = len(blob)
    return out


def cmd_snap(path: Path) -> int:
    snap = snapshot()
    path.write_text(json.dumps(snap, indent=1, default=repr), encoding="utf-8")
    print(f"SNAP {path}  {len(snap) - 1} properties, model {snap['_modelLength']} bytes")
    return 0


def cmd_watch(stem: Path, seconds: float) -> int:
    """Repeated snapshots while something is happening, so a transient state is not missed.

    A screen that opens and closes again between two hand-timed snapshots leaves no trace; this
    keeps every read, numbered, and reports which consecutive pairs differ at all.
    """
    shots, n = [], 0
    deadline = time.time() + seconds
    while time.time() < deadline:
        s = snapshot()
        p = stem.with_name(f"{stem.stem}_{n:02d}{stem.suffix}")
        p.write_text(json.dumps(s, indent=1, default=repr), encoding="utf-8")
        shots.append((p, s))
        print(f"  {p.name}  model {s['_modelLength']}")
        n += 1
    for (pa, a), (pb, b) in zip(shots, shots[1:]):
        d = [k for k in set(a) | set(b) if a.get(k) != b.get(k) and not is_noisy(k)]
        print(f"{pa.name} -> {pb.name}: {len(d)} non-noisy change(s)"
              + (f"  {', '.join(sorted(d)[:8])}" if d else ""))
    return 0


def cmd_diff(a_path: Path, b_path: Path, quiet: bool) -> int:
    a = json.loads(a_path.read_text(encoding="utf-8"))
    b = json.loads(b_path.read_text(encoding="utf-8"))
    changed, added, gone, dropped = [], [], [], 0
    for k in sorted(set(a) | set(b)):
        if k == "_modelLength":
            continue
        if k not in b:
            gone.append(k)
        elif k not in a:
            added.append(k)
        elif a[k] != b[k]:
            if quiet and is_noisy(k):
                dropped += 1
            else:
                changed.append((k, a[k], b[k]))

    print(f"model length {a.get('_modelLength')} -> {b.get('_modelLength')}"
          + ("  OK, no stray" if a.get("_modelLength") == b.get("_modelLength")
             else "  *** LENGTH CHANGED: something was APPENDED ***"))
    print(f"\n{len(changed)} changed value(s)"
          + (f", {dropped} self-moving one(s) hidden by --quiet" if dropped else ""))
    for k, av, bv in changed:
        print(f"  {k}\n      {av!r}  ->  {bv!r}")
    # A property APPEARING is the stray-append signature, and it matters more than a changed value.
    for label, keys in (("ONLY IN THE FIRST", gone), ("ONLY IN THE SECOND", added)):
        if keys:
            print(f"\n{len(keys)} {label}:")
            for k in keys[:40]:
                print(f"  {k}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("snap", "diff", "watch"))
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("--quiet", action="store_true", help="hide meters, clocks and progress")
    ap.add_argument("--seconds", type=float, default=20.0, help="for watch")
    args = ap.parse_args()

    if args.cmd == "snap":
        return cmd_snap(args.paths[0])
    if args.cmd == "watch":
        return cmd_watch(args.paths[0], args.seconds)
    if len(args.paths) != 2:
        print("diff needs two snapshot files")
        return 1
    return cmd_diff(args.paths[0], args.paths[1], args.quiet)


if __name__ == "__main__":
    raise SystemExit(main())
