"""AccessCaster - the one listener: a single read-only HID handle hearing everything the desk pushes.

The desk pushes a report-04 frame of its own accord whenever anything on it changes. This module
holds the app's only long-lived handle for hearing them, decodes each push once, applies it to the
desk model (`desk_model`), and then hands it to every subscriber that asked for that property name,
so a subscriber that looks at the model sees the value it is being told about.

  - EVERY SUBSCRIBER HAS ITS OWN QUEUE, and nothing a subscriber does runs on the reader. Encoder
    hold's mute is a write under the HID lock and CallMe rings and speaks, so a callback on the
    reader would stall every other consumer, and a write waiting for its own push would wait on
    itself. The reader only decodes and enqueues.
  - A `Subscription` is the queue alone, for a caller that waits on it itself (`PushListener`, the
    announcer's loop). A `Follower` is a subscription with a thread that hands each event to
    callbacks, for everything else.
  - The queue carries three kinds of event: `Push`, `Connected` and `Lost`. A subscription made
    while the desk is open receives a `Connected` first, so "the desk is there" is learned the same
    way on the first open and on every reopen.
  - A subscription made with `own_writes` also hears what this app writes, as a `Push` marked
    `ours`, because the desk does not push all of them back.

THE HANDLE IS REOPENED ON A FRESHLY RESOLVED INTERFACE, never a remembered one: the desk
re-enumerates on `outputMultiMode`, `engineMode` and `systemMainMode` and comes back under a
different product id and path. It is closed only by the reader thread, because closing a hidapi
handle while a read is in flight on another thread segfaults the interpreter.

Windows gives every open handle every input report, model dumps included; `rcp_read.parse_push`
answers None for a dump slice, so a page reading the desk never arrives here as a change.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import NamedTuple, Optional

import desk_model
import diagnostics
import rcp_hid
import rcp_read

# Reconnect pacing. Quick at first, so a re-enumeration (about 2.5 s) is picked up as soon as the
# desk is back; slow after that, because every attempt rescans the USB HID tree and a desk off the
# bus may never return without a replug.
RETRY_S = 0.3
RETRY_SLOW_S = 2.0
RETRY_FAST_TRIES = 15
READ_TIMEOUT_MS = 250
# How long a new `PushListener` waits for the listener to have the desk open, so a command sent
# straight after the app starts is not written before anything is listening for its answer.
CONNECT_WAIT_S = 1.0


class Push(NamedTuple):
    """One property the desk pushed, or this app wrote (`ours`). `key` is the node it is addressed
    at, named by the desk's own layout (`rcp_read.push_key`), or None when no layout is known or
    the node is not a fixed one. `was` is the desk model's value before it, None when it held none,
    which is how a subscriber tells a change from the desk re-pushing a value it already had."""
    path: tuple
    name: str
    value: object
    kind: str
    key: Optional[tuple]
    was: object = None
    ours: bool = False


class Connected(NamedTuple):
    """The listener has the desk open. `gap` is how long it was deaf before this, in seconds, or
    None for the first open this subscriber has seen."""
    gap: Optional[float]


class Lost(NamedTuple):
    """The listener's read failed, which is what the desk leaving the bus looks like."""
    error: str


class Subscription:
    """A queue of the events one consumer asked for. `names` is the push names it wants, or None for
    every push; `Connected` and `Lost` always arrive, and this app's own writes only with
    `own_writes`."""

    def __init__(self, purpose: str, names=None, own_writes: bool = False):
        self.purpose = purpose
        self.names = frozenset(names) if names is not None else None
        self.own_writes = own_writes
        self.lost = False           # the desk dropped since this subscription was made
        self._q: queue.Queue = queue.Queue()

    def wants(self, name: str) -> bool:
        return self.names is None or name in self.names

    def get(self, timeout: float):
        """The next event, or None if nothing arrives within `timeout` seconds."""
        try:
            return self._q.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None

    def _put(self, event) -> None:
        if isinstance(event, Lost):
            self.lost = True
        self._q.put(event)

    def close(self) -> None:
        _unregister(self)


