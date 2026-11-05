"""
AccessCaster - the master outputs: the monitor and Bluetooth auto-mutes, each headphone output's
amp type, the master Compellor and the output delay, and the Master Mix page's state. All share one
gate, `write_gate`, which the USB audio switches use as well.
"""

from __future__ import annotations

import time

import desk_access
import devices
import rcp_read
import rcp_transfer
import recorder
from desk_labels import OUTPUT_SWITCH_LABEL


def write_gate():
    """Master/global output writes: USB multitrack and its raw switch, monitor and BT auto-mute,
    headphone type, master Compellor, output delay: named properties on the OUTPUT, HEADPHONE and
    MASTERCHANNEL blocks. The recorder's options share the capability (`recording._options_gate`)."""
    return desk_access.cap_gate("output_write", lambda name:
                     f"Changing the master output settings is not enabled on the {name} yet, so "
                     f"these controls are read-only here.")


# The two auto-mutes, by name: the writer, the property to read back, and what to call it.
# Both live in the OUTPUT block and neither is duplicated anywhere else in the model (checked on
# hardware), and the read-back is still taken BY BLOCK rather than by first occurrence: that costs
# nothing and is the habit that stops the next duplicate biting.
# Each entry carries its own two sentences rather than a shared template, because one phrase cannot
# serve a plural subject and a singular one ("the monitor speakers will not mute on its own"), and
# wording a blind operator relies on is the wrong place to save four lines.
AUTO_MUTE = {
    "monitor": {
        "label": OUTPUT_SWITCH_LABEL["outputMonAutoMute"],
        "prop": "outputMonAutoMute",
        "writer": "set_monitor_auto_mute",
        "on": ("Monitor auto-mute is on. The monitor speakers mute themselves whenever a "
               "microphone fader is brought up, which is what stops feedback."),
        "off": ("Monitor auto-mute is off, so the monitor speakers stay live when a microphone "
                "fader is brought up. With speakers connected, that is how you get feedback."),
    },
    "bt": {
        "label": OUTPUT_SWITCH_LABEL["outputBTAutoMute"],
        "prop": "outputBTAutoMute",
        "writer": "set_bt_auto_mute",
        "on": ("Bluetooth auto-mute is on, so what a paired phone hears mutes by itself when a "
               "microphone fader is brought up."),
        "off": ("Bluetooth auto-mute is off, so a paired phone keeps hearing the mix when a "
                "microphone fader is brought up."),
    },
}


def read_auto_mute(which: str) -> bool:
    """Whether one of the two auto-mutes is on, from a model read. Raises on an unknown target or
    a failed read."""
    with desk_access.hid_lock:
        blob = rcp_read.read_model()
    return bool(rcp_read.block_value(blob, "OUTPUT", AUTO_MUTE[which]["prop"]))


def action_set_auto_mute(which: str, on: bool) -> dict:
    """Turn one of the two auto-mutes on or off, and REPORT WHAT THE DESK ACTUALLY DID.

    NEVER REPORT SUCCESS FROM THE WRITE RETURNING. The failure mode is a tick box reading ON while
    the desk reads False - here, an auto-mute the operator believes is on, and feedback through the
    speakers - and nothing in the app notices, because nothing asks. Every other write in this app
    reads back before it claims anything.

    The state is returned as well as reported, so the page can correct its own switch instead of
    showing a setting the desk does not have.
    """
    gate = write_gate()
    if gate is not None:
        return gate
    spec = AUTO_MUTE.get(which)
    if spec is None:
        return {"ok": False, "message": f"Unknown auto-mute target {which}."}
    try:
        with desk_access.hid_lock:
            getattr(rcp_transfer, spec["writer"])(bool(on))
            time.sleep(_AUTO_MUTE_SETTLE_S)
            now = rcp_read.block_value(rcp_read.read_model_stable(fresh=True),
                                       "OUTPUT", spec["prop"])
    except Exception as exc:
        return {"ok": False, "message": f"Could not change {spec['label'].lower()}: {exc}"}
    now = bool(now)
    if now != bool(on):
        return {"ok": False, "on": now,
                "message": (f"The desk did not take that: {spec['label'].lower()} is still "
                            f"{'on' if now else 'off'}.")}
    return {"ok": True, "on": now, "message": spec["on" if now else "off"]}


# The desk needs a moment before the read-back is meaningful; the same settle the other output
# writes use. Long enough that a slow desk is not read as a refusal.
_AUTO_MUTE_SETTLE_S = 0.6


