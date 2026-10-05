"""Phase 0 of the coverage audit: classify EVERY device property against our code.

Generates ProjectMemory/PROPERTY_COVERAGE_<DESK>.md - the THIRD yardstick alongside the user guide
(PARITY.md) and the RODE app's own screens.

  python DevTools/property_coverage.py            # regenerate the markdown
  python DevTools/property_coverage.py --check     # print the summary only, write nothing

NO DEVICE NEEDED and nothing is written to hardware: this reads the generated
docs/PROPERTY_LIST_<DESK>.md and greps our source. Re-run it whenever that list or the code
changes; the file it writes is generated, so edit THIS script, never the markdown.

Two things it works out per property:
  1. COVERAGE - do we read it, write it, or only mention it in a comment. Derived from where the
     property NAME appears: an explicit read/write call on the line, else the enclosing constant
     or function (CONTEXT_ROLE below), else "referenced".
  2. CLASSIFICATION - for everything we never touch: internal/derived, live state, or candidate
     feature, plus an accessibility VALUE ranking. Those are JUDGEMENTS, hand-written in RULES
     below with a one-line reason each, not something a script can infer. Phases 1-3 of the plan
     confirm or overturn them; when one is settled, edit the rule and regenerate.
"""
from __future__ import annotations
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401  (puts Project/ on sys.path; unused here but keeps the convention)

REPO = Path(__file__).resolve().parent.parent
# The spec stays in THIS repo (it is written for anyone building their own controller); the reports
# below are notes, so they are written to the private sibling repo instead.
PROTOCOL = REPO / "docs" / "RODECASTER_PROTOCOL.txt"

# DEVICE-SCOPED, because this report consumes the OCCURRENCE COUNTS and those genuinely differ
# between the desks (Duo PAD x22 vs Pro II x24, PADBUTTON x48 vs x64 - the physical shape). The
# property NAMES are identical on both (463, measured), so anything needing only
# the name universe does NOT need to be device-aware; this does. Still no hardware required:
# get_active_device() falls back to the Pro II when nothing is plugged in.
import devices  # noqa: E402  (after _bootstrap puts Project/ on sys.path)


def _slug() -> str:
    return devices.get_active_device().slug


def list_path(slug: str | None = None) -> Path:
    return Path(_bootstrap.docs_path(f"PROPERTY_LIST_{slug or _slug()}.md"))


def out_path(slug: str | None = None) -> Path:
    return Path(_bootstrap.notes_path("ProjectMemory", f"PROPERTY_COVERAGE_{slug or _slug()}.md"))

# The app's own surface. DevTools scripts are scanned separately and reported apart: a property only
# a dev script has ever touched is NOT a shipped feature.
APP_GLOBS = ["Project/*.py", "Project/static/*.js", "Project/static/*.html"]
DEV_GLOBS = ["DevTools/*.py"]


# ---------------------------------------------------------------------------
# 1. The property list
# ---------------------------------------------------------------------------
# READS docs/PROPERTY_LIST_<DESK>.md, which DevTools/property_list.py generates from the live
# desk. NO DEVICE NEEDED - it reads the generated file, not the desk. Do not point it back at a
# hand-frozen appendix: one written by the old flat walk truncated EFFECTS_PARAMETERS to
# PARAMETERS, invented a YPOT and missed five nested container blocks, so coverage came out
# measured against a slightly wrong universe.
_LIST_ROW = re.compile(r"^- `([A-Za-z]\w*)` - ([\w/?]+), x(\d+)\s*$")


def read_appendix() -> list[tuple[str, str, int]]:
    """(name, type, occurrences) for every property the desk publishes."""
    path = list_path()
    if not path.exists():
        raise SystemExit(f"ProjectMemory/{path.name} is missing. "
                         "Regenerate it with: python DevTools/property_list.py")
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _LIST_ROW.match(line)
        if m:
            out.append((m.group(1), m.group(2), int(m.group(3))))
    return out


# ---------------------------------------------------------------------------
# 2. Where our code touches each property
# ---------------------------------------------------------------------------
# A line that decodes a value out of a model dump.
READ_CALL = re.compile(r"find_all|\bvalues\(|\bvec\(|\bfirst\(|read_model|path_value|block_value|props\.get")
# A line that builds or sends a record to the device.
WRITE_CALL = re.compile(r"build_\w+|send_record|send_records_paced")

