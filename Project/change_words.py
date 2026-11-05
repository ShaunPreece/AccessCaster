"""A change on the desk in words: what the announcer says when something is changed elsewhere.

`describe` turns one changed property into a `Change`, or None when it has no words here; the
announcer gathers a burst of them and `summarise` makes the burst one sentence. ONE SENTENCE,
because speech keeps only the newest phrase waiting: a phrase each for a preset's thirty
parameters would leave only the last one heard.

Names come from the desk model (which channel is on which fader) and the connected device (what
each source and output is called), so they follow the layout and the desk attached. Labels for
settings come from `desk_labels`, the same tables the window builds its controls from.
"""
from __future__ import annotations

from typing import NamedTuple, Optional

import channels
import desk_labels
import desk_model
import devices
import rcp_transfer
import wording

# A whole output's mute, addressed at the OUTPUT block rather than a strip. The monitor's is what an
# encoder hold on the monitor level toggles.
OUTPUT_MUTES = {"outputMonMute": "Monitor speakers", "outputBTMute": "Bluetooth output"}
# Pushed alongside a change that is already worded, so never news of their own: a virtual fader's
# level comes with its cross-points, and the preset marker with the parameters it names.
NOT_NEWS = frozenset({"faderLevel", channels.CHAIN_PRESET_MARKER_PROP})
# More changes than this to one channel's processing in one burst (a preset, or advanced
# processing switched) are counted rather than read out.
GROUP_ITEMS_MAX = 3
# More phrases than this in one burst, and the rest are counted.
PHRASES_MAX = 6

_POT_MAX = 127.0


class Change(NamedTuple):
    """One worded change. `target` is what changed: a later change to the same target in the same
    burst replaces this one. Changes sharing a `group` are counted when there are many. `source`
    is the matrix source a fader, level or send change concerns."""
    target: tuple
    words: str
    group: Optional[tuple] = None
    source: Optional[int] = None


# ---- names -----------------------------------------------------------------------------------

def source_name(code: int) -> str:
    """A matrix SOURCE code as it is spoken, e.g. 'Mic 1'.

    `labels_for`, NOT `code_label`: a fader can HOLD a stereo-linked pair (codes 4, 5, 6) even
    though a pair is never something you assign, and code_label covers only the assignable set, so
    a linked Mic 1 + 2 would be announced as "Source 4"."""
    try:
        dev = devices.get_active_device()
        label = dev.fader.labels_for(channels.usb_expanded()).get(code)
        if label:
            return label
        for s in dev.matrix.sources_for(True):
            if s.get("index") == code:
                return s.get("label") or f"source {code}"
    except Exception:
        pass
    return f"Source {code}"


def strip_name(strip: int) -> str:
    """The channel on a strip, e.g. 'Mic 2', from the desk model's fader layout, however old: the
    last layout seen beats 'Channel N', which is said when no layout has been seen or it does not
    name that slot."""
    try:
        vec = desk_model.strip_vector()
        if vec and 0 <= strip < len(vec):
            # labels_for, so a linked pair is named rather than spoken as a bare code, and the
            # Expanded four are named rather than announced as "Source 12".
            label = devices.get_active_device().fader.labels_for(
                channels.usb_expanded()).get(vec[strip])
            if label:
                return label
    except Exception:
        pass
    return f"Channel {strip + 1}"


def output_name(index: int) -> str:
    """An output of the matrix, e.g. 'Headphone 4'."""
    try:
        for out in devices.get_active_device().matrix.outputs:
            if out["index"] == index:
                return out["label"]
    except Exception:
        pass
    return f"Output {index + 1}"


def gain_name(key, is_mic: bool):
    """The channel whose GAIN moved, from the INPUTSOURCE key it was pushed at, or None.

    Mic strips carry 'gain'; line strips carry 'level', which is what the app and the desk call them,
    so the spoken phrase matches the control the operator knows.

    Matched against DeviceModel.input_key: the announcer and the writer read the same rule, so they
    cannot drift. A per-device selector table would miss every gain turned on a Duo's own controls."""
    dev = devices.get_active_device()
    entry = dev.channel_for_input_key(key)
    if entry is None:
        return None
    if is_mic:
        if entry.get("kind") != "mic":
            return None
        return f"Mic {channels.mic_channel_number(entry)} gain"
    if entry.get("kind") != "line":
        return None
    # RESOLVED from the wide set above, NAMED from the current mode: the entry that an address
    # matches is fixed, what it is called is not.
    label = (dev.fader.labels_for(channels.usb_expanded()).get(entry.get("source_code"))
             or entry.get("label") or entry.get("name"))
    return f"{label} input level"


