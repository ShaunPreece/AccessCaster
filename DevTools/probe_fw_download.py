"""FIRMWARE: start the desk's own update download, then cancel it before it finishes.

Proves the download half of an update with nothing installed: `downloadInitiateRequested` starts
the download, `updateDownloadProgress` reports it, and `downloadCancelRequested` stops it and
deletes what was staged. The cancel is sent once progress passes `--cancel-at` percent, or
after `--max-s`, and however the watch ends, so an interrupted run never leaves the desk
downloading. If progress still moves after the cancel, the desk's wifi is switched off until it
stops, then switched back on and read back.

A cancel sent mid-download holds the desk's engine for about two and a half minutes, every model
dump coming back empty; the read afterwards waits that out.

Nothing here installs. `updateInitiateRequested` and `updateRebootRequested` are never written, and
the script refuses to start if `updateResetAfterFWURequested` is set (a reboot would then wipe
the desk). `updateDownloaded` can read True from an earlier download: a cancel leaves it set.

    python DevTools/probe_fw_download.py [--cancel-at 20] [--max-s 90]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401
import model_tree
import rcp_read
import rcp_transfer
from _desk import push_handle, pushes

WATCH = ("update", "download", "appUpdateAvailable", "osUpdateAvailable", "systemFirmwareVersion")
WIFI_OFF_LIMIT_S = 120   # how long the wifi stays off waiting for a download to stop


def state(wait_s: float = 0.0):
    """The update properties; with `wait_s`, retried while the engine is held by a cancel."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            root = model_tree.parse(rcp_read.read_model_stable(fresh=True))[0]
            break
        except ValueError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(10)
    out = {}
    for n in model_tree.walk(root):
        for p, v, _k in n.props:
            if p.startswith(WATCH):
                out[p] = (n.path(), v)
    return out


def write(st, name, value=True):
    rcp_transfer.send_record(rcp_transfer.build_path_prop_bool(name, value, st[name][0]))
    print(f"wrote {name} = {value} at {st[name][0]}", flush=True)


def wifi_on():
    return rcp_read.block_value(rcp_read.read_model_stable(fresh=True), "NETWORK", "wifi")


def progress_now():
    return state(wait_s=300).get("updateDownloadProgress", (None, None))[1]


def wait_until_still(limit_s: float) -> bool:
    """True once two readings 8 s apart show the same download progress."""
    deadline = time.monotonic() + limit_s
    last = progress_now()
    while time.monotonic() < deadline:
        time.sleep(8)
        now = progress_now()
        if now == last:
            return True
        last = now
    return False


def main(cancel_at: int, max_s: float) -> int:
    before = state()
    values = {k: v for k, (_p, v) in before.items()}
    print("before:", dict(sorted(values.items())), flush=True)
    if values.get("updateResetAfterFWURequested"):
        print("REFUSED: updateResetAfterFWURequested is set, so any reboot would wipe the desk.")
        return 1
    if not (values.get("osUpdateAvailable") or values.get("appUpdateAvailable")):
        print("REFUSED: the desk offers no update; run a check first.")
        return 1

    progress, cancelled_at, last_move = 0, None, time.monotonic()
    # The engine acts on a change to True and never clears these, so each is written False first.
    for name in ("downloadInitiateRequested", "downloadCancelRequested"):
        if values.get(name):
            write(before, name, False)
    with push_handle("firmware download probe") as dev:
        write(before, "downloadInitiateRequested")
        start = time.monotonic()

        def done():
            # Ten seconds of stillness after the cancel is the evidence the download stopped.
            return cancelled_at is not None and time.monotonic() - last_move > 10

        try:
            for at, path, name, val in pushes(dev, max_s + 60, stop=done):
                if not name.startswith(WATCH):
                    continue
                print(f"  +{at:6.1f}s {path} {name} = {val!r}", flush=True)
                if name == "updateDownloadProgress":
                    if val != progress:
                        last_move = time.monotonic()
                    progress = val or 0
                if name == "updateDownloaded" and val:
                    print("WARNING: the download finished before the cancel.", flush=True)
                if cancelled_at is None and (progress >= cancel_at or at >= max_s):
                    write(before, "downloadCancelRequested")
                    cancelled_at, last_move = time.monotonic(), time.monotonic()
        finally:
            if cancelled_at is None:
                write(before, "downloadCancelRequested")
                cancelled_at = time.monotonic()
                time.sleep(10)
        print(f"download ran {cancelled_at - start:.1f}s before the cancel", flush=True)

    time.sleep(3)
    after = state(wait_s=300)
    first = after.get("updateDownloadProgress", (None, None))[1]
    time.sleep(8)
    later = state()
    second = later.get("updateDownloadProgress", (None, None))[1]
    if second not in (0, None) and second != first:
        if not wifi_on():
            print(f"STILL DOWNLOADING after the cancel ({first} then {second}), with the wifi "
                  "already off, so the download is not on wifi. Check the desk.")
        else:
            print(f"STILL DOWNLOADING after the cancel ({first} then {second}): switching the "
                  "wifi off until it stops.", flush=True)
            rcp_transfer.write_at(rcp_transfer.NETWORK, "wifi", "bool", False)
            try:
                stopped = wait_until_still(WIFI_OFF_LIMIT_S)
                print("the download stopped" if stopped else
                      f"STILL MOVING after {WIFI_OFF_LIMIT_S} s with the wifi off", flush=True)
            finally:
                rcp_transfer.write_at(rcp_transfer.NETWORK, "wifi", "bool", True)
            time.sleep(2)
            print("wifi switched back on" if wifi_on() else
                  "WIFI DID NOT COME BACK ON: switch it on in the desk's network settings.")
        later = state()
    changed = {k: (values.get(k), v) for k, (_p, v) in later.items() if values.get(k) != v}
    print("changed from before:", changed or "nothing", flush=True)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cancel-at", type=int, default=20)
    ap.add_argument("--max-s", type=float, default=90.0)
    a = ap.parse_args()
    raise SystemExit(main(a.cancel_at, a.max_s))
