"""STORAGE: eject or erase the memory card or the USB drive, and log what the desk does.

`storageVolumeEject` and `storageVolumeErase` are per-volume switches on the STORAGEVOLUME nodes
under SYSTEM. THE THIRD VOLUME IS THE UNIT'S INTERNAL STORAGE, WHERE THE PAD SOUNDS LIVE, so this
refuses any volume `recorder.read_volumes` does not mark `can_record`. Erase deletes everything on
the chosen volume.

    python DevTools/probe_storage_live.py state
    python DevTools/probe_storage_live.py eject <0 card | 1 drive> [seconds]
    python DevTools/probe_storage_live.py erase <0 card | 1 drive> [seconds]
"""
from __future__ import annotations
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import model_tree
import rcp_hid
import rcp_read
import rcp_transfer
import recorder

PROP = {"eject": "storageVolumeEject", "erase": "storageVolumeErase"}


def volumes():
    blob = rcp_read.read_model_stable(fresh=True)
    root = model_tree.parse(blob)[0]
    nodes = [n for n in model_tree.walk(root) if n.name == "STORAGEVOLUME"]
    return recorder.read_volumes(blob), nodes


def show():
    vols, nodes = volumes()
    for v in vols:
        print(json.dumps({k: v[k] for k in ("index", "label", "inserted", "mounted", "free_mb",
                                            "can_record")}))
    for n in nodes:
        print("  ", n.path(), {p: v for p, v, _k in n.props if p in (
            "storageVolumeState", "storageVolumeInserted", "storageVolumeMounted",
            "storageVolumeEject", "storageVolumeErase", "storageVolumeFormatted")})


def act(what: str, index: int, seconds: float) -> int:
    vols, nodes = volumes()
    vol = next((v for v in vols if v["index"] == index), None)
    if vol is None or not vol.get("can_record"):
        raise SystemExit(f"refused: volume {index} is not a card or drive this may {what}")
    if not vol.get("inserted"):
        raise SystemExit(f"refused: nothing is in {vol['label']}")
    node = nodes[index]
    prop = PROP[what]
    print(f"{what} {vol['label']} at {node.path()} ({vol['free_mb']} MB free)")
    show()
    dev = rcp_hid.open_handle(rcp_hid.find_control_interface(), f"storage {what} probe")
    t0 = time.time()
    try:
        dev.set_nonblocking(False)
        rcp_transfer.send_record(rcp_transfer.build_path_prop_bool(prop, True, node.path()))
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
            if name.startswith(("storage", "record", "show")):
                print(f"  +{time.time() - t0:5.1f}s {p} {name} = {val!r}", flush=True)
    finally:
        rcp_hid.close_handle(dev, f"storage {what} probe")
    print("after:")
    show()
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] == "state":
        show()
    elif a[0] in PROP and len(a) >= 2:
        raise SystemExit(act(a[0], int(a[1]), float(a[2]) if len(a) > 2 else 15))
    else:
        print(__doc__)
