"""
AccessCaster - the Device page: the Current state report, Hardware, and Desk settings, with
MIDI control; and the connected desk's status, whose USB serial files every per-device preference
(`desk_record`).
Wireless connections is network.py, and the firmware is firmware.py.
"""

from __future__ import annotations

import channels
import desk_access
import desk_firmware
import devices
import network
import rcp_hid
import rcp_read
import rcp_transfer
import recorder
import settings_file
import soundpad
import wording
from desk_labels import (  # what each setting is called, shared with the announcer
    OUTPUT_MODE_LABEL,
    UNIT_SETTING_BY_PROP,
    UNIT_SETTINGS,
    unit_setting_words,
)


# What Device > Hardware shows from `read_hardware`, for following the desk. Storage is
# followed through recordState, which the desk pushes when a card or drive goes in or out: its
# `storageVolumeState` is left out because a recording ticks its free space down throughout.
HARDWARE_PROPS = frozenset(("recordState", "recordingTotalCount", "systemFirmwareVersion",
                            "wiredConnected") + desk_firmware.UPDATE_PROPS)


def read_hardware() -> dict:
    """Device > Hardware from one model dump, no writes: the cards and drive with their free space,
    the recorder's state and take count, the firmware and the wired network.

    `storageVolumeState` is the usable source for card space: the numeric
    storageVolumeFree/Capacity read 0 even when a volume is mounted (measured)."""
    def val(prop, kind=None):
        return rcp_read.values(blob, prop, kind)

    try:
        with desk_access.hid_lock:
            blob = rcp_read.read_model_stable()
    except Exception as exc:
        return {"ok": False, "message": f"Could not read the desk's live state: {exc}"}

    out = {"ok": True}

    # Parsed in ONE place (recorder.read_volumes), so this and the Recording section name the same
    # volume the same way. SLOT 1 IS THE USB PORT, not "Card slot 2".
    out["storage"] = {"volumes": recorder.read_volumes(blob)}
    rs = val("recordState", "int")
    out["recorder"] = {
        "state_label": recorder.STATE_LABEL.get(rs[0] if rs else None, "Unknown"),
        "total_recordings": (val("recordingTotalCount", "int") or [None])[0],
    }

    # ---- firmware and the wired network. Neither is a connection you can make or change, so they
    # sit with the other facts the unit reports about itself rather than on a tab of wireless
    # controls.
    out["firmware"] = desk_firmware.version(blob)
    out["firmware_update"] = desk_firmware.update(blob)
    wired = next((v for v in val("wiredConnected") if v is not None), None)
    out["wired_connected"] = bool(wired) if wired is not None else None
    return out


# ----------------------------------------------------------------------------
# UNIT SETTINGS - the desk's own display, buttons and listen behaviour.
#
# All of these are readable from any dump; what makes them WRITABLE is a confirmed write SELECTOR
# for each (rcp_transfer.UNIT_SETTING_SEL). Without them the whole group is reachable ONLY from
# the touchscreen, which is exactly the class of setting this project exists to replace.
#
# ONE SPEC DRIVES FOUR THINGS: the read, the write's value validation, the control the window
# builds, and the profile's restore set. Adding a setting to desk_labels.UNIT_SETTINGS adds it to
# all four, so the Device page's copy and what a profile actually carries cannot drift apart.
#   kind    : "bool" -> a checkbox, "choice" -> a select, "int" -> a range slider
#   detail  : what the user hears after the label, so it adds only what the label does not say, in
#             one short sentence. The Help page carries the fuller account.
# Every one of these is in rcp_transfer's address table, or a write here would raise at the very
# last moment instead of at import. Cheap, and it is the pairing that has to stay honest.
assert all(p in rcp_transfer.UNIT_SETTING_SEL for p in UNIT_SETTING_BY_PROP), \
    "a UNIT_SETTINGS entry has no selector in rcp_transfer.UNIT_SETTING_SEL"

