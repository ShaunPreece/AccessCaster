"""The recorder: start, stop, pause, where it is recording to, and what is already on the drive.

WHY THIS IS THE ACCESSIBILITY WIN IT IS. Pressing REC on the unit tells a blind operator nothing:
there is no spoken confirmation, and the two things that stop a recording happening (no destination,
or a drive with no room) are only visible on the touchscreen. So an operator could talk for an hour
into a unit that never started. This module makes the transport writable from software and the state
readable, and the state arrives as a PUSH, so a press on the unit itself is caught too.

WHAT IS PROVEN ON HARDWARE (DevTools/test_recorder_live.py):
  * `requestRecordState` on RECORDER selector 0x03 is the command. 2 = record, 1 = pause, 0 = stop.
    It is a REQUEST: the firmware consumes it and resets it to 3, so reading it back tells you
    nothing about whether you are recording. Read `recordState` for that.
  * `recordState` is the RESULT: 0 ready, 1 paused, 2 recording, 3 no destination. It read 3 for
    this whole project, which is why the recorder looked inert; it dropped to 0 the moment a drive
    was mounted.
  * PAUSE REALLY PAUSES, and it is measured rather than assumed. A take of 14.1 s wall-clock with a
    3.2 s pause in the middle produced a 10.8 s file: the paused seconds are not in the recording.
  * The desk PUSHES `recordState` on report 0x04 with selector 0x03 within ~30 ms of the change,
    whether the change came from us or from the unit's own REC button. That is what the Watcher
    below listens to, and it is why the app can report the state without polling a 1.5 s model dump.

WHAT IS NOT HERE, and why, so it is not re-planned:
  * **Elapsed time from the unit.** `recordTimeMs` stays 0 right through a recording, in the dump
    and in the push stream. So elapsed time is COUNTED HERE, from the state changes we saw, and is
    honestly reported as unknown when we did not see the start.
  * **Deleting a recording.** `requestDeleteUID` sits in the RECORDINGS block, and the selector
    model puts that block at 0x28E - past what a one-byte selector in the global record shape can
    address. So it is not a rule we are choosing to obey, it is an address we cannot write. Nothing
    to try until a wider address form turns up.
  * **Deleting a recording**, above, is still the only thing in this file that cannot be done.

MARKERS WORK - PROVEN BY READING THE DRIVE ON A PC, which is the only place the
evidence exists. `requestDropMarker` (RECORDER 0x03) writes a standard WAV cue point into the
recording. Nothing over USB shows it: the desk consumes the write and pushes the field back
cleared, and the recording's own record carries identical flags either way, which is why this was
held back as unverifiable until the pendrive was moved to a computer. The take that settled it had
two markers at exactly 4.0 s and 8.0 s, where they were dropped, and the take beside it with no
markers had no cue chunk at all. Both the int and the double form of the write land; the double is
used here because that is the type the desk's own echo comes back as.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

import rcp_read
import rcp_transfer

# RECORDER block. Read straight off the wire (recordState and requestRecordState both
# arrived carrying this selector), so it is a confirmed address, not a derived one.
SEL_RECORDER = 0x03

READY = 0
PAUSED = 1
RECORDING = 2
NO_DESTINATION = 3

# What each state means to someone who cannot see the unit. These are the words the page and the
# announcer both use, so the app cannot describe the same state two different ways.
STATE_LABEL = {
    READY: "Ready to record",
    PAUSED: "Paused",
    RECORDING: "Recording",
    NO_DESTINATION: "No recording destination",
}
STATE_SPOKEN = {
    READY: "Recording stopped",
    PAUSED: "Recording paused",
    RECORDING: "Recording started",
    NO_DESTINATION: "Recording stopped: the unit has nowhere to record to",
}


def spoken_change(was, now, medium: str = "") -> str:
    """What to say when the state MOVES from `was` to `now`, or "" to stay quiet.

    WHY THIS IS NOT `STATE_SPOKEN[now]`. That map answers "what is the desk doing", which is a
    different question from "what just happened", and using it for the second one says the opposite
    of the truth: inserting storage moves the desk NO_DESTINATION -> READY, and READY's word for
    itself is "Recording stopped", so an operator putting a drive in to START recording is told
    recording has stopped, and one pulling it out is told the same thing.

    THE PLUG IS FULLY IDENTIFIABLE FROM THE PAIR OF STATES, with no extra device read, no second
    push to correlate and no watching the filesystem. That is because NO_DESTINATION is a STORAGE
    state and not a transport one: the desk can only sit in it with nothing to record to, so
    crossing that boundary IS the card or drive going in or out.

        3 -> 0    storage arrived                     "Recording now available"
        0 -> 3    storage removed while idle          "Recording no longer available"
        2/1 -> 3  storage removed MID-TAKE            a stop, and it says WHY, because this one
                                                      cost the operator a recording
        2/1 -> 0  the transport genuinely stopped     "Recording stopped"
        1 -> 2    resumed from a pause                "Recording again", not "started"

    `medium` is the destination's spoken name ("USB drive", "memory card") when the caller knows it,
    and it is only ever used for a REMOVAL, where the last known name is the thing that just left.
    An insertion deliberately does not name what arrived: the state push carries no such name, and
    the caller's last known one belongs to whatever was pulled out before it.

    `was` of None is NOT a change - it is the first state the app has ever seen - and announcing it
    is how an app greets you with "Recording stopped" on startup."""
    if now is None or was is None or was == now:
        return ""
    if now == RECORDING:
        return "Recording again" if was == PAUSED else STATE_SPOKEN[RECORDING]
    if now == PAUSED:
        return STATE_SPOKEN[PAUSED]
    if now == READY:
        # The whole point of this function: from NO_DESTINATION nothing was recording, so nothing
        # stopped. Somewhere to record to just appeared.
        return "Recording now available" if was == NO_DESTINATION else STATE_SPOKEN[READY]
    if now == NO_DESTINATION:
        gone = f"the {medium} was removed" if medium else ""
        if was in (RECORDING, PAUSED):
            # A take was running and the storage went away, which is the one case where a plug
            # really did stop a recording. Never softened into "no longer available".
            return f"Recording stopped: {gone}" if gone else STATE_SPOKEN[NO_DESTINATION]

        return ("Recording no longer available: " +
                (gone or "the unit has nowhere to record to"))
    return ""

# MULTITRACK, named for what it DOES rather than for the unit's own word for it.
#
# `recordingMultitrackMode` really is the RECORDER's setting and not the USB stream's, MEASURED:
# two null results and one decisive one. `recordBytesPerSecond` reads 384000 in all three modes and
# never moves, the Main Multitrack USB endpoint stays absent in all three, but a 10-second take uses
# 3.50 MB at Off and 26.25 MB at Post-Fader - **7.2 times the data on the drive**. So it must never
# be labelled "Multitrack USB recording".
#
# EVERY LABEL NAMES THE RESULT, because the unit's own names do not: "Pre-Fader" and "Post-Fader"
# never say that they ARE multitrack, and "Off" never says you still get a recording, so RODE's
# names on their own leave "does post fader mean multi-track?" unanswered. Answering it is the
# label's job. The VALUES are untouched, so the write and its validation are unaffected.
# THE LABEL SAYS YOU GET BOTH, so the hint does not have to. Multi-track ADDS the separate files,
# it never replaces the mix - a real
# take writes `Stereo Mix` plus nine `Track*.wav` - and the old wording ("Pre-fader multi-track"
# beside "Single mixed track") read as a choice between a mix and tracks, which is the thing people
# get wrong.
#
# THE DIFFERENCE IS FRONT-LOADED on purpose: arrowing a dropdown reads each option from the start,
# so "Pre-fader" and "Post-fader" have to arrive before the part that is the same in both. Putting
# the shared "plus the mix" first would mean hearing the same six words before the one that
# distinguishes them.
MULTITRACK_LABEL = {
    0: "Mixed track only",
    1: "Pre-fader separate tracks, plus the mix",
    2: "Post-fader separate tracks, plus the mix",
}

# SHORT, because the labels now carry the "plus the mix" part. What is left is the two things a
# label cannot fit: the cost, and what pre versus post actually means.
# LIVES HERE, NOT IN app.js, so all three of this page's hints come from one place and the browser
# holds none of the wording. The other two are below.
MULTITRACK_HINT = ("Separate tracks use about seven times the space. Pre-fader ignores your fader "
                   "positions, post-fader keeps them.")

# WHAT THE RECORDING IS WRITTEN AS. `recordingCompressionQuality` is a TWO-MEMBER enum and nothing
# more: the firmware's type is `CompressionQualityType` and its touchscreen page ships exactly two
# icons, IconCompressionWAV.svg and IconCompressionMP3.svg.
#
# ONLY 0 AND 1 MAY EVER BE WRITTEN. The property is an unclamped raw store, so 2, 3, 99, -1 and 255
# all stick and read back, and the recorder treats any non-zero as MP3 - which is exactly how "it
# has four quality levels" got believed for a while. Takes at 1, 2 and 3 came back byte-identical in
# format: 128 kbps CBR, 48 kHz, stereo. Writing a 2 would leave the desk holding a value its own
# screen cannot draw. See PROJECT_GUIDE.md.
#
# The labels carry the SIZE, because that is the whole trade and it is the part you cannot hear:
# measured on the same take, the WAV was about eighteen times the MP3.
RECORDING_FORMAT_LABEL = {
    0: "WAV, full quality",
    1: "MP3, smaller files",
}
RECORDING_FORMAT_HINT = ("This is the mixed stereo file only: when you record separate tracks they "
                         "are always 24-bit WAV whatever you choose here. WAV is uncompressed and "
                         "about eighteen times the size, which is what you want if the recording "
                         "will be edited. MP3 is 128 kbps, fine for speech, and fits far more on "
                         "the card.")

# THE OTHER MULTITRACK, and it is a genuinely different destination rather than a second name for
# the same one. `outputMultiMode` decides what the desk sends THIS COMPUTER over USB; the pair
# above decides what it writes to the card or drive. They were told apart by measurement on
# Measured: switching this one makes the Main Multitrack USB device
# appear with 20 channels, and moves nothing on the drive.
#
# The labels say WHO RECEIVES IT, because "multitrack" on its own is the word that made these two
# indistinguishable in the first place.
USB_MULTITRACK_LABEL = {
    0: "Just the mix (2 channels)",
    1: "Every channel, pre-fader",
    2: "Every channel, post-fader",
}

# EXPANDED: `outputMultiMode` IS TWO SETTINGS IN ONE VALUE, and RODE's own app shows them as two
# tabs. The label map above is the USB 1 OUTPUT half; adding 3 puts the USB 1 INPUT into Expanded.
# All six measured on a Pro II, fw 1.7.6:
#   0 Off/Standard   1 Pre-Fader/Standard   2 Post-Fader/Standard
#   3 Off/Expanded   4 Pre-Fader/Expanded   5 Post-Fader/Expanded
#
# COMPOSED, NEVER GUARDED. A guard would have to be remembered at every call site and the cost of
# forgetting is invisible to whoever forgets: writing a bare output mode onto a desk in Expanded
# drops it to Standard, and the desk then unassigns every extended input the operator had put on a
# fader. Splitting the value into the two fields it really holds makes that unreachable instead.
USB_EXPANDED_OFFSET = 3


def split_usb_multitrack(value):
    """`outputMultiMode` -> (output mode 0-2, expanded bool). None in, (None, None) out, so an
    offline read propagates rather than reading as Off."""
    if value is None:
        return None, None
    value = int(value)
    expanded = value >= USB_EXPANDED_OFFSET
    return (value - USB_EXPANDED_OFFSET if expanded else value), expanded


def join_usb_multitrack(mode, expanded) -> int:
    """(output mode 0-2, expanded bool) -> the `outputMultiMode` value to write."""
    return int(mode) + (USB_EXPANDED_OFFSET if expanded else 0)


# Named for what ARRIVES, like every other label here, because "Expanded" alone says nothing about
# what you get and the state it names is invisible without the driver.
USB_EXPANDED_LABEL = {
    False: "Main and Chat only",
    True: "Main, Chat and four separate app inputs",
}
USB_EXPANDED_HINT = ("Expanded splits what this computer sends into four more inputs, Game, Music, "
                     "A and B, each assignable to its own fader, so one program can be faded "
                     "without the rest. The four names are interchangeable labels, not different "
                     "kinds of input. It needs RODE's Virtual Devices driver installed here or the "
                     "four arrive silent, it stops the RODECaster ASIO driver working, and turning "
                     "it back off unassigns whatever you put on those faders. "
                     # THE TWO WINDOWS STEPS, because the feature does nothing without them and
                     # neither is discoverable from this app or from the desk. RODE's own guide is
                     # the source for both.
                     "In Windows, set your sound output to System (RODECaster) rather than Main, "
                     "then send a program to one of the four in Settings, System, Sound, App "
                     "volume and device preferences, with that program running.")

# What the two processing-bypass switches mean, in RODE's own terms and ours.
#
# RODE's guide: "When recording in multitrack, you can choose to exclude processing from your
# recordings... The 'mix' output will not be affected by this setting and will always reflect the
# final mix output including all processing." So BOTH halves of that go in the hint: it is
# multitrack-only, and the mixed track always keeps its processing.
BYPASS_HINT = ("Separate tracks only, and it changes what is IN them rather than how many you get "
               "or what format they are. The mixed track always keeps the unit's processing, so "
               "this is for when you would rather do the compression and EQ yourself afterwards.")
BYPASS_NEEDS_MULTITRACK = ("This does nothing while only the mixed track is being made. Choose "
                           "pre-fader or post-fader above first.")

# The command values, named so a caller never passes a bare number.
CMD_STOP = 0
CMD_PAUSE = 1
CMD_RECORD = 2
COMMANDS = {"start": CMD_RECORD, "resume": CMD_RECORD, "pause": CMD_PAUSE, "stop": CMD_STOP}


# ---------------------------------------------------------------------------
# Reading: everything below parses a dump that has ALREADY been read, so one model read answers
# the whole page and none of this touches the device.

def read_state(blob: bytes):
    """`recordState`, or None if the dump does not carry it."""
    vals = rcp_read.values(blob, "recordState", "int")
    return vals[0] if vals else None


# The unit's three fixed storage slots, in the order it reports them.
#
# THE OLD LABELS WERE WRONG and these correct them: the app called slot 1 "Card slot 2" when it is
# the USB port, which matters the moment anyone is told where their recording went. Settled by
# evidence, not by guessing - with a 128 GB pendrive in USB 2 and no memory card in
# the unit, slot 0 read as empty, slot 1 reported the pendrive's capacity byte-for-byte identically
# to Windows (123042267136), and slot 2 is the unit's own ~5 GB volume that the SMART pads live on.
# `storageVolumeRecDestination` agreed: [True, True, False].
#
# Two names each, and the second is not decoration: lower-casing the label to drop it into a
# sentence turned "USB drive" into "usb drive", which a screen reader reads as "uzb".
VOLUME_NAMES = [
    ("Memory card slot", "memory card"),
    ("USB drive", "USB drive"),
    ("Internal storage (SMART pads)", "internal storage"),
]


def volume_label(index: int) -> str:
    """A speakable name for a storage slot, and a numbered fallback past the ones we know."""
    return VOLUME_NAMES[index][0] if index < len(VOLUME_NAMES) else f"Storage {index + 1}"


def volume_spoken(index: int) -> str:
    """The same volume named the way it fits INSIDE a sentence ("recording to the USB drive")."""
    return VOLUME_NAMES[index][1] if index < len(VOLUME_NAMES) else f"storage {index + 1}"


def read_volumes(blob: bytes, include_extra: bool = False) -> list:
    """Every storage volume the unit reports: what it is, whether anything is in it, how full it
    is, and whether a recording can go there.

    Parses `storageVolumeState` ("capacity|free|?|inserted|mounted"), which is the ONLY honest
    source: the numeric storageVolumeFree / Capacity / Inserted / Mounted properties all read 0 or
    False even with a drive mounted, re-confirmed with the pendrive in.

    A FOURTH VOLUME APPEARS WHILE RECORDING, and it caught this code out the first time it ran: the
    unit adds a live view of the drive it is writing to, a duplicate of that slot whose free space
    ticks down as the file grows, and it disappears again on stop. Listing it would name the same
    drive twice and, worse, an earlier version of this function keyed its labels on the NUMBER of
    volumes, so starting a recording renamed every slot mid-take ("Recording to the storage 2").
    So the extras are dropped by default and the labels are keyed on the INDEX, which does not
    move. `include_extra=True` returns them for anything that wants the live figure."""
    states = rcp_read.values(blob, "storageVolumeState", "str")
    dests = [bool(v) for v in rcp_read.values(blob, "storageVolumeRecDestination", "bool")]
    out = []
    for i, s in enumerate(states):
        if i >= len(VOLUME_NAMES) and not include_extra:
            continue
        parts = str(s).split("|")
        if len(parts) < 5:
            continue
        try:
            cap, free = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        inserted, mounted = parts[3] == "1", parts[4] == "1"
        pct = round(free * 100 / cap) if cap else None
        out.append({
            "index": i,
            "label": volume_label(i),
            "spoken": volume_spoken(i),
            # True for the live view the unit adds while it is recording (see the docstring).
            "live_view": i >= len(VOLUME_NAMES),
            "inserted": inserted,
            "mounted": mounted,
            "capacity_mb": round(cap / (1024 * 1024)),
            "free_mb": round(free / (1024 * 1024)),
            "free_bytes": free,
            "free_pct": pct,
            # A JUDGEMENT, made in one place: read aloud, "4960 MB free of 5033" is a division you
            # do in your head while a recording runs. Below a fifth left is where a long take or a
            # pad upload starts being a real risk.
            "low": bool(inserted and pct is not None and pct < 20),
            "can_record": bool(dests[i]) if i < len(dests) else False,
        })
    return out


def destination(volumes: list):
    """The volume a recording will actually go to, or None when there is nowhere.

    The unit does not report its choice, so this reports what is ELIGIBLE (in, mounted, and flagged
    as a record destination) and the caller words it. With one eligible volume, which is the normal
    case, that is the answer."""
    ready = [v for v in volumes if v["can_record"] and v["inserted"] and v["mounted"]]
    return ready[0] if ready else None


def parse_recording(uid, content: str) -> dict:
    """One entry of the recordings list, from its `recordingContent` string.

        name | device uid | path | epoch-ms timestamp | seconds | six unmapped flags

    THE TIMESTAMP IS A REAL EPOCH IN UTC, and it is shown in THIS COMPUTER'S LOCAL TIME.

    DO NOT render it AS UTC on the belief that the unit writes local wall clock into an epoch
    field. That belief comes from a loose observation ("the unit stamped 10:39 while this PC's UTC
    clock said 09:46"), which is 53 minutes and not an offset at all. Measured properly: four takes
    made to order, each read seconds later, all match this PC's UTC clock to within the time it
    takes to read the model, with local time at UTC+1. The field is ordinary UTC and the desk's
    clock is right; rendering it as UTC shows every recording an hour early.

    `systemDateTimezone` does NOT move it either (indices 44, 0 and 12 each produced a take stamped
    at the same UTC second), so the desk's timezone setting is a touchscreen display matter and
    cannot be checked from here - see ProjectNotes.
    """
    parts = str(content).split("|")
    if len(parts) < 5:
        return {"uid": uid, "name": str(content), "seconds": None, "when": "", "path": ""}
    try:
        seconds = round(float(parts[4]), 1)
    except ValueError:
        seconds = None
    when = ""
    try:
        # Local time on THIS computer (no tz argument = the system zone), so the time shown is the
        # wall clock the person remembers making the recording at. See the docstring.
        stamp = datetime.fromtimestamp(int(parts[3]) / 1000.0)
        when = stamp.strftime("%d %B %Y at %H:%M")
    except (ValueError, OSError, OverflowError):
        pass
    return {"uid": uid, "name": parts[0], "path": parts[2], "when": when, "seconds": seconds,
            "duration": describe_duration(seconds)}


def describe_duration(seconds) -> str:
    """A length said the way a person says it. Never "0:07.3": a screen reader reads punctuation
    and a decimal, and neither carries meaning here."""
    if seconds is None:
        return "length unknown"
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} second{'' if seconds == 1 else 's'}"
    mins, secs = divmod(seconds, 60)
    if mins < 60:
        return (f"{mins} minute{'' if mins == 1 else 's'}"
                + (f" {secs} second{'' if secs == 1 else 's'}" if secs else ""))
    hours, mins = divmod(mins, 60)
    return (f"{hours} hour{'' if hours == 1 else 's'}"
            + (f" {mins} minute{'' if mins == 1 else 's'}" if mins else ""))


def read_recordings(blob: bytes) -> list:
    """Everything on the drive, newest first.

    Each finished recording adds a RECORDING block to the model (about 255 bytes, which is why the
    dump grows after a take - the per-property occurrence counts confirm it is a new block and not a
    stray). `recordingUID` and `recordingContent` occur once per block in the same order, so they
    are paired positionally."""
    uids = rcp_read.values(blob, "recordingUID", "int")
    contents = rcp_read.values(blob, "recordingContent", "str")
    out = []
    for i, content in enumerate(contents):
        out.append(parse_recording(uids[i] if i < len(uids) else None, content))
    out.reverse()          # the unit lists them oldest first; the newest is the one you want
    return out


def room_left(blob: bytes, dest) -> dict:
    """How much RECORDING TIME is left on the destination, not just how many megabytes.

    This is the number a sighted operator gets from a bar and a blind one has never had: megabytes
    do not tell you whether a two-hour show fits, and the arithmetic changes with the recording
    settings. The unit reports `recordBytesPerSecond` itself (384000 for a stereo mix here, and it
    rises with multitrack), so the sum uses the unit's own figure rather than an assumed bitrate."""
    if not dest or not dest.get("free_bytes"):
        return {"seconds": None, "words": None, "bytes_per_second": None}
    rate = rcp_read.values(blob, "recordBytesPerSecond", "int")
    rate = rate[0] if rate else 0
    if rate <= 0:
        return {"seconds": None, "words": None, "bytes_per_second": None}
    seconds = int(dest["free_bytes"] / rate)
    return {"seconds": seconds, "words": describe_duration(seconds), "bytes_per_second": rate}


def describe(blob: bytes) -> dict:
    """The whole Recording page from one dump: state, destination, volumes and the recordings."""
    state = read_state(blob)
    volumes = read_volumes(blob)
    dest = destination(volumes)
    total = rcp_read.values(blob, "recordingTotalCount", "int")
    # The live view of the drive being written to, when there is one (see read_volumes). It is the
    # SAME drive as the destination, so it is reported separately rather than as another volume.
    extra = [v for v in read_volumes(blob, include_extra=True) if v["live_view"]]
    return {
        "state": state,
        "state_label": STATE_LABEL.get(state, "Unknown"),
        "recording": state == RECORDING,
        "paused": state == PAUSED,
        "can_record": state in (READY, PAUSED, RECORDING),
        "volumes": volumes,
        "destination": dest,
        "writing_to": extra[0] if extra else None,
        "room": room_left(blob, extra[0] if extra else dest),
        "recordings": read_recordings(blob),
        "total": total[0] if total else None,
    }


# ---------------------------------------------------------------------------
# Writing. One command field, one selector, and the caller reads `recordState` back to find out
# what actually happened - which is the only honest way round, since the request field self-clears.

def send_command(value: int) -> None:
    """Write `requestRecordState`. Nothing is verified here: see server.action_record, which reads
    the state back rather than trusting the write."""
    rcp_transfer.send_record(
        rcp_transfer.build_global_prop_int("requestRecordState", SEL_RECORDER, int(value)))


def drop_marker() -> None:
    """Put a marker at the current point of the recording.

    IT CANNOT BE VERIFIED FROM HERE, and the caller must not pretend otherwise: the desk consumes
    the write and pushes the field back cleared, and no readable property changes. The proof is in
    the file - a standard WAV cue point, confirmed on the drive (see the module docstring). So the
    honest report is "sent", with the position we believe it landed at, rather than "done".

    SENT AS A FLOAT (`build_global_prop_float`, value block `01 09 04`), which is the closest of
    our builders to the field's own type - the desk echoes it back as `01 09 06`, an eight-byte
    form we have no builder for. Both this and the plain int form were confirmed landing in the
    same session, so the exact value type is not what the firmware is keying on."""
    rcp_transfer.send_record(
        rcp_transfer.build_global_prop_float("requestDropMarker", SEL_RECORDER, 1.0))


# ---------------------------------------------------------------------------
# DELETING A RECORDING. The last real gap on the Recording page: today only the unit's own
# touchscreen can remove a take, which is exactly the thing a blind operator cannot reach.
#
# THE ADDRESS. `requestDeleteUID` lives in the RECORDINGS block, which our block walk numbers 0x28F.
# That is past what the one-byte global form can carry, so it is sent in the TWO-BYTE form
# (`01 01 01 02 <lo> <hi>`), the same shape every mic gain write uses. What is known, measured:
#   - 0x28E = SHOWCONTROL and 0x290 = TEST are both PROVEN, so
#     RECORDINGS is bracketed one slot either side.
#   - `requestDeleteUID` exists in exactly TWO blocks in the whole model: RECORDINGS, natively, and
#     SHOWCONTROL, where it is the inert stray the 0x28E probe left behind.
#   - Writing a UID that does not exist (9999) on 0x28F appended NO stray, so the address resolved
#     to one of those two blocks and nothing else. Which means the worst a real write can do is
#     nothing at all: the SHOWCONTROL copy is inert.
#
# >>> PROVEN ON HARDWARE. <<<
# `requestDeleteUID` = 20 on 0x28F removed Recording 20 from the pendrive. The recordings list went
# from 20 to 19, `recordingUID` and `recordingContent` each lost exactly one occurrence, and NOTHING
# else in the model moved - no stray, and no other block touched. So RECORDINGS = 0x28F is now an
# anchor in its own right rather than an inference from its neighbours.
#
# TWO THINGS MEASURED IN THE SAME RUN, both worth knowing before building on this:
#   - THE REMAINING RECORDINGS ARE NOT RENUMBERED. Uids 1-19 stayed 1-19 with 20 removed, so a UID
#     is a stable handle and a list held by a page does not silently shift under it.
#   - THE FIELD SELF-CLEARS, so the write can never confirm itself. The read-back is the recordings
#     list, which is why `server.action_delete_recording` re-reads it and reports what it finds
#     rather than reporting that a write was sent.
SEL_RECORDINGS = 0x28F
DELETE_PROVEN = True
DELETE_UNPROVEN_MESSAGE = ""      # kept for the payload's shape; nothing says this any more


def build_delete_request(uid: int) -> bytes:
    """The record that asks the unit to delete recording `uid`. Two-byte address; see above."""
    return rcp_transfer.build_channel_prop_int(
        "requestDeleteUID", int(uid), SEL_RECORDINGS & 0xFF, SEL_RECORDINGS >> 8)


def delete_recording(uid: int) -> None:
    """Send the delete request. VERIFIES NOTHING - the field self-clears either way, so the caller
    must re-read the recordings list and check the UID has gone (server.action_delete_recording)."""
    rcp_transfer.send_record(build_delete_request(uid))


# ---------------------------------------------------------------------------
# The Watcher: what the unit is doing, live, for free.

class Watcher(threading.Thread):
    """Listens for `recordState` pushes on its own read-only HID handle.

    WHY A LISTENER RATHER THAN POLLING. Reading the state from the model costs a ~1.5 s dump and the
    HID lock, so polling it would fight every write in the app. The unit pushes the state change
    within ~30 ms instead, and pushes NOTHING while nothing changes, so this thread is idle almost
    all the time. It also catches a press of the unit's own REC button, which is the case the page
    could never poll fast enough for anyway.

    Its own handle, never the announcer's: input reports are delivered per handle, so two listeners
    do not steal each other's packets (the same reasoning as the announcer's persistent handle).
    Strictly read-only - it never writes.

    ELAPSED TIME IS COUNTED HERE, because the unit will not tell us: `recordTimeMs` is 0 in the dump
    and never pushed. Paused time is excluded, matching what the unit actually records. If the app
    started while a recording was already running we did not see the start, so `elapsed()` returns
    None and the UI says the time is unknown rather than showing a number that is wrong."""

    def __init__(self, on_change=None):
        super().__init__(daemon=True)
        self._on_change = on_change
        self._lock = threading.Lock()
        self._state = None
        self._since = None          # when the CURRENT recording run started, or None
        self._banked = 0.0          # seconds recorded before the current run (pauses)
        self._timed = False         # did we see this recording START?
        self._seen_at = 0.0
        self.running = True

    # -- state, as the rest of the app asks for it -------------------------
    def snapshot(self) -> dict:
        with self._lock:
            state, seen_at = self._state, self._seen_at
            elapsed = self._elapsed_locked()
        return {"state": state,
                "state_label": STATE_LABEL.get(state) if state is not None else None,
                "elapsed_seconds": elapsed,
                "elapsed": describe_duration(elapsed) if elapsed is not None else None,
                "seen": bool(seen_at),
                "live": True}

    def _elapsed_locked(self):
        if self._state not in (RECORDING, PAUSED) or not self._timed:
            return None
        run = (time.time() - self._since) if (self._state == RECORDING and self._since) else 0.0
        return round(self._banked + run, 1)

    def note_state(self, state, timed: bool = True) -> None:
        """Record a state we learned from somewhere other than a push (a model read at startup, or
        the read-back after our own write). `timed=False` says we did not witness the start, so no
        elapsed time is claimed."""
        self._apply(state, timed=timed, speak=False)

    def _apply(self, state, timed: bool = True, speak: bool = True) -> None:
        if state is None:
            return
        with self._lock:
            if state == self._state:
                self._seen_at = time.time()
                return
            was = self._state
            now = time.time()
            if was == RECORDING and self._since:
                self._banked += now - self._since
                self._since = None
            if state == RECORDING:
                self._since = now
                if was not in (RECORDING, PAUSED):
                    self._banked, self._timed = 0.0, timed
            elif state in (READY, NO_DESTINATION):
                self._banked, self._timed = 0.0, False
            self._state, self._seen_at = state, now
        if speak and self._on_change:
            try:
                self._on_change(was, state)
            except Exception:
                pass          # a failed announcement must never take the listener down

    # -- the thread -------------------------------------------------------
    def run(self) -> None:
        import rcp_hid      # local, so importing this module never needs the HID stack
        import diagnostics
        dev = None
        while self.running:
            if dev is None:
                iface = rcp_transfer.find_control_interface()
                if iface is None:
                    diagnostics.event("device.absent", "no control interface to watch on")
                    time.sleep(2.0)               # unit unplugged; try again shortly
                    continue
                try:
                    dev = rcp_hid.open_handle(iface, "recorder watcher")
                except Exception as exc:
                    dev = None
                    diagnostics.event("device.open_failed", "recorder watcher", error=f"{exc}")
                    time.sleep(2.0)
                    continue
            try:
                rep = dev.read(256, timeout_ms=250)
            except Exception as exc:
                # THE DESK STOPPING IS SEEN HERE FIRST, because this is the one reader that is
                # always on and always reading. Its timestamp is the app's own answer to "when did
                # it stop answering", which nothing else can give without inferring it from outside.
                diagnostics.event("device.lost", "recorder watcher read failed", error=f"{exc}")
                rcp_hid.close_handle(dev, "recorder watcher")
                dev = None
                time.sleep(0.5)
                continue
            if not rep:
                continue
            try:
                state = _state_from_push(bytes(rep))
                if state is not None:
                    self._apply(state)
            except Exception:
                pass          # one undecodable packet costs one update, never the thread
        if dev is not None:
            rcp_hid.close_handle(dev, "recorder watcher")


def _state_from_push(report: bytes):
    """`recordState`'s value out of one report-04 packet, or None if that is not what this is.

    The push carries the global address form `01 01 01 01 <selector>` followed by the name and an
    int value block (`01 05 01` + 4 bytes LE), which is the same shape our own writes use. The
    selector is checked as well as the name: `recordState` is single-instance today, but matching
    the address too is what stops a same-named field in another block being read as the recorder."""
    if not report or report[0] != 0x04:
        return None
    i = report.find(b"recordState")
    if i < 0 or report[i - 1:i] != bytes([SEL_RECORDER]):
        return None
    j = i + len("recordState")
    if report[j:j + 4] != b"\x00\x01\x05\x01":
        return None
    return int.from_bytes(report[j + 4:j + 8], "little", signed=True)
