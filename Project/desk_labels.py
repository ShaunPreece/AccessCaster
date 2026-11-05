"""What the desk's settings are called: the words the window shows and the announcer speaks.

Pure data, importing nothing that touches the desk, so the window, the server and the announcer all
name a setting from one table. A table here is ALSO the spec the window builds its controls from,
so a label changed here changes the control and the spoken change together.
"""
from __future__ import annotations

import channels

# Channel-processing controls exposed in the UI. Every parameter is a normalised
# 0..1 float on the device (we present it as 0-100 percent); the property name
# drives it (see channels.set_channel_processing / PROCESSING_PROPS). Grouped
# the way the RODE app's per-channel processing panel groups them.
PROCESSING_CONTROLS = [
    # Each band is gain, then frequency, then WIDTH (Q), so the three controls for one band sit
    # together rather than the six-then-three arrangement the property list would suggest. "Width" is
    # the label, with Q in brackets: Q is the engineer's word and width is what it does, and a
    # screen-reader user arrowing a slider needs the second one first. Q is a control at all only
    # because it is independently writable (see channels.PROCESSING_PROPS).
    {"group": "Equaliser", "controls": [
        {"prop": "eqLowGain", "label": "Low gain"},
        {"prop": "eqLowShelf", "label": "Low frequency"},
        {"prop": "eqLowQ", "label": "Low width (Q)"},
        {"prop": "eqMidGain", "label": "Mid gain"},
        {"prop": "eqMidBell", "label": "Mid frequency"},
        {"prop": "eqMidQ", "label": "Mid width (Q)"},
        {"prop": "eqHighGain", "label": "High gain"},
        {"prop": "eqHighBell", "label": "High frequency"},
        {"prop": "eqHighQ", "label": "High width (Q)"},
    ]},
    {"group": "High-pass filter", "controls": [
        {"prop": "hpfFrequency", "label": "Frequency"},
        {"prop": "hpfSlope", "label": "Slope"},
    ]},
    {"group": "Compressor", "controls": [
        {"prop": "compressorThreshold", "label": "Threshold"},
        {"prop": "compressorRatio", "label": "Ratio"},
        {"prop": "compressorAttack", "label": "Attack"},
        {"prop": "compressorRelease", "label": "Release"},
        {"prop": "compressorGain", "label": "Makeup gain"},
    ]},
    {"group": "Noise gate", "controls": [
        {"prop": "noiseGateThreshold", "label": "Threshold"},
        {"prop": "noiseGateAttack", "label": "Attack"},
        {"prop": "noiseGateHold", "label": "Hold"},
        {"prop": "noiseGateRelease", "label": "Release"},
        {"prop": "noiseGateRange", "label": "Range"},
        {"prop": "noiseGateHysteresis", "label": "Hysteresis"},
    ]},
    {"group": "De-esser", "controls": [
        {"prop": "deesserThreshold", "label": "Threshold"},
        {"prop": "deesserRatio", "label": "Ratio"},
        {"prop": "deesserAttack", "label": "Attack"},
        {"prop": "deesserRelease", "label": "Release"},
        {"prop": "deesserGain", "label": "Gain"},
        {"prop": "deesserFrequency", "label": "Frequency"},
    ]},
    {"group": "Tone shaping", "controls": [
        {"prop": "channelDepth", "label": "Depth"},
        {"prop": "channelSparkle", "label": "Sparkle"},
        {"prop": "channelPunch", "label": "Punch"},
        {"prop": "aphexBBDrive", "label": "Big bottom (bass)"},
        {"prop": "aphexAEMix", "label": "Aural exciter (air)"},
    ]},
    # A mono channel gets channelPan; a stereo one gets the two legs instead (see devices.Channel
    # .stereo), so "stereo" says which kind of channel a control is for.
    {"group": "Pan", "controls": [
        {"prop": "channelPan", "label": "Pan", "pan": True, "stereo": False},
        {"prop": "channelPanL", "label": "Left side position", "pan": True, "stereo": True},
        {"prop": "channelPanR", "label": "Right side position", "pan": True, "stereo": True},
    ]},
]
# A parameter as it is spoken: "Threshold" alone could be the compressor's, the gate's or the
# de-esser's, so the group goes in front wherever the label does not already name its module.
PROCESSING_SPOKEN = {c["prop"]: (c["label"] if g["group"] in ("Pan", "Tone shaping")
                                 else f"{g['group']} {c['label'].lower()}")
                     for g in PROCESSING_CONTROLS for c in g["controls"]}
