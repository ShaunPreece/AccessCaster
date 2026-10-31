"""FIRMWARE: ask the desk to check for an update, and log everything it pushes about updates.

`updateCheckRequested` is the touchscreen's Check for Update. It is written True on the node that
carries it (SYSTEM, found by walking the tree), and the update* pushes that follow are printed with
their timing. Nothing here downloads or installs: `updateInitiateRequested` is never written.

    python DevTools/probe_update_check.py [seconds]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401
import model_tree
import rcp_read
import rcp_transfer
from _desk import push_handle, pushes

WATCH = ("update", "appUpdateAvailable", "osUpdateAvailable", "systemFirmwareVersion")


def state(root):
    out = {}
    for n in model_tree.walk(root):
        for p, v, _k in n.props:
            if p.startswith(WATCH):
                out[p] = (n.path(), v)
    return out


def main(seconds: float) -> int:
    before = state(model_tree.parse(rcp_read.read_model_stable(fresh=True))[0])
    path = before["updateCheckRequested"][0]
    print("before:", {k: v for k, (_p, v) in sorted(before.items())})
    with push_handle("update check probe") as dev:
        rcp_transfer.send_record(rcp_transfer.build_path_prop_bool("updateCheckRequested", True, path))
        print(f"wrote updateCheckRequested = True at {path}")
        for at, p, name, val in pushes(dev, seconds):
            if name.startswith(WATCH):
                print(f"  +{at:5.1f}s {p} {name} = {val!r}", flush=True)
    after = state(model_tree.parse(rcp_read.read_model_stable(fresh=True))[0])
    changed = {k: (before.get(k, (None, None))[1], v) for k, (_p, v) in after.items()
               if before.get(k, (None, None))[1] != v}
    print("changed:", changed or "nothing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(float(sys.argv[1]) if len(sys.argv) > 1 else 30))
