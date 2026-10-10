"""LIVE TEST: every mixer-action pad create, on real hardware.

For each mixer mode (Censor, Trash Talk, Fade, Back Channel, Ducking), on one EMPTY pad:
  1. create it with soundpad.create_mixer;
  2. read the pad back and check its type, mode and trigger mode against what was asked for;
  3. clear it and check the pad is empty and the effect pool is unchanged.

A mixer pad writes NO region-B object, so the pool before and after every create must be identical;
a difference means the create touched the pool.

EXCLUSIVE DEVICE ACCESS: stop the running service first (the app owns the HID handle), then restart
it with `python DevTools/run_dev_server.py`.

Usage:
    python DevTools/test_pad_mixer_live.py --pad 17               # all five modes, cleared after each
    python DevTools/test_pad_mixer_live.py --pad 17 --mode fade   # one mode only
    python DevTools/test_pad_mixer_live.py --pad 17 --clear       # just clear that pad again
"""
import argparse

import _bootstrap  # noqa: F401  - puts Project/ on sys.path

import soundpad


def read_pad(grid):
    return next((p for p in soundpad.read_pads_live()["pads"] if p["grid"] == grid), None)


def run_mode(grid, mode, pool_before):
    label = soundpad.MIXER_MODE_LABEL[mode]
    want_trigger = soundpad._MIXER_DEFAULTS[mode]["trigger_mode"]
    print("CREATE %s on pad %d" % (label, grid))
    res = soundpad.create_mixer(grid, mode)
    print("  " + res.get("message", ""))
    pad = read_pad(grid)
    created = bool(pad and pad["present"] and pad["type"] == soundpad.PAD_TYPE_MIXER
                   and pad["mixer_mode"] == mode and pad["mixer_trigger"] == want_trigger)
    print("  read back: type=%s mode=%s trigger=%s name=%r  -> %s"
          % (pad and pad["type_label"], pad and pad["mixer_mode"], pad and pad["mixer_trigger"],
             pad and pad["name"], "OK" if created else "WRONG"))

    soundpad.clear_grid_pad(grid)
    pad = read_pad(grid)
    pool = soundpad.pool_health()
    cleared = bool(pad and not pad["present"])
    pool_same = pool == pool_before
    print("  cleared: %s  pool unchanged: %s" % (cleared, pool_same))
    if not pool_same:
        print("  pool before: %r\n  pool after:  %r" % (pool_before, pool))
    return created and cleared and pool_same


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pad", type=int, default=17, help="grid pad to test on (MUST be empty)")
    ap.add_argument("--mode", default=None, help="one mode only, 0-4 or its name")
    ap.add_argument("--clear", action="store_true", help="only clear the pad and report")
    args = ap.parse_args()
    grid = args.pad

    if args.clear:
        print(soundpad.clear_grid_pad(grid).get("message", ""))
        print("  pool clean:", soundpad.pool_health()["clean"])
        return 0

    pad = read_pad(grid)
    if pad is None or pad["present"]:
        print("pad %d is not empty (%s). Pick an empty pad." % (grid, pad and pad["type_label"]))
        return 1
    pool_before = soundpad.pool_health()
    if not pool_before["clean"]:
        print("the effect pool is not clean before the test; stopping. %r" % pool_before)
        return 1

    modes = ([soundpad._resolve_mixer_mode(args.mode)] if args.mode is not None
             else sorted(soundpad.MIXER_MODE_LABEL))
    results = {soundpad.MIXER_MODE_LABEL[m]: run_mode(grid, m, pool_before) for m in modes}
    print("RESULT")
    for label, ok in results.items():
        print("  %-14s %s" % (label, "PASS" if ok else "FAIL"))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
