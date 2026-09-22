"""LIVE exercise of every capability the attached desk already has ON (Duo plan A5).

Each check writes ONE value, diffs the WHOLE tree to see everything that moved, restores, and diffs
again. Nothing is inferred: if a write touches something we did not expect, the diff says so.

CONFIRMED SELECTORS ONLY, where CONFIRMED now includes "the desk's own tree says so". A write to an
unmapped selector can append a stray that only a factory reset clears, so every address here either
came from a capture or resolves, in this dump, to a node of the right block carrying the property.
That second route is what let the unit-settings block be enabled on a Duo with no capture behind it.

WHY "OCCURRENCE COUNTS UNCHANGED" IS NOT THE TEST: on a Duo the first write to an input property
CREATES it, and writing the old value back does not remove it (measured). So an ADDED property at
the address we just wrote is correct behaviour.
An added property anywhere ELSE, or a changed value we did not ask for, is not.

NOTHING HERE WAITS A FIXED TIME FOR THE DESK. See `settled`.

NEVER add disableAllPhysicalButtons, disableAllHeadphoneOutputs or disableAllLineoutOutputs. They
take the desk's buttons or its outputs away, and this suite is meant to run with nobody there.

Run: python DevTools/test_duo_caps_live.py [--only NAME]
"""
from __future__ import annotations
import sys
import time

import _bootstrap  # noqa: F401
import channels as c
import devices
import model_tree
import rcp_read as rr

FAILED = 0
NOTES: list[str] = []
ONLY = None
if "--only" in sys.argv:
    ONLY = sys.argv[sys.argv.index("--only") + 1]


def snapshot():
    blob = rr.read_model_stable(fresh=True)
    root, _u, _t = model_tree.parse(blob)
    out = {}
    for n in model_tree.walk(root):
        p = tuple(n.path())
        for name, val, _k in n.props:
            out[(p, n.name, name)] = val
    return blob, out


def settled(timeout=12.0):
    """A snapshot taken once the WHOLE TREE has stopped moving, not after a fixed sleep.

    `read_model_stable` only guarantees the DUMP is self-consistent; it says nothing about the desk
    having finished reacting. A write can land in one property immediately and in the properties it
    drags with it a moment later (muting a channel also mutes 13 cross-points; fixing the monitor
    level also clears the encoder colour), and a pad create/clear moves dozens over a second or two.
    A fixed `time.sleep` is therefore a race, and it presents as flakiness: a check passes on its
    own and fails about one run in three in sequence, always somewhere different. Polling until two
    consecutive snapshots agree is deterministic AND usually faster than the worst-case sleep.
    """
    deadline = time.time() + timeout
    blob, prev = snapshot()
    while time.time() < deadline:
        blob, cur = snapshot()
        if cur == prev:
            return blob, cur
        prev = cur
    print(f"  note the desk was still changing after {timeout:.0f}s; using the last reading")
    return blob, prev


def _fmt(k):
    return f"[{','.join(hex(i) for i in k[0])}] {k[1]}.{k[2]}"


# DERIVED LIVE STATE: not settings, so they are not part of "did it restore".
#
# `encoderColour` is what the encoder's ring is lit for. It goes to 7 when the encoder becomes live
# on the monitor level and DECAYS TO -1 AFTER ABOUT 30 SECONDS of nothing touching the desk, timed
# on a Duo. `systemChannelSelected` stays -1 throughout, so it is NOT tracking the
# selected channel. Setting outputMonFixed takes the encoder's job away and blanks it immediately.
#
# TREATING IT AS A SETTING MAKES THE WHOLE SUITE LOOK FLAKY: a different check fails on nearly every
# run, always on this one property. The write ORDER is identical run to run, so the writes cannot
# explain it. What varies is the CLOCK: the suite takes minutes, the timer is 30 seconds, so it
# expires inside whichever check happens to be running.
#
# WHY it decays is not worth chasing - it sets itself either way. The likely answer is the
# touchscreen returning to its home screen on a timeout; systemMainMode stays 0 and nothing else in
# the model moves, but a transient overlay need not be exposed. Derived either way.
#
# Add to this set only with that kind of evidence. A value that is merely INCONVENIENT is a finding,
# not a volatile.
DERIVED = {"encoderColour"}


def diff(a, b, ignore=()):
    """(added, removed, changed) ignoring derived live state - see DERIVED - and `ignore`.

    `ignore` is per-probe, and is for a WRITE-ONLY TRIGGER this particular write is known to move.
    It is deliberately not DERIVED: a global entry would hide the same property changing anywhere
    else in the suite, which is exactly the evidence we would want to see.
    """
    skip = DERIVED | set(ignore)
    return ([k for k in b if k not in a and k[2] not in skip],
            [k for k in a if k not in b and k[2] not in skip],
            [k for k in a if k in b and a[k] != b[k] and k[2] not in skip])


