"""
AccessCaster - the window's full read: every page that follows the desk, built from ONE model dump.

`read` takes the dump; `from_dump` builds the reply from a dump already in hand, a read's or the
desk model's snapshot (`desk_model.snapshot`), which is how the window follows the desk between
reads. Each page's part comes from the module that owns that page.
"""

from __future__ import annotations

import channel_actions
import desk_access
import faders
import master_outputs
import output_mixes
import rcp_read


def read() -> dict:
    """The native window's full read: every channel, as
    `channel_actions.action_get_channels(percent=False)`, and the output mixes (`matrix`,
    `rcp_read.read_matrix_state` keyed by index, with `output_mixes.matrix_model`'s layout as
    `matrix_model`), the fader layout (`faders`: `fader_model` as `model`, then `layout`, `labels`
    and `stereo` as a fader write's reply carries them), the Levels page's state (`levels`,
    `_levels`) and the Master Mix page's (`master`, `master_outputs.window_master`)."""
    try:
        with desk_access.hid_lock:
            blob = rcp_read.read_model_stable()
    except Exception as exc:
        return {"ok": False, "message": f"Could not read the desk: {exc}"}
    return from_dump(blob)


def from_dump(blob: bytes) -> dict:
    """`read`'s reply from a dump already in hand."""
    reply = channel_actions.channels_reply(blob, percent=False)
    if reply.get("ok"):
        reply["matrix"] = rcp_read.read_matrix_state(blob)
        reply["matrix_model"] = output_mixes.matrix_model()
        spec = faders.active_faders()
        cis = rcp_read.strip_values(blob, "channelInputSource")
        layout = [cis[i] if i < len(cis) else None for i in range(spec.count)]
        reply["faders"] = {"model": faders.fader_model(), "layout": layout,
                           "labels": [faders.channel_name(c, spec) for c in layout],
                           "stereo": faders.stereo_link_state(cis, spec)}
        reply["levels"] = _levels(blob)
        reply["master"] = master_outputs.window_master(blob)
    return reply


def _levels(blob: bytes) -> dict:
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
