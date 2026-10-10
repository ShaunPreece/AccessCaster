"""CallMe: RODE's remote-guest calling, read, answered and announced from this side.

The protocol is section 8.2 of RODECASTER_PROTOCOL.txt and the traps are under CallMe in
PROJECT_GUIDE.md part 2. In short: everything lives on the SIPCALLING node and three call-slot
nodes, all root children, addressed from the live tree; an incoming call is PUSHED; answering is one
composite write. The touchscreen is the only other way to do any of it, which is why this exists.

THE WORDING LIVES HERE, like the recorder's: the server speaks it, the page shows it, and the
browser composes none of it.
"""

from __future__ import annotations

import datetime
import json
import threading
import time

import diagnostics
import model_tree
import rcp_hid
import rcp_read
import rcp_transfer
from wording import describe_duration

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

_SECRET_PROPS = ("sipAccountPassword", "sipNATTraversalPassword", "sipWebPassword")


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
    n = d.day
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix} {d.strftime('%B')}" + (f" {d.year}" if year else "")


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
# owner can say it. The server owns the stored list and hands it here with set_contacts.

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
    (which leaves the call alone) and the second raises it. Measured on a live call: (False, False)
    then (True, False) ends it as "Local drop"; (True, True) over (True, True) and (False, True)
    over (True, True) do nothing."""
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
        return {"ok": False, "message": "This unit does not publish CallMe."}, None, []
    return _state(p, slots, blob), ctl, slot_at


def _state(p: dict, slots: list, blob: bytes) -> dict:
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
            "slots": slots[:TIER_SLOTS.get(mode, len(slots))],
            # A call can be answered with CallMe on no fader, but the guest is then heard nowhere.
            "on_fader": CALLME1_SOURCE in rcp_read.values(blob, "channelInputSource")}


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
    now = time.time() if now is None else now
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
        parts.append("The unit is offline, so no call can connect.")
    elif d.get("hosting"):
        parts.append(f"Your unit's code is {spell(d.get('code') or '')}."
                     if d.get("code") else "Guests can call in.")
    else:
        parts.append("Incoming calls are off.")
    return " ".join(parts)


def readiness(d: dict) -> list:
    """What has to be in place before a call can work, as [{key, ok, text}], most basic first. The
    page shows only this, with a fix beside each item, until every item is ok.

    Offline, Join Call on the guest's page spins for a few seconds and gives up with nothing reaching
    the desk; with CallMe 1 on no fader it does nothing at all, and the desk never hears of the call.
    Neither shows on the desk or the guest's page. An unknown (None) is reported as not ok, never
    assumed fine. Hosting is not listed: with it off a call out still works, so it is a setting,
    not a fault."""
    out = []
    online = d.get("online")
    out.append({"key": "online", "ok": online is True,
                "text": "The unit is online." if online else
                        ("The unit is offline, so no call can connect either way. Join it to wifi "
                         "or plug in a network cable." if online is False else
                         "Whether the unit is online is not known yet.")})
    out.append({"key": "fader", "ok": d.get("on_fader") is True,
                "text": "CallMe 1 is on a fader." if d.get("on_fader") else
                        "CallMe 1 is on no fader, so a guest's call never reaches the unit. "
                        "Put it on a fader."})
    return out


def spell(code: str) -> str:
    """A code read out one character at a time, so a screen reader does not try it as a word."""
    return " ".join(code)


# ---------------------------------------------------------------------------------------------
# THE WATCHER: the pushes, heard on a handle of its own
# ---------------------------------------------------------------------------------------------

class Watcher(threading.Thread):
    """Listens for CallMe pushes on its own read-only HID handle and keeps the live state.

    ITS OWN HANDLE, LIKE THE RECORDER'S, and not a branch of the announcer: the announcer only runs
    while announcements are on, and a call has to be tracked whether or not anyone wants it spoken.
    Speech goes through `on_say`, which the server points at the announcer's queue, so the
    announcer switch still decides what is heard.

    SEEDED FROM A DUMP: the slot paths, and anything that changed while nobody was listening, only
    come from a model read. `read_model` is called each time the handle opens, so a desk that comes
    back from a replug is re-read before its pushes are trusted, and every full page read re-seeds
    it too (`seed`). A push that arrives before any seed is dropped."""

    def __init__(self, on_say=None, read_model=None, on_expiring=None, on_ring=None):
        super().__init__(daemon=True, name="callme-watch")
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
        self._state = None              # the read() shape, kept current from pushes
        self._ctl = None
        self._slots = []                # slot paths, in slot order
        self._handled = set()           # call ids answered or declined, whose clear is not news
        self._quiet = set()             # slot indices whose next "Connected" the reply speaks
        self._quiet_end = set()         # slot indices whose next "ended" the reply speaks
        self._since = {}                # call uuid -> (connected at, exact): see since_for
        # Who the last missed call was from, until the next call rings or goes up. Kept here, not
        # in _state, because every page read re-seeds _state from a dump, which has no memory.
        self._missed = None
        self.version = 0                # bumped on every change the page would show
        self.running = True

    # -- state ---------------------------------------------------------------------------------
    def seed(self, blob: bytes) -> None:
        try:
            d, ctl, slots = read_all(blob)
        except Exception:
            return
        if not d.get("ok"):
            return
        with self._lock:
            self._state = d
            self._ctl = ctl
            self._slots = slots
            # A call already up when first seen started at some unknown earlier time.
            for s in d.get("slots", []):
                if s.get("state") in CONNECTED and s.get("uuid") and s["uuid"] not in self._since:
                    self._since[s["uuid"]] = (time.time(), False)
            self.version += 1

    def quiet_connect(self, slot_index: int, on: bool) -> None:
        """Keep "Connected" unsaid for a call this app is answering, whose reply says it instead.
        Switched back off if the reply gives up waiting, so a late connection is still heard."""
        with self._lock:
            if on:
                self._quiet.add(slot_index)
            else:
                self._quiet.discard(slot_index)

    def snapshot(self):
        with self._lock:
            if self._state is None:
                return None
            d = json.loads(json.dumps(self._state))
            d["version"] = self.version
            d["missed"] = self._missed
            for s in d.get("slots", []):
                s["since"], s["since_exact"] = self._since.get(s.get("uuid"), (None, None))
            return d

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
            return self._slots[slot_index] if 0 <= slot_index < len(self._slots) else None

    def control_path(self):
        with self._lock:
            return self._ctl

    def note_handled(self, call_id) -> None:
        """A call this app answered or declined: the reply already said so, so the watcher must
        not say it again when the ringing clears."""
        with self._lock:
            self._handled.add(call_id)

    # -- one push -------------------------------------------------------------------------------
    def apply(self, path, name: str, value) -> None:
        """Fold one pushed property into the state and speak what deserves it."""
        say, ring = [], []
        with self._lock:
            d = self._state
            if d is None:
                return
            changed = True
            if path and self._ctl is not None and list(path) == list(self._ctl):
                changed = self._apply_control(d, name, value, say, ring)
            elif path in self._slots:
                changed = self._apply_slot(d, self._slots.index(path), name, value, say)
            else:
                changed = False
            if changed:
                self.version += 1
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

    def _apply_control(self, d, name, value, say, ring) -> bool:
        if name == "sipIncomingCallDetails":
            call = parse_call(value)
            was = d.get("incoming")
            d["incoming"] = call
            if call and (not was or was.get("id") != call["id"]):
                self._missed = None
                text = f"CallMe call from {call['name']}."
                if self._on_ring:
                    ring.append((call, text))
                else:
                    say.append(text)
            elif not call and was:
                # Cleared. An answer or decline from this app, or from the touchscreen (whose
                # accept push arrives first and marks it handled), has been spoken already. What is
                # left is a call nobody took: the 120 s ring ran out, or the guest gave up.
                if was["id"] not in self._handled:
                    self._missed = was["name"]
                    say.append(f"Missed CallMe call from {was['name']}.")
            return True
        if name == "sipIncomingCallAccept":
            try:
                d_acc = json.loads(composite_text(value) or "{}")
            except ValueError:
                return False
            call_id = d_acc.get("id")
            if call_id is not None and call_id not in self._handled:
                self._handled.add(call_id)
                inc = d.get("incoming") or {}
                who = inc.get("name") if inc.get("id") == call_id else "the guest"
                if d_acc.get("sip_slot", -1) < 0:
                    say.append(f"Declined the call from {who} on the unit.")
            return False
        if name == "sipCallHostingEnabled" and isinstance(value, bool):
            d["hosting"] = value
            if not value:
                d["code"], d["link"] = "", ""
            return True
        if name == "sipRodeCode":
            code = value if isinstance(value, str) else ""
            d["code"] = code
            d["link"] = GUEST_LINK + code if code else ""
            if code:
                d["hosting"] = True
            return True
        if name == "sipCallHostingToggleEnabled" and isinstance(value, bool):
            d["hosting_locked"] = not value
            return True
        if name == "sipRemainingCalltime":
            d["minutes_left"] = value
            return True
        if name == "sipRemainingWebCalltime":
            d["web_minutes_left"] = value
            return True
        return False

    def _apply_slot(self, d, i, name, value, say) -> bool:
        slots = d.get("slots") or []
        if i >= len(slots):
            return False
        s = slots[i]
        if name == "sipSlotCallAddress":
            s["peer"] = peer_name(value)
            return True
        if name == "sipSlotCallIsHost" and isinstance(value, bool):
            s["incoming"] = value
            return True
        if name == "sipSlotCallUUID":
            s["uuid"] = value if isinstance(value, str) else ""
            return True
        if name == "sipSlotCallState":
            state, reason = parse_slot_state(value)
            was = s.get("state")
            s["state"], s["reason"] = state, reason
            if state in UP:
                self._missed = None
            who = s.get("peer") or "the guest"
            if state == "Expiring" and was != "Expiring":
                if not (self._on_expiring and self._on_expiring(i)):
                    say.append(f"Call with {who} ends in five minutes unless you extend it.")
            elif state == "Active" and was == "Expiring":
                # Extended, here or on the unit's screen.
                if i in self._quiet:
                    self._quiet.discard(i)
                else:
                    say.append(f"Call with {who} extended.")
            elif state == "Active" and was not in CONNECTED:
                if s.get("uuid"):
                    self._since[s["uuid"]] = (time.time(), True)
                if i in self._quiet:
                    self._quiet.discard(i)
                else:
                    say.append(f"Connected to {who}.")
            elif state == "Idle" and was in UP:
                s["ended"] = s.get("ended", 0) + 1
                if i in self._quiet_end:
                    self._quiet_end.discard(i)
                else:
                    say.append(f"Call with {who} ended"
                               + (f": {end_reason(reason)}." if reason else "."))
            if state not in CONNECTED:
                s.update(quality=None, bitrate_kbps=None, jitter_ms=None, packet_loss=None)
            return True
        if s.get("state") not in CONNECTED:
            return False
        if name == "sipSlotCallQuality":
            s["quality"] = value
        elif name == "sipSlotCallBitrate" and isinstance(value, float):
            s["bitrate_kbps"] = round(value / 1000)
        elif name == "sipSlotCallJitter" and isinstance(value, float):
            s["jitter_ms"] = round(value * 1000, 1)
        elif name == "sipSlotCallPacketLoss" and isinstance(value, float):
            s["packet_loss"] = value
        else:
            return False
        return True

    # -- the thread ----------------------------------------------------------------------------
    def run(self) -> None:
        dev = None
        while self.running:
            if dev is None:
                iface = rcp_transfer.find_control_interface(fresh=True)
                if iface is None:
                    time.sleep(2.0)
                    continue
                try:
                    dev = rcp_hid.open_handle(iface, "callme watcher")
                except Exception as exc:
                    dev = None
                    diagnostics.event("device.open_failed", "callme watcher", error=f"{exc}")
                    time.sleep(2.0)
                    continue
                # The dump is taken AFTER the handle opens, so nothing pushed meanwhile is lost: it
                # queues on this handle and is folded in on top of the seed.
                if self._read_model is not None:
                    try:
                        self.seed(self._read_model())
                    except Exception as exc:
                        diagnostics.event("callme.seed_failed", "callme watcher", error=f"{exc}")
            try:
                rep = dev.read(256, timeout_ms=250)
            except Exception:
                rcp_hid.close_handle(dev, "callme watcher")
                dev = None
                time.sleep(0.5)
                continue
            if not rep or rep[0] != 0x04 or b"sip" not in bytes(rep):
                continue
            try:
                rec = rcp_read.parse_push(bytes(rep))
                if rec is None:
                    continue
                path, name, value, _k = rec
                if name.startswith("sip") and name not in _SECRET_PROPS:
                    self.apply(path, name, value)
            except Exception:
                pass          # one undecodable packet costs one update, never the thread
        if dev is not None:
            rcp_hid.close_handle(dev, "callme watcher")