# THE OTHER DIRECTION IS THE ONE THAT DRIFTS: a name added to the address table with no Device page
# entry fails nothing. So membership has ONE source, the address table: a writable setting either
# appears on the Device page or is named here with the reason, and `unit_settings_off_page` reports
# anything that is neither. Same shape as profile_coverage.NOT_SAVED: an omission is a written
# decision, not a gap nobody noticed.
UNIT_SETTINGS_NOT_ON_PAGE = {
    "outputMonMute": "Has its own control: Routing > Levels, 'Mute the monitor speakers'. A second "
                     "copy on the Device page would be two controls for one setting, which is worse "
                     "than none.",
    "outputBTMute": "Has its own control beside the monitor mute, for the same reason.",
    "outputBTLevel": "Has its own control: the Bluetooth output level, next to the two mutes. It is "
                     "a level rather than a desk preference, so it belongs with the other levels.",
    "systemDateTimezone": "DELIBERATELY NOT OFFERED. Measured display-only on this desk (three "
                          "indices, three takes, one UTC second), and at the touchscreen sitting "
                          "the desk's own Time Zone field would not open either. Writing it would "
                          "change nothing a user could see, so offering it would be a lie. Still "
                          "saved and restored by a profile, because it is real stored state.",
    "systemDateTimeDaylightSavings": "NOT OFFERED, for the same reason as the timezone: a write is "
                                     "stored and reads back, and the clock on the desk's screen "
                                     "stays where it was. The touchscreen "
                                     "applies it. Still saved and restored by a profile.",
}


def unit_settings_off_page() -> list:
    """Writable unit settings that are neither on the Device page nor deliberately excluded.

    Empty is the only correct answer. Asserted by DevTools/test_specs.py rather than at import, so
    a mismatch fails a test run instead of stopping the app from starting for a user."""
    return sorted(p for p in rcp_transfer.UNIT_SETTING_SEL
                  if p not in UNIT_SETTING_BY_PROP and p not in UNIT_SETTINGS_NOT_ON_PAGE)


def _unit_setting_values(blob: bytes) -> dict:
    """{prop: value} for every unit setting, read from an ALREADY-READ dump.

    Reads through rcp_read.snapshot_properties, which gets EVERY property in one pass: this wants
    every UNIT_SETTINGS name at once, so one walk beats a lookup per name.

    Bools are normalised to 0/1 so a "choice" control can compare them numerically; a property the
    dump does not carry comes back None and the UI says it could not be read rather than showing a
    zero."""
    props = rcp_read.snapshot_properties(blob)
    out = {}
    for spec in UNIT_SETTINGS:
        vals = props.get(spec["prop"]) or []
        v = vals[0] if vals else None
        if isinstance(v, bool):
            v = int(v)
        out[spec["prop"]] = v if isinstance(v, int) else None
    return out


UNIT_SETTING_PROPS = frozenset([s["prop"] for s in UNIT_SETTINGS] + ["systemMidiControl"])


def read_unit_settings() -> dict:
    """The Device page's "Unit settings" group: one dump, every setting, and whether this desk can
    write them.

    `writable` is the device's "unit_settings" cap. A desk without it reads them and refuses to
    write - the same capture-wider-than-write rule the fader levels use, and the refusal that keeps
    a wrong selector (the stray-append hazard) off an unproven desk."""
    dev = devices.get_active_device()
    try:
        with desk_access.hid_lock:
            blob = rcp_read.read_model_stable()
    except Exception as exc:
        return {"ok": False, "message": f"Could not read the desk's settings: {exc}"}
    values = _unit_setting_values(blob)
    settings = [dict(s, value=values.get(s["prop"])) for s in UNIT_SETTINGS]
    # MIDI LIVES ON THIS TAB, not on one of its own for a single switch, so its value is reported
    # here and the tab still makes exactly ONE call. It is NOT a UNIT_SETTINGS
    # entry and deliberately so: that spec drives the profile's restore set as well as the page, and
    # MIDI already has its own write (rcp_transfer.set_midi_control) and its own key in a profile. Adding it there would write it twice on every
    # restore. Read off the dump we already have, by exact name for the reason in
    # _unit_setting_values.
    midi = (rcp_read.snapshot_properties(blob).get("systemMidiControl") or [None])[0]
    return {"ok": True, "settings": settings, "device_name": dev.name,
            "midi": bool(midi) if midi is not None else None,
            "writable": "unit_settings" in dev.caps}


