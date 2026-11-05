"""
AccessCaster - the service: everything the app knows about and does to the desk.

A screen-reader and keyboard friendly controller for the RODECaster Pro II and Duo. It talks to
the unit over the same USB control channel RODE's own app uses, and every control shows the
unit's real state: one command makes the desk stream its whole property model, which rcp_read
parses.

The window (`native/`) calls the read_* and action_* functions here directly, on worker threads.
Each section that runs something in the background has a `start_*` (and `stop_*` where it holds
something), which `service.py` calls; `tray.py` is the entry point.
"""

from __future__ import annotations

import datetime
import threading

import announcer  # speaks the desk's button presses aloud (_AnnouncerHooks)
import change_words  # a change on the desk in words
import channels
import devices  # per-device model (matrix labels, caps) keyed by product id
import device_page  # the Device page, and the connected desk's USB serial
import desk_access  # the lock, the desk as it is now, the capability gate, the long job
import desk_firmware  # the desk's firmware version and the update it offers
import desk_live  # the one listener: everything the desk pushes, to every subscriber
import desk_model  # the desk as last seen, kept current by the listener and our own writes
import diagnostics  # the app's own account of what it did, in %LOCALAPPDATA%\\...\\logs
import faders  # the fader layout and stereo links, and the Levels page's writes
import master_outputs  # the auto-mutes, headphone types, master Compellor and output delay
import mic_presets  # mic processing presets: factory and saved, applying one, which a strip is on
import network  # wifi and Bluetooth, and the wireless mics
import output_mixes  # the output mixes: modes, cross-points, monitor levels and mix schemes
import pad_actions  # the SMART pads: live edits, sounds, voice effects and their preview
import pad_shortcuts  # firing pads and stepping the bank from a global hotkey
import pan_link  # one Pan control for both legs of a stereo channel
import profiles  # settings backup/restore (save+restore live here, not in server)
import rcp_read
import rcp_transfer
import recorder  # the transport: start/pause/stop, storage volumes, recordings
import rode_app  # whether RODE's own app is running, which the desk does not report
import settings_file  # settings.json, read and merged
import shows  # the DESK'S own backup: export/import/factory reset (not profiles)
import soundpad  # single source of truth for ALL pad operations
import usb_audio  # the USB mode, the stream's raw switch and RODE's virtual devices
import wording  # plurals, spoken lists and durations
from desk_labels import (  # what each setting is called, shared with the announcer
    MODULE_TOGGLE_LABEL,
    PROCESSING_CONTROLS,
    PROCESSING_MODULES,
    PROCESSING_SPOKEN,
)
from paths import safe_name


_PROCESSING_CONTROL_BY_PROP = {c["prop"]: c for g in PROCESSING_CONTROLS for c in g["controls"]}
_PROCESSING_PROP_SET = set(_PROCESSING_CONTROL_BY_PROP)


# ----------------------------------------------------------------------------
# Control actions. Each returns {"ok": bool, "message": str}. All device access
# goes through desk_access.hid_lock so the unit is only ever opened by one request at once.
# ----------------------------------------------------------------------------


# The control announcer and the speech thread are announcer.py and speech.py. This is the server's
# side of the announcer's interface.
class _AnnouncerHooks(announcer.Hooks):
    """The device lock, the shortcut layer's mixer pads, and what a reconnect means for the USB
    mode and RODE's virtual devices (`usb_audio.reconnected`). Each looks its state up when
    called."""

    lock = desk_access.hid_lock


    def mixer_positions(self):
        return pad_shortcuts.mixer_positions

    def reconnected(self, was_usb, now_usb):
        usb_audio.reconnected(was_usb, now_usb)


announcer.hooks = _AnnouncerHooks()


def action_set_gain(channel: int, db: int) -> dict:
    if channel not in channels.MIC_CHANNELS:
        return {"ok": False, "message": f"Mic channel {channel} is not valid."}
    if db < channels.GAIN_MIN_DB or db > channels.GAIN_MAX_DB:
        return {"ok": False, "message": f"Gain must be between {channels.GAIN_MIN_DB} and "
                                        f"{channels.GAIN_MAX_DB} dB."}
    with desk_access.hid_lock:
        channels.set_mic_gain(channel, db)
    announcer.mark_self_gain(channel, True)     # the desk echoes this; the UI already reports it
    return {"ok": True, "message": f"Mic {channel} gain set to {db} dB."}


def action_set_phantom(channel: int, on: bool) -> dict:
    if channel not in channels.MIC_CHANNELS:
        return {"ok": False, "message": f"Mic channel {channel} is not valid."}
    with desk_access.hid_lock:
        channels.set_phantom(channel, on)
    return {"ok": True,
            "message": f"Mic {channel} 48V phantom power turned {'on' if on else 'off'}."}


def action_set_polarity(channel: int, on: bool) -> dict:
    if channel not in channels.MIC_CHANNELS:
        return {"ok": False, "message": f"Mic channel {channel} is not valid."}
    with desk_access.hid_lock:
        channels.set_polarity(channel, on)
    return {"ok": True,
            "message": f"Mic {channel} polarity {'inverted' if on else 'normal'}."}


def _mic_processing_snapshot(channel: int) -> dict:
    """{param/module name: value} for one mic's strip, off one dump, as the Channels page shows it.

    Deliberately the SAME reader the page uses (channels.read_channels), so a before/after diff here
    can never disagree with what the user is looking at."""
    try:
        recs = channels.read_channels()
    except Exception as exc:
        diagnostics.event("device.read_failed", "processing snapshot", error=str(exc))
        return {}
    entry = next((c for c in channels.active_ui_channels()
                  if c.get("kind") == "mic" and channels.mic_channel_number(c) == channel), None)
    rec = next((r for r in recs if entry and r.get("name") == entry.get("name")), None)
    if rec is None:
        return {}
    out = dict(rec.get("params") or {})
    out.update(rec.get("modules") or {})
    return out


def action_set_mic_type(channel: int, type_value: int) -> dict:
    """Set a mic's voicing, AND SAY WHAT IT DID TO THE GAIN AND THE PROCESSING CHAIN.

    CHOOSING A VOICING SETS THAT VOICING'S OWN DEFAULT GAIN - the desk does it, not us (measured:
    Dynamic 45, Condenser 25, PodMic 50, Broadcaster 27, Procaster 50, NT1 30, NT1-A 31, RE20 51,
    SM7B 55; Line-in and Instrument leave it alone).
    Without the read-back below, the gain control keeps showing the OLD number and nothing is
    announced, so a screen-reader user is told the voicing changed and never learns their preamp
    moved by up to 30 dB. Costs one dump on a deliberate, rare action.

    IT ALSO REPLACES THE WHOLE PROCESSING CHAIN on a Pro II - 13 params measured (Depth, Sparkle,
    Punch, compressor threshold + gain, de-esser, gate threshold + range, all three EQ gains, both
    Aphex) - and writing the old type back does NOT undo it. A Duo mic moves the gain only. None of
    it is undone here on purpose: the voicing's own chain is what the user asked for by naming their
    microphone, and it is what the desk and the RODE app both do. What must not happen is saying
    nothing, which leaves a screen-reader user with a panel full of stale numbers and no idea their
    compressor moved. So it is COUNTED and ANNOUNCED, exactly as the gain is, and the client re-reads
    the panel on success."""
    if channel not in channels.MIC_CHANNELS:
        return {"ok": False, "message": f"Mic channel {channel} is not valid."}
    # ALL THREE READS INSIDE THE LOCK, like every other read that has to line up with a write of
    # ours: the readers do not take the HID lock themselves, the caller does. The first is adopted
    # by the desk model, which is where the gain before the change comes from.
    with desk_access.hid_lock:
        proc_before = _mic_processing_snapshot(channel)
        was = channels.mic_gain_known(channel)
        channels.set_mic_type(channel, type_value)
        gain, _proc = channels.mic_gain_and_processing(channel)
        proc_after = _mic_processing_snapshot(channel)
    name = channels.MIC_TYPE.get(type_value, f"type {type_value}")
    msg = f"Mic {channel} input type set to {name}."
    if gain is not None and gain != was:
        msg += f" The desk moved the gain to {gain} dB for this microphone."
    moved = sorted(k for k, v in proc_after.items()
                   if k in proc_before and proc_before[k] != v)
    if moved:
        msg += (f" The desk also replaced this channel's processing with the {name} voicing: "
                f"{wording.count(len(moved), 'setting')} changed, so anything you had "
                f"set here by hand is gone.")
    return {"ok": True, "message": msg, "gain_db": gain}


# --- Channels view: every processing-capable strip the device exposes (on the Pro II the four
# --- mics, BT/USB1/Chat/USB2, and the SMART pads bus) ------------------------------------------
# One adaptive panel per channel, mirroring the RODE app: mic channels add gain/phantom/polarity/
# mic-type/effects; line channels have Level + processing. Nodes are resolved per channel from
# the active device (channels.proc_key).


def _ui_chan(name):
    """A channel by name on the CONNECTED device, resolved per call: the Duo's names ("Headset",
    "USB1") differ from the Pro II's, so a map built at import would reject them."""
    return channels.ui_channel_map().get(name)




def action_get_channels(percent: bool = True) -> dict:
    """Read every UI channel's full adaptive state (one model dump). Params are 0-100 percent ints,
    or with `percent` False the desk's own 0..1 floats, which `mic_presets.preset_now` can compare again
    after a pushed change (a whole percent is as coarse as its tolerance)."""
    try:
        with desk_access.hid_lock:
            # ONE DUMP FOR BOTH. The wireless-mic slots ride the same read as the channels so the
            # "Mute the wireless mic" switches can be made unavailable when the unit reports no
            # transmitter, without a second trip to the desk for a fact we already have in hand.
            blob = rcp_read.read_model_stable()
    except Exception as exc:
        return {"ok": False, "message": f"Could not read channels from the device: {exc}"}
    return _channels_reply(blob, percent)


def read_window_state() -> dict:
    """The native window's full read: every channel, as `action_get_channels(percent=False)`, and
    the output mixes (`matrix`, `rcp_read.read_matrix_state` keyed by index, with
    `output_mixes.matrix_model`'s layout as `matrix_model`), the fader layout (`faders`:
    `fader_model` as `model`, then `layout`, `labels` and `stereo` as a fader write's reply carries
    them), the Levels page's state (`levels`, `_window_levels`) and the Master Mix page's
    (`master`, `master_outputs.window_master`), from ONE model dump."""
    try:
        with desk_access.hid_lock:
            blob = rcp_read.read_model_stable()
    except Exception as exc:
        return {"ok": False, "message": f"Could not read the desk: {exc}"}
    return window_state(blob)


def window_state(blob: bytes) -> dict:
    """`read_window_state`'s reply from a dump already in hand: a read's, or the desk model's
    snapshot (`desk_model.snapshot`), which is how the window follows the desk between reads."""
    reply = _channels_reply(blob, percent=False)
    if reply.get("ok"):
        reply["matrix"] = rcp_read.read_matrix_state(blob)
        reply["matrix_model"] = output_mixes.matrix_model()
        spec = faders.active_faders()
        cis = list(rcp_read.values(blob, "channelInputSource"))
        layout = [cis[i] if i < len(cis) else None for i in range(spec.count)]
        reply["faders"] = {"model": faders.fader_model(), "layout": layout,
                           "labels": [faders.channel_name(c, spec) for c in layout],
                           "stereo": faders.stereo_link_state(cis, spec)}
        reply["levels"] = _window_levels(blob)
        reply["master"] = master_outputs.window_master(blob)
    return reply


def _window_levels(blob: bytes) -> dict:
    """The native Levels page's state from one dump: `faders.fader_levels`, each output's cross-point
    levels (`mix`, `rcp_read.read_matrix_levels` less its modes, which the window keeps in
    `matrix`), the two auto-mutes, the headphone types, and the page's layout as `model`."""
    levels = faders.fader_levels(blob)
    mix = rcp_read.read_matrix_levels(blob)
    mix.pop("modes", None)
    auto_mute = master_outputs.AUTO_MUTE
    phones = master_outputs.active_headphones()
    levels.update(
        mix=mix,
        auto_mute={which: rcp_read.block_value(blob, "OUTPUT", spec["prop"])
                   for which, spec in auto_mute.items()},
        headphone_types=master_outputs.headphone_types(blob),
        model={"faders": list(faders.active_faders().level_faders),
               "monitor_mix": output_mixes.monitor_mix_model(),
               "auto_mute": [[which, spec["label"]] for which, spec in auto_mute.items()],
               "headphones": {
                   "outputs": [lab for _sel, lab in phones.outputs],
                   "values": [[code, lab] for code, lab in phones.values]}})
    return levels


