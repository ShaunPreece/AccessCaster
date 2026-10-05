"""AccessCaster - the COMMAND REGISTRY: everything a keyboard shortcut can be pointed at.

WHY IT EXISTS. Anything the app offers elsewhere should be bindable to a key, and a hand-written
list cannot keep up with that. So this is not a list but a CATALOGUE GENERATED FROM THE SPEC TABLES
THE APP ALREADY HAS - the channel list, the fader layout, the pad
grid, the saved mix schemes, the mic presets. Add a channel, a pad or a scheme and it appears here
without anyone editing this file; that is the whole point, and it is why the catalogue is rebuilt on
demand rather than stored.

WHAT MAKES A GOOD SHORTCUT COMMAND, and the rule the catalogue follows: ONE PRESS, NO ARGUMENTS, NO
DECISION. So:
  - a TOGGLE becomes one command (mute a channel, phantom on a mic, MIDI, Bluetooth discoverable);
  - a NUMBER becomes a PAIR (monitor level up, monitor level down), stepping by a fixed amount,
    because a shortcut cannot ask you how far;
  - a ONE-SHOT stays as it is (start recording, drop a marker, apply a mix scheme).
Nothing destructive is offered. There is no "clear all pads", no restore, no factory reset: a global
hotkey is by definition pressable by accident, from any application, with no dialog in the way.

A COMMAND ID IS THE STABLE HANDLE, and it is what a shortcut stores: "chan.mute:Mic1",
"pad.fire:14", "scheme.apply:My Show". It survives the catalogue being rebuilt, a pad being renamed
and the app being restarted. Where an id names something that has since gone (a deleted scheme, a
cleared pad), `run` says so in a sentence rather than failing silently.

WHY IT IMPORTS `server` INSIDE THE FUNCTIONS. server.py imports this module, so a top-level import
back would be circular. Every dispatch happens long after both modules are loaded, so a local import
is free and keeps the arrow pointing one way.
"""

from __future__ import annotations

import channels
import devices
import recorder
import soundpad

# How far one press moves a level, as a percentage of full scale. Five is a real step you can hear
# without being a jump, and four presses make a fifth of the range.
LEVEL_STEP = 5

# The order categories appear in. The add dialog offers a category first, because the catalogue runs
# to well over a hundred entries on a full desk and one flat list of that is unusable by ear.
CATEGORIES = ["App", "Channels", "Levels", "Recording", "CallMe", "Pads", "Outputs", "Mixes",
              "Unit"]


def _cmd(cid, category, label):
    return {"id": cid, "category": category, "label": label}


# COMMANDS THAT ARRIVE SILENT, and the one place that judgement is made. A shortcut normally speaks
# what it did, because when you press one you are looking at something else and silence is what a
# broken shortcut sounds like. FIRING A PAD IS THE EXCEPTION: the pad itself is the feedback, it is
# loud, and it is often going out on air, so a voice over the top of every airhorn is worse than
# useless.
#
# Only the ACTION is silenced. A failure is spoken whatever this says (see HotkeyManager._run), the
# bank keys still speak because where you are is not audible, and "what is playing" is nothing but
# speech. The user can turn any of it back on per shortcut.
_SILENT_PREFIXES = ("pad.fire:", "pad.slot:")


def speaks_by_default(command_id: str) -> bool:
    return not str(command_id or "").startswith(_SILENT_PREFIXES)


def catalogue(include_device: bool = True) -> list:
    """Every command that can be bound, newest state included. One list, ordered by category.

    `include_device=False` skips everything that needs a model dump (pads, presets), so the
    shortcuts page can render instantly and fill the rest in when the desk answers."""
    out = []
    out += _app_commands()
    out += _channel_commands()
    out += _level_commands()
    out += _recording_commands()
    out += _callme_commands()
    out += _output_commands()
    out += _unit_commands()
    out += _scheme_commands()
    out += _pad_control_commands()
    if include_device:
        out += _pad_commands()
        out += _preset_commands()
    order = {c: i for i, c in enumerate(CATEGORIES)}
    out.sort(key=lambda c: (order.get(c["category"], 99), c["label"].lower()))
    return out


def label_for(command_id: str) -> str:
    """The label a stored id should show, WITHOUT rebuilding the whole catalogue where possible.

    A shortcut bound to something that no longer exists (a deleted mix scheme, a cleared pad) keeps
    its place in the list and SAYS so, rather than vanishing and leaving a key mysteriously taken."""
    for c in catalogue(include_device=True):
        if c["id"] == command_id:
            return c["label"]
    return f"{command_id} (not available now)"