def _input_name(key) -> Optional[str]:
    """The channel an INPUTSOURCE key belongs to, or None."""
    if not key or key[0] != "INPUTSOURCE":
        return None
    try:
        entry = devices.get_active_device().channel_for_input_key(key)
    except Exception:
        entry = None
    return source_name(entry["source_code"]) if entry else source_name(key[1])


# ---- words -----------------------------------------------------------------------------------

def percent(level) -> int:
    """A 0 to 1 level as the whole percent spoken."""
    return max(0, min(100, int(round(float(level) * 100))))


def level_words(name: str, level: float, *, name_first: bool, say_percent: bool = True) -> str:
    """THE one place a level is turned into words.

    A LEVEL MOVED -> number first: "USB 2 (Main Stereo)" takes about a second, and each new update
    cuts off the phrase before it, so a name first means the number never arrives until the
    movement stops. SOMETHING WAS SELECTED -> name first: nothing has moved yet, and the question is
    what you have got. `say_percent` is dropped during a run of updates, where the unit is obvious."""
    pct = percent(level)
    if name_first:
        return f"{name} {pct} percent" if say_percent else f"{name} {pct}"
    return f"{pct} percent, {name}" if say_percent else f"{pct}, {name}"


def _on_off(value) -> str:
    return "on" if value else "off"


def _cell(value):
    """A mixLevelWithAnchor value as (level, linked), or None when it does not parse."""
    if not isinstance(value, str) or "|" not in value:
        return None
    level, anchor = value.split("|", 1)
    try:
        return float(level), level == anchor
    except ValueError:
        return None


def describe(name: str, value, kind: str, key, was) -> Optional[Change]:
    """`name` at `key` changed from `was` to `value`: the words for it, or None when there is
    nothing to say. A value the same as `was`, or no `was` at all, is no change: the desk re-pushes
    values it already had, and this app's own writes reach the desk model before their echo."""
    if was is None or was == value or name in NOT_NEWS:
        return None
    block = key[0] if key else None
    strip = key[1] if block == "CHANNEL" else None

    if name == "channelInputSource" and strip is not None:
        placed = isinstance(value, int) and value >= 0
        return Change(("fader", strip),
                      f"Fader {strip + 1} is now {source_name(value) if placed else 'empty'}",
                      source=value if placed else None)
    if strip is not None:
        who = strip_name(strip)
        if name == channels.MUTE_PROP:
            return Change(("mute", strip), f"{who} {'muted' if value else 'unmuted'}")
        if name == channels.CUE_PROP:
            return Change(("solo", strip), f"{who} solo {_on_off(value)}")
        if name == channels.WIRELESS_MUTE_PROP:
            return Change(("wireless", strip),
                          f"{who} wireless mic {'muted' if value else 'unmuted'}")
        if name == "channelBypassProcessing":
            # True is bypassed: processing OFF.
            return Change((name, strip), f"{who} processing {_on_off(not value)}",
                          ("processing", strip))
        if name == channels.CHAIN_ADVANCED_PROP:
            return Change((name, strip), f"{who} advanced processing {_on_off(value)}",
                          ("processing", strip))
        if name in desk_labels.MODULE_LABEL_BY_PROP:
            return Change((name, strip),
                          f"{who} {desk_labels.MODULE_LABEL_BY_PROP[name].lower()} "
                          f"{_on_off(value)}", ("processing", strip))
        if name in desk_labels.PROCESSING_SPOKEN and kind == "float":
            pct = percent(value)
            said = (desk_labels.pan_words(pct) if name in desk_labels.PAN_PROPS
                    else f"{pct} percent")
            return Change((name, strip),
                          f"{who} {desk_labels.PROCESSING_SPOKEN[name].lower()} {said}",
                          ("processing", strip))
        return None

    if block == "EFFECTS_PARAMETERS":
        who = strip_name(key[1])
        effect = next((e for e, p in channels.CHANNEL_FX_ON.items() if p == name), None)
        if effect:
            return Change((name, key[1]), f"{who} {effect} {_on_off(value)}", ("effects", key[1]))
        if name in desk_labels.EFFECT_SPOKEN and kind == "float":
            return Change((name, key[1]),
                          f"{who} {desk_labels.EFFECT_SPOKEN[name]} {percent(value)} percent",
                          ("effects", key[1]))
        return None

    if block == "INPUTSOURCE":
        if name in ("inputMicrophoneGain", "inputDigitalGain"):
            who = gain_name(key, name == "inputMicrophoneGain")
            return Change((name, key[1]), f"{value} d B, {who}") if who else None
        who = _input_name(key)
        if name == "inputPower":
            return Change((name, key[1]), f"{who} phantom power {_on_off(value)}")
        if name == "inputPhaseFlip":
            return Change((name, key[1]),
                          f"{who} polarity {'inverted' if value else 'normal'}")
        if name == "inputMicrophoneType":
            return Change((name, key[1]),
                          f"{who} mic type {channels.MIC_TYPE.get(value, value)}")
        return None

    if name == "mixLevelWithAnchor":
        cell, before = rcp_transfer.cross_point_of(key), _cell(was)
        now = _cell(value)
        if cell is None or now is None:
            return None
        out, src = cell
        level, linked = now
        if linked:
            # A linked cross-point follows the fader: the change is the fader's own level.
            return Change(("level", src), level_words(source_name(src), level, name_first=False),
                          source=src)
        if before is not None and not before[1] and percent(before[0]) == percent(level):
            return None          # only the fader under it moved
        return Change(("send", out, src), level_words(
            f"{source_name(src)} in {output_name(out)}", level, name_first=False), source=src)
    if name == "mixDisabled":
        cell = rcp_transfer.cross_point_of(key)
        if cell is None:
            return None
        out, src = cell
        return Change(("send-on", out, src),
                      f"{source_name(src)} {'taken out of' if value else 'added to'} "
                      f"{output_name(out)}", source=src)
    if name == "outputMixMinus" and block == "MIXMINUSES":
        mode = desk_labels.OUTPUT_MODE_LABEL.get(value, value)
        return Change((name, key[1]), f"{output_name(key[1])} set to {mode}")

    if name in OUTPUT_MUTES:
        return Change((name,), f"{OUTPUT_MUTES[name]} {'muted' if value else 'unmuted'}")
    if name == "outputMonLevel" and kind == "float":
        return Change((name,), level_words("Monitor", value, name_first=False))
    if name == "potLevel" and key and key[:3] == ("PHYSICALINTERFACE", 0, "POT"):
        return Change((name, key[3]), level_words(
            f"Headphone {key[3] + 1}", max(0.0, min(1.0, value / _POT_MAX)), name_first=False))
    if name == "headphoneType" and block == "HEADPHONE":
        spec = devices.get_active_device().headphones
        out = dict(spec.outputs).get(key[1], f"Headphone {key[1] + 1}")
        return Change((name, key[1]), f"{out} type {dict(spec.values).get(value, value)}")
    if name in desk_labels.OUTPUT_SWITCH_LABEL:
        return Change((name,), f"{desk_labels.OUTPUT_SWITCH_LABEL[name]} {_on_off(value)}")
    spec = desk_labels.UNIT_SETTING_BY_PROP.get(name)
    if spec is not None:
        return Change((name,), f"{spec['label']}: {desk_labels.unit_setting_words(spec, value)}")
    return None


