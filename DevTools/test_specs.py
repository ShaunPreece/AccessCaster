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

import rcp_transfer
import server

FAILED = []


def check(label, ok, detail=""):
    if ok:
        print(f"  ok  {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL {label}\n       {detail}")


def _raises(fn):
    """True if calling fn() raises. Used where refusing IS the correct behaviour."""
    try:
        fn()
    except Exception:
        return True
    return False


print("unit settings: address table vs the Device page")

# THE CHECK THIS FILE WAS WRITTEN FOR.
off = server.unit_settings_off_page()
check("every writable unit setting is on the page or deliberately off it",
      not off,
      "these are writable but neither on the Device page nor listed in "
      f"UNIT_SETTINGS_NOT_ON_PAGE, so nobody has decided about them: {off}")

# The reverse direction is asserted at import in server.py, but assert it here too so a failure
# names itself instead of arriving as an ImportError.
missing_sel = sorted(p for p in server.UNIT_SETTING_BY_PROP
                     if p not in rcp_transfer.UNIT_SETTING_SEL)
check("every setting on the page has an address", not missing_sel,
      f"on the page with no way to write them: {missing_sel}")

# An exemption for something that is not writable at all is a leftover, and a leftover reason is
# worse than none: it implies a decision was made about a setting that no longer exists.
stale = sorted(p for p in server.UNIT_SETTINGS_NOT_ON_PAGE
               if p not in rcp_transfer.UNIT_SETTING_SEL)
check("no exemption names a setting that is not writable", not stale,
      f"listed as deliberately off the page but not in the address table: {stale}")

# A setting cannot be both on the page and excluded from it.
both = sorted(p for p in server.UNIT_SETTINGS_NOT_ON_PAGE if p in server.UNIT_SETTING_BY_PROP)
check("nothing is both on the page and excluded from it", not both, f"in both: {both}")

# Every exemption has to say WHY, in a sentence, or it is just a silencer.
thin = sorted(p for p, why in server.UNIT_SETTINGS_NOT_ON_PAGE.items() if len(why.strip()) < 30)
check("every exemption gives a real reason", not thin, f"reason too thin to be one: {thin}")

print("\nlookups that could match inside a longer property name")

# rcp_read.find_all searches the dump for a property name as RAW TEXT, so a name that is a PREFIX of
# another real property matches both and takes whichever comes first in the stream. That is how
# polarity once always read off ("inputPhase" inside "inputPhaseFlip"), and on it
# was found lying in wait on the two auto-mutes: "outputMonAutoMute" also matches inside
# "outputMonAutoMuteActive", the live ducking flag. It worked only because of stream order.
#
# The names below are the ones the app looks up that way. Any name that is a prefix of another real
# property must NOT be in this list: read it by block and exact name instead.
import re, pathlib
src = pathlib.Path(__file__).resolve().parents[1].joinpath("Project", "server.py")
looked_up = sorted(set(re.findall(r'first\("([A-Za-z][A-Za-z0-9_]+)"\)',
                                  src.read_text(encoding="utf-8"))))

# The full property list, from the GENERATED docs/PROPERTY_LIST_<DESK>.md, so this needs
# no device. Do not point it at a frozen appendix in RODECASTER_PROTOCOL.txt: one written by the
# old flat walk carries that reader's defects.
#
# DELIBERATELY NOT DEVICE-AWARE: this check wants the NAME universe, and the two
# desks publish identical names (463 each, measured). Only the occurrence COUNTS differ, and nothing
# here reads a count. So it takes whichever desk's list is present, which also means it still runs
# on a machine that has only ever seen one of the two desks.
_lists = sorted(pathlib.Path(_bootstrap.docs_path())
                .glob("PROPERTY_LIST_*.md"))
check("a generated property list exists", bool(_lists),
      "none found - regenerate with DevTools/property_list.py")
appendix = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in _lists)
known = set(re.findall(r"^- `([a-z][A-Za-z0-9_]{3,})` - ", appendix, re.M))
check("the generated property list was readable", len(known) > 300,
      f"only found {len(known)} names - regenerate with DevTools/property_list.py")

