"""The window's picture of the desk, its channels, its output mixes, its levels and its master mix:
read once, then kept current by what the desk pushes.

It follows the desk listener (`desk_live`) while the window exists. The state belongs to the UI
thread: the follower's thread hands each push over with wx.CallAfter, so the records are changed,
and subscribers told, only on the thread that draws them.

A full read happens at start, on request (F5, or after a write that moves other values), when the
desk comes back after dropping off the bus, and shortly after a push that changes which channels
exist or where they sit (a fader's source, the USB mode) or what a mic is (its type, which moves
the gain and the chain). Everything else a push carries is applied to the record it addresses.
"""
import wx

import app_window
import channels
import desk_live
import devices
import rcp_read
import rcp_transfer
import server
from native.calls import call

# Pushes that are answered with a full read, this long after the last of them: a fader
# reassignment or a restore pushes them in bursts.
STRUCTURAL = {"channelInputSource", "outputMultiMode", "inputMicrophoneType"}
REREAD_AFTER_MS = 1500
# A pushed change to a mic's chain is compared with its preset this long after the last one, since
# the comparison reads the preset files.
PRESET_AFTER_MS = 500

# Per-strip state on the channel block (CHANNEL[strip]): record field, or a function of the
# record and the value.
_STRIP_BOOLS = {channels.MUTE_PROP: "muted", channels.CUE_PROP: "solo",
                channels.WIRELESS_MUTE_PROP: "wireless_muted",
                channels.CHAIN_ADVANCED_PROP: "advanced"}
_MODULES = set(channels.PROCESSING_MODULE_BOOLS)
_PARAMS = set(channels.PROCESSING_READ_PROPS)
# Per-strip effects on the effects block (EFFECTS_PARAMETERS[strip]).
_FX_ON = {prop: effect for effect, prop in channels.CHANNEL_FX_ON.items()}
_FX_PARAMS = {p: effect for effect, ps in channels.CHANNEL_FX_PARAMS.items() for p in ps}
# Per-input values, addressed at the input itself.
_INPUTS = {"inputMicrophoneGain", "inputDigitalGain", "inputPower", "inputPhaseFlip"}

# The output mixes: an output's mode, on its own MIXMINUSES node, and a source's place in an
# output's Custom mix, on the cross-point's MIX node.
_MIX = {"outputMixMinus", "mixDisabled"}
MIX_MINUS, CUSTOM = 1, 2      # an output's mix types, as outputMixMinus holds them; 0 is Main Mix

# The Levels page's values on the OUTPUT block: (group in `levels`, key, kind pushed).
_OUTPUT_LEVELS = {
    "outputMonLevel": ("monitor", "percent", "float"),
    "outputMonMute": ("monitor", "muted", "bool"),
    "outputBTLevel": ("bluetooth_out", "percent", "float"),
    "outputBTMute": ("bluetooth_out", "muted", "bool"),
    "outputMonAutoMute": ("auto_mute", "monitor", "bool"),
    "outputBTAutoMute": ("auto_mute", "bt", "bool"),
}
# A fader's mute and solo are its strip's, so they land on the channel record too.
_FADER_TOGGLES = {channels.MUTE_PROP: "muted", channels.CUE_PROP: "soloed"}
_LEVELS = set(_OUTPUT_LEVELS) | {"faderLevel", "headphoneType", "mixLevelWithAnchor"}

# The Master Mix page's values: name -> (its block's key, key in `master`, kind pushed).
_MASTER = {
    "outputMultiBypass": (rcp_transfer.OUTPUT, "usb_bypass", "bool"),
    "masterCompellorOn": (rcp_transfer.MASTER, "compellor_on", "bool"),
    "masterCompellorThreshold": (rcp_transfer.MASTER, "compellor_threshold", "float"),
    "masterCompellorAttack": (rcp_transfer.MASTER, "compellor_attack", "float"),
    "masterCompellorRelease": (rcp_transfer.MASTER, "compellor_release", "float"),
    "masterCompellorGain": (rcp_transfer.MASTER, "compellor_gain", "float"),
    "masterDelayOn": (rcp_transfer.MASTER, "delay_on", "bool"),
    "masterDelaySeconds": (rcp_transfer.MASTER, "delay_seconds", "float"),
}

_RELEVANT = (STRUCTURAL | set(_STRIP_BOOLS) | _MODULES | _PARAMS | set(_FX_ON) | set(_FX_PARAMS)
             | _INPUTS | _MIX | _LEVELS | set(_MASTER)
             | {"channelBypassProcessing", channels.CHAIN_PRESET_MARKER_PROP, "recordState"})
