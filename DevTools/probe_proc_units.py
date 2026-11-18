"""Put one known normalised value on every processing parameter of an unused mic, so RODE's app can
be read against it, then put the strip back. For finding each parameter's real-unit curve.

Three separate runs, because RODE's app is read between them:
  baseline       save Mic N's chain to a file in %TEMP%; refuses if one is already saved, so a
                 test state can never be saved over the real baseline
  set V          write V (0..1) to every curve parameter; turns advanced processing on, which
                 RODE's app needs to show the knobs
  restore        write the saved chain back, prove it byte-identical with no stray, delete the file

The pan legs and the VoxLab knobs are left alone: pan is not a unit question, and a knob write
re-derives other values.

Runs on Mic 3 by default: nothing is plugged into it, and Mic 1 is never written by tests.
Usage: python DevTools/probe_proc_units.py baseline|set V|restore [--channel 3]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401
import channels
import rcp_read
from _desk import count_changes, service, stray_counts

SETTLE = 0.9
SKIP = {"channelPan", "channelPanL", "channelPanR", *channels.VOXLAB_KNOB_PROPS}
CURVE_PROPS = sorted(p for p in channels.PRESET_FLOAT_PROPS if p not in SKIP)
CHAIN_PROPS = (sorted(channels.PRESET_FLOAT_PROPS) + list(channels.PROCESSING_MODULE_BOOLS)
               + [channels.CHAIN_ADVANCED_PROP, channels.CHAIN_PRESET_MARKER_PROP])


def _baseline_path(ch: int) -> str:
    return os.path.join(tempfile.gettempdir(), f"accesscaster_proc_units_mic{ch}.json")


def read_strip(strip: int, props) -> tuple[dict, bytes]:
    """Each of `props` on one strip, and the fresh dump they came from."""
    blob = rcp_read.read_model_stable(fresh=True)
    out = {}
    for p in props:
        vals = [v for v, _k in rcp_read.find_all(blob, p)]
        out[p] = vals[strip] if strip < len(vals) else None
    return out, blob


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=("baseline", "set", "restore"))
    ap.add_argument("value", nargs="?", type=float)
    ap.add_argument("--channel", type=int, default=3)
    args = ap.parse_args()
    ch = args.channel
    if ch == 1:
        raise SystemExit("Mic 1 is never written by tests.")
    service()
    strip = channels.channel_strip_index(ch)
    path = _baseline_path(ch)

    if args.command == "baseline":
        if os.path.exists(path):
            raise SystemExit(f"A baseline is already saved at {path}: restore it first.")
        chain, blob = read_strip(strip, CHAIN_PROPS)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"strip": strip, "chain": chain, "counts": stray_counts(blob)}, f, indent=1)
        print(f"Mic {ch} (strip {strip}) baseline saved to {path}")
        for p in CHAIN_PROPS:
            print(f"  {p} = {chain[p]}")
        return 0

    if not os.path.exists(path):
        raise SystemExit("No baseline saved: run baseline first.")
    with open(path, encoding="utf-8") as f:
        saved = json.load(f)
    if saved["strip"] != strip:
        raise SystemExit(f"Mic {ch} has moved from strip {saved['strip']} to {strip}: stopping.")

    if args.command == "set":
        if args.value is None or not 0.0 <= args.value <= 1.0:
            raise SystemExit("set needs a value from 0 to 1.")
        props = {channels.CHAIN_ADVANCED_PROP: True}
        props.update({p: args.value for p in CURVE_PROPS})
        channels.apply_processing_preset(ch, props)
        time.sleep(SETTLE)
        got, _ = read_strip(strip, CURVE_PROPS)
        off = {p: got[p] for p in CURVE_PROPS if got[p] is None or abs(got[p] - args.value) > 1e-6}
        print(f"Mic {ch}: every curve parameter set to {args.value}"
              + (f"; NOT TAKEN: {off}" if off else ", all read back"))
        return 1 if off else 0

    base = saved["chain"]
    channels.apply_processing_preset(ch, {p: v for p, v in base.items() if v is not None})
    time.sleep(SETTLE)
    back, blob = read_strip(strip, CHAIN_PROPS)
    drift = {p: (base[p], back[p]) for p in CHAIN_PROPS if base[p] != back[p]}
    added, gone, moved = count_changes(saved["counts"], stray_counts(blob))
    if drift or added or gone or moved:
        print(f"RESTORE FAILED: drift {drift}, added {added}, gone {gone}, moved {moved}")
        print(f"The baseline is kept at {path}.")
        return 1
    os.remove(path)
    print(f"Mic {ch} restored byte-identical, no stray; baseline file deleted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
