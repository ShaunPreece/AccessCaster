"""Offline checks for the recorder: parsing, wording, storage labels and the elapsed-time count.

NO DEVICE NEEDED, and that is the point - every fact these pin is expensive to re-establish on a
desk, since it needs a drive in the unit and a real take. This is what stops the parsing
regressing.

    python DevTools/test_recorder.py
"""
from __future__ import annotations

import sys
import time

from datetime import UTC, datetime

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
import desk_live
import desk_model
import rcp_transfer
import recorder
from _testkit import addr, check, check_in, hold_desk
from wording import spoken_datetime

# A recordingContent string as the unit sends it. The trailing six fields are unmapped.
CONTENT = ("Recording 4|94486d8a68da47c7a516e1400a3279dc|"
           "/Application/ssd-mount/RODECaster/4 - 28 Aug 2026/Stereo Mix.wav|"
           "1787914947088|10.8173|0|0|1|0|0|1|1")

# The report-04 pushes the desk sends when a recording starts (state 2) and stops (0).
PUSH_RECORDING = bytes.fromhex(
    "041800000001010101037265636f7264537461746500010501020000000000000000000000000000")
PUSH_STOPPED = bytes.fromhex(
    "041800000001010101037265636f7264537461746500010501000000000000000000000000000000")


print("\nRECORDING RECORDS")
r = recorder.parse_recording(4, CONTENT)
check("name", r["name"], "Recording 4")
check("seconds", r["seconds"], 10.8)
check("duration in words", r["duration"], "11 seconds")
# THE TIMESTAMP RULE. The field is an ordinary UTC epoch, so it is shown in THIS COMPUTER'S LOCAL
# time, which is the wall clock the person remembers; rendered as UTC, a recording reads an hour
# early under UTC+1 (recorder.parse_recording). The expectation is computed rather than written
# out, because a literal would only pass in one timezone.
stamp_ms = 1787914947088
local = datetime.fromtimestamp(stamp_ms / 1000.0)
check("timestamp is shown in local time", r["when"], spoken_datetime(local))
utc_render = spoken_datetime(datetime.fromtimestamp(stamp_ms / 1000.0, tz=UTC))
if utc_render != r["when"]:
    check("and it is NOT the raw UTC rendering", r["when"] != utc_render, True)
else:
    print("  note this machine is on UTC, so local and UTC read the same; which of the two is "
          "shown cannot be pinned here")
# Firmware 1.7.6 inserts an empty field after the path, which shifts the timestamp and duration.
CONTENT_176 = ("Recording 27|c9f82780f0fb47e694b408e95ed52616|"
               "/Application/ssd-mount/RODECaster/27 - 4 Oct 2026/Stereo Mix.wav||"
               "1791121655376|9.42433|0|0|1|0|0|1|1|0")
r = recorder.parse_recording(27, CONTENT_176)
check("1.7.6 seconds", r["seconds"], 9.4)
check("1.7.6 timestamp", r["when"],
      spoken_datetime(datetime.fromtimestamp(1791121655376 / 1000.0)))
check("a malformed record does not raise",
      recorder.parse_recording(1, "nonsense")["seconds"], None)
check("and still says how long it is", recorder.parse_recording(1, "nonsense")["duration"],
      "length unknown")

print("\nLENGTHS IN WORDS (never punctuation: a reader says '0:07.3' as literal colons)")
check("under a minute", recorder.describe_duration(7), "7 seconds")
check("exactly one", recorder.describe_duration(1), "1 second")
check("whole minutes lose the seconds", recorder.describe_duration(120), "2 minutes")
check("minutes and seconds", recorder.describe_duration(125), "2 minutes 5 seconds")
check("hours", recorder.describe_duration(3600 * 2 + 60), "2 hours 1 minute")
check("unknown is said, not shown as zero", recorder.describe_duration(None), "length unknown")

print("\nSTORAGE SLOT NAMES (slot 1 is the USB port, not a second card slot)")
check("slot 0 is the card slot", recorder.volume_label(0), "Memory card slot")
check("slot 1 is the USB port, NOT 'Card slot 2'", recorder.volume_label(1), "USB drive")
check("slot 2 is the pad volume", recorder.volume_label(2), "Internal storage (SMART pads)")
check("an unexpected extra slot is numbered, not guessed",
      recorder.volume_label(7), "Storage 8")