def run(command_id: str) -> str:
    """Carry out a command and return the sentence to speak. Never raises for an unknown id."""
    # The namespace is up to the FIRST DOT; everything after it belongs to that namespace's own
    # parser. Colons separate a command from what it acts on ("chan.mute:Mic1"), so splitting on a
    # colon here would leave "app.announcer" unmatched.
    head, _, arg = (command_id or "").partition(".")
    fn = _DISPATCH.get(head)
    if fn is None:
        return f"{command_id} is not something this app can do."
    return fn(arg)


# ---------------------------------------------------------------------------------------------
# APP: the things that are about AccessCaster itself rather than the desk.
# ---------------------------------------------------------------------------------------------

def _app_commands() -> list:
    return [
        _cmd("app.announcer", "App", "Announcements on or off"),
        _cmd("app.mics", "App", "Mute or unmute every mic (cough key)"),
        # NOT "Say what the unit is doing", which promises more than it delivers (is it recording,
        # is it resetting). This label is what it actually speaks: the routing that differs from
        # usual, what is muted or soloed, and the monitor, plus a recording or a latched pad when
        # there is one to mention.
        _cmd("app.state", "App", "Current routing and levels"),
        _cmd("app.window", "App", "Open the AccessCaster window"),
        _cmd("app.volume.up", "App", f"Announcement volume up {LEVEL_STEP} percent"),
        _cmd("app.volume.down", "App", f"Announcement volume down {LEVEL_STEP} percent"),
    ]


def _run_app(arg: str) -> str:
    import server
    if arg == "announcer":
        return server._hk_announcer()
    if arg == "mics":
        return server._hk_mics()
    if arg == "state":
        return server._hk_state()
    if arg == "window":
        return server._hk_window()
    if arg in ("volume.up", "volume.down"):
        step = LEVEL_STEP if arg.endswith("up") else -LEVEL_STEP
        res = server.action_set_announce_volume(server._ann_volume + step)
        # The confirmation is spoken by the action itself, at the new level, which is the point of
        # it: you hear what you have chosen. Saying it twice would talk over that.
        return "" if res.get("ok") else res.get("message", "")
    return f"Unknown app command {arg}."


# ---------------------------------------------------------------------------------------------
# CHANNELS: one set per channel the connected desk actually has.
# ---------------------------------------------------------------------------------------------

def _channel_commands() -> list:
    out = []
    for ch in channels.active_ui_channels():
        name, label = ch["name"], ch["label"]
        out.append(_cmd(f"chan.mute:{name}", "Channels", f"Mute or unmute {label}"))
        out.append(_cmd(f"chan.solo:{name}", "Channels", f"Listen to {label} on its own, or stop"))
        out.append(_cmd(f"chan.proc:{name}", "Channels", f"Processing on or off for {label}"))
        if ch.get("kind") == "mic":
            out.append(_cmd(f"chan.phantom:{name}", "Channels",
                            f"48 volt phantom power on or off for {label}"))
    out.append(_cmd("chan.solos.clear", "Channels", "Clear every solo"))
    return out


def _muted_now(name: str) -> bool:
    """Whether a channel is muted. The announcer's push-fed cache answers for free; a dump is the
    fallback when it has nothing, the same trade the cough key makes."""
    import server
    muted = (server._ann_live_state or {}).get("muted") or {}
    strip = channels.channel_strip_index(name)
    if strip in muted:
        return bool(muted[strip])
    return _channel_read(name).get("muted", False)


def _channel_read(name: str) -> dict:
    """One channel's state from a fresh read: mute, solo, processing and phantom."""
    import server
    res = server.action_get_channels()
    for c in res.get("channels", []):
        if c.get("name") == name:
            return {"muted": bool(c.get("muted")), "solo": bool(c.get("solo")),
                    "processing_on": bool(c.get("processing_on")),
                    "phantom": bool(c.get("phantom"))}
    return {}


def _run_chan(arg: str) -> str:
    import server
    what, _, name = arg.partition(":")
    if what == "solos.clear":
        return server.action_clear_solos().get("message", "")
    ch = channels.ui_channel_map().get(name)
    if ch is None:
        return f"This unit has no channel called {name}."
    if what == "mute":
        return server.action_set_channel_mute(name, not _muted_now(name)).get("message", "")
    # Solo, processing and phantom have no push-fed cache, so each reads the channel first. About a
    # second and a half, and worth it: a shortcut that guesses the current state and writes the
    # opposite is a shortcut that does the wrong thing every other press.
    full = _channel_read(name)
    if what == "solo":
        return server.action_set_channel_solo(name, not full.get("solo", False)).get("message", "")
    if what == "proc":
        return server.action_set_processing_enabled(
            name, not full.get("processing_on", False)).get("message", "")
    if what == "phantom":
        mic = channels.mic_channel_number(ch)
        return server.action_set_phantom(mic, not full.get("phantom", False)).get("message", "")
    return f"Unknown channel command {what}."


