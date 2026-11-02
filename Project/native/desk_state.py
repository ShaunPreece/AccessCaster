"""The window's picture of the desk: read once, then kept current by what the desk pushes.

It listens on a read-only HID handle of its own, because the announcer's listener runs only while
announcements are on. The state belongs to the UI thread: the listener thread decodes a push and
hands it over with wx.CallAfter, so the records are changed, and subscribers told, only on the
thread that draws them.

A full read happens at start, on request (F5, or after a write that moves other values), when the
desk comes back after dropping off the bus, and shortly after a push that changes which channels
exist or where they sit (a fader's source, the USB mode) or what a mic is (its type, which moves
the gain and the chain). Everything else a push carries is applied to the record it addresses.
"""
import threading
import time

import wx

import channels
import devices
import diagnostics
import rcp_hid
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

# Per-strip state on the channel block (SEL_CHANNEL + strip): record field, or a function of the
# record and the value.
_STRIP_BOOLS = {channels.MUTE_PROP: "muted", channels.CUE_PROP: "solo",
                channels.WIRELESS_MUTE_PROP: "wireless_muted",
                channels.CHAIN_ADVANCED_PROP: "advanced"}
_MODULES = set(channels.PROCESSING_MODULE_BOOLS)
_PARAMS = set(channels.PROCESSING_READ_PROPS)
# Per-strip effects on the effects block (SEL_CHANNEL_FX + strip).
_FX_ON = {prop: effect for effect, prop in channels.CHANNEL_FX_ON.items()}
_FX_PARAMS = {p: effect for effect, ps in channels.CHANNEL_FX_PARAMS.items() for p in ps}
# Per-input values, addressed at the input itself.
_INPUTS = {"inputMicrophoneGain", "inputDigitalGain", "inputPower", "inputPhaseFlip"}

_RELEVANT = (STRUCTURAL | set(_STRIP_BOOLS) | _MODULES | _PARAMS | set(_FX_ON) | set(_FX_PARAMS)
             | _INPUTS | {"channelBypassProcessing", channels.CHAIN_PRESET_MARKER_PROP})
_CHAIN = (_MODULES | _PARAMS | {channels.CHAIN_ADVANCED_PROP, channels.CHAIN_PRESET_MARKER_PROP})


class DeskState:
    """`channels` is the last full read (server.action_get_channels with the desk's own 0..1
    params), with each record changed in place as pushes arrive. A subscriber is called as
    `fn("read", reply)` after every full read, successful or not, and `fn("changed", names)` with
    the names of the channels a push changed."""

    def __init__(self, owner):
        self.owner = owner
        self.channels = None
        self._records = {}           # channel name -> its record in `channels`
        self._strip_names = {}       # strip index -> channel name
        self._subscribers = []
        self._reading = False
        self._read_again = False
        self._reread = None
        self._preset_timers = {}
        self._dirty = set()
        self._flush = None
        self._stop = threading.Event()
        self._thread = None

    def subscribe(self, fn):
        self._subscribers.append(fn)

    def start(self):
        self._thread = threading.Thread(target=self._listen, name="window-desk-listener",
                                        daemon=True)
        self._thread.start()
        self.refresh()

    def stop(self):
        self._stop.set()
        for timer in [self._reread, self._flush, *self._preset_timers.values()]:
            if timer is not None:
                timer.Stop()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

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

    def _reread_soon(self):
        if self._reread is not None and self._reread.IsRunning():
            self._reread.Start(REREAD_AFTER_MS)
        else:
            self._reread = wx.CallLater(REREAD_AFTER_MS, self.refresh)

    # ---- pushes ----------------------------------------------------------------------------

    def _listen(self):
        """The listener thread. It reopens its handle when the desk drops off the bus, and asks
        for a full read once it is back."""
        dev, lost = None, False
        while not self._stop.is_set():
            if dev is None:
                iface = rcp_transfer.find_control_interface(fresh=True)
                if iface is not None:
                    try:
                        dev = rcp_hid.open_handle(iface, "window listener")
                    except Exception as exc:
                        diagnostics.event("device.open_failed", "window listener",
                                          error=f"{exc}")
                if dev is None:
                    self._stop.wait(2.0)
                    continue
                if lost:
                    lost = False
                    wx.CallAfter(self.refresh)
            try:
                report = dev.read(256, timeout_ms=250)
            except Exception:
                rcp_hid.close_handle(dev, "window listener")
                dev, lost = None, True
                self._stop.wait(0.5)
                continue
            if not report or report[0] != 0x04:
                continue
            push = rcp_read.parse_push(bytes(report))
            if push is not None and push[1] in _RELEVANT and not self._stop.is_set():
                wx.CallAfter(self._apply, *push)
        if dev is not None:
            rcp_hid.close_handle(dev, "window listener")

    def _apply(self, path, name, value, kind):
        if self._stop.is_set() or self.channels is None:
            return
        if name in STRUCTURAL:
            self._reread_soon()
            return
        if len(path) != 1:
            return
        rec = self._record_for(path[0], name)
        if rec is None or not _set(rec, name, value, kind):
            return
        self._dirty.add(rec["name"])
        if name in _CHAIN and rec["kind"] == "mic":
            self._preset_soon(rec["name"])
        if self._flush is None or not self._flush.IsRunning():
            # One notification for a burst: a fader or a knob pushes a value per step.
            self._flush = wx.CallLater(30, self._flush_dirty)

    def _record_for(self, selector, name):
        if name in _INPUTS:
            channel = devices.get_active_device().channel_for_input_path([selector])
            return self._records.get(channel["name"]) if channel else None
        base = rcp_transfer.SEL_CHANNEL_FX if name in _FX_ON or name in _FX_PARAMS \
            else rcp_transfer.SEL_CHANNEL
        return self._records.get(self._strip_names.get(selector - base))

    def _flush_dirty(self):
        names, self._dirty = self._dirty, set()
        if names:
            self._notify("changed", names)

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
    reply = server.action_get_channels(percent=False)
    if reply.get("ok"):
        reply["strips"] = channels.last_known_strip_vector()
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
