"""Prove the announcer's ECHO SUPPRESSION on real hardware. Reversible: restores what it moves.

THE PROBLEM IT GUARDS: the desk pushes our OWN writes back on the state stream, so a level set from
the web UI is announced twice - once by the screen reader reading the UI's live region, once by the
announcer speaking the echo. server._ann_mark_self_write suppresses the second.

Why the test is shaped like this: the marks live in the process that DECODES the stream, and the
process holding port 8770 is the installed build, which does not have them. So this script does both
halves itself - it marks the write exactly as action_set_fader_level does (via _ann_publish_level,
so the fader-index to source-code translation is exercised, not bypassed), drives the real write over
the running app's HTTP API, and reads the real echo off its own HID handle.

Every path is tested in PAIRS, so a silent result cannot be mistaken for a broken listener:
  MARKED   - expect NOTHING spoken (the suppression working)
  UNMARKED - the CONTROL. Whether it speaks is the measurement, not the assumption.

Three paths, all three measured on real hardware:
  LEVEL  a fader level, marked by _ann_publish_level   - the desk DOES echo. Suppression proven.
  MUTE   a channel mute, marked by _ann_publish_mute   - the desk does NOT echo it as
         channelOutputMute. What it pushes instead is twelve `mixMute` records, one per output mix
         (the firmware propagating the mute into the matrix), and the announcer speaks none of them.
         So there is no second announcement on this path to suppress.
  GAIN   a mic gain, marked by _ann_mark_self_gain     - the desk pushes NOTHING back at all,
         though the write itself lands and reads back.

THAT ASYMMETRY IS THE FINDING, and it narrows the claim: "the desk pushes our own writes back" is
true of LEVELS, not of every write. The suppression on the mute and gain paths stays anyway - it
costs nothing, it is right if a firmware ever starts echoing, and removing it would leave the three
writers disagreeing about their own contract - but it is a no-op today rather than a proven fix.

So a silent UNMARKED half is a NOTE, not a pass and not a failure: the check prints what each path
does and fails only when a MARKED write is spoken (suppression genuinely broken) or when a path that
IS known to echo goes quiet (the listener died, and the marked half then proves nothing).

Usage: python DevTools/announcer_echo_check.py

NOTE: the mute and gain phases need a server that HAS /api/channels/mute, i.e. one run from this
source tree (DevTools/run_dev_server.py). Against an older installed build they report the 404
rather than guessing, because a missing endpoint looks exactly like perfect suppression.
"""
from __future__ import annotations
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401  (puts Project/ on sys.path)

import hid
import server
import channels
import rcp_read
import rcp_transfer

BASE = "http://127.0.0.1:8770"
FADER = 8          # 0-based fader index: the USB 2 virtual fader (no physical fader to fight)
MUTE_CHAN = "Mic3"  # nothing is plugged into Mic 3, and the standing rule keeps Mic 1 unwritten
GAIN_CHAN = 3       # same reason: Mic 1 carries Shaun's SM7B and is never written by a test

SAID: list[tuple[float, str]] = []
_t0 = time.time()
server._ann_say = lambda text: SAID.append((time.time() - _t0, text))
# Level and gain pushes are gated on the "announce levels" setting. This process only DECODES, it
# never speaks, so forcing it on cannot make the real announcer chattier - but leaving it off would
# make the unmarked control halves silent and report a false pass.
server._ann_levels_on = True


def post(path: str, payload: dict) -> dict:
    """POST and return the JSON. A 404 comes back as a RESULT rather than an exception, because a
    missing endpoint is the one failure that would otherwise read as flawless suppression."""
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        return {"ok": False, "http_status": exc.code,
                "message": f"{path} returned HTTP {exc.code}"}


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read().decode())


def listen(dev, st: dict, secs: float) -> list[str]:
    """Pump the decoder for `secs` and return everything it would have spoken."""
    before = len(SAID)
    end = time.time() + secs
    while time.time() < end:
        rep = dev.read(256, timeout_ms=100)
        if rep:
            server._ann_handle_report(bytes(rep), st)
        server._ann_flush_pending(st)
    return [text for _t, text in SAID[before:]]


def phase(dev, st, label: str, mark, path: str, payload: dict, expect_speech, failures: list,
          notes: list) -> list[str]:
    """One half of a pair: optionally mark the write as ours, drive it over HTTP, then listen.

    `mark` is a zero-argument callable (or None for the UNMARKED control). Marking happens BEFORE
    the write, exactly as the server does it, so the echo cannot land inside the gap.

    `expect_speech` is True (this path echoes, so silence here means the listener is dead),
    False (a marked write: speech here is the suppression failing) or None (measured to be silent
    because the desk does not echo this path - report it, do not judge it)."""
    if mark is not None:
        mark()
    res = post(path, payload)
    if res.get("http_status") == 404:
        failures.append(f"{label}: {res['message']} - run the server from THIS source tree "
                        f"(python DevTools/run_dev_server.py)")
        print(f"{label:22} -> {res['message']}")
        return []
    if not res.get("ok"):
        failures.append(f"{label}: the write itself failed: {res.get('message')}")
    said = listen(dev, st, 4.0)
    print(f"{label:22} -> spoken: {said or 'nothing'}")
    if expect_speech is True and not said:
        failures.append(f"{label}: no echo at all, so the marked half proves nothing "
                        f"(the listener is not hearing the desk)")
    if expect_speech is False and said:
        failures.append(f"{label}: suppression failed, announcer still said {said}")
    if expect_speech is None:
        notes.append(f"{label}: {'the desk NOW echoes this path, so the '
                                 'suppression has become a real fix rather than a no-op'
                     if said else 'silent, as measured: the desk does not echo this write'}")
    return said


