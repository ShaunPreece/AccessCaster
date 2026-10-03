"""FIRMWARE: ask the desk to check for an update, and log everything it pushes about updates.

`updateCheckRequested` is the touchscreen's Check for Update. It is written True on the node that
carries it (SYSTEM, found by walking the tree), and the update* pushes that follow are printed with
their timing. Nothing here downloads or installs: `updateInitiateRequested` is never written.

    python DevTools/probe_update_check.py [seconds]
"""
from __future__ import annotations
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import model_tree
import rcp_hid
import rcp_read
import rcp_transfer

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
    dev = rcp_hid.open_handle(rcp_hid.find_control_interface(), "update check probe")
    t0 = time.time()
    try:
        dev.set_nonblocking(False)
        rcp_transfer.send_record(rcp_transfer.build_path_prop_bool("updateCheckRequested", True, path))
        print(f"wrote updateCheckRequested = True at {path}")
        while time.time() - t0 < seconds:
            rep = dev.read(rcp_read.BULK_LEN, timeout_ms=400)
            if not rep or rep[0] != 0x04:
                continue
            frame = bytes(rep)
            n = rcp_read._push_record_len(frame)
            if n is None:
                continue
            try:
                p, name, val, _k, _e = model_tree.parse_record(frame[5:5 + n])
            except ValueError:
                continue
            if name.startswith(WATCH):
                print(f"  +{time.time() - t0:5.1f}s {p} {name} = {val!r}", flush=True)
    finally:
        rcp_hid.close_handle(dev, "update check probe")
    after = state(model_tree.parse(rcp_read.read_model_stable(fresh=True))[0])
    changed = {k: (before.get(k, (None, None))[1], v) for k, (_p, v) in after.items()
               if before.get(k, (None, None))[1] != v}
    print("changed:", changed or "nothing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(float(sys.argv[1]) if len(sys.argv) > 1 else 30))
