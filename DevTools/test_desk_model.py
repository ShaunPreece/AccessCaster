"""The desk model (Project/desk_model.py), offline, on a synthetic dump.

A dump is adopted with what was pushed while it was read replayed on top; the picture is trusted
only while the listener is up, on the connection the dump came from, with no structural change since
it started; this app's writes land only on a fixed node that carries the property; and through the
listener, a subscriber is told the value a push replaced, after the model already holds the new one,
and hears this app's writes only when it asked for them.

    python DevTools/test_desk_model.py
"""
from __future__ import annotations

import queue
import sys
import time

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
import desk_live
import desk_model
import fake_model
import rcp_hid
import rcp_read
import rcp_transfer
from _testkit import check, check_true, patched, push
from fake_model import node

FADER, POT = "FADER", "POT"


def dump(mute1=False, source2=5, bank=0, selected=-1, mon=0.5):
    """A small desk: PHYSICALINTERFACE (four pots, nine faders), three channel strips, OUTPUT, GUI,
    RECORDER, SYSTEM and a pad pool of two."""
    phys = node("PHYSICALINTERFACE", {}, [node(POT, {"potLevel": 10 * i}) for i in range(4)]
                + [node(FADER, {"faderLevel": 127 if i == 7 else 0}) for i in range(9)])
    strips = [node("CHANNEL", {"channelInputSource": src, "channelOutputMute": muted,
                               "channelCueEnable": False})
              for src, muted in ((0, False), (1, mute1), (source2, False))]
    return fake_model.node("Rodecaster", {}, [
        phys, *strips,
        node("OUTPUT", {"outputMonLevel": mon, "outputMultiMode": 0}),
        node("GUI", {"selectedBank": bank}),
        node("RECORDER", {"recordState": 1}),
        node("SYSTEM", {"systemChannelSelected": selected, "systemFirmwareVersion": "1.7.7"}),
        node("SOUNDPADS", {}, [node("PAD", {"padActive": False}) for _ in range(2)]),
    ])


def v_bool(on: bool) -> bytes:
    return bytes([0x01, 0x01, 0x02 if on else 0x03])


def v_int(value: int) -> bytes:
    return bytes([0x01, 0x05, 0x01]) + int(value).to_bytes(4, "little", signed=True)


def adopt(blob, token=None):
    desk_model.dump_finished(token or desk_model.dump_starting(), blob)


BOOK = rcp_read.layout_of(dump())
STRIP1 = tuple(BOOK.path(("CHANNEL", 1)))
STRIP2 = tuple(BOOK.path(("CHANNEL", 2)))
GUI = tuple(BOOK.path(("GUI", 0)))
PAD0 = tuple(BOOK.path(("SOUNDPADS", 0))) + (0,)

desk_model._reset()
desk_model.set_connected(True)

print("ADOPTING A DUMP")
check("nothing is known before the first dump", (desk_model.bank(), desk_model.trusted()),
      (None, False))
adopt(dump(mute1=True))
check("the fader layout", desk_model.strip_vector(), [0, 1, 5])
check("the mutes, by strip", desk_model.mutes(), {0: False, 1: True, 2: False})
check("a virtual fader's level", desk_model.fader_level(7), 1.0)
check("a headphone knob's level", desk_model.pot_level(2), 20 / 127)
check("the monitor level, the bank, the selected channel, the record state, the firmware",
      (desk_model.monitor_level(), desk_model.bank(), desk_model.selected_channel(),
       desk_model.record_state(), desk_model.firmware()), (0.5, 0, -1, 1, "1.7.7"))
check_true("trusted once read with the listener up", desk_model.trusted())

print("\nA PUSH")
was = desk_model.apply_push(STRIP1, "channelOutputMute", False, "bool")
check("is applied, and answers the value it replaced", (desk_model.mutes()[1], was), (False, True))
check("to a property the picture never held, answers None",
      desk_model.apply_push(GUI, "newThing", 3, "int"), None)
check("which is held from then on", desk_model.value(("GUI", 0), "newThing"), 3)

print("\nA DUMP TAKEN WHILE THE DESK CHANGES")
token = desk_model.dump_starting()
desk_model.apply_push(GUI, "selectedBank", 4, "int")            # heard while the dump is read
adopt(dump(bank=0), token)                                      # the dump still says bank 0
check("what was pushed during the read is replayed on top", desk_model.bank(), 4)
check_true("and the picture is trusted", desk_model.trusted())

print("\nSTRUCTURAL CHANGES")
desk_model.apply_push(STRIP2, "channelInputSource", 5, "int")
check_true("re-pushing a fader's source at its value changes nothing", desk_model.trusted())
desk_model.apply_push(STRIP2, "channelInputSource", 7, "int")
check_true("a fader's source moving makes the picture untrusted", not desk_model.trusted())
token = desk_model.dump_starting()
adopt(dump(source2=7), token)
check_true("until a dump started after it is adopted", desk_model.trusted())
token = desk_model.dump_starting()
desk_model.apply_push(STRIP2, "channelInputSource", 8, "int")
adopt(dump(source2=7), token)
check("a move during the read is replayed", desk_model.strip_vector(), [0, 1, 8])
check_true("but leaves the picture untrusted, the read having started before it",
           not desk_model.trusted())
