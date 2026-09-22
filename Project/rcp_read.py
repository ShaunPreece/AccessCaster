"""AccessCaster - APP-FREE READ-BACK of current device settings.

Discovery (session 12): selecting the device in the RODE app makes it send a
4-byte command on report-3 (payload ad10a7b0), and the device answers by
streaming its ENTIRE property model on report-0x04 IN (gain, P48, MIDI,
polarity, EQ, FX, ...). This module replays that command from our own code and
parses the returned stream, so we can read current settings with NO RODE app.

The returned stream is a concatenated tree of sections and properties; each
property is  <name> 00 <value-block>, value-block one of:
  01 05 01 <4-byte LE int>      integer
  01 01 <02|03>                 compact bool (02 = ON/true, 03 = OFF/false)
  01 <vlen> 05 <bytes> 00       string
We don't fully parse the tree; we locate property names and decode the value
that follows, which is enough to read every setting we care about.
"""
from __future__ import annotations
import queue, re, struct, time, threading
import model_tree            # THE parser: the dump is a tree, and this reads it
import diagnostics           # the app's own account of what it read and how long it took
import rcp_hid   # write-generation counter, so a write invalidates the cached dump below
from rcp_hid import find_control_interface
import devices   # device model table (matrix shape, channel order) keyed by product id

# The output-mode array: each output is a `MIXMINUSES` section holding an outputMixMinus
# int (0=Main,1=MixMinus,2=Custom). We anchor on the MIXMINUSES section header and read the
# int after it; the first 11 headers are outputs 0-10 in order (verified by writing each).
# Anchoring on the SECTION rather than the property name also survives a mangled name, which is
# what a framing fault does to outputMixMinus on HP1/USB1.
_MIXMINUSES_HDR = re.compile(rb"MIXMINUSES\x00")

BULK_LEN = 256
# Verbatim trigger captured from the app (micmidi_reads.pcap frame 70043):
# report 0x03, payload-len 4, payload ad10a7b0 = "dump full device model".
TRIGGER = (bytes([0x03]) + (4).to_bytes(4, "little")
           + bytes.fromhex("ad10a7b0")).ljust(BULK_LEN, b"\x00")


# End-of-stream detection. The device streams the whole model as a continuous burst of 256-byte
# report-04 frames (measured on this unit: 549 frames, ~140 KB, delivered in ~375 ms with sub-
# millisecond gaps), then goes quiet. So the dump is finished once the line has been idle for
# QUIET_S. That was 0.4 s, which made every dump cost ~800 ms with half of it spent waiting on a
# stream that had already ended; 0.12 s returns a byte-identical blob (verified 10/10 dumps,
# 139995 bytes each) for ~590 ms. READ_TIMEOUT_MS is the per-read block, and it also sets how
# finely we notice the quiet period, so it is well under QUIET_S.
QUIET_S = 0.12
READ_TIMEOUT_MS = 60

# ---- TELLING A DUMP FRAME FROM AN ASYNCHRONOUS PUSH -------------------------
#
# The desk answers the trigger with a burst of report-04 frames, and it ALSO sends report-04 frames
# of its own accord whenever anything changes on the desk. Those two were indistinguishable here
# unless the reader tells them apart, and confusing them costs two separate faults:
#
#   * THE READ NEVER ENDED. Quiet detection alone means the deadline is pushed out by QUIET_S on
#     every frame. A playing pad pushes `padProgress` about 20 times a second, so the line never
#     goes quiet, and the loop runs to its `max_reads` ceiling: 240 s worst case, 172 s measured on
#     a Duo. It reaches far wider than the pads page, because every per-channel write resolves its
#     strip through `channels.strip_vector`, which dumps whenever its cache has expired - so mute,
#     solo, processing and every EQ parameter could each hang for minutes while a pad played, and
#     a hung request leaves its control busy, which reads out as "that is not available".
#   * AND THE BLOB CAME BACK CONTAMINATED. A push frame was appended into the dump like any other,
#     so the read was the wrong SIZE and the wrong SHAPE: 264180 and 139485 bytes against a 136170
#     baseline, both taken just after a pad was touched. It presents as "an unstable read".
#
# BOTH HALVES FALL OUT OF TWO MEASURED FACTS (live Duo):
#
#  1. THE DUMP DECLARES ITS OWN LENGTH. Its first frame opens with a 4-byte little-endian byte
#     count covering everything after those four bytes, so the whole stream is 4 + declared bytes
#     and the read can end on STRUCTURE rather than on silence. 136675 on this Duo, identical on
#     every dump measured; it is the first four of the five header bytes model_tree.parse skips.
#  2. A PUSH IS ONE WHOLE RECORD, ALONE IN ITS FRAME: `04 <len32> <record> <zeros to 256>`. Real
#     ones are 20 to 30 bytes, so 220-odd bytes of padding, where a dump frame carries 255 dense
#     bytes with at most a handful of trailing zeros. `_push_record_len` decides it structurally
#     rather than by size, so the answer does not rest on a threshold.
#
# AND THE BURST IS ATOMIC. Over four runs with a pad playing throughout, pushes arrived BEFORE the
# first dump frame and AFTER the last, never between two of them - 536 uninterrupted dump frames
# every time. So dropping push frames cannot punch a hole in the middle of a dump.
#
# THE QUIET TIMEOUT STAYS, as the backstop for an ABNORMAL read: a replug mid-stream, or a firmware
# that stops declaring a length. Structure ends a healthy read; silence still ends a broken one.
PUSH_MAX_RECORD = 200
# Plausibility bounds on the declared length, so a garbled first frame degrades to quiet detection
# instead of ending the read after one frame or waiting for bytes that will never come. Measured
# models: 136675 (Duo) and 143187 (Pro II).
MODEL_MIN_DECLARED = 4096
MODEL_MAX_DECLARED = 4 << 20

