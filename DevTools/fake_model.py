"""Synthetic model dumps for the offline suites, in the real tree grammar (see model_tree):

    node     := NAME "\\0" count(P) property x P  count(C) node x C
    property := name "\\0" count(vlen) <type byte + payload>

`encode` takes (block, property, value) triples in stream order. Each run of one block becomes one
child of the root; a block of None puts the property on the root itself. Values: bool, int (int32),
float (float64), str, and a composite as the decoder gives it, [(value, kind)]. The bytes come from
model_tree's encoders; this only picks each value's kind from its Python type.

    python DevTools/fake_model.py      # self-check: an encoded dump parses back to its input
"""
from __future__ import annotations

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import model_tree


def _kind(value) -> str:
    # BOOL BEFORE INT: bool subclasses int, and the int branch would give True the wrong kind.
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):         # a composite, as the decoder gives it: [(value, kind)]
        return "pair"
    raise TypeError(f"no encoding for {value!r}")


def node(name: str, props=(), kids=()) -> bytes:
    """One node: `props` is {name: value} or [(name, value)], `kids` already-encoded nodes."""
    props = list(props.items()) if isinstance(props, dict) else list(props or ())
    kids = list(kids or ())
    return (name.encode("latin1") + b"\x00" + model_tree.encode_count(len(props))
            + b"".join(p.encode("latin1") + b"\x00" + model_tree.encode_value(v, _kind(v))
                       for p, v in props)
            + model_tree.encode_count(len(kids)) + b"".join(kids))


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
    rows = [(None, "top", 1), ("A", "x", True), ("A", "y", 0.5), ("B", "s", "hi")]
    root, _used, tail = model_tree.parse(encode(rows))
    got = [(None, p, v) for p, v, _k in root.props] + [(n.name, p, v) for n in root.kids
                                                       for p, v, _k in n.props]
    assert got == rows and not tail, got
    print("fake_model: ok")
