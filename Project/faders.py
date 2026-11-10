"""
AccessCaster - the faders: which channel sits on each fader (the Faders page), with stereo links and
the chain and level following a moved channel; and the Levels page's writes, the virtual faders, the
monitor speaker, the two output mutes and the Bluetooth output; and putting the encoder back on the
virtual faders. The per-desk shape is devices.FaderSpec, the writes rcp_transfer.
"""

from __future__ import annotations

import time

import announcer
import channels
import desk_access
import device_page
import devices
import rcp_read
import rcp_transfer
import wording

# --- FADER ASSIGNMENT (which channel sits on which fader) ---------------------
# The fader layout IS the channelInputSource vector (see RODECASTER_PROTOCOL.txt section 6):
# index 0 = fader 1 ... value = channel code on that fader.
# We read the layout from the vector and write it with rcp_transfer.set_fader_source
# (CHANNEL[index]). The per-device SHAPE (vector length, physical/virtual split, assignable channel
# list + labels, factory-default vector) lives in devices.FaderSpec: the Pro II has 9
# faders and Mic 1-4; the Duo has a 10-entry vector, code 2 = Headset (not Mic 3), no Mic3/Mic4.


def active_faders():
    """The FaderSpec for the currently-connected device (devices.py)."""
    return devices.get_active_device().fader


def chain_follow_on() -> bool:
    """Whether "the chain follows the channel" is enabled for the connected device.

    Gated behind its own cap rather than riding on "fader_write", because the Duo has swap cases the
    Pro II does not: its default layout contains an EMPTY strip (-1) and a DUPLICATED source (USB 2
    twice), and index() on a duplicate finds only the first. A desk with the layout write but not
    this cap moves the layout and leaves the chain on its strip."""
    return "chain_follow" in devices.get_active_device().caps


def follow_chains(old_vec, new_vec, chains_before) -> str:
    """Move each channel's processing/effects chain to the strip it landed on, then VERIFY.

    Called with the HID lock held, immediately after the layout write. `chains_before` is the
    per-strip chain snapshot taken from the dump BEFORE that write, so for every source that moved we
    know exactly which chain belongs to it. Only the DIFFERENCES are written (a real swap is about
    13 writes and a second); a blind full write would be ~110.

    THREE CASES, not one. A channel can move between strips, LEAVE the layout, or RETURN to it. A
    channel that leaves has its chain parked (channels.park_chain), because the strip it vacated is
    about to be overwritten by whoever moves in and the desk keeps no per-channel copy. A channel
    that returns gets that parked chain written onto its new strip, or it silently inherits the
    previous occupant's voicing. Leaving and returning is not an edge case: it is the ordinary path
    for stereo link, which evicts the upper mic of a pair and hands it back on unlink.

    Returns a sentence for the user: what moved, or precisely what did not. Never raises - a fader
    move that succeeded must not be reported as a failure because the chain step had a problem."""
    try:
        # Where did each source live, and where does it live now? Sources present in both layouts and
        # sitting on a DIFFERENT strip are the ones whose chain has to travel.
        old_at = {code: i for i, code in enumerate(old_vec) if code is not None and code >= 0}
        new_at = {code: i for i, code in enumerate(new_vec) if code is not None and code >= 0}
        moves = []
        for code, i in new_at.items():
            src = old_at.get(code)
            if src is not None and src != i and src < len(chains_before) and i < len(chains_before):
                moves.append((code, src, i))
        # PARK FIRST, off the pre-write snapshot: once the moves below start writing, the vacated
        # strip's chain is gone for good.
        device_id = _layout_store_key()
        parked = []
        for code, src in old_at.items():
            if code not in new_at and src < len(chains_before):
                channels.park_chain(device_id, code, chains_before[src])
                parked.append(code)
        # A channel with no strip before and a strip now: hand back what we parked when it left.
        returns = []
        homeless = []
        for code, dst in new_at.items():
            if code in old_at or dst >= len(chains_before):
                continue
            chain = channels.take_chain(device_id, code)
            if chain is None:
                homeless.append(code)      # never saw it leave (moved off-fader by the RODE
            else:                          # app or the touchscreen, which we cannot see)
                returns.append((code, dst, chain))
        if not moves and not returns:
            return _chain_note(spec=active_faders(), parked=parked, homeless=homeless)
        written = 0
        for _code, src, dst in moves:
            # `current` is what physically sits on the destination strip right now, i.e. the previous
            # occupant's chain, so the diff is exactly the handful of values that actually differ.
            res = channels.write_chain(dst, chains_before[src], chains_before[dst])
            written += res["written"]
        for _code, dst, chain in returns:
            res = channels.write_chain(dst, chain, chains_before[dst])
            written += res["written"]
        # Read back and check every moved chain landed. This is the half of the job that makes a
        # partial write safe to report honestly.
        after = channels.read_chains(rcp_read.read_model(fresh=True))
        spec = active_faders()
        bad = []
        for code, src, dst in moves:
            if dst >= len(after) or channels.chain_diff(channels.fit_chain(chains_before[src], dst),
                                                        after[dst]):
                bad.append(channel_name(code, spec))
        for code, dst, chain in returns:
            if dst >= len(after) or channels.chain_diff(channels.fit_chain(chain, dst), after[dst]):
                bad.append(channel_name(code, spec))
                channels.park_chain(device_id, code, chain)   # put it back; it did not land
        if bad:
            return (f"WARNING: the processing chain did not fully follow {wording.join_and(bad)} "
                    f"({written} of the writes landed). Set that channel's processing again, or "
                    f"restore a profile.")
        return _chain_note(spec=spec, parked=parked, homeless=homeless,
                           moved=[c for c, _s, _d in moves],
                           restored=[c for c, _d, _c in returns])
    except Exception as exc:
        return (f"WARNING: the fader layout changed, but moving the processing chain failed ({exc}). "
                f"Check the affected channels' processing.")


