"""Build the CallMe fixture where nothing is ready for calls: the REAL /api/callme payload, with the
unit offline, CallMe 1 on no fader and hosting off.

Generated rather than hand-written for the same reason as make_readonly_stub.py: the payload is the
server's shape, and a hand-written copy would decay. The readiness list is re-worded by callme.py
itself, so the page is checked against the server's own sentences.

    python DevTools/make_callme_unready_stub.py      # writes DevTools/_probe_stub_callme_unready.json
    python DevTools/ui_probe.py --checks DevTools/ui_checks_callme_ready.js --route callme \
        --stub DevTools/_probe_stub_callme_unready.json --timeout 240
"""
from __future__ import annotations
import json
import urllib.request

import _bootstrap  # noqa: F401

import callme

BASE = "http://127.0.0.1:8770"
OUT = _bootstrap.devtools_path("_probe_stub_callme_unready.json")


def main() -> int:
    with urllib.request.urlopen(BASE + "/api/callme", timeout=30) as r:
        d = json.load(r)
    if not d.get("ok"):
        print("The server could not read CallMe:", d.get("message"))
        return 1
    # One fixed version for both reads, so the page's live poll sees nothing new and keeps the stub.
    d.update(online=False, on_fader=False, hosting=False, code="", link="", version=999999)
    d["ready"] = callme.readiness(d)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"/api/callme": d, "/api/callme/live": d}, f, indent=1)
    print("Wrote", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