# ---- the listener ----------------------------------------------------------------------------

_lock = threading.Lock()            # the subscriber list, and the connected state they are told
_subscribers: list[Subscription] = []
_connected = threading.Event()
_stop = threading.Event()
_thread: Optional[threading.Thread] = None
_start_lock = threading.Lock()


def start() -> None:
    """Start the listener if it is not running. Safe to call from any thread, any number of times."""
    global _thread
    with _start_lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_run, name="desk-listener", daemon=True)
        _thread.start()


def stop(timeout: float = 3.0) -> None:
    """Stop the listener and wait for its thread, which closes the handle. Subscriptions stay
    registered and hear from a listener started again."""
    global _thread
    with _start_lock:
        _stop.set()
        if _thread is not None:
            _thread.join(timeout=timeout)
        _thread = None
        _connected.clear()
        desk_model.set_connected(False)


def connected() -> bool:
    """Whether the listener has the desk open right now."""
    return _connected.is_set()


def wait_connected(timeout: float) -> bool:
    return _connected.wait(timeout)


def subscribe(purpose: str, names=None, own_writes: bool = False) -> Subscription:
    """A new subscription, with the listener started if it was not running."""
    sub = Subscription(purpose, names, own_writes)
    with _lock:
        _subscribers.append(sub)
        if _connected.is_set():
            sub._put(Connected(None))
    start()
    return sub


def _unregister(sub: Subscription) -> None:
    with _lock:
        if sub in _subscribers:
            _subscribers.remove(sub)


def _broadcast(event) -> None:
    with _lock:
        if isinstance(event, Connected):
            _connected.set()
            desk_model.set_connected(True)
        elif isinstance(event, Lost):
            _connected.clear()
            desk_model.set_connected(False)
        for sub in _subscribers:
            sub._put(event)


def _fan_out(push: Push) -> None:
    with _lock:
        for sub in _subscribers:
            if sub.wants(push.name) and (sub.own_writes or not push.ours):
                sub._put(push)


def _written(record: bytes) -> None:
    """`rcp_hid.write_watcher`: one record this app has written, applied to the desk model and,
    when it landed there, handed to the subscriptions that asked for our own writes."""
    got = desk_model.apply_write(record)
    if got is not None:
        path, name, value, kind, was = got
        _fan_out(Push(path, name, value, kind, rcp_read.push_key(path), was, True))


rcp_hid.write_watcher = _written


def decode(frame: bytes) -> Optional[Push]:
    """The push in one report-04 frame, or None for anything else, a model dump's slice included."""
    rec = rcp_read.parse_push(frame)
    if rec is None:
        return None
    path, name, value, kind = rec
    return Push(tuple(path), name, value, kind, rcp_read.push_key(path))