# Most property names live inside a named collection rather than at a call site, so the enclosing
# constant or function decides the role. Anything not listed falls back to "referenced".
CONTEXT_ROLE = {
    # reads
    "PROCESSING_READ_PROPS": "read", "PROCESSING_MODULE_BOOLS": "read",
    "read_settings": "read", "read_mixer": "read", "read_processing": "read",
    "read_channels": "read", "read_matrix_state": "read", "read_matrix_levels": "read",
    "read_output_modes": "read", "read_pads_live": "read", "pad_positions": "read",
    "_fields_by_padidx": "read", "first": "read", "read_device_status": "read",
    "strip_vector": "read", "publish_strip_vector": "read", "read_routing_live": "read",
    # writes
    "PROCESSING_PROPS": "write", "PRESET_EXTRA_FLOAT_PROPS": "write",
    "PRESET_FLOAT_PROPS": "write", "PROCESSING_MODULE_ON": "write", "CHANNEL_PROP": "write",
    "CHANNEL_FX_ON": "write", "PAD_PROP": "write", "MASTER_COMPELLOR_PARAM": "write",
    # The unit's own settings (display/haptics/REC button/listen mode): the address table in
    # rcp_transfer is the write side; server.UNIT_SETTINGS is the spec that drives both the read and
    # the write, so it counts as both.
    "UNIT_SETTING_SEL": "write", "UNIT_SETTINGS": "both",
    # Channel mute: written by channels.set_channel_mute and read by read_channels.
    "MUTE_PROP": "both",
    # Channel solo/Listen: channels.set_channel_solo + clear_all_solos write it, read_channels and
    # read_solos read it. Same two-sided shape as the mute, so the same verdict.
    "CUE_PROP": "both",
    "_PHASE1": "write", "_EFFECTS": "write",
    "EFFECT_FLAG": "write", "FX_ENGINE_PARAMS": "write",
    "BACK_CHANNEL_SOURCES": "write", "OUTPUT_MIX_MINUS": "write",
    # both (UI specs and device tables describe a property we read AND write)
    "PROCESSING_CONTROLS": "both", "_PRESET_PROPS": "both", "CHANNEL_FX_PARAMS": "both",
    "FX_BUILD_SPEC": "both", "FX_PARAM_LABELS": "both", "FX_PARAMS_BY_EFFECT": "both",
    "PRO2": "both", "DUO": "both", "PRO2_INPUT_STRIPS": "both", "DUO_INPUT_STRIPS": "both",
    "PRO2_UI_CHANNELS": "both", "DUO_UI_CHANNELS": "both",
}
# Names the heuristics cannot place from their line alone.
NAME_ROLE = {
    # decoded out of the live report-04 event stream by the announcer, not from a model dump
    "selectedBank": "read", "encoderPressed": "read",
    # a sync marker the device EMITS during a pad create; we wait for it
    "padRCVSyncPadType": "read",
}
_TOP = re.compile(r"^(?:const |let |var )?([A-Za-z_][\w]*)\s*[:=]")
_DEF = re.compile(r"^\s*(?:async\s+)?(?:def|function)\s+([A-Za-z_]\w+)")


def _code_and_comment(path: Path) -> list[tuple[int, str, bool]]:
    """(lineno, text, is_comment) per line, with Python docstrings and JS/HTML comments marked.

    Crude but sufficient: we only need to tell "this property is really used" from "this property
    is only talked about in a comment", and a name inside a string literal on a code line is a use.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    out, in_doc, in_block = [], None, False
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        comment = False
        if path.suffix == ".py":
            if in_doc:
                comment = True
                if in_doc in line:
                    in_doc = None
            else:
                m = re.match(r'^\s*[rbfu]*("""|\'\'\')', line)
                if m:
                    comment = True
                    rest = line.split(m.group(1), 1)[1]
                    if m.group(1) not in rest:
                        in_doc = m.group(1)
                elif stripped.startswith("#"):
                    comment = True
        else:
            if in_block:
                comment = True
                if "*/" in line:
                    in_block = False
            elif stripped.startswith("//") or stripped.startswith("<!--"):
                comment = True
            elif "/*" in line and "*/" not in line:
                comment, in_block = True, True
        out.append((i, line, comment))
    return out


# Short property names collide with ordinary words in our own code and markup, so they only count
# when they appear as a STRING LITERAL (the way every real property reference is written).
SHORT_NAME = 8
# ...and these still collide even then, so they are excluded by hand.
FALSE_POSITIVES = {
    "wifi": 'profiles.py has its own "wifi" snapshot key; the device property is never touched.',
}


# Some property names are held in a CONSTANT instead of being repeated as a literal, so scanning for
# the literal alone finds only the constant's definition and under-reports the property as merely
# "referenced" (it did exactly that for the two chain properties added). Count uses of the
# constant as uses of the property.
NAME_ALIASES = {
    "channelAdvancedProcessing": ("CHAIN_ADVANCED_PROP",),
    "channelCurrentFxPreset": ("CHAIN_PRESET_MARKER_PROP",),
}


def _pat(name: str) -> re.Pattern:
    alts = [re.escape(a) for a in NAME_ALIASES.get(name, ())]
    if len(name) <= SHORT_NAME:
        lit = r"""["'`]""" + name + r"""["'`]"""
        return re.compile("|".join([lit] + [r"\b" + a + r"\b" for a in alts]))
    return re.compile(r"\b(" + "|".join([name] + alts) + r")\b")


