"""Audit: does a PROFILE really back up every setting on the desk?

Generates ProjectMemory/PROFILE_COVERAGE_<DESK>.md. NO DEVICE NEEDED, nothing written to hardware.

  python DevTools/profile_coverage.py            # regenerate the markdown
  python DevTools/profile_coverage.py --check    # print the summary only, write nothing

WHY IT IS A SCRIPT AND NOT A HAND-WRITTEN LIST. A profile claims to be a full backup, and the way
that claim rots is silent: someone adds a writable setting to the app and nobody remembers to add it
to profiles.py, so the profile keeps saving cleanly and just quietly does not carry the new thing.
That is exactly how the virtual fader levels and the per-output headphone type went missing. So the
question is answered MECHANICALLY, from the code, three ways:

  1. WHAT A PROFILE SAVES is read out of the save path itself - the property names in
     profiles.save, channels.read_channels, the matrix readers and the pad readers, plus the
     constant collections those functions iterate. Nothing is hand-listed, so it cannot drift.
  2. WHAT THE APP CAN WRITE is taken from DevTools/property_coverage.py's scan of our whole
     source tree (the same scan that produces the property-coverage report).
  3. THE GAP is (2) minus (1): a setting the app can change but a profile would not bring back.
     Every one is judged in NOT_SAVED below, as either "ok" (a command, editor state or live
     state) or "GAP" (real work to do), and the report lists the GAPs first.

TWO HAND-WRITTEN TABLES, and the script FAILS (exit 1) if either falls behind the code, which is
what stops the audit going stale:
  * RESTORED_BY - for each property a profile SAVES, how it is written back (or why it is
    deliberately save-only). Saving something new forces a decision about restoring it.
  * NOT_SAVED - for each property the app can WRITE but a profile does not save, the verdict.
    Adding a new writable setting forces a decision about backing it up.
"""
from __future__ import annotations
import inspect
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import property_coverage as pc

import channels
import profiles
import rcp_read
import rcp_transfer
import soundpad

REPO = Path(__file__).resolve().parent.parent

# DEVICE-SCOPED for the same reason as PROPERTY_COVERAGE: this report is built on
# pc.read_appendix(), which carries the per-desk occurrence counts. Reuses that module's slug so
# the two reports can never disagree about which desk they describe.
def out_path() -> Path:
    return Path(_bootstrap.notes_path("ProjectMemory", f"PROFILE_COVERAGE_{pc._slug()}.md"))


