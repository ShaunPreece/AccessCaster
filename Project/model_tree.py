"""THE MODEL IS A SELF-DESCRIBING TREE. This parses it properly, and the parse is its own proof.

THE GRAMMAR, and every byte of the dump is accounted for by it:

    node     := NAME "\\0" count(P) property x P  count(C) node x C
    property := name "\\0" 0x01 <vlen> <payload, vlen bytes; payload[0] = type>
    count(n) := <length byte L> <n, L bytes little-endian>      # L = 0 means the count is 0

A parent's child COUNT sits just before its first child's name, and a node with no children still
carries a count of zero, so nothing is special-cased: SOUNDPADS is an ordinary node with 0
properties and 24 children.

WHY THIS LIVES IN Project/ AND NOT DevTools/: it is not a probing tool, it is THE reader. `rcp_read`
builds its whole index from it, so every value the app shows comes through this parser, and
`DevTools/tree_tool.py` is a command line over it.

DO NOT REPLACE IT WITH A BYTE SCAN FOR NAMES. A scan that finds a name by stepping back from a value
marker invents properties (a phantom `iPOT`), cuts EFFECTS_PARAMETERS at the underscore, and cannot
decode the 64-bit ints, composites or long strings (`fxPresetContents`, 316 bytes). The tree parse
of a 143 KB dump takes about 10 ms, roughly nine times faster than such a scan.

THE WRITE ADDRESS IS THE TREE PATH, not a flat number:

    01 01 <levels> [ <len> <index, len bytes LE> ] x levels

`levels` is the DEPTH. A child of the root takes ONE index (which is why every selector ever
confirmed equals its root-child position: 0x1C = CHANNEL[0], 0x026E = INPUTSOURCE[0] as a two-byte
index), and a node one level deeper takes TWO - exactly the pad path `01 01 02 02 9a 02 01 <n>`:
root child 0x029A = SOUNDPADS, then child n = PAD[n].

PAD[n] is not a root child, so counting nodes in dump order does not give an address past it.

A POSITION IS NEVER WRITTEN DOWN, IT IS LOOKED UP. `Layout` names every fixed node by block and
copy, ("CHANNEL", 3), and answers its path on the tree it was built from. A firmware that grows one
block moves every block after it, and a write to a stale position lands on a different, valid node
or appends a stray that only a factory reset clears.

NOTHING HERE WRITES TO THE DEVICE.
"""
from __future__ import annotations

import struct
from collections import Counter


class Node:
    __slots__ = ("name", "props", "kids", "parent", "index")

    def __init__(self, name):
        self.name = name
        self.props = []          # [(prop name, value, kind)]
        self.kids = []           # [Node]
        self.parent = None
        self.index = 0           # position among its parent's children

    # ---- addressing ----------------------------------------------------------------------
    def path(self) -> list:
        """Child indices from the root down to this node. [] for the root itself."""
        out, n = [], self
        while n.parent is not None:
            out.append(n.index)
            n = n.parent
        return list(reversed(out))

    def address(self) -> bytes:
        """The report-3 address bytes for this node. See `encode_path`."""
        return encode_path(self.path())

    def __repr__(self):
        return f"<{self.name} path={self.path()} props={len(self.props)} kids={len(self.kids)}>"


def encode_index(i: int, empty_zero: bool = False) -> bytes:
    """One index of a path: a length byte, then the index little-endian in that many bytes.

    THE LENGTH RULE: the fewest bytes that hold it, MINIMUM ONE, so zero is `01 00`. The grammar
    also allows a zero-length count for 0 (`00`), which the desk itself pushes and RODE's app sends;
    `empty_zero` asks for that form, and only the records proven in it use it (see `encode_path`).
    """
    i = int(i)
    if i < 0:
        raise ValueError(f"negative index: {i}")
    if i == 0 and empty_zero:
        return b"\x00"
    n = 1 if i <= 0xFF else 2 if i <= 0xFFFF else 4
    return bytes([n]) + i.to_bytes(n, "little")