def scan(globs: list[str], names: list[str]) -> dict[str, dict]:
    """{property: {"roles": set, "sites": [(file, line, role)], "code": int, "comment": int}}."""
    pats = {n: _pat(n) for n in names if n not in FALSE_POSITIVES}
    found: dict[str, dict] = {}
    for glob in globs:
        for path in sorted(REPO.glob(glob)):
            if path.resolve() == Path(__file__).resolve():
                continue    # this script names properties in its own RULES table
            rel = path.relative_to(REPO).as_posix()
            scope = None
            for lineno, line, is_comment in _code_and_comment(path):
                m = _TOP.match(line)
                if m:
                    scope = m.group(1)
                d = _DEF.match(line)
                if d:
                    scope = d.group(1)
                for name, pat in pats.items():
                    if not pat.search(line):
                        continue
                    e = found.setdefault(name, {"roles": set(), "sites": [], "csites": [],
                                               "code": 0, "comment": 0})
                    if is_comment:
                        e["comment"] += 1
                        if len(e["csites"]) < 2:
                            e["csites"].append((rel, lineno, "comment"))
                        continue
                    e["code"] += 1
                    if name in NAME_ROLE:
                        role = NAME_ROLE[name]
                    elif WRITE_CALL.search(line) and READ_CALL.search(line):
                        role = "both"
                    elif WRITE_CALL.search(line):
                        role = "write"
                    elif READ_CALL.search(line):
                        role = "read"
                    elif scope in CONTEXT_ROLE:
                        role = CONTEXT_ROLE[scope]
                    elif scope and re.match(r"(build|set|send|apply|clear|restore)_", scope):
                        # a multi-line builder call: the name sits on its own argument line
                        role = "write"
                    elif scope and scope.startswith("read_"):
                        role = "read"
                    else:
                        role = "referenced"
                    e["roles"].add(role)
                    if len(e["sites"]) < 4:
                        e["sites"].append((rel, lineno, role))
    return found


def coverage_of(entry: dict | None) -> str:
    """One word for the coverage column."""
    if entry is None:
        return "untouched"
    if entry["code"] == 0:
        return "comment only"
    roles = entry["roles"]
    if "both" in roles or {"read", "write"} <= roles:
        return "read+write"
    if "write" in roles:
        return "write"
    if "read" in roles:
        return "read"
    return "referenced"