# ---------------------------------------------------------------------------
# 1. What the SAVE path reads, straight out of the code
# ---------------------------------------------------------------------------
# Every function a profile save runs through. profiles.save takes an already-read model blob, and
# the server hands it channels.read_channels' output and (optionally) soundpad.backup_pads', so the
# save path is the union of all of these.
SAVE_FUNCS = [
    (profiles.save, "profiles.save"),
    (channels.read_channels, "channels.read_channels"),
    (rcp_read.read_matrix_state, "rcp_read.read_matrix_state"),
    (rcp_read.read_matrix_levels, "rcp_read.read_matrix_levels"),
    (rcp_read.read_output_modes, "rcp_read.read_output_modes"),
    # The master Compellor and output delay live in this shared helper rather than in
    # profiles.save, so its source has to be scanned too or all seven read as UNJUDGED - which
    # would report a refactor as a change to what a profile saves.
    (rcp_read.master_processing, "rcp_read.master_processing"),
]
# PADS ARE DIFFERENT and must be listed by hand. soundpad.read_pads_live reads far MORE than
# soundpad.backup_pads keeps: the backup builds its own entry dict and quietly drops the rest, so
# scanning the reader would over-report the backup. This maps each property to the entry KEY it
# survives as. Check it against backup_pads' entry literal when that function changes.
PAD_SAVED = {
    "padIdx": "grid", "padType": "type", "padName": "name", "padColourIndex": "colour",
    "padGain": "gain", "padLoop": "loop", "padReplay": "replay", "padMixerMode": "mixer_mode",
    "padPlayMode": "play_mode", "padMixerTriggerMode": "mixer_trigger",
    "padFilePath": "file (the audio itself is copied into the profile folder)",
    "padMixerBackChannelBluetooth": "back_channel", "padMixerBackChannelCallMe1": "back_channel",
    "padMixerBackChannelCallMe2": "back_channel", "padMixerBackChannelCallMe3": "back_channel",
    "padMixerBackChannelMic2": "back_channel", "padMixerBackChannelMic3": "back_channel",
    "padMixerBackChannelMic4": "back_channel", "padMixerBackChannelUsb1Comms": "back_channel",
    "padMixerBackChannelUsb2Main": "back_channel",
    # The MIDI trigger mapping. backup_pads stores the WHOLE trigger dict (soundpad.TRIGGER_FIELD),
    # including the two fields whose meaning is not settled, so all eight survive as "trigger".
    "padTriggerCustom": "trigger", "padTriggerType": "trigger", "padTriggerControl": "trigger",
    "padTriggerChannel": "trigger", "padTriggerOn": "trigger", "padTriggerOff": "trigger",
    "padTriggerMode": "trigger", "padTriggerSend": "trigger",
    # A sound pad's trim/fade envelope, a Fade pad's times and host exclusion, a Censor pad's own
    # sound. backup_pads keeps each group whole (soundpad.ENV_FIELD / FADE_FIELD / CENSOR_FIELD).
    "padEnvStart": "envelope", "padEnvFadeIn": "envelope", "padEnvFadeOut": "envelope",
    "padEnvStop": "envelope",
    "padMixerFadeInSeconds": "fade", "padMixerFadeOutSeconds": "fade",
    "padMixerFadeExcludeHost": "fade",
    "padMixerCensorCustom": "censor", "padMixerCensorFilePath": "censor",
}
# Collections those functions ITERATE rather than naming property by property. Without these the
# whole processing chain would look unsaved (read_channels says `for p in PROCESSING_READ_PROPS`).
SAVE_CONSTANTS = [
    ("channels.PROCESSING_READ_PROPS", channels.PROCESSING_READ_PROPS),
    ("channels.PROCESSING_MODULE_BOOLS", channels.PROCESSING_MODULE_BOOLS),
    ("channels._CHANNEL_FX_PARAM_SET", channels._CHANNEL_FX_PARAM_SET),
    ("channels.CHAIN_ADVANCED_PROP", [channels.CHAIN_ADVANCED_PROP]),
    ("channels.CHAIN_PRESET_MARKER_PROP", [channels.CHAIN_PRESET_MARKER_PROP]),
    # profiles._unit_settings walks this table rather than naming each property, so without this the
    # unit's own settings (display, buttons, haptics, REC button, listen mode) would look unsaved.
    ("rcp_transfer.UNIT_SETTING_SEL", list(rcp_transfer.UNIT_SETTING_SEL)),
]
# Pads are an OPTIONAL part of a profile ("Include SMART pads"), so they are reported separately:
# a profile saved without the tick genuinely does not carry them, and that is the user's choice.
PAD_SOURCE = "soundpad.backup_pads"


def saved_props(names: list[str]) -> dict[str, list[str]]:
    """{property: [where it is read]} for everything the save path touches."""
    found: dict[str, list[str]] = {}
    pats = {n: pc._pat(n) for n in names if n not in pc.FALSE_POSITIVES}
    for fn, label in SAVE_FUNCS:
        src = inspect.getsource(fn)
        for n, pat in pats.items():
            if pat.search(src):
                found.setdefault(n, []).append(label)
    for label, coll in SAVE_CONSTANTS:
        for n in coll:
            if n in pats:
                found.setdefault(n, []).append(label)
    for n in PAD_SAVED:
        if n in pats:
            found.setdefault(n, []).append(PAD_SOURCE)
    return found