def _chain_note(spec, parked=(), homeless=(), moved=(), restored=()) -> str:
    """The one sentence the user hears about what happened to the processing. Says what was KEPT for
    an off-fader channel as well as what moved: "off-fader" otherwise reads as "deleted", and the
    whole point of parking is that it is not."""
    def names(codes):
        return wording.join_and(channel_name(c, spec) for c in codes)

    def own(codes):
        return "their own" if len(codes) > 1 else "its own"

    out = ""
    if moved and restored:
        out = (f"Processing and effects followed {names(moved)}, and {names(restored)} got "
               f"{own(restored)} processing back.")
    elif moved:
        out = f"Processing and effects followed {names(moved)}."
    elif restored:
        out = f"{names(restored)} got {own(restored)} processing back."
    if parked:
        # Says only what happened to the PROCESSING. action_set_fader already reports the layout
        # side ("X is now off-fader"), and hearing the same clause twice in one announcement is
        # worse than terse.
        many = len(parked) > 1
        out += (f" The processing for {names(parked)} is kept, and comes back when "
                f"{'they get faders' if many else 'it gets a fader'} again.")
    if homeless:
        many = len(homeless) > 1
        out += (f" {names(homeless)} had no stored processing, so "
                f"{'each is' if many else 'it is'} using whatever was already on "
                f"{'its' if many else 'that'} fader; set {own(homeless)} processing to taste.")
    return out.strip()


def fader_write_gate():
    """Fader-assignment WRITES. Reads and labels are always device-aware and safe; writes gate
    because a device whose fader write model is only partly known must refuse rather than write a
    guessed layout."""
    return desk_access.cap_gate("fader_write", lambda name:
                     f"Changing the fader layout is not enabled on the {name} yet, so the faders "
                     f"are read-only here. You can still see the current layout.")


def is_empty_fader(code) -> bool:
    """True for an empty/unsourced fader strip. The Duo's vector carries -1 for an empty
    logical strip; None appears when the read vector is shorter than the fader count."""
    return code is None or code == -1


def fader_label(i: int, spec=None) -> str:
    spec = spec or active_faders()
    return f"Fader {i + 1}" + ("" if i < spec.physical else " (virtual)")


def channel_name(code, spec=None) -> str:
    # labels_for, not code_label: a fader can HOLD a stereo-linked pair even though a pair is never
    # something you assign, and "channel code 4" is no use to anyone reading the layout aloud.
    spec = spec or active_faders()
    if is_empty_fader(code):
        return "empty (no channel)"
    return spec.labels_for(channels.usb_expanded()).get(code, f"channel code {code}")


def follow_levels(old_vec, new_vec, levels_before, spec) -> str:
    """Move each channel's LEVEL to the fader it landed on, the way follow_chains moves its chain.

    WHY IT EXISTS. A fader's level belongs to the STRIP, not to the channel sitting on it, so a
    reassignment silently hands a channel whatever level that strip happens to be at. A backup that
    moves Main Stereo from fader 8 onto fader 7 lands it on whatever 7 was sitting at, zero in the
    worst case, while the channel that took fader 8 carries on at 95. Every reading is then clean -
    unmuted, in the matrix, on a fader, layout exactly as the backup says - because nothing IS
    wrong. The level has simply not travelled with the channel, and a level is not visible as a
    fault; it is just silence.

    The app already accepts that a channel's settings should follow the channel: that is the whole
    reason `follow_chains` and the off-fader chain store exist. The level is the only one of them
    that can silence somebody without saying so.

    ONLY BETWEEN FADERS THAT BOTH HAVE A LEVEL CONTROL, i.e. the virtual ones. A physical fader's
    position is physical: it cannot be read and it cannot be written, so a channel arriving from one
    brings no level with it and putting a number there would be inventing state. Those cases are the
    restore warning's job, not this one's.

    Written from the PRE-WRITE snapshot, so a swap works: each channel takes the level it had, not
    whatever the other one has just been given.

    Never raises. A layout change that succeeded must not be reported as a failure because this
    step had a problem - the same rule `follow_chains` follows, and for the same reason.
    """
    try:
        old_at = {code: i for i, code in enumerate(old_vec) if code is not None and code >= 0}
        new_at = {code: i for i, code in enumerate(new_vec) if code is not None and code >= 0}
        moved = []
        for code, dst in new_at.items():
            src = old_at.get(code)
            if src is None or src == dst:
                continue
            if src not in spec.level_faders or dst not in spec.level_faders:
                continue
            was = levels_before[src] if src < len(levels_before) else None
            now = levels_before[dst] if dst < len(levels_before) else None
            if not isinstance(was, int) or was == now:
                continue
            rcp_transfer.set_fader_level(dst, was)
            moved.append((code, dst, was))
        if not moved:
            return ""
        pct = [(c, d, round(v * 100 / rcp_transfer.FADER_LEVEL_MAX)) for c, d, v in moved]
        return " " + " ".join(
            f"{channel_name(c, spec)} kept its level of {p} percent on "
            f"{fader_label(d, spec)}." for c, d, p in pct)
    except Exception as exc:
        return f" The channel moved but its level did not follow it: {exc}"


