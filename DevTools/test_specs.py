"""The app's own spec tables must agree with each other. Offline, no device.

WHY THIS FILE EXISTS. The app keeps a setting's ADDRESS (how to write it on the wire) apart from its
PRESENTATION (what to call it and how to explain it), and that split is right: the address table is
shared with the Duo and has no business carrying help text. The hazard is MEMBERSHIP being
duplicated with nothing checking it, because then a setting becomes writable and never reaches the
page, and the only symptom is some other test reporting the two sets as unequal - which reads as a
broken test rather than as drift.

THE RULE: the address table is the single source of membership, and every entry either appears on
the page or is named in UNIT_SETTINGS_NOT_ON_PAGE with the reason.

This is the place for any other "two tables that must agree" check. Run:
    python DevTools/test_specs.py
"""
import _bootstrap  # noqa: F401  (puts Project/ on the path; import FIRST)
import _testkit
import channel_actions
import channels
import device_page
import desk_access
import desk_labels
import devices
import model_tree
import pad_records
import profiles
import rcp_transfer
from _testkit import check, check_true, patched


def _raises(fn):
    """True if calling fn() raises ValueError. Used where refusing IS the correct behaviour."""
    try:
        fn()
    except ValueError:
        return True
    return False


print("desk settings: address table vs the Device page")

# THE CHECK THIS FILE WAS WRITTEN FOR.
off = device_page.unit_settings_off_page()
check_true("every writable desk setting is on the page or deliberately off it",
           not off,
           "these are writable but neither on the Device page nor listed in "
           f"UNIT_SETTINGS_NOT_ON_PAGE, so nobody has decided about them: {off}")

# The reverse direction is asserted at import in device_page.py, but assert it here too so a failure
# names itself instead of arriving as an ImportError.
missing_sel = sorted(p for p in desk_labels.UNIT_SETTING_BY_PROP
                     if p not in rcp_transfer.UNIT_SETTING_SEL)
check_true("every setting on the page has an address", not missing_sel,
           f"on the page with no way to write them: {missing_sel}")

# An exemption for something that is not writable at all is a leftover, and a leftover reason is
# worse than none: it implies a decision was made about a setting that no longer exists.
stale = sorted(p for p in device_page.UNIT_SETTINGS_NOT_ON_PAGE
               if p not in rcp_transfer.UNIT_SETTING_SEL)
check_true("no exemption names a setting that is not writable", not stale,
           f"listed as deliberately off the page but not in the address table: {stale}")

# A setting cannot be both on the page and excluded from it.
both = sorted(p for p in device_page.UNIT_SETTINGS_NOT_ON_PAGE if p in desk_labels.UNIT_SETTING_BY_PROP)
check_true("nothing is both on the page and excluded from it", not both, f"in both: {both}")

# Every exemption has to say WHY, in a sentence, or it is just a silencer.
thin = sorted(p for p, why in device_page.UNIT_SETTINGS_NOT_ON_PAGE.items() if len(why.strip()) < 30)
check_true("every exemption gives a real reason", not thin, f"reason too thin to be one: {thin}")

print("\nthe address is a tree path, and there is one builder for it")


# EVERY address shape this app ever hand-wrote, proven to be the same format. These hex strings are
# the CAPTURED bytes (from real RODE app frames), not something the builder produced, so this is a
# comparison against ground truth rather than against itself.
for label, path, captured in [
    ("global, mic processing", [0x1C],        "010101011c"),
    ("channel, Pro II mic 1",  [0x026E],      "010101026e02"),
    ("channel, Duo mic 1",     [0x0148],      "010101024801"),
    ("SHOWCONTROL",            [0x28E],       "010101028e02"),
    ("a pad",                  [0x29A, 0x01], "010102029a020101"),
]:
    check_true(f"{label} is path {[hex(x) for x in path]}",
               model_tree.encode_path(path).hex() == captured,
               f"built {model_tree.encode_path(path).hex()} against captured {captured}")

# The builders must still emit those bytes. rcp_transfer._selftest checks the whole records against
# the captures; this checks the ADDRESS part specifically, so a regression here names itself.
check_true("a root child addresses by path",
           rcp_transfer.build_record("x", "int", 0, [0x1C])[5:10]
           == model_tree.encode_path([0x1C]), "global address drifted")
check_true("a two-byte index addresses by path",
           rcp_transfer.build_record("x", "int", 0, [0x026E])[5:11]
           == model_tree.encode_path([0x026E]), "two-byte address drifted")
check_true("a pad record addresses by path",
           rcp_transfer.build_record("padIdx", "int", 15, [0x29A, 0x01])[5:13].hex()
           == "010102029a020101", "pad address no longer matches the captured bytes")
