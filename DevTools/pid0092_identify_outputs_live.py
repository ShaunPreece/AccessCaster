"""LIVE: find out which output is which on the PID 0x0092 desk, ONE CROSS-POINT AT A TIME.

TOUCHES THE DESK AND WRITES TO IT, which is why the name ends in _live. Run it only with the desk's
owner listening. What it does, and nothing more:

  1. one model dump, so THIS desk's layout is known (and the product id must be 0x0092);
  2. for ONE source (default Mic 2, source code 1) and ONE output index (0..12), it sets that single
     cross-point's mixDisabled to True (the source taken out of that output), holds it until you
     press Enter or --hold seconds pass, then writes back EXACTLY the value it had before;
  3. every write is confirmed by a fresh read (layout_routing; the desk does not push back a change
     the host made), and at the end the whole cell and the output's mode are compared with what
     they were, so you are told plainly if anything is not as it was.

THE OUTPUT MUST BE ON CUSTOM FOR AN EXCLUSION TO CHANGE THE SOUND (measured on a Duo: in Main Mix or
Mix Minus a cross-point write stores and reads back and changes nothing). So when the output is not
on Custom, you choose:
  --custom   switch that output to Custom for the duration (confirmed), and back afterwards. Its
             stored cross-points are what it then plays: on this unit every row reads included and
             following its fader, so it should sound like Main Mix until the one source drops out.
             A Mix Minus output (6, 8, 9 on the Pro II order) switched to Custom ALSO hears its own
             source back for the duration - a caller on Chat would hear themselves.
  --as-is    leave the mode alone and toggle the stored cell only; on a Pro II that is silent, so
             hearing a change here would itself be news.
Outputs 10..12 have their modes in a later block that is never written, so --custom refuses there.

WHAT IT REFUSES, before it reads or writes anything: source code 6 (the Mic 3 + 4 pair, a line input that
carries live audio - never used for tests), Mic 1 (source 0) unless --allow-mic1, any source or output
outside the main mix grid, and a cell that is already excluded. It never touches a second cell.

--measure also meters the two capture streams the PC gets from the desk ("Main Multitrack" and
"Chat", Windows' own peak meters through Project/meters.py) while you speak into the source, first
with it included and then excluded, and says which stream dropped. USB 1 and Chat can be identified
that way without anyone listening. Per-channel peaks of the first two Multitrack channels are shown
too: that they carry the main stereo mix is a HYPOTHESIS. If the meters cannot be opened the
measurement is skipped and the toggle still runs.

    python DevTools/pid0092_identify_outputs_live.py --output 7 --custom --measure
    python DevTools/pid0092_identify_outputs_live.py --output 3 --custom --hold 20
    python DevTools/pid0092_identify_outputs_live.py --output 8 --dry-run

--dry-run reads the desk and prints the plan without writing anything.
"""
from __future__ import annotations

import argparse
import ctypes
import sys
import time

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import channels
import devices
import layout_routing
import rcp_read
import rcp_transfer

PID = devices.PID_LAYOUT_0092
DEFAULT_SOURCE = 1                      # Mic 2
MIC1 = 0
CELL_PROPS = ("mixLevelWithAnchor", "mixLink", "mixMute", "mixDisabled")
# The capture streams to meter, by a word in Windows' name for them ("Microphone (RODECaster Pro II
# Main Multitrack)", "Microphone (2- RODECaster Pro II Chat)" on the owner's PC).
STREAMS = (("Main Multitrack (USB 1)", "multitrack"), ("Chat", "chat"))
DROP_RATIO = 0.5                       # excluded peak at or under half the included one = dropped
MIN_SIGNAL = 0.02                      # an included peak under this is no signal to judge by


class Refused(RuntimeError):
    """The helper will not do this. Nothing was written."""


def plan(dev, output: int, source: int, mode_now, cell_now: dict, custom: bool, as_is: bool,
         allow_mic1: bool = False) -> dict:
    """What the run would do, or Refused. PURE: no device, no write. The offline test pins it."""
    output, source = int(output), int(source)
    if source == devices.LAYOUT_PINNED_CODE:
        raise Refused("Source code 6 is the Mic 3 + 4 pair, a line input. It is never used "
                      "for a test.")
    if source == MIC1 and not allow_mic1:
        raise Refused("Mic 1 is not written unless you pass --allow-mic1.")
    try:
        path = layout_routing.cell_path(dev, output, source)
    except devices.WriteRefused as exc:
        raise Refused(str(exc))
    if cell_now.get("mixDisabled") is not False:
        raise Refused(f"That cross-point does not read as included (mixDisabled = "
                      f"{cell_now.get('mixDisabled')!r}), so there is nothing to toggle.")
    switch = False
    if mode_now != 2:
        if custom == as_is:
            raise Refused(f"{layout_routing.output_label(dev, output)} is on "
                          f"{layout_routing.MODE_NAMES.get(mode_now, mode_now)}, where an exclusion "
                          f"changes no sound. Pass --custom (switch it to Custom for the duration) "
                          f"or --as-is (toggle the stored cell only).")
        if custom:
            try:
                layout_routing.mode_path(dev, output)
            except devices.WriteRefused as exc:
                raise Refused(str(exc))
            switch = True
    return {"output": output, "source": source, "path": path, "switch_mode": switch,
            "mode_before": mode_now, "disabled_before": cell_now.get("mixDisabled"),
            "mute_before": cell_now.get("mixMute")}


