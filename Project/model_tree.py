"""THE MODEL IS A SELF-DESCRIBING TREE. This parses it properly, and the parse is its own proof.

Decoded off a live dump, after `DevTools/block_order.py` showed that skipping
STORAGEVOLUME and WIFISCANRESULT in the old flat walk is LOAD-BEARING (their instances sit between
SYSTEM 0x0F and the proven NETWORK 0x10, so they really do take no slot) - which meant the flat walk
was fitting around a structure it could not see.

THE GRAMMAR, and every byte of the dump is accounted for by it:

    node     := NAME "\\0" count(P) property x P  count(C) node x C
    property := name "\\0" 0x01 <vlen> <payload, vlen bytes; payload[0] = type>
    count(n) := <length byte L> <n, L bytes little-endian>      # L = 0 means the count is 0

So the two bytes the flat walk read as "an array marker before a block name" are the PARENT's child
COUNT, and the lone 0x00 between two adjacent POT blocks is the first POT's own child count of zero.
Nothing is special-cased; SOUNDPADS is not a hole in the walk, it is an ordinary node with 0
properties and 24 children.

WHY THIS LIVES IN Project/ AND NOT DevTools/: it is not a probing tool, it is THE reader. `rcp_read`
builds its whole index from it, so every value the app shows comes through this parser, and
`DevTools/tree_tool.py` is a command line over it.

WHY IT REPLACED THE FLAT WALK, measured on a live 143 KB dump rather than argued:
  * FIVE TIMES FASTER: 9.6 ms to parse the whole tree against 84.5 ms for the flat block walk. The
    speed objection that kept the crude substring reader alive was backwards.
  * IT DOES NOT INVENT PROPERTIES. The flat walk found a phantom `iPOT`, because it recovered a name
    by stepping back over alphanumerics and swallowed the preceding byte.
  * IT GETS BLOCK NAMES RIGHT. The same step-back stops at an underscore, so EFFECTS_PARAMETERS
    reached callers as "PARAMETERS" - 24 occurrences misfiled.
  * IT DECODES 12 PROPERTIES THE FLAT READER COULD NOT, all of which it returned as None: the
    64-bit ints (`showLastModification`, `storageVolumeCapacity`/`Free`, `requestDropMarker`), the
    composites (`padRecordStateRequest`, `mixLinkRequest`, `encoderSignal`, ...) and the long
    strings (`fxPresetContents`, 316 bytes, invisible to a one-byte length reader).
  * NO REGRESSIONS. Every one of the 471 shared property names agreed, occurrence for occurrence,
    except those 12 - and in all 12 the flat reader had nothing.

THE WRITE ADDRESS IS THE TREE PATH, not a flat number:

    01 01 <levels> [ <len> <index, len bytes LE> ] x levels

`levels` is the DEPTH. A child of the root takes ONE index (which is why every selector ever
confirmed equals its root-child position: 0x1C = CHANNEL[0], 0x026E = INPUTSOURCE[0] as a two-byte
index), and a node one level deeper takes TWO - exactly the pad path `01 01 02 02 9a 02 01 <n>`:
root child 0x029A = SOUNDPADS, then child n = PAD[n].

That retires the "flat index past 0xFF" problem AND the unexplained "drift": PAD[0..23] were never
root children, so counting them as such shifted everything after them. It also gives STORAGEVOLUME
and WIFISCANRESULT real addresses for the first time.

NOTHING HERE WRITES TO THE DEVICE.
"""
from __future__ import annotations

import struct


class Node:
    __slots__ = ("name", "props", "kids", "parent", "index", "start", "end")

    def __init__(self, name, start):
        self.name = name
        self.props = []          # [(prop name, value, kind)]
        self.kids = []           # [Node]
        self.parent = None
        self.index = 0           # position among its parent's children
        self.start = start
        self.end = start

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


