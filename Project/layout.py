"""WHERE THE BLOCKS ARE, read off the model tree instead of assumed from another desk.

A root child's INDEX is its selector (see model_tree.encode_path), so every per-channel, per-input
and per-cross-point address the app uses is "the root index of block N of some group". On the Pro II
and the Duo those indexes are constants in devices.py, each one measured on that desk. A desk whose
tree is laid out differently needs them from its OWN dump, and this module is where that is done.

WHAT A LAYOUT IS: for each mixer group, the root index of its first block and how many blocks follow
it, plus the first occurrence of each single block. Nothing here touches the device and nothing here
writes; it is a pure function of a parsed tree.

THE MAIN MIXER IS THE FIRST CONTIGUOUS RUN OF A GROUP, and only that run. A tree can carry LATER
blocks with the same name: measured on a RODECaster with USB product id 0x0092 (firmware 1.7.6),
CHANNEL x9 at 0x1A is followed much later by another CHANNEL x1 at 0xFA, and MIX x156 at 0x37 by
MIX x91 at 0xFF and MIX x143 at 0x17D. Reading every block called MIX concatenates those into one list
whose positions mean nothing, so the run is resolved by POSITION, not by name alone.

THE SHAPE IS CHECKED BEFORE ANYTHING USES IT. A routing cross-point is MIX[13 * source + output], so
the MIX run must be exactly ROW_STRIDE cells per INPUTSOURCE entry; and there must be at least one
CHANNEL (fader strip). A tree that fails either is reported invalid, with the reason, and a caller
must then treat the desk's geometry as unknown rather than fall back to another desk's numbers.

THE CACHE. `note(pid, layout)` keeps the last VALID layout per USB product id, and `current(pid)` is
how device code reads it back. rcp_read records it whenever it parses a dump it read off a desk,
because only then is the product id known; a dump loaded from a file is never recorded, so a saved
capture of one desk can never replace the geometry of the desk that is plugged in.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from typing import Optional

# Groups resolved as a RUN (base + count). Order is only the order they are reported in.
GROUP_NAMES = ("CHANNEL", "INPUTSOURCE", "MIX", "HEADPHONE", "MIXMINUSES", "EFFECTS_PARAMETERS")
# Blocks resolved by their FIRST occurrence among the root's children.
SINGLE_NAMES = ("PHYSICALINTERFACE", "RECBUTTON", "RECORDER", "PLAYER", "ENCODER", "GUI", "APP",
                "OUTPUT", "MASTERCHANNEL", "SYSTEM", "NETWORK", "AUDIO", "SOUNDPADS",
                "SHOWCONTROL", "RECORDINGS")
# Cross-points per routing source: MIX[ROW_STRIDE * source + output]. The same 13 on the Pro II and
# the Duo (rcp_transfer.CROSS_POINT_SOURCE_STRIDE), and on the 0x0092 desk a fader move pushes
# exactly 13 consecutive MIX selectors (0x37..0x43 for Mic 1).
ROW_STRIDE = 13


@dataclass(frozen=True)
class Run:
    """`count` consecutive root children named `name`, the first at root index `base`."""
    name: str
    base: int
    count: int

    def selector(self, i: int) -> int:
        """Root index (= one-level selector) of block `i` of this run. Refuses outside the run."""
        i = int(i)
        if not 0 <= i < self.count:
            raise IndexError(f"{self.name}[{i}] is outside the run of {self.count}")
        return self.base + i

    def index_of(self, sel: int) -> Optional[int]:
        """Position of root index `sel` within this run, or None if it is not one of its blocks."""
        i = int(sel) - self.base
        return i if 0 <= i < self.count else None


@dataclass(frozen=True)
class Layout:
    """The main-mixer geometry of one dump. `problems` empty means valid."""
    runs: tuple = ()               # (Run, ...) for GROUP_NAMES found in the tree
    singles: tuple = ()            # ((name, root index), ...) for SINGLE_NAMES found
    root_children: int = 0
    problems: tuple = ()           # human-readable reasons the layout cannot be trusted
    # Every run of every name in dump order, for diagnosis (the later duplicate blocks included).
    all_runs: tuple = field(default=(), compare=False)
    # The first run of each child name UNDER PHYSICALINTERFACE (POT, FADER, ...). A Run here is
    # SECOND-LEVEL: its base is an index among PHYSICALINTERFACE's children, so a node's tree path is
    # [single("PHYSICALINTERFACE"), base + i]. Resolved by name, never assumed to start at 0x04.
    physical_runs: tuple = ()
    # WHAT EACH BLOCK CARRIES, for the generalised write gate (devices.layout_tree_write_ok):
    # {path tuple: (block name, {property: kind})} for every root child and every child of a root
    # child, exactly as this dump publishes them. A write may only ever change a property that is
    # already here, on that node, with that kind: that is what keeps a stray from being appended.
    block_props: dict = field(default_factory=dict, compare=False, repr=False)

    def block_at(self, path) -> Optional[tuple]:
        """(block name, {property: kind}) of the node at `path` in the dump this layout was read
        from, or None when the dump has no node there (or the path is deeper than two levels)."""
        try:
            return self.block_props.get(tuple(int(p) for p in path))
        except (TypeError, ValueError):
            return None

    def main_run_of(self, root_index: int) -> Optional[Run]:
        """The run holding root child `root_index` if that run is the FIRST run of its name in the
        dump (the main mixer), else None: a later duplicate (CHANNEL 0xFA, MIX 0xFF, ...) is not a
        main block, and neither is an index no run covers."""
        root_index = int(root_index)
        hit = next((r for r in self.all_runs if r.index_of(root_index) is not None), None)
        if hit is None:
            return None
        first = next((r for r in self.all_runs if r.name == hit.name), None)
        return hit if first is hit else None

    def physical(self, name: str) -> Optional[Run]:
        """The first run of `name` among PHYSICALINTERFACE's children (FADER, POT, ...), or None."""
        return next((r for r in self.physical_runs if r.name == name), None)

    def physical_path(self, name: str, i: int) -> Optional[list]:
        """Tree path of node `i` of that run, e.g. FADER 0 -> [0, 4], or None if it has none."""
        r, top = self.physical(name), self.single("PHYSICALINTERFACE")
        if r is None or top is None or not 0 <= int(i) < r.count:
            return None
        return [top, r.base + int(i)]

    @property
    def valid(self) -> bool:
        return not self.problems

    def run(self, name: str) -> Optional[Run]:
        return next((r for r in self.runs if r.name == name), None)

    def base(self, name: str) -> Optional[int]:
        r = self.run(name)
        if r is not None:
            return r.base
        return dict(self.singles).get(name)

    def count(self, name: str) -> int:
        r = self.run(name)
        return r.count if r is not None else 0

    def single(self, name: str) -> Optional[int]:
        return dict(self.singles).get(name)