def _channels_reply(blob: bytes, percent: bool) -> dict:
    try:
        chans = channels.read_channels(blob)
    except Exception as exc:
        return {"ok": False, "message": f"Could not read channels from the device: {exc}"}
    # Which preset each mic strip is on. BEFORE the percent conversion below, because the comparison
    # is against the preset's own normalised 0..1 values (see mic_presets.preset_now).
    for c in chans:
        if c.get("kind") == "mic":
            ui = _ui_chan(c.get("name"))
            if ui is not None:
                c["preset_now"] = mic_presets.preset_now(channels.mic_channel_number(ui), c)
    if percent:
        for c in chans:
            c["params"] = {p: (round(v * 100) if v is not None else None)
                           for p, v in c["params"].items()}
    return {
        "ok": True, "channels": chans, "groups": PROCESSING_CONTROLS,
        "wireless_mics": network.wireless_mics(blob),
        "modules": PROCESSING_MODULES, "mic_types": sorted([list(kv) for kv in channels.MIC_TYPE.items()]),
        "presets": mic_presets.preset_list(),
        "pan_links": pan_link.pan_links(),
        "gain_min_db": channels.GAIN_MIN_DB, "gain_max_db": channels.GAIN_MAX_DB,
        "level_min_db": channels.LEVEL_MIN_DB, "level_max_db": channels.LEVEL_MAX_DB,
        "message": "Read all channels from the device.",
    }


def action_set_channel_level(name: str, db: int) -> dict:
    """Set a channel's Level: mics use mic gain (0-76 dB); line channels use inputDigitalGain."""
    ch = _ui_chan(name)
    if ch is None:
        return {"ok": False, "message": f"Unknown channel: {name}."}
    if ch["kind"] == "mic":
        return action_set_gain(channels.mic_channel_number(ch), db)
    if db < channels.LEVEL_MIN_DB or db > channels.LEVEL_MAX_DB:
        return {"ok": False, "message": f"Level must be between {channels.LEVEL_MIN_DB} and "
                                        f"{channels.LEVEL_MAX_DB} dB."}
    with desk_access.hid_lock:
        channels.set_channel_level(name, db)
    announcer.mark_self_gain(name, False)       # line strips push inputDigitalGain back at us
    return {"ok": True, "message": f"{ch['label']} level set to {db} dB."}


_rode_read_busy = threading.Lock()
# How long a read for RODE's app waits for a write or a job's read to let the desk go before it
# skips this look; the next comes `rode_app.POLL_S` later.
_RODE_READ_WAIT_S = 1.0


_rode_was_open = False


def follow_rode_app(open_now: bool) -> None:
    """`rode_app.on_look`: while RODE's app is open, compare each dump with the desk model, so what
    it changed, which the desk never pushes, reaches whatever follows the desk
    (`desk_model.reveal`). NOTHING IS READ ON A TIMER: every reader is sent every dump's frames,
    and ours arriving every few seconds keep RODE's app on "Synchronizing device" for good. The
    window reads once when it comes back to the front (`read_for_rode_app`), and the desk is read
    once more as RODE's app closes, for what it changed since."""
    global _rode_was_open
    closed, _rode_was_open = _rode_was_open and not open_now, open_now
    if closed:
        _read_unheard()
    desk_model.reveal(open_now)
    announcer.gather_changes(open_now)


def read_for_rode_app() -> None:
    """One read of the desk while RODE's app is open, for what it has changed, which the
    announcer then says with what it has gathered."""
    if rode_app.is_open():
        _read_unheard()
        announcer.speak_gathered()


def _read_unheard() -> None:
    """One read, compared with the desk model. Skipped while a job has the desk, while the desk is
    gone, and while another such read runs."""
    if not desk_live.connected() or desk_access.work_in_progress() is not None:
        return
    if not _rode_read_busy.acquire(blocking=False):
        return
    try:
        if not desk_access.hid_lock.acquire(timeout=_RODE_READ_WAIT_S):
            return
        try:
            rcp_read.read_model(fresh=True)
        finally:
            desk_access.hid_lock.release()
    except Exception as exc:
        diagnostics.event("rode_app.read_failed", str(exc))
    finally:
        _rode_read_busy.release()


def _mute_reach_message(who: str, sources) -> str:
    """What a mute of `who` (matrix source codes `sources`) actually silences. CALLER HOLDS THE HID
    LOCK.

    A mute reaches each mix only through the cross-points that follow the fader. A Custom output
    with that source UNLINKED at a manual level keeps playing it, so the sentence names every such
    output instead of claiming silence everywhere. A cached dump is enough: muting moves no link
    and no level. If the grid cannot be read, the reply says so rather than guessing either way."""
    try:
        blob = desk_access.desk_now()
        lv = rcp_read.read_matrix_levels(blob)
        ex = rcp_read.read_matrix_state(blob)
    except Exception:
        lv = ex = {"ok": False}
    if not (lv.get("ok") and ex.get("ok")):
        return f"{who} muted. Could not check whether any output still hears it."
    still = []
    for oi, label in ((o["index"], o["label"]) for o in output_mixes.active_matrix().outputs):
        if lv["modes"].get(oi) != 2:
            continue
        for s in sources:
            cell = lv["levels"].get(oi, {}).get(s) or {}
            if (cell.get("linked") is False and (cell.get("level") or 0) > 0
                    and not ex["excluded"].get(oi, {}).get(s)):
                still.append(label)
                break
    if not still:
        return f"{who} muted; it is silent in every mix."
    verb = "hears" if len(still) == 1 else "hear"
    return (f"{who} muted, but {wording.join_and(still)} still {verb} it: its level there is set "
            f"by hand, and a mute only reaches levels that follow the fader.")


def action_set_channel_mute(name: str, on: bool) -> dict:
    """Mute or unmute a channel from software (channelOutputMute, proven writable on hardware).

    The real accessibility win here is reach: this is the same mute as the button on the unit, so it
    can be pressed from a phone or another room instead of by finding the right button by touch.

    Two details matter. It silences the channel in every mix that follows the fader, not just the
    monitor, and the message names any output it does NOT reach (_mute_reach_message). And the
    write is marked as ours (announcer.mark_self_mute), or the desk's echo of the mute would be
    spoken on top of the UI's own announcement."""
    ch = _ui_chan(name)
    if ch is None:
        return {"ok": False, "message": f"Unknown channel: {name}."}
    try:
        with desk_access.hid_lock:
            channels.set_channel_mute(name, on)
            strip = channels.channel_strip_index(name)
    except Exception as exc:
        return {"ok": False, "message": f"Could not change the mute: {exc}"}
    announcer.mark_self_mute(strip)
    if not on:
        return {"ok": True, "message": f"{ch['label']} unmuted."}
    with desk_access.hid_lock:
        msg = _mute_reach_message(ch["label"], [int(ch["source_code"])])
    return {"ok": True, "message": msg}


def encoder_hold_mute(strip: int) -> None:
    """`encoder_hold`'s write: toggle the mute of the channel a channel button has given the encoder,
    and speak it as the desk's own hold is spoken."""
    if _strip_toggle_gate("Muting") is not None:
        return
    with desk_access.hid_lock:
        mutes = rcp_read.values(desk_access.desk_now(), channels.MUTE_PROP)
        if not 0 <= strip < len(mutes) or not isinstance(mutes[strip], bool):
            return
        on = not mutes[strip]
        rcp_transfer.write_at(channels.proc_key_for_strip(strip), channels.MUTE_PROP, "bool", on)
    announcer.announce_mute(strip, on)
    diagnostics.event("encoder_hold.mute", change_words.strip_name(strip), on=on)


def _strip_toggle_gate(what: str):
    """Per-strip MUTE and SOLO writes, gated on "processing" and NOT on the level gate.

    They are compact bools on the channel's own processing selector, so the capability that decides
    them is the same one that decides every other module toggle. Tying them to faders.level_write_gate
    conflates two unrelated things: the Duo's faderLevel selectors are unconfirmed, which says
    nothing about its mute. It also leaves the UI hiding a control the HTTP endpoint still accepts,
    which is the wrong way round on a device where the write is unproven.

    The Channels page's by-name mute reaches the same gate through channels._require_processing;
    this is the by-fader route's half, so the two cannot drift apart."""
    return desk_access.cap_gate("processing", lambda name:
                     f"{what} a channel is not enabled on the {name} yet, so this is read-only "
                     f"here. You can still see which channels are muted or soloed.")


def action_set_fader_mute(fader: int, on: bool) -> dict:
    """Mute whatever is on one fader, addressed by FADER rather than by channel name.

    Same property and the same announcer publish as action_set_channel_mute; only the way in
    differs. It exists because the Levels tab's rows ARE strips, and two of the things a strip can
    hold have no channel name to look up: an EMPTY fader, and a stereo-linked PAIR (its own source
    code, deliberately absent from the assignable channel list). Routing those through a by-name
    call would mean either refusing to mute a linked pair or teaching the channel table about pairs,
    and the strip is what the desk mutes anyway.

    Named for the fader because that is what the user pressed. The reply names the CHANNEL, because
    a virtual fader is only ever meaningful as whatever is currently sitting on it."""
    gate = _strip_toggle_gate("Muting")
    if gate is not None:
        return gate
    spec = faders.active_faders()
    if not 0 <= int(fader) < spec.count:
        return {"ok": False, "message": f"Fader {int(fader) + 1} is not valid."}
    try:
        with desk_access.hid_lock:
            vec = faders.read_fader_vector(spec=spec)
            code = vec[int(fader)] if int(fader) < len(vec) else None
            if faders.is_empty_fader(code):
                return {"ok": False,
                        "message": (f"{faders.fader_label(int(fader), spec)} has no channel on it, so "
                                    f"there is nothing to mute. Put a channel on it first.")}
            rcp_transfer.write_at(channels.proc_key_for_strip(int(fader)), channels.MUTE_PROP,
                                  "bool", bool(on))
    except Exception as exc:
        return {"ok": False, "message": f"Could not change the mute: {exc}"}
    announcer.mark_self_mute(int(fader))
    who = f"{faders.channel_name(code, spec)} on {faders.fader_label(int(fader), spec)}"
    if not on:
        return {"ok": True, "muted": False, "message": f"{who} unmuted."}
    # A stereo-linked pair is its own fader code but two matrix sources, its two mics.
    sources = list(spec.pair_map.get(code, (code,)))
    with desk_access.hid_lock:
        msg = _mute_reach_message(who, sources)
    return {"ok": True, "muted": True, "message": msg}


def action_set_fader_solo(fader: int, on: bool) -> dict:
    """Solo ("Listen") whatever is on one fader. The solo twin of action_set_fader_mute, and it
    exists for the same reason: a Levels row IS a strip, and an empty fader or a stereo-linked pair
    has no channel name to look up.

    IT KEEPS THE SAFETY WORDING, which is the whole condition on offering solo in a second place. A
    solo replaces the headphone feed, so left on by accident the desk sounds broken with nothing
    saying why - and a switch that only knew about its own fader would be exactly that trap. The
    reply names everything the headphones now carry, not just this one, and the Levels tab carries
    the same one-press Clear all solos that the Channels page has."""
    gate = _strip_toggle_gate("Solo")
    if gate is not None:
        return gate
    spec = faders.active_faders()
    if not 0 <= int(fader) < spec.count:
        return {"ok": False, "message": f"Fader {int(fader) + 1} is not valid."}
    try:
        with desk_access.hid_lock:
            vec = faders.read_fader_vector(spec=spec)
            code = vec[int(fader)] if int(fader) < len(vec) else None
            if faders.is_empty_fader(code):
                return {"ok": False,
                        "message": (f"{faders.fader_label(int(fader), spec)} has no channel on it, so "
                                    f"there is nothing to solo.")}
            rcp_transfer.write_at(channels.proc_key_for_strip(int(fader)), channels.CUE_PROP,
                                  "bool", bool(on))
    except Exception as exc:
        return {"ok": False, "message": f"Could not change the solo: {exc}"}
    announcer.mark_self_solo(int(fader))
    who = f"{faders.channel_name(code, spec)} on {faders.fader_label(int(fader), spec)}"
    return {"ok": True, "soloed": bool(on),
            "message": (f"{who} soloed; the headphones now carry "
                        f"{_solo_summary(int(fader), True)}. The mixes, the recording and anything "
                        f"you are streaming are untouched."
                        if on else
                        f"{who} solo off. {_solo_after_clear(int(fader))}")}


