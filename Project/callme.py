"""CallMe: RODE's remote-guest calling, read, answered and announced from this side.

The protocol is section 8.2 of RODECASTER_PROTOCOL.txt and the traps are under CallMe in
PROJECT_GUIDE.md part 2. In short: everything lives on the SIPCALLING node and three call-slot
nodes, all root children, addressed from the live tree; an incoming call is PUSHED; answering is one
composite write. The touchscreen is the only other way to do any of it, which is why this exists.

THE WORDING LIVES HERE, like the recorder's: the app speaks it, the window shows it, and the
window composes none of it.
"""

from __future__ import annotations

import datetime
import json
import threading
import time

import desk_live
import desk_model
import diagnostics
import model_tree
import rcp_transfer
from wording import describe_duration, spoken_date

GUEST_LINK = "https://callme.rode.com/s?c="
# CallMe 1's source code: what a fader carries when CallMe is on it.
CALLME1_SOURCE = 16

# Only mode 0 has been read off a desk (an unlicensed Pro II). The paid tiers' numbers are unknown,
# so they are reported by number rather than guessed at.
TIER_NAMES = {0: "Lite"}
# How many of the three published slots a tier can use: RODE sell Lite with one CallMe channel,
# CallMe with two and Pro with three. An unknown mode gets all three rather than hiding a real one.
TIER_SLOTS = {0: 1}

# The desk's reasons for a call ending, as a sentence ending. The numbered ones are SIP status
# codes. Anything else is spoken as the desk words it.
END_REASONS = {
    "remote drop": "the guest hung up",
    "local drop": "you hung up",
    "rtp stream error": "the call's audio could not connect",
    "486 rejected": "they declined the call",
    "404 not found": "no RODECaster has that code",
    "408 request timeout": "nobody answered",
    "idle timeout": "it reached the plan's one-hour limit",
}
# Endings that need no explanation afterwards: "No call." says enough.
ORDINARY_ENDS = ("remote drop", "local drop")
# A call is CONNECTED while Active, and still while Expiring: the state the Lite and standard plans
# give it five minutes before their one-hour limit ends it. UP adds the ringing-out Connecting.
CONNECTED = ("Active", "Expiring")
UP = CONNECTED + ("Connecting",)
# How a RODECaster is addressed when dialled, and how its peer address reads back.
RODE_PREFIX = "rode:"


def tier_name(mode) -> str:
    if mode in TIER_NAMES:
        return TIER_NAMES[mode]
    return f"a paid plan (mode {mode})" if isinstance(mode, int) else "unknown"


def renewal_date(text: str, year: bool = True) -> str:
    """RODE's "October 3, 2026" as "3rd October 2026", or "3rd October" without the year. Left as
    it came if it does not parse."""
    try:
        d = datetime.datetime.strptime((text or "").strip(), "%B %d, %Y")
    except ValueError:
        return text or ""
    return spoken_date(d, year)


def utf8(text) -> str:
    """The desk sends UTF-8 and the model decoder reads latin-1, so "RØDE-CallMe" arrives as
    mojibake unless it is re-decoded."""
    if not isinstance(text, str):
        return ""
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def composite_text(value):
    """The string half of a (string, bool) composite, or the value itself when it is a plain
    string. `sipIncomingCallAccept` is a plain string until the touchscreen first answers a call and
    a composite from then on, and `sipIncomingCallDetails` is always a composite."""
    if isinstance(value, list):
        for v, k in value:
            if k == "str":
                return v
        return ""
    return value if isinstance(value, str) else ""


def parse_call(value):
    """{"id", "name", "type"} from `sipIncomingCallDetails`, or None when nothing is ringing."""
    text = composite_text(value)
    if not text:
        return None
    try:
        d = json.loads(text)
    except ValueError:
        return None
    if "id" not in d:
        return None
    return {"id": d["id"], "name": utf8(d.get("peername") or "") or "a guest",
            "type": d.get("calltype") or ""}


