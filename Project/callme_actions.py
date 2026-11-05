"""
AccessCaster - CallMe, RODE's remote guests: hosting, answering, dialling, contacts, and the ring.

The protocol and the wording are callme.py; this is the app side. The state is the desk model's,
which the Watcher reads and adds its own memory to, so `read_live` costs no device read and the
window can poll it, while `read` does a dump, which the model adopts.
"""

from __future__ import annotations

import threading
import time

import callme
import desk_access
import desk_model
import devices
import diagnostics
import rcp_read
import rcp_transfer
import settings_file
import speech
import tones

_watch = None
# How long an answer is given to reach Active before the reply goes out without it. Measured at
# about 1.3 s from the write.
_ANSWER_WAIT_S = 4.0


def watcher():
    """The running Watcher, started on first use, or None if it could not be started."""
    global _watch
    if _watch is None:
        try:
            w = callme.Watcher(on_say=speech.say, read_model=_dump,
                               on_expiring=_on_expiring, on_ring=_on_ring)
            w.start()
            _watch = w
        except Exception as exc:
            diagnostics.event("callme.watcher_failed", "the CallMe listener did not start",
                              error=str(exc))
            return None
    return _watch


def _gate():
    return desk_access.cap_gate("callme", lambda name:
                     f"AccessCaster can show CallMe on the {name} but not answer calls or change "
                     f"hosting.", confirmed=True)


def _dump() -> bytes:
    with desk_access.hid_lock:
        return rcp_read.read_model()


def payload(d: dict) -> dict:
    """The page's reply: the state, plus the sentences the page shows, all worded here."""
    d = dict(d)
    d["online"] = desk_model.online()
    w = watcher()
    if w is not None and "missed" not in d:
        d["missed"] = w.missed()        # a dump has no memory of a missed call; the Watcher does
    for s in d.get("slots", []):
        # A page read straight off a dump has no length: the Watcher holds it, keyed by the call.
        if "since" not in s and w is not None:
            s["since"], s["since_exact"] = w.since_for(s.get("uuid"))
    d["announcing"] = speech.enabled.is_set()
    d["plan"] = [{"label": k, "value": v} for k, v in callme.plan_lines(d)]
    d["now"] = callme.call_now(d)
    d["details"] = [{"label": k, "value": v} for k, v in callme.call_details(d)]
    d["end_label"] = callme.end_label(d)
    d["callme1_code"] = callme.CALLME1_SOURCE
    d["contacts"] = _contact_rows(contacts())
    settings = settings_file.load()
    d["auto_extend"] = bool(settings.get("callme_auto_extend"))
    d["ringtone"] = bool(settings.get("callme_ringtone", True))
    return d


def contacts() -> list:
    """The saved contacts, cleaned."""
    return callme.clean_contacts(settings_file.load().get("callme_contacts"))


def load_contacts() -> None:
    """Hand callme the saved contacts, which it names callers by."""
    callme.set_contacts(contacts())


def _contact_rows(saved: list) -> list:
    return [dict(c, spoken=callme.spell(c["code"])) for c in saved]


def _store_contacts(saved: list, message: str) -> dict:
    settings_file.update(callme_contacts=saved)
    callme.set_contacts(saved)
    return {"ok": True, "message": message, "contacts": _contact_rows(saved)}


def action_contact_save(name: str, code: str, was: str = "") -> dict:
    """Add a contact, or replace the one saved under the code `was`."""
    saved, message = callme.save_contact(contacts(), name, code, was)
    if saved is None:
        return {"ok": False, "message": message}
    return dict(_store_contacts(saved, message), code=callme.normalise_code(code))


def action_contact_delete(code: str) -> dict:
    saved, message = callme.delete_contact(contacts(), code)
    if saved is None:
        return {"ok": False, "message": message}
    return _store_contacts(saved, message)


def read() -> dict:
    """The whole CallMe page from one dump, which also re-seeds the Watcher."""
    try:
        blob = _dump()
    except Exception as exc:
        return {"ok": False, "message": f"Could not read the desk: {exc}"}
    d = callme.read(blob)
    if not d.get("ok"):
        return d
    return payload(d)