def action_set_channel_solo(name: str, on: bool) -> dict:
    """Solo ("Listen") a channel from software, or clear it (channelCueEnable).

    The reach argument is the same as the mute's, but the SAFETY argument is stronger and shapes the
    message. A solo replaces the headphone feed: leave one on by accident and the desk sounds broken
    with nothing on screen to say why, and it is exactly the sort of state a blind operator cannot
    spot by glancing at a lit button. So the reply always says how many solos are now active and
    names them, and there is a one-press way out (action_clear_solos)."""
    ch = _ui_chan(name)
    if ch is None:
        return {"ok": False, "message": f"Unknown channel: {name}."}
    try:
        with desk_access.hid_lock:
            channels.set_channel_solo(name, on)
            strip = channels.channel_strip_index(name)
    except Exception as exc:
        return {"ok": False, "message": f"Could not change the solo: {exc}"}
    announcer.mark_self_solo(strip)
    return {"ok": True, "message": (f"{ch['label']} soloed; the headphones now carry "
                                    f"{_solo_summary(strip, True)}. Nothing else has changed: the "
                                    f"mixes, the recording and anything you are streaming are "
                                    f"untouched."
                                    if on else
                                    f"{ch['label']} solo off. {_solo_after_clear(strip)}")}


def action_set_channel_wireless_mute(name: str, on: bool) -> dict:
    """Mute or unmute a RODE wireless mic paired to a channel (channelWirelessMute).

    NOT the same control as action_set_channel_mute, and the reply has to keep them apart or it is
    worse than no control at all: that one silences the strip inside the desk, this one asks the
    TRANSMITTER to stop sending. Mute the strip and the desk still receives; mute the transmitter and
    nothing arrives at all, which is the one you want when somebody walks off still wearing it.

    THE REPLY SAYS WHAT WAS SENT, NOT WHAT HAPPENED, and that is deliberate. The write is addressed
    correctly (the property sits on the same CHANNEL node as the mute, measured) but nobody here owns
    a transmitter, so it has never been heard to work, and a channel with nothing paired has nothing
    to obey it. Claiming "the mic is muted" would be the app reading its own write back as an
    outcome."""
    ch = _ui_chan(name)
    if ch is None:
        return {"ok": False, "message": f"Unknown channel: {name}."}
    if ch.get("kind") != "mic":
        return {"ok": False, "message": f"{ch['label']} is not a mic channel, so no wireless mic "
                                        f"can be paired to it."}
    try:
        with desk_access.hid_lock:
            channels.set_channel_wireless_mute(name, on)
    except Exception as exc:
        return {"ok": False, "message": f"Could not change the wireless mic mute: {exc}"}
    return {"ok": True,
            "message": (f"Mute sent to the wireless mic on {ch['label']}. If a RODE wireless "
                        f"transmitter is paired there it should now be silent; the channel itself "
                        f"is not muted."
                        if on else
                        f"Unmute sent to the wireless mic on {ch['label']}.")}


def action_clear_solos() -> dict:
    """Clear every solo in one go. The way out of the state above without having to find which
    channel caused it - and the reply says plainly when there was nothing to clear, rather than
    reporting a cheerful success for a no-op."""
    try:
        with desk_access.hid_lock:
            cleared = channels.clear_all_solos()
    except Exception as exc:
        return {"ok": False, "message": f"Could not clear the solos: {exc}"}
    if not cleared:
        return {"ok": True, "message": "Nothing was soloed; the headphones already carry the "
                                       "full monitor mix."}
    announcer.forget_solos()
    return {"ok": True, "message": (f"{wording.count(cleared, 'solo')} cleared; the "
                                    f"headphones carry the full monitor mix again.")}


def _solo_live() -> dict:
    """{strip: bool} for every strip the desk reports, or {} if it cannot be read. Never raises:
    this only ever decorates a message, and a message is not worth failing a write over."""
    try:
        with desk_access.hid_lock:
            return {i: bool(v) for i, v in enumerate(channels.read_solos())}
    except Exception:
        return {}


def _solo_summary(strip: int, on: bool) -> str:
    """Name what the headphones carry, with the write we just made folded in - the desk's own read
    can lag a write by a moment, so trusting it alone would sometimes report the state we just left."""
    live = _solo_live()
    live[strip] = on
    names = [change_words.strip_name(i) for i, v in sorted(live.items()) if v]
    return wording.join_and(names) or "the full monitor mix"


def _solo_after_clear(strip: int) -> str:
    left = _solo_summary(strip, False)
    return ("The headphones are back to the full monitor mix."
            if left == "the full monitor mix"
            else f"Still soloed: {left}.")


def action_set_processing_enabled(name: str, on: bool) -> dict:
    """Master per-channel processing on/off (the 'Processing' toggle)."""
    ch = _ui_chan(name)
    if ch is None:
        return {"ok": False, "message": f"Unknown channel: {name}."}
    with desk_access.hid_lock:
        channels.set_processing_enabled(name, on)
    return {"ok": True,
            "message": f"{ch['label']} processing turned {'on' if on else 'off'}."}


def action_set_channel_advanced(name: str, on: bool) -> dict:
    """Switch a channel between ADVANCED processing (the modules and their parameters) and SIMPLE
    (the three tone-shaping controls only). channelAdvancedProcessing, per fader strip.

    Worth knowing, and why the message says so: in SIMPLE mode the unit DERIVES most of the chain
    from Depth/Sparkle/Punch, so the module parameters below can be recomputed from those three. The
    RODE app clears them outright when you leave advanced mode; we do not touch them, so switching
    back and forth here is non-destructive."""
    ch = _ui_chan(name)
    if ch is None:
        return {"ok": False, "message": f"Unknown channel: {name}."}
    try:
        with desk_access.hid_lock:
            channels.set_channel_advanced(name, on)
    except Exception as exc:
        return {"ok": False, "message": f"Could not change the processing mode: {exc}"}
    return {"ok": True,
            "message": (f"{ch['label']} set to advanced processing; the modules and their "
                        f"parameters are in charge." if on else
                        f"{ch['label']} set to simple processing; the desk works the chain out "
                        f"from Depth, Sparkle and Punch.")}


def action_set_channel_module(name: str, module: str, on: bool) -> dict:
    """Enable/disable one processing module (hpf/eq/compressor/noiseGate/deesser/aphex/pan)."""
    ch = _ui_chan(name)
    if ch is None:
        return {"ok": False, "message": f"Unknown channel: {name}."}
    try:
        with desk_access.hid_lock:
            channels.set_processing_module(name, module, on)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True,
            "message": (f"{ch['label']} {MODULE_TOGGLE_LABEL.get(module, module)} turned "
                        f"{'on' if on else 'off'}.")}


def action_set_channel_effect(name: str, effect: str, on: bool) -> dict:
    """Toggle a mic channel's always-on effect (echo / reverb). Mic channels only (the app shows
    Echo/Reverb only on mics)."""
    ch = _ui_chan(name)
    if ch is None or ch["kind"] != "mic":
        return {"ok": False, "message": "Echo and reverb are available on mic channels only."}
    try:
        with desk_access.hid_lock:
            channels.set_channel_effect(channels.mic_channel_number(ch), effect, on)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": f"{ch['label']} {effect} turned {'on' if on else 'off'}."}


def action_set_channel_effect_param(name: str, param: str, value01: float) -> dict:
    """Set one channel-effect param (echo*/reverb*) on a mic channel. value01 is 0.0..1.0
    (decoded: float64 on the strip's EFFECTS_PARAMETERS node, byte-for-byte the RODE app's write)."""
    ch = _ui_chan(name)
    if ch is None or ch["kind"] != "mic":
        return {"ok": False, "message": "Echo and reverb are available on mic channels only."}
    try:
        v = max(0.0, min(1.0, float(value01)))
        with desk_access.hid_lock:
            channels.set_channel_effect_param(channels.mic_channel_number(ch), param, v)
    except (ValueError, TypeError) as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": f"{ch['label']} {param} set to {round(v * 100)} percent."}


def action_set_channel_param(name: str, prop: str, value01: float) -> dict:
    """Set one processing parameter (0..1 normalised) on any UI channel."""
    ch = _ui_chan(name)
    if ch is None:
        return {"ok": False, "message": f"Unknown channel: {name}."}
    if prop not in _PROCESSING_PROP_SET:
        return {"ok": False, "message": f"Unknown processing control: {prop}."}
    try:
        v = max(0.0, min(1.0, float(value01)))
    except (TypeError, ValueError):
        return {"ok": False, "message": "Value must be a number from 0 to 1."}
    with desk_access.hid_lock:
        channels.set_channel_processing(name, prop, v)
    ctrl = _PROCESSING_CONTROL_BY_PROP[prop]
    pct = round(v * 100)
    if ctrl.get("pan"):
        where = ("centre" if pct == 50 else "hard left" if pct == 0 else "hard right" if pct == 100
                 else f"{(50 - pct) * 2} percent left" if pct < 50 else f"{(pct - 50) * 2} percent right")
    else:
        where = f"{pct} percent"
    return {"ok": True,
            "message": f"{ch['label']} {PROCESSING_SPOKEN[prop].lower()} set to {where}."}


# ----------------------------------------------------------------------------
# Settings profiles. The save/restore logic lives in profiles.py; these are thin wrappers that
# own the HID lock (read the model under it for a save, hold it across the writes for a restore)
# and pass the server's validation constants in. See profiles.py for the format + field detail.
# ----------------------------------------------------------------------------

# Measured, and the ONLY honest basis for the waiting-time the page quotes. A pad
# restore is dominated by moving its audio over transfer mode, and it is per pad, so the total grows
# with the pad count without limit - the page must work it out, not guess a phrase.
PAD_RESTORE_SECONDS = 15
SETTINGS_RESTORE_SECONDS = 25


def list_profiles() -> dict:
    return profiles.list_profiles()


def _profile_file_job_busy():
    """Message if a save or restore is in flight, else None. Renaming or deleting a profile is
    pure file work, but either job is mid-way through reading or writing that same folder, so the
    file operations stand aside rather than pulling it out from under them."""
    if _profile_job.busy():
        return "A profile save is running. Try again when it finishes."
    if _restore_job.busy():
        return "A profile restore is running. Try again when it finishes."
    return None


def profile_info(name: str) -> dict:
    """What the page needs to describe a profile BEFORE restoring it, above all its pad count: pads
    dominate the time (about 15 seconds each, measured), so a 40-pad profile is a ten-minute job
    rather than the minute or so a settings-only restore takes. Reads the one profile, not the whole
    store."""
    loaded = profiles.load(name)
    if not loaded.get("ok"):
        return loaded
    prof = loaded["prof"]
    return {"ok": True, "name": loaded["safe"], "pads": len(prof.get("pads") or []),
            "saved_at": prof.get("saved_at"), "device_name": prof.get("device_name"),
            "seconds_per_pad": PAD_RESTORE_SECONDS, "settings_seconds": SETTINGS_RESTORE_SECONDS}


def delete_profile(name: str, force: bool = False) -> dict:
    """Delete a saved profile from this computer. No HID lock and no device access.

    `force` is the second step for the as-found baseline only: without it, deleting the baseline
    comes back with baseline: True and a message for the page to confirm against."""
    busy = _profile_file_job_busy()
    return {"ok": False, "message": busy} if busy else profiles.delete(name, force=force)


def rename_profile(name: str, new_name: str) -> dict:
    """Rename a saved profile on this computer. No HID lock and no device access."""
    busy = _profile_file_job_busy()
    return {"ok": False, "message": busy} if busy else profiles.rename(name, new_name)


# A settings-only profile save is quick, but include_pads copies every pad's sound file off the unit
# over transfer mode and takes a minute or more, so it runs as a job and the window polls
# `profile_save_status`.
_profile_job = desk_access.BackgroundJob("A profile save is already running.",
                                         "Could not save the profile", "A backup is running.")


def profile_save_status() -> dict:
    return _profile_job.status()


