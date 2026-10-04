"""AccessCaster - DEVICE MODEL table (Pro II + Duo), keyed by USB product id.

WHY THIS FILE EXISTS: the app was reverse-engineered against the
RODECaster Pro II, with the device-specific numbers (channel order, per-input
selectors, routing-matrix dimensions) hardcoded inside rcp_read.py / channels.py.
The RODECaster Duo speaks the SAME protocol (same HID transport, same report-3
record framing, same value encodings, same model-dump read) but has DIFFERENT
data: 7 real inputs vs 4 mics, 10 strips vs 9, a 13x19 matrix vs the Pro II grid,
and a still-unknown input-mic write format.

So we DO NOT fork the code. The protocol logic stays single-copy in rcp_hid /
rcp_transfer / rcp_read / channels; only the *data* that differs per unit lives
here, in one DeviceModel per product id. Adding a third RODE desk later is one
more table entry, not a second codebase.

NAMING: this is the DEVICE model. It is unrelated to profiles.py, which stores
user SETTINGS SNAPSHOTS (backup/restore).

Everything here is data + a resolver. This module imports only rcp_hid (which
imports only hidapi), so it never creates an import cycle with rcp_read /
rcp_transfer / channels (those import THIS module, not the reverse).
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
    the per-device fader shape lives; before GAP-1 it was Pro II-hardcoded in server.py.
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
                       faders' REFUSES rather than writing a guessed layout. (The Duo's
                       default is not yet confirmed: the fw-1.7.3 baseline dump reads
                       [0,1,10,11,2,7,8,9,-1,9], which carries an empty strip and a
                       duplicated source, so it is not a trustworthy reset target.)
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
        to naming it is the announcer saying "0 percent, Source 12" out loud - which is exactly
        what a linked pair used to do before `code_label` was removed for the same reason."""
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
    """Flexible input-source strips for the older by-NAME routing view (GAP-3).

    This is the pre-GAP-1 representation of fader assignment, consumed by server.py's
    routing_model / read_routing_live / action_set_input_source (the /api/routing/inputsource
    endpoint) and by legacy-profile restore (profiles._apply_legacy). It exposes the
    reassignable strips by a fixed NAME and offers a small source palette to drop onto them.
    The device-aware fader_model (FaderSpec) is the primary, INDEX-based view; this by-name
    view remains only for that endpoint and for restoring pre-full-format profiles.

      strips  : (key, vec_index, label) per reassignable strip. `key` MUST be a key of
                rcp_transfer.CHANNEL_INPUT_SOURCE_SELECTOR (its selector = 0x1C + vec_index);
                `vec_index` is the strip's position in the channelInputSource vector.
      sources : (code, label) source palette. `code` is an rcp_transfer.INPUT_SOURCE_CODE value.

    A device that uses ONLY the index-based fader_model leaves this EMPTY (the default). The Duo
    does: its flexible strips sit at different vector positions and its writes route through the
    index-based set_fader_source, not the by-name set_channel_input_source, and its fader-write
    model is unconfirmed anyway (fader_write cap OFF). With an empty spec the by-name endpoints
    report nothing and refuse, and legacy profiles skip the input_sources subset - all safe.
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
    """Per-output 'headphone type' (amp profile) setting for one device (GAP-6).

    headphoneType is a GLOBAL int property (rcp_transfer.build_global_prop_int). The SELECTOR
    picks WHICH headphone output; the VALUE picks the amp profile. Confirmed selectors:
    0x09 = Headphone 1 (from the original Pro II reversing) and 0x0A = Headphone 2 (captured on
    the Duo fw-1.7.3 output-write pass). Both desks share these selectors byte-for-byte
    (the whole output block is identical), so both expose HP1 + HP2. The write path is gated by the
    "output_write" cap, and 0x09/0x0A are KNOWN selectors, so writing them is not a stray-append hazard.

      outputs : (selector, label) per addressable headphone output, in UI order.
      values  : (int, label) per selectable amp type, in UI order.

    NOT YET CONFIRMED ON HARDWARE (see PARITY.md and Completed.md "Duo support", GAP-6):
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