def encode_path(path, empty_zero: bool = False) -> bytes:
    """The report-3 ADDRESS BYTES for a tree path: `01 01 <levels> [<len><index LE>] x levels`.

    THE ONE ADDRESS FORMAT. Every address shape `rcp_transfer` builds is a special case of this:

        01 01 01 01 1c            CHANNEL[0]                 -> path [0x1C]
        01 01 01 02 6e 02         INPUTSOURCE[0]             -> path [0x26E]
        01 01 02 02 9a 02 01 <n>  a pad                      -> path [0x29A, n]
        01 01 02 02 9a 02 00      the pad edit cursor        -> path [0x29A, 0], empty_zero
        01 01 02 00 01 <n>        a fader's level            -> path [0, n], empty_zero

    A two-byte index is why an address can look like a selector with a "trailer": `02 6e 02` is
    0x026E, and the last byte is its high byte.

    `empty_zero` sends a zero index as a zero-length count. The hardware has proven each form only
    where it is used: `01 00` on every pad write, the empty form on the edit cursor and on
    faderLevel, which the desk ignores in any other address shape. Do not move a record from one
    form to the other on the one address family where a bad write can append a stray that only a
    factory reset clears.
    """
    out = bytearray([0x01, 0x01, len(path)])
    for i in path:
        out += encode_index(i, empty_zero)
    return bytes(out)


class AddressUnknown(LookupError):
    """The tree has no such node, or the node does not carry the property to be written there."""


# Root children whose OWN children are fixed by the firmware too, so a `Layout` names them. Every
# other list below the root (pads, recordings, shows, scan results, effect objects) grows and shrinks
# while the desk runs, so its positions are read from a fresh dump at the moment of use.
FIXED_CHILDREN = ("PHYSICALINTERFACE",)


class Layout:
    """WHERE EVERY FIXED NODE SITS in one parsed tree: the lookup every write address and every
    pushed address goes through.

    A node is named by a KEY, never by a number: `("CHANNEL", 3)` is the fourth CHANNEL among the
    root's children, and `("PHYSICALINTERFACE", 0, "FADER", 6)` the seventh FADER inside it,
    wherever this firmware puts them. The copy counts over nodes of that name only, so a block
    appended elsewhere moves no key.

    `path(key, prop)` REFUSES what the tree does not have: a missing block, a copy past the last,
    and a property the node does not carry. The last is the stray guard. Every property the app
    writes is published on its node before the first write, on both desks, so a name the node lacks
    is the wrong node.

    Holds paths and property names only, not the tree, so it outlives the dump it was read from.
    """

    __slots__ = ("_by_key", "_by_path", "_counts")

    def __init__(self, root: Node):
        self._by_key = {}       # key -> (path tuple, frozenset of property names)
        self._by_path = {}      # path tuple -> key
        self._counts = Counter()
        seen = Counter()
        for kid in root.kids:
            key = (kid.name, seen[kid.name])
            seen[kid.name] += 1
            self._add(key, (kid.index,), kid)
            if kid.name in FIXED_CHILDREN:
                inner = Counter()
                for g in kid.kids:
                    sub = key + (g.name, inner[g.name])
                    inner[g.name] += 1
                    self._add(sub, (kid.index, g.index), g)

    def _add(self, key: tuple, path: tuple, node: Node) -> None:
        self._by_key[key] = (path, frozenset(p for p, _v, _k in node.props))
        self._by_path[path] = key
        self._counts[key[:-1]] += 1

    def __eq__(self, other) -> bool:
        """Same nodes at the same paths. Property sets are left out: a stray adds a name, not a node."""
        return isinstance(other, Layout) and self._by_path == other._by_path

    __hash__ = None

    def path(self, key: tuple, prop: str | None = None) -> list:
        """The tree path of `key`, checked to carry `prop` when one is named. Raises AddressUnknown."""
        entry = self._by_key.get(tuple(key))
        if entry is None:
            raise AddressUnknown(f"this desk has no {format_key(key)} "
                                 f"(it has {self.count(*key[:-1])} of that block)")
        path, props = entry
        if prop is not None and prop not in props:
            raise AddressUnknown(f"{format_key(key)} carries no {prop}: writing it there would "
                                 f"append a stray")
        return list(path)

    def locate(self, path) -> tuple | None:
        """The key at a tree path, or None. The reverse of `path`, for naming a pushed address."""
        try:
            return self._by_path.get(tuple(int(i) for i in path))
        except (TypeError, ValueError):
            return None

    def count(self, *block) -> int:
        """How many copies of a block there are: `count("MIX")`, or
        `count("PHYSICALINTERFACE", 0, "FADER")` for one inside a fixed parent."""
        return self._counts.get(tuple(block), 0)

    def has(self, key: tuple, prop: str | None = None) -> bool:
        try:
            self.path(key, prop)
            return True
        except AddressUnknown:
            return False