def _do_snapshot(safe: str, include_pads: bool, prog, baseline: bool = False, show=None) -> dict:
    """Read the whole desk under the HID lock, then hand it to profiles.save. With include_pads,
    also back up the SMART pad grid (metadata + copy each sound file off the unit) in the same
    locked session. The device reads stay here because the lock lives here."""
    try:
        with desk_access.hid_lock:
            # PHASES, NOT ITEMS. Everything spoken during a job goes to one dialog's notifier, so
            # each line cuts off the one before it; with a line per pad you hear the first two words of
            # each and nothing else, and the closing line sounds like one more pad. Three lines for
            # the whole job, and the last word of the last one cannot be mistaken for a pad name.
            prog("Backing up the settings.")
            blob = rcp_read.read_model_stable()
            chans = channels.read_channels(blob)
            if include_pads:
                pad_backup = soundpad.backup_pads(profiles.pads_dir(safe), progress=None)
                n = len(pad_backup.get("pads") or [])
                prog(f"Settings done. Backed up {wording.count(n, 'SMART pad')}.")
            else:
                pad_backup = None
    except Exception as exc:
        return {"ok": False, "message": f"Could not read settings from the device: {exc}"}
    firmware = desk_firmware.version(blob)
    dev = devices.get_active_device()
    # capture_length, not count: a profile must capture the WHOLE channelInputSource vector,
    # including any slot the desk does not expose as a fader (both known desks have a 10th, measured),
    # so the saved layout is the real one rather than a truncated view.
    # Capture is deliberately WIDER than write: profiles.apply only writes the first
    # vector_length entries, so the extra slot round-trips into the JSON and is never written back.
    res = profiles.save(safe, blob, chans, firmware, faders.active_faders().capture_length,
                        pads=(pad_backup["pads"] if pad_backup else None),
                        product_id=dev.product_id, device_name=dev.name,
                        headphone_output_count=len(master_outputs.active_headphones().outputs),
                        baseline=baseline, show=show)
    if pad_backup and pad_backup.get("unrecognised"):
        odd = pad_backup["unrecognised"]
        grids = wording.join_and(str(g) for g in odd)
        res["message"] += (f" Note: the voice-FX {wording.plural(len(odd), 'pad')} at grid {grids} "
                           f"carry a name that matches no factory or saved effect (likely "
                           f"renamed), so the effect could not be identified and they will be "
                           f"skipped on restore.")
    return res


def _removable_volume_ready() -> bool:
    """Is there a card or USB drive in the unit with room for a show?

    A show can ONLY go to removable storage: measured, every export takes about 24 MB off the USB
    drive and leaves the internal volume untouched, and that internal volume is the SMART pad
    partition, which reports it cannot even record. So with nothing inserted there is nowhere for a
    backup to go, and asking for one times out rather than failing cleanly.

    ANSWERED BY `recorder.destination`, LIKE EVERY OTHER CALLER. A hand-rolled inserted/mounted/
    can_record loop here would be a SECOND OPINION on whether a backup is possible, and the
    Backups page, the Recording section and this could disagree. Deliberately NOT fresh: the short-lived cache is what keeps this cheap
    enough to ask on a page load."""
    try:
        with desk_access.hid_lock:
            blob = rcp_read.read_model()
        return bool(recorder.destination(recorder.read_volumes(blob)))
    except Exception:
        return False


def action_backup_now(name: str) -> dict:
    """BACK THE UNIT UP: one name, one button, and both mechanisms underneath.

    THE OBVIOUS ALTERNATIVE LOOKS TIDIER AND IS WORSE: offering shows OR profiles depending on
    whether a drive is in. It fails two ways: pull the drive to offload recordings and the user's
    whole backup history vanishes from the page (and dies for good if they format it), and a
    shows-only list cannot be named, because the unit names every export after the loaded show and
    offers no rename, so it reads as a column of identical lines.

    So: export a show when there is somewhere to put one, ALWAYS save a profile, and pair them. The
    user types one name and never learns there are two mechanisms. With no removable storage the
    profile goes on alone and nothing announces a missing feature.

    The show is exported FIRST, while the desk still holds exactly what is being named."""
    safe = safe_name(name)
    if not safe:
        return {"ok": False, "message": "Backup name must be 1 to 40 letters, digits, "
                                        "spaces, dashes or underscores."}

    return _profile_job.start(lambda job: backup_unit(job, safe),
                              message="Backing up, please wait.", started="Backing up.")


def backup_unit(job, safe: str) -> dict:
    """The body of a backup, run inside `job`: the unit's own copy when there is a drive, then the
    settings and pads on this computer. See `action_backup_now`."""
    show = None
    note = ""
    if "shows" in devices.get_active_device().caps and _removable_volume_ready():
        got = _export_and_wait(job.step, "Backing the desk up to its own drive...")
        if got.get("ok"):
            s = got["show"]
            show = {"snapshot": s.get("snapshot"), "uuid": s.get("uuid"), "name": s.get("name")}
        else:
            # The profile is still worth having, so carry on rather than losing the lot. Say so
            # plainly: a backup that quietly did half of what was asked is worse than a slow one.
            note = (" The desk's own backup did not appear on the drive, so this is a settings "
                    "and pads backup only. Check there is a drive in with room on it.")
    res = _do_snapshot(safe, True, job.tick, show=show)
    if not res.get("ok"):
        return res
    res["message"] = _backup_wording(safe, show) + note
    return res


def _backup_wording(safe: str, show) -> str:
    """What a finished backup says. A restore always reads the copy on this computer, so that is
    what both wordings lead with; the unit's own copy is an extra, and its absence is not a fault."""
    if show:
        return (f"Backed up as {safe}: your settings and pads on this computer, plus a copy of "
                f"the whole desk on its own drive.")
    return f"Backed up as {safe}: your settings and pads, on this computer."


def snapshot_profile(name: str, include_pads: bool = False, baseline: bool = False) -> dict:
    """Validate the name, then start the save on a worker thread. The name check stays synchronous
    so a bad name comes straight back as an error instead of through the progress dialog."""
    safe = safe_name(name)
    if not safe:
        return {"ok": False, "message": "Profile name must be 1 to 40 letters, digits, "
                                        "spaces, dashes or underscores."}
    return _profile_job.start(
        lambda job: _do_snapshot(safe, include_pads, job.tick, baseline=baseline),
        message="Reading the desk's settings, please wait.",
        started="Saving the profile.")


# --- THE AS-FOUND BASELINE, offered on first run -----------------------------------------------
# The most valuable thing this app can do on the day it is installed is record how the desk already
# was: the moment anything is written, "how it came" is gone, and the touchscreen wizard that would
# otherwise rebuild it is not reachable by a blind operator.
#
# IT IS NOT THE ONLY UNDO, and it is the better one. `action_show_reset` puts the
# desk back to the factory, but it takes everything with it and it is the command implicated in the
# USB dropout; restoring this baseline puts back what the user actually had, needs no reset, and
# works on a desk with no card or drive in it.
#
# It is offered ONCE PER DESK, not once per install: plug in a second unit later and it gets its own
# offer. The answer (captured, or declined) is remembered against the hardware id so the dialog does
# not reappear at every launch, and declining does not close the door - the Backups page can still
# capture one, and pads can be added to an existing baseline afterwards.
_BASELINE_ASKED_KEY = "baseline_asked"
# MEASURED: a full backup of a 16-pad desk, 8 of them sound pads and 19.7 MB of audio, takes 4.6
# SECONDS END TO END, settings read included. Most of that is fixed cost (the model read, then
# entering transfer mode and mounting); the copying itself is a fraction of a second per file.
#
# DO NOT QUOTE PAD_RESTORE_SECONDS HERE. That 15 s/pad is genuinely measured, but for the other
# direction: a restore WRITES over transfer mode and the desk has to take each file, while a backup
# only READS a mounted FAT volume. Reusing it tells people a 16-pad backup takes four minutes when
# it takes five seconds, which is the kind of wrong that makes somebody skip the backup.
#
# Reading off the mounted pad volume runs at ~20 MB/s (12.14 MB in 0.56 s, and seven smaller files
# at 12-21 MB/s), so the whole grid moves in about a second. The copy is real rather than a cache
# trick or an empty file: every WAV was md5'd on the volume and again in the backup folder, and all
# eight matched byte for byte.
#
# SO THE HONEST SHAPE IS THROUGHPUT, NOT A PER-PAD CONSTANT. This grid's biggest pad is 12 MB; a
# ten-minute bed at 100 MB would take five seconds on its own. One second per pad is right for pads
# this size and is rounded up; if somebody's grid is all long beds it will under-quote, which costs
# little at a second a pad.
PAD_BACKUP_SECONDS = 1


def _baseline_asked() -> list:
    got = settings_file.load().get(_BASELINE_ASKED_KEY)
    return [str(x) for x in got] if isinstance(got, list) else []


def _note_baseline_asked() -> None:
    """Remember that this desk has had its offer, so the dialog is a first-run event and not a nag."""
    here = device_page.hardware_id()
    if here and here not in _baseline_asked():
        settings_file.update(**{_BASELINE_ASKED_KEY: [*_baseline_asked(), here]})


def _baseline_name() -> str:
    """The auto-name for a baseline: what it is, when it was taken, and which desk.

    Deliberately not "app install": a year from now that says nothing about which unit or when.
    Kept inside profiles.safe_name's character set (letters, digits, spaces, dashes) and its 40
    character limit, which is why there are no brackets and the desk is named without the
    "RODECaster" every desk shares: the longest date, "30th September 2026", leaves room for no
    more. The date is spoken rather than numeric because a profile name is heard."""
    when = f"As found {wording.spoken_date(datetime.date.today())}"
    desk = devices.get_active_device().name.removeprefix("RODECaster ").strip()
    return safe_name(f"{when} {desk}"[:40]) or when


def baseline_status() -> dict:
    """Should the page offer a first-run baseline capture, and what is there already?

    Cheap on purpose: it reads the profile store (small, local) and the settings file, and does NO
    device I/O at all. The page asks this on every load, right after the status read that publishes
    the hardware id, so "do we know this desk's id" is exactly as good as "is a desk connected" here
    and costs nothing."""
    existing = profiles.find_baseline()
    here = device_page.hardware_id()
    connected = bool(here)
    asked = here in _baseline_asked() if here else False
    return {
        "ok": True,
        # Only offer when there is a desk to read, nothing captured yet, and we have not already
        # asked. No desk means no offer rather than a dialog that could not do anything anyway.
        "offer": bool(connected and not existing and not asked),
        "connected": connected,
        "baseline": existing or None,
        "suggested_name": _baseline_name(),
        "device_name": devices.get_active_device().name,
        "seconds_per_pad": PAD_BACKUP_SECONDS,
    }


def action_baseline_skip() -> dict:
    """Decline the first-run capture. Remembered per desk so it is asked once, not every launch."""
    _note_baseline_asked()
    return {"ok": True, "message": "No baseline was captured. You can still capture one from the "
                                   "Backups page at any time, though it will then record the desk "
                                   "as it is now rather than as you found it."}


def action_baseline_save(include_pads: bool = True) -> dict:
    """Capture the as-found baseline. Runs on the same worker and reports through the same progress
    dialog as an ordinary profile save; only the name, the marking and the protections differ."""
    existing = profiles.find_baseline()
    if existing:
        return {"ok": False, "message": f"This desk already has a baseline, {existing['name']}, "
                                        f"and there can only be one. Nothing was changed."}
    _note_baseline_asked()          # asked and answered, whichever way the capture then goes
    return snapshot_profile(_baseline_name(), include_pads=include_pads, baseline=True)


def action_baseline_add_pads() -> dict:
    """Add the SMART pads to a baseline that was captured without them.

    The settings snapshot is left EXACTLY as it was captured (profiles.attach_pads, not a re-save):
    re-reading the desk would quietly turn an as-found record into a record of today, which is the
    one thing a baseline must never become."""
    existing = profiles.find_baseline()
    if not existing:
        return {"ok": False, "message": "There is no baseline on this computer to add pads to."}
    if existing.get("pads"):
        return {"ok": False, "message": f"{existing['name']} already has "
                                        f"{wording.count(existing['pads'], 'SMART pad')} saved."}
    safe = existing["name"]

    def _work(job):
        with desk_access.hid_lock:
            backup = soundpad.backup_pads(profiles.pads_dir(safe), progress=job.tick)
        return profiles.attach_pads(safe, backup["pads"])

    return _profile_job.start(_work,
                              message="Backing up SMART pads, please wait.",
                              started="Backing up the SMART pads.",
                              failed="Could not back up the pads")


# Profile restore runs in a background thread (the settings, then an optional pad rebuild at about
# PAD_RESTORE_SECONDS a pad), so the page is not blocked and can poll progress for live-region
# announcements.
_restore_job = desk_access.BackgroundJob("A profile restore is already running.",
                                         "Could not restore the profile", "A restore is running.",
                                         rewrites_desk=True)


def profile_restore_status() -> dict:
    return _restore_job.status()


