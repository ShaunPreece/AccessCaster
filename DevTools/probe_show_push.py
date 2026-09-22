"""What does the desk PUSH, and what moves in its tree, when a show command is sent?

TWO QUESTIONS, both blocking other work.

1. DID THE COMMAND REACH THE ENGINE? On a desk with no storage an export cannot succeed, and
   `showControlLastError` LATCHES at 8, so once it has failed once, a later failed export is
   indistinguishable from no export at all. That made ten export trials on worth
   nothing: no drop, and no evidence the command ever landed. The whole-tree diff is this project's
   own answer to that (make the ENGINE answer, then diff everything), so it is what this uses.

2. CAN WE DETECT A COMMAND FINISHING BY LISTENING INSTEAD OF POLLING? The app currently proves a
   reset by re-reading the ENTIRE property tree every 1.5 s until the loaded show's uuid changes,
   because the protocol has no partial read: one trigger, 536 frames, about 136 kB. But the desk
   also pushes its own report-04 frames whenever something changes on it, unprompted, which is how
   the recording watcher already sees a take start without reading anything. If a show command
   pushes anything that identifies itself, the polling can go, and with it all our traffic during
   the window where the desk is rebuilding its USB.

THE LISTENER NEVER RUNS DURING OUR OWN DUMPS, deliberately. A dump arrives on report 04 as well, as
536 back-to-back frames, so a listener left running across one would swallow the model and report it
as a flood of pushes. Order is: snapshot, start listening, send, capture, stop listening, snapshot.

PUSH VERSUS DUMP FRAME, per RODECASTER_PROTOCOL 2.5.1: a push is `04 <len32> <one record of exactly
len32 bytes> <zeros>`, a dump frame is `04` plus 255 raw bytes of tree. Nothing here has to tell them
apart, because of the ordering above, but the length is printed so a surprise is visible.

Run: python DevTools/probe_show_push.py [--export | --reset] [--seconds N]

`--reset` is a REAL UNBACKED FACTORY RESET. Expendable desks only.
"""
from __future__ import annotations
import re
import sys
import threading
import time

import _bootstrap  # noqa: F401
import hid
import model_tree
import rcp_hid
import rcp_read
import shows

# Property names are printable ASCII runs; four characters is enough to skip value bytes that
# happen to be printable. Same shape as the announcer's own name scan.
NAME = re.compile(rb"[A-Za-z][A-Za-z0-9_]{3,}")

# Live state that moves on its own, so a change in one is not evidence of anything we did. From
# test_duo_caps_live's DERIVED list, which was earned by chasing them.
DERIVED = {"encoderColour", "padProgress", "padActive", "playerProgress",
           "playerCurrentPositionTime", "recordState", "recordTimeMs"}


def snapshot() -> tuple[bytes, dict]:
    blob = rcp_read.read_model_stable(fresh=True)
    root, _u, _t = model_tree.parse(blob)
    out = {}
    for n in model_tree.walk(root):
        p = tuple(n.path())
        for name, val, _k in n.props:
            out[(p, n.name, name)] = val
    return blob, out


class Listener:
    """Collects every report-04 frame the desk sends us, on its own handle and its own thread."""

    def __init__(self):
        self.frames: list[tuple[float, bytes]] = []      # (arrival time, frame)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        iface = rcp_hid.find_control_interface()
        if iface is None:
            raise RuntimeError("no control interface (is the desk connected?)")
        dev = hid.device()
        dev.open_path(iface.path)
        # The handle is opened AND closed on this thread; closing it from elsewhere while a read is
        # in flight segfaults the interpreter (measured).
        self._thread = threading.Thread(target=self._run, args=(dev,), daemon=True)
        self._thread.start()

    def _run(self, dev) -> None:
        try:
            while not self._stop.is_set():
                try:
                    rep = dev.read(256, timeout_ms=200)
                except Exception:
                    return
                if rep and rep[0] == 0x04:
                    self.frames.append((time.time(), bytes(rep)))
        finally:
            try:
                dev.close()
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)


def describe(frame: bytes) -> str:
    declared = int.from_bytes(frame[1:5], "little")
    names = [m.group().decode() for m in NAME.finditer(frame[5:])]
    seen = []
    for x in names:                      # keep order, drop repeats
        if x not in seen:
            seen.append(x)
    return f"len={declared:<5} {', '.join(seen[:6]) or '(no readable name)'}"


