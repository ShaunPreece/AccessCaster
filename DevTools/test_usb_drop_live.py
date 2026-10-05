"""WHICH LAYER takes the desk off the USB bus during a show factory reset?

THE FAULT. A show command has dropped a desk off USB three times, on a Pro II and a Duo.
Windows ends up with ZERO devices carrying RODE's vendor id, so the
desk is off the bus rather than a handle being lost, and only a PHYSICAL REPLUG brings it back. All
three were through the running app.

THE METHOD: ONE VARIABLE AT A TIME, EACH SELECTABLE ON ITS OWN. `--layers` takes any combination, so
each factor can be run alone before anything is stacked. The first combination that drops the desk
names the cause; a factor that never drops it alone is not thereby innocent, which is why they also
stack.

  (none)     THE COMMAND ALONE. One open, one write, one close, then total silence.
             SETTLED: 11 trials, 11 clean, every one confirmed applied. So the
             firmware's own re-enumeration is NOT sufficient, and the cause is somewhere above.
  handles1   one persistent read handle open across the write.
  handles2   two, which is what the app really holds: recorder.Watcher keeps one and the announcer
             keeps another, on separate handles on purpose, and NEITHER takes the HID lock. So at the
             moment the firmware tears its USB function down, the app has two blocking reads
             outstanding on interfaces that are disappearing.
  poll       a full model dump every POLL_S seconds right through the desk's rebuild window, which is
             what shows.wait_for does while it waits for the new uuid.
  export     an export command fired immediately before the reset. The default reset path is TWO show
             commands back to back, not one, and ordering is a suspect in its own right. On a desk
             with no drive the export cannot succeed, but the COMMAND still goes out, which is the
             part under test.
  app        THE WHOLE RUNNING SERVER, end to end over HTTP. THE CONTROL, and it must be run: if
             every layer above comes up clean and this still drops, a layer is missing and the list
             is wrong. Exclusive - it ignores the other layers, because the app brings its own.

PRESENCE IS CHECKED WITHOUT OPENING ANYTHING. `hid.enumerate` briefly opens each device on Windows to
read its strings, which would put HID traffic inside the very window this test depends on being
quiet, so presence comes from PowerShell's `Get-PnpDevice` - pure SetupAPI, no handle. It is also a
SEPARATE PROCESS, which is what makes it valid for the `app` layer, where the thing under test is the
process we would otherwise be asking.

EVERY TRIAL PROVES THE RESET ACTUALLY RAN, by reading the loaded show's uuid afterwards.
`newFromDefault` mints a NEW uuid, so an unchanged one means the command did nothing: that trial is
VOID, not clean. Without this, a run of "no drop" could be a run of "no reset", which would read as
evidence of safety while proving nothing. The read doubles as the next trial's baseline, so it costs
one dump per trial and none inside the quiet window.

IT STOPS DEAD ON THE FIRST DROP. Sending more resets at a desk that is off the bus proves nothing and
would queue commands to land at whatever moment somebody replugs it.

A SHORT BLIP IS NOT MEASURED and is not the fault being chased. `Get-PnpDevice` takes about a second,
so a one-to-two-second re-enumeration can fall between samples. Worth knowing, because the notes'
recorded mechanism said a full-config write re-enumerates USB BY DESIGN: across 11 bare trials
nothing was ever missing at t+3s, t+10s or t+20s.

EXPENDABLE DESKS ONLY. Every trial is an UNBACKED factory reset: settings and pads go to factory with
no undo. Right for the Duo test desk, wrong for a desk somebody works on. It also leaves mic channels
possibly silent, because `server.wake_channels` is not called except in the `app` layer, which runs
it itself.

Run: python DevTools/test_usb_drop.py [--layers handles2,poll] [--trials N] [--quiet-seconds N]
     python DevTools/test_usb_drop.py --layers app --trials 5
"""
from __future__ import annotations
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import _bootstrap  # noqa: F401
import hid
import rcp_hid
import rcp_read
import shows

