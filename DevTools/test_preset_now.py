"""Unit tests for WHICH PRESET a strip is on (server._preset_now). NO DEVICE, no HID, no writes.

The device cannot answer this on its own: channelCurrentFxPreset is a name-only marker with three
factory slots that the unit blanks at the first parameter write, so it can never name a custom
preset and stops naming a factory one the moment you tweak anything. Left to the marker alone, the
Presets tab cannot remember what you applied.

The answer is our own record CHECKED against the live values, with the device marker preferred when
it has one. What these tests pin down is the honesty of that claim: an edited chain must not be
reported as the preset, another desk's record must not be claimed as this one's, and a record must
never outrank the unit's own marker.

Run: python DevTools/test_preset_now.py
"""
from __future__ import annotations
import sys

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import server
import channels

FAILED = 0


def check(label, got, want):
    global FAILED
    if got == want:
        print(f"  ok   {label}: {got!r}")
    else:
        FAILED += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


# A preset as the files store it: normalised floats, the module flags, the mode, and the marker.
PRESET = {
    "eqLowGain": 0.5, "eqMidGain": 0.25, "eqHighGain": 0.75,
    "aphexBBDrive": 0.1, "aphexAEMix": 0.2,
    "compressorThreshold": 0.3, "compressorGain": 0.4,
    "deesserThreshold": 0.6, "noiseGateThreshold": 0.7,
    "noiseGateRange": 0.8, "noiseGateRelease": 0.9,
    "channelDepth": 0.15, "channelSparkle": 0.35, "channelPunch": 0.55,
    channels.CHAIN_ADVANCED_PROP: True,
    **{m: (m in ("eqOn", "compressorOn")) for m in channels.PROCESSING_MODULE_BOOLS},
    channels.CHAIN_PRESET_MARKER_PROP: -1,
}


def live(**over):
    """A channel dump as channels.read_channels builds it, matching PRESET unless overridden."""
    ch = {
        "kind": "mic", "name": "Mic1", "preset_index": -1,
        "advanced": PRESET[channels.CHAIN_ADVANCED_PROP],
        "modules": {m: PRESET[m] for m in channels.PROCESSING_MODULE_BOOLS},
        "params": {p: v for p, v in PRESET.items()
                   if p not in channels.PROCESSING_MODULE_BOOLS
                   and p not in (channels.CHAIN_ADVANCED_PROP,
                                 channels.CHAIN_PRESET_MARKER_PROP)},
    }
    for k, v in over.items():
        if k in ("modules", "params"):
            ch[k] = {**ch[k], **v}
        else:
            ch[k] = v
    return ch


# Stub the three things that would otherwise touch the disk or the desk.
SETTINGS: dict = {}
server.load_settings = lambda: dict(SETTINGS)
server.update_settings = lambda **kw: SETTINGS.update(kw)
server._device_hardware_id = lambda: "DESK-A"
server._load_factory_presets = lambda: {"Podcast Studio": PRESET}
server._load_user_presets = lambda: {"Shaun voice": PRESET}


print("THE UNIT'S OWN MARKER WINS WHEN IT HAS AN ANSWER")
# A non-negative marker means a factory preset was applied and NOTHING has been written since - the
# firmware blanks it otherwise. That beats any record of ours, and it is the only thing that knows
# about a preset applied from the RODE app.
check("marker 1 is Podcast Studio, unedited", server._preset_now(1, live(preset_index=1)),
      {"name": "Podcast Studio", "edited": False, "source": "device"})
check("marker 0 is Neutral", server._preset_now(1, live(preset_index=0))["name"], "Neutral")
check("marker 2 is Broadcast", server._preset_now(1, live(preset_index=2))["name"], "Broadcast")
SETTINGS["applied_presets"] = {"device": "DESK-A", "channels": {"1": "Shaun voice"}}
check("and it outranks our own record", server._preset_now(1, live(preset_index=1))["name"],
      "Podcast Studio")

print("\nWITH NO MARKER, OUR RECORD ANSWERS - BUT ONLY AFTER THE VALUES AGREE")
SETTINGS["applied_presets"] = {"device": "DESK-A", "channels": {"1": "Shaun voice"}}
check("an untouched chain is the preset", server._preset_now(1, live()),
      {"name": "Shaun voice", "edited": False, "source": "record"})
check("a moved PARAMETER makes it edited, not the preset",
      server._preset_now(1, live(params={"eqMidGain": 0.60}))["edited"], True)
check("a switched MODULE does too (a voicing is which parts are engaged, not just values)",
      server._preset_now(1, live(modules={"eqOn": False}))["edited"], True)
check("so does the processing MODE",
      server._preset_now(1, live(advanced=False))["edited"], True)
# Read-back is never bit-identical, so a hair of movement is the same preset, not an edit.
check("a read-back wobble inside the tolerance is NOT an edit",
      server._preset_now(1, live(params={"eqMidGain": 0.25 + (server._PRESET_MATCH_TOL / 2)}))
      ["edited"], False)
check("but a change just outside it is", server._preset_now(
    1, live(params={"eqMidGain": 0.25 + (server._PRESET_MATCH_TOL * 2)}))["edited"], True)
check("a value we could not read is treated as a mismatch, never as a match",
      server._preset_now(1, live(params={"channelPunch": None}))["edited"], True)

print("\nWHEN NOTHING CAN BE CLAIMED, NOTHING IS")
SETTINGS["applied_presets"] = {"device": "DESK-A", "channels": {}}
check("no marker and no record", server._preset_now(1, live()), None)
SETTINGS["applied_presets"] = {"device": "DESK-A", "channels": {"1": "Deleted preset"}}
check("a record naming a preset that no longer exists is named but not judged",
      server._preset_now(1, live()), {"name": "Deleted preset", "edited": None, "source": "record"})

print("\nANOTHER DESK'S RECORD IS NEVER CLAIMED AS THIS ONE'S")
SETTINGS["applied_presets"] = {"device": "DESK-B", "channels": {"1": "Shaun voice"}}
check("a record written against another hardware id is ignored", server._preset_now(1, live()), None)
SETTINGS["applied_presets"] = {"device": "", "channels": {"1": "Shaun voice"}}
check("an untagged record still answers (written before we tagged them)",
      server._preset_now(1, live())["name"], "Shaun voice")

print("\nTHE RECORD IS PER CHANNEL, AND WRITING IT KEEPS THE OTHERS")
SETTINGS.clear()
server._note_applied_preset(1, "Podcast Studio")
server._note_applied_preset(3, "Shaun voice")
check("both channels are remembered",
      SETTINGS["applied_presets"]["channels"], {"1": "Podcast Studio", "3": "Shaun voice"})
check("tagged with the desk it was written on", SETTINGS["applied_presets"]["device"], "DESK-A")
check("channel 1 answers for itself", server._preset_now(1, live())["name"], "Podcast Studio")
check("channel 3 for itself", server._preset_now(3, live())["name"], "Shaun voice")
check("and a channel with no record still claims nothing", server._preset_now(2, live()), None)

print("\nALL CHECKS PASSED" if not FAILED else f"\n{FAILED} CHECK(S) FAILED")
sys.exit(1 if FAILED else 0)