# ---- metering (optional; read only, Windows' own meters) ---------------------------------------

class Streams:
    """Peak meters on the desk's capture streams. Created and read on ONE thread (COM)."""

    def __init__(self, out=print):
        import meters
        self.meters = []
        for e in meters.list_endpoints(meters.STATE_ACTIVE):
            if e["flow"] != meters.FLOW_NAMES[meters.CAPTURE] or not meters.is_desk_endpoint(e["name"]):
                continue
            low = e["name"].lower()
            label = next((lab for lab, word in STREAMS if word in low), None)
            if label is None:
                continue
            try:
                self.meters.append((label, e["name"], meters.Meter(e["id"])))
            except Exception as exc:
                out(f"  could not open a meter on {e['name']}: {exc}")
        for label, name, m in self.meters:
            out(f"  metering {label}: {name}" + ("  (WAKE FAILED: may read 0)" if m.wake_failed else ""))

    def sample(self) -> dict:
        """{label: (endpoint peak, peak of its first two channels or None)}."""
        out = {}
        for label, _name, m in self.meters:
            first2 = None
            try:
                n = int(m._meter.GetMeteringChannelCount())
                if n >= 2:
                    arr = (ctypes.c_float * n)()
                    m._meter.GetChannelsPeakValues(n, arr)
                    first2 = max(arr[0], arr[1])
            except Exception:
                first2 = None
            out[label] = (m.peak(), first2)
        return out

    def close(self):
        for _l, _n, m in self.meters:
            m.close()


class Window:
    """The loudest reading per stream over one listening window."""

    def __init__(self):
        self.peak, self.first2 = {}, {}

    def add(self, s: dict):
        for label, (p, f2) in s.items():
            self.peak[label] = max(self.peak.get(label, 0.0), p)
            if f2 is not None:
                self.first2[label] = max(self.first2.get(label, 0.0), f2)


def verdict(included: Window, excluded: Window) -> list:
    """One line per stream: did it drop when the source left the output?"""
    lines = []
    for label in sorted(included.peak):
        a, b = included.peak.get(label, 0.0), excluded.peak.get(label, 0.0)
        line = f"  {label}: included {a:.3f}, excluded {b:.3f}"
        if a < MIN_SIGNAL:
            line += "  -> no signal while included, nothing to judge"
        elif b <= a * DROP_RATIO:
            line += "  -> DROPPED: this output likely feeds this stream"
        else:
            line += "  -> no drop"
        if label in included.first2:
            line += (f"  [first two channels: {included.first2[label]:.3f} -> "
                     f"{excluded.first2.get(label, 0.0):.3f}]")
        lines.append(line)
    return lines


def _hold(seconds: float, tick) -> None:
    """Wait for Enter or `seconds`, calling tick() about 20 times a second (the meter samples)."""
    try:
        import msvcrt
    except ImportError:
        msvcrt = None
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        tick()
        if msvcrt is not None and msvcrt.kbhit() and msvcrt.getwch() in ("\r", "\n"):
            return
        time.sleep(0.05)


# ---- the run ----------------------------------------------------------------------------------

def _cell(blob, dev, output, source) -> dict:
    c = layout_routing.read_cell(blob, output, source, dev)
    return {p: c.get(p) for p in CELL_PROPS}