# ---------------------------------------------------------------------------
# 3. Classification of what we never touch (judgement, one reason each)
# ---------------------------------------------------------------------------
# (name regex, tag, value, note). First match wins, so order matters.
#   tag   - internal/derived | live state | candidate feature | chain gap
#   value - high | med | low | -   (accessibility value if we exposed it)
RULES: list[tuple[str, str, str, str]] = [
    # PRESENT ONLY ONCE A CUSTOM VOICE EFFECT HAS BEEN SAVED, so a factory-clean dump does not list
    # it at all. A 316-byte string holding a preset's whole engine/param stack, written by our own
    # FX editor.
    (r"^fxPresetContents$", "internal/derived", "-",
     "The saved custom voice effect itself: a long string carrying the whole engine and parameter "
     "stack, one per FXPRESET block. We already create and edit these through the FX editor, which "
     "writes the stack property by property, so there is nothing to expose separately - and it is "
     "the property whose 316-byte length proved the model needs a real string-length reader "
     "(model_tree)."),
    # ---- blind spot 1: per-strip properties missing from our chain set -------------------
    (r"^eq(Low|Mid|High)Q$", "internal/derived", "-",
     "Per-strip EQ Q, and RESOLVED: the USB capture of a preset apply shows the RODE app "
     "NEVER writes these - the device recomputes them from the EQ params, and they follow a channel "
     "across a fader move for the same reason. So they need no writer and no place in our chain set."),
    (r"^(channelPanL|channelPanR)$", "chain gap", "med",
     "Per-strip. The pan of a STEREO source (where each leg sits); channelPan only pans a mono one."),
    (r"^hpf(Higher|Lower)On$", "chain gap", "med",
     "Per-strip HPF variants with no app control found yet. Confirm before including in the chain set."),
    (r"^channelAdvancedProcessing$", "candidate feature", "med",
     "CONFIRMED phase 2 + BUILT: the app's advanced-vs-simple processing mode, per strip. "
     "Now read, written, carried in presets/profiles and moved by a fader swap "
     "(channels.CHAIN_ADVANCED_PROP). Still no UI control of its own - that is the remaining work."),
    (r"^channelCurrentFxPreset$", "candidate feature", "med",
     "The marker deciding which preset NAME a strip shows. BUILT into the chain set, "
     "written LAST (the device blanks it to -1 on any parameter write). Remaining work: show the "
     "preset name in our UI, with the caveat that it is a NAME only - the values may not match it."),
    (r"^channelWirelessMute$", "live state", "med",
     "BUILT: mutes a paired RODE wireless transmitter at the mic, per strip. Read "
     "onto the mic channel panel and written by channels.set_channel_wireless_mute, a compact bool on "
     "0x1C + strip - the SAME CHANNEL node the mute and the solo sit on, read off a live tree rather "
     "than captured. It is NOT in the chain set and must not join it: a mute is live state, so a "
     "fader move carrying it would silence a mic somebody had not muted. Never ear-proven; nobody "
     "here owns a wireless mic."),
    (r"^streamerXPreset(Created|Name)$", "chain gap", "low",
     "Per-strip but Streamer X naming; expected irrelevant to Pro II/Duo, confirm and dismiss."),
    (r"^channelCueEnable$", "live state", "high",
     "PHASE 1 CONFIRMED: this is the desk's SOLO/cue state, per strip, PUSHED on report-04 when a "
     "solo button is pressed (selector 0x1C + strip). Overturns the backlog claim that solo state is "
     "unreadable. AND IT IS WRITABLE: proven on hardware (compact bool on 0x1C + strip, "
     "the same shape as the mute below), so Listen/solo from software is a shipped feature "
     "(channels.set_channel_solo, plus clear_all_solos as the way out). It had been filed "
     "'physical/MIDI only' for a year purely because the RODE app has no control for it - which "
     "means no CAPTURE ROUTE, never unwritable. Per-strip live state, not chain, and deliberately "
     "never restored by a profile (see the profile-coverage report)."),
    (r"^(channelListenSource|channelTalkbackEnable)$", "live state", "med",
     "Per-strip LIVE STATE, not chain. Phase 2 shows whether the RODE app moves it on a fader drag; "
     "do not guess."),

    (r"^channelOutputMute$", "live state", "high",
     "Per-strip MUTE state, x10. PUSHED on report-04 the moment a mute changes, with selector "
     "0x1C + strip, so the announcer decodes it from the stream it already reads - no polling. AND "
     "IT IS WRITABLE: proven on hardware (compact bool on 0x1C + strip, the module-toggle shape), "
     "so mute/unmute from software is a shipped feature (channels.set_channel_mute). A scan that "
     "calls it unsettable over report-3 is using the mic-preamp channel-address format, which is "
     "not this property's."),
    (r"^systemChannelSelected$", "live state", "med",
     "Which channel the desk itself has selected. Mentioned in our comments as NOT needed before a "
     "write; as a READ it would let us announce what the encoder is about to change."),
    (r"^duckerDepth$", "candidate feature", "med",
     "Auto-duck depth. Read ad hoc during the bed-gating investigation, never exposed as a control."),
    (r"^encoderColour$", "internal/derived", "-",
     "Encoder ring colour; appearance only."),

    # ---- metering and physical control position: the accessibility headline --------------
    # `metering` IDENTIFIED (phase 2): it is the touchscreen METER STYLE, not a data
    # subscription. The app's Display > Metering screen offers Default (0) and Broadcast (1), and
    # switching to Broadcast pushed NOTHING and left the meter arrays frozen. So it is a display
    # setting, and one worth exposing for a sighted co-host, not a route to meter values.
    (r"^metering$", "candidate feature", "low",
     "Touchscreen METER STYLE: 0 = Default (markers + unity), 1 = Broadcast (dBFS markers + gain "
     "reduction). Confirmed by driving the app's Display > Metering screen. NOT a "
     "metering-data subscription - Broadcast mode pushed nothing. EXPOSED: selector "
     "captured (0x07, the GUI block) and read-back verified, so a sighted co-host's meter style can "
     "be set from the Device page."),
    (r"^(meterLevel[LR]|meterPeak[LR]|meterStereo|broadcastMeters|assignableMeterSource)$",
     "live state", "-",
     "METER VALUES: CLOSED as unavailable over the control protocol. Phase 1 saw nothing "
     "pushed into a live mic; phase 2 saw nothing with the RODE app connected (it sends ONE record, "
     "its dump trigger, in 37 s - USB capture), and nothing in Broadcast meter style either. The "
     "arrays are frozen placeholders. THE ROUTE IS WASAPI on the desk's USB AUDIO streams (main mix, "
     "Chat, recording), which needs no protocol work: Never blind-write these."),
    (r"^fader(Level|Max|Min)$", "internal/derived", "-",
     "NOT the physical fader position, whatever the name suggests: it does not move while fader 1 "
     "goes from top to halfway. The live position is mixLevelWithAnchor, which "
     "rcp_read.read_matrix_levels already parses."),
    (r"^pot(Level|Max|Min)$", "internal/derived", "-",
     "Does not move across an encoder turn (outputMonLevel does), so like faderLevel this is not "
     "the live control position. Not tested in isolation"),
    (r"^(mutePressed|soloPressed|recButtonPressed|padButtonPressed|padButtonPreview|encoderSignal|"
     r"screenTouched|onOffSw)$", "live state", "med",
     "Physical control PRESS events. The announcer already decodes the report-04 equivalents; these "
     "are edges, not state (see the mute-state backlog item, which wants channelOutputMute polling)."),
    (r"^tx", "live state", "high",
     "Wireless TX telemetry: battery, RSSI, signal quality, charging, pad, remote mute, device type. "
     "A flat wireless mic is invisible to a blind operator today."),

    # ---- recorder / storage / player ----------------------------------------------------
    (r"^(recordState|recordTimeMs|recordBytesPerSecond|requestRecordState|requestDropMarker|"
     r"lastRecordingID|requestDeleteUID|recordingTotalCount|recordingTotalDuration|"
     r"recordingProcessingBypass|recordingCompressionQuality)$", "candidate feature", "high",
     "The recorder: start/stop, elapsed time, marker drop, raw-vs-processed, quality, recording list. "
     "Spoken record state is a real gap; needs a phase-3 capture for start/stop ordering."),
    (r"^storageVolume", "candidate feature", "high",
     "SD/USB storage x3: inserted, mounted, formatted, free, capacity, name, record destination, "
     "eject, erase. 'The card is nearly full' before a show, with no touchscreen."),
    (r"^padRecord", "candidate feature", "med",
     "On-device pad recorder (record straight onto a SMART pad), including a memory-full event."),
    (r"^player", "candidate feature", "med",
     "On-device player transport: state, position, progress, speed, fade in/out envelope, file path."),

    # ---- outputs, lockouts, shows -------------------------------------------------------
    (r"^output(Mon|BT)", "candidate feature", "high",
     "Monitor speaker and Bluetooth output LEVEL/MUTE (and fixed-level). We already write the "
     "auto-mute toggles but never the level or the mute itself. HIGH because outputMonLevel is what "
     "the encoder actually moves, it is PUSHED live, and a blind operator who nudges the encoder "
     "has no other way to put the monitor level back from software."),
    # outputPrefader IS NOT MULTITRACK-RELATED, whatever the name beside it suggests. It is the
    # app's Output > Listen choice, i.e. what SOLO plays in the headphones - False = After-fader
    # Listen (the channel at its fader level), True = Pre-fader Listen (at unity regardless of the
    # fader). Identified by driving that screen: one clean diff, nothing else moved. HIGH because it
    # is a real device setting a blind operator cannot otherwise reach, and it is one bit. Needs ONE
    # capture for its write selector.
    (r"^outputPrefader$", "candidate feature", "high",
     "SOLO/Listen mode: False = After-fader Listen (solo plays the channel at its fader level), "
     "True = Pre-fader Listen (at unity). Confirmed by driving the app's Output > Listen screen. "
     "Selector not captured, so it is read-only for us."),
    (r"^disableAll", "candidate feature", "med",
     "Lockouts: all headphone outputs, all line outs, all physical buttons. Useful for a locked-down "
     "venue setup."),
    (r"^(showControl|currentShow)", "candidate feature", "med",
     "The device's own Shows: export, import, new-from-default, delete, lockout, progress, plus the "
     "current show's name/UUID/icon. Compare against our home-grown Profiles BEFORE duplicating work."),

    # ---- touchscreen takeover -----------------------------------------------------------
    (r"^bt", "candidate feature", "high",
     "Bluetooth pairing: scan, pair, connect, disconnect, pair code, paired list. Touchscreen-only "
     "today, so this is core to the replace-the-touchscreen plan."),
    (r"^wifi", "candidate feature", "high",
     "Wifi scan and join. Touchscreen-only today. Values include the PSK: never log or commit them."),
    (r"^(ipAddress|subnetMask|primaryDns|secondaryDns|staticIpSet|wiredConnected|nonHTTPSInterface)$",
     "candidate feature", "low",
     "Network status and static IP configuration."),
    (r"^systemDateTime|^systemDateTimezone$", "candidate feature", "med",
     "Clock, 24h, timezone, daylight saving, show-on-home. Touchscreen-only setup today."),
    # VALUES CONFIRMED by driving the app's Display and System tabs.
    # SELECTORS CAPTURED, so these three are now WRITTEN, not candidates: they are on the
    # Device page and in a profile (server.UNIT_SETTINGS). The notes are kept for their value
    # rankings and their decoded value tables.
    (r"^systemRecButtonSetting$", "candidate feature", "med",
     "REC button behaviour: 0 = Press to Pause (hold to stop), 1 = Press to Stop (hold to pause). "
     "Confirmed on the app's Display > REC Button screen. EXPOSED (selector 0x0F, the "
     "SYSTEM block, captured + read-back verified)."),
    (r"^systemHapticSetting$", "candidate feature", "med",
     "Touchscreen haptics: 0 = none, 1 = on hold, 2 = always on. Confirmed on the app's Display > "
     "Haptics screen. Haptics are one of the few non-visual confirmations the desk itself gives. "
     "EXPOSED (selector 0x0F). STILL OPEN: whether the physical buttons buzz or only the "
     "touchscreen does - the clean test is to write 2 and press a button."),
    (r"^(systemName|systemMainMode|systemMixSelected)$", "candidate feature", "low",
     "Device name and current screen/mix selection: touchscreen settings."),
    (r"^(screenBrightness|hwScreenBrightness|autoBrightness|screenDimAfterSeconds|"
     r"activeButtonsBrightness|inactiveButtonsBrightness|allLEDSWhite|themeId|headphoneColour)$",
     "candidate feature", "low",
     "Brightness, LED and theme. Low value to a blind operator, but part of the touchscreen takeover "
     "for a sighted co-host. Mapped on the app's Display > Brightness screen: "
     "autoBrightness = the Automatic Brightness switch (and hwScreenBrightness then follows the light "
     "sensor, 250 -> 70 here), screenDimAfterSeconds = the Dim After row (30 min = 1800, Never = 0, "
     "which decodes as a bool false), inactiveButtonsBrightness = the Buttons slider (0-255). "
     "EXPOSED on the GUI block, selector 0x07: screenBrightness, autoBrightness, "
     "screenDimAfterSeconds and inactiveButtonsBrightness were captured, and "
     "activeButtonsBrightness was written on the DERIVED 0x07 and read back (the app has no control "
     "for it at all). Still untouched here: hwScreenBrightness (the light sensor's own reading, "
     "read-only), allLEDSWhite, themeId, headphoneColour - no app control was ever found for those, "
     "and allLEDSWhite/themeId sit under TEST/THEME, blocks with no confirmed member."),
    (r"^(powerOffRequest|toneGeneration)$", "candidate feature", "med",
     "Power off, and a test-tone generator that would make line-up possible by ear without the app."),
    (r"^(radioPair|radioPaired|radioUnpair|rxRadioId)$", "candidate feature", "med",
     "Wireless receiver pairing (pairs with the tx* telemetry above)."),

    # ---- smaller candidates -------------------------------------------------------------
    (r"^mixLink$", "internal/derived", "-",
     "NOT the per-cross-point link state, despite what this file said before: MEASURED it "
     "reads True on 508 of the 516 cells while the proven link inference (level == anchor in "
     "mixLevelWithAnchor) says 114 linked, agreeing on only 112. So it is some other per-cell flag "
     "(what CAN be linked, or a stereo pairing), and reading it as link state would misreport the "
     "matrix. Keep inferring link from mixLevelWithAnchor, which is ear- and read-back-proven."),
    (r"^emergencyMuteActive$", "live state", "med",
     "Emergency mute engaged. Worth announcing; already read ad hoc in one investigation."),
    (r"^audio", "candidate feature", "low",
     "USB audio interface facts: sample rate, channel counts, latency, buffer size. Would enrich the "
     "Device page's existing USB-interfaces section."),
    (r"^(usb1Connected|usbHostOnUsb2|usbNumberPad|usbHostUnspportedFirmware)$", "live state", "low",
     "USB host/device connection state."),
    (r"^(fxPresetIdx|padActiveEdit|badPadDetected)$", "live state", "low",
     "App/editor-side selection and health state."),
    (r"^input(Id|RcvAudioSourceType|SipCallSlot|WirelessSN)$", "live state", "low",
     "Per-input-slot identity, x30: source type, SIP slot, wireless serial."),

    # ---- internals, other products, machinery we deliberately avoid ---------------------
    (r"^lang$", "candidate feature", "low",
     "Device UI language; touchscreen-only setting."),
    (r"^eqParamMode", "internal/derived", "-",
     "Global EQ editor mode (x1): app UI state, not device audio."),
    (r"^(update|download|osUpdateAvailable|appUpdateAvailable)", "internal/derived", "-",
     "Firmware update machinery. Deliberately out of scope: we do not want a mis-write anywhere near it."),
    (r"^build", "internal/derived", "-",
     "Firmware/GUI build strings and git SHAs; diagnostics only (we already read systemFirmwareVersion)."),
    (r"^(streamerX|activeStreamerXMixPreset|streammixmixLevel)", "internal/derived", "-",
     "Streamer X is a different product; its schema rides along in the shared model."),
    (r"^(rcSync|mixRcSync|unifyMode|enableRFCDuplication|forceCodecChoice|engineMode|boardType)",
     "internal/derived", "-",
     "Device-to-device sync/unify, codec and hardware identity internals."),
    (r"^app(Compression|MonitorMix|OutputDevice|Recording)$", "internal/derived", "-",
     "Mirrors of the connected RODE app's own settings, not device state."),
    (r"^cell", "internal/derived", "-",
     "Cellular accessory support; not present on these desks."),
    (r"^sip|^(dtmfMode|rodeCallQuality|receiveJitterBuffer|incoming|enableVideoStream|presenter|"
     r"share(Anom|Resource))", "internal/derived", "low",
     "CallMe/SIP telephony (41 properties) and its call-quality plumbing. Only matters if CallMe is "
     "in use; several hold credentials, so never log or commit values."),
    (r"^padSIP", "internal/derived", "-",
     "Per-pad SIP call-slot flash/lock state; SIP-only."),
    (r"^systemBetaMode$|^systemSerialNumber$", "internal/derived", "-",
     "Beta flag and serial number; identity/diagnostics."),
]
_RULES = [(re.compile(p), t, v, n) for p, t, v, n in RULES]


