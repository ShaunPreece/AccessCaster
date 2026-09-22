"""Try a WRITE on a selector DERIVED from the block rule, prove it, and put it straight back.

THE POINT. `DevTools/selector_map.py` says which block a property belongs to, and therefore which
selector a write to it would take. What it CANNOT say is whether the field accepts a write at all.
This file closes that gap the only honest way: write it, read it back off the desk, restore it, and
check the model came back the same shape it started (a write that lands nowhere can append a stray
property, and a stray is factory-reset-only - see the STRAYS incident).

It exists because the alternative had a false floor. For a year, properties with no control in the
RODE app were filed BLOCKED, on the reasoning that there was no capture to copy. That reasoning has
now been wrong five times running (`outputMonLevel`, `activeButtonsBrightness`, `channelOutputMute`,
`channelCueEnable`, `eq*Q`). No app control means no CAPTURE ROUTE. It never meant unwritable.

SAFETY, and it is not decoration:
  * Only properties in a block with a CONFIRMED anchor are allowed. An unknown block is refused,
    not guessed, because a wrong selector is how strays happen.
  * The value written is deliberately near the current one where the type allows, so a moment spent
    in the test state is a small change rather than an extreme one.
  * The original is restored in a `finally`, and the restore is READ BACK too. A test that leaves the
    desk changed has failed even if the write succeeded.
  * Anything that cannot be undone from here is on DENY below and is refused by name, with the
    reason. `powerOffRequest` is the clear case: this code can turn the desk off and cannot turn it
    back on, so it is the owner's call to make at the unit.

Usage:
    python DevTools/probe_derived_write.py --list
    python DevTools/probe_derived_write.py outputBTMute
    python DevTools/probe_derived_write.py outputBTLevel --value 0.4
    python DevTools/probe_derived_write.py --group output     # a whole vetted batch
"""
from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import rcp_read
import rcp_transfer as _t
import selector_map

SETTLE = 0.9

# Properties we refuse to write from here, and why. Each of these could be perfectly writable; the
# point is that we could not UNDO it from this side, which is the line the project's own rules draw.
DENY = {
    "powerOffRequest": "would power the desk off, and nothing on this side can power it back on. "
                       "Shaun has to be at the desk for that one.",
    "updateRebootRequested": "reboots the unit mid-session.",
    "updateInitiateRequested": "starts a firmware update.",
    "updateResetDeviceRequested": "a device reset; the post-reset flow is touchscreen-only.",
    "updateResetAfterFWURequested": "as above.",
    "updateResetAppRequested": "as above.",
    "updateViaUSB": "part of the firmware update path.",
    "downloadInitiateRequested": "starts a firmware download.",
    "systemBetaMode": "changes which firmware track the unit is on.",
    "remountPadStorage": "remounts the pad volume; the STRAYS incident lives in that area.",
    "rcSyncModeRequest": "puts the unit into a sync mode we have not decoded.",
    "systemSerialNumber": "identity, not a setting.",
    "systemFirmwareVersion": "reported by the unit, not ours to set.",
}

# Vetted batches. Each is a list of (property, test value or None for 'flip the bool').
GROUPS = {
    # The output block. All audio-only and instantly reversible: the worst a failed restore does is
    # leave a level or a mute somewhere obvious, which the read-back would report.
    "output": ["outputBTMute", "outputMonMute", "outputBTLevel", "outputMonFixed",
               "outputPrefader", "outputMultiMode", "outputMultiBypass",
               "recordingProcessingBypass", "recordingCompressionQuality"],
    # The clock block. Nothing here makes a sound; the reason it matters is that recordings are
    # timestamped, so a desk on the wrong timezone mislabels every file it makes.
    "clock": ["systemDateTime24h", "systemDateTimeDaylightSavings", "systemDateTimeOnHome",
              "systemDateTimezone"],
    # The lockouts. Reversible by the same write, but they are the ones that could leave the desk
    # feeling broken, so they are their own group and are meant to be run deliberately.
    "lockouts": ["disableAllHeadphoneOutputs", "disableAllLineoutOutputs",
                 "disableAllPhysicalButtons"],
}


def read_one(prop: str):
    blob = rcp_read.read_model_stable(fresh=True)
    hits = rcp_read.find_all(blob, prop)
    return (hits[0] if hits else (None, None)), blob


def derive(prop: str, blob: bytes):
    """(selector, header) for a property, or (None, header) when the block is not anchored."""
    where, count = selector_map.sections(blob)
    header = where.get(prop)
    if count.get(prop, 0) != 1:
        return None, header, f"{prop} occurs {count.get(prop, 0)} times; this tool only handles " \
                             f"single-instance properties (an indexed block uses base + index)"
    sel = selector_map.HEADER_SELECTOR.get(header)
    if sel is None:
        return None, header, f"{prop} is under header {header!r}, which has no confirmed selector. " \
                             f"Guessing it is how a stray gets appended - do not."
    return sel, header, None


