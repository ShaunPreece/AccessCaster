"""Offline checks for CallMe: the answer record, the parsing, the wording and what the Watcher says.

NO DEVICE NEEDED. Every fact pinned here holds on a Pro II with a browser guest, and
re-establishing one costs a real call and a minute of the CallMe plan. The live driving is
DevTools/probe_callme_live.py.

    python DevTools/test_callme.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
import callme
import desk_live
import desk_model
from _testkit import check, decode_record
from fake_model import node

CTL = [664]
SLOTS = [[659], [660], [661]]


DETAILS = [('{"id":5,"calltype":"rode","peeruri":"rode:x","peername":"Shaun"}', "str"),
           (True, "bool")]
CLEARED = [("", "str"), (False, "bool")]

# The fake desk the Watcher answers from: three call slots, then the control node, on a Lite plan.
DESK_SLOTS = [[0], [1], [2]]
DESK_CTL = [3]
KIND = {bool: "bool", int: "int", float: "float", str: "str", list: "pair"}


def watcher():
    """A Watcher over a fake desk in the desk model, nothing ringing and hosting on code AB12CD,
    recording what it says."""
    desk_model._reset()
    slot = {"sipSlotCallState": "Idle", "sipSlotCallAddress": "", "sipSlotCallUUID": "",
            "sipSlotCallIsHost": False, "sipSlotCallBitrate": 0.0, "sipSlotCallPacketLoss": 0.0}
    ctl = {"sipIncomingCallAccept": "", "sipIncomingCallDetails": CLEARED,
           "sipCallHostingEnabled": True, "sipRodeCode": "AB12CD",
           "sipCallHostingToggleEnabled": True, "sipRemainingWebCalltime": 300,
           "sipLicenceMode": 0}
    blob = node("Rodecaster", {}, [node("SIPSLOT", slot) for _ in DESK_SLOTS]
                + [node("SIPCALLING", ctl)])
    desk_model.dump_finished(desk_model.dump_starting(), blob)
    said = []
    return callme.Watcher(on_say=said.append), said


def hear(w, path, name, value):
    """One push, as the listener delivers it: applied to the model first, then heard."""
    kind = KIND[type(value)]
    was = desk_model.apply_push(path, name, value, kind)
    w.apply(desk_live.Push(tuple(path), name, value, kind, None, was))
DEFAULT_NAME = "RÃ\u0098DE-CallMe"      # "RØDE-CallMe" as the latin-1 decoder hands it over

print("the answer record")
rec = callme.build_answer(5, 0, CTL)
path, name, value, kind, end = decode_record(rec)
check("addressed at the control node", path, CTL)
check("names the accept property", name, "sipIncomingCallAccept")
# THE BOOL IS FALSE. With True the call sets up and its audio never opens.
check("a composite of the JSON and a FALSE bool", value,
      [('{"id":5,"sip_slot":0}', "str"), (False, "bool")])
check("byte form matches the spec", rec[5:5 + end].hex(),
      "010101029802" + b"sipIncomingCallAccept\x00".hex()
      + "011d08011705" + b'{"id":5,"sip_slot":0}\x00'.hex() + "010103")
_p, _n, value, _k, _e = decode_record(callme.build_answer(5, -1, CTL))
check("decline is slot -1", value[0][0], '{"id":5,"sip_slot":-1}')

print("the hang-up records")
recs = callme.build_hangup(SLOTS[0])
vals = []
for r in recs:
    path, name, value, _k, _e = decode_record(r)
    vals.append((path, name, value))
# AN EDGE: the first bool has to RISE, so a single (True, False) does nothing from rest.
check("two writes, lowering then raising the first bool", [v[2] for v in vals],
      [[(False, "bool"), (False, "bool")], [(True, "bool"), (False, "bool")]])
check("both on the call's slot", {(tuple(v[0]), v[1]) for v in vals},
      {(tuple(SLOTS[0]), "sipSlotCallDisconnect")})

print("dialling")
dial = []
for rec in callme.build_dial("AB12CD", 0, CTL):
    dial.append(decode_record(rec)[:3])
check("both records go to the control node's outgoing property", {(tuple(d[0]), d[1]) for d in dial},
      {(tuple(CTL), "sipOutgoingCallDetails")})
# THE DESK ACTS ONLY ON A CHANGE, so the property is cleared first or a redial does nothing.
check("cleared first, to its factory empty string", dial[0][2], "")
check("then the touchscreen's template, with a FALSE bool", dial[1][2],
      [('{"dest":"rode:AB12CD","sip_slot":0}', "str"), (False, "bool")])
for typed, want in [("ab12cd", "AB12CD"), (" AB 12 CD ", "AB12CD"), ("rode:AB12CD", "AB12CD"),
                    ("https://callme.rode.com/s?c=AB12CD", "AB12CD"), ("bad-code", ""), ("", "")]:
    check(f"code from {typed!r}", callme.normalise_code(typed), want)
check("a dialled desk is spoken as its code", callme.peer_name("rode:AB12CD"),
      "RODECaster A B 1 2 C D")
check("a guest keeps their own name", callme.peer_name("Shaun"), "Shaun")
check("declined, in words", callme.end_reason("486 Rejected"), "they declined the call")
check("unknown code, in words", callme.end_reason("404 Not Found"), "no RODECaster has that code")
check("a line opening with a name is capitalised", callme.sentence("a guest is calling."),
      "A guest is calling.")
w, said = watcher()
before = w.snapshot()["slots"][0]["ended"]
hear(w, DESK_SLOTS[0], "sipSlotCallAddress", "rode:ZZ9QQ9")
hear(w, DESK_SLOTS[0], "sipSlotCallState", '{"state":"Connecting","reason":""}')
hear(w, DESK_SLOTS[0], "sipSlotCallState", '{"state":"Idle","reason":"404 Not Found"}')
check("each ending bumps the slot's count", w.snapshot()["slots"][0]["ended"], before + 1)

print("parsing")
check("a ringing call", callme.parse_call(DETAILS), {"id": 5, "name": "Shaun", "type": "rode"})
check("nothing ringing", callme.parse_call(CLEARED), None)
check("default guest name re-decoded", callme.utf8(DEFAULT_NAME), "RØDE-CallMe")
check("a slot that never had a call", callme.parse_slot_state("Idle"), ("Idle", ""))
check("a slot after one", callme.parse_slot_state('{"state":"Idle","reason":"Remote drop"}'),
      ("Idle", "Remote drop"))
check("renewal date", callme.renewal_date("October 3, 2026"), "3rd October 2026")
check("renewal date, 11th", callme.renewal_date("November 11, 2026"), "11th November 2026")
check("renewal date without the year", callme.renewal_date("October 3, 2026", year=False),
      "3rd October")
check("code spelt out", callme.spell("AB12CD"), "A B 1 2 C D")
check("minutes", callme.describe_minutes(596), "9 hours 56 minutes")
check("tier 0", callme.tier_name(0), "Lite")
check("an unmeasured tier is not guessed", callme.tier_name(3), "a paid plan (mode 3)")
check("the plan, one fact to a line",
      callme.plan_lines({"tier": "Lite", "minutes_left": 416, "web_minutes_left": 295,
                         "renews": "3rd October"}),
      [("CallMe plan", "Lite"), ("Desk to desk minutes remaining", "6 hours 56 minutes"),
       ("Browser minutes remaining", "4 hours 55 minutes"), ("Plan resets", "3rd October")])

print("a call answered on the touchscreen")
w, said = watcher()
hear(w, DESK_CTL, "sipIncomingCallDetails", DETAILS)
check("ringing is announced", said, ["CallMe call from Shaun."])
hear(w, DESK_CTL, "sipIncomingCallAccept", [('{"id":5,"sip_slot":0}', "str"), (False, "bool")])
hear(w, DESK_CTL, "sipIncomingCallDetails", CLEARED)
hear(w, DESK_SLOTS[0], "sipSlotCallAddress", "Shaun")
hear(w, DESK_SLOTS[0], "sipSlotCallState", '{"state":"Connecting","reason":""}')
hear(w, DESK_SLOTS[0], "sipSlotCallState", '{"state":"Active","reason":""}')
check("answered: not 'missed', then connected", said[1:], ["Connected to Shaun."])
hear(w, DESK_SLOTS[0], "sipSlotCallBitrate", 20457.48)
hear(w, DESK_SLOTS[0], "sipSlotCallPacketLoss", 0.0)
check("quality is never spoken", said[1:], ["Connected to Shaun."])
check("quality is kept", w.snapshot()["slots"][0]["bitrate_kbps"], 20)
hear(w, DESK_SLOTS[0], "sipSlotCallState", '{"state":"Idle","reason":"Remote drop"}')
check("the end and why", said[-1], "Call with Shaun ended: the guest hung up.")
check("quality cleared after", w.snapshot()["slots"][0]["bitrate_kbps"], None)

print("a call answered from this app")
w, said = watcher()
hear(w, DESK_CTL, "sipIncomingCallDetails", DETAILS)
w.note_handled(5)
w.quiet_connect(0, True)
hear(w, DESK_CTL, "sipIncomingCallDetails", CLEARED)
hear(w, DESK_SLOTS[0], "sipSlotCallAddress", "Shaun")
hear(w, DESK_SLOTS[0], "sipSlotCallState", '{"state":"Active","reason":""}')
check("the reply says connected, the watcher does not", said, ["CallMe call from Shaun."])

print("a call hung up from this app")
w, said = watcher()
hear(w, DESK_SLOTS[0], "sipSlotCallAddress", "Shaun")
hear(w, DESK_SLOTS[0], "sipSlotCallState", '{"state":"Active","reason":""}')
said.clear()
w.quiet_end(0, True)
hear(w, DESK_SLOTS[0], "sipSlotCallState", '{"state":"Idle","reason":"Local drop"}')
check("the reply says hung up, the watcher does not", said, [])
hear(w, DESK_SLOTS[0], "sipSlotCallState", '{"state":"Active","reason":""}')
hear(w, DESK_SLOTS[0], "sipSlotCallState", '{"state":"Idle","reason":"Local drop"}')
check("a hang-up on the desk is said in plain words", said[-1],
      "Call with Shaun ended: you hung up.")

print("the call area")
idle = {"incoming": None, "slots": [{"slot": 1, "state": "Idle", "reason": ""}]}
check("nothing happening", callme.call_now(idle), "No call.")
check("after an ordinary hang-up, nothing more",
      callme.call_now({"incoming": None, "slots": [{"slot": 1, "state": "Idle",
                                                    "reason": "Remote drop"}]}),
      "No call.")
check("after a call that went wrong, why it ended",
      callme.call_now({"incoming": None, "slots": [{"slot": 1, "state": "Idle",
                                                    "reason": "486 Rejected"}]}),
      "No call. The last one ended because they declined the call.")
ringing = {"incoming": {"id": 5, "name": "a guest"}, "slots": idle["slots"]}
check("ringing", callme.call_now(ringing), "A guest is calling.")
check("the second button declines a ringing call", callme.end_label(ringing), "Decline")
check("and hangs up otherwise", callme.end_label(idle), "Hang up")
out = {"incoming": None, "slots": [{"slot": 1, "state": "Connecting", "incoming": False,
                                    "peer": "RODECaster A B 1 2 C D"}]}
check("a call placed here is 'calling'", callme.call_now(out), "Calling RODECaster A B 1 2 C D.")
inn = {"incoming": None, "slots": [{"slot": 1, "state": "Connecting", "incoming": True,
                                    "peer": "Shaun"}]}
check("a call answered here is 'connecting'", callme.call_now(inn), "Connecting to Shaun.")
live = {"incoming": None, "slots": [{"slot": 1, "state": "Active", "peer": "Shaun",
                                     "bitrate_kbps": 23, "packet_loss": 0.0, "jitter_ms": 1.3}]}
check("connected", callme.call_now(live), "Talking to Shaun.")
check("the details, one to a line", callme.call_details(live),
      [("Bitrate", "23 kilobits a second"), ("Audio lost", "none"), ("Jitter", "1.3 milliseconds")])
check("no details with no call", callme.call_details(idle), [])

print("the one-hour limit")
w, said = watcher()
hear(w, DESK_SLOTS[0], "sipSlotCallAddress", "Shaun")
hear(w, DESK_SLOTS[0], "sipSlotCallState", '{"state":"Active","reason":""}')
said.clear()
hear(w, DESK_SLOTS[0], "sipSlotCallState", '{"state":"Expiring","reason":""}')
check("five minutes' warning is spoken", said,
      ["Call with Shaun ends in five minutes unless you extend it."])
hear(w, DESK_SLOTS[0], "sipSlotCallState", '{"state":"Active","reason":""}')
check("an extension on the desk is spoken", said[-1], "Call with Shaun extended.")
hear(w, DESK_SLOTS[0], "sipSlotCallState", '{"state":"Expiring","reason":""}')
ext = []
for r in callme.build_extend(SLOTS[0]):
    path, name, value, _k, _e = decode_record(r)
    ext.append((tuple(path), name, value))
check("extend lowers the pair, then raises both, as the touchscreen leaves it",
      [e[2] for e in ext],
      [[(False, "bool"), (False, "bool")], [(True, "bool"), (True, "bool")]])
check("both on the call's slot", {e[:2] for e in ext}, {(tuple(SLOTS[0]), "sipSlotCallExtend")})
auto, auto_said = watcher()
asked = []
auto._on_expiring = lambda i: asked.append(i) or True
hear(auto, DESK_SLOTS[0], "sipSlotCallAddress", "Shaun")
hear(auto, DESK_SLOTS[0], "sipSlotCallState", '{"state":"Active","reason":""}')
auto_said.clear()
hear(auto, DESK_SLOTS[0], "sipSlotCallState", '{"state":"Expiring","reason":""}')
check("auto-extend is asked with the slot", asked, [0])
check("and the warning goes unsaid while it extends", auto_said, [])
check("an expiring call is still connected", w.snapshot()["slots"][0]["state"] in callme.CONNECTED,
      True)
check("the call line says so",
      callme.call_now({"incoming": None, "slots": [{"slot": 1, "state": "Expiring",
                                                    "peer": "Shaun"}]}),
      "Talking to Shaun, ending within five minutes.")
hear(w, DESK_SLOTS[0], "sipSlotCallState", '{"state":"Idle","reason":"Idle timeout"}')
check("the end at the hour, in words", said[-1],
      "Call with Shaun ended: it reached the plan's one-hour limit.")

print("calls nobody took")
w, said = watcher()
hear(w, DESK_CTL, "sipIncomingCallDetails", DETAILS)
hear(w, DESK_CTL, "sipIncomingCallDetails", CLEARED)
check("a ring that runs out is missed", said[-1], "Missed CallMe call from Shaun.")
check("and the page's line keeps it afterwards", callme.call_now(w.snapshot()),
      "No call. You missed a call from Shaun.")
hear(w, DESK_CTL, "sipIncomingCallDetails", DETAILS)
check("until the next call rings", w.snapshot()["missed"], None)
w, said = watcher()
hear(w, DESK_CTL, "sipIncomingCallDetails", DETAILS)
hear(w, DESK_CTL, "sipIncomingCallAccept", [('{"id":5,"sip_slot":-1}', "str"), (True, "bool")])
hear(w, DESK_CTL, "sipIncomingCallDetails", CLEARED)
check("declined on the desk", said[1:], ["Declined the call from Shaun on the desk."])

print("the audio never opening")
w, said = watcher()
hear(w, DESK_SLOTS[0], "sipSlotCallAddress", "Shaun")
hear(w, DESK_SLOTS[0], "sipSlotCallState", '{"state":"Connecting","reason":""}')
hear(w, DESK_SLOTS[0], "sipSlotCallState", '{"state":"Idle","reason":"rtp stream error"}')
check("said as a failed connection", said,
      ["Call with Shaun ended: the call's audio could not connect."])

print("hosting and the plan")
w, said = watcher()
hear(w, DESK_CTL, "sipRodeCode", "ABC123")
check("a new code updates the link", w.snapshot()["link"], callme.GUEST_LINK + "ABC123")
hear(w, DESK_CTL, "sipCallHostingToggleEnabled", False)
check("the switch locks during a call", w.snapshot()["hosting_locked"], True)
v = w.snapshot()["version"]
hear(w, DESK_CTL, "sipRemainingWebCalltime", 295)
check("minutes follow the model, though the Watcher does not narrate them",
      (w.snapshot()["web_minutes_left"], w.snapshot()["version"]), (295, v + 1))
v = w.snapshot()["version"]
hear(w, DESK_SLOTS[1], "sipSlotCallState", '{"state":"Active","reason":""}')
check("a slot the plan does not have is ignored", (w.snapshot()["version"], said[-1:]), (v, []))
desk_model._reset()
unseeded_said = []
hear(callme.Watcher(on_say=unseeded_said.append), DESK_CTL, "sipIncomingCallDetails", DETAILS)
check("nothing is spoken before the model holds CallMe", unseeded_said, [])

print("\nCALL LENGTH")
w, said = watcher()
hear(w, DESK_SLOTS[0], "sipSlotCallUUID", "abc")
hear(w, DESK_SLOTS[0], "sipSlotCallState", '{"state":"Connecting","reason":""}')
hear(w, DESK_SLOTS[0], "sipSlotCallState", '{"state":"Active","reason":""}')
s0 = w.snapshot()["slots"][0]
check("a call seen connecting is timed exactly", (s0["since"] is not None, s0["since_exact"]),
      (True, True))
check("its length leads the details",
      callme.call_details(w.snapshot(), now=s0["since"] + 3900)[0], ("Call length", "1 hour 5 minutes"))
check("under a minute", callme.describe_length(30), "less than a minute")
check("a call already up when the app started says 'at least'",
      callme.describe_length(600, exact=False), "at least 10 minutes")
hear(w, DESK_SLOTS[0], "sipSlotCallState", '{"state":"Expiring","reason":""}')
hear(w, DESK_SLOTS[0], "sipSlotCallState", '{"state":"Active","reason":""}')
check("an extension does not restart the count", w.snapshot()["slots"][0]["since"], s0["since"])

print("\nCONTACTS")
book, msg = callme.save_contact([], " Alex ", "https://callme.rode.com/s?c=xy34zw")
check("a guest link is saved as its code", book, [{"name": "Alex", "code": "XY34ZW"}])
check("saving says so, code spelt", msg, "Saved Alex, code X Y 3 4 Z W.")
check("a duplicate code is refused", callme.save_contact(book, "Al", "XY34ZW")[1],
      "That code is already saved as Alex.")
check("a duplicate name is refused", callme.save_contact(book, "alex", "ABC123")[1],
      "You already have a contact called Alex.")
check("a name is required", callme.save_contact(book, " ", "ABC123")[0], None)
book2, _ = callme.save_contact(book, "Alex", "XYZ789", was="XY34ZW")
check("an edit replaces the entry, not adds one", book2, [{"name": "Alex", "code": "XYZ789"}])
check("a malformed stored entry is dropped",
      callme.clean_contacts([{"name": "A", "code": "!!"}, "junk", {"name": "B", "code": "b1"}]),
      [{"name": "B", "code": "B1"}])
callme.set_contacts(book)
check("a saved code is spoken by name", callme.peer_name("rode:xy34zw"), "Alex")
check("an unsaved code is still spelt", callme.peer_name("rode:AB12CD"), "RODECaster A B 1 2 C D")
check("deleting", callme.delete_contact(book, "XY34ZW"), ([], "Deleted Alex."))
check("deleting what is not there", callme.delete_contact(book, "ZZZ"),
      (None, "That contact is not saved."))
callme.set_contacts([])

print("a new call rings once, and only once")
w, said = watcher()
rang = []
w._on_ring = lambda call, text: rang.append((call["id"], text)) or True
hear(w, DESK_CTL, "sipIncomingCallDetails", DETAILS)
hear(w, DESK_CTL, "sipIncomingCallDetails", DETAILS)
check("the ring hook gets the call and its sentence, once", rang, [(5, "CallMe call from Shaun.")])
check("and the Watcher does not say it as well, or the first ring would cut it off", said, [])
hear(w, DESK_CTL, "sipIncomingCallDetails", CLEARED)
check("a clear does not ring", len(rang), 1)
w, said = watcher()
w._on_ring = lambda call, text: False
hear(w, DESK_CTL, "sipIncomingCallDetails", DETAILS)
check("with the ringtone off, the Watcher says it itself", said, ["CallMe call from Shaun."])

print("ready for calls")
READY = {"online": True, "hosting": True}
check("all in place: nothing to fix",
      [r["key"] for r in callme.readiness(True, True) if not r["ok"]], [])
check("offline and no fader: both, most basic first",
      [r["key"] for r in callme.readiness(False, False) if not r["ok"]], ["online", "fader"])
check("not knowing whether it is online is not taken as online",
      callme.readiness(None, True)[0]["ok"], False)
check("not knowing whether it is on a fader is not taken as on one",
      callme.readiness(True, None)[1]["ok"], False)
check("offline outranks hosting in the status line",
      callme.status_sentence(dict(READY, online=False, slots=[], incoming=None)).endswith(
          "The desk is offline, so no call can connect."), True)

print()
sys.exit(_testkit.finish())
