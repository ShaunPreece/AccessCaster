"""Every property the connected desk publishes: the full menu of what is available to work with.

    python DevTools/property_list.py            read the desk, rewrite PROPERTY_LIST_<DESK>.md
    python DevTools/property_list.py --check    compare and exit 1 if the NAME SET has changed
    python DevTools/property_list.py --blob X   use a saved .bin dump instead of the desk

WHY IT EXISTS. This list is the raw material every feature is built out of, and it had become
unreachable and unrebuildable at the same time. It lived as section 12 of `RODECASTER_PROTOCOL.txt`,
492 lines buried at the bottom of a 1575-line spec, so nobody looking for "what can this desk even
do" would find it. Worse, its own header named a generator and a source dump that had BOTH been
deleted in a DevTools cull, so it was a frozen snapshot nothing could rebuild or check. It was also
Duo-only, on a project where a Pro II has been attached the whole time.

NAMES AND TYPES ONLY, NEVER VALUES, and that is a hard rule rather than a tidiness choice. The dump
carries the wifi PSK and the SIP account password in plain text. Emitting names alone means this
file is safe to commit and safe to share, and `wifiPSK` still appears so a reader knows the property
is there. Nothing here needs a value to be useful.

READ-ONLY AND SAFE TO RUN ANY TIME. It sends the dump trigger and reads; it never writes, so it
needs no confirmation and cannot leave a stray. It does not need exclusive access either - it runs
happily alongside the app's own server (measured).

**`--check` COMPARES THE NAME SET, NOT THE COUNTS, and that is deliberate.** Occurrence counts move
with ordinary use: create a pad and the PAD blocks go up, connect a caller and the matrix changes
shape. Failing on that would make the guard cry wolf until it was ignored. The NAMES are the stable
fact - they come from the firmware's schema and change only when the firmware does - so a new name
appearing is real news, and it is exactly how a STRAY announces itself.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
from collections import Counter, defaultdict

import _bootstrap  # noqa: F401  (puts Project/ on the path; import FIRST)

import devices
import model_tree
import rcp_read

def out_path(dev) -> pathlib.Path:
    """DEVICE-SCOPED output: PROPERTY_LIST_PRO2.md / PROPERTY_LIST_DUO.md.

    Never one shared list. The two desks publish the SAME 463 property names and differ
    only in counts (PAD/PADBUTTON, following the physical shape), so a shared file regenerated on
    the other desk overwrote it silently AND passed --check, which compares names only. One file
    per desk means --check compares like with like and neither desk can clobber the other.
    """
    return pathlib.Path(_bootstrap.docs_path(f"PROPERTY_LIST_{dev.slug}.md"))


# The guide's PROVED INACTIVE list: properties written, read back, and shown to do nothing.
#
# WHY THIS FILE HAS TO CARRY IT. The dead list lives in `PROJECT_GUIDE.md` and the hunting for
# unclaimed properties happens HERE, in the generated list. Two different files means the warning is
# only ever found by somebody who already suspects they need it, which is nobody: a promising name
# like `toneGeneration` gets picked up as new work however many times it has been proven dead.
#
# PARSED, NEVER COPIED. A second copy of the list would rot the moment the guide gained an entry,
# and this file is regenerated far more often than the guide is read.
_DEAD_HEADING = "**PROVED INACTIVE:"


def dead_properties():
    """{name: reason} from the guide's PROVED INACTIVE list, or None if the guide is not here.

    None is NOT an error: a public clone has no notes beside it (see `_bootstrap.notes_path`), and
    a property list that cannot find the guide is still worth generating. It simply says so rather
    than silently shipping a list with no warnings on it.
    """
    guide = pathlib.Path(_bootstrap.docs_path("PROJECT_GUIDE.md"))
    if not guide.exists():
        return None
    lines = guide.read_text(encoding="utf-8").splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if _DEAD_HEADING in l)
    except StopIteration:
        return None
    # The entry runs to the next bold sub-point or the next top-level bullet, whichever comes first.
    body = []
    for line in lines[start + 1:]:
        stripped = line.strip()
        if stripped.startswith("**") or line.startswith("- ") or not stripped:
            break
        body.append(stripped)
    text = " ".join(body)
    out = {}
    for chunk in re.finditer(r"`(\w+)`\s*(?:\(([^)]*)\))?", text):
        out[chunk.group(1)] = (chunk.group(2) or "").strip()
    return out


def collect(blob: bytes):
    """(properties, blocks) from one dump. properties: {name: (types, count)}."""
    root, _used, _tail = model_tree.parse(blob)
    types: dict = defaultdict(set)
    counts: Counter = Counter()
    blocks: Counter = Counter()
    for node in model_tree.walk(root):
        if node.name:
            blocks[node.name] += 1
        for pname, _val, kind in node.props:
            counts[pname] += 1
            types[pname].add(kind)
    props = {n: (sorted(types[n]), counts[n]) for n in sorted(counts)}
    return props, blocks


def render(props: dict, blocks: Counter, device_name: str, firmware, dead=None) -> str:
    """The document. Names and types only - never a value (see the module docstring).

    `dead` is the guide's PROVED INACTIVE map, or None when the guide is not beside the repo."""
    L = []
    a = L.append
    a("# Property list: everything the desk publishes")
    a("")
    a("GENERATED FILE - do not hand-edit. Regenerate with `python DevTools/property_list.py`")
    a("(reads the connected desk; read-only, no writes, safe to run while the app is running).")
    a("`--check` exits 1 if the set of NAMES has changed, which is also how a stray shows up.")
    a("")
    a("**This is the raw material, not a to-do list and not a record of what we have built.** It")
    a("says what the desk EXPOSES. What we are aiming to build is `PARITY.md`; which of these")
    a("properties the app actually touches is the per-desk coverage report in the private notes.")
    a("")
    if dead:
        a(f"**{len(dead)} properties below are marked PROVED INACTIVE.** Each was written, read")
        a("back and shown to do nothing on real hardware, and each has a promising name, which is")
        a("the whole trap - they are the ones most likely to be picked up as new work. The reasons")
        a("come from `PROJECT_GUIDE.md`; do not re-test them, and do not copy the list anywhere.")
        a("**THE MEASUREMENTS ARE NOT PER-DESK.** One list covers both, because the Pro 2 and the")
        a("Duo run the same firmware and publish the same property names, and because every reason")
        a("here is an engine behaviour rather than anything about the hardware. A property proved")
        a("dead on one desk has not separately been proved dead on the other.")
        a("")
    elif dead is None:
        a("(The PROVED INACTIVE markers are missing: `PROJECT_GUIDE.md` is not beside this repo, so")
        a("nothing here warns you off a property already shown to be dead.)")
        a("")
    a("**NAMES AND TYPES ONLY, no values, deliberately.** The dump carries the wifi PSK and the SIP")
    a("password in plain text, so values are never written here and this file is safe to share.")
    a("`wifiPSK` appears as a NAME, which is all a reader needs to know it exists.")
    a("")
    a(f"Read from a **{device_name}**" + (f", firmware {firmware}" if firmware else "") + ".")
    a(f"**{len(props)} properties, {len(blocks)} block types.**")
    a("")
    a("The occurrence count hints at SCOPE and moves with ordinary use, so treat it as a shape")
    a("rather than a number: x1 is global, and a count matching a block's instance count below")
    a("means one per fader, per pad, per input slot or per matrix cell. Type `?` means the value")
    a("did not decode in this dump, which is usually an action or trigger property that carries")
    a("nothing at rest.")
    a("")
    a("--------------------------------------------------------------------------------")
    a("## Blocks (tree groupings, not settable properties)")
    a("")
    for name, n in sorted(blocks.items()):
        a(f"- `{name}` x{n}")
    a("")
    a("--------------------------------------------------------------------------------")
    a("## Properties")
    a("")
    for name, (kinds, n) in props.items():
        note = ""
        if dead and name in dead:
            why = dead[name]
            note = "  **PROVED INACTIVE**" + (f": {why}." if why else ".")
        a(f"- `{name}` - {'/'.join(kinds)}, x{n}{note}")
    a("")
    return "\n".join(L)


