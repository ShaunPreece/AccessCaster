"""Synthetic model dumps for the offline suites, in the real tree grammar (see model_tree):

    node     := NAME "\\0" count(P) property x P  count(C) node x C
    property := name "\\0" count(vlen) <type byte + payload>

`encode` takes (block, property, value) triples in stream order. Each run of one block becomes one
child of the root; a block of None puts the property on the root itself. Values: bool, int (int32),
float (float64) and str.

    python DevTools/fake_model.py      # self-check: an encoded dump parses back to its input
"""
from __future__ import annotations

import struct


def _count(n: int) -> bytes:
    if n == 0:
        return b"\x00"
    ln = 1 if n <= 0xFF else 2 if n <= 0xFFFF else 4
    return bytes([ln]) + n.to_bytes(ln, "little")


def _prop(name: str, value) -> bytes:
    # BOOL BEFORE INT: bool subclasses int, and the int branch would emit True with the wrong kind.
    if isinstance(value, bool):
        payload = bytes([0x02 if value else 0x03])
    elif isinstance(value, int):
        payload = b"\x01" + value.to_bytes(4, "little", signed=True)
    elif isinstance(value, float):
        payload = b"\x04" + struct.pack("<d", value)
    elif isinstance(value, str):
        payload = b"\x05" + value.encode("latin1") + b"\x00"
    else:
        raise TypeError(f"no encoding for {value!r}")
    return name.encode("latin1") + b"\x00" + _count(len(payload)) + payload


def node(name: str, props=(), kids=()) -> bytes:
    """One node: `props` is {name: value} or [(name, value)], `kids` already-encoded nodes."""
    props = list(props.items()) if isinstance(props, dict) else list(props or ())
    kids = list(kids or ())
    return (name.encode("latin1") + b"\x00" + _count(len(props))
            + b"".join(_prop(p, v) for p, v in props) + _count(len(kids)) + b"".join(kids))


def encode(entries) -> bytes:
    """A whole dump from (block, property, value) triples. It opens with the root node's name, as a
    real dump does, so `rcp_read.is_model_dump` accepts it."""
    root_props, runs = [], []                    # runs: [(block, [(name, value)])]
    for block, name, value in entries:
        if block is None:
            root_props.append((name, value))
            continue
        if not runs or runs[-1][0] != block:
            runs.append((block, []))
        runs[-1][1].append((name, value))
    return node("Rodecaster", root_props, [node(b, props) for b, props in runs])


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _bootstrap  # noqa: F401
    import model_tree
    rows = [(None, "top", 1), ("A", "x", True), ("A", "y", 0.5), ("B", "s", "hi")]
    root, _used, tail = model_tree.parse(encode(rows))
    got = [(None, p, v) for p, v, _k in root.props] + [(n.name, p, v) for n in root.kids
                                                       for p, v, _k in n.props]
    assert got == rows and not tail, got
    print("fake_model: ok")