QUIET_SECONDS = 20.0
POLL_S = 1.5                    # shows.POLL_S, so `poll` polls exactly as the app does
SAMPLE_AT = (3.0, 10.0, 20.0)
BASE = "http://127.0.0.1:8770"
LAYERS = ("handles1", "handles2", "poll", "export", "exportonly", "app")


def present() -> int:
    """How many VID_19F7 devices Windows has present, WITHOUT opening any of them."""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "@(Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | "
         "Where-Object { $_.InstanceId -match 'VID_19F7' }).Count"],
        capture_output=True, text=True)
    try:
        return int(out.stdout.strip())
    except ValueError:
        return -1


def api(path: str, timeout: float = 90.0, post: dict | None = None) -> dict:
    """GET, or POST with a JSON body when `post` is given.

    ACTION ROUTES TAKE THEIR ARGUMENTS IN A JSON BODY, NOT THE QUERY STRING, and `do_POST` matches
    `self.path` EXACTLY - so `?backup_first=false` does not merely get ignored, it makes the path
    miss the table and return 404.
    """
    data = json.dumps(post).encode() if post is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}


def uuid_direct() -> str | None:
    return shows.read_current(rcp_read.read_model_stable(fresh=True)).get("uuid")


def uuid_via_app() -> str | None:
    """The app's own answer, so the `app` layer never opens a competing handle of ours."""
    return (api("/api/shows").get("current") or {}).get("uuid")


class Load:
    """The layers that have to exist DURING the write, held open across it.

    These are deliberately NOT a reimplementation of recorder.Watcher or the announcer. The claim
    under test is not "our copy of their loop behaves the same", it is the far simpler "a read handle
    was open, and a dump was in flight, while the desk tore its USB function down". So this opens the
    handles and issues the dumps, and nothing else.
    """

    def __init__(self, handles: int = 0, poll: bool = False):
        self.handles = handles
        self.poll = poll
        self._devs: list = []
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self.dumps = 0
        self.read_errors = 0

    def start(self) -> None:
        iface = rcp_hid.find_control_interface()
        if iface is None:
            raise RuntimeError("no control interface (is the desk connected?)")
        for _ in range(self.handles):
            dev = hid.device()
            dev.open_path(iface.path)
            self._devs.append(dev)
            t = threading.Thread(target=self._read_loop, args=(dev,), daemon=True)
            t.start()
            self._threads.append(t)
        if self.poll:
            t = threading.Thread(target=self._poll_loop, daemon=True)
            t.start()
            self._threads.append(t)

    def _read_loop(self, dev) -> None:
        # A BLOCKING read with a short timeout, which is what both real readers use (200 ms). The
        # point is that a read is genuinely outstanding when the device goes, not that it spins.
        #
        # THE HANDLE IS CLOSED HERE, ON THE THREAD THAT READS IT, and that is not tidiness: closing
        # it from `stop()` while this read is in flight SEGFAULTS the interpreter (measured, exit
        # 139). hidapi does not defend a handle against a close from another thread. Both real
        # readers already close on their own thread
        # (recorder.py:603 and the announcer's loop), so this matches them as well as being safe.
        try:
            while not self._stop.is_set():
                try:
                    dev.read(256, timeout_ms=200)
                except Exception:
                    self.read_errors += 1
                    return
        finally:
            try:
                dev.close()
            except Exception:
                pass

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(POLL_S)
            if self._stop.is_set():
                return
            try:
                rcp_read.read_model_stable(fresh=True)
                self.dumps += 1
            except Exception:
                pass

    def stop(self) -> None:
        """Ask the threads to finish and WAIT for them, so each handle is closed by its own reader.

        The join is what makes it safe (see `_read_loop`). A read blocks at most 200 ms, so this
        costs a fraction of a second."""
        self._stop.set()
        for t in self._threads:
            t.join(timeout=3.0)
        self._threads = []
        self._devs = []

    def describe(self) -> str:
        bits = []
        if self.handles:
            bits.append(f"{self.handles} read handle(s) open")
        if self.poll:
            bits.append(f"dump every {POLL_S}s")
        return ", ".join(bits) or "nothing held open"