def main() -> int:
    levels = get("/api/faders/levels")
    original = next((f["percent"] for f in levels["faders"] if f["index"] == FADER), None)
    if original is None:
        print(f"fader index {FADER} has no level control here; nothing to test")
        return 1
    print(f"fader {FADER} starts at {original} percent (restored at the end)")

    vec = [v for v, _k in rcp_read.find_all(rcp_read.read_model_stable(fresh=True),
                                            "channelInputSource")]
    channels.publish_strip_vector(vec)
    print(f"fader layout: {vec}  -> fader {FADER} carries source code {vec[FADER]}")

    iface = rcp_transfer.find_control_interface()
    if iface is None:
        print("no device")
        return 1
    chans = {c["name"]: c for c in get("/api/channels")["channels"]}
    mute_was = bool(chans[MUTE_CHAN]["muted"])
    strip = channels.channel_strip_index(MUTE_CHAN)
    # The channels list carries mute but not gain (gain is a mixer read), so take it from the desk.
    gain_was = channels.read_mixer()["mics"][GAIN_CHAN]["gain_db"]
    print(f"{MUTE_CHAN} starts {'muted' if mute_was else 'unmuted'} on strip {strip}; "
          f"Mic {GAIN_CHAN} gain starts at {gain_was} dB (both restored at the end)")

    dev = hid.device()
    dev.open_path(iface.path)
    st = {"bank": None, "pending": None, "at": 0.0}
    server._ann_live_state = st
    failures, notes = [], []
    try:
        listen(dev, st, 2.0)          # let the stream settle, and swallow any startup chatter

        # ---- LEVEL: the one path the desk really does echo. This is the regression. ----
        target_a = 60 if original != 60 else 55
        phase(dev, st, "LEVEL   MARKED",
              lambda: server._ann_publish_level("fader", FADER, target_a / 100.0),
              "/api/faders/level", {"fader": FADER, "percent": target_a}, False, failures, notes)
        phase(dev, st, "LEVEL   UNMARKED", None,
              "/api/faders/level", {"fader": FADER, "percent": original}, True, failures, notes)

        # ---- MUTE: keyed by STRIP index, so no layout translation on either side. ----
        # Flip AWAY from where it started for the marked half and back for the unmarked one, which
        # also means the control half doubles as the restore. Both halves are expected SILENT: see
        # the module docstring - the desk answers a software mute with mixMute matrix records only.
        phase(dev, st, "MUTE    MARKED",
              lambda: server._ann_publish_mute(strip, not mute_was),
              "/api/channels/mute", {"name": MUTE_CHAN, "on": not mute_was}, False, failures, notes)
        phase(dev, st, "MUTE    UNMARKED", None,
              "/api/channels/mute", {"name": MUTE_CHAN, "on": mute_was}, None, failures, notes)

        # ---- GAIN: the two sides name a gain differently, which is the whole point of the mark. ----
        # Two DIFFERENT values, because the push side dedupes on (who, value): repeating a value
        # would be swallowed by that dedupe and the silence would prove nothing.
        base = gain_was if isinstance(gain_was, int) else 0
        g_a = base + 6 if base <= 30 else base - 6
        g_b = base + 12 if base <= 24 else base - 12
        phase(dev, st, "GAIN    MARKED",
              lambda: server._ann_mark_self_gain(GAIN_CHAN, True),
              "/api/channel/gain", {"channel": GAIN_CHAN, "db": g_a}, False, failures, notes)
        phase(dev, st, "GAIN    UNMARKED", None,
              "/api/channel/gain", {"channel": GAIN_CHAN, "db": g_b}, None, failures, notes)
    finally:
        dev.close()
        post("/api/faders/level", {"fader": FADER, "percent": original})
        post("/api/channels/mute", {"name": MUTE_CHAN, "on": mute_was})
        if isinstance(gain_was, int):
            post("/api/channel/gain", {"channel": GAIN_CHAN, "db": gain_was})
        back = get("/api/faders/levels")
        now = next((f["percent"] for f in back["faders"] if f["index"] == FADER), None)
        chans2 = {c["name"]: c for c in get("/api/channels")["channels"]}
        mute_now = bool(chans2[MUTE_CHAN]["muted"])
        gain_now = channels.read_mixer()["mics"][GAIN_CHAN]["gain_db"]
        for what, before, after in (("fader level", original, now),
                                    (f"{MUTE_CHAN} mute", mute_was, mute_now),
                                    (f"Mic {GAIN_CHAN} gain", gain_was, gain_now)):
            ok = (before == after) or after is None and before is None
            print(f"restored: {what} {before} -> {after} "
                  f"({'ok' if ok else 'MISMATCH, check the desk'})")
            if not ok:
                failures.append(f"restore: {what} came back as {after}, not {before}")

    if notes:
        print("\nMEASURED: \n  " + "\n  ".join(notes))
    print("\nRESULT: " + ("PASS - our own writes are silent, and the one path that really echoes "
                          "(level) still does"
                          if not failures else "FAIL\n  " + "\n  ".join(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
