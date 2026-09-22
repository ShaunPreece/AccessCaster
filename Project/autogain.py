"""Set a microphone's gain by measuring it, for someone who cannot watch a meter.

WHY THIS EXISTS. Setting mic gain is the one studio job that is purely visual: you talk, you watch
a meter, you turn the knob. There is no accessible equivalent, and the usual workarounds are worse
than useless - a spoken running level is unusable while you are the one talking, and the desk's own
compressor and limiter hold the OUTPUT near full scale whatever the gain is, so the number you can
hear says almost nothing about the number you need to set, and somebody who works by ear gets no
use out of a peak meter at all.

HOW IT MEASURES, and each of these is the answer to a way it would otherwise be wrong:

  * **PROCESSING OFF.** The meter reads the desk's USB output, which is after the compressor and
    limiter. With processing on you are measuring the safety net, not the gain - and an overdriven
    preamp distorts BEFORE the limiter, which then tidies the level so the meter looks healthy while
    the sound is already damaged. The bypass is restored afterwards.
  * **THE CHANNEL ALONE.** The desk's Main Multitrack USB device carries every channel on its own
    pair, so the measurement is of that microphone and nothing else - no other mic, no SMART pad, no
    audio from the computer. Metering a mixed stream instead would set your gain from whatever else
    happened to be live.
  * **WINDOWS OUT OF THE WAY.** Windows' own level on the endpoint scales the reading, so it is set
    to unity first and any Windows mute cleared. Otherwise a slider nobody can see decides the gain.
  * **SILENT WHILE MEASURING.** The app says nothing at all during the take. Anything played aloud
    is picked up by the microphone being measured, which would set the gain from the app's own voice.
  * **THE 99TH PERCENTILE, NOT THE PEAK.** One cough, one desk knock or one door is not your voice,
    and gain set from it would leave you 10 dB too quiet for the whole session. The loudest single
    sample is reported as well, because the difference between the two is itself worth knowing.

The target is peaks around -9 dBFS, which leaves normal speech in the -12 to -6 range that this
project has been quoting all along, with headroom for the moments a percentile deliberately ignores.
"""
from __future__ import annotations

import math
import time

import meters

# Peaks are aimed here. -9 dBFS puts ordinary speech in the -12 to -6 range with room left for the
# occasional loud word a percentile deliberately ignores.
TARGET_DBFS = -9.0
# How much of the take to ignore at the top. The 99th percentile of a 30-second sample at 20 polls
# a second discards the loudest ~6 readings, which is a cough, not a sentence.
PERCENTILE = 0.99
# Below this the take is treated as silence and nothing is written: there is no honest gain to
# derive from a microphone that was not used.
MIN_USABLE_DBFS = -45.0
# How long Windows takes to bring the multitrack endpoint up after the desk is told to expose it.
# Measured at about three seconds; four is the comfortable side of that.
ENDPOINT_WAIT_S = 4.0


def to_db(amplitude: float) -> float:
    """Linear 0..1 to dBFS. Silence is reported as a very negative number rather than -inf, so it
    can be compared and printed without a special case at every call site."""
    return 20.0 * math.log10(amplitude) if amplitude > 0 else -120.0


def from_db(db: float) -> float:
    return 10.0 ** (db / 20.0)


def percentile(values: list, fraction: float) -> float:
    """The value `fraction` of the way up a sorted list. Plain nearest-rank: with 600 readings the
    difference between interpolation methods is far below anything that matters here."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[idx]


def verdict(result: dict) -> dict:
    """Turn a measurement into a decision: how many dB to move, or why we are not moving anything."""
    loud_db = result["loud_db"]
    if loud_db < MIN_USABLE_DBFS:
        # Nothing usable. Say WHICH kind of nothing, because the two have different fixes.
        if result["mix_max_db"] > MIN_USABLE_DBFS:
            why = ("The unit heard something, but not on that channel. Check the microphone is "
                   "plugged into that input and that the channel is not muted.")
        else:
            why = ("Nothing came through at all. Check the microphone is plugged in, and that you "
                   "were talking into the right one.")
        return {"ok": False, "delta_db": 0.0, "reason": why}
    return {"ok": True, "delta_db": TARGET_DBFS - loud_db, "reason": ""}


def chat_endpoint():
    """The unit's Chat capture endpoint: where an isolated channel comes back to the computer."""
    for e in meters.list_endpoints():
        if (meters.is_desk_endpoint(e["name"]) and e["active"]
                and e["flow"] == meters.FLOW_NAMES[meters.CAPTURE] and "Chat" in e["name"]):
            return e
    return None


def measure_endpoint(endpoint_id: str, seconds: float, poll: float = 0.05,
                     should_stop=None) -> dict:
    """Watch ONE endpoint for a while. Used with an isolated mix, so what it hears is one channel.

    The mix reading doubles as the noise-floor check: with the mix isolated to a single source,
    anything present before the person starts talking is that channel's own floor."""
    m = meters.Meter(endpoint_id)
    samples = []
    try:
        end = time.time() + seconds
        while time.time() < end:
            if should_stop is not None and should_stop():
                break
            samples.append(m.peak())
            time.sleep(poll)
        muted, ceiling = m.muted, m.ceiling
    finally:
        m.close()
    loud = percentile(samples, PERCENTILE)
    top = max(samples) if samples else 0.0
    return {"seconds": round(len(samples) * poll, 1), "samples": len(samples),
            "loud": loud, "loud_db": to_db(loud), "max": top, "max_db": to_db(top),
            # No separate mix pair here: the isolated mix IS the channel, so "the unit heard
            # something else" is not a distinction this route can make.
            "mix_max_db": to_db(top), "muted": muted, "ceiling": ceiling}


def correct_for_level(result: dict, level: float) -> dict:
    """Take the cross-point's own level back out of a measurement.

    A cross-point carries its source at that level, so a channel measured through one reads quieter
    than it really is by exactly that much. Without this the gain would follow the fader: put the
    fader down 6 dB and auto gain would push the preamp up 6 dB to compensate, which is wrong in
    every way that matters."""
    if not level or level <= 0:
        return result
    offset = to_db(level)
    out = dict(result)
    out["crosspoint_db"] = offset
    for key in ("loud_db", "max_db", "mix_max_db"):
        out[key] = result[key] - offset
    out["loud"] = from_db(out["loud_db"])
    out["max"] = from_db(out["max_db"])
    return out