# ---------------------------------------------------------------------------------------------
# LEVELS: the numbers, offered as up/down pairs because a key cannot ask "how far".
# ---------------------------------------------------------------------------------------------

def _level_commands() -> list:
    out = [
        _cmd("level.monitor.up", "Levels", f"Monitor level up {LEVEL_STEP} percent"),
        _cmd("level.monitor.down", "Levels", f"Monitor level down {LEVEL_STEP} percent"),
    ]
    spec = devices.get_active_device().fader
    for i in range(spec.count):
        n = i + 1
        out.append(_cmd(f"level.fader.up:{n}", "Levels", f"Fader {n} up {LEVEL_STEP} percent"))
        out.append(_cmd(f"level.fader.down:{n}", "Levels", f"Fader {n} down {LEVEL_STEP} percent"))
    return out


def _known_level(kind: str, idx: int):
    """A level from the announcer's own cache, 0-1, or None. The cache is fed by the desk's pushes,
    so it is both free and current whenever the announcer is running."""
    import server
    st = server._ann_live_state
    known = (st or {}).get("known") or {}
    return known.get((kind, idx))


def _run_level(arg: str) -> str:
    import server
    parts = arg.split(".")
    what = parts[0]
    direction = parts[1] if len(parts) > 1 else ""
    step = LEVEL_STEP if direction.startswith("up") else -LEVEL_STEP
    if what == "monitor":
        now = _known_level("monitor", 0)
        if now is None:
            live = server.fader_levels_live()
            pct = live.get("monitor_percent")
            now = (pct / 100.0) if isinstance(pct, int) else None
        if now is None:
            return "The monitor level could not be read."
        return server.action_set_monitor_speaker_level(
            max(0, min(100, int(round(now * 100)) + step))).get("message", "")
    if what == "fader":
        _dir, _, num = direction.partition(":")
        try:
            fader = int(num)
        except ValueError:
            return f"{num} is not a fader."
        now = _known_level("fader", fader - 1)
        if now is None:
            live = server.fader_levels_live()
            for f in live.get("faders", []):
                if f.get("fader") == fader and isinstance(f.get("percent"), int):
                    now = f["percent"] / 100.0
        if now is None:
            return f"Fader {fader}'s level could not be read."
        return server.action_set_fader_level(
            fader, max(0, min(100, int(round(now * 100)) + step))).get("message", "")
    return f"Unknown level command {arg}."


# ---------------------------------------------------------------------------------------------
# RECORDING
# ---------------------------------------------------------------------------------------------

def _recording_commands() -> list:
    return [
        _cmd("rec.toggle", "Recording", "Start or stop recording"),
        _cmd("rec.pause", "Recording", "Pause or resume the recording"),
        _cmd("rec.marker", "Recording", "Drop a marker in the recording"),
    ]


def _run_rec(arg: str) -> str:
    import server
    if arg == "toggle":
        return server._hk_record()
    if arg == "marker":
        return server.action_drop_marker().get("message", "")
    if arg == "pause":
        live = server.read_recorder_live()
        paused = live.get("state") == recorder.PAUSED
        return server.action_record("resume" if paused else "pause").get("message", "")
    return f"Unknown recording command {arg}."


# ---------------------------------------------------------------------------------------------
# CALLME: a call has to be answerable from whatever application has focus.
# ---------------------------------------------------------------------------------------------

def _callme_commands() -> list:
    return [
        _cmd("callme.answer", "CallMe", "Answer the CallMe call, or extend one about to end"),
        _cmd("callme.decline", "CallMe", "Decline the CallMe call"),
        _cmd("callme.hangup", "CallMe", "Hang up the CallMe call"),
        _cmd("callme.end", "CallMe", "Decline the ringing CallMe call, or else hang up"),
        _cmd("callme.extend", "CallMe", "Extend the CallMe call before the hour runs out"),
        _cmd("callme.status", "CallMe", "Say who is calling and who is connected"),
        _cmd("callme.hosting", "CallMe", "CallMe hosting on or off"),
    ] + _callme_contact_commands()


