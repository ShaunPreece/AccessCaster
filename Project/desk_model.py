"""AccessCaster - the one picture of the desk: every property it publishes, held once, kept current.

THE PICTURE IS BUILT FROM WHAT THE APP ALREADY DOES, and reads nothing of its own:
  - every complete model dump any reader takes is adopted (`rcp_read.dump_watcher`);
  - every push the listener hears is applied before any subscriber hears of it (`desk_live`), so a
    subscriber that looks here sees the value it was told about;
  - every write this app sends is applied as it goes out (`rcp_hid.write_watcher`), because the desk
    does not echo all of them: a software solo pushes nothing.
No other software's write is pushed either, RODE's app's included, so while it is open (`reveal`)
each adopted dump is compared with the picture and what changed unheard is handed on as pushes.

A DUMP IS A SNAPSHOT OF A DESK THAT KEEPS CHANGING while it is read, for a second and more. Every
push and write is journalled with a sequence number, and a dump is adopted with everything
journalled after it started replayed on top, so a change made during the read is not lost to it.

TRUSTED (`trusted`) means the listener has the desk open, a dump has been adopted since the desk
last appeared (`rcp_hid.connection_epoch`), and no STRUCTURAL change has arrived, and nothing has
called `distrust`, since that dump started. A picture that is not trusted still answers with the last values it had, which is right
for naming (the last fader layout seen beats "Channel 4") and wrong for acting: a caller about to
toggle something or address a write checks `trusted()` and reads the desk when it is not, and that
read is adopted here like any other.

A WRITE IS APPLIED ONLY AT A FIXED NODE (`model_tree.Layout`) THAT ALREADY CARRIES THE PROPERTY. A
pad edit is written at the edit cursor, whose path is not the pad it edits, and a write to a
property the node lacks is a stray, not a value.

THE PICTURE CAN BE READ AS A DUMP (`snapshot`): the adopted dump with every change since written
into it, so the decoders that read a dump answer for the desk as it is now without reading it, and
the window's state is built by the same code whether it came from a read or a push. A property the
dump did not carry has no place in it and is left out.

The state is module-level, as the listener's is: there is one desk.
"""
from __future__ import annotations

import sys
import threading
from collections import deque
from typing import NamedTuple, Optional

import model_tree
import rcp_hid
import rcp_read
import rcp_transfer

# Changes after which the rest of the picture cannot be trusted until it is read again: a fader's
# source moves which channel a strip's values belong to, the USB mode re-enumerates the desk and
# reassigns sources, and a mic's type moves its gain and its chain.
STRUCTURAL = frozenset({"channelInputSource", "outputMultiMode", "inputMicrophoneType"})
# Enough for the longest burst measured, a factory reset's 3267 pushes, during one dump.
JOURNAL_MAX = 8192

_FADER_MAX = rcp_transfer.FADER_LEVEL_MAX
_POT_MAX = 127.0                 # each POT node publishes potMin 0 and potMax 127


class Token(NamedTuple):
    """Where the journal stood when a dump started, and on which connection."""
    seq: int
    epoch: int


_lock = threading.Lock()
_props: dict = {}                # path tuple -> {name: (value, kind)}
_names: dict = {}                # name -> [path tuple], in dump order
_layout: Optional[model_tree.Layout] = None
_epoch = -1                      # the connection epoch of the adopted dump
_read_seq = -1                   # the journal position the adopted dump started at; -1 none usable
_stale_seq = 0                   # the journal position of the last structural change
_seq = 0
_journal: deque = deque(maxlen=JOURNAL_MAX)    # (seq, path, name, value, kind, may_add)
_connected = False
_blob = b""                      # the adopted dump
_spans: dict = {}                # (path, name) -> (start, end) of its value block in `_blob`
_dirty: set = set()              # (path, name) changed since the dump was adopted
_snap: Optional[bytes] = None    # `snapshot`, until the next change
_reveal = False                  # `reveal`
# Called with [(path, name, value, kind, was)] for what an adopted dump showed the desk changed
# without a push saying so, while `reveal` is on. Set by `desk_live`, which hands them on as pushes.
revealed_watcher = None


# ---- what feeds it ---------------------------------------------------------------------------

def dump_starting() -> Token:
    with _lock:
        return Token(_seq, rcp_hid.connection_epoch())