# The spoken form exists so a sentence does not lower-case an initialism into "uzb drive".
check("spoken form keeps the initialism", recorder.volume_spoken(1), "USB drive")
check("spoken form of the card", recorder.volume_spoken(0), "memory card")

print("\nWHERE A RECORDING GOES")
vols = [
    {"index": 0, "label": "Memory card slot", "spoken": "memory card", "inserted": False,
     "mounted": False, "can_record": True, "free_bytes": 0, "low": False},
    {"index": 1, "label": "USB drive", "spoken": "USB drive", "inserted": True,
     "mounted": True, "can_record": True, "free_bytes": 123_000_000_000, "low": False},
    {"index": 2, "label": "Internal storage (SMART pads)", "spoken": "internal storage",
     "inserted": True, "mounted": True, "can_record": False, "free_bytes": 5_000_000_000,
     "low": False},
]
check("an EMPTY slot is not a destination, even though the desk flags it as eligible",
      recorder.destination(vols)["spoken"], "USB drive")
check("the pad volume is never a destination",
      recorder.destination([vols[2]]), None)
check("nothing in the desk means nowhere to record", recorder.destination([]), None)

print("\nHOW LONG WILL IT FIT (the figure a bar gives a sighted operator)")
check("no destination, no claim",
      recorder.room_left(b"", None)["seconds"], None)

print("\nDECODING THE PUSH (the desk's own packets)")


def state_of(frame):
    push = desk_live.decode(frame)
    return recorder._state_of(push) if push is not None else None


check("a start push reads as recording", state_of(PUSH_RECORDING), 2)
check("a stop push reads as ready", state_of(PUSH_STOPPED), 0)
check("a report that is not 04 is ignored",
      state_of(b"\x03" + PUSH_RECORDING[1:]), None)
check("an unrelated packet is ignored",
      state_of(b"\x04\x00\x00\x00\x00selectedBank\x00\x01\x05\x01\x02\x00\x00\x00"),
      None)
# The SELECTOR is checked as well as the name, so a same-named field in another block cannot be
# read as the recorder's.
check("the wrong selector is refused",
      state_of(PUSH_RECORDING[:9] + b"\x0f" + PUSH_RECORDING[10:]), None)

print("\nELAPSED TIME (counted here, because the desk will not: recordTimeMs stays 0)")


def pushed(watcher, state):
    """The desk pushing `state`, as the listener hands it on: the desk model first."""
    path = tuple(addr(rcp_transfer.RECORDER))
    was = desk_model.apply_push(path, "recordState", state, "int")
    watcher._push(desk_live.Push(path, "recordState", state, "int", rcp_transfer.RECORDER, was))


hold_desk(without=("recordState",))
said = []
w = recorder.Watcher(on_change=lambda was, now: said.append(recorder.STATE_SPOKEN.get(now)))
pushed(w, recorder.READY)
pushed(w, recorder.RECORDING)
time.sleep(0.3)
check("a recording we started IS timed", w.snapshot()["elapsed_seconds"] is not None, True)
pushed(w, recorder.PAUSED)
paused_at = w.snapshot()["elapsed_seconds"]
time.sleep(0.4)
check("a PAUSE stops the clock, exactly as it stops the recording",
      w.snapshot()["elapsed_seconds"], paused_at)
pushed(w, recorder.RECORDING)
time.sleep(0.2)
check("resuming carries on from where it paused, it does not restart",
      w.snapshot()["elapsed_seconds"] > paused_at, True)
pushed(w, recorder.READY)
check("stopping clears the count", w.snapshot()["elapsed_seconds"], None)
check("every transition was spoken, and in order",
      said, ["Recording stopped", "Recording started", "Recording paused",
             "Recording started", "Recording stopped"])

print("\nAN ELAPSED TIME WE CANNOT HONESTLY GIVE")
hold_desk()
w2 = recorder.Watcher()
# The app started while the unit was ALREADY recording: the desk model learned the state from a
# dump, so we never saw it begin. A number here would be wrong, so there is no number.
desk_model.apply_push(tuple(addr(rcp_transfer.RECORDER)), "recordState", recorder.RECORDING, "int")
check("no start seen, no elapsed claimed", w2.snapshot()["elapsed_seconds"], None)
check("but the state is still reported", w2.snapshot()["state_label"], "Recording")
w2.note_state(recorder.RECORDING, timed=False)
check("a repeated read does not reset anything", w2.snapshot()["state"], recorder.RECORDING)
pushed(w2, recorder.RECORDING)
check("a repeated push does not claim the start either", w2.snapshot()["elapsed_seconds"], None)