def _run() -> None:
    dev = None
    misses = 0                  # consecutive failed opens, for the back-off
    lost_at = None              # when the last open handle failed, while deaf
    absent = False              # "no interface" is logged once per absence, not once a retry
    while not _stop.is_set():
        if dev is None:
            # A reconnect scans afresh: a remembered list would wait out its age limit first.
            iface = rcp_hid.find_control_interface(fresh=lost_at is not None or misses > 0)
            if iface is None:
                if not absent:
                    diagnostics.event("device.absent", "no control interface to listen on")
                    absent = True
            else:
                absent = False
                try:
                    dev = rcp_hid.open_handle(iface, "desk listener")
                except Exception as exc:
                    diagnostics.event("device.open_failed", "desk listener", error=f"{exc}")
            if dev is None:
                misses += 1
                _stop.wait(RETRY_S if misses <= RETRY_FAST_TRIES else RETRY_SLOW_S)
                continue
            misses = 0
            gap = None if lost_at is None else time.monotonic() - lost_at
            lost_at = None
            _broadcast(Connected(gap))
        try:
            rep = dev.read(rcp_read.BULK_LEN, timeout_ms=READ_TIMEOUT_MS)
        except Exception as exc:
            # THE DESK STOPPING IS SEEN HERE FIRST, because this is the one reader that is always
            # on. Its timestamp is the app's own answer to "when did it stop answering".
            diagnostics.event("device.lost", "desk listener read failed", error=f"{exc}")
            rcp_hid.forget_interfaces()
            rcp_hid.close_handle(dev, "desk listener")
            dev = None
            lost_at = time.monotonic()
            _broadcast(Lost(f"{exc}"))
            _stop.wait(RETRY_S)
            continue
        if not rep or rep[0] != 0x04:
            continue
        try:
            push = decode(bytes(rep))
        except Exception as exc:    # one undecodable frame costs one push, never the listener
            diagnostics.event("desk_live.push_undecoded", str(exc))
            continue
        if push is not None:
            was = desk_model.apply_push(push.path, push.name, push.value, push.kind)
            _fan_out(push._replace(was=was))
    if dev is not None:
        rcp_hid.close_handle(dev, "desk listener")
    _connected.clear()
    desk_model.set_connected(False)


# ---- a subscriber with a thread of its own ----------------------------------------------------