def restore_profile(name: str, clean: bool = False) -> dict:
    """Start a background restore: apply the settings, then (if the profile carries pads) rebuild
    the SMART pad grid. The window polls `profile_restore_status` for progress. profiles.apply
    and soundpad.restore_pads do the device writes and assume the caller holds the HID lock.

    `clean` PICKS BETWEEN TWO GENUINELY DIFFERENT OPERATIONS, and the page names both rather than
    choosing for the user:

      * MERGE (the default): write the profile's values over
        whatever the desk holds now. Everything the profile does not cover is LEFT ALONE. That is
        the right choice for "put my show back", and it is the only one that preserves settings this
        app cannot write - a paired Bluetooth phone, a pad's custom censor sound, the strip colours.

      * CLEAN: software-factory-reset the desk first (shows.send_new_from_default), then restore on
        top. The desk then matches the profile EXACTLY, because the ~300 properties a profile cannot
        write are at factory defaults rather than at some unknown leftover value. It also clears
        strays and orphan pads, which no profile restore can do. The cost is real and is stated on
        the page: anything the profile does not carry is DESTROYED rather than left, and the pad
        phase gets slower because the reset puts 16 factory pads back for it to replace.

    An export is taken before the reset as the undo, for the reason action_show_reset gives.
    """
    loaded = profiles.load(name)
    if not loaded.get("ok"):
        return loaded
    safe, prof = loaded["safe"], loaded["prof"]
    refusal = _other_desk_refusal(prof)
    if refusal:
        return refusal
    pad_list = prof.get("pads") or []
    # Refuse a clean restore we cannot actually do CLEANLY, rather than silently doing a merge and
    # calling it clean. Someone who asked for an exact match needs to know they did not get one.
    if clean:
        gate = _shows_write_gate()
        if gate is not None:
            return {"ok": False,
                    "message": ("A clean restore resets the desk first, and that is not available "
                                "on this desk. " + gate["message"] + " Restore without the reset "
                                "instead: it puts your settings back but leaves anything the "
                                "profile does not cover as it is.")}

    def _work(job):
        # TWO PROGRESS CHANNELS, deliberately. The settings phase reports its own sections (it is
        # ~20s of paced writes) so it uses job.step, which does not touch the count; the pad phase
        # is a countable list, so it uses job.tick. The pad phase formats its OWN counts too
        # (soundpad.restore_pads knows how many pads are actually being written, which is not the
        # same as how many the profile holds once the verify pass has spared the ones already
        # correct), so no `phrase` is passed and the message goes through as it comes.
        reset_note = ""
        if clean:
            # THE RESET, AND ITS UNDO, BEFORE ANY PROFILE WRITE. Order is load-bearing: the export
            # has to be taken while the desk still holds the state being replaced, and the reset has
            # to land before profiles.apply, or the reset would wipe the values we just wrote.
            undo = _clean_restore_reset(job.step)
            if not undo.get("ok"):
                return undo
            reset_note = undo["message"]
        # AN OLD PROFILE REARRANGES THE FADERS AND THEN SAYS NOTHING ABOUT THE LEVELS, which is how
        # it can leave a channel silent. A fader's level belongs to the STRIP, so a channel moved to
        # a different fader inherits whatever that strip was sitting at - and profiles saved before
        # `fader_levels` existed carry the layout without the levels, so a restore can put a channel
        # on a fader at zero with every reading clean, because nothing IS wrong, and leave the
        # silence with no explanation.
        # So when the profile has no levels of its own, the levels FOLLOW THE CHANNELS instead, the
        # same way `action_set_fader` already carries them on a manual reassignment. When the
        # profile DOES carry levels they are authoritative and this must not run: `profiles.apply`
        # writes them after the layout, and overwriting them with the pre-restore values would throw
        # away the very thing the backup recorded.
        spec = faders.active_faders()
        carry_levels = not (prof.get("routing") or {}).get("fader_levels")
        vec_before = levels_before = None
        with desk_access.hid_lock:
            if carry_levels and faders.level_write_gate() is None:
                try:
                    blob0 = rcp_read.read_model()
                    vec_before = faders.read_fader_vector(blob0, spec)
                    levels_before = rcp_read.values(blob0, "faderLevel")
                except Exception:
                    vec_before = levels_before = None
            res = profiles.apply(prof, fader_count=faders.active_faders().vector_length,
                                 fader_code_set=faders.active_faders().code_set, progress=job.step)
            if levels_before is not None:
                faders.follow_levels(vec_before, faders.read_fader_vector(spec=spec), levels_before, spec)
        # A PROFILE CARRIES `systemMidiControl`, so one taken while MIDI was off has just switched
        # the MIDI port back off and every sound-pad shortcut with it. Forget that we armed it and
        # the next pad press arms it again - which is why arming is per-press-until-done rather than
        # once at startup.
        pad_shortcuts.rearm_midi()
        # A restore can also rewrite the whole pad grid, so the shortcut layer's map is wrong.
        pad_shortcuts.pads_stale()
        # Output mix matrix + monitor-mix levels (v2 profiles only).
        job.step("Settings done. Restoring output routing...")
        with desk_access.hid_lock:
            mx_res = output_mixes.apply_matrix(prof.get("routing") or {})
        pad_res = None
        pad_tidy = None
        pad_extra = None
        if pad_list:
            job.step("Restoring SMART pads...")
            with desk_access.hid_lock:
                # ONE TRANSFER SESSION FOR THE WHOLE PAD RESTORE. Without it each create enters
                # transfer mode, remounts the volume and leaves again for itself: sixteen cycles for
                # sixteen pads, every one dropping the desk's audio, popping a Windows device sound,
                # and costing about a second per pad. The session exits in its own __exit__, so a
                # failure on any pad still closes it.
                with rcp_transfer.transfer_session():
                    pad_res = soundpad.restore_pads(pad_list, profiles.pads_dir(safe),
                                                    progress=job.tick)
                # Only on a clean restore: there every pad outside the profile is one the reset
                # put back. An ordinary restore leaves pads the profile does not mention alone.
                if clean:
                    pad_extra = soundpad.clear_pads_not_in(pad_list, progress=job.step)
                # Pads replaced by the restore left their old audio behind; clear it out.
                pad_tidy = soundpad.tidy_unused_pad_audio()

        # AFTER THE PAD PHASE, AND ONLY ON A CLEAN RESTORE. The reset puts sixteen factory pads back,
        # six of them voice-FX pads with no effect definition behind them (soundpad.heal_fx_pool).
        # A profile that CARRIES pads has just rebuilt its own, so this finds nothing missing and
        # costs one dump; a profile saved without pads leaves the factory six needing the rebuild.
        healed = None
        if clean:
            job.step("Checking the voice effects survived the reset...")
            with desk_access.hid_lock:
                healed = soundpad.heal_fx_pool(progress=job.step)

        msg = profiles.restore_result(safe, res)["message"]
        if reset_note:
            msg = reset_note + " " + msg
        if mx_res and not mx_res.get("skipped"):
            msg += f" Re-applied output routing ({mx_res['applied']} matrix writes)."
        if pad_res is not None:
            msg += _pad_restore_words(pad_res, pad_extra, pad_tidy)
        if healed and healed.get("message"):
            msg += " " + healed["message"]
        return {"ok": True, "message": msg}

    return _restore_job.start(_work, total=len(pad_list),
                              message=("Backing the desk up before resetting it..." if clean
                                       else "Applying settings, please wait."),
                              started="Restoring the profile.")


# ----------------------------------------------------------------------------
# THE DESK'S OWN SHOWS: export, import, software factory reset, delete.
#
# The mechanism, the traps and why this sits alongside profiles rather than replacing them are all
# in shows.py. What lives HERE is the part that must not be in a module: the gate, the wording, and
# the rule that no command is ever reported as having worked until the desk has been read back.
#
# ALL FOUR RUN AS ONE BACKGROUND JOB, sharing one state block, because they are mutually exclusive
# by nature (you cannot import while a reset runs) and because every one of them needs a progress
# dialog: an import and a reset take seconds, and a blind operator staring at a page that has not
# changed cannot tell "working" from "died".
_show_job = desk_access.BackgroundJob("A show command is already running.",
                                      "The show command failed",
                                      "A backup, restore or reset is running on the desk.",
                                      rewrites_desk=True)


def _shows_write_gate():
    """SHOWCONTROL, where the consequence of a wrong selector is the worst of any gate: the address
    carries a FACTORY RESET, so a guess could land a reset, or a stray, on some other block.

    BOTH KNOWN DESKS HOLD THIS CAP, so nothing refuses here today. The gate stays because the
    hazard is a property of the ADDRESS and not of any one desk, and the next unknown desk gets the
    same treatment. `DevTools/test_shows_sim.py` proves it with a synthetic capless device."""
    return desk_access.cap_gate("shows", lambda name:
                     f"AccessCaster can list the shows on the {name} but not export, restore or "
                     f"reset them: doing that has only ever been confirmed on a RODECaster Pro 2, "
                     f"and the same control carries the factory reset, so it refuses rather than "
                     f"risk it.")


_NO_BACKUP_STORAGE = ("There is no memory card or USB drive in the desk, so there is nowhere to "
                      "save a backup.")
_RESET_STOPPED_NO_BACKUP = ("Stopped before resetting anything, and nothing on the desk has changed: "
                            "the backup did not appear on the drive, so there would have been no "
                            "way back. Check there is a drive in with room on it, and try again.")


# What the Backups page shows from `read_backup_storage` and `read_backups`, for following the
# desk: the shows on the drive, the loaded one, and recordState for a drive going in or out.
SHOW_STATE_PROPS = frozenset(("showName", "showUID", "showUUID", "showSnapshotIndex",
                              "showLastModification", "currentShowName", "currentShowUUID",
                              "recordState"))


def read_backup_storage() -> dict:
    """Whether this desk can make its own backup, and where: the shows cap, and a card or drive to
    write to (`recorder.destination`, the same answer the Recording section gives). Known up front
    so the factory reset's "back the unit up first" is not offered where it cannot work."""
    try:
        with desk_access.hid_lock:
            blob = rcp_read.read_model()
    except Exception as exc:
        return {"ok": False, "message": f"Could not read the desk's storage: {exc}"}
    dest = recorder.destination(recorder.read_volumes(blob))
    return {"ok": True, "writable": "shows" in devices.get_active_device().caps,
            "can_backup": bool(dest),
            "backup_blocked_reason": "" if dest else _NO_BACKUP_STORAGE}


def show_job_status() -> dict:
    """Progress for whichever show command is running. Polled by the page."""
    return _show_job.status()


def _run_show_job(label: str, work) -> dict:
    """Start `work` on a worker thread under the one job state, which all the show commands share
    because they are mutually exclusive. `work` reports through the `step` it is handed."""
    gate = _shows_write_gate()
    if gate is not None:
        return gate
    return _show_job.start(lambda job: work(job.step),
                           message=f"{label}, please wait.",
                           started=f"{label}.",
                           failed=f"{label} failed")


def _locked_dump():
    """One fresh model dump, taking the HID lock only for the dump itself.

    Passed to shows.wait_for so a forty-second wait does not hold the lock through its sleeps. See
    the note on shows.wait_for: holding it across the wait blocked the announcer and read as a
    frozen app."""
    with desk_access.hid_lock:
        return rcp_read.read_model_stable(fresh=True)


def _read_shows_now() -> tuple:
    """A FRESH shows list plus the blob it came from. Every command resolves its uid through this
    immediately before writing, because `showUID` is positional and an export renumbers the lot
    (shows.py trap 1)."""
    blob = rcp_read.read_model_stable(fresh=True)
    return shows.read_shows(blob), blob


def _export_and_wait(step, label: str) -> dict:
    """Export the desk and WAIT FOR THE FILE TO APPEAR. Shared by every path that needs a backup.

    Identifies the new show by a NEW SNAPSHOT INDEX appearing. It cannot be done by uuid: three
    exports of one desk state all carry the same uuid, so "a uuid we have not seen" never fires on
    the second export and every repeat backup is silently reported as a failure. It is not done by
    "uid 0" either, which is true but would also be satisfied by an export somebody else triggered
    from the touchscreen mid-wait.

    IT REFUSES BEFORE SENDING ANYTHING IF THERE IS NOWHERE TO WRITE, and this is the only place that
    has to, because it is the only place an export is sent from. `read_backup_storage` computes the
    same fact for the page, so the reset dialog can grey out its tick, but a check on that side alone
    leaves every non-UI route asking the impossible: the command goes out, the desk refuses it
    silently, and we poll the whole 136 kB model for up to 40 s waiting for a file that can never
    appear.

    THE LIVE CASE IS A DUO. `_clean_restore_reset` calls this unconditionally and the clean-restore
    gate only checks the shows CAPABILITY, which a Duo has, while a Duo has no removable storage at
    all - so a Duo owner choosing a clean profile restore gets a pointless command, a 40 second wait
    and a refusal.

    It also matters for the USB fault: a real export is the prime suspect for taking a desk off the
    bus, so not sending one that cannot succeed removes the risk from every case that was never going
    to work anyway.
    """
    with desk_access.hid_lock:
        before, blob = _read_shows_now()
        dest = recorder.destination(recorder.read_volumes(blob))
        if not dest:
            # `no_storage` so callers can word their own refusal for this specific cause - a clean
            # restore wants to point at the merge restore, which needs no backup at all.
            return {"ok": False, "count": len(before), "no_storage": True,
                    "message": _NO_BACKUP_STORAGE}
        known = {s["snapshot"] for s in before if s.get("snapshot") is not None}
        step(label)
        shows.send_export()

    def appeared(blob):
        for s in shows.read_shows(blob):
            if s.get("snapshot") is not None and s["snapshot"] not in known:
                return s
        return None
    found, _ = shows.wait_for(appeared, read=_locked_dump)
    if not found:
        return {"ok": False, "count": len(before),
                "message": ("The desk took the command but no new backup has appeared on the "
                            "drive. Check there is a drive in and that it has room.")}
    return {"ok": True, "show": found, "count": len(before) + 1}