def read_fader_vector(blob=None, spec=None):
    """The current channel code on each fader, from channelInputSource (device-length).

    Every layout write re-reads through here straight afterwards. That dump is what makes the desk
    model trusted again, since our own write of a fader source untrusts it (`desk_model.STRUCTURAL`)."""
    spec = spec or active_faders()
    if blob is None:
        blob = rcp_read.read_model()
    cis = rcp_read.strip_values(blob, "channelInputSource")
    return [cis[i] if i < len(cis) else None for i in range(spec.count)]


def _fader_channel_options(spec) -> list:
    """[code, name] for every channel a fader may take in the USB mode last published. It changes
    with the mode (Expanded adds Game, Music, A and B and renames USB 1), so the live read carries it
    as well as the static model."""
    expanded = channels.usb_expanded()
    labels = spec.labels_for(expanded)
    return [[code, labels.get(code, str(code))] for code in spec.code_order_for(expanded)]


def fader_model() -> dict:
    """Static layout the Fader assignment UI renders: the connected device's faders + the
    channels that can be placed on them (device-aware, see devices.FaderSpec)."""
    spec = active_faders()
    if spec.physical < spec.count:
        phys_note = (f" Faders 1 to {spec.physical} are the physical faders; the rest are "
                     f"virtual faders.")
    else:
        phys_note = ""
    return {
        "faders": [{"index": i, "label": fader_label(i, spec), "physical": i < spec.physical}
                   for i in range(spec.count)],
        # THE DROPDOWN AND THE WRITE GATE MUST OFFER THE SAME SET, or Expanded's four inputs are
        # either refused after being chosen or missing from the list that can choose them. Both
        # come off `code_..._for`, so there is one answer to "what may go on a fader".
        "channels": _fader_channel_options(spec),
        "read_only": fader_write_gate() is not None,
        # The PAIRS are static (which links exist, and what to call them); whether each is currently
        # linked is live state and arrives with the layout read, not from here.
        "stereo_pairs": [{"code": code, "label": label,
                          "lower": channel_name(lo, spec), "upper": channel_name(up, spec)}
                         for code, lo, up, label in spec.stereo_pairs],
        "stereo_read_only": _stereo_link_gate() is not None,
        "stereo_note": ("Linking two inputs puts both on one fader, panned hard left and right, so "
                        "one fader controls the pair. Only inputs next to each other can link. The "
                        "fader the second input leaves becomes empty, and unlinking gives that input "
                        "its fader and its processing back."),
        # NEVER WRITE A FADER ON `change`. A closed select fires on EVERY arrow press, and arrowing
        # a closed select is how a screen-reader user reads one, so writing on change means simply
        # browsing the list performs the swap. Each fader applies on Enter.
        "note": ("Choose which channel sits on each fader." + phys_note + " Putting a channel "
                 "on a fader that already holds it elsewhere swaps the two. Nothing is sent to the "
                 "desk until you press Enter."),
    }


def _settle_mutes(new_vec, wanted: dict) -> list:
    """Write channelOutputMute on the strip each code in `wanted` now sits on, where the desk left
    it different. Returns the codes this OPENED, so the reply can say a mic is live.

    THE DESK'S MUTES DO NOT FOLLOW THE INPUTS: a strip has kept its own mute through a link, so the
    pair inherited the lower mic's, and has also been seen clearing it. Neither is relied on; the
    outcome is read back and settled. A code with no strip (an upper mic left off-fader) is skipped."""
    mutes = rcp_read.values(rcp_read.read_model(fresh=True), "channelOutputMute")
    opened = []
    for code, muted in wanted.items():
        strip = next((i for i, c in enumerate(new_vec) if c == code), None)
        if strip is None or strip >= len(mutes) or bool(mutes[strip]) == muted:
            continue
        rcp_transfer.write_at(channels.proc_key_for_strip(strip), channels.MUTE_PROP, "bool", muted)
        if not muted:
            opened.append(code)
    return opened


def _unlink_mutes(kept, pair_muted: bool, lower: int, upper: int) -> dict:
    """Each mic's mute after an unlink. `kept` is `channels.take_link_mutes`' record.

    A pair whose mute is unchanged since the link was never muted or opened by anybody, so each mic
    gets back what it had: a muted Mic 3 linked with an open Mic 4 unlinks the same way. A pair muted
    or opened while linked was a choice, and both mics keep it. With no record (linked outside the
    app) the pair's mute is all there is to go on."""
    if kept and kept.get("pair") == pair_muted:
        return {lower: bool(kept.get("lower")), upper: bool(kept.get("upper"))}
    return {lower: pair_muted, upper: pair_muted}


def _layout_store_key() -> str:
    """The desk key for the layout parking stores. Falls back to the model name because
    `device_page.hardware_id` is "" until the first status read: parking under one key and looking
    under another would lose the record."""
    return device_page.hardware_id() or devices.get_active_device().name