def read_live() -> dict:
    """The cheap one, from the Watcher alone: no device read. `version` changes whenever anything
    the page shows has changed, so the page redraws only then."""
    w = watcher()
    snap = w.snapshot() if w is not None else None
    if snap is None:
        return {"ok": False, "message": "The CallMe listener has not read the desk yet."}
    return payload(snap)


def state_now():
    """(state, control path) from the Watcher once the model holds CallMe, else from a fresh dump.
    A hotkey answer should not wait a second and a half for a dump the model makes unnecessary."""
    w = watcher()
    snap = w.snapshot() if w is not None else None
    if snap is not None and w.control_path() is not None:
        return snap, w.control_path()
    d, ctl, _slots = callme.read_all(_dump())
    return d, ctl


def action_hosting(on: bool) -> dict:
    """Switch call hosting, then wait for RODE's service to issue the code before saying so."""
    gate = _gate()
    if gate is not None:
        return gate
    on = bool(on)
    try:
        d, path = state_now()
        if path is None:
            return {"ok": False, "message": "This desk does not publish CallMe."}
        if d.get("hosting_locked"):
            return {"ok": False, "message": "Incoming calls cannot be switched while a call is up."}
        with desk_access.hid_lock:
            rcp_transfer.send_record(callme.build_hosting(on, path))
        # The code comes from RODE's service over the network, about a second after the switch. The
        # lock is taken per read, not across the wait, so shortcuts and the announcer carry on.
        deadline = time.monotonic() + (6.0 if on else 2.0)
        while True:
            time.sleep(1.0)
            with desk_access.hid_lock:
                blob = rcp_read.read_model_stable(fresh=True)
            d = callme.read(blob)
            settled = d.get("hosting") == on and (not on or d.get("code"))
            if settled or time.monotonic() > deadline:
                break
    except Exception as exc:
        return {"ok": False, "message": f"Could not change hosting: {exc}"}
    out = payload(d)
    if d.get("hosting") != on:
        out.update(ok=False, message="The desk did not take that: incoming calls are still "
                                     + ("on." if d.get("hosting") else "off."))
    elif not on:
        out.update(ok=True, message="Incoming calls are off.")
    elif d.get("code"):
        out.update(ok=True, message=(f"Guests can call in. Your desk's code is "
                                     f"{callme.spell(d['code'])}."))
    else:
        out.update(ok=True, message="Incoming calls are on, but RODE's service has not sent a code "
                                    "yet. Check the desk is online.")
    return out


def _callme1_on_fader():
    """Whether CallMe 1 is on a fader, read from the desk's fader layout, or None when the desk
    cannot be read. CallMe's own state does not follow the faders, and the desk does not push a
    layout change this app makes, so only a read of the layout itself is current."""
    count = devices.get_active_device().fader.count
    try:
        with desk_access.hid_lock:
            layout = rcp_read.values(rcp_read.read_model(), "channelInputSource")[:count]
    except Exception:
        return None
    return callme.CALLME1_SOURCE in layout


