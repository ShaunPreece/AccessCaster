"""Offline test of the off-fader CHAIN STORE and `server._follow_chains`. NO DEVICE, no HID writes.

WHY THIS EXISTS. Processing and effects live on a fader STRIP, never on a channel, so a channel with
no fader has no processing anywhere on the desk. Until "the chain follows the
channel" only handled channels present in BOTH layouts, which left two silent ways to lose a voicing:

  * a channel LEAVES the layout, and the next occupant of its strip writes over the chain;
  * a channel RETURNS to the layout, and inherits whatever the previous occupant left there.

Both were measured, not theorised: a fader move brought Mic 2 back from off-fader onto the strip USB
1 had just vacated, and Mic 2 came back with USB 1's processing - gate off, EQ flat, bypass on.
Stereo link makes it the ordinary path rather than an edge case, because linking a pair evicts the
upper mic and only the unlink hands it back.

WHAT IT PINS:
  * park/take is a POP, so a chain cannot be applied twice and later overwrite a real edit;
  * the store is keyed by DESK, so two units do not hand each other voicings;
  * parking happens BEFORE any chain write, because the pre-write snapshot is the only copy;
  * a returning channel with nothing parked is REPORTED, not silently left on a stranger's voicing;
  * a return that fails to land is parked AGAIN rather than dropped.

Run: python DevTools/test_fader_chains.py
"""
from __future__ import annotations

import _bootstrap  # noqa: F401  (puts Project/ on the path; import FIRST)

import channels
import server

FAILED = []


def check(label, got, want=True, show=None):
    ok = (got == want)
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  [{show if show is not None else got}]"
                                                     if not ok or show is not None else ""))
    if not ok:
        FAILED.append(label)


def check_in(label, needle, hay):
    ok = needle.lower() in (hay or "").lower()
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f"  [{hay}]"))
    if not ok:
        FAILED.append(label)


def chain(tag):
    """A chain dict distinguishable by tag, shaped like channels.read_chains returns."""
    return {"bools": {"noiseGateOn": bool(tag % 2)}, "floats": {"channelDepth": tag / 10.0},
            "fx_bools": {}, "fx_floats": {}, "marker": tag}


# --- the store, on its own -----------------------------------------------------------------
# Redirected to a scratch file so a test run never touches the user's real parked chains.
import tempfile, os                                                        # noqa: E402
_scratch = os.path.join(tempfile.mkdtemp(prefix="ac_chaintest_"), "off_fader_chains.json")
channels._paths.data_path = lambda *parts: _scratch                        # noqa: SLF001

print("the off-fader chain store")
check("nothing parked reads back as None", channels.take_chain("DESK-A", 1), None)
channels.park_chain("DESK-A", 1, chain(7))
check("a parked chain comes back intact", channels.take_chain("DESK-A", 1), chain(7))
check("and taking it POPS it, so it cannot be applied twice",
      channels.take_chain("DESK-A", 1), None)
channels.park_chain("DESK-A", 2, chain(3))
check("another desk does not see it", channels.take_chain("DESK-B", 2), None)
check("the owning desk still does", channels.take_chain("DESK-A", 2), chain(3))
channels.park_chain("DESK-A", 3, {})
check("an empty chain is not parked (nothing to give back)",
      channels.take_chain("DESK-A", 3), None)
check("the store survives being re-read from disk", (channels.park_chain("DESK-A", 4, chain(9)) or
      channels._orphan_load().get("DESK-A", {}).get("4")), chain(9))
channels.take_chain("DESK-A", 4)


# --- _follow_chains, with the device mocked out --------------------------------------------
WRITES = []          # (strip, chain) for every write_chain call, in order
STATE = {}           # strip -> chain, as the desk would end up


class FakeSpec:
    count = 9


def fake_write_chain(strip_idx, desired, current=None, delay=0.02):
    WRITES.append((strip_idx, desired))
    STATE[strip_idx] = desired
    return {"written": len(desired.get("floats", {})) + len(desired.get("bools", {})),
            "props": []}


def install(before):
    """Point server at the fakes and seed the desk state from a pre-write snapshot."""
    WRITES.clear()
    STATE.clear()
    STATE.update({i: c for i, c in enumerate(before)})
    channels.write_chain = fake_write_chain
    channels.read_chains = lambda blob=None, strips=None: [STATE.get(i) for i in range(len(before))]
    channels.chain_diff = lambda desired, current: [] if desired == current else ["differs"]
    server.rcp_read.read_model = lambda *a, **k: b""
    server._active_faders = lambda: FakeSpec()
    server._channel_name = lambda code, spec=None: f"Ch{code}"
    server._device_hardware_id = lambda: "DESK-TEST"


print()
print("_follow_chains: a channel that MOVES strip (the case that already worked)")
before = [chain(0), chain(1), chain(2)]
install(before)
note = server._follow_chains([0, 1, 2], [1, 0, 2], before)
check("both chains travel with their channels", sorted(WRITES)[:2],
      sorted([(0, chain(1)), (1, chain(0))])[:2])
check_in("and the note says what followed", "followed", note)

print()
print("_follow_chains: a channel that LEAVES the layout")
before = [chain(0), chain(1), chain(2)]
install(before)
note = server._follow_chains([0, 1, 2], [0, 7, 2], before)
check("the departing channel's chain is parked", channels._orphan_load()
      .get("DESK-TEST", {}).get("1"), chain(1))
check_in("and the user is told it is kept, not lost", "comes back", note)
check_in("the note is about PROCESSING, not the layout (reported separately)",
         "the processing for", note)

print()
print("_follow_chains: a channel that RETURNS from off-fader")
before = [chain(0), chain(5), chain(2)]         # strip 1 holds a STRANGER's voicing
install(before)
note = server._follow_chains([0, 7, 2], [0, 1, 2], before)
check("its own parked chain is written back onto the strip it landed on",
      STATE[1], chain(1))
check("and it is NOT left on the previous occupant's voicing", STATE[1] == chain(5), False)
check("the parked copy is consumed", channels.take_chain("DESK-TEST", 1), None)
check_in("the note says the channel got its processing back", "got its own processing back", note)

print()
print("_follow_chains: a channel returns with NOTHING parked")
before = [chain(0), chain(5), chain(2)]
install(before)
note = server._follow_chains([0, 7, 2], [0, 4, 2], before)
check("nothing is written for it (we have no chain to write)",
      [w for w in WRITES if w[0] == 1], [])
check_in("but the user is TOLD it is on whatever was there", "no stored processing", note)

print()
print("_follow_chains: a return that does not land is parked again")
before = [chain(0), chain(5), chain(2)]
install(before)
channels.park_chain("DESK-TEST", 1, chain(1))
channels.write_chain = lambda strip_idx, desired, current=None, delay=0.02: (
    WRITES.append((strip_idx, desired)) or {"written": 0, "props": []})   # write silently does nothing
note = server._follow_chains([0, 7, 2], [0, 1, 2], before)
check_in("the failure is reported, not swallowed", "warning", note)
check("and the chain is parked again rather than dropped",
      channels.take_chain("DESK-TEST", 1), chain(1))

print()
if FAILED:
    print(f"{len(FAILED)} CHECK(S) FAILED:")
    for f in FAILED:
        print("  -", f)
    raise SystemExit(1)
print("ALL FADER CHAIN CHECKS PASSED")
