"""AccessCaster - the DESK'S OWN show backup: export, import, factory reset, delete.

WHAT A SHOW IS. The whole desk state as the firmware itself stores it: every setting, the fader
layout, the routing, and the SMART pads with their audio. An export writes about 24 MB onto the
recordings drive in roughly a second, and an import puts all of it back in a few seconds. That is
faster and MORE COMPLETE than our own profiles, which can only put back the 148 properties this app
knows how to write (see ProjectMemory/PROFILE_COVERAGE_<DESK>.md).

WHY BOTH STILL EXIST, since the obvious question is why we do not just delete profiles:
  * A show is a black box. It lives on the desk's drive, cannot be read, edited or diffed, cannot be
    copied to another desk by us, and cannot restore ONE part of itself.
  * A profile is JSON on this PC. It is portable, inspectable, partial-restorable, and it carries a
    redacted snapshot of the whole model for the record.
So: a show is the fast, complete round trip; a profile is the readable, selective one. The Profiles
page offers both and says which is which.

**ALL FOUR WRITES ARE PROVEN, on a Pro II with the recordings drive in.** Two
full reset-and-restore cycles were driven from this module alone, with nobody at the touchscreen:

  * EXPORT put a new SHOW node on the drive in about a second, at the next snapshot index, stamped
    with the wall clock, and renumbered every uid beneath it exactly as trap 1 predicts.
  * DELETE removed a named backup; it was gone from a fresh read and the free space came back.
  * RESET minted a new show uuid, which is what makes the backup importable again.
  * IMPORT put the desk back. The second cycle came back with ZERO real differences against a
    profile snapshot taken moments before the reset - every diff was the new show node's own
    metadata or the drive's free space - and all 16 SMART pads matched to the leaf value, FX stacks,
    envelopes and audio files included.

One deviation, and it is NOT the round trip: Mic 3's row of 13 mix cross-points creeps on its own
across sessions (0, then 1/127, then 9/127, then 23/127) and holds steady through a cycle.
Unexplained, harmless, and nothing to do with shows.

BEWARE A PROOF THAT EXPORTS AT THE TOUCHSCREEN and reads the tree afterwards: it proves the traps
below and nothing at all about our own writes. The loop above is driven entirely from this module.

THE COMMANDS AS RECORDED. All live in SHOWCONTROL and are written at the two-byte address
`01 01 01 02 8e 02`:
  * `showControlExport` = True         -> a new show file on the recordings drive.
  * `showControlImport` = <uid>        -> load that show. Restores everything, pads included.
  * `showControlNewFromDefault` = True -> THE SOFTWARE FACTORY RESET. A fresh default show with a
    NEW uuid, so an export taken beforehand can be imported straight back as the undo.
  * `showControlDelete` = <uid>        -> remove one show file and give the space back.

FOUR TRAPS, each measured rather than assumed:

1. **`showUID` IS POSITIONAL AND IS REASSIGNED ON EVERY EXPORT.** The newest show is always uid 0
   and everything else shuffles down. So a uid held by a page goes stale the moment anybody exports,
   and every command here resolves the uid from a FRESH read taken immediately before the write.
   `read_shows` is deliberately cheap to call for exactly this reason.

2. **`showUUID` IDENTIFIES THE SHOW, NOT THE FILE, so it is NOT a unique handle.** Measured on
   hardware: three exports of the same desk state produced three SHOW nodes all
   carrying the SAME uuid. An early version of this module used the uuid as the handle precisely
   because the uid was known to be positional, and it was wrong in a way that only showed up against
   a real drive - a second export became undetectable, and a delete could never verify itself.

   **`showSnapshotIndex` IS THE STABLE PER-FILE HANDLE**, and that is measured too, in the same run:
   exporting a third show renumbered every uid (the file at uid 0 moved to uid 1) while every
   snapshot index STAYED PUT, and the new export took the next index up. So snapshot index is a
   monotonically increasing export counter, and it is what this module keys on. It is resolved to a
   uid against a fresh read at the moment of the write, because the PROTOCOL only accepts a uid.

3. **AN IMPORT DOES *NOT* REFUSE A SHOW WHOSE UUID MATCHES THE LOADED ONE**, and believing it does
   is expensive: since a uuid changes only on a factory reset, EVERY backup shares the loaded uuid,
   so refusing on that basis makes shows useless for the ordinary case - back up, change something,
   put it back.

   Measured twice on hardware: with the desk deliberately drifted, importing a
   backup carrying the loaded uuid restored Mic 4's gain from 30 to 50, and again from 42 to 50,
   inside four seconds, with `showControlLastError` staying 0.

   `showControlLastError` = 1 IS real (seen twice) but it is not the uuid clash
   it was taken for. What actually provokes it is unknown, so the app sends the write and REPORTS
   the unit's refusal rather than predicting it.

   **The consequence for verification is the whole of `restore_fingerprint`.** With the uuid already
   equal to the target, "has the uuid become the target" is true before anything happens, so the
   success test has to watch the desk STATE instead.

4. **EVERY COMMAND FIELD SELF-CLEARS**, so a write can never confirm itself. Nothing in this module
   reports success from a write returning cleanly. The caller reads the SHOWS list (or the current
   show's uuid) back and reports what it finds - the same discipline as deleting a recording.
   The idle value is -1, not 0, and 0 is a legitimate uid, so never test one for the other.

`showLastModification` NOW DECODES: it is a 64-bit epoch in milliseconds. It read as nothing until
the tree parser went in, because a reader that only takes a one-byte length
and could not see a 64-bit type. So a backup can be dated after all, and `_describe_modified` renders
it in this computer's local time, the same way the recordings list does.

AN IMPORT AND A RESET BOTH MOVE THE MONITOR LEVEL (0.25 -> 0.95 measured on an import). Any UI
offering either must warn first; see docs/PROJECT_GUIDE.md.
"""
from __future__ import annotations