def _respond(answer: bool) -> dict:
    """Answer the ringing call into the first free slot, or decline it."""
    gate = _gate()
    if gate is not None:
        return gate
    try:
        d, path = state_now()
    except Exception as exc:
        return {"ok": False, "message": f"Could not read the desk: {exc}"}
    call = d.get("incoming")
    if not call:
        return {"ok": False, "message": "No CallMe call is ringing."}
    if path is None:
        return {"ok": False, "message": "This desk does not publish CallMe."}
    slot = -1
    if answer:
        free = [s["slot"] - 1 for s in d.get("slots", []) if s.get("state") == "Idle"]
        if not free:
            return {"ok": False, "message": callme.sentence(
                f"{call['name']} is calling, but every call this plan allows is already in use.")}
        slot = free[0]
    w = watcher()
    if w is not None:
        w.note_handled(call["id"])
        if answer:
            w.quiet_connect(slot, True)
    try:
        with desk_access.hid_lock:
            rcp_transfer.send_record(callme.build_answer(call["id"], slot, path))
    except Exception as exc:
        if w is not None and answer:
            w.quiet_connect(slot, False)
        return {"ok": False, "message": f"Could not send that to the desk: {exc}"}
    if not answer:
        return {"ok": True, "message": f"Declined the call from {call['name']}."}
    # WAIT FOR THE CALL TO GO ACTIVE, which is the only honest confirmation: a write the unit
    # stores and ignores looks exactly like one that worked, and so does a call whose audio never
    # opens until it fails a minute later.
    state, snap = _wait_for(w, slot + 1, "Active") if w is not None else (None, {})
    if state == "Active":
        msg = f"Connected to {call['name']}."
        if _callme1_on_fader() is False:
            msg += " No fader carries CallMe 1, so you will not hear them until one does."
        return {"ok": True, "message": msg}
    if w is not None:
        w.quiet_connect(slot, False)
    if state == "Connecting":
        return {"ok": True, "message": f"Answered {call['name']}. Still connecting."}
    return {"ok": False, "message": (f"Sent the answer for {call['name']}'s call, but the desk has "
                                     f"not started it.")}


def _wait_for(w, slot_number: int, want: str) -> tuple:
    """Poll the Watcher until call `slot_number` (1 to 3) reaches the state `want`, for up to
    _ANSWER_WAIT_S. Returns the last state seen and the snapshot it came from.

    Waiting for the state is the only honest confirmation: a write the unit stores and ignores
    looks exactly like one that worked."""
    deadline = time.monotonic() + _ANSWER_WAIT_S
    state, snap = None, {}
    while time.monotonic() < deadline:
        time.sleep(0.25)
        snap = w.snapshot() or {}
        state = next((s.get("state") for s in snap.get("slots", [])
                      if s.get("slot") == slot_number), None)
        if state == want:
            break
    return state, snap


def action_hangup(slot: int = 0) -> dict:
    """End a call. `slot` is the call's number, 1 to 3, or 0 for "the one call that is up"."""
    gate = _gate()
    if gate is not None:
        return gate
    w = watcher()
    snap = w.snapshot() if w is not None else None
    if snap is None:
        return {"ok": False, "message": "The CallMe listener has not read the desk yet."}
    up = [s for s in snap.get("slots", []) if s.get("state") in callme.UP]
    if slot:
        up = [s for s in up if s.get("slot") == int(slot)]
    if not up:
        return {"ok": False, "message": "No CallMe call is up."}
    if len(up) > 1:
        return {"ok": False, "message": "More than one call is up, so say which one to end."}
    target = up[0]
    i = target["slot"] - 1
    path = w.slot_path(i)
    if path is None:
        return {"ok": False, "message": "This desk does not publish that call."}
    who = target.get("peer") or "the guest"
    w.quiet_end(i, True)
    try:
        with desk_access.hid_lock:
            for rec in callme.build_hangup(path):
                rcp_transfer.send_record(rec)
                time.sleep(0.15)
    except Exception as exc:
        w.quiet_end(i, False)
        return {"ok": False, "message": f"Could not send that to the desk: {exc}"}
    if _wait_for(w, target["slot"], "Idle")[0] == "Idle":
        return {"ok": True, "message": f"Hung up on {who}."}
    w.quiet_end(i, False)
    return {"ok": False, "message": f"Sent the hang-up, but the call with {who} is still up."}


# How long a dial is given to show it is ringing or has failed. A wrong code fails in 0.4 s.
_DIAL_WAIT_S = 3.0