def classify(name: str) -> tuple[str, str, str]:
    for pat, tag, value, note in _RULES:
        if pat.search(name):
            return tag, value, note
    return "unclassified", "-", "NOT YET CLASSIFIED - add a rule in DevTools/property_coverage.py."


# ---------------------------------------------------------------------------
# 4. Report
# ---------------------------------------------------------------------------
VALUE_ORDER = {"high": 0, "med": 1, "low": 2, "-": 3}


def chain_set() -> set[str]:
    """The per-strip properties our chain features actually carry, read from the code itself.

    Importing channels touches no hardware (rcp_hid does nothing at import), so this stays a
    no-device script while never drifting from the lists it describes.
    """
    import channels
    return (set(channels.PROCESSING_READ_PROPS) | set(channels.PRESET_FLOAT_PROPS)
            | set(channels.PROCESSING_MODULE_BOOLS) | {"channelBypassProcessing"})


CHAIN_SET = chain_set()


def build() -> tuple[str, dict]:
    props = read_appendix()
    names = [n for n, _t, _c in props]
    app = scan(APP_GLOBS, names)
    dev = scan(DEV_GLOBS, names)

    rows = []
    for name, ptype, occ in props:
        entry = app.get(name)
        cov = coverage_of(entry)
        tag, value, note = ("-", "-", "") if cov in ("read", "write", "read+write", "referenced") \
            else classify(name)
        sites = (entry or {}).get("sites") or (entry or {}).get("csites") or []
        where = ", ".join(f"{f}:{l}" for f, l, _r in sites[:2])
        if not where and name in dev and dev[name]["code"]:
            where = "DevTools only: " + ", ".join(
                f"{f}:{l}" for f, l, _r in (dev[name]["sites"] or dev[name]["csites"])[:1])
        rows.append(dict(name=name, type=ptype, occ=occ, cov=cov, tag=tag,
                         value=value, note=note, where=where))

    counts = {}
    for r in rows:
        counts[r["cov"]] = counts.get(r["cov"], 0) + 1
    tags = {}
    for r in rows:
        if r["tag"] != "-":
            tags[r["tag"]] = tags.get(r["tag"], 0) + 1

    touched = sum(counts.get(k, 0) for k in ("read", "write", "read+write", "referenced"))
    dev_only = sum(1 for r in rows if r["cov"] in ("untouched", "comment only")
                   and dev.get(r["name"], {}).get("code"))

    out = []
    w = out.append
    w("# Property coverage: every device property vs our code")
    w("")
    w("GENERATED FILE - do not hand-edit. Regenerate with `python DevTools/property_coverage.py`")
    w("(no device needed). The classifications and value rankings are JUDGEMENTS held in that")
    w("script's RULES table with a reason each; to change one, edit the rule and regenerate.")
    w("")
    w("This is the THIRD yardstick, alongside the user guide")
    w("(`PARITY.md`) and the RODE app's UI. It answers the question neither of those can: which")
    w("device properties exist that no control of ours, and sometimes no control of RODE's, exposes.")
    w("Source of the property list: the appendix in `RODECASTER_PROTOCOL.txt` (a Duo fw 1.7.3 model")
    w("dump; the Pro II schema is essentially the same, occurrence counts differ).")
    w("")
    w("## Summary")
    w("")
    w(f"- **{len(rows)} properties** in the appendix.")
    w(f"- **{touched} touched by the app** "
      f"(read {counts.get('read', 0)}, write {counts.get('write', 0)}, "
      f"read+write {counts.get('read+write', 0)}, referenced {counts.get('referenced', 0)}).")
    w(f"- **{counts.get('comment only', 0)} appear only in comments** - documented findings, not "
      f"live code.")
    w(f"- **{counts.get('untouched', 0)} never touched**, of which {dev_only} are used by a DevTools "
      f"script only (a probe, not a shipped feature).")
    w("")
    w("Classification of everything the app does not touch:")
    w("")
    for tag in ("chain gap", "live state", "candidate feature", "internal/derived", "unclassified"):
        if tag in tags:
            w(f"- **{tag}: {tags[tag]}**")
    w("")
    w("`chain gap` = per-strip properties missing from our chain set (blind spot 1). `live state` =")
    w("changes as the desk is used, so it is read-only telemetry (phase 1). `candidate feature` =")
    w("something we could expose. `internal/derived` = device internals, other products, or")
    w("machinery we deliberately stay away from.")
    w("")

    w("## Shortlist: candidates ranked by accessibility value")
    w("")
    w("The point of the exercise. Everything tagged `high`, then `med`, grouped by the feature it")
    w("would become. Nothing here is agreed work yet.")
    w("")
    short = [r for r in rows if r["value"] in ("high", "med")]
    short.sort(key=lambda r: (VALUE_ORDER[r["value"]], r["note"], r["name"]))
    seen_note = None
    for r in short:
        if r["note"] != seen_note:
            seen_note = r["note"]
            members = [x["name"] for x in short if x["note"] == r["note"]]
            w(f"**{r['value'].upper()} - {r['tag']}** ({len(members)}): "
              f"`{'`, `'.join(members[:8])}`" + (" ..." if len(members) > 8 else ""))
            w("")
            w(f"  {r['note']}")
            w("")
    w("")

    w("## Blind spot 1: the per-strip census")
    w("")
    w("Every x10 (per fader strip) property, checked against the chain set our three chain features")
    w("share - mic presets, profile snapshots and the coming chain-follows-channel swap. Computed")
    w("from `channels.PROCESSING_READ_PROPS`, `PRESET_FLOAT_PROPS`, `PROCESSING_MODULE_BOOLS` and")
    w("`channelBypassProcessing`, so it cannot drift from the code the way a hand-written list does.")
    w("`channelInputSource` is excluded: it IS the layout, not a thing carried by it.")
    w("")
    strip_props = [n for n, _t, occ in props if occ == 10 and n != "channelInputSource"]
    in_chain = sorted(p for p in strip_props if p in CHAIN_SET)
    missing = [p for p in strip_props if p not in CHAIN_SET]
    w(f"**{len(strip_props)} per-strip properties. The chain set covers {len(in_chain)}. "
      f"{len(missing)} are missing from it:**")
    w("")
    w("| Property | Type | Coverage elsewhere | Class | Verdict to establish |")
    w("|---|---|---|---|---|")
    by_name = {r["name"]: r for r in rows}
    for p in missing:
        r = by_name[p]
        tag, value, note = (r["tag"], r["value"], r["note"])
        if tag == "-":
            tag, value, note = classify(p)
        w(f"| `{p}` | {r['type']} | {r['cov']} | {tag} | {note.replace('|', '/')} |")
    w("")
    w("A property being covered elsewhere (a named setter, the preset marker) is NOT the same as")
    w("being in the chain set: only the chain set decides what a preset saves, what a profile")
    w("snapshots, and what a fader move must carry with it.")
    w("")

    w("## Full table")
    w("")
    w("| Property | Type | Occ | Coverage | Class | Value | Where / note |")
    w("|---|---|---|---|---|---|---|")
    for r in sorted(rows, key=lambda r: r["name"].lower()):
        detail = r["where"] if r["cov"] not in ("untouched", "comment only") else r["note"]
        if r["cov"] == "comment only" and r["where"]:
            detail = f"comment at {r['where']}. {r['note']}"
        detail = detail.replace("|", "/")
        w(f"| `{r['name']}` | {r['type']} | x{r['occ']} | {r['cov']} | {r['tag']} | "
          f"{r['value']} | {detail} |")
    w("")
    return "\n".join(out) + "\n", dict(rows=rows, counts=counts, tags=tags, touched=touched)


def main() -> None:
    text, stats = build()
    unclassified = [r["name"] for r in stats["rows"] if r["tag"] == "unclassified"]
    print(f"{len(stats['rows'])} properties; touched {stats['touched']}; "
          f"comment only {stats['counts'].get('comment only', 0)}; "
          f"untouched {stats['counts'].get('untouched', 0)}")
    print("classes: " + ", ".join(f"{k}={v}" for k, v in sorted(stats["tags"].items())))
    if unclassified:
        print(f"UNCLASSIFIED ({len(unclassified)}): {', '.join(unclassified)}")
    if "--check" in sys.argv:
        return
    out = out_path()
    out.write_text(text, encoding="utf-8")
    # relpath, not relative_to: the notes are OUTSIDE this repo, so the answer starts with "..".
    print(f"wrote {Path(os.path.relpath(out, REPO)).as_posix()} ({len(text.splitlines())} lines)")


if __name__ == "__main__":
    main()