# The two Aphex tune knobs a preset moves, which have no control of their own.
PROCESSING_SPOKEN.update({"aphexBBTune": "Big bottom tune", "aphexAETune": "Aural exciter tune"})
PAN_PROPS = frozenset(c["prop"] for g in PROCESSING_CONTROLS for c in g["controls"]
                      if c.get("pan"))

# Module on/off toggles shown in the UI, with screen-reader labels. `prop` is the key of the
# module's state in a channel record's `modules`, which is not always `key` + "On" (pan's is
# channelPanOn).
PROCESSING_MODULES = [
    {"key": key, "label": label, "prop": channels.PROCESSING_MODULE_ON[key]}
    for key, label in (("hpf", "High-pass filter"), ("eq", "Equaliser"),
                       ("compressor", "Compressor"), ("noiseGate", "Noise gate"),
                       ("deesser", "De-esser"), ("aphex", "Aphex exciter"), ("pan", "Pan"))
]
MODULE_TOGGLE_LABEL = {m["key"]: m["label"].lower() for m in PROCESSING_MODULES}
# Every module switch by its property, the equaliser's three bands included.
MODULE_LABEL_BY_PROP = dict(
    {m["prop"]: m["label"] for m in PROCESSING_MODULES},
    eqHighOn="Equaliser high band", eqMidOn="Equaliser mid band", eqLowOn="Equaliser low band")

# Echo and reverb parameters in the order shown, with the words after the effect's name.
EFFECT_PARAMS = {
    "echo": [("echoMix", "level"), ("echoLowCut", "low cut"), ("echoHighCut", "high cut"),
             ("echoDelay", "delay"), ("echoDecay", "decay")],
    "reverb": [("reverbMix", "level"), ("reverbModel", "size"), ("reverbLowCut", "low cut"),
               ("reverbHighCut", "high cut")],
}
EFFECT_SPOKEN = {prop: f"{effect} {words}"
                 for effect, params in EFFECT_PARAMS.items() for prop, words in params}

# An output's mix type, as outputMixMinus holds it.
OUTPUT_MODE_LABEL = {0: "Main Mix", 1: "Mix Minus", 2: "Custom"}

# Switches on the OUTPUT and MASTER blocks, by property.
OUTPUT_SWITCH_LABEL = {
    "outputMonAutoMute": "Monitor auto-mute",
    "outputBTAutoMute": "Bluetooth auto-mute",
    "outputMultiBypass": "Processing on the tracks sent to this computer",
    "masterCompellorOn": "Master Compellor",
    "masterDelayOn": "Master delay",
}