def format_key(key) -> str:
    """("CHANNEL", 3) -> "CHANNEL[3]"; a nested key joins with a dot."""
    key = tuple(key)
    return ".".join(f"{key[i]}[{key[i + 1]}]" for i in range(0, len(key) - 1, 2))


def _count(blob: bytes, i: int):
    """A length-prefixed little-endian integer. Returns (value, next index).

    THE ONE PRIMITIVE THE WHOLE FORMAT IS BUILT FROM. A child count, a property count and a value's
    own length are all this same shape, which is why `fxPresetContents` (a 316-byte string, written
    `02 3c 01 05 ...`) parses with no special case while a reader that only ever takes a ONE-byte
    length cannot see it at all.
    """
    ln = blob[i]
    return int.from_bytes(blob[i + 1:i + 1 + ln], "little"), i + 1 + ln


def _value(blob: bytes, i: int):
    """Decode one value block. Returns (value, kind, next index). payload[0] is the type byte."""
    vlen, j = _count(blob, i)
    payload = blob[j:j + vlen]
    end = j + vlen
    if not payload:
        return None, "?", end
    t = payload[0]
    if t == 0x01:
        return int.from_bytes(payload[1:5], "little", signed=True), "int", end
    if t in (0x02, 0x03):
        return t == 0x02, "bool", end
    if t == 0x04:
        return struct.unpack("<d", payload[1:9])[0] if len(payload) >= 9 else None, "float", end
    if t == 0x05:
        return payload[1:].split(b"\x00")[0].decode("latin1"), "str", end
    if t == 0x06:
        # 64-bit int. STORAGEVOLUME[2] carries its capacity and free space in it, where the SAME
        # property name is a 32-bit 0x01 on the other two volumes: a property's type is per
        # OCCURRENCE, not per name.
        return int.from_bytes(payload[1:9], "little", signed=True), "int64", end
    if t == 0x08:
        # A COMPOSITE: two or more complete value blocks back to back (padRecordStateRequest is an
        # int followed by a bool). Decoded recursively so the parts are readable.
        parts, j = [], 0
        inner = payload[1:]
        while j < len(inner):
            v, k, j = _value(inner, j)
            parts.append((v, k))
        return parts, "pair", end
    return payload[1:], f"raw{t:#04x}", end


def _encode_count(n: int) -> bytes:
    """`_count`'s inverse: the fewest bytes that hold `n`, and a zero length for 0."""
    if n == 0:
        return b"\x00"
    ln = 1 if n <= 0xFF else 2 if n <= 0xFFFF else 4
    return bytes([ln]) + n.to_bytes(ln, "little")


def encode_value(value, kind: str) -> bytes:
    """A value block, `_value`'s inverse: the count, then the payload with its type byte first.

    THE ONE VALUE ENCODER, for a write (`rcp_transfer.value_block`) and for the desk model's
    snapshot alike. A kind `_value` could not decode (`raw0x..`) goes back as the bytes it kept."""
    if kind == "int":
        payload = b"\x01" + int(value).to_bytes(4, "little", signed=True)
    elif kind == "bool":
        payload = b"\x02" if value else b"\x03"
    elif kind == "float":
        payload = b"\x04" + (struct.pack("<d", float(value)) if value is not None else b"")
    elif kind == "str":
        payload = b"\x05" + str(value).encode("latin1") + b"\x00"
    elif kind == "int64":
        payload = b"\x06" + int(value).to_bytes(8, "little", signed=True)
    elif kind == "pair":
        payload = b"\x08" + b"".join(encode_value(v, k) for v, k in value)
    elif kind.startswith("raw"):
        payload = bytes([int(kind[3:], 16)]) + bytes(value)
    elif kind == "?":
        payload = b""
    else:
        raise ValueError(f"no value block for kind {kind!r}")
    return _encode_count(len(payload)) + payload