def probe(label, expect_at, write, test_val, restore_val, expect_extra=0, ignore=(),
          restore_extra=None, after_write=None):
    """Write, diff the whole tree, restore, diff again.

    `expect_at` is the (path, block, property) we expect to move, or None to just report.
    `expect_extra` is how many OTHER properties the desk is allowed to move of its own accord
    (a mic type, for instance, also moves the gain - that is the desk, not us).
    `ignore` is a set of property NAMES this write is known to move and cannot put back.
    `restore_extra(before)` runs after the restore write, for a write the desk DRAGS other settings
    along with: writing the value back restores the value, not the drag. It is given the baseline
    snapshot and is expected to put the rest back by name.
    `after_write(after)` ASSERTS THE ENGINE'S OWN RESPONSE, given the settled post-write snapshot.

    THAT LAST HOOK IS THE POINT OF THIS FILE NOW. Everything else here asks "did the value land at
    its own address", which a dead address answers just as happily as a live one - that is exactly
    how a Duo's mic gain passed this suite for two months while changing no audio. Where the desk
    DERIVES something from a write, assert the derived thing: it is the only part a wrong address
    cannot fake.
    """
    global FAILED
    if ONLY and ONLY not in label:
        return
    print(f"\n=== {label} ===")
    b0, before = snapshot()
    print(f"  baseline {_fmt(expect_at) if expect_at else '(no single address)'}"
          f" = {before.get(expect_at)!r}" if expect_at else "  baseline taken")
    try:
        write(test_val)
    except Exception as e:
        FAILED += 1
        print(f"  FAIL the write raised: {type(e).__name__}: {e}")
        return
    b1, after = settled()
    added, removed, changed = diff(before, after, ignore)
    # An ignored property is REPORTED, never hidden: the whole point of diffing the whole tree is
    # that a write which touches something says so. `ignore` only stops it FAILING the restore.
    for k in sorted({k for k in before if k in after and before[k] != after[k]
                     and k[2] in set(ignore)}, key=str):
        print(f"    (ignored) {_fmt(k)}: {before[k]!r} -> {after[k]!r}")
    for k in added:
        print(f"    ADDED   {_fmt(k)} = {after[k]!r}")
    for k in removed:
        print(f"    REMOVED {_fmt(k)} = {before[k]!r}")
    for k in changed:
        print(f"    CHANGED {_fmt(k)}: {before[k]!r} -> {after[k]!r}")

    if expect_at is not None:
        got = after.get(expect_at)
        if got == test_val:
            print(f"  ok   the value landed at its own address")
        else:
            FAILED += 1
            print(f"  FAIL expected {test_val!r} at {_fmt(expect_at)}, got {got!r}")
        touched = len(added) + len(removed) + len(changed)
        if touched > 1 + expect_extra:
            NOTES.append(f"{label}: touched {touched} properties, expected {1 + expect_extra}")
            print(f"  note the write moved {touched} properties (expected {1 + expect_extra})")

    if after_write is not None:
        after_write(after)

    try:
        write(restore_val)
    except Exception as e:
        FAILED += 1
        print(f"  FAIL the RESTORE raised: {type(e).__name__}: {e}")
        return
    if restore_extra is not None:
        restore_extra(before)
    b2, back = settled()
    a2, r2, c2 = diff(before, back, ignore)
    if not (a2 or r2 or c2):
        print(f"  ok   restored, tree identical ({len(b2)} bytes)")
    elif not c2 and not r2:
        # Properties that did not exist before and now do, at their original values: the Duo's
        # create-on-first-write. Behaviour is restored even though the tree is one property longer.
        print(f"  ok   restored; {len(a2)} property(ies) newly EXIST (create-on-first-write):")
        for k in a2:
            print(f"         {_fmt(k)} = {back[k]!r}")
    else:
        FAILED += 1
        print(f"  FAIL not restored:")
        for k in c2:
            print(f"         CHANGED {_fmt(k)}: {before[k]!r} -> {back[k]!r}")
        for k in r2:
            print(f"         REMOVED {_fmt(k)} = {before[k]!r}")


dev = devices.get_active_device()
print(f"device: {dev.name}   caps ON: {', '.join(sorted(dev.caps))}")
blob0, _ = snapshot()
print(f"model: {len(blob0)} bytes")

mics = [e for e in dev.ui_channels if e.get("kind") == "mic"]
lines = [e for e in dev.ui_channels if e.get("kind") == "line"]
mic = mics[-1]                      # the highest-numbered mic
line = lines[0]
mic_n = c.mic_channel_number(mic)
mic_path = tuple(dev.input_path(mic))
line_path = tuple(dev.input_path(line))

# ---- input_mic_write -------------------------------------------------------------------------
if "input_mic_write" in dev.caps:
    cur = c.input_value(blob0, mic, "inputMicrophoneGain", dev)
    probe(f"input_mic_write: {mic['label']} gain",
          (mic_path, "INPUTSOURCE", "inputMicrophoneGain"),
          lambda v: c.set_mic_gain(mic_n, v), 33 if cur != 33 else 34, cur)

    cur = c.input_value(blob0, mic, "inputPhaseFlip", dev)
    probe(f"input_mic_write: {mic['label']} polarity",
          (mic_path, "INPUTSOURCE", "inputPhaseFlip"),
          lambda v: c.set_polarity(mic_n, v), not bool(cur), bool(cur))

    cur = c.input_value(blob0, line, "inputDigitalGain", dev)
    probe(f"input_mic_write: {line['label']} level",
          (line_path, "INPUTSOURCE", "inputDigitalGain"),
          lambda v: c.set_channel_level(line["name"], v), (cur or 0) - 6, cur)