risky = []
for n in looked_up:
    longer = sorted(o for o in known if o != n and o.startswith(n))
    if longer:
        risky.append(f"{n} also matches {longer}")
check("no raw-text lookup is a prefix of another real property", not risky,
      "read these by block and exact name instead: " + "; ".join(risky))

print("\nthe address is a tree path, and there is one builder for it")

import model_tree

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
    check(f"{label} is path {[hex(x) for x in path]}",
          model_tree.encode_path(path).hex() == captured,
          f"built {model_tree.encode_path(path).hex()} against captured {captured}")

# The builders must still emit those bytes. rcp_transfer._selftest checks the whole records against
# the captures; this checks the ADDRESS part specifically, so a regression here names itself.
check("build_global_prop_int addresses by path",
      rcp_transfer.build_global_prop_int("x", 0x1C, 0)[5:10]
      == model_tree.encode_path([0x1C]), "global address drifted")
check("build_channel_prop_int addresses by path",
      rcp_transfer.build_channel_prop_int("x", 0, 0x6E, 0x02)[5:11]
      == model_tree.encode_path([0x026E]), "channel address drifted")
check("build_pad_prop_int addresses by path",
      rcp_transfer.build_pad_prop_int("padIdx", 15, 0x01)[5:13]
      == rcp_transfer.PAD_ADDRESS, "pad address no longer matches the captured constant")

# ZERO INDEX: we emit `01 00`, NOT the zero-length form. Both are legal by the grammar and the desk
# takes both, but our pad writes have always used `01 00` and are proven on hardware. Changing
# working write bytes to match an untested capture is risk for nothing, on the one address family
# where a bad write appends a stray that only a factory reset clears.
check("index zero is emitted as one byte, as our proven pad writes do",
      model_tree.encode_path([0x29A, 0]).hex() == "010102029a020100",
      model_tree.encode_path([0x29A, 0]).hex())
check("and that is exactly what the pad builder sends",
      rcp_transfer.build_pad_prop_int("padIdx", 0, 0x00)[5:13].hex() == "010102029a020100",
      rcp_transfer.build_pad_prop_int("padIdx", 0, 0x00)[5:13].hex())

check("a negative index is refused rather than encoded",
      _raises(lambda: model_tree.encode_path([-1])), True)
# A two-byte index must not silently truncate: 0x26E is the difference between a mic and something
# else entirely.
check("an index over 255 takes two bytes",
      model_tree.encode_path([0x026E]).hex() == "010101026e02",
      model_tree.encode_path([0x026E]).hex())

print("\nunit settings: what a profile does with them")

# The four that drifted are all SAVED by a profile; two are deliberately never written back. Assert
# that pairing, because "saved but never restored" is the failure mode that looks like a backup and
# is not one, and the reason it is acceptable here (a restored mute silences someone mid-show) has
# to stay attached to the decision.
import profiles
never = profiles.UNIT_NEVER_RESTORE
check("the never-restore list only names writable settings",
      all(p in rcp_transfer.UNIT_SETTING_SEL for p in never), f"{sorted(never)}")
check("and it is only the mutes, which is the whole justification for it",
      set(never) == {"outputMonMute", "outputBTMute"},
      f"if this changed, the reason written in profiles.py may no longer cover it: {sorted(never)}")

print("\nstereo-linked pairs: nameable, never assignable")