def fire_in_process(layers: set) -> str:
    if "exportonly" in layers:
        # THE CLOSEST RECONSTRUCTION OF A REAL DROPOUT, and no reset follows it. With no drive in
        # the desk, the reset dialog's backup fires this, polls for up to 40 s, and then
        # action_show_reset REFUSES - so the reset never runs and this is all that happened.
        shows.send_export()
        return "showControlExport ALONE, no reset (impossible: nowhere to write)"
    if "export" in layers:
        shows.send_export()
        time.sleep(1.0)
        shows.send_new_from_default()
        return "showControlExport then showControlNewFromDefault"
    shows.send_new_from_default()
    return "showControlNewFromDefault (in process)"


def fire_via_app() -> str:
    """Drive the real server. backup_first=false because a Duo has nowhere to save one, and a
    refused backup would stop the reset before it ever ran (action_show_reset refuses rather than
    reset unbacked, on purpose)."""
    started = api("/api/shows/reset", timeout=20.0, post={"backup_first": False})
    if not started.get("ok", True) and "already running" in str(started.get("message", "")):
        return f"app REFUSED: {started.get('message')}"
    return f"app: {started.get('message') or started}"


def wait_for_app_job(limit: float = 90.0) -> str:
    end = time.time() + limit
    last = ""
    while time.time() < end:
        st = api("/api/shows/status", timeout=15.0)
        if not st.get("running", False):
            return st.get("message") or last or "job finished"
        last = st.get("message") or last
        time.sleep(1.0)
    return f"job still running after {limit:.0f}s (last: {last})"


def trial(n: int, baseline: str | None, layers: set, quiet: float) -> tuple[str, str | None]:
    """One reset under the chosen layers. Returns (status, uuid) with status
    clean / void / dropped / abort."""
    use_app = "app" in layers
    before = present()
    print(f"\n--- trial {n} --- present={before} baseline={baseline}")
    if before <= 0:
        print("  ABORT: the desk is not on the bus before we even start.")
        return "abort", baseline

    load = None
    if not use_app:
        load = Load(handles=2 if "handles2" in layers else (1 if "handles1" in layers else 0),
                    poll="poll" in layers)
        load.start()
        if load.handles or load.poll:
            time.sleep(POLL_S + 0.5)      # let the handles settle and a dump get in flight
        print(f"  holding: {load.describe()}")

    sent_at = time.time()
    what = fire_via_app() if use_app else fire_in_process(layers)
    print(f"  {time.strftime('%H:%M:%S')} {what}")

    dropped_at = None
    for at in SAMPLE_AT:
        if at > quiet:
            break
        while time.time() - sent_at < at:
            time.sleep(0.2)
        now = present()
        print(f"  t+{at:>4.0f}s present={now}")
        if now <= 0 and dropped_at is None:
            dropped_at = at

    if load is not None:
        if load.poll:
            print(f"  {load.dumps} dumps landed across the window")
        if load.read_errors:
            print(f"  {load.read_errors} read handle(s) errored out during it")
        load.stop()
    if use_app:
        print(f"  app job: {wait_for_app_job()}")

    if dropped_at is not None:
        time.sleep(10.0)                  # a merely slow re-enumeration is not the fault
        if present() <= 0:
            print(f"  *** DROPPED at t+{dropped_at:.0f}s, still gone 10s later.")
            print("  *** THE DESK NEEDS A PHYSICAL REPLUG. Stopping.")
            return "dropped", baseline
        print("  came back on its own after a longer wait - NOT the permanent fault.")

    if "exportonly" in layers:
        # NO NEW UUID TO CHECK, because no reset happens. The honest evidence is weaker here and
        # says so out loud: `showControlLastError` goes 0 -> 8 on a refused export, but it LATCHES,
        # so once it is 8 a later export cannot be distinguished from no export at all. A dump that
        # still answers at least proves the desk is alive and listening.
        blob = rcp_read.read_model_stable(fresh=True)
        err = shows.read_last_error(blob)
        alive = shows.read_current(blob).get("uuid")
        if not alive:
            print("  VOID: the desk did not answer a read afterwards.")
            return "void", baseline
        note = "latched already, so this trial cannot prove the export reached the engine" \
            if baseline == str(err) else f"moved to {err}, so the export DID reach the engine"
        print(f"  clean: on the bus and answering. lastError={err} ({note})")
        return "clean", str(err)

    after = uuid_via_app() if use_app else uuid_direct()
    if after and after != baseline:
        print(f"  clean: on the bus, and the reset APPLIED (uuid -> {after})")
        return "clean", after
    print(f"  VOID: no drop, but the uuid did not change ({after}), so the command did not run "
          f"and this trial proves nothing.")
    return "void", after or baseline