def name_set(text: str) -> set:
    return {l.split("`")[1] for l in text.splitlines()
            if l.startswith("- `") and "`" in l[3:]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="report drift and exit 1, write nothing")
    ap.add_argument("--blob", help="a saved .bin dump instead of reading the desk")
    args = ap.parse_args()

    if args.blob:
        blob = pathlib.Path(args.blob).read_bytes()
    else:
        blob = rcp_read.read_model_stable(fresh=True)
    if not blob:
        print("no dump: is the unit connected, and is the RODE app closed?")
        return 2

    props, blocks = collect(blob)
    # A SAVED DUMP CARRIES ITS OWN PROVENANCE: trust the blob's systemName over whatever is plugged
    # in, or a Pro II capture read while a Duo is attached would be written to the Duo's file.
    dev = devices.get_active_device()
    if args.blob:
        named = devices.device_from_system_name(rcp_read.first(blob, "systemName"))
        if named is None:
            print("the dump does not name a desk in systemName; refusing to guess which file to "
                  "write. Regenerate from the desk, or check the capture is a model dump.")
            return 2
        dev = named
        print(f"dump names itself a {dev.name}, so writing that desk's file "
              f"(ignoring the attached {devices.get_active_device().name}).")
    out = out_path(dev)
    fw = rcp_read.first(blob, "systemFirmwareVersion")

    # A NAME ON THE DEAD LIST THAT THIS DESK DOES NOT PUBLISH IS THE LIST ROTTING, and it is worth
    # stopping for: the entry is either a typo, or a property a firmware update has taken away, and
    # either way the warning it carries is now pointed at nothing. Reported before the file is
    # written so a bad list cannot quietly produce a good-looking document.
    dead = dead_properties()
    if dead:
        stale = sorted(n for n in dead if n not in props)
        if stale:
            for n in stale:
                print(f"  PROVED INACTIVE list names a property this desk does not publish: {n}")
            print("\nFix PROJECT_GUIDE.md's list, or check this is the right desk. Nothing written.")
            return 1

    text = render(props, blocks, dev.name, fw, dead)

    if args.check and out.exists():
        old = name_set(out.read_text(encoding="utf-8"))
        new = name_set(text)
        added, gone = sorted(new - old), sorted(old - new)
        if not added and not gone:
            print(f"{out.name}: unchanged ({len(props)} properties) on the {dev.name}.")
            return 0
        # A NAME THE FILE HAS NEVER SEEN IS THE SHAPE OF A STRAY. It is not proof - a firmware
        # update adds names legitimately - but it is the only cheap signal there is, and a stray
        # clears only on a factory reset, so it is worth stopping for.
        for n in added:
            print(f"  NEW NAME (firmware update, or a STRAY): {n}")
        for n in gone:
            print(f"  GONE: {n}")
        print("\n--check: nothing written. Investigate before regenerating.")
        return 1

    out.write_text(text, encoding="utf-8")
    print(f"{out.name}: {len(props)} properties, {len(blocks)} block types, from a {dev.name}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