adopt(dump(source2=8))

print("\nTHE DESK LEAVING")
token = desk_model.dump_starting()
desk_model.set_connected(False)
rcp_hid.forget_interfaces()
check_true("a lost listener makes the picture untrusted", not desk_model.trusted())
check("but it still answers, for naming", desk_model.strip_vector(), [0, 1, 8])
desk_model.set_connected(True)
adopt(dump(source2=3), token)
check("a dump begun on the old connection is not adopted", desk_model.strip_vector(), [0, 1, 8])
check_true("and the picture stays untrusted", not desk_model.trusted())
adopt(dump(source2=3))
check_true("a dump on the new connection restores it", desk_model.trusted())

print("\nTHE JOURNAL RUNNING OUT")
token = desk_model.dump_starting()
for i in range(desk_model.JOURNAL_MAX + 1):
    desk_model.apply_push(GUI, "selectedBank", i % 8, "int")
adopt(dump(), token)
check_true("a read that outlived the journal leaves the picture untrusted", not desk_model.trusted())
adopt(dump())

print("\nTHE PICTURE AS A DUMP")
check("unchanged, it is the dump adopted, byte for byte", desk_model.snapshot(), dump())
desk_model.apply_push(GUI, "selectedBank", 5, "int")
desk_model.apply_push(STRIP1, "channelOutputMute", True, "bool")
desk_model.apply_push(tuple(BOOK.path(("OUTPUT", 0))), "outputMonLevel", 0.25, "float")
check("each change is written in where the desk would put it", desk_model.snapshot(),
      dump(mute1=True, bank=5, mon=0.25))
SYSTEM = tuple(BOOK.path(("SYSTEM", 0)))
desk_model.apply_push(SYSTEM, "newThing", 1, "int")
check("a property the dump did not carry has no place in it", desk_model.snapshot(),
      dump(mute1=True, bank=5, mon=0.25))
body = dump()
headed = (len(body) + 1).to_bytes(4, "little") + b"\x02" + body
adopt(headed)
desk_model.apply_push(SYSTEM, "systemFirmwareVersion", "1.7.10", "str")
snap = desk_model.snapshot()
check("a value that grows is read back from it", rcp_read.first(snap, "systemFirmwareVersion"),
      "1.7.10")
check("and the stream header's length grows with it", int.from_bytes(snap[:4], "little"),
      len(snap) - 4)
adopt(dump())

print("\nTHIS APP'S WRITES")
mute = rcp_transfer.build_record("channelOutputMute", "bool", True, STRIP1)
check("a write at a fixed node carrying it is applied", desk_model.apply_write(mute),
      (STRIP1, "channelOutputMute", True, "bool", False))
check("and held", desk_model.mutes()[1], True)
stray = rcp_transfer.build_record("notOnAStrip", "bool", True, STRIP1)
check("a property the node does not carry is not", desk_model.apply_write(stray), None)
pad = rcp_transfer.build_record("padActive", "bool", True, PAD0)
check("nor is a write to a pad, which is not a fixed node", desk_model.apply_write(pad), None)
check("and the pad keeps its value", desk_model.values("padActive"), [False, False])
check("a frame that is not a record is ignored", desk_model.apply_write(b"\x01\x02"), None)

print("\nTHROUGH THE LISTENER")


class FakeIface:
    product_id = 0x0037
    interface_number = 9
    path = b"fake"


class FakeDev:
    def __init__(self, frames):
        self.frames = frames

    def read(self, _len, timeout_ms=0):
        try:
            return list(self.frames.get(timeout=timeout_ms / 1000))
        except queue.Empty:
            return []


frames = queue.Queue()
with patched((rcp_hid, {"find_control_interface": lambda *a, **k: FakeIface(),
                        "open_handle": lambda iface, purpose: FakeDev(frames),
                        "close_handle": lambda dev, purpose="": None})):
    adopt(dump(mute1=True))
    seen = []
    follower = desk_live.Follower(
        "bank", {"selectedBank"},
        on_push=lambda p: seen.append((p.value, p.was, desk_model.bank(), p.ours))).start()
    window = desk_live.subscribe("window", {"channelOutputMute"}, own_writes=True)
    others = desk_live.subscribe("others", {"channelOutputMute"})
    check_true("the listener opens the desk", desk_live.wait_connected(1.0))
    frames.put(push(GUI, "selectedBank", v_int(6)))
    deadline = time.monotonic() + 1.0
    while not seen and time.monotonic() < deadline:
        time.sleep(0.02)
    check("a subscriber hears the value, the one it replaced, and the model already has it",
          seen, [(6, 0, 6, False)])
    rcp_hid.note_written(rcp_transfer.build_record("channelOutputMute", "bool", False, STRIP1))

    def pushes(sub):
        out = []
        while (e := sub.get(0.1)) is not None:
            if isinstance(e, desk_live.Push):
                out.append(e)
        return out

    got = pushes(window)
    check("a subscriber asking for our writes hears one, marked ours",
          [(p.name, p.value, p.was, p.ours) for p in got],
          [("channelOutputMute", False, True, True)])
    check("one that did not ask hears nothing of it", pushes(others), [])
    follower.stop()
    window.close()
    others.close()
    desk_live.stop()

sys.exit(_testkit.finish())