print("\nA SNAPSHOT BETWEEN THE MODEL TAKING A PUSH AND THE FOLLOWER HANDLING IT")
hold_desk()
w3 = recorder.Watcher()
pushed(w3, recorder.READY)
path = tuple(addr(rcp_transfer.RECORDER))
was = desk_model.apply_push(path, "recordState", recorder.RECORDING, "int")
w3.snapshot()
w3._push(desk_live.Push(path, "recordState", recorder.RECORDING, "int", rcp_transfer.RECORDER, was))
check("the push still times the take", w3.snapshot()["elapsed_seconds"] is not None, True)

print("\nCOMMANDS")
check("start and resume are the same command to the desk",
      recorder.COMMANDS["start"], recorder.COMMANDS["resume"])
check("stop is 0", recorder.COMMANDS["stop"], 0)
check("pause is 1", recorder.COMMANDS["pause"], 1)
check("every state has words", sorted(recorder.STATE_LABEL), [0, 1, 2, 3])
check("every state has a spoken form", sorted(recorder.STATE_SPOKEN), [0, 1, 2, 3])
check_in("no destination is EXPLAINED, not just named",
         "nowhere", recorder.STATE_SPOKEN[recorder.NO_DESTINATION])

print("\nPLUGGING A DRIVE IN IS NOT A RECORDING STOPPING")
# THE WORDING NEEDS BOTH STATES. Keyed on the NEW state alone, inserting a pen drive
# (NO_DESTINATION -> READY) announces READY's own word for itself, "Recording stopped".
S = recorder.spoken_change
check("putting a card or drive IN says recording is available",
      S(recorder.NO_DESTINATION, recorder.READY), "Recording now available")
check("and it never says anything stopped",
      "stopped" in S(recorder.NO_DESTINATION, recorder.READY).lower(), False)
check("taking it out while idle says it is no longer available, and names what left",
      S(recorder.READY, recorder.NO_DESTINATION, "USB drive"),
      "Recording no longer available: the USB drive was removed")
check("with no name known it still explains itself rather than guessing a medium",
      S(recorder.READY, recorder.NO_DESTINATION),
      "Recording no longer available: the desk has nowhere to record to")
# The one case where a plug really does stop a recording. It must NOT be softened into the
# "no longer available" wording: the operator has just lost the end of a take.
check("pulling it MID-TAKE is a stop, and says why",
      S(recorder.RECORDING, recorder.NO_DESTINATION, "memory card"),
      "Recording stopped: the memory card was removed")
check_in("a mid-take pull with no name still reports a stop",
         "Recording stopped", S(recorder.RECORDING, recorder.NO_DESTINATION))
check("a genuine stop is still a plain stop",
      S(recorder.RECORDING, recorder.READY), "Recording stopped")
check("an insertion never names the drive that was pulled out before it",
      S(recorder.NO_DESTINATION, recorder.READY, "USB drive"), "Recording now available")
check("resuming from a pause is not a fresh start",
      S(recorder.PAUSED, recorder.RECORDING), "Recording again")
check("starting from ready is", S(recorder.READY, recorder.RECORDING), "Recording started")
# `was` of None is the FIRST state the app ever sees. Nothing moved, so nothing is said - this is
# what stops the app greeting you with "Recording stopped" on startup.
check("the first state we ever see is not a change", S(None, recorder.READY), "")
check("nor is one that did not move", S(recorder.READY, recorder.READY), "")
check("an unreadable state says nothing", S(recorder.READY, None), "")

print("\nTWO MULTITRACKS, AND NEITHER LABEL MAY BE MISTAKEN FOR THE OTHER")
# RODE calls both of them "Multitrack", so labels that collide make the two indistinguishable.
check("the recorder offers three modes", sorted(recorder.MULTITRACK_LABEL), [0, 1, 2])
check("so does the USB stream", sorted(recorder.USB_MULTITRACK_LABEL), [0, 1, 2])
check("no label appears in both lists",
      sorted(set(recorder.MULTITRACK_LABEL.values())
             & set(recorder.USB_MULTITRACK_LABEL.values())), [])
check("every mode says what you get, not just pre or post",
      [v for v in list(recorder.MULTITRACK_LABEL.values())
       + list(recorder.USB_MULTITRACK_LABEL.values()) if len(v.split()) < 2], [])