try:
    rcp_transfer._selftest()
    _selftest_error = ""
except AssertionError as e:
    _selftest_error = str(e)
check_true("the builders reproduce every captured record (rcp_transfer._selftest)",
           _selftest_error == "", _selftest_error)

# ZERO INDEX: the builders emit `01 00`, NOT the zero-length form. Both are legal by the grammar and
# the desk takes both, but pad writes are confirmed only with `01 00`, and pads are the address
# family where a bad write appends a stray that only a factory reset clears. Do not switch forms.
check_true("index zero is emitted as one byte, as our proven pad writes do",
           model_tree.encode_path([0x29A, 0]).hex() == "010102029a020100",
           model_tree.encode_path([0x29A, 0]).hex())
check_true("and that is exactly what the pad builder sends",
           pad_records.write_record("padIdx", "int", 0, 0x00)[5:13].hex() == "010102029a020100",
           pad_records.write_record("padIdx", "int", 0, 0x00)[5:13].hex())

check_true("a negative index is refused rather than encoded",
           _raises(lambda: model_tree.encode_path([-1])), True)
# A two-byte index must not silently truncate: 0x26E is the difference between a mic and something
# else entirely.
check_true("an index over 255 takes two bytes",
           model_tree.encode_path([0x026E]).hex() == "010101026e02",
           model_tree.encode_path([0x026E]).hex())

print("\nunit settings: what a profile does with them")

# Every unit setting is SAVED by a profile; the two mutes are deliberately never written back. Assert
# that pairing, because "saved but never restored" is the failure mode that looks like a backup and
# is not one, and the reason it is acceptable here (a restored mute silences someone mid-show) has
# to stay attached to the decision.

never = profiles.UNIT_NEVER_RESTORE
check_true("the never-restore list only names writable settings",
           all(p in rcp_transfer.UNIT_SETTING_SEL for p in never), f"{sorted(never)}")
check_true("and it is only the mutes, which is the whole justification for it",
           set(never) == {"outputMonMute", "outputBTMute"},
           "if this changed, the reason written in profiles.py may no longer cover it: "
           f"{sorted(never)}")

print("\nstereo-linked pairs: nameable, never assignable")

# THE load-bearing separation. A linked pair is its own source code, so it can sit in the fader
# vector and must be nameable - but it must never be something the UI offers to place on a fader,
# and never a gap-fill candidate for the layout tidy. If a pair code reached `code_set`, the tidy
# would hand "Mic 1 + 2" to a spare strip while Mic 1 and Mic 2 were also assignable, and a mic
# would end up both linked and on its own fader.

for _d in devices.DEVICES.values():
    _f = _d.fader
    check_true(f"{_d.name}: pair codes stay OUT of code_set",
               not (set(_f.pair_map) & set(_f.code_set)),
               f"{sorted(set(_f.pair_map) & set(_f.code_set))}")
    check_true(f"{_d.name}: but every pair code IS nameable",
               all(c in _f.all_labels for c in _f.pair_map), f"{sorted(_f.pair_map)}")
    check_true(f"{_d.name}: every pair's two members are assignable channels",
               all(lo in _f.code_set and up in _f.code_set for lo, up in _f.pair_map.values()),
               f"{_f.pair_map}")
    check_true(f"{_d.name}: no pair code collides with an assignable one",
               not (set(_f.pair_map) & set(c for c, _l in _f.assignable)))
    # A device with the cap but no table (or the reverse) is a half-wired feature; the gate checks
    # both at runtime, and this says so at build time.
    check_true(f"{_d.name}: the stereo_link cap and the pair table agree",
               ("stereo_link" in _d.caps) == bool(_f.stereo_pairs),
               f"cap={'stereo_link' in _d.caps} pairs={bool(_f.stereo_pairs)}")


# --- A DESK IS IDENTIFIED BY ITS SERIAL, NOT BY AN ID THAT MOVES -----------------------------
# The product id encodes the USB audio configuration, so a desk in any non-default `outputMultiMode`
# presents an id no table holds (a Pro II reads 0x0037, 0x0078, 0x0092 or 0x0094 by mode). Resolving
# on the id alone then falls through to DEFAULT, which IS a Pro II - so the fault is invisible on a
# Pro II and hands a DUO the wrong matrix shape and channel layout. Checked here rather than on
# hardware because only the Duo half is wrong and no Duo is attached.

print("\nIDENTIFYING A DESK WHOSE PRODUCT ID IS NOT IN THE TABLE")


class _Iface:
    def __init__(self, product_id, serial):
        self.product_id, self.serial_number = product_id, serial
        self.interface_number = 9


