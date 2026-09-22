"""Phase 1 of the coverage audit: what does the desk TELL us while it is being used.

READ ONLY. This script never sends a write record; the only thing it transmits is the same
full-model dump trigger our normal reads use (`listen` does not even do that). Safe to run with
the app server up: Windows gives every open HID handle its own input queue, which is why the
server's announcer can hold a read handle while pad writes go through another (server.py).

Modes
  listen  <seconds> [--label L]   PASSIVE. Log the report-04 stream WITHOUT asking for anything.
                                  Answers the phase-1 question "can we subscribe instead of poll":
                                  if the desk pushes meters / fader moves / mutes unasked, live
                                  feedback is nearly free. Nothing else in the project has ever
                                  looked at what arrives unsolicited.
  snap    <name>                  One full dump, parsed into every property, saved (redacted) as
                                  a JSON snapshot for later diffing.
  diff    <nameA> <nameB>         What changed between two snapshots, per property occurrence.
  watch   [--dumps N] [--gap S]   Repeated dumps, diffed against each other as they arrive: for
                                  watching something move (a fader, a meter) with no second call.
  cost    [--n N]                 Time a dump, so phase 1 can say whether polling is viable.
  step    <label> [--raw]         ONE action, and the capture closes when the operator creates
                                  STOP_FILE, not when a clock expires. Use this, not `guided`,
                                  whenever the prompt has to be RELAYED to whoever is at the desk:
                                  see step()'s docstring for what timed windows cost us.

Usage with a human at the desk (the intended flow):
    python DevTools/state_observatory.py snap before
    ... Shaun does ONE thing ...
    python DevTools/state_observatory.py diff before after --take

SECRETS: a model dump carries plaintext wifi PSK and SIP credentials, so every value written to
disk or printed is redacted by SECRET_NAME below. Snapshots land in DevTools/captures/ which is
gitignored; never move one out of there.
"""
from __future__ import annotations
import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401  (puts Project/ on sys.path)

import hid
import rcp_read
import rcp_hid

OUT_DIR = Path(__file__).resolve().parent / "captures" / "observatory"

# Any property whose NAME matches this never has its value printed or saved.
SECRET_NAME = re.compile(r"psk|password|paircode|rodecode|authusername|username|ssid|serial",
                         re.IGNORECASE)
# A property name in the stream: an identifier followed by the name terminator.
NAME_RE = re.compile(rb"[A-Za-z][A-Za-z0-9]{3,63}\x00")


def split_selector(matched: str) -> tuple[str, int | None]:
    """Peel a selector byte off the front of a regex-matched name.

    NAME_RE cannot tell a name from a name with an address byte glued to its front, and the bytes
    we most want are exactly the ones it eats: a matrix cross-point at 0x4c to 0x58 is ASCII 'L' to
    'X', so `mixLevelWithAnchor` there arrives looking like `LmixLevelWithAnchor`. Measured on a
    Duo: a fader move reported thirteen different "properties", one per
    cross-point, and the real name appeared nowhere.

    EVERY property this protocol publishes starts lowercase (all 462 the Duo reports, checked), so
    an initial CAPITAL is never part of a name and is always the preceding byte. Returns the clean
    name plus that byte, which is the address the caller wanted all along.

    PARTIAL BY CONSTRUCTION: a selector in the lowercase range 0x61 to 0x7a is still invisible this
    way. The complete fix is to parse records structurally via `model_tree` instead of scanning for
    names; this buys the addresses back without that rework.
    """
    if matched[:1].isupper():
        return matched[1:], ord(matched[0])
    return matched, None


def redact(name: str, value):
    return "<redacted>" if SECRET_NAME.search(name) else value


# ---------------------------------------------------------------------------
# Generic parse: every property in a dump, not just the ones we already know
# ---------------------------------------------------------------------------
def parse_all(blob: bytes) -> dict[str, list]:
    """{name: [value, ...]} in stream order, for EVERY decodable property in the dump.

    rcp_read.find_all answers "what is the value of X" for a name we already know. Phase 1 needs
    the opposite: everything that is in there, so a diff can surface a property no one has ever
    looked at. Names inside string values can produce a phantom entry; harmless here because both
    sides of a diff are parsed the same way.
    """
    out: dict[str, list] = {}
    for m in NAME_RE.finditer(blob):
        name = m.group()[:-1].decode("latin1")
        value, kind = rcp_read._val_after(blob, m.end() - 1)
        if kind == "?":
            continue
        out.setdefault(name, []).append(value)
    return out


