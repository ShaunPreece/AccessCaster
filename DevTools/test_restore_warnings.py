"""A restore must not leave you unable to hear, and must not do it silently.

Two halves of one problem, both pinned here. The WARNING says what a backup will silence, before
you commit to it. The LEVELS FOLLOWING THE CHANNELS stops an old backup causing it in the first
place. They are together because they answer the same failure and neither is sufficient alone: the
warning covers backups we cannot change, and the carry covers the ones it would otherwise be too
late to warn about.

--- the warning ---

WHY IT IS PINNED. Everything the app says AFTER a restore is spoken through the desk, so a restore
that silences an output silences its own explanation: lose Main Stereo and the announcement saying
so goes with it. The confirmation dialog is read while the audio still works and can still be
cancelled, which makes it the only place such a warning is any use - so if this logic goes quiet,
the failure mode is silence with no explanation, which is exactly what it prevents.

THE CASE IT IS BUILT ON is the first check below. `before routing reset` swaps Main Stereo and Chat
between faders 7 and 8, and carries no fader levels at all. A fader's level belongs to the STRIP,
not to the channel on it, so the channel landing on fader 7 inherits fader 7's level, zero in this
backup. Every reading afterwards is clean, because nothing IS wrong: the level has simply not
travelled with the channel.

Offline: the device reads are replaced, so this needs no desk and asserts the DECISIONS only.
"""
import sys

import _bootstrap  # noqa: F401  (puts Project/ on the path; import FIRST)

import devices
import rcp_read
import server

devices.force_device(devices.PRO2)      # or the answer comes from whatever is on the USB port

FAILS = []


def check(label, got, want):
    ok = got == want
    print(f"  {'ok ' if ok else 'FAIL'} {label}: {got!r}")
    if not ok:
        FAILS.append(f"{label}\n    got  {got!r}\n    want {want!r}")


def warnings_for(prof, vec, levels):
    """Run the warning against a desk in a known state."""
    server._read_fader_vector = lambda blob=None, spec=None: list(vec)
    rcp_read.values = lambda blob, name, kind=None: (list(levels) if name == "faderLevel" else [])
    return server._restore_silence_warnings(prof, b"a blob")


NORMAL = [0, 1, 2, 3, 10, 11, 7, 8, 9]          # Mic1-4, Bluetooth, pads, USB1, Chat, USB2
SWAPPED = [1, 0, 2, 3, 10, 11, 8, 7, 9]         # `before routing reset`: USB1 and Chat swap
FADER7_DOWN = [0, 0, 0, 0, 0, 0, 0, 95, 91]     # fader 7 at zero: the state that silences a channel
ALL_UP = [0, 0, 0, 0, 0, 0, 89, 89, 89]

print("the backup shape that silences a desk")
got = warnings_for({"routing": {"faders": SWAPPED}}, NORMAL, FADER7_DOWN)
check("it names the channel that goes silent", any("silent" in w for w in got), True)
check("and says which fader is at zero", any("at zero" in w for w in got), True)
check("and warns that it carries no levels", any("does not set fader levels" in w for w in got), True)
check("and says the faders are rearranged", any("rearranges" in w for w in got), True)

print("\na backup made today, carrying levels, everything up")
got = warnings_for({"routing": {"faders": NORMAL, "fader_levels": ALL_UP}}, NORMAL, FADER7_DOWN)
check("says nothing at all", got, [])

print("\na backup that genuinely holds a channel at zero")
got = warnings_for({"routing": {"faders": NORMAL, "fader_levels": [0, 0, 0, 0, 0, 0, 0, 89, 89]}},
                   NORMAL, ALL_UP)
check("still warns, because the backup's own value wins", any("silent" in w for w in got), True)

print("\na backup carrying levels that rearranges the faders")
got = warnings_for({"routing": {"faders": SWAPPED, "fader_levels": ALL_UP}}, NORMAL, FADER7_DOWN)
check("says it rearranges them", any("rearranges" in w for w in got), True)
check("but does NOT claim it leaves levels alone", any("does not set fader levels" in w for w in got),
      False)
check("and nothing is silent, since it sets every level", any("silent" in w for w in got), False)

print("\na profile with no routing at all (an ancient one)")
check("says nothing rather than crashing", warnings_for({}, NORMAL, ALL_UP), [])

print("\na backup identical to the current layout and levels")
got = warnings_for({"routing": {"faders": NORMAL, "fader_levels": ALL_UP}}, NORMAL, ALL_UP)
check("is silent", got, [])

# ---------------------------------------------------------------------------------------------
# THE OTHER HALF OF THE SAME PROBLEM: a restore that REARRANGES the faders and says nothing about
# the levels. An old profile carries the layout without `fader_levels`, so a channel lands on
# whatever level that strip was sitting at - which is how Main Stereo went silent. The levels now
# follow the channels in that case, and must NOT when the profile carries its own.

print("\nlevels follow the channels when the profile has none of its own")
CALLS = []
server._follow_levels = lambda old, new, before, spec: CALLS.append((list(old), list(new),
                                                                     list(before)))
server._level_write_gate = lambda: None
server.profiles.apply = lambda prof, **kw: {"ok": True, "applied": 0, "failed": 0, "errs": []}
server._apply_matrix = lambda routing: {"ok": True, "applied": 0, "message": ""}
server._hk_rearm_midi = lambda: None
server._hk_pads_stale = lambda: None
rcp_read.read_model = lambda *a, **k: b"blob"

_seq = [list(NORMAL), list(SWAPPED)]          # before the write, then after it
server._read_fader_vector = lambda blob=None, spec=None: _seq.pop(0) if _seq else list(SWAPPED)
rcp_read.values = lambda blob, name, kind=None: (list(FADER7_DOWN)
                                                 if name == "faderLevel" else [])


def run_restore(prof):
    CALLS.clear()
    _seq[:] = [list(NORMAL), list(SWAPPED)]
    server.profiles.load = lambda name: {"ok": True, "safe": name, "prof": prof}
    job = type("J", (), {"step": lambda self, m: None, "tick": lambda self, m: None})()
    # Reach the worker directly: start() would put it on a thread and we want the call, not a race.
    started = {}
    server._restore_job.start = lambda work, **kw: started.setdefault("res", work(job)) or {"ok": True}
    server.restore_profile("any")
    return CALLS


old = {"routing": {"faders": SWAPPED}, "pads": []}                       # no fader_levels
got = run_restore(old)
check("an old profile makes the levels follow", len(got), 1)
if got:
    check("and it is handed the layout from before the write", got[0][0], NORMAL)
    check("and the layout from after it", got[0][1], SWAPPED)

new = {"routing": {"faders": SWAPPED, "fader_levels": ALL_UP}, "pads": []}
check("a profile carrying its own levels does NOT", len(run_restore(new)), 0)

if FAILS:
    print("\nFAILURES:")
    for f in FAILS:
        print("  " + f)
    sys.exit(1)
print("\nALL RESTORE WARNING CHECKS PASSED")