# How long to listen for the reset's own confirmation. The whole push burst finishes by t+4.96 s on
# a Duo (measured), so this is generous rather than tight - the cost of it being too short is a
# needless fallback dump, and no Pro II figure has been taken yet.
_RESET_PUSH_WAIT_S = 20.0


def _reset_and_wait(step) -> dict:
    """Software-factory-reset the desk and wait for the evidence.

    The evidence is the LOADED SHOW BECOMING A DIFFERENT ONE: `newFromDefault` mints a new uuid, so
    a changed uuid is the desk confirming it in the only way it does.

    WE LISTEN FOR IT RATHER THAN ASK FOR IT. Re-dumping the whole property model every 1.5 s until
    the uuid changes is about 136 kB a time, six or seven times over, and it puts our traffic on the
    wire for the whole time the desk is rewriting its configuration and re-enumerating its USB audio
    function. The desk pushes `currentShowUUID` by itself 0.02 s after the command, and its whole
    burst is done by t+4.96 s (measured on a Duo), so the answer arrives sooner with nothing of ours
    in the way.

    THE HID LOCK IS HELD FOR THE WRITE ONLY, not across the wait: listening needs no lock, so an
    announcement or a page read does not queue behind this.

    A READ IS STILL THE FALLBACK. A missed push is not a failed reset, and hidapi makes no promise
    about a frame arriving, so a timeout costs one dump before anything is reported as wrong.
    """
    listener = desk_live.PushListener()
    try:
        with desk_access.hid_lock:
            before_uuid = shows.read_current(rcp_read.read_model_stable(fresh=True)).get("uuid")
            step("Resetting the desk to factory settings and pads...")
            listener.start()        # before the write: the push burst starts 20 ms after it
            shows.send_new_from_default()
        # NEVER LONGER THAN THE SHOW-COMMAND TIMEOUT ITSELF. Two independent waits for one command is
        # one too many: `shows.COMMAND_TIMEOUT_S` is the ceiling for "how long we give the desk", so
        # this listens for the shorter of the two and the offline sim's single knob still governs it.
        after = listener.wait_for("currentShowUUID",
                                  timeout=min(_RESET_PUSH_WAIT_S, shows.COMMAND_TIMEOUT_S),
                                  match=lambda v: v and v != before_uuid)
        listener_lost = listener.lost
        if after:
            # DO NOT ACT ON THE ANSWER THE INSTANT IT ARRIVES. The uuid lands at about t+1.8 s and
            # the desk carries on rewriting itself until t+4.96 s, so the wake below - which WRITES
            # to every mic channel - would otherwise land in the middle of that, and on a reset that
            # is also while the desk re-enumerates USB.
            step("Waiting for the desk to finish...")
            listener.wait_until_quiet()
    finally:
        listener.stop()

    # THE FALLBACK READ ALSO ANSWERS A SECOND QUESTION: is the desk still THERE? "It took the command
    # and did nothing" and "it has left the USB bus" are different failures needing different words:
    # only the second needs the cable unplugged.
    answered = None
    if not after:
        step("Checking the desk...")
        try:
            with desk_access.hid_lock:
                got = shows.read_current(rcp_read.read_model_stable(fresh=True)).get("uuid")
            answered = bool(got)
            after = got if (got and got != before_uuid) else None
        except Exception:
            answered = False

    if not after:
        if answered:
            # The desk is present and talking, so nothing needs replugging and nothing is lost.
            return {"ok": False,
                    "message": ("The desk took the command but its show has not changed, so the "
                                "reset does not look like it happened. Nothing has been lost; "
                                "check the Shows list in a moment.")}
        # Not answering: the command has almost certainly applied, and nothing about it can be
        # verified at all.
        return {"ok": False,
                "message": ("The desk stopped answering before it could confirm the reset"
                            + (", and the connection dropped. " if listener_lost else ". ")
                            + "The reset itself was almost certainly carried out, but nothing can "
                              "be checked while the desk is not responding. Unplug the USB cable "
                              "in the socket nearest the power inlet, wait a few seconds and plug "
                              "it back in, then look at the Shows list. If it still does not come "
                              "back, try that once or twice more, and if it will not appear at "
                              "all, disconnect the power, hold the red button on the back for "
                              "sixty seconds, then reconnect the power supply on its own and "
                              "switch the desk on. Nothing is lost either way.")}
    wake_channels()              # a reset leaves the same silent-channel fault; see wake_channels
    # THE OTHER THING A RESET LEAVES BROKEN - the voice-FX pads' effect definitions - IS HEALED BY
    # THE CALLER, NOT HERE, and deliberately. soundpad.heal_fx_pool rebuilds six pads at about
    # fifteen seconds each, and a CLEAN PROFILE RESTORE is about to rewrite the whole pad grid
    # anyway, so healing here would spend ninety seconds building pads that are cleared moments
    # later. Each caller runs it once its own pad phase is finished; it is a no-op when nothing is
    # missing, so the ordering costs nothing when the profile did carry the pads.
    return {"ok": True}


def _clean_restore_reset(step) -> dict:
    """The reset half of a CLEAN profile restore: back the desk up, then reset it.

    REFUSES TO RESET IF THE BACKUP DID NOT LAND. A clean restore is chosen precisely because it
    destroys what it does not replace, so proceeding without the undo would be the one outcome
    nobody asked for."""
    saved = _export_and_wait(step, "Backing the desk up, so the reset can be undone...")
    if not saved.get("ok"):
        if saved.get("no_storage"):
            # NAME THE OTHER OPTION. This is the Duo's permanent case, not a "put a drive in and try
            # again" case, so telling them to find storage would be telling them to buy hardware.
            return {"ok": False,
                    "message": (saved["message"] + " A clean restore has to back the desk up first, "
                                "because it wipes anything the profile does not cover, so it cannot "
                                "be done. Choose the ordinary restore instead: it puts your settings "
                                "back and leaves everything else as it is.")}
        return {"ok": False, "message": _RESET_STOPPED_NO_BACKUP}
    done = _reset_and_wait(step)
    if not done.get("ok"):
        return done
    return {"ok": True,
            "message": (f"Reset the desk to factory settings first, after backing it up as "
                        f"\"{saved['show']['name']}\".")}


def action_show_import(snapshot: int, as_name: str = "") -> dict:
    """Restore the whole desk from an exported show.

    `as_name` IS THE NAME THE USER GAVE IT, and it exists because the unit does not keep one: every
    export is named after whichever show was loaded, so a backup saved as "Testing New Backup Flow"
    is called "Default Show" on the drive. Reporting the desk's name therefore tells the user they
    have restored something they never chose, which is the worst sentence a restore can produce.
    The Backups page passes the name it holds; the Shows list has none to pass and falls back to
    the desk's.

    TAKES A SNAPSHOT INDEX, NOT A UID AND NOT A UUID. A uid is a position that any export renumbers,
    so a page holding one is holding something that may already mean a different file; a uuid names
    the SHOW rather than the file, so several backups can share one. The snapshot index is the only
    stable per-file handle (shows.py trap 2), and it is resolved to a uid against a read taken
    moments before the write, because the protocol only accepts a uid."""
    try:
        snapshot = int(snapshot)
    except (TypeError, ValueError):
        return {"ok": False, "message": "Choose which backup to restore."}

    def work(step):
        with desk_access.hid_lock:
            current, blob = _read_shows_now()
            match = shows.find_by_snapshot(current, snapshot)
            if match is None:
                return {"ok": False,
                        "message": "That backup is no longer on the drive. The list has been "
                                   "refreshed."}
            # NO UUID PRE-CHECK. Refusing a backup whose uuid matches the loaded show assumes the
            # unit would reject it, and THE UNIT DOES NOT: proven twice on hardware, where a backup
            # sharing the loaded uuid restored Mic 4's gain from 30 to 50 and again from 42 to 50,
            # with showControlLastError staying 0. Since a uuid only changes on a factory reset,
            # that refusal blocks the ordinary case this feature exists for - change something, put
            # it back - and leaves shows usable only straight after a reset. The unit's own refusal
            # (error 1) is reported below; we simply never guess it in advance.
            before = shows.restore_fingerprint(blob)
            err_before = shows.read_last_error(blob)
            shown = as_name or match["name"]     # see the docstring: the user's name wins
            step(f"Restoring \"{shown}\". This also moves the monitor level.")
            shows.send_import(match["uid"])
        target_uuid = match.get("uuid")
        loaded_uuid = shows.read_current(blob).get("uuid")

        # THE EVIDENCE IS THE STATE MOVING, not the uuid: see shows.restore_fingerprint for why
        # every other signal fails. The uuid test is kept as a second route because it is the one
        # that fires when the backup IS a different show (the undo for a factory reset).
        # ONLY A CODE THAT CHANGED IS THIS IMPORT'S ANSWER. The code latches, so one left by an
        # earlier refusal reads non-zero from the start; a refusal then shows as nothing moving, and
        # the timeout branch below reports it from the code still standing.
        def settled(blob):
            err = shows.read_last_error(blob)
            if err and err != err_before:
                return {"error": err}
            if target_uuid != loaded_uuid and shows.read_current(blob).get("uuid") == target_uuid:
                return {"done": True}
            if shows.restore_fingerprint(blob) != before:
                return {"done": True}
            return None
        got, blob_after = shows.wait_for(settled, read=_locked_dump)
        if got and got.get("error"):
            return {"ok": False, "message": shows.error_message(got["error"])}
        tail = ("Everything is back: settings, routing, the fader layout and the SMART pads. The "
                "monitor speaker level will have moved, so check it before turning speakers up.")
        if not got:
            # THE DESK NEVER ANSWERED AT ALL, which is a different outcome from "it did nothing"
            # and has to be said differently. `shows.wait_for` hands back no blob when not one
            # usable dump arrived in the whole wait, and that is what a desk leaving the bus
            # mid-restore looks like from here: it stops answering about 30 s after the import
            # command and Windows drops it a minute later.
            if blob_after is None:
                return {"ok": False,
                        "message": ("The desk stopped responding during the restore and is no "
                                    "longer answering. Unplug its USB cable and plug it back in, "
                                    "then check the Device page before trusting any setting.")}
            # Nothing moved and the unit raised no error. That is only benign for a backup of the
            # SAME show, where "nothing to do" is a real and reportable outcome rather than a
            # failure - saying "it did not work" would send someone hunting a problem that is not
            # there. For a DIFFERENT show it is a genuine failure, because a successful import
            # would have changed the loaded uuid and it did not.
            # A success clears the code to 0, so one still standing is a refusal.
            err_after = shows.read_last_error(blob_after)
            if err_after:
                return {"ok": False, "message": shows.error_message(err_after)}
            if target_uuid == loaded_uuid:
                return {"ok": True,
                        "message": (f"The desk already matched \"{shown}\", so nothing "
                                    f"needed to change.")}
            return {"ok": False,
                    "message": (f"The desk took the command but has not loaded "
                                f"\"{shown}\" yet. Check the list in a moment.")}
        # THE ENGINE CAN COME BACK WITH A CHANNEL SILENT. See wake_channels: the model looks
        # perfect, so this cannot be conditional on detecting anything.
        wake_channels()
        return {"ok": True, "message": f"Restored \"{shown}\". {tail}"}
    return _run_show_job("Restoring the show", work)