_MODE5 = _Iface(0x0094, "TEST-DUO-1")        # an Expanded desk: a real measured id, absent from DEVICES
_saved_learned = dict(devices._LEARNED)
devices._LEARNED.clear()
try:
    check_true("a mode-5 product id is genuinely unknown, or this test proves nothing",
               0x0094 not in devices.DEVICES)
    check_true("and such an interface is flagged for identifying", devices.unidentified(_MODE5))
    check_true("a DUO says so in its own dump, whatever id it arrived under",
               devices.note_system_name(_MODE5, "RODECaster Duo") is devices.DUO)
    check_true("which is remembered against the SERIAL, the one thing that does not change",
               devices._LEARNED.get("TEST-DUO-1") is devices.DUO)
    check_true("so it is not asked again", not devices.unidentified(_MODE5))
    check_true("a name matching neither desk teaches nothing, rather than teaching a guess",
               devices.note_system_name(_Iface(0x0094, "TEST-OTHER"), "Some Other Box") is None
               and "TEST-OTHER" not in devices._LEARNED)
    check_true("a known id still wins without any dump at all",
               devices.resolve_device(devices.PID_DUO) is devices.DUO
               and devices.resolve_device(devices.PID_PRO2) is devices.PRO2)
    # The consequence, stated as the thing that actually breaks: a Duo given a Pro II's spec has
    # the wrong number of faders and the wrong matrix, and every read and write follows that.
    check_true("the two desks really do differ, so misidentifying one matters",
               devices.DUO.fader.count != devices.PRO2.fader.count
               or devices.DUO.matrix.output_count != devices.PRO2.matrix.output_count)
finally:
    devices._LEARNED.clear()
    devices._LEARNED.update(_saved_learned)


# --- EXPANDED'S FOUR SOURCES: NAMED ALWAYS, ASSIGNABLE ONLY IN EXPANDED ----------------------
# Codes 12-15 are RODE's Virtual Devices. The split matters both ways:
# offering them in Standard puts a permanently silent input on a fader, and refusing to NAME them
# there leaves the announcer saying "0 percent, Source 12" about a fader that still holds one.
print("\nTHE EXPANDED-ONLY SOURCES")
for dev in (devices.PRO2, devices.DUO):
    _f = dev.fader
    _codes = [c for c, _l in _f.expanded_sources]
    check_true(f"{dev.name}: it has the four", _codes == [12, 13, 14, 15], _codes)
    check_true(f"{dev.name}: named even in Standard, or the announcer says a bare number",
               all(_f.labels_for(False).get(c) for c in _codes))
    check_true(f"{dev.name}: not assignable in Standard",
               not any(c in _f.code_set_for(False) for c in _codes))
    check_true(f"{dev.name}: assignable in Expanded",
               all(c in _f.code_set_for(True) for c in _codes))
    check_true(f"{dev.name}: the dropdown order and the write gate agree",
               set(_f.code_order_for(True)) == set(_f.code_set_for(True)))
    check_true(f"{dev.name}: a stereo PAIR code is still never assignable",
               not any(c in _f.code_set_for(True) for c, _lo, _up, _l in _f.stereo_pairs))
check_true("USB 1 is renamed in Expanded, since Main Stereo is not what the computer sends there",
           devices.PRO2.fader.labels_for(True)[7] != devices.PRO2.fader.labels_for(False)[7])
check_true("and a relabel never changes what may be written",
           devices.PRO2.fader.code_set_for(True) >= devices.PRO2.fader.code_set_for(False))


print("\nEVERY SOURCE CODE A DESK HAS IS NAMED")
for _dev, _table in ((devices.PRO2, devices.CHANNELS_PRO2), (devices.DUO, devices.CHANNELS_DUO)):
    _want = {c.code for c in _table} | {code for code, *_rest in _dev.fader.stereo_pairs}
    check_true(f"{_dev.name}: every channel and stereo-pair code has a name",
               set(_dev.source_codes) == _want,
               f"named {sorted(_dev.source_codes)}, want {sorted(_want)}")


