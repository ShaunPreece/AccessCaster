"""Build the read-only UI fixture: the REAL payloads, with every write cap turned off.

WHY IT IS GENERATED RATHER THAN HAND-WRITTEN. The controls that a read-only desk cannot drive are
built from the device payload - the faders, the headphone outputs, the compellor params, the monitor
mix sources - so a hand-written fixture would be a second, decaying copy of the server's shape, and
a check run against it would pass while the real page had moved on. This takes what the attached
desk really answers and flips only the gates.

WHY A FIXTURE AT ALL. Every one of those gates (`unit_settings`, `pad_write`, `fader_write`,
`output_write`, `level_write`, `matrix_write`) is now ON for BOTH desks, so no desk in existence
reaches the read-only branches any more. Driving the app cannot exercise them; feeding the UI the
payload a capless desk would send is the only way left.

    python DevTools/make_readonly_stub.py            # writes DevTools/_probe_stub_readonly.json
    python DevTools/ui_probe.py --checks DevTools/ui_checks_readonly.js \
        --stub DevTools/_probe_stub_readonly.json --route routing
"""
from __future__ import annotations
import json
import urllib.request

import _bootstrap  # noqa: F401

BASE = "http://127.0.0.1:8770"
# `_probe_stub_*` is the repo's name for a ui_probe fixture taken off the connected desk, and
# it is gitignored: the repo carries the CHECKS, not a snapshot of one machine's desk.
OUT = _bootstrap.devtools_path("_probe_stub_readonly.json")


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=20) as fh:
        return json.load(fh)


def main() -> None:
    routing = get("/api/routing")
    # The four gates the Routing page reads, each the flag the server sets from a missing cap.
    routing["output_write_read_only"] = True
    for key in ("fader_model", "matrix", "monitor_mix"):
        if isinstance(routing.get(key), dict):
            routing[key]["read_only"] = True

    settings = get("/api/device/settings")
    settings["writable"] = False

    pads = get("/api/soundpads")
    pads["read_only"] = True

    levels = get("/api/faders/levels")
    levels["read_only"] = True          # the LEVEL gate; the mute/solo gate is its own flag
    levels["toggles_read_only"] = True
    levels["toggles_message"] = "Muting is not enabled on this device yet."

    stub = {
        "/api/routing": routing,
        "/api/device/settings": settings,
        "/api/soundpads": pads,
        "/api/faders/levels": levels,
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(stub, fh, indent=1)
    print(f"wrote {OUT}")
    print(f"  routing: {len(routing.get('fader_model', {}).get('faders', []))} faders, "
          f"{len(routing.get('headphones', {}).get('outputs', []))} headphone outputs")
    print(f"  settings: {len(settings.get('settings', []))} unit settings")
    print(f"  levels: {len(levels.get('faders', []))} fader levels")


if __name__ == "__main__":
    main()