# --- Fader specs (see FaderSpec). Defined before DeviceModel so it can default to the Pro II
# shape, keeping offline / unknown-device paths on Pro II behaviour. -----------------------------
# capture_len=10, MEASURED: the Pro II's raw channelInputSource is TEN long, not nine -
# live read (0,7,2,3,10,11,1,8,9,9), and every per-strip array is x10 too (channelBypassProcessing,
# channelOutputMute, channelCueEnable, noiseGateOn). Slot 9 duplicates USB 2 (code 9), exactly as on
# the Duo, so the 10-slot strip space with a mirrored last slot is a SHARED firmware shape, not a Duo
# quirk. RODE documents nine faders (six physical + three virtual), so no fader is missing: slot 9 is
# a spare the desk does not expose. We CAPTURE it in profiles (so it round-trips if a future firmware
# ever puts it to use) but deliberately keep vector_len unset so restore still writes only 9 - writing
# slot 9 would show the layout-tidy logic a duplicated source it never meets today.
#
# --- THE ONE CHANNEL TABLE ---------------------------------------------------------------------
# EVERY INPUT THIS DESK CAN CARRY, WRITTEN ONCE. The three lists the app used to keep - the fader
# dropdown, the Channels page and the routing-matrix rows - are now VIEWS over this, derived below.
#
# WHY IT IS ONE TABLE. Those three each spelled out the same source code and the same label, and
# nothing checked them against each other, so adding an input meant remembering three places and
# a rename meant remembering three more. Measured, 29th September 2026: adding RODE's four Virtual
# Devices updated two of the three, which left the routing matrix naming USB 1 after a Windows
# device that no longer exists and offering no rows at all for the four new inputs. That is not a
# mistake anyone can be careful enough to avoid; the structure has to make it impossible.
#
# ADDING AN INPUT IS NOW ONE ENTRY. Which surfaces it appears on is stated per entry, because
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
    carries the same set with labels for the UI, and the two cannot disagree now that both come
    from one table - which the docstring above has always required and nothing used to check."""
    return tuple(c.code for c in sorted(table, key=lambda c: c.code)
                 if c.in_matrix and not c.expanded_only)


def _expanded_matrix_index_view(table) -> tuple:
    return tuple(c.code for c in sorted(table, key=lambda c: c.code)
                 if c.in_matrix and c.expanded_only)


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


# RODE's Virtual Devices: source codes measured on hardware 29th September 2026, each written to a
# fader and read back. Shared by both desks - a code is a position in the firmware's
# InputSourceType enum and both run the same binary, the argument that settles the pair codes too.
# Not independently measured on a Duo; `DevTools/input_sources.py` against one would confirm it.
# The four are functionally identical by RODE's own account, so the ORDER is theirs (guide and
# driver INF) and costs a label if wrong, never an address.
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

PRO2_FADERS = FaderSpec(
    count=9, physical=6, capture_len=10,
    # Virtual faders 7/8/9 (indices 6-8) carry a settable level; faders 1-6 are analog pots. The
    # addresses are DERIVED - see FaderSpec.level_selectors, which returns 0x0a/0x0b/0x0c without
    # anything naming them here.
    # (code, label): codes are channelInputSource values (mics read as 0-3). 10 assignable
    # sources over 9 faders, so one is always off-fader - exactly the Pro II behaviour.
    assignable=_assignable_view(CHANNELS_PRO2),
    # Only ADJACENT inputs link, which is RODE's own restriction, so these three are the whole set.
    # All three codes captured from the RODE app on hardware.
    stereo_pairs=((4, 0, 1, "Mic 1 + 2"), (5, 1, 2, "Mic 2 + 3"), (6, 2, 3, "Mic 3 + 4")),
    # RODE's Virtual Devices, measured on hardware 29th September 2026: each of 12 to 15 was
    # written to a fader and read back, and all four were accepted. They were found by their
    # INPUTSOURCE entries rather than guessed - every usable input carries a non-black
    # `inputColour`, and `inputDigitalGain` -12 marks the family that arrives over USB, which is
    # what separates these four from the reserved slots either side. `DevTools/input_sources.py`
    # prints that table. The ORDER is RODE's own, from their user guide and their driver's INF,
    # and is not independently measured; it costs a label if wrong, never a wrong address, and
    # RODE say the four are functionally identical anyway.
    expanded_sources=_expanded_assignable_view(CHANNELS_PRO2),
    # In Expanded the computer no longer sends one stereo mix, so naming USB 1 after a Windows
    # device that has gone is worse than not naming it at all.
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
# NO TOUCHSCREEN WAS NEEDED FOR ANY OF THAT, which matters because the obvious plan - read the
# Duo's screen, or run RODE's app - is the one a blind Duo owner cannot carry out. The tree plus a
# level write settled it instead.
#
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
    #     has asked for it - a judgement, deliberately, not a hardware limit. The old wording called
    #     it something nobody "would want or RODE would offer", which dressed that judgement up as a
    #     fact about the desk.
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
# Pro II shape, keeping offline / unknown-device paths on Pro II behaviour. --------------------------
# Pro II: EXACTLY the constants that were hardcoded in server.py before GAP-3 (_INPUT_STRIP_VEC_INDEX
# + _INPUT_STRIP_LABEL + _INPUT_SOURCE_LABEL), preserving order and codes byte-for-byte. The five
# reassignable strips sit on faders 5 to 9 (vector indices 4 to 8); mics 1 to 4 (faders 1 to 4) are
# fixed XLR inputs and are not listed. Strip keys are CHANNEL_INPUT_SOURCE_SELECTOR keys; source
# codes are INPUT_SOURCE_CODE values (Bluetooth 10, USB1 7, Chat 8, USB2 9, SMARTpads 11, CallMe1 16).
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

# Duo: EMPTY - the Duo uses the index-based fader_model (FaderSpec) for reassignment, not this
# by-name view. Its flexible strips sit at different vector positions and its fader-write model is
# unconfirmed (fader_write cap OFF), so the by-name endpoints report nothing and refuse rather than
# drive the Pro II-shaped set_channel_input_source path. See InputStripSpec.
DUO_INPUT_STRIPS = InputStripSpec()


@dataclass(frozen=True)
class DeviceModel:
    product_id: int
    name: str
    # Input-channel strip order in the model-dump stream (rcp_read.CHANNELS for Pro II).
    channels: tuple
    # "channel" = channel-address format (01 01 01 02 <sel> <trailer>); "unknown" = TBD.
    mic_input_format: str
    matrix: MatrixSpec
    # Capability flags gate features that are decoded+verified for THIS device:
    #   "input_mic_write" - gain/phantom/type/polarity writes are known
    #   "matrix_read"     - the routing-matrix read formula is verified
    #   "matrix_write"    - the matrix cross-point write selector is known
    #   "processing"      - channel-processing selectors are verified (all strips)
    #   "channel_read"    - the per-channel read mapping (array indices) is verified
    #   "output_write"    - the master/global output-block write selectors are verified:
    #                       multitrack (0x0D), monitor/BT auto-mute (0x0D), headphone type
    #                       (0x09), master Compellor + output delay (0x0E). These are GLOBAL
    #                       named-property writes; an UNMAPPED selector on a device would
    #                       silently append a stray, so a desk without this cap keeps them
    #                       read-only rather than writing Pro II-decoded selectors blindly.
    #   "pad_write"       - the SMART-pad create/clear/edit choreography (region-B pool GC,
    #                       effectsIdx==padIdx binding, handle=pad-count) is VERIFIED on this
    #                       device. Pad READS are always safe and need no cap; only device
    #                       WRITES gate on this. Pro II has it; the Duo does NOT yet (its pad
    #                       model is byte-identical to the Pro II per duo_pad_probe.py, but the
    #                       three firmware behaviours the pool safety rests on still need a
    #                       reversible live create/clear test, verified by read-back), so Duo pads
    #                       are read-only here for now.
    #   "fader_write"     - the fader-assignment WRITE path (set a channel on a fader, reset to the
    #                       factory layout) is verified for THIS device. Fader READS/labels are
    #                       always safe (device-aware, no cap). Writes gate on this because a device
    #                       whose fader model is only partly known (the Duo: its 10-entry vector
    #                       carries EMPTY strips encoded as -1 and its clean DEFAULT layout is not
    #                       confirmed) could otherwise write an unconfirmed value (e.g. -1 to vacate
    #                       a strip) or a guessed default. Pro II has it; the Duo does NOT yet (needs
    #                       a reversible live set/reset test + a confirmed default vector), so Duo
    #                       fader writes safely refuse rather than mis-write.
    #   "unit_settings"   - the desk's own display/haptics/REC-button/listen settings can be WRITTEN
    #                       (server.UNIT_SETTINGS, rcp_transfer.UNIT_SETTING_SEL). All ten selectors
    #                       are captured and read-back verified on the Pro II, and none
    #                       has ever been seen on a Duo, so a desk without this cap READS them (the
    #                       dump is always safe) and refuses to write. Same capture-wider-than-write
    #                       rule the fader levels use.
    caps: frozenset
    # Per-channel PROCESSING and EFFECTS selector BASES (global form 01 01 01 01 <sel>).
    # A channel's processing selector = proc_sel_base + strip_index; its effects selector =
    # fx_sel_base + strip_index. Same on both desks (Pro II + Duo). Both confirmed on the Duo
    # on a non-Mic1 strip: proc across six strips (gaps prove strip-indexing) and fx on Mic2
    # (strip 1 -> 0x27, from a capture). NOTE: set_channel_effect still keys fx off
    # (channel-1); that equals strip_index in the default layout but not after a fader reassign.
    proc_sel_base: int = 0x1C
    fx_sel_base: int = 0x26
    # Root-child index of INPUTSOURCE[0]: a 30-entry table, indexed by SOURCE CODE, that both desks
    # publish and that holds EVERY input value for EVERY channel. 0x26E on each, read off a live dump
    # of each desk rather than assumed shared. See input_path.
    input_table_base: int = 0x26E
    # Per-device READ MAP: the UI channels this desk exposes. Each entry is a dict:
    #   name, label, kind ("mic"|"line"|"bus"),
    #   proc_idx    - fixed index into the per-strip processing/effects arrays, or None to
    #                 resolve it dynamically at read time as channelInputSource.index(source_code),
    #   source_code - the channelInputSource code. THE ONE PER-CHANNEL NUMBER: it drives the
    #                 dynamic proc_idx lookup, and it indexes the INPUTSOURCE table, which is both
    #                 where every input value is READ and where every input value is WRITTEN (see
    #                 input_path). There is deliberately NO second "input_slot" field for the
    #                 input-array position: it would hold the same value as source_code in every
    #                 entry on both desks, and two names for one number is what makes a POSITIONAL
    #                 input read look reasonable.
    ui_channels: tuple = ()
    # The same shape of entry for channels that EXIST ONLY IN EXPANDED. Kept apart rather than
    # flagged inside `ui_channels`, so every caller that has no business knowing about the mode
    # keeps the plain tuple and cannot accidentally offer a channel the desk is not carrying.
    # Reach for both through `ui_channels_for`. The fader side does the same thing with
    # `FaderSpec.expanded_sources`, and for the same reason.
    expanded_ui_channels: tuple = ()
    # SMART-pad grid shape (see PadSpec). Defaults to the Pro II 8-per-bank grid so offline /
    # unknown-device code paths keep Pro II behaviour.
    pad: PadSpec = PadSpec(pads_per_bank=8)
    # Fader-assignment layout (see FaderSpec). Defaults to the Pro II 9-fader shape so offline /
    # unknown-device code paths keep Pro II behaviour.
    fader: FaderSpec = PRO2_FADERS
    # Flexible input-source strips for the by-name routing view (see InputStripSpec, GAP-3).
    # Defaults to the Pro II shape so offline / unknown-device code paths keep Pro II behaviour.
    input_strips: InputStripSpec = PRO2_INPUT_STRIPS
    # Per-output headphone-type (amp profile) setting (see HeadphoneTypeSpec, GAP-6). Both desks
    # share the confirmed HP1/HP2 selectors + value enum, so this defaults to the shared spec.
    headphones: HeadphoneTypeSpec = HEADPHONE_TYPES

    # --- Where a channel's INPUT values live -------------------------------------------------
    # ONE ADDRESS, ON EVERY DESK: INPUTSOURCE[source_code]. Read there, written there.
    #
    # THE DUO DOES NOT KEEP ITS INPUT VALUES IN A MIX BLOCK at 0x48 + source_code, however much
    # that address looks alive. It is DEAD STORAGE: a write lands in it and reads back from it, and
    # the audio engine never looks at it, so a mic gain written there is silently cosmetic
    # (proven by ear).
    #
    # HOW THAT WAS SETTLED WITHOUT EARS, because a read-back cannot tell a live cell from a dead one:
    # selecting a mic VOICING makes the engine apply that voicing's own preamp gain, phantom setting
    # and DSP profile, so the engine's response is the witness. Writing inputMicrophoneType to the
    # MIX address changed our own byte and NOTHING else. Writing it to INPUTSOURCE made the engine
    # set the Condenser default gain of 25, switch inputPower on by itself, and retune all thirteen
    # parameters of that strip's chain. Same result on Mic 1 and Mic 2. Confirmed from the other
    # direction too: the desk PUSHES its own gain moves at the INPUTSOURCE address, never at 0x48.
    #
    # THE MIX ADDRESS CAME FROM A GOOD-FAITH READING OF A REAL CAPTURE, which is why it survived so
    # long: the RODE app genuinely does send inputMicrophoneGain to 01 01 01 02 48 01, host->device,
    # as a single standalone record byte-identical to ours (duo_mic1_gain2.pcap frame 367). Copying
    # it was reasonable. It is still inert when WE send it, and that is what a capture cannot show.
    # Byte-for-byte agreement with the official app is NOT evidence that a write does anything.
    #
    # The Pro II was never affected, because its own selectors already ARE this rule: mics 0x6E-0x71
    # are codes 0-3 and lines 0x75-0x78 are codes 7-10, each with the 0x02 trailer that is really the
    # high byte of 0x26E + code. Two tables of per-device selectors have gone with the second
    # address; they were a redundant encoding of "0x26E + source_code" that only the Duo got wrong,
    # and deriving it from the source code instead makes that mistake unrepresentable.
    # test_input_read.py asserts every retired Pro II selector against the derivation.

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

        WHY THIS EXISTS: the three generated references (property list, property coverage, profile
        coverage) would otherwise each write to ONE device-agnostic path, so regenerating on the Duo
        silently overwrites the Pro II reference with Duo data - and `--check` compares NAMES only,
        which are identical across the two desks, so it reports "unchanged" while the file has
        swapped desks. The reader has no way to tell.
        Generated per-device files carry the slug; there is no shared one to clobber.
        """
        tail = self.name.replace("RODECaster", "").strip()      # "Pro 2" / "Duo"
        return "".join(ch for ch in tail if ch.isalnum()).upper() or "UNKNOWN"