def main() -> int:
    do_reset = "--reset" in sys.argv
    if not do_reset and "--export" not in sys.argv:
        print(__doc__)
        print("Choose --export or --reset.")
        return 2
    seconds = 10.0
    if "--seconds" in sys.argv:
        seconds = float(sys.argv[sys.argv.index("--seconds") + 1])

    if do_reset:
        _bootstrap.require_expendable_desk(
            "FACTORY RESETS the desk (showControlNewFromDefault), with no backup and no undo")
    what = "showControlNewFromDefault (FACTORY RESET)" if do_reset else "showControlExport"
    print(f"Probing {what}, listening {seconds:.0f}s.\n")

    print("snapshot before...")
    _b, before = snapshot()
    err_before = shows.read_last_error(_b)
    uuid_before = shows.read_current(_b).get("uuid")
    print(f"  {len(before)} properties, lastError={err_before}, uuid={uuid_before}")

    listener = Listener()
    listener.start()
    time.sleep(1.0)
    quiet_before = len(listener.frames)
    if quiet_before:
        print(f"  NOTE: {quiet_before} frame(s) arrived before we sent anything - the desk was "
              f"already talking, so subtract these.")

    print(f"\nsending {what}...")
    if do_reset:
        shows.send_new_from_default()
    else:
        shows.send_export()
    sent_at = time.time()

    time.sleep(seconds)
    listener.stop()
    pushed = listener.frames[quiet_before:]
    print(f"\n{len(pushed)} frame(s) pushed in {time.time() - sent_at:.0f}s")
    if not pushed:
        print("  NOTHING. So this command cannot be detected by listening, and a read is the only "
              "evidence available.")

    # WHEN THE FLOOD STARTS AND STOPS IS THE POINT, not what is in it. A reset pushes thousands of
    # frames, so no single one of them is a completion signal; the SILENCE AFTER THEM is. An export
    # pushes three, and those can be keyed on directly. Both numbers are what a listen-instead-of-
    # poll design has to be built from, so both are printed rather than described.
    if pushed:
        first = pushed[0][0] - sent_at
        last = pushed[-1][0] - sent_at
        print(f"  first at t+{first:.2f}s, last at t+{last:.2f}s")
        gaps = [(pushed[i][0] - pushed[i - 1][0], pushed[i - 1][0] - sent_at)
                for i in range(1, len(pushed))]
        big = sorted((g for g in gaps if g[0] >= 0.5), reverse=True)[:5]
        if big:
            print("  longest silences inside it: "
                  + ", ".join(f"{g:.2f}s after t+{at:.1f}s" for g, at in big))
        else:
            print("  no gap of 0.5s or more: it arrived as one continuous burst")

        counts: dict[str, int] = {}
        for _t, f in pushed:
            for m in NAME.finditer(f[5:]):
                n = m.group().decode()
                counts[n] = counts.get(n, 0) + 1
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:12]
        print(f"  {len(counts)} distinct property name(s). Most frequent: "
              + ", ".join(f"{n} x{c}" for n, c in top))
        show = sorted(n for n in counts if n.startswith("showControl") or "uuid" in n.lower()
                      or "howName" in n)
        print(f"  show-related names pushed: {', '.join(show) if show else 'NONE'}")
        for i, (t, f) in enumerate(pushed[:8], 1):
            print(f"  {i:>3}. t+{t - sent_at:>6.2f}s {describe(f)}")

    print("\nsnapshot after...")
    _a, after = snapshot()
    err_after = shows.read_last_error(_a)
    uuid_after = shows.read_current(_a).get("uuid")
    print(f"  {len(after)} properties, lastError={err_after}, uuid={uuid_after}")

    added = [k for k in after if k not in before]
    removed = [k for k in before if k not in after]
    changed = [k for k in before if k in after and before[k] != after[k]]
    real = [k for k in changed if k[2] not in DERIVED]
    derived = [k for k in changed if k[2] in DERIVED]

    print(f"\ntree: {len(added)} added, {len(removed)} removed, {len(changed)} changed "
          f"({len(derived)} of them derived live state)")
    for k in (added + removed + real)[:40]:
        was = before.get(k, "-")
        now = after.get(k, "-")
        # Path elements are node INDEXES (ints), not names, so they need stringifying before a
        # join. The first run of this crashed here after printing everything else.
        where = "/".join(str(x) for x in k[0]) or "(root)"
        print(f"  {where} :: {k[1]} :: {k[2]}: {was!r} -> {now!r}")
    if len(added + removed + real) > 40:
        print(f"  ... and {len(added + removed + real) - 40} more")

    print()
    # A PUSH IS EVIDENCE IN ITS OWN RIGHT, so the verdict must not rest on the tree diff alone. On
    # an impossible export the push is the ONLY evidence there is: the tree does not move and
    # lastError is already latched.
    if do_reset:
        print("VERDICT: reset APPLIED." if uuid_after != uuid_before
              else "VERDICT: reset did NOT apply - the uuid is unchanged.")
    elif pushed:
        names = {m.group().decode() for _t, f in pushed for m in NAME.finditer(f[5:])}
        show_names = sorted(n for n in names if n.startswith("showControl"))
        print(f"VERDICT: the export REACHED THE ENGINE - it pushed {len(pushed)} frame(s)"
              + (f" naming {', '.join(show_names)}" if show_names else "") + ".")
        if not (real or added or removed) and err_after == err_before:
            print("         And the PUSH IS THE ONLY EVIDENCE: the tree did not move and lastError "
                  "was already latched, so no amount of reading could have seen this.")
    elif real or added or removed or err_after != err_before:
        print("VERDICT: the export REACHED THE ENGINE - something moved in the tree.")
    else:
        print("VERDICT: NO EVIDENCE the export reached the engine - nothing pushed, nothing moved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