# ---------------------------------------------------------------------------
# 2. What a restore writes back (the one hand-written table)
# ---------------------------------------------------------------------------
# {property: how it is restored}. "SAVE ONLY: <reason>" means deliberately not written back.
RESTORED_BY = {
    # --- layout, levels -------------------------------------------------------------------
    "channelInputSource": "profiles._apply_v2, first of all (the layout decides where every "
                          "per-strip write lands). Gated on the fader_write cap.",
    "faderLevel": "profiles._apply_v2, straight after the layout. Only the slots this device has a "
                  "confirmed selector for (FaderSpec.level_selectors); gated on the level_write cap.",
    "outputMonLevel": "profiles._apply_v2 (rcp_transfer.set_monitor_level), gated on level_write.",
    # --- per channel ----------------------------------------------------------------------
    "inputMicrophoneGain": ("profiles._apply_v2 -> channels.set_mic_gain, written AFTER the mic "
                            "type: selecting a voicing sets that voicing's own default gain (SM7B "
                            "55, Condenser 25, ...), so the old gain-first order lost it on any "
                            "restore that changed the voicing."),
    "inputDigitalGain": "profiles._apply_v2 -> channels.set_channel_level (line channels).",
    "inputPower": "profiles._apply_v2 -> channels.set_phantom.",
    "inputPhaseFlip": "profiles._apply_v2 -> channels.set_polarity.",
    "inputMicrophoneType": ("profiles._apply_v2 -> channels.set_mic_type, FIRST of the mic block "
                            "for the reason in the inputMicrophoneGain row."),
    "channelBypassProcessing": "profiles._apply_v2 -> channels.set_processing_enabled.",
    "channelAdvancedProcessing": "profiles._apply_v2 -> channels.set_channel_advanced, written "
                                 "FIRST of the chain (simple mode re-derives the rest).",
    "channelCurrentFxPreset": "profiles._apply_v2 -> channels.set_channel_preset_marker, written "
                              "LAST (any parameter write blanks it to -1).",
    "echoOn": "profiles._apply_v2 -> channels.set_channel_effect.",
    "reverbOn": "profiles._apply_v2 -> channels.set_channel_effect.",
    # --- master / output block ------------------------------------------------------------
    "systemMidiControl": "profiles._apply_v2 -> rcp_transfer.set_midi_control.",
    "recordingMultitrackMode": "profiles._apply_v2, gated on the output_write cap.",
    # The rest of the "what do I end up with" set: both multitrack destinations and both raw
    # switches. Saving a mode without its bypass backs up half a decision, and a profile taken on
    # raw pre-fader tracks then comes back processed, which is not recoverable after the take.
    "recordingProcessingBypass": "profiles._apply_v2, gated on output_write "
                                 "(rcp_transfer.set_recording_processing_bypass).",
    # WAV or MP3. MEMBERSHIP-CHECKED against rcp_transfer.RECORDING_FORMATS on the way back out,
    # unlike most of this list, because the desk stores out-of-range values for this one: a profile
    # captured from a desk left on a 3 must not write that 3 back.
    "recordingCompressionQuality": "profiles._apply_v2, gated on output_write "
                                   "(rcp_transfer.set_recording_format), and only for a value in "
                                   "RECORDING_FORMATS.",
    "outputMultiMode": "profiles._apply_v2, gated on output_write "
                       "(rcp_transfer.set_usb_multitrack_mode). The USB destination's own "
                       "mode; it lives on Routing > USB audio, not the Recording page. Saved and "
                       "restored as the TWO settings it carries, usb_multitrack plus usb_expanded, "
                       "composed into one write.",
    "outputMultiBypass": "profiles._apply_v2, gated on output_write "
                         "(rcp_transfer.set_usb_multitrack_bypass).",
    "outputMonAutoMute": "profiles._apply_v2, gated on output_write.",
    "outputBTAutoMute": "profiles._apply_v2, gated on output_write.",
    "headphoneType": "profiles._apply_v2, one write per addressable output, value validated "
                     "against the device's HeadphoneTypeSpec. Gated on output_write.",
    "masterCompellorOn": "profiles._apply_v2, gated on output_write.",
    "masterCompellorThreshold": "profiles._apply_v2, gated on output_write.",
    "masterCompellorAttack": "profiles._apply_v2, gated on output_write.",
    "masterCompellorRelease": "profiles._apply_v2, gated on output_write.",
    "masterCompellorGain": "profiles._apply_v2, gated on output_write.",
    "masterDelayOn": "profiles._apply_v2, gated on output_write.",
    "masterDelaySeconds": "profiles._apply_v2 (written with masterDelayOn).",
    # --- the routing matrix ---------------------------------------------------------------
    "outputMixMinus": "server._apply_matrix -> rcp_transfer.set_output_routing, per output.",
    "mixDisabled": "server._apply_matrix -> rcp_transfer.set_cross_point_excluded (the Custom "
                   "mixes' exclusions).",
    "mixLevelWithAnchor": "server._apply_matrix, through routing_schemes.apply: EVERY output's "
                          "cross-points, not the monitor ones only. Each "
                          "source is re-included (which re-links it), then any saved manual level "
                          "is written back.",
    # --- SMART pads (only when 'Include SMART pads' was ticked) ----------------------------
    "padIdx": "soundpad.restore_pads rebuilds each pad at its absolute grid position.",
    "padName": "soundpad.restore_pads.",
    "padGain": "soundpad.restore_pads.",
    "padLoop": "soundpad.restore_pads.",
    "padReplay": "soundpad.restore_pads.",
    "padColourIndex": "soundpad.restore_pads.",
    "padMixerMode": "soundpad.restore_pads (the pad's action type).",
    "padFilePath": "soundpad.restore_pads re-uploads the sound file copied into the profile folder.",
    "padType": "soundpad.restore_pads (sound / voice FX / mixer action).",
    "padPlayMode": "soundpad.restore_pads -> set_play_mode, written BEFORE loop (selecting One "
                   "Shot clears padLoop).",
    "padMixerTriggerMode": "soundpad.restore_pads -> set_mixer_trigger (latching / momentary).",
    "padTriggerCustom": "soundpad.restore_pads -> set_trigger_mapping, written with the numbers it "
                        "gates (the desk ignores them without it).",
    "padTriggerType": "soundpad.restore_pads -> set_trigger_mapping (Control Change or Note), when "
                      "the saved pad had a custom mapping.",
    "padTriggerControl": "soundpad.restore_pads -> set_trigger_mapping (the CC or note number).",
    "padTriggerChannel": "soundpad.restore_pads -> set_trigger_mapping (MIDI channel).",
    "padTriggerOn": "soundpad.restore_pads -> set_trigger_mapping (value sent when pressed).",
    "padTriggerOff": "soundpad.restore_pads -> set_trigger_mapping (value sent when released).",
    "padTriggerMode": "soundpad.restore_pads -> set_trigger_mapping. Written whether or not the "
                      "mapping is custom, because latching versus momentary is proven to change what "
                      "the pad sends and we have not proven the custom flag gates it.",
    "inputInstrumentGain": "profiles._apply_v2 -> channels.set_instrument_gain.",
    "inputType": "SAVE ONLY, and correctly so: it is DERIVED from inputMicrophoneType (proven "
                 "on Mic 3 - Line-in gives 2, Instrument gives 1, any mic voicing gives "
                 "0), so restoring the mic type brings the input path back on its own. Writing it "
                 "as well could only produce a combination the desk's own UI cannot make.",
    "inputColour": "SAVE ONLY: the strip colour is a STRING of AARRGGBB hex (decoded) and "
                   "there is no string channel-property writer yet, so it is read and backed up but "
                   "cannot be put back. There is no string channel-property writer rather than "
                   "int()-ing a colour.",
    "padEnvStart": "soundpad.restore_pads -> set_pad_properties, the whole envelope in one session. "
                   "The four floats are POSITIONS through the sound, 0-1, ear-proven.",
    "padEnvFadeIn": "soundpad.restore_pads (with the rest of the envelope).",
    "padEnvFadeOut": "soundpad.restore_pads (with the rest of the envelope).",
    "padEnvStop": "soundpad.restore_pads (with the rest of the envelope).",
    "padMixerFadeInSeconds": "soundpad.restore_pads -> set_pad_properties, Fade pads only.",
    "padMixerFadeOutSeconds": "soundpad.restore_pads -> set_pad_properties, Fade pads only.",
    "padMixerFadeExcludeHost": "soundpad.restore_pads -> set_pad_properties, Fade pads only.",
    "padMixerCensorCustom": "soundpad.restore_pads -> set_pad_properties, Censor pads only.",
    "padMixerCensorFilePath": "SAVE ONLY: the path is recorded, but restoring it would point a pad "
                              "at audio that may not be on the unit, and putting a custom censor "
                              "sound there is a file upload rather than a setting. The flag above is "
                              "restored, so a pad comes back on your own sound if that sound is "
                              "still on the unit.",
    "padTriggerSend": "SAVE ONLY: read and kept in the profile, but never written back. The create "
                      "template stamps it 2 and no test has shown what it does, so restoring a value "
                      "we do not understand would be a guess with a pad's behaviour attached.",
    "padMixerBackChannelBluetooth": "soundpad.restore_pads -> set_back_channel_member, every "
                                    "member written so the restore is authoritative, not additive.",
    "padMixerBackChannelCallMe1": "soundpad.restore_pads -> set_back_channel_member.",
    "padMixerBackChannelCallMe2": "soundpad.restore_pads -> set_back_channel_member.",
    "padMixerBackChannelCallMe3": "soundpad.restore_pads -> set_back_channel_member.",
    "padMixerBackChannelMic2": "soundpad.restore_pads -> set_back_channel_member.",
    "padMixerBackChannelMic3": "soundpad.restore_pads -> set_back_channel_member.",
    "padMixerBackChannelMic4": "soundpad.restore_pads -> set_back_channel_member.",
    "padMixerBackChannelUsb1Comms": "soundpad.restore_pads -> set_back_channel_member.",
    "padMixerBackChannelUsb2Main": "soundpad.restore_pads -> set_back_channel_member.",
    # --- recorded for reference, deliberately never written back ---------------------------
    "wifiSSID": "SAVE ONLY: recorded so a profile says which network the desk was on. Joining a "
                "network is not a write we have decoded, and the PSK must never be stored.",
    "wifiIpAddress": "SAVE ONLY: reference only; the address is assigned by the network.",
    "systemFirmwareVersion": "SAVE ONLY: stamped so a restore can be judged against the firmware "
                             "it was taken from.",
}