# --- ONE CHANNEL TABLE, AND THE THREE SURFACES DERIVED FROM IT ------------------------------
# The fader dropdown, the Channels page and the routing-matrix rows are views over
# devices.CHANNELS_<DESK>. Three hand-written lists drift apart silently, so these checks stop a
# separate list growing back.
print("\nTHE CHANNEL TABLE IS THE ONLY LIST")
for dev, _table in ((devices.PRO2, devices.CHANNELS_PRO2), (devices.DUO, devices.CHANNELS_DUO)):
    _n = dev.name
    _by_code = {c.code: c for c in _table}
    check_true(f"{_n}: no code appears twice", len(_by_code) == len(_table))
    check_true(f"{_n}: no key appears twice", len({c.key for c in _table}) == len(_table))
    check_true(f"{_n}: every kind is one we handle",
               {c.kind for c in _table} <= {"mic", "line", "bus"}, {c.kind for c in _table})
    # The three surfaces must AGREE about any channel they share. This is the whole point.
    _fader = dict(dev.fader.labels_for(False))
    _page = {c["source_code"]: c["label"] for c in dev.ui_channels}
    _grid = {s["index"]: s["label"] for s in dev.matrix.sources}
    _shared = set(_fader) & set(_page) & set(_grid)
    check_true(f"{_n}: the three surfaces label every shared channel identically",
               all(_fader[c] == _page[c] == _grid[c] for c in _shared),
               {c: (_fader[c], _page[c], _grid[c]) for c in _shared
               if not _fader[c] == _page[c] == _grid[c]})
    check_true(f"{_n}: and they agree in Expanded too",
               all(dev.fader.labels_for(True)[c]
               == {x["source_code"]: x["label"] for x in dev.ui_channels_for(True)}[c]
               == {s["index"]: s["label"] for s in dev.matrix.sources_for(True)}[c]
               for c in _shared))
    # Membership is per entry and stated, so a surface cannot silently miss a channel.
    check_true(f"{_n}: every channel-page entry comes from the table",
               {c["source_code"] for c in dev.ui_channels}
               == {c.code for c in _table if c.on_channels_page and not c.expanded_only})
    check_true(f"{_n}: every matrix row comes from the table",
               {s["index"] for s in dev.matrix.sources}
               == {c.code for c in _table if c.in_matrix and not c.expanded_only})
    check_true(f"{_n}: every assignable code comes from the table",
               set(dev.fader.code_set)
               == {c.code for c in _table if c.on_faders and not c.expanded_only})
    # The four reach ALL THREE in Expanded.
    _virtual = {c.code for c in _table if c.expanded_only}
    check_true(f"{_n}: Expanded's four reach the faders",
               _virtual <= set(dev.fader.code_set_for(True)))
    check_true(f"{_n}: ...the Channels page",
               _virtual <= {c["source_code"] for c in dev.ui_channels_for(True)})
    check_true(f"{_n}: ...and the routing matrix",
               _virtual <= {s["index"] for s in dev.matrix.sources_for(True)})
    # THE ROWS THE GRID READS AND THE ROWS THE PAGE OFFERS. The read must never be the narrower of
    # the two, or a row is offered with no state behind it.
    for _exp in (False, True):
        check_true(f"{_n}: the grid read covers every row offered (expanded={_exp})",
               set(s["index"] for s in dev.matrix.sources_for(_exp))
               <= set(dev.matrix.all_source_indices))
    # A SCHEME'S SHAPE MUST NOT DEPEND ON THE MODE, so what is read and stored is the wide set in
    # both: a scheme saved in Standard with no Virtual A cell leaves that cell as it is when applied
    # in Expanded, and "Everything normal" cannot reset it.
    check_true(f"{_n}: the read set is mode-independent and covers Expanded's four",
               _virtual <= set(dev.matrix.all_source_indices))
    check_true(f"{_n}: a scheme covers every row the grid reads",
               tuple(s["index"] for s in dev.matrix.all_sources)
               == tuple(dev.matrix.all_source_indices))

print("\nA REFUSAL NAMES THE DESKS A FEATURE IS CONFIRMED ON, FROM THEIR CAPS")
for _cap in ("callme", "storage_write"):
    _holders = [d.name for d in devices.DEVICES.values() if _cap in d.caps]
    _said = desk_access.confirmed_on(_cap)
    check_true(f"{_cap}: every desk holding it is named, and no other",
               all((d.name in _said) == (d.name in _holders) for d in devices.DEVICES.values()))
check("a cap no desk holds names nobody", desk_access.confirmed_on("no_such_cap"), "")

PAN_SPOKEN = {0.0: "hard left", 0.5: "centre", 0.6: "20 percent right", 1.0: "hard right"}
print("\nA PAN IS SPOKEN AS A POSITION, AS THE LINKED PAN SAYS IT")
with patched((channels, {"set_channel_processing": lambda *_a: None})):
    for _v, _want in PAN_SPOKEN.items():
        _msg = channel_actions.action_set_channel_param("Mic1", "channelPan", _v)["message"]
        check(f"a pan of {_v} is {_want}", _msg.endswith(f" set to {_want}."), True)

raise SystemExit(_testkit.finish())