# --- Read maps (see DeviceModel.ui_channels) ----------------------------------
# Pro II: the 8 processing-capable strips the UI exposes. INPUT values (mic gain/phantom/polarity/
# type, line Level) are read and written BY ADDRESS at the channel's INPUTSOURCE entry, never by
# array position - see DeviceModel.input_path.
#
# PROCESSING and EFFECTS are per-STRIP, i.e. per FADER POSITION, not per source, so proc_idx is left
# None here and resolved from the LIVE fader vector (channels._strip_index -> channelInputSource.
# index(source_code)), exactly as the Duo does. HARDWARE-CONFIRMED, and it disproves the tempting
# claim that "fader reassignment does not move a channel's processing": with USB 1 moved onto strip
# 1, switching off the gate the app labels "Mic 2" (the pinned slot 1) removes the gating from
# USB 1's audio, so slot 1 processes whatever sits on STRIP 1. On the DEFAULT layout the dynamic
# lookup returns the same indices a pinned map does (0,1,2,3,4,-,6,7,8), so a pinned map looks
# correct until faders are moved.
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

# Duo read map, against the stray-free fw-1.7.3 reference dump.
# INPUT values are read and written BY ADDRESS (DeviceModel.input_path), not by array position, and
# on this desk that address is the INPUTSOURCE entry for the source code, exactly as on the Pro II.
# Read off a factory desk:
# INPUTSOURCE inputDigitalGain is -12 at codes 7/8/9/10 (USB1, Chat, USB2, BT) and
# 0 at code 2 (Headset), inputMicrophoneGain is 50 throughout, inputPower is 0 on the mics and -1
# (not applicable) on the line inputs. An OLDER note here quoted per-index values from the flat
# positional read; those were taken through the shift this addressing fixes, so they are not
# evidence of anything and have been dropped rather than re-stated.
#
# PROCESSING/EFFECTS arrays are per-STRIP (10 strips): a source's proc index =
# channelInputSource.index(source_code) (proc_idx left None => resolved dynamically at read time).
# Mic1 -> strip 0 -> proc idx 0 is CONFIRMED (capture). Mic2 (source 1) is combo-linked/hidden and
# not on any strip in the default layout, so its proc index resolves to None (processing fields read
# as None) while its input values still read.
DUO_UI_CHANNELS = _ui_channel_view(CHANNELS_DUO)