# Collections restored as a group, so each member does not need its own line above.
RESTORED_GROUPS = [
    (set(channels.PRESET_FLOAT_PROPS),
     "profiles._apply_v2 -> channels.set_channel_processing, per channel. Written in chain order "
     "(VoxLab knobs before the values simple mode derives from them)."),
    (set(channels.PROCESSING_MODULE_BOOLS),
     "profiles._apply_v2 -> channels.set_processing_module (it accepts the raw <module>On name)."),
    (set(channels._CHANNEL_FX_PARAM_SET),
     "profiles._apply_v2 -> channels.set_channel_effect_param (echo + reverb, mic channels)."),
    (set(rcp_transfer.UNIT_SETTING_SEL),
     "profiles._apply_v2 -> rcp_transfer.set_unit_setting, last of all (nothing else depends on "
     "them). Only names in the address table are written, so a profile from a newer build carrying "
     "a setting this build does not know is skipped. Gated on the unit_settings cap."),
]


def restored_by(name: str) -> str:
    if name in RESTORED_BY:
        return RESTORED_BY[name]
    for members, how in RESTORED_GROUPS:
        if name in members:
            return how
    return ""

# Every property the app can WRITE but a profile does not save, with a verdict. Anything writable
# and unsaved that is not listed here fails the run, so a new writable setting cannot be added to
# the app without someone deciding whether a profile should carry it.
#   verdict "ok"  - correctly not saved (a command, editor state, or live state)
#   verdict "GAP" - a real setting a profile should carry. Listed in the report as work to do.
NOT_SAVED = {
    # ---- not settings at all: commands, transport, editor state ---------------------------
    "requestDeleteUID": ("ok", "A COMMAND, not state: 'delete the recording with this id'. "
                              "It self-clears when the desk consumes it, and restoring a "
                              "number here would delete somebody's take. It IS writable, which is "
                              "exactly why the verdict has to be deliberate rather than absent."),
    # ---- the desk's OWN show backup, all four writable ------------------------------------
    # All four are commands, and the verdicts have to be deliberate rather than absent because one
    # of them is a factory reset: a profile that "restored" it would wipe the desk it was restoring.
    "showControlExport": ("ok", "A COMMAND: 'back the desk up now'. It self-clears, and a backup "
                                "is an event rather than a setting, so there is nothing to put "
                                "back. Offered as a button on the Profiles page instead."),
    "showControlImport": ("ok", "A COMMAND taking a show's uid: 'load this backup'. Restoring a "
                                "number here would replace the whole desk with whatever backup "
                                "happened to sit at that position, which is the opposite of what "
                                "the profile being restored is asking for."),
    "showControlNewFromDefault": ("ok", "A COMMAND, and it is THE FACTORY RESET. Writing it back "
                                        "as part of a restore would erase every setting the "
                                        "restore had just written. The clean-restore option calls "
                                        "it deliberately, before the profile is applied, never "
                                        "from the saved values."),
    "showControlDelete": ("ok", "A COMMAND taking a show's uid: 'delete this backup'. Restoring a "
                                "number here would destroy a backup, and uids are positional so it "
                                "would not even be reliably the same one."),
    "wifiScan": ("ok", "A COMMAND: 'scan for networks now'. What it produces is the ten result "
                       "slots, which are a transient reading of the room rather than a setting, "
                       "and are worthless a day later."),
    "mixLinkRequest": ("ok", "A COMMAND, not state: 'link this cross-point'. The state it produces "
                             "is mixLevelWithAnchor, which a profile does save."),
    "mixUnlinkRequest": ("ok", "The other half of the same command."),
    "remountPadStorage": ("ok", "Transport command used to mount the pad volume for a file copy."),
    "transferModeType": ("ok", "Transport command: puts the desk into transfer mode."),
    "padProgress": ("ok", "Upload progress reported during a pad write."),
    "padIsInternal": ("ok", "Set by the create path to mark a pad's audio as on-device; not a "
                            "user setting."),
    "effectsIdx": ("ok", "Which effect slot the editor is working on; app-side selection."),
    "padActive": ("ok", "Whether a pad is latched RIGHT NOW. Live state - restoring it would fire "
                        "a pad."),
    "channelOutputMute": ("ok", "Live MUTE state, not a setting. We CAN write it now (a mute button "
                                "on every channel), which is exactly why the verdict "
                                "has to be deliberate: restoring a saved mute would silence a "
                                "channel at the worst possible moment. Saved in the full snapshot "
                                "for the record, never written back."),
    "channelCueEnable": ("ok", "Live SOLO (Listen) state, not a setting. It IS writable, and the "
                               "reason for NOT restoring it is stronger than the mute's: a "
                               "restored solo would leave the headphones carrying one channel with "
                               "nothing on screen to explain it, which is precisely the state a "
                               "blind operator cannot spot. Saved for the record, never written "
                               "back."),
    "systemChannelSelected": ("ok", "Which channel the desk has selected: the operator changes it "
                                    "with a button. We write it only to FREE the encoder."),
    "requestRecordState": ("ok", "A COMMAND, not state: start / pause / stop the recorder. It is "
                                 "self-clearing and must never be read for status (recordState is "
                                 "the status). Restoring it would start or stop a recording."),
    "requestDropMarker": ("ok", "A COMMAND: drop a cue point into the take being recorded. Nothing "
                                "to restore, and firing it on restore would mark a stranger's file."),
    # ---- network: writable, and deliberately outside a profile -------------------------------
    "btVisible": ("ok", "Bluetooth discoverability. It IS writable, and that is why the "
                        "verdict has to be deliberate: it is a momentary state you turn on to pair "
                        "a phone, so restoring a saved True would silently advertise the desk to "
                        "the room. Turned on from the Wireless connections tab when it is wanted."),
    "wifiPSK": ("ok", "THE WIFI PASSWORD, and the rule is absolute: it stays out of profiles, the "
                      "API, the browser and every log line. It is READABLE off the desk, which is "
                      "exactly why a profile must not carry it - a backup file on the PC would be "
                      "a plaintext copy of Shaun's network key. wifiSSID is not saved either, "
                      "since an SSID with no key restores nothing usable."),
    # ---- SIP / CallMe: only meaningful during a call, and several hold credentials ---------
    "padSIPCallSlot": ("ok", "CallMe pad plumbing; set by the create path from the pad's type."),
    "padSIPPhoneBookEntry": ("ok", "CallMe phone-book link; SIP-only and credential-adjacent."),
    # ---- per-channel registry entries with no UI and unconfirmed semantics -----------------
    "inputInstrumentGain": ("GAP", "Instrument input gain. A real per-channel setting, writable "
                                   "through channels.CHANNEL_PROP, but with no control in our UI "
                                   "and never read back, so a profile does not carry it."),
    "inputColour": ("GAP", "Channel strip colour. Writable through CHANNEL_PROP but its ENCODING "
                           "is unconfirmed, so it is neither exposed nor saved. Worth SAVING for "
                           "reference even before we dare write it."),
    "inputType": ("GAP", "Per-channel input type. Same position as inputColour: writable in the "
                         "registry, values unconfirmed, not saved."),
    "channelWirelessMute": ("ok", "Mute for a wireless mic on the channel: live state, and it "
                                  "belongs to a mic that may not be paired at restore time."),
    # ---- SMART pad settings the backup does not keep ---------------------------------------
    # ---- pad sub-settings our CREATE path writes from defaults ------------------------------
    "padEnvStart": ("GAP", "Pad trim IN point. Our create path writes a default, so a trimmed pad "
                           "loses its trim on restore. Settable on the desk, not in our UI."),
    "padEnvStop": ("GAP", "Pad trim OUT point, as above."),
    "padEnvFadeIn": ("GAP", "Pad fade-in, as above."),
    "padEnvFadeOut": ("GAP", "Pad fade-out, as above."),
    "padMixerFadeInSeconds": ("GAP", "Fade time for a fade/ducking pad; create-path default."),
    "padMixerFadeOutSeconds": ("GAP", "Fade time for a fade/ducking pad; create-path default."),
    "padMixerFadeExcludeHost": ("GAP", "Whether a fade pad excludes the host; create-path default."),
    "padMixerCensorCustom": ("GAP", "Whether a Censor pad uses a custom sound; create-path default."),
    "padMixerCensorFilePath": ("GAP", "The custom Censor sound's file; create-path default."),
    "padTriggerMode": ("GAP", "Per-pad MIDI/trigger mapping. PARITY.md lists MIDI mapping "
                              "as part of a full pad backup; the backup does not keep it."),
    "padTriggerType": ("GAP", "Per-pad trigger mapping, as above."),
    "padTriggerSend": ("GAP", "Per-pad trigger mapping, as above."),
    "padTriggerCustom": ("GAP", "Per-pad trigger mapping, as above."),
    "padTriggerControl": ("GAP", "Per-pad trigger mapping, as above."),
    "padTriggerChannel": ("GAP", "Per-pad trigger mapping, as above."),
    "padTriggerOn": ("GAP", "Per-pad trigger mapping, as above."),
    "padTriggerOff": ("GAP", "Per-pad trigger mapping, as above."),
    "padEffectInput": ("ok", "Which mic a voice-FX pad processes; written by the create path from "
                             "the recipe. Carried by fx_stack, not as a property."),
    "padEffectTriggerMode": ("ok", "Voice-FX trigger mode; written by the create path from the "
                                   "recipe."),
    # ---- voice-FX engine parameters: carried by the RECIPE, not property by property --------
    "distortionOn": ("ok", "Voice-FX engine parameter. A pad backup stores the effect's RECIPE "
                           "(name + full stack), and restore rebuilds the engine from it. CAVEAT: "
                           "a pad whose effect was EDITED IN PLACE comes back at the recipe's "
                           "values, not the edited ones."),
    "distortionIntensity": ("ok", "Voice-FX engine parameter; carried by the recipe."),
    "pitchShiftOn": ("ok", "Voice-FX engine parameter; carried by the recipe."),
    "pitchShiftSemitones": ("ok", "Voice-FX engine parameter; carried by the recipe."),
    "robotOn": ("ok", "Voice-FX engine parameter; carried by the recipe."),
    "robotMix": ("ok", "Voice-FX engine parameter; carried by the recipe."),
    "robotLevel": ("ok", "Voice-FX engine parameter; carried by the recipe."),
    "voiceDisguiseOn": ("ok", "Voice-FX engine parameter; carried by the recipe."),
}