def encode_path(path) -> bytes:
    """The report-3 ADDRESS BYTES for a tree path: `01 01 <levels> [<len><index LE>] x levels`.

    THE ONE ADDRESS FORMAT. Every address shape `rcp_transfer` builds is a special case of this
    (decoded and checked byte-for-byte):

        01 01 01 01 1c            global, selector 0x1C      -> path [0x1C]
        01 01 01 02 6e 02         "channel address", Pro II  -> path [0x26E]
        01 01 01 02 48 01         the same, Duo              -> path [0x148]
        01 01 02 02 9a 02 01 <n>  a pad                      -> path [0x29A, n]
        01 01 02 02 9a 02 00      the "internal pad" case    -> path [0x29A, 0]

    So the Duo's mysterious "trailing 0x01, not the Pro II's 0x02" is not a device quirk at all: it
    is the HIGH BYTE of a two-byte index, and the two desks simply have that node at a different
    position. And the internal pad, which looks like "addressed WITHOUT a selector byte, one byte
    shorter", is just child 0.

    THE LENGTH RULE: an index is emitted in the fewest bytes that hold it, MINIMUM ONE.

    Zero is emitted as `01 00`, not as a zero-length count, and that is a deliberate choice rather
    than an oversight. The grammar does allow a length of 0 to mean the value is 0, and the RODE
    app's own internal-pad capture uses it (`...9a 02 00`). But OUR pad writes have always sent
    `01 00` for stream position 0 and are proven on hardware; the zero-length form appears in a
    capture we have never emitted. Changing working write bytes to match a form we have not tested
    would be risk for nothing, on the one address family where a bad write can append a stray that
    only a factory reset clears. So the desk evidently accepts both, and we keep sending the one we
    know. `PAD_ADDRESS_INTERNAL` in rcp_transfer records the other form; nothing uses it.
    """
    out = bytearray([0x01, 0x01, len(path)])
    for i in path:
        i = int(i)
        if i < 0:
            raise ValueError(f"negative index in path: {path}")
        n = 1 if i <= 0xFF else 2 if i <= 0xFFFF else 4
        out.append(n)
        out += i.to_bytes(n, "little")
    return bytes(out)


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
        # 64-bit int. Seen on STORAGEVOLUME[2]'s capacity/free, where the SAME property name is a
        # 32-bit 0x01 on the other two volumes - so a property's type is per OCCURRENCE, not per
        # name. That is why `storageVolumeCapacity` always read as a stale zero: the one volume
        # that fills it in writes a type the old one-byte reader could not decode.
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


def _read_name(blob: bytes, i: int):
    j = blob.find(b"\x00", i)
    if j < 0:
        raise ValueError(f"unterminated name at {i}")
    return blob[i:j], j + 1


def _parse_node(blob: bytes, i: int, parent, index: int) -> tuple:
    name, i = _read_name(blob, i)
    node = Node(name.decode("latin1"), i)
    node.parent, node.index = parent, index
    nprops, i = _count(blob, i)
    for _ in range(nprops):
        pname, i = _read_name(blob, i)
        val, kind, i = _value(blob, i)
        node.props.append((pname.decode("latin1"), val, kind))
    nkids, i = _count(blob, i)
    for k in range(nkids):
        kid, i = _parse_node(blob, i, node, k)
        node.kids.append(kid)
    node.end = i
    return node, i


MAX_PATH_DEPTH = 8   # the deepest address ever seen is 2 (SOUNDPADS then the pad); 8 is head-room


def parse_record(buf: bytes) -> tuple:
    """One ADDRESSED PROPERTY RECORD, the shape of both our writes and the desk's own pushes:

        01 01 <levels> [ <len> <index, len bytes LE> ] x levels  <name> "\\0" <value block>

    Returns (path, name, value, kind, end), where `end` is the index one past the record, so a
    caller can check the record accounted for EXACTLY the bytes it was told to expect.

    IT IS A TEST AS WELL AS A DECODER, which is why every malformed input raises ValueError rather
    than returning something partial: `rcp_read._push_record_len` asks "is this whole report-04 frame
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


def parse(blob: bytes) -> tuple:
    """(root, bytes consumed, trailing bytes). The root name is preceded by a 5-byte stream header
    (`53 2f 02 00 02` on this firmware) that is not part of the tree.

    THE FIRST FOUR OF THOSE FIVE BYTES ARE THE DUMP'S OWN LENGTH, little-endian, counting everything
    after them (measured on a live Duo: 136675, stable across every dump). That
    is what lets `rcp_read.read_model_raw` end the read on structure instead of on silence.
    """
    start = blob.find(b"Rodecaster\x00")
    if start < 0:
        raise ValueError("no root node in this blob")
    root, end = _parse_node(blob, start, None, 0)
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