# What a strip holds for the channel on it, which moves with the channel to another fader.
_STRIP_TARGETS = frozenset({"mute", "solo", "wireless"})
# What a source holds in the mixes, which RODE's app rewrites as it moves the source.
_SOURCE_TARGETS = frozenset({"level", "send", "send-on"})


def _without_what_moved(changes) -> list:
    """`changes` less what a channel moving fader carries with it: its strip's processing, effects,
    mute and solo arrive on the new strip, and its levels and sends are rewritten, so with the move
    said those are not news."""
    moved = [c for c in changes if c.target[0] == "fader"]
    if not moved:
        return list(changes)
    strips = {c.target[1] for c in moved}
    sources = {c.source for c in moved if c.source is not None}
    return [c for c in changes
            if not ((c.group is not None and c.group[1] in strips)
                    or (c.target[0] in _STRIP_TARGETS and c.target[1] in strips)
                    or (c.target[0] in _SOURCE_TARGETS and c.source in sources))]


def summarise(changes, unworded: int = 0) -> str:
    """A burst of `Change`s, in the order they came, as one sentence. A group with more than
    GROUP_ITEMS_MAX changes is counted under its channel's name; past PHRASES_MAX phrases the rest
    are counted with `unworded`, the changes with no words of their own. Empty for no changes."""
    changes = _without_what_moved(changes)
    groups: dict = {}
    for c in changes:
        if c.group is not None:
            groups.setdefault(c.group, []).append(c)
    phrases, counted = [], set()
    for c in changes:
        members = groups.get(c.group) if c.group is not None else None
        if members and len(members) > GROUP_ITEMS_MAX:
            if c.group not in counted:
                counted.add(c.group)
                kind, strip = c.group
                phrases.append(f"{strip_name(strip)}: "
                               f"{wording.count(len(members), f'{kind} setting')} changed")
            continue
        phrases.append(c.words)
    more = max(0, len(phrases) - PHRASES_MAX) + unworded
    phrases = phrases[:PHRASES_MAX]
    if more:
        phrases.append(f"{wording.count(more, 'other setting')} changed" if phrases
                       else f"{wording.count(more, 'setting')} changed")
    return ". ".join(phrases) + "." if phrases else ""