print("\nTHE RAW SWITCH SAYS BOTH HALVES OF WHAT IT DOES")
# RODE's guide: multitrack only, and the mix track always keeps its processing. A hint that gave
# only the first half would leave someone expecting a raw mixed track, which they never get.
check_in("it is separate tracks only", "Separate tracks only", recorder.BYPASS_HINT)
check_in("and the mixed track is called out", "mixed track", recorder.BYPASS_HINT)
check_in("with multitrack off, it says nothing happens yet",
         "does nothing", recorder.BYPASS_NEEDS_MULTITRACK)

print("\nA TRANSPORT PRESS WAITS FOR THE DESK'S ANSWER, NOT A FIXED TIME")
import recording  # noqa: E402


class Pushes:
    """A PushListener that hands back queued pushes, and times out once they run out."""

    def __init__(self, *pushes):
        self.pushes = list(pushes)
        self.timeouts = []

    def wait_for(self, name, timeout, match=None):
        self.timeouts.append(timeout)
        while self.pushes:
            pushed, value = self.pushes.pop(0)
            if pushed == name and (match is None or match(value)):
                return value
        return None


start = time.monotonic()
heard = recording._await_record(Pushes(("recordState", recorder.RECORDING)), recorder.RECORDING)
check("a start ends at the pushed state", sorted(heard), ["recordState"])
check("without waiting out the margin", time.monotonic() - start < 0.5, True)
listen = Pushes(("recordState", recorder.PAUSED), ("recordState", recorder.READY))
check("a stop is answered by READY, which is 0",
      sorted(recording._await_record(listen, recorder.READY)), ["recordState"])
check("and a state on the way is not the answer", listen.pushes, [])
listen = Pushes()
check("a press the desk ignores hears nothing",
      recording._await_record(listen, recorder.RECORDING), {})
check("and gives up within the margin", listen.timeouts, [recording._RECORD_ANSWER_S])

print("\nTHE CARD ON THIS COMPUTER: transferModeType 1 on SYSTEM, and 0 to take it back")
import devices  # noqa: E402
import fake_model  # noqa: E402
import folders  # noqa: E402
import layout_fixture as lf  # noqa: E402
import rcp_read  # noqa: E402
from _testkit import as_device, check_true, decode_record, on_layout, patched  # noqa: E402

check("handing the card over is transferModeType 1 on SYSTEM",
      decode_record(recorder.build_card_transfer(True))[:4],
      (addr(rcp_transfer.SYSTEM), "transferModeType", 1, "int"))
check("taking it back is 0 on the same node",
      decode_record(recorder.build_card_transfer(False))[:4],
      (addr(rcp_transfer.SYSTEM), "transferModeType", 0, "int"))
with on_layout(lf.PRO2_177_PID0092):
    rec = recorder.build_card_transfer(True)
check("on a desk whose SYSTEM is root child 0x0D, the record RODE's app sends, byte for byte",
      rec[5:5 + int.from_bytes(rec[1:5], "little")].hex(),
      "010101010d" + b"transferModeType\x00".hex() + "010501" + "01000000")


def system_dump(mode, stray=None):
    """A dump whose SYSTEM carries `mode`, behind a block carrying a stray copy when one is given."""
    ahead = [fake_model.node("AUDIO", {"transferModeType": stray})] if stray is not None else []
    return fake_model.node("Rodecaster", (), ahead + [
        fake_model.node("SYSTEM", {"transferModeType": mode})])


check("the mode is read on SYSTEM", recorder.transfer_mode(system_dump(1)), 1)
check_true("which is the card on the computer", recorder.card_out(system_dump(1)))
check_true("2 is the pad storage, not the card", not recorder.card_out(system_dump(2)))
check_true("a stray copy ahead of SYSTEM is not the mode", not recorder.card_out(system_dump(0, 1)))


