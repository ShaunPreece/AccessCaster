"""The window's picture of the desk, its channels, its output mixes, its levels and its master mix:
a view over the desk model (`desk_model`), built by `server.window_state`, the same code a read of
the desk builds it with.

It follows the desk listener (`desk_live`) while the window exists, this app's own writes included.
A change to anything the window shows asks for a rebuild from the model's snapshot, one per burst,
on a worker thread. The rebuild is compared with what the window holds, and subscribers are told
what changed, on the UI thread that draws it. Records are updated IN PLACE, because a channel page
keeps the record it was built from.

A full read of the desk happens at start, on request (F5, or after a write that moves other
values), when the desk comes back after dropping off the bus, and shortly after a STRUCTURAL change
(a fader's source, the USB mode, a mic's type), after which the desk may have moved values it does
not push.

A page that reads the desk for itself (the pads, the backups, the Device pages) watches the
properties it shows through the same follower (`watch`), so the window holds one subscription.
"""
import wx

import channels
import desk_live
import desk_model
import devices
import rcp_transfer
import server
from native.calls import call

STRUCTURAL = desk_model.STRUCTURAL
REREAD_AFTER_MS = 1500
# One rebuild for a burst: a fader or a knob pushes a value per step, and a mix preset a
# cross-point per write.
REBUILD_AFTER_MS = 30
MIX_MINUS, CUSTOM = 1, 2      # an output's mix types, as outputMixMinus holds them; 0 is Main Mix

# Every property the window shows: a push or a write of one rebuilds it.
_SHOWN = (STRUCTURAL
          | {channels.MUTE_PROP, channels.CUE_PROP, channels.WIRELESS_MUTE_PROP,
             channels.CHAIN_ADVANCED_PROP, channels.CHAIN_PRESET_MARKER_PROP,
             "channelBypassProcessing"}
          | set(channels.PROCESSING_MODULE_BOOLS) | set(channels.PROCESSING_READ_PROPS)
          | set(channels.CHANNEL_FX_ON.values())
          | {p for params in channels.CHANNEL_FX_PARAMS.values() for p in params}
          | {"inputMicrophoneGain", "inputDigitalGain", "inputPower", "inputPhaseFlip"}
          | {"outputMixMinus", "mixDisabled", "mixLevelWithAnchor"}
          | {"faderLevel", "headphoneType", "outputMonLevel", "outputMonMute", "outputBTLevel",
             "outputBTMute", "outputMonAutoMute", "outputBTAutoMute"}
          | {"outputMultiBypass", "masterCompellorOn", "masterCompellorThreshold",
             "masterCompellorAttack", "masterCompellorRelease", "masterCompellorGain",
             "masterDelayOn", "masterDelaySeconds"})


