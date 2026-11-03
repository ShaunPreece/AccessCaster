"""Classify EVERY device property against the app's code.

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
     below with a one-line reason each, not something a script can infer. When one is settled,
     edit the rule and regenerate.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # puts Project/ on sys.path, and finds docs/ and the notes

REPO = Path(__file__).resolve().parent.parent
# The property list is read from docs/ in THIS repo; the reports below are notes, so they are written
# to the private sibling repo instead.

# DEVICE-SCOPED, because this report consumes the OCCURRENCE COUNTS and those genuinely differ
# between the desks (Duo PAD x22 vs Pro II x24, PADBUTTON x48 vs x64 - the physical shape), and so
# do the names each desk's firmware publishes. Still no hardware required: get_active_device()
# falls back to the Pro II when nothing is plugged in.
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
# desk. NO DEVICE NEEDED - it reads the generated file, not the desk. Do not point it at a
# hand-frozen appendix: a flat walk of the dump truncates EFFECTS_PARAMETERS to PARAMETERS, invents
# a YPOT and misses five nested container blocks, so coverage is measured against a wrong universe.
_LIST_ROW = re.compile(r"^- `([A-Za-z]\w*)` - ([\w/?]+), x(\d+)\s*$")


def read_property_list() -> list[tuple[str, str, int]]:
    """(name, type, occurrences) for every property the desk publishes, from the generated
    `docs/PROPERTY_LIST_<DESK>.md`."""
    path = list_path()
    if not path.exists():
        raise SystemExit(f"docs/{path.name} is missing. "
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
READ_CALL = re.compile(r"find_all|\bvalues\(|\bvec\(|\bfirst\(|read_model|path_value|keyed_value|"
                       r"block_value|props\.get")
# A line that builds or sends a record to the device.
WRITE_CALL = re.compile(r"build_\w+|write_at|send_record|send_records_paced")

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
    "BACK_CHANNEL_SOURCES": "write",
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
    # sent as raw record bytes by rcp_transfer.remount_pad_storage, not through a builder
    "remountPadStorage": "write",
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


# A module-level constant holding a property's name (`WIRELESS_MUTE_PROP = "channelWirelessMute"`).
# Scanning for the literal alone finds only the constant's definition and under-reports the
# property as merely "referenced", so a use of the constant counts as a use of the property.
_NAME_CONSTANT = re.compile(r"""^([A-Z][A-Z0-9_]*)\s*=\s*["']([A-Za-z]\w*)["']\s*(?:#.*)?$""")


def name_aliases(names) -> dict[str, list[str]]:
    """{property: [CONSTANT, ...]} for every module-level constant in Project/*.py that holds one
    of `names`."""
    known = set(names)
    out: dict[str, list[str]] = {}
    for path in sorted((REPO / "Project").glob("*.py")):
        for line in path.read_text(encoding="utf-8").splitlines():
            m = _NAME_CONSTANT.match(line)
            if m and m.group(2) in known:
                out.setdefault(m.group(2), []).append(m.group(1))
    return out


_WORD = re.compile(r"\w+")
_QUOTED = re.compile(r"""["'`](\w+)(?=["'`])""")


class NameMatcher:
    """Which of a list of property names a piece of text references, found in one pass over it.

    A long name counts as a whole word, and so does a constant holding it (`name_aliases`). A
    SHORT_NAME one counts only as a string literal, because short names collide with ordinary words.
    """

    def __init__(self, names):
        self.words: dict[str, str] = {}
        self.quoted: dict[str, str] = {}
        for name in names:
            if name not in FALSE_POSITIVES:
                (self.quoted if len(name) <= SHORT_NAME else self.words)[name] = name
        for name, constants in name_aliases(names).items():
            if name not in FALSE_POSITIVES:
                self.words.update((c, name) for c in constants)

    def names_in(self, text: str) -> set[str]:
        found = {self.words[w] for w in _WORD.findall(text) if w in self.words}
        found.update(self.quoted[q] for q in _QUOTED.findall(text) if q in self.quoted)
        return found


def scan(globs: list[str], names: list[str]) -> dict[str, dict]:
    """{property: {"roles": set, "sites": [(file, line, role)], "code": int, "comment": int}}."""
    matcher = NameMatcher(names)
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
                for name in sorted(matcher.names_in(line)):
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
    (r"^hpf(Higher|Lower)On$", "chain gap", "med",
     "Per-strip HPF variants with no app control found yet. Confirm before including in the chain set."),
    (r"^streamerXPreset(Created|Name)$", "chain gap", "low",
     "Per-strip but Streamer X naming; expected irrelevant to Pro II/Duo, confirm and dismiss."),
    (r"^(channelListenSource|channelTalkbackEnable)$", "live state", "med",
     "Per-strip LIVE STATE, not chain. Phase 2 shows whether the RODE app moves it on a fader drag; "
     "do not guess."),

    (r"^duckerDepth$", "candidate feature", "med",
     "Auto-duck depth. Read ad hoc during the bed-gating investigation, never exposed as a control."),

    # ---- metering and physical control position: the accessibility headline --------------
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
     "NOT the per-cross-point link state: it "
     "reads True on 508 of the 516 cells while the proven link inference (level == anchor in "
     "mixLevelWithAnchor) says 114 linked, agreeing on only 112. So it is some other per-cell flag "
     "(what CAN be linked, or a stereo pairing), and reading it as link state would misreport the "
     "matrix. Keep inferring link from mixLevelWithAnchor, which is ear- and read-back-proven."),
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
    (r"^icecast", "candidate feature", "med",
     "Streaming the mix to an Icecast server: address, port, mountpoint, username, password, "
     "bitrate, TLS, a connect command and its state. Not decoded or exercised. The password, like "
     "the wifi key, must never reach a profile, a log or the browser."),
    (r"^proxy", "candidate feature", "low",
     "The desk's network proxy: on/off, address, port and credentials. Not decoded; a set-up step "
     "few desks need, and the password must never reach a profile, a log or the browser."),
    (r"^(requestSignIn|requestSignOut|hasValidToken|userCode|userName|verificationURL|"
     r"verificationURLBare|responsePending|errorMessage)$", "candidate feature", "low",
     "The desk's RODE account sign-in: the commands, the code and address the touchscreen shows "
     "for finishing a sign-in in a browser, and the outcome. Not decoded; a one-off set-up step."),
    (r"^cloudUpload(Request|Cancel)UID$", "internal/derived", "-",
     "Commands to upload a recording to RODE's cloud or cancel one, which need the account above. "
     "Out of scope with it."),
    (r"^show(Icon|StorageType)$", "internal/derived", "-",
     "Per-show metadata in the desk's show list. The app lists and restores shows by name and "
     "snapshot and needs neither."),
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
    props = read_property_list()
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
    w(f"Source of the property list: `docs/{list_path().name}`, generated from this desk's own")
    w("model dump by `DevTools/property_list.py`.")
    w("")
    w("## Summary")
    w("")
    w(f"- **{len(rows)} properties** in the list.")
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
    w("share - mic presets, profile snapshots and the chain-follows-channel swap. Computed")
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