# The root node's name, which every real dump opens with (see model_tree.parse).
_ROOT_MARKER = b"Rodecaster\x00"


def is_model_dump(blob) -> bool:
    """True if this blob is actually a model dump rather than the ABSENCE of one.

    A DESK THAT HAS STOPPED ANSWERING RETURNS ZERO BYTES, NOT AN ERROR. `read_model_raw` ends on
    silence and hands back whatever it collected, which is `b""`, and every downstream test then
    reads that as a perfectly good answer that simply differs from the last one. That is how a
    restore reports success with the desk already off the bus, `job.end ok=True` a minute after the
    last byte: the import's evidence is "the state moved", and an empty blob differs from
    everything.

    So anything deciding whether the desk DID something must ask this first. The test is the root
    marker rather than a length, because a short dump is a different fault (a truncated read, which
    `read_model_stable` already guards) and would be wrong to report as the desk being gone.
    """
    return bool(blob) and _ROOT_MARKER in blob

# What the last dump actually did, for diagnosis. A skipped frame is invisible otherwise, and
# "the read was fine" is exactly the wrong thing to conclude from silence.
_last_read: dict = {}

# ---- short-lived cache of the last dump -------------------------------------
# One page view fires several independent reads (channels, routing live, matrix, mix levels) within
# a second or two, and without this each one pays for its own ~800 ms dump. The blob is the SAME
# device state, so the first read caches it and the rest reuse it. The cache is dropped the instant
# anything is written (rcp_hid.write_generation) so a read-back after a write is always a fresh
# dump, and it expires after CACHE_TTL_S so a change made on the desk itself (a knob, the
# touchscreen) can never be shown as current for longer than that.
CACHE_TTL_S = 1.5
_cache_lock = threading.Lock()
_cache = {"blob": None, "at": 0.0, "gen": -1, "stable": False}


def _cache_get(need_stable: bool):
    """The cached dump if it is still valid (and stability-checked when required), else None."""
    with _cache_lock:
        c = _cache
        if c["blob"] is None or c["gen"] != rcp_hid.write_generation():
            return None
        if time.time() - c["at"] > CACHE_TTL_S:
            return None
        if need_stable and not c["stable"]:
            return None
        return c["blob"]


def _cache_put(blob: bytes, stable: bool) -> None:
    with _cache_lock:
        _cache.update(blob=blob, at=time.time(), gen=rcp_hid.write_generation(), stable=stable)


def read_model(settle_ms: int = 1500, max_reads: int = 4000, fresh: bool = False) -> bytes:
    """The device's full property model, from the short-lived cache when it is still valid.

    Pass fresh=True to force a real dump. Non-default settle_ms/max_reads also bypass the cache,
    since the caller is asking for specific timing.
    """
    if not fresh and settle_ms == 1500 and max_reads == 4000:
        cached = _cache_get(need_stable=False)
        if cached is not None:
            return cached
    blob = read_model_raw(settle_ms, max_reads)
    if settle_ms == 1500 and max_reads == 4000:
        _cache_put(blob, stable=False)
    return blob


def _push_record_len(frame: bytes):
    """The record length if this report-04 frame is one asynchronous push, else None.

    A push is `04 <len32> <record> <zero padding>`. The test is structural on purpose: the declared
    length must be followed by a record that parses COMPLETELY and accounts for exactly that many
    bytes, and every byte after it must be zero. A 255-byte slice of the dump stream cannot satisfy
    both - it would need a fifty-plus byte run of trailing zeros AND a self-consistent addressed
    record sitting at offset 5.
    """
    if len(frame) < 6:
        return None
    n = int.from_bytes(frame[1:5], "little")
    if not 6 <= n <= PUSH_MAX_RECORD or 5 + n > len(frame):
        return None
    if any(frame[5 + n:]):
        return None
    try:
        end = model_tree.parse_record(frame[5:5 + n])[4]
    except ValueError:
        return None
    return n if end == n else None


