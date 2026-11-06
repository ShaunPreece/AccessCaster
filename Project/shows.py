"""AccessCaster - the DESK'S OWN show backup: export, import, factory reset, delete.

WHAT A SHOW IS. The whole desk state as the firmware itself stores it: every setting, the fader
layout, the routing, and the SMART pads with their audio. An export writes about 24 MB onto the
recordings drive in roughly a second, and an import puts all of it back in a few seconds. That is
faster and MORE COMPLETE than our own profiles, which can only put back the properties this app
knows how to write.

WHY BOTH STILL EXIST, since the obvious question is why we do not just delete profiles:
  * A show is a black box. It lives on the desk's drive, cannot be read, edited or diffed, cannot be
    copied to another desk by us, and cannot restore ONE part of itself.
  * A profile is JSON on this PC. It is portable, inspectable, partial-restorable, and it carries a
    redacted snapshot of the whole model for the record.
So: a show is the fast, complete round trip; a profile is the readable, selective one. The Profiles
page offers both and says which is which.

**ALL FOUR WRITES ARE CONFIRMED, on a Pro II with the recordings drive in:**

  * EXPORT puts a new SHOW node on the drive in about a second, at the next snapshot index, stamped
    with the wall clock, and renumbers every uid beneath it (trap 1).
  * DELETE removes a named backup and gives its free space back.
  * RESET mints a new show uuid, which is what makes the backup importable again.
  * IMPORT puts the desk back: against a profile snapshot taken just before a reset, the only
    differences are the new show node's own metadata and the drive's free space, and every SMART
    pad matches to the leaf value, FX stacks, envelopes and audio files included.

THE COMMANDS. All live in SHOWCONTROL and are written at the two-byte address
`01 01 01 02 8e 02`:
  * `showControlExport` = True         -> a new show file on the recordings drive.
  * `showControlImport` = <uid>        -> load that show. Restores everything, pads included.
  * `showControlNewFromDefault` = True -> THE SOFTWARE FACTORY RESET. A fresh default show with a
    NEW uuid, so an export taken beforehand can be imported straight back as the undo.
  * `showControlDelete` = <uid>        -> remove one show file and give the space back.

FOUR TRAPS:

1. **`showUID` IS POSITIONAL AND IS REASSIGNED ON EVERY EXPORT.** The newest show is always uid 0
   and everything else shuffles down. So a uid held by a page goes stale the moment anybody exports,
   and every command here resolves the uid from a FRESH read taken immediately before the write.
   `read_shows` is deliberately cheap to call for exactly this reason.

2. **`showUUID` IDENTIFIES THE SHOW, NOT THE FILE, so it is NOT a unique handle.** Three exports
   of the same desk state produce three SHOW nodes carrying the SAME uuid, so keyed on the uuid a
   second export is undetectable and a delete can never verify itself.

   **`showSnapshotIndex` IS THE STABLE PER-FILE HANDLE.** Exporting a third show renumbers every
   uid (the file at uid 0 moves to uid 1) while every snapshot index stays put, and the new export
   takes the next index up: a monotonically increasing export counter, which is what this module
   keys on. It is resolved to a uid against a fresh read at the moment of the write, because the
   PROTOCOL only accepts a uid.

3. **AN IMPORT DOES *NOT* REFUSE A SHOW WHOSE UUID MATCHES THE LOADED ONE.** A uuid changes only on
   a factory reset, so EVERY backup shares the loaded uuid, and refusing on that basis would make
   shows useless for the ordinary case: back up, change something, put it back. Such an import puts
   a drifted setting back inside four seconds, with `showControlLastError` staying 0.

   `showControlLastError` = 1 IS real but is not a uuid clash. What provokes it is
   unknown, so the app sends the write and REPORTS the unit's refusal rather than predicting it.

   **The consequence for verification is the whole of `restore_fingerprint`.** With the uuid already
   equal to the target, "has the uuid become the target" is true before anything happens, so the
   success test has to watch the desk STATE instead.

4. **EVERY COMMAND FIELD SELF-CLEARS**, so a write can never confirm itself. Nothing in this module
   reports success from a write returning cleanly. The caller reads the SHOWS list (or the current
   show's uuid) back and reports what it finds - the same discipline as deleting a recording.
   The idle value is -1, not 0, and 0 is a legitimate uid, so never test one for the other.

`showLastModification` is a 64-bit int that reads as epoch milliseconds; `_describe_modified`
renders it in this computer's local time, the same way the recordings list does.

AN IMPORT AND A RESET BOTH MOVE THE MONITOR LEVEL (0.25 -> 0.95 measured on an import). Any UI
offering either must warn first; see docs/PROJECT_GUIDE.md.
"""
from __future__ import annotations

import datetime
import time

import desk_model
import diagnostics
import rcp_read
import rcp_transfer
import wording

