"""Non-destructive dump of every voice-FX effect-object on the device.

Reads the model, splits the EFFECTS_PARAMETERS markers into region A (channel-strip
effects, one per input channel) and region B (the special + per-pad effect-objects),
and prints each region-B object's full recipe: every on-flag + param value + effectsIdx.
Also lists the pad table so each FX pad can be mapped to its effect-object(s).

This is the source-of-truth extractor for factory voice-FX recipes (SOUNDPADS "match
factory exactly" task). It writes NOTHING to the device.

Usage:  python decode_effects.py
"""
import json
import pickle
import sys

import _bootstrap  # noqa: F401  - puts Project/ on sys.path
import rcp_read as rr


def _restrip(raw: bytes) -> bytes:
    """Some snapshots hold the raw frame stream WITH the 0x04 report-id at every 256-byte
    boundary. Drop byte 0 of each 256-byte frame to get the clean contiguous stream that
    find_all/_val_after expect."""
    if len(raw) % 256 == 0 and raw[:1] == b"\x04":
        return b"".join(raw[i + 1:i + 256] for i in range(0, len(raw), 256))
    return raw


def load_stream(path: str) -> bytes:
    """Load a model stream from a live read, a .bin dump, or a snapshot .pkl dict."""
    if path == "live":
        return rr.read_model_stable()
    if path.endswith(".pkl"):
        d = pickle.load(open(path, "rb"))
        raw = d["raw"] if isinstance(d, dict) else d
        return _restrip(bytes(raw))
    return _restrip(open(path, "rb").read())

# The 20 fields that make up an effect-object (order as written by the app/builder).
EFFECT_FIELDS = [
    ("reverbOn", "bool"), ("reverbMix", "float"), ("reverbLowCut", "float"),
    ("reverbHighCut", "float"), ("reverbModel", "float"),
    ("echoOn", "bool"), ("echoLowCut", "float"), ("echoHighCut", "float"),
    ("echoDelay", "float"), ("echoMix", "float"), ("echoDecay", "float"),
    ("pitchShiftOn", "bool"), ("pitchShiftSemitones", "float"),
    ("distortionOn", "bool"), ("distortionIntensity", "float"),
    ("robotOn", "bool"), ("robotMix", "float"), ("robotLevel", "float"),
    ("voiceDisguiseOn", "bool"), ("effectsIdx", "int"),
]
ON_FLAGS = [n for n, k in EFFECT_FIELDS if n.endswith("On")]


def _marker_offsets(m: bytes):
    offs, s = [], 0
    while True:
        i = m.find(b"EFFECTS_PARAMETERS", s)
        if i < 0:
            break
        offs.append(i)
        s = i + 1
    return offs


def decode_objects(m: bytes):
    offs = _marker_offsets(m)
    if len(offs) < 2:
        region_a = len(offs)
    else:
        gaps = [(offs[k + 1] - offs[k], k) for k in range(len(offs) - 1)]
        region_a = max(gaps)[1] + 1
    objs = []
    for j, o in enumerate(offs):
        nxt = offs[j + 1] if j + 1 < len(offs) else len(m)
        rec = {"abs_index": j, "region": "A" if j < region_a else "B",
               "regionB_index": (j - region_a) if j >= region_a else None}
        for name, kind in EFFECT_FIELDS:
            p = m.find(name.encode(), o, nxt)
            if p < 0:
                rec[name] = None
            else:
                val, _ = rr._val_after(m, p + len(name))
                rec[name] = val
        objs.append(rec)
    return region_a, objs


def pad_table(m: bytes):
    names = [v for v, k in rr.find_all(m, "padName")]
    types = [v for v, k in rr.find_all(m, "padType")]
    idxs = [v for v, k in rr.find_all(m, "padIdx")]
    pads = []
    for i, nm in enumerate(names):
        pads.append({
            "pos": i,
            "padName": nm,
            "padType": types[i] if i < len(types) else None,
            "padIdx": idxs[i] if i < len(idxs) else None,
        })
    return pads


def main():
    src = next((a for a in sys.argv[1:] if not a.startswith("--")), "live")
    m = load_stream(src)
    print(f"source: {src}")
    print(f"model bytes: {len(m)}")
    region_a, objs = decode_objects(m)
    print(f"effect-objects: {len(objs)} total, region-A (channel strips) = {region_a}, "
          f"region-B (pads/special) = {len(objs) - region_a}\n")

    print("=== PAD TABLE (FX pads = padType 2) ===")
    for p in pad_table(m):
        flag = "  <-- FX" if p["padType"] == 2 else ""
        print(f"  pos {p['pos']:2}  padIdx {str(p['padIdx']):>4}  type {p['padType']}  "
              f"{p['padName']!r}{flag}")

    print("\n=== REGION-B EFFECT-OBJECTS (the per-pad recipes) ===")
    for r in objs:
        if r["region"] != "B":
            continue
        on = [n for n in ON_FLAGS if r.get(n) is True]
        on_str = "+".join(n[:-2] for n in on) if on else "(none on)"
        print(f"\n  regionB#{r['regionB_index']:2}  effectsIdx={r['effectsIdx']}  ACTIVE: {on_str}")
        for name, kind in EFFECT_FIELDS:
            if name == "effectsIdx":
                continue
            v = r.get(name)
            if v is None:
                continue
            mark = "  *" if (name in ON_FLAGS and v is True) else ""
            print(f"      {name:24} = {v}{mark}")

    if "--json" in sys.argv:
        out = {"region_a": region_a, "objects": objs, "pads": pad_table(m)}
        with open("effects_dump.json", "w") as f:
            json.dump(out, f, indent=2)
        print("\nwrote effects_dump.json")


if __name__ == "__main__":
    main()
