"""AccessCaster - DEVICE MODEL table (Pro II + Duo), keyed by USB product id.

The Pro II and the Duo speak the same protocol (HID transport, report-3 framing, value encodings,
model-dump read) and differ only in data: which inputs exist, how many faders and pads, which
outputs the matrix exposes, and which writes are proven. The protocol logic stays single-copy in
rcp_hid / rcp_transfer / rcp_read / channels, and only that data lives here, one DeviceModel per
desk. A third desk is one more table entry.

This is the DEVICE model, unrelated to profiles.py, which stores user settings snapshots.

This module imports only rcp_hid, so rcp_read / rcp_transfer / channels can import it without a
cycle.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

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
        """THE tree path of a channel's input values: INPUTSOURCE[source_code]. Read and write."""
        return [self.input_table_base + int(ch["source_code"])]

    def channel_for_input_path(self, path) -> Optional[dict]:
        """The UI channel a pushed input address belongs to, or None. The reverse of input_path.

        The announcer needs this: when the DESK moves a gain it pushes the change at the address it
        owns, and the spoken line has to name the channel it belongs to."""
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

# The default when no device is connected / product id is unknown: Pro II, so all
# existing Pro II behaviour is unchanged for offline code paths and unit tests.
DEFAULT = PRO2


def resolve_device(product_id: Optional[int]) -> DeviceModel:
    """Map a USB product id to its DeviceModel; DEFAULT (Pro II) if unknown/None."""
    if product_id is None:
        return DEFAULT
    return DEVICES.get(product_id, DEFAULT)


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
    about this SERIAL. Falls back to DEFAULT (Pro II) if nothing is connected or enumeration
    fails, so callers never crash and Pro II-era behaviour is preserved when offline.

    **THE PRODUCT ID ALONE IS ONLY RIGHT IN THE DEFAULT AUDIO MODE.** It encodes the USB audio
    configuration, so a desk in any other `outputMultiMode` presents an id no table can hold, and
    would silently resolve to DEFAULT - which is a Pro II, so a Duo in Expanded would be handed a
    Pro II's matrix shape and channel layout. The learned map fixes that: it is keyed by serial,
    which does not change, and is filled in from the desk's own `systemName` the first time a dump
    is read. A known id still wins, because it is definitive and needs no dump.
    """
    if _FORCED_DEVICE is not None:
        return _FORCED_DEVICE
    try:
        iface = rcp_hid.find_control_interface()
        if iface is not None:
            known = DEVICES.get(iface.product_id)
            if known is not None:
                return known
            learned = _LEARNED.get(iface.serial_number)
            if learned is not None:
                return learned
    except Exception:
        pass
    return DEFAULT