def parse_slot_state(value):
    """(state, reason) from `sipSlotCallState`. A slot that has never carried a call reads a bare
    "Idle"; after that it is JSON, {"state": ..., "reason": ...}."""
    text = value if isinstance(value, str) else ""
    if not text.startswith("{"):
        return (text or "Idle"), ""
    try:
        d = json.loads(text)
    except ValueError:
        return text, ""
    return (d.get("state") or "Idle"), (d.get("reason") or "")


def end_reason(reason: str) -> str:
    return END_REASONS.get((reason or "").strip().lower(), reason or "")


def peer_name(address) -> str:
    """Who a call is with, in words. A dialled RODECaster reads back as "rode:<code>", which is
    the contact's name if the code is saved, else the code spoken one character at a time; a
    guest's own name is used as it is."""
    text = utf8(address or "")
    if text.lower().startswith(RODE_PREFIX):
        code = text[len(RODE_PREFIX):].upper()
        return contact_name(code) or "RODECaster " + spell(code)
    return text


# ---------------------------------------------------------------------------------------------
# CONTACTS: names for RODECaster codes, kept on this computer.
# ---------------------------------------------------------------------------------------------
# A code belongs to RODE's service, which issues a new one each time its owner switches hosting
# off and on. So an entry can go stale and nothing on this side can learn the new code; only its
# owner can say it. callme_actions owns the stored list and hands it here with set_contacts.

_contacts: dict = {}        # code -> name


def clean_contacts(raw) -> list:
    """Stored contacts as [{"name", "code"}], sorted by name, with anything malformed dropped."""
    out, seen = [], set()
    for c in raw if isinstance(raw, list) else []:
        if not isinstance(c, dict):
            continue
        name = " ".join(str(c.get("name") or "").split())
        code = normalise_code(c.get("code"))
        if name and code and code not in seen:
            seen.add(code)
            out.append({"name": name, "code": code})
    return sorted(out, key=lambda c: c["name"].lower())


def set_contacts(contacts) -> None:
    global _contacts
    _contacts = {c["code"]: c["name"] for c in clean_contacts(contacts)}


def contact_name(code) -> str:
    """The saved name for a code, or ""."""
    return _contacts.get(normalise_code(code), "")


def save_contact(contacts: list, name, code, was="") -> tuple:
    """(new list, message) adding a contact, or replacing the one whose code is `was`.
    The list is None when the entry is refused, and the message says why."""
    name = " ".join(str(name or "").split())
    code = normalise_code(code)
    was = normalise_code(was)
    if not name:
        return None, "Type a name for the contact."
    if not code:
        return None, "Type their CallMe code, letters and numbers only."
    others = [c for c in clean_contacts(contacts) if c["code"] != was]
    for c in others:
        if c["code"] == code:
            return None, f"That code is already saved as {c['name']}."
        if c["name"].lower() == name.lower():
            return None, f"You already have a contact called {c['name']}."
    new = clean_contacts(others + [{"name": name, "code": code}])
    verb = "Updated" if was else "Saved"
    return new, f"{verb} {name}, code {spell(code)}."


def delete_contact(contacts: list, code) -> tuple:
    """(new list, message) without the contact whose code is `code`. None if there is no such
    contact."""
    code = normalise_code(code)
    cur = clean_contacts(contacts)
    gone = next((c for c in cur if c["code"] == code), None)
    if gone is None:
        return None, "That contact is not saved."
    return [c for c in cur if c["code"] != code], f"Deleted {gone['name']}."


def normalise_code(text) -> str:
    """A RODECaster code from what somebody typed or pasted: the bare code, the code with spaces,
    "rode:<code>", or a whole guest link. Empty if there is no usable code in it."""
    t = (text or "").strip()
    if "?c=" in t:
        t = t.split("?c=", 1)[1].split("&", 1)[0]
    if t.lower().startswith(RODE_PREFIX):
        t = t[len(RODE_PREFIX):]
    t = "".join(t.split()).upper()
    return t if t.isalnum() else ""