import time

import rcp_read
import rcp_transfer

# The two blocks. SHOWS holds one SHOW node per exported file; SHOWCONTROL holds the commands.
# Both are past what the one-byte global address can carry, so writes use the two-byte channel form
# (`01 01 01 02 <lo> <hi>`), exactly as recorder.build_delete_request does for RECORDINGS 0x28F.
# 0x28E is PROVEN on hardware, and again through the whole export / import / reset loop; RECORDINGS
# at 0x28F is anchored one slot above it.
SEL_SHOWCONTROL = 0x28E
BLOCK_SHOW = "SHOW"

# `showControlLastError` codes. CODE 1's CAUSE IS NOT KNOWN, and it is NOT "the uuid matches the
# loaded show", which is disproven: an import of a backup carrying the loaded uuid succeeds
# (trap 3). So its wording says the unit refused it and claims no cause. Anything with no entry here
# is reported by NUMBER rather than guessed at, because
# inventing wording for an unknown error is how a user chases the wrong problem.
ERROR_REFUSED = 1
ERROR_UUID_CLASH = ERROR_REFUSED     # old name, kept so existing callers and tests still resolve

# CODE 8 IS "NOWHERE TO PUT IT", and unlike code 1 the circumstance IS known, because it was
# provoked on purpose: an export asked of a Duo with no card and no drive in it.
# The desk answered within ONE SECOND - `showControlLastError` 0 -> 8, `showControlExport` cleared
# back to False, no show created, and nothing else in the whole tree moved. That is also the proof
# the SHOWCONTROL address is LIVE on a Duo, because the desk did something we did not send.
#
# THE WORDING NAMES THE CIRCUMSTANCE, NOT A CAUSE WE GUESSED, which is the same rule code 1 follows.
# Missing storage is what was measured; a full or unwritable volume could plausibly report the same
# code, so the text covers that too rather than insisting the drive is absent.
ERROR_NO_STORAGE = 8
ERROR_TEXT = {
    ERROR_REFUSED: ("The unit refused to restore that backup. It does not say why, and the one "
                    "thing that reliably clears it is restoring a different backup."),
    ERROR_NO_STORAGE: ("The unit has nowhere to put a backup. Put a memory card in, or a USB drive "
                       "in the second USB port, and try again. If storage IS in, it may be full or "
                       "the unit may not be able to write to it."),
}

# How long to wait for a command to finish. An export measured about a second and an import a few,
# so this is generous; the wait ends as soon as the evidence arrives rather than running the clock
# down. A reset is the slowest of the three.
COMMAND_TIMEOUT_S = 40.0
POLL_S = 1.5


# ---------------------------------------------------------------------------
# Reading

def read_shows(blob: bytes) -> list:
    """Every exported show on the drive, newest first.

    Read BY BLOCK rather than by pairing `showUID` with `showName` positionally. Positional pairing
    is what recorder.read_recordings does and it is fine there, but a SHOW node carries four fields
    and there is no guarantee a future firmware emits them in a fixed order or fills them all in.
    Grouping on the block boundary cannot mis-pair a name with someone else's uid.

    NEWEST FIRST, sorted on the snapshot index rather than on stream order. The first draft copied
    recorder.read_recordings and simply reversed the stream, on the inherited belief that the unit
    lists oldest first. IT DOES NOT: a real dump comes back uid 0 (newest) first, so reversing put
    the oldest at the top and labelled it "the newest". Sorting on the monotonic export counter
    cannot be wrong either way round, and does not depend on the firmware's emission order at all.
    """
    out: list = []
    current: dict = {}
    for hdr, name, value, _kind in rcp_read.iter_block_properties(blob):
        if hdr != BLOCK_SHOW:
            continue
        if name in current:            # a repeated field means the next SHOW node has started
            out.append(current)
            current = {}
        current[name] = value
    if current:
        out.append(current)

    shows = [_describe_show(s) for s in out]
    # A node with no readable snapshot index sorts last rather than crashing the sort or jumping to
    # the top: it is the one entry we cannot place, so it must not claim to be the newest.
    shows.sort(key=lambda s: (s["snapshot"] is None, -(s["snapshot"] or 0)))
    return shows