class Follower:
    """A subscription whose events are handed to callbacks on a thread of its own.

    `on_push(push)` for each push named in `names` (and each of this app's own writes of them, with
    `own_writes`), `on_connected(gap)` and `on_lost()` as the
    events arrive, and `on_tick()` at least every `tick_s` seconds, for a consumer that times
    something between pushes. A callback that raises costs that one event, never the thread."""

    def __init__(self, purpose: str, names, on_push=None, on_connected=None, on_lost=None,
                 on_tick=None, tick_s: float = 0.25, own_writes: bool = False):
        self.purpose = purpose
        self._names = names
        self._own_writes = own_writes
        self._on_push = on_push
        self._on_connected = on_connected
        self._on_lost = on_lost
        self._on_tick = on_tick
        self._tick_s = tick_s
        self._sub: Optional[Subscription] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "Follower":
        self._sub = subscribe(self.purpose, self._names, self._own_writes)
        self._thread = threading.Thread(target=self._run, name=f"follow-{self.purpose}",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        if self._sub is not None:
            self._sub.close()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        sub = self._sub
        next_tick = time.monotonic() + self._tick_s
        while not self._stop.is_set():
            event = sub.get(max(0.0, next_tick - time.monotonic()) if self._on_tick
                            else self._tick_s)
            if self._stop.is_set():
                break
            if event is not None:
                self._deliver(event)
            if self._on_tick and time.monotonic() >= next_tick:
                self._call(self._on_tick)
                next_tick = time.monotonic() + self._tick_s

    def _deliver(self, event) -> None:
        if isinstance(event, Push):
            if self._on_push:
                self._call(self._on_push, event)
        elif isinstance(event, Connected):
            if self._on_connected:
                self._call(self._on_connected, event.gap)
        elif isinstance(event, Lost):
            if self._on_lost:
                self._call(self._on_lost)

    def _call(self, fn, *args) -> None:
        try:
            fn(*args)
        except Exception as exc:
            diagnostics.event("desk_live.subscriber_failed", self.purpose, error=f"{exc}")


# ---- waiting for one answer -------------------------------------------------------------------

class PushListener:
    """WAIT FOR THE DESK TO SAY SO, instead of re-reading the whole tree until it has.

    The protocol has no partial read: one trigger, 536 frames, about 136 kB, well over a second. So
    answering "did that command work?" by dumping the model every 1.5 s puts our traffic on the
    wire for the whole time the desk is busy, which on a factory reset is exactly when it
    re-enumerates its USB audio function. The desk pushes what changed instead.

    MEASURED ON A DUO. A factory reset pushes 3267 frames covering 195 distinct properties, from
    0.02 s after the command to t+4.96 s as one continuous burst with no gap of half a second in
    it. `currentShowUUID` is among them, so the new show id, the desk's only confirmation that a
    reset happened, arrives on its own. An impossible export pushes just three frames
    (`showControlLastError` twice, then `showControlExport`) and moves NOTHING in the tree, so for
    that command a push is the only evidence there is.

    NO HID LOCK. It is a subscription on the one listener, so callers hold the lock for the WRITE
    and let go before waiting, rather than holding it across a multi-second wait.

    START IT BEFORE THE WRITE. The burst starts 20 ms after the command, and only pushes heard after
    `start` are queued. 74 of the 3267 frames in a reset burst are not one complete record and are
    dropped by `parse_push`, with no dump running anywhere.
    """

    def __init__(self):
        self._sub: Optional[Subscription] = None

    def __enter__(self) -> "PushListener":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def lost(self) -> bool:
        """The desk dropped off the bus while this was listening.

        NOT proof on its own: hidapi does not promise an exception on device removal, so a drop
        can also present as a plain timeout. Treat this as a strong hint, never the only check."""
        return self._sub is not None and self._sub.lost

    def start(self) -> None:
        self._sub = subscribe("push listener")
        wait_connected(CONNECT_WAIT_S)

    def _next_push(self, deadline: float):
        """The next push queued before `deadline`, or None once it has passed or the desk is lost.
        Each wait ends at the deadline, so a steady stream of other pushes (a playing pad sends
        padProgress about 20 times a second) cannot hold it open. A lost desk does not discard
        what was already heard: the queue is drained first, so a desk that pushed its answer and
        then fell off the bus is not reported as a failure."""
        while True:
            event = self._sub.get(min(0.1, deadline - time.monotonic()))
            if event is None:
                if self.lost or time.monotonic() >= deadline:
                    return None
                continue
            if isinstance(event, Push):
                return event

    def wait_for(self, name: str, timeout: float, match=None):
        """The value of the next pushed `name`, or None on timeout or on the desk being lost.

        `match(value)` filters, for the common case of waiting on a CHANGE rather than on any push:
        the desk re-pushes a property at its existing value (`showControlLastError` arrived twice
        still reading 8), so "it was pushed" and "it changed" are different questions."""
        deadline = time.monotonic() + timeout
        while True:
            push = self._next_push(deadline)
            if push is None:
                return None
            if push.name == name and (match is None or match(push.value)):
                return push.value

    def wait_any(self, names, timeout: float):
        """(name, value) of the next push of any of `names`, or None on timeout or on the desk
        being lost. For following a job the desk reports through several properties at once."""
        deadline = time.monotonic() + timeout
        while True:
            push = self._next_push(deadline)
            if push is None:
                return None
            if push.name in names:
                return push.name, push.value

    def wait_until_quiet(self, idle: float = 0.75, timeout: float = 20.0) -> float:
        """Block until the desk has pushed nothing for `idle` seconds. Returns how long that took.

        "IT WORKED" AND "IT HAS FINISHED" ARE DIFFERENT QUESTIONS, and only the first is answered
        by a property arriving. A factory reset pushes `currentShowUUID` at about t+1.8 s and keeps
        going until t+4.96 s, so a caller that acts the moment its answer lands is writing to a desk
        that is still rewriting itself, and on a reset that is also while it re-enumerates.

        `idle` is comfortably above the burst's own internal gaps: measured over 3267 frames, not
        one gap reached half a second."""
        started = time.monotonic()
        deadline = started + timeout
        while True:
            event = self._sub.get(idle)
            if event is None:
                return time.monotonic() - started
            if self.lost or time.monotonic() >= deadline:
                return time.monotonic() - started

    def stop(self) -> None:
        if self._sub is not None:
            self._sub.close()