def test_value(kind, current):
    """A test value that is clearly different from the current one but not extreme."""
    if kind == "bool":
        return not bool(current)
    if kind == "float":
        cur = float(current or 0.0)
        return round(0.30 if cur > 0.5 else 0.70, 4)
    if kind == "int":
        cur = int(current or 0)
        return cur + 1 if cur < 1 else cur - 1
    return None


def write_one(prop: str, sel: int, kind: str, value):
    if kind == "bool":
        return _t.send_record(_t.build_global_prop_bool(prop, bool(value), sel))
    if kind == "float":
        return _t.send_record(_t.build_global_prop_float(prop, sel, float(value)))
    if kind == "int":
        return _t.send_record(_t.build_global_prop_int(prop, sel, int(value)))
    raise ValueError(f"no writer for a {kind!r} value")


def probe(prop: str, forced=None) -> dict:
    """Write, read back, restore, verify. Returns a result dict; never leaves the desk changed if it
    can help it (the restore is in a finally and is itself read back)."""
    res = {"prop": prop, "ok": False, "note": ""}
    if prop in DENY:
        res["note"] = "REFUSED: " + DENY[prop]
        return res

    (current, kind), blob0 = read_one(prop)
    if kind in (None, "?"):
        res["note"] = f"could not read {prop} off the desk (kind={kind!r}); nothing to test against"
        return res
    sel, header, why = derive(prop, blob0)
    res.update(header=header, selector=sel, kind=kind, before=current)
    if sel is None:
        res["note"] = "REFUSED: " + why
        return res

    want = forced if forced is not None else test_value(kind, current)
    if want is None:
        res["note"] = f"no test value for a {kind} property"
        return res
    res["wrote"] = want

    try:
        write_one(prop, sel, kind, want)
        time.sleep(SETTLE)
        (after, _k), _ = read_one(prop)
        res["read_back"] = after
        if kind == "float":
            res["took"] = after is not None and abs(float(after) - float(want)) < 0.01
        else:
            res["took"] = after == want
    except Exception as exc:
        res["note"] = f"the write itself raised: {exc}"
        return res
    finally:
        try:
            write_one(prop, sel, kind, current)
            time.sleep(SETTLE)
            (back, _k), blob2 = read_one(prop)
            res["restored"] = back == current
            res["after"] = back
            res["clean"] = (len(blob0) == len(blob2)
                            and len(rcp_read.find_all(blob0, prop))
                            == len(rcp_read.find_all(blob2, prop)))
        except Exception as exc:
            res["note"] = f"RESTORE FAILED ({exc}) - check {prop} on the desk by hand"
            return res

    res["ok"] = bool(res.get("took")) and res.get("restored") and res.get("clean")
    if not res["ok"] and not res["note"]:
        res["note"] = ("wrote cleanly but the value did not change: the block is right and this "
                       "particular field is read-only or ignores the write"
                       if not res.get("took") else "restore or cleanliness check failed")
    return res


def show(r: dict) -> None:
    head = f"{r['prop']:<30} {str(r.get('header') or '?'):<14}"
    sel = f"0x{r['selector']:02X}" if r.get("selector") is not None else "  - "
    if r.get("note") and "REFUSED" in r["note"]:
        print(f"{head} {sel}  {r['note']}")
        return
    print(f"{head} {sel}  {r.get('kind','?'):<5} {r.get('before')!r} -> wrote {r.get('wrote')!r} "
          f"-> read {r.get('read_back')!r} | took={r.get('took')} restored={r.get('restored')} "
          f"clean={r.get('clean')}"
          + (f"\n{'':46}{r['note']}" if r.get("note") else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("props", nargs="*", help="property names to probe")
    ap.add_argument("--group", choices=sorted(GROUPS), help="a vetted batch")
    ap.add_argument("--value", help="force the test value (for a single property)")
    ap.add_argument("--list", action="store_true", help="list the groups and the deny list")
    args = ap.parse_args()

    if args.list:
        for g, props in sorted(GROUPS.items()):
            print(f"{g}: {', '.join(props)}")
        print("\nrefused by name:")
        for p, why in sorted(DENY.items()):
            print(f"  {p:<32} {why}")
        return 0

    props = list(args.props) + (GROUPS[args.group] if args.group else [])
    if not props:
        print("nothing to probe; pass property names, --group, or --list")
        return 2

    forced = None
    if args.value is not None:
        v = args.value
        forced = (True if v.lower() == "true" else False if v.lower() == "false"
                  else float(v) if "." in v else int(v))

    results = [probe(p, forced if len(props) == 1 else None) for p in props]
    print()
    for r in results:
        show(r)
    took = [r["prop"] for r in results if r.get("took")]
    inert = [r["prop"] for r in results if r.get("read_back") is not None and not r.get("took")]
    dirty = [r["prop"] for r in results if r.get("clean") is False or r.get("restored") is False]
    print(f"\nWRITABLE: {', '.join(took) or 'none'}")
    print(f"INERT (write accepted, value unchanged): {', '.join(inert) or 'none'}")
    print(f"NEEDS A LOOK (restore or cleanliness): {', '.join(dirty) or 'none'}")
    return 1 if dirty else 0


if __name__ == "__main__":
    raise SystemExit(main())