_CHAIN = (_MODULES | _PARAMS | {channels.CHAIN_ADVANCED_PROP, channels.CHAIN_PRESET_MARKER_PROP})


class DeskState:
    """`channels` is the last full read (server.read_window_state: the channels with the desk's
    own 0..1 params), with each record changed in place as pushes arrive. `matrix` is the output
    mixes from the same read (rcp_read.read_matrix_state: `modes` and `excluded`, keyed by output
    and source index, `ok` False with a `reason` when the grid could not be read), with the layout
    as `matrix["model"]` (server.matrix_model). `faders` is the fader layout from the same read
    (`model`, `layout`, `labels`, `stereo`); a fader write's reply updates it through
    `faders_changed`, and the push that follows asks for a full read (`STRUCTURAL`). `levels` is
    the Levels page's state from the same read (server._window_levels), and `master` the Master
    Mix page's (server._window_master).

    A subscriber is called as `fn("read", reply)` after every full read, successful or not,
    `fn("changed", names)` with the names of the channels a push changed, `fn("mix", outputs)`
    with the indices of the outputs whose mode or mix a push changed, `fn("faders", None)` when a
    fader write's reply changed the layout, `fn("levels", None)` when a push changed anything in
    `levels`, `fn("master", None)` when a push changed anything in `master`, and
    `fn("recorder", state)` with each recording state the desk pushes (`recorder.READY` and the
    rest), which it pushes for its own REC button and for a card or drive going in or out, and
    `fn("lost", None)` when the desk drops off the bus. The recorder is not held here: the
    Recording section reads it for itself."""

    def __init__(self, owner):
        self.owner = owner
        self.channels = None
        self.matrix = None
        self.faders = None
        self.levels = None
        self.master = None
        self._records = {}           # channel name -> its record in `channels`
        self._strip_names = {}       # strip index -> channel name
        self._subscribers = []
        self._reading = False
        self._read_again = False
        self._reread = None
        self._preset_timers = {}
        self._dirty = set()
        self._dirty_mix = set()
        self._dirty_levels = False
        self._dirty_master = False
        self._flush = None
        self._stopped = False
        self._follower = None

    def subscribe(self, fn):
        self._subscribers.append(fn)

    def start(self):
        app_window.pushed = self._pushed
        self._follower = desk_live.Follower(
            "window", _RELEVANT, on_push=lambda push: wx.CallAfter(self._apply, *push[:4]),
            on_connected=self._connected,
            on_lost=lambda: wx.CallAfter(self._notify, "lost", None)).start()
        self.refresh()

    def stop(self):
        if app_window.pushed == self._pushed:
            app_window.pushed = None
        self._stopped = True
        for timer in [self._reread, self._flush, *self._preset_timers.values()]:
            if timer is not None:
                timer.Stop()
        if self._follower is not None:
            self._follower.stop()

    # ---- full reads ------------------------------------------------------------------------

    def refresh(self):
        if self._reading:
            self._read_again = True
            return
        self._reading = True
        call(self.owner, _read_channels, done=self._read_done)

    def _read_done(self, reply):
        self._reading = False
        if reply.get("ok"):
            self.channels = reply
            self.matrix = dict(reply["matrix"], model=reply["matrix_model"])
            self.faders = reply["faders"]
            self.levels = reply["levels"]
            self.master = reply["master"]
            self._records = {rec["name"]: rec for rec in reply["channels"]}
            by_code = {entry["source_code"]: name
                       for name, entry in channels.ui_channel_map().items()}
            self._strip_names = {strip: by_code[code]
                                 for strip, code in enumerate(reply.get("strips") or [])
                                 if code in by_code}
        self._notify("read", reply)
        if self._read_again:
            self._read_again = False
            self.refresh()

    def faders_changed(self, reply):
        """A fader write's reply: its new layout, shown at once, then a full read for the channels
        that moved with it."""
        if self.faders is None:
            return
        for key, field in (("faders", "layout"), ("labels", "labels"), ("stereo", "stereo")):
            if reply.get(key) is not None:
                self.faders[field] = reply[key]
        self._notify("faders", None)
        self.refresh()

    def monitor_mix_written(self, output, source, level):
        """A monitor mix level write was accepted: the source now has a level of its own in that
        output, which is now a Custom mix. The desk pushes back the level but not the mode."""
        cells = (self.levels["mix"].get("levels") or {}).get(output)
        if cells is not None and source in cells:
            cells[source] = {"level": level, "linked": False}
        modes = self.matrix.get("modes") or {}
        if self.matrix.get("ok") and modes.get(output) != CUSTOM:
            modes[output] = CUSTOM
            self._notify("mix", {output})

    def solos_cleared(self):
        """Clear all solos was accepted: no channel and no fader is soloed."""
        for rec in self._records.values():
            rec["solo"] = False
        for fader in self.levels["faders"]:
            if fader["soloed"] is not None:
                fader["soloed"] = False
        self._notify("changed", set(self._records))
        self._notify("levels", None)

    def _reread_soon(self):
        if self._reread is not None and self._reread.IsRunning():
            self._reread.Start(REREAD_AFTER_MS)
        else:
            self._reread = wx.CallLater(REREAD_AFTER_MS, self.refresh)

    # ---- pushes ----------------------------------------------------------------------------

    def _connected(self, gap):
        """The desk is open again after dropping off the bus: read it in full."""
        if gap is not None:
            wx.CallAfter(self.refresh)

    def _pushed(self, path, name, value, kind):
        """`app_window.pushed`: one of our own writes, applied as the desk's push of it would be."""
        if name in _RELEVANT:
            wx.CallAfter(self._apply, path, name, value, kind)

    def _apply(self, path, name, value, kind):
        if self._stopped:
            return
        # The node the push is addressed at, named by the desk's own layout.
        node = rcp_read.push_key(path)
        if name == "recordState":
            if node == rcp_transfer.RECORDER and kind == "int":
                self._notify("recorder", value)
            return
        if self.channels is None:
            return
        if name in STRUCTURAL:
            self._reread_soon()
            return
        if name in _MASTER:
            if _set_master(self.master, node, name, value, kind):
                self._dirty_master = True
                self._flush_soon()
            return
        if (name in _LEVELS or name in _FADER_TOGGLES) and _set_level(
                self.levels, node, name, value, kind):
            self._dirty_levels = True
            self._flush_soon()
        if name in _LEVELS or node is None or len(node) != 2:
            return
        if name in _MIX:
            output = _set_mix(self.matrix, node, name, value, kind)
            if output is None:
                return
            self._dirty_mix.add(output)
        else:
            rec = self._record_for(node, name)
            if rec is None or not _set(rec, name, value, kind):
                return
            self._dirty.add(rec["name"])
            if name in _CHAIN and rec["kind"] == "mic":
                self._preset_soon(rec["name"])
        self._flush_soon()

    def _flush_soon(self):
        if self._flush is None or not self._flush.IsRunning():
            # One notification for a burst: a fader or a knob pushes a value per step, and a mix
            # preset a cross-point per write.
            self._flush = wx.CallLater(30, self._flush_dirty)

    def _record_for(self, node, name):
        if name in _INPUTS:
            channel = devices.get_active_device().channel_for_input_key(node)
            return self._records.get(channel["name"]) if channel else None
        block = "EFFECTS_PARAMETERS" if name in _FX_ON or name in _FX_PARAMS else "CHANNEL"
        if node[0] != block:
            return None
        return self._records.get(self._strip_names.get(node[1]))

    def _flush_dirty(self):
        names, self._dirty = self._dirty, set()
        outputs, self._dirty_mix = self._dirty_mix, set()
        levels, self._dirty_levels = self._dirty_levels, False
        master, self._dirty_master = self._dirty_master, False
        if names:
            self._notify("changed", names)
        if outputs:
            self._notify("mix", outputs)
        if levels:
            self._notify("levels", None)
        if master:
            self._notify("master", None)

    def _preset_soon(self, name):
        timer = self._preset_timers.get(name)
        if timer is not None and timer.IsRunning():
            timer.Start(PRESET_AFTER_MS)
        else:
            self._preset_timers[name] = wx.CallLater(PRESET_AFTER_MS, self._recompare, name)

    def _recompare(self, name):
        rec = self._records.get(name)
        if rec is None:
            return
        number = channels.mic_channel_number(channels.ui_channel_map()[name])
        now = server._preset_now(number, rec)
        if now != rec.get("preset_now"):
            rec["preset_now"] = now
            self._notify("changed", {name})

    def _notify(self, event, detail):
        for fn in self._subscribers:
            fn(event, detail)


