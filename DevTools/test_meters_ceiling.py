"""Offline: the two meter bugs found on must not come back. No device, no microphone.

WHY THESE ARE WORTH PINNING. Both bugs were invisible to code review and to every existing test:

  1. THE CAPTURE ENGINE WAKER. `meters.IAudioClient` was declared, the module docstring claimed an
     `activate()` opened it, and nothing in the repo ever called it. So an input's meter read 0.0 no
     matter how loud the microphone was, and nobody noticed because the feature had only ever been
     exercised over Remote Desktop, where the desk's endpoints do not exist at all.

  2. THE ENDPOINT CEILING. Windows' own capture level scales the meter. On Shaun's machine that level
     is -1.08 dB, so a signal at TRUE full scale reads 0.883 - and the clip threshold of 0.99 was
     therefore UNREACHABLE. The clipping warning, the one thing a recording cannot recover from,
     could never be spoken. Measured twice at 8 dB apart, identical 88.3 percent both times.

Neither is testable against hardware without shouting into a microphone with the processing bypassed,
so the ARITHMETIC and the WORDING are pinned here instead, and the hardware runs are recorded in
Measured.

Usage: python DevTools/test_meters_ceiling.py
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import meters

fails = []


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        fails.append(label)


def close(label, got, want, tol=1e-3):
    ok = abs(got - want) <= tol
    print(f"  {'ok  ' if ok else 'FAIL'} {label}  ({got:.4f})")
    if not ok:
        print(f"        wanted {want:.4f} +/- {tol}")
        fails.append(label)


def ceiling_for(db: float) -> float:
    """The same expression Meter.refresh_ceiling uses, kept here so a change to one is visible."""
    return min(1.0, 10.0 ** (db / 20.0))


print("THE CEILING ARITHMETIC")
close("0 dB endpoint can reach full scale", ceiling_for(0.0), 1.0)
close("-1.08 dB (Shaun's measured capture level) reaches 0.883", ceiling_for(-1.08), 0.8832)
close("-6 dB reaches about half", ceiling_for(-6.0), 0.5012)
close("a BOOST is capped at 1.0, never above", ceiling_for(30.0), 1.0)
close("a big attenuation still gives a usable floor", ceiling_for(-20.0), 0.1)

print("\nCLIPPING IS REACHABLE ON AN ATTENUATED ENDPOINT (bug 2)")
NAME = "Microphone (RODECaster Pro II Main Stereo)"
hot = 0.883                      # what true full scale reads at -1.08 dB
check("full scale on a -1.08 dB endpoint IS reported as clipping",
      "CLIPPING" in meters.describe(hot, 1.0, NAME, ceiling=ceiling_for(-1.08)), True)
check("the same peak on a unity endpoint is NOT clipping (it is genuinely below full scale)",
      "CLIPPING" in meters.describe(hot, 1.0, NAME, ceiling=1.0), False)
check("a unity endpoint still calls real full scale clipping",
      "CLIPPING" in meters.describe(1.0, 1.0, NAME, ceiling=1.0), True)
check("a quiet signal is never called clipping, whatever the ceiling",
      "CLIPPING" in meters.describe(0.2, 1.0, NAME, ceiling=ceiling_for(-1.08)), False)
# The guard that stops a pathological endpoint reading making everything 'clipping'.
check("an absurd ceiling cannot make a whisper clip",
      "CLIPPING" in meters.describe(0.05, 1.0, NAME, ceiling=0.0), False)

print("\nTHE WINDOWS RECORDING LEVEL IS EXPLAINED, AND BLAMED CORRECTLY")
# Measured on this endpoint: 50% = -1.08 dB, about 55% = unity, 75% = +7.67 dB, 100% = +30 dB. So
# the common habit of setting a new device to 75% puts nearly 8 dB of BOOST on a desk that already
# delivers a levelled stream, and the resulting clipping happens in Windows, not in the desk.
check("a near-unity level says nothing at all", meters.level_note(-1.08), "")
check("unity says nothing at all", meters.level_note(0.0), "")
boost = meters.level_note(7.67)
check("a boosted input is called out", "adding 7.7 dB" in boost, True)
# The advice names the app's own dB control, not a percentage: its button lands exactly on unity,
# which dragging a percentage slider cannot.
check("and it names the fix, which is the Windows level and not the desk's gain",
      "Set it to no change" in boost, True)
check("and it still sends you to the unit for the gain itself",
      "on the unit itself" in boost, True)
check("a heavily attenuated input is called out too",
      "taking 12.0 dB off" in meters.level_note(-12.0), True)
clip_boosted = meters.describe(1.0, 1.0, NAME, ceiling=1.0, volume_db=7.67)
check("clipping on a BOOSTED input blames Windows, not the desk",
      "Windows is adding" in clip_boosted, True)
check("and it does NOT just say turn it down, which would be the wrong knob",
      clip_boosted.endswith("Turn it down."), False)
check("clipping at unity still says turn it down",
      meters.describe(1.0, 1.0, NAME, ceiling=1.0, volume_db=0.0).endswith("Turn it down."), True)

print("\nSILENCE IS NOT CONFUSED WITH A STREAM WE COULD NOT START (bug 1)")
silent = meters.describe(0.0, 0.0, NAME)
unwoken = meters.describe(0.0, 0.0, NAME, wake_failed=True)
check("a real silence still says silent", "silent" in silent, True)
check("an unwoken input does NOT say silent", "silent" in unwoken, False)
check("an unwoken input explains that nothing is recording from it",
      "recording from it" in unwoken, True)
check("the two sentences are different", silent == unwoken, False)

print("\nTHE WAKER IS ACTUALLY WIRED UP (the whole of bug 1)")
src = Path(meters.__file__).read_text(encoding="utf-8")
check("_EngineWaker exists", hasattr(meters, "_EngineWaker"), True)
check("Meter constructs it, rather than only declaring the class",
      "_EngineWaker(dev)" in src, True)
check("it is only started for INPUTS", "if self.is_capture:" in src, True)
check("shared mode, never exclusive (exclusive would steal the mic from every other app)",
      "AUDCLNT_SHAREMODE_SHARED" in src and meters.AUDCLNT_SHAREMODE_SHARED == 0, True)
check("the module no longer claims a non-existent activate() opens it",
      "which is why `activate()` opens" in src, False)
check("Watcher follows the endpoint volume while running",
      "refresh_ceiling()" in src and "CEILING_REFRESH_S" in src, True)
# A SETTER EXISTS, AND THE RULE BEHIND IT IS UNCHANGED: the app must not move Windows' level behind
# the user's back, because that changes what EVERY application on the machine records. A control the
# USER operates is a different thing from the app deciding, so the setter's only caller is that
# control. What is pinned here is the surrounding safety: the read-back (drivers refuse silently),
# the narrowed range (-96 dB is off, not quiet), and that nothing in the METERING path - Meter,
# Watcher, sample, describe - ever writes.
check("a setter exists, for the control the user operates",
      hasattr(meters, "set_endpoint_volume_db"), True)
check("it READS BACK rather than trusting the write (a driver can refuse silently)",
      "GetMasterVolumeLevel()" in src.split("def set_endpoint_volume_db")[1][:1200], True)
check("the offered range is narrowed: -96 dB is OFF, not quiet",
      meters.OFFERED_LEVEL_MIN_DB >= -30.0, True)
metering_only = src.split("def endpoint_volume")[0]
check("NOTHING in the metering path itself writes a level",
      "SetMasterVolumeLevel(" in metering_only.replace(
          'COMMETHOD([], HRESULT, "SetMasterVolumeLevel"', ""), False)

print("\n" + ("ALL CHECKS PASSED" if not fails else f"{len(fails)} FAILURE(S): {fails}"))
raise SystemExit(1 if fails else 0)