class PushListener:
    """WAIT FOR THE DESK TO SAY SO, instead of re-reading the whole tree until it has.

    WHY THIS EXISTS. The protocol has no partial read: one trigger, 536 frames, about 136 kB, well
    over a second, so answering "did that command work?" by dumping the ENTIRE model every 1.5 s
    until the answer appears puts our traffic on the wire for the whole time the desk
    is busy, and in the case of a factory reset that is exactly when it re-enumerates its USB audio
    function. The desk does not need asking. It PUSHES what changed, unprompted, on report 04.

    MEASURED ON A DUO. A factory reset pushes 3267 frames covering 195 distinct
    properties, starting 0.02 s after the command and finishing at t+4.96 s as one continuous burst
    with no gap of half a second anywhere in it. `currentShowUUID` is among them, so the new show id
    - the desk's only confirmation that a reset happened - arrives on its own. An impossible export
    pushes just three frames (`showControlLastError` twice, then `showControlExport`) and moves
    NOTHING in the tree, so for that command a push is the only evidence that exists at all: no
    amount of reading could ever have seen it.

    ITS OWN HANDLE, AND NO HID LOCK. Input reports are delivered per handle, so this cannot steal
    packets from `recorder.Watcher` or the announcer, and being read-only it does not need to
    serialise against writes. Callers therefore hold the lock for the WRITE and let go before
    waiting, rather than holding it across a multi-second poll.

    OPEN IT BEFORE THE WRITE. The burst starts 20 ms after the command, so a listener started
    afterwards races it and loses. Frames are queued from the moment it opens, so there is no gap
    between opening and the first `wait_for`.

    DUMP FRAMES ARE FILTERED OUT STRUCTURALLY by `_push_record_len`, because a model dump arrives on
    report 04 as well and any dump triggered anywhere in the app is delivered to this handle too.
    The same test also drops the occasional frame that is not one complete record for other reasons;
    see `not_a_record`, which is counted separately rather than assumed to be a dump.
    """

    def __init__(self):
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None
        self.frames = 0            # report-04 frames seen
        self.pushes = 0            # of those, ones that were a complete addressed record
        # NOT "dump frames", which is what this was called until it was measured: 74 of 3267 frames
        # in a reset burst land here with no dump running anywhere, so they are mostly pushes the
        # structural test rejected - a record over PUSH_MAX_RECORD, or one that would not parse -
        # rather than slices of somebody's model. Naming them after the assumption hid that.
        self.not_a_record = 0

    def __enter__(self) -> "PushListener":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def lost(self) -> bool:
        """The reader stopped early, which is what a desk leaving the USB bus looks like from here.

        NOT proof on its own: hidapi does not promise an exception on device removal, so a drop can
        also present as a plain timeout. Treat this as a strong hint, never as the only check."""
        return self._error is not None

    def start(self) -> None:
        iface = find_control_interface()
        if iface is None:
            raise RuntimeError("no control interface (is the unit connected?)")
        dev = rcp_hid.open_handle(iface, "push listener")
        # THE HANDLE IS OPENED HERE AND CLOSED ON THE READER THREAD, never from stop(). Closing a
        # hidapi handle while a read is in flight on another thread segfaults the interpreter
        # (exit 139). recorder.Watcher and the announcer both close on their own thread for the
        # same reason.
        self._thread = threading.Thread(target=self._run, args=(dev,),
                                        name="rcp-push-listener", daemon=True)
        self._thread.start()

    def _run(self, dev) -> None:
        try:
            dev.set_nonblocking(False)
            while not self._stop.is_set():
                try:
                    rep = dev.read(BULK_LEN, timeout_ms=READ_TIMEOUT_MS)
                except Exception as exc:                  # device gone, or handle torn down
                    self._error = exc
                    diagnostics.event("device.read_failed", "push listener stopped early",
                                      error=f"{exc}")
                    return
                if not rep or rep[0] != 0x04:
                    continue
                frame = bytes(rep)
                self.frames += 1
                n = _push_record_len(frame)
                if n is None:
                    self.not_a_record += 1
                    continue
                try:
                    path, name, val, kind, _end = model_tree.parse_record(frame[5:5 + n])
                except ValueError:
                    continue                              # already improbable; never fatal
                self.pushes += 1
                self._q.put((tuple(path), name, val, kind))
        finally:
            rcp_hid.close_handle(dev, "push listener")

    def wait_for(self, name: str, timeout: float, match=None):
        """The value of the next pushed `name`, or None on timeout or on the reader dying.

        `match(value)` filters, for the common case of waiting on a CHANGE rather than on any push
        at all: the desk re-pushes a property at its existing value (`showControlLastError` arrived
        twice still reading 8), so "it was pushed" and "it changed" are different questions.

        A DEAD READER DOES NOT DISCARD WHAT IT ALREADY HEARD: the queue is drained before giving up,
        or a desk that pushed its answer and then fell off the bus would be reported as a failure
        having actually succeeded.
        """
        deadline = time.time() + timeout
        while True:
            try:
                _path, got, val, _kind = self._q.get(timeout=0.1)
            except queue.Empty:
                if self.lost or time.time() >= deadline:
                    return None
                continue
            if got != name:
                continue
            if match is not None and not match(val):
                continue
            return val

    def wait_until_quiet(self, idle: float = 0.75, timeout: float = 20.0) -> float:
        """Block until the desk has pushed nothing for `idle` seconds. Returns how long that took.

        "IT WORKED" AND "IT HAS FINISHED" ARE DIFFERENT QUESTIONS, and only the first is answered by
        a property arriving. A factory reset pushes `currentShowUUID` at about t+1.8 s and then keeps
        going until t+4.96 s, so a caller that acts the moment its answer lands is WRITING TO A DESK
        THAT IS STILL REWRITING ITSELF - and on a reset that is also while it re-enumerates its USB
        audio function. The old poll-a-dump-every-1.5 s approach was accidentally protected from
        this by being slow; listening is fast enough to need the distinction made explicitly.

        `idle` is comfortably above the burst's own internal gaps: measured over 3267 frames, not one
        gap reached half a second.
        """
        started = time.time()
        deadline = started + timeout
        while True:
            try:
                self._q.get(timeout=idle)
            except queue.Empty:
                return time.time() - started        # nothing for `idle` seconds: it has stopped
            if self.lost or time.time() >= deadline:
                return time.time() - started

    def stop(self) -> None:
        """Ask the reader to finish and WAIT for it, so the handle is closed by its own thread."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None


def read_model_raw(settle_ms: int = 1500, max_reads: int = 4000) -> bytes:
    """Send the dump trigger and collect the report-0x04 IN stream into one blob.

    Each 256-byte HID frame starts with the 0x04 report-id byte; the model data is the
    remaining 255 bytes. We MUST strip that report-id from every frame, otherwise a 0x04
    is left wedged into the stream at every 256-byte boundary and any property name/value
    that spans a boundary gets a spurious 0x04 injected into it (e.g. 'o\\x04utputMixMinus').
    That was the long-standing cause of split/missed properties and of read counts drifting
    between dumps as value lengths shift. Stripping per frame yields one contiguous stream.

    Asynchronous PUSH frames are skipped, and the dump ends at the length it declares for itself
    rather than when the line goes quiet. See the block comment above `PUSH_MAX_RECORD` for why
    both of those are load-bearing rather than tidying.

    The returned blob is the frame payloads CONCATENATED AND NOT TRIMMED, deliberately: it is
    therefore byte-identical to what a quiet read has always produced, trailing frame padding and
    all, so no offset or recorded baseline downstream shifts.
    """
    iface = find_control_interface()
    if iface is None:
        raise RuntimeError("no control interface (is the unit connected? close the RODE app)")
    dev = rcp_hid.open_handle(iface, "model dump")
    buf = bytearray()
    need = None        # 4 + the declared length, once the dump's first frame has told us
    frames = pushes = declared = 0
    started = time.time()
    try:
        dev.set_nonblocking(False)
        dev.write(TRIGGER)
        deadline = time.time() + settle_ms / 1000.0
        reads = 0
        while time.time() < deadline and reads < max_reads:
            chunk = dev.read(BULK_LEN, timeout_ms=READ_TIMEOUT_MS)
            reads += 1
            if not chunk:
                continue
            b = bytes(chunk)
            if not b or b[0] != 0x04:
                continue
            if _push_record_len(b) is not None:
                # The desk telling us about something else entirely. Not part of this dump, and
                # appending it is what corrupted the blob and stopped the read ever finishing.
                pushes += 1
                continue
            frames += 1
            buf.extend(b[1:])      # drop the 1-byte report id, keep the 255-byte payload
            if need is None:
                declared = int.from_bytes(buf[:4], "little")
                if MODEL_MIN_DECLARED <= declared <= MODEL_MAX_DECLARED:
                    need = 4 + declared
            if need is not None and len(buf) >= need:
                break
            deadline = time.time() + QUIET_S  # extend while data keeps flowing
    finally:
        rcp_hid.close_handle(dev, "model dump")
    _last_read.update(bytes=len(buf), frames=frames, pushes_skipped=pushes, declared=declared,
                      reads=reads, bounded_by="length" if need is not None else "silence",
                      at=time.time())
    # A DUMP THAT ENDED ON "silence" IS THE DESK FAILING TO ANSWER, and the size says how far it
    # got before it stopped. This is the app's own reading of the desk's health, taken every time
    # it looks at it, and it is the line that dates a dropout without inferring it from outside.
    diagnostics.event("model.read", bounded_by="length" if need is not None else "silence",
                      bytes=len(buf), frames=frames, pushes_skipped=pushes,
                      took=f"{time.time() - started:.2f}s")
    return bytes(buf)


def _val_after(blob: bytes, i: int):
    """Decode the value block right after a property name that ends at index i."""
    v = blob[i:]
    if v[:1] == b"\x00":
        v = v[1:]
    if len(v) >= 3 and v[0] == 0x01:
        vlen, vtype = v[1], v[2]
        if vtype == 0x01:  # int LE
            return int.from_bytes(v[3:7], "little", signed=True), "int"
        if vtype in (0x02, 0x03):  # compact bool
            return (vtype == 0x02), "bool"
        if vtype == 0x04 and len(v) >= 11:  # 64-bit float (channel processing, normalised 0..1)
            return struct.unpack("<d", v[3:11])[0], "float"
        if vtype == 0x05:  # string
            s = v[3:3 + max(0, vlen - 1)].split(b"\x00")[0]
            return s.decode("latin1"), "str"
    return None, "?"


# ---- ONE PARSE PER DUMP, and every read served from it ----------------------
#
# ONE READER, NOT THREE. A raw substring search, a flat block walk and a tree parser can all coexist
# here, and the crude one ends up carrying most of the traffic because it looks fast.
#
# That speed argument is backwards. The cost is never the parsing METHOD, it is parsing PER
# LOOKUP. Measured on a live 143 KB dump: 33 lookups cost 4 ms by substring search, but 1711 ms if
# each one walked. Parse ONCE into an index and every read after it is a dict lookup - and the tree
# parse is 9.6 ms, five times FASTER than the 84.5 ms flat walk it replaces.
#
# So: `model_tree.parse` once per dump, cached, and find_all / iter_properties /
# iter_block_properties / block_value all serve from that index. See model_tree.py for what the
# tree fixes (a phantom `iPOT`, EFFECTS_PARAMETERS misfiled as PARAMETERS, and 12 properties the
# flat reader returned as None).
#
# Keyed by IDENTITY and holding a strong reference to the blob, deliberately: id() alone is unsafe
# because a freed object's id can be reused, which would hand back another dump's index. Two entries
# is enough - callers pass the same blob repeatedly, and the pad paths interleave at most two.
_INDEX_CACHE: list = []
_INDEX_CACHE_MAX = 2


class _Index:
    """A parsed dump: the tree, the properties in stream order, and name + path lookups."""

    __slots__ = ("root", "flat", "by_name", "by_path")

    def __init__(self, root):
        self.root = root
        self.flat = []                    # [(block, name, value, kind)] in dump order
        self.by_name: dict = {}
        # ADDRESS -> node. The tree path IS the write address (see model_tree.encode_path), so this
        # is what lets a read be addressed exactly the way the matching write is. by_name throws the
        # address away, which is fine for "every occurrence" and wrong for "this channel's value".
        self.by_path: dict = {}
        for node in model_tree.walk(root):
            self.by_path[tuple(node.path())] = node
            for pname, val, kind in node.props:
                self.flat.append((node.name, pname, val, kind))
                self.by_name.setdefault(pname, []).append((val, kind))


def _index(blob: bytes):
    """The parsed index for a dump, parsed once and remembered. None if it will not parse.

    A DUMP THAT WILL NOT PARSE MUST NOT TAKE THE APP DOWN. A read can race a write and come back
    truncated, and the honest response to that is to fall back to the old flat walk for this one
    blob, not to raise into a page handler. read_model_stable already guards the common case by
    requiring two dumps to agree.
    """
    for cached_blob, idx in _INDEX_CACHE:
        if cached_blob is blob:
            return idx
    try:
        root, _used, _tail = model_tree.parse(blob)
    except Exception:
        return None
    idx = _Index(root)
    _INDEX_CACHE.append((blob, idx))
    del _INDEX_CACHE[:-_INDEX_CACHE_MAX]
    return idx


def find_all(blob: bytes, name: str):
    """All decoded values for a property name, in stream order: [(value, kind), ...].

    EXACT NAME, never a prefix. The old substring search could not tell a name from the start of a
    longer one: `padActive` matched inside `padActiveEdit` and put a junk entry at the FRONT of the
    list, which `soundpad.latched_pads` compensated for with a hardcoded "+1" - a parser artefact
    worked around inside the check that catches a voice-FX pad left engaged on a live mic.
    `outputMonAutoMute` collided with `outputMonAutoMuteActive` the same way.

    A property whose value cannot be decoded is still returned, as (None, "?"), because it IS an
    occurrence and dropping it would shift every positional read past it.
    """
    idx = _index(blob)
    if idx is None:
        return _legacy_find_all(blob, name)
    return list(idx.by_name.get(name, ()))


def values(blob: bytes, name: str, kind: str | None = None) -> list:
    """Just the VALUES for a property name, in stream order, optionally only those of one `kind`.

    THE ONE WAY TO READ A LIST OF VALUES, and what almost every caller actually wants. find_all
    returns (value, kind) pairs because positional reads need the kind to filter on; unwrapping
    that pair was written out by hand at 32 call sites, four of which had grown their own identical
    `def val(prop, kind=None)` closure (server.py twice, channels.py twice). Filtering on `kind`
    matters and is not cosmetic: a name can decode to more than one type across the dump, and
    `[v for v, k in find_all(...)]` with no filter quietly lets the wrong one through.

    Positional callers must pass `kind=None` when they need EVERY occurrence, including the
    undecodable (None, "?") ones - dropping those shifts every index past them (see find_all).
    """
    return [v for v, k in find_all(blob, name) if kind is None or k == kind]


def first(blob: bytes, name: str, kind: str | None = None, default=None):
    """The first MEANINGFUL value for a property name, or `default` if there is none.

    Meaningful means "not None and not an empty string", and it deliberately KEEPS False and 0,
    which are real settings. A name can occur in several blocks with only one of them filled in
    (systemFirmwareVersion is the standing example: it reads "" in one section and the version in
    another), so taking the literal first occurrence returns a blank often enough to matter.

    This was written out as an identical local closure three times - server.read_network_status,
    server.routing_model and profiles.save - each of them the same six lines.
    """
    for v in values(blob, name, kind):
        if v is not None and v != "":
            return v
    return default


def _legacy_find_all(blob: bytes, name: str):
    """The old raw substring search. ONLY for a dump the tree parser cannot read; it is kept
    because a truncated dump should degrade rather than raise, not because it is trustworthy."""
    key = name.encode()
    out, start = [], 0
    while True:
        i = blob.find(key, start)
        if i < 0:
            break
        val, kind = _val_after(blob, i + len(key))
        out.append((val, kind))
        start = i + len(key)
    return out


_ALNUM = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
def _clean_prop_name(name: str) -> str:
    """Strip a SELECTOR BYTE that has been absorbed into a property name.

    Every property name in this schema starts with a lowercase letter, so a leading digit, or a
    leading capital in front of a lowercase one, is the record's selector byte happening to be
    printable - the same artefact that once made the announcer only ever report channel 1 (see
    server._ann_is). Section headers are ALL CAPS and are left alone.
    """
    i = 0
    while i < len(name) and name[i].isdigit():
        i += 1
    name = name[i:]
    if len(name) > 1 and name[0].isupper() and name[1].islower() and not name.isupper():
        name = name[1:]
    return name


def iter_properties(blob: bytes):
    """Every property occurrence in a model dump, in stream order: (name, value, kind).

    The generic counterpart to find_all, which has to be told what to look for. This covers the
    whole dump instead, so it also finds properties nobody has decoded yet - which is what lets a
    profile be a complete RECORD of the desk rather than a record of the settings we happen to
    understand today.
    """
    idx = _index(blob)
    if idx is None:
        yield from _legacy_iter_properties(blob)
        return
    for _block, name, val, kind in idx.flat:
        yield name, val, kind


def iter_block_properties(blob: bytes):
    """Every property occurrence WITH THE BLOCK IT SITS IN: (block, name, value, kind).

    iter_properties throws the block away, which is fine while a name is unique and wrong the moment
    it is not: "the first one in the stream" is then a coin toss, because False and 0 are meaningful
    values and there is no way to tell a real answer from the wrong block's.

    A NAME CAN BE DUPLICATED TWO WAYS AND ONLY ONE IS PERMANENT. Legitimately, by design -
    `MIX`/`RCSYNCMIX` and `MIXMINUSES`/`RCSYNCMIXMINUES` share six property names, and NETWORK
    carries `ipAddress` / `wifiIpAddress` / `cellIpAddress` as separate names per transport. And
    accidentally, as a STRAY appended by one malformed write, which reads back like a real property
    and does nothing; `btVisible` was duplicated into SYSTEM that way for a while and went with a
    factory reset. Either way, anything reading a duplicated name must say which block it means.
    """
    idx = _index(blob)
    if idx is None:
        yield from _legacy_iter_block_properties(blob)
        return
    yield from idx.flat


def block_value(blob: bytes, block: str, name: str, instance: int = 0):
    """The value of `name` inside `block`, or None if that block does not carry it.

    `instance` picks which occurrence of an ARRAY block to read (HEADPHONE 0 = Headphone 1, and so
    on), counted over matching occurrences in dump order.
    """
    seen = -1
    for hdr, prop, val, _kind in iter_block_properties(blob):
        if hdr == block and prop == name:
            seen += 1
            if seen == instance:
                return val
    return None


MISSING = object()
"""Sentinel for path_value: "this node does not carry that property at all".