def dump_finished(token: Token, blob: bytes) -> None:
    """Adopt a complete dump that started at `token`. A dump from an earlier connection is ignored:
    the desk may have changed under it."""
    if token.epoch != rcp_hid.connection_epoch():
        return
    found = []
    try:
        root, _used, _tail = model_tree.parse(blob, found)
    except Exception:
        return
    props, names, spans, paths = {}, {}, {}, {}
    for node in model_tree.walk(root):
        paths[id(node)] = tuple(node.path())
        props[paths[id(node)]] = {}
    for node, name, start, end in found:
        path = paths[id(node)]
        if name not in props[path]:
            props[path][name] = next((v, k) for p, v, k in node.props if p == name)
            names.setdefault(name, []).append(path)
            spans[(path, name)] = (start, end)
    book = model_tree.Layout(root)
    global _props, _names, _layout, _epoch, _read_seq, _blob, _spans, _dirty, _snap
    with _lock:
        if token.epoch != rcp_hid.connection_epoch():
            return
        complete = _seq - token.seq <= len(_journal)
        dirty = set()
        for seq, path, name, value, kind, may_add in _journal:
            if seq > token.seq and name is not None:
                _put(props, names, path, name, value, kind, may_add)
                dirty.add((path, name))
        unheard = _unheard(props, dirty, token) if _reveal else []
        _props, _names, _layout, _epoch = props, names, book, token.epoch
        _blob, _spans, _dirty, _snap = blob, spans, dirty, None
        # A journal that overflowed during the read cannot say what the read missed.
        _read_seq = token.seq if complete else -1
    if unheard and revealed_watcher is not None:
        try:
            revealed_watcher(unheard)
        except Exception as exc:
            print(f"desk_model: revealed changes not handed on: {exc}", file=sys.stderr)


def reveal(on: bool) -> None:
    """While on, every dump adopted on a trusted picture is compared with it, and what the desk
    changed without pushing goes to `revealed_watcher`. For while RODE's app is open: the desk
    pushes none of its writes, so the picture cannot follow them any other way."""
    global _reveal
    _reveal = on


def _unheard(props, told, token: Token) -> list:
    """What `props`, a dump just read, holds differently from the picture, leaving out what was
    journalled during the read (`told`, already heard) and everything when the picture is not
    trusted: then a difference is the picture's gap, not news. Caller holds `_lock`."""
    if not (_epoch == token.epoch and 0 <= _read_seq and _stale_seq <= _read_seq):
        return []
    out = []
    for path, held in _props.items():
        now = props.get(path)
        if now is None:
            continue
        for name, (was, _kind) in held.items():
            got = now.get(name)
            if got is not None and got[0] != was and (path, name) not in told:
                out.append((path, name, got[0], got[1], was))
    return out


def apply_push(path, name: str, value, kind: str):
    """Apply one push. Returns the value it replaced, or None when there was none."""
    global _seq, _stale_seq, _snap
    path = tuple(path)
    with _lock:
        _seq += 1
        _journal.append((_seq, path, name, value, kind, True))
        was = _put(_props, _names, path, name, value, kind, True)
        _dirty.add((path, name))
        _snap = None
        if name in STRUCTURAL and was != value:
            _stale_seq = _seq
    return was


def apply_write(frame: bytes):
    """Apply one report-3 record this app has just written. Returns (path, name, value, kind,
    was) when it landed on the picture, or None for a record that names no fixed node carrying
    that property, or is not a property record at all."""
    global _seq, _stale_seq, _snap
    if not frame or frame[0] != rcp_hid.REPORT_BULK_OUT:
        return None
    n = int.from_bytes(frame[1:5], "little")
    try:
        path, name, value, kind, _end = model_tree.parse_record(frame[5:5 + n])
    except ValueError:
        return None
    path = tuple(path)
    with _lock:
        if (_layout is None or _layout.locate(path) is None
                or name not in _props.get(path, ())):
            return None
        _seq += 1
        _journal.append((_seq, path, name, value, kind, False))
        was = _put(_props, _names, path, name, value, kind, False)
        _dirty.add((path, name))
        _snap = None
        if name in STRUCTURAL and was != value:
            _stale_seq = _seq
    return path, name, value, kind, was


def distrust() -> None:
    """Untrust the picture until a dump that starts after now is adopted: for a change the desk
    does not push, such as a fader moved in RODE's app or a show load rebuilding the desk. It is
    journalled, as a marker with no property, so a dump already under way cannot restore trust."""
    global _seq, _stale_seq
    with _lock:
        _seq += 1
        _journal.append((_seq, None, None, None, None, False))
        _stale_seq = _seq


def set_connected(on: bool) -> None:
    """The listener has the desk open, or has lost it. Called by `desk_live` before it tells
    anyone else."""
    global _connected
    _connected = on


def _put(props, names, path, name, value, kind, may_add):
    node = props.get(path)
    if node is None:
        if not may_add:
            return None
        node = props[path] = {}
    held = node.get(name)
    if held is None:
        if not may_add:
            return None
        names.setdefault(name, []).append(path)
    node[name] = (value, kind)
    return held[0] if held is not None else None


# ---- what it answers -------------------------------------------------------------------------

def trusted() -> bool:
    """Whether the picture is the desk as it is now: see the module docstring."""
    with _lock:
        return (_connected and bool(_props) and _epoch == rcp_hid.connection_epoch()
                and 0 <= _read_seq and _stale_seq <= _read_seq)