# --- Routing-matrix UI label maps (see MatrixSpec.sources / .outputs) ----------
# Pro II: exactly what server.py had hardcoded in _MATRIX_SOURCES / _MATRIX_OUTPUTS, moved here
# so the routing screen is device-aware. The confirmed sources + 11 confirmed outputs (indices
# match rcp_transfer.CROSS_POINT_SOURCE_INDEX / CROSS_POINT_OUTPUT_INDEX; the inferred CallMe2/3
# slots stay unexposed, exactly as before).
PRO2_MATRIX_SOURCES = _matrix_source_view(CHANNELS_PRO2)
PRO2_MATRIX_EXPANDED_SOURCES = _expanded_matrix_source_view(CHANNELS_PRO2)
# "default_mode" (0 Main Mix / 1 Mix Minus / 2 Custom) is the mode "Reset all output mixes" puts
# each output back to.
#
# PROVENANCE: VERIFIED AGAINST A REAL FACTORY DUMP. All 11 values below match both
# `post-reset-raw.bin` (taken at the setup wizard, before anything was touched) and
# `default-show-raw.bin` (a showControlNewFromDefault show). Chat is MIX MINUS here: a user profile
# reading Chat = Custom is a "Chat = my mic only" preset, not a default.
# USB 1 keeps Main Mix despite offering mix-minus: it is the program feed to the computer, not a
# caller. Re-check with `rcp_read.read_output_modes` against those .bin dumps if this is ever
# doubted, rather than trusting this comment; the dumps are local-only (they hold the wifi PSK) and
# gitignored.
# NOTE the array read back is THIRTEEN long, not 11: indices 11 and 12 exist in MIXMINUSES
# and are not exposed as outputs on either desk. Do not size a loop off len(read_output_modes()).
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

