"""LIVE TEST: a restore's skip check on voice-FX pads, on real hardware.

  1. Every voice-FX pad on the desk is entered as a backup would record it, and every one must
     match: a factory pad that does not match its own recipe means the check cannot be trusted.
  2. One param on one pad is changed in place (the live-preview edit) and the pad disengaged again:
     that pad must now differ and every other pad must still match.
  3. The param is written back: the object must read back identical to how it started, the pool
     clean, and every pad match again.

The only writes are the one param and its restore on one pad, plus the disengage after each; the
pad is engaged for under a second each time, so its effect is briefly live on its input.

EXCLUSIVE DEVICE ACCESS: stop the running service first (the app owns the HID handle), then restart
it with `python DevTools/run_dev_server.py`.

Usage:
    python DevTools/test_fx_skip_live.py                  # Megaphone (grid 10), echoMix
    python DevTools/test_fx_skip_live.py --pad 14 --param pitchShiftSemitones --value -4
"""
import argparse

import _bootstrap  # noqa: F401  - puts Project/ on sys.path

import rcp_read as rr
import soundpad


def snapshot():
    """(saved entries for every FX pad, verdict per grid, pad props, region-B objects, clean)."""
    live = soundpad.read_pads_live()["pads"]
    blob = rr.read_model_stable()
    clean = soundpad.pool_health(blob)["clean"]
    props = {p.idx: p.props for p in soundpad.pad_nodes(blob)}
    node = soundpad._top_node(blob, "PADEFFECTS")
    objects = [{k: v for k, v, _ in n.props} for n in (node.kids if node else ())]
    saved = []
    for p in live:
        if not p["present"] or p["type"] != soundpad.PAD_TYPE_VOICE_FX:
            continue
        key = soundpad._safe_match_recipe(p["name"])
        stack = (soundpad.all_recipes().get(key) or {}).get("stack") if key else None
        saved.append({"grid": p["grid"], "name": p["name"], "fx_recipe": key, "fx_stack": stack})
    verdict = {e["grid"]: soundpad._fx_effect_matches(e, props.get(e["grid"] - 1), objects, clean)
               for e in saved}
    return saved, verdict, props, objects, clean


def object_of(objects, grid):
    return next((o for o in objects if o.get("effectsIdx") == grid - 1), None)


def edit(grid, param, value):
    soundpad.preview_update(grid, param, value)
    pos = next(p for p, pi, _t, _n in soundpad.pad_positions() if pi == grid - 1)
    soundpad.unlatch_pad(pos)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pad", type=int, default=10, help="grid of an FX pad to edit")
    ap.add_argument("--param", default="echoMix")
    ap.add_argument("--value", type=float, default=0.25)
    args = ap.parse_args()
    grid = args.pad
    results = []

    def check(label, ok):
        results.append(ok)
        print(("  ok    " if ok else "  FAIL  ") + label)

    print("1. EVERY FX PAD AS FOUND")
    saved, verdict, props, objects, clean = snapshot()
    print("  pool clean: %s" % clean)
    for e in saved:
        check("pad %d %s (recipe %s) matches" % (e["grid"], e["name"], e["fx_recipe"]),
              verdict[e["grid"]])
    before = object_of(objects, grid)
    if before is None or grid not in verdict:
        print("pad %d is not a voice-FX pad; stopping." % grid)
        return 1
    if not all(results) or not clean:
        print("not everything matched as found; stopping before any write.")
        return 1
    original = before[args.param]

    print("\n2. %s ON PAD %d CHANGED %r -> %r" % (args.param, grid, original, args.value))
    try:
        edit(grid, args.param, args.value)
        _s, verdict, props, objects, clean = snapshot()
        print("  read back: %r, padActive %r" % (object_of(objects, grid)[args.param],
                                                 props[grid - 1].get("padActive")))
        check("the edited pad no longer matches", not verdict[grid])
        check("every other FX pad still matches",
              all(v for g, v in verdict.items() if g != grid))
    finally:
        print("\n3. PUT BACK")
        edit(grid, args.param, original)
    _s, verdict, props, objects, clean = snapshot()
    check("the object reads back exactly as it started", object_of(objects, grid) == before)
    check("the pad is disengaged", props[grid - 1].get("padActive") is False)
    check("the pool is clean", clean)
    check("every FX pad matches again", all(verdict.values()))

    print("\nRESULT: %s" % ("PASS" if all(results) else "FAIL"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