def _stereo_link_gate():
    """Stereo-link WRITES. Two conditions, not one: the device must claim the cap AND carry a proven
    pair table, so a future device that gets the cap by copy-paste still refuses until its codes are
    captured rather than writing a Pro II code at a desk that means something else by it."""
    gate = desk_access.cap_gate("stereo_link", lambda name:
                     f"Linking two inputs into a stereo pair is not enabled on the {name} yet, so "
                     f"this is read-only here.")
    if gate is not None:
        return gate
    if not active_faders().stereo_pairs:
        dev = devices.get_active_device()
        return {"ok": False,
                "message": (f"Linking two inputs into a stereo pair is not available on the "
                            f"{dev.name}: its pair codes have not been confirmed.")}
    return None


def stereo_link_state(vec=None, spec=None) -> list:
    """Each legal pair and whether it is currently linked, for the UI. Read-only, and it takes the
    already-read vector when there is one so opening the page costs no extra dump."""
    spec = spec or active_faders()
    vec = vec if vec is not None else read_fader_vector(spec=spec)
    out = []
    for code, lo, up, label in spec.stereo_pairs:
        out.append({"code": code, "label": label, "linked": code in (vec or []),
                    "lower": channel_name(lo, spec), "upper": channel_name(up, spec)})
    return out


def action_set_stereo_link(pair_code: int, on: bool) -> dict:
    """Link or unlink one adjacent mic pair (RODECASTER_PROTOCOL 6.5).

    LINKING is two vacates and one write: the pair code goes onto the LOWER mic's fader and the other
    fader is emptied, because a linked pair occupies one fader rather than two. That is the desk's
    own shape; the RODE app sends two more records, which are cosmetic: writing the source code
    alone leaves the desk byte-identical.

    UNLINKING is where we deliberately DIVERGE from the desk. It returns only the lower mic and
    abandons the upper one off-fader, which on the RODE app leaves you with a mic you cannot hear and
    no indication why. We put the upper mic back on the fader the link emptied when that fader is
    still free, so a link followed by an unlink is a true round trip. With its processing restored by
    the off-fader chain store, the desk ends up where it started. When there is no free fader we say
    so rather than displacing something the user chose.

    Processing follows both ways through follow_chains: linking parks BOTH mics' chains (the pair
    keeps the lower mic's strip voicing, which is what the desk does), and unlinking hands each one
    back.

    Mutes follow the same way. A pair is muted if EITHER mic was, so a link never opens a mic the
    user silenced; each mic's own mute is parked (`channels.park_link_mutes`) and `_unlink_mutes`
    decides what the unlink hands back."""
    gate = _stereo_link_gate()
    if gate is not None:
        return gate
    spec = active_faders()
    pair = spec.pair_map.get(int(pair_code))
    if pair is None:
        return {"ok": False, "message": f"{pair_code} is not a stereo pair on this desk."}
    lower, upper = pair
    label = spec.pair_label.get(int(pair_code), f"pair {pair_code}")
    chain_note = mute_note = ""

    def names(codes):
        return wording.join_and(channel_name(c, spec) for c in codes)

    try:
        with desk_access.hid_lock:
            blob = rcp_read.read_model()
            vec = read_fader_vector(blob, spec)
            target = list(vec)
            if on:
                if int(pair_code) in vec:
                    return {"ok": True, "faders": vec,
                            "labels": [channel_name(c, spec) for c in vec],
                            "stereo": stereo_link_state(vec, spec),
                            "message": f"{label} is already linked."}
                # The pair lands where the LOWER mic sits. If neither mic is on a fader (both were
                # displaced earlier) the first empty strip will do; with none we refuse rather than
                # evict a channel the user put there on purpose.
                seat = next((i for i, c in enumerate(target) if c == lower), None)
                if seat is None:
                    seat = next((i for i, c in enumerate(target) if c == upper), None)
                if seat is None:
                    seat = next((i for i, c in enumerate(target) if is_empty_fader(c)), None)
                if seat is None:
                    return {"ok": False,
                            "message": (f"There is no free fader for {label}. Move a channel off a "
                                        f"fader first, then link.")}
                for i, c in enumerate(target):
                    if c in (lower, upper):
                        target[i] = -1
                target[seat] = int(pair_code)
            else:
                seat = next((i for i, c in enumerate(target) if c == int(pair_code)), None)
                if seat is None:
                    return {"ok": True, "faders": vec,
                            "labels": [channel_name(c, spec) for c in vec],
                            "stereo": stereo_link_state(vec, spec),
                            "message": f"{label} is not linked."}
                target[seat] = lower
                spare = next((i for i, c in enumerate(target) if is_empty_fader(c)), None)
                if spare is not None:
                    target[spare] = upper
            # Mutes are read before the write, since a linked mic has no strip of its own to read
            # afterwards.
            mutes = rcp_read.values(blob, "channelOutputMute")

            def muted_now(code):
                i = next((j for j, c in enumerate(vec) if c == code), None)
                return bool(mutes[i]) if i is not None and i < len(mutes) else False

            lower_muted, upper_muted, pair_muted = (muted_now(lower), muted_now(upper),
                                                    muted_now(int(pair_code)))
            chains_before = channels.read_chains(blob) if chain_follow_on() else None
            for i in range(spec.count):
                if target[i] != vec[i]:
                    rcp_transfer.set_fader_source(i, target[i])
            new = read_fader_vector(spec=spec)
            if chains_before is not None:
                chain_note = follow_chains(vec, new, chains_before)
            if on:
                pair_muted = lower_muted or upper_muted
                _settle_mutes(new, {int(pair_code): pair_muted})
                channels.park_link_mutes(_layout_store_key(), pair_code, lower_muted,
                                         upper_muted, pair_muted)
                if pair_muted:
                    because = [] if lower_muted and upper_muted else [
                        lower if lower_muted else upper]
                    mute_note = (f" {label} is muted, because {names(because)} was." if because
                                 else f" {label} is muted.")
            else:
                wanted = _unlink_mutes(channels.take_link_mutes(_layout_store_key(), pair_code),
                                       pair_muted, lower, upper)
                opened = _settle_mutes(new, wanted)
                muted = [c for c in (lower, upper) if wanted[c] and c in new]
                if muted:
                    mute_note += f" {names(muted)} {'is' if len(muted) == 1 else 'are'} muted."
                if opened:
                    mute_note += (f" {names(opened)} {'is' if len(opened) == 1 else 'are'} open, "
                                  f"as before the link.")
    except Exception as exc:
        verb = "link" if on else "unlink"
        return {"ok": False, "message": f"Could not {verb} {label}: {exc}"}
    if on:
        msg = (f"{label} linked, on {fader_label(new.index(int(pair_code)), spec)}. "
               f"One fader now carries both inputs.")
    else:
        msg = f"{label} unlinked."
        if upper in new:
            msg += f" {channel_name(upper, spec)} is back on {fader_label(new.index(upper), spec)}."
        else:
            msg += (f" {channel_name(upper, spec)} has no free fader to go back to, so it is "
                    f"off-fader; put it on a fader yourself when you have one spare.")
    if chain_note:
        msg += " " + chain_note
    msg += mute_note
    return {"ok": True, "faders": new, "labels": [channel_name(c, spec) for c in new],
            "stereo": stereo_link_state(new, spec), "message": msg}