def snapshot() -> bytes:
    """The picture as a model dump: the adopted dump with every change since written into it, its
    declared length corrected to match. b"" before the first dump."""
    global _snap
    with _lock:
        if _snap is None:
            _snap = _written_into(_blob)
        return _snap


def _written_into(blob: bytes) -> bytes:
    """`blob` with the current value of every changed property in place of its old one. Caller
    holds `_lock`."""
    edits = sorted((_spans[key], model_tree.encode_value(*_props[key[0]][key[1]]))
                   for key in _dirty if key in _spans)
    out, pos = bytearray(), 0
    for (start, end), block in edits:
        out += blob[pos:start]
        out += block
        pos = end
    out += blob[pos:]
    # The stream header's first four bytes are the dump's length after them (`model_tree.parse`).
    grew = len(out) - len(blob)
    if grew and blob.find(b"Rodecaster\x00") >= 4:
        out[:4] = (int.from_bytes(blob[:4], "little") + grew).to_bytes(4, "little")
    return bytes(out)


def layout() -> Optional[model_tree.Layout]:
    """The layout of the adopted dump, or None before the first."""
    return _layout


def value(key, name: str, default=None):
    """The value of `name` on the node `key` names (`model_tree.Layout`), or `default`."""
    with _lock:
        if _layout is None:
            return default
        try:
            path = tuple(_layout.path(key))
        except model_tree.AddressUnknown:
            return default
        held = _props.get(path, {}).get(name)
    return held[0] if held is not None else default


def values(name: str, kind: Optional[str] = None) -> list:
    """Every value of `name`, in dump order, optionally only those of one `kind`: the picture's
    counterpart to `rcp_read.values`."""
    with _lock:
        held = [_props[p][name] for p in _names.get(name, ())]
    return [v for v, k in held if kind is None or k == kind]


def first(name: str, kind: Optional[str] = None, default=None):
    """The first meaningful value of `name` (not None, not ""), as `rcp_read.first` reads a dump."""
    for v in values(name, kind):
        if v is not None and v != "":
            return v
    return default


def strip_vector() -> Optional[list]:
    """The fader layout, `channelInputSource` by strip, or None before the first read."""
    return values("channelInputSource") or None


def mutes() -> dict:
    """{strip: muted} for every channel strip the picture holds."""
    book = _layout
    count = book.count("CHANNEL") if book is not None else 0
    out = {}
    for strip in range(count):
        on = value(rcp_transfer.channel_key(strip), "channelOutputMute")
        if isinstance(on, bool):
            out[strip] = on
    return out


def fader_level(index: int) -> Optional[float]:
    """A fader's level, 0 to 1, or None. Only the virtual faders carry one."""
    v = value(rcp_transfer.fader_key(index), "faderLevel")
    return max(0.0, min(1.0, v / _FADER_MAX)) if isinstance(v, int) else None


def monitor_level() -> Optional[float]:
    v = value(rcp_transfer.OUTPUT, "outputMonLevel")
    return v if isinstance(v, float) else None


def pot_level(index: int) -> Optional[float]:
    """A headphone knob's level, 0 to 1, or None."""
    v = value(("PHYSICALINTERFACE", 0, "POT", int(index)), "potLevel")
    return max(0.0, min(1.0, v / _POT_MAX)) if isinstance(v, int) else None


def selected_channel() -> Optional[int]:
    """The strip a channel button has given the encoder, -1 for none, None when unknown."""
    return first("systemChannelSelected", "int")


def bank() -> Optional[int]:
    """The pad bank the desk is on, 0-based, or None when unknown."""
    return first("selectedBank", "int")


def record_state() -> Optional[int]:
    return first("recordState", "int")


def usb_mode() -> Optional[int]:
    """`outputMultiMode`, or None when unknown."""
    return first("outputMultiMode", "int")


def firmware() -> Optional[str]:
    return first("systemFirmwareVersion", "str")


def online() -> Optional[bool]:
    """Whether the unit reaches the internet, by wifi or cable, or None before the picture holds
    the NETWORK block. A wifi address means joined; the radio being on does not. The address is
    read on its block, as `network.connection` reads a dump: a stray of the same name can sit in
    another."""
    book = _layout
    if book is None or not book.has(rcp_transfer.NETWORK):
        return None
    wired = next((v for v in values("wiredConnected") if v is not None), None)
    return bool(value(rcp_transfer.NETWORK, "wifiIpAddress")) or bool(wired)


def _reset() -> None:
    """Forget everything. For offline tests."""
    global _props, _names, _layout, _epoch, _read_seq, _stale_seq, _seq, _connected
    global _blob, _spans, _dirty, _snap, _reveal
    with _lock:
        _reveal = False
        _props, _names, _layout = {}, {}, None
        _blob, _spans, _dirty, _snap = b"", {}, set(), None
        _epoch, _read_seq, _stale_seq, _seq, _connected = -1, -1, 0, 0, False
        _journal.clear()


rcp_read.dump_watcher = sys.modules[__name__]