def sentence(text: str) -> str:
    """Capitalise the first letter, for a sentence that may open with a name like "a guest"."""
    return text[:1].upper() + text[1:] if text else text


# ---------------------------------------------------------------------------------------------
# WHERE THINGS ARE. The control node (SIPCALLING) and the call slots are found by the property
# they carry (`sipIncomingCallAccept`, `sipSlotCallState`), never by position: see read_all.
# ---------------------------------------------------------------------------------------------

def _root(blob: bytes):
    return model_tree.parse(blob)[0]


def _props(node) -> dict:
    return {p: v for p, v, _k in node.props}


# ---------------------------------------------------------------------------------------------
# WRITES
# ---------------------------------------------------------------------------------------------

def _block(payload: bytes) -> bytes:
    """One value block: a one-byte length, then the type byte and its data."""
    return bytes([0x01, len(payload)]) + payload


def build_answer(call_id: int, slot: int, path) -> bytes:
    """Answer the ringing call into `slot`, or decline it with slot -1.

    A COMPOSITE OF THE JSON AND A FALSE BOOL, exactly as the touchscreen writes it. Both halves
    matter and neither looks like it does: the string alone is stored and ignored, and with a TRUE
    bool the call is set up but its audio never opens, failing 60 s later as "rtp stream error"."""
    value = json.dumps({"id": int(call_id), "sip_slot": int(slot)}, separators=(",", ":"))
    inner = _block(b"\x05" + value.encode("ascii") + b"\x00") + _block(b"\x03")
    payload = (rcp_transfer.build_address(list(path)) + b"sipIncomingCallAccept\x00"
               + _block(b"\x08" + inner))
    return rcp_transfer._frame(payload)


def build_hangup(path) -> list:
    """End the call on the slot at `path`: two records, sent in order.

    `sipSlotCallDisconnect` is a pair of bools and the desk hangs up when the FIRST one RISES from
    False to True. Writing the value it already holds does nothing, so the first record lowers it
    (which leaves the call alone) and the second raises it. (False, False) then (True, False) ends
    a call as "Local drop"; (True, True) over (True, True) and (False, True) over (True, True) do
    nothing."""
    def rec(first: bool) -> bytes:
        inner = _block(b"\x02" if first else b"\x03") + _block(b"\x03")
        return rcp_transfer._frame(rcp_transfer.build_address(list(path))
                                   + b"sipSlotCallDisconnect\x00" + _block(b"\x08" + inner))
    return [rec(False), rec(True)]


def build_extend(path) -> list:
    """Extend the expiring call on the slot at `path`: two records, sent in order.

    `sipSlotCallExtend` is a pair of bools, and the touchscreen's Extend leaves it at (True, True).
    A single bool is stored and ignored. The pair is lowered first and then raised, the shape the
    hang-up pair needs, because a write of the value already held may do nothing; whether Extend
    strictly needs the lowering is unmeasured."""
    def rec(on: bool) -> bytes:
        flag = _block(b"\x02" if on else b"\x03")
        return rcp_transfer._frame(rcp_transfer.build_address(list(path))
                                   + b"sipSlotCallExtend\x00" + _block(b"\x08" + flag + flag))
    return [rec(False), rec(True)]


def build_dial(code: str, slot: int, path) -> list:
    """Call the RODECaster with `code` from `slot`: two records, sent in order.

    The call is the touchscreen's own template, `{"dest":"rode:<code>","sip_slot":N}`, as a
    composite with a FALSE bool, like an answer. THE DESK ACTS ONLY ON A CHANGE: a dial identical to
    the one the property already holds is stored and ignored, so redialling the same code would do
    nothing. The first record clears the property to its factory value, a plain empty string,
    which starts nothing. The slot goes Connecting at once; a declined call ends as "486 Rejected"
    and an unknown code as "404 Not Found" within half a second."""
    head = rcp_transfer.build_address(list(path)) + b"sipOutgoingCallDetails\x00"
    value = json.dumps({"dest": RODE_PREFIX + code, "sip_slot": int(slot)}, separators=(",", ":"))
    inner = _block(b"\x05" + value.encode("ascii") + b"\x00") + _block(b"\x03")
    return [rcp_transfer._frame(head + _block(b"\x05\x00")),
            rcp_transfer._frame(head + _block(b"\x08" + inner))]