# ---- processing ------------------------------------------------------------------------------
if "processing" in dev.caps:
    strip = c._strip_index(mic)
    proc_path = (dev.proc_sel_base + strip,)
    blob, snap = snapshot()
    cur = snap.get((proc_path, "CHANNEL", "hpfOn"))
    probe(f"processing: {mic['label']} high-pass filter on/off (strip {strip})",
          (proc_path, "CHANNEL", "hpfOn"),
          lambda v: c.set_processing_module(mic_n, "hpf", v), not bool(cur), bool(cur))

    cur = snap.get((proc_path, "CHANNEL", "eqLowGain"))
    if cur is not None:
        probe(f"processing: {mic['label']} EQ low gain (a float param)",
              (proc_path, "CHANNEL", "eqLowGain"),
              lambda v: c.set_channel_processing(mic_n, "eqLowGain", v),
              0.75 if abs(cur - 0.75) > 0.01 else 0.25, cur)

    cur = snap.get((proc_path, "CHANNEL", c.MUTE_PROP))
    probe(f"processing: {mic['label']} mute (strip {strip})",
          (proc_path, "CHANNEL", c.MUTE_PROP),
          lambda v: c.set_channel_mute(mic["name"], v), not bool(cur), bool(cur))

    # THE WIRELESS MIC'S OWN MUTE. Same CHANNEL node as the two above, so what is worth asserting is
    # the DIFFERENCE: the channel mute drags thirteen matrix cross-points with it, and this one must
    # drag NOTHING (expect_extra stays 0). A write that moved the matrix would mean we had reached
    # the channel mute by another name. It cannot be ear-proven here - nobody on the project owns a
    # wireless mic - so this proves the address and the absence of side effects, and no more.
    cur = snap.get((proc_path, "CHANNEL", c.WIRELESS_MUTE_PROP))
    probe(f"processing: {mic['label']} WIRELESS mic mute (strip {strip}, must move nothing else)",
          (proc_path, "CHANNEL", c.WIRELESS_MUTE_PROP),
          lambda v: c.set_channel_wireless_mute(mic["name"], v), not bool(cur), bool(cur))

    # THE SMART PADS BUS, and this one asks THE ENGINE. Its mute is the same record on the same
    # selector family as the mic's above, so "the value landed" proves nothing the mic has not
    # already proved. What does prove something is what the firmware then does UNASKED: it mutes
    # every matrix cross-point of that SOURCE, which it only does for a channel it really mixes.
    # Worth asserting here rather than on the mic because the bus is the strip a Duo owner drives
    # from a PHYSICAL fader, and it is easy to leave out of the Duo's read map.
    pads = next((e for e in dev.ui_channels if e.get("kind") == "bus"), None)
    if pads is not None:
        import rcp_transfer as _rt
        pads_strip = c._strip_index(pads)
        pads_path = (dev.proc_sel_base + pads_strip,)
        pads_row = [(_rt.cross_point_selector(o, pads["source_code"]),)
                    for o in range(dev.matrix.output_count)]
        cur = snap.get((pads_path, "CHANNEL", c.MUTE_PROP))

        def pads_engine(after, _want=not bool(cur)):
            global FAILED
            got = [after.get((p, "MIX", "mixMute")) for p in pads_row]
            ok = all(v is _want for v in got)
            print(f"  {'ok  ' if ok else 'FAIL'} the desk moved all {len(pads_row)} cross-points of "
                  f"source {pads['source_code']} to {_want}: {got}")
            FAILED += 0 if ok else 1

        # An already-muted bus would make the assertion meaningless: the cross-points UNmuting back
        # to their resting state is what they do anyway, so it would pass on a dead address too.
        probe(f"processing: {pads['label']} mute (strip {pads_strip}) - the ENGINE answers",
              (pads_path, "CHANNEL", c.MUTE_PROP),
              lambda v: c.set_channel_mute(pads["name"], v), not bool(cur), bool(cur),
              expect_extra=dev.matrix.output_count,
              after_write=(pads_engine if not cur else None))
        if cur:
            print("  note the bus was already muted, so the cross-point assertion was skipped")

    # THE MIC TYPE MOVES THE GAIN **AND THE WHOLE PROCESSING CHAIN**, firmware-side. Each voicing
    # carries its own chain, so selecting one overwrites the strip's params, and WRITING THE OLD TYPE
    # BACK DOES NOT UNDO IT - the desk applies a voicing on the way in and leaves the params alone on
    # the way out. Measured on a Pro II: PodMic -> Dynamic moved 13 params (Depth,
    # Sparkle, Punch, compressor threshold + gain, de-esser, gate threshold + range, the three EQ
    # gains, both Aphex) and every one was still at the Dynamic value after the type went back.
    # THE DUO DRAGS THE SAME THINGS THE PRO II DOES, so the gain and the chain both have to go back
    # BY NAME - that is what `restore_extra` is for. A weaker claim here ("a Duo mic moves the gain
    # only") passes for the worst possible reason: a write aimed at a node the audio engine never
    # reads drags nothing because the desk never notices it.
    def restore_voicing(before):
        # settled(), not snapshot(): the voicing lands over a second or so, and restoring against a
        # half-applied chain would leave whatever arrived after us.
        _b, now = settled()
        left = []
        for key, want in before.items():
            if now.get(key) == want:
                continue
            if key == (mic_path, "INPUTSOURCE", "inputMicrophoneGain"):
                c.set_mic_gain(mic_n, want)         # the voicing's default gain, put back
                time.sleep(0.08)
            elif key[0] == proc_path and key[1] == "CHANNEL" and key[2] in c.PRESET_FLOAT_PROPS:
                c.set_channel_processing(mic_n, key[2], want)
                time.sleep(0.08)
            elif key[0] == proc_path and key[1] == "CHANNEL":
                left.append(key[2])
        if left:
            # Not silently: a dragged property with no setter is a finding, not a tidy-up.
            print(f"  note the voicing moved {len(left)} property(ies) this cannot restore: {left}")

    cur = c.input_value(blob0, mic, "inputMicrophoneType", dev)
    probe(f"input_mic_write: {mic['label']} mic type (the desk also moves the gain and the chain)",
          (mic_path, "INPUTSOURCE", "inputMicrophoneType"),
          lambda v: c.set_mic_type(mic_n, v), 2 if cur != 2 else 3, cur,
          expect_extra=14, restore_extra=restore_voicing)