# Duo: the 8 real exposed sources CONFIRMED on hardware by a routing capture - the Pro II
# set minus src3, with source code 2 = HEADSET on the Duo (it is Mic 3 on the Pro II). Outputs:
# only Headphone 1 / Headphone 2 are exposed, the two the routing capture drove and confirmed
# (mode selector 0x31 + output_index). The Duo's other output identities beyond HP1/HP2 are not
# yet captured, and matrix WRITES are gated off anyway (devices.DUO.caps lacks "matrix_write"), so
# the matrix screen is read-only on the Duo and we expose only what is confirmed rather than guess.
DUO_MATRIX_SOURCES = _matrix_source_view(CHANNELS_DUO)
DUO_MATRIX_EXPANDED_SOURCES = _expanded_matrix_source_view(CHANNELS_DUO)
# CONFIRMED on hardware, by driving the RODECaster App's Output > Routing screen: the Duo
# exposes NINE outputs. It has no Headphone 3/4 (only two headphone jacks), so it SKIPS Pro II indices
# 2 and 3, but keeps the SAME internal indexing for the rest - proven by the clean-dump outputMixMinus
# defaults [0,0,0,0,0,0,1,0,1,1,1,1,1] lining up exactly with the Pro II indices (BT/Chat/USB2/CallMe
# default to Mix-Minus at 6/8/9/10, USB1 Main at 7). Headphone/Monitor/REC offer Main+Custom only;
# BT/USB1/Chat/USB2/CallMe1 also offer Mix-Minus (matching the Pro II). See RODECASTER_PROTOCOL.txt section 5.
# default_mode: this is the Pro II set by key. The Pro II half is now verified against a real
# factory dump (see PRO2_MATRIX_OUTPUTS above), but the DUO half is not: only the fader vector was
# captured from that desk's factory reset, not the output modes. So the values are trustworthy in
# origin, unconfirmed on this device.
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