def action_set_unit_setting(prop: str, value: int) -> dict:
    """Write one unit setting, validating the value against its own spec first.

    The validation is the safety here, not ceremony: these are ordinary global property writes, and
    the thing that makes them safe is that both the selector AND the property name are confirmed
    pairings. So an unknown property is refused outright, a choice must be one of its listed values,
    and a slider is clamped to its range."""
    spec = UNIT_SETTING_BY_PROP.get(prop)
    if spec is None:
        return {"ok": False, "message": f"Unknown desk setting: {prop}."}
    gate = desk_access.cap_gate("unit_settings", lambda name:
                     f"These settings can be read but not changed on the {name}: changing them "
                     f"has only been confirmed on a RODECaster Pro 2.")
    if gate is not None:
        return gate
    try:
        v = int(value)
    except (TypeError, ValueError):
        return {"ok": False, "message": f"{spec['label']} needs a whole number."}
    if spec["kind"] == "choice":
        allowed = {int(c[0]) for c in spec["choices"]}
        if v not in allowed:
            return {"ok": False, "message": f"{v} is not one of the choices for {spec['label']}."}
    elif spec["kind"] == "bool":
        v = 1 if v else 0
    else:
        v = max(int(spec["min"]), min(int(spec["max"]), v))
    said = unit_setting_words(spec, v)
    # WRITING outputMonFixed CLEARS outputMonMute, so switching to fixed level OPENS THE MONITOR
    # SPEAKERS - with a live mic in the room, which is feedback (measured on a Pro II). Writing
    # outputMonFixed back does not re-mute, so the mute is read first and PUT BACK after, and the
    # user is told. The mute is only rewritten if the desk actually moved it, so a firmware that
    # stops doing this gets no write nobody asked for.
    guard_mute = prop == "outputMonFixed"
    extra = ""
    try:
        with desk_access.hid_lock:
            mute_before = (rcp_read.values(rcp_read.read_model_stable(fresh=True),
                                           "outputMonMute") or [None])[0] if guard_mute else None
            rcp_transfer.set_unit_setting(prop, v)
            if mute_before is not None:
                mute_after = (rcp_read.values(rcp_read.read_model_stable(fresh=True),
                                              "outputMonMute") or [None])[0]
                if mute_after is not None and bool(mute_after) != bool(mute_before):
                    rcp_transfer.set_unit_setting("outputMonMute", bool(mute_before))
                    extra = (" The desk un-muted the monitor speakers when it did that, so the mute "
                             "has been put back on."
                             if mute_before else
                             " The desk muted the monitor speakers when it did that, so the mute has "
                             "been taken back off.")
    except Exception as exc:
        return {"ok": False, "message": f"Could not set {spec['label']}: {exc}"}
    return {"ok": True, "value": v, "message": f"{spec['label']}: {said}.{extra}"}


def action_set_midi(on: bool) -> dict:
    with desk_access.hid_lock:
        rcp_transfer.set_midi_control(on)
    return {"ok": True, "message": f"MIDI control turned {'on' if on else 'off'}."}


# ----------------------------------------------------------------------------
# THE CURRENT STATE REPORT - what is muted, soloed and unusual, from one dump.

def _overview_routing(blob: bytes) -> dict:
    """Routing reported BY EXCEPTION: only what differs from the usual, never the full grid.

    Reading eleven outputs against ten sources aloud is 110 facts to get through before reaching
    the one that matters, which defeats the point of a troubleshooting summary. So we report an
    output only when its MODE differs from the default, or when it EXCLUDES a source.

    Two things shape the wording. First, the defaults in devices.py are a known-good set rather
    than a factory capture (see the provenance comment there), so this says "usually" and never
    "wrong". Second, a Mix Minus output carries no exclusion flags at all, because the
    self-exclusion is implicit in the mode, so mode is reported before exclusions."""
    dev = devices.get_active_device()
    state = rcp_read.read_matrix_state(blob)
    if not state.get("ok"):
        return {"ok": False, "reason": state.get("reason") or "The routing could not be read.",
                "notes": []}
    # NAMED FROM EVERY SOURCE, COUNTED FROM THE LIVE ONES. The desk keeps a cross-point for Game,
    # Music, A and B in both modes, but in Standard nothing can feed them, so a note that counts them
    # says "USB 2 carries only Game, Music, A and B" about an output that carries nothing at all.
    src_label = {s["index"]: s["label"] for s in (dev.matrix.all_sources or ())}
    live = {s["index"] for s in (dev.matrix.sources_for(channels.usb_expanded()) or ())}
    notes = []
    for out in (dev.matrix.outputs or ()):
        o = out["index"]
        mode = state["modes"].get(o)
        if mode is None:
            continue
        default = out.get("default_mode")
        if default is not None and mode != default:
            notes.append(f"{out['label']} is set to {OUTPUT_MODE_LABEL.get(mode, mode)}, "
                         f"usually {OUTPUT_MODE_LABEL.get(default, default)}.")
        cells = sorted((s, off) for s, off in (state["excluded"].get(o) or {}).items()
                       if s in live)
        excluded = [src_label.get(s, f"source {s}") for s, off in cells if off]
        included = [src_label.get(s, f"source {s}") for s, off in cells if not off]
        # Say whichever list is shorter. A typical Chat preset excludes nine of ten sources, and
        # "Chat does not include Mic 2, Mic 3, Mic 4, USB 1, Chat, USB 2, Bluetooth and SMART pads"
        # is a mouthful that hides the one fact that matters: Chat carries Mic 1 only.
        if excluded and len(included) < len(excluded):
            notes.append(f"{out['label']} carries only {wording.join_and(included)}."
                         if included else f"{out['label']} carries nothing at all.")
        elif excluded:
            notes.append(f"{out['label']} does not include {wording.join_and(excluded)}.")
    return {"ok": True, "reason": "", "notes": notes}