# ---- fader_write -------------------------------------------------------------------------------
if "fader_write" in dev.caps:
    import rcp_transfer as t
    blob, _snap = snapshot()
    vec = rr.values(blob, "channelInputSource")
    print(f"\n=== fader_write: strip sources ===\n  layout now: {vec}")
    # Swap the sources on two strips and put them back. Uses the desk's own vector, so whatever
    # the layout is when this runs, it is restored to exactly that.
    if len(vec) >= 2:
        a_i, b_i = 0, 1
        orig_a, orig_b = vec[a_i], vec[b_i]

        def swap(swapped):
            lo, hi = (orig_b, orig_a) if swapped else (orig_a, orig_b)
            t.set_fader_source(a_i, lo)
            time.sleep(0.25)
            t.set_fader_source(b_i, hi)

        # sipSlotCallDisconnect DOES NOT COME BACK INSIDE THE RUN, and it is the desk, not us.
        # Measured on a Pro II over four runs: a fader-source write flips the second
        # element of the composite on all three SIPCALLSLOTS nodes (0x293/0x294/0x295), and it is back
        # at its resting value ([T,F], [T,T], [T,T]) by the start of the next run, so it is TRANSIENT -
        # the settle window is seconds and the revert takes longer than that. Ruled out as a
        # mis-addressed write of ours: the selector we write is 0x1c + fader, a CHANNEL block, the
        # layout reads back exact, no property is added anywhere, and a mic-gain write leaves all three
        # alone. It is a disconnect REQUEST composite and nothing in the app reads it. Exempted BY
        # NAME here rather than in DERIVED, so a SIP change during any other probe still fails.
        # THE CROSS-POINT LEVELS ARE ASSERTED, and they do restore: a source's level is its fader's
        # level, the swap carries it across and the swap back carries it home. If this probe ever
        # reports a level that did not come back, ASK WHETHER A PHYSICAL FADER WAS TOUCHED before
        # reading it as a bug - the level IS the pot, so a hand on the desk changes it mid-run and
        # every value in the diff is a multiple of 1/127, which is the tell.
        probe(f"fader_write: swap strip {a_i} ({orig_a}) and strip {b_i} ({orig_b})",
              None, swap, True, False, ignore={"sipSlotCallDisconnect"})
        after = rr.values(rr.read_model_stable(fresh=True), "channelInputSource")
        if after == vec:
            print(f"  ok   the fader layout is exactly as it was: {after}")
        else:
            FAILED += 1
            print(f"  FAIL layout not restored: {after} != {vec}")

# ---- stereo_link -------------------------------------------------------------------------------
if "stereo_link" in dev.caps and dev.fader.stereo_pairs:
    import rcp_transfer as t
    code, lower, upper, label = dev.fader.stereo_pairs[0]
    vec = list(rr.values(rr.read_model_stable(fresh=True), "channelInputSource"))
    print(f"\n=== stereo_link: {label} ===\n  layout now: {vec}")
    if code in vec:
        print(f"  skipped: {label} is already linked, so there is no round trip to make")
    elif lower not in vec:
        print(f"  skipped: source {lower} is not on a fader")
    else:
        seat = vec.index(lower)
        up_at = vec.index(upper) if upper in vec else None

        def link(on):
            """The desk's own sequence: vacate both mics, then seat the pair on the LOWER one's
            fader. Unlinking puts both mics back where they started, which is what makes this a
            round trip rather than the RODE app's one-way behaviour."""
            if on:
                if up_at is not None:
                    t.set_fader_source(up_at, -1)
                    time.sleep(0.25)
                t.set_fader_source(seat, -1)
                time.sleep(0.25)
                t.set_fader_source(seat, code)
            else:
                t.set_fader_source(seat, lower)
                time.sleep(0.25)
                if up_at is not None:
                    t.set_fader_source(up_at, upper)

        # WHAT THIS DOES AND DOES NOT PROVE. It proves the desk ACCEPTS the pair code and gives the
        # layout back byte-clean, which is what a user's undo depends on. It does NOT prove the two
        # mics are actually summed: the engine's model is device-agnostic (a Duo will just as
        # happily seat source 3, which is a Mic 4 it does not have), so only an ear can close that.
        probe(f"stereo_link: link and unlink {label}",
              None, link, True, False, ignore={"sipSlotCallDisconnect"})
        after = list(rr.values(rr.read_model_stable(fresh=True), "channelInputSource"))
        if after == vec:
            print(f"  ok   the fader layout is exactly as it was: {after}")
        else:
            FAILED += 1
            print(f"  FAIL layout not restored: {after} != {vec}")