def run(output: int, source: int = DEFAULT_SOURCE, *, custom=False, as_is=False, hold=15.0,
        listen=5.0, measure=False, dry_run=False, allow_mic1=False, out=print,
        hold_fn=_hold, streams_factory=Streams) -> int:
    """The whole session. Returns 0 if the cell and mode are back exactly as they were."""
    iface = rcp_transfer.find_control_interface()
    if iface is None:
        out("No control interface (MI_09) found. Is the desk on its USB 1 port?")
        return 1
    if iface.product_id != PID:
        out(f"This is product id 0x{iface.product_id:04X}, not 0x{PID:04X}. Stopping.")
        return 1
    blob = rcp_read.read_model(fresh=True)
    dev = devices.get_active_device()
    if not dev.layout_required or "routing_write" not in dev.caps or dev.layout is None:
        out(f"{dev.name}: output-mix writes are not enabled, so nothing is done.")
        return 1
    try:
        mode_now = layout_routing.read_mode(blob, output, dev)
        cell_before = _cell(blob, dev, output, source)
        p = plan(dev, output, source, mode_now, cell_before, custom, as_is, allow_mic1)
    except (Refused, devices.WriteRefused) as exc:
        out(f"REFUSED, nothing written: {exc}")
        return 2
    o_lab = layout_routing.output_label(dev, output)
    s_lab = layout_routing.source_label(dev, source)
    out(f"{s_lab} in {o_lab}: cross-point at root index 0x{p['path'][0]:02X}")
    out(f"  before: mode {layout_routing.MODE_NAMES.get(mode_now, mode_now)}, cell {cell_before}")
    out(f"  plan: {'switch the output to Custom, ' if p['switch_mode'] else ''}"
        f"mixDisabled True, hold up to {hold:g} s, mixDisabled back to False"
        f"{', mode back' if p['switch_mode'] else ''}.")
    if dry_run:
        out("DRY RUN: nothing written.")
        return 0

    streams = None
    if measure:
        try:
            streams = streams_factory(out)
            if not streams.meters:
                out("  no desk capture stream to meter; measuring skipped.")
                streams = None
        except Exception as exc:
            out(f"  meters unavailable ({exc}); measuring skipped.")
            streams = None
    included, excluded = Window(), Window()

    mode_sent = cell_sent = False
    problems = []
    try:
        if p["switch_mode"]:
            mode_sent = True
            layout_routing.set_output_mode(output, 2)
            out(f"  {o_lab} is on Custom (confirmed).")
        if streams is not None:
            out(f"  INCLUDED: speak into {s_lab} for {listen:g} s ...")
            hold_fn(listen, lambda: included.add(streams.sample()))
        cell_sent = True
        layout_routing.set_cell(output, source, "mixDisabled", True)
        out(f"  EXCLUDED (confirmed): {s_lab} is out of {o_lab}. Listen; press Enter to put it "
            f"back (or wait {hold:g} s).")
        hold_fn(hold, (lambda: excluded.add(streams.sample())) if streams is not None
                else (lambda: None))
    except KeyboardInterrupt:
        out("  interrupted: restoring now.")
    except (devices.WriteRefused, channels.WriteNotConfirmed) as exc:
        problems.append(str(exc))
        out(f"  WRITE PROBLEM: {exc}")
    finally:
        # RESTORE, whatever happened above. A record that was sent may have landed even when its
        # confirm failed, so anything sent is put back.
        if cell_sent:
            try:
                layout_routing.set_cell(output, source, "mixDisabled", p["disabled_before"])
                out(f"  mixDisabled back to {p['disabled_before']} (confirmed).")
            except Exception as exc:
                problems.append(f"mixDisabled not restored: {exc}")
            try:
                now = _cell(rcp_read.read_model(fresh=True), dev, output, source)
                if now.get("mixMute") != p["mute_before"]:
                    # The desk may set mixMute with an exclusion (it does on a Duo); same cell.
                    layout_routing.set_cell(output, source, "mixMute", p["mute_before"])
                    out(f"  mixMute had moved; back to {p['mute_before']} (confirmed).")
            except Exception as exc:
                problems.append(f"mixMute not restored: {exc}")
        if mode_sent:
            try:
                layout_routing.set_output_mode(output, p["mode_before"])
                out(f"  mode back to {layout_routing.MODE_NAMES.get(p['mode_before'])} (confirmed).")
            except Exception as exc:
                problems.append(f"mode not restored: {exc}")
        if streams is not None:
            streams.close()

    final = rcp_read.read_model(fresh=True)
    cell_after = _cell(final, dev, output, source)
    mode_after = layout_routing.read_mode(final, output, dev)
    diff = {k: (cell_before[k], cell_after[k]) for k in CELL_PROPS if cell_before[k] != cell_after[k]}
    if mode_after != mode_now:
        diff["mode"] = (mode_now, mode_after)
    if streams is not None or included.peak:
        out("MEASURED:")
        for line in verdict(included, excluded):
            out(line)
    if diff or problems:
        out("NOT BACK EXACTLY AS IT WAS:")
        for k, (a, b) in diff.items():
            out(f"  {k}: was {a!r}, now {b!r}")
        for msg in problems:
            out(f"  {msg}")
        if "mixLink" in diff or "mixLevelWithAnchor" in diff:
            out("  A Pro II puts a cell back to following its fader with a link request; this "
                "helper does not send one. Use 'Reset this output to follow the faders' if needed.")
        return 1
    out("RESTORED EXACTLY: the cell and the output's mode read as they did before.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--output", type=int, required=True, help="output index 0..12")
    ap.add_argument("--source", type=int, default=DEFAULT_SOURCE,
                    help="source code (default 1 = Mic 2; 6 is always refused)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--custom", action="store_true", help="switch the output to Custom meanwhile")
    mode.add_argument("--as-is", action="store_true", help="toggle the stored cell only")
    ap.add_argument("--hold", type=float, default=15.0, help="seconds to hold the exclusion")
    ap.add_argument("--listen", type=float, default=5.0, help="seconds to meter while included")
    ap.add_argument("--measure", action="store_true", help="meter the PC's two capture streams")
    ap.add_argument("--dry-run", action="store_true", help="read and plan, write nothing")
    ap.add_argument("--allow-mic1", action="store_true", help="allow source 0 (Mic 1)")
    a = ap.parse_args(argv)
    return run(a.output, a.source, custom=a.custom, as_is=a.as_is, hold=a.hold, listen=a.listen,
               measure=a.measure, dry_run=a.dry_run, allow_mic1=a.allow_mic1)


if __name__ == "__main__":
    sys.exit(main())