def _pads_engaged(blob: bytes) -> list:
    """Pads the desk currently has ENGAGED, named, from a dump already read. [] when none are.

    A VOICE-FX pad is the one worth catching: it processes a live channel and is otherwise silent,
    so the only symptom is sounding wrong. Never raises - this decorates the overview, and an
    unreadable pad table is not worth failing the whole page for."""
    try:
        return [{"grid": pi + 1, "name": nm, "kind": "voice effect" if pi is not None else "pad"}
                for _pos, pi, nm in soundpad.latched_pads(blob)]
    except Exception:
        return []


def _storage_low(blob: bytes) -> list:
    """Volumes with less than a fifth of their space left, named and quantified, from a dump we have
    already read. [] when everything has room, which is what lets the overview stay by-exception.

    The parsing and the "nearly full" judgement both live in `recorder.read_volumes`, so this
    warning, the Hardware tab and the Recording page's destination cannot disagree about which
    volume is which or when one is short of room. Never raises: an unreadable volume is reported as
    nothing rather than taking the whole overview down with it."""
    try:
        return [{"label": v["label"], "free_pct": v["free_pct"], "free_mb": v["free_mb"]}
                for v in recorder.read_volumes(blob) if v["low"]]
    except Exception:
        return []


def _overview_recording(blob: bytes):
    """A word for the overview when the unit is recording or paused, else None.

    Deliberately thin: the overview reports EXCEPTIONS, and a unit sitting ready to record is not
    one. Never raises, for the same reason as the rest of this page's helpers."""
    try:
        state = recorder.read_state(blob)
        if state in (recorder.RECORDING, recorder.PAUSED):
            return {"state": state, "label": recorder.STATE_LABEL[state]}
    except Exception:
        pass
    return None


