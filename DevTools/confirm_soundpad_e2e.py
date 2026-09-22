"""GAP-3 confirmation: soundpad create/edit/clear END-TO-END through the browser/HTTP surface.

Drives the SAME HTTP endpoints the web UI uses (server must be running headless on :8770),
on EMPTY user pads only, reading the pool health back via HTTP after every step, then clearing
everything and confirming the pad table + region-B pool return byte-clean to the baseline
(7 objects). Fully reversible. See ProjectMemory/Completed.md, Duo support.
"""
import json
import struct
import urllib.request

import _bootstrap

BASE = "http://localhost:8770"
# GENERATED, never a path into the repo or a user account: the app ships no audio, and a test that
# needs a file makes its own (see _bootstrap.test_wav).
WAV = _bootstrap.test_wav()

# Empty grids to use (confirmed empty at baseline).
G_WAV, G_MP3, G_MIX, G_FX = 17, 18, 19, 20


def _req(path, data=None, headers=None, method=None):
    url = BASE + path
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method=method or ("POST" if data is not None else "GET"))
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def get(path):
    return _req(path, method="GET")


def post_json(path, obj):
    body = json.dumps(obj).encode("utf-8")
    return _req(path, data=body, headers={"Content-Type": "application/json"})


def post_raw(path, filename, raw):
    return _req(path, data=raw, headers={"X-Filename": filename,
                                         "Content-Type": "application/octet-stream"})


def make_silent_mp3(frames=30):
    """A structurally valid MPEG-1 Layer III mono 44.1kHz 128kbps SILENT stream.
    Header FF FB 90 C0 (MPEG1/LayerIII/no-CRC, 128k, 44100, mono) + zeroed side-info/main-data
    (big_values=0 => silence). Frame length = 417 bytes. Enough to exercise the .mp3 code path."""
    header = bytes([0xFF, 0xFB, 0x90, 0xC0])
    frame = header + bytes(417 - len(header))
    return frame * frames


def health(tag):
    d = get("/api/soundpads")
    h = d.get("health") or {}
    print(f"  [{tag}] health.clean={h.get('clean')} region_b_count={h.get('region_b_count')} "
          f"dups={h.get('duplicates')} orphans={h.get('orphans')} missing={h.get('missing')}")
    return d, h


def occupied(d):
    return sorted(p.get("grid") for p in (d.get("pads") or [])
                  if (p.get("type") not in (None, 0)) or p.get("name"))


def main():
    print("=== BASELINE ===")
    base, hb = health("baseline")
    base_occ = occupied(base)
    base_rb = hb.get("region_b_count")
    print("  read_only:", base.get("read_only"), "| occupied grids:", base_occ)
    assert base.get("read_only") is False, "Duo should be un-gated (read_only False)"

    with open(WAV, "rb") as f:
        wav_bytes = f.read()
    mp3_bytes = make_silent_mp3()

    print(f"=== A: WAV upload -> grid {G_WAV} ({len(wav_bytes)} bytes) ===")
    print("  resp:", post_raw(f"/api/pad/replace?pad={G_WAV}", "AC_TestScratch.wav", wav_bytes).get("message"))
    health("after WAV")

    print(f"=== B: MP3 upload -> grid {G_MP3} ({len(mp3_bytes)} bytes) ===")
    print("  resp:", post_raw(f"/api/pad/replace?pad={G_MP3}", "AC_TestSilent.mp3", mp3_bytes).get("message"))
    health("after MP3")

    print(f"=== C: mixer create (Fade In/Out) -> grid {G_MIX} ===")
    print("  resp:", post_json("/api/pad/create-special", {"pad": G_MIX, "type": "mixer:2"}).get("message"))
    health("after mixer")

    print(f"=== D: voice-FX assign (Reverb) -> grid {G_FX}  (region-B pool ADD, expect +1) ===")
    print("  resp:", post_json("/api/pad/assign-fx", {"pad": G_FX, "effect": "Reverb"}).get("message"))
    _, hfx = health("after FX")
    print(f"  region_b_count {base_rb} -> {hfx.get('region_b_count')} (expect +1)")

    print(f"=== E: in-place edits on grid {G_WAV} ===")
    print("  name :", post_json("/api/pad/name", {"pad": G_WAV, "name": "AC Renamed"}).get("message"))
    print("  gain :", post_json("/api/pad/gain", {"pad": G_WAV, "db": -6.0}).get("message"))
    print("  loop :", post_json("/api/pad/loop", {"pad": G_WAV, "on": True}).get("message"))
    de, _ = health("after edits")
    padw = next((p for p in de["pads"] if p.get("grid") == G_WAV), {})
    print("  grid17 now:", {k: padw.get(k) for k in ("name", "gain", "loop", "type")})

    print("=== CLEANUP: clear test pads ===")
    for g in (G_WAV, G_MP3, G_MIX, G_FX):
        print(f"  clear {g}:", post_json("/api/pad/clear", {"pad": g}).get("message"))

    print("=== FINAL ===")
    fin, hf = health("final")
    fin_occ = occupied(fin)
    print("  occupied grids:", fin_occ)
    print("  region_b_count:", hf.get("region_b_count"), "(baseline", base_rb, ")")
    ok = (hf.get("clean") and hf.get("region_b_count") == base_rb and fin_occ == base_occ)
    print("RESULT byte-clean restore:", ok)


if __name__ == "__main__":
    main()
