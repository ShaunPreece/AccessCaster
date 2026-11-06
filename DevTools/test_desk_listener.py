"""The one desk listener (Project/desk_live.py), offline: the real reader thread over a fake handle.

Each subscriber hears only the names it asked for, a subscriber made while the desk is open hears
`Connected` first, a dump slice is never a push, a failed read is `Lost` to everyone and the
reopen is `Connected` with the gap, a slow subscriber holds up nobody else, and `PushListener`
answers from the queue it was given before the write.

    python DevTools/test_desk_listener.py
"""
from __future__ import annotations

import queue
import sys
import threading
import time

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
import desk_live
import rcp_hid
from _testkit import check, check_true, patched, push, v_bool, v_int


class FakeIface:
    product_id = 0x0037
    interface_number = 9
    path = b"fake"


class FakeDev:
    """Hands out queued frames; `fail()` makes the next read raise, as a desk leaving the bus does."""

    def __init__(self, frames):
        self.frames = frames
        self.dead = False

    def read(self, _len, timeout_ms=0):
        if self.dead:
            raise OSError("read error")
        try:
            return list(self.frames.get(timeout=timeout_ms / 1000))
        except queue.Empty:
            return []


class Desk:
    def __init__(self):
        self.frames = queue.Queue()
        self.dev = None
        self.present = True
        self.opens = 0

    def find(self, *a, **k):
        return FakeIface() if self.present else None

    def open(self, iface, purpose):
        self.opens += 1
        self.dev = FakeDev(self.frames)
        return self.dev

    def send(self, frame):
        self.frames.put(frame)

    def drop(self, gap):
        self.present = False
        self.dev.dead = True
        time.sleep(gap)
        self.present = True


def events(sub, wait=0.3):
    out, deadline = [], time.monotonic() + wait
    while time.monotonic() < deadline:
        e = sub.get(0.05)
        if e is not None:
            out.append(e)
    return out


desk = Desk()
with patched((rcp_hid, {"find_control_interface": desk.find, "open_handle": desk.open,
                        "close_handle": lambda dev, purpose="": None,
                        "forget_interfaces": lambda: None}),
             (desk_live, {"RETRY_S": 0.02})):
    print("SUBSCRIBING")
    everything = desk_live.subscribe("all")
    check_true("the listener opens the desk", desk_live.wait_connected(1.0))
    check("a subscriber hears that the desk is open", type(everything.get(1.0)).__name__,
          "Connected")
    mutes = desk_live.subscribe("mutes", {"channelOutputMute"})
    check("a later subscriber hears it too, with no gap", mutes.get(1.0), desk_live.Connected(None))

    print("\nPUSHES")
    desk.send(push([3, 1], "channelOutputMute", v_bool(True)))
    desk.send(push([10], "selectedBank", v_int(2)))
    desk.send(bytes([0x04]) + bytes(range(1, 256)))         # a slice of a model dump
    got = events(everything)
    check("every push reaches a subscriber that asked for everything",
          [e.name for e in got], ["channelOutputMute", "selectedBank"])
    check("decoded once, with the value and kind", (got[0].path, got[0].value, got[0].kind),
          ((3, 1), True, "bool"))
    check("a subscriber hears only the names it asked for",
          [e.name for e in events(mutes, 0.1)], ["channelOutputMute"])

    print("\nA SLOW SUBSCRIBER HOLDS UP NOBODY")
    slow_started, slow_release, heard = threading.Event(), threading.Event(), []

    def slow(_push):
        slow_started.set()
        slow_release.wait(2.0)

    slow_one = desk_live.Follower("slow", {"selectedBank"}, on_push=slow).start()
    quick = desk_live.Follower("quick", {"selectedBank"}, on_push=heard.append).start()
    desk.send(push([10], "selectedBank", v_int(3)))
    slow_started.wait(1.0)
    time.sleep(0.2)
    check("the quick follower has it while the slow one is still busy", len(heard), 1)
    slow_release.set()
    slow_one.stop()
    quick.stop()

    print("\nA FOLLOWER TICKS BETWEEN PUSHES")
    ticks = []
    ticker = desk_live.Follower("ticker", set(), on_tick=lambda: ticks.append(1), tick_s=0.05).start()
    time.sleep(0.3)
    ticker.stop()
    check_true("it ticked with nothing pushed", len(ticks) >= 3, f"{len(ticks)} ticks")

    print("\nTHE DESK DROPS AND COMES BACK")
    events(everything, 0.05)
    desk.drop(0.2)
    got = events(everything, 0.5)
    kinds = [type(e).__name__ for e in got]
    check("everyone hears it lost, then back", kinds, ["Lost", "Connected"])
    # Deaf from the failed read to the reopen, not from the moment the desk went.
    check_true("and the reopen carries how long it was deaf", got[-1].gap > 0, f"{got[-1].gap}")
    check("the handle was opened again", desk.opens, 2)
    check_true("a subscription remembers the loss", everything.lost)

    print("\nWAITING FOR ONE ANSWER")
    listener = desk_live.PushListener()
    listener.start()
    desk.send(push([10], "selectedBank", v_int(1)))
    desk.send(push([3, 2], "channelOutputMute", v_bool(False)))
    check("wait_for skips what it is not waiting for",
          listener.wait_for("channelOutputMute", 1.0), False)
    check("and times out when nothing matches", listener.wait_for("currentShowUUID", 0.2), None)
    desk.send(push([10], "selectedBank", v_int(0)))
    check("wait_any names the push it found",
          listener.wait_any(("selectedBank", "padActive"), 1.0), ("selectedBank", 0))
    check_true("wait_until_quiet returns once nothing is pushed",
               listener.wait_until_quiet(idle=0.1, timeout=1.0) < 0.5)
    listener.stop()

    print("\nUNSUBSCRIBING")
    mutes.close()
    events(mutes, 0.05)
    desk.send(push([3, 1], "channelOutputMute", v_bool(False)))
    time.sleep(0.2)
    check("a closed subscription hears nothing more", mutes.get(0.1), None)
    everything.close()
    desk_live.stop()
    check("stopping clears the connected state", desk_live.connected(), False)

sys.exit(_testkit.finish())