def _read_name(blob: bytes, i: int):
    j = blob.find(b"\x00", i)
    if j < 0:
        raise ValueError(f"unterminated name at {i}")
    return blob[i:j], j + 1


def _parse_node(blob: bytes, i: int, parent, index: int, spans) -> tuple:
    name, i = _read_name(blob, i)
    node = Node(name.decode("latin1"))
    node.parent, node.index = parent, index
    nprops, i = _count(blob, i)
    for _ in range(nprops):
        pname, i = _read_name(blob, i)
        start = i
        val, kind, i = _value(blob, i)
        node.props.append((pname.decode("latin1"), val, kind))
        if spans is not None:
            spans.append((node, node.props[-1][0], start, i))
    nkids, i = _count(blob, i)
    for k in range(nkids):
        kid, i = _parse_node(blob, i, node, k, spans)
        node.kids.append(kid)
    return node, i


MAX_PATH_DEPTH = 8   # the deepest address ever seen is 2 (SOUNDPADS then the pad); 8 is head-room


def parse_record(buf: bytes) -> tuple:
    """One ADDRESSED PROPERTY RECORD, the shape of both our writes and the desk's own pushes:

        01 01 <levels> [ <len> <index, len bytes LE> ] x levels  <name> "\\0" <value block>

    Returns (path, name, value, kind, end), where `end` is the index one past the record, so a
    caller can check the record accounted for EXACTLY the bytes it was told to expect.

    IT IS A TEST AS WELL AS A DECODER, which is why every malformed input raises ValueError rather
    than returning something partial: `rcp_read._push_record` asks "is this whole report-04 frame
    one complete record?" to tell an asynchronous push from a slice of the model dump, and a decoder
    that guessed would answer yes on dump content.
    """
    try:
        if buf[:2] != b"\x01\x01":
            raise ValueError("not an addressed record: no 01 01 opener")
        levels = buf[2]
        if not 1 <= levels <= MAX_PATH_DEPTH:
            raise ValueError(f"implausible path depth {levels}")
        i, path = 3, []
        for _ in range(levels):
            idx, i = _count(buf, i)
            path.append(idx)
        name, i = _read_name(buf, i)
        if not name or not all(0x20 <= c < 0x7F for c in name):
            raise ValueError(f"not a property name: {name!r}")
        val, kind, i = _value(buf, i)
        if i > len(buf):
            raise ValueError("value block runs past the end of the record")
    except (IndexError, struct.error) as exc:
        raise ValueError(f"truncated record: {exc}") from exc
    return path, name.decode("latin1"), val, kind, i


def parse(blob: bytes, spans: list | None = None) -> tuple:
    """(root, bytes consumed, trailing bytes). The root name is preceded by a 5-byte stream header
    (`53 2f 02 00 02` on this firmware) that is not part of the tree. `spans`, when given, gets
    (node, property name, start, end) for every property: where its value block sits in `blob`.

    THE FIRST FOUR OF THOSE FIVE BYTES ARE THE DUMP'S OWN LENGTH, little-endian, counting everything
    after them. That is what lets `rcp_read.read_model_raw` end the read on structure instead of on silence.
    """
    start = blob.find(b"Rodecaster\x00")
    if start < 0:
        raise ValueError("no root node in this blob")
    root, end = _parse_node(blob, start, None, 0, spans)
    return root, end - start, blob[end:]


def walk(node: Node):
    """Pre-order: every node including the root."""
    yield node
    for k in node.kids:
        yield from walk(k)


def instances(root: Node, name: str) -> list:
    """Every node with this block name, in dump order."""
    return [n for n in walk(root) if n.name == name]


def find_property(root: Node, prop: str) -> list:
    """[(node, value, kind)] for every occurrence of a property name."""
    out = []
    for n in walk(root):
        for p, v, k in n.props:
            if p == prop:
                out.append((n, v, k))
    return out