# The two blocks. SHOWS holds one SHOW node per exported file; SHOWCONTROL
# (`rcp_transfer.SHOWCONTROL`) holds the commands.
BLOCK_SHOW = "SHOW"

# `showControlLastError` codes. CODE 1's CAUSE IS NOT KNOWN, and it is NOT "the uuid matches the
# loaded show", which is disproven: an import of a backup carrying the loaded uuid succeeds
# (trap 3). So its wording says the unit refused it and claims no cause. Anything with no entry here
# is reported by NUMBER rather than guessed at, because inventing wording for an unknown error is
# how a user chases the wrong problem.
#
# A REFUSAL STICKS UNTIL THE DESK IS POWER-CYCLED (Pro II, fw 1.7.6). After one, every import is
# refused, a fresh backup of the loaded state included, and restoring a different backup does not
# clear it; after a power cycle the same import restores. The CODE outlives the power cycle too: it
# reads 1 until the next import succeeds, which clears it to 0. So a non-zero code read BEFORE an
# import is not evidence about that import (see show_actions.action_show_import).
ERROR_REFUSED = 1

# CODE 8 IS "NOWHERE TO PUT IT": an export asked of a Duo with no card and no drive in it sets
# `showControlLastError` to 8 within a second, clears `showControlExport` to False, and creates no
# show. A full or unwritable volume may report the same code, so the wording covers that too.
ERROR_NO_STORAGE = 8
ERROR_TEXT = {
    ERROR_REFUSED: ("The desk refused to restore that backup, and does not say why. Once it has "
                    "refused one, it refuses every restore until it is restarted: switch it off "
                    "and on again with a firm press of the red button on the back, then try "
                    "again."),
    ERROR_NO_STORAGE: ("The desk has nowhere to put a backup. Put a memory card in, or a USB drive "
                       "in the second USB port, and try again. If storage IS in, it may be full or "
                       "the desk may not be able to write to it."),
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

    NEWEST FIRST, sorted on the snapshot index rather than on stream order. Unlike recordings, a
    real dump lists shows uid 0 (newest) first, so copying recorder.read_recordings' reversal puts
    the oldest at the top. Sorting on the monotonic export counter does not depend on the
    firmware's emission order at all.
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
            return wording.spoken_datetime(datetime.datetime.fromtimestamp(seconds))
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
                    or rcp_read.first(blob, "currentShowName") or "").strip(),
        "uuid": str(rcp_read.block_value(blob, "CURRENTSHOW", "currentShowUUID")
                    or rcp_read.first(blob, "currentShowUUID") or ""),
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


# ---------------------------------------------------------------------------
# Writing. One command field each, and NOT ONE OF THEM VERIFIES ITSELF (trap 4).


def _send_bool(name: str, on: bool) -> None:
    rcp_transfer.write_at(rcp_transfer.SHOWCONTROL, name, "bool", bool(on))


def _send_int(name: str, value: int) -> None:
    rcp_transfer.write_at(rcp_transfer.SHOWCONTROL, name, "int", int(value))


def send_export() -> None:
    """Ask the unit to write the current state to a new show file on the recordings drive.

    EVERY EXPORT MAKES A NEW COPY rather than replacing the last one, and each is about 24 MB. So a
    UI that offers this must also offer the delete, or the drive fills up quietly."""
    _send_bool("showControlExport", True)


def send_import(uid: int) -> None:
    """Load an exported show. Resolve `uid` from a read taken moments ago (trap 1).

    The desk rebuilds itself from the show, and whether it pushes every change it makes is
    unmeasured, so the desk model is untrusted until it is read again."""
    _send_int("showControlImport", int(uid))
    desk_model.distrust()


def send_new_from_default() -> None:
    """THE SOFTWARE FACTORY RESET: settings and pads back to factory, with a new uuid.

    Not the same as the unit's own factory reset, and the difference is the whole reason this is the
    one we offer: the touchscreen reset drops the desk at a SETUP WIZARD that cannot be driven by the
    encoder, which strands a blind operator until somebody sighted taps through it. This does not.

    Wifi, language and the analytics answer live OUTSIDE the show and survive it. The desk model
    is untrusted until it is read again, as after `send_import`."""
    _send_bool("showControlNewFromDefault", True)
    desk_model.distrust()


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
    # Resolved here rather than as argument defaults, which bind at import and would ignore a later
    # change to COMMAND_TIMEOUT_S (the simulation suite tunes it).
    read = read or (lambda: rcp_read.read_model_stable(fresh=True))
    timeout = COMMAND_TIMEOUT_S if timeout is None else timeout
    deadline = time.monotonic() + timeout
    blob = None
    while time.monotonic() < deadline:
        time.sleep(POLL_S)
        try:
            fresh_blob = read()
        except Exception as exc:
            diagnostics.event("shows.read_failed", "still waiting", error=str(exc))
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
    return (f"The desk refused it and reported error {int(code)}, which this app has not seen "
            f"before and cannot explain.")
