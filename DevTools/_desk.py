"""Shared helpers for the DevTools scripts that drive a real desk.

The running server's API, how many RODE devices Windows has present, the desk's pushes on a handle
of the caller's own, and one mic's input node and processing chain read off a fresh dump.
"""
from __future__ import annotations

import contextlib
import json
import subprocess
import time
import urllib.error
import urllib.request

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import channels
import model_tree
import rcp_hid
import rcp_read

BASE = "http://127.0.0.1:8770"


def api(path: str, post: dict | None = None, timeout: float = 90.0) -> dict:
    """GET `path` from the running server, or POST `post` to it as the JSON body.

    A failure comes back as `{"ok": False, "message": ...}` rather than raising, with `http_status`
    when the server answered with an error: a 404 is the one failure that would otherwise read as
    the server doing nothing.

    ACTION ROUTES TAKE THEIR ARGUMENTS IN A JSON BODY, NOT THE QUERY STRING, and `do_POST` matches
    the path EXACTLY, so `?backup_first=false` makes the path miss the table and return 404.
    """
    data = json.dumps(post).encode() if post is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return {"ok": False, "http_status": e.code, "message": f"{path} returned HTTP {e.code}"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}


def rode_devices_present() -> int:
    """How many VID_19F7 devices Windows has present, opening none of them.

    Raises RuntimeError when PowerShell's answer is not a count, so a failed query is never read
    as the desk dropping off the bus.
    """
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "@(Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | "
         "Where-Object { $_.InstanceId -match 'VID_19F7' }).Count"],
        capture_output=True, text=True, check=False)
    try:
        return int(out.stdout.strip())
    except ValueError:
        raise RuntimeError(f"could not count the RODE devices: {out.stderr.strip() or 'no output'}"
                           ) from None


@contextlib.contextmanager
def push_handle(purpose: str):
    """A blocking HID handle of its own on the desk's control interface, closed however the block
    ends."""
    dev = rcp_hid.open_handle(rcp_hid.find_control_interface(), purpose)
    try:
        dev.set_nonblocking(False)
        yield dev
    finally:
        rcp_hid.close_handle(dev, purpose)


def pushes(dev, seconds: float, stop=None, poll_ms: int = 400):
    """(seconds since the start, path, name, value) for each push read off `dev` for `seconds`, or
    until `stop()` is true. `poll_ms` is how long one read waits, so how soon `stop` is noticed.

    Decoded by `rcp_read.parse_push`, so a slice of a model dump is never taken for a push."""
    start = time.monotonic()
    while time.monotonic() - start < seconds and not (stop and stop()):
        rep = dev.read(rcp_read.BULK_LEN, timeout_ms=poll_ms)
        got = rcp_read.parse_push(bytes(rep)) if rep else None
        if got is not None:
            path, name, value, _kind = got
            yield time.monotonic() - start, path, name, value


def read_input(slot: int) -> dict:
    """Every property of one INPUTSOURCE node, by input slot (Mic 1 = 0), from a fresh dump."""
    root, _, _ = model_tree.parse(rcp_read.read_model_stable(fresh=True))
    nodes = [n for n in model_tree.walk(root) if n.name == "INPUTSOURCE"]
    return {p: v for p, v, _k in nodes[slot].props}


def mic_chain(ch: int) -> dict:
    """Mic `ch`'s processing chain (the channel's `params`), from a fresh dump."""
    blob = rcp_read.read_model_stable(fresh=True)
    rec = next((r for r in channels.read_channels(blob) if r.get("name") == f"Mic{ch}"), None)
    return {} if rec is None else dict(rec.get("params") or {})


def settled_chain(ch: int, timeout: float = 8.0, pause: float = 0.3) -> dict:
    """Mic `ch`'s chain once two fresh reads `pause` apart agree: a voicing takes a second or two
    to apply, so a single read can catch it part way."""
    end = time.monotonic() + timeout
    prev = mic_chain(ch)
    while time.monotonic() < end:
        time.sleep(pause)
        cur = mic_chain(ch)
        if cur == prev:
            return cur
        prev = cur
    return prev


def advanced_processing(ch: int) -> bool:
    """Whether mic `ch`'s strip has advanced processing on, from a fresh dump."""
    entry = next(e for e in channels.active_ui_channels()
                 if e.get("kind") == "mic" and channels.mic_channel_number(e) == ch)
    flags = rcp_read.values(rcp_read.read_model_stable(fresh=True), channels.CHAIN_ADVANCED_PROP)
    return bool(flags[channels._strip_index(entry)])


def put_chain_back(ch: int, chain: dict, advanced: bool) -> None:
    """Put mic `ch`'s chain and advanced flag back once its voicing has finished applying, since a
    voicing overwrites the chain and writing the old type back does not undo that."""
    settled_chain(ch)
    channels.apply_processing_preset(ch, dict(chain, **{channels.CHAIN_ADVANCED_PROP: advanced}))
    settled_chain(ch)