def snapshot(fresh: bool = True) -> dict[str, list]:
    return parse_all(rcp_read.read_model(fresh=fresh))


def save(name: str, data: dict[str, list]) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{name}.json"
    safe = {k: ([redact(k, v) for v in vals]) for k, vals in data.items()}
    path.write_text(json.dumps({"at": time.time(), "props": safe}, indent=1), encoding="utf-8")
    return path


def load(name: str) -> dict[str, list]:
    path = OUT_DIR / f"{name}.json"
    if not path.exists():
        raise SystemExit(f"no snapshot called {name!r} (looked in {OUT_DIR})")
    return json.loads(path.read_text(encoding="utf-8"))["props"]


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------
def diff(a: dict[str, list], b: dict[str, list]) -> list[tuple[str, int, object, object]]:
    """(name, occurrence index, before, after) for every value that moved."""
    changes = []
    for name in sorted(set(a) | set(b)):
        va, vb = a.get(name, []), b.get(name, [])
        if len(va) != len(vb):
            changes.append((name, -1, f"{len(va)} occurrences", f"{len(vb)} occurrences"))
            continue
        for i, (x, y) in enumerate(zip(va, vb)):
            if x != y:
                changes.append((name, i, redact(name, x), redact(name, y)))
    return changes


def print_diff(changes, title: str) -> None:
    print(f"\n=== {title}: {len(changes)} value(s) changed ===")
    by_name: dict[str, list] = {}
    for name, i, x, y in changes:
        by_name.setdefault(name, []).append((i, x, y))
    for name, items in sorted(by_name.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        head = ", ".join(f"[{i}] {x} -> {y}" for i, x, y in items[:6])
        more = f"  (+{len(items) - 6} more)" if len(items) > 6 else ""
        print(f"  {name:32} {head}{more}")
    if not changes:
        print("  (nothing moved)")


# ---------------------------------------------------------------------------
# Passive listen
# ---------------------------------------------------------------------------
def listen(seconds: float, label: str) -> None:
    """Log the unsolicited report-04 stream. No trigger is sent: whatever arrives, the desk sent
    of its own accord."""
    iface = rcp_hid.find_control_interface()
    if iface is None:
        raise SystemExit("no control interface (is the unit connected?)")
    dev = hid.device()
    dev.open_path(iface.path)
    seen: dict[str, list] = {}
    packets, first_at, last_at = 0, None, None
    t_end = time.time() + seconds
    try:
        while time.time() < t_end:
            rep = dev.read(256, timeout_ms=200)
            if not rep:
                continue
            b = bytes(rep)
            if b[0] != 0x04:
                continue
            now = time.time()
            packets += 1
            first_at = first_at or now
            last_at = now
            payload = b[1:]
            for m in NAME_RE.finditer(payload):
                name, sel = split_selector(m.group()[:-1].decode("latin1"))
                value, kind = rcp_read._val_after(payload, m.end() - 1)
                if kind == "?":
                    continue
                key = f"{name} @0x{sel:02x}" if sel is not None else name
                seen.setdefault(key, []).append((round(now - (first_at or now), 2),
                                                 redact(name, value)))
    finally:
        dev.close()
    span = (last_at - first_at) if (first_at and last_at) else 0.0
    print(f"\n=== listen [{label}] {seconds:.0f}s: {packets} report-04 packets"
          f"{f' over {span:.1f}s' if span else ''}, {len(seen)} distinct properties ===")
    if not seen:
        print("  NOTHING was pushed. This desk only speaks when asked, so live state must be polled.")
        return
    for name, hits in sorted(seen.items(), key=lambda kv: -len(kv[1])):
        vals = [v for _t, v in hits]
        distinct = len({repr(v) for v in vals})
        rate = f"{len(hits) / span:.1f}/s" if span else "-"
        print(f"  {name:32} {len(hits):5} update(s)  {rate:>8}  {distinct} distinct  "
              f"first={vals[0]!r} last={vals[-1]!r}")


# ---------------------------------------------------------------------------
# One action, one capture, closed on the operator's word
# ---------------------------------------------------------------------------
STOP_FILE = OUT_DIR / "step.stop"


def step(label: str, timeout: float, raw: bool, speak: bool = False) -> None:
    """Capture ONE action, and close when STOP_FILE appears rather than when a clock runs out.

    Why a file and not a keypress: the person at the desk and the person at the keyboard are not
    the same person, and the keyboard one is driving through a harness with no terminal, so there
    is nothing to press. Writing a file is the one gate both can reach. Same idiom as
    `cap_control/cmd.txt`.

    Why not a timer: `guided` used timed windows and every one of them drifted, because relaying a
    spoken prompt by voice to someone in another room costs seconds that the clock does not know
    about. Measured on a Duo: the mute window caught a soundpad trigger and the
    REC window caught a touchscreen tap, so two of the five answers we came for were missed. The
    timeout here is a safety net for an abandoned capture, not the mechanism.

    ONE action per capture is the other half of the fix: with a single action in the window there is
    nothing to mis-attribute it to.
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    STOP_FILE.unlink(missing_ok=True)  # a stale one would close the capture instantly
    iface = rcp_hid.find_control_interface()
    if iface is None:
        raise SystemExit("no control interface (is the unit connected?)")
    dev = hid.device()
    dev.open_path(iface.path)

    # DELIBERATELY SILENT by default. The operator relays the instruction by voice anyway, so
    # speaking it here reaches nobody who needs it AND collides with the app's own announcer, which
    # is talking about the very control under test - measured: a spoken prompt here talks straight
    # over the announcer saying "encoder not linked", which is the answer the capture is for.
    if speak:
        _say(label)
    print(f"CAPTURE OPEN: {label}", flush=True)
    print(f"  close it by creating {STOP_FILE}  (or wait {timeout:.0f}s)", flush=True)
    events, packets, t0 = [], 0, time.time()
    stopped_by = "timeout"
    try:
        while time.time() - t0 < timeout:
            if STOP_FILE.exists():
                stopped_by = "operator"
                break
            rep = dev.read(256, timeout_ms=100)
            if not rep:
                continue
            b = bytes(rep)
            if b[0] != 0x04:
                continue
            packets += 1
            payload = b[1:]
            for m in NAME_RE.finditer(payload):
                name, sel = split_selector(m.group()[:-1].decode("latin1"))
                value, kind = rcp_read._val_after(payload, m.end() - 1)
                if kind == "?":
                    continue
                pre = payload[max(0, m.start() - 8):m.start()].hex() if raw else ""
                events.append((round(time.time() - t0, 2), name, sel,
                               redact(name, value), pre))
    finally:
        dev.close()
        STOP_FILE.unlink(missing_ok=True)

    took = time.time() - t0
    print(f"\n=== {label} === closed by {stopped_by} after {took:.1f}s: "
          f"{packets} report-04 packets, {len(events)} decoded values", flush=True)
    if not events:
        print("  NOTHING pushed. Either the desk stays silent for this control, or the action "
              "never happened inside the capture.", flush=True)
        return
    # Group by (name, selector): the selector is what tells "channel 1 muted" from "channel 2".
    by_key: dict[tuple[str, int | None], list] = {}
    for t, name, sel, value, pre in events:
        by_key.setdefault((name, sel), []).append((t, value, pre))
    for (name, sel), hits in sorted(by_key.items(), key=lambda kv: -len(kv[1])):
        vals = [v for _t, v, _p in hits]
        span = hits[-1][0] - hits[0][0]
        rate = f"{len(hits) / span:.1f}/s" if span > 0.2 else "-"
        addr = f"0x{sel:02x}" if sel is not None else "  -  "
        print(f"  {addr}  {name:28} {len(hits):4} x {rate:>8}  "
              f"{len({repr(v) for v in vals})} distinct  first={vals[0]!r} last={vals[-1]!r}",
              flush=True)
        if raw:
            for t, value, pre in hits[:4]:
                print(f"        t={t:5.1f}  pre={pre}  value={value!r}", flush=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    safe_label = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:60]
    path = OUT_DIR / f"step_{safe_label}.json"
    path.write_text(json.dumps(events, indent=1), encoding="utf-8")
    print(f"\nevent log -> {path}", flush=True)


# ---------------------------------------------------------------------------
# Guided session: speak each step, listen throughout, tag events by step
# ---------------------------------------------------------------------------
# Shaun is at the DESK during phase 1, not at the keyboard, so the steps have to be spoken.
# say.ps1 normally stays silent while the Claude GUI has focus; -Force overrides that, which is
# exactly this case (he is not reading the chat, he is pressing buttons).
SAY = Path(__file__).resolve().parent.parent / "Project" / "say.ps1"

# (spoken prompt, seconds to allow). Kept here so a re-run on the Duo uses the same script.
DEFAULT_STEPS = [
    ("Move fader 1 slowly all the way up, then back down", 9),
    ("Press mute on channel 1, then press it again to unmute", 8),
    ("Press solo on channel 1, then press it again", 8),
    ("Turn the encoder a few clicks, then press it in", 9),
    ("Talk into your microphone until I say stop", 7),
    ("Stop talking. Tap the touchscreen once", 6),
    ("All done, hands off the desk", 4),
]


def _say(text: str) -> None:
    """Speak without blocking the listener (the read loop must not miss packets)."""
    import subprocess
    try:
        subprocess.Popen(["powershell", "-ExecutionPolicy", "Bypass", "-File", str(SAY),
                          "-Text", text, "-Force"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"(could not speak: {e})")


def guided(steps=None, raw: bool = False) -> None:
    """Run the spoken step list while passively logging the report-04 stream.

    raw=True also keeps the 8 bytes in front of each property name. A pushed record carries its
    SELECTOR there, which is how we tell "channel 1 muted" from "channel 2 muted" - the summary
    alone only shows the value.
    """
    steps = steps or DEFAULT_STEPS
    iface = rcp_hid.find_control_interface()
    if iface is None:
        raise SystemExit("no control interface (is the unit connected?)")
    dev = hid.device()
    dev.open_path(iface.path)
    events: list[tuple[float, str, str, object]] = []   # (t, step, name, value)
    packets = 0
    _say("Starting the listen now")
    time.sleep(2.5)
    t0 = time.time()
    try:
        for prompt, secs in steps:
            _say(prompt)
            print(f"[{time.time() - t0:5.1f}s] STEP: {prompt}  ({secs}s)")
            t_end = time.time() + secs
            while time.time() < t_end:
                rep = dev.read(256, timeout_ms=100)
                if not rep:
                    continue
                b = bytes(rep)
                if b[0] != 0x04:
                    continue
                packets += 1
                payload = b[1:]
                for m in NAME_RE.finditer(payload):
                    name = m.group()[:-1].decode("latin1")
                    value, kind = rcp_read._val_after(payload, m.end() - 1)
                    if kind == "?":
                        continue
                    pre = payload[max(0, m.start() - 8):m.start()].hex() if raw else ""
                    events.append((round(time.time() - t0, 2), prompt, name,
                                   redact(name, value), pre))
    finally:
        dev.close()

    print(f"\n=== guided session: {packets} report-04 packets, {len(events)} decoded values, "
          f"{len({e[2] for e in events})} distinct properties ===")
    for prompt, _secs in steps:
        rows = [e for e in events if e[1] == prompt]
        print(f"\n-- {prompt}  ({len(rows)} value(s))")
        if not rows:
            print("     nothing pushed")
            continue
        by_name: dict[str, list] = {}
        for t, _p, name, value, pre in rows:
            by_name.setdefault(name, []).append((t, value, pre))
        for name, hits in sorted(by_name.items(), key=lambda kv: -len(kv[1])):
            vals = [v for _t, v, _pre in hits]
            distinct = len({repr(v) for v in vals})
            span = hits[-1][0] - hits[0][0]
            rate = f"{len(hits) / span:.1f}/s" if span > 0.2 else "-"
            print(f"     {name:28} {len(hits):4} x  {rate:>7}  {distinct} distinct  "
                  f"first={vals[0]!r} last={vals[-1]!r}")
            if raw:
                for t, value, pre in hits[:6]:
                    print(f"        t={t:5.1f}  pre={pre}  value={value!r}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "guided_events.json"
    path.write_text(json.dumps(events, indent=1), encoding="utf-8")
    print(f"\nraw event log -> {path}")


# ---------------------------------------------------------------------------
# Watch + cost
# ---------------------------------------------------------------------------
def watch(dumps: int, gap: float, label: str) -> None:
    prev, prev_t = None, None
    for n in range(dumps):
        t0 = time.time()
        cur = snapshot()
        took = time.time() - t0
        if prev is not None:
            print_diff(diff(prev, cur), f"{label} dump {n} (dump took {took:.2f}s, "
                                        f"{time.time() - prev_t:.2f}s since the last)")
        else:
            print(f"=== {label}: baseline dump, {sum(len(v) for v in cur.values())} values "
                  f"across {len(cur)} properties in {took:.2f}s ===")
        prev, prev_t = cur, time.time()
        if n < dumps - 1:
            time.sleep(gap)


def cost(n: int) -> None:
    times = []
    for _ in range(n):
        t0 = time.time()
        blob = rcp_read.read_model(fresh=True)
        times.append(time.time() - t0)
        print(f"  dump {len(blob)} bytes in {times[-1]:.3f}s")
    print(f"=== {n} dumps: best {min(times):.3f}s, worst {max(times):.3f}s, "
          f"mean {sum(times) / len(times):.3f}s ===")


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 1 state observatory (read only)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("listen"); p.add_argument("seconds", type=float, nargs="?", default=10.0)
    p.add_argument("--label", default="idle")

    p = sub.add_parser("snap"); p.add_argument("name")

    p = sub.add_parser("diff"); p.add_argument("a"); p.add_argument("b")
    p.add_argument("--take", action="store_true",
                   help="take snapshot B live now instead of loading it from disk")

    p = sub.add_parser("watch"); p.add_argument("--dumps", type=int, default=3)
    p.add_argument("--gap", type=float, default=1.0); p.add_argument("--label", default="watch")

    p = sub.add_parser("cost"); p.add_argument("--n", type=int, default=3)

    p = sub.add_parser("guided", help="speak the step list aloud and log what the desk pushes")
    p.add_argument("--raw", action="store_true", help="also show the 8 bytes before each name")
    p.add_argument("--steps", default="", help='"prompt:secs;prompt:secs" to override the script')

    p = sub.add_parser("step", help="ONE action, closed on the operator's word, not on a clock")
    p.add_argument("label")
    p.add_argument("--timeout", type=float, default=180.0, help="safety net for an abandoned capture")
    p.add_argument("--raw", action="store_true", help="also show the 8 bytes before each name")
    p.add_argument("--speak", action="store_true",
                   help="speak the label too; off by default, it fights the app's announcer")

    args = ap.parse_args()
    if args.cmd == "listen":
        listen(args.seconds, args.label)
    elif args.cmd == "step":
        step(args.label, args.timeout, args.raw, args.speak)
    elif args.cmd == "snap":
        data = snapshot()
        path = save(args.name, data)
        print(f"snapshot {args.name!r}: {len(data)} properties, "
              f"{sum(len(v) for v in data.values())} values -> {path}")
    elif args.cmd == "diff":
        a = load(args.a)
        if args.take:
            b = snapshot()
            save(args.b, b)
        else:
            b = load(args.b)
        print_diff(diff(a, b), f"{args.a} -> {args.b}")
    elif args.cmd == "watch":
        watch(args.dumps, args.gap, args.label)
    elif args.cmd == "cost":
        cost(args.n)
    elif args.cmd == "guided":
        steps = None
        if args.steps:
            steps = []
            for part in args.steps.split(";"):
                prompt, _, secs = part.rpartition(":")
                steps.append((prompt.strip(), float(secs)))
        guided(steps, raw=args.raw)


if __name__ == "__main__":
    main()
