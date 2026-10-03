"""Offline checks for CallMe: the answer record, the parsing, the wording and what the Watcher says.

NO DEVICE NEEDED. Every fact pinned here came off a Pro II with a phone browser as the guest, and
re-establishing one costs a real call and a minute of the plan. The live driving is
DevTools/probe_callme_live.py.

    python DevTools/test_callme.py
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401  (puts Project/ on sys.path)

import callme
import model_tree

FAILS = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILS.append(f"{name}: got {got!r}, wanted {want!r}")
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")


CTL = [664]
SLOTS = [[659], [660], [661]]


def watcher(slots=1):
    """A Watcher seeded by hand, as `seed` would leave it, recording what it says."""
    said = []
    w = callme.Watcher(on_say=said.append)
    w._state = {"ok": True, "hosting": True, "hosting_locked": False, "code": "AB12CD",
                "link": callme.GUEST_LINK + "AB12CD", "incoming": None, "on_fader": True,
                "slots": [callme._slot_view(i + 1, {"sipSlotCallState": "Idle"})
                          for i in range(slots)]}
    w._ctl, w._slots = CTL, SLOTS
    return w, said


DETAILS = [('{"id":5,"calltype":"rode","peeruri":"rode:x","peername":"Shaun"}', "str"),
           (True, "bool")]
CLEARED = [("", "str"), (False, "bool")]
DEFAULT_NAME = "RÃ\u0098DE-CallMe"      # "RØDE-CallMe" as the latin-1 decoder hands it over

print("the answer record")
rec = callme.build_answer(5, 0, CTL)
n = int.from_bytes(rec[1:5], "little")
path, name, value, kind, _end = model_tree.parse_record(rec[5:5 + n])
check("addressed at the control node", path, CTL)
check("names the accept property", name, "sipIncomingCallAccept")
# THE BOOL IS FALSE. With True the call sets up and its audio never opens.
check("a composite of the JSON and a FALSE bool", value,
      [('{"id":5,"sip_slot":0}', "str"), (False, "bool")])
check("byte form matches the spec", rec[5:5 + n].hex(),
      "010101029802" + b"sipIncomingCallAccept\x00".hex()
      + "011d08011705" + b'{"id":5,"sip_slot":0}\x00'.hex() + "010103")
_p, _n, value, _k, _e = model_tree.parse_record(
    callme.build_answer(5, -1, CTL)[5:5 + n + 1])
check("decline is slot -1", value[0][0], '{"id":5,"sip_slot":-1}')

print("the hang-up records")
recs = callme.build_hangup(SLOTS[0])
vals = []
for r in recs:
    n = int.from_bytes(r[1:5], "little")
    path, name, value, _k, _e = model_tree.parse_record(r[5:5 + n])
    vals.append((path, name, value))
# AN EDGE: the first bool has to RISE, so a single (True, False) does nothing from rest.
check("two writes, lowering then raising the first bool", [v[2] for v in vals],
      [[(False, "bool"), (False, "bool")], [(True, "bool"), (False, "bool")]])
check("both on the call's slot", {(tuple(v[0]), v[1]) for v in vals},
      {(tuple(SLOTS[0]), "sipSlotCallDisconnect")})

print("dialling")
dial = []
for rec in callme.build_dial("AB12CD", 0, CTL):
    n = int.from_bytes(rec[1:5], "little")
    dial.append(model_tree.parse_record(rec[5:5 + n])[:3])
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
w.apply(SLOTS[0], "sipSlotCallAddress", "rode:ZZ9QQ9")
w.apply(SLOTS[0], "sipSlotCallState", '{"state":"Connecting","reason":""}')
w.apply(SLOTS[0], "sipSlotCallState", '{"state":"Idle","reason":"404 Not Found"}')
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
w.apply(CTL, "sipIncomingCallDetails", DETAILS)
check("ringing is announced", said, ["CallMe call from Shaun."])
w.apply(CTL, "sipIncomingCallAccept", [('{"id":5,"sip_slot":0}', "str"), (False, "bool")])
w.apply(CTL, "sipIncomingCallDetails", CLEARED)
w.apply(SLOTS[0], "sipSlotCallAddress", "Shaun")
w.apply(SLOTS[0], "sipSlotCallState", '{"state":"Connecting","reason":""}')
w.apply(SLOTS[0], "sipSlotCallState", '{"state":"Active","reason":""}')
check("answered: not 'missed', then connected", said[1:], ["Connected to Shaun."])
w.apply(SLOTS[0], "sipSlotCallBitrate", 20457.48)
w.apply(SLOTS[0], "sipSlotCallPacketLoss", 0.0)
check("quality is never spoken", said[1:], ["Connected to Shaun."])
check("quality is kept", w.snapshot()["slots"][0]["bitrate_kbps"], 20)
w.apply(SLOTS[0], "sipSlotCallState", '{"state":"Idle","reason":"Remote drop"}')
check("the end and why", said[-1], "Call with Shaun ended: the guest hung up.")
check("quality cleared after", w.snapshot()["slots"][0]["bitrate_kbps"], None)

print("a call answered from this app")
w, said = watcher()
w.apply(CTL, "sipIncomingCallDetails", DETAILS)
w.note_handled(5)
w.quiet_connect(0, True)
w.apply(CTL, "sipIncomingCallDetails", CLEARED)
w.apply(SLOTS[0], "sipSlotCallAddress", "Shaun")
w.apply(SLOTS[0], "sipSlotCallState", '{"state":"Active","reason":""}')
check("the reply says connected, the watcher does not", said, ["CallMe call from Shaun."])

print("a call hung up from this app")
w, said = watcher()
w.apply(SLOTS[0], "sipSlotCallAddress", "Shaun")
w.apply(SLOTS[0], "sipSlotCallState", '{"state":"Active","reason":""}')
said.clear()
w.quiet_end(0, True)
w.apply(SLOTS[0], "sipSlotCallState", '{"state":"Idle","reason":"Local drop"}')
check("the reply says hung up, the watcher does not", said, [])
w.apply(SLOTS[0], "sipSlotCallState", '{"state":"Active","reason":""}')
w.apply(SLOTS[0], "sipSlotCallState", '{"state":"Idle","reason":"Local drop"}')
check("a hang-up on the unit is said in plain words", said[-1],
      "Call with Shaun ended: you hung up.")

print("the call area")
idle = {"incoming": None, "slots": [{"slot": 1, "state": "Idle", "reason": ""}]}
check("nothing happening", callme.call_now(idle), "No call.")
check("after a call, why it ended",
      callme.call_now({"incoming": None, "slots": [{"slot": 1, "state": "Idle",
                                                    "reason": "Remote drop"}]}),
      "No call. The last one ended because the guest hung up.")
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
w.apply(SLOTS[0], "sipSlotCallAddress", "Shaun")
w.apply(SLOTS[0], "sipSlotCallState", '{"state":"Active","reason":""}')
said.clear()
w.apply(SLOTS[0], "sipSlotCallState", '{"state":"Expiring","reason":""}')
check("five minutes' warning is spoken", said,
      ["Call with Shaun ends in five minutes unless you extend it."])
w.apply(SLOTS[0], "sipSlotCallState", '{"state":"Active","reason":""}')
check("an extension on the unit is spoken", said[-1], "Call with Shaun extended.")
w.apply(SLOTS[0], "sipSlotCallState", '{"state":"Expiring","reason":""}')
ext = []
for r in callme.build_extend(SLOTS[0]):
    n = int.from_bytes(r[1:5], "little")
    path, name, value, _k, _e = model_tree.parse_record(r[5:5 + n])
    ext.append((tuple(path), name, value))
check("extend lowers the pair, then raises both, as the touchscreen leaves it",
      [e[2] for e in ext],
      [[(False, "bool"), (False, "bool")], [(True, "bool"), (True, "bool")]])
check("both on the call's slot", {e[:2] for e in ext}, {(tuple(SLOTS[0]), "sipSlotCallExtend")})
auto, auto_said = watcher()
asked = []
auto._on_expiring = lambda i: asked.append(i) or True
auto.apply(SLOTS[0], "sipSlotCallAddress", "Shaun")
auto.apply(SLOTS[0], "sipSlotCallState", '{"state":"Active","reason":""}')
auto_said.clear()
auto.apply(SLOTS[0], "sipSlotCallState", '{"state":"Expiring","reason":""}')
check("auto-extend is asked with the slot", asked, [0])
check("and the warning goes unsaid while it extends", auto_said, [])
check("an expiring call is still connected", w.snapshot()["slots"][0]["state"] in callme.CONNECTED,
      True)
check("and still up, so it can be hung up", callme.end_label({"incoming": None}), "Hang up")
check("the call line says so",
      callme.call_now({"incoming": None, "slots": [{"slot": 1, "state": "Expiring",
                                                    "peer": "Shaun"}]}),
      "Talking to Shaun, ending within five minutes.")
w.apply(SLOTS[0], "sipSlotCallState", '{"state":"Idle","reason":"Idle timeout"}')
check("the end at the hour, in words", said[-1],
      "Call with Shaun ended: it reached the plan's one-hour limit.")

print("calls nobody took")
w, said = watcher()
w.apply(CTL, "sipIncomingCallDetails", DETAILS)
w.apply(CTL, "sipIncomingCallDetails", CLEARED)
check("a ring that runs out is missed", said[-1], "Missed CallMe call from Shaun.")
w, said = watcher()
w.apply(CTL, "sipIncomingCallDetails", DETAILS)
w.apply(CTL, "sipIncomingCallAccept", [('{"id":5,"sip_slot":-1}', "str"), (True, "bool")])
w.apply(CTL, "sipIncomingCallDetails", CLEARED)
check("declined on the unit", said[1:], ["Declined the call from Shaun on the unit."])

print("the audio never opening")
w, said = watcher()
w.apply(SLOTS[0], "sipSlotCallAddress", "Shaun")
w.apply(SLOTS[0], "sipSlotCallState", '{"state":"Connecting","reason":""}')
w.apply(SLOTS[0], "sipSlotCallState", '{"state":"Idle","reason":"rtp stream error"}')
check("said as a failed connection", said,
      ["Call with Shaun ended: the call's audio could not connect."])

print("hosting and the plan")
w, said = watcher()
w.apply(CTL, "sipRodeCode", "ABC123")
check("a new code updates the link", w.snapshot()["link"], callme.GUEST_LINK + "ABC123")
w.apply(CTL, "sipCallHostingToggleEnabled", False)
check("the switch locks during a call", w.snapshot()["hosting_locked"], True)
w.apply(CTL, "sipRemainingWebCalltime", 295)
check("minutes follow the push", w.snapshot()["web_minutes_left"], 295)
v = w.version
w.apply(SLOTS[1], "sipSlotCallState", '{"state":"Active","reason":""}')
check("a slot the plan does not have is ignored", w.version, v)
unseeded_said = []
callme.Watcher(on_say=unseeded_said.append).apply(CTL, "sipIncomingCallDetails", DETAILS)
check("nothing is spoken before a seed", unseeded_said, [])

print("\nCALL LENGTH")
w, said = watcher()
w.apply(SLOTS[0], "sipSlotCallUUID", "abc")
w.apply(SLOTS[0], "sipSlotCallState", '{"state":"Connecting","reason":""}')
w.apply(SLOTS[0], "sipSlotCallState", '{"state":"Active","reason":""}')
s0 = w.snapshot()["slots"][0]
check("a call seen connecting is timed exactly", (s0["since"] is not None, s0["since_exact"]),
      (True, True))
check("its length leads the details",
      callme.call_details(w.snapshot(), now=s0["since"] + 3900)[0], ("Call length", "1 hour 5 minutes"))
check("under a minute", callme.describe_length(30), "less than a minute")
check("a call already up when the app started says 'at least'",
      callme.describe_length(600, exact=False), "at least 10 minutes")
w.apply(SLOTS[0], "sipSlotCallState", '{"state":"Expiring","reason":""}')
w.apply(SLOTS[0], "sipSlotCallState", '{"state":"Active","reason":""}')
check("an extension does not restart the count", w.snapshot()["slots"][0]["since"], s0["since"])

print("\nCONTACTS")
book, msg = callme.save_contact([], " Steven ", "https://callme.rode.com/s?c=xy34zw")
check("a guest link is saved as its code", book, [{"name": "Steven", "code": "XY34ZW"}])
check("saving says so, code spelt", msg, "Saved Steven, code X Y 3 4 Z W.")
check("a duplicate code is refused", callme.save_contact(book, "Steve", "XY34ZW")[1],
      "That code is already saved as Steven.")
check("a duplicate name is refused", callme.save_contact(book, "steven", "ABC123")[1],
      "You already have a contact called Steven.")
check("a name is required", callme.save_contact(book, " ", "ABC123")[0], None)
book2, _ = callme.save_contact(book, "Steven", "XYZ789", was="XY34ZW")
check("an edit replaces the entry, not adds one", book2, [{"name": "Steven", "code": "XYZ789"}])
check("a malformed stored entry is dropped",
      callme.clean_contacts([{"name": "A", "code": "!!"}, "junk", {"name": "B", "code": "b1"}]),
      [{"name": "B", "code": "B1"}])
callme.set_contacts(book)
check("a saved code is spoken by name", callme.peer_name("rode:xy34zw"), "Steven")
check("an unsaved code is still spelt", callme.peer_name("rode:AB12CD"), "RODECaster A B 1 2 C D")
check("deleting", callme.delete_contact(book, "XY34ZW"), ([], "Deleted Steven."))
check("deleting what is not there", callme.delete_contact(book, "ZZZ"),
      (None, "That contact is not saved."))
callme.set_contacts([])

print()
if FAILS:
    print(f"{len(FAILS)} FAILED:")
    for f in FAILS:
        print("  " + f)
    raise SystemExit(1)
print("all passed")