class DeskState:
    """`channels` is the window's channels (server.window_state: the channels with the desk's own
    0..1 params), one record per channel. `matrix` is the output mixes (rcp_read.read_matrix_state:
    `modes` and `excluded`, keyed by output and source index, `ok` False with a `reason` when the
    grid could not be read), with the layout as `matrix["model"]` (output_mixes.matrix_model). `faders`
    is the fader layout (`model`, `layout`, `labels`, `stereo`), `levels` the Levels page's state
    (server._window_levels) and `master` the Master Mix page's (server._window_master).

    A subscriber is called as `fn("read", reply)` after every full read, successful or not, and
    after a rebuild that changed which channels exist; `fn("changed", names)` with the names of the
    channels a change touched, `fn("mix", outputs)` with the indices of the outputs whose mode or
    mix changed, `fn("faders", None)`, `fn("levels", None)` and `fn("master", None)` when those
    changed, `fn("recorder", state)` with each recording state the desk pushes (`recorder.READY`
    and the rest), which it pushes for its own REC button and for a card or drive going in or out,
    and `fn("lost", None)` when the desk drops off the bus. The recorder is not held here: the
    Recording section reads it for itself."""

    def __init__(self, owner):
        self.owner = owner
        self.channels = None
        self.matrix = None
        self.faders = None
        self.levels = None
        self.master = None
        self._records = {}           # channel name -> its record in `channels`
        self._subscribers = []
        self._reading = False
        self._read_again = False
        self._reread = None
        self._building = False
        self._build_again = False
        self._rebuild = None
        self._stopped = False
        self._follower = None
        self._watches = []           # (names, fn) from `watch`

    def subscribe(self, fn):
        self._subscribers.append(fn)

    def watch(self, names, fn):
        """Call `fn()` on the UI thread for each push or write of a property in `names`. Made
        before `start`, which subscribes to every name watched."""
        self._watches.append((frozenset(names), fn))

    def start(self):
        watched = set().union(*(names for names, _fn in self._watches))
        self._follower = desk_live.Follower(
            "window", _SHOWN | {"recordState"} | watched,
            on_push=lambda push: wx.CallAfter(self._pushed, push),
            on_connected=self._connected,
            on_lost=lambda: wx.CallAfter(self._notify, "lost", None), own_writes=True).start()
        self.refresh()

    def stop(self):
        self._stopped = True
        for timer in (self._reread, self._rebuild):
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
        call(self.owner, _read, done=self._read_done)

    def _read_done(self, reply):
        self._reading = False
        if reply.get("ok"):
            self._take(reply)
        self._notify("read", reply)
        if self._read_again:
            self._read_again = False
            self.refresh()

    def _take(self, reply):
        self.channels = reply
        self.matrix = dict(reply["matrix"], model=reply["matrix_model"])
        self.faders = reply["faders"]
        self.levels = reply["levels"]
        self.master = reply["master"]
        self._records = {rec["name"]: rec for rec in reply["channels"]}

    def _reread_soon(self):
        if self._reread is not None and self._reread.IsRunning():
            self._reread.Start(REREAD_AFTER_MS)
        else:
            self._reread = wx.CallLater(REREAD_AFTER_MS, self.refresh)

    # ---- following the desk model ----------------------------------------------------------

    def _connected(self, gap):
        """The desk is open again after dropping off the bus: read it in full."""
        if gap is not None:
            wx.CallAfter(self.refresh)

    def _pushed(self, push):
        if self._stopped:
            return
        for names, fn in self._watches:
            if push.name in names:
                fn()
        if push.name == "recordState":
            if push.key == rcp_transfer.RECORDER and push.kind == "int":
                self._notify("recorder", push.value)
            return
        if self.channels is None or push.name not in _SHOWN:
            return
        if push.name in STRUCTURAL:
            self._reread_soon()
        if self._rebuild is None or not self._rebuild.IsRunning():
            self._rebuild = wx.CallLater(REBUILD_AFTER_MS, self._build)

    def _build(self):
        if self._stopped:
            return
        if self._building:
            self._build_again = True
            return
        self._building = True
        call(self.owner, _from_model, done=self._built)

    def _built(self, reply):
        self._building = False
        if not self._stopped and reply.get("ok") and self.channels is not None:
            self._merge(reply)
        if self._build_again:
            self._build_again = False
            self._build()

    def _merge(self, reply):
        """Take a rebuild, telling subscribers what it changed."""
        if [rec["name"] for rec in reply["channels"]] != list(self._records):
            self._take(reply)
            self._notify("read", reply)
            return
        names = set()
        for rec in reply["channels"]:
            held = self._records[rec["name"]]
            if held != rec:
                held.clear()
                held.update(rec)
                names.add(rec["name"])
        self.channels.update((k, v) for k, v in reply.items() if k != "channels")
        matrix = dict(reply["matrix"], model=reply["matrix_model"])
        outputs = _outputs_changed(self.matrix, matrix)
        _replace(self.matrix, matrix)
        faders = _replace(self.faders, reply["faders"])
        levels = _replace(self.levels, reply["levels"])
        master = _replace(self.master, reply["master"])
        if names:
            self._notify("changed", names)
        if outputs:
            self._notify("mix", outputs)
        if faders:
            self._notify("faders", None)
        if levels:
            self._notify("levels", None)
        if master:
            self._notify("master", None)

    def _notify(self, event, detail):
        for fn in self._subscribers:
            fn(event, detail)


def _read():
    """A full read on a worker thread."""
    return _named(server.read_window_state())


def _from_model():
    """The window's state from the desk model's snapshot, on a worker thread."""
    blob = desk_model.snapshot()
    if not blob:
        return {"ok": False}
    return _named(server.window_state(blob))


def _named(reply):
    if reply.get("ok"):
        reply["device_name"] = devices.get_active_device().name
    return reply


def _replace(held, new):
    """Make `held` equal `new` in place. Returns whether it changed."""
    if held == new:
        return False
    held.clear()
    held.update(new)
    return True


def _outputs_changed(old, new):
    """The outputs whose mode or Custom mix differ between two `matrix` states, and every output
    either knows of when one of them could not be read."""
    if not (old.get("ok") and new.get("ok")):
        return (set(old.get("modes") or ()) | set(new.get("modes") or ())) if old != new else set()
    return {o for o in set(old["modes"]) | set(new["modes"])
            if old["modes"].get(o) != new["modes"].get(o)
            or old["excluded"].get(o) != new["excluded"].get(o)}