# ---- level_write -------------------------------------------------------------------------------
if "level_write" in dev.caps:
    import rcp_transfer as t
    blob, _snap = snapshot()
    vec = rr.values(blob, "channelInputSource")

    # A VIRTUAL fader, and one that actually has a source on it - an empty strip (-1) moves no
    # cross-points, so it would prove only that the byte landed, which is the evidence this whole
    # suite is now suspicious of.
    idx = next((i for i in sorted(dev.fader.level_selectors)
                if i < len(vec) and vec[i] >= 0), None)
    if idx is None:
        print("\n=== level_write: SKIPPED, no virtual fader has a source on it ===")
    else:
        sel = dev.fader.level_selectors[idx]
        source = vec[idx]
        node = (0, sel)
        cur = _snap.get((node, "FADER", "faderLevel"))
        target = 40 if cur != 40 else 60
        # THE ENGINE IS THE WITNESS, not the read-back. A fader's level IS its source's cross-point
        # level, so the desk recomputes all 13 cells of that source's routing row by itself, to
        # value/127 exactly. That is the difference between "the byte landed" and "the audio moved",
        # and it is what proved these addresses on the Duo in the first place.
        want = f"{target / t.FADER_LEVEL_MAX:.6f}"
        row = [t.cross_point_selector(out, source) for out in range(dev.matrix.output_count)]

        def check_row(after):
            bad = [p for p in row
                   if not str(after.get(((p,), "MIX", "mixLevelWithAnchor"), "")).startswith(want)]
            if bad:
                globals()["FAILED"] += 1
                print(f"  FAIL the engine moved only {len(row) - len(bad)} of {len(row)} "
                      f"cross-points to {want}")
            else:
                print(f"  ok   the ENGINE moved all {len(row)} of source {source}'s cross-points "
                      f"to {want} ({target}/127) on its own")

        probe(f"level_write: virtual fader {idx} (source {source}) at [0,0x{sel:02x}]",
              (node, "FADER", "faderLevel"),
              lambda v: t.set_fader_level(sel, v), target, cur,
              expect_extra=dev.matrix.output_count, after_write=check_row)

    mon = [v for v in rr.values(rr.read_model_stable(fresh=True), "outputMonLevel")
           if isinstance(v, float)]
    if mon:
        cur = mon[0]
        probe("level_write: monitor speaker level (outputMonLevel on OUTPUT 0x0d)",
              ((0x0D,), "OUTPUT", "outputMonLevel"),
              lambda v: t.set_monitor_level(v), 0.25 if abs(cur - 0.25) > 0.01 else 0.5, cur)

# ---- matrix_write ------------------------------------------------------------------------------
if "matrix_write" in dev.caps and (not ONLY or "matrix" in ONLY):
    import rcp_transfer as t
    # THE OUTPUT MUST BE IN CUSTOM MODE or the cross-point write stores a value that changes no
    # routing. Output 1 is Headphone 2, which has nothing plugged into it on this desk.
    src_i, out_i = mic["source_code"], 1
    sel = t.cross_point_selector(out_i, src_i)
    print(f"\n=== matrix_write: source {src_i} -> output {out_i}, selector {sel:#04x} ===")
    b0, base = snapshot()
    mode0 = rr.read_output_modes(b0)[out_i]
    print(f"  output mode was {mode0} (0 Main Mix, 1 Mix Minus, 2 Custom)")
    try:
        t.set_output_routing(out_i, 2)
        time.sleep(0.8)
        t.set_cross_point_excluded(out_i, src_i, True)
        time.sleep(0.8)
        _b, s = settled()
        ok = (s.get(((sel,), "MIX", "mixDisabled")) is True
              and s.get(((sel,), "MIX", "mixLink")) is False
              and s.get(((sel,), "MIX", "mixMute")) is True)
        print(f"  {'ok  ' if ok else 'FAIL'} excluding sets mixDisabled, clears mixLink, sets mixMute")
        FAILED += 0 if ok else 1
        t.set_cross_point_excluded(out_i, src_i, False)
        time.sleep(0.8)
    finally:
        t.set_output_routing(out_i, mode0)
    _b, back = settled()
    added, removed, changed = diff(base, back)
    # THE TWO REQUEST TRIGGERS DO NOT COME BACK, and that is the desk, not us. mixLinkRequest and
    # mixUnlinkRequest are write-only triggers; the desk answers our composite payload (which is
    # byte-for-byte what the RODE app sends) by storing a CLEARED composite in the same slot. So
    # their decoded KIND changes bool -> pair on any cross-point you touch, permanently, and going
    # back to Main Mix does not undo it. Nothing in the app reads either property, and every value
    # that means something (mixDisabled, mixLink, mixMute, the level) restores exactly.
    triggers = {k for k in changed if k[2] in ("mixLinkRequest", "mixUnlinkRequest")}
    real = [k for k in changed if k not in triggers]
    if not added and not removed and not real:
        print(f"  ok   every routing value restored; {len(triggers)} request trigger(s) left as a "
              f"cleared composite, which is the desk's own bookkeeping")
    else:
        FAILED += 1
        print("  FAIL something other than the request triggers did not restore:")
        for k in real:
            print(f"         CHANGED {_fmt(k)}: {base[k]!r} -> {back[k]!r}")
        for k in added:
            print(f"         ADDED   {_fmt(k)} = {back[k]!r}")
        for k in removed:
            print(f"         REMOVED {_fmt(k)} = {base[k]!r}")