def wake_channels() -> dict:
    """Force the audio engine to re-latch every mic channel, after a reset or an import.

    THE FAULT THIS EXISTS FOR, reproduced deliberately on hardware: after a software factory reset
    followed by a show import, a mic channel can be COMPLETELY SILENT while the control model is
    byte-identical to its working state.
    Measured: 472 properties compared, 8 differed, and all 8 were the new backup's own list entry and
    the drive's free space. Level 1|1, chain matching the working reference exactly, unmuted, gain
    and phantom correct - and no audio. So NOTHING CAN DETECT THIS BY READING THE DESK, and any fix
    that displays a value is wasted effort.

    WHAT WAKES IT IS A REAL VALUE CHANGE, and only that. Writing the gain back at its CURRENT value
    does nothing at all (measured), which is why the desk's own encoder recovers it: an encoder
    sends a genuine change. So this nudges the gain by
    a step and puts it straight back.

    Kept to MIC channels because that is where a dead input is silently catastrophic and where we
    have a cheap real-value write. Whether the fault can hit a non-mic channel is UNKNOWN: nothing
    was plugged into the others when it was reproduced, so it could not be heard either way.
    """
    gate = desk_access.cap_gate("input_mic_write",
                     lambda name: f"Cannot wake the channels on a {name}.")
    if gate is not None:
        return gate
    # THE GAIN COMES FROM A FRESH READ, NOT FROM THE DESK MODEL. The "put it straight back" half of
    # the nudge has to write the value the desk currently holds: the only callers are a reset and
    # an import, exactly the two moments the cache is guaranteed stale. Sourcing it from the cache
    # would write the gain the app last displayed over the one the restore just put back.
    try:
        with desk_access.hid_lock:
            blob = rcp_read.read_model_stable(fresh=True)
    except Exception as exc:
        diagnostics.event("wake.read_failed", str(exc))
        blob = None
    if not rcp_read.is_model_dump(blob):
        return {"ok": False, "woken": [],
                "message": "The desk is not answering, so its mic channels could not be woken."}
    woken = []
    for ch in channels.MIC_CHANNELS:
        try:
            now = channels.mic_gain(blob, ch)
            if now is None:
                continue
            # A step DOWN unless that would fall off the bottom, so the nudge is always a real
            # change. Putting the original back immediately means the user never hears the step.
            other = now - 1 if now > channels.GAIN_MIN_DB else now + 1
            with desk_access.hid_lock:
                channels.set_mic_gain(ch, other)
                channels.set_mic_gain(ch, now)
            announcer.mark_self_gain(ch, True)
            woken.append(ch)
        except Exception as exc:          # one channel failing must not leave the rest unwoken
            diagnostics.event("wake.channel_failed", str(exc), mic=ch)
    return {"ok": True, "woken": woken,
            "message": (f"Nudged {wording.count(len(woken), 'mic channel')} so the desk's audio "
                        f"engine picks them up again." if woken
                        else "No mic channels needed waking.")}


def _restore_silence_warnings(prof, blob) -> list:
    """What this backup would leave you unable to hear, worked out BEFORE anything is sent.

    THE ONE WARNING THAT HAS TO ARRIVE EARLY. Everything the app says after a restore is spoken
    through the desk, so a restore that silences an output silences its own explanation: lose Main
    Stereo and the announcement telling you so goes with it. The confirmation dialog is read while
    the audio still works, and it can still be declined, so this is the only place a warning about
    silence is any use.

    TWO WAYS A BACKUP LEAVES A CHANNEL SILENT, and both are invisible afterwards:
      * it MOVES the channel to a different fader, and the level belongs to the fader rather than
        the channel, so it inherits whatever that strip was sitting at;
      * it sets that fader's level to zero outright.

    A backup with no `fader_levels` at all is the worse case, and most older ones are like that: it
    rearranges the layout and then has nothing to say about the levels it scrambled.
    """
    out = []
    try:
        spec = faders.active_faders()
        routing = prof.get("routing") or {}
        want = routing.get("faders")
        want_levels = routing.get("fader_levels")
        now_vec = faders.read_fader_vector(blob, spec)
        now_levels = rcp_read.values(blob, "faderLevel")

        def level_of(i, table):
            return table[i] if table and i < len(table) and isinstance(table[i], int) else None

        for i in spec.level_faders:
            code = want[i] if want and i < len(want) else (now_vec[i] if i < len(now_vec) else None)
            if code is None or code < 0:
                continue
            after = level_of(i, want_levels) if want_levels else level_of(i, now_levels)
            if after == 0:
                out.append(f"{faders.channel_name(code, spec)} will be silent: it ends up on "
                           f"{faders.fader_label(i, spec)}, which is at zero.")
        if want and list(want[:len(now_vec)]) != list(now_vec[:len(want)]):
            movers = [faders.channel_name(c, spec) for i, c in enumerate(want)
                      if i < len(now_vec) and c != now_vec[i] and c is not None and c >= 0]
            if movers:
                out.append("It rearranges the faders, moving "
                           + wording.join_and(sorted(set(movers))) + ".")
            if not want_levels:
                out.append("It does not set fader levels, so each channel keeps whatever level "
                           "the fader it lands on is already at.")
    except Exception:
        return out          # a warning that crashes must never block the restore it describes
    return out


def backup_restore_plan(name: str) -> dict:
    """How this backup WOULD be restored, without restoring it, so the confirmation can say what is
    about to happen, in the user's terms, BEFORE the user commits."""
    if name.startswith("show:"):
        # One the unit made, with no profile beside it: there is only one way to put it back, and
        # NOTHING CAN BE SAID ABOUT WHAT IS IN IT. The show file is the desk's own and we cannot
        # read it, so the honest answer is to say so rather than to imply we checked.
        return {"ok": True, "name": name, "via": "show", "paired": True,
                "seconds_per_pad": PAD_RESTORE_SECONDS, "pads": 0,
                "warnings": ["This backup was made by the desk itself, so there is no way to "
                             "tell you what it contains before it is restored. It may move "
                             "channels between faders or change their levels."]}
    loaded = profiles.load(name)
    if not loaded.get("ok"):
        return loaded
    prof = loaded["prof"]
    paired = prof.get(profiles.SHOW_KEY) or None
    blob = None
    try:
        with desk_access.hid_lock:
            blob = rcp_read.read_model()
    except Exception:
        blob = None
    # THE PROFILE IS READ EVEN WHEN THE SHOW WILL BE USED. The two halves were saved in the same
    # instant, so the profile is the only readable description of what the show is about to do.
    warnings = _restore_silence_warnings(prof, blob) if blob is not None else []
    return {
        "ok": True,
        "name": loaded["safe"],
        "via": "profile",             # always; see action_restore_backup
        "paired": bool(paired),
        "seconds_per_pad": PAD_RESTORE_SECONDS,
        "pads": len(prof.get("pads") or []),
        "warnings": warnings,
    }


def read_backups() -> dict:
    """THE ONE LIST BEHIND THE BACKUPS PAGE: every backup this desk can be put back from, whichever
    mechanism happens to hold it.

    The user types one name, presses one button and never learns there are two mechanisms; this is
    the read that lets the page keep that promise. Three kinds come out of it, and the kind is the
    page's business, not the user's:
      * a PAIRED backup - a profile on this computer plus the unit's own show on the drive. It is
        restored from the profile like any other (action_restore_backup); the show is a spare copy.
      * a PROFILE-ONLY backup - either there was no storage when it was taken, or the show has since
        been deleted or the drive erased.
      * a UNIT-MADE show with no profile beside it, which is one taken at the touchscreen or by
        RODE's own software. Named by the desk, not by anyone here, so it cannot be renamed.

    ONE DEVICE READ, NOT ONE PER BACKUP. `backup_restore_plan` answers "is the paired show still
    there" for a single name and reads the model to do it; asking it per profile would cost a dump
    each. The shows are read once and matched by snapshot."""
    names = profiles._list_profile_names()
    dev = devices.get_active_device()
    on_drive, storage, note = [], False, ""
    if "shows" in dev.caps:
        try:
            with desk_access.hid_lock:
                blob = rcp_read.read_model()
            on_drive = shows.read_shows(blob)
            storage = bool(recorder.destination(recorder.read_volumes(blob)))
        except Exception as exc:
            note = f"The desk's own backups could not be read: {exc}"
    by_snapshot = {s.get("snapshot"): s for s in on_drive}

    out, claimed = [], set()
    for safe in names:
        loaded = profiles.load(safe)
        if not loaded.get("ok"):
            continue
        prof = loaded["prof"]
        paired = prof.get(profiles.SHOW_KEY) or None
        snap = paired.get("snapshot") if paired else None
        show = by_snapshot.get(snap) if snap is not None else None
        if show is not None:
            claimed.add(snap)
        out.append({
            "key": "profile:" + safe,
            "name": safe,
            "kind": "paired" if show else "profile",
            "pads": len(prof.get("pads") or []),
            "saved_at": prof.get("saved_at"),
            "baseline": bool(prof.get("baseline")),
            "renameable": True,
            "snapshot": snap if show else None,
        })
    for s in on_drive:
        if s.get("snapshot") in claimed:
            continue
        out.append({
            # The DESK named this one, after whichever show was loaded, and it offers no rename, so
            # several can share a name. The modification date is the only thing that tells them
            # apart, which is why it is part of the label rather than a detail line.
            "key": "show:%s" % s.get("snapshot"),
            "name": s.get("name") or "Made on the desk",
            "kind": "unit",
            "pads": None,
            "saved_at": None,
            "modified": s.get("modified"),
            "baseline": False,
            "renameable": False,
            "snapshot": s.get("snapshot"),
        })
    return {"ok": True, "backups": out, "storage": storage, "shows_capable": "shows" in dev.caps,
            "note": note}


def action_delete_backup(name: str, force: bool = False) -> dict:
    """Delete a backup, BOTH HALVES OF IT. `name` is a profile name, or "show:<snapshot>" for one
    the unit made with no profile beside it.

    The page offers one Delete because the user made one backup; leaving the unit's copy behind
    would quietly fill the drive with 24 MB files they believe they have deleted. The show goes
    first: if that fails there is still a profile listed, whereas the reverse leaves an orphan
    nobody can name."""
    if name.startswith("show:"):
        try:
            snap = int(name.split(":", 1)[1])
        except ValueError:
            return {"ok": False, "message": "That backup could not be identified."}
        return action_show_delete(snap)
    loaded = profiles.load(name)
    if not loaded.get("ok"):
        return loaded
    # THE AS-FOUND BACKUP'S TWO-STEP SURVIVES THIS ROUTE, and it is asked BEFORE the unit's own
    # copy is touched. `profiles.delete` refuses the baseline without `force` and the page arms the
    # second press from that refusal; deleting the show first and then hitting the refusal would
    # destroy half of the one record of how the desk arrived, while reporting that nothing happened.
    if not force and profiles.is_baseline(loaded["prof"]):
        return profiles.delete(name, force=False)
    paired = (loaded["prof"].get(profiles.SHOW_KEY) or {}).get("snapshot")
    if paired is not None and "shows" in devices.get_active_device().caps:
        try:
            with desk_access.hid_lock:
                blob = rcp_read.read_model()
            still_there = shows.find_by_snapshot(shows.read_shows(blob), paired)
        except Exception:
            still_there = None
        if still_there is not None:
            # BOTH HALVES IN ONE JOB, so the page shows one progress dialog and one result. `via`
            # tells it which job state to watch, exactly as the restore does.
            res = _delete_show_then_profile(paired, name)
            res["via"] = "show"
            return res
    res = delete_profile(name, force=True)
    res["via"] = "profile"
    return res


def _other_desk_refusal(prof: dict) -> dict | None:
    """A refusal for a profile saved on another model of desk, or None. A profile stamps the product
    id of the desk it was saved on; its fader vector, matrix shape, channel set and pad grid are
    device-specific, so applying a Pro II profile to a Duo (or the reverse) would mis-write. Older
    profiles carry no product id (all saved on the Pro II) and are allowed, with the per-write
    guards still protecting."""
    saved_pid = prof.get("product_id")
    active = devices.get_active_device()
    if saved_pid is None or saved_pid == active.product_id:
        return None
    saved_name = prof.get("device_name") or devices.resolve_device(saved_pid).name
    return {"ok": False,
            "message": (f"This profile was saved from a {saved_name}, but a {active.name} is "
                        f"connected. Profiles are device-specific and cannot be restored across "
                        f"different RODECaster models.")}


