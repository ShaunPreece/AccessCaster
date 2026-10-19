"""AccessCaster - DEVICE MODEL table (Pro II + Duo), keyed by USB product id.

The Pro II and the Duo speak the same protocol (HID transport, report-3 framing, value encodings,
model-dump read) and differ only in data: which inputs exist, how many faders and pads, which
outputs the matrix exposes, and which writes are proven. The protocol logic stays single-copy in
rcp_hid / rcp_transfer / rcp_read / channels, and only that data lives here, one DeviceModel per
desk. A third desk is one more table entry.

This is the DEVICE model, unrelated to profiles.py, which stores user settings snapshots.

This module imports only rcp_hid and layout (which imports nothing of ours at module level), so
rcp_read / rcp_transfer / channels can import it without a cycle.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import layout as _layout     # block geometry of a desk read off its own dump (pure, no imports)
import rcp_hid

# Known RODECaster USB product ids on the vendor control interface (MI_09).
#
# THESE ARE THE IDS IN THE DEFAULT AUDIO CONFIGURATION ONLY, so the table below is incomplete by
# construction: the product id encodes the USB audio configuration, and a desk re-enumerates under
# a different one whenever `outputMultiMode` changes (a Pro II reads 0x0092, 0x0078 or 0x0094 in
# its other modes). An unknown id therefore falls through to DEFAULT rather than naming the desk,
# which is right for a Pro II and wrong for a Duo in Expanded. Identifying by the dump's own
# `systemName` is the fix; see `device_from_system_name` below and PROJECT_GUIDE part 2.
PID_PRO2 = 0x0037
PID_DUO = 0x0050


@dataclass(frozen=True)
class MatrixSpec:
    """Routing-matrix shape for one device.

    The model-dump stream carries one mixDisabled/mixMute/mixLevelWithAnchor block
    per cross-point. read index into that flat list = row_stride * source + output.
      block         : the dump block holding the grid. MUST be named, and every read
                      scoped to it with rcp_read.block_values: RCSYNCMIX carries the
                      same cross-point names, so an unscoped read concatenates two
                      grids and RODE resizing the RC Sync one moves the total while
                      the routing matrix stands still.
      cell_count    : expected number of blocks IN `block` (a guard - if the live count
                      differs the matrix changed shape, e.g. a SIP caller, so reads refuse).
      output_count  : number of outputs (read_output_modes yields this many).
      source_indices: the channelInputSource codes we EXPOSE as matrix rows.
      row_stride    : blocks per source in the flat list (Pro II = 13).
    """
    cell_count: int
    output_count: int
    source_indices: tuple
    row_stride: int
    block: str = "MIX"
    # ADDRESSED GRID, for a desk whose geometry comes from its own tree (see layout_model). `base` is
    # the root index of cell 0 of the MAIN MIX run and `modes_base` that of the main MIXMINUSES run;
    # rcp_read._read_matrix then reads exactly those runs by address. None (the Pro II and the Duo)
    # keeps the block-name read those two desks were verified with.
    base: Optional[int] = None
    modes_base: Optional[int] = None
    # LATER MODE RUNS, read only: ((root index, count), ...) that continue the output-mode list
    # after the main MIXMINUSES run, in output order. On PID 0x0092 the main run holds 10 modes and
    # a later MIXMINUSES run at 0xFC holds the last 3 of the 13 (layout_model). Never written.
    modes_extra: tuple = ()
    # UI LABEL MAPS (device-specific). The read code above only needs the numbers; these carry
    # the human-readable names so the routing screen labels the Duo's real sources/outputs
    # (e.g. source code 2 = "Headset" on the Duo, not "Mic 3" as on the Pro II) instead of the
    # Pro II names. Each source entry: {"index", "key", "label"} (index == channelInputSource
    # source code == matrix row). Each output entry: {"index", "key", "label", "mixminus"}
    # (index == matrix column; mixminus = whether the output offers a Mix-Minus mode). Order is
    # the reading order for the UI. {s["index"] for s in sources} must equal set(source_indices).
    sources: tuple = ()
    # Rows that exist only in Expanded. KEPT OUT OF `sources` on purpose, because `sources` is the
    # DISPLAY list and offering four permanently silent rows across every output is a lot of grid
    # for a screen reader to walk past for nothing. The page asks `sources_for`; anything that
    # READS or WRITES the grid asks `all_sources`, because the grid carries all 30 source rows on
    # the wire in both modes.
    expanded_sources: tuple = ()
    # {code: label} for a row whose MEANING changes in Expanded, off the same channel table that
    # feeds FaderSpec.expanded_relabel. Carried here too so the matrix cannot drift from the
    # Channels page, which is the exact fault this whole table exists to make impossible.
    expanded_relabel: tuple = ()
    outputs: tuple = ()

    @property
    def all_sources(self) -> tuple:
        """EVERY row the grid carries, mode-independent, and the one list anything that READS or
        WRITES a cross-point should walk.

        The desk publishes all 30 source rows whichever mode it is in, so the Expanded four are
        ordinary MIX nodes with real state at all times. Reading or storing a NARROWER set than
        that is what made a mix scheme mode-dependent: saved in Standard it carried no Virtual A
        cell, and applied in Expanded it left one behind. A scheme and a profile are keyed off
        this, so the saved shape is the same whatever mode the desk was in.

        No relabelling: a scheme is keyed, so the Expanded names would be decoration here, and in
        Standard they would be wrong."""
        return tuple(self.sources) + tuple(self.expanded_sources)

    @property
    def all_source_indices(self) -> tuple:
        """The codes of `all_sources`, for the grid read."""
        return tuple(s["index"] for s in self.all_sources) if self.sources \
            else tuple(self.source_indices)

    def source_indices_for(self, expanded: bool) -> tuple:
        """The codes behind `sources_for`. The DISPLAY set, so it is a subset of
        `all_source_indices`; a reader wants that one."""
        return tuple(self.source_indices) + (
            tuple(s["index"] for s in self.expanded_sources) if expanded else ())

    def sources_for(self, expanded: bool) -> tuple:
        """The matrix rows to SHOW. Expanded adds Game, Music, A and B and renames USB 1; they are
        real cross-points in both modes, but offering four dead rows across every output is a lot
        of grid for a screen reader to walk past for nothing."""
        rows = tuple(self.sources) + (tuple(self.expanded_sources) if expanded else ())
        if not expanded:
            return rows
        renames = dict(self.expanded_relabel)
        return tuple(dict(r, label=renames[r["index"]]) if r["index"] in renames else r
                     for r in rows)


@dataclass(frozen=True)
class PadSpec:
    """SMART-pad grid shape for one device (see DeviceModel.pad).

    The pad grid is banks of physical pads. padIdx is a flat 0-based index:
    padIdx = pads_per_bank*(bank-1) + pad, banks and pads both 1-based on screen.
      pads_per_bank : physical pads per bank (Pro II = 8, Duo = 6 - it has 2 fewer pads).
      user_banks    : count of user-assignable banks (banks 1..user_banks). Both desks = 7.
      system_bank   : 1-based bank that holds the factory system/function pads (both = 8).
    All three are CONFIRMED read-only on the Duo: its 6 system pads sit at
    padIdx 42-47 = 7*6..8*6-1, i.e. bank 8 of a 6-per-bank grid,
    so the Duo is 6/bank x 8 banks = 48 slots (vs the Pro II's 8/bank x 8 = 64)."""
    pads_per_bank: int
    user_banks: int = 7
    system_bank: int = 8

    @property
    def max_user_pads(self) -> int:
        """Number of user-assignable pad slots (padIdx 0 .. this-1)."""
        return self.pads_per_bank * self.user_banks


# Where the desk keeps a fader's LEVEL: PHYSICALINTERFACE (root child 0) holds the pots first, then
# one FADER node per fader, so fader `i` is tree path [0, FADER_NODE_BASE + i]. Read off a live Duo
# dump - four POT nodes at [0,0..3] then nine FADER nodes at [0,4..12] - and it
# reproduces the Pro II's three long-confirmed addresses exactly. See FaderSpec.level_selectors.
FADER_NODE_BASE = 0x04


@dataclass(frozen=True)
class FaderSpec:
    """Fader-assignment layout for one device (consumed by server.py's fader functions).

    The fader LAYOUT is the channelInputSource int vector: vector POSITION = fader
    (index 0 = fader 1), VALUE = the channel/source CODE on that fader, written at
    selector 0x1C + index (RODECASTER_PROTOCOL.txt section 6). This is the ONLY place
    the per-device fader shape lives.
      count          : number of faders the desk actually EXPOSES (Pro II 9, Duo 9). This is
                       what the UI lists and the highest index a channel can be assigned to.
                       NOT necessarily the vector length - see vector_len.
      vector_len     : how many slots we are willing to WRITE, when that is more than the exposed
                       fader count (Duo: 10 slots for 9 faders, live-tested). None = same as count.
                       Profile RESTORE and the reset default use this length.
      capture_len    : how many slots a profile SAVE records. None = same as vector_len. Split from
                       vector_len: the Pro II's raw vector is 10 long too (slot 9 mirrors
                       USB 2), but we do not want restore writing that slot, because the layout-tidy
                       logic would then meet a duplicated source it never sees today. So the Pro II
                       CAPTURES 10 and WRITES 9: the slot is preserved in every profile in case a
                       firmware update ever brings it into use, at no risk to restore.
      physical       : number of physical hardware faders; faders at index >= this are
                       'virtual'. COSMETIC only (drives the "(virtual)" label). Set equal
                       to count on a device whose physical/virtual split is not confirmed,
                       so nothing is mislabelled 'virtual'.
      assignable     : the (code, label) channels the UI offers to place on a fader, in
                       display order. code is a channelInputSource value. This order is
                       also the gap-fill order used when tidying a messy layout.
      default_vector : the factory-default channelInputSource vector, or None when the
                       device's clean default is NOT confirmed - in which case 'reset
                       faders' REFUSES rather than writing a guessed layout.
    """
    count: int
    physical: int
    assignable: tuple = ()
    # (pair code, lower mic code, upper mic code, label) per LEGAL stereo link. Empty on a device
    # whose pair codes are not proven, which makes the whole feature refuse there. See pair_map.
    stereo_pairs: tuple = ()
    # (code, label) sources that EXIST ONLY IN EXPANDED, RODE's Virtual Devices. Nameable always,
    # assignable only when the desk says it is in Expanded - offering them in Standard would put a
    # permanently silent input on somebody's fader. See expanded_labels / code_set_for.
    expanded_sources: tuple = ()
    # {code: label} to use INSTEAD of the assignable one while the desk is in Expanded, for a
    # channel whose meaning changes with the mode.
    expanded_relabel: tuple = ()
    default_vector: Optional[tuple] = None
    vector_len: Optional[int] = None
    capture_len: Optional[int] = None

    @property
    def level_selectors(self) -> dict:
        """{fader index (0-based): faderLevel address} for the faders whose LEVEL software may set.

        DERIVED, not a table, and that is the point. A fader's level lives at
        `PHYSICALINTERFACE.FADER[FADER_NODE_BASE + index]`, tree path `[0, base + index]`, and the
        desk lays exactly `count` of those out in a row. So the address is arithmetic on the fader
        index, with nothing per-device left to get wrong.

        ONLY THE VIRTUAL FADERS ARE OURS. The physical ones are analog pots, so writing them would
        fight the hardware - and the desk says so itself: on a Duo the four physical nodes read a
        flat 0 while the five virtual ones carry a real level.

        DERIVED, never a hand-kept dict of "observed selectors": that shape is empty on any desk
        nobody has captured, so the whole feature refuses there. The Pro II's three entries
        (6:0x0A, 7:0x0B, 8:0x0C) are exactly what this returns. They look device-specific and
        unguessable only when they are recorded as one-level selectors, when they have always been
        the SECOND index of a two-level path.
        `test_duo_parity.py` asserts the retired Pro II three against this derivation.
        """
        return {i: FADER_NODE_BASE + i for i in range(self.physical, self.count)}

    @property
    def vector_length(self) -> int:
        """How many channelInputSource slots we WRITE (>= count). Profile restore + reset default."""
        return self.vector_len if self.vector_len is not None else self.count

    @property
    def capture_length(self) -> int:
        """How many channelInputSource slots a profile SAVE records (>= vector_length).
        Read-only: profiles._apply_v2 writes only the first vector_length entries, so any extra
        captured slot round-trips into the JSON without ever being written back."""
        return self.capture_len if self.capture_len is not None else self.vector_length

    # THERE IS NO `code_label` ANY MORE, and it is not coming back. It returned {code: label} for the
    # ASSIGNABLE channels only, which is the wrong set for naming: a fader can HOLD a stereo-linked
    # pair (codes 4, 5, 6) that is deliberately not assignable, so every caller that named something
    # from it is one linked pair away from saying a bare number out loud: "0 percent, Source 4" for
    # a linked Mic 1 + 2. Use `all_labels` to NAME anything the vector can hold, and `code_set` /
    # `code_order` to decide what may be WRITTEN. Naming is the wide set, writing is the narrow one.
    @property
    def code_set(self) -> frozenset:
        """The set of assignable source codes (for validating a requested placement)."""
        return frozenset(code for code, _label in self.assignable)

    @property
    def code_order(self) -> tuple:
        """Assignable codes in display order (the tidy/gap-fill order)."""
        return tuple(code for code, _label in self.assignable)

    # --- stereo-linked pairs ---------------------------------------------------------------
    # A linked pair is its OWN source code, not a flag on a mic (RODECASTER_PROTOCOL 6.5). It can
    # therefore sit in the fader vector, which means it must be NAMEABLE - but it must NOT be
    # assignable, and that separation is the load-bearing part. If a pair code joined `assignable`
    # the UI would offer "Mic 1 + 2" as a drop target beside "Mic 1", and `_tidy_fader_target` would
    # gap-fill with it, producing layouts where a mic is both linked and on its own fader.
    @property
    def pair_map(self) -> dict:
        """{pair code: (lower mic code, upper mic code)}."""
        return {code: (lo, up) for code, lo, up, _label in self.stereo_pairs}

    @property
    def pair_label(self) -> dict:
        """{pair code: label}."""
        return {code: label for code, _lo, _up, label in self.stereo_pairs}

    @property
    def all_labels(self) -> dict:
        """{code: label} for naming ANYTHING the vector can hold - the assignable channels, the
        linked pairs, and the Expanded-only sources. Naming is the wide set; `code_set` (what may
        be WRITTEN) stays narrow.

        THE EXPANDED FOUR ARE IN HERE EVEN IN STANDARD, deliberately. A fader keeps whatever code
        it was given, so a desk that leaves Expanded can still be holding one, and the alternative
        to naming it is the announcer saying "0 percent, Source 12" out loud."""
        out = dict(self.assignable)
        out.update(self.expanded_sources)
        out.update(self.pair_label)
        return out

    def labels_for(self, expanded: bool) -> dict:
        """`all_labels`, with any channel whose MEANING changes in Expanded renamed.

        On a Pro II that is USB 1: in Standard the computer sends it one stereo mix and Windows
        calls the endpoint Main Stereo, and in Expanded the same channel carries the System virtual
        device with Game, Music, A and B alongside it. Announcing "USB 1 (Main Stereo)" there names
        a Windows device the person no longer has."""
        out = self.all_labels
        if expanded:
            out.update(self.expanded_relabel)
        return out

    def code_set_for(self, expanded: bool) -> frozenset:
        """What may be WRITTEN to a fader right now. The Expanded four join only when the desk is
        in Expanded, because a source that exists in neither the mixer nor Windows is not a channel
        anyone should be able to put on a fader."""
        if not expanded:
            return self.code_set
        return self.code_set | frozenset(code for code, _label in self.expanded_sources)

    def code_order_for(self, expanded: bool) -> tuple:
        """`code_order` plus the Expanded four, in display order, for the same reason."""
        if not expanded:
            return self.code_order
        return self.code_order + tuple(code for code, _label in self.expanded_sources)

    def pair_swallowing(self, code: int, vec) -> Optional[int]:
        """The pair code in `vec` that currently contains mic `code`, or None. Used so the tidy
        logic does not treat a linked mic as a channel that has gone missing from the layout."""
        for pair, (lo, up) in self.pair_map.items():
            if code in (lo, up) and pair in vec:
                return pair
        return None


@dataclass(frozen=True)
class InputStripSpec:
    """Flexible input-source strips for the older by-NAME routing view.

    This is the older representation of fader assignment, consumed by server.py's
    routing_model / read_routing_live / action_set_input_source (the /api/routing/inputsource
    endpoint) and by legacy-profile restore (profiles._apply_legacy). It exposes the
    reassignable strips by a fixed NAME and offers a small source palette to drop onto them.
    The device-aware fader_model (FaderSpec) is the primary, INDEX-based view; this by-name
    view remains only for that endpoint and for restoring pre-full-format profiles.

      strips  : (key, vec_index, label) per reassignable strip. `vec_index` is the strip's
                position in the channelInputSource vector, written with set_fader_source.
      sources : (code, label) source palette. `code` is an rcp_transfer.INPUT_SOURCE_CODE value.

    A device that uses ONLY the index-based fader_model leaves this EMPTY (the default). The Duo
    does, because its flexible strips sit at different vector positions. With an empty spec the
    by-name endpoints report nothing and refuse, and legacy profiles skip the input_sources subset.
    """
    strips: tuple = ()
    sources: tuple = ()

    @property
    def strip_vec_index(self) -> dict:
        """{strip key: channelInputSource vector index}."""
        return {k: idx for k, idx, _lab in self.strips}

    @property
    def strip_label(self) -> dict:
        """{strip key: UI label}."""
        return {k: lab for k, _idx, lab in self.strips}

    @property
    def source_palette(self) -> list:
        """The droppable-source palette as [{code, label}] in display order."""
        return [{"code": code, "label": lab} for code, lab in self.sources]

    @property
    def source_label(self) -> dict:
        """{source code: label} for the palette."""
        return {code: lab for code, lab in self.sources}

    @property
    def valid_source_codes(self) -> frozenset:
        """The set of source codes a strip may be set to (for validating a write)."""
        return frozenset(code for code, _lab in self.sources)


@dataclass(frozen=True)
class HeadphoneTypeSpec:
    """Per-output 'headphone type' (amp profile) setting for one device.

    headphoneType is a GLOBAL int property (rcp_transfer.build_global_prop_int). The SELECTOR
    picks WHICH headphone output; the VALUE picks the amp profile. Confirmed selectors:
    0x09 = Headphone 1 (from the original Pro II reversing) and 0x0A = Headphone 2 (captured on
    the Duo fw-1.7.3 output-write pass). Both desks share these selectors byte-for-byte
    (the whole output block is identical), so both expose HP1 + HP2. The write path is gated by the
    "output_write" cap, and 0x09/0x0A are KNOWN selectors, so writing them is not a stray-append hazard.

      outputs : (selector, label) per addressable headphone output, in UI order.
      values  : (int, label) per selectable amp type, in UI order.

    NOT YET CONFIRMED ON HARDWARE:
      * value 2 = "Low sensitivity" - named from the RODE app UI but not yet capture-verified.
      * HP2 (0x0A) on the PRO II specifically (confirmed on the Duo; the Pro II shares the selector).
      * The Pro II's Headphone 3/4 outputs (inferred 0x0B/0x0C) are deliberately NOT listed: their
        selectors are unconfirmed, so listing them would risk writing an unmapped selector (a stray).
    All are reversible global-int writes; confirm during a supervised live test before relying on them.
    """
    outputs: tuple = ()
    values: tuple = ()

    @property
    def output_selectors(self) -> tuple:
        """The addressable headphone-output selectors, in UI order (index 0 == Headphone 1)."""
        return tuple(sel for sel, _lab in self.outputs)

    @property
    def value_codes(self) -> frozenset:
        """The set of selectable type values (for validating a requested write)."""
        return frozenset(code for code, _lab in self.values)

    @property
    def value_label(self) -> dict:
        """{value: label} for the selectable amp types."""
        return {code: lab for code, lab in self.values}


# Both desks share the confirmed HP1 (0x09) + HP2 (0x0A) selectors and the same 3-value amp enum.
# Kept as one spec used by both device models (see HeadphoneTypeSpec for the confirmation caveats).
HEADPHONE_TYPES = HeadphoneTypeSpec(
    outputs=((0x09, "Headphone 1"), (0x0A, "Headphone 2")),
    values=((0, "NTH-100"), (1, "High sensitivity"), (2, "Low sensitivity")),
)


# --- THE ONE CHANNEL TABLE ---------------------------------------------------------------------
# EVERY INPUT THIS DESK CAN CARRY, WRITTEN ONCE. The fader dropdown, the Channels page and the
# routing-matrix rows are VIEWS over this, derived below, so an input added or renamed here reaches
# all three and none can drift from the others.
#
# ADDING AN INPUT IS ONE ENTRY. Which surfaces it appears on is stated per entry, because
# membership genuinely differs - CallMe 1 is on the faders and the Channels page but has no matrix
# row on a Duo - and a flag that says so is checkable where three separate omissions are not.
@dataclass(frozen=True)
class Channel:
    """One input, and everything the app needs to know about it.

      code  : the `channelInputSource` value, which is also its index into the desk's INPUTSOURCE
              table. The one identifier the DESK uses.
      key   : the stable identifier WE use. Profiles, mix schemes and the applied-preset record
              all store this, so **renaming a key silently orphans saved user data** - rename the
              label instead, which is what is displayed.
      kind  : "mic" (gain, phantom, polarity, type, effects), "line" (a Level), or "bus" (mute and
              solo only - see the SMART pads note below; a bus has no Level and no processing).
      expanded_only / expanded_label : Expanded is RODE's Virtual Devices mode, which adds four
              inputs and changes what USB 1 carries. See FaderSpec.expanded_sources.
      stereo: a two-leg source. Its pan is channelPanL / channelPanR, one per leg; channelPan only
              pans a mono source and does nothing here. Heard on USB 1 and CallMe 1; the other
              computer and phone feeds are taken to be the same kind of input.
    """

    code: int
    key: str
    label: str
    kind: str
    stereo: bool = False
    on_faders: bool = True
    on_channels_page: bool = True
    in_matrix: bool = True
    expanded_only: bool = False
    expanded_label: str = ""


def _ui_channel_view(table) -> tuple:
    """The Channels page's list, IN TABLE ORDER. The order is what a screen reader walks, so it is
    the table's own order rather than anything derived."""
    return tuple({"name": c.key, "label": c.label, "kind": c.kind, "source_code": c.code,
                  "stereo": c.stereo}
                 for c in table if c.on_channels_page and not c.expanded_only)


def _expanded_ui_channel_view(table) -> tuple:
    return tuple({"name": c.key, "label": c.label, "kind": c.kind, "source_code": c.code,
                  "stereo": c.stereo}
                 for c in table if c.on_channels_page and c.expanded_only)


def _matrix_index_view(table) -> tuple:
    """Just the codes of the matrix rows, in code order. The grid read walks these; `sources`
    carries the same set with labels for the UI, and both come from one table so they cannot
    disagree."""
    return tuple(c.code for c in sorted(table, key=lambda c: c.code)
                 if c.in_matrix and not c.expanded_only)


def _matrix_source_view(table) -> tuple:
    """The matrix rows, IN CODE ORDER, because a row's position is its source index on the wire."""
    return tuple({"index": c.code, "key": c.key, "label": c.label}
                 for c in sorted(table, key=lambda c: c.code)
                 if c.in_matrix and not c.expanded_only)


def _expanded_matrix_source_view(table) -> tuple:
    return tuple({"index": c.code, "key": c.key, "label": c.label}
                 for c in sorted(table, key=lambda c: c.code)
                 if c.in_matrix and c.expanded_only)


def _assignable_view(table) -> tuple:
    """(code, label) for the fader dropdown, IN CODE ORDER - which is also the gap-fill order used
    when tidying a messy layout, so it wants to be the desk's own numbering and not a UI whim."""
    return tuple((c.code, c.label) for c in sorted(table, key=lambda c: c.code)
                 if c.on_faders and not c.expanded_only)


def _expanded_assignable_view(table) -> tuple:
    return tuple((c.code, c.label) for c in sorted(table, key=lambda c: c.code)
                 if c.on_faders and c.expanded_only)


def _relabel_view(table) -> tuple:
    """{code: label} for channels whose MEANING changes in Expanded."""
    return tuple((c.code, c.expanded_label) for c in table if c.expanded_label)


# RODE's Virtual Devices: source codes measured on a Pro II, each written to a fader and read back.
# Shared by both desks, because a code is a position in the firmware's InputSourceType enum and both
# run the same binary; `DevTools/input_sources.py` against a Duo would confirm it. The four are
# functionally identical by RODE's own account, so the ORDER is theirs (guide and driver INF) and
# costs a label if wrong, never an address.
_VIRTUAL_DEVICES = (
    Channel(12, "Game", "Game", "line", stereo=True, expanded_only=True),
    Channel(13, "Music", "Music", "line", stereo=True, expanded_only=True),
    Channel(14, "VirtualA", "Virtual A", "line", stereo=True, expanded_only=True),
    Channel(15, "VirtualB", "Virtual B", "line", stereo=True, expanded_only=True),
)

CHANNELS_PRO2 = (
    Channel(0, "Mic1", "Mic 1", "mic"),
    Channel(1, "Mic2", "Mic 2", "mic"),
    Channel(2, "Mic3", "Mic 3", "mic"),
    Channel(3, "Mic4", "Mic 4", "mic"),
    Channel(10, "Bluetooth", "Bluetooth", "line", stereo=True),
    # In Expanded the computer no longer sends one stereo mix down USB 1: the channel carries the
    # System virtual device, with Game, Music, A and B alongside it. Naming it after a Windows
    # device the person no longer has is worse than naming it plainly, and this is read aloud on
    # every encoder turn.
    Channel(7, "USB1", "USB 1 (Main Stereo)", "line", stereo=True,
            expanded_label="USB 1 (System)"),
    Channel(8, "Chat", "Chat", "line", stereo=True),
    Channel(9, "USB2", "USB 2 (Secondary)", "line", stereo=True),
    # THE SMART PADS BUS IS A CHANNEL, BUT NOT A PROCESSED ONE. It sits on a strip and that strip
    # PUBLISHES a full chain: it reads channelCurrentFxPreset -1 with channelBypassProcessing
    # False, and every parameter on it accepts a write, lands, reads back and restores byte-clean.
    # NONE OF THAT IS PROOF.
    #
    # SETTLED BY EAR ON BOTH DESKS: the chain does NOTHING to the pads audio. Pan hard over moves
    # nothing, against a same-listener, same-headphones control where pan on Mic 2 moves the voice
    # to one ear; maximum HPF, full EQ cuts, a slammed noise gate and zero make-up gain are all
    # inaudible. **RODE's own guide says so** ("You can add processing to any audio channel except
    # the SMART pads") and so does their app, which offers no processing for it. A write landing
    # at its own address proves an address is live, never that it reaches audio.
    # So the bus keeps MUTE, SOLO and its fader, and has no chain in the app - see
    # channels.read_channels, which is where that is enforced, and app.js renderChannelPanel.
    # DO NOT PUT THE CHAIN BACK on the strength of a write reading back cleanly. It will.
    #
    # Its level is its FADER, not inputDigitalGain (slot 11 reads a flat 0 while every real line
    # slot carries a value), so offering a Level slider would offer a control we have not shown to
    # do anything.
    Channel(11, "SMARTpads", "SMART pads", "bus"),
    # A remote caller. Like every channel its chain is the one on whichever strip carries it, so it
    # has processing only while it is on a fader. Its matrix row is source 16: muting it moves
    # nodes 284 to 296.
    Channel(16, "CallMe1", "CallMe 1", "line", stereo=True),
) + _VIRTUAL_DEVICES

CHANNELS_DUO = (
    Channel(0, "Mic1", "Mic 1", "mic"),
    Channel(1, "Mic2", "Mic 2", "mic"),
    # Source 2 is the 3.5mm TRRS HEADSET jack on a Duo, where it is Mic 3 on a Pro II. Classed
    # "line" and its Level writes inputDigitalGain; the desk's own INPUTSOURCE node for it looks
    # like a MIC's, which is an open question - see PROJECT_GUIDE part 2. Nobody has had a headset
    # in that jack to settle it by ear.
    Channel(2, "Headset", "Headset", "line"),
    Channel(7, "USB1", "USB 1", "line", stereo=True),
    Channel(8, "Chat", "Chat", "line", stereo=True),
    Channel(9, "USB2", "USB 2", "line", stereo=True),
    Channel(10, "Bluetooth", "Bluetooth", "line", stereo=True),
    # Mute is EAR-PROVEN here and only the mute: the pads go silent and come back, and the desk
    # mutes all 13 of source 11's cross-points on its own. The processing chain does nothing on
    # this desk either - see CHANNELS_PRO2 above.
    Channel(11, "SMARTpads", "SMART pads", "bus"),
    # No matrix row: source 16's row is measured on a Pro II only, and the Duo's baseline dump
    # reads rows 12 and up as placeholders.
    Channel(16, "CallMe1", "CallMe 1", "line", stereo=True, in_matrix=False),
) + _VIRTUAL_DEVICES

# capture_len=10, MEASURED: the Pro II's raw channelInputSource is TEN long, as is every per-strip
# array, and slot 9 mirrors USB 2 exactly as on the Duo. RODE document nine faders, so slot 9 is a
# spare the desk does not expose. Profiles CAPTURE it so it round-trips, and vector_len stays unset
# so restore writes only 9: writing slot 9 would show the layout tidy a duplicated source.
PRO2_FADERS = FaderSpec(
    count=9, physical=6, capture_len=10,
    # Faders 1-6 are analog pots; virtual faders 7-9 carry a settable level at the addresses
    # FaderSpec.level_selectors derives. 10 assignable sources over 9 faders, so one is always
    # off-fader.
    assignable=_assignable_view(CHANNELS_PRO2),
    # Only ADJACENT inputs link, which is RODE's own restriction, so these three are the whole set.
    # All three codes captured from the RODE app on hardware.
    stereo_pairs=((4, 0, 1, "Mic 1 + 2"), (5, 1, 2, "Mic 2 + 3"), (6, 2, 3, "Mic 3 + 4")),
    # Found by their INPUTSOURCE entries: every usable input carries a non-black `inputColour`, and
    # `inputDigitalGain` -12 marks the family that arrives over USB. `DevTools/input_sources.py`
    # prints that table. See _VIRTUAL_DEVICES for the order.
    expanded_sources=_expanded_assignable_view(CHANNELS_PRO2),
    expanded_relabel=_relabel_view(CHANNELS_PRO2),
    default_vector=(0, 1, 2, 3, 10, 11, 7, 8, 9),   # confirmed clean default
)

# Duo: 10-entry vector but only NINE faders. No Mic 3/Mic 4 (code 2 = HEADSET on the Duo).
#
# THE VECTOR LENGTH IS NOT THE FADER COUNT. Using it as one gives count=10/physical=10 and labels
# nothing virtual, so the app lists more faders than the desk has. RODE's own sources settle it:
# the Duo shipped with 4
# physical faders + 3 virtual, and firmware 1.7.3 "include[d] two extra virtual faders, allowing a
# 9 stereo channel audio mixer to be used" (update.rode.com/notes/rcp2). So: 9 faders, 4 physical,
# 5 virtual - the same 9 as the Pro II, but carried in a 10-slot vector.
#
# INDEX 9 IS THE NON-FADER, AND IT IS PROVEN RATHER THAN INFERRED. `PHYSICALINTERFACE` carries four
# POT nodes and then exactly NINE FADER nodes, at [0,4]..[0,12], for a ten-slot vector - so one slot
# has no fader behind it. Which one came from writing each fader's level and hearing which audio
# moved: fader index 5 moved source 7's thirteen cross-points and slot 5 holds source 7, so fader
# index == vector slot; fader index 8 moved nothing, and slot 8 is the empty strip (-1), which is an
# ordinary vacant fader rather than an absent one. That leaves slot 9, which also holds a DUPLICATE
# of USB 2 (already at index 7). So indices 0-8 are the nine faders, matching the Pro II, and the
# virtual labels and the encoder ring do NOT shift.
# Indices 0-3 = Mic 1, Mic 2, Bluetooth, SMART pads, which is exactly the four physical faders
# RODE describes ("after the two main input channels, there are faders for two stereo channels:
# Bluetooth and the SMART pads").
#
# physical=4 IS COUNTED ON HARDWARE, by hand, not read off RODE's wording: the four physical faders
# drive sources 0, 1, 10 and 11. Worth saying because the wording alone also supports TWO, if the
# stereo channels are taken not to be faders, and that reading is wrong.
# default_vector CONFIRMED as (0,1,10,11,2,7,8,9,-1,9): the channelInputSource from the stray-free
# fw-1.7.3 reference dump taken straight after a factory reset, verified as 10 entries starting at
# source 0 with no +1 shift (the dump itself is a capture and stays out of the repo), and matched
# byte-for-byte by an independent live read. So the empty strip (idx8 = -1) and the duplicated USB2
# (idx9 = 9, also at idx7) are the genuine SHIPPED default on a 10-strip Duo, NOT stray residue,
# which is what makes it a trustworthy reset target. 'reset faders' is enabled on the Duo (writes -1
# via signed build_global_prop_int and idx9 via the device-aware set_fader_source bound; both
# live-tested).
DUO_FADERS = FaderSpec(
    count=9, physical=4, vector_len=10,
    assignable=_assignable_view(CHANNELS_DUO),
    # ONE legal pair, and it is one because of what the Duo's inputs ARE, not caution. Pair codes are
    # positions in the firmware's InputSourceType enum, and both desks run the SAME binary, so code 4
    # is "1 + 2" everywhere.
    #
    # THE OTHER TWO ARE NOT THE SAME CASE AS EACH OTHER, so do not describe them as one. Be exact,
    # because one is impossible and one is a CHOICE:
    #   * CODE 6 = input 3 + input 4, and a Duo HAS no input 4. Impossible, full stop, and no amount
    #     of fader reassignment changes it: a pair combines two adjacent INPUTS at the input stage,
    #     and the pair then sits on one fader. Faders are slots you put a source on; they cannot
    #     conjure an input the desk does not have.
    #   * CODE 5 = input 2 + input 3, which on a Duo is Mic 2 + the HEADSET jack. Both are real,
    #     adjacent, physical inputs, so this one is POSSIBLE. It is left out because linking an XLR
    #     mic to a headset mic as a hard-panned stereo pair is a strange thing to want, and nobody
    #     has asked for it - a judgement, not a hardware limit.
    # ADDING CODE 5 WOULD NEED A REASON TO WANT IT, NOT A TEST. Writing it and watching the desk
    # accept it proves nothing: the engine takes source 3 on a Duo, hardware that does not exist
    # (see PROJECT_GUIDE part 2). And nobody has a 3.5mm headset to hear the result with.
    stereo_pairs=((4, 0, 1, "Mic 1 + 2"),),
    # The same four, on the same argument that settles the pair codes above: a source code is a
    # position in the firmware's InputSourceType enum and both desks run the SAME binary. A Duo has
    # Expanded and it is where the mode was found. NOT measured on a Duo, and the way to measure it
    # is `DevTools/input_sources.py` with one attached - four codes carrying a colour and a digital
    # gain of -12 is the signature.
    expanded_sources=_expanded_assignable_view(CHANNELS_DUO),
    default_vector=(0, 1, 10, 11, 2, 7, 8, 9, -1, 9),
)


# --- Input-strip specs (see InputStripSpec). Defined before DeviceModel so it can default to the
# Pro II shape. --------------------------------------------------------------------------------------
# Pro II: the five reassignable strips sit on faders 5 to 9 (vector indices 4 to 8); mics 1 to 4
# (faders 1 to 4) are fixed XLR inputs and are not listed.
PRO2_INPUT_STRIPS = InputStripSpec(
    strips=(
        ("Bluetooth", 4, "Fader 5 source"),
        ("SMARTpads", 5, "Fader 6 source"),
        ("USB1", 6, "Fader 7 source (virtual)"),
        ("Chat", 7, "Fader 8 source (virtual)"),
        ("USB2", 8, "Fader 9 source (virtual)"),
    ),
    sources=(
        (10, "Bluetooth"),
        (7, "USB 1 (Main Stereo)"),
        (8, "Chat"),
        (9, "USB 2 (Secondary)"),
        (11, "SMART pads"),
        (16, "CallMe 1"),
    ),
)

# Duo: EMPTY - it reassigns through the index-based FaderSpec only. See InputStripSpec.
DUO_INPUT_STRIPS = InputStripSpec()


@dataclass(frozen=True)
class DeviceModel:
    product_id: int
    name: str
    matrix: MatrixSpec
    # Capability flags. Each gates a WRITE path proven on THIS device; reads need no cap. A desk
    # without one refuses rather than writing Pro II-decoded selectors blindly, because an unmapped
    # selector does not error, it appends a stray.
    #   "input_mic_write" - gain/phantom/type/polarity and line Level
    #   "matrix_read"     - the routing-matrix read formula
    #   "matrix_write"    - cross-point and output-mode writes
    #   "processing"      - the per-strip processing and effects chain
    #   "channel_read"    - the per-channel read map
    #   "output_write"    - the global output block: multitrack, monitor/BT auto-mute (0x0D),
    #                       headphone type (0x09/0x0A), master Compellor and output delay (0x0E)
    #   "pad_write"       - SMART-pad create/clear/edit (region-B pool GC, effectsIdx==padIdx,
    #                       handle = pad count)
    #   "fader_write"     - setting a channel on a fader, vacating one (-1), and resetting to
    #                       FaderSpec.default_vector
    #   "unit_settings"   - display/haptics/REC-button/listen (rcp_transfer.UNIT_SETTING_SEL)
    #   The rest are explained where each desk's set is built.
    caps: frozenset
    # Per-channel PROCESSING and EFFECTS selector BASES (global form 01 01 01 01 <sel>). A channel's
    # processing selector is proc_sel_base + strip index and its effects selector fx_sel_base + strip
    # index, the strip being its live fader position. Same on both desks.
    proc_sel_base: int = 0x1C
    fx_sel_base: int = 0x26
    # Root-child index of INPUTSOURCE[0]: a 30-entry table, indexed by SOURCE CODE, that both desks
    # publish and that holds EVERY input value for EVERY channel. 0x26E on each, read off a live dump
    # of each desk rather than assumed shared. See input_path.
    input_table_base: int = 0x26E
    # Per-device READ MAP: the UI channels this desk exposes, built by _ui_channel_view. Each entry
    # is a dict of name, label, kind ("mic"|"line"|"bus"), stereo and source_code.
    #   source_code - the channelInputSource code. THE ONE PER-CHANNEL NUMBER: its position in the
    #                 live fader vector is the processing strip, and it indexes the INPUTSOURCE
    #                 table, where every input value is read and written (see input_path). There is
    #                 deliberately NO second "input_slot" field: it would hold the same value as
    #                 source_code on both desks, and two names for one number is what makes a
    #                 POSITIONAL input read look reasonable.
    ui_channels: tuple = ()
    # The same shape of entry for channels that EXIST ONLY IN EXPANDED. Kept apart rather than
    # flagged inside `ui_channels`, so every caller that has no business knowing about the mode
    # keeps the plain tuple and cannot accidentally offer a channel the desk is not carrying.
    # Reach for both through `ui_channels_for`. The fader side does the same thing with
    # `FaderSpec.expanded_sources`, and for the same reason.
    expanded_ui_channels: tuple = ()
    # The specs below default to the Pro II shape.
    pad: PadSpec = PadSpec(pads_per_bank=8)
    fader: FaderSpec = PRO2_FADERS
    input_strips: InputStripSpec = PRO2_INPUT_STRIPS
    headphones: HeadphoneTypeSpec = HEADPHONE_TYPES
    # GEOMETRY FROM THE DESK'S OWN TREE. False on the Pro II and the Duo, whose bases above are
    # measured constants. True on a desk resolved through layout_model: its bases come from
    # `layout` (a layout.Layout, or None while no dump of it has been parsed), and per-strip reads
    # go by address through that layout rather than by position in a values() list.
    layout_required: bool = False
    layout: object = None
    # WHY THIS DESK MUST NOT BE WRITTEN TO, or None if it may be. A sentence for the user. Checked by
    # require_writable, which every write helper calls before it builds an address, and by the send
    # layer in rcp_transfer, which refuses any product id outside DEVICES.
    write_block: Optional[str] = None

    # --- Where a channel's INPUT values live -------------------------------------------------
    # ONE ADDRESS, ON EVERY DESK: INPUTSOURCE[source_code]. Read there, written there.
    #
    # DO NOT ADD A PER-DEVICE INPUT SELECTOR TABLE. The RODE app sends a Duo's input writes to a MIX
    # node at 0x48 + source_code, and that node is DEAD STORAGE: a write lands and reads back, and
    # the audio engine never reads it. Byte-for-byte agreement with the official app is not evidence
    # that a write does anything. The proof is in PROJECT_GUIDE part 2. The Pro II's old selectors
    # (mics 0x6E-0x71, lines 0x75-0x78, trailer 0x02) are this same rule, 0x26E + code, and
    # test_input_read.py asserts them against the derivation.

    def input_path(self, ch: dict) -> list:
        """THE tree path of a channel's input values: INPUTSOURCE[source_code]. Read and write.

        Raises when this desk's input table is not known (a layout desk with no parsed dump yet),
        rather than addressing another desk's table."""
        if self.input_table_base is None:
            raise RuntimeError(f"The input table of the {self.name} is not known yet: no model "
                               f"dump of it has been read.")
        code = int(ch["source_code"])
        lay = self.layout
        if self.layout_required and lay is not None and not 0 <= code < lay.count("INPUTSOURCE"):
            raise ValueError(f"source {code} is outside the {self.name}'s input table")
        return [self.input_table_base + code]

    def channel_for_input_path(self, path) -> Optional[dict]:
        """The UI channel a pushed input address belongs to, or None. The reverse of input_path.

        The announcer needs this: when the DESK moves a gain it pushes the change at the address it
        owns, and the spoken line has to name the channel it belongs to."""
        if self.input_table_base is None:
            return None
        try:
            code = int(list(path)[0]) - self.input_table_base
        except Exception:
            return None
        return next((c for c in self.every_ui_channel
                     if c.get("source_code") == code), None)

    @property
    def every_ui_channel(self) -> tuple:
        """Every channel this desk can ever carry, whatever mode it is in, WITHOUT any relabel.

        FOR RESOLVING, NOT FOR DISPLAY, and that distinction is what `channel_for_input_path`
        needs: an address that resolves to Game is Game whatever mode we happen to think we are
        in, so a push must never fail to name a channel because a cached flag says otherwise. Take
        the LABEL from `ui_channels_for` instead - resolving with the wide set and then displaying
        its label is how the Standard-mode announcer came to say "USB 1 (System)"."""
        return tuple(self.ui_channels) + tuple(self.expanded_ui_channels)

    def ui_channels_for(self, expanded: bool) -> tuple:
        """The channel list to SHOW, given whether the desk is in Expanded.

        THE RELABEL COMES FROM `fader.expanded_relabel` rather than a second table of its own.
        Both are keyed by source code and both answer "what is this channel called in Expanded",
        so two tables would be one rename away from the Channels page and the Routing page calling
        the same input different things."""
        out = tuple(self.ui_channels) + (tuple(self.expanded_ui_channels) if expanded else ())
        if not expanded:
            return out
        renames = dict(self.fader.expanded_relabel)
        return tuple(dict(c, label=renames[c["source_code"]])
                     if c.get("source_code") in renames else c for c in out)

    @property
    def slug(self) -> str:
        """Short uppercase tag for this desk, for DEVICE-SCOPED generated filenames.

        "RODECaster Pro 2" -> PRO2, "RODECaster Duo" -> DUO. Derived from `name` so a new desk
        added to the table gets a slug for free rather than needing a per-generator lookup.

        A generated reference written to one shared path would be overwritten by whichever desk ran
        last, and `--check` compares NAMES only, which are identical across the two desks, so it
        would report "unchanged" while the file had swapped desks.
        """
        tail = self.name.replace("RODECaster", "").strip()      # "Pro 2" / "Duo"
        return "".join(ch for ch in tail if ch.isalnum()).upper() or "UNKNOWN"


# --- Read maps (see DeviceModel.ui_channels) ----------------------------------
# INPUT values (mic gain/phantom/polarity/type, line Level) are read and written BY ADDRESS at the
# channel's INPUTSOURCE entry, never by array position - see DeviceModel.input_path.
#
# PROCESSING and EFFECTS are per-STRIP, i.e. per FADER POSITION, not per source, so a channel's strip
# is resolved from the LIVE fader vector (channels._strip_index), on both desks. HARDWARE-CONFIRMED:
# with USB 1 moved onto strip 1, switching off strip 1's gate removes the gating from USB 1's audio.
# DO NOT PIN a strip per channel: on the default layout a pinned map returns the same indices as the
# live lookup, so it looks correct until somebody moves a fader.
PRO2_UI_CHANNELS = _ui_channel_view(CHANNELS_PRO2)

# RODE's Virtual Devices, present only while the desk is in Expanded (source codes measured on
# hardware; see FaderSpec.expanded_sources). Kind "line", like USB 1 and Chat, because that is what
# they are: a stereo feed arriving over the same USB cable. Each has its own INPUTSOURCE node
# carrying `inputDigitalGain`, so the Level control addresses the same property at the same kind of
# address as every other line input, and the processing chain is the ordinary per-STRIP one.
#
# THE LEVEL IS PROVEN ON HARDWARE and the processing is not. A write reaching its own address and
# reading back is never proof that it reaches audio - the SMART pads bus is the standing example,
# and it is why that one is kind "bus". These four are treated as ordinary line inputs because they
# arrive on the same cable as USB 1, which is processed; if one turns out to be inaudible, the
# entry to change is here.
EXPANDED_UI_CHANNELS = _expanded_ui_channel_view(CHANNELS_PRO2)

# Duo: INPUT values sit at INPUTSOURCE[source_code] exactly as on the Pro II. A factory desk reads
# inputDigitalGain -12 at codes 7/8/9/10 (USB1, Chat, USB2, BT) and 0 at code 2 (Headset),
# inputMicrophoneGain 50 throughout, and inputPower 0 on the mics and -1 (not applicable) on the
# line inputs. Processing and effects are per strip, resolved live as on the Pro II.
DUO_UI_CHANNELS = _ui_channel_view(CHANNELS_DUO)


# --- Routing-matrix UI label maps (see MatrixSpec.sources / .outputs) ----------
# Indices match rcp_transfer.INPUT_SOURCE_CODE / CROSS_POINT_OUTPUT_INDEX; the inferred CallMe 2/3
# outputs stay unexposed.
PRO2_MATRIX_SOURCES = _matrix_source_view(CHANNELS_PRO2)
PRO2_MATRIX_EXPANDED_SOURCES = _expanded_matrix_source_view(CHANNELS_PRO2)
# "default_mode" (0 Main Mix / 1 Mix Minus / 2 Custom) is the mode "Reset all output mixes" puts
# each output back to. All 11 match a factory dump taken at the setup wizard and a
# showControlNewFromDefault show. Chat is MIX MINUS by default: a profile reading Chat = Custom is a
# "Chat = my mic only" preset. USB 1 keeps Main Mix despite offering mix-minus, because it is the
# program feed to the computer, not a caller.
# The array read back is THIRTEEN long, not 11: indices 11 and 12 exist in MIXMINUSES and are not
# exposed as outputs on either desk. Do not size a loop off len(read_output_modes()).
PRO2_MATRIX_OUTPUTS = (
    {"index": 0, "key": "Headphone1", "label": "Headphone 1", "mixminus": False, "default_mode": 0},
    {"index": 1, "key": "Headphone2", "label": "Headphone 2", "mixminus": False, "default_mode": 0},
    {"index": 2, "key": "Headphone3", "label": "Headphone 3", "mixminus": False, "default_mode": 0},
    {"index": 3, "key": "Headphone4", "label": "Headphone 4", "mixminus": False, "default_mode": 0},
    {"index": 4, "key": "Monitor", "label": "Monitor (speaker)", "mixminus": False, "default_mode": 0},
    {"index": 5, "key": "REC", "label": "Recording", "mixminus": False, "default_mode": 0},
    {"index": 6, "key": "Bluetooth", "label": "Bluetooth", "mixminus": True, "default_mode": 1},
    {"index": 7, "key": "USB1", "label": "USB 1 (Main Stereo)", "mixminus": True, "default_mode": 0},
    {"index": 8, "key": "Chat", "label": "Chat", "mixminus": True, "default_mode": 1},
    {"index": 9, "key": "USB2", "label": "USB 2 (Secondary)", "mixminus": True, "default_mode": 1},
    {"index": 10, "key": "CallMe1", "label": "CallMe 1", "mixminus": True, "default_mode": 1},
)

# Duo: the Pro II's sources minus Mic 4, with source code 2 the HEADSET jack (Mic 3 on a Pro II).
DUO_MATRIX_SOURCES = _matrix_source_view(CHANNELS_DUO)
DUO_MATRIX_EXPANDED_SOURCES = _expanded_matrix_source_view(CHANNELS_DUO)
# NINE outputs, read off RODE's app's Output > Routing screen. A Duo has two headphone jacks, so it
# SKIPS Pro II indices 2 and 3 and keeps the same indexing for the rest: its factory outputMixMinus
# [0,0,0,0,0,0,1,0,1,1,1,1,1] lines up with the Pro II's indices exactly.
# default_mode is the Pro II's set by key; a Duo's factory output modes have not been dumped.
DUO_MATRIX_OUTPUTS = (
    {"index": 0, "key": "Headphone1", "label": "Headphone 1", "mixminus": False, "default_mode": 0},
    {"index": 1, "key": "Headphone2", "label": "Headphone 2", "mixminus": False, "default_mode": 0},
    {"index": 4, "key": "Monitor", "label": "Monitor (speaker)", "mixminus": False, "default_mode": 0},
    {"index": 5, "key": "REC", "label": "Recording", "mixminus": False, "default_mode": 0},
    {"index": 6, "key": "Bluetooth", "label": "Bluetooth", "mixminus": True, "default_mode": 1},
    {"index": 7, "key": "USB1", "label": "USB 1", "mixminus": True, "default_mode": 0},
    {"index": 8, "key": "Chat", "label": "Chat", "mixminus": True, "default_mode": 1},
    {"index": 9, "key": "USB2", "label": "USB 2", "mixminus": True, "default_mode": 1},
    {"index": 10, "key": "CallMe1", "label": "CallMe 1", "mixminus": True, "default_mode": 1},
)


PRO2 = DeviceModel(
    product_id=PID_PRO2,
    name="RODECaster Pro 2",
    matrix=MatrixSpec(cell_count=390, output_count=11,
                      source_indices=_matrix_index_view(CHANNELS_PRO2), row_stride=13,
                      sources=PRO2_MATRIX_SOURCES,
                      expanded_sources=PRO2_MATRIX_EXPANDED_SOURCES,
                      expanded_relabel=_relabel_view(CHANNELS_PRO2), outputs=PRO2_MATRIX_OUTPUTS),
    # The caps not listed on DeviceModel.caps:
    # "chain_follow": a fader move also moves that channel's processing/effects chain to the strip
    # it lands on.
    # "stereo_link": two adjacent mic inputs linked onto one fader, by their pair code. Guarded by
    # FaderSpec.stereo_pairs too, so an empty pair table refuses on its own.
    # "level_write": the VIRTUAL fader levels and the monitor speaker level (RODECASTER_PROTOCOL
    # 6.1).
    # "recorder": start, pause and stop, through requestRecordState on RECORDER 0x03, a selector the
    # desk pushes while its REC button is pressed.
    # "network_write": wifi on/off, joining a network and Bluetooth discoverability, on NETWORK
    # 0x10. Proven by a phone pairing and playing audio, and by a network joined with the
    # touchscreen untouched.
    # "shows": the desk's own backup - export, import, software factory reset and delete, on
    # SHOWCONTROL 0x28E. Two full reset-and-restore cycles from our own writes, the second leaving
    # zero real differences against a snapshot taken just before.
    # "callme": hosting and answering a CallMe call, on the SIPCALLING node found by walking the
    # tree. Heard both ways with a browser guest.
    # "storage_write": ejecting and erasing the card or drive, on the STORAGEVOLUME nodes.
    caps=frozenset({"input_mic_write", "matrix_read", "matrix_write", "processing",
                    "channel_read", "output_write", "pad_write", "fader_write", "chain_follow",
                    "level_write", "unit_settings", "recorder", "network_write", "shows",
                    "stereo_link", "callme", "storage_write"}),
    proc_sel_base=0x1C,
    fx_sel_base=0x26,
    ui_channels=PRO2_UI_CHANNELS,
    expanded_ui_channels=EXPANDED_UI_CHANNELS,
    pad=PadSpec(pads_per_bank=8),   # 8 pads/bank x 8 banks = 64 slots; system pads 56-63
    fader=PRO2_FADERS,
    input_strips=PRO2_INPUT_STRIPS,
)

# --- Duo ----------------------------------------------------------------------------------------
# Every Pro II cap except storage_write, which waits for a Duo with a card or drive in it. Most were
# proven by the TREE, then the ENGINE: the property resolves to a node of the right block on a live
# Duo, then a write makes the desk do something we did not send. AN ENGINE ANSWERING PROVES AN
# ADDRESS IS LIVE, NEVER THAT A FEATURE EXISTS ON THIS DESK: the engine's model is device-agnostic
# and answers for Mic 4, hardware a Duo does not have, exactly as for Mic 2 (PROJECT_GUIDE part 2).
# STRAYS PERSIST ACROSS A POWER CYCLE here too, and only a factory reset clears them: one shifts
# inputMicrophoneGain and channelInputSource +1, mis-targeting every read and processing write.
DUO = DeviceModel(
    product_id=PID_DUO,
    name="RODECaster Duo",
    # Same scheme as the Pro II, by routing capture and a reversible live write: read index
    # 13 * source + output, cross-point write selector 0x4C + index, output-mode selector
    # 0x31 + output. cell_count is the MIX block's 390.
    matrix=MatrixSpec(cell_count=390, output_count=13,
                      source_indices=_matrix_index_view(CHANNELS_DUO), row_stride=13,
                      sources=DUO_MATRIX_SOURCES,
                      expanded_sources=DUO_MATRIX_EXPANDED_SOURCES,
                      expanded_relabel=_relabel_view(CHANNELS_DUO), outputs=DUO_MATRIX_OUTPUTS),
    # What establishes each on this desk:
    # "processing": console capture on six channels, 0x1C + strip with the empty strips leaving
    # gaps, and echoOn on Mic 2 at 0x27 = 0x26 + strip 1.
    # "output_write": RODE app capture, every selector byte-identical to the Pro II's.
    # "pad_write": a reversible live create/clear proved the three firmware behaviours a read
    # cannot: clear GCs the bound region-B object, a bare create does not append to the pool, and
    # handle = pad count. Then the web endpoints end to end.
    # "fader_write": set a source on the empty strip, vacate it with -1, write slot 9, and reset to
    # DUO_FADERS.default_vector, each read back byte-clean.
    # "unit_settings": every rcp_transfer.UNIT_SETTING_SEL property resolves to a node of the right
    # block (GUI 0x07, OUTPUT 0x0D, SYSTEM 0x0F), and one from each block wrote, read back and
    # restored byte-clean (DevTools/test_duo_caps_live.py).
    # "network_write", "recorder", "shows": tree then engine. The engine answers a `wifi` False by
    # clearing the desk's IP address, consumes a requestRecordState and self-clears it, and refuses
    # a show export with showControlLastError 8 (no storage). No Duo here has made a recording or a
    # show, for want of a card.
    # "level_write": tree then engine; a fader level written to 40 recomputes all thirteen of that
    # source's cross-points to 40/127.
    # "chain_follow": a move onto the empty fader and a swap between two occupied ones, each
    # carrying its chain and restoring byte-identical.
    # "stereo_link": RODE's Duo guide documents linking, pair codes are firmware enum positions
    # shared by both desks, and Mic 2 is heard hard-panned when linked. See DUO_FADERS.stereo_pairs.
    # "callme": every write finds its node by property on the live tree; a Pro II to Duo call
    # connects both ways.
    # THE WIFI SCAN is the one network write not proven here: a Duo already joined to a network
    # fills no slot. action_scan_wifi warns that an empty result may be spurious.
    caps=frozenset({"input_mic_write", "processing", "channel_read", "matrix_read",
                    "matrix_write", "output_write", "pad_write", "fader_write",
                    "level_write", "chain_follow", "unit_settings", "stereo_link",
                    "network_write", "recorder", "shows", "callme"}),
    proc_sel_base=0x1C,
    fx_sel_base=0x26,
    ui_channels=DUO_UI_CHANNELS,
    expanded_ui_channels=EXPANDED_UI_CHANNELS,
    # 6 pads/bank x 8 banks = 48 slots; system pads at padIdx 42-47 (bank 8).
    pad=PadSpec(pads_per_bank=6),
    fader=DUO_FADERS,
    input_strips=DUO_INPUT_STRIPS,
)

DEVICES = {PID_PRO2: PRO2, PID_DUO: DUO}

# Consistency guard (cheap, import-time): the matrix label maps must line up with the numeric
# read spec, so a future edit that adds a source label but forgets source_indices (or vice versa)
# fails loudly here instead of silently mislabelling a cross-point on the routing screen.
for _dev in DEVICES.values():
    _m = _dev.matrix
    if _m.sources:
        assert {s["index"] for s in _m.sources} == set(_m.source_indices), (
            f"{_dev.name}: matrix.sources indices {sorted(s['index'] for s in _m.sources)} "
            f"!= source_indices {sorted(_m.source_indices)}")
    if _m.outputs:
        assert all(0 <= o["index"] < _m.output_count for o in _m.outputs), (
            f"{_dev.name}: a matrix.outputs index is outside 0..{_m.output_count - 1}")
    # Headphone spec must list at least one output + at least one value, and every output selector
    # must be one of the KNOWN headphoneType selectors (guards against a future edit adding an
    # unconfirmed selector - which would risk a stray append when written). 0x09/0x0A only.
    _hp = _dev.headphones
    assert _hp.outputs and _hp.values, f"{_dev.name}: headphones spec must list outputs + values"
    assert all(sel in (0x09, 0x0A) for sel in _hp.output_selectors), (
        f"{_dev.name}: a headphone output selector is not a known headphoneType selector (0x09/0x0A)")
del _dev, _m, _hp

# The default when NO device is connected (product id None): Pro II, so all existing Pro II
# behaviour is unchanged for offline code paths and unit tests. A CONNECTED desk with a product id
# outside DEVICES never gets this - see resolve_device.
DEFAULT = PRO2


# --- DESKS WHOSE GEOMETRY COMES FROM THEIR OWN TREE ----------------------------------------------
# PID 0x0092 is a RODECaster Pro II on firmware 1.7.6 (VID 0x19F7, control interface MI_09, usage
# page 0xFF00 usage 1) whose root children sit at DIFFERENT indexes from the Pro II this app was
# built on: CHANNEL at 0x1A (not 0x1C), MIX at 0x37 (not 0x4C), INPUTSOURCE at 0xDF (not 0x26E), and
# OUTPUT/SYSTEM/NETWORK two lower. So every Pro II selector constant addresses a DIFFERENT node
# there, and writing one appends a stray. Its geometry is read off its own dump instead (layout.py).
# write_block still makes every Pro II selector-building helper refuse there (require_writable);
# what it DOES take goes through the narrow caps and the generalised gate below (layout_write_ok),
# each judged from the finished record against that desk's own dump.
#
# WHAT IS MEASURED ON THAT UNIT, and nothing else is assumed:
#   * the main runs: CHANNEL x9 at 0x1A, EFFECTS_PARAMETERS x10 at 0x23, MIXMINUSES x10 at 0x2D,
#     MIX x156 at 0x37 (= 12 x 13), INPUTSOURCE x12 at 0xDF;
#   * fader 1's movement pushes mixLevelWithAnchor on 0x37..0x43, its mute pushes channelOutputMute
#     on 0x1A and mixMute on 0x37..0x43, its cue button channelCueEnable on 0x1A;
#   * the fader layout reads [0, 1, 6, 7, 9, 8, -1, 11, 10], and its owner confirmed faders 1-6 as
#     Mic 1, Mic 2, the Mic 3 + 4 pair (set to line input), USB 1, USB 2 and Chat. So the Pro II
#     SOURCE CODES 0, 1, 6, 7, 8 and 9 mean the same on this unit; INPUTSOURCE inputType 2 = line was
#     confirmed on the Mic 3 + 4 source.
PID_LAYOUT_0092 = 0x0092
# THE SAME UNIT IN "EXPANDED" MODE (Output > Multitrack > USB 1 Input > Expanded, for the RODE
# Virtual Device Driver): the desk re-enumerates on USB as 0x0094. Measured 7th October 2026 by a
# dump before and after the switch: the tree is identical except OUTPUT outputMultiMode 1 -> 4, so
# every group sits at the same root index and the layout read off the dump applies unchanged. It
# keeps the 0x0092 name so profiles and baselines stay one desk across the two modes.
PID_LAYOUT_0094 = 0x0094
# THE NAME IS SHORT AND PLAIN ON PURPOSE: it ends up in a profile name ("As found <date> <desk>"),
# which takes letters, digits, spaces and dashes only and 40 characters in all (profiles.safe_name),
# so brackets or a longer name silently drop the desk from the baseline's name.
LAYOUT_PIDS = {PID_LAYOUT_0092: "RODECaster Pro2 0092", PID_LAYOUT_0094: "RODECaster Pro2 0092"}

# (code, label) per Pro II source code this unit may hold, reusing the Pro II wording so the two
# desks say the same thing. CONFIRMED on the unit: 0, 1, 7, 8, 9 (and pair code 6, below).
# UNCONFIRMED there, labelled from the Pro II only: 2 and 3 (Mic 3 / Mic 4 - implied by the 3 + 4
# pair, never seen on a fader of their own), 10 (Bluetooth) and 11 (SMART pads), which sit on
# faders 9 and 8, the two virtual faders nobody has checked. CallMe 1 (16) is left out: it lies
# outside the main INPUTSOURCE run of 12 on this unit.
_LAYOUT_SOURCES = (
    (0, "Mic 1"), (1, "Mic 2"),
    (2, "Mic 3"), (3, "Mic 4"),                            # unconfirmed on PID 0x0092
    (7, "USB 1 (Main Stereo)"), (8, "Chat"), (9, "USB 2 (Secondary)"),
    (10, "Bluetooth"), (11, "SMART pads"),                 # unconfirmed on PID 0x0092
)
# Stereo pairs, for NAMING a fader that holds one (FaderSpec.all_labels). Code 6 = Mic 3 + 4 is
# confirmed on the unit (fader 3); 4 and 5 are the Pro II codes, unconfirmed there. Never written:
# the "stereo_link" cap is absent.
_LAYOUT_PAIRS = ((4, 0, 1, "Mic 1 + 2"), (5, 1, 2, "Mic 2 + 3"), (6, 2, 3, "Mic 3 + 4"))
_LAYOUT_UI = {c["source_code"]: c for c in PRO2_UI_CHANNELS}
# THE ROUTING ROWS (matrix sources): all 12 rows of the main MIX run, one per INPUTSOURCE code.
# Confirmed on the unit: 0 (fader 1's pushes land on row 0), and 1, 6, 7, 8, 9 by the fader layout
# its owner confirmed. Every other name is the Pro II's or the pair table's and is said to be
# unconfirmed IN THE LABEL, so the page never states a guess as a fact.
_LAYOUT_ROW_UNCONFIRMED = {2: ("Mic3", "Mic 3"), 3: ("Mic4", "Mic 4"), 4: ("Mic1+2", "Mic 1 + 2"),
                           5: ("Mic2+3", "Mic 2 + 3"), 10: ("Bluetooth", "Bluetooth"),
                           11: ("SMARTpads", "SMART pads")}
# THE OUTPUTS ARE THE PRO II ORDER (RODECASTER_PROTOCOL.txt 5.2), with its keys, names and default
# modes. What stands behind that on this unit, and nothing more:
#   * output 0 = Headphone 1 is MEASURED: on 7th October 2026 its owner set output 0 to Custom and
#     took Mic 1 and Mic 2 out of it, and the sound on the headphones in jack 1 changed accordingly
#     (read back: MIXMINUSES 0x2D outputMixMinus 0 -> 2);
#   * the unit's 13 mode values read [0,0,0,0,0,0,1,0,1,1,1,1,1], which is EXACTLY the Pro II's
#     default per output (headphones, monitor, recording and USB 1 on Main Mix; Bluetooth, Chat,
#     USB 2 and the CallMe outputs on Mix Minus);
#   * the owner ACCEPTED the Pro II order for all 13 outputs on that basis (7th October 2026).
# Outputs 1..12 have NOT each been measured by ear. The names are shown plainly (no "unconfirmed"),
# by the owner's decision; this comment is where the difference is kept.
# Outputs 11 and 12 (CallMe 2 / 3) exist on this unit's grid and have no Pro II entry, so they are
# named here in the same pattern, Mix Minus by default like CallMe 1 (and as the unit reads them).
_LAYOUT_OUTPUTS = tuple(dict(o, mixminus=True) for o in PRO2_MATRIX_OUTPUTS) + (
    {"index": 11, "key": "CallMe2", "label": "CallMe 2", "mixminus": True, "default_mode": 1},
    {"index": 12, "key": "CallMe3", "label": "CallMe 3", "mixminus": True, "default_mode": 1},
)


def _layout_mode_runs(lay, stride: int) -> tuple:
    """The LATER MIXMINUSES runs that complete the main run to one mode per output (`stride`), as
    ((base, count), ...), or () if they do not add up to exactly that. Read only."""
    main = lay.run("MIXMINUSES")
    if main is None or main.count >= stride:
        return ()
    extra, total = [], main.count
    for r in lay.all_runs:
        if r.name != "MIXMINUSES" or r.base <= main.base:
            continue
        extra.append((r.base, r.count))
        total += r.count
        if total >= stride:
            break
    return tuple(extra) if total == stride else ()

_WRITE_BLOCK_LAYOUT = ("That setting cannot be changed on the {name} from here: its blocks sit at "
                       "other addresses than on the RODECaster Pro II, so AccessCaster only writes "
                       "a setting there through the unit's own layout, where the setting already "
                       "exists on that block, and confirms it by reading it back.")
_WRITE_BLOCK_UNKNOWN = ("The {name} is not a desk this app knows, so AccessCaster will not read its "
                        "layout or change any of its settings.")


def layout_headphone_outputs(lay) -> tuple:
    """((root index, "Headphone N"), ...) for a layout desk's HEADPHONE run, or () unless that run
    is exactly LAYOUT_HEADPHONE_COUNT blocks long. Measured on PID 0x0092 (7th October 2026): four
    consecutive HEADPHONE blocks at 0x07..0x0A, right after APP (0x06) and before OUTPUT (0x0B);
    RODE's app changing headphone 1 moved 0x07 only. 0x08..0x0A are headphones 2..4 by run order."""
    run = lay.run("HEADPHONE") if lay is not None else None
    if run is None or run.count != LAYOUT_HEADPHONE_COUNT:
        return ()
    return tuple((run.base + i, f"Headphone {i + 1}") for i in range(run.count))


def _geometry_unknown_model(pid: int, name: str, write_block: str) -> DeviceModel:
    """A connected desk whose geometry is NOT known: no channels, no matrix, no faders, no caps,
    no bases. Every read that needs an address reports nothing and every write refuses."""
    return DeviceModel(
        product_id=pid, name=name,
        matrix=MatrixSpec(cell_count=0, output_count=0, source_indices=(), row_stride=13),
        caps=frozenset(), proc_sel_base=None, fx_sel_base=None, input_table_base=None,
        ui_channels=(), fader=FaderSpec(count=0, physical=0), input_strips=InputStripSpec(),
        headphones=HeadphoneTypeSpec(), layout_required=True, layout=None,
        write_block=write_block.format(name=name))


def layout_model(pid: int, lay) -> DeviceModel:
    """The DeviceModel of a LAYOUT desk (LAYOUT_PIDS), its geometry taken from `lay`.

    Every number comes from the layout's MAIN runs: the matrix is MIX (base, cells) with
    rows = INPUTSOURCE run and outputs = MIXMINUSES run, the input table is INPUTSOURCE, the strips
    are CHANNEL and their count is the fader count. Without a valid layout the desk is
    geometry-unknown (_geometry_unknown_model) rather than borrowing another desk's numbers.
    """
    name = LAYOUT_PIDS.get(pid) or f"RODECaster PID {int(pid):04X}"
    if lay is None or not getattr(lay, "valid", False):
        return _geometry_unknown_model(pid, name, _WRITE_BLOCK_LAYOUT)
    n_src = lay.count("INPUTSOURCE")
    sources = tuple((c, lab) for c, lab in _LAYOUT_SOURCES if c < n_src)
    pairs = tuple(p for p in _LAYOUT_PAIRS if p[0] < n_src)
    # Matrix rows: every row of the main MIX run (= every INPUTSOURCE code of the main run), each
    # named, the unconfirmed ones saying so (_LAYOUT_ROW_UNCONFIRMED). The Mic 3 + 4 pair (code 6)
    # is a routing row of its own because this unit's fader 3 holds it.
    keys = {c: _LAYOUT_UI[c]["name"] for c, _ in sources if c in _LAYOUT_UI}
    labels = dict(sources)
    if 6 < n_src:
        keys[6], labels[6] = "Mic3+4", "Mic 3 + 4"
    for c, (k, lab) in _LAYOUT_ROW_UNCONFIRMED.items():
        if c < n_src:
            keys[c], labels[c] = k, f"{lab} (unconfirmed)"
    rows = tuple(sorted(keys))
    n_strips = lay.count("CHANNEL")
    stride = lay.count("MIX") // n_src
    # Outputs: one per cell of a row. The main MIXMINUSES run carries the first modes (10 here) and
    # a later run the rest (3 at 0xFC); only when they add up to exactly one mode per output is the
    # later run used, else the outputs stop at the main run.
    modes_extra = _layout_mode_runs(lay, stride)
    n_out = lay.count("MIXMINUSES") + sum(c for _b, c in modes_extra)
    return DeviceModel(
        product_id=pid,
        name=name,
        matrix=MatrixSpec(
            cell_count=lay.count("MIX"),
            # An output is a MIXMINUSES block; a source row is 13 cells (layout.ROW_STRIDE).
            output_count=n_out,
            # Every row of the main MIX run, the unconfirmed ones labelled so.
            source_indices=rows,
            row_stride=stride,
            base=lay.base("MIX"),
            modes_base=lay.base("MIXMINUSES"),
            modes_extra=modes_extra,
            sources=tuple({"index": c, "key": keys[c], "label": labels[c]} for c in rows),
            # The Pro II order, keys, names and default modes (_LAYOUT_OUTPUTS: output 0 measured,
            # the rest accepted by the owner on the matching default modes). Mix Minus is offered
            # for every one because the mode READ can return it anywhere. An output past that
            # table would be named by position with no default, which keeps "reset all" off.
            outputs=tuple(dict(_LAYOUT_OUTPUTS[o]) if o < len(_LAYOUT_OUTPUTS) else
                          {"index": o, "key": f"Output{o + 1}", "label": f"Output {o + 1}",
                           "mixminus": True, "default_mode": None}
                          for o in range(n_out))),
        # READ caps, plus four narrow write caps and nothing else: "input_write" (the INPUTSOURCE
        # set in LAYOUT_INPUT_WRITES), "fx_write" (echo/reverb on a strip's EFFECTS_PARAMETERS
        # block, LAYOUT_FX_WRITES), "fader_write" (channelInputSource on the main CHANNEL run,
        # codes LAYOUT_FADER_CODES only, see layout_fader_write_ok), "strip_write" (processing, mute
        # and solo on the main CHANNEL run, LAYOUT_STRIP_WRITES - NOT the "processing" cap, which
        # stands for the Pro II selector path and its presets) and "routing_write" (the
        # cross-points of the main MIX run and the modes of the main MIXMINUSES run,
        # LAYOUT_ROUTING_CELL_WRITES, see layout_routing_write_ok). NOT "matrix_write": that cap
        # opens the Pro II routing paths (mix presets, reset all, auto gain's Chat isolation), which
        # stay refused here. Every other write family stays refused (write_block) until proven
        # here. NOT "chain_follow", "level_write" or "stereo_link": a fader move here moves the
        # source only. Two more narrow caps for the master output block, NOT "output_write" (which
        # opens every Pro II global at its 0x0D / 0x0E / 0x09 selectors): "headphone_write"
        # (headphoneType 0/1/2 on the four HEADPHONE blocks, layout_headphone_write_ok) and
        # "usbmode_write" (outputMultiMode 1 or 4 on OUTPUT, layout_usbmode_write_ok).
        # And "tree_write", the GENERALISED gate (layout_tree_write_ok): any other setting the Pro
        # II code writes, at this desk's own block for it (found by NAME in its dump), when the
        # property already sits on that very block with that kind and is not a command
        # (LAYOUT_TREE_DENY). It stands in for the Pro II caps "output_write" and "unit_settings"
        # (has_write_cap); everything it lets through is also checked before and confirmed after
        # (layout_tree.write_records). The narrow caps above keep their own, stricter checks.
        caps=frozenset({"matrix_read", "channel_read", "input_write", "fx_write", "fader_write",
                        "strip_write", "routing_write", "headphone_write", "usbmode_write",
                        "tree_write"}),
        proc_sel_base=lay.base("CHANNEL"),
        # EFFECTS_PARAMETERS[strip] for strips 0..8 (see LAYOUT_FX_WRITES): the Pro II / Duo rule,
        # effects block k belongs to fader strip k. PROVEN for strip 0 (Mic 1, 0x23, heard live). The run is 10 long against
        # 9 strips; the tenth block is never read or written.
        fx_sel_base=lay.base("EFFECTS_PARAMETERS"),
        input_table_base=lay.base("INPUTSOURCE"),
        ui_channels=tuple(_LAYOUT_UI[c] for c, _ in sources if c in _LAYOUT_UI),
        # Pad grid NOT measured on this unit; the Pro II shape is kept for READING only (pad reads
        # window on padName and need no address), and "pad_write" is absent.
        pad=PadSpec(pads_per_bank=8),
        fader=FaderSpec(
            count=n_strips,
            # Physical/virtual split unconfirmed on this unit, so nothing is labelled virtual.
            physical=n_strips,
            assignable=sources,
            stereo_pairs=pairs,
            default_vector=None),          # not known: 'reset faders' refuses
        input_strips=InputStripSpec(),
        # The HEADPHONE run read off the dump (0x07..0x0A on PID 0x0092, not the Pro II's 0x09/0x0A),
        # listed only when it is exactly the four outputs (layout_headphone_outputs).
        headphones=HeadphoneTypeSpec(outputs=layout_headphone_outputs(lay),
                                     values=HEADPHONE_TYPES.values)
        if layout_headphone_outputs(lay) else HeadphoneTypeSpec(),
        layout_required=True,
        layout=lay,
        write_block=_WRITE_BLOCK_LAYOUT.format(name=name),
    )


_model_cache: dict = {}


def resolve_device(product_id: Optional[int]) -> DeviceModel:
    """Map a USB product id to its DeviceModel.

    None (nothing connected, or an offline caller) -> DEFAULT (Pro II), as it always has been.
    A KNOWN static desk -> its table entry. A LAYOUT desk (LAYOUT_PIDS) -> layout_model built on the
    last layout recorded for it. ANY OTHER connected product id -> a geometry-unknown model that
    reads nothing by address and writes nothing: it is NEVER silently treated as a Pro II.
    """
    if product_id is None:
        return DEFAULT
    if product_id in DEVICES:
        return DEVICES[product_id]
    if product_id in LAYOUT_PIDS:
        lay = _layout.current(product_id)
        key = (product_id, id(lay))
        cached = _model_cache.get(key)
        if cached is None or cached.layout is not lay:
            cached = layout_model(product_id, lay)
            _model_cache.clear()
            _model_cache[key] = cached
        return cached
    return _geometry_unknown_model(product_id, f"RODECaster PID {int(product_id):04X}",
                                   _WRITE_BLOCK_UNKNOWN)


class WriteRefused(RuntimeError):
    """A write was asked of a desk whose write addresses are not confirmed. Nothing was sent."""


def require_writable(dev: Optional[DeviceModel] = None, what: str = "") -> DeviceModel:
    """Raise WriteRefused if the desk may not be written to; returns the device otherwise.

    Call it BEFORE building an address: on a desk whose blocks sit elsewhere, the Pro II selector a
    helper would compute names a different node, and sending it appends a stray only a factory
    reset clears."""
    dev = dev or get_active_device()
    if dev.write_block:
        raise WriteRefused(dev.write_block if not what else f"{what}: {dev.write_block}")
    return dev


# THE ONLY WRITES A LAYOUT DESK TAKES: {property: (kind, min, max)}, on INPUTSOURCE[source] of the
# MAIN run, one-level address - the form the desk itself pushes these in (measured 7th October 2026
# on PID 0x0092: a gain turned on the desk arrives at INPUTSOURCE[0xDF + source], one level).
# Exactly what the Channels page writes for a mic or line input on the Pro II, minus two:
#   * inputType - the app never writes it on any desk: it is derived from the mic type.
#   * inputInstrumentGain - only a profile RESTORE writes it, which stays refused, and no range for
#     it has been measured.
# Ranges are the Pro II UI's (server.GAIN_MIN_DB/MAX, LEVEL_MIN_DB/MAX); the unit's dump sits well
# inside them (mic gains 27-51, line levels -12 and 0, phantom 0/1).
# inputMicrophoneType uses the Pro II voicing list (channels.MIC_TYPE, 0..10), PROVEN to be this
# unit's numbering on 7th October 2026: Mic 1 reads 9 and is an Electro-Voice RE20 (9 = "RE20"),
# Mic 2 reads 5 and is a RODE Broadcaster (5 = "Broadcaster"), and the line pair reads 0
# ("Line-in"). Mics only (sources 0..3, LAYOUT_INPUT_SOURCE_LIMIT). Choosing a voicing also makes
# the desk apply that voicing's gain and chain; the write confirms the TYPE only.
LAYOUT_INPUT_WRITES = {
    "inputMicrophoneGain": ("int", 0, 76),
    "inputPower": ("int", 0, 1),
    "inputPhaseFlip": ("bool", None, None),
    "inputDigitalGain": ("int", -60, 12),
    "inputMicrophoneType": ("int", 0, 10),
}
# Sources the input writes may address on a layout desk: the main INPUTSOURCE run, codes 0..11.
LAYOUT_INPUT_MAX_SOURCE = 11
# A narrower source limit for a property: the voicing belongs to the four mic inputs only.
LAYOUT_INPUT_SOURCE_LIMIT = {"inputMicrophoneType": 3}


def layout_input_write_ok(dev: DeviceModel, path, name: str, value, kind: str) -> Optional[str]:
    """None if this one input write is allowed on a layout desk, else the reason it is not."""
    if "input_write" not in dev.caps or dev.layout is None or dev.input_table_base is None:
        return f"Input writes are not enabled on the {dev.name}."
    spec = LAYOUT_INPUT_WRITES.get(name)
    if spec is None:
        return f"{name} is not one of the input settings enabled on the {dev.name}."
    path = list(path or ())
    if len(path) != 1:
        return f"{name} must address one INPUTSOURCE block on the {dev.name}."
    code = int(path[0]) - dev.input_table_base
    top = min(LAYOUT_INPUT_MAX_SOURCE, LAYOUT_INPUT_SOURCE_LIMIT.get(name, LAYOUT_INPUT_MAX_SOURCE))
    if not 0 <= code < min(dev.layout.count("INPUTSOURCE"), top + 1):
        return f"Root index 0x{int(path[0]):02X} is not an input source the {dev.name} may write."
    want_kind, lo, hi = spec
    if kind != want_kind or (want_kind == "int" and isinstance(value, bool)):
        return f"{name} must be written as {want_kind} on the {dev.name}."
    if lo is not None and not lo <= int(value) <= hi:
        return f"{name} {value} is outside {lo} to {hi}."
    return None


# THE EFFECTS WRITES A LAYOUT DESK TAKES (cap "fx_write"): exactly what the Pro II Effects tab writes
# (channels.CHANNEL_FX_ON + CHANNEL_FX_PARAMS): echo and reverb on/off, and their params as floats
# normalised 0..1 - the range comes from the Pro II code (server.action_set_channel_effect_param and
# channels.set_channel_effect_param clamp to 0..1), and every one of these reads 0..1 in the 0x0092
# dump. NOT pitch shift, distortion, robot or voice disguise: the block carries them, but no screen
# of this app writes them on any desk, and pitchShiftSemitones reads 7.0, i.e. is not a 0..1 value.
#
# THE ADDRESS: EFFECTS_PARAMETERS[strip] at its run base + the fader STRIP index, which is how the
# Pro II and the Duo are proven (0x26 + strip right after their CHANNEL run). On PID 0x0092 the run
# is 0x23..0x2C with effectsIdx 0..9 (10 blocks for 9 strips), so strips 0..8 -> 0x23..0x2B.
# PROVEN for strip 0: echo and reverb on Mic 1 written at 0x23 were heard live by the unit's owner
# on 7th October 2026. Strips 1..8 follow the same rule and have not been heard yet.
LAYOUT_FX_WRITES = {
    "echoOn": ("bool", None, None), "reverbOn": ("bool", None, None),
    **{p: ("float", 0.0, 1.0) for p in ("echoMix", "echoLowCut", "echoHighCut", "echoDelay",
                                        "echoDecay", "reverbMix", "reverbModel", "reverbLowCut",
                                        "reverbHighCut")},
}


def layout_fx_strip_count(dev: DeviceModel) -> int:
    """How many EFFECTS_PARAMETERS blocks a layout desk may write: one per CHANNEL strip, and never
    more than the run holds."""
    lay = dev.layout
    return 0 if lay is None else min(lay.count("CHANNEL"), lay.count("EFFECTS_PARAMETERS"))


def layout_fx_write_ok(dev: DeviceModel, path, name: str, value, kind: str) -> Optional[str]:
    """None if this one effects write is allowed on a layout desk, else the reason it is not."""
    if "fx_write" not in dev.caps or dev.layout is None or dev.fx_sel_base is None:
        return f"Effects writes are not enabled on the {dev.name}."
    spec = LAYOUT_FX_WRITES.get(name)
    if spec is None:
        return f"{name} is not one of the effects settings enabled on the {dev.name}."
    path = list(path or ())
    if len(path) != 1:
        return f"{name} must address one EFFECTS_PARAMETERS block on the {dev.name}."
    strip = int(path[0]) - dev.fx_sel_base
    if not 0 <= strip < layout_fx_strip_count(dev):
        return f"Root index 0x{int(path[0]):02X} is not a strip's effects block on the {dev.name}."
    want_kind, lo, hi = spec
    if kind != want_kind:
        return f"{name} must be written as {want_kind} on the {dev.name}."
    if lo is not None and not (isinstance(value, float) and lo <= value <= hi):
        return f"{name} {value} is outside {lo} to {hi}."
    return None


# THE FADER LAYOUT WRITE A LAYOUT DESK TAKES: channelInputSource (int) on CHANNEL[strip] of the MAIN
# run, one-level address 01 01 01 01 <CHANNEL base + strip> - the form the Pro II's own fader writes
# have (rcp_transfer.set_fader_source), with this desk's CHANNEL base (0x1A on PID 0x0092, strips
# 0x1A..0x22). The value may only be a code this unit has shown to mean what the Pro II means by it
# (0 Mic 1, 1 Mic 2, 6 the Mic 3 + 4 pair, 7 USB 1, 8 Chat, 9 USB 2, confirmed by the unit's owner
# against its read layout [0, 1, 6, 7, 9, 8, -1, 11, 10] on 7th October 2026), -1 for an empty
# strip, or 10 / 11, which are NOT confirmed as Bluetooth / SMART pads but are the values the unit
# itself holds on faders 9 and 8, so writing them only ever puts back what was there. Mic 3 and
# Mic 4 on their own (2, 3), the other pair codes (4, 5) and every source past the first run stay
# refused.
LAYOUT_FADER_PROP = "channelInputSource"
LAYOUT_FADER_CODES = frozenset({-1, 0, 1, 6, 7, 8, 9, 10, 11})
# Strips the fader write may address: the main CHANNEL run, at most nine.
LAYOUT_FADER_MAX_STRIPS = 9
# The Mic 3 + 4 pair. On PID 0x0092 it carries a line input with live audio, so it is never
# moved off its fader by anything but the user choosing that fader: no reset, no tidy, no gap-fill.
LAYOUT_PINNED_CODE = 6


def layout_fader_write_ok(dev: DeviceModel, path, name: str, value, kind: str) -> Optional[str]:
    """None if this one fader-source write is allowed on a layout desk, else the reason it is not."""
    if "fader_write" not in dev.caps or dev.layout is None or dev.proc_sel_base is None:
        return f"Changing the fader layout is not enabled on the {dev.name}."
    if name != LAYOUT_FADER_PROP:
        return f"{name} is not the fader layout setting on the {dev.name}."
    path = list(path or ())
    if len(path) != 1:
        return f"{name} must address one CHANNEL block on the {dev.name}."
    strip = int(path[0]) - dev.layout.base("CHANNEL")
    if not 0 <= strip < min(dev.layout.count("CHANNEL"), LAYOUT_FADER_MAX_STRIPS):
        return f"Root index 0x{int(path[0]):02X} is not a fader the {dev.name} may write."
    if kind != "int" or isinstance(value, bool):
        return f"{name} must be written as int on the {dev.name}."
    if int(value) not in LAYOUT_FADER_CODES:
        return (f"Channel code {value} has not been confirmed on the {dev.name}, so it is not "
                f"put on a fader there.")
    return None


# THE PER-STRIP CHANNEL WRITES A LAYOUT DESK TAKES (cap "strip_write"): what the Processing,
# Equaliser, Dynamics, Tone and pan tabs, and the mute / solo switches, write on the Pro II - on
# CHANNEL[strip] of the MAIN run, one-level address 01 01 01 01 <CHANNEL base + strip> (0x1A..0x22 on
# PID 0x0092), the same shape the Pro II sends at SEL_CHANNEL_PROC + strip. The address family is
# proven on this unit by its own pushes: fader 1's mute arrives as channelOutputMute on 0x1A and its
# cue as channelCueEnable on 0x1A (7th October 2026), and its fader-source writes land on the same
# nodes. Floats are exactly the controls the Channels page offers (server.PROCESSING_CONTROLS), all
# normalised 0..1 as the Pro II code clamps them; bools are the module switches
# (channels.PROCESSING_MODULE_ON), processing on/off (channelBypassProcessing), simple/advanced
# (channelAdvancedProcessing), mute and solo.
# NOT: channelCurrentFxPreset (the preset NAME marker - presets stay refused), aphexBBTune /
# aphexAETune (only a preset writes them), channelWirelessMute (no transmitter to confirm it), and
# every other CHANNEL property (listen source, talkback, the HPF slope flags).
# channelPanL / channelPanR came with 2.62's stereo pan (two leg sliders on a stereo channel). Both
# sit as floats 0..1 on every main-run CHANNEL block of PID 0x0092 (read off its dump: 0.0 and 1.0,
# hard left and hard right), so they take the same checked, confirmed path as channelPan.
LAYOUT_STRIP_FLOATS = frozenset({
    "aphexAEMix", "aphexBBDrive", "channelDepth", "channelPan", "channelPanL", "channelPanR",
    "channelPunch", "channelSparkle",
    "compressorAttack", "compressorGain", "compressorRatio", "compressorRelease",
    "compressorThreshold", "deesserAttack", "deesserFrequency", "deesserGain", "deesserRatio",
    "deesserRelease", "deesserThreshold", "eqHighBell", "eqHighGain", "eqHighQ", "eqLowGain",
    "eqLowQ", "eqLowShelf", "eqMidBell", "eqMidGain", "eqMidQ", "hpfFrequency", "hpfSlope",
    "noiseGateAttack", "noiseGateHold", "noiseGateHysteresis", "noiseGateRange",
    "noiseGateRelease", "noiseGateThreshold",
})
LAYOUT_STRIP_BOOLS = frozenset({
    "hpfOn", "eqOn", "compressorOn", "noiseGateOn", "deesserOn", "aphexOn", "channelPanOn",
    "eqHighOn", "eqMidOn", "eqLowOn",
    "channelBypassProcessing", "channelAdvancedProcessing", "channelOutputMute", "channelCueEnable",
})
LAYOUT_STRIP_WRITES = {**{p: ("float", 0.0, 1.0) for p in LAYOUT_STRIP_FLOATS},
                       **{p: ("bool", None, None) for p in LAYOUT_STRIP_BOOLS}}


def layout_strip_count(dev: DeviceModel) -> int:
    """How many CHANNEL strips a layout desk may write: the main run, at most nine."""
    lay = dev.layout
    return 0 if lay is None else min(lay.count("CHANNEL"), LAYOUT_FADER_MAX_STRIPS)


def layout_strip_write_ok(dev: DeviceModel, path, name: str, value, kind: str) -> Optional[str]:
    """None if this one per-strip channel write is allowed on a layout desk, else why not."""
    if "strip_write" not in dev.caps or dev.layout is None or dev.proc_sel_base is None:
        return f"Channel processing, mute and solo are not enabled on the {dev.name}."
    spec = LAYOUT_STRIP_WRITES.get(name)
    if spec is None:
        return f"{name} is not one of the channel settings enabled on the {dev.name}."
    path = list(path or ())
    if len(path) != 1:
        return f"{name} must address one CHANNEL block on the {dev.name}."
    strip = int(path[0]) - dev.layout.base("CHANNEL")
    if not 0 <= strip < layout_strip_count(dev):
        return f"Root index 0x{int(path[0]):02X} is not a fader strip the {dev.name} may write."
    want_kind, lo, hi = spec
    if kind != want_kind:
        return f"{name} must be written as {want_kind} on the {dev.name}."
    if lo is not None and not (isinstance(value, float) and lo <= value <= hi):
        return f"{name} {value} is outside {lo} to {hi}."
    return None


# THE ROUTING WRITES A LAYOUT DESK TAKES (cap "routing_write"): exactly the record forms the Pro II
# routing code sends (rcp_transfer.set_cross_point_excluded / set_cross_point_level /
# build_mix_link_request / set_output_routing), each on one node of a MAIN run, one-level address:
#   * on a cross-point, MIX[13 * source + output] of the main run (0x37..0xD2 on PID 0x0092, 12 rows
#     of 13; fader 1's own pushes land on 0x37..0x43): mixDisabled and mixMute (bool), the level
#     mixLevelWithAnchor as the Pro II writes it ("<level>|0", level 0..1 in its minimal-decimal
#     form) and mixLinkRequest / mixUnlinkRequest with the constant composite (two True bools);
#   * on an output, outputMixMinus (int 0 Main Mix, 1 Mix Minus, 2 Custom) on the main MIXMINUSES
#     run only (0x2D..0x36: outputs 0..9). The three later modes (0xFC run) are read, never written.
# THAT THESE FORMS DO ON THIS UNIT WHAT THEY DO ON A PRO II IS A HYPOTHESIS until heard: they are the
# Pro II's, and every write is confirmed by a fresh read (layout_routing), so one that the desk does
# not take is reported as not done. The later MIX runs (0xFF, 0x17D), mix presets and "reset all
# output mixes" stay refused. mixMute is in the set because the cell carries it as a plain bool next
# to mixDisabled, but no page writes it; the desk itself sets it when a channel is muted.
LAYOUT_ROUTING_CELL_WRITES = {
    "mixDisabled": "bool",
    "mixMute": "bool",
    "mixLevelWithAnchor": "str",
    "mixLinkRequest": "pair",
    "mixUnlinkRequest": "pair",
}
LAYOUT_ROUTING_MODE_PROP = "outputMixMinus"
LAYOUT_ROUTING_MODES = frozenset({0, 1, 2})
# The request payload as the parser reads the constant composite 01 07 08 01 01 02 01 01 02.
LAYOUT_ROUTING_REQUEST_VALUE = [(True, "bool"), (True, "bool")]


def layout_level_text(level: float) -> str:
    """The Pro II's minimal-decimal form of a 0..1 cross-point level ('0', '0.5', '0.89', '1'),
    the one form a layout desk's level write may carry (rcp_transfer._level_str)."""
    s = f"{max(0.0, min(1.0, float(level))):.2f}".rstrip("0").rstrip(".")
    return s if s else "0"


def layout_routing_write_ok(dev: DeviceModel, path, name: str, value, kind: str) -> Optional[str]:
    """None if this one routing write is allowed on a layout desk, else the reason it is not."""
    lay = dev.layout
    if "routing_write" not in dev.caps or lay is None or dev.matrix.base is None:
        return f"Changing output mixes is not enabled on the {dev.name}."
    path = list(path or ())
    if len(path) != 1:
        return f"{name} must address one block on the {dev.name}."
    if name == LAYOUT_ROUTING_MODE_PROP:
        out = lay.run("MIXMINUSES").index_of(path[0]) if lay.run("MIXMINUSES") else None
        if out is None:
            return (f"Root index 0x{int(path[0]):02X} is not an output mode the {dev.name} may "
                    f"write.")
        if kind != "int" or isinstance(value, bool) or int(value) not in LAYOUT_ROUTING_MODES:
            return f"{name} must be 0, 1 or 2, written as int, on the {dev.name}."
        return None
    want = LAYOUT_ROUTING_CELL_WRITES.get(name)
    if want is None:
        return f"{name} is not one of the routing settings enabled on the {dev.name}."
    mix = lay.run("MIX")
    if mix is None or mix.index_of(path[0]) is None:
        return f"Root index 0x{int(path[0]):02X} is not a cross-point the {dev.name} may write."
    if kind != want:
        return f"{name} must be written as {want} on the {dev.name}."
    if want == "pair" and value != LAYOUT_ROUTING_REQUEST_VALUE:
        return f"{name} carries only the constant request on the {dev.name}."
    if want == "str":
        level, sep, anchor = str(value).partition("|")
        try:
            ok = sep == "|" and anchor == "0" and layout_level_text(float(level)) == level \
                and 0.0 <= float(level) <= 1.0
        except ValueError:
            ok = False
        if not ok:
            return f"{name} {value!r} is not a level from 0 to 1 in the form the {dev.name} takes."
    return None


# THE HEADPHONE TYPE A LAYOUT DESK TAKES (cap "headphone_write"): headphoneType (int) on one block of
# the MAIN HEADPHONE run, one-level address 01 01 01 01 <index> - the Pro II record form
# (rcp_transfer.set_headphone_type) at this desk's own blocks (0x07..0x0A on PID 0x0092, headphones
# 1..4). The values are the Pro II's amp types, ALL MEASURED on PID 0x0092 on 7th October 2026 by a
# dump before and after a change in RODE's own app on headphone 1 (0x07): "RODE NTH-100" = 0
# (the factory value on all four), "Low Sensitivity" 0 -> 2, "High Sensitivity" 2 -> 1. Nothing
# else in the tree moved but knob positions. headphoneColour, the block's other property, is never
# written.
LAYOUT_HEADPHONE_PROP = "headphoneType"
LAYOUT_HEADPHONE_TYPES = frozenset({0, 1, 2})
LAYOUT_HEADPHONE_COUNT = 4
# WHICH CHANGES MAKE A HEADPHONE LOUDER on this desk, by ear (the unit's owner, 7th October 2026):
# 1 "High sensitivity" is clearly the SOFTEST; 0 NTH-100 and 2 Low sensitivity are about equally
# loud. RODE's app asks before 1 -> 2 ("this will increase the headphone output", turn them down
# first) and not before 0 -> 2. So (from, to) pairs leaving 1 are warned about before writing.
LAYOUT_HEADPHONE_LOUDER = frozenset({(1, 0), (1, 2)})
LAYOUT_HEADPHONE_LOUDER_WARNING = ("This makes {output} louder. Turn your headphones down first, "
                                   "then apply.")
LAYOUT_HEADPHONE_NOTE = ("On this unit High sensitivity is the quietest setting; NTH-100 and Low "
                         "sensitivity are about equally loud, and louder than it. Moving away from "
                         "High sensitivity asks first, so you can turn your headphones down.")

# WHAT THIS COMPUTER RECEIVES OVER USB ON A LAYOUT DESK (cap "usbmode_write"): outputMultiMode (int)
# on the OUTPUT block (0x0B on PID 0x0092), one-level address. ONLY the two values measured there on
# 7th October 2026, each with the USB product id the desk comes back under: RODE's "USB 1 Input:
# Standard" (USB 1 Output Pre-Fader) = 1 -> PID 0x0092, "Expanded" = 4 -> PID 0x0094. The Pro II
# helper's 0 (Off) and 2 (Post-Fader) are NOT measured on this desk and stay refused, as does
# everything else. Switching RE-ENUMERATES THE WHOLE DESK on USB, HID included, and Expanded hands
# the "Main Multitrack" playback endpoint to the RODE Virtual Device Driver (other apps get 'device
# in use', 0x8889000A) and makes Windows pick "System (RODECaster)" as its default output - every
# path that offers it must say so first (LAYOUT_USBMODE_WARNING).
LAYOUT_USBMODE_PROP = "outputMultiMode"
LAYOUT_USBMODE_PIDS = {1: PID_LAYOUT_0092, 4: PID_LAYOUT_0094}
LAYOUT_USBMODE_LABEL = {
    1: "Standard: every channel, pre-fader",
    4: "Expanded: for the RODE Virtual Device Driver",
}
LAYOUT_USBMODE_WARNING = (
    "Expanded hands the unit's Main Multitrack playback device to the RODE Virtual Device Driver: "
    "other programs can no longer play to it (Windows reports it as in use), and Windows makes "
    "System (RODECaster) its default output. Either way the unit disconnects from USB and comes "
    "back under another product id, so every RODECaster sound device drops out while it does.")


def layout_headphone_write_ok(dev: DeviceModel, path, name: str, value, kind: str) -> Optional[str]:
    """None if this one headphone-type write is allowed on a layout desk, else the reason it is not."""
    lay = dev.layout
    if "headphone_write" not in dev.caps or lay is None:
        return f"Changing the headphone type is not enabled on the {dev.name}."
    if name != LAYOUT_HEADPHONE_PROP:
        return f"{name} is not the headphone type setting on the {dev.name}."
    path = list(path or ())
    if len(path) != 1:
        return f"{name} must address one HEADPHONE block on the {dev.name}."
    if int(path[0]) not in {sel for sel, _lab in layout_headphone_outputs(lay)}:
        return f"Root index 0x{int(path[0]):02X} is not a headphone output the {dev.name} may write."
    if kind != "int" or isinstance(value, bool) or int(value) not in LAYOUT_HEADPHONE_TYPES:
        return f"{name} must be 0, 1 or 2, written as int, on the {dev.name}."
    return None


def layout_usbmode_write_ok(dev: DeviceModel, path, name: str, value, kind: str) -> Optional[str]:
    """None if this one USB-mode write is allowed on a layout desk, else the reason it is not."""
    lay = dev.layout
    if "usbmode_write" not in dev.caps or lay is None or lay.single("OUTPUT") is None:
        return f"Changing what this computer receives over USB is not enabled on the {dev.name}."
    if name != LAYOUT_USBMODE_PROP:
        return f"{name} is not the USB mode setting on the {dev.name}."
    path = list(path or ())
    if len(path) != 1 or int(path[0]) != lay.single("OUTPUT"):
        return f"{name} must address the OUTPUT block on the {dev.name}."
    if kind != "int" or isinstance(value, bool) or int(value) not in LAYOUT_USBMODE_PIDS:
        return (f"{name} {value!r} has not been measured on the {dev.name}: only 1 (Standard) and "
                f"4 (Expanded), written as int, are sent there.")
    return None


# ---- THE GENERALISED GATE (cap "tree_write") -----------------------------------------------------
# Every property name in this desk's tree is the SAME name the Pro II uses, in the same record
# format; only the root index of each block differs, and the layout reads that off the desk's own
# dump. So a write is safe exactly when:
#   (a) the property NAME already sits on that very block in the most recent dump of this desk (the
#       real danger is a name the desk does not publish there: writing it appends a stray that only
#       a factory reset clears),
#   (b) the value's kind (bool / int / float / str) is the kind the dump has for it there, and
#   (c) the address is a block the layout located BY NAME: one of LAYOUT_TREE_BLOCKS, in the FIRST
#       run of that name (never a later duplicate such as CHANNEL 0xFA or MIX 0xFF), one level deep.
# On top of that, the writers check the same thing against a FRESH dump before sending and read the
# value back after (layout_tree.write_records). Names in a narrow family above (input, effects,
# fader source, strip, routing, headphone type, USB mode) are judged by that family only.
#
# THE BLOCKS: those the Pro II UI writes settings to. NOT: NETWORK (wifi, Bluetooth pairing: commands
# and credentials), RECORDER / PLAYER / SHOWCONTROL / RECORDINGS (transport and show commands),
# SOUNDPADS and its effects (the pad create/edit choreography needs transfer mode and a pad grid not
# measured here), PHYSICALINTERFACE (fader levels: which faders are virtual is not measured here),
# AUDIO (engine parameters - sample rate, buffer - no page writes them), SIP / RC Sync / StreamerX.
LAYOUT_TREE_BLOCKS = frozenset({"GUI", "HEADPHONE", "OUTPUT", "MASTERCHANNEL", "SYSTEM", "CHANNEL",
                                "EFFECTS_PARAMETERS", "MIXMINUSES", "MIX", "INPUTSOURCE"})
# THE DENY LIST: commands rather than settings, status the desk reports, identities, and settings
# with unmeasured hardware side effects. Matched against the WORDS of the camelCase name
# ("updateResetDeviceRequested" -> update, reset, device, requested), never as raw substrings, so a
# setting that merely contains the letters is not caught ("channelSparkle" is not "channels",
# "channelCurrentFxPreset" is not "reset"). Refused whatever the dump says.
LAYOUT_TREE_DENY = frozenset({
    # commands / triggers
    "request", "requested", "requests", "initiate", "reset", "erase", "format", "formatted",
    "delete", "update", "reboot", "factory", "power", "remount", "transfer", "eject", "cancel",
    "download", "downloaded", "do", "edit", "clear", "import", "export", "extend", "disconnect",
    # network, Bluetooth pairing, calls, cloud, radio
    "wifi", "bt", "sip", "call", "calling", "scan", "pair", "paired", "unpair", "connect",
    "connected", "icecast", "radio", "cell", "oauth",
    # the recorder, shows, storage, the emergency mute, the pad recorder
    "show", "record", "state", "storage", "emergency", "volume",
    # status and identity the desk reports
    "selected", "serial", "sn", "id", "uid", "firmware", "name", "version", "progress", "available",
    "complete", "internet", "touched", "pressed", "detected", "signal", "rcv", "board", "last",
    # engine and modes with effects nobody has measured
    "rc", "engine", "beta", "host", "unify", "presenter", "share", "latency", "rate", "buffer",
    "channels",
})
# Shapes the words cannot say: status flags that END in "Active" (outputMonAutoMuteActive reports
# whether the auto-mute is engaged right now; "activeButtonsBrightness" is a setting and passes),
# and binding indexes that end in "Idx" (effectsIdx, padIdx tie a block to a strip or a pad).
_LAYOUT_TREE_DENY_RE = re.compile(r"Active$|Idx$|SN$")
# Whole names refused: inputType is DERIVED by the desk from the mic type (channels.CHANNEL_PROP),
# and writing it alone makes a state the desk's own screen cannot make; systemMainMode switches the
# desk's whole operating mode, which nothing here has measured.
LAYOUT_TREE_DENY_NAMES = frozenset({"inputType", "systemMainMode"})
_WORDS = re.compile(r"[a-z]+|[A-Z][a-z]*|[0-9]+")
# The Pro II caps the generalised gate stands in for on a layout desk (has_write_cap).
LAYOUT_TREE_CAPS = frozenset({"output_write", "unit_settings"})


def layout_tree_denied(name: str) -> bool:
    """True when `name` is a command or an unmeasured side effect (LAYOUT_TREE_DENY)."""
    name = str(name)
    words = {w.lower() for w in _WORDS.findall(name)}
    return (name in LAYOUT_TREE_DENY_NAMES or bool(words & LAYOUT_TREE_DENY)
            or bool(_LAYOUT_TREE_DENY_RE.search(name)))


def layout_tree_block(dev: DeviceModel, path) -> Optional[tuple]:
    """(block name, {property: kind}) for a path the generalised gate may address, or None: a
    one-level path to a root child in the FIRST run of a LAYOUT_TREE_BLOCKS name."""
    lay = dev.layout
    path = list(path or ())
    if lay is None or len(path) != 1:
        return None
    run = lay.main_run_of(path[0])
    if run is None or run.name not in LAYOUT_TREE_BLOCKS:
        return None
    hit = lay.block_at(path)
    return hit if hit is not None and hit[0] == run.name else None


def layout_tree_write_ok(dev: DeviceModel, path, name: str, value, kind: str) -> Optional[str]:
    """None if this one write passes the generalised gate on a layout desk, else why not."""
    if "tree_write" not in dev.caps or dev.layout is None:
        return f"{name} cannot be changed on the {dev.name}."
    if layout_tree_denied(name):
        return (f"{name} is a command or a setting with effects nobody has measured on the "
                f"{dev.name}, so AccessCaster does not send it there.")
    path = list(path or ())
    hit = layout_tree_block(dev, path)
    if hit is None:
        where = "/".join(f"0x{int(p):02X}" for p in path) or "nothing"
        return (f"{name} at {where} is not one of the {dev.name}'s own setting blocks, so it is "
                f"not written.")
    block, props = hit
    if name not in props:
        return (f"The {dev.name} does not publish {name} on its {block} block, so it is not "
                f"written (that would add a stray setting).")
    if kind != props[name] or kind not in ("bool", "int", "float", "str"):
        return (f"{name} is {props[name]} on the {dev.name}'s {block} block, not {kind}, so it is "
                f"not written.")
    return None


def has_write_cap(dev: DeviceModel, cap: str) -> bool:
    """True when the desk may take the writes `cap` names: it holds the cap itself, or it is a
    layout desk whose generalised gate ("tree_write") stands in for it (LAYOUT_TREE_CAPS). On the
    Pro II and the Duo this is exactly `cap in dev.caps`."""
    return cap in dev.caps or (dev.layout_required and "tree_write" in dev.caps
                               and cap in LAYOUT_TREE_CAPS)


def layout_block_selector(dev: DeviceModel, block: str, instance: int = 0) -> int:
    """The root index of block `block` (instance `instance` of its main run) on a layout desk, read
    off its own dump: what replaces a Pro II selector constant there. Raises WriteRefused when the
    desk has no such block."""
    lay = dev.layout
    run = None
    if lay is not None:
        run = lay.run(block) or next((r for r in lay.all_runs if r.name == block), None)
    if run is None or not 0 <= int(instance) < run.count:
        raise WriteRefused(f"The {dev.name} has no {block} block"
                           f"{'' if not instance else f' {int(instance) + 1}'} in its layout, so "
                           f"that setting is not written.")
    return run.base + int(instance)


def layout_write_ok(dev: DeviceModel, path, name: str, value, kind: str) -> Optional[str]:
    """None if this one write is allowed on a layout desk: an input, effects, fader-source,
    per-strip channel, routing, headphone-type or USB-mode write (each by its own narrow family -
    the sets are disjoint), or any other name through the generalised gate (layout_tree_write_ok)."""
    if name == LAYOUT_HEADPHONE_PROP:
        return layout_headphone_write_ok(dev, path, name, value, kind)
    if name == LAYOUT_USBMODE_PROP:
        return layout_usbmode_write_ok(dev, path, name, value, kind)
    if name == LAYOUT_FADER_PROP:
        return layout_fader_write_ok(dev, path, name, value, kind)
    if name in LAYOUT_ROUTING_CELL_WRITES or name == LAYOUT_ROUTING_MODE_PROP:
        return layout_routing_write_ok(dev, path, name, value, kind)
    if name in LAYOUT_FX_WRITES:
        return layout_fx_write_ok(dev, path, name, value, kind)
    if name in LAYOUT_STRIP_WRITES:
        return layout_strip_write_ok(dev, path, name, value, kind)
    if name in LAYOUT_INPUT_WRITES:
        return layout_input_write_ok(dev, path, name, value, kind)
    return layout_tree_write_ok(dev, path, name, value, kind)


def refuse_unproven_pid(product_id, record: Optional[bytes] = None) -> None:
    """The send layer's last check, given the WHOLE record about to go out.

    The static desks in DEVICES take anything (their helpers carry their own gates). A layout desk
    takes ONLY a record that parses as one of LAYOUT_INPUT_WRITES at an allowed INPUTSOURCE address
    (layout_input_write_ok), one of LAYOUT_FX_WRITES at an allowed EFFECTS_PARAMETERS address
    (layout_fx_write_ok), a channelInputSource write of an allowed code on a main-run CHANNEL
    strip (layout_fader_write_ok), one of the Pro II routing record forms on a main-run MIX
    cross-point or MIXMINUSES output (layout_routing_write_ok), a headphoneType 0..2 on a HEADPHONE
    block (layout_headphone_write_ok), an outputMultiMode 1 or 4 on OUTPUT
    (layout_usbmode_write_ok), or any other single property that passes the generalised gate
    (layout_tree_write_ok: a setting block found by name, the name already on it in the dump, the
    same kind, not a command). Everything else is refused before a handle is opened."""
    if product_id in DEVICES:
        return
    dev = resolve_device(product_id)
    reason = dev.write_block or f"Writing to PID 0x{int(product_id):04X} is refused."
    if record is not None and product_id in LAYOUT_PIDS:
        import model_tree             # local: devices keeps its module imports to rcp_hid and layout
        try:
            n = int.from_bytes(record[1:5], "little")
            if record[:1] != b"\x03" or not 0 < n <= len(record) - 5 or any(record[5 + n:]):
                raise ValueError("not one report-3 record")
            path, name, value, kind, end = model_tree.parse_record(record[5:5 + n])
            if end != n:
                raise ValueError("trailing bytes in the record")
        except (ValueError, IndexError):
            raise WriteRefused(reason)
        why = layout_write_ok(dev, path, name, value, kind)
        if why is None:
            return
        reason = why
    raise WriteRefused(reason)


def device_from_system_name(system_name) -> Optional[DeviceModel]:
    """The DeviceModel a DUMP names in its own `systemName`, or None if it names neither desk.

    A SAVED DUMP MUST NOT BE READ AS THE CONNECTED DESK. `property_list.py
    --blob` can be handed a Pro II capture while a Duo is plugged in, and resolving the model from
    the USB interface would then write the DUO's file with PRO II data - precisely the overwrite the
    device-scoped filenames exist to prevent. The desk states its own model in the dump
    ("RODECaster Pro II" / "RODECaster Duo"), so a saved capture carries its own provenance and does
    not need the hardware present.
    """
    if not isinstance(system_name, str) or not system_name:
        return None
    s = system_name.lower()
    if "duo" in s:
        return DUO
    if "pro" in s:
        return PRO2
    return None


# What a dump told us this desk is, keyed by its USB SERIAL.
#
# THE SERIAL IS THE ONLY STABLE IDENTIFIER THIS DESK HAS. The product id encodes the audio
# configuration and changes under `outputMultiMode`, the path changes with it, and the MI_09
# product string is the useless "HID Interface" on both desks. The serial survives every one of
# those, so a desk seen once in any mode is known in all of them.
#
# LEARNED RATHER THAN TABULATED, because a table of every mode's id per desk is six entries each,
# unmeasured for the Duo, and would be wrong again the day RODE add a mode. The desk states its own
# model in every dump; this just remembers it.
_LEARNED: dict = {}


def unidentified(iface) -> bool:
    """Whether this interface's product id names no desk and nothing has been learned for it.

    The caller's cue to pay for reading `systemName` out of a dump it already has. Cheap enough to
    ask on every dump, which is the point: the expensive half runs once per desk per session."""
    if iface is None or _FORCED_DEVICE is not None:
        return False
    return (iface.product_id not in DEVICES
            and bool(iface.serial_number)
            and iface.serial_number not in _LEARNED)


def note_system_name(iface, system_name) -> Optional[DeviceModel]:
    """Record what a dump says this desk is, against its serial. Returns the model, or None.

    Silently does nothing for a name that matches neither desk, so an unreadable dump cannot teach
    us something false and have it believed for the rest of the session."""
    if iface is None or not iface.serial_number:
        return None
    model = device_from_system_name(system_name)
    if model is not None:
        _LEARNED[iface.serial_number] = model
    return model


_FORCED_DEVICE: Optional[DeviceModel] = None


def force_device(dev: Optional[DeviceModel]) -> Optional[DeviceModel]:
    """Pin the active device. FOR OFFLINE TESTS ONLY; returns the previous pin.

    An offline test that calls get_active_device() gets whatever desk is plugged into the machine it
    runs on, so without a pin the same test passes beside one desk and fails beside the other, which
    reads exactly like a hardware finding.

    Pass None to go back to resolving from the connected interface. Never call this from app code -
    the whole point of the device model is that the desk in front of you decides.
    """
    global _FORCED_DEVICE
    prev = _FORCED_DEVICE
    _FORCED_DEVICE = dev
    return prev


def get_active_device() -> DeviceModel:
    """The DeviceModel for the currently connected RODECaster control interface.

    Resolves from the vendor control interface's product id, then from what a dump has told us
    about this SERIAL. Falls back to DEFAULT (Pro II) only if nothing is connected or enumeration
    fails, so callers never crash and Pro II-era behaviour is preserved when offline.

    **THE PRODUCT ID ALONE IS ONLY RIGHT IN THE DEFAULT AUDIO MODE.** It encodes the USB audio
    configuration, so a desk in any other `outputMultiMode` presents an id no table can hold, and
    would silently resolve to DEFAULT - which is a Pro II, so a Duo in Expanded would be handed a
    Pro II's matrix shape and channel layout. The learned map fixes that for a DUO: it is keyed by
    serial, which does not change, and is filled in from the desk's own `systemName` the first time
    a dump is read. A known id still wins, because it is definitive and needs no dump.

    A LAYOUT desk (LAYOUT_PIDS) is resolved BEFORE the learned map, and a learned "Pro II" is NOT
    believed for any other unknown id: the Pro II at PID 0x0092 / 0x0094 says "RODECaster Pro II" in
    its own systemName, yet its root children sit at other indexes than the Pro II this app was
    built on, so handing it the PRO2 table would point every selector at a different node. Such a
    desk stays a layout desk or a geometry-unknown one (resolve_device), never a Pro II.
    """
    if _FORCED_DEVICE is not None:
        return _FORCED_DEVICE
    try:
        iface = rcp_hid.find_control_interface()
        if iface is not None:
            known = DEVICES.get(iface.product_id)
            if known is not None:
                return known
            if iface.product_id not in LAYOUT_PIDS:
                learned = _LEARNED.get(iface.serial_number)
                if learned is DUO:
                    return learned
            return resolve_device(iface.product_id)
    except Exception:
        pass
    return DEFAULT