def _tidy_fader_target(vec, fader_index, code, order):
    """Return the target fader vector that places `code` on `fader_index` while keeping a
    VALID layout: no duplicate assignable source, no orphaned channel that is off-fader yet
    still in the mix. The channel displaced from `fader_index` moves onto wherever `code` used
    to sit (a true swap, matching a hardware drag). If the prior layout was already messy (code
    on 2+ faders, or pre-existing duplicates), each leftover duplicate slot is filled with an
    assignable source currently missing, so the result is always clean. EMPTY strips (Duo -1 /
    None) are legitimate and left untouched: they are never treated as duplicates nor forced to
    hold a source (a device can have more strips than assignable sources). `order` is the
    device's assignable-code display order (the gap-fill order)."""
    target = list(vec)
    old = target[fader_index]
    target[fader_index] = code
    # The swap: the channel we displaced goes to the first slot that still holds `code`.
    #
    # AND WHEN WE DISPLACED NOTHING, THAT SLOT IS VACATED, which is what `old` already is. Do NOT
    # guard this with `and not is_empty_fader(old)`: moving a channel onto an EMPTY fader then
    # leaves it on its old strip as well, and the dedup loop below resolves that duplicate by
    # overwriting the slot the user just asked for - "put the Headset on fader 9" leaves the Headset
    # on fader 5 and puts another channel on fader 9. Only a desk with more faders than sources can
    # reach it, which is why it is invisible on a Pro II.
    dup_positions = [j for j in range(len(target)) if j != fader_index and target[j] == code]
    if dup_positions:
        target[dup_positions[0]] = old
    # Clean up any remaining duplicates (extra copies, or a pre-existing mess) by handing each
    # later duplicate an assignable source that is currently absent. Empties are skipped.
    #
    # A mic inside a STEREO-LINKED PAIR is not missing, it is on the fader carrying the pair. Without
    # this, linking 1+2 and then moving any other fader would see Mic 1 and Mic 2 as absent and
    # gap-fill one of them onto a spare strip, so a mic would be both linked and on its own fader.
    spec = active_faders()
    seen = set()
    for j in range(len(target)):
        if is_empty_fader(target[j]):
            continue
        if target[j] in seen:
            missing = [c for c in order
                       if c not in target and spec.pair_swallowing(c, target) is None]
            target[j] = missing[0] if missing else -1
            if is_empty_fader(target[j]):
                continue
        seen.add(target[j])
    return target