# ---- matrix_write: the cross-point LEVEL ------------------------------------------------------
if "matrix_write" in dev.caps and (not ONLY or "level" in ONLY):
    import rcp_transfer as t
    src_i, out_i = mic["source_code"], 1
    sel = t.cross_point_selector(out_i, src_i)
    key = ((sel,), "MIX", "mixLevelWithAnchor")
    print(f"\n=== matrix_write: cross-point LEVEL, selector {sel:#04x} ===")
    b0, base = snapshot()
    mode0 = rr.read_output_modes(b0)[out_i]
    # A level is only writable on an UNLINKED cross-point in a CUSTOM output. Relinking afterwards
    # is what restores it: a linked cell follows the fader, and reads back level == anchor.
    try:
        t.set_output_routing(out_i, 2)
        time.sleep(0.8)
        t.send_record(t.build_mix_link_request(sel, link=False))
        time.sleep(0.8)
        t.set_cross_point_level(out_i, src_i, 0.25)
        time.sleep(0.8)
        _b, s = settled()
        got = s.get(key)
        ok = bool(got) and got.startswith("0.25")
        print(f"  {'ok  ' if ok else 'FAIL'} the level landed: {got!r}")
        FAILED += 0 if ok else 1
        lv = rr.read_matrix_levels()
        cellv = ((lv.get("levels") or {}).get(out_i) or {}).get(src_i) or {}
        ok2 = abs((cellv.get("level") or 0) - 0.25) < 0.01 and cellv.get("linked") is False
        print(f"  {'ok  ' if ok2 else 'FAIL'} the app's own reader agrees: {cellv}")
        FAILED += 0 if ok2 else 1
    finally:
        t.send_record(t.build_mix_link_request(sel, link=True))
        time.sleep(0.8)
        t.set_output_routing(out_i, mode0)
    _b, back = settled()
    added, removed, changed = diff(base, back)
    real = [k for k in changed if k[2] not in ("mixLinkRequest", "mixUnlinkRequest")]
    if not added and not removed and not real:
        print("  ok   relinking restored the level exactly")
    else:
        FAILED += 1
        for k in real:
            print(f"  FAIL {_fmt(k)}: {base[k]!r} -> {back[k]!r}")

# ---- pad_write ---------------------------------------------------------------------------------
if "pad_write" in dev.caps and (not ONLY or "pad" in ONLY):
    import soundpad as sp
    # A MIDI TRIGGER pad: no audio file and nothing in region B, so it cannot orphan a pool object.
    # That makes it the cheapest honest exercise of the create/clear path. Uses the first empty grid
    # slot the desk offers, so it never displaces a pad somebody put there.
    print("\n=== pad_write: create and clear a pad ===")
    b0, base = snapshot()
    health0 = sp.pool_health()
    pos0 = sp.pad_positions(b0)
    grid = sp.find_empty_grid()
    print(f"  {len(pos0)} pad records, pool clean = {health0['clean']}, first empty grid = {grid}")
    if grid is None:
        print("  skipped: no empty grid slot")
    else:
        res = sp.create_trigger(grid, name="A5 Test")
        b1, s1 = settled()
        pos1 = sp.pad_positions(b1)
        made = [p for p in pos1 if p not in pos0]
        ok = res.get("ok") and len(pos1) == len(pos0) + 1 and len(made) == 1
        print(f"  {'ok  ' if ok else 'FAIL'} created: {made}")
        FAILED += 0 if ok else 1
        added = [k for k in s1 if k not in base]
        node = {k[0] for k in added}
        print(f"  the new pad added {len(added)} properties on {len(node)} node(s): "
              f"{[','.join(hex(i) for i in p) for p in node]}")

        res = sp.clear_grid_pad(grid)
        b2, back = settled()
        pos2 = sp.pad_positions(b2)
        health1 = sp.pool_health()
        a2, r2, c2 = diff(base, back)
        if not (a2 or r2 or c2) and pos2 == pos0 and health1["clean"]:
            print(f"  ok   cleared; tree identical, {len(pos2)} records, pool clean")
        else:
            FAILED += 1
            print(f"  FAIL after clear: +{len(a2)} -{len(r2)} ~{len(c2)}, "
                  f"records {len(pos0)}->{len(pos2)}, pool clean = {health1['clean']}")
            # WITH VALUES, always. A property name on its own says nothing about WHICH WAY a value
            # moved, and the direction is usually the only thing that identifies what happened.
            for k in a2[:10]:
                print(f"         ADDED   {_fmt(k)} = {back[k]!r}")
            for k in r2[:10]:
                print(f"         REMOVED {_fmt(k)} = {base[k]!r}")
            for k in c2[:10]:
                print(f"         CHANGED {_fmt(k)}: {base[k]!r} -> {back[k]!r}")

# ---- output_write ----------------------------------------------------------------------------
if "output_write" in dev.caps:
    import rcp_transfer as t
    blob, snap = snapshot()
    key = ((t.SEL_OUTPUT_GLOBAL,), "OUTPUT", "outputMonMute")
    cur = snap.get(key)
    if cur is not None:
        probe("output_write: monitor speaker mute",
              key, lambda v: t.set_unit_setting("outputMonMute", v), not bool(cur), bool(cur))
    else:
        print("\n=== output_write: monitor speaker mute ===\n  skipped: outputMonMute not in this dump")

