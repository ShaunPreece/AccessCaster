"""Write one property at a TREE PATH address, then say exactly what the desk did with it.

Addresses by TREE PATH, not by a flat selector byte. An address is a PATH of child indices (see
`model_tree.py` for the grammar), so this takes the BLOCK NAME and INSTANCE, looks the node up in
the live tree and builds the address from the structure. Nothing is guessed and nothing is counted
by hand.

That is what reaches the blocks a flat selector cannot: STORAGEVOLUME is not "slotless", it is
`SYSTEM[15] -> child 0..2`, so its address is `01 01 02 01 0f 01 <n>` - two levels, the same shape
as a pad write.

Same three outcomes, same per-property OCCURRENCE-COUNT check as its predecessor (never the
model's byte length, which moves on its own whenever a string value changes):

  LANDED   the property changed value in the node you aimed at.
  STRAY    a new occurrence appeared - and it prints WHICH node, which is what a stray is for.
  NOTHING  nothing moved. The write was swallowed.

Usage:
    python DevTools/probe_path_write.py --block STORAGEVOLUME --instance 1 \\
        --prop storageVolumeTransfer --value on --yes
    python DevTools/probe_path_write.py --block TEST --prop toneGeneration --kind int --value 0 --yes
"""
from __future__ import annotations
import argparse
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import model_tree as mt
import rcp_read
import rcp_transfer

SETTLE = 1.5


def snapshot():
    """(root, {property name: occurrence count}) from a fresh dump."""
    root, _used, _tail = mt.parse(rcp_read.read_model_stable(fresh=True))
    c = Counter()
    for n in mt.walk(root):
        for p, _v, _k in n.props:
            c[p] += 1
    return root, c


def value_block(kind: str, value: str) -> bytes:
    if kind == "bool":
        return bytes([0x01, 0x01, 0x02 if value.strip().lower() in ("on", "true", "1", "yes")
                      else 0x03])
    if kind == "int":
        return bytes([0x01, 0x05, 0x01]) + int(value).to_bytes(4, "little", signed=True)
    raise ValueError(kind)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--block", required=True, help="block NAME, e.g. STORAGEVOLUME")
    ap.add_argument("--instance", type=int, default=0, help="which instance of that block")
    ap.add_argument("--prop", required=True)
    ap.add_argument("--value", required=True, help="on/off for a bool, a number for an int")
    ap.add_argument("--kind", choices=["bool", "int"], default="bool")
    ap.add_argument("--yes", action="store_true", help="required; a stray costs a factory reset")
    args = ap.parse_args()

    root, before = snapshot()
    nodes = mt.instances(root, args.block.upper())
    if args.instance >= len(nodes):
        print(f"{args.block} has {len(nodes)} instance(s); no [{args.instance}]. Refusing.")
        return 2
    node = nodes[args.instance]
    have = [p for p, _v, _k in node.props]
    print(f"TARGET {node.name}[{args.instance}] path {node.path()} "
          f"address {node.address().hex(' ')}")
    for p, v, k in node.props:
        mark = " <-- writing this" if p == args.prop else ""
        print(f"    {p:<34} {k:<5} {v!r}{mark}")
    if args.prop not in have:
        print(f"\n  {args.prop} is not a property of this node. Refusing: a name the node has never "
              f"carried is the write most likely to append a stray.")
        return 2
    if not args.yes:
        print("\n  --yes not given, so nothing was written.")
        return 1

    rec = rcp_transfer._frame(node.address() + args.prop.encode("ascii") + b"\x00"
                              + value_block(args.kind, args.value))
    print(f"\nWRITING {args.prop} = {args.value} ({args.kind})")
    print(f"  record: {rec[:32].hex(' ')} ...")
    rcp_transfer.send_record(rec)
    time.sleep(SETTLE)

    root2, after = snapshot()
    print("\nRESULT")
    moved = {p: (before.get(p, 0), n) for p, n in after.items() if n != before.get(p, 0)}
    moved.update({p: (n, after.get(p, 0)) for p, n in before.items() if p not in after})
    if moved:
        print("  STRAY / STRUCTURE CHANGE - occurrence counts moved:")
        for p, (b, a) in moved.items():
            print(f"    {p}: {b} -> {a}")
        old = {tuple(n.path()) for n, _v, _k in mt.find_property(root, args.prop)}
        for n, v, _k in mt.find_property(root2, args.prop):
            if tuple(n.path()) not in old:
                print(f"  >>> THE NEW OCCURRENCE IS IN {n.name} path {n.path()}, value {v!r}.")
    else:
        print("  no occurrence count changed anywhere: NO STRAY.")

    try:
        now = mt.instances(root2, args.block.upper())[args.instance]
        was = dict((p, v) for p, v, _k in node.props)
        is_ = dict((p, v) for p, v, _k in now.props)
        changed = [(p, was.get(p), is_.get(p)) for p in is_ if was.get(p) != is_.get(p)]
        print(f"\n  {args.block}[{args.instance}] now:")
        for p, v, k in now.props:
            flag = "  CHANGED" if was.get(p) != v else ""
            print(f"    {p:<34} {k:<5} {v!r}{flag}")
        if not changed and not moved:
            print("\n  NOTHING HAPPENED. The write was swallowed: the field is not writable this "
                  "way, or the path is not what addresses it.")
    except IndexError:
        print("  the block vanished from the model after the write (!)")

    # Anything else that moved anywhere in the tree - a wrong address can land on a real field.
    def flat(r):
        return {(tuple(n.path()), p): v for n in mt.walk(r) for p, v, _k in n.props}
    a, b = flat(root), flat(root2)
    others = [(k, a[k], b[k]) for k in b if k in a and a[k] != b[k]
              and k[1] != args.prop]
    if others:
        print("\n  OTHER VALUES THAT MOVED (some are live meters/clocks):")
        for (path, p), x, y in others[:25]:
            print(f"    path {path} {p}: {x!r} -> {y!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
