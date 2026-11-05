"""
AccessCaster - the mute and the solo, by channel (the Channels page and the shortcuts) and by fader
(the Levels page's rows), with the wireless mic's mute and the encoder hold's toggle.

Every reply says what the change REACHES: a mute names any output that still hears the channel, and
a solo names everything the headphones now carry. Each write is marked as ours with the announcer,
so the desk's echo of it is not spoken over the window's own reply.
"""

from __future__ import annotations

import announcer
import change_words
import channels
import desk_access
import diagnostics
import faders
import output_mixes
import rcp_read
import rcp_transfer
import wording


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
    """Mute or unmute a channel from software (channelOutputMute, a confirmed write).

    The real accessibility win here is reach: this is the same mute as the button on the unit, so it
    can be pressed from a shortcut instead of by finding the right button by touch.

    Two details matter. It silences the channel in every mix that follows the fader, not just the
    monitor, and the message names any output it does NOT reach (_mute_reach_message). And the
    write is marked as ours (announcer.mark_self_mute), or the desk's echo of the mute would be
    spoken on top of the UI's own announcement."""
    ch = channels.ui_channel(name)
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
    them is the same one that decides every other module toggle. Tying them to
    faders.level_write_gate conflates two unrelated things: the Duo's faderLevel selectors are
    unconfirmed, which says nothing about its mute.

    The Channels page's by-name mute reaches the same gate through channels._require_processing;
    this is the by-fader route's half, so the two cannot drift apart."""
    return desk_access.cap_gate("processing", lambda name:
                     f"{what} a channel is not enabled on the {name} yet, so this is read-only "
                     f"here. You can still see which channels are muted or soloed.")


def _write_fader_toggle(fader: int, prop: str, on: bool, gate_word: str, noun: str) -> tuple:
    """The by-fader mute and solo up to the reply: the gate, the range, the refusal for an empty
    fader, and the write of `prop` on the fader's strip. Returns (refusal, None, None), or
    (None, the channel code on the fader, the fader spec)."""
    gate = _strip_toggle_gate(gate_word)
    if gate is not None:
        return gate, None, None
    spec = faders.active_faders()
    fader = int(fader)
    if not 0 <= fader < spec.count:
        return {"ok": False, "message": f"Fader {fader + 1} is not valid."}, None, None
    try:
        with desk_access.hid_lock:
            vec = faders.read_fader_vector(spec=spec)
            code = vec[fader] if fader < len(vec) else None
            if faders.is_empty_fader(code):
                empty = (f"{faders.fader_label(fader, spec)} has no channel on it, so there is "
                         f"nothing to {noun}. Put a channel on it first.")
                return {"ok": False, "message": empty}, None, None
            rcp_transfer.write_at(channels.proc_key_for_strip(fader), prop, "bool", bool(on))
    except Exception as exc:
        return {"ok": False, "message": f"Could not change the {noun}: {exc}"}, None, None
    return None, code, spec


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
    refusal, code, spec = _write_fader_toggle(fader, channels.MUTE_PROP, on, "Muting", "mute")
    if refusal is not None:
        return refusal
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
    refusal, code, spec = _write_fader_toggle(fader, channels.CUE_PROP, on, "Solo", "solo")
    if refusal is not None:
        return refusal
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
    ch = channels.ui_channel(name)
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
    correctly (the property sits on the same CHANNEL node as the mute) but is unverified with a
    paired transmitter, and a channel with nothing paired has nothing to obey it. Claiming "the mic is muted" would be the app reading its own write back as an
    outcome."""
    ch = channels.ui_channel(name)
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