# ---- unit_settings -----------------------------------------------------------------------------
if "unit_settings" in dev.caps and (not ONLY or "unit" in ONLY):
    import rcp_transfer as t
    # EVERY address here is confirmed by the desk's OWN TREE, not derived: the check below fails the
    # run if any selector stops resolving to a node that carries its property. That is what made this
    # safe to enable on a Duo without a capture. The three writes then cover all three blocks.
    print("\n=== unit_settings: every address resolves on this desk ===")
    b0, _ = snapshot()
    bad = []
    for prop, (sel, _kind) in sorted(t.UNIT_SETTING_SEL.items()):
        if rr.path_value(b0, [sel], prop, rr.MISSING) is rr.MISSING:
            bad.append(f"{prop} (sel {sel:#04x})")
    if bad:
        FAILED += 1
        print(f"  FAIL {len(bad)} setting(s) are NOT on this desk: {', '.join(bad)}")
    else:
        print(f"  ok   all {len(t.UNIT_SETTING_SEL)} resolve to a node that carries them")

    # outputMonFixed UNMUTES THE MONITOR SPEAKERS, and writing it back does not re-mute. Measured on
    # a Pro II. It is the audible half of what the DERIVED note already says about
    # this property taking the encoder's job away, and it matters to a user, not just to this suite:
    # switching the monitor output to fixed level opens the speakers up. Put the mute back by name.
    def restore_mon_mute(before):
        key = ((t.SEL_OUTPUT_GLOBAL,), "OUTPUT", "outputMonMute")
        want = before.get(key)
        if want is not None:
            t.set_unit_setting("outputMonMute", want)

    # One from each block. NEVER the disableAll* trio: they take the desk's buttons or outputs away
    # and there is nobody there to undo it by hand.
    for prop, sel, block, newval, fixup in (("metering", 0x07, "GUI", 1, None),
                                            ("systemHapticSetting", 0x0F, "SYSTEM", 2, None),
                                            ("outputMonFixed", 0x0D, "OUTPUT", True, restore_mon_mute)):
        b, snap = snapshot()
        cur = snap.get(((sel,), block, prop))
        if cur is None:
            print(f"\n=== unit_settings: {prop} ===\n  skipped: not present")
            continue
        probe(f"unit_settings: {prop} ({block} block, selector {sel:#04x})",
              ((sel,), block, prop), lambda v, p=prop: t.set_unit_setting(p, v),
              newval if newval != cur else (0 if newval else 1), cur, restore_extra=fixup)

# ---- recorder ----------------------------------------------------------------------------------
# NOT written with probe(): `requestRecordState` is a REQUEST the firmware consumes and parks at 3,
# so it cannot be written-and-restored like an ordinary property, and a read-back of it says nothing
# at all. The engine CONSUMING it is the whole proof, which is the same lesson as probe()'s
# after_write hook.
if "recorder" in dev.caps and (not ONLY or "record" in ONLY):
    import recorder as rec
    print("\n=== recorder: both selectors resolve on this desk ===")
    b0, snap0 = snapshot()
    missing = [f"{p} (RECORDER {rec.SEL_RECORDER:#04x})"
               for p in ("recordState", "recordTimeMs", "requestRecordState",
                         "requestDropMarker", "recordBytesPerSecond")
               if rr.path_value(b0, [rec.SEL_RECORDER], p, rr.MISSING) is rr.MISSING]
    missing += [f"{p} (RECORDINGS {rec.SEL_RECORDINGS:#04x})"
                for p in ("requestDeleteUID", "recordingTotalCount")
                if rr.path_value(b0, [rec.SEL_RECORDINGS], p, rr.MISSING) is rr.MISSING]
    if missing:
        FAILED += 1
        print(f"  FAIL not on this desk: {', '.join(missing)}")
    else:
        print(f"  ok   RECORDER {rec.SEL_RECORDER:#04x} and RECORDINGS "
              f"{rec.SEL_RECORDINGS:#04x} both carry their properties")

    state = rec.read_state(b0)
    print("\n=== recorder: the engine consumes a record request ===")
    if state != rec.NO_DESTINATION:
        # THE SAFETY GATE, and it is not optional: with storage in the desk this write would start a
        # REAL recording, unattended, on somebody's card. The engine check is only meaningful, and
        # only harmless, when there is nowhere to record to.
        print(f"  skipped: recordState is {state} ({rec.STATE_LABEL.get(state)}), so a record "
              f"request could start a real take on this desk")
        NOTES.append("recorder: the engine check was skipped because this desk HAS a recording "
                     "destination. Drive it from the app instead, where a person can hear it.")
    else:
        rec.send_command(rec.CMD_RECORD)
        time.sleep(1.5)
        b1, snap1 = snapshot()
        req = rr.values(b1, "requestRecordState", "int")
        # The desk parks the field at 3 once it has read the request. That is the firmware acting,
        # not our value coming back: we sent 2.
        consumed = bool(req) and req[0] == 3
        still_idle = rec.read_state(b1) == rec.NO_DESTINATION
        added = [k for k in snap1 if k not in snap0]
        if consumed and still_idle and not added:
            print("  ok   the request was consumed (parked at 3, and we sent 2), the desk still "
                  "reports no destination, and nothing was appended")
        else:
            FAILED += 1
            print(f"  FAIL consumed={consumed} (requestRecordState now {req}), "
                  f"still_idle={still_idle}, {len(added)} property(s) appended")
            for k in added[:10]:
                print(f"         ADDED   {_fmt(k)} = {snap1[k]!r}")
        # Leave nothing pending either way, whatever the desk made of the first request.
        rec.send_command(rec.CMD_STOP)
        time.sleep(1.0)
        b2, snap2 = snapshot()
        changed = [k for k in snap2 if k in snap0 and snap2[k] != snap0[k]]
        if not changed and len(b2) == len(b0):
            print("  ok   byte-clean: nothing in the whole tree moved")
        else:
            # requestRecordState itself moving 255 -> 3 is EXPECTED on a desk that has never had a
            # record request since boot, and it cannot be put back. It is the firmware's idle value.
            real = [k for k in changed if k[2] != "requestRecordState"]
            if real:
                FAILED += 1
                print(f"  FAIL {len(real)} property(s) did not come back")
                for k in real[:10]:
                    print(f"         CHANGED {_fmt(k)}: {snap0[k]!r} -> {snap2[k]!r}")
            else:
                print("  ok   only requestRecordState moved, 255 -> 3, which is the firmware "
                      "parking its own request field and is not restorable")