def main() -> int:
    # THE GUARD, not the docstring, is what stops this reaching a desk somebody works on. See
    # _bootstrap.require_expendable_desk for why it is a flag: this file already said "expendable
    # desks only" in capitals when it was run over Shaun's Pro II by a sweep.
    _bootstrap.require_expendable_desk(
        "FACTORY RESETS the desk, once per trial, with no backup and no undo")
    layers: set = set()
    if "--layers" in sys.argv:
        raw = sys.argv[sys.argv.index("--layers") + 1]
        layers = {x.strip() for x in raw.split(",") if x.strip()}
        unknown = layers - set(LAYERS)
        if unknown:
            print(f"Unknown layer(s): {', '.join(sorted(unknown))}")
            print(f"Known: {', '.join(LAYERS)}")
            return 2
    if "app" in layers and layers != {"app"}:
        print("NOTE: `app` is exclusive - it brings its own handles and polling. Ignoring "
              f"{', '.join(sorted(layers - {'app'}))}.")
        layers = {"app"}

    trials = 10
    if "--trials" in sys.argv:
        trials = int(sys.argv[sys.argv.index("--trials") + 1])
    quiet = QUIET_SECONDS
    if "--quiet-seconds" in sys.argv:
        quiet = float(sys.argv[sys.argv.index("--quiet-seconds") + 1])

    name = ",".join(sorted(layers)) if layers else "none (command alone)"
    print(f"USB drop test. Layers: {name}. {trials} trials, {quiet:.0f}s window each.")
    print("EVERY TRIAL IS AN UNBACKED FACTORY RESET. Expendable desks only.")

    if "app" in layers:
        probe = api("/api/shows", timeout=15.0)
        if not probe.get("ok"):
            print(f"The server is not answering on {BASE}: {probe.get('message')}")
            print("Start it first: python DevTools/run_dev_server.py")
            return 2
        baseline = uuid_via_app()
    elif "exportonly" in layers:
        baseline = str(shows.read_last_error(rcp_read.read_model_stable(fresh=True)))
        print(f"lastError before we start: {baseline}")
    else:
        baseline = uuid_direct()
    print(f"starting uuid: {baseline}")

    clean = voids = 0
    for i in range(1, trials + 1):
        status, baseline = trial(i, baseline, layers, quiet)
        if status in ("dropped", "abort"):
            print(f"\nSTOPPED on layers [{name}]. {clean} clean and {voids} void before it.")
            return 1
        clean += status == "clean"
        voids += status == "void"

    print(f"\n=== layers [{name}]: {clean} clean, {voids} void, no permanent drop in {trials}. ===")
    if voids:
        print("A VOID TRIAL IS NOT EVIDENCE: the command never ran, so it neither drops nor clears "
              "the desk.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