def _read_channels():
    """A full read on a worker thread, with the fader layout that read published, which is how a
    push addressed at a strip is put on a channel."""
    reply = server.read_window_state()
    if reply.get("ok"):
        reply["strips"] = channels.last_known_strip_vector()
        reply["device_name"] = devices.get_active_device().name
    return reply


def _set(rec, name, value, kind):
    """Put one pushed value on a channel record. Returns whether anything changed."""
    if name in _STRIP_BOOLS or name == "channelBypassProcessing" or name in _MODULES \
            or name in _FX_ON:
        if kind != "bool":
            return False
        if name == "channelBypassProcessing":
            holder, key, value = rec, "processing_on", not value
        elif name in _MODULES:
            holder, key = rec.setdefault("modules", {}), name
        elif name in _FX_ON:
            holder, key = rec.setdefault("effects", {}), _FX_ON[name]
        else:
            holder, key = rec, _STRIP_BOOLS[name]
    elif name in _PARAMS or name in _FX_PARAMS:
        if kind != "float":
            return False
        holder = (rec.setdefault("params", {}) if name in _PARAMS
                  else rec.setdefault("effects", {}).setdefault(_FX_PARAMS[name] + "_params", {}))
        key = name
    elif name == channels.CHAIN_PRESET_MARKER_PROP:
        holder, key = rec, "preset_index"
    elif name in ("inputMicrophoneGain", "inputDigitalGain"):
        # A mic's level is its preamp gain and a line channel's its digital gain.
        if (kind != "int" or rec["kind"] == "bus"
                or (name == "inputMicrophoneGain") != (rec["kind"] == "mic")):
            return False
        holder, key = rec, "level_db"
    elif name == "inputPower":
        holder, key, value = rec, "phantom", value == 1
    elif name == "inputPhaseFlip":
        holder, key, value = rec, "polarity", bool(value)
    else:
        return False
    if holder.get(key) == value:
        return False
    holder[key] = value
    return True