def active_headphones():
    """The HeadphoneTypeSpec for the currently-connected device (devices.py)."""
    return devices.get_active_device().headphones


def headphone_types(blob: bytes) -> list:
    """Each exposed headphone output's amp type, read on its own HEADPHONE node: the node its write
    goes to."""
    return [rcp_read.keyed_value(blob, rcp_transfer.headphone_key(i), "headphoneType")
            for i, _label in active_headphones().outputs]


def action_set_headphone_type(value: int, output: int = 0) -> dict:
    """Set a headphone output's amp type. `output` is an INDEX into the active device's headphone
    outputs (0 = Headphone 1, 1 = Headphone 2), `value` an amp-type code from that device's
    HeadphoneTypeSpec (0=NTH-100, 1=High sensitivity, 2=Low sensitivity). This resolves the
    output from the spec, so the client never names one that is not confirmed."""
    gate = write_gate()
    if gate is not None:
        return gate
    spec = active_headphones()
    if not 0 <= output < len(spec.outputs):
        return {"ok": False, "message": f"Headphone output {output} is not available on this device."}
    index, out_label = spec.outputs[output]
    if value not in spec.value_codes:
        return {"ok": False, "message": "That headphone type is not a valid value for this device."}
    with desk_access.hid_lock:
        rcp_transfer.set_headphone_type(value, index)
    return {"ok": True, "message": f"{out_label} type set to {spec.value_label[value]}."}


def action_set_master_compellor(on: bool) -> dict:
    """Master output Compellor on/off. Affects all Main-Mix outputs (master audio)."""
    gate = write_gate()
    if gate is not None:
        return gate
    with desk_access.hid_lock:
        rcp_transfer.set_master_compellor(on)
    return {"ok": True, "message": f"Master Compellor turned {'on' if on else 'off'}."}


def action_set_compellor_param(param: str, value01: float) -> dict:
    """Master Compellor parameter (threshold/attack/release/gain) as a normalised 0..1 float."""
    gate = write_gate()
    if gate is not None:
        return gate
    if param not in rcp_transfer.MASTER_COMPELLOR_PARAM:
        return {"ok": False, "message": f"Unknown Compellor parameter {param!r}."}
    v = max(0.0, min(1.0, float(value01)))
    with desk_access.hid_lock:
        rcp_transfer.set_master_compellor_param(param, v)
    return {"ok": True, "message": f"Master Compellor {param} set to {round(v * 100)} percent."}


def action_set_output_delay(on: bool, ms: float | None) -> dict:
    """Output delay on/off + time in milliseconds (0..2000), for A/V sync. Master audio."""
    gate = write_gate()
    if gate is not None:
        return gate
    seconds = None if ms is None else max(0.0, min(2.0, float(ms) / 1000.0))
    with desk_access.hid_lock:
        rcp_transfer.set_output_delay(on, seconds)
    if not on:
        return {"ok": True, "message": "Output delay turned off." if seconds is None else
                f"Output delay is off. Its time is {int(round(seconds * 1000))} milliseconds."}
    if seconds is None:
        return {"ok": True, "message": "Output delay turned on."}
    return {"ok": True, "message": f"Output delay on, {int(round(seconds * 1000))} milliseconds."}


def window_master(blob: bytes) -> dict:
    """The native Master Mix page's state from one dump: the USB audio mode in its two halves, the
    USB stream's raw switch, the master bus's processing (`rcp_read.master_processing`), and the
    page's layout as `model`."""
    usb_mode, usb_expanded = recorder.split_usb_multitrack(
        rcp_read.first(blob, "outputMultiMode", "int"))
    return {
        "usb_multitrack": usb_mode, "usb_expanded": usb_expanded,
        "usb_bypass": rcp_read.first(blob, "outputMultiBypass", "bool"),
        **rcp_read.master_processing(blob),
        "model": {"usb_modes": sorted(recorder.USB_MULTITRACK_LABEL.items()),
                  "usb_expanded": sorted(recorder.USB_EXPANDED_LABEL.items()),
                  "usb_expanded_hint": recorder.USB_EXPANDED_HINT,
                  "usb_drop_hint": recorder.USB_DROP_HINT,
                  "usb_expanded_drop_hint": recorder.USB_EXPANDED_DROP_HINT,
                  "bypass_hint": recorder.BYPASS_HINT,
                  "bypass_needs_multitrack": recorder.BYPASS_NEEDS_MULTITRACK,
                  "read_only": write_gate() is not None}}