def _pad_restore_words(pad_res: dict, extra: dict | None, tidy: dict | None) -> str:
    """What a pad restore did, for the end of its reply: what was rebuilt and what was already right,
    anything it could not restore, the pads it emptied, and the audio it tidied."""
    unchanged = pad_res.get("unchanged") or []
    rebuilt = sum(1 for r in pad_res["results"] if r.get("ok") and not r.get("unchanged"))
    total_pads = len(pad_res["results"])
    # What was left alone as well as what was written: a restore that finishes in seconds because
    # everything already matched must not read like it did nothing.
    if unchanged:
        msg = (f" SMART pads: {len(unchanged)} of {total_pads} were already exactly as saved and "
               f"were left alone, {rebuilt} rebuilt.")
    else:
        msg = f" Restored {rebuilt} of {total_pads} SMART pads."
    if pad_res.get("skipped"):
        msg += " Could not restore: " + "; ".join(pad_res["skipped"]) + "."
    if extra and extra["cleared"]:
        msg += (f" Cleared {wording.plural(len(extra['cleared']), 'pad')} "
                f"{wording.join_and(map(str, extra['cleared']))}, empty in the profile.")
    if extra and extra["failed"]:
        msg += (f" Could not clear {wording.plural(len(extra['failed']), 'pad')} "
                f"{wording.join_and(map(str, extra['failed']))}, which the profile has empty.")
    if tidy and tidy.get("removed"):
        msg += " " + tidy["message"]
    return msg


def action_restore_backup(name: str, clean: bool = False) -> dict:
    """RESTORE A BACKUP, from this computer's copy. See RESTORE_USES_THE_PROFILE below.

    THE SHOW IMPORT CRASHES THE DESK ABOUT ONE TIME IN SIX, so this does not use it. Measured on a
    Pro II with the diagnostic log running:

        PROFILE restores :   9   failed: 0   median 126s
        SHOW imports     :  31   failed: 5   median   6s

    Five crashes, on three different backups, with Bluetooth connected and disconnected. The desk
    hangs inside its own firmware, keeps a stuck audio buffer looping on the headphone output, stays
    electrically present on USB for another minute, and then needs its MAINS pulled - a USB replug
    does not bring it back, because the link is not the problem. Nothing we send is wrong and no
    change to our code prevents it; avoiding the path is the only fix available to us.

    WHY THAT IS WORTH THE COST, and the cost is real: the profile writes back 148 of 182 writable
    properties one at a time and takes two minutes against six seconds. What it misses that anyone
    would notice is the monitor output level and the auto-duck depth. Bluetooth pairing, the
    headline reason for shows existing at all, is NOT carried by a show either.
    Against that: the desk IS the screen reader's audio device for the people this app is for, so a
    desk that stops responding takes their speech with it, and an NVDA add-on that can escape to
    another output is not something everyone has.

    THE EXPORT HALF STAYS. It is the import that crashes; backing up to a show has never
    misbehaved, costs seconds, and leaves a complete copy on the drive if it is ever wanted.

    `name` may also be "show:<snapshot>", which is a backup the unit made with no profile beside it.
    That one has no profile to restore from, so it is the single remaining case that must use the
    import, and it says so before it runs."""
    # THE REPLY SAYS WHICH HALF RAN, in `via`, and the page needs it rather than merely liking it:
    # the two halves report progress through DIFFERENT job states (the show commands share one, the
    # profile save/restore another), so a page that guessed wrong would poll a job that is not
    # running and watch a finished restore in silence.
    if name.startswith("show:"):
        try:
            res = action_show_import(int(name.split(":", 1)[1]))
        except ValueError:
            return {"ok": False, "message": "That backup could not be identified."}
        res["via"] = "show"
        return res
    # "RESET THE UNIT TO FACTORY FIRST" IS A PROFILE-RESTORE MODE, so asking for it forces the
    # profile path even where a show is available. The show is an exact copy of the desk, which is
    # the opposite of what this mode is for: it exists to clear everything the profile does NOT
    # cover, so that what is left matches the profile and nothing else survives underneath.
    if clean:
        res = restore_profile(name, clean=True)
        res["via"] = "profile"
        return res
    # RESTORE_USES_THE_PROFILE, never the paired show, however available the show is; the docstring
    # holds the measurement behind that. The pairing is still made and still exported, so backing up
    # is unaffected and a paired backup is not stranded - only which half is read back.
    res = restore_profile(name)
    res["via"] = "profile"
    return res


def factory_backup_name() -> str:
    """The backup a factory reset leaves on this computer, one per model of desk."""
    desk = devices.get_active_device().name.removeprefix("RODECaster ").strip()
    return safe_name(f"Factory pads and settings {desk}"[:40])


def _keep_factory_pads(step) -> str:
    """Back the freshly reset desk up, pads and sounds included, so Restore the pads from a backup
    can put the factory pads back on their own later. The app ships none of RODE's sounds: each
    user's copy comes from their own desk. Saved over the last one, so a firmware update's factory
    pads replace the old ones. Returns a sentence for the reset's reply."""
    name = factory_backup_name()
    res = _do_snapshot(name, include_pads=True, prog=step)
    if not res.get("ok"):
        return (" The factory pads could not be kept on this computer: "
                + (res.get("message") or "the backup failed."))
    return (f" A copy of the factory pads is kept on this computer as {name}, and Restore the pads "
            "from a backup puts them back without resetting anything else.")


def action_show_reset(backup_first: bool = True) -> dict:
    """THE SOFTWARE FACTORY RESET: settings and pads back to factory, without the setup wizard.

    `backup_first` DEFAULTS TO TRUE and the UI does not offer False lightly, because an export is
    the only real undo this has: it costs about a second, and without it there is no way back to
    what the desk was.

    What survives: wifi, language and the analytics answer all live outside the show. Recordings on
    the drive are untouched.
    """
    def work(step):
        saved = None
        if backup_first:
            # REFUSE RATHER THAN CARRY ON if the backup does not land. Asked for a reset WITH a
            # backup, doing the reset anyway would destroy the desk's state having promised not to.
            # The user can still choose the unbacked reset explicitly.
            res = _export_and_wait(step, "Backing the desk up first, so the reset can be undone...")
            if not res.get("ok"):
                if res.get("no_storage"):
                    return {"ok": False,
                            "message": (res["message"] + " Nothing on the desk has changed. Either "
                                        "put a card or drive in, or choose the reset without a "
                                        "backup, which cannot be undone.")}
                return {"ok": False, "message": _RESET_STOPPED_NO_BACKUP}
            saved = res["show"]
        done = _reset_and_wait(step)
        if not done.get("ok"):
            return done
        # REBUILD THE VOICE EFFECTS THE DEFAULT SHOW DOES NOT BRING WITH IT. Without this the reset
        # hands back six voice-FX pads with the right name, type and colour that do nothing at all,
        # and says "factory SMART pads" while it does so - see soundpad.heal_fx_pool for the
        # mechanism and the captures that prove it.
        step("Rebuilding the factory voice effects...")
        with desk_access.hid_lock:
            healed = soundpad.heal_fx_pool(progress=step)
        pad_shortcuts.pads_stale()   # the pad grid was rewritten, so the shortcut layer's is stale
        msg = ("The desk is back to factory settings and factory SMART pads. Your wifi, language "
               "and the analytics answer were not part of it and are untouched, and so are the "
               "recordings on the drive. The monitor speaker level has gone back to its default, "
               "so check it before turning speakers up.")
        if healed.get("message"):
            msg += " " + healed["message"]
        msg += _keep_factory_pads(step)
        if saved:
            msg += f" To undo this, restore \"{saved['name']}\" from the Shows list."
        return {"ok": True, "message": msg}
    return _run_show_job("Resetting the desk", work)


def action_show_delete(snapshot: int) -> dict:
    """Delete one exported backup from the drive. By snapshot index (see action_show_import).

    Deleting the backup that happens to hold the LOADED show is ALLOWED. Refusing it treats the uuid
    as a file identity, which it is not (shows.py trap 2), so with several backups of the current
    state such a rule refuses to delete ANY of them - exactly the case where the drive fills up. A
    show FILE is not the loaded state, and deleting one does not disturb the desk."""
    try:
        snapshot = int(snapshot)
    except (TypeError, ValueError):
        return {"ok": False, "message": "Choose which backup to delete."}

    return _run_show_job("Deleting the backup",
                         lambda step: _show_delete_work(snapshot, step))


def _show_delete_work(snapshot: int, step) -> dict:
    """Take one backup off the drive and wait for it to be gone.

    RUNS INSIDE A SHOW JOB THAT THE CALLER OWNS, which is the whole reason it is not just the body
    of the function above: the unified delete has to remove the unit's copy AND this computer's in
    ONE job, so the user gets one progress dialog and one result for one backup."""
    with desk_access.hid_lock:
        current, blob = _read_shows_now()
        match = shows.find_by_snapshot(current, snapshot)
        if match is None:
            return {"ok": False,
                    "message": "That backup is no longer on the drive. The list has been "
                               "refreshed."}
        step(f"Deleting \"{match['name']}\"...")
        shows.send_delete(match["uid"])
    # The evidence is that SNAPSHOT INDEX leaving the list. A count drop alone would be fooled
    # by anything else removing a backup at the same moment.
    def gone(blob):
        return True if shows.find_by_snapshot(shows.read_shows(blob), snapshot) is None else None
    done, _ = shows.wait_for(gone, read=_locked_dump)
    if not done:
        return {"ok": False,
                "message": f"The desk took the command but \"{match['name']}\" is still on the "
                           f"drive."}
    return {"ok": True, "message": f"Deleted \"{match['name']}\" and gave the space back."}


def _delete_show_then_profile(snapshot: int, name: str):
    """Both halves of one backup, in one job: the unit's copy first, then this computer's.

    THE ORDER IS THE POINT. If the show delete fails, the profile is still listed and the user can
    try again; the other way round leaves 24 MB on the drive under a name nothing on this page knows
    about any more."""
    def work(step):
        res = _show_delete_work(snapshot, step)
        step("Deleting this computer's copy...")
        gone = delete_profile(name, force=True)
        if not gone.get("ok"):
            return gone
        tail = (" The desk's own copy went too." if res.get("ok")
                else " The desk's own copy could not be deleted, so it is still on the drive.")
        return {"ok": True, "message": (gone.get("message") or "Deleted.") + tail}
    return _run_show_job("Deleting the backup", work)


def pad_backups() -> dict:
    """The backups on this computer that hold SMART pads saved on this model of desk, newest first,
    for restoring their pads alone. Reads each profile once; the store is a handful of files."""
    active = devices.get_active_device()
    found = []
    for name in profiles.list_profiles()["profiles"]:
        loaded = profiles.load(name)
        if not loaded.get("ok"):
            continue
        prof = loaded["prof"]
        pads = len(prof.get("pads") or [])
        if pads and prof.get("product_id") in (None, active.product_id):
            found.append({"name": loaded["safe"], "pads": pads, "saved_at": prof.get("saved_at")})
    found.sort(key=lambda b: b["saved_at"] or 0, reverse=True)
    return {"ok": True, "backups": found, "seconds_per_pad": PAD_RESTORE_SECONDS}


def action_restore_backup_pads(name: str) -> dict:
    """Put a backup's SMART pads back and nothing else: every pad as the backup has it, a pad the
    backup has empty emptied, the rest of the desk untouched. A backup taken just after a factory
    reset makes this the way back to the factory pads, sounds included, which the app holds no copy
    of. Runs on the pad-restore progress channel, like the factory voice effects restore."""
    loaded = profiles.load(name)
    if not loaded.get("ok"):
        return loaded
    safe, prof = loaded["safe"], loaded["prof"]
    refusal = _other_desk_refusal(prof)
    if refusal:
        return refusal
    pad_list = prof.get("pads") or []
    if not pad_list:
        return {"ok": False, "message": f"{safe} has no SMART pads in it."}
    if _restore_job.busy():
        return {"ok": False, "message": "A profile restore is running. Try again when it finishes."}

    def _work(job):
        with desk_access.hid_lock:
            # One transfer session for every pad, as the full restore does.
            with rcp_transfer.transfer_session():
                pad_res = soundpad.restore_pads(pad_list, profiles.pads_dir(safe),
                                                progress=job.tick)
            # Outside the session: a clear only completes when the desk leaves transfer mode.
            extra = soundpad.clear_pads_not_in(pad_list, progress=job.step)
            # Unspoken: the result follows straight after and would cut it off, and says what it
            # removed.
            tidy = soundpad.tidy_unused_pad_audio()
        pad_shortcuts.pads_stale()
        return {"ok": True, "message": (f"The SMART pads from {safe} are back, and nothing else on "
                                        f"the desk was changed."
                                        + _pad_restore_words(pad_res, extra, tidy))}

    return pad_actions.restore_job.start(_work, total=len(pad_list),
                                  message=f"Restoring the SMART pads from {safe}.",
                                  failed="Could not restore the pads")


def read_desk_once() -> None:
    """The first read of the desk, adopted by the desk model, so every page and listener starts
    from the desk as it is. Raises when the desk cannot be read."""
    with desk_access.hid_lock:
        rcp_read.read_model_stable()