def build_hosting(on: bool, path) -> bytes:
    return rcp_transfer.build_path_prop_bool("sipCallHostingEnabled", bool(on), path)


# ---------------------------------------------------------------------------------------------
# THE STATE, as the page and the spoken status want it
# ---------------------------------------------------------------------------------------------

def _slot_view(n: int, props: dict) -> dict:
    state, reason = parse_slot_state(props.get("sipSlotCallState"))
    active = state in CONNECTED
    bitrate = props.get("sipSlotCallBitrate")
    jitter = props.get("sipSlotCallJitter")
    loss = props.get("sipSlotCallPacketLoss")
    return {"slot": n, "state": state, "reason": reason,
            "peer": peer_name(props.get("sipSlotCallAddress")) if state != "Idle" else "",
            # Identifies one call across re-seeds, which is what keeps its length counting.
            "uuid": props.get("sipSlotCallUUID") or "",
            # True for a call that came IN (this unit is hosting it), False for one placed here.
            "incoming": bool(props.get("sipSlotCallIsHost")),
            # Bumped by the Watcher each time a call on this slot ends, so a caller waiting on the
            # slot can tell a NEW ending from the reason the last call left behind.
            "ended": 0,
            "quality": props.get("sipSlotCallQuality") if active else None,
            "bitrate_kbps": round(bitrate / 1000) if active and isinstance(bitrate, float) else None,
            "jitter_ms": round(jitter * 1000, 1) if active and isinstance(jitter, float) else None,
            "packet_loss": loss if active and isinstance(loss, float) else None}


def read(blob: bytes) -> dict:
    """Everything the page shows, from one model dump. Passwords are never included."""
    return read_all(blob)[0]


def read_all(blob: bytes) -> tuple:
    """(read's state, control path, slot paths) from ONE parse of the dump."""
    root = _root(blob)
    p, slots, ctl, slot_at = None, [], None, []
    for n in model_tree.walk(root):
        props = _props(n)
        if "sipIncomingCallAccept" in props:
            p, ctl = props, n.path()
        elif "sipSlotCallState" in props:
            slots.append(_slot_view(len(slots) + 1, props))
            slot_at.append(n.path())
    if p is None:
        return {"ok": False, "message": "This desk does not publish CallMe."}, None, []
    return _state(p, slots), ctl, slot_at


def _state(p: dict, slots: list) -> dict:
    code = p.get("sipRodeCode") or ""
    hosting = bool(p.get("sipCallHostingEnabled"))
    mode = p.get("sipLicenceMode")
    return {"ok": True,
            "hosting": hosting,
            # The desk locks the switch while a call is up.
            "hosting_locked": p.get("sipCallHostingToggleEnabled") is False,
            "code": code if hosting else "",
            "link": (GUEST_LINK + code) if hosting and code else "",
            "tier_mode": mode,
            "tier": tier_name(mode),
            "minutes_left": p.get("sipRemainingCalltime"),
            "web_minutes_left": p.get("sipRemainingWebCalltime"),
            "renews": renewal_date(p.get("sipRenewalDate") or "", year=False),
            "incoming": parse_call(p.get("sipIncomingCallDetails")),
            "slots": slots[:TIER_SLOTS.get(mode, len(slots))]}


def plan_lines(d: dict) -> list:
    """The plan as (label, value) pairs, one fact per line on the page."""
    mins, web = d.get("minutes_left"), d.get("web_minutes_left")
    out = [("CallMe plan", d.get("tier") or "unknown")]
    if isinstance(mins, int):
        out.append(("Desk to desk minutes remaining", describe_minutes(mins)))
    if isinstance(web, int):
        out.append(("Browser minutes remaining", describe_minutes(web)))
    if d.get("renews"):
        out.append(("Plan resets", d["renews"]))
    return out