def action_set_fader(fader_index: int, code: int) -> dict:
    """Put channel `code` on fader `fader_index` (0-based) on the connected device. Always
    produces a valid layout: the displaced channel swaps onto wherever `code` used to sit, and
    any duplicate/orphan left by an already-messy layout is tidied (see _tidy_fader_target).
    Re-reads to report the real result. Device-aware (devices.FaderSpec); refused on a device
    without the "fader_write" cap."""
    gate = fader_write_gate()
    if gate is not None:
        return gate
    spec = active_faders()
    if not 0 <= fader_index < spec.count:
        return {"ok": False, "message": f"Fader {fader_index + 1} is not valid."}
    # THE OFFERED SET WIDENS IN EXPANDED, where Game, Music, A and B become real inputs. In
    # Standard they are nameable but not assignable: putting one on a fader there gives somebody a
    # channel that can never carry audio.
    if code not in spec.code_set_for(channels.usb_expanded()):
        return {"ok": False, "message": f"Channel code {code} is not assignable."}
    chain_note = ""
    try:
        with desk_access.hid_lock:
            blob = rcp_read.read_model()
            vec = read_fader_vector(blob, spec)
            old = vec[fader_index]
            if code == old:
                return {"ok": True, "faders": vec,
                        "labels": [channel_name(c, spec) for c in vec],
                        "message": f"{fader_label(fader_index, spec)} already carries "
                                   f"{channel_name(code, spec)}."}
            target = _tidy_fader_target(vec, fader_index, code,
                                        spec.code_order_for(channels.usb_expanded()))
            # THE CHAIN FOLLOWS THE CHANNEL. Processing and effects are per fader STRIP, so moving a
            # channel without moving its chain leaves it being processed by whatever voicing sits on
            # the strip it lands on. The RODE app does the same on its own drag. Snapshot BEFORE the
            # layout write, off the dump we already have, so a failure can be reported precisely.
            chains_before = channels.read_chains(blob) if chain_follow_on() else None
            # THE LEVEL FOLLOWS THE CHANNEL TOO, and for the same reason the chain does. Snapshot
            # before the write, off the dump we already have; see follow_levels for the silence
            # this prevents. Gated, because a desk with no confirmed faderLevel selectors must not
            # be written to at all.
            levels_before = (rcp_read.values(blob, "faderLevel")
                             if level_write_gate() is None else None)
            # Only write the faders that actually change.
            for i in range(spec.count):
                if target[i] != vec[i]:
                    rcp_transfer.set_fader_source(i, target[i])
            # The layout IS the processing/effects strip map (channels._strip_index), so a stale
            # cached vector here would address the next processing write to the old strip.
            new = read_fader_vector(spec=spec)
            if chains_before is not None:
                chain_note = follow_chains(vec, new, chains_before)
            if levels_before is not None:
                chain_note += follow_levels(vec, new, levels_before, spec)
    except Exception as exc:
        return {"ok": False, "message": f"Could not change the fader layout: {exc}"}
    changed = [i for i in range(spec.count) if i != fader_index and vec[i] != new[i]]
    # SAY WHAT THE FADER ACTUALLY CARRIES, read back, not what was asked for: whenever the tidy
    # step overrides the request, reporting `code` would announce the opposite of what it did.
    landed = new[fader_index] if fader_index < len(new) else None
    if landed == code:
        parts = [f"{fader_label(fader_index, spec)} now carries {channel_name(code, spec)}."]
    else:
        parts = [f"{fader_label(fader_index, spec)} carries {channel_name(landed, spec)}, "
                 f"not {channel_name(code, spec)}."]
    moved_to = next((j for j in changed if new[j] == old), None)
    if moved_to is not None:
        parts.append(f"{fader_label(moved_to, spec)} now carries {channel_name(old, spec)}.")
    elif not is_empty_fader(old) and old not in new:
        # More assignable sources than faders: the displaced channel now sits on no fader.
        parts.append(f"{channel_name(old, spec)} is now off-fader; no fader carries it.")
    if any(j != moved_to for j in changed):
        parts.append("Tidied the layout so no channel is duplicated or off-fader.")
    if chain_note:
        parts.append(chain_note)
    # `stereo` rides along on every layout write: a swap can seat a channel on top of a linked pair,
    # so the link switches have to move with the dropdowns or they start claiming a stale state.
    return {"ok": True, "faders": new, "stereo": stereo_link_state(new, spec),
            "labels": [channel_name(c, spec) for c in new], "message": " ".join(parts)}


# --- Virtual fader levels + monitor level --------------------------------------------------------
# The two levels the desk normally keeps to itself: the VIRTUAL faders (7-9 on the Pro II) have no
# physical fader, and the monitor speaker level has no control in the RODE app either, so without
# these writes both are reachable only from the desk, by encoder or touchscreen. The encoder is not
# usable without sight (its target is chosen by touch and it silently unbinds), which is why these
# matter. Both are confirmed writes; see RODECASTER_PROTOCOL.txt 6.1.
def level_write_gate():
    """Fader and monitor LEVEL writes. The one gate with a second condition beyond the cap: the
    device must also have a virtual fader, so it cannot use desk_access.cap_gate alone."""
    dev = devices.get_active_device()
    if "level_write" not in dev.caps or not dev.fader.level_faders:
        return {"ok": False,
                "message": (f"Setting fader and monitor levels is not verified on the {dev.name} "
                            f"yet, so it is disabled here rather than writing an unconfirmed "
                            f"selector.")}
    return None


def fader_levels_live() -> dict:
    """Read the levels we can set: each writable (virtual) fader, plus the monitor speaker."""
    try:
        with desk_access.hid_lock:
            blob = rcp_read.read_model_stable()
        return fader_levels(blob)
    except Exception as exc:
        return {"ok": False, "message": f"Could not read the levels: {exc}"}