Not None, because None, False, 0 and "" are all REAL values a property can hold, and a caller
deciding whether to fall back to a default has to be able to tell "absent" from "absent-looking".
On a Duo that distinction is the whole ballgame: an input property does not exist until the first
write creates it (measured), so absent and zero mean opposite things.
"""


def node_at(blob: bytes, path):
    """The tree node at an ADDRESS, or None if the dump has no node there.

    `path` is the child-index list that `model_tree.encode_path` turns into the write address, so
    a caller reads exactly where it would write. Returns None (rather than falling back to the flat
    walk) when the dump will not parse: there is no honest positional equivalent of an address.
    """
    idx = _index(blob)
    return None if idx is None else idx.by_path.get(tuple(path))


def path_value(blob: bytes, path, name: str, default=None):
    """The value of `name` on the node at `path`, or `default` if the node or property is absent.

    THE ADDRESSED READ, and the counterpart to every build_* writer. Prefer it over `values()` +
    an index for anything that belongs to one channel, output or pad.

    WHY IT EXISTS (measured on both desks). `values()` returns every occurrence
    of a NAME in dump order and callers were picking one out by position. That is only correct while
    stream order happens to match the caller's numbering, and on the Pro II it did - its per-input
    write selectors ARE the INPUTSOURCE table entries, so occurrence N really was input N. The Duo
    proves it is luck: a write into a MIX node at 0x48+source_code CREATES the property there, and
    one of those sitting ahead of the table shifts every index past it, so one mic's gain is
    reported as another's. Nothing writes there now (both desks address INPUTSOURCE) but the litter
    is permanent - a used Duo still carries it, and
    only a factory reset removes it, so a positional read stays wrong there for good.
    """
    node = node_at(blob, path)
    if node is None:
        return default
    for pname, val, _kind in node.props:
        if pname == name:
            return val
    return default


def snapshot_properties(blob: bytes) -> dict:
    """{property name: [value, ...]} for EVERY property in a dump, values in stream order."""
    out: dict[str, list] = {}
    for name, val, _kind in iter_properties(blob):
        out.setdefault(name, []).append(val)
    return out


def _legacy_iter_properties(blob: bytes):
    """The old flat walk. ONLY for a dump the tree parser cannot read - see _index.

    Method: every value block starts \\x00\\x01, so we walk BACK from each one over the ASCII run
    that precedes it to get the name (a backwards walk, not a forward regex, because a forward
    scan cannot overlap and so truncates a name whose neighbour ended inside it).

    TWO KNOWN FIRMWARE ARTEFACTS, neither of which we can tidy away honestly:
      * a printable SELECTOR byte glued to the front of the name - handled by _clean_prop_name;
      * the first character of a name occasionally OVERWRITTEN by a framing byte, which is why
        rcp_read.read_output_modes anchors on the section header rather than on `outputMixMinus`.
        Measured on a live Pro II dump: 4 names out of 511 arrive one character short. They are
        recorded as they came, so the snapshot stays a faithful record of the stream.
    ALL-CAPS section headers (MIXMINUSES, AUDIO, ...) are structure, not properties, and are skipped.
    """
    for _header, name, val, kind in _legacy_walk(blob):
        if not name.isupper():              # ALL-CAPS entries are section headers, not properties
            yield name, val, kind


def _legacy_iter_block_properties(blob: bytes):
    """The old flat walk WITH the section header. ONLY for a dump the tree cannot read.

    Kept honest about what it gets wrong: it recovers a name by stepping back over alphanumerics,
    so it invents a phantom `iPOT` and truncates EFFECTS_PARAMETERS to PARAMETERS.
    """
    for header, name, val, kind in _legacy_walk(blob):
        if not name.isupper():
            yield header, name, val, kind


def _legacy_walk(blob: bytes):
    """The single flat scan both legacy readers run on, yielding (header, name, value, kind) and
    ALL-CAPS section headers as (header, HEADER, ...) so a caller can tell them apart.

    It was written out twice, the two copies differing only in whether they tracked the header.
    Both are dead weight in the normal case (the tree index serves every read) and only run for a
    dump the parser cannot read at all, which is exactly when having two subtly different scans
    would be hardest to reason about.
    """
    header = None
    i = 0
    while True:
        i = blob.find(b"\x00\x01", i)
        if i < 0:
            return
        j = i
        while j > 0 and blob[j - 1] in _ALNUM:
            j -= 1
        raw = blob[j:i]
        if len(raw) >= 3:
            # Clean BEFORE the letter test: a name whose absorbed selector byte is a digit
            # ("9systemMidiControl") starts with a digit until the selector is stripped.
            name = _clean_prop_name(raw.decode("latin1"))
            if len(name) >= 3 and name[0].isalpha():
                if name.isupper():
                    header = name
                    yield header, name, None, "?"
                else:
                    val, kind = _val_after(blob, i)
                    yield header, name, val, kind
        i += 2


# Channel order in the model stream (the unit's fixed input list).
CHANNELS = ["Mic1", "Mic2", "Mic3", "Mic4", "Bluetooth", "SMARTpads",
            "USB1", "Chat", "USB2"]


def read_settings() -> dict:
    """RAW STREAM-ORDER arrays plus MIDI and firmware. A diagnostic, NOT a per-channel read.

    The lists here are every occurrence of each name in dump order. DO NOT index them by channel,
    mic number or source code: that is only correct when the desk's per-input write addresses happen
    to be the INPUTSOURCE table entries, which is true on the Pro II and false on the Duo. Use
    channels.input_value, which addresses the channel's own node. This function is kept for the
    command-line dump below and for anyone eyeballing the raw shape.
    """
    blob = read_model()
    gains = values(blob, "inputMicrophoneGain")
    p48 = values(blob, "inputPower")
    # The real property is inputPhaseFlip (bool). Reading "inputPhase" matched INSIDE
    # "inputPhaseFlip" and decoded the value at the wrong offset (always None -> polarity
    # always read False); fixed to the full name so an inverted channel reads True.
    phase = values(blob, "inputPhaseFlip")
    mtype = values(blob, "inputMicrophoneType")
    midi = first(blob, "systemMidiControl")
    # systemFirmwareVersion reads "" in one section and the version in another, which is exactly
    # what `first` is for (it skips blanks and keeps False/0).
    fw = first(blob, "systemFirmwareVersion")
    return {
        "_bytes": len(blob),
        "inputMicrophoneGain": gains,
        "inputPower": p48,
        "inputPhaseFlip": phase,
        "inputMicrophoneType": mtype,
        "systemMidiControl": midi,
        "systemFirmwareVersion": fw,
    }


# read_mixer / read_channels (+ UI_CHANNELS, PROCESSING_READ_PROPS, PROCESSING_MODULE_BOOLS) moved
# to channels.py - the single source of truth for channel + processing ops.


# ---- Routing matrix read-back (the output Custom mixes) ---------------------
# On the CORRECTLY-ASSEMBLED stream (read_model strips the per-frame report id) the
# matrix is a clean grid: cell (output, source) -> the (13*source + output)th mixDisabled
# block, i.e. read_index == cross-point selector - 76. Verified for all 99 cells, zero
# mismatches. (The old calibrated map + mixMute special-case were artefacts of the framing
# bug splitting properties; matrix_read_map.json + calibrate_matrix.py are now obsolete.)
# See RODECASTER_PROTOCOL.txt section 5.

# Matrix shape (sources exposed, healthy mixDisabled block count, output count, read
# stride) is now per-device in devices.py (Pro II: 9 sources, 516 blocks, 11 outputs,
# stride 13). The block-count is a guard: if the live count differs the matrix changed
# shape (a SIP caller activated hidden cross-points) so we refuse rather than read by a
# now-wrong index. The read formula is index = row_stride * source + output.


def _stable_key(b: bytes):
    return (len(b),
            tuple(values(b, "mixDisabled")),
            tuple(values(b, "mixMute")))


def read_model_stable(max_try: int = 6, fresh: bool = False) -> bytes:
    """Read the model until two consecutive dumps agree (size + matrix state vectors),
    so a read that raced a write and came back truncated/partial is never returned.

    A cached dump counts as the FIRST of the two: if the cache already holds a fresh blob (no write
    since, still inside the TTL) only one more dump is needed to confirm it, rather than two from
    scratch. A cached blob that has already been stability-checked is returned outright.

    Pass fresh=True to ignore the cache entirely (the pad paths do: a stale stream position is
    a corruption hazard, so they always pay for real dumps).
    """
    cached_stable = None if fresh else _cache_get(need_stable=True)
    if cached_stable is not None:
        return cached_stable
    seed = None if fresh else _cache_get(need_stable=False)
    prev_key = _stable_key(seed) if seed is not None else None
    prev_blob = seed
    for n in range(1, max_try + 1):
        b = read_model(fresh=True)
        key = _stable_key(b)
        if prev_key is not None and key == prev_key:
            _cache_put(b, stable=True)
            if n > 2:
                diagnostics.event("model.stable", "settled after several reads", reads=n)
            return b
        prev_key, prev_blob = key, b
        time.sleep(0.15)
    # NEVER SETTLED. Worth a line of its own: on a healthy desk two reads agree, so exhausting the
    # attempts means either the desk is being written to throughout or it is answering differently
    # every time, and the blob handed back is then the LAST one rather than an agreed one.
    diagnostics.event("model.unstable", "no two reads agreed", reads=max_try,
                      last_bytes=len(prev_blob) if prev_blob is not None else 0)
    return prev_blob if prev_blob is not None else read_model(fresh=True)


def read_output_modes(blob: bytes) -> list:
    """The 11 output modes (0=Main Mix, 1=Mix Minus, 2=Custom), index = output index.
    Anchors on the MIXMINUSES section header (intact even when the outputMixMinus name
    is corrupted by a framing byte). Returns [] if the array is not found cleanly."""
    out = []
    for m in _MIXMINUSES_HDR.finditer(blob):
        seg = blob[m.end():m.end() + 34]
        vh = seg.find(b"\x01\x05\x01")           # int value header
        if vh >= 0:
            out.append(seg[vh + 3])              # low byte of the int = the mode
    return out


def master_processing(blob: bytes) -> dict:
    """The master bus's own processing, as the SEVEN keys the page and a profile both use.

    Written out twice (server.routing_model built it for the Routing page, profiles.save built the
    identical block for a backup), which meant a property added to one and not the other would
    silently be a setting the page could change and a profile could not put back. One source now,
    and the key names are the wire format the browser and the profile JSON both already speak, so
    both callers splat this straight in.
    """
    return {
        "compellor_on": first(blob, "masterCompellorOn"),
        "compellor_threshold": first(blob, "masterCompellorThreshold"),
        "compellor_attack": first(blob, "masterCompellorAttack"),
        "compellor_release": first(blob, "masterCompellorRelease"),
        "compellor_gain": first(blob, "masterCompellorGain"),
        "delay_on": first(blob, "masterDelayOn"),
        "delay_seconds": first(blob, "masterDelaySeconds"),
    }


def _read_matrix(blob, prop: str, key: str, cell, cannot: str) -> dict:
    """The matrix grid read, and the ONE copy of it. Reads `prop` for every cross-point by the
    proven formula (read_index = row_stride * source + output) and passes each raw cell through
    `cell` to get what the caller wants out of it.

    read_matrix_state and read_matrix_levels were two forty-line functions differing only in the
    property name, the result key and how one cell decodes; they are also nearly always called as a
    PAIR (profiles.save and routing_schemes.capture both do), so the duplication was paid twice per
    read. Merged.

    BOTH GUARDS ARE LOAD-BEARING and neither is cosmetic:
      * the device cap - a desk whose matrix read is not verified refuses rather than reading by an
        index that may not mean what it does on a Pro II;
      * the block count - if the matrix changes SHAPE (a live SIP caller activates hidden
        cross-points) the grid order shifts, so every cell would be attributed to the wrong pair.
    A refusal comes back as ok False with a sentence, never as a wrong grid.
    """
    dev = devices.get_active_device()
    m = dev.matrix
    empty = {"ok": False, key: {}, "modes": {}}
    if "matrix_read" not in dev.caps:
        return dict(empty, reason=(f"The routing matrix read is not yet verified on the "
                                   f"{dev.name}, so {cannot} cannot be shown reliably yet."))
    if blob is None:
        blob = read_model_stable()
    raw = values(blob, prop)
    modes_all = read_output_modes(blob)
    if len(raw) != m.cell_count or len(modes_all) < m.output_count:
        return dict(empty, reason=("The matrix changed shape (a phone caller may be connected), "
                                   f"so the live {cannot} cannot be read reliably right now."))
    out = {}
    for o in range(m.output_count):
        out[o] = {}
        for s in m.source_indices:
            i = m.row_stride * s + o
            out[o][s] = cell(raw[i] if i < len(raw) else None)
    return {"ok": True, key: out, "reason": "",
            "modes": {o: int(modes_all[o]) for o in range(m.output_count)}}


def read_matrix_state(blob: bytes | None = None) -> dict:
    """Current matrix state, app-free: per-cross-point EXCLUDED state plus each output's MODE.

    Returns {"ok": bool, "reason": str, "excluded": {output: {source: bool}},
             "modes": {output: 0|1|2}}. See _read_matrix for the guards.
    """
    return _read_matrix(blob, "mixDisabled", "excluded", lambda v: v is True, "it")


def _decode_level_cell(cell) -> dict:
    """One mixLevelWithAnchor cell: the ASCII string 'level|anchor' (0..1). level == anchor means
    the cross-point is LINKED (it follows the channel fader); they differ when it has been unlinked
    at a manual level. Anything that will not parse comes back as two Nones rather than a guess."""
    if isinstance(cell, str) and "|" in cell:
        a, b = cell.split("|", 1)
        try:
            return {"level": float(a), "linked": (a == b)}
        except ValueError:
            pass
    return {"level": None, "linked": None}


def read_matrix_levels(blob: bytes | None = None) -> dict:
    """Current per-cross-point LEVEL (and link state) for the matrix.

    Returns {"ok", "reason", "levels": {output: {source: {"level": float|None, "linked": bool}}},
             "modes": {output: 0|1|2}}. See _read_matrix for the guards.
    """
    return _read_matrix(blob, "mixLevelWithAnchor", "levels", _decode_level_cell, "levels")


if __name__ == "__main__":
    # Named per channel via the ADDRESSED read, so this prints the same values the app shows on
    # whichever desk is attached. NEVER label the raw stream-order list with the Pro II channel
    # names: that is the mis-attribution this module exists to prevent.
    import channels as _ch
    import devices as _dev

    s = read_settings()
    d = _dev.get_active_device()
    print(f"device: {d.name}")
    print(f"stream bytes: {s['_bytes']}")
    print(f"systemFirmwareVersion : {s['systemFirmwareVersion']}")
    print(f"systemMidiControl     : {'ON' if s['systemMidiControl'] else 'OFF'} ({s['systemMidiControl']})")
    blob = read_model()
    print(f"{'channel':<12} {'gain/level':>10} {'48V':>5} {'polarity':>9} {'type':>5}")
    for entry in d.ui_channels:
        mic = entry.get("kind") == "mic"
        lvl = _ch.input_value(blob, entry,
                              "inputMicrophoneGain" if mic else "inputDigitalGain", d)
        print(f"{entry['label']:<12} {str(lvl):>10} "
              f"{str(_ch.input_value(blob, entry, 'inputPower', d)):>5} "
              f"{str(_ch.input_value(blob, entry, 'inputPhaseFlip', d)):>9} "
              f"{str(_ch.input_value(blob, entry, 'inputMicrophoneType', d)):>5}")