def describe_minutes(m: int) -> str:
    return describe_duration(m * 60) if m else "0 minutes"


def _live(d: dict) -> list:
    return [s for s in d.get("slots", []) if s.get("state") in UP]


def _doing(s: dict) -> str:
    who = s.get("peer") or "a guest"
    if s.get("state") == "Expiring":
        return f"talking to {who}, ending within five minutes"
    if s.get("state") == "Active":
        return f"talking to {who}"
    return f"connecting to {who}" if s.get("incoming") else f"calling {who}"


def call_now(d: dict) -> str:
    """The one line saying what is happening: ringing, calling, connected, or nothing."""
    parts = []
    inc = d.get("incoming")
    if inc:
        parts.append(sentence(f"{inc['name']} is calling."))
    live = _live(d)
    if len(live) == 1:
        parts.append(sentence(_doing(live[0]) + "."))
    else:
        parts += [f"Call {s['slot']}: {_doing(s)}." for s in live]
    if not parts:
        # A missed call is always the latest event, since any later call clears it.
        last = next((s.get("reason") for s in d.get("slots", []) if s.get("reason")), "")
        if d.get("missed"):
            parts.append(f"No call. You missed a call from {d['missed']}.")
        elif last and last.strip().lower() not in ORDINARY_ENDS:
            parts.append(f"No call. The last one ended because {end_reason(last)}.")
        else:
            parts.append("No call.")
    return " ".join(parts)