def overview(fresh: bool = True) -> dict:
    """The Device page's opening summary: what is muted, what is soloed, where every fader sits,
    anything unusual about the routing, and the current bank.

    Why it exists: these facts otherwise live on four different pages, and mute and solo have no
    read path at all outside the announcer, so a desk left with two channels muted gives the app no
    way to find out which. This is the one place to look when something sounds wrong.

    ONE model dump feeds everything here. Parsing each section from its own read would cost four
    round trips of about half a second each on the page the app opens on.

    Fader positions come from the ANCHOR half of `mixLevelWithAnchor`, not from `faderLevel`:
    faderLevel is NOT the physical position (it does not move while fader 1 travels top to halfway)
    and only the virtual slots carry a value there. The anchor is the channel fader's own position
    and is present on every cross-point in that source's row, physical faders included, so we read
    it from the row's first cell.

    `fresh` False builds it from the desk model where that can be trusted
    (`desk_access.desk_now`): for the re-build after each push. Everything here is pushed when it
    changes, physical fader moves included (as `mixLevelWithAnchor`), except a card's or drive's
    free space, which only a read brings up to date."""
    dev = devices.get_active_device()
    try:
        with desk_access.hid_lock:
            blob = rcp_read.read_model_stable() if fresh else desk_access.desk_now()
    except Exception as exc:
        return {"ok": False, "device_name": dev.name,
                "message": f"Could not read the desk: {exc}"}

    spec = dev.fader
    vec = rcp_read.values(blob, "channelInputSource")
    mutes = rcp_read.values(blob, "channelOutputMute")
    cues = rcp_read.values(blob, "channelCueEnable")

    # The anchor lookup, guarded on the matrix keeping its expected shape - the same guard
    # rcp_read uses, because a connected SIP caller shifts the grid and every index would be wrong.
    cells = rcp_read.block_values(blob, dev.matrix.block, "mixLevelWithAnchor")
    shape_ok = len(cells) == dev.matrix.cell_count

    def anchor_pct(source_code):
        if not shape_ok or source_code is None or source_code < 0:
            return None
        cell = cells[dev.matrix.row_stride * source_code] \
            if dev.matrix.row_stride * source_code < len(cells) else None
        if not isinstance(cell, str) or "|" not in cell:
            return None
        try:
            return max(0, min(100, int(round(float(cell.split("|", 1)[1]) * 100))))
        except ValueError:
            return None

    def label(i):
        code = vec[i] if i < len(vec) else None
        # labels_for: a fader holding a linked pair must be named, not fall through to "Fader N",
        # and USB 1 is named after what the computer actually sends in the mode the desk is in.
        return spec.labels_for(channels.usb_expanded()).get(code) or f"Fader {i + 1}"

    faders, muted, soloed = [], [], []
    for i in range(spec.count):
        code = vec[i] if i < len(vec) else None
        empty = code is None or code < 0
        is_muted = bool(mutes[i]) if (i < len(mutes) and not empty) else False
        faders.append({"fader": i + 1,
                       "label": "empty" if empty else label(i),
                       "percent": None if empty else anchor_pct(code),
                       "muted": is_muted,
                       "virtual": i >= spec.physical})
        if is_muted:
            muted.append({"fader": i + 1, "label": label(i)})
        if not empty and i < len(cues) and cues[i]:
            soloed.append({"fader": i + 1, "label": label(i)})

    mon = [v for v in rcp_read.values(blob, "outputMonLevel") if isinstance(v, float)]
    bank = [v for v in rcp_read.values(blob, "selectedBank") if isinstance(v, int)]
    return {"ok": True,
            "device_name": dev.name,
            "muted": muted,
            "soloed": soloed,
            # EVERYTHING BELOW IS BY EXCEPTION: empty when the desk is fine. No card and no
            # recording destination are deliberately NOT exceptions: a desk often has neither, and
            # a warning that is always there is one nobody hears. A nearly full volume is, because
            # it is the fact you need BEFORE you press record.
            "storage_low": _storage_low(blob),
            "recording": _overview_recording(blob),
            # A latched voice-FX pad sits on a live mic, makes no sound of its own and simply makes
            # you sound wrong, with nothing on screen to say why.
            "pads_engaged": _pads_engaged(blob),
            "firmware_update": desk_firmware.update(blob),
            # Always reported, unlike the exceptions above: whether the unit is online decides
            # whether CallMe and the firmware check can work at all. `callme_hosting` lets the
            # spoken summary call an offline unit a problem only when guests expect to reach it.
            "network": dict(network.connection(blob),
                            callme_hosting=bool(rcp_read.first(blob, "sipCallHostingEnabled"))),
            "faders": faders,
            "monitor_percent": max(0, min(100, int(round(mon[0] * 100)))) if mon else None,
            "bank": (bank[0] + 1) if bank else None,       # the desk numbers banks from 0
            "levels_ok": shape_ok,
            "routing": _overview_routing(blob),
            "message": ""}


# ----------------------------------------------------------------------------
# THE DESK'S STATUS - whether it is connected, and which unit it is.
#
# The USB SERIAL of the desk we are talking to, published from every status read. It TAGS
# per-device preferences (which preset a strip is on, whether the as-found baseline has been
# offered) so that plugging in a second unit does not inherit the first one's claims. Kept as
# (device name, serial) so a desk swap mid-session replaces it rather than keeping a stale
# pairing. Nothing here opens the device: see hardware_id for why that matters.
_id_cache = ("", "")


def _note_id(device_name, serial) -> None:
    global _id_cache
    if serial:
        _id_cache = (str(device_name or ""), str(serial))


