"""LIVE TEST: a MIDI trigger pad's mapping, on real hardware.

Proves the padTrigger* properties end to end on one EMPTY pad, then puts the desk back:
  1. create a MIDI trigger pad and READ its mapping (the firmware default);
  2. write a CUSTOM mapping field by field and read every one back;
  3. optionally flip padTriggerMode (--mode), which is LATCHING versus MOMENTARY: proven by
     listening to the desk's MIDI output under a press. Mode 1 sends the on value on one press and
     the off value on the next; mode 0 sends on at press and off at release. Use --mode to hear it
     for yourself with DevTools/midi_monitor.py;
  4. turn the custom flag off and assert what the DESK does then, which is discard the numbers;
  5. clear the pad and confirm the grid and the effect pool are back to baseline.

EXCLUSIVE DEVICE ACCESS: stop the running service first (the app owns the HID handle), then restart
it with `python DevTools/run_dev_server.py`. Everything here is reversible and read-back verified; a
trigger pad writes NO region-B object, so it cannot leave a pool orphan (SOUNDPADS.md).

Usage:
    python DevTools/test_pad_trigger_live.py --pad 17            # full sequence, then clear
    python DevTools/test_pad_trigger_live.py --pad 17 --mode 0   # leave it mapped, mode 0, NO clear
    python DevTools/test_pad_trigger_live.py --pad 17 --clear    # just clear that pad again
"""
import argparse
import json

import _bootstrap  # noqa: F401  - puts Project/ on sys.path

import soundpad

CUSTOM = {"type": 1, "control": 60, "channel": 2, "on": 100, "off": 0}


def mapping(grid):
    pads = soundpad.read_pads_live()["pads"]
    pad = next((p for p in pads if p["grid"] == grid), None)
    return pad


def show(label, pad):
    print("  %-22s %s" % (label, json.dumps(pad and pad["trigger"])))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pad", type=int, default=17, help="grid pad to test on (MUST be empty)")
    ap.add_argument("--mode", type=int, default=None,
                    help="also write padTriggerMode (the unknown field) and stop, leaving the pad")
    ap.add_argument("--clear", action="store_true", help="only clear the pad and report")
    args = ap.parse_args()
    grid = args.pad

    if args.clear:
        print(json.dumps(soundpad.clear_grid_pad(grid), indent=1)[:400])
        print("  pool clean:", soundpad.pool_health()["clean"])
        return 0

    pad = mapping(grid)
    if args.mode is not None:
        if not pad or pad["type"] != 4:
            print("pad %d is not a MIDI trigger; run the full sequence first." % grid)
            return 1
        res = soundpad.set_pad_property(grid, "padTriggerMode", args.mode, kind="int")
        print(res["message"] if res.get("ok") else "FAILED: " + res.get("message", ""))
        show("mode now", mapping(grid))
        return 0 if res.get("ok") else 1

    if pad and pad["present"]:
        print("pad %d is not empty (%s). Pick an empty pad." % (grid, pad["type_label"]))
        return 1

    print("CREATE a MIDI trigger on pad %d" % grid)
    res = soundpad.create_trigger(grid)
    print("  " + res.get("message", ""))
    pad = mapping(grid)
    show("default mapping", pad)
    padidx = grid - 1
    ok = (pad and pad["trigger"] and pad["trigger"]["custom"] is False
          and pad["trigger"]["type"] == 0 and pad["trigger"]["control"] == padidx
          and pad["trigger"]["channel"] == 1 and pad["trigger"]["on"] == 127
          and pad["trigger"]["off"] == 0)
    print("  DEFAULT IS CC ON THE PAD'S OWN NUMBER, channel 1, 127/0:", ok)

    print("WRITE a custom mapping, one field per call")
    for key, value in CUSTOM.items():
        r = soundpad.set_trigger_mapping(grid, **{key: value})
        print("  %-8s -> %-4s %s" % (key, value, "ok" if r.get("ok") else "FAILED " + str(r)))
    pad = mapping(grid)
    show("custom mapping", pad)
    got = pad["trigger"]
    print("  EVERY FIELD READ BACK AS WRITTEN:",
          all(got[k] == v for k, v in CUSTOM.items()) and got["custom"] is True)

    # MEASURED: the desk resets control/channel/on/off to its own defaults when the flag goes off,
    # and switching it back on does NOT return them. So this asserts the DESTRUCTIVE behaviour on
    # purpose - if a firmware update ever makes "off" park the numbers instead, this check notices.
    print("TURN THE CUSTOM FLAG OFF (the desk discards the numbers)")
    soundpad.set_trigger_mapping(grid, custom=False)
    pad = mapping(grid)
    show("custom off", pad)
    got = pad["trigger"]
    print("  FLAG OFF, AND THE DESK RESET THE NUMBERS TO DEFAULT:",
          got["custom"] is False and got["control"] == padidx and got["channel"] == 1
          and got["on"] == 127 and got["off"] == 0)
    print("  (padTriggerType is NOT reset by the desk; it reads %r, which is why the page describes "
          "a default pad from its position, not from this field)" % got["type"])

    print("CLEAR the pad and check the baseline")
    soundpad.clear_grid_pad(grid)
    pad = mapping(grid)
    health = soundpad.pool_health()
    print("  pad %d empty again: %s" % (grid, not pad["present"]))
    print("  pool clean: %s  orphans: %s  duplicates: %s"
          % (health["clean"], health["orphans"], health["duplicates"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
