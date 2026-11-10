"""A desk's LAYOUT as a fixture: the fixed nodes and the property NAMES each carries, with no
values, so it can live in the public repo where a model dump cannot (a dump holds the wifi password).

`load` turns a fixture back into a real `model_tree.Layout` by encoding a skeleton tree with
`fake_model` and parsing it, so the offline suites address writes exactly as the app does on a desk.

    python DevTools/layout_fixture.py <dump.bin> <name>   # writes DevTools/fixtures/layout_<name>.json
    python DevTools/layout_fixture.py                     # self-check: every fixture round-trips

A fixture is a list of runs, `[name, count, [props], children]`: `count` consecutive root children
of that name carrying the same property names. `children` is the same shape for a node in
`model_tree.FIXED_CHILDREN`, else empty.
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import fake_model
import model_tree

FIXTURES = _bootstrap.devtools_path("fixtures")

# The desks and firmware the suites are pinned to. Pro II 1.7.7 is the default; the others are what
# test_addressing resolves every write against.
PRO2_177 = "pro2_1.7.7"
PRO2_176 = "pro2_1.7.6"
DUO_173 = "duo_1.7.3"


def _runs(nodes, fixed: bool) -> list:
    out = []
    for n in nodes:
        props = sorted({p for p, _v, _k in n.props})
        kids = _runs(n.kids, False) if fixed and n.name in model_tree.FIXED_CHILDREN else []
        if out and out[-1][0] == n.name and out[-1][2] == props and not kids and not out[-1][3]:
            out[-1][1] += 1
        else:
            out.append([n.name, 1, props, kids])
    return out


def from_dump(blob: bytes) -> list:
    """The fixture for a model dump."""
    root, _used, _tail = model_tree.parse(blob)
    return _runs(root.kids, True)


def _encode(runs, values=None, prefix=(), without=frozenset()) -> list:
    values = values or {}
    nodes, seen = [], Counter()
    for name, count, props, kids in runs:
        for _ in range(count):
            key = prefix + (name, seen[name])
            seen[name] += 1
            nodes.append(fake_model.node(name, [(p, values.get((key, p), 0)) for p in props
                                                if p not in without],
                                         _encode(kids, values, key, without)))
    return nodes


def skeleton(name: str, values=None, without=()) -> bytes:
    """A model dump with a saved fixture's shape, for a suite that needs the desk model
    (`desk_model`) to hold a desk: it addresses exactly as `load(name)` does. Every property is 0
    but those `values` names, as {(layout key, property): value}, and the property names in
    `without` are left off every node, as a dump that does not carry them."""
    with open(os.path.join(FIXTURES, f"layout_{name}.json"), encoding="utf-8") as fh:
        runs = json.load(fh)
    return fake_model.node("Rodecaster", (), _encode(runs, values or {}, (), frozenset(without)))


def load(name: str) -> model_tree.Layout:
    """The `model_tree.Layout` of a saved fixture, e.g. load(PRO2_177)."""
    root, _used, _tail = model_tree.parse(skeleton(name))
    return model_tree.Layout(root)


def save(blob: bytes, name: str) -> str:
    os.makedirs(FIXTURES, exist_ok=True)
    path = os.path.join(FIXTURES, f"layout_{name}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(from_dump(blob), fh, separators=(",", ":"))
        fh.write("\n")
    return path


if __name__ == "__main__":
    if len(sys.argv) == 3:
        with open(sys.argv[1], "rb") as fh:
            print("wrote", save(fh.read(), sys.argv[2]))
        sys.exit(0)
    for name in (PRO2_177, PRO2_176, DUO_173):
        book = load(name)
        print(f"{name}: {book.count('MIX')} MIX, {book.count('CHANNEL')} CHANNEL, "
              f"{book.count('PHYSICALINTERFACE', 0, 'FADER')} FADER")