class CardDesk:
    """As much of a desk, and of Windows, as handing the card over touches: the recorder, the card
    slot, the recordings, and the desk's drive, which holds a volume while the card is out."""

    def __init__(self, state=recorder.READY, mode=0, card=True, windows=True):
        self.state, self.mode, self.card, self.windows = state, mode, card, windows
        self.log = []                # what was done, in order

    def dump(self, **_asked):
        out = self.mode == recorder.CARD_TRANSFER
        slot = "64000000000|32000000000|1|1|1" if self.card and not out else "0|0|1|0|0"
        takes = [] if out else [fake_model.node("RECORDING", {"recordingUID": n,
                                                              "recordingContent": CONTENT})
                                for n in (1, 2)]
        return fake_model.node("Rodecaster", (), [
            fake_model.node("RECORDER", {
                "recordState": recorder.NO_DESTINATION if out or not self.card else self.state}),
            fake_model.node("SYSTEM", {"transferModeType": self.mode}, [
                fake_model.node("STORAGEVOLUME", {"storageVolumeState": slot,
                                                  "storageVolumeRecDestination": True,
                                                  "storageVolumeEject": False})]),
            fake_model.node("RECORDINGS", {"recordingTotalCount": len(takes)}, takes)])

    def send(self, record):
        _path, name, value, _kind, _end = decode_record(record)
        self.log.append((name, value))
        self.mode = value
        return len(record)

    def roots(self):
        return ["E:\\"] if self.mode == recorder.CARD_TRANSFER and self.windows else []

    def run(self, on, dev=devices.PRO2):
        with as_device(dev), patched(
                (rcp_read, {"read_model_stable": self.dump}),
                (rcp_transfer, {"send_record": self.send, "desk_volume_roots": self.roots,
                                "dismount_volume": lambda root: self.log.append(("dismount", root))}),
                (folders, {"open_folder_reply": lambda folder, _what: self.log.append(
                    ("explorer", folder)) or {"ok": True, "message": ""}}),
                (recording.os.path, {"isdir": lambda _path: True}),
                (recording.time, {"sleep": lambda _s: None}),
                (recording, {"_CARD_WAIT_S": 0.0})):
            return recording.action_card_to_computer(on)


desk = CardDesk()
reply = desk.run(True)
check_true("opening the card succeeds", reply["ok"], reply.get("message"))
check("one write, then File Explorer on its recordings", desk.log,
      [("transferModeType", 1), ("explorer", "E:\\RODECaster")])
check_in("the reply names the drive", "as drive E:", reply["message"])
check("the page is told it is open, and where", reply["recorder"]["card"],
      {"open": True, "drive": "E:", "note": ""})
recording._note_context(reply["recorder"])
check("the state sentence says why nothing can be recorded",
      recording._sentence(reply["recorder"]), recording.CARD_OUT_SENTENCE)
try:
    with patched((rcp_transfer, {"send_record": desk.send})):
        rcp_transfer.set_transfer_mode(True)
    refused = ""
except RuntimeError as exc:
    refused = str(exc)
check("pad transfer mode is refused while the card is out", refused, rcp_transfer.CARD_OUT_REFUSAL)
check("and sends nothing", desk.log[-1], ("explorer", "E:\\RODECaster"))
del desk.log[:]
check_in("opening it twice says it is open", "already open", desk.run(True)["message"])
check("and writes nothing", desk.log, [])

reply = desk.run(False)
check_true("giving it back succeeds", reply["ok"], reply.get("message"))
check("Windows lets go of the volume before the desk takes the media away", desk.log,
      [("dismount", "E:\\"), ("transferModeType", 0)])
check_in("the recordings are counted off the desk again", "2 recordings", reply["message"])
check("the page is told it is back", reply["recorder"]["card"]["open"], False)
check_true("and pad transfer mode is free again", not rcp_transfer.card_out())
del desk.log[:]
check_in("giving it back twice says where it is", "already in the desk",
         desk.run(False)["message"])
check("and writes nothing", desk.log, [])

for label, refusing, words in (
        ("a take running", CardDesk(state=recorder.RECORDING), "Stop the recording first"),
        ("a paused take", CardDesk(state=recorder.PAUSED), "Stop the recording first"),
        ("no card in the desk", CardDesk(card=False), "no memory card"),
        ("the pad storage holding transfer mode", CardDesk(mode=2), "SMART pad sounds")):
    reply = refusing.run(True)
    check_in(f"{label} refuses", words, reply["message"])
    check(f"{label} writes nothing", (reply["ok"], refusing.log), (False, []))

slow = CardDesk(windows=False)
reply = slow.run(True)
check_in("a card Windows never shows is a failure that says so", "did not show", reply["message"])
check("and the page still offers to give it back", (reply["ok"], reply["recorder"]["card"]["open"]),
      (False, True))
check("with no folder opened", slow.log, [("transferModeType", 1)])

duo = CardDesk()
reply = duo.run(True, devices.DUO)
check_in("a desk it is not confirmed on refuses by name", "RODECaster Duo", reply["message"])
check("and writes nothing", (reply["ok"], duo.log), (False, []))
rcp_transfer.note_card_out(False)

sys.exit(_testkit.finish())