# ---------------------------------------------------------------------------
# 3. Report
# ---------------------------------------------------------------------------
def build() -> tuple[str, dict]:
    props = pc.read_appendix()
    names = [n for n, _t, _c in props]
    app = pc.scan(pc.APP_GLOBS, names)
    saved = saved_props(names)

    by_type = {n: t for n, t, _c in props}
    occ = {n: c for n, _t, c in props}

    rows = []
    for name in names:
        cov = pc.coverage_of(app.get(name))
        where = saved.get(name, [])
        pad_only = bool(where) and all(w == PAD_SOURCE for w in where)
        rows.append(dict(
            name=name, type=by_type[name], occ=occ[name], cov=cov,
            saved=bool(where), where=where, pad_only=pad_only,
            restored=restored_by(name),
        ))

    writable = [r for r in rows if r["cov"] in ("write", "read+write")]
    saved_rows = [r for r in rows if r["saved"]]
    unsaved_writable = [r for r in writable if not r["saved"]]
    undeclared = [r["name"] for r in saved_rows if not r["restored"]]
    unjudged = [r["name"] for r in unsaved_writable if r["name"] not in NOT_SAVED]
    gaps = [r for r in unsaved_writable if NOT_SAVED.get(r["name"], ("", ""))[0] == "GAP"]
    fine = [r for r in unsaved_writable if NOT_SAVED.get(r["name"], ("", ""))[0] == "ok"]

    # Settings we cannot restore because we cannot write them at all, ranked by what they'd be worth.
    unwritable = []
    for r in rows:
        if r["saved"] or r["cov"] in ("write", "read+write"):
            continue
        tag, value, note = pc.classify(r["name"])
        if tag == "candidate feature" and value in ("high", "med"):
            unwritable.append((r["name"], value, note))

    out = []
    w = out.append
    w("# Profile coverage: is a profile really a full backup?")
    w("")
    w("GENERATED FILE - do not hand-edit. Regenerate with `python DevTools/profile_coverage.py`")
    w("(no device needed). It reads the SAVE PATH ITSELF for what a profile records, so this file")
    w("cannot drift from the code the way a hand-written list does. Companion to")
    w("`PROPERTY_COVERAGE_<DESK>.md`, which asks the wider question of what our code touches at all.")
    w("")
    w("## The short answer")
    w("")
    w(f"- A profile records **{len(saved_rows)} of the {len(rows)} device properties**, of which "
      f"**{sum(1 for r in saved_rows if r['pad_only'])}** come from the optional SMART pad backup.")
    w(f"- The app can WRITE {len(writable)} properties. {len(writable) - len(unsaved_writable)} of "
      f"those are saved; **{len(gaps)} are a real gap** and {len(fine)} are correctly left out "
      f"(commands, editor state, live state, or carried some other way).")
    w("- The SETTINGS half of a profile (channels, processing, routing, master outputs) is "
      "complete. **Every gap below is in the SMART PAD backup**, bar three per-channel registry "
      "entries with no UI.")
    w("- **THIS FILE COUNTS PROPERTY NAMES, NOT OCCURRENCES, so a per-STRIP gap can hide in it.** "
      "Channel processing is per fader strip and the desk has 10 of them; a profile carries the "
      "channels the UI exposes, and a strip left out would still show every one of its properties "
      "ticked as saved, because the same NAME is saved for the other strips. That is how the SMART "
      "pads bus hid as unsaved, until a clean restore left its custom chain at factory values. It "
      "is a channel now (9 on the Pro II). **CallMe1 is the one still "
      "out**: the tenth strip resolves to source 9, which USB 2 already claims, so it needs a "
      "capture before it can be addressed.")
    w("- **Every property below, gap or not, is nonetheless RECORDED**: a profile also carries a "
      "snapshot of the whole model dump (`device_snapshot`, ~55 KB, secrets "
      "redacted). So nothing is lost from the backup FILE. What the gaps below cost is the ability "
      "to put a setting BACK, and a profile saved today becomes restorable retrospectively as each "
      "one is decoded.")
    w("- The honest limit: restore can only write what the app can write, and there is a large set "
      "of real desk settings we cannot write yet (last section).")
    w("")

    if gaps:
        w("## GAPS: settings the app can change that a profile does not carry")
        w("")
        w("| Property | Occ | What is lost |")
        w("|---|---|---|")
        for r in sorted(gaps, key=lambda r: r["name"].lower()):
            w(f"| `{r['name']}` | x{r['occ']} | {NOT_SAVED[r['name']][1].replace('|', '/')} |")
        w("")

    w("## Writable but correctly not saved")
    w("")
    w("| Property | Why not |")
    w("|---|---|")
    for r in sorted(fine, key=lambda r: r["name"].lower()):
        w(f"| `{r['name']}` | {NOT_SAVED[r['name']][1].replace('|', '/')} |")
    w("")

    w("## What a profile saves, and how each one is restored")
    w("")
    w("`pads` marks a property that comes from the OPTIONAL SMART pad backup, so it is only in a")
    w("profile saved with \"Include SMART pads\" ticked. `SAVE ONLY` marks something recorded for")
    w("reference that is deliberately never written back.")
    w("")
    w("| Property | Occ | Read by | Restored by |")
    w("|---|---|---|---|")
    for r in sorted(saved_rows, key=lambda r: (r["pad_only"], r["name"].lower())):
        src = "pads" if r["pad_only"] else ", ".join(sorted(set(r["where"]))[:2])
        w(f"| `{r['name']}` | x{r['occ']} | {src} | {r['restored'].replace('|', '/')} |")
    w("")

    w("## The honest limit: settings no profile can restore, because we cannot write them")
    w("")
    w("These are real desk settings with no writer in our code, so a profile neither saves nor")
    w("restores them. Most need one capture of their write selector; some are touchscreen-only.")
    w("Ranked by what they would be worth to a blind operator (from the property-coverage report).")
    w("")
    w("| Property | Value | What it is |")
    w("|---|---|---|")
    for name, value, note in sorted(unwritable, key=lambda x: (pc.VALUE_ORDER[x[1]], x[0].lower())):
        w(f"| `{name}` | {value} | {note.replace('|', '/')} |")
    w("")
    return "\n".join(out) + "\n", dict(rows=rows, saved=saved_rows, writable=writable,
                                       gaps=gaps, fine=fine, undeclared=undeclared,
                                       unjudged=unjudged, unwritable=unwritable)