UNIT_SETTINGS = [
    {"prop": "outputPrefader", "label": "Listen mode", "kind": "choice",
     "choices": [[1, "Pre-fader listen"], [0, "After-fader listen"]],
     "detail": "Pre-fader hears a soloed channel whatever its fader is doing; after-fader hears "
               "it as the fader has it."},
    {"prop": "outputMonFixed", "label": "Fixed output level on the monitor output", "kind": "bool",
     "detail": "The monitor output sends a fixed level and ignores the monitor knob, for "
               "speakers with their own volume."},
    {"prop": "systemRecButtonSetting", "label": "What the record button does when pressed",
     "kind": "choice", "choices": [[0, "Press to pause"], [1, "Press to stop"]],
     "detail": "Press and hold is unaffected."},
    {"prop": "systemHapticSetting", "label": "Haptic feedback", "kind": "choice",
     "choices": [[0, "Off"], [1, "On press and hold"], [2, "Always on"]],
     "detail": "Whether the desk buzzes when you touch a control."},
    {"prop": "metering", "label": "Meter style", "kind": "choice",
     "choices": [[0, "Default"], [1, "Broadcast"]],
     "detail": "How the desk's screen draws its meters. Nothing you can hear."},
    {"prop": "screenBrightness", "label": "Screen brightness", "kind": "int",
     "min": 0, "max": 255, "step": 5,
     "detail": "0 to 255."},
    {"prop": "autoBrightness", "label": "Adjust screen brightness automatically", "kind": "bool",
     "detail": "The desk sets its screen brightness from the room's light, overriding the "
               "setting above."},
    {"prop": "screenDimAfterSeconds", "label": "Dim the screen after", "kind": "choice",
     "choices": [[0, "Never"], [30, "30 seconds"], [60, "1 minute"], [120, "2 minutes"],
                 [300, "5 minutes"], [600, "10 minutes"], [1800, "30 minutes"]],
     "detail": "With nothing touched."},
    {"prop": "activeButtonsBrightness", "label": "Button brightness when lit", "kind": "int",
     "min": 0, "max": 255, "step": 5,
     "detail": "0 to 255."},
    {"prop": "inactiveButtonsBrightness", "label": "Button brightness when not lit", "kind": "int",
     "min": 0, "max": 255, "step": 5,
     "detail": "0 to 255. Turn it down to darken the desk and keep lit buttons lit."},
    # ---- THE CLOCK (selectors derived and read-back proven). -------------------------------------
    # The timezone and daylight saving are NOT offered: both store a write and leave the clock
    # alone, because the touchscreen applies them. See device_page.UNIT_SETTINGS_NOT_ON_PAGE.
    {"prop": "systemDateTime24h", "label": "24-hour clock", "kind": "bool",
     "detail": "Otherwise 12-hour with am and pm."},
    {"prop": "systemDateTimeOnHome", "label": "Show the clock on the desk's home screen",
     "kind": "bool",
     "detail": "Nothing you can hear; for a sighted co-host."},
    # ---- THE LOCKOUTS (selectors derived and read-back proven). ----------------------------------
    # They are last on the page on purpose: they are the ones that can make a working desk feel
    # broken, so the wording has to say exactly what goes quiet and that this app can always undo
    # it.
    {"prop": "disableAllPhysicalButtons", "label": "Lock the desk's physical buttons", "kind": "bool",
     "detail": "The faders and touchscreen still work, and this app can switch it back off."},
    {"prop": "disableAllHeadphoneOutputs", "label": "Silence every headphone output", "kind": "bool",
     "detail": "Mixes, recording and streaming carry on. Switch it off here to get them back."},
    {"prop": "disableAllLineoutOutputs", "label": "Silence every line output", "kind": "bool",
     "detail": "Usually your studio speakers. Recording and USB are unaffected."},
]
UNIT_SETTING_BY_PROP = {s["prop"]: s for s in UNIT_SETTINGS}


def unit_setting_words(spec: dict, value) -> str:
    """A unit setting's value as the Device page names it: the choice's own words, on or off, or
    the number."""
    if spec["kind"] == "choice":
        return next((c[1] for c in spec["choices"] if int(c[0]) == int(value)), str(value))
    if spec["kind"] == "bool":
        return "on" if value else "off"
    return str(value)


def pan_words(value):
    """A pan position, 0 to 100 with 50 the centre, in words: a bare percentage gives no
    direction."""
    if value == 50:
        return "centre"
    if value <= 0:
        return "hard left"
    if value >= 100:
        return "hard right"
    return f"{(50 - value) * 2} percent left" if value < 50 else f"{(value - 50) * 2} percent right"