def _describe_show(raw: dict) -> dict:
    """One SHOW node, worded for reading aloud.

    `snapshot` is the HANDLE the rest of the app holds (trap 2); `uid` is only good for the moment
    it was read, and is resolved again at the point of the write."""
    uid = raw.get("showUID")
    snapshot = raw.get("showSnapshotIndex")
    name = str(raw.get("showName") or "").strip()
    return {
        "uid": int(uid) if isinstance(uid, int) else None,
        "snapshot": int(snapshot) if isinstance(snapshot, int) else None,
        "name": name or "Unnamed show",
        "uuid": str(raw.get("showUUID") or ""),
        "modified": _describe_modified(raw.get("showLastModification")),
    }


def find_by_snapshot(show_list: list, snapshot: int):
    """The show with this snapshot index, or None. The ONE way to turn a held handle into a show."""
    for show in show_list:
        if show.get("snapshot") == snapshot:
            return show
    return None


def _describe_modified(value) -> str:
    """When the show was last written, if the unit gave us something we can read.

    THE FORMAT IS NOT PROVEN. `showLastModification` has only ever been seen on this desk's own
    exports and nobody has checked it against a known wall clock the way `recordingContent`'s
    timestamp was checked (recorder.parse_recording). So an epoch-looking number is rendered as one
    and ANYTHING ELSE IS PASSED THROUGH AS TEXT rather than being coerced into a date that might be
    an hour, a day or a decade out. A wrong date read aloud with confidence is worse than no date.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, int):
        # Milliseconds since the epoch is what the recordings list uses, so it is the likely shape.
        # Anything outside a plausible range is left as a bare number instead.
        try:
            seconds = value / 1000.0 if value > 10_000_000_000 else float(value)
            if not (946_684_800 < seconds < 4_102_444_800):     # 2000 to 2100
                return str(value)
            stamp = time.localtime(seconds)
            return time.strftime("%d %B %Y at %H:%M", stamp)
        except (ValueError, OSError, OverflowError):
            return str(value)
    return str(value)


def read_current(blob: bytes) -> dict:
    """The show the desk has loaded right now.

    IT LIVES IN `CURRENTSHOW`, NOT `SHOWCONTROL`. Asking SHOWCONTROL first only works because it
    falls through to a whole-dump search, which makes the fallback the load path. Name the right
    block and the fallback stays a fallback.
    """
    return {
        "name": str(rcp_read.block_value(blob, "CURRENTSHOW", "currentShowName")
                    or _first(blob, "currentShowName") or "").strip(),
        "uuid": str(rcp_read.block_value(blob, "CURRENTSHOW", "currentShowUUID")
                    or _first(blob, "currentShowUUID") or ""),
    }


# The properties an import is certain to put back if the desk differs from the backup. Used as the
# EVIDENCE that an import acted, because nothing else can serve: see restore_fingerprint.
_FINGERPRINT_NAMES = ("inputMicrophoneGain", "faderLevel", "outputMonLevel", "channelInputSource",
                      "mixLevelWithAnchor", "inputDigitalGain", "padName")


def restore_fingerprint(blob: bytes) -> tuple:
    """A cheap signature of the state an import restores, for telling ACTED from DID NOTHING.

    WHY THIS EXISTS. Every other candidate fails. The command field self-clears, so a clean write
    proves nothing (part 2 of the guide: consumed means it acted, stuck means it did not - but the
    idle value is -1 both BEFORE we send and after it is consumed, so a poll that misses the ~4 s
    window cannot tell the two apart). The loaded show's UUID does not move either: it changes only
    on `newFromDefault`, so in the ordinary case - export, change something, restore - the uuid is
    ALREADY the target and a uuid test reports success instantly whether or not anything happened.
    That was a real bug, not a hypothetical: it shipped that way.

    So we watch the STATE instead. If the desk differs from the backup, an import moves at least one
    of these; if it does not move, the desk already matched, which is a true and reportable outcome
    rather than a failure. Deliberately a handful of names rather than the whole dump: free space,
    the shows list and the self-clearing mix flags all churn on their own and would report an effect
    where there was none.
    """
    return tuple(tuple(rcp_read.values(blob, name)) for name in _FINGERPRINT_NAMES)


def read_last_error(blob: bytes):
    """`showControlLastError`, or None. Read BY BLOCK: a stray of the same name costs nothing to
    ignore that way and cannot fool the read."""
    return rcp_read.block_value(blob, "SHOWCONTROL", "showControlLastError")


def _first(blob: bytes, name: str):
    """First meaningful value anywhere in the dump, for the names that are not block-duplicated."""
    for v in rcp_read.values(blob, name):
        if v is not None and v != "":
            return v
    return None


def describe(blob: bytes) -> dict:
    """Everything the Shows section of the page needs, from one dump."""
    shows = read_shows(blob)
    current = read_current(blob)
    return {
        "shows": shows,
        "current": current,
        # A show whose uuid matches the loaded one cannot be imported (trap 2). Marking it here
        # means the page can say so on the control itself rather than after a failed attempt.
        "count": len(shows),
    }


# ---------------------------------------------------------------------------
# Writing. One command field each, and NOT ONE OF THEM VERIFIES ITSELF (trap 3).


def _send_bool(name: str, on: bool) -> None:
    rcp_transfer.send_record(rcp_transfer.build_channel_prop_bool(
        name, bool(on), SEL_SHOWCONTROL & 0xFF, SEL_SHOWCONTROL >> 8))


def _send_int(name: str, value: int) -> None:
    rcp_transfer.send_record(rcp_transfer.build_channel_prop_int(
        name, int(value), SEL_SHOWCONTROL & 0xFF, SEL_SHOWCONTROL >> 8))


def send_export() -> None:
    """Ask the unit to write the current state to a new show file on the recordings drive.

    EVERY EXPORT MAKES A NEW COPY rather than replacing the last one, and each is about 24 MB. So a
    UI that offers this must also offer the delete, or the drive fills up quietly."""
    _send_bool("showControlExport", True)


def send_import(uid: int) -> None:
    """Load an exported show. Resolve `uid` from a read taken moments ago (trap 1)."""
    _send_int("showControlImport", int(uid))


def send_new_from_default() -> None:
    """THE SOFTWARE FACTORY RESET: settings and pads back to factory, with a new uuid.

    Not the same as the unit's own factory reset, and the difference is the whole reason this is the
    one we offer: the touchscreen reset drops the desk at a SETUP WIZARD that cannot be driven by the
    encoder, which strands a blind operator until somebody sighted taps through it. This does not.

    Wifi, language and the analytics answer live OUTSIDE the show and survive it."""
    _send_bool("showControlNewFromDefault", True)


def send_delete(uid: int) -> None:
    """Remove one show file. Resolve `uid` from a read taken moments ago (trap 1)."""
    _send_int("showControlDelete", int(uid))


# ---------------------------------------------------------------------------
# Waiting for the evidence, since no command reports its own result.

def wait_for(check, timeout: float | None = None, read=None):
    """Poll a fresh model until `check(blob)` returns something truthy, or time runs out.

    Returns (result, blob) with result None on a timeout, so the caller can still report what the
    desk looked like when we gave up rather than saying only "no".

    `read` IS HOW THE HID LOCK STAYS OUT OF THE SLEEP. The caller passes a function that takes the
    lock, dumps, and releases it, so the lock is held for the ~1.5 s of each dump and NOT for the
    1.5 s of waiting in between. Holding it across the whole wait would have blocked the announcer
    and every other read for up to forty seconds while this function did nothing but sleep, which
    presents as the app freezing. Defaults to a bare read for callers that hold no lock (tests).
    """
    # Both resolved HERE rather than as argument defaults, which bind at import and so cannot be
    # tuned afterwards: `timeout=COMMAND_TIMEOUT_S` in the signature made the constant look
    # adjustable while silently ignoring any change to it.
    read = read or (lambda: rcp_read.read_model_stable(fresh=True))
    timeout = COMMAND_TIMEOUT_S if timeout is None else timeout
    deadline = time.time() + timeout
    blob = None
    while time.time() < deadline:
        time.sleep(POLL_S)
        try:
            fresh_blob = read()
        except Exception:
            continue
        # A NON-DUMP IS NOT AN ANSWER, and `check` must never see one. A desk that has stopped
        # answering returns zero bytes rather than raising, and every check here asks whether
        # something CHANGED - which an empty blob does, against anything, so an import reports
        # success with the desk already off the bus. Returning (None, None) on a desk that never
        # answered is the honest outcome, and the caller can say so.
        if not rcp_read.is_model_dump(fresh_blob):
            continue
        blob = fresh_blob
        got = check(blob)
        if got:
            return got, blob
    return None, blob


def error_message(code) -> str:
    """Wording for a `showControlLastError`, never invented for a code we have not seen."""
    if not code:
        return ""
    text = ERROR_TEXT.get(int(code))
    if text:
        return text
    return (f"The unit refused it and reported error {int(code)}, which this app has not seen "
            f"before and cannot explain.")