def main() -> None:
    text, s = build()
    print(f"{len(s['rows'])} properties; a profile saves {len(s['saved'])}; "
          f"the app can write {len(s['writable'])}")
    print(f"REAL GAPS (writable settings a profile drops): {len(s['gaps'])} "
          + (", ".join(r["name"] for r in s["gaps"]) if s["gaps"] else "(none)"))
    print(f"writable but correctly not saved: {len(s['fine'])}")
    print(f"settings we cannot write at all (high/med value): {len(s['unwritable'])}")
    if "--check" not in sys.argv:
        out = out_path()
        out.write_text(text, encoding="utf-8")
        # relpath, not relative_to: the notes are OUTSIDE this repo, so the answer starts with "..".
        print(f"wrote {Path(os.path.relpath(out, REPO)).as_posix()} ({len(text.splitlines())} lines)")
    # These two are the drift guards: a property saved with no restore verdict, or writable with
    # no verdict at all, means someone changed the code without deciding what a profile should do.
    if s["undeclared"]:
        print(f"UNDECLARED - saved but no entry in RESTORED_BY: {', '.join(s['undeclared'])}")
    if s["unjudged"]:
        print(f"UNJUDGED - writable, unsaved, no entry in NOT_SAVED: {', '.join(s['unjudged'])}")
    if s["undeclared"] or s["unjudged"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