def _callme_contact_commands() -> list:
    # BOUND BY NAME, NOT BY CODE: a code is what gets edited when its owner's changes, and the
    # shortcut has to survive that edit.
    import server
    return [_cmd(f"callme.contact:{c['name']}", "CallMe", f"Call {c['name']}")
            for c in server._callme_contacts()]


def _run_callme(arg: str) -> str:
    import server
    if arg.startswith("contact:"):
        name = arg.partition(":")[2]
        c = next((c for c in server._callme_contacts() if c["name"].lower() == name.lower()), None)
        if c is None:
            return f"There is no contact called {name} any more."
        return server.action_callme_dial(c["code"]).get("message", "")
    if arg == "answer":
        return server.action_callme_answer().get("message", "")
    if arg == "decline":
        return server.action_callme_decline().get("message", "")
    if arg == "hangup":
        return server.action_callme_hangup().get("message", "")
    if arg == "end":
        return server.action_callme_end().get("message", "")
    if arg == "extend":
        return server.action_callme_extend().get("message", "")
    if arg == "status":
        return server.callme_status_sentence()
    if arg == "hosting":
        now, _path = server._callme_state_now()
        return server.action_callme_hosting(not now.get("hosting")).get("message", "")
    return f"Unknown CallMe command {arg}."


# ---------------------------------------------------------------------------------------------
# OUTPUTS
# ---------------------------------------------------------------------------------------------

def _output_commands() -> list:
    return [
        _cmd("out.mute:monitor", "Outputs", "Mute or unmute the monitor speakers"),
        _cmd("out.mute:bluetooth", "Outputs", "Mute or unmute the Bluetooth output"),
        _cmd("out.automute:monitor", "Outputs", "Monitor auto-mute on or off"),
        _cmd("out.automute:bt", "Outputs", "Bluetooth auto-mute on or off"),
    ]


def _run_out(arg: str) -> str:
    import server
    what, _, which = arg.partition(":")
    if what == "mute":
        # The two output mutes are UNIT SETTINGS (that is the write path action_set_output_mute
        # uses), so their current value comes from the unit-settings read, not the routing one.
        prop = "outputMonMute" if which == "monitor" else "outputBTMute"
        now = False
        for s in server.read_unit_settings().get("settings", []):
            if s.get("prop") == prop:
                now = bool(s.get("value"))
        return server.action_set_output_mute(which, not now).get("message", "")
    if what == "automute":
        live = server.read_routing_live()
        key = "monitor_auto_mute" if which == "monitor" else "bt_auto_mute"
        return server.action_set_auto_mute(which, not bool(live.get(key))).get("message", "")
    return f"Unknown output command {what}."


# ---------------------------------------------------------------------------------------------
# UNIT: the desk's own switches that are a plain on/off.
# ---------------------------------------------------------------------------------------------

def _unit_commands() -> list:
    return [
        _cmd("unit.midi", "Unit", "MIDI control on or off"),
        _cmd("unit.wifi", "Unit", "Wifi on or off"),
        _cmd("unit.bt", "Unit", "Make the unit discoverable over Bluetooth, or stop"),
    ]


def _run_unit(arg: str) -> str:
    import server
    if arg == "midi":
        now = bool(server.read_unit_settings().get("midi"))
        return server.action_set_midi(not now).get("message", "")
    net = server.read_network_status()
    if arg == "wifi":
        now = bool((net.get("wifi") or {}).get("on"))
        return server.action_set_wifi(not now).get("message", "")
    if arg == "bt":
        now = bool((net.get("bluetooth") or {}).get("visible"))
        return server.action_set_bt_visible(not now).get("message", "")
    return f"Unknown unit command {arg}."


# ---------------------------------------------------------------------------------------------
# MIXES: the saved and built-in output-mix schemes, by name.
# ---------------------------------------------------------------------------------------------

def _scheme_commands() -> list:
    import server
    out = []
    try:
        schemes = server.read_routing_schemes()
    except Exception:
        return out
    # A BUILT-IN IS APPLIED BY ITS id, a saved one by its name: that is the split
    # `action_apply_routing_scheme` already takes, and it is why the two are separate namespaces
    # here rather than one list of names.
    for s in schemes.get("builtins", []) or []:
        if s.get("destructive"):
            # Not offered on a key. A scheme that rewrites the whole matrix is not something to put
            # one accidental keypress away from, from inside any application.
            continue
        out.append(_cmd(f"scheme.builtin:{s['id']}", "Mixes", f"Apply the {s['name']} mix"))
    for s in schemes.get("saved", []) or []:
        out.append(_cmd(f"scheme.saved:{s['name']}", "Mixes", f"Apply the saved mix {s['name']}"))
    return out