def fader_levels(blob: bytes) -> dict:
    """`fader_levels_live`'s reply from a dump already read.

    faderLevel is 0..127 and only the virtual slots carry a value; we present percent, which is what
    the announcements say too. `channel` names whatever sits on that fader right now."""
    spec = active_faders()
    gate = level_write_gate()
    raw = rcp_read.values(blob, "faderLevel")
    # A virtual fader's MUTE and SOLO have no control on the unit, so software is the only way to
    # reach them, which is why they sit beside its level here. Its LEVEL is not in that position:
    # the encoder cycles onto the virtual faders (announcer._encoder_ring).
    mutes = rcp_read.values(blob, "channelOutputMute")
    solos = rcp_read.values(blob, channels.CUE_PROP)
    mon = rcp_read.values(blob, "outputMonLevel")
    # The two output MUTES and the Bluetooth output LEVEL, all proven writable.
    # They ride on this read rather than on `read_routing_live` because they belong beside the
    # monitor level on the Levels tab, and this is the read that tab already makes.
    outs = rcp_read.snapshot_properties(blob)
    vec = read_fader_vector(blob, spec)
    def switch(values, idx):
        # None for an empty fader, so the UI can say why the switch is unavailable rather than
        # offering a mute for nothing.
        if idx >= len(vec) or is_empty_fader(vec[idx]) or idx >= len(values):
            return None
        return bool(values[idx])

    faders = []
    for idx in spec.level_faders:
        val = raw[idx] if idx < len(raw) else None
        faders.append({
            "index": idx,
            "label": fader_label(idx, spec),
            "channel": channel_name(vec[idx], spec) if idx < len(vec) else None,
            "percent": (None if val is None
                        else max(0, min(100, round(val * 100 / rcp_transfer.FADER_LEVEL_MAX)))),
            "muted": switch(mutes, idx),
            "soloed": switch(solos, idx),
        })

    def one(prop):
        vals = outs.get(prop) or []
        return vals[0] if vals else None

    bt_level = one("outputBTLevel")
    return {"ok": True, "read_only": gate is not None,
            "message": (gate or {}).get("message", ""),
            "faders": faders,
            "monitor": {"percent": (round(mon[0] * 100) if mon else None),
                        "muted": one("outputMonMute")},
            # The Bluetooth OUTPUT: what a paired phone hears from the desk. Nothing to do with the
            # Bluetooth INPUT channel on the Channels page, which is what the phone sends US, and the
            # labels have to keep saying so or the two are impossible to tell apart by ear.
            "bluetooth_out": {"percent": (None if bt_level is None else round(bt_level * 100)),
                              "muted": one("outputBTMute")}}


def action_set_fader_level(fader: int, percent: int) -> dict:
    """Set one VIRTUAL fader's level, 0-100 percent (faderLevel, 0-127 on the wire)."""
    gate = level_write_gate()
    if gate is not None:
        return gate
    spec = active_faders()
    if int(fader) not in spec.level_faders:
        return {"ok": False,
                "message": (f"{fader_label(int(fader), spec)} has no level control here. Only the "
                            f"virtual faders do: the others are physical faders you move by hand.")}
    pct = max(0, min(100, int(percent)))
    # The reply names the fader's channel from the layout already held: a slider step that dumped
    # the model to name it would cost a second and a half per step.
    vec = channels.last_known_strip_vector()
    try:
        with desk_access.hid_lock:
            if vec is None:
                vec = read_fader_vector(spec=spec)
            rcp_transfer.set_fader_level(int(fader), round(pct * rcp_transfer.FADER_LEVEL_MAX / 100))
    except Exception as exc:
        return {"ok": False, "message": f"Could not set the level: {exc}"}
    # The desk echoes it, and the echo would otherwise be spoken over the top of the UI's own
    # announcement.
    announcer.mark_self_level("fader", int(fader))
    who = channel_name(vec[int(fader)] if int(fader) < len(vec) else None, spec)
    return {"ok": True, "percent": pct,
            "message": f"{fader_label(int(fader), spec)} ({who}) set to {pct} percent."}


def action_set_monitor_speaker_level(percent: int) -> dict:
    """Set the monitor SPEAKER level, 0-100 percent (outputMonLevel).

    Named for the speaker deliberately: output_mixes.action_set_monitor_level is a different thing
    entirely, one source's level inside a monitor MIX (a matrix cross-point), and a shared name
    would silently shadow one with the other."""
    gate = level_write_gate()
    if gate is not None:
        return gate
    pct = max(0, min(100, int(percent)))
    try:
        with desk_access.hid_lock:
            rcp_transfer.set_monitor_level(pct / 100.0)
    except Exception as exc:
        return {"ok": False, "message": f"Could not set the monitor level: {exc}"}
    announcer.mark_self_level("monitor", 0)      # same reason as the fader level above
    return {"ok": True, "percent": pct, "message": f"Monitor speaker level set to {pct} percent."}


# A channel button pressed twice puts the encoder on the virtual faders and the monitor. Selecting a
# strip and then none does the same from software, and overrides a target the touchscreen chose: an
# encoder that was turning Mic 1's gain with systemChannelSelected already at -1 turned the monitor
# level afterwards (measured on a Pro II). The strip is selected, never turned, so no gain moves.
_ENCODER_SELECT_SETTLE_S = 0.8
ENCODER_HOME = "Encoder on the virtual faders and monitor."