def action_dial(code: str) -> dict:
    """Call another RODECaster by its code. Replies once the call is RINGING, or with why it is
    not; whether it connects comes later, from the Watcher, because the other end may take up to
    two minutes to answer."""
    gate = _gate()
    if gate is not None:
        return gate
    code = callme.normalise_code(code)
    if not code:
        return {"ok": False, "message": "Type the other desk's CallMe code, letters and numbers "
                                        "only."}
    try:
        d, path = state_now()
    except Exception as exc:
        return {"ok": False, "message": f"Could not read the desk: {exc}"}
    if path is None:
        return {"ok": False, "message": "This desk does not publish CallMe."}
    if code == (d.get("code") or ""):
        return {"ok": False, "message": "That is this desk's own code."}
    if d.get("incoming"):
        return {"ok": False, "message": callme.sentence(
            f"{d['incoming']['name']} is calling you. Answer or decline that first.")}
    free = [s for s in d.get("slots", []) if s.get("state") == "Idle"]
    if not free:
        return {"ok": False, "message": "Every call this plan allows is already in use. Hang up "
                                        "first."}
    slot = free[0]
    i = slot["slot"] - 1
    who = callme.peer_name(callme.RODE_PREFIX + code)
    w = watcher()
    if w is None:
        # The listener is what tells a call that rang from one that failed, so without it a dial
        # would go out with nothing to follow it.
        return {"ok": False, "message": "The CallMe listener could not be started, so a call "
                                        "cannot be made. Restart AccessCaster and try again."}
    # A failure inside the wait is said by this reply, so the Watcher is kept from saying it too.
    w.quiet_end(i, True)
    try:
        with desk_access.hid_lock:
            for rec in callme.build_dial(code, i, path):
                rcp_transfer.send_record(rec)
                time.sleep(0.15)
    except Exception as exc:
        w.quiet_end(i, False)
        return {"ok": False, "message": f"Could not send that to the desk: {exc}"}
    ended_before = slot.get("ended", 0)
    started = time.monotonic()
    while time.monotonic() - started < _DIAL_WAIT_S:
        time.sleep(0.1)
        s = next((x for x in (w.snapshot() or {}).get("slots", [])
                  if x.get("slot") == slot["slot"]), {})
        if s.get("ended", 0) > ended_before:
            msg = f"Could not call {who}: {callme.end_reason(s.get('reason'))}."
            if callme.contact_name(code) and s.get("reason", "").startswith("404"):
                msg += (f" {who}'s code changes whenever they switch incoming calls off and on. Ask "
                        f"them for the new one and edit the contact.")
            return {"ok": False, "message": msg}
        # An unknown code goes through Connecting before it fails, so Connecting only counts as
        # ringing once it has lasted a second.
        if s.get("state") in callme.UP and time.monotonic() - started >= 1.0:
            w.quiet_end(i, False)
            return {"ok": True, "message": f"Calling {who}."}
    w.quiet_end(i, False)
    return {"ok": False, "message": f"Sent the call to {who}, but the desk has not started it."}


def _on_expiring(_slot_index: int) -> bool:
    """The Watcher's hook when a call goes Expiring. Runs under the Watcher's lock, so the extend
    itself goes to a thread."""
    if not settings_file.load().get("callme_auto_extend"):
        return False
    threading.Thread(target=_auto_extend, daemon=True, name="callme-auto-extend").start()
    return True


def _auto_extend() -> None:
    res = action_extend(auto=True)
    speech.say(res.get("message") or "Could not extend the call.")


def action_auto_extend(on: bool) -> dict:
    settings_file.update(callme_auto_extend=bool(on))
    return {"ok": True, "auto_extend": bool(on),
            "message": ("Calls will be extended automatically before the hour runs out." if on
                        else "Calls will no longer be extended automatically.")}


# The desk stops ringing an unanswered call after 120 s; the loop is capped a little past that so a
# missed clear cannot leave it ringing for ever.
_RING_MAX_S = 130.0
# One ring, then who is calling, starting again every this many seconds while the call rings.
_RING_CYCLE_S = 4.0


def _on_ring(call: dict, sentence: str) -> bool:
    """The Watcher's hook when a new call starts ringing. With the ringtone on, a thread of its own
    rings and says who is calling in turn (see _ring); True tells the Watcher the sentence is
    taken care of."""
    if not settings_file.load().get("callme_ringtone", True):
        return False
    threading.Thread(target=_ring, args=(call.get("id"), sentence), daemon=True,
                     name="callme-ring").start()
    return True