def _run_scheme(arg: str) -> str:
    import server
    kind, _, name = arg.partition(":")
    res = server.action_apply_routing_scheme(name, builtin=(kind == "builtin"))
    return res.get("message", "")


# ---------------------------------------------------------------------------------------------
# PADS: every type that does something when fired from software, which since MIDI is all of them.
# ---------------------------------------------------------------------------------------------
# SOUND PADS ARE HERE BECAUSE OF MIDI, NOT BECAUSE HID CAN FIRE THEM. Writing `padActive` switches
# a sound pad's flag and the unit plays nothing - an ear-proven negative, and it still stands. MIDI
# lifts that limit. Voice-effect and mixer pads go over HID, where the flag IS the state; the
# dispatch is per pad in server._hk_fire_pad.
PAD_TYPES = (soundpad.PAD_TYPE_SOUND, soundpad.PAD_TYPE_VOICE_FX, soundpad.PAD_TYPE_MIXER)


def _pad_control_commands() -> list:
    """The pad commands that name a PLACE rather than a pad, so none of them needs a model dump and
    they can all render before the desk has answered.

    TWO KINDS OF PAD COMMAND, and the difference matters when choosing one. `pad.fire` is PINNED: it
    names a padIdx, so it plays the same sound for ever, wherever the desk happens to be. `pad.slot`
    MOVES: it plays pad N of whatever bank you are on, which is what makes a row of number keys
    reach every pad on the desk instead of only the first eight."""
    # N is per-device: 8 on a Pro II, 6 on a Duo, from the device table and never hard-coded. A Duo
    # is not offered a seventh, so a shortcut that could only ever say "there is no pad 7" cannot be
    # created in the first place.
    per_bank = devices.get_active_device().pad.pads_per_bank
    out = [_cmd("pad.playing", "Pads", "Say what is playing"),
           _cmd("pad.bank:down", "Pads", "Pad bank down"),
           _cmd("pad.bank:up", "Pads", "Pad bank up")]
    out += [_cmd(f"pad.slot:{n}", "Pads", f"Play pad {n} of the current bank")
            for n in range(1, per_bank + 1)]
    return out


def _pad_commands() -> list:
    """One command per assigned pad, pinned to that pad for ever. Needs a model dump."""
    import server
    out = []
    per_bank = devices.get_active_device().pad.pads_per_bank
    try:
        pads = server._hk_refresh_pads()
    except Exception:
        return out
    for pad_idx in sorted(pads):
        _pos, name, pad_type, _mode = pads[pad_idx]
        if not name or pad_type not in PAD_TYPES:
            continue
        bank, pad = pad_idx // per_bank + 1, pad_idx % per_bank + 1
        kind = soundpad.PAD_TYPE_LABEL.get(pad_type, "Pad")
        out.append(_cmd(f"pad.fire:{pad_idx}", "Pads",
                        f"{name} ({kind}, bank {bank} pad {pad})"))
    return out


def _run_pad(arg: str) -> str:
    import server
    what, _, target = arg.partition(":")
    if what == "slot":
        return server._hk_pad_slot(target)
    if what == "bank":
        return server._hk_bank_step(target)
    if what == "playing":
        return server._hk_playing_now(target)
    return server._hk_pad(target)


# ---------------------------------------------------------------------------------------------
# PRESETS: a mic voicing applied to a channel.
# ---------------------------------------------------------------------------------------------

def _preset_commands() -> list:
    import server
    out = []
    try:
        presets = server.preset_list()
    except Exception:
        return out
    mics = [c for c in channels.active_ui_channels() if c.get("kind") == "mic"]
    for p in presets:
        for ch in mics:
            out.append(_cmd(f"preset.apply:{ch['name']}:{p['name']}", "Channels",
                            f"Apply the {p['name']} preset to {ch['label']}"))
    return out


def _run_preset(arg: str) -> str:
    import server
    _what, _, rest = arg.partition(":")
    name, _, preset = rest.partition(":")
    ch = channels.ui_channel_map().get(name)
    if ch is None or ch.get("kind") != "mic":
        return f"{name} is not a microphone channel on this unit."
    return server.action_apply_mic_preset(channels.mic_channel_number(ch), preset).get("message", "")


_DISPATCH = {
    "app": _run_app,
    "chan": _run_chan,
    "level": _run_level,
    "rec": _run_rec,
    "callme": _run_callme,
    "out": _run_out,
    "unit": _run_unit,
    "scheme": _run_scheme,
    "pad": _run_pad,
    "preset": _run_preset,
}