# THE load-bearing separation. A linked pair is its own source code, so it can sit in the fader
# vector and must be nameable - but it must never be something the UI offers to place on a fader,
# and never a gap-fill candidate for the layout tidy. If a pair code reached `code_set`, the tidy
# would hand "Mic 1 + 2" to a spare strip while Mic 1 and Mic 2 were also assignable, and a mic
# would end up both linked and on its own fader.
import devices as _dev
for _d in _dev.DEVICES.values():
    _f = _d.fader
    check(f"{_d.name}: pair codes stay OUT of code_set",
          not (set(_f.pair_map) & set(_f.code_set)),
          f"{sorted(set(_f.pair_map) & set(_f.code_set))}")
    check(f"{_d.name}: but every pair code IS nameable",
          all(c in _f.all_labels for c in _f.pair_map), f"{sorted(_f.pair_map)}")
    check(f"{_d.name}: every pair's two members are assignable channels",
          all(lo in _f.code_set and up in _f.code_set for lo, up in _f.pair_map.values()),
          f"{_f.pair_map}")
    check(f"{_d.name}: no pair code collides with an assignable one",
          not (set(_f.pair_map) & set(c for c, _l in _f.assignable)))
    # A device with the cap but no table (or the reverse) is a half-wired feature; the gate checks
    # both at runtime, and this says so at build time.
    check(f"{_d.name}: the stereo_link cap and the pair table agree",
          ("stereo_link" in _d.caps) == bool(_f.stereo_pairs),
          f"cap={'stereo_link' in _d.caps} pairs={bool(_f.stereo_pairs)}")

# The pair codes are protocol facts, so they must match the one table that records them.
check("the pair codes match rcp_transfer.STEREO_LINK_PAIRS",
      _dev.PRO2.fader.pair_map == rcp_transfer.STEREO_LINK_PAIRS,
      f"{_dev.PRO2.fader.pair_map} vs {rcp_transfer.STEREO_LINK_PAIRS}")


# ---------------------------------------------------------------------------
# ONE VOICE PER OUTCOME: a live region and announce() must never carry the same words
# ---------------------------------------------------------------------------
# A `role="status"` element and `announce()` are both polite live regions. Write the same message
# into both in one tick and a screen reader says it twice or drops one, which is why the guide
# forbids it. The pairing is invisible on screen and easy to reintroduce by copying a handler, so
# it is asserted here rather than left to a browser pass.
#
# WHAT IS CHECKED IS THE PAIR, not either half on its own. A live region written by itself is fine
# and is how most sections speak; `announce()` by itself is fine too. It is writing the SAME words
# to both, in the same tick, that produces the fault.
#
# `setStatus` is the helper for these elements and it clears before it writes, so a repeated message
# is still spoken. That is the half a bare `setText` into a live region loses, which is why the
# converted sections use it - but converting the other twenty-odd is a job of its own and NOT
# asserted here, or this check would fail on code that is merely old rather than wrong.
import pathlib as _pl
import re as _re

_static = _pl.Path(_bootstrap.repo_path("Project", "static"))
_html = (_static / "index.html").read_text(encoding="utf-8")
_js = (_static / "app.js").read_text(encoding="utf-8")

# Every id in the markup that is its own live region.
_live_ids = set(_re.findall(r'id="([\w-]+)"[^>]*\brole="status"', _html))
_live_ids |= set(_re.findall(r'id="([\w-]+)"[^>]*\baria-live=', _html))
check("index.html declares some live-region status lines", len(_live_ids) > 5,
      f"found {len(_live_ids)}")

_paired = []
for _m in _re.finditer(
        r'set(?:Text|Status)\("([\w-]+)",\s*([^;]+?)\);\s*\n\s*announce\(([^;]+?)\);', _js):
    said, again = _m.group(2).strip(), _m.group(3)
    head = said.rstrip(")").split(",")[0].strip()
    if _m.group(1) in _live_ids and head and head in again:
        _paired.append(_m.group(1))
check("no live region is announced a second time with the same words",
      not _paired, f"double-announced: {sorted(set(_paired))}")

# These three sections are the ones that repeat a message, so they must write through the clearing
# helper rather than a bare setText.
for _id in ("fader-levels-status", "reset-outputs-status", "stereo-status"):
    check(f"{_id} is written only through setStatus", f'setText("{_id}"' not in _js,
          "a bare setText here goes silent when the same message repeats")


# --- A DESK IS IDENTIFIED BY ITS SERIAL, NOT BY AN ID THAT MOVES -----------------------------
# The product id encodes the USB audio configuration, so a desk in any non-default `outputMultiMode`
# presents an id no table holds (a Pro II reads 0x0037, 0x0078, 0x0092 or 0x0094 by mode). Resolving
# on the id alone then falls through to DEFAULT, which IS a Pro II - so the fault is invisible on a
# Pro II and hands a DUO the wrong matrix shape and channel layout. Checked here rather than on
# hardware because only the Duo half is wrong and no Duo is attached.
import devices                                                          # noqa: E402