# ---- shows --------------------------------------------------------------------------------------
# Same shape as the recorder branch above and for the same reason: `showControlExport` is a request
# the desk consumes, so only the desk's own ANSWER can prove the address. Here that answer is
# `showControlLastError`, which a dead address could never set.
if "shows" in dev.caps and (not ONLY or "show" in ONLY):
    import recorder as rec          # for read_volumes: the gate below needs to know about storage
    import shows as sh
    print("\n=== shows: SHOWCONTROL resolves on this desk ===")
    b0, snap0 = snapshot()
    want = ("showControlExport", "showControlImport", "showControlNewFromDefault",
            "showControlDelete", "showControlLastError")
    gone = [p for p in want
            if rr.path_value(b0, [sh.SEL_SHOWCONTROL], p, rr.MISSING) is rr.MISSING]
    if gone:
        FAILED += 1
        print(f"  FAIL not on this desk: {', '.join(gone)}")
    else:
        print(f"  ok   SHOWCONTROL {sh.SEL_SHOWCONTROL:#04x} carries all "
              f"{len(want)} commands and the error field")

    print("\n=== shows: the engine answers an export it cannot do ===")
    vols = [v for v in rec.read_volumes(b0) if v["can_record"] and v["inserted"]]
    if vols:
        # THE SAFETY GATE: with storage in, this would write a REAL backup to somebody's card. That
        # is harmless in itself, but it is not this suite's business to leave files behind, and the
        # error-8 answer is only available when there is nowhere to put it.
        print(f"  skipped: this desk HAS storage ({vols[0]['label']}), so an export would write a "
              f"real backup")
        NOTES.append("shows: the engine check was skipped because this desk HAS storage. Drive the "
                     "export/import loop from the app instead - that is the half nobody has "
                     "witnessed on a Duo.")
    else:
        before_err = sh.read_last_error(b0)
        before_n = len(sh.read_shows(b0))
        sh.send_export()
        time.sleep(2.0)
        b1, snap1 = snapshot()
        err = sh.read_last_error(b1)
        flag = rr.values(b1, "showControlExport", "bool")
        added = [k for k in snap1 if k not in snap0]
        # Any non-zero error proves the desk READ the command. 8 is the one measured, so it is
        # named, but a different code is still an answer and not a failure of addressing.
        if err and len(sh.read_shows(b1)) == before_n and not added:
            how = ("error 8, nowhere to put it" if err == sh.ERROR_NO_STORAGE
                   else f"error {err}, which is not the 8 measured on")
            print(f"  ok   the desk answered in about a second: {how}; the export flag cleared to "
                  f"{flag}, no show was created, nothing appended")
            if err != sh.ERROR_NO_STORAGE:
                NOTES.append(f"shows: a card-less export reported error {err}, not 8. The address "
                             f"is still proven live, but shows.ERROR_NO_STORAGE may need company.")
        else:
            FAILED += 1
            print(f"  FAIL lastError {before_err} -> {err}, shows {before_n} -> "
                  f"{len(sh.read_shows(b1))}, {len(added)} property(s) appended")
            for k in added[:10]:
                print(f"         ADDED   {_fmt(k)} = {snap1[k]!r}")
        changed = [k for k in snap1 if k in snap0 and snap1[k] != snap0[k]
                   and k[2] not in ("showControlLastError", "showControlExport")]
        if changed:
            FAILED += 1
            print(f"  FAIL {len(changed)} property(s) moved that had no business moving")
            for k in changed[:10]:
                print(f"         CHANGED {_fmt(k)}: {snap0[k]!r} -> {snap1[k]!r}")
        else:
            print("  ok   only the error and the request flag moved, both the desk's own fields")

print("\n" + "=" * 70)
if NOTES:
    print("NOTES (not failures, but worth a look):")
    for n in NOTES:
        print(f"  - {n}")
if FAILED:
    print(f"{FAILED} CHECK(S) FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