def _runs(kids) -> list:
    """Every maximal run of consecutive same-named root children, in order, as Run objects."""
    out = []
    for k in kids:
        if out and out[-1].name == k.name and out[-1].base + out[-1].count == k.index:
            out[-1] = Run(k.name, out[-1].base, out[-1].count + 1)
        else:
            out.append(Run(k.name, k.index, 1))
    return out


def from_tree(root) -> Layout:
    """Build a Layout from a parsed model tree (model_tree.parse's root). Never raises on shape;
    an unusable tree comes back with `problems` filled in."""
    kids = list(getattr(root, "kids", ()) or ())
    all_runs = _runs(kids)
    runs = []
    for name in GROUP_NAMES:
        r = next((r for r in all_runs if r.name == name), None)
        if r is not None:
            runs.append(r)
    singles = []
    for name in SINGLE_NAMES:
        k = next((k for k in kids if k.name == name), None)
        if k is not None:
            singles.append((name, k.index))
    phys = next((k for k in kids if k.name == "PHYSICALINTERFACE"), None)
    physical_runs, seen = [], set()
    for r in _runs(list(getattr(phys, "kids", ()) or ())):
        if r.name not in seen:                   # FIRST run of each name only
            seen.add(r.name)
            physical_runs.append(r)
    lay = Layout(runs=tuple(runs), singles=tuple(singles), root_children=len(kids),
                 all_runs=tuple(all_runs), physical_runs=tuple(physical_runs),
                 block_props=_block_props(kids))
    return replace(lay, problems=tuple(_check(lay)))


def _props_of(node) -> dict:
    """{property: kind} of one node, the FIRST occurrence of a name winning (a node never carries a
    name twice in a dump this app has read, but the first is the one a write would meet)."""
    out = {}
    for prop in (getattr(node, "props", ()) or ()):
        try:
            name, _val, kind = prop
        except (TypeError, ValueError):
            continue
        out.setdefault(name, kind)
    return out


def _block_props(kids) -> dict:
    """{(root index,): (name, props), (root index, child index): (name, props)} for a parsed tree.
    Stand-in nodes without props (the shape tests) give empty dicts, which no write can pass."""
    out = {}
    for k in kids:
        out[(k.index,)] = (k.name, _props_of(k))
        for c in (getattr(k, "kids", ()) or ()):
            out[(k.index, c.index)] = (c.name, _props_of(c))
    return out


def _check(lay: Layout) -> list:
    problems = []
    chan, src, mix = lay.run("CHANNEL"), lay.run("INPUTSOURCE"), lay.run("MIX")
    if chan is None or chan.count <= 0:
        problems.append("no CHANNEL block, so there are no fader strips to address")
    if src is None or src.count <= 0:
        problems.append("no INPUTSOURCE block, so there is no input table")
    if mix is None:
        problems.append("no MIX block, so there is no routing matrix")
    if src is not None and mix is not None and mix.count != src.count * ROW_STRIDE:
        problems.append(f"the MIX run holds {mix.count} cells, not {ROW_STRIDE} for each of the "
                        f"{src.count} inputs ({src.count * ROW_STRIDE})")
    return problems


# ---- the per-desk cache ------------------------------------------------------------------------
_lock = threading.Lock()
_by_pid: dict = {}


def note(pid, lay: Optional[Layout]) -> bool:
    """Remember `lay` as the current layout of the desk with USB product id `pid`, if it is valid.
    An invalid or missing layout leaves the previous one in place. Returns True if recorded."""
    if pid is None or lay is None or not lay.valid:
        return False
    with _lock:
        _by_pid[int(pid)] = lay
    return True


def current(pid) -> Optional[Layout]:
    """The last valid layout recorded for this product id, or None if none has been seen."""
    if pid is None:
        return None
    with _lock:
        return _by_pid.get(int(pid))


def forget(pid=None) -> None:
    """Drop the recorded layout for one product id, or all of them. For tests."""
    with _lock:
        if pid is None:
            _by_pid.clear()
        else:
            _by_pid.pop(int(pid), None)