print("\nIDENTIFYING A DESK WHOSE PRODUCT ID IS NOT IN THE TABLE")


class _Iface:
    def __init__(self, product_id, serial):
        self.product_id, self.serial_number = product_id, serial
        self.interface_number = 9


_MODE5 = _Iface(0x0094, "TEST-DUO-1")        # an Expanded desk: a real measured id, absent from DEVICES
_saved_learned = dict(devices._LEARNED)
devices._LEARNED.clear()
try:
    check("a mode-5 product id is genuinely unknown, or this test proves nothing",
          0x0094 not in devices.DEVICES)
    check("and such an interface is flagged for identifying", devices.unidentified(_MODE5))
    check("a DUO says so in its own dump, whatever id it arrived under",
          devices.note_system_name(_MODE5, "RODECaster Duo") is devices.DUO)
    check("which is remembered against the SERIAL, the one thing that does not change",
          devices._LEARNED.get("TEST-DUO-1") is devices.DUO)
    check("so it is not asked again", not devices.unidentified(_MODE5))
    check("a name matching neither desk teaches nothing, rather than teaching a guess",
          devices.note_system_name(_Iface(0x0094, "TEST-OTHER"), "Some Other Box") is None
          and "TEST-OTHER" not in devices._LEARNED)
    check("a known id still wins without any dump at all",
          devices.resolve_device(devices.PID_DUO) is devices.DUO
          and devices.resolve_device(devices.PID_PRO2) is devices.PRO2)
    # The consequence, stated as the thing that actually breaks: a Duo given a Pro II's spec has
    # the wrong number of faders and the wrong matrix, and every read and write follows that.
    check("the two desks really do differ, so misidentifying one matters",
          devices.DUO.fader.count != devices.PRO2.fader.count
          or devices.DUO.matrix.output_count != devices.PRO2.matrix.output_count)
finally:
    devices._LEARNED.clear()
    devices._LEARNED.update(_saved_learned)


# --- EXPANDED'S FOUR SOURCES: NAMED ALWAYS, ASSIGNABLE ONLY IN EXPANDED ----------------------
# Codes 12-15 are RODE's Virtual Devices, measured on hardware. The split matters both ways:
# offering them in Standard puts a permanently silent input on a fader, and refusing to NAME them
# there leaves the announcer saying "0 percent, Source 12" about a fader that still holds one.
print("\nTHE EXPANDED-ONLY SOURCES")
for _dev in (devices.PRO2, devices.DUO):
    _f = _dev.fader
    _codes = [c for c, _l in _f.expanded_sources]
    check(f"{_dev.name}: it has the four", _codes == [12, 13, 14, 15], _codes)
    check(f"{_dev.name}: named even in Standard, or the announcer says a bare number",
          all(_f.labels_for(False).get(c) for c in _codes))
    check(f"{_dev.name}: not assignable in Standard",
          not any(c in _f.code_set_for(False) for c in _codes))
    check(f"{_dev.name}: assignable in Expanded",
          all(c in _f.code_set_for(True) for c in _codes))
    check(f"{_dev.name}: the dropdown order and the write gate agree",
          set(_f.code_order_for(True)) == set(_f.code_set_for(True)))
    check(f"{_dev.name}: a stereo PAIR code is still never assignable",
          not any(c in _f.code_set_for(True) for c, _lo, _up, _l in _f.stereo_pairs))
check("USB 1 is renamed in Expanded, since Main Stereo is not what the computer sends there",
      devices.PRO2.fader.labels_for(True)[7] != devices.PRO2.fader.labels_for(False)[7])
check("and a relabel never changes what may be written",
      devices.PRO2.fader.code_set_for(True) >= devices.PRO2.fader.code_set_for(False))

print(f"\n{'ALL SPEC CHECKS PASSED' if not FAILED else str(len(FAILED)) + ' CHECK(S) FAILED'}")
raise SystemExit(1 if FAILED else 0)