def _ring(call_id, sentence: str) -> None:
    """Ring until that call stops ringing (answered, declined, given up or timed out): one ring,
    then who is calling, again every _RING_CYCLE_S.

    IN TURN, NEVER TOGETHER: the speech thread plays one item at a time and keeps only the newest
    waiting, so a name queued while the ring plays would replace the next ring. So the ring is
    queued and given its own length to play, and the name is spoken with speech.say_and_wait.
    Nothing plays between turns, so the call and the ringtone switch are checked only before each
    one. The announcer switch silences both."""
    start = time.monotonic()

    def still_ringing() -> bool:
        w = watcher()
        snap = w.snapshot() if w is not None else None
        ringing = (snap or {}).get("incoming") or {}
        return (ringing.get("id") == call_id and time.monotonic() - start < _RING_MAX_S
                and settings_file.load().get("callme_ringtone", True))

    while still_ringing():
        cycle = time.monotonic()
        if speech.tone("ring"):
            time.sleep(tones.RING_DURATION_S + 0.15)
        if not still_ringing():
            return
        speech.say_and_wait(sentence)
        time.sleep(max(0.0, _RING_CYCLE_S - (time.monotonic() - cycle)))


def action_ringtone(on: bool) -> dict:
    settings_file.update(callme_ringtone=bool(on))
    return {"ok": True, "ringtone": bool(on),
            "message": ("An incoming call rings until it is answered." if on
                        else "An incoming call is announced without ringing.")}


def action_extend(auto: bool = False) -> dict:
    """Extend the call that is about to reach the plan's one-hour limit."""
    gate = _gate()
    if gate is not None:
        return gate
    w = watcher()
    snap = w.snapshot() if w is not None else None
    if snap is None:
        return {"ok": False, "message": "The CallMe listener has not read the desk yet."}
    expiring = [s for s in snap.get("slots", []) if s.get("state") == "Expiring"]
    if not expiring:
        return {"ok": False, "message": "No call is about to end, so there is nothing to extend."}
    target = expiring[0]
    i = target["slot"] - 1
    path = w.slot_path(i)
    if path is None:
        return {"ok": False, "message": "This desk does not publish that call."}
    who = target.get("peer") or "the guest"
    w.quiet_connect(i, True)
    try:
        with desk_access.hid_lock:
            for rec in callme.build_extend(path):
                rcp_transfer.send_record(rec)
                time.sleep(0.15)
    except Exception as exc:
        w.quiet_connect(i, False)
        return {"ok": False, "message": f"Could not send that to the desk: {exc}"}
    if _wait_for(w, target["slot"], "Active")[0] == "Active":
        return {"ok": True, "message": (f"Extended the call with {who}"
                                        + (" automatically." if auto else "."))}
    w.quiet_connect(i, False)
    return {"ok": False, "message": (f"Sent Extend, but the call with {who} is still about to "
                                     f"end in five minutes.")}


def action_end() -> dict:
    """Decline the call that is ringing, or else hang up the one that is up. A ringing call comes
    first, so a second call arriving mid-conversation is turned away rather than the guest you are
    talking to being cut off."""
    w = watcher()
    snap = (w.snapshot() if w is not None else None) or {}
    if snap.get("incoming"):
        return action_decline()
    if any(s.get("state") in callme.UP for s in snap.get("slots", [])):
        return action_hangup()
    return {"ok": False, "message": "No CallMe call to end."}


def action_answer() -> dict:
    """Answer the ringing call; with nothing ringing, extend a call that is about to end. A ringing
    call comes first, because its two minutes run out sooner than an expiring call's five."""
    w = watcher()
    snap = (w.snapshot() if w is not None else None) or {}
    if not snap.get("incoming") and any(s.get("state") == "Expiring"
                                        for s in snap.get("slots", [])):
        return action_extend()
    return _respond(True)


def action_decline() -> dict:
    return _respond(False)


def status() -> dict:
    """For the status shortcut: from the Watcher, falling back to a dump."""
    try:
        d, _path = state_now()
    except Exception as exc:
        return {"ok": False, "message": f"Could not read CallMe: {exc}"}
    if not d.get("ok", True):
        return {"ok": False, "message": d.get("message") or "CallMe could not be read."}
    return {"ok": True, "message": callme.status_sentence(d)}