def describe_length(seconds: float, exact: bool = True) -> str:
    """A call's length in whole minutes. Not exact when the app started mid-call, so "at least"."""
    m = int(max(0, seconds) // 60)
    text = "less than a minute" if m == 0 else describe_minutes(m)
    return text if exact else f"at least {text}"


def call_details(d: dict, now: float | None = None) -> list:
    """Each connected call's length and technical side, as (label, value) pairs. Empty with no call
    up. `sipSlotCallQuality` is left out: its scale is unknown, and 2 is the only value ever seen.
    The desk publishes no start time, so the length is the Watcher's own count from Active."""
    now = time.monotonic() if now is None else now
    out = []
    live = [s for s in _live(d) if s.get("state") in CONNECTED]
    for s in live:
        tag = f"Call {s['slot']} " if len(live) > 1 else ""
        if s.get("since"):
            out.append((tag + "Call length",
                        describe_length(now - s["since"], s.get("since_exact", True))))
        if s.get("bitrate_kbps") is not None:
            out.append((tag + "Bitrate", f"{s['bitrate_kbps']} kilobits a second"))
        out.append((tag + "Audio lost", _loss_words(s.get("packet_loss"))))
        if s.get("jitter_ms") is not None:
            out.append((tag + "Jitter", f"{s['jitter_ms']:g} milliseconds"))
    return out


def end_label(d: dict) -> str:
    """The second button's name: it declines a ringing call first, as the D shortcut does."""
    return "Decline" if d.get("incoming") else "Hang up"


def _loss_words(loss) -> str:
    if not isinstance(loss, float) or loss <= 0:
        return "none"
    return f"{loss:g} percent"


def status_sentence(d: dict) -> str:
    """The spoken summary: what is happening, and whether calls can come in."""
    parts = [call_now(d)]
    if d.get("online") is False:
        parts.append("The desk is offline, so no call can connect.")
    elif d.get("hosting"):
        parts.append(f"Your desk's code is {spell(d.get('code') or '')}."
                     if d.get("code") else "Guests can call in.")
    else:
        parts.append("Incoming calls are off.")
    return " ".join(parts)


def readiness(online, on_fader) -> list:
    """What has to be in place before a call can work, as [{key, ok, text}], most basic first. The
    page shows only this, with a fix beside each item, until every item is ok.

    Offline, Join Call on the guest's page spins for a few seconds and gives up with nothing reaching
    the desk; with CallMe 1 on no fader it does nothing at all, and the desk never hears of the call.
    Neither shows on the desk or the guest's page. An unknown (None) is reported as not ok, never
    assumed fine. Hosting is not listed: with it off a call out still works, so it is a setting,
    not a fault.

    `on_fader` is the caller's: whether CALLME1_SOURCE is in the fader layout it already holds.
    CallMe's own state does not follow the faders, so it never carries that fact."""
    out = []
    out.append({"key": "online", "ok": online is True,
                "text": "The desk is online." if online else
                        ("The desk is offline, so no call can connect either way. Join it to wifi "
                         "or plug in a network cable." if online is False else
                         "Whether the desk is online is not known yet.")})
    out.append({"key": "fader", "ok": on_fader is True,
                "text": "CallMe 1 is on a fader." if on_fader else
                        "CallMe 1 is on no fader, so a guest's call never reaches the desk. "
                        "Put it on a fader."})
    return out


def spell(code: str) -> str:
    """A code read out one character at a time, so a screen reader does not try it as a word."""
    return " ".join(code)




# ---------------------------------------------------------------------------------------------
# THE WATCHER: what the pushes mean, and what the desk does not remember
# ---------------------------------------------------------------------------------------------

# The pushes the Watcher speaks from: the control node's, then a slot's.
NARRATED = frozenset({
    "sipIncomingCallDetails", "sipIncomingCallAccept",
    "sipSlotCallAddress", "sipSlotCallUUID", "sipSlotCallState"})


class Watcher:
    """Speaks CallMe as it happens, and answers for the page from the desk model (`desk_model`).

    THE STATE IS THE MODEL'S. `snapshot` is `read` over the model's picture, derived again only
    when the picture has changed, so the page, the hotkeys and the waits after a write cost no
    device read and see every change, pushed or read. The Watcher adds only what the desk does not
    hold: who the last missed call was from, when each call connected, how many calls each slot has
    ended, and which calls and endings a reply from this app is already saying.

    SPEECH NEEDS EACH PUSH IN THE ORDER IT CAME, which the model cannot give: the listener applies a
    push to the model before any subscriber hears it, so by the time a call's end is heard the
    slot's address may already be cleared. A transition is read from the push itself (`Push.was`),
    and who is ringing and who is on each slot are remembered as they were heard.

    NOT A BRANCH OF THE ANNOUNCER: the announcer only runs while announcements are on, and a call
    has to be tracked whether or not anyone wants it spoken. Speech goes through `on_say`, which
    `callme_actions` points at `speech.say`, so the announcements switch still decides what is heard.

    `read_model` is called each time the listener has the desk open: the model adopts that read,
    and what the Watcher remembers is seeded from it. Nothing is spoken before the model holds
    CallMe."""

    def __init__(self, on_say=None, read_model=None, on_expiring=None, on_ring=None):
        self._on_say = on_say
        # Called outside the lock as on_ring(call, sentence) when a NEW call starts ringing. True
        # means it is announcing the call itself (so it can ring AFTER the sentence, which a ring
        # queued alongside would cut off); False leaves the sentence to on_say.
        self._on_ring = on_ring
        self._read_model = read_model
        # Called with the slot index when a call goes Expiring, under the Watcher's lock, so it
        # must not block. True means it is extending the call and the warning goes unsaid.
        self._on_expiring = on_expiring
        self._lock = threading.Lock()
        self._blob = None               # the model snapshot `_derived` was read from
        self._derived = None            # read_all of it, or None while it holds no CallMe
        self._shown = None              # the last snapshot handed out, to tell when it changes
        self._ringing = None            # the call ringing, as last heard
        self._peer = {}                 # slot index -> who is on it, as last heard
        self._uuid = {}                 # slot index -> its call's id, as last heard
        self._ended = {}                # slot index -> calls that have ended on it
        self._handled = set()           # call ids answered or declined, whose clear is not news
        self._quiet = set()             # slot indices whose next "Connected" the reply speaks
        self._quiet_end = set()         # slot indices whose next "ended" the reply speaks
        self._since = {}                # call uuid -> (connected at, exact): see since_for
        self._missed = None             # who the last missed call was from, until the next call
        self.version = 0                # bumped whenever the snapshot differs from the last one
        self._follower = None

    def start(self) -> None:
        self._follower = desk_live.Follower("callme", NARRATED, on_push=self.apply,
                                            on_connected=self._connected).start()

    def _connected(self, _gap) -> None:
        # The pushes heard while this dump runs queue behind it, and the model replays them on top.
        if self._read_model is None:
            return
        try:
            self._read_model()
        except Exception as exc:
            diagnostics.event("callme.seed_failed", "callme watcher", error=f"{exc}")
            return
        self.seed()

    # -- state ---------------------------------------------------------------------------------
    def _view(self):
        """(read's state, control path, slot paths) from the model's picture, or None while it
        holds no CallMe. Caller holds the lock."""
        blob = desk_model.snapshot()
        if blob is not self._blob:      # the model hands out one object until its next change
            self._blob = blob
            try:
                d, ctl, slots = read_all(blob) if blob else ({"ok": False}, None, [])
            except Exception as exc:
                diagnostics.event("callme.read_failed", "callme watcher", error=f"{exc}")
                d, ctl, slots = {"ok": False}, None, []
            self._derived = (d, ctl, slots) if d.get("ok") else None
            # A call already up when first seen started at some unknown earlier time.
            for s in d.get("slots", []):
                if s["state"] in CONNECTED and s["uuid"] and s["uuid"] not in self._since:
                    self._since[s["uuid"]] = (time.monotonic(), False)
        return self._derived

    def seed(self) -> None:
        """Learn who is ringing and who is on each slot from the model, after a time nobody was
        listening."""
        with self._lock:
            view = self._view()
            if view is None:
                return
            d = view[0]
            self._ringing = d.get("incoming")
            self._peer = {i: s["peer"] for i, s in enumerate(d["slots"])}
            self._uuid = {i: s["uuid"] for i, s in enumerate(d["slots"])}

    def snapshot(self):
        """The page's state, `read`'s shape plus `missed`, each slot's `ended`, `since` and
        `since_exact`, and `version`; None while the model holds no CallMe."""
        with self._lock:
            view = self._view()
            if view is None:
                return None
            d = json.loads(json.dumps(view[0]))
            # Part of what `version` covers, so the page's Not ready group follows the network.
            d["online"] = desk_model.online()
            d["missed"] = self._missed
            for i, s in enumerate(d["slots"]):
                s["ended"] = self._ended.get(i, 0)
                s["since"], s["since_exact"] = self._since.get(s["uuid"], (None, None))
            if d != self._shown:
                self._shown = d
                self.version += 1
            return json.loads(json.dumps(dict(d, version=self.version)))

    def quiet_connect(self, slot_index: int, on: bool) -> None:
        """Keep "Connected" unsaid for a call this app is answering, whose reply says it instead.
        Switched back off if the reply gives up waiting, so a late connection is still heard."""
        with self._lock:
            if on:
                self._quiet.add(slot_index)
            else:
                self._quiet.discard(slot_index)

    def missed(self):
        """Who the last missed call was from, or None."""
        with self._lock:
            return self._missed

    def since_for(self, uuid) -> tuple:
        """(time the call connected, whether that is exact) for a call, or (None, None)."""
        with self._lock:
            return self._since.get(uuid, (None, None))

    def quiet_end(self, slot_index: int, on: bool) -> None:
        """The same as quiet_connect, for a call this app is hanging up."""
        with self._lock:
            if on:
                self._quiet_end.add(slot_index)
            else:
                self._quiet_end.discard(slot_index)

    def slot_path(self, slot_index: int):
        with self._lock:
            view = self._view()
            slots = view[2] if view is not None else []
            return slots[slot_index] if 0 <= slot_index < len(slots) else None

    def control_path(self):
        with self._lock:
            view = self._view()
            return view[1] if view is not None else None

    def note_handled(self, call_id) -> None:
        """A call this app answered or declined: the reply already said so, so the watcher must
        not say it again when the ringing clears."""
        with self._lock:
            self._handled.add(call_id)

    # -- one push -------------------------------------------------------------------------------
    def apply(self, push) -> None:
        """Speak what one push (`desk_live.Push`) means."""
        path = list(push.path) if push.path is not None else None
        say, ring = [], []
        with self._lock:
            view = self._view()
            if view is None or not path:
                return
            d, ctl, slots = view
            if ctl is not None and path == list(ctl):
                self._control(push.name, push.value, push.was, say, ring)
            elif path in slots and slots.index(path) < len(d["slots"]):
                self._slot(slots.index(path), push.name, push.value, push.was, say)
        for call, text in ring:
            try:
                taken = bool(self._on_ring(call, text))
            except Exception:
                taken = False
            if not taken:
                say.insert(0, text)
        for text in say:
            if self._on_say:
                try:
                    self._on_say(text)
                except Exception:
                    pass

    def _control(self, name, value, was, say, ring) -> None:
        if name == "sipIncomingCallDetails":
            call, before = parse_call(value), parse_call(was)
            self._ringing = call
            if call and (not before or before["id"] != call["id"]):
                self._missed = None
                text = f"CallMe call from {call['name']}."
                if self._on_ring:
                    ring.append((call, text))
                else:
                    say.append(text)
            elif not call and before and before["id"] not in self._handled:
                # Cleared. An answer or decline from this app, or from the touchscreen (whose
                # accept push arrives first and marks it handled), has been spoken already. What is
                # left is a call nobody took: the 120 s ring ran out, or the guest gave up.
                self._missed = before["name"]
                say.append(f"Missed CallMe call from {before['name']}.")
        elif name == "sipIncomingCallAccept":
            try:
                accept = json.loads(composite_text(value) or "{}")
            except ValueError:
                return
            call_id = accept.get("id")
            if call_id is not None and call_id not in self._handled:
                self._handled.add(call_id)
                ringing = self._ringing or {}
                who = ringing.get("name") if ringing.get("id") == call_id else "the guest"
                if accept.get("sip_slot", -1) < 0:
                    say.append(f"Declined the call from {who} on the desk.")

    def _slot(self, i, name, value, was, say) -> None:
        if name == "sipSlotCallAddress":
            self._peer[i] = peer_name(value)
        elif name == "sipSlotCallUUID":
            self._uuid[i] = value if isinstance(value, str) else ""
        elif name == "sipSlotCallState":
            state, reason = parse_slot_state(value)
            before = parse_slot_state(was)[0] if was is not None else None
            if state in UP:
                self._missed = None
            who = self._peer.get(i) or "the guest"
            if state == "Expiring" and before != "Expiring":
                if not (self._on_expiring and self._on_expiring(i)):
                    say.append(f"Call with {who} ends in five minutes unless you extend it.")
            elif state == "Active" and before == "Expiring":
                # Extended, here or on the unit's screen.
                if i in self._quiet:
                    self._quiet.discard(i)
                else:
                    say.append(f"Call with {who} extended.")
            elif state == "Active" and before not in CONNECTED:
                if self._uuid.get(i):
                    self._since[self._uuid[i]] = (time.monotonic(), True)
                if i in self._quiet:
                    self._quiet.discard(i)
                else:
                    say.append(f"Connected to {who}.")
            elif state == "Idle" and before in UP:
                self._ended[i] = self._ended.get(i, 0) + 1
                if i in self._quiet_end:
                    self._quiet_end.discard(i)
                else:
                    say.append(f"Call with {who} ended"
                               + (f": {end_reason(reason)}." if reason else "."))