def action_reset_encoder() -> dict:
    """Put the encoder back on the virtual faders and the monitor, whatever it was bound to."""
    try:
        with desk_access.hid_lock:
            for strip in (0, -1):
                announcer.mark_self_write("channel_selected")
                rcp_transfer.write_at(rcp_transfer.SYSTEM, "systemChannelSelected", "int", strip)
                time.sleep(_ENCODER_SELECT_SETTLE_S)
    except Exception as exc:
        return {"ok": False, "message": f"Could not move the encoder: {exc}"}
    return {"ok": True, "message": ENCODER_HOME}


def action_set_output_mute(which: str, on: bool) -> dict:
    """Mute or unmute a whole OUTPUT: the monitor speakers, or the Bluetooth send.

    Both selectors are derived from the block rule and confirmed. The
    accessibility case is the channel mute's, only stronger: killing the speakers is what you reach
    for when somebody walks in, and the only other way to do it is a control on the unit you have to
    find by touch.

    The two are one endpoint rather than two because they are the same idea with a different target,
    and because the message has to draw the SAME distinction in both directions: muting an output
    changes what somebody HEARS, never what is recorded or streamed."""
    targets = {
        "monitor": ("outputMonMute", "Monitor speakers",
                    "your speakers are silent; headphones, the recording and your streams are not"),
        "bluetooth": ("outputBTMute", "Bluetooth output",
                      "a paired phone hears nothing from the desk; everything else is unaffected"),
    }
    if which not in targets:
        return {"ok": False, "message": f"Unknown output: {which}."}
    prop, label, effect = targets[which]
    gate = level_write_gate()
    if gate is not None:
        return gate
    announcer.mark_self_write(("output_mute", prop))
    try:
        with desk_access.hid_lock:
            rcp_transfer.set_unit_setting(prop, bool(on))
    except Exception as exc:
        return {"ok": False, "message": f"Could not change the {label.lower()} mute: {exc}"}
    return {"ok": True, "message": (f"{label} muted: {effect}." if on
                                    else f"{label} unmuted.")}


def action_set_bt_output_level(percent: int) -> dict:
    """Set the BLUETOOTH OUTPUT level, 0-100 percent (outputBTLevel).

    This is what a paired phone HEARS from the desk. It is not the Bluetooth channel on the Channels
    page, which is what the phone SENDS us; the two have almost the same name and opposite meanings,
    so every label and message about this one says "output" out loud."""
    gate = level_write_gate()
    if gate is not None:
        return gate
    pct = max(0, min(100, int(percent)))
    try:
        with desk_access.hid_lock:
            rcp_transfer.set_unit_setting("outputBTLevel", pct / 100.0)
    except Exception as exc:
        return {"ok": False, "message": f"Could not set the Bluetooth output level: {exc}"}
    return {"ok": True, "percent": pct,
            "message": f"Bluetooth output level set to {pct} percent: that is what a paired phone "
                       f"hears from the desk."}


def action_reset_faders() -> dict:
    """Restore the factory-default fader layout in one go (direct set per position, no swap
    logic so it lands exactly on the default). Device-aware: a device whose clean default vector
    is NOT confirmed (devices.FaderSpec.default_vector is None) REFUSES rather than writing a
    guessed layout. Both shipped desks HAVE a confirmed vector, so neither refuses today; the
    guard is for the next desk added.

    THE CHAIN AND THE LEVEL FOLLOW THE CHANNEL HERE TOO, exactly as in action_set_fader. Without
    the chain, a reset drops USB 1 onto a strip holding a mic voicing with the noise gate on and
    music beds start gating; without the level, a channel landing on a virtual fader at zero goes
    silent. Both are per fader STRIP and a reset moves EVERY channel at once, so it is the single
    move most likely to hand a channel the wrong one."""
    gate = fader_write_gate()
    if gate is not None:
        return gate
    spec = active_faders()
    if not spec.default_vector:
        dev = devices.get_active_device()
        return {"ok": False,
                "message": (f"Resetting the faders to a default layout is not available on the "
                            f"{dev.name} yet: its factory-default fader layout has not been "
                            f"confirmed, so this control stays disabled rather than write a "
                            f"guessed layout.")}
    chain_note = ""
    try:
        with desk_access.hid_lock:
            # One dump serves both jobs: the layout we are about to replace, and the per-strip
            # chains as they stand BEFORE the write, which is what tells us whose chain is whose.
            blob = rcp_read.read_model()
            old = read_fader_vector(blob, spec)
            chains_before = channels.read_chains(blob) if chain_follow_on() else None
            levels_before = (rcp_read.values(blob, "faderLevel")
                             if level_write_gate() is None else None)
            for i, code in enumerate(spec.default_vector):
                rcp_transfer.set_fader_source(i, code)
            new = read_fader_vector(spec=spec)
            if chains_before is not None:
                chain_note = follow_chains(old, new, chains_before)
            if levels_before is not None:
                chain_note += follow_levels(old, new, levels_before, spec)
    except Exception as exc:
        return {"ok": False, "message": f"Could not reset the faders: {exc}"}
    labels = ", ".join(channel_name(c, spec) for c in spec.default_vector)
    msg = f"Faders reset to the default layout: {labels}."
    if chain_note:
        msg += " " + chain_note.strip()
    return {"ok": True, "faders": new, "labels": [channel_name(c, spec) for c in new],
            "stereo": stereo_link_state(new, spec), "message": msg}