def read_status() -> dict:
    """Open the control interface, run the one safe status command, return a status dict.

    Read-only. Never changes device state. See the comment at the command for why it is only `I`.
    """
    with desk_access.hid_lock:
        # A status read is the one place a person asks "is it plugged in?", so it scans the bus
        # rather than trusting the remembered list, and the finder below then reuses that scan.
        found = rcp_hid.refresh_interfaces()
        interfaces = [i.describe() for i in found]
        iface = rcp_hid.find_control_interface()
        # WHICH KIND OF NOTHING IS IT? The finder returns MI_09 or None and never guesses (see its
        # docstring), so telling "no desk at all" from "a desk on the wrong port" is done here, where
        # the answer is a sentence for a person rather than a value for a caller.
        #
        # The desk has TWO USB ports and this protocol works over exactly one interface, MI_09, which
        # is on USB 1, the port nearest the power inlet. USB 2 presents its own device (PID 0x0026,
        # MI_03) that carries the audio and answers no read: every report-1 command comes back
        # empty. So a cable in USB 2 alone looks like a connected unit to anyone watching the
        # lights while the app can do nothing with it, and that state invites a whole causal theory
        # about the protocol when the real answer is which cable is plugged in.
        if iface is None:
            others = [i for i in found if i.is_control]
            if others:
                device_name = devices.resolve_device(others[0].product_id).name
                return {
                    "connected": False,
                    "device_name": device_name,
                    "control_interface": others[0].describe(),
                    "interfaces": interfaces,
                    "message": (f"The {device_name} is half connected: its audio is here but the "
                                f"connection AccessCaster controls it over is not. That is almost "
                                f"always the USB cable in the port nearest the power inlet, which "
                                f"is the one this app needs. Plug that one back in and everything "
                                f"comes back straight away."),
                }
            return {
                "connected": False,
                "device_name": None,
                # NAMING THE PORT IS THE POINT OF THIS MESSAGE. It is what someone setting up for
                # the first time reads, and a cable in the wrong port produces exactly this and
                # nothing else to go on.
                "message": "No RODECaster found. Check it is powered on, and that the USB cable "
                           "goes into the socket nearest the power inlet. That is the only one "
                           "this app can control the desk over; the other USB socket carries "
                           "audio only.",
                "interfaces": interfaces,
            }
        # The connected desk's own name, which the page's title, brand and About take up.
        device_name = devices.resolve_device(iface.product_id).name
        try:
            # ONE COMMAND, `I`, AND NEVER `VER` OR THE `U` PING. With the desk in Expanded, either
            # of those two makes it drop RODE's virtual `System (RODECaster)` playback endpoint
            # about 17 seconds later; Windows surprise-removes and re-adds it, and a screen reader
            # on that output loses its speech for a few seconds. Measured one command at a time:
            # `I` and a model dump are clean. `I` answering is proof enough that the desk is
            # responding, and the identity below comes from the USB serial, so nothing is lost.
            with rcp_hid.RodeCaster.open_path(iface, "device status") as dev:
                info = dev.get_info()
            online = bool(info)
            # THE UNIT'S IDENTITY IS ITS USB SERIAL. It survives factory resets and power cycles
            # unchanged, and comes free with the enumeration without opening anything. `VER` was
            # never a substitute: it changes across a reboot, so anything filed under it is orphaned.
            _note_id(device_name, iface.serial_number)
            return {
                "connected": True,
                "device_name": device_name,
                "online": online,
                "hardware_id": iface.serial_number or "",
                "info_hex": info[:16].hex(),
                "control_interface": iface.describe(),
                "interfaces": interfaces,
                "message": f"Connected to {device_name} (serial "
                           f"{iface.serial_number or 'unknown'}).",
            }
        except Exception as exc:  # surfaced to the UI
            return {
                "connected": False,
                "device_name": device_name,
                "message": f"Found the control interface but could not talk to "
                           f"it: {exc}",
                "interfaces": interfaces,
            }


def hardware_id() -> str:
    """The connected desk's USB SERIAL, or "" if we do not know it yet. It is what every per-device
    preference is filed under, so it has to be the same value tomorrow: see read_status for why
    `VER` is not.

    PURE LOOKUP, deliberately: it reads the value read_status already published (see _note_id) and
    never opens the device itself. Two reasons. Reading it is a full device round trip, and this is
    called from a channel read that the UI does on every visit to the page. And read_status takes
    the HID lock, which is not reentrant, so calling it from anywhere that already holds the lock
    would deadlock the app rather than merely be slow. "" is a perfectly good answer here: it only
    means a preference goes untagged until the first status read, which the page does on load."""
    return _id_cache[1]



def desk_record(key: str) -> dict:
    """The per-channel record settings.json files under `key`, as {"device": hardware_id,
    "channels": {...}}: its channels, or {} when another desk wrote it, so two desks never inherit
    each other's records. A record with no id, or read before the id is known, counts as this
    desk's."""
    rec = settings_file.load().get(key)
    if not isinstance(rec, dict):
        return {}
    here = hardware_id()
    if rec.get("device") and here and rec.get("device") != here:
        return {}
    chans = rec.get("channels")
    return chans if isinstance(chans, dict) else {}


def save_desk_record(key: str, chans: dict) -> None:
    """Write `key`'s per-channel record, filed under this desk (`desk_record`)."""
    settings_file.update(**{key: {"device": hardware_id(), "channels": chans}})