# --- Pro II: exactly the constants that were hardcoded before this table -------
PRO2 = DeviceModel(
    product_id=PID_PRO2,
    name="RODECaster Pro 2",
    channels=("Mic1", "Mic2", "Mic3", "Mic4", "Bluetooth", "SMARTpads",
              "USB1", "Chat", "USB2"),
    mic_input_format="channel",
    matrix=MatrixSpec(cell_count=390, output_count=11,
                      source_indices=_matrix_index_view(CHANNELS_PRO2), row_stride=13,
                      sources=PRO2_MATRIX_SOURCES,
    expanded_sources=PRO2_MATRIX_EXPANDED_SOURCES,
    expanded_relabel=_relabel_view(CHANNELS_PRO2), outputs=PRO2_MATRIX_OUTPUTS),
    # "chain_follow": a fader move also MOVES that channel's processing/effects chain to the strip
    # it landed on. On both desks. The Duo's "empty strip (-1) and a duplicated source" is not a
    # reason to withhold it: the duplicate sits in the 10th vector slot, which is not a fader and
    # which _read_fader_vector slices away, and the empty strip is already filtered.
    # "stereo_link": two adjacent mic inputs can be linked onto one fader. Pro II
    # only because the PAIR CODES are what makes it work and only the Pro II's are proven; the Duo
    # has no Mic 3/Mic 4 (code 2 is its Headset). See DUO_FADERS.stereo_pairs for which of the Duo's
    # pairs are impossible and which are merely not offered - they are NOT the same thing.
    # Guarded by FaderSpec.stereo_pairs too, so an empty pair table refuses on its own.
    # "level_write": the VIRTUAL fader levels and the monitor speaker level can be set from software
    # (RODECASTER_PROTOCOL 6.1). On the Duo the addresses come from the TREE and are confirmed by
    # the ENGINE, with nobody needed at the encoder - see the Duo's own note below.
    # "unit_settings": the display/haptics/REC-button/listen settings. Every
    # selector was captured on THIS desk and read back, and the desk was byte-identical to its
    # pre-capture baseline afterwards. Pro II only; the Duo reads them and refuses to write.
    # "recorder": starting, pausing and stopping a recording from software.
    # `requestRecordState` on RECORDER 0x03, and that selector was read STRAIGHT OFF THE WIRE on
    # this desk rather than derived - the unit pushed it while the REC button was pressed. It has
    # never been seen on a Duo, whose block ordering could differ, so the Duo reads the recorder
    # and refuses to drive it. Reading is safe everywhere and is not gated.
    # "network_write": wifi on/off, joining a network, and Bluetooth discoverability, on NETWORK
    # 0x10. Proven twice on hardware - btVisible written there makes a phone pair and play audio,
    # and wifi credentials written there join a network with the touchscreen untouched, confirmed by
    # pinging the desk. On both desks.
    # "shows": the desk's OWN backup - export, import, software factory reset and delete, on
    # SHOWCONTROL 0x28E. ALL FOUR COMMANDS ARE PROVEN FROM OUR OWN WRITES: two full
    # reset-and-restore cycles with nobody at the touchscreen, the second returning ZERO real
    # differences against a profile snapshot taken moments before the reset, pads included. The cap
    # is ON because the loop works, not merely because the page is gated.
    # It is on for the Duo too: root child 0x28E on a live Duo IS the SHOWCONTROL block, carrying
    # all ten showControl* properties, so a two-byte selector cannot land a factory reset on an
    # unknown block. What a Duo here has no route to is the EXPORT TARGET, its storage volumes
    # reading empty with nothing inserted.
    # "callme": hosting and answering a CallMe call, on the SIPCALLING node found by walking the
    # tree. Heard both ways on a Pro II with a browser guest; never tried on a Duo.
    # "storage_write": ejecting and erasing the card or drive, on the STORAGEVOLUME nodes. Both
    # proven on a Pro II's USB drive; a Duo here has never had storage in it.
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

# --- Duo: what the reads, the reversible probes and the console capture establish --
# STRUCTURE (read-only, verified): 19 input slots (0-6 real), 10 strips, 9 faders,
# 13 outputs, 16 pads. Matrix = 516 cells on fw 1.7.3 (the fw-1.6.8 dump had 247; the update grew
# the model to the Pro II shape). Read/write scheme VERIFIED identical to the Pro II (see matrix note).
# WRITES: global-format writes work + reflect in reads; the fader vector
# channelInputSource uses global format at selector 0x1C + strip_index (same as Pro II).
# INPUT-MIC write format CRACKED (captured from the fw-1.7.3 console; see
# RODECASTER_PROTOCOL.txt section 4.1): channel-address 01 01 01 02 <sel> 01
# (trailing 0x01, NOT the Pro II 0x02) with selector = 0x48 + channelInputSource source_code.
# The capture campaign CONFIRMED every input selector: Mic1 0x48, Mic2 0x49,
# Headset 0x4a, USB1 0x4f, Chat 0x50, USB2 0x51, Bluetooth 0x52 (source codes 0/1/2/7/8/9/10).
# gain/P48/polarity/type share the address, distinguished by property name; value int =
# 01 05 01 <int32 LE>. Mic2 (0x49) was confirmed by a code write + array read-back; the others
# by console capture. => input_mic_write cap is ON.
# PROCESSING (proc_sel_base 0x1C) + EFFECTS (fx_sel_base 0x26) were captured on Mic1 (proc idx 0):
# channelBypassProcessing / eqOn / eqHighGain / channelCurrentFxPreset on 0x1C and echoOn / echoMix
# on 0x26, byte-for-byte.
# PROCESSING SELECTOR FULLY CONFIRMED (console capture, six channels): the processing
# selector = proc_sel_base (0x1C) + STRIP index, where strip index = channelInputSource.index(
# source_code) - EXACTLY the dynamic lookup the read map uses. Captured Mic1 s0->0x1C, USB1 s1->0x1D,
# Chat s2->0x1E, USB2 s3->0x1F, Headset s6->0x22, Bluetooth s8->0x24. The two GAPS (empty strips
# 4/5/7) prove it is keyed to STRIP index, NOT carousel position, which also answers "does
# processing follow source or strip?": the array is per physical strip and the app
# addresses proc[current strip of the source]. (rcp_transfer._selftest asserts all six byte-for-byte.)
# EFFECTS on non-Mic1 strips: CONFIRMED on hardware, by driving the RODECaster App Mic 2 channel
# (Mic 2 sits on strip 1 in the default layout) -> Effects tab -> toggled Echo on/off while capturing.
# Both echoOn writes carried address 01 01 01 01 27, i.e. selector 0x27 = fx base 0x26 + strip 1, so
# effects are strip-indexed just like processing (0x1C+strip). Echo restored to off; device verified
# byte-clean (all key props match the pre-capture read, no strays). pcap: captures/duo_fx_mic2_strip1.
# (Mic2 = channel 2 = strip 1 here, so this alone does not distinguish 0x26+strip from 0x26+(ch-1);
# processing already proved strip-indexing, and the fx array shares that architecture.)
#
# STRAYS PERSIST ACROSS A POWER CYCLE AND ONLY A FACTORY RESET CLEARS THEM. What a stray does here:
# one spurious entry prepended at idx0 of BOTH inputMicrophoneGain (=44) and channelInputSource (=1),
# shifting each +1, which mis-targets every read and every processing write. After a reset
# inputMicrophoneGain[0] reads 50 and channelInputSource is back to 10 entries starting at source 0
# with no shift. The stray-free fw-1.7.3 baseline is the single reference dump (a capture, so not in
# the repo).
DUO = DeviceModel(
    product_id=PID_DUO,
    name="RODECaster Duo",
    # The Duo's CONFIRMED input identities (source codes 0,1,2,7,8,9,10,11), in reading order.
    # Unlike the Pro II, the Duo has no fixed per-strip names: its 10 logical strips are
    # dynamically sourced by the fader vector (channelInputSource), so we name the fixed INPUTS
    # (what the mixer actually addresses, keyed by source code) rather than the old Strip1..Strip10
    # placeholders. Matches DUO_UI_CHANNELS / DUO_MATRIX_SOURCES.
    channels=("Mic1", "Mic2", "Headset", "USB1", "Chat", "USB2", "Bluetooth", "SMARTpads"),
    # NO INPUT SELECTOR TABLE, on purpose. RODE app captures put the Duo's mics at 0x48/0x49 and its
    # line levels at 0x48 + source_code, all with a 0x01 trailer. Every one of those
    # addresses is DEAD STORAGE that the audio engine never reads - see DeviceModel.input_path for
    # the proof and for why the captures were misleading rather than wrong. Input values on this desk
    # are addressed exactly as they are on the Pro II, from the source code.
    mic_input_format="channel",
    # MATRIX VERIFIED ON HARDWARE (routing capture, RODECaster App fw 1.7.3): the Duo
    # matrix is BYTE-IDENTICAL in scheme to the Pro II. fw 1.7.3 grew the model to 516 cells (the old
    # 247 came from the fw-1.6.8 dump). read index = 13*source + output; write selector = 0x4c + index;
    # output-mode selector = 0x31 + output_index (HP1=0x31, HP2=0x32 captured). Four cross-points
    # confirmed selector==0x4c+index: (out0,src10)=idx130/0xce, (out0,src2)=idx26/0x66,
    # (out0,src7)=idx91/0xa7, (out1,src10)=idx131/0xcf. output 0->1 shifts index & selector by +1
    # (output = fast axis, stride 1); source stride = 13. Real exposed sources (non-placeholder at
    # baseline) = 0,1,2,7,8,9,10,11 (Mic1/Mic2/Headset/USB1/Chat/USB2/BT/SMART); the Pro II set minus
    # src3 (no Mic3/Mic4 on the Duo). See RODECASTER_PROTOCOL.txt section 5 (routing matrix).
    matrix=MatrixSpec(cell_count=390, output_count=13,
                      source_indices=_matrix_index_view(CHANNELS_DUO),  # CONFIRMED on hardware
                      row_stride=13,                               # CONFIRMED (13*source+output)
                      sources=DUO_MATRIX_SOURCES,
    expanded_sources=DUO_MATRIX_EXPANDED_SOURCES,
    expanded_relabel=_relabel_view(CHANNELS_DUO), outputs=DUO_MATRIX_OUTPUTS),
    # input_mic_write ON (format captured + byte-verified). "processing" + "channel_read" ON (both
    # hardware-confirmed, on a stray-free desk). "matrix_read" is ON (formula verified on hardware,
    # see the matrix note above; cell_count guard set to the MIX block's 390).
    # "matrix_write" is ON (confirmed on hardware): a reversible live test via our
    # own code - HP1 -> Custom (outputMixMinus 0x31 -> mode 2), excluded then re-included Bluetooth on
    # HP1 (cross-point selector 0x4c + 13*10 + 0 = 0xCE, mixDisabled[130] toggled True then False),
    # then HP1 -> Main Mix. Read-back matched at every step and the device was byte-clean afterwards
    # (all three matrix arrays identical to baseline, 0 excluded cross-points, no stray). So the
    # whole routing matrix, monitor mix and Chat preset are writable on the Duo, same as the Pro II.
    # "output_write" is ON (confirmed on hardware): drove the RODECaster App and
    # captured the master/global output-block writes. All selectors are BYTE-IDENTICAL to the Pro II -
    # recordingMultitrackMode 0x0D, outputBTAutoMute 0x0D, masterCompellorOn 0x0E, headphoneType 0x09
    # (Headphone 1) / 0x0A (Headphone 2). Our build_global_prop_* reproduce the app's writes exactly;
    # toggled + restored on hardware, device verified byte-clean afterwards. (GAP-6: headphone type
    # is device-model-driven via HeadphoneTypeSpec / DeviceModel.headphones -
    # both desks expose HP1 0x09 + HP2 0x0A and the 3-value amp enum. Value 2 = Low sensitivity and
    # HP2-on-Pro-II are inferred and still want a supervised live capture to confirm; HP3/HP4 remain
    # unlisted until their selectors are captured. See HeadphoneTypeSpec.)
    # SMART pads: READS are confirmed identical to the Pro II - grid geometry, region-A/B split,
    # effectsIdx==padIdx binding, clean 7-object pool, and all 6 factory FX recipes byte-identical.
    # "pad_write" is ON: a reversible, read-back-verified live create/clear on empty bank-4 slots
    # PROVED the three FIRMWARE behaviours a read cannot -
    # (1) clear GCs the bound region-B object (FX create region_b 7->8, clear 8->7), (2) a bare
    # create does not append to the pool (trigger create kept region_b at 7), (3) handle = pad count
    # (both creates used handle 0x16 = 22). Pool stayed clean throughout; device returned to the
    # 22-record / 7-object baseline with all 6 factory recipes still byte-identical.
    # FULL BROWSER/HTTP END-TO-END also CONFIRMED, driving
    # the live web endpoints (/api/pad/replace, create-special, assign-fx, name/gain/loop, clear) on
    # empty pads - WAV + MP3 upload (extension preserved), mixer create, a voice-FX create (region-B
    # 7->8) and clear (GC 8->7), and read-back in-place edits. read_only reported False (un-gated for
    # the Duo). Pool clean at every step; pads + region-B pool returned byte-clean to the 7-object baseline.
    # "fader_write" is ON (GAP-1, CONFIRMED on hardware): a reversible, read-back-verified
    # live test proved the Duo fader WRITE semantics via our own code - set a source on an empty strip
    # (idx8 -1 -> 16, read back 16), VACATE a strip by writing -1 (idx8 -> -1, read back -1), and write
    # the highest strip idx9/fader 10 (empty + restore), all byte-clean back to the [0,1,10,11,2,7,8,9,-1,9]
    # baseline. The build_global_prop_int signed-int fix (rcp_transfer) lets us encode -1, and set_fader_source's
    # fader-index bound is now device-aware (10 on the Duo). So SETTING a channel on a fader is enabled.
    # RESETTING to a factory default is enabled too: DUO_FADERS.default_vector is set, confirmed
    # against the post-factory-reset dump and re-read on hardware. Its
    # empty strip (idx8 = -1) and mirrored slot (idx9) are the genuine shipped default, not a
    # corrupt read - see DUO_FADERS. A live reset-and-read-back has not been run yet (B6).
    # "unit_settings" is ON WITHOUT A CAPTURE, and the TREE is what makes that safe rather than a
    # guess at an unmapped selector: all 20 properties in rcp_transfer.UNIT_SETTING_SEL resolve to a
    # real node of the right block name on this desk (GUI 0x07, OUTPUT 0x0D, SYSTEM 0x0F) and every
    # one is present on it, and so is every setting the Device page offers. Then one property from
    # each of the three blocks was written, read back and restored byte-clean (metering,
    # screenBrightness, systemRecButtonSetting, systemHapticSetting); see DevTools/test_duo_caps_live.py.
    # "level_write" is ON without a capture. `faderLevel` occurs nine times in a Duo dump, at
    # PHYSICALINTERFACE.FADER[4..12], which is the same shape as the Pro II's three long-proven
    # addresses. The desk confirms it is the LIVE address by
    # acting on its own: writing fader index 4 to 40 made the engine recompute all thirteen of that
    # source's routing cross-points to 0.314961, which is 40/127 exactly, and fader 5 moved source
    # 7's thirteen. Both restored byte-clean. The monitor half writes outputMonLevel on OUTPUT 0x0D,
    # the node `unit_settings` already writes and the node the desk pushes its own encoder turns on.
    # "chain_follow" is proven live on this desk both ways: a channel
    # moved onto the EMPTY fader took its chain with it (15 values) and came home with it, and a
    # swap between two OCCUPIED faders crossed both chains (6 values) and put them back. Layout
    # byte-identical to the factory vector afterwards each time.
    # "stereo_link" is ON. "The Duo has no Mic 3/Mic 4, so its pairs are not even the same question"
    # is wrong about TWO of the three: inputs 1 and 2 are adjacent, so "1 + 2" is exactly the same
    # question, and "2 + 3" is Mic 2 + the Headset jack, two real adjacent inputs, so it is a live
    # possibility we CHOOSE not to offer rather than one the desk cannot do. Only "3 + 4" is
    # impossible, because there is no input 4. See DUO_FADERS.stereo_pairs, which says which is
    # which. Three things settle the cap, and NO model test is among them:
    #   * RODE's own Duo user guide documents stereo linking on the channel set-up screen, with the
    #     same behaviour (hard-panned L/R, two meters on one fader). So the HARDWARE has it.
    #   * Pair codes are positions in the firmware's InputSourceType enum, and both desks run the
    #     SAME binary (one app.zip, "rcp2"), so code 4 cannot mean something else here.
    #   * The write is proven on this desk: code 4 lands in the vector, the desk accepts it and
    #     link/unlink restores the tree byte-identical.
    # EAR-PROVEN, not merely written: linked from the app, Mic 2 is heard hard-panned into ONE EAR
    # and the vacated fader beside it stays silent. Unlink puts the layout back exactly.
    # DO NOT TRY TO PROVE THIS FROM THE MODEL, which is what the probe went looking for and could
    # not find: THE ENGINE'S PROPERTY MODEL IS DEVICE-AGNOSTIC. Source 3 is Mic 4, hardware a Duo
    # does not have, and it accepts a fader placement, fills its whole routing row and answers a
    # mic-type write EXACTLY as the real Mic 2 does. So "the engine responded" proves an ADDRESS is
    # live, never that a FEATURE exists on this desk. See PROJECT_GUIDE part 2.
    caps=frozenset({"input_mic_write", "processing", "channel_read", "matrix_read",
                    "matrix_write", "output_write", "pad_write", "fader_write",
    # "network_write" is ON by the same TREE-then-ENGINE route as the three above. Root child 0x10 on a live Duo IS the NETWORK block (56 properties) and carries every
    # property the app writes: wifi, wifiSSID, wifiPSK, wifiScan, btVisible and wifiScan1..10. Then
    # the engine confirmed the address by doing something we did not send: writing `wifi` False made
    # the DESK clear wifiIpAddress, wifiSubnetMask, wifiGateway and primaryDns, and turning it back
    # on brought the same DHCP address back. btVisible round-tripped byte-clean.
    # THE SCAN IS THE ONE PART NOT PROVEN HERE. `wifiScan` = True on a Duo already joined to a
    # network filled no slot in 35 seconds and the desk did not even clear the flag, where a Pro II
    # answers in a few. Not an addressing problem - the same write reaches the radio - so the likely
    # answer is that this firmware will not scan while associated. action_scan_wifi already tells
    # the user an empty result may be spurious, which is the right behaviour either way.
    # "recorder" is ON by the same TREE-then-ENGINE route as the four above. RECORDER resolves to root child [3] = `01 01 01 01 03` on a live Duo,
    # byte-for-byte recorder.SEL_RECORDER, carrying recordState / recordTimeMs / requestRecordState /
    # requestDropMarker / recordBytesPerSecond; RECORDINGS resolves to [655] = 0x28F =
    # recorder.SEL_RECORDINGS. The ENGINE then confirmed the address rather than a read-back: a real
    # requestRecordState = 2 was CONSUMED and self-cleared 255 -> 3, the same firmware behaviour the
    # Pro II shows, and the whole dump came back byte-identical with no stray appended. The read half
    # needed nothing: the Duo reports the same three storage slots (card, USB, internal 5033 MB pad
    # volume) with storageVolumeRecDestination [True, True, False].
    #
    # WHAT THIS CAP DOES NOT CLAIM, and the distinction is the one four lines above this: an engine
    # answering proves an ADDRESS is live, never that a FEATURE works. NOBODY HAS HEARD A DUO MAKE A
    # RECORDING - there is no card in this one, so `recordState` sits at 3 (no destination) and the
    # transport has never been driven to a file. The cap is on anyway because the OLD behaviour was
    # a lie in the other direction: it told a Duo owner the recorder was unsupported, when it is
    # present and addressable, and a card-less Duo now correctly says "No recording destination" -
    # the same true message a drive-less Pro II gives. This matters more here than on a Pro II
    # because THE DUO HAS NO PHYSICAL REC BUTTON (measured), so without this the touchscreen is a
    # blind operator's only route to recording at all.
    # "shows" is ON alongside "recorder" and for the same reason: holding it back would mean an app
    # update before anyone with storage could test it. The tree says the address is there (root
    # child 0x28E on a live Duo IS SHOWCONTROL, all ten showControl* properties) and
    # THE ENGINE PROVES IT LIVE: an export asked of a card-less Duo is answered in ONE
    # SECOND with showControlLastError 0 -> 8 and showControlExport cleared back to False, no show
    # created, nothing else in the tree moved. A desk that reports a specific refusal code is a desk
    # that read the command.
    #
    # SO THE FAILURE MODE WITH NO STORAGE IS KNOWN RATHER THAN GUESSED, which is what makes this safe
    # to enable ahead of the hardware: shows.ERROR_NO_STORAGE now words code 8 properly instead of
    # "cannot explain". WHAT IS STILL UNWITNESSED is a Duo actually WRITING and RELOADING a show,
    # which needs a card or drive in that desk. All four commands are proven on a Pro II.
    #
    # "callme": every CallMe write finds its node by the property it carries on the live tree,
    # never by a selector, so a Duo cannot be handed a Pro II address; both desks run the same
    # firmware program. A Pro II to Duo call connects in both directions.
    # "storage_write" stays OFF: no Duo here has had a card or drive in it.
                    "level_write", "chain_follow", "unit_settings", "stereo_link",
                    "network_write", "recorder", "shows", "callme"}),
    proc_sel_base=0x1C,
    fx_sel_base=0x26,
    ui_channels=DUO_UI_CHANNELS,
    expanded_ui_channels=EXPANDED_UI_CHANNELS,
    # 6 pads/bank x 8 banks = 48 slots; system pads at padIdx 42-47 (bank 8). CONFIRMED read-only.
    pad=PadSpec(pads_per_bank=6),
    # Fader vector is 10 entries (10 logical strips); code 2 = Headset (not Mic 3); no Mic3/Mic4.
    # default_vector IS set (confirmed against the post-factory-reset dump), so 'reset
    # faders' is enabled here - see DUO_FADERS for why the empty strip + mirrored slot are genuine.
    # Its five virtual faders (indices 4-8) resolve to [0,8]..[0,12] through FaderSpec.
    # level_selectors, the same derivation the Pro II uses.
    fader=DUO_FADERS,
    # No by-name flexible strips: the Duo uses the index-based fader_model (see DUO_INPUT_STRIPS).
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
    runs on, so the same test passes on one desk and fails on the other with the code unchanged.
    That is not a hardware finding, but it reads exactly like one: three suites (test_announcer,
    test_shows_sim, test_level_check) were failing on scottpc purely because a Duo was attached, and
    each had to be re-run against unmodified code to establish that it meant nothing.

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
