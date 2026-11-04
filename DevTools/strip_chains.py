"""Save every fader strip's processing chain, and put it back, read-back verified.

For a test that moves faders by some route other than ours (the touchscreen, RODE's app): their
move regenerates the derived chain values (channels.DERIVED_FLOAT_PROPS) instead of carrying them,
so moving a channel there and back does not restore its voicing. This puts it back exactly.

  save    <name>             READ ONLY, safe with the app up. Every strip's chain and the fader
                             layout, keyed by strip, to DevTools/captures/strip_chains/<name>.json.
  restore <name> [--write]   What differs, strip by strip; --write sends it and reads it back.
                             Refuses while the app runs, and refuses unless the fader layout is
                             the one saved, because a chain belongs to a strip, not a channel.

Writes only chain properties the strip already carries (channels.fit_chain), so it can append no
stray.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401
import channels
import rcp_read

OUT_DIR = Path(__file__).resolve().parent / "captures" / "strip_chains"


def _live() -> tuple[list, list]:
    """(fader layout, every strip's chain) from one fresh dump."""
    blob = rcp_read.read_model_stable(fresh=True)
    return rcp_read.values(blob, "channelInputSource"), channels.read_chains(blob)


def _pending(saved: list, live: list) -> list:
    """[(strip, ops)] for every strip whose live chain differs from the saved one."""
    out = []
    for strip, (want, cur) in enumerate(zip(saved, live)):
        ops = channels.chain_diff(channels.fit_chain(want, strip), cur)
        if ops:
            out.append((strip, ops))
    return out


def save(name: str) -> None:
    layout, chains = _live()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{name}.json"
    path.write_text(json.dumps({"layout": layout, "chains": chains}, indent=1), encoding="utf-8")
    print(f"saved {len(chains)} strips, layout {layout} -> {path}")


def restore(name: str, write: bool) -> int:
    path = OUT_DIR / f"{name}.json"
    if not path.exists():
        raise SystemExit(f"no saved chains called {name!r} (looked in {OUT_DIR})")
    saved = json.loads(path.read_text(encoding="utf-8"))
    if write:
        from _desk import service
        server = service()
    layout, live = _live()
    if layout != saved["layout"]:
        raise SystemExit(f"the fader layout is {layout}, saved {saved['layout']}: put the faders "
                         f"back first, or every chain lands on the wrong channel")
    pending = _pending(saved["chains"], live)
    for strip, ops in pending:
        print(f"strip {strip}: {len(ops)} to write")
        for _kind, prop, value in ops:
            print(f"    {prop} -> {value!r}")
    if not pending:
        print("every strip matches the saved chains")
        return 0
    if not write:
        print("dry run: add --write to send it")
        return 0
    with server._hid_lock:
        for strip, _ops in pending:
            channels.write_chain(strip, saved["chains"][strip], live[strip])
    left = _pending(saved["chains"], _live()[1])
    if left:
        print(f"STILL DIFFERENT after the write: strips {[s for s, _ in left]}")
        return 1
    print("written and read back: every strip matches the saved chains")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Save and restore every strip's processing chain")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("save")
    p.add_argument("name")
    p = sub.add_parser("restore")
    p.add_argument("name")
    p.add_argument("--write", action="store_true")
    args = ap.parse_args()
    if args.cmd == "save":
        save(args.name)
        return 0
    return restore(args.name, args.write)


if __name__ == "__main__":
    sys.exit(main())