def _set_mix(matrix, node, name, value, kind):
    """Put one pushed mix value on `matrix`. Returns the output it changed, or None."""
    if matrix is None or not matrix.get("ok"):
        return None
    if name == "outputMixMinus":
        if node[0] != "MIXMINUSES":
            return None
        output = node[1]
        if kind != "int" or output not in matrix["modes"]:
            return None
        holder, key = matrix["modes"], output
    else:
        # RCSYNCMIX carries mixDisabled too, on nodes of its own name.
        cell = rcp_transfer.cross_point_of(node)
        if kind != "bool" or cell is None:
            return None
        output, source = cell
        holder = matrix["excluded"].get(output)
        if holder is None or source not in holder:
            return None
        key = source
    if holder[key] == value:
        return None
    holder[key] = value
    return output


def _set_level(levels, node, name, value, kind):
    """Put one pushed value on the Levels page's state. Returns whether anything changed."""
    if levels is None or node is None:
        return False
    if name == "faderLevel":
        # Addressed at the fader itself: PHYSICALINTERFACE.FADER[index].
        if kind != "int" or node[:3] != ("PHYSICALINTERFACE", 0, "FADER"):
            return False
        holder = _fader(levels, node[3])
        key, value = "percent", _percent(value / rcp_transfer.FADER_LEVEL_MAX)
    elif name in _FADER_TOGGLES:
        if kind != "bool" or node[0] != "CHANNEL":
            return False
        holder, key = _fader(levels, node[1]), _FADER_TOGGLES[name]
        # None is an empty fader, which has nothing to mute or solo.
        if holder is None or holder[key] is None:
            return False
    elif name in _OUTPUT_LEVELS:
        group, key, wanted = _OUTPUT_LEVELS[name]
        if kind != wanted or node != rcp_transfer.OUTPUT:
            return False
        holder = levels[group]
        if wanted == "float":
            value = _percent(value)
    elif name == "headphoneType":
        outputs = [i for i, _label in devices.get_active_device().headphones.outputs]
        if kind != "int" or node[0] != "HEADPHONE" or node[1] not in outputs:
            return False
        holder, key = levels["headphone_types"], outputs.index(node[1])
    elif name == "mixLevelWithAnchor":
        mix = levels["mix"]
        cell = rcp_transfer.cross_point_of(node)
        # RCSYNCMIX carries the same name, on nodes of its own name.
        if kind != "str" or not mix.get("ok") or cell is None:
            return False
        output, source = cell
        holder = mix["levels"].get(output)
        if holder is None or source not in holder:
            return False
        key, value = source, rcp_read.decode_level_cell(value)
    else:
        return False
    if holder is None or holder[key] == value:
        return False
    holder[key] = value
    return True


def _set_master(master, node, name, value, kind):
    """Put one pushed value on the Master Mix page's state. Returns whether anything changed."""
    block, key, wanted = _MASTER[name]
    if master is None or kind != wanted or node != block or master[key] == value:
        return False
    master[key] = value
    return True


def _fader(levels, index):
    """The Levels state's record for fader `index`, or None for a fader without a level."""
    return next((f for f in levels["faders"] if f["index"] == index), None)


def _percent(level):
    return max(0, min(100, round(level * 100)))
