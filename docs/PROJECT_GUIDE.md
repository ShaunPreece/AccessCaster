# AccessCaster: the project guide

**The user guide to this project: what is true, how it is put together, and what will bite you.**
Everything here is FACT, established on hardware. It is written for somebody who did not write this
code - whether that is a developer picking it up, or a future session of mine.

It is in two parts. **Part 1 is the narrative**: what the RODECaster protocol turned out to be, how
we found it, how the app is layered, and what we would do differently. Read it once, start to
finish. **Part 2 is the invariants**: the load-bearing, non-obvious rules that could be re-broken,
in terse bullets grouped by area. **Grep part 2 for the property, member or helper you are about to
change** - not for a file name.

If you are working on your own RODECaster project, or on a different USB device that is only
configurable through a screen you cannot use, start at part 1.

WHAT IS NOT HERE, deliberately, and all of it ships beside this file. The byte-level reference is
`RODECASTER_PROTOCOL.txt`, beside this file: addresses, selectors, encodings and per-device deltas,
looked up rather than read. The full list of properties each desk publishes is the generated
`docs/PROPERTY_LIST_PRO2.md` / `docs/PROPERTY_LIST_DUO.md`. Where the code sits is
`docs/CodeMap.md`. What the app is aiming to match on the hardware is `docs/PARITY.md`. Anything
about the sound pads or the voice effects is `docs/SOUNDPADS.md`, and that is the first thing to
read before touching either.

The project's own development notes are NOT here and are not public: what shipped and why, what is
open, and the per-device work logs live in a private repo. Nothing in them is needed to understand
the hardware, which is what this file is for.

Nothing here is official. RODE publish no protocol documentation; all of it was worked out from
captured USB traffic, the RODE app's own binary, and the published firmware image. It is accurate as
of firmware 1.7.7 on a RODECaster Pro II and 1.7.3 on a RODECaster Duo. 1.7.7 changes nothing
here: its tree is 1.7.6's, block for block, plus one property (`cloudUploadMultitrackRequestUID`).

--------------------------------------------------------------------------------
# PART 1: HOW IT WORKS

## The short version

The desk exposes a vendor HID interface. You write **named-property records** to it and read the
desk's replies back. One special four-byte record makes the desk stream its **entire property
model** to you, so you can always read its true current state rather than guessing from the last
command you sent. That is the whole protocol; everything else is detail.

Two things are NOT in that protocol. **Pad audio** is a filesystem: the desk mounts its pad storage
as an ordinary FAT removable drive and you copy WAV or MP3 files onto it. And **the touchscreen** is
not on the USB bus at all, so the first-boot wizard, Bluetooth pairing dialogs and anything else that
only exists as a screen cannot be driven from a computer, by us or by RODE's own app.

## What turned out to be possible

Far more than we expected when we started, and much of it is stuff RODE's own desktop app does not
offer:

- Every mixer setting: preamp gain, phantom power, polarity, mic type, line levels, the full
  processing chain per channel (EQ, compressor, gate, de-esser, high-pass, tone shaping, pan), the
  per-channel effects, and the master bus processing.
- The whole routing matrix, read and written by name, including per-output mixes and cross-point
  levels.
- Fader assignment. The fader layout IS a property vector, so which channel sits on which fader is
  readable and writable, mic faders included.
- SMART pads end to end: create, clear, reassign, rename, volume, colour, looping, trim and fade
  envelopes, MIDI trigger mappings, and building custom voice effects.
- The recorder: start, pause, stop, marker drop, the file list, deleting a take, whether it
  writes one mixed track or every channel separately, and whether the mix is WAV or MP3.
- The desk's own show backups, including a software factory reset that does NOT trigger the
  touchscreen setup wizard.
- Settings that were touchscreen-only: screen brightness, dim timeout, button brightness, meter
  style, haptics, record-button behaviour, listen mode, monitor fixed level, the clock display
  options, and three lockouts.
- Wifi network scanning and joining, and Bluetooth discoverability.
- Physical control EVENTS. Button presses, encoder turns, fader moves and bank changes arrive as
  pushed properties, which is what makes a spoken announcer possible.

## The device in one picture

Vendor ID `0x19F7`. Pro II is PID `0x0037`, Duo is `0x0050`.

Both expose several USB interfaces. The one that matters is the vendor HID interface **MI_09**
(usage page `0xFF00`, usage `0x01`). Pick it in code by those two values, preferring interface
number 9; the same rule works on both desks. Everything is written there.

Two traps worth knowing before you start:

- The Pro II enumerates a SECOND vendor HID interface (`0x0026`, MI_03) which is silent to this
  protocol. If your interface-picking code falls back to it, everything looks connected and every
  read returns nothing. That presents as the app having frozen, and it cost us an hour.
- The desk has two USB ports and this protocol works over exactly ONE of them, the port nearest the
  power inlet. Audio still works on the other, so "the cable is in" is not the same as "the control
  channel is there". Our app detects this state and names it, rather than reporting a connection.

## The control protocol, in brief

Two report paths on MI_09:

- **Report `0x01` out / `0x02` in**, 64 bytes. A simple command path, useful only for read-only
  probes: `VER` (a session token, not a stable id), `I` (status bytes), `U` (a ping). No
  configuration happens here.
- **Report `0x03` out / `0x04` in**, 256 bytes. This is the real one. All configuration is
  named-property records.

A record is `0x03`, a four-byte little-endian payload length, the payload, zero-padded to 256 bytes.
The payload is **an address, an ASCII property name, a `0x00`, and a typed value**. Types are int32,
int64, float64 (normalised 0 to 1), bool, string and composite; bools are `0x02` for true and `0x03`
for false, which is the sort of thing you only learn by getting it wrong. Full encodings in the spec,
section 2.3.

Send one four-byte record with payload `ad10a7b0` and the desk streams its entire property model back
as a sequence of 256-byte frames on report `0x04`. **Strip the report-id byte from every frame before
concatenating**, or a stray `0x04` lands at each 256-byte boundary and corrupts any name or value
that straddles it. That single mistake produced weeks of "the dump keeps drifting and values are
corrupt" confusion.

## The single most useful structural insight: the model is a tree

We got this wrong for months, and getting it right made everything else simpler.

The dump is not a flat list of properties with magic "selector" numbers attached. It is a
**self-describing tree**:

```
node     := NAME "\0" count(P) property x P  count(C) node x C
property := name "\0" count(vlen) <payload; payload[0] = the type byte>
count(n) := <length byte L> <n as L bytes little-endian>
```

And a write address is a **path through that tree**:

```
01 01 <levels> [ <len> <index, len bytes little-endian> ] x levels
```

`levels` is the node's depth; each index is that node's position among its parent's children.

Once you see that, every "special case" collapses. What looked like seven different address formats
is one grammar with different numbers. The Duo's mysterious "trailing `0x01`" is just the high byte
of a two-byte index. The internal pad's "special case" is child 0. There is no "`0xFF` wall" and
there are no "slotless blocks": `STORAGEVOLUME` is simply a child of `SYSTEM`, and `WIFISCANRESULT`
a child of `NETWORK`. A root child needs one index, which is why every confirmed selector equals its
root-child position.

**Derive addresses from a parsed tree, never by counting blocks in dump order.** A flat walk has to
skip nested containers it cannot see, and every wrong conclusion we reached about the address space
came from doing that.

So the app writes no position down at all. It names a node by block and copy, `("CHANNEL", 3)` or
`("MIX", 13 * source + output)`, and looks the path up on the attached desk's own tree
(`model_tree.Layout`, held by `rcp_read.layout`). A firmware that grows one block moves every block
after it; the lookup follows, where a written-down number would land on a different node.

## How we found it, in the order we would do it again

1. **Capture the official app talking to the device.** USBPcap plus Wireshark, with the RODE app
   driven one control at a time. Change one thing, capture, diff. This gives you the record framing
   and a first set of real addresses to anchor against. It is slow and it is unavoidable.
2. **Trigger a full model dump and parse it.** This is the single highest-value step, and we should
   have done it much earlier. It hands you every property NAME the desk publishes, its structure and
   its current values, with no capturing at all. Most of what we later "discovered" was already
   sitting in a dump we had.
3. **Read the official app's own name table.** The app has to know a property name to send it, so the
   names are in its binary. Ours pulled 666 names against the desk's 472 published ones. That is how
   you prove a NEGATIVE: if a name does not exist in either the desk's model or the app's table, no
   amount of capturing will find it. We used this to close the "is there a set-clock command?"
   question for good.
4. **Read the firmware.** RODE publish the image. It is embedded Linux with a JUCE touchscreen app
   beside the audio engine, and it tells you what the desk DOES rather than what it publishes. It is
   how we learned that the desk sets its own clock with `ntpdate`, that a timezone is applied by the
   touchscreen writing a symlink, and how storage is bound to a USB gadget LUN. **Look here before
   concluding anything is impossible.**

The hierarchy is worth stating plainly, because we kept forgetting it: **a dump shows what the desk
publishes, the app's binary shows what the app knows, the firmware shows what the desk does.**

## The second source of selectors: make the desk change it, and watch

Captures are not the only way to learn an address, and assuming they are closed three doors that
were all open. The desk's own PUSHES are a second source: put the desk into the state you want,
change it by hand, and read the selector out of what it volunteers.

Three assumptions that were wrong, each dismissed before this method existed:

- **"No press event, so the control is not on the bus."** The channel buttons emit no press record
  of any kind. THE STATE CHANGE IS THE EVENT (`systemChannelSelected` on 0x0f). A capture hunting
  for a press was looking for something that does not exist.
- **"No control in the RODE app, so no capture is possible, so it is unwritable."**
  `outputMonLevel` and the virtual fader levels were both filed unwritable. All are now written,
  read back and restored, from selectors the desk volunteered while the encoder moved.
- **"Already investigated, came back blank."** `systemChannelSelected` and `encoderColour` were both
  tried and dismissed. Watched as STATE while a real gesture happened, they carried the answer to
  the whole encoder question.

The generalisation: **make the desk change the thing, then watch what it says.** Never conclude from
a capture that hunted for one specific record shape. `DevTools/state_observatory.py` is the tool.

## Pad audio is a filesystem, not a protocol

Setting a pad is two separate jobs. The pad's METADATA (name, colour, volume, play mode, trigger
mapping) is properties like everything else. The SOUND is a file.

Put the desk into Transfer Mode and its pad storage appears as an ordinary FAT removable drive. Copy
a WAV or MP3 onto it at the path the pad record names, and that is the sound. **No format conversion
is needed**, which surprised us; we built a converter before testing whether one was required.

The parts that need care: the write has to be flushed to the device before the mode is dropped, the
volume takes a moment to be genuinely readable after it mounts (stat is not enough, read bytes), and
a half-committed pad leaves an orphan entry that only a factory reset clears. That last one is why
our pad code paces its writes and refuses to be "optimised" by shortening its sleeps.

## What the desk will not give you

Say these plainly, because each one cost real hardware time to settle:

- **The touchscreen.** Not on the bus. The setup wizard, Bluetooth pairing dialogs and the desk's
  own media player screen cannot be reached. This is a hardware fact, not a gap in our work.
- **Setting the clock.** There is no set-time, read-time, epoch or RTC command anywhere in the
  protocol, proven three independent ways. The desk syncs from `pool.ntp.org` itself.
- **Playing a sound pad over HID.** The flag sets and nothing plays. A sound pad plays when the
  request goes to the desk's own USB MIDI port instead, and selecting the BANK is an ordinary HID
  write, so the two transports work together: see the pad-firing entries in part 2.
- **Level meters.** The desk describes its front-panel meters in the model but does not publish
  their values there; they hold unpopulated defaults. The official app shows moving meters, so the
  data goes somewhere else and finding it is a capture job. We read the desk's USB audio streams
  through Windows instead, which is close but is not the desk's own meter.
- **The recordings drive.** The desk only ever binds its pad image or its update image to the USB
  gadget, never the recordings partition, so no transfer mode makes recordings appear.
- **A static IP.** `staticIpSet` accepts a write, but `ipAddress`, `subnetMask` and `gateway` are
  read-only REPORTS of what the interface currently has, so there is nothing to express the address
  you want with. A flag with no operands.

Each of these is filed as **PROVED INACTIVE** rather than "not built yet", which is a distinction
worth keeping in your own notes: it tells the next person "tested and dead" instead of leaving them
to re-test it. The probe that settled each one is then DELETED - the finding is the record, not the
script, and a fresh tool will carry fresh assumptions if the question is ever reopened.

## Measuring what you cannot meter

Two techniques did more work than any amount of reading:

**Route the thing you cannot measure back into something you can.** The desk's monitor output is
analogue and not a Windows audio device, so nothing there could be metered - which left "is this
audible?" resting on somebody's ears. Putting speakers on the monitor output with a LIVE MIC turns
it into a measurable path: whatever the speakers play, the mic hears and puts back into the main
mix, which IS readable as a USB audio stream. That is how the test tone was finally killed.

**Demand a rise AND a fall.** A one-shot before-and-after is fooled by anything else making noise.
A test that requires the level to go up when the thing is switched on and back down when it is
switched off cannot be. We reported a false positive for a day because the check only looked at the
rise, on a machine where the PC's own audio plays into the desk continuously and the "silence
baseline" was never silent.

## How the app is built

A Windows system-tray app (`Project/tray.py`) that runs the service (`server.py`) and a native
window (`native/`) in one process; the window calls the service's functions directly, and nothing
listens on the network. Everything that touches USB is behind one lock (`desk_access.hid_lock`),
because HID is a single resource and the window, the announcer, the clip warning and the shortcut thread all want it.

The layers, bottom up:

- `rcp_hid.py` opens the interface and owns the transport.
- `model_tree.py` parses a dump into a tree. **There is one parser and one index per dump**, and
  two real bugs came from the cruder readers it replaced. Do not add another. The one exception left
  is `soundpad.py`, which still windows the raw dump with `blob.find` (see the `padType` entry).
- `rcp_transfer.py` builds every write record, and owns Transfer Mode.
- `rcp_read.py` serves every lookup from the cached parse.
- `desk_live.py` is the one listener, the app's only long-lived handle, and `desk_model.py` the one
  picture of the desk: every dump any reader takes, every push the listener hears and every write
  the app sends lands there, so a fact about the desk is held once and read from there.
- `desk_access.py` is the shared core the features reach the desk through: the lock, the desk as
  it is now, the capability gate and the long job. It imports none of the modules that use it.
- Feature modules on top: `channels.py`, `soundpad.py`, `profiles.py`, `routing_schemes.py`,
  `recorder.py`, `recording.py` (the Recording page's app side, over `recorder.py`),
  `firmware.py` (the check and the install, over `desk_firmware.py`), `shows.py`,
  `network.py`, `meters.py`, `hotkeys.py`, and `announcer.py` (the desk's button presses spoken)
  over `speech.py` (everything the app says) and `settings_file.py`.
- `devices.py` holds one model per product id, so Pro II and Duo differences are data rather than
  branches, with a capability set that refuses a write BY NAME rather than writing blind when a
  selector has never been observed on that desk.
- `server.py` is the service over them: the `read_*` and `action_*` functions the window calls.
- Above the server, and never imported by it: `commands.py` (what a shortcut can do),
  `shortcuts.py` (the manager and the Keyboard shortcuts page) and `app_settings_actions.py` (the
  App settings pages).
- `service.py` is the composition root: `start` brings up the desk listener, the announcer, the
  shortcuts and the watchers, joining each to the code that acts on it, and `stop` lets them go.
- `native/` is the wxPython window. `tray.py` hosts it in the service's own process.

Two rules do most of the work of keeping it honest:

- **One spec drives everything.** A setting is defined once, and that definition produces the read,
  the write validation, the control the window builds, and what a profile backs up. Adding a
  setting in one place adds it in four, so the UI and the backup cannot drift apart.
- **Read back every write.** The address model predicts which BLOCK a property is in, not that a
  given field accepts a write. A write that lands nowhere can append a permanent stray property to
  the model that only a factory reset removes. So nothing reports success on the strength of having
  been sent.

## The accessibility decisions that shaped the UI

This is a blind user's app, and several of these are non-obvious enough to be worth copying:

- **Any dropdown that changes the desk applies on Enter.** A closed `<select>` fires a change event
  on every arrow press, and arrowing through a closed select is exactly how a screen reader user
  reads one, so browsing a list would perform the action. A commit delay makes it worse, not
  better: it fires precisely when you pause to listen. Escape or leaving puts the dropdown back and
  says so. One rule for every such dropdown, so nobody has to know which kind they are in.
  Dropdowns that only change what you are LOOKING at stay instant.
- **One live region per page.** Two regions talking over each other means neither is heard.
- **Tab strips rather than long pages**, with exactly one panel in the document at a time, so
  heading navigation and the tab order only ever contain what you are actually looking at.
- **The end of a page is a hard stop** with an audible cue, rather than wrapping silently back to
  the top.
- **Anything that is conceptually a dialog is a real modal** `<dialog>`, opened with `showModal()`,
  with focus trapped and returned. An inline focused region leaks Tab and strands the user.
- **Speak what actually happened, keyed off the property that changed**, never off an assumption
  about what a command should have done.

## Gotchas that cost us days

The worst of them, in one list. Every one is in part 2 in full,
with the measurement behind it.

- The report-id byte at every 256-byte frame boundary (see above). Strip it per frame.
- Bools are `0x02` and `0x03`, not 1 and 0.
- A value longer than 255 bytes uses a two-byte length. A reader that only ever reads a one-byte
  length silently mis-parses it, which is why a 64-bit capacity field used to read as a stale zero.
- **A property's type is per OCCURRENCE, not per name.** The same name is an int32 on two storage
  volumes and an int64 on the third.
- **A negative result is only as good as the address AND the format you tested with.** We recorded
  channel mute as unwritable on the strength of an exhaustive 256-selector scan. It is writable; the
  scan used the mic-preamp address format and mute lives in a different block that takes the global
  format.
- **The desk clears a request field it has carried out.** A field that reads back exactly as written
  was merely STORED, not acted on. That one test separates a real control from a dead one, and
  several properties with promising names are stored and ignored.
- **A restore onto the state a backup was taken from proves almost nothing**, because every write is
  a no-op. Ours scored zero differences across 6467 properties while carrying a real write-ordering
  bug.
- **Write order matters.** Choosing a mic type loads that mic's own default gain, so a restore that
  writes gain before type loses the gain.

## If you are doing this for another device

The method generalises better than the protocol does:

1. Get the manufacturer's documentation and firmware image first. It is faster than exploring.
2. Capture the official app, one control at a time, and diff.
3. Look for a "give me your whole state" command early. If one exists, it is worth more than a
   hundred captures.
4. Pull the name table out of the official app's binary. It bounds what is possible and lets you
   prove negatives.
5. Read back every write, and treat a value that sticks without acting as a warning, not a success.
6. Write the negatives down, with the mechanism, the moment you settle them. Otherwise every session
   re-tests the same dead ends.

## Licence and attribution

AccessCaster is GPLv3. If this file saves you time, that is exactly what it is for; an
acknowledgement is welcome but not required. It is an independent project and is not made by,
endorsed by, or affiliated with RODE. Everything here was worked out from a legally purchased device
and RODE's own publicly published firmware and application, for the purpose of making the hardware
usable by blind people.

--------------------------------------------------------------------------------
# PART 2: THE INVARIANTS

Load-bearing, non-obvious rules that could be re-broken. **Grep this half for the property, member
or helper you are about to change**, not for a file name. Each entry: the rule, the mechanism, the
trap. Reasoning that only matters at one call site lives in a comment there instead.

--------------------------------------------------------------------------------
## Writing to the device

- **DO NOT SHORTEN THE PAD-CREATE SLEEPS OR THE 60 ms HID PACING.** Two of four proposed speedups
  were built and measured 29th August 2026 (16.29 s per pad down to 13.79 s). The other two were
  exactly these, and they must stay undone: the fixed sleeps and the pacing are WHAT MAKES A CREATE
  COMMIT. The remaining overhead is them, so the only thing left to win is the thing that makes it
  work.

- **EVERY WRITE ADDRESS IS LOOKED UP ON THE DESK'S OWN TREE; APP CODE NAMES NO POSITION.** A write
  goes through `rcp_transfer.address_of(key, prop)` (or `build_at` / `write_at` over it), which asks
  `rcp_read.layout()`: the attached desk's `model_tree.Layout`, learned from a stable dump and kept
  while `rcp_hid.connection_epoch` holds still, re-checked on every later stable dump. A position
  inside a block (a cross-point's `13 * source + output`, a fader's copy) stays arithmetic in the
  code; the block's own position never does. `DevTools/test_addressing.py` drives every write path
  against the saved Pro II 1.7.6 and 1.7.7 and Duo 1.7.3 layouts, checks each record is
  byte-identical to the address used before the lookup, checks every record follows a moved block,
  and fails on any literal path in `Project/` outside `rcp_transfer`.
  - **THE LOOKUP IS THE STRAY GUARD.** It refuses a key the desk lacks and a property the node does
    not carry (`model_tree.AddressUnknown`). Every property the app writes is published on its node
    before the first write, on both desks, so a missing name means the wrong node.
  - **NO READABLE MODEL, NO WRITE.** With no layout and no dump, `rcp_read.layout` raises rather
    than fall back to a remembered position.
  - **A LISTENER NAMES A PUSH WITH `rcp_read.push_key`**, which uses the last layout known, or the
    cached dump's, and never dumps: a listener thread must not start a model read of its own.
  - **AN OFFLINE SUITE ADDRESSES ON A SAVED LAYOUT.** `_testkit` pins `layout_fixture.PRO2_177`;
    `on_layout` switches desk. A fixture holds block and property NAMES only: a model dump holds the
    wifi password and cannot be committed. Regenerate one with `DevTools/layout_fixture.py <dump>
    <name>` when a desk's firmware changes.
  - **THE STRIPS ARE NOT ALIKE.** On the Duo (1.7.3) `EFFECTS_PARAMETERS[9]`, the tenth strip's
    effects node, carries `echoOn`, `reverbOn` and `effectsIdx` and none of the 17 effect
    parameters; the Pro II's carries all 20. `channels.write_chain` writes only what the strip
    carries (`fit_chain`), and chain-follow's read-back compares against the fitted chain. Unfitted,
    a chain moved there writes 17 properties that node does not have.
- **READ A STRIP'S CHAIN PER NODE, NEVER BY POSITION IN THE DUMP.** The pads' effect objects are
  EFFECTS_PARAMETERS blocks too (children of PADEFFECTS), so the Nth `reverbMix` in the stream is
  strip N's only while every earlier strip carries one: read that way, a Duo's tenth strip returns
  the first pad effect object's values. `channels.read_chains` reads each strip's CHANNEL and
  EFFECTS_PARAMETERS node by key (`rcp_read.keyed_node`).

- **THE BLOCK RULE PLUS A READ-BACK TEST REPLACES CAPTURE, and this is the single most useful thing
  the project has learned.** If one property in a block is addressable, the whole block is. Measured
  28th August 2026: sixteen properties filed as touchscreen-only went from BLOCKED to shipped in one
  afternoon, none with a capture behind it, every one written, read back and restored byte-clean.
  **So there is no longer any excuse for filing something BLOCKED because the RODE app has no
  control for it.** Try the block, read it back, restore.
- **A STORY THAT EXPLAINS THE EVIDENCE IS NOT A TEST.** The counter-example, same week: the EQ Q
  values had a causal story ("the app never writes them, yet they move, so the device derives them")
  that fitted every fact then available and was FALSE - Q is directly writable and reads back
  exactly. The only thing that settles a write question is writing it and reading it back.

- **READ BACK every write.** The selector model predicts the BLOCK, not that a given field accepts a
  write. A write that lands nowhere can append a stray, and a stray clears only on a factory reset.
- **A WRITE ADDRESS IS A TREE PATH, not a flat selector.** `01 01 <levels> [<len><index LE>]`, each
  index a position among its parent's children. Derive it with `DevTools/tree_tool.py`, never by
  counting blocks in dump order: that flat walk has to skip nested containers it cannot see, and
  every "past the 0xFF wall" and "slotless block" conclusion came from it.
- **CONSUMED MEANS IT ACTED; STUCK MEANS IT DID NOT.** The firmware CLEARS a request field it has
  carried out (`requestRecordState`, `requestDropMarker`, `showControlExport` back to False,
  `showControlImport` back to -1). A field that reads back exactly as written was merely STORED.
  `padRecordStateRequest`, `storageVolumeTransfer` and `toneGeneration` all stick and do nothing.
  `btDoScan` also sticks, yet scanned once on a Duo, so a flag that never self-clears is not proof of a
  dead one: test for the result too (see the `btDoScan` entry). Use it before concluding a
  feature works: a clean read-back is NOT evidence of an effect.
- **PROVED INACTIVE: written, read back, and they do nothing. Do not re-test any of these.**
  The phrase to use for anything settled this way, so a reader can tell "tested and dead" from "not
  built yet" at a glance. Each below was proven on hardware, and each has a promising name, which
  is the whole trap.
  `toneGeneration` (no sound at 1, 2, 3, 440, 1000 or 255, re-proved 22nd September 2026 on a clean
  tree with the listening path proved first - a pad playing loudly through HP1 - because the
  original reading was taken while a stray existed),
  `broadcastMeters` (no dump values, no pushes),
  `emergencyMuteActive` (a pad kept playing, and audibly: re-checked 22nd September 2026 with
  `padActive` confirming a pad was genuinely playing at the moment the flag was set),
  `padRecordStateRequest` and `padRecordIdx` (PADRECORDER stores and never acts; measured while a
  stray existed and not re-proved, because PADRECORDER is not to be probed again: see "Recording to
  a pad from the touchscreen reboots the desk"),
  `storageVolumeTransfer` (mounts nothing at any transferModeType 0-8),
  `staticIpSet` (a flag with no operands; the address fields are read-only reports),
  `systemDateTimezone` (applied by the touchscreen, not the engine).
- **THE SIX "UNCLAIMED" READABLE PROPERTIES ARE PLAIN STORAGE, AND THE QUESTION IS CLOSED**
  (1st October 2026). `eqParamModeHigh` / `Mid` / `Low` are in
  the GUI block beside `debugMode` and `metering`; `appOutputDevice`, `appMonitorMix` and
  `appRecording` are in an APP block nothing else addresses. All six took a write at their TREE
  address, read back exactly, restored, and left the node, property and name counts untouched. The
  telling part is what did NOT happen: **not one of them pushed anything at all.** A live write has
  a consequence on the push stream even when the property itself is not echoed - muting a channel
  pushes `mixMute` on each of that source's thirteen cross-points and never `channelOutputMute` -
  and these six produced no frame of any kind. Consistent with their names: they hold the RODE
  app's own preferences on the desk's behalf, so there is nothing for us in them.
  **Not filed as PROVED INACTIVE**, because that phrase is for a property whose JOB we know and
  which fails to do it; nobody has watched the touchscreen while an `eqParamMode` changed, and
  that is the only thing left they could plausibly move.
- **`btDoScan` WAS ON THAT LIST AND DOES NOT BELONG THERE.** Written True on a Duo it once filled
  `btScan1` with `'<MAC> - <device name>'` for a real device in range. ONCE, never repeated: that
  is enough to take it off a list of dead properties, not enough to call Bluetooth discovery
  reachable from software. The old entry read "no results on either block, phone in pairing mode",
  which is the weakest kind of negative: a scan that finds nothing is indistinguishable from a scan
  that did not run, because the answer depends on what happened to be in range. **A NEGATIVE THAT
  DEPENDS ON THE ENVIRONMENT DOES NOT BELONG ON THIS LIST AT ALL** - "the desk ignored the value" is
  a fact about the desk, "the desk found nothing" is a fact about the room.
  **ONE DUO SCAN, ONE PRO 2 SILENCE.** On the Duo, a dump taken minutes earlier has `btScan1`
  empty, so that write caused that result; whether it repeats is untested. The Pro 2 half
  took four attempts to test properly and the first three were worthless, which is the useful part:
  an Echo Pop of unknown state, then a Windows PC assumed to be advertising because its pairing
  dialog was open, then an iPhone (Apple biases to BLE and is not reliably discoverable to a classic
  scan). **THE ONE THAT COUNTS: a Pixel on its Bluetooth screen, a fresh False->True transition,
  60 seconds, no slot filled.** Around it, on the same desk that hour: an iPhone paired and played
  audio through it, so the RADIO IS ALIVE, and `btConnectedAddress` read back `'<MAC> - iPhone'`,
  so the desk DOES publish Bluetooth state into the model. Neither "the radio is off" nor "the
  Pro 2 does not report Bluetooth" survives. It reports connection and not scan results.
  **WHAT IS STILL UNKNOWN IS WHETHER THAT IS THIS UNIT OR EVERY PRO 2** - one desk cannot answer it,
  and a second owner can in minutes.
  Two smaller findings from the same hour: `btConnectedType` is 3 for nothing connected and 2 for a
  phone, and `btPairedNumber1` to `5` stay EMPTY with a phone actually paired, so the paired list is
  not published even though the connected address is.
  **THE ORIGINAL NEGATIVE WAS AIMED AT A PHANTOM, which is why it proved nothing.** That session ran
  while `btDoScan` had a stray occurrence in SYSTEM (see the stray entry below): writes to a stray
  read back True and change nothing, so the whole Bluetooth family could be "tested" with every
  write appearing to land. The factory reset removed it, and both desks carry exactly one
  `btDoScan`, in `NETWORK[16]`, today. **A NEGATIVE TAKEN WHILE A STRAY EXISTED IS NOT EVIDENCE
  ABOUT ANYTHING** - re-take it. That is the general lesson, and it applies to every property
  investigated before the reset, not only this one.
  One behaviour to build around: the flag does NOT self-clear, so it stays True until something
  writes it back (the same shape as `wifiScan`). Re-triggering returned the same single device,
  which is what a working re-scan looks like when the room has not changed, so it is not evidence
  either way about whether each trigger re-scans. The
  rest of the family - `btDoPair`, `btPairCode`, `btDoConnect`, `btDoDisconnect`, `btDoUnPair`,
  `btPairedNumber1` to `5` - has never been tested and must not be assumed dead by association. The
  pair and connect triggers take an address whose format has never been seen; pairing FROM the
  phone while the desk is discoverable needs no address at all.
  This matters more than one wrong entry: pairing is touchscreen-only, so a blind owner cannot do
  it at all today.
- **THE FACTORY PAD AUDIO LIVES ON THE DESK, and a software factory reset restores it.** The eight
  sound pads point at `/Application/emmc-data/pads/<n>/<Name>.wav` on the internal eMMC partition,
  and `showControlNewFromDefault` puts both the records and the files back (confirmed on hardware
  21st September 2026; `post-showreset-raw.bin` and `default-show-raw.bin` both show the paths).
  So the app never needs to ship or harvest RODE's samples.
  **THE APP CANNOT SOURCE THEM LOCALLY, so do not try.** The as-found baseline holds whatever was on
  the desk when it was taken, which is the FACTORY set only if the user had never customised before
  installing - so a "restore the defaults from the baseline" button works for some people and
  silently restores somebody's own sounds for others. A factory reset is the one answer that is the
  same for every user.

- **AN OFFLINE TEST CANNOT WRITE TO THE DESK, and that is a GUARD rather than a convention**
  (21st September 2026). `DevTools/_bootstrap` sets `ACCESSCASTER_NO_DEVICE_WRITE` for any script
  named `test_*`, and every write choke point raises on it: `rcp_hid.write_report`,
  `rcp_transfer.send_record`, `rcp_transfer.send_records_paced`, `midi_out.send_cc` and `MidiOut.cc`. A `test_*_live`
  suite opts back in automatically, because driving the desk is what it is for.
  **WHY IT RAISES RATHER THAN SWALLOWING THE WRITE:** a silently dropped write leaves the test
  passing while proving nothing. Earned on 21st September 2026, when a new check in
  `test_latched_pads` called the real `pad_shortcuts._pad_toggle` and disengaged a voice-FX pad on Shaun's
  live desk twice - he heard the tone and the audio drop from another room, and the suite reported
  ALL CHECKS PASSED both times.
  **THE SECOND HALF IS THE BACKGROUND THREAD.** `_pad_toggle` starts `_pad_verify` on its own
  thread, which READS the desk, so stubbing only the write still left the interpreter exiting with a
  HID read in flight: every check printed ok and the process then segfaulted (exit 139). Stub the
  verify too. A suite that passes and then crashes is worse than one that fails.
  **WINDOWS' AUDIO IS GUARDED THE SAME WAY** (5th October 2026). Moving the default output,
  enabling or disabling an endpoint, and setting an endpoint's level or mute all raise in an
  offline test through `write_guard.refuse_in_tests`: every endpoint change goes through
  `virtual_devices._policy`, and the level and mute setters call it first. A new Windows audio
  write must do the same. Without it, `test_announcer_reconnect`'s reconnects ran the real
  `_virtual_devices_follow` and moved Shaun's screen reader between the laptop and the desk.

  **A SETTLED QUESTION DOES NOT KEEP ITS PROBE** (Shaun, 30th August 2026: "if we really want to
  reopen a property investigation we'll write a new tool"). Ten one-shot probes were deleted with
  this list; git history holds them. Keeping one "as the record" is the mistake - the record is the
  line above it. What EARNS a place in DevTools is something that runs again: a suite, or an
  instrument like `tree_tool`, `selector_map`, `firmware_dig` or `property_coverage`.
  A rewrite may repeat an old error, but it may equally try the angle the first one could not, since
  an old probe carries the old assumptions. Shaun's call, and the trade is accepted deliberately.

- **The stray check must diff per-property OCCURRENCE COUNTS, not property names.** An appended copy
  is a new occurrence of a name that already exists, so a distinct-name count reads clean. Diff
  `len(values)` per property against a pre-write snapshot. **And never use the model's BYTE LENGTH:**
  string properties change length with their value (`mixLevelWithAnchor` "1|1" against
  "0.551181|0.551181"), which called a clean matrix restore dirty on 29th August 2026.
- **Never write on an unconfirmed selector.** Selectors are reused across regions, so a wrong
  prediction does not simply miss: the record still lands on some object. The property NAME is what
  disambiguates.
- **A STRAY IS A LIVE WRITE TARGET, so a stray check that only looks for NEW occurrences can pass
  while you write into an old one.** `btDoScan` had a stray occurrence in SYSTEM from an earlier
  session; writes to it read back True, changed nothing, and every stray check said CLEAN because
  the count did not move. It only came to light when the factory reset removed it and the property
  count in SYSTEM dropped. Before trusting a read-back, check the property appears in the block you
  MEANT, and that the block is one the factory dump also has it in.
  **THERE WERE FIVE, MEASURED.** Diffing the dumps either side of the reset (22nd September 2026)
  for any property name appearing in more than one BLOCK TYPE:

      autoReconnect        SIPADVANCED + SYSTEM
      btDoScan             NETWORK + STREAMERXMIXPRESET + SYSTEM      (three copies)
      btVisible            NETWORK + SYSTEM
      requestDeleteUID     RECORDINGS + SHOWCONTROL
      systemHapticSetting  RECBUTTON + SYSTEM

  The post-reset dump has ZERO, as do both show-reset dumps and both desks today. The only
  cross-block duplicates that remain are legitimate: `MIX`/`RCSYNCMIX` and
  `MIXMINUSES`/`RCSYNCMIXMINUES`, seven names, identical on Pro 2 and Duo.
  **THAT IS ALSO THE CHEAPEST STRAY CHECK THERE IS** - count the BLOCK TYPES a name appears in,
  against that known-legitimate set - and it catches what an occurrence-count diff cannot, because
  it needs no before-snapshot.
  `autoReconnect`'s real home is SIPADVANCED, beside the CallMe and Icecast settings: it is not a
  Bluetooth setting, whatever its neighbours in SYSTEM suggested.
  `systemHapticSetting` in RECBUTTON has a known origin: a haptics write sent to a selector by
  number, with its arguments the wrong way round, which landed on selector 2. The `btVisible` copy was written up
  in `server.py` as possibly belonging to a sibling RODE product sharing this schema - reasonable,
  and wrong.
  **A STRAY THEREFORE COSTS MORE THAN A BAD READING: IT GROWS A THEORY.** Nobody doubts a property
  they can see in the dump, so the explanation gets written into the code and outlives the stray.
  When one is found, grep for anything written to explain it.
- **An index past 0xFF needs NO special builder.** `build_record` passes the path to
  `build_address` whole and `encode_path` widens each index, so root child 258 is `02 02 01`, the
  form the desk itself uses for RECORDINGS (0x28F) and SHOWCONTROL (0x28E) on the Pro II.
- **"The RODE app has no control for it" means there is no CAPTURE ROUTE. It has never once meant
  unwritable.** That inference has been wrong six times.
- **A value reading back is NOT evidence the value is valid.** `recordingCompressionQuality` stores
  99, -1 and 255 and hands each one straight back. So an int field's range can never be inferred
  from what it accepts; it needs an observable effect. Validate on OUR side, in the action, and say
  no by name (`action_set_usb_multitrack` refuses anything not in the label map).
- **A negative result is only as good as the address AND the format it was tested with.**
- **No blind selector scans on hardware.** An early one appended two persistent stray global props
  that shifted two read arrays by +1 and survived power-cycles.
- **Read a property BY BLOCK and instance, never "first occurrence in the stream".** A stray copy of
  `btVisible` in SYSTEM once made the Network page report "not discoverable" while the radio was, and
  `MIX` / `RCSYNCMIX` share six names legitimately. Use `rcp_read.block_value()` /
  `iter_block_properties()`.
- **`requestRecordState`, `requestDropMarker` and `requestDeleteUID` are self-clearing REQUESTS.**
  Never read one for status; read the thing it acts on (`recordState`, or the recordings list).
- **RECORDINGS = 0x28F, PROVEN 29th August 2026** by deleting a real recording (list 20 -> 19,
  `recordingUID` and `recordingContent` each down one, nothing else moved). Written in the TWO-BYTE
  address form. Anything that masks a selector to one byte turns `0x28F` into `0x8F`, a live mix
  cross-point; the global builders no longer mask (see "The write side widens the same way").
- **Deleting a recording does NOT renumber the others.** Uids 1-19 stayed put when 20 went, so a UID
  is a stable handle and a page's list does not shift under it.
- **A RECORDING'S TIMESTAMP IS AN ORDINARY UTC EPOCH, shown in the COMPUTER'S LOCAL time.** The
  opposite rule ("local wall clock in an epoch field, so render it as UTC") sat in four files and
  was asserted by a test; it rested on one 53-minute observation and showed every recording an hour
  early in summer. Measured 29th August 2026 against four made-to-order takes.
- **`systemDateTimezone` does NOT move that stamp** (44, 0 and 12 all gave the same UTC second), so
  it is a touchscreen display setting and an index cannot be checked from here.
- **`flush_volume` MUST really flush.** It calls `FlushFileBuffers` on the volume's device handle
  now (milliseconds) instead of launching PowerShell (1.2-2.3 s per pad), and falls back to the
  PowerShell path if the handle will not open. A flush that silently does nothing leaves a pad's
  audio in Windows' cache and the desk gets a truncated file, which nobody discovers until the pad
  is pressed. `DevTools/test_flush_verify_live.py` is the licence: write, LEAVE transfer mode,
  re-enter, read back, compare md5.
- **`rcp_transfer.transfer_session()` makes `set_transfer_mode(False)` a no-op while held.** That is
  the point - the per-pad code keeps its own enter/exit calls and they stop taking effect - but it
  means any new code that genuinely needs the volume unmounted mid-batch will not get it. The hold
  is reentrant and its `__exit__` is unconditional, so a failure on any pad still closes the session.
- **A pad BACKUP and a pad RESTORE are not the same job at the same speed.** A restore writes over
  transfer mode at ~15 s per pad (measured); a backup only reads a mounted volume and does the whole
  grid in seconds (16 pads, 19.7 MB, 4.6 s end to end, measured 29th August 2026). Never quote one
  rate for the other - the dialog did, and overstated a backup by 30x. The read rate is **~20 MB/s**
  and every copied WAV was md5-verified against the volume, so it is throughput, not a per-pad
  constant.
- **The report-04 event channel is NOT selector-free.** `cmutePressed` is selector 0x63 +
  `mutePressed`. Injecting the record button does nothing; use `requestRecordState`.
- **Read back the first write on any NEW string property.** A string write combines two
  separately-proven halves, and the combination itself is only proven per property.
- **`wifiPSK` WAS READABLE off the desk, and on 1.7.6 reads empty** (10th October 2026, a Pro II
  joined to a password-protected network), so "has a password saved" never shows there. Older
  firmware may still publish it, so keep it out of every reply, the window, profiles and log lines
  regardless.
- **THE UNIT SCANS FOR WIFI ONLY WHEN ITS RADIO COMES UP WITH NO NETWORK TO JOIN** (3rd November
  2026, Pro II, 1.7.6). Joined, `wifiScan` (True, or False then True) is stored, never cleared, and
  no result arrives. Unjoined, the results clear about 3 s after the radio comes on and fill about
  12 s after it, in `wifiScan1..10` and the WIFISCANRESULT blocks alike (one unnumbered
  `wifiScanResultSSID` per block), before `wifiScan` is written; whether `wifiScan` does anything
  is unproven. So `network.action_scan_wifi` cycles the radio and refuses while joined.
- **THERE IS NO WIFI DISCONNECT OR FORGET PROPERTY** (the whole 1.7.6 property list has
  `btDoDisconnect` only). `network.action_disconnect_wifi` writes `wifiSSID` = "" (accepted, reads back
  empty) and cycles the radio. Any `wifiSSID` write loses the stored password: writing the old name
  back did not rejoin in a minute. So `network.action_join_wifi` sends nothing when the password is empty
  and the last scan lists that network as needing one.
- **USB multitrack mode 1 IS pre-fader and mode 2 IS post-fader. MEASURED 29th August 2026**, no
  longer inferred from the recorder's twin field: with the fader down, mode 1's per-channel stream
  stayed busy (active 0.61) and mode 2's went silent (0.06). Needs a person - the mic faders are
  physical.
- **`outputMultiMode` CARRIES TWO SETTINGS, AND TREATING IT AS ONE SILENTLY DESTROYS A FADER
  LAYOUT.** The low part is the USB 1 multitrack output mode (0 Off, 1 Pre-Fader, 2 Post-Fader) and
  adding 3 puts the USB 1 INPUT into Expanded, which is RODE's Virtual Devices. All six values
  measured on a Pro II, fw 1.7.6. Compose on write and decompose on read; never write a bare 0-2,
  because on a desk in Expanded that drops it back to Standard and the desk unassigns every
  extended input the user had put on a fader. The desk's own app warns before doing either.
- **EXPANDED ADDS NO BLOCKS TO THE MODEL.** Byte length is 156825 in both states on a Pro II, so
  Game, Music, A and B are existing slots in the ten-wide strip space rather than new inputs.
- **GAME, MUSIC, A AND B ARE SOURCE CODES 12, 13, 14 AND 15**, measured on a Pro II, fw 1.7.6:
  each was written to a fader and read back, and all four were accepted. **4, 5 and 6 are NOT
  spare and never were** - they are the stereo-link pair codes.
  **READ THE `INPUTSOURCE` TABLE RATHER THAN GUESSING FROM GAPS.** Every desk publishes 30 nodes
  whatever it has plugged in, so presence proves nothing, but two fields separate real from
  reserved: `inputColour` `ff000000` is a placeholder slot, and `inputDigitalGain` `-12` marks the
  family that arrives over USB (7, 8, 9, 10 carry it and no microphone does). Four consecutive
  coloured entries at -12 is the signature that found these. `DevTools/input_sources.py` prints
  the table and diffs two dumps.
  **THE ORDER IS RODE'S, NOT MEASURED.** Their guide and their driver's INF both list Game, Music,
  A, B, and they say the four are functionally identical. Naming them in any other order costs a
  label, never a wrong address.
  **NAMED ALWAYS, ASSIGNABLE ONLY IN EXPANDED**, and both halves are load-bearing. A fader keeps
  whatever code it was given, so a desk that leaves Expanded can still hold one and the announcer
  would otherwise say "0 percent, Source 12" out loud; and offering them in Standard puts an input
  on a fader that can never carry audio. Every `*_for(expanded)` accessor is that pair, fed by
  `channels.usb_expanded()`. **The list that OFFERS and the list that ACTS must come from the same
  call**, or a source is offered and then refused, or a matrix row is drawn with no state behind
  it. Both of those happened during the day this was built.
- **THERE IS ONE CHANNEL TABLE PER DESK, `devices.CHANNELS_PRO2` / `CHANNELS_DUO`, AND EVERY
  CHANNEL LIST IS A VIEW OVER IT.** The fader dropdown, the Channels page, the matrix rows and the
  matrix row INDICES were four hand-written lists spelling out the same codes and labels, with
  nothing comparing them. Adding the Virtual Devices updated two of the four, which left the
  routing matrix naming USB 1 after a Windows device that no longer existed and offering no rows
  for the four at all. **Adding an input is now one `Channel` entry**; which surfaces it appears on
  is stated per entry, because membership genuinely differs (on a Duo, CallMe 1 takes a fader and
  has a channel strip, but no matrix row). `test_specs.py` asserts the surfaces agree and that the grid
  reads exactly the rows the page offers.
  **`Channel.key` IS STORED USER DATA** - profiles, mix schemes and the applied-preset record all
  save it - so renaming a key orphans somebody's saved settings. Rename the `label`, which is the
  thing displayed.
- **USB 1 IS NOT "MAIN STEREO" IN EXPANDED.** The channel carries the `System (RØDECaster)` virtual
  device there, with the other four alongside it, so `FaderSpec.expanded_relabel` renames it to
  "USB 1 (System)". A label naming a Windows device the person no longer has is worse than a plain
  one, and this is read aloud on every encoder turn.
- **THE DESK COMES BACK FROM A RE-ENUMERATION UNDER A DIFFERENT USB PRODUCT ID, because the id
  encodes the audio configuration.** Measured on a Pro II, fw 1.7.6, against `outputMultiMode`:
  0 gives PID `0x0037`, 2 gives `0x0092`, 3 gives `0x0078` and 5 gives `0x0094`. The HID path
  changes with it, so **NO LONG-LIVED READER MAY CACHE ITS `RodeInterface`** - resolve it again on
  every reopen, as the desk listener (`desk_live`) does. Caching it is what made the
  announcer go silent for good after a USB setting was changed in the app itself: the read failed,
  the reopen was tried on the old path for ever, and to a screen-reader user that is
  indistinguishable from the app having died. Reconnection now takes about 3 seconds and says
  "<desk name> reconnected", because silence is the one thing that cannot be interpreted.
  **The exception is a mode the app's own switch has just written** (`_usb_mode_written_by_app`):
  the page's reply has said it, and a second line lands on top of Windows and RODE's driver
  announcing the same arrival. The tidy and the follow-up still run.
- **A WRITE THAT RECONFIGURES USB AUDIO MUST WAIT FOR THE DESK TO COME BACK, OR THE WRITES BEHIND
  IT ARE LOST.** The desk is off the bus about 60 ms after `outputMultiMode` lands and back 1.7 to
  2.5 s later, so anything sent in between goes nowhere and is reported as "no control interface",
  which reads to the user as their desk being unplugged. Measured on a profile restore carrying
  `usb_expanded`: 480 settings applied and **27 silently lost**, among them both headphone types
  and the whole Compellor set; with the wait, 507 applied and none lost.
  `rcp_transfer.set_usb_multitrack_mode` therefore ends in `rcp_hid.wait_for_reenumeration`, and
  the caller keeps its device lock across it, because nothing else could have succeeded in that
  window anyway. **Watching the OLD path die is what tells a real re-enumeration from a write the
  desk ignored**, so writing a value it already holds costs a fraction of a second and no more.
- **`rcp_hid.find_control_interface` ANSWERS FROM A REMEMBERED INTERFACE LIST, NOT A FRESH SCAN.**
  A `hid.enumerate` blocks for tens of seconds on a desk half off the bus, and the active device is
  asked several times per request, so a scan per lookup stalls every one of them. The list ages out after 5 seconds
  and is dropped by `forget_interfaces`; keep all four of these: a failed `open_handle` forgets it,
  the desk listener's lost read forgets it, `wait_for_reenumeration` forgets it and polls with
  `fresh=True`, and the listener's RECONNECT lookup passes `fresh=True` (without that, an arrival
  waits out the age limit). A status read scans with `refresh_interfaces`. **Never call `enumerate_interfaces`
  for a lookup**: it bypasses the list and brings the stall back.
- **THE ANNOUNCER SAYS WHEN THE USB MODE MOVED WHILE IT WAS DEAF, AND A PUSH CANNOT DO THIS JOB.**
  The desk can leave Expanded unprompted (measured: off the bus with nothing written to it for
  nine minutes, back two seconds later in Standard with the setting reset), and it changes as it
  leaves the bus, so any push announcing it is sent into the gap. The announcer therefore takes
  `outputMultiMode` from the desk model when the desk is lost (the model keeps it until the desk is
  read again) and the reconnect compares. It names the half that MOVED rather than
  reciting both, and Expanded going away is the consequential one: the operator's Game, Music, A
  and B inputs go dead and every RODECaster device on the computer is renamed.
- **THE THIRD STORAGEVOLUME IS THE INTERNAL PAD STORAGE, AND ERASE MUST NEVER REACH IT.** Eject and
  erase are per-volume switches; `recording._storage_target` takes the volume from `recorder.read_volumes` on
  the live dump and refuses anything not marked `can_record`, which the internal one never is. Do
  not address the nodes by a fixed index instead.
- **LEAVING EXPANDED OVER USB DOES NOT UNASSIGN THE FOUR; THE TOUCHSCREEN DOES IT ITSELF.** The
  unit's screen asks "All extended inputs will be unassigned" and empties those faders; writing
  Standard from here leaves them assigned to inputs nothing can feed (read 3rd October 2026: mode 0,
  faders 2 to 5 still 12 to 15). So `_clear_extended_faders` empties them, BEFORE the mode write,
  because the change takes the desk off the bus. `_tidy_after_standard` does the same when the desk
  is found in Standard with them still assigned: at start-up, and after it leaves Expanded on its
  own.
- **A DESK IS IDENTIFIED BY ITS SERIAL, AND A PRODUCT ID IS ONLY TRUSTED WHEN THE TABLE HOLDS IT.**
  `devices.DEVICES` knows `0x0037` (Pro II) and `0x0050` (Duo), which are the DEFAULT-mode ids, so
  any desk in a non-zero `outputMultiMode` presents an id no table can hold. Resolving on the id
  alone then falls through to `DEFAULT`, which IS a Pro II - invisible on a Pro II and wrong on a
  Duo in Expanded, which would be handed a Pro II's matrix shape and channel layout.
  So `get_active_device` tries the id, then a map learned from the desk's own `systemName` and
  keyed by SERIAL, which survives every re-enumeration. `rcp_read.read_model_raw` fills that map,
  but only when `devices.unidentified` says the id names nothing: one dict lookup per dump, and the
  string read once per desk per session. **Do not tabulate the other ids instead**: that is six
  entries per desk, unmeasured for the Duo, and wrong again the day RODE add a mode. The MI_09
  product string is no help either - it reads `HID Interface` on both desks.
- **VIRTUAL DEVICES NEED BOTH HALVES OF `outputMultiMode`, AND A BIND THAT RODE'S SERVICE ONLY
  SOMETIMES MAKES.** Measured on a Pro II, fw 1.7.6, driver 1.0.5, with RODE's app never opened.
  Post-fader multitrack AND Expanded (value 5, product id 0x0094) yields ten capture endpoints,
  `Fader1` to `Fader9` plus `System`, matching the desk's 20-channel multitrack stream as ten
  stereo pairs, and one playback endpoint, `System`. That is a per-fader recording input for any
  app that can pick a microphone, with no ASIO and no DAW. **Expanded with the mix alone (value 3,
  0x0078) BINDS: it is not the explanation for a missed bind.** RODE's troubleshooting asks for
  Expanded USB 1 Input and a pre or post fader USB 1 Output together, and one mix-only arrival on
  1st October 2026 failed to bind where post-fader bound in two seconds, which looked like a cause.
  Six alternating flips the same afternoon settled it: three mix-only arrivals and three post-fader
  ones, all six bound in about 4.5 seconds. The mode is not the factor. It exposes only `System`
  either way, because the mix alone carries no per-fader stream.
  RODE's driver is a kernel virtual audio device (enumerator ROOT, device name `RØDECaster`) plus
  a user-mode service, `RODECasterVADService`.
- **THE BIND FAILS WHEN ANYTHING IN WINDOWS IS PLAYING TO THE DESK'S OWN MAIN OUTPUT** (proved
  4th October 2026). The service's bind opens the KS render pin behind that output, and the pin
  takes one user: a shared stream on `Speakers (RODECaster Pro II Main Multitrack)` fails with
  `AUDCLNT_E_DEVICE_IN_USE` while the service is bound, and the service cannot bind while a stream
  holds it. Holding that stream from the moment the endpoint appeared kept the service unbound for
  the full ten seconds, and it bound 0.22 s after release, four trials out of four
  (`DevTools/probe_bind_hold.py`). The service
  retries every 100 ms (`FUN_14000d310` in a decompile of version 1.0.5), so "never binds later"
  was wrong: it binds the instant the pin is free. What holds the pin is whatever follows Windows'
  default device. With the driver unbound `System (RØDECaster)` does not exist, so a screen reader
  set to it falls back to the default, which Windows moves back onto the desk the moment the desk
  re-appears; whoever speaks first after an arrival (the screen reader, the announcer's "USB input
  is now Expanded" at about 1.3 s, Windows' own sounds) wins against a bind due at 1.0 to 1.4 s,
  and once lost it stays lost, because `System` never appears for the screen reader to move to.
  That feedback is what the "failing runs" were. **The remedy is parking**: before any write that
  brings the desk back as an Expanded device, `virtual_devices.park_output` moves the default to
  another live playback device, and a guard (`server._park_for_arrival`) puts it back each time
  Windows moves it onto the desk, then moves it on to `System` the moment the service binds.
  **The parking device is never muted**: speech heard briefly on the laptop is fine, and a muted
  default left behind by a crash mid-park is a silent machine. **It is not a fix for every setup**:
  a PC whose only playback device is the desk has nowhere to park, and a program set to the desk's
  Main output by name ignores the default, so the retries stay. **Switching the desk's Main
  playback endpoint off in Windows is NOT a way round it**: the service checks that interface is
  enabled before it tries (`DeviceInterfaceAlias disabled`), and missed twelve arrivals out of twelve
  with it off. **Never put a replug in the failure sentence**: it is one more arrival in the same
  race. The real fix is RODE's: keep `System` present while unbound, or keep Windows off the pin.
- `virtual_devices.py` reads the service's own status block (shared memory
  `Global\RODECaster_VAD{...}`, readable by anyone: connected flag, Enable, heartbeat, desk,
  firmware), which is the detector, and `reconcile` makes Windows agree with it. The follow-up
  runs after every USB mode write, on every announcer reconnect (the desk re-enumerates for its
  own reasons too) and at startup. **It never announces the bind or its loss**: RODE's driver
  raises its own Windows notification for both, which a screen reader reads, so the follow-up
  speaks only a miss, missing Faders, a moved default output or an error.
- **`System (RØDECaster)` HAS A FIXED VOLUME, 0 to 0 dB** (measured 5th November 2026; the desk's
  Main Stereo has -96 to 0 dB), so Windows' volume keys do nothing while it is the default output.
  **The app does not take the volume keys to make up for it**: built and tried, it took a key from
  Windows, changed USB 1's Level on the desk behind the user, and needed a per-side level swap to
  undo itself. The owner's ruling: overreach, left to the user (the encoder, or another output).
- **RODE'S OWN APP DOES NOTHING TO MAKE THE BIND HAPPEN, SO THERE IS NOTHING TO CAPTURE FROM IT.**
  Its import table carries only read-only device APIs: `CM_Get_Parent`, `CM_Get_Child`,
  `CM_Get_Sibling`, `CM_Locate_DevNodeA`, `CM_Get_Device_ID_List`, `SetupDiEnumDeviceInterfaces`.
  There is no `CM_Reenumerate_DevNode`, no `SetupDiCallClassInstaller`, no `StartService` or
  `ControlService`, and `ShellExecuteW` opens their web pages. It writes `outputMultiMode` over
  HID exactly as we do and leaves the rest to the service, so driving it under USB capture to
  learn about the bind would show only the record we already send.
- **THE SERVICE HAS NO DEVICE-NOTIFICATION IMPORT AT ALL: IT POLLS.** No
  `RegisterDeviceNotification`, no `CM_Register_Notification`; what it has is `HID.DLL`
  (`HidD_GetHidGuid`, `HidD_GetSerialNumberString`, which is where the desk string in the status
  block comes from) plus `Sleep`, `timeGetTime` and `WaitForMultipleObjects`. It matches the desk
  by a device-interface path holding `usb#vid_19f7&pid_0078`, `0079`, `0094` or `0095`, then
  instantiates the KS filters named `usb1main` and `usb1chat`. Its failure strings are
  "DeviceInterfaceAlias disabled.", "No devices found" and the KS pin set
  (`Failed to instantiate Capture Pin`).
- **THE BIND IS NOT A RACE BETWEEN THE HID INTERFACE AND THE AUDIO INTERFACES.** That is the
  obvious reading of "DeviceInterfaceAlias disabled" and it is wrong here: sampled at 120 ms
  across four arrivals, the desk's KSCATEGORY_AUDIO, CAPTURE, RENDER
  and TOPOLOGY interfaces and its HID interface go ACTIVE within 0.15 s of each other, and the
  audio ones sometimes go first. There is no window in which HID is up and audio is not.
- **THE SERVICE'S OWN TRACE IS COMPILED OUT, SO DEBUGVIEW WILL NEVER SHOW A BIND.** The binary
  carries the whole trace string table (`%s Enter`, `%s Leave, returning %#08x`, `Found Device:
  %s`) and imports `OutputDebugStringW`, which reads as a way in. It emits nothing: a global
  DBWIN listener held across four arrivals caught 49,507 lines from `audiodg` and 114 from
  `svchost`, both session 0, and zero from the service. Every trace call is gated on a level
  global that nothing in the binary writes, and its registry key holds only `Version`, `Path`,
  `Enable` and `Latency`, so there is no flag to switch tracing on either.
- **WITH THE DEFAULT ALREADY ON `System (RØDECaster)`, NOTHING CONTESTS THE BIND.** Every arrival
  between two bound states binds, because whatever plays is on `System` and the desk's Main output
  is free: that is a "binding run", and it validates any change made during it. Only a test that
  makes the contest itself (`DevTools/probe_bind_hold.py`) says anything about the bind.
- **THE SERVICE'S HANDLE COUNT IS NOT THE CAUSE.** It rises about 25 per arrival and a restart
  resets it, but misses and binds overlap across the whole range, and a restart with the desk
  present binds nothing while the pin is held. The bind history (`DevTools/bind_history.py`)
  records the service's state on every attempt and stays useful for spotting a miss in ordinary
  use; it does not record who held the pin.
- **THE FOLLOW-UP RE-READS THE DESK'S MODE BEFORE EVERY RETRY AND BEFORE IT SPEAKS.** Only one
  runs at a time and with retries it lasts over a minute, so a mode changed meanwhile gets no
  follow-up of its own. Retrying with the mode it started with put the desk back into Expanded
  after the user had left it, and judging against it reported fader inputs missing from mix only.
- **IN STANDARD THE SERVICE'S CONNECTED FLAG IS STALE, NOT TRUE.** It polls, so for a few seconds
  after the desk leaves Expanded it still says connected; `reconcile(expect_connected=False)`
  overrides it, or the endpoints are switched on on the way out.
  `describe(expanded=False)` does the same for the Routing page's driver line, which is polled
  while the page is open because the bind lands, or retries, after the page has read it.
- **BOTH USB APPLY BUTTONS ARE HELD FOR THE WHOLE SWITCH, BIND AND RETRIES INCLUDED.**
  `_virtual_devices_following` is taken before the follow-up's thread starts, so the reply never
  goes out with the switch looking finished, and the driver poll reports it as `switching`. The
  page blocks the buttons with a spoken reason (`setUsbSwitching`); the server needs no gate of its
  own, because a write that gets through anyway is followed by the running follow-up.
- **PRE-FADER TO POST-FADER IN EXPANDED IS NOT AN ARRIVAL.** Both are product id 0x0094 and the
  desk stays on the bus, so the service has nothing to bind to. `_write_usb_mode` reports an
  arrival only when the product id changes, and only arrivals go into the bind history.
- **THE DIAGNOSTIC TOOLS ARE NOT THE CAUSE, and it was worth asking.** Every failure of that
  afternoon fell inside a window where a device-interface sampler or a DBWIN listener was running, which
  reads as an observer effect: the first enumerates five device-interface classes eight times a
  second, and the second throttles `audiodg` by making it wait on a Python reader for 330 debug
  lines a second. The test is to run the flips WITH the observer on purpose, and that gives 6
  binds out of 6. The correlation was with whatever held the desk's Main output, not the
  instrument.
- **A BOUND SERVICE CAN LEAVE ITS ENDPOINTS DISABLED, AND THE APP SWITCHES THEM ON.** The service
  switches its endpoints on and off by writing Windows' audio endpoint registry, which Windows
  re-reads only on an audio device arrival; after a mode change that write can land after the
  pass, and the eleven endpoints then sit in Windows' "disabled" state until the next arrival,
  which is what RODE's reboot instruction supplies. `reconcile` switches them on at once through
  `IPolicyConfig`, the interface the Sound control panel uses, with no elevation; proven by ear,
  a voice on Fader2 and the screen reader on System.
- **`reconcile` ACTS ON ONE SAMPLE, AND IT CAN BE THE ONE THAT DISABLES THE ENDPOINTS.** It switches
  the virtual endpoints OFF while the service reads as not connected, which is right at the instant
  it looks and wrong a moment later if the bind then lands: nothing re-samples, and Windows is left
  holding endpoints this app switched off. Measured 1st October 2026 - three playback endpoints
  disabled with the desk holding all four channels on faders and the service reporting connected,
  and the log line `reconcile ... not connected ... enabled=0 disabled=3` naming the act. **The
  repair is an event, not a second guess at a delay**: `virtual_devices.watch_endpoints` registers
  an `IMMNotificationClient` and `server._virtual_devices_repair` reconciles whenever Windows
  reports an endpoint moving. It runs under `_virtual_devices_busy`, so the deliberate
  re-enumerations of a retry are skipped rather than fought, and it terminates because reconcile is
  idempotent: its own repair wakes it once more and changes nothing.
- **A PROGRAM RECORDING FROM THE DESK'S MAIN MULTITRACK INPUT STOPS THE SERVICE CREATING ITS FADER
  ENDPOINTS, AND THE SERVICE STILL SAYS CONNECTED.** It opens that capture pin directly, and a pin
  the audio engine already has open cannot be opened again. The Sound control panel's Recording tab
  meters every input, so it counts; measured three times with the panel open (connected, nine
  Faders "not present") and once with it closed (nine Faders active three seconds after connect).
  `reconcile` reports it as `faders_missing` with the remedy in words, and the remedy is a fresh
  arrival, not "apply again": the desk ignores a write of the value it holds. This app's own meter
  cannot cause it: a WASAPI stream dies with its device and `meters.Watcher` never reopens on its
  own, while the Sound panel re-subscribes the moment the device returns.
- **WINDOWS' DEFAULT OUTPUT IS MOVED ONLY WHEN IT WAS ON THE DESK OR IS DEAD.** Turning Expanded on
  renames the desk's Main output, so the endpoint Windows was using stays listed and silent and
  Windows falls back to whatever it can find, the laptop speakers included. `_restore_default`
  moves it to `System (RØDECaster)` while the driver is connected and to the desk's Main output
  only while it is not, and moves the console and multimedia roles together, leaving
  communications alone. **NEVER THE DESK'S MAIN OUTPUT WHILE THE DRIVER IS BOUND:** the service
  holds its render pin, so a stream there opens and plays nothing, and every screen reader and
  announcement following the default goes silent. A bind read before `System` exists (an arrival
  from the touchscreen or RODE's app, nothing parked) waits for it (`_wait_for_system`), and
  leaves the default alone if it never comes. A default that is
  alive and elsewhere was chosen by the user and stays. **So leaving Expanded parks as well**
  (`_write_usb_mode_parked`): `System` goes before the desk's Main output comes back, Windows falls
  back to the laptop speakers, and unparked that fallback reads as the user's choice and stays.
  The Standard follow-up waits for the desk's Main output (`_wait_for_desk_main`) and moves the
  parked default onto it. **A leave nobody parked for** (the touchscreen, a dropout) is caught by
  `_note_default`: a default that moved off a desk or driver endpoint which is no longer active is
  Windows falling back, recorded as displaced and treated as parked. It samples on every endpoint
  change, so the endpoint the default left is still the last one seen.
- **NEVER MATCH A VIRTUAL ENDPOINT AGAINST THE DRIVER'S INF NAMES.** The INF string table declares
  `RØDECaster Combo1 In`, `SmartPads In`, `Headset In` and the rest, and NONE of them appear. The
  service names endpoints from the attached desk at runtime, which is how `Fader1` to `Fader9` arise.
  The four playback devices it can name are `system output`, `game`, `music`, `virtual a` and
  `virtual b`, each also carrying a capitalised display form.
- **A VIRTUAL DEVICE'S WINDOWS ENDPOINT EXISTS ONLY WHILE THAT CHANNEL IS ON A FADER.** Measured on a
  Pro II, driver 1.0.5, 1st October 2026: with none of codes 12-15 on a fader the driver publishes
  one playback endpoint, `System`; assigning Game produced `Game` six seconds later, and all four
  assigned produced all four. RODE's troubleshooting article says the same. So a desk showing a
  single playback device is a desk with no virtual channel on a fader, which is what every earlier
  measurement here was. With Expanded on, `System (RØDECaster)` replaces `RODECaster Pro II Main` as
  the computer's output, and a program is pointed at one of the four in Windows' Settings > System >
  Sound > App volume and device preferences. The four are interchangeable labels rather than
  different kinds of input, and the desk's USB 1 input carries them as five stereo pairs on ONE
  10-channel playback endpoint (`RODECaster Pro II Main Multitrack`, 10 ch / 48 kHz / 32-bit in
  Expanded, the same endpoint at 2 ch in Standard). The driver's `System` and the four are two-channel
  windows onto pairs of that one stream, not separate hardware.
- **THE DRIVER NAMES THE ENDPOINT, SO IT NAMES THE CODE.** Assigning 12, 13, 14, 15 one at a time
  produced `Game`, `Music`, `Virtual A`, `Virtual B`, which settles the four without listening to
  anything. RODE's help centre calls the last two "Chat A" and "Chat B" and their user guide calls
  them "A" and "B"; the driver's own names are what Windows shows and what the app matches.
- **WINDOWS PROMOTES THE NEWEST ENDPOINT TO DEFAULT, so assigning the four moves the default output
  to whichever appeared last.** The per-app records under
  `HKCU\...\LowRegistry\Audio\PolicyConfig\PropertyStore` are a trace of the endpoint each program
  LAST PLAYED ON, not a choice somebody made: they are written automatically, they name programs
  whose output is set elsewhere (NVDA picks its device inside NVDA), and they do not survive the
  default moving. Do not read one as a user preference.
- **NO CHANNEL ON THIS DESK HAS A NAME PROPERTY.** `CHANNEL` carries ten instances and no string at
  all, and `INPUTSOURCE` has `inputColour` but no label; the only per-instance strings anywhere are
  pad names, storage volume names, `sipUnitName`, `systemName` and StreamerX preset names. So "Game"
  cannot be renamed on the desk, by us or by RODE's app, and a rename in Windows reaches neither the
  desk nor this app's labels, which come from the table in `devices.py`.
- **ASIO AND EXPANDED ARE MUTUALLY EXCLUSIVE** (RODE): the RODECaster ASIO driver is unavailable
  while Expanded is on, and reaching it needs the Virtual Devices driver set to Bypass AND USB 1
  Input back to Standard. That tray Bypass is also what hands back the conventional single
  16-channel multitrack device for a DAW, keeping fader assignments; with the driver active,
  multitrack instead arrives as one device per channel.
  **RODE DOCUMENT A REBOOT AFTER INSTALLING THE DRIVER.** A reboot is one more USB arrival, which
  is all the enable-landed-late case above needs; `reconcile` does the same without one. Their
  driver also has a tray icon carrying Enable and a Latency setting, Ultra-low by default and
  droppable to Low if the computer cannot keep up; both are written into a second shared block
  (`...{127DD4F7...}`, writable by ordinary users) which the service mirrors into its admin-only
  registry key within a second. The app reads the status block and never writes the other:
  bouncing Enable re-creates only `System`, never the Faders.
- **NEVER SEND `VER` OR THE `U` PING: IN EXPANDED, EACH ONE DROPS THE VIRTUAL PLAYBACK ENDPOINT
  ABOUT 17 SECONDS LATER.** Windows surprise-removes `System (RODECaster)` and re-adds it a second
  or two on, and a screen reader on that output loses its speech for a few seconds each time.
  Measured on 30th September 2026 one action at a time with the server stopped: `VER` alone 16.9 s,
  `U` alone 17.3 s, deterministic; a model dump, the `I` command, a bare HID open and close, SAPI
  speech, SAPI enumeration, a Chromium app window and every `meters.py` call all clean. It looked
  like an Alt-Tab fault because the app's status read sent both on every read.
  `read_device_status` now sends `I` only, whose answer is the liveness proof, and identity is the
  USB serial. `rcp_hid.quick_probe` and the DevTools probes still send both; they are tools, not
  app paths, and cost a few seconds of speech when run.
- **A WINDOWS ENDPOINT RENAME DOES NOT SURVIVE TOGGLING EXPANDED, AND THE OLD NAME LINGERS DEAD.**
  Re-enumeration creates a fresh instance with a numeric prefix, `Speakers (2- RODECaster Pro II
  Main Multitrack)`, while the previous one stays listed and silent. So anything that stores a
  playback device BY NAME rots, looking correct in its own settings while producing nothing. The
  original name does come back cleanly once the desk returns to Standard.
- **THE VIRTUAL DEVICES DRIVER IS SIGNED "FREEDMAN ELECTRONICS PTY LTD", NOT "RODE".** Freedman is
  RODE's parent company, so a publisher check looking for the string "RODE" REJECTS THE GENUINE
  INSTALLER. Verified valid, DigiCert-issued, on 29th September 2026. `driver_install.SIGNER` is
  that string, compared WHOLE against the certificate's O= field (`subject_organisation`), never
  searched for in the subject, and it is the whole of what makes the download safe to run.
- **THE APP REACHES THE INTERNET IN TWO PLACES AND NO OTHER: this driver download, from its button
  only, and its own update check and download (`selfupdate/http.py`), to
  `updates.slightperception.com` only.** Everything else talks to the USB cable.
  `test_driver_install.py` asserts it by scanning `Project/` for a `urlopen`. The README
  and Help list both, because somebody who chose this app partly for what it does not do deserves
  to be told exactly what it contacts. A third caller means changing all three together.
- **CHECK THE SIGNATURE, NEVER A HASH.** The URL carries no version and RODE replace the file in
  place, so a pinned hash refuses every release they ship, which trains people to skip the check.
- **PASS A PATH TO POWERSHELL BY ENVIRONMENT VARIABLE, NOT AS AN ARGUMENT.**
  `powershell -Command "... $args[0] ..." -args <path>` does NOT populate `$args`: the signature
  check ran against an empty path, reported its status as "-args", and refused RODE's genuine MSI.
  An env var cannot be mis-parsed and cannot be injected into either.
- **MATCH THE DRIVER BY UpgradeCode, NEVER BY NAME: EVERY NAME IT CARRIES SPELLS RØDE WITH A SLASHED
  O.** Its MSI ProductName is `RØDECaster Virtual Device Driver` and its Manufacturer is
  `RØDE Microphones` (U+00D8), and Windows copies both verbatim into the uninstall registry. A
  detector matching the literal "RODECaster" or "RODE" therefore reports "not installed" on a machine
  that has it, which is the same failure as the Freedman publisher check above and fails silently.
- **DETECT THE DRIVER WITH TWO CHECKS, NOT ONE, OR THE MESSAGE IS CONFIDENTLY WRONG.** The installed
  MSI answers "is it present" (UpgradeCode `{36C667D1-F3A6-4A7B-9A3B-1A6A5C1A5707}`; the UpgradeCode
  survives version bumps, the ProductCode does not - 1.0.5 is
  `{0612AAD0-4DE5-4C02-A531-FE61F95ACDC8}`). The audio endpoints answer "is it ACTIVE". Present with
  no endpoints means Bypassed or awaiting a reboot, and telling that user to install it is worse
  than saying nothing. Source: `https://update.rode.com/virtual_dev_driver/RODECasterDriver_WIN.zip`,
  a 1.1 MB zip holding one MSI, version 1.0.5 as of 29th September 2026. The URL carries no version,
  so RODE replace the file in place and a pinned hash would fail on their every release; check the
  signature instead.
- **THREE properties re-enumerate the desk's whole USB audio function: `outputMultiMode`,
  `engineMode` and `systemMainMode`.** All four endpoints drop for a second or two, Windows falls
  back to the laptop sound card, and the default device moves. **This takes Shaun's screen reader
  out**, and it drops OUR control interface mid-test, so anything measured across such a write is
  worthless - on 29th August 2026 a meter reading was taken on the very endpoint the write was
  tearing down. Before any test that touches these: move announcements off the desk, and expect the
  interface to need a retry. Measured, not guessed.
- **`engineMode` and `systemMainMode` are NOT settings to sweep.** `systemMainMode` read back as 2
  after a write of 1, so the desk drives it as well; each experiment costs Shaun his screen reader.
  One targeted write at a time, never a sweep.
- **Anything the desk reports LIVE is push-only and reads 0 in the model dump for ever.**
  `playerProgress` and `playerCurrentPositionTime` join `recordState` in this. Reading the dump for
  one of these produces a confident, wrong negative - it cost half a session on 29th August 2026,
  after the same trap was already documented for the recorder.
- **USB 1 is the port nearest the power inlet and carries `PID=0x0037 MI_09`, the only interface our
  protocol works over.** Put a drive in USB 2. Pull the USB 1 cable and the app goes deaf while USB 2
  keeps the audio functions and a second HID device alive.

## Write ordering and derived values

- **Chain write order is load-bearing: mode, VoxLab knobs, neutral params, derived family, preset
  marker LAST.** In simple mode the desk re-derives the chain from the knobs; the first live attempt
  lost 4 values to exactly that. `channels.apply_processing_preset` orders its floats with
  `_float_write_order` itself, so every caller gets this order whatever order its dict is in (a
  preset's keys put the knobs after the derived family).
- **Derivation fires on the MACRO WRITE, not on entering simple mode.** A strip can sit in simple
  mode holding a chain its own macros would not produce.
- **The preset marker is a NAME only.** Any parameter write blanks it to -1, and there is no HTTP
  marker writer, so a settings-only profile is the only way to put it back.
- **`channelBypassProcessing` is deliberately NOT in a preset.** The channel's master on/off is the
  operator's control. Asserted both ways by a test. RODE's own app behaves the same.
- **`faderLevel` is addressed per STRIP.** Written before the layout it lands on whatever channel was
  there before, so restore writes levels straight AFTER the layout.
- **`profiles.apply` restores THE FADER LAYOUT FIRST**, before channel processing. It used to be
  last, so a profile saved with a reordered layout restored its voicings onto the wrong channels.
- **CHOOSING A MIC VOICING ALSO REPLACES THE WHOLE PROCESSING CHAIN, and writing the old type back
  does NOT undo it.** Measured on a Pro II 31st August 2026: PodMic -> Dynamic moved 13 params
  (Depth, Sparkle, Punch, compressor threshold + gain, de-esser, gate threshold + range, all three EQ
  gains, both Aphex), and every one was still on the Dynamic value after the type went back. A DUO
  mic moves the gain only. NOT undone by `server.action_set_mic_type` on purpose - the voicing's own
  chain is what the user asked for by naming their mic - but it is COUNTED and ANNOUNCED, and the
  client re-reads the panel, because the alternative is a screen reader reading 13 stale numbers.
- **A VOICING TAKES A SECOND OR TWO TO APPLY, AND A CHAIN WRITE LANDING INSIDE THAT WINDOW IS LOST.**
  Proven by writing `eqLowGain` 0.3125 exactly 0.12 s after a type change and reading back the
  voicing's own 0.59375. `profiles.apply` waits for the strip to settle after a type that
  CHANGES. On a strip in SIMPLE mode it is worse than a straight overwrite: the desk derives EQ,
  compressor, gate and de-esser from the three VoxLab knobs, so values written mid-apply come back as
  neither voicing's - 8 of 13 did, at values belonging to nothing. **Restore a chain with
  `apply_processing_preset`, never a loop of per-param writes**: it puts the MODE on first, which is
  what stops the derivation regenerating everything behind you.
- **CHOOSING A MIC VOICING SETS THAT VOICING'S OWN DEFAULT GAIN**, so `profiles.apply` writes the
  MIC TYPE BEFORE the gain (it used to be gain first, type fourth, and a restore that changed the
  voicing threw the saved gain away). Measured Dynamic 45, Condenser 25, PodMic 50, Broadcaster 27,
  Procaster 50, NT1 30, NT1-A 31, RE20 51, SM7B 55; Line-in and Instrument leave it alone.
  `DevTools/test_mic_type_gain_live.py` re-measures, `test_restore_mic_order_live.py` proves the
  restore end to end, and `test_profiles.py` asserts the order offline. **`server.action_set_mic_type`
  reads the gain back, updates the Level slider and says the new number** - the UI must never leave a
  gain on screen that the desk has moved by up to 30 dB.
- **A RESTORE ONTO THE STATE IT WAS SAVED FROM PROVES ALMOST NOTHING.** Every write is a no-op there,
  so a write that is ignored, mis-ordered or silently wrong still reads back perfect. That is exactly
  why the merge restore scored 0 differences on 6467 properties while carrying this bug. Test a
  restore from a DIFFERENT state.
- **A behaviour added to one write path needs checking on its neighbours.** "The chain follows the
  channel" shipped on `action_set_fader` and `action_reset_faders` never got it, which is the bug
  Shaun heard as gated bed tails.
- **PROCESSING BELONGS TO A STRIP, NEVER TO A CHANNEL, so a channel with no fader has no processing
  anywhere on the desk.** Only we can hold it: `channels.park_chain` / `take_chain` keep a vacated
  chain in `%LOCALAPPDATA%\AccessCaster\off_fader_chains.json`, keyed by desk, and `_follow_chains`
  hands it back when the channel returns. Without that, a channel LEAVING loses its voicing to the
  next occupant and a channel RETURNING silently inherits a stranger's. Measured 31st August 2026:
  Mic 2 came back from off-fader wearing USB 1's chain, gate off and bypass on. **`_follow_chains`
  has three cases, not one** (moved / left / returned); adding a fourth kind of layout change means
  checking all three. A file rather than a cache because stereo link evicts the upper mic of a pair
  and the unlink that returns it can be days later.
- **`take_chain` POPS.** A parked chain that stayed parked would be re-applied on the next return and
  overwrite whatever the user had since dialled in.
- **A STEREO-LINKED PAIR IS A SOURCE CODE (4 / 5 / 6), so it must be NAMEABLE BUT NEVER ASSIGNABLE.**
  `FaderSpec.all_labels` names it; `code_set` and `code_order` must not contain it. If a pair code
  reached `code_set`, the UI would offer "Mic 1 + 2" as a drop target beside "Mic 1" and
  `_tidy_fader_target` would gap-fill with it, so a mic would be both linked and on its own fader.
  Asserted in `test_specs.py`. For the same reason the tidy asks `pair_swallowing` before deciding a
  mic has gone missing: a linked mic is not absent, it is inside the pair.
- **A LEVELS ROW IS A STRIP, so its mute and solo are addressed BY FADER, not by channel name.**
  `action_set_fader_mute` / `action_set_fader_solo`. An empty fader and a stereo-linked pair both
  have no channel name to look up, and a pair is deliberately absent from the assignable channel
  list, so a by-name route could not mute one. They still write the same properties and call the
  same `announcer.mark_self_mute` / `announcer.mark_self_solo` as the Channels page, which is what
  stops the two pages disagreeing.
- **PER-STRIP MUTE AND SOLO GATE ON `processing`, NEVER ON THE LEVEL GATE** (`_strip_toggle_gate`).
  They are compact bools on the channel's processing selector, so `processing` is the capability
  that decides them. Shipped tied to `_level_write_gate` for an hour on 31st August 2026: the Duo's
  `faderLevel` selectors are unconfirmed, which says NOTHING about its mute, so the Duo would have
  lost a working control and been told the wrong reason. The same mistake also left the UI hiding a
  control the HTTP endpoint still accepted. **A gate belongs to the capability the write needs, not
  to whatever gate the surrounding read already had.**
- **ANYTHING THAT OFFERS SOLO MUST ALSO OFFER THE WAY OUT.** A solo replaces the headphone feed, so
  one left on leaves the desk sounding dead with nothing on screen saying why - the state a blind
  operator cannot spot by glancing at a lit button. Every solo reply names EVERYTHING the headphones
  now carry (`_solo_summary`), not just the channel pressed, and every page with a solo control also
  carries Clear all solos. Adding solo somewhere new means adding both.
- **A STEREO LINK'S MUTES ARE SETTLED BY US, NEVER LEFT TO THE DESK.** The desk's `channelOutputMute`
  does not follow the inputs: a strip has kept its own mute through a link (the pair inherited a
  muted Mic 3 while Mic 4 was open, 2nd November 2026), and has also been seen clearing it.
  `_settle_mutes` reads back and writes the outcome. A pair is muted if EITHER mic was, so a link
  never opens a mic the user silenced. Each mic's own mute is parked (`channels.park_link_mutes`, a
  file, because the unlink can be days later) and `_unlink_mutes` hands it back if the pair's mute
  is unchanged since the link; a pair muted or opened while linked passes that choice to both.
  Without the parked record, a muted-plus-open link followed by an unlink mutes the open mic.
- **A CROSS-POINT'S PATH LEAVES BYTE RANGE AT SOURCE 14.** It is MIX[13*source + output], and MIX
  starts at root child 76 on the Pro II, so Game (12) is 232 and Music (13) is 245 but Virtual A
  (14) is 258 and Virtual B (15) is 271, and the desk addresses those two with a TWO-byte index.
  Read as one byte both come back as 1, below the MIX block, so the push is silently discarded and the
  fader moves with no level and no name - while mute and listen, addressed by strip index, still
  speak, which is the signature that tells the two apart. Take the address as a whole path element
  from `rcp_read.parse_push`, never as a byte. Only codes 14 and 15 reach that far, and only in
  Expanded, which is why this was invisible until a virtual channel was first put on a fader.
- **THE WRITE SIDE WIDENS THE SAME WAY, AND A GLOBAL SELECTOR IS AN INDEX RATHER THAN A BYTE.**
  Every record builder hands its path to `build_address` whole, so `encode_path` emits
  `01 0f` for 15 and `02 02 01` for 258 - the same two-byte form the desk pushes. Masking to 0xFF
  truncated SILENTLY, which is worse than raising: 258 became 2, a valid node 256 cross-points
  away. Proven on the Pro II for both Virtual A (258) and Virtual B (271): `mixDisabled` written,
  read back, restored, and the model's node, property and MIX counts unchanged, so no stray
  (`DevTools/test_cross_point_wide_live.py`, 1st October 2026).
- **EVERY CROSS-POINT WRITE GOES THROUGH A `rcp_transfer.set_cross_point_*` HELPER**
  (`_linked`, `_excluded`, `_level`), each of which runs `_check_cross_point` first. Sending
  `build_cross_point_link` raw skips that gate, so a cell the connected desk does not offer reaches
  the wire.
- **READING OR WRITING THE GRID USES `MatrixSpec.all_sources`; ONLY THE DISPLAY LIST FOLLOWS THE
  MODE.** The desk publishes all 30 source rows whichever mode it is in, so rows 12 to 15 are
  ordinary MIX nodes with real state at all times. `all_sources` / `all_source_indices` is that
  wide, mode-independent set, and `_read_matrix`, `routing_schemes.capture`, `routing_schemes.apply`
  and `_check_cross_point` all take it; `sources_for(expanded)` is the narrower list the page
  OFFERS, because four permanently silent rows across every output is a lot of grid to walk past
  for nothing. Three faults came from walking the display list instead, and they are different
  faults with one cause:
  - the write guard refused all FOUR Expanded rows, not just the two that overflowed a byte, so
    Game and Music looked writable and were not;
  - a mix scheme's SHAPE followed the mode. Captured in Standard it held no Virtual A cell, so
    applying it in Expanded left one behind and "Everything normal" could not reset it. A scheme
    is saved user data, so its shape must not depend on which mode the desk happened to be in.
  - the Chat preset and the auto-gain isolation both mean "every other source out", and in
    Expanded the computer's own audio arrives on those four. So "Chat = my mic only" left a screen
    reader going down the line, and a level check measured it.
  **The source COUNT in a scheme's spoken description is the one place the mode belongs**
  (`describe(scheme, expanded=...)`): the stored scheme always holds every row (14 on a Pro II),
  and saying "1 of 14" to somebody looking at 10 would name rows they cannot see.
- **A mix scheme writes the output MODE FIRST, then its cross-points.** Leaving Custom mode discards
  that output's cross-point set on the desk, so the other order throws away what it just wrote.
- **EXCLUDING a cross-point also UNLINKS it, and an unlink FREEZES the level at the fader's current
  position.** So an excluded cell always reads back "unlinked at some number", that number is inert,
  and it drifts as the faders move. `routing_schemes.capture` records nothing but `excluded` for
  such a cell; recording the level made a clean restore fail its own read-back.
- **`linked` is inferred from level == anchor**, so it is unreliable for the moment right after an
  unlink (the unlink makes them equal). Fine on a settled desk, wrong as a post-write read-back.
- **A CROSS-POINT WHOSE CHANNEL IS OFF-FADER CANNOT BE SEEN TO RELINK, AND THAT IS PERMANENT.** The
  level is republished by the FADER, so with no fader carrying that channel the level and the
  anchor stay wherever they were parted and the inference reads unlinked for good. Measured on the
  Pro II: unlink Mic 3's Headphone 1 cell at 0.5, send `mixLinkRequest`, and it still reads
  `'0.5|0'`. The link itself is fine - it is the only OBSERVABLE of it that is missing. So any
  "everything is following its faders" assertion has to ask about on-fader sources only, which is
  what `DevTools/test_schemes_live.py` does; Mic 2, Mic 3, Mic 4 and Bluetooth are routinely
  off-fader on a desk in Expanded.
- **A SOURCE CAN BE A CROSS-POINT OF ITS OWN OUTPUT, AND THE ROUTING PAGE MUST OFFER IT.** USB 1,
  USB 2, Chat and Bluetooth are on both lists, and the cell at `13*source + output` for a matching
  pair is an ordinary one: proven 16th September 2026 by excluding USB 2 from the USB 2 output
  through our own API and reading it back, restored clean. The page used to HIDE that switch on the
  reasoning that a channel cannot be a source of itself, which made a hand-built mix minus
  impossible in the app and was reported as a bug (you exclude USB 2 from USB 2 so a second computer
  is audible locally without its own screen reader going back to it). Mix Minus MODE dropping the
  output's own source is the desk's own doing and is a separate thing.
- **THERE IS ONE MATRIX CAPTURE AND ONE MATRIX APPLY, and they live in `routing_schemes.py`.** A
  profile stores the scheme verbatim (`routing.mix_scheme`) and `server._apply_matrix` hands it
  straight back. Do not grow a second copy in `profiles.py`: the one that used to be there covered
  the monitor outputs only and recorded levels for excluded cells.

## Channel properties

- **Channel processing is PER FADER STRIP.** Slot N processes whatever sits on strip N. Resolve the
  strip from the LIVE fader vector (`channels._strip_index`), never from a pinned per-channel index.
  **A channel the connected desk does not carry REFUSES** (`channels._channel_strip`); it never falls
  back to a fixed strip number. With a fallback, Mic 3 in a Pro II profile restored onto a Duo lands
  its chain on Duo strip 2, which holds Bluetooth. `test_duo_parity` asserts the refusal.
- **NAME A SOURCE CODE FROM `FaderSpec.all_labels`, NEVER FROM THE ASSIGNABLE LIST.** A stereo-linked
  pair is its OWN source code (4, 5, 6) and can sit in the fader vector, but it is deliberately NOT
  assignable, so the assignable-only map cannot name it. `code_label` was that map and is DELETED
  (2nd September 2026) rather than fixed, because three of its four callers had the bug and the
  fourth was one link away from it: the announcer said "0 percent, Source 4" out loud for a linked
  Mic 1 + 2, which Shaun heard while the live caps suite was linking them. **Naming is the WIDE set
  (`all_labels`), writing is the NARROW one (`code_set` / `code_order`)** - and they must stay apart,
  because a pair code in `assignable` would let the UI offer "Mic 1 + 2" as a drop target and let
  `_tidy_fader_target` gap-fill with it. Pinned by `test_announcer.py`.
- **`channelWirelessMute` IS IN THE CHANNEL BLOCK, PER STRIP, NOT IN THE INPUT TABLE.** It sits on
  exactly the nodes `channelOutputMute` and `channelCueEnable` sit on (`rcp_transfer.channel_key(strip)`, ten
  of them, read off a live tree 2nd September 2026), so it is written with
  `channels.set_channel_wireless_mute` like any other toggle there. `CHANNEL_PROP` listed it among
  the properties addressed at the INPUT selector until then, which is a different block indexed by
  SOURCE CODE; nothing could reach it, so no wrong write ever shipped, but the note was wrong for as
  long as it existed. It moves ONE property and drags nothing, unlike the channel mute below, and a
  write that moved the matrix would mean the channel mute had been reached by another name. It is
  NOT in the chain set and must not join it: a mute is live state, so a fader move carrying it would
  silence a mic nobody muted. NEVER ear-proven, and it cannot be here - nobody owns a wireless mic.
- **CallMe 1 (source 16) is on the Channels page** like any line input: its chain is the one on
  the strip it is on (`channelInputSource.index(16)`), so it has processing only while on a fader.
  Heard on a phone call, 3rd October 2026: mute, Level and the high-pass all reach the caller's
  audio, and `channelPan` hard left and hard right does nothing. Its cross-point row is source 16
  (a mute moves nodes 284 to 296), shown on the Routing page on a Pro II only: unmeasured on a Duo.
- **A STEREO PAIR JOINS TWO ADJACENT INPUTS, NOT TWO FADERS, AND REASSIGNING FADERS CANNOT CHANGE
  WHICH PAIRS EXIST.** The pair codes are fixed positions in the firmware's input enum: 4 = inputs
  1+2, 5 = inputs 2+3, 6 = inputs 3+4. A fader is a slot you put a source on; the PAIR is formed at
  the input stage and then occupies one fader. So "reassign fader 3, then link 3 and 4" is not a
  thing (Shaun asked exactly that, 2nd September 2026, and it is a reasonable thing to ask).
- **"IMPOSSIBLE" AND "NOT OFFERED" ARE DIFFERENT, and the Duo's pairs are one of each.** Code 6 needs
  an input 4 that a Duo does not have: impossible. Code 5 is Mic 2 + the Headset jack, two real
  adjacent inputs: possible, and left out purely because nobody has wanted to hard-pan an XLR mic
  against a headset mic. The comment used to call both "not a pair anyone would want or RODE would
  offer", which dressed a judgement up as a fact about the hardware. If code 5 is ever added it needs
  a REASON, not a test: the engine accepts source 3 on a Duo, hardware that does not exist.
- **THE SMART PADS BUS TAKES NO PROCESSING. MUTE AND SOLO ONLY, ON BOTH DESKS.** Settled BY EAR on
  2nd September 2026, on a Duo and on Shaun's Pro II. The strip publishes a full chain and accepts
  every write to it - pan, EQ, HPF, gate, make-up gain all land, read back and restore byte-clean -
  and none of it reaches the audio. The control that makes it believable: pan on Mic 2 moved the same
  listener's voice to one ear minutes before pan on the bus did nothing.
  **RODE'S OWN GUIDE AND THEIR APP SAID SO ALL ALONG** ("You can add processing to any audio channel
  except the SMART pads"), and that was this code's ORIGINAL reason for excluding the bus, overruled
  on 29th August by a write that landed and read back. Enforced in `channels.read_channels` (a bus
  gets `None` for the whole chain) and `native/channel_tabs.build_tabs` (a bus gets the settings
  tab only).
  **DO NOT PUT THE CHAIN BACK BECAUSE A WRITE READS BACK CLEANLY. It will.** That is the single most
  expensive mistake in this project's history and it has now been made twice on the same property.
- **PAN IS NOT A FAIR TEST OF A STEREO SOURCE ON ITS OWN**, which was the innocent explanation while
  this was still open: a mic is mono and pans, the pads bus is stereo. It was ruled out by testing
  non-pan parameters (HPF, EQ, gate, make-up gain) and by the Pro II hearing the same nothing.
- **A STEREO SOURCE PANS WITH `channelPanL` AND `channelPanR`, NOT `channelPan`.** Heard 3rd
  October 2026: `channelPan` hard over did nothing on USB 1 or CallMe 1, and both legs at 0.0 put
  USB 1 hard left. Every strip carries all three; default legs are 0.0 and 1.0. A pan control on a
  stereo channel that writes `channelPan` is dead. RODE's app shows a stereo channel two knobs,
  LEFT and RIGHT, one per leg, not one balance control.
- **PAN DOES NOTHING WHILE A CHANNEL'S PROCESSING IS OFF** (heard 2nd November 2026, USB 1): the
  legs are written and read back, and nothing moves until processing is on. The window says so in
  each pan control's description (`channel_tabs.pan_note`).
- **WRITING A VOXLAB MACRO (`channelDepth` / `Sparkle` / `Punch`) RECOMPUTES EQ AND DYNAMICS.**
  The desk rewrites the EQ gains, compressor, gate and de-esser thresholds from the macros, so a
  restore writes the macros FIRST and the derived values after. Turning Advanced off in RODE's app
  resets the whole chain to new values (it warns), so it is not a way back to a baseline.
- **MUTING A CHANNEL ALSO MUTES ITS WHOLE MATRIX ROW.** Writing `channelOutputMute` on a strip sets
  `mixMute` on all 13 cross-points for that source as well, and clearing it clears all 13. Measured
  on a Duo 31st August 2026 (14 properties moved from one write, and restored). The 13 are exactly
  the cells the matrix formula predicts, `0x4C + 13*source + output`, so this independently confirms
  the base and the stride on the Duo from a direction the capture never covered. Do not treat a
  `mixMute` sweep as evidence of a routing edit; it may just be a muted channel.
- **A CROSS-POINT WRITE ONLY MEANS ANYTHING IF ITS OUTPUT IS IN CUSTOM MODE.** In Main Mix or
  Mix Minus the write still STORES, and reads back, and changes no audio at all - so a test that
  forgets the mode passes while proving nothing. `set_output_routing(out, 2)` first, and put the
  mode back afterwards. A cross-point LEVEL additionally needs the cell UNLINKED, and RELINKING is
  what restores it (a linked cell follows the fader and reads back level == anchor).
- **`mixLinkRequest` / `mixUnlinkRequest` ARE WRITE-ONLY TRIGGERS, AND TOUCHING ONE CHANGES ITS TYPE
  FOR GOOD.** We send the constant composite payload the RODE app sends; the desk answers by storing
  a CLEARED composite in the same slot, so the property decodes as `pair` rather than `bool` from
  then on, on every cross-point you have ever touched. Going back to Main Mix does not undo it; a
  soft reset does. Nothing in the app reads either property and every value that means something
  (`mixDisabled`, `mixLink`, `mixMute`, the level) restores exactly, so this is cosmetic TODAY.
  **The trap is the KIND change**: `values(blob, "mixLinkRequest", "bool")` silently returns fewer
  than the real count, so never add a kind-filtered or positional read of these two.
- **THE ROUTING MATRIX IS SOURCE-KEYED: a grid row is the audio SOURCE, not the fader strip it
  happens to sit on.** Settled on hardware 25th August 2026, read-only. Processing is the opposite,
  keyed by strip, which is the whole reason the two must never be moved together: a chain-follow
  moves the DSP chain ONLY, never the matrix. **Get this backwards and every custom mix silently
  sends the wrong channels** - nothing errors, nothing looks broken, and the first you know is a
  guest hearing themselves. The read formula is `index = row_stride * source + output`.
- **A DESK IS IDENTIFIED BY ITS USB SERIAL, NOT BY WHAT `VER` RETURNS.** The serial (`GVxxxxxxx` on
  the Pro II here) survived eight factory resets and a power cycle on 16th September 2026 and comes
  free with the enumeration, without opening anything. `VER` read `FBB9-CBE2` that afternoon and
  `0576-FC06` by the evening, so everything filed under it - applied mic presets, the baseline
  offer, parked chains - was being silently orphaned. The app no longer sends `VER` at all (see the
  Expanded entry below). Profiles were never affected: they gate on `product_id`, which is the
  model, not the unit.
- **`inputColour` is a STRING** (AARRGGBB hex), not an int. There is no string channel-property
  writer at all, so it is SAVE ONLY in a profile: read and backed up, never restored.
- **`inputType` is DERIVED from the mic type** (0 mic, 1 instrument, 2 line, -1 no physical input).
  A profile restores the MIC TYPE and lets the path follow; restoring the path can only produce a
  pairing the desk's own UI cannot make.
- **`inputInstrumentGain` IS a stored setting**, though: selecting Instrument sets it to 20 and
  switching away does not clear it.
- **`mixLink` IS NOT PER-CROSS-POINT LINK STATE, and the direct test is the proof rather than the
  counts.** Unlink an INCLUDED cross-point at a manual level and `mixLink` stays True (measured on
  the Pro II: Headphone 1 x Mic 3 unlinked at 0.5, `mixLink` True throughout). What it actually
  tracks is EXCLUSION: measured across all 390 cells it was False on exactly the 16 excluded ones
  and True on every other, with no cell False and included. Keep the level-vs-anchor inference; it
  is the only thing that reports a manual unlink.
  **The counts argument that used to stand here was not evidence** - "True on 383 of 390 while the
  inference says 114" looks damning until you look at the 221 the two disagree about, and every one
  of them is `'-1|0'`: a placeholder cell for a source slot the desk does not fill. The grid is 30
  source rows wide and a desk uses 13 of them.

- **A CHANNEL MUTE DOES NOT SILENCE AN UNLINKED CROSS-POINT.** `channelOutputMute` reaches each
  mix through the cross-points that follow the fader; one unlinked at a manual level keeps playing.
  Measured on the Pro II, 12th October 2026: USB 1 muted, Headphone 1 (USB 1 linked) went silent,
  the monitor (USB 1 unlinked at 50%) kept playing. So "silent in every mix" is only true when
  every one of that source's cross-points is linked, and a listener on the unlinked output hears
  no mute at all.

- **THE ROUTING MATRIX IS THE `MIX` BLOCK, 390 CELLS, AND A POSITIONAL READ MUST SAY SO**
  (`rcp_read.block_values`, never `values`). `RCSYNCMIX` reuses all six cross-point names
  (`mixDisabled`, `mixMute`, `mixLevelWithAnchor`, `mixLink`, `mixLinkRequest`, `mixUnlinkRequest`)
  and follows MIX in the stream, so an unscoped read returns both grids concatenated and its length
  is the sum of two unrelated shapes. That is what the old `cell_count=516` was: 390 + RCSYNCMIX's
  126. RCSYNCMIX is sized by how many devices RODECaster Sync supports, so adding one resizes it -
  fw 1.7.6 took it to 153, total 543 - and every matrix read then refuses with "the matrix changed
  shape" on a grid that never moved. `MatrixSpec.block` names the block for exactly this reason.
  The `_stable_key` fingerprint is the one deliberate exception: it compares whole dumps and never
  indexes, so the extra cells only widen what it notices.

## Soundpads

- **A LIVE PREVIEW EDIT IS WRITTEN ONLY TO A PAD STILL NAMED "Preview"** (`soundpad.preview_write`,
  `name=PREVIEW_NAME`). Edits are queued (`server._PreviewWriter`) and can be written after the
  preview has ended; without the check, a late one lands on whatever pad was made since at that
  grid. `action_preview_stop` also drops what is waiting (`forget`). `preview_update` is the one
  caller with `name=None`: an in-place edit of any pad, for tools.
- **THE APP SHIPS NONE OF RODE'S PAD SOUNDS.** They are RODE's audio, licensed with the desk, and
  the repo and installer are public. The factory pads come back from the user's OWN desk instead:
  `action_show_reset` ends with `_keep_factory_pads`, a backup of the freshly reset desk saved over
  the last one, and Restore the pads from a backup (`action_restore_backup_pads`) puts its pads back
  with nothing else changed. A test that drives the reset must fake `_keep_factory_pads`: it reads
  the pads and writes the real backup store.
- **"RESTORE THE FACTORY VOICE EFFECTS" REBUILDS EIGHT PADS, NOT SIX, AND IT IS DESTRUCTIVE.**
  `soundpad.restore_factory_fx_pads` clears and recreates grid positions **9 to 16**: the six voice
  effects (9, 10, 11, 13, 14, 15) plus Censor (12) and Ducking (16). All eight qualify because they
  rebuild from tables in `soundpad.py` with no WAV to find, which is what separates them from the
  eight factory SOUNDS. Nothing at grid 17 or above is touched.
  **THE BUTTON'S NAME IS WHY THIS GETS WRITTEN DOWN AS SIX.** Any UI or help copy describing it has
  to say eight and has to warn that a user's own pad on 12 or 16 is destroyed.
- **THE FACTORY DEFAULT SHOW CARRIES THE VOICE-FX PADS BUT NOT THEIR EFFECT DEFINITIONS**, so a
  software factory reset hands back six FX pads with the right name, type and colour that do nothing
  at all. Proven 4th September 2026 from captures taken weeks earlier: `pre-showreset-raw.bin` holds
  all seven region-B objects, `post-showreset-raw.bin` holds only the special one at 65 with all six
  pad records present, and `default-show-raw.bin` shows the template is built that way.
  The mechanism is an asymmetry already recorded in SOUNDPADS.md and never joined up: the firmware
  GARBAGE-COLLECTS a region-B object the moment no pad claims it, but NEVER ALLOCATES one - only an
  explicit effects block (a 0x9d frame) does that. So `newFromDefault` drops the pad table, the GC
  takes all six objects with it, the default show writes the records back, and nothing re-allocates.
  Object 65 survives because it belongs to no pad. A show IMPORT is unaffected
  (`post-showimport-raw.bin` holds all seven): an exported show carries the objects.
  **`soundpad.heal_fx_pool` is the repair, and every path that can rewrite the pad grid must end with
  it** - the software reset and the clean profile restore both do. It is a no-op when nothing is
  missing, so it costs one dump on a healthy desk. Nothing on screen can show this fault, which is
  why it must never be left to the user to notice.

- **SYSTEM PADS (padType 6, bank 8: Input 1-4, Scene A/B, Auto, Cut) ARE NOT LATCHED PADS.** Their
  `padActive` is the desk showing which input and scene it has selected. `latched_pads` walked every
  record without filtering, so `unlatch_all` "made safe" by switching off the desk's own Input 1 and
  Scene A (seen 4th September 2026 at the end of a factory-FX rebuild), and `pad_shortcuts._pad_verify` would
  unlatch Input 1 and announce it had fired the wrong pad. Filter on `SYSTEM_PAD_TYPE`, and note the
  flag vector still carries one entry per pad INCLUDING them, so skipping must not shift the index.

- **`soundpad.clear_pad(position)` TAKES A STREAM POSITION, not a grid and not a padIdx.** To clear
  a pad you can name, call `clear_grid_pad(grid)`, which resolves the position itself. Passing a
  grid-1 to `clear_pad` silently clears whatever record sits at that position: on 29th August 2026
  a benchmark did exactly that and destroyed two live mixer pads (Censor on grid 12, Back Channel on
  16). Both were rebuilt from the as-found baseline, which is the only reason it cost nothing.

- **`padTriggerCustom = False` is a DISCARD, not a park.** Writing that flag makes the DESK reset
  control, channel, on and off to defaults, and setting the flag back does NOT return them.
- **`padMixerTriggerMode` and `padTriggerMode` agree: 0 Momentary, 1 Latching.** Ear-proven on a
  Back Channel pad holding 1 that latches, and corroborated by the factory grid, where Censor is 0
  (hold to bleep) and Ducking is 1. Do not invert either one to match the other.
- **Write play mode BEFORE loop.** Selecting One Shot clears `padLoop`.
- **Envelope fade points are absolute POSITIONS (0.0 to 1.0), not lengths.** A fade point outside the
  played part is accepted by the desk and does nothing whatsoever, with nothing on screen or in the
  record to show it, so `soundpad.envelope_warning` says it in words.
- **`tidy_unused_pad_audio` REFUSES to run when the live read reports no pad files at all** unless
  the caller passes `allow_empty`. "Empty grid" and "failed read" are indistinguishable from there,
  and one of them would wipe every sound.
- **`clear_pad` is HID-only ON PURPOSE** (no `remountPadStorage`, no file writes, hence no drive
  toast). It leaves the wav behind; the sweep or the manual button collects it.
- **Every pad write enters transfer mode through `soundpad._transfer_mode`**, whose `finally` always
  leaves it. A bare `set_transfer_mode(True)` with the exit on the next line leaves the desk mounted
  when anything between them raises (a copy that fails six times does). Transfer mode does not
  remove the desk's audio devices (measured 10th October 2026, Standard USB).
- **A high-level create's `ok` is NOT "the pad was made"**: it also needs a clean pool, and a
  harmless partial object in the middle makes it False. Ask `created`. The create and every setter
  report failure in the return value rather than raising, so `restore_pads` checks each one.
- **A voice-FX pad EDITED IN PLACE restores at the recipe's values, not the edited ones.**
- **A restore skips a voice-FX pad only when its region-B object matches the recipe IN FULL**:
  all twenty properties, compared as float32, against `pad_records.expected_effect_object`, which
  reads the same `_EFFECTS` table the create writes. Never narrow it to the engines that are on: the
  object is the whole of what a rebuild would write, and a partial compare could leave a pad wrong
  that a rebuild would have fixed. The pad must also rest disengaged with `padEffectTriggerMode` 1,
  and the pool must be clean. `test_fx_skip_live` is the hardware proof (all six factory pads match;
  a one-param edit is caught).
- **Keep the pool clean, and remove each kind of orphan only its own way** (`SOUNDPADS.md`
  section 5). An ATTACHED orphan (effectsIdx stored last) goes by bare bind + clear; a delete by
  tree path does not touch it. A DETACHED one (effectsIdx stored first, left by RODE's app) and a
  PARTIAL one (no effectsIdx) go only by the path delete, and ONLY when last. Any removal from the
  middle of the list makes the next FX clear leave a partial object, which is why every in-use clear
  goes through `clear_slot` and never a bare `clear_pad`. `remove_orphan_objects` and
  `remove_partial_objects` stop on a pass that removes nothing; never turn that into a retry loop.
- **`find_pad_volume` MUST READ BYTES before returning a root, not just stat it.** `isdir` and
  `disk_usage > 0` were each added after a failure of this class and the volume passed BOTH and then
  died on the first real read with EINVAL (29th August 2026, the as-found baseline, pad 1). The
  readiness test has to do the SAME KIND of I/O as the use it gates, or it is not a test. Do not
  "optimise" `pad_volume_readable` out of the wait loop: a not-ready volume returning False is what
  keeps the remount loop going, and this guards backup, restore AND create.
- **The pad volume is the drive on the DESK'S OWN USB DEVICE, found by hardware
  (`desk_drives.on_desk`), never by what is on it.** A USB stick holding a copy of the pad folder
  passes every content test, and taking it sends uploads to it and lets the tidy delete its files.
  Do not go back to judging by timing (the drive that appears after entry): the desk's letter stays
  assigned out of transfer mode as an empty drive, and after an exit it can linger over 60 s still
  listing `pads` from cache, so a before-and-after comparison counts the desk's own letter as
  somebody else's and refuses it. A desk left in transfer mode needs no exit first; its volume is
  simply found. Inside a `transfer_session` every find returns the session's first root.
- **Wrap pad-volume I/O in `rcp_transfer.quiet_drive_errors`.** A half-mounted removable volume makes
  Windows raise a MODAL "There is a problem with this drive" box on our behalf, for a condition we
  already retry and already report better. Shaun has to find and dismiss it with a screen reader.
  Thread-scoped on purpose: never swap it for process-wide `SetErrorMode`.
- **EVERY TRANSFER-MODE EXIT DISMOUNTS THE PAD VOLUME FIRST** (`rcp_transfer.dismount_volume`, from
  `_set_transfer_mode_raw`): flush, lock, `FSCTL_DISMOUNT_VOLUME`. The exit pulls the media without
  warning, and otherwise Windows keeps its mount, cached FAT and free-space bitmap included, across
  the desk's own changes to the disk. A FAT_FILE_SYSTEM bugcheck (0x23, in
  `FatDeallocateDiskSpace` while the tidy deleted a pad file) happened with no dismount; it did not
  reproduce in 40 cycles, so this is the likely fix, not a proven one. A refused lock still
  dismounts. **The volume arrives with its boot-sector dirty byte (0x41) already set**, with nothing
  written and after a clean dismount (Pro II, fw 1.7.6), so the "There's a problem with this drive"
  notification is the desk's doing, and the dismount does not stop it. Do not clear the byte: the
  desk appears to keep its own side mounted.

## CallMe

- **AN ANSWER IS A COMPOSITE WITH A FALSE BOOL, AND THE BOOL DECIDES WHETHER THE AUDIO OPENS.**
  `sipIncomingCallAccept` takes `{"id":<id>,"sip_slot":<slot>}` plus a bool, the shape the
  touchscreen writes. The string alone is stored and ignored; with a TRUE bool the slot goes
  Connecting and fails 60 s later as "rtp stream error", which looks exactly like a network fault.
  Slot -1 declines. Byte form in section 8.2 of the protocol spec.
- **A HANG-UP IS AN EDGE, NOT A VALUE.** `sipSlotCallDisconnect` ends the call when its first
  bool rises from False to True, so `callme.build_hangup` sends (False, False) then (True, False).
  Sending only the second does nothing whenever the slot already holds it, which is its resting
  value, so a one-write version passes the first test and fails every call after.
- **CallMe has its own Watcher, not a branch of the announcer**, because the announcer stops
  following the desk when announcements are off and a call still has to be tracked and
  answerable. Its speech goes through `speech.say`, which is where the switch is enforced. **The
  call state it answers with is the desk model's** (`Watcher.snapshot` is `callme.read` over
  `desk_model.snapshot`); it keeps only what the desk does not: missed calls, call lengths, each
  slot's `ended` count, and who is ringing and on each slot as heard (see the next rule but one
  under the model).
- **`online` IS PART OF `Watcher.snapshot`, SO IT MOVES `version`.** The page redraws only when
  `version` moves, and the network is not a CallMe property: set only in `server._callme_payload`,
  the Not ready group's "desk is offline" waited for F5 to appear and to clear.
- **A REDIAL DOES NOTHING UNLESS THE PROPERTY IS CLEARED FIRST.** The desk acts on a change to
  `sipOutgoingCallDetails`, so dialling the code it already holds is stored and ignored; the reply
  then times out with no clue why. `callme.build_dial` writes the factory empty string first.
  Answering never meets this because every call has a new id.
- **A dial reports ringing only after a second of Connecting.** An unknown code passes through
  Connecting before it fails 0.4 s later, so the first Connecting push is not proof the call is
  ringing. Each slot's `ended` count, bumped by the Watcher, is how the reply tells a NEW failure
  from the reason the previous call left behind.
- **Refuse a dial to this unit's own code**: it rings this desk, and on Lite the one slot is already
  busy dialling, so it can never be answered.
- **"Expiring" is a connected state.** A call on the Lite or standard plan spends its last five
  minutes in it, so everything that asks "is a call up" uses `callme.CONNECTED` / `callme.UP`,
  never `== "Active"`: a bare comparison makes the Hang up button refuse a live call.
- **Extend is a PAIR, `sipSlotCallExtend` = (True, True) on the slot node** (`callme.build_extend`),
  captured from the touchscreen. A single bool is stored and ignored. Auto-extend runs from the
  Watcher's `on_expiring` hook, which is called under the Watcher's lock, so it only starts a
  thread.
- **CallMe codes in tests are made up, never a real desk's.** The repo is public, and a code plus
  the guest link rings that desk.
- **The call id comes from the `sipIncomingCallDetails` push, never from a counter.** It rises by one
  per call today, but nothing promises that.
- **A GUEST'S CALL NEVER REACHES A DESK WITH CALLME 1 (source code 16) ON NO FADER, OR AN OFFLINE
  ONE** (10th October 2026). With no fader, Join Call on the guest's page does nothing; offline, it
  spins for a few seconds and gives up. Either way the desk pushes nothing at all, so neither shows
  anywhere but in `callme.readiness`. While either is wrong the CallMe page is that list alone,
  with a fix beside each; every item must keep a focusable fix, or the page has nothing to Tab to
  (the fault the Recording page's "nothing is ever hidden" rule exists for). Hosting off is not an
  item: a call out still works. Online comes from the desk model (`desk_model.online`), since the
  page's poll reads no dump. **ON A FADER COMES FROM THE FADER LAYOUT, NEVER FROM CALLME'S OWN STATE**: the page's from
  the window's desk state (`callme_section.problems`), the answer reply's from a read of the layout
  (`server._callme1_on_fader`). The Watcher hears only `sip*`, and the desk does not push a layout
  change this app makes, so a copy kept with CallMe's state goes stale. A desk can show Wi-Fi on, joined to nothing and scanning empty with its radio stuck; a power
  cycle cleared it.
- **THE RING AND THE CALLER'S NAME TAKE TURNS, NEVER OVERLAP.** The speech thread plays one item at
  a time and keeps only the newest one waiting, so a name and a ring queued together lose one of
  them. So `_callme_ring` queues one ring, waits `tones.RING_DURATION_S`, speaks the name with
  `speech.say_and_wait`, and starts again every `_CALLME_RING_CYCLE_S`. It stops when the SAME call id
  is no longer ringing, the ringtone is switched off, or past the desk's 120 s.

## The desk pushes, and does not echo

- **The desk pushes changes made ON the desk and does NOT echo all of our own writes** (a software
  solo pushes nothing). Two consequences, and both have bitten more than once:
  - Anything that compares a pushed value against what it knew must know it from a READ, or the
    desk's first resting push reads as a change (the phantom "Levels" and "Bank 1"). The desk model
    is read at start and the comparison is `push.was`.
  - Our own writes reach the desk model as they are sent (`rcp_hid.note_written`), and the window's
    desk state hears them as pushes marked `ours` (`own_writes`), whoever made the write: a
    shortcut, an encoder hold, a profile.
- **The exception, proven 28th August 2026: it DOES push most of our own property writes back on
  report 04.** That is why the announcer needs the 1.5 s echo-suppression window per target. The two
  facts sit at different layers; do not collapse them. Since our write reaches the desk model first,
  its echo arrives with `was` equal to its value, so anything that speaks only on a change (the
  bank, an output mute) is already silent for it: a bank a pad shortcut selects is said by the
  shortcut, not the announcer.
- **EVERY PHYSICAL-CONTROL PUSH IS ONE WHOLE RECORD, AND THE DUMP FOLLOWS A MUTE SET AT THE DESK.**
  Measured on the Pro II, fw 1.7.6, 31st October 2026: four minutes of mutes, solos, the encoder, a
  fader and pads gave 1124 pushes, none of them a malformed frame, so `rcp_read.parse_push` (and the
  announcer) hears all of them. Mutes held on at the desk read True in `channelOutputMute` in the
  dump taken while they were held, so the app's mute display from the dump is right. `potLevel` was
  never pushed.
- **ONE LISTENER, ONE HANDLE: `desk_live`.** Started by `service.start` (or by the first
  subscription), it reads every report-04 frame, decodes each push once and hands it to each
  subscriber that named it. **NOTHING A SUBSCRIBER DOES RUNS ON THE READER**: each has its own
  queue, and a `Follower` its own thread, because encoder hold's mute is a write under the HID lock
  and CallMe rings and speaks, so one slow consumer would stall the rest and a write waiting for its
  own push would wait on itself. Do not open another long-lived read handle; subscribe.
- **ANYTHING THAT MUST STAY CURRENT WITH ANNOUNCEMENTS OFF FOLLOWS THE DESK ITSELF, never through the
  announcer**, which unsubscribes when switched off. A fact about the desk is the desk model's,
  which the listener keeps whatever is subscribed; a follower of its own is for what is not a desk
  property (`pad_shortcuts.follow_desk`'s playback tracking) or reacts to a change
  (`channels.follow_desk` dropping the write cache).
- **A FACT ABOUT THE DESK IS HELD ONCE, IN `desk_model`. Do not keep a copy of one anywhere else**;
  read the model. Every copy that existed (the announcer's mutes, levels, bank and selected channel,
  encoder hold's selection, the pad bank, the recorder's state, the naming layout, the Expanded flag,
  the server's session state, CallMe's call state, the online flag) went stale in its own way. What a consumer keeps is its own
  bookkeeping: what was said, a ring position, an elapsed count.
- **THE MODEL READS NOTHING OF ITS OWN.** It adopts the dumps other readers take, and a caller that
  needs it current reads the desk itself (which is then adopted). A background dump would land
  mid-reset or mid-re-enumeration, which is exactly what `PushListener` exists to avoid.
- **A DUMP IS ADOPTED WITH WHAT WAS PUSHED DURING IT REPLAYED ON TOP** (`desk_model._journal`), because
  a dump takes a second and more and the desk keeps changing. A dump begun on an earlier connection
  (`rcp_hid.connection_epoch`) is never adopted, and only a dump that ended on its declared length
  is offered.
- **TRUSTED IS FOR ACTING, THE LAST VALUE IS FOR NAMING.** `desk_model.trusted()` holds while the
  listener is up, a dump has been adopted on this connection, and no `STRUCTURAL` change (a fader
  source, the USB mode, a mic type) has arrived since it started. A toggle's direction or a level
  step reads the desk when it is not trusted (the cough key, `commands._known_level`); a name takes
  the last layout seen, because "Mic 2" from a stale layout beats "Channel 7".
- **OUR WRITE IS APPLIED ONLY AT A FIXED NODE THAT ALREADY CARRIES THE PROPERTY** (`apply_write`). A pad
  edit is written at the edit cursor, whose path is not the pad it edits, and a property the node
  lacks is a stray, not a value.
- **THE MODEL IS UPDATED BEFORE A SUBSCRIBER HEARS THE PUSH**, so a subscriber that looks at the
  model sees the value it is being told about. Keep `apply_push` ahead of `_fan_out` in the reader.
  **The flip side: the model may already hold LATER pushes too.** A subscriber that narrates in
  order reads a transition from the push (`Push.was`), never from the model, and remembers a
  neighbouring value as it heard it: a call's end can arrive after its slot's address is cleared
  (`callme.Watcher._peer`).
- **LISTEN FOR THE ANSWER, DO NOT RE-READ THE TREE UNTIL IT APPEARS.** `desk_live.PushListener`: a
  subscription on the one listener, no HID lock. A factory reset pushes `currentShowUUID` itself at
  about t+1.75 s. **START IT BEFORE THE WRITE** - the burst starts 0.02 s after, and only pushes
  heard after `start` are queued.
- **"IT WORKED" AND "IT HAS FINISHED" ARE DIFFERENT QUESTIONS.** The reset's uuid arrives at t+1.75 s
  and the desk keeps rewriting itself until t+4.96 s, so acting on the answer means writing to a desk
  mid-rebuild. Use `wait_until_quiet()` before the next write, as `_reset_and_wait` does before
  `wake_channels`. The old polling was slow enough to hide this.
- **AN IMPOSSIBLE EXPORT CAN ONLY BE SEEN BY LISTENING.** With no card or drive in, the desk pushes
  three frames and moves NOTHING in the tree, and `showControlLastError` LATCHES at 8 (a reset does
  not clear it). So no read can tell a refused export from one that never happened.
- **A hidapi HANDLE MUST BE CLOSED BY THE THREAD THAT READS IT.** Closing one from elsewhere while a
  read is in flight segfaults the interpreter (exit 139). The desk listener closes on its own
  thread, and `desk_live.stop` joins it.
- **A WRITE IS ADDRESSED FROM THE DESK MODEL ONLY WHILE IT IS TRUSTED.** `channels.strip_vector`
  takes the model's layout when `desk_model.trusted()`, and a fresh dump otherwise;
  `last_known_strip_vector` is the layout as last seen, for naming. Trust rests on every layout
  change reaching the model: the touchscreen pushes each one (and untrusts the model, as a
  `STRUCTURAL` change, until the next dump), our own writes reach it as sent, and a show load
  calls `desk_model.distrust()`. **No software's fader move is pushed**, RODE's app's included,
  and no other write of RODE's app is either (captured on 1.7.7: `screenDimAfterSeconds` at [7]
  and `masterCompellorOn` at [14], each one clean record, no report-04 frame back), so a page that
  follows pushes cannot see a change made there. **NEVER READ THE DESK ON A TIMER WHILE RODE'S
  APP IS OPEN:** every reader is sent every dump's frames, and one read every 3 s kept RODE's app
  on "Synchronizing device" for good (measured). Instead, while it is open, `desk_model.reveal`
  compares every read with the picture and hands on what changed as pushes, and the window reads
  once when it comes back to the front (`server.read_for_rode_app`). While RODE's app is running (`rode_app.is_open`) every layout-addressed write reads the desk,
  and the user is warned once per run of it. `channels.follow_desk` also untrusts the model on a
  derived chain value pushed from elsewhere, ignoring our own writes' echoes for `CHAIN_ECHO_S`.
  A read for a reply follows the same rule through `desk_access.desk_now` (the mute's reach check, the
  encoder hold, the status bar's re-build after a push): a fresh dump per mute made a three-mic
  cough key take 1.5 s, and the status bar's dump after each change held up the next press.
  Current state still reads the desk when opened: a card's free space is never pushed.
  Other software moving faders is seen only at the next read: an accepted limit, which the help
  covers. A new route that moves a fader source without a push must call `distrust()`.
- **RODE'S APP OPENING IS SPOKEN, AND NOTHING ELSE.** No dialog: a write is already safe (the
  faders are read before it) and what RODE's app changed is caught on return, so a warning would
  only be in the way. A background process cannot take the focus from the app in front, so the
  app's own voice is the one channel heard whatever is in front; a tray notification
  (`tray._notify`) has never been seen to arrive on this Windows 11 PC. **The desk is read once
  more as RODE's app closes** (`server.follow_rode_app`), still compared: a change made there
  just before it quit is otherwise never seen, since the window reads on return only while RODE's
  app is open.
- **A CLEAN PROFILE RESTORE IS A SHOW OPERATION, so anything true of shows is true of it.**
  `restore_profile(clean=True)` sends `shows.send_new_from_default()` (the software factory reset)
  and takes an export first as the undo. Two consequences people keep missing: the profile and show
  features are NOT independent, so "we could drop shows" does not remove the reset or its risks; and
  every warning that belongs on a reset belongs on the clean restore too. This is also why the dead
  mic was a CLEAN-RESTORE bug and not a Shows-page bug. A MERGE restore touches none of it. The page
  does not offer a clean restore (the reset's crash risk does not belong on a routine restore); it
  is reachable through the API and the DevTools tests only.
- **A CLEAN RESTORE CLEARS EVERY USER PAD THE PROFILE HAS EMPTY** (`soundpad.clear_pads_not_in`),
  because the reset refills the factory pads. It runs AFTER the pad transfer session closes: a clear
  only completes when the desk leaves transfer mode. A merge restore leaves unmentioned pads alone.
- **THE VERSION NUMBER LIVES IN `Project/version.py` AND NOWHERE ELSE, INCLUDING THE EXE'S WINDOWS
  VERSION RESOURCE.** `AccessCaster.spec` is ordinary Python at build time, so it reads that file
  with a regex and GENERATES `ProjectBuilds/version_info.generated.txt` (gitignored); `installer.iss`
  takes the same string via `/DAppVer`. **Do not hand-write a version resource file** - that is a
  second place holding the number, which is the one thing this rule forbids. The spec RAISES on a
  version it cannot parse rather than stamping 0.0.0.0. Both exes carried a BLANK version until
  3rd September 2026, so the number appeared only in the installer's filename.
- **THE MINOR IS A TWO-DIGIT FIELD: 2.50 is "2.5", the way 2.10 is "2.1".** So a plain `"2.5"` must
  never be written into `version.py`: Windows compares the resource fields numerically, and 2.5
  becomes 2.5.0.0, which is OLDER than 2.10.0.0. Verified both ways on 3rd September 2026 -
  `2.50.0.0 -gt 2.10.0.0` is True.
- **A SPOKEN CHANGE MUST BE WORDED FROM THE TRANSITION, NEVER FROM THE NEW STATE ALONE.** "What is
  the desk doing" and "what just happened" are different questions, and a state->words map only
  answers the first. Plugging the pen drive in announced "Recording stopped" until 3rd September
  2026, because storage arriving moves the recorder NO_DESTINATION -> READY and READY's own word is
  "Recording stopped". Fixed in `recorder.spoken_change(was, now, medium)`; `STATE_SPOKEN` is still
  the state vocabulary and is correct for the page's label. Two things fall out of it that are worth
  keeping: `recordState` 3 is a STORAGE state and not a transport one, so a card or drive going in or
  out is identifiable from the pair of states with no extra device read; and a `was` of None is not a
  change, which is what stops the app greeting you with "Recording stopped" on startup. The
  announcer-SUSPENSION decisions stay keyed on the new state, correctly: announcements are off only
  while RECORDING, whatever the reason the desk left it. A PAUSE gives them back, because nothing
  reaches the take then and a silent pause sounds exactly like recording. A resume from the app
  speaks "Resuming" and waits for it to finish (`speech.say_and_wait`) before it sends the command; a
  resume from the unit's button is already recording by the time we hear it.
- **A RECORD TONE IS QUEUED IN `recording._state_changed` BEFORE `suspend_for_record`, AND PLAYS FOR
  THE APP'S OWN PRESSES TOO** (`announcer.record_feedback` "tone", `recorder.record_cue`). The speech thread
  plays what is already queued after announcements go off, but `speech._submit` drops anything
  submitted after: queued after the suspend, or left to `action_record`'s reply, which returns after
  the watcher has already suspended, a start is silent. That is also why a shortcut's spoken reply to
  a start is never heard. With tones on, a resume from the app skips "Resuming": the start tone marks it.
- **RECORDING'S SOUND IS ONE OF THREE, NEVER TWO AT ONCE** (`announcer.RECORD_FEEDBACK`: speak, tone,
  silent). Speech after a tone repeats it and lands in the take. "Say nothing" silences only the
  transport changes `recorder.record_cue` names; a card or drive going in or out, and a take ended by
  a pulled drive, are still spoken. Announcements are still suspended for the take whichever is
  chosen: that is the separate "Silence other announcements while recording". The earlier
  `announcer_record_tones` tick box still reads as "tone" (`_saved_record_feedback`).

## Reading the model dump

- **A DUMP ENDS AT THE LENGTH IT DECLARES, NOT WHEN THE LINE GOES QUIET, AND PUSH FRAMES ARE
  DROPPED. Both halves of `rcp_read.read_model_raw` are load-bearing, and removing either brings
  back a fault that was live for two months.** Quiet detection alone could not tell the dump from an
  asynchronous push, because both are report-04 frames, so a playing pad (which pushes `padProgress`
  about 20 times a second) meant the line never went quiet: the read ran to its 4000-read ceiling,
  240 s worst case and 172 s measured, AND every push frame was appended, so the blob came back
  contaminated as well as late (264180 and 139485 bytes against a 136170 baseline, both mistaken at
  the time for "an unstable read"). It bit far wider than the pads page, because every per-channel
  write resolves its strip through `channels.strip_vector`, which can dump.
  Fixed 2nd September 2026; `DevTools/test_read_under_push_live.py` pins all of it, including a
  deliberate run of the OLD loop so the two faults stay demonstrated rather than remembered.
- **THE DUMP'S FIRST FRAME OPENS WITH ITS OWN TOTAL LENGTH: four bytes little-endian, counting
  everything after them.** 136675 on a Duo and 143187 on a Pro II, identical on every dump measured.
  It is the first four of the five header bytes `model_tree.parse` skips before `Rodecaster`. This is
  what lets the read end on STRUCTURE. An implausible value falls back to quiet detection rather
  than trusting it, and `rcp_read._last_read["bounded_by"]` says which happened.
- **A PUSH IS ONE WHOLE RECORD ALONE IN ITS FRAME: `04 <len32> <record> <zeros to 256>`.** Real ones
  are 20 to 30 bytes, so 220-odd bytes of padding, where a dump frame is 255 dense bytes with at
  most a handful of trailing zeros. `rcp_read._push_record` decides it STRUCTURALLY, via
  `model_tree.parse_record`: the record must parse completely and account for exactly the declared
  length, and every byte after it must be zero. Do not reduce that to a size threshold - a false
  positive silently punches 255 bytes out of the middle of the model.
- **EVERY LISTENER DECODES A PUSH WITH `rcp_read.parse_push`, NEVER BY SCANNING THE FRAME FOR A NAME.**
  The desk listener receives the model dump's frames too, because Windows gives every open handle
  every input report. A name scan finds
  property names inside a dump slice and reads garbage either side of them; it also glues a
  printable selector onto the name (`mutePressed` at [0, 0x63] reads as `cmutePressed`).
  `parse_push` answers None for anything that is not exactly one record, so a page reading the desk
  can never sound like the desk changing.
- **THE DUMP BURST IS ATOMIC.** Measured with a pad playing throughout: pushes arrive BEFORE the
  first dump frame and AFTER the last, never between two of them, 536 uninterrupted dump frames every
  time. That is why dropping push frames cannot leave a hole. It is also why a HEALTHY read now skips
  ZERO pushes - the length bound closes the handle before the queued ones are read - so a zero skip
  count is the fix working, not the test missing.
- **THE TWO WORKAROUNDS SHAUN REJECTED, so do not quietly reintroduce them:** capping the read with a
  fallback to the last known layout, and keeping the layout cache warm off the push stream. Both hide
  the fault instead of fixing the read.
- **`rcp_read.values(blob, name, kind=None)` IS HOW YOU READ A LIST OF VALUES, and `first(...)` how
  you read one.** `find_all` under them returns `(value, kind)` pairs and is only for a positional
  read that needs the kind. Unwrapping that pair by hand is what produced 32 call sites and four
  separate copies of `def val(prop, kind=None)` before 30th August 2026.
- **`first` returns the first MEANINGFUL value: it skips None and `""` and KEEPS `False` and `0`.**
  Not a nicety. A name can occur in several blocks with only one filled in, and
  `systemFirmwareVersion` is the standing case: `""` in one section, the version in another. Taking
  the literal first occurrence returns a blank often enough to matter. `False` and `0` are real
  settings and must never be skipped.
- **`find_all` HAS NOT BEEN A SUBSTRING SEARCH SINCE 29th August 2026.** It matches whole names
  through the tree index. Three comments still warned that `screenBrightness` would match inside
  `hwScreenBrightness` and were steering people away from the right tool; corrected 30th August
  2026. **If you find another, fix it rather than believing it.**
- **A NAME CAN OCCUR IN MORE THAN ONE BLOCK, and that is still real.** `MIX` and `RCSYNCMIX` share
  six names, and a stray adds a copy wherever it lands (`btVisible` had one in SYSTEM until a factory
  reset, and only the NETWORK copy drives the radio). Use `block_value` and say which block you mean;
  `outputMonAutoMute` and `outputBTAutoMute` are read by block in `routing_model` for this reason.
- **A positional read must pass `kind=None`** so it gets EVERY occurrence including the
  undecodable `(None, "?")` ones. Dropping those shifts every index past them.
- **EVERY INPUT VALUE, ON EVERY DESK, LIVES AT `INPUTSOURCE[source_code]`** (root child
  `0x26E + code` on both desks' current firmware). Mic gain, phantom, polarity, mic type,
  instrument gain, line level, input type and strip colour. Read it with `channels.input_value`,
  write it at `DeviceModel.input_key`, derive nothing else.
  There is no per-device selector table any more and there must not be one again: the two that
  existed were a second encoding of this rule, and the Duo's copies of them encoded a different and
  inert address. Never read one of these by POSITION either - indexing a `values()` list by channel
  number reported one Duo mic's gain as another's (fixed 31st August 2026).
- **A WRITE THAT LANDS AND READS BACK CAN STILL DO NOTHING, AND THE DUO'S MIC GAIN IS THE PROOF.**
  Until 2nd September 2026 the app wrote a Duo's input values to a MIX node at `0x48 + source_code`.
  The write landed, read back, returned `ok: true`, and the audio never changed, because the engine
  keeps the live value at INPUTSOURCE. The display was stale in the other direction for the same
  reason: after any desk-side change the app re-read its own inert copy and believed it.
  **Byte-for-byte agreement with the RODE app is NOT evidence that a write does anything.** That
  address came from a real capture - RODE's app does send `01 01 01 02 48 01`, host to device, one
  standalone record identical to ours - and it is still dead when WE send it.
- **TO TEST WHETHER A WRITE IS LIVE WITHOUT EARS, MAKE THE ENGINE ANSWER.** Pick a write with a side
  effect the DESK performs, do it, and diff the whole tree. `inputMicrophoneType` is the standing
  example: the engine applies that voicing's own gain, its phantom setting and thirteen chain
  parameters. At the dead address it moved our byte and nothing else; at INPUTSOURCE it set gain 25,
  switched `inputPower` on by itself and retuned the strip. That differential
  generalises to any property the desk derives something from.
- **BUT THE ENGINE'S MODEL IS DEVICE-AGNOSTIC, SO "THE ENGINE ANSWERED" PROVES AN ADDRESS IS LIVE AND
  NEVER THAT A FEATURE EXISTS ON THIS HARDWARE.** Both desks run ONE binary (`app.zip`, "rcp2") and
  publish the same 30-slot `INPUTSOURCE` table and the same 30 matrix rows, so the engine acts on a
  source the desk does not physically have. Measured 2nd September 2026: on a Duo, source 3 is MIC 4,
  which it has no jack for, and it accepts a fader placement, fills all thirteen of its routing
  cross-points with real levels, and answers a mic-type write by moving `inputType` and
  `inputInstrumentGain` EXACTLY as the real Mic 2 does. What is device-specific lives at
  `SYSTEM.boardType` (`RcBoardConfig::HostType`, 1 on a Duo) and in the touchscreen, neither of which
  the property tree exposes. **So run the CONTROL**: make the same write to a slot the desk cannot
  possibly have, and if it behaves the same, the test measured nothing. Without it, two successive
  verdicts on the Duo's stereo pair codes were wrong before the third stood up.
- **THE FIRMWARE IS THE AUTHORITY THE MODEL IS NOT, and it is public.** `update.rode.com/manifest.json`
  names `rcp2/<version>/app.zip`: 36 MB, the two Linux binaries, md5 published beside it and verified.
  `rc_audio_mixer` is the engine, `rc_gui_juce` the touchscreen. Its string table gives up the
  `InputSourceType` label list ("Input 1", "Input 2", "Headset", ... "1 + 2", "2 + 3", "3 + 4") and
  class names like `InputAssignSourcePage::LinkButton`, which is how the stereo-pair codes were shown
  to be firmware-wide rather than per-desk. Embedded Linux (Amlogic); the touchscreen is JUCE, with
  a 4787-entry UI string table. RODE's, so it stays OUTSIDE the repo; `DevTools/firmware_dig.py`
  looks for a `rode-firmware` folder beside the repo or in your home folder. **Look here before
  calling anything blocked.**
- **THE DUO MIX NODES CARRYING AN INPUT PROPERTY ARE OUR OWN LITTER, and only a factory reset removes
  them.** Of 390 MIX nodes on a used Duo, 387 carry exactly six properties; the exceptions are the
  three this app wrote to. That count is the cheapest way to spot a write landing where it does not
  belong. They are inert now nothing reads them, and the July 2026 "strays" that cost a factory reset
  were the same thing seen without the explanation. It also means a Duo dump's property COUNT grows
  with use, so never read meaning into a count difference without asking how each desk has been used.
- **ABSENT AND ZERO MEAN OPPOSITE THINGS**, which is why `rcp_read.path_value` takes a `MISSING`
  sentinel rather than defaulting to None.
- **RODE'S OWN DUO GUIDE UNDERSTATES THE FADERS: it says four physical and THREE virtual, seven in
  total.** The desk on firmware 1.7.3 has NINE (four physical, five virtual), which is what its model
  publishes - nine FADER nodes - and what RODE's release notes for 1.7.3 describe. The guide page was
  simply not updated. RODE's guide, at `rode.com/en-us/user-guides/rodecaster-duo`, is worth reading
  for how the hardware is laid out and NOT for counts; a count read off it once already produced a
  wrong fader model. The same page is still the authority on the headset: one 3.5mm TRRS jack on the front,
  whose microphone is its own input channel and whose earpieces share the Headphone 1 mix and are
  turned up with the Headphone 1 encoder.
- **THE ANNOUNCER MUST READ A PUSHED ADDRESS IN FULL, not just its low byte** (the path
  `rcp_read.parse_push` returns). `0x026E` needs two bytes and `0x6E` alone is an ordinary one-byte selector elsewhere. Taking the
  low byte and looking it up in a per-device table is how a gain turned ON THE DESK went unspoken on
  a Duo: the table held the dead selectors, the lookup missed, and nothing was said at all.
- **`rcp_read.read_settings` returns RAW STREAM-ORDER arrays and is a diagnostic, not a per-channel
  read.** Do not index it by mic number. It is what the old broken read was built on.
- **`source_code` is THE per-channel number.** It addresses the input on both desks, indexes the
  INPUTSOURCE table, names the matrix source, and mic channel number is `source_code + 1`. There
  used to be an `input_slot` beside it holding the identical value in all 16 entries across both
  desks; two names for one number is what made a positional input read look defensible. Removed 31st
  August 2026, and `test_input_read.py` fails if it returns.

--------------------------------------------------------------------------------
## The app's own settings (export / import)

- **THE EXPORT IS AN ALLOW-LIST, NEVER A DUMP OF settings.json.** The file holds state about this
  machine and its desks beside the preferences, and an export is a file people email to themselves
  and import elsewhere. `app_settings.EXCLUDED` holds that state, each key with its reason at the
  site: among them `baseline_asked` (carrying
  it would silently suppress a desk's one-and-only first-run backup offer on a new machine), and
  `applied_presets` and `pan_links` (each describes one desk, not a preference). **Adding a new preference means
  adding it to PORTABLE, MACHINE_SPECIFIC or EXCLUDED** - `test_app_settings.py` fails if a key the
  app writes is in none of them, so a new setting cannot quietly become unbackupable. It finds the
  keys by reading `settings_file.update(...)` calls out of `Project/`, never from a hand list: the hand
  list missed three. A preference the import should apply LIVE also needs a line in
  `speech.apply_settings` or `announcer.apply_settings`, and a voice or output change bumps
  `speech.config_gen`.
- **IMPORT MERGES, IT NEVER REPLACES.** The excluded keys live in the same file, so a replace would
  delete this machine's first-run record as a side effect of restoring somebody's shortcuts.
- **`hotkeys` IS `{"enabled": ..., "shortcuts": [...]}`, NOT A MAPPING OF SHORTCUTS.** `len()` on it
  returns 2 for ever, whatever the user has, which is exactly what shipped for an hour on 2nd
  September 2026 and reported "2 keyboard shortcuts" for a machine with six. Count through
  `app_settings.count_shortcuts`. The first version of the test invented its own fixture shape and
  so passed while the counting was broken: **a fixture that is not the real shape tests nothing.**
- **AN EMPTY "what this machine has" LIST MEANS "CANNOT TELL", NOT "HAS NONE".** SAPI enumeration
  fails over RDP, so the voice list comes back empty there; refusing to restore a voice on that
  basis would silently drop the setting. `merge_import` only rejects a name when it has a real list
  to check it against.
- **EVERY USER-DATA JSON FILE IS WRITTEN WITH `paths.write_json`, NEVER `open(..., "w")`.** It
  writes a temporary file beside the target and swaps it in, so a kill or power cut mid-write leaves
  the old file whole; written in place, a half file parses as nothing and the next
  `settings_file.update` writes a file holding only its own key. **Every one is read with
  `paths.read_json`**, which keeps a file that will not parse as `<name>.bad` and raises when the
  file cannot be opened, so no caller saves over a file it could not read: `settings_file.update` drops
  the change then, because writing would replace every other key with nothing.

## AccessCaster's own updates

The scheme is shared with two other apps, Hydra and Claudio, which read the same signed index from
the same bucket: `selfupdate/` is a copy of Claudio's port, and `selfupdate/versions.py` is kept
verbatim against the publishing tool's own copy. When to ask and who is told is `app_updates.py`.

- **THE VERSION IS THREE-PART SEMVER** ("2.63.0", "2.64.0-beta.1"). `UpdateVersion.parse` refuses
  "2.62", and a build whose own version it cannot read never checks at all, silently.
  `test_selfupdate.py` parses `version.__version__`. A pre-release label puts the build on the beta
  track; the track is read off the version, never stored.
- **CHECKING IS AUTOMATIC, INSTALLING NEVER IS.** An automatic check only ever raises a Windows
  notification from the tray icon (`app_updates.notify`). Never the app's own voice: speech nobody
  asked for talks over the screen reader.
- **THE DOWNLOAD IS TWO DIALOGS ON THE PAGE, NOT ONE**, handed over in one tick (`startUpdate`).
  The other apps carry the whole update in one dialog; on this page that would be a progress view
  revealed inside an open dialog, which a screen reader never reads (see Web UI below).
- **THE SERVER WAITS FOR THE PAGE'S `proceed` BEFORE STARTING THE INSTALLER.** The page takes its
  controls away, says the closing sentence and gives it time to be read; started first, the
  installer cuts the sentence off. No `proceed` within `PROCEED_WITHIN_S` installs nothing.
- **THE APP DOES NOT EXIT ITSELF TO INSTALL.** Setup's Restart Manager closes it (the tray answers
  WM_QUERYENDSESSION and WM_ENDSESSION) and `/RESTARTAPPLICATIONS` brings it back with `--updated`,
  registered by `restart.register` just before. The restart opens the window, which the install
  closed, and App settings there takes the update's report.
- **`/VERYSILENT`, never `/SILENT`**: under `/SILENT` Setup's progress window takes focus on its
  Cancel button and a Space cancels the install. **And the installer must not prompt during a
  silent run**: removing the firewall rule an earlier version added needs a permission prompt, so
  `installer.iss` does it only on an interactive install or the uninstall, never silently.
- **"Could not download" and "did not match" are different sentences.** A complete file that hashes
  wrong is the one case the signature exists to catch; calling it a network failure hides it.
- **The user agent is `AccessCaster/<version>`.** Cloudflare refuses Python's default with a 403,
  which reads as "no release has happened".
- **Every update route is LOCAL ONLY** (`server.UPDATE_ROUTES`): a phone on the network can read the
  state but cannot start, steer or stop an install on this PC.
- **A failed download forgets the held offer AND the last-checked stamp**, so a republished index
  reaches a running app. The downloaded installer is deleted only on the launch after a confirmed
  update; every other path keeps it, because the failure message tells the user to run it by hand.

## Native window

- **THE WINDOW BELONGS TO THE TRAY, AND CLOSING IT HIDES IT** (`MainFrame._on_close` vetoes; only
  Quit's `Close(force=True)` destroys it). It is built the first time it is wanted, never at
  sign-in. On close the window hides BEFORE its page, so Windows records the focused control as it
  deactivates; hiding the page then stops its polls, which start and stop on the page's EVT_SHOW.
  `present` shows the page first, so activation lands the focus back on that control. A closed
  window says nothing (`say`). With `keep_in_tray` off, closing quits instead, through the same
  `_request_quit` as the tray's Quit.
- **A QUITTING COPY HOLDS THE SINGLE-INSTANCE LOCK UNTIL ITS PROCESS ENDS**, after its icon window
  has gone. A launch that finds the lock held and no window therefore waits for it
  (`tray._claim_when_free`) before calling it another account's copy; without that, quitting and
  reopening straight away showed the other-account warning.
- **AN AUDIO OUTPUT LEFT OPEN HOLDS THE PROCESS'S EXIT UP BY ABOUT FIVE SECONDS** (measured 3rd
  November 2026: 5.1 s with the speech output open, 0.6 s closed). The speech output stays open
  for `wave_player.CLOSE_AFTER_S` after a phrase, so `service.stop` closes it (`speech.stop`). A new
  thread that holds an audio device must be closed there too.
- **QUIT REFUSES WHILE THE DESK IS PART-WAY THROUGH A CHANGE** (`desk_access.work_in_progress`:
  any `BackgroundJob`, each of which registers itself, or `rcp_transfer.transfer_active`). A pad
  sound cut off mid-copy leaves an entry only a factory reset clears. A session end
  (Windows shutting down, or the installer's Restart Manager) is NOT refused: it goes straight to
  `_quit`, because a refusal there cannot hold anything open. So an app update checks instead,
  before it downloads and again before it hands over (`update_dialogs._Follower`).
- **THE PROCESS RUNS ITS OWN `wx.GUIEventLoop`, NOT `app.MainLoop`** (`tray.main`). wxPython's
  `MainLoop` returns at once when no window exists, so a sign-in start would exit, and it ends
  when the last window is destroyed. Quit ends the loop with `ScheduleExit`: `Exit` is ignored
  while a dialog's loop is the active one, and the process then never ends. Quit ends any modal
  dialog first, for the same reason.
- **ONLY THE INNERMOST MODAL LOOP CAN BE ENDED** (`dialogs.modal_dialogs`). `EndModal` on a dialog
  with another modal open over it is a wx assertion that leaves its loop running: the window stays
  disabled, the job's clock keeps ticking, and the process outlives its own Quit. The tray's Still
  working opens over a running `JobDialog`, so `JobDialog.finish` waits for any dialog over it to
  close, and Quit (`tray._close_window`) ends dialogs newest first, one per loop return.
- **THE TRAY'S OWN WINDOW TAKES THE PROCESS'S FIRST `ShowWindow`** (`_create_window`). The first
  call takes the show state the launcher asked for, whatever it passes, so a hidden launch would
  hide the main window while wx records it as shown, and no later `Show` brings it up.
- **wx'S DEFAULT `EVT_QUERY_END_SESSION` CLOSES THE TOP WINDOW**, which the window vetoes by hiding,
  so the app would refuse to let Windows end the session or Restart Manager close it. `TrayApp`
  binds both session events on the app itself.
- **THE OPEN SHORTCUT SHOWS THE WINDOW WITH A SENT MESSAGE AND RAISES IT ON THE HOTKEY THREAD**
  (`TrayApp._show_from_any_thread`). The foreground right belongs to the hotkey handler while it
  runs (`hotkeys._FOREGROUND_COMMANDS`); `SendMessageTimeout` keeps it running while the wx thread
  shows the window, then `app_window.raise_window` raises it from the handler. A second launch hands
  its own right over with `AllowSetForegroundWindow` before posting `WM_OPEN_WINDOW`.
- **NOTHING IN `native/` OUTSIDE `native/win/` CALLS A WINDOWS API.** Screen reader speech and
  control descriptions go through `native.announce` and `native.a11y`, which import the platform's
  own. Import `native.win` directly, or add a Win32 call to a page, and a port to another platform
  has to hunt for it. The Categories tree is a `wx.TreeCtrl` on purpose: on Windows it is the
  native tree view, where `wx.dataview.DataViewTreeCtrl` is a control wx draws itself. A port to
  macOS swaps it, because there the roles reverse.
- **NO `wx.Simplebook` OR `wx.Notebook` TO SWITCH THE PAGES.** Both move the focus into each page
  they show, which pulls it out of the Categories tree on every arrow press. `MainFrame._show`
  shows and hides the pages itself.
- **A CHANNEL PAGE'S TABS ARE A `wx.Notebook`, AND ITS SELECTION IS ONLY CHANGED BY CODE WHILE IT IS
  HIDDEN.** A notebook takes the focus whenever its selection changes on screen. So `ChannelPage`
  hides itself before building its notebook, and moves to the tab last chosen in `before_show`,
  which `MainFrame._show` calls while the page is still hidden.
- **A CHANNEL PAGE, AND EACH OF ITS TABS, IS BUILT THE FIRST TIME IT IS SHOWN, NEVER ALL AT ONCE**
  (`Page(build=...)`, `Tab.build`). Building every channel's page held the wx thread for 2.1 s at
  start, and nothing answers the screen reader meanwhile; one mic page's tabs together are about
  0.5 s. A page is built just BEFORE it is shown, for the notebook rule above, and a tab in
  `before_show`, on `EVT_NOTEBOOK_PAGE_CHANGING` and on `EVT_NOTEBOOK_PAGE_CHANGED`: **a change
  the native tab control makes on Windows names the OLD tab in PAGE_CHANGING**, and shows the new
  one only after PAGE_CHANGED, so building on PAGE_CHANGING alone leaves the user on an empty
  panel ("scrolledpanel"). A tab's panel exists from the start, so the
  tab control lists every tab; `ChannelPage.show` skips a tab not yet built, which reads the
  record when it is. `DevTools/probe_ui_stalls.py` measures it.
- **CTRL+TAB IS THE TAB CONTROL'S WHEREVER THE FOCUS IS INSIDE ONE** (`MainFrame._in_tabs`), the
  Windows convention; elsewhere, including the tree, it steps the section's pages.
- **A PAGE NEVER READS THE DESK WHEN IT IS SHOWN.** Arrowing the tree shows every page passed, so a
  read there would queue a dump per arrow press. The pages draw from the window's desk state
  (`native.desk_state`), read in full at start, on F5, on a reconnect, after a write that moves
  other values, and after a `STRUCTURAL` push; `show(rec)` puts new values on the same controls
  rather than rebuilding them, so a re-read never moves the focus.
- **THE WINDOW'S DESK STATE IS A VIEW OVER THE DESK MODEL, BUILT BY THE CODE A READ USES.** A change
  the window shows, pushed or ours, rebuilds it from `desk_model.snapshot` through
  `server.window_state`, so there is ONE decoder for a read and a push. Do not put a push's value on
  a record by hand again, and do not patch the desk state from a write's reply: our write reaches
  the model as it is sent. The rebuild updates records IN PLACE, because a channel page keeps the
  record it was built from. A channel page still `remember`s its own writes, because a channel on
  no fader keeps its chain in the app's own store, which no push or write to the desk announces.
- **THE DESK STATE BELONGS TO THE UI THREAD.** Its follower's thread only hands a push over with
  `wx.CallAfter`; the records change, and subscribers hear of it, on the thread that draws them.
- **A SLIDER IGNORES `show` FOR `HANDS_OFF_S` AFTER IT MOVES.** The desk pushes back each value
  written, and an older one arriving mid-drag would pull the slider back under the user's hand.
- **A PUSHED VALUE LANDS ON THE FOCUSED SLIDER ONCE, `FOCUSED_SETTLE_MS` AFTER THE PUSHES STOP.**
  The screen reader queues a reading of every value set on the focused control, so setting each
  encoder step counts 56, 57, 58 aloud long after the knob has stopped.
- **A SUCCESS IS SPOKEN ONLY WHILE ITS CONTROL STILL HAS THE FOCUS** (`frame.reply`; refusals are
  always spoken). A reply arriving after the user has moved on cuts off the screen reader reading
  the control they are on now.
- **THE WINDOW'S RECORDS HOLD THE DESK'S OWN 0..1 PARAMETERS** (`action_get_channels(percent=False)`;
  the controls scale with `field(..., scale=100)`). `_preset_now` compares within half a percent, so
  a record rounded to whole percents cannot be compared again after a pushed change.
- **EVERY SERVER CALL GOES THROUGH `native.calls`**, never straight from an event handler: an
  action holds the HID lock for seconds, and on the UI thread that freezes the window. A slider's
  writes go through `LatestWins`, which sends only the newest value, and at least every
  `Slider.MAX_WAIT_MS` while it keeps moving: a held arrow repeats faster than the delay, and
  without the maximum nothing is sent until the key is let go. A list must not get one.
- **A CONTROL'S NAME IS THE STATIC TEXT CREATED JUST BEFORE IT**, with the same parent (Win32's
  labelling, `pages.labelled`). Create the label second and the control reads unnamed.
- **F6 FINDS THE PAGE'S FIRST CONTROL BY WALKING ITS CHILDREN** (`_first_focusable`):
  `NavigateIn` does not reach controls inside a group box.
- **A PAGE HAS NO GROUP BOX OF ITS OWN** (`pages.page_body`). The tree has just named it, so a group
  named the same repeats the name on the way in. `MainFrame.go_to` says nothing, so the control
  that calls it names the page ("Go to Wireless connections").
- **DESCRIPTIONS AND A SLIDER'S VALUE IN WORDS COME FROM `native.a11y.describe`**, an MSAA
  override of those two answers only. Override anything else (name, role, state) and the control
  stops reading as the native control it is.
- **REPLIES ARE SPOKEN WITH `frame.say`**, a UIA notification the screen reader voices, not the
  app's own SAPI voice. `recent=True` for a slider's replies, so a newer one replaces an older one.
- **MIC TYPE, THE PROCESSING PRESET AND BOTH HALVES OF THE USB MODE ARE APPLIED BY A BUTTON, NEVER
  BY THE LIST.** Arrowing a closed native list fires a change per item; a mic type or a preset
  replaces the processing chain (a voicing moves the gain too), and a USB mode write tears the
  desk's USB audio down. Re-applying the same value is refused for the same reason.
- **A CONTROL READS AND RECORDS THROUGH `page.rec`** (`ChannelPage.field`), never a record captured
  when it was built: a re-read replaces the record, and a captured one goes stale silently.
- **A STAGE THE USER MUST HEAR IN FULL OPENS A DIALOG OF ITS OWN** (`native.dialogs`): a screen
  reader reads a dialog's text when it opens, never when it changes. While a job runs, its stages
  are spoken by `JobDialog.say`, a notification from the dialog's own notifier. No progress bar
  in a `JobDialog`: NVDA beeps its changes, and auto gain is measuring a microphone.
- **A JOB DIALOG TICKS, EXCEPT AUTO GAIN** (`JobDialog(ticks=False)` in `channel_jobs`): a soft
  clock (`speech.start_loop("working")`) plays from 1.5 s into a job until it ends, so a silent wait
  is never mistaken for a hang. A job that measures a microphone must pass `ticks=False`, or the
  measurement hears the clock. The loop has its own device handle: through the speech thread, each
  tick would cut off the phrase playing.
- **A LEVEL CHECK BUTTON'S LABEL CHANGES ONLY WHILE IT DOES NOT HAVE THE FOCUS**
  (`channel_jobs.LevelCheck`). A screen reader reads a focused button's new name aloud, over the
  levels the check is speaking. The server toggles, so a press is right whatever the label says.
- **A STEREO PAN'S THREE SLIDERS ARE BUILT ONCE AND THE LINK ONLY SHOWS OR HIDES THEM**
  (`channel_tabs.StereoPan`), so linking leaves the focus on the switch.
- **A PAN LINK'S "HAVE THE LEGS MOVED" TEST IS `server.linked_pan_offset` ALONE**, which the
  window asks (`channel_tabs.StereoPan`). It shows centre for legs moved since the link, and the
  server starts the link again from those legs on the next move; a second copy of the test would
  let the slider's position and the written legs disagree.
- **THE OUTPUT MIXES PAGE RE-READS AFTER A MODE WRITE** (`routing_mixes.OutputMixesPage`): leaving
  or entering Custom moves the output's cross-points on the desk, which it does not push. Its own
  cross-point writes need nothing: the desk pushes back neither `mixDisabled` nor `outputMixMinus`
  for them, but they reach the desk model as they are sent.
  `mixLink`, which is pushed, is not the inclusion: it stays True for a source on no fader.
- **ITS SOURCE SWITCHES ARE HIDDEN ON ANYTHING BUT A CUSTOM MIX.** Outside Custom the desk ignores
  the cross-points it has stored, so the switches would describe a mix the output does not carry.
- **A MONITOR MIX LEVEL WRITE MAKES ITS OUTPUT CUSTOM** (`server.action_set_monitor_level` writes
  the mode first), which the desk does not push back; the write reaches the desk model, which is
  enough. The per-output reset re-reads instead, since what its link requests do to the levels
  comes back from no write of ours.
- **A VIRTUAL FADER'S MUTE AND SOLO LIVE ON `levels["faders"]`, NOT ONLY ON A CHANNEL RECORD.** An
  empty fader or a stereo-linked pair has no channel record; `muted` None is an empty fader.
- **A CONTROL THAT CANNOT ACT NOW REFUSES THROUGH ITS GUARD, AND IS NEVER DISABLED**
  (`Switch(guard=)`, the Master Mix page's raw tracks switch while no separate tracks are made). A
  disabled Win32 control leaves the tab order, so it would vanish from under the user.
- **THE MASTER MIX PAGE POLLS RODE'S DRIVER LINE WHILE IT IS SHOWN**, as the CallMe pages poll
  their listener: neither reads the desk. `server.read_usb_driver` reads Windows and the service's status, never the
  desk. Its `switching` holds both USB Confirm buttons: a second switch made sooner lands in the
  middle of the first's bind wait.
- **A LIST OR NUMBER BOX WHOSE VALUE REPLACES NOTHING WRITES AS IT CHANGES, THROUGH `LatestWins`**
  (`controls.Choice`, `controls.Number`): a pad's colour, play mode or MIDI mapping. Arrowing sends
  only the newest value, `DELAY_MS` after the arrows stop. Anything that replaces something keeps
  its Confirm button (above).
- **THE PADS ARE NOT IN THE DESK STATE** (`pads_section.PadsSection`). They come from a model dump
  of their own, about 1.5 s, read after the desk state's first read, on F5, after a change made in
  the window, and when the desk pushes a pad change (below).
- **A PAGE THAT READS THE DESK FOR ITSELF FOLLOWS IT WITH `pages.FollowDesk`**: the pads, the
  backups, and Device's Hardware, Desk settings and Wireless connections. Each names the properties
  it shows (`soundpad.PAD_GRID_PROPS`, `server.SHOW_STATE_PROPS`, `HARDWARE_PROPS`,
  `UNIT_SETTING_PROPS`, `network.PROPS`), watched through the desk state's one follower
  (`DeskState.watch`), and re-reads once their pushes stop for `AFTER_MS`, never while a modal
  dialog is open. A RE-READ, NOT THE MODEL, because which of these the desk pushes for a
  touchscreen change is unmeasured. Keep out of the lists what changes while the desk is merely in
  use, or the page dumps the desk throughout: `padActive` and `padProgress` (a pad playing),
  `storageVolumeState` (a recording's free space; `recordState` covers a card going in or out).
  The wifi scan results are never pushed. A followed Hardware read skips `read_device_status`,
  which rescans the bus. A name the desk does not publish follows nothing, so
  `test_native_desk_state` checks every list against `PROPERTY_LIST_PRO2.md`.
- **A PAGE RE-READ WHILE THE USER IS ON IT SETS ONLY WHAT CHANGED** (`pages.set_text`, `set_items`,
  `select`, `controls.set_choices`): a screen reader reads a focused control again whenever its
  value or selection is set, even to the same thing.
- **A RESTORE OR A FACTORY RESET ENDS WITH `frame.desk_rewritten`, NOT `desk.refresh`**
  (`backups_section`). The pads and the backups are read by their sections, outside the desk state,
  so a desk state refresh alone leaves the Pads pages showing the grid from before.
- **THE WINDOW READS THE DEVICE STATUS AS THE SERVICE COMES UP** (`MainFrame.start`). Only
  `read_device_status` publishes the hardware id every per-desk setting is filed under: without it
  the pan links save against no desk and the as-found backup is never offered.
- **THE PADS ARE READ AGAIN `REREAD_AFTER_MS` AFTER A SETTINGS DIALOG THAT WROTE, LONGER THAN
  `_Field.DELAY_MS`.** A write still waiting in `LatestWins` when the dialog closes is still sent,
  but its reply has no dialog to land on; reading sooner would read the pad before it.
- **NO DIALOG FOCUSES ANYTHING FROM ITS CONSTRUCTOR; THE CONTROL TO LAND ON IS CREATED FIRST.** A
  dialog activating focuses its first focusable control, and that focus event is what is read; a
  `SetFocus` as well is a second event, and the control is read twice. `TextDialog` moves its
  default button first in the tab order. `test_native_dialogs` walks every native dialog for it.
- **EVERY DIALOG HAS A CONTROL TO LAND ON**, even a job that cannot be closed: its button is Please
  wait (`JobDialog(closable=False)`). With none, the screen reader focuses something of its own.
- **A DIALOG'S NOTIFICATION WINDOW HAS NO NAME** (`dialogs.SpeakingDialog`). A screen reader reads
  it as part of the dialog's text, and in a dialog with nothing else to focus, as the focus: named
  with the title, the title is heard three times.
- **A DIALOG OPENED STRAIGHT AFTER ANOTHER CLOSES IS OPENED WITH `wx.CallAfter`**
  (`PadsPage._changed`). Opened at once, it comes up while the focus is still returning from the
  first, and its first control is never read.
- **THE RECORDER IS NOT IN THE DESK STATE, BUT ITS PUSHES COME THROUGH IT**
  (`recording_section.RecordingSection`). `DeskState` passes each `recordState` push on as a
  `recorder` event, and the section reads the recorder (one dump) only for a state it is not
  showing. A push caused by the window's own press arrives before the press's reply, so it is held
  and read after the reply only if the two disagree; reading at once queues a dump behind the
  command. The transport buttons follow that held push at once (the button label is the
  confirmation), so the pressed label is taken at the press, not at the reply.
- **A TRANSPORT PRESS WAITS FOR THE DESK'S `recordState` PUSH, NOT A FIXED TIME**
  (`recording._await_record`, with the HID lock released while it waits). A start answers in about
  40 ms, a stop in 0.5 to 1.1 s; `_RECORD_ANSWER_S` (3 s) is only waited out by a press the unit
  ignores. Cut it near 1 s and a slow stop is reported as a press that did not take. A stop pushes
  no `lastRecordingID` under this listener, and the new take is in the list by the time the state
  arrives.
- **THE CALLME PAGES ASK THE SERVER'S LISTENER EVERY SECOND WHILE ONE IS SHOWN**
  (`callme_section.CallMeSection.poll`, `server.read_callme_live`), which reads no desk; a full
  read is a dump. A call's state lives in that listener, so the desk state's pushes do not carry
  it. Each redraw puts text on the same controls and only when `version` moves: during a call it
  moves once a second, with the call quality.
- **THE CALLME END BUTTON IS "DECLINE OR HANG UP", AND THE SERVER CHOOSES AT THE PRESS**
  (`action_callme_end`): a call that began ringing a moment ago is declined, not the guest already
  on the line.
- **THE WINDOW'S TRANSPORT IS TWO BUTTONS WHOSE LABELS FOLLOW THE STATE** (Start recording / Stop
  recording, Pause / Resume), from this window's presses and the unit's REC button alike. A focused
  button that renames itself is read out, so THE NEW LABEL IS THE CONFIRMATION:
  `RecordingSection._commanded` speaks no reply when the pressed button's label changed and puts the
  reply on the Now line instead; a refusal or a failure, which renames nothing, is spoken. Neither
  button is ever disabled. The transport buttons come first on the page and Now sits below the
  buttons, one Tab after a press.
- **A MODULE'S STATE IS `modules[m["prop"]]`, NOT `m["key"] + "On"`.** Pan's state is
  `channelPanOn`, so the guessed name reads Pan as off whatever the desk holds.
- **THE WINDOW'S DESK SUMMARY IS ITS STATUS BAR, NEVER SPOKEN ON ITS OWN**
  (`device_section.StatePage.shown` -> `frame.set_status`). A native status bar is read on demand
  (NVDA+End, JAWS Insert+Page Down) and not announced as it changes, so it cannot talk over the
  focus. It is read again `SUMMARY_AFTER_MS` after the last desk event in `SUMMARY_EVENTS`; add an
  event there if the summary gains a fact it carries.
- **THE CLIP WARNING WATCHES A RECORDING ENDPOINT AND SPEAKS ONLY THROUGH `speech.say`**
  (`server._main_mix_stream`, `_clip_start`). The desk's playback endpoint reads this computer's
  own audio, so a watch on it hears the screen reader and its own words. `speech.say` follows the
  announcer, so the warning is silent while a recording suspends it; `meters.clip_alert` plays on
  the default output, usually the desk, and would land in the take, so `clip_sound` stays False.
- **THE WINDOW FITS ITS DISPLAY, AND ITS STATUS BAR IS NOT ELLIPSIZED** (`MainFrame._fit_to_screen`,
  the `CreateStatusBar` style). NVDA+End finds a status bar at the window's bottom-left corner;
  with that corner off the screen it reads its own copy of the bottom line instead, which keeps the
  tail of longer text drawn there before. wx's default ellipsizing puts a shortened copy in the
  native control, and that copy is what a screen reader reads.
- **THE HELP IS `help/help.txt`, AND TWO READERS READ IT**: the window (`help_section.topics`) and
  the website's guide importer (`SlightPerception\Website\Tools\importAppGuide.py`, its `text`
  shape). A change to the format changes both in the same session. A `#` heading is a topic in the
  tree, and so is each `##` part of Help; the importer lists every `#` heading and refuses a run
  that finds one it does not know, so a new topic needs an entry there too.
- **THE WIFI PASSWORD BOX IS GONE BEFORE THE JOIN STARTS**, whatever the outcome: it lives in
  `wifi_dialogs.JoinDialog`, destroyed as `NetworkPage._ask_join` leaves its `with`. A password left
  in a field is one review command from being read out in a room with people in it. The server
  never returns one, so it is never prefilled; a refused password reopens the dialog empty.
- **A READING'S TEXT BOX IS ALWAYS MULTI-LINE, EVEN FOR ONE LINE** (`pages.read_only`): wx makes
  a text box a tab stop only while it is editable or multi-line, so a single-line read-only box is
  never reached by Tab. `test_native_device.py` checks the WiFi status box is.
- **THE WIFI DIALOGS HAVE NO DEFAULT BUTTON**: each opens straight after another closes, so the
  Enter that closed the last could land on it. Enter acts through each list's and box's own key
  handling instead, and a stray one is harmless: a listed network that needs a password refuses
  an empty one.
- **AN UPDATE'S DIALOG IS HELD BEFORE `app_updates.proceed`** (`update_dialogs._Follower`): the
  installer closes this app, so once the download is verified nothing may stop it, and the closing
  sentence gets its reading time first. Proceed sooner and the sentence is cut off mid-word.

## Screen reader, wording and speech

- **DEVICE AND BACKUPS ARE THE UNIT'S; APP SETTINGS (Alt+8) IS ACCESSCASTER'S.** A new preference of
  the app goes on App settings, never on a unit page, and none of its pages reads the desk.
- **NO QUOTE MARKS AROUND A NAME IN ANY MESSAGE.** A quote straight after a digit is read as a
  unit: "Saved as 'Tester1'." is heard as "Tester 1 feet", and a double quote as inches. Write
  `Saved Mic 2's current settings as {name}.`, never `'{name}'` or `"{name}"`.

- **`speech.say` IS WHERE THE ANNOUNCER SWITCH IS ENFORCED, because it is the only place every phrase
  passes through.** `speech.enabled` gates the read loop, and roughly half this app's speech comes
  from elsewhere: the virtual-devices follow-up after a USB mode change, the recorder watcher. Gate
  it anywhere else and "announcements off" leaves all of that talking. The phrases announcing speech
  is BACK are safe because each sets the flag before it speaks, so no force path is needed. A
  suppressed phrase is still logged, marked: "it said nothing" and "it was never asked to" are
  different faults. Hotkey replies and the clip warning are handed `speech.say` itself; a second
  "speak if announcing" wrapper would only skip that log line.
- **A JOB THAT REWRITES THE DESK HOLDS THE ANNOUNCER'S DESK-CHANGE SPEECH** (`BackgroundJob`'s
  `rewrites_desk`: the profile restore, the show commands, the pad restore). The announcer drops
  every desk change until 1.5 s after the last job ends, and says nothing of its own about the
  job: the window that started it speaks its progress through the screen reader, and a sentence in
  the announcer's voice as well talks over it. The state cache keeps updating. Desk-change speech
  must go through `announcer._say` / `_tone`, or it talks through a restore. The job's own steps
  and its end tone are not held.

- **THE ANNOUNCER STARTS ON UNLESS ITS OFF IS REMEMBERED, and only a person's toggle is saved.**
  "Start with announcements the way I left them" (`announcer_remember`) is unticked by default,
  because a start-up in silence sounds exactly like the app or the desk being broken. Ticked, the
  window's switch, the tray's item and the shortcut all save `announcer` through
  `announcer.action_set_announce(chosen=True)`; start-up and the switch-back after a take pass
  `chosen=False` and never save, or a take would overwrite the person's choice. With no saved
  `announcer_remember`, a saved `announcer` of False counts as ticked.

- **THE SECTION IS CALLED "Backups" AND ITS ROUTE KEY IS `profiles`, DELIBERATELY.** So `profiles`
  in code is right and "Profiles page" in USER-FACING COPY is wrong: it names a page nobody can
  find. The same trap applies to "Shows": the feature is `shows` in code, and the user sees backups
  in one list.
- **`FaderSpec.count` IS 9 ON BOTH DESKS; ONLY `physical` DIFFERS (Pro II 6, Duo 4).** So the Pro II
  has three virtual faders and the **Duo has five**, and copy that says three for the Duo is wrong.
  The Duo's fourth assignable source is Headset, which sits on a virtual fader and is the one
  usually left out of the list. Its ninth fader ships EMPTY (`default_vector` index 8 is -1), and
  the tenth VECTOR SLOT is not a fader at all, which is a separate fact: PHYSICALINTERFACE carries
  four POT nodes then exactly NINE FADER nodes, so one of the ten slots has none.
  Labels and the encoder ring do not shift.

- **THE HELP DRIFTS, AND IT DRIFTS SILENTLY.** `help/help.txt` describes every page and control,
  and nothing fails when a control changes underneath it. Five false claims were found in one spot
  check on 20th September 2026. When you change a control, a default, a page or where focus lands,
  grep `help.txt` for it in the SAME edit. To settle a claim about what is on screen, read the
  labels the window's own code gives its controls, not a description of them.

- **DO NOT MARK CAPABILITY-GATED SHORTCUTS AS UNAVAILABLE IN THE CATALOGUE** (Shaun's ruling, 2nd
  September 2026, rejecting exactly that proposal). The shortcut catalogue IS filtered by what a desk
  HAS - Channels drops from 32 entries on a Pro II to 27 on a Duo - but NOT by what it can DO, so a
  desk lacking a capability still lists its shortcuts. Pressing one explains itself clearly, so it
  is not silent, only late. The reasoning: a capability gate is TEMPORARY, and building UI around a
  gap with a known expiry date is work that has to be rolled back later. Do not re-propose it.
- **USER-FACING TEXT SAYS "RODECaster Pro 2", NEVER "Pro II"** (Shaun, 30th August 2026). A screen
  reader speaks the roman numeral as "Pro aye aye". RODE's own branding is "Pro II" and the instinct
  to "correct" it back is strong - it was done once already, in this very rule's session - so:
  **anything spoken or displayed uses the numeral 2.** That includes `devices.PRO2.name`, which
  feeds the status line, the window title and every message that interpolates the device name.
  **THREE PLACES KEEP "Pro II" DELIBERATELY:** Windows' own audio ENDPOINT names, which we do not
  own and must match exactly (`meters.stream_label` strips the model prefix, so none of it
  is ever spoken); code comments and docstrings, which describe RODE's product; and
  `RODECASTER_PROTOCOL.txt`. Nothing keys off the display name - per-device preferences use the
  hardware id - so changing it broke no stored data, and a profile saved earlier just carries the
  old stamp.
- **A BACKUP IS ABOUT A SECOND A PAD; A RESTORE IS ABOUT FIFTEEN. Never quote one figure for the
  other.** `PAD_BACKUP_SECONDS = 1` (a backup COPIES files off the mounted pad volume with
  `shutil`), `PAD_RESTORE_SECONDS = 15` (a restore pushes each sound back over transfer mode). Both
  are in server.py and both are published, so copy must read the right one. Quoting the restore
  figure on the backup path calls a full grid "a job of many minutes", wrong by an order of
  magnitude, and makes people skip the pads.
  **THERE IS NO SETTINGS-BACKUP FIGURE AT ALL, and `SETTINGS_RESTORE_SECONDS = 25` is not it.** The
  backup reply publishes `seconds_per_pad` and nothing else. The help once quoted the 25 on the
  backup path, so check BOTH the window's dialog copy AND `help.txt` whenever either number moves.
  **ALSO UNMEASURED: how long the unit's OWN backup takes.** `action_backup_now` is proven to work
  but has never been timed, so do not put a number on it. The only measured neighbour is the unit's
  own RESTORE at a 6 second median.

- **ONE HINT DESCRIBES AT MOST ONE CONTROL.** A paragraph attached to the network name, the password
  and Join was read out three times while moving through three controls (Shaun, 30th August 2026:
  "the repeated hints in wifi name and password fields is too much"). The LABELS say what each box
  is, which is what a label is for. Attach a description (`a11y.describe`) to at most one control,
  and only where it says something the label cannot.
- **A HINT IS FOR SOMETHING NON-OBVIOUS, OR FOR A CONTROL THAT AFFECTS OTHER THINGS** (Shaun, 30th
  August 2026: "the elements themselves say all you need to know"). A labelled list whose items name
  themselves gets no description; a description is re-read on every focus, so length costs more
  there than anywhere else. It says only what the label does not, in a sentence or two: not "you
  are asked to confirm" (the dialog says it), not "takes effect straight away" (the default), never
  the label again. The long account belongs in the help.
- **THE SHORTCUTS LIST IS ONE LIST, NOT A BUTTON PER SHORTCUT.** A button per shortcut is a tab stop
  per shortcut. The list is one tab stop, and every action is in its menu (the Applications key,
  Shift+F10 or a right-click) or on Enter and Delete.
- **A RESULT SPOKEN AS A DIALOG CLOSES WAITS FOR THE FOCUS TO LAND BACK.** Said before, the focus
  arriving on the page cuts it off (`shortcuts_page.ShortcutsPage._applied`,
  `pages.AFTER_FOCUS_MS`). A successful delete says nothing at all.
- **THE AFTER-UPDATE REPORT IS TAKEN ONLY ONCE THE WINDOW IS ACTIVE, AND SAID `AFTER_FOCUS_MS`
  LATER** (`UpdatesPage.take_report`). Said while the window is still coming to the front, the
  screen reader drops it; and taking it cancels the 30 s notification that says it otherwise, so
  it must not be taken before it can be heard.
- **ONE CHORD FOR EVERY SHORTCUT** (`HotkeyManager._mods`, saved as `hotkeys.mods`). A shortcut has
  no modifiers of its own choosing: `set_mods` moves them all, and a later duplicate is switched off.
- **A DIALOG OPENS ONTO ITS FIRST CONTROL, not its heading, and carries no intro paragraph.** The
  Add-a-shortcut dialog read its name, then an intro, then both labels, then the heading, before
  reaching anything you could use. `test_native_dialogs.py` checks every native dialog.
- **NVDA NEVER READS CONTENT REVEALED INSIDE AN ALREADY-OPEN DIALOG.** Open a FRESH dialog per stage
  (auto gain has three: confirm, progress, result).
- **NOTHING MAY REPLACE A CONTROL A SCREEN READER MAY BE SITTING ON**, whatever triggered it: a
  read, a poll or a write's confirmation. `pages.set_text` and `set_items` replace only what
  differs, and a section rebuilds a page only when its STRUCTURE changes, never its values.
- **A FAILED READ MUST SAY SO, not leave old numbers looking live.**

- **AUTOSTART IS ONE REGISTRY VALUE WRITTEN BY TWO THINGS THAT CANNOT SHARE CODE**, and nothing at
  runtime would notice them drifting: `ProjectBuilds/installer.iss` and `Project/autostart.py` must
  agree on the key, the value NAME and the command. A different name breaks the installer's
  `uninsdeletevalue`, so an uninstall leaves a Run entry pointing at a deleted exe - a startup error
  every login with nothing left to turn it off. `DevTools/test_autostart.py` reads the `.iss` and
  asserts it. **HKCU, never HKLM**: only one copy runs per PC (`instance.py`), so a machine-wide
  entry starts the app for every account and all but the first are refused (seen with one user at
  the console and another over Remote Desktop).
  **RENAMING THE APP MEANS MIGRATING WHAT LIVES ON THE USER'S MACHINE UNDER THE OLD NAME**: the Run
  value (two entries naming one exe start the app twice at sign-in), the Start Menu group and the
  desktop shortcut (Inno writes the new ones beside the old), each removed by `installer.iss` on
  every install, NOT gated on the tasks the user ticks, and by `autostart.py` for the Run value. The
  "Access Caster" to "AccessCaster" migration is retired, both users being past it (5th November
  2026). **`UsePreviousGroup=no` stays**: Inno otherwise reuses the group name the machine
  remembers, and a renamed build writes its shortcuts into a folder still called by the old name.
  **The `--startup` flag is the whole difference between a login launch and a manual one** - at login
  `tray.main` opens no window and comes up in the tray with the announcer and shortcuts live.
  **A source run REFUSES to write it** rather than naming python.exe plus a script path, which would
  break silently at a login.

- **OPENING A FOLDER MUST ALSO PUT THE USER IN IT.** `os.startfile` opens the window BEHIND the
  app's: measured, the foreground two seconds after the call was still AccessCaster. A window you
  were not moved to is indistinguishable from nothing having happened, and that is exactly how it
  was reported. Windows will not let a background process take the foreground unaided, so
  `server._open_folder_focused` borrows the input state of the window that has it
  (`AttachThreadInput`). Match the window on a title PREFIX: Explorer sets the folder name first and
  appends " - File Explorer" a moment later, so an equality test passes or fails on how fast the
  poll got there. Failing to focus is NOT failing to open, so everything after `startfile` swallows
  its own errors.

- **A CUE IS A WAV PLAYED BY THE SPEECH THREAD, NEVER A BEEP.** `winsound.Beep` and
  `[Console]::Beep` go to the SYSTEM default output, which on these machines is the desk, so a cue would go to the callers
  instead of the operator. `tones.py` generates the WAVs and `speech._Engine` plays them on the same
  player as the speech, so they inherit the announcer's chosen device and volume. A cue must be
  22.05 kHz mono 16-bit, the player's format; any other file is spoken as its name instead.
- **SAPI RENDERS INTO MEMORY; IT NEVER SPEAKS TO THE DEVICE.** Spoken to the device, SAPI reopens it
  for every phrase and pads each with silence (80 to 105 ms before the word on Hazel, about 480 ms
  after), so a moving fader's numbers are cut off before they are heard. `speech._Engine._render`
  trims both ends and `wave_player` plays the rest on a device held open between phrases. The
  chosen output is a SAPI token turned into a waveOut id (`SpMMAudioOut.DeviceId`) at every open,
  because that id is a position in Windows' list and moves when a device is plugged in or removed.
- **THE SPEECH THREAD KEEPS ONLY THE NEWEST ITEM, SO TWO CUES QUEUED TOGETHER BECOME ONE.** Right
  for speech (the newest state is the only one worth hearing), wrong for a pair. Anything that must play two sounds in order has to space
  them itself. A real mute press only ever queues one, so this is a confirmation problem, not a
  feature problem.
- **A TONE FILE'S SHAPE IS PART OF ITS NAME (`muted-v2.wav`), and `TONE_VERSION` is bumped when the
  shape changes.** The files live in the user data dir and are not regenerated because the code
  changed its mind, so without this everyone who had heard an older cue would keep hearing it.
- **`SpeakStream` REPORTS SUCCESS WHETHER OR NOT A SOUND COMES OUT.** Every test of a cue is by ear;
  there is no API to ask. Two listening tests on 16th September 2026 were misread in opposite
  directions before this was understood, one of which produced a confident and wrong note about
  sample rates.
- **THE ANNOUNCER IS AT THE MERCY OF THE DESK WHENEVER IT IS POINTED AT THE DESK, and monitor
  auto-mute is the part nobody expects.** Announcements arrive on USB 1, so they already vanish on a
  solo or a USB 1 mute. `monitorAutoMute` adds a subtler one: the unit silences the monitor speakers
  whenever a mic is live, so the same announcement reaches the headphones and not the room, or the
  reverse, according to what was just pressed. Shaun met it through the mute CUES on 16th September
  2026 - falling cue in the headphones, rising cue out of the speakers - and it looked like the app
  routing two identical files to two different places. It is not new and it is not ours: speech has
  always done it, and a 165 ms cue merely makes it obvious where a one-second sentence straddles the
  change. **Do not debug this as an audio bug.** The fix is the announcer's OUTPUT setting: point it
  away from the desk and the whole class disappears, which is what that setting is for.

## Metering and Windows audio

- **A SPOKEN LEVEL IS THE LOUDEST SINCE THE LAST ONE SPOKEN, NEVER `peak_hold`** (`meters.Watcher`).
  The hold falls under 1 dB a second, so it went on reading a voice for half a minute after it
  stopped. Below `meters.QUIET` (-50 dBFS) a check says "quiet" once, then nothing.
- **`_ChatIsolation.__enter__` PUTS BACK WHAT IT CHANGED WHEN IT FAILS PART WAY**, because a
  `with` block does not call `__exit__` when `__enter__` raises. A restore that fails is reported
  with `CHAT_NOT_RESTORED`, by auto gain and the level check alike. `changed` is set BEFORE each
  write, not after: a mix that already carried the one source is neither settled nor restored, and a
  write that fails part way must still count as a change.
- **Level meters read Windows audio endpoints, so metering cannot work or be tested over Remote
  Desktop.** Run `query session` first; that is the whole precondition.
- **Default to a CAPTURE endpoint.** With no stream chosen the meter defaulted to the PLAYBACK
  endpoint, so it measured this PC's own screen reader instead of the mic. "Microphone (...
  Main Stereo)" is the desk's OUTPUT mix; label devices by direction.
- **A capture endpoint reads 0.0 until something opens it.** `meters._EngineWaker` is that something,
  started by every `meters.Meter` on a capture endpoint; holding the stream open with `ffmpeg -f
  dshow` reads 42 percent where a bare meter reads 0.0. Over Remote Desktop no desk endpoint exists,
  so a waker that never starts is invisible there.
- **The clip threshold must be scaled by an INPUT's LIVE volume ceiling, and never an output's.**
  Windows' capture level scales an input's meter, so at -1.08 dB true full scale can never pass
  0.99. An output's meter reads BEFORE its volume (a -6 dBFS tone read 0.500 on the desk's Main
  Stereo output at -6.3 and -18.3 dB, 1st November 2026), so scaling it calls ordinary speech
  clipping; `meters.reading_scale` holds both rules. `trusted_ceiling()`
  refuses an untrustworthy ceiling and falls back to 1.0, because a failed volume read would
  otherwise announce a whisper as clipping. The healthy/strong/hot boundaries are Shaun's judgements;
  do not rescale those.
- **The Windows recording slider is BOOST above about 55 percent** on this endpoint: 50% is
  -1.08 dB, ~55% is unity, 75% is +7.67 dB, 100% is +30 dB. Best practice, and it is SET on this
  machine: Windows at unity, gain on the desk, peaks of -12 to -6 dBFS.
- **The `... Chat` input REFUSES to move from -1.08 dB**, by dB and by scalar, silently, though it
  reports a range of -96 to +30 dB like the other capture endpoints; it looks driver-enforced.
  Immaterial: Chat carries the chat return.
- **Chat cannot be level-checked.** The check listens on the Chat OUTPUT, which is mix-minus, so the
  caller is deliberately not in it. No button is built for it.
- **A channel with its fader down reaches the isolation mix at 0.** That refusal is the common path,
  so it says to bring the fader up rather than describing the mixer.
- **`meters.Watcher` must record a mid-loop exception.** It once died silently, so a broken run was
  summarised as a QUIET CHANNEL. A run is in progress if EITHER the watcher or the isolation exists,
  and each run's end-timer carries its run number.
- **`meters.Meter.peak` ASKS ITS ENDPOINT'S STATE EVERY `PRESENT_CHECK_S` AND RAISES ONCE IT IS
  GONE.** The endpoint meter does not raise when Windows removes the endpoint (a USB mode change
  re-enumerates the desk): it reads 0 for as long as it is gone. Without the check a `Watcher` sits
  on the dead endpoint, and the clip warning's supervisor, which reopens only a watcher that has
  ended, never reopens it (measured 5th November 2026).

## Shows, and what the touchscreen alone can do

- **RECORDING TO A PAD FROM THE TOUCHSCREEN REBOOTS THE DESK.** Three of three, with and without us
  on the USB. It recovers clean. Never build it, never probe `PADRECORDER` again, and if a session
  sees an unexplained reboot, ask what the desk's own screen was doing.
- **SHOWS DO NOT CAUSE THE USB DROPOUT.** It was the whole case against the feature and it was wrong.
  See "USB dropout" below.
- **THERE IS NO SET-TIME ANYWHERE IN THE PROTOCOL, AND THE HUNT IS CLOSED.** Settled three ways
  (30th August 2026): by NAME (the only time-shaped properties are the four FORMAT ones), by
  BEHAVIOUR (across dumps 30 minutes and 7 hours apart, not one value advances with wall-clock
  time), and by the RODE APP'S OWN NAME TABLE, which holds 666 names against the desk's 472 and no
  set-time, read-time, epoch or RTC command among them. The firmware says why: the ENGINE runs
  `ntpdate pool.ntp.org` then `hwclock -w`, and the TOUCHSCREEN sets the clock with `settimeofday`
  locally, so setting it never has to cross the wire. **The timezone is the same shape**: applying
  one is `ln -sf <zoneinfo> /etc/localtime`, written by the touchscreen app, which is why
  `systemDateTimezone` stores our writes and does nothing. Do not re-hunt for a property.
  **`systemDateTimeDaylightSavings` is the same shape** (3rd October 2026, watched on a webcam): a
  write reads back and the clock on the screen stays an hour out. Both are profile-only, off the
  Device page. Seen working on the same camera: `systemDateTime24h`, `systemDateTimeOnHome`,
  `screenBrightness`, `autoBrightness` and `activeButtonsBrightness`.
  **SETTING THE CLOCK IS UNSOLVED AND PARKED, and the NTP idea is THEORY ONLY** (Shaun's ruling,
  30th August 2026). The only route in is DNS - the desk believes whatever answers as
  `pool.ntp.org` - and every way of controlling that answer means the USER reconfiguring their
  router, or running a permanent fake NTP service on a hotspot the desk is joined to. That is not
  something a blind user can be asked to do, and it could never ship as a feature, so it is NOT
  owed work. **Do not propose it again.** The desk's clock is correct in practice anyway; this only
  ever mattered for a desk with no internet.
- **A STATIC IP CANNOT BE SET, AND IT IS NOT A GAP IN OUR WORK.** Spiked on hardware 30th August
  2026 with the ethernet plugged in. `staticIpSet` accepts True and reads back True, but
  **`ipAddress` will not take a value at all**: 192.168.7.231 was written and the property still read
  the DHCP address 192.168.7.227 afterwards. Ping agreed, which is the proof that matters - the new
  address never answered and the old one never stopped. So `ipAddress`, `subnetMask` and `gateway`
  are READ-ONLY REPORTS of what the interface currently has (they are empty on wifi and fill the
  moment a cable goes in), and `staticIpSet` is a flag with no operands. It fits the firmware:
  `staticIpSet` is in the engine's name table only, absent from the touchscreen binary, and neither
  binary carries `ifconfig`, `dhcpcd`, `udhcpc` or `wpa_supplicant`. **No stray was appended** (all
  four still one occurrence) and the desk restored to DHCP clean. Do not build the control; do not
  re-spike it.
- **ALL FOUR SHOW COMMANDS ARE PROVEN FROM OUR OWN WRITES** - export, delete, reset, import - on a
  Pro II with the recordings drive in, 2nd September 2026, over two full reset-and-restore cycles
  with nobody at the touchscreen. **The second cycle came back with ZERO real differences** against a
  profile snapshot taken moments before the reset: every diff was the new show node's own metadata or
  the drive's free space, and all 16 SMART pads matched to the leaf value including FX stacks,
  envelopes and audio files. So the round trip is EXACT, and a show is the most complete undo this
  app has. The earlier doubt ("only the reset ever did anything") was aimed at the wrong thing: the
  29th August sitting exported AT THE TOUCHSCREEN, so it proved the traps and nothing about our
  writes. Do not re-open this.
- **A SHOW NEEDS REMOVABLE STORAGE: it goes to the card or the USB drive, NEVER to the desk's own
  internal volume.** Measured by arithmetic 2nd September 2026: each export dropped volume 1 (the USB
  drive) by exactly 23,986,176 bytes and left volume 2 untouched, and deleting the spares put the
  free count back to the same byte it started on. Volume 2 is 5 GB of internal storage, labelled
  "Internal storage (SMART pads)" and reporting `can_record` false, so it is not a candidate. That is
  why the Duo cannot do this: nothing is inserted in it. UNTESTED: whether the memory card slot
  serves as well as the USB drive (both report `can_record`), and what an export does with no
  removable volume at all - the code times out and says to check there is a drive in, which nobody
  has watched happen.
- **A SHOW IMPORT LEAVES THE MIC PREAMP UNPROGRAMMED, AND THE CHANNEL IS ~55 dB DOWN. THIS IS THE
  "RANDOM DEAD MIC", AND IT WAS OURS.** Reproduced deliberately 3rd September 2026, twice out of
  twice: software factory reset, then `showControlImport`, and Mic 1 goes inaudible. **A reset ALONE
  does not do it** (checked in the same sitting), so it is the import.
  **NOTHING CAN SEE IT IN THE MODEL.** 472 properties compared between working and dead: 8 differed
  and all 8 were the new backup's own list entry and the drive's free space. Level `1|1`, chain
  matching the working reference exactly, unmuted, gain 55, phantom right. So no read, no display
  and no read-back test can ever detect this - do not build one.
  **THE MECHANISM IS THAT MIC PRE-GAIN IS ANALOG AND SET OUT OF BAND.** The firmware names it:
  `RodecasterDBusAudioActions::setMicPreGain()` writing `/sys/.../rodecaster-aud-gpio/gain_ch`. The
  model is the control layer only; an import restores the model and never re-programs the preamp.
  Measured on the meter: dead reads -56 to -60 dB, and a nudge takes it to -4 dB. That jump is ~55
  dB, which IS the channel's configured pre-gain. It also explains the "loud hiss with the voice
  faint underneath" a caller once reported.
  **ONLY A REAL VALUE CHANGE RE-PROGRAMS IT.** Writing the gain back at its CURRENT value did
  nothing at all, which is why the desk's own encoder was the only recovery anyone ever found.
  `server.wake_channels` nudges each mic gain by one and straight back, and runs after every reset
  and import. Verified three times out of three (-5, -1, -3 dB, live) against two out of two dead
  without it. **The desk publishes no re-apply, refresh or resync property** - the whole property
  list was searched - so a real change is not a workaround, it is the only lever there is.
  Writing the mic TYPE also forces a re-apply but destroys the channel's chain, so it is not usable.
  **A POWER CYCLE IS CLEAN** (measured 3rd September 2026: Mic 1 live and -3 dB straight after a
  boot, untouched). Boot initialises the hardware; only the import path skips it. So the wake does
  NOT need to run when the app connects to a desk, and a dead mic that appears with no restore
  involved did not come from switching the desk on.
  **THE TOUCHSCREEN ALMOST CERTAINLY BREAKS IT TOO, so this is RODE's bug and not ours.**
  `setMicPreGain` and `gain_ch` appear ONLY in `rc_audio_mixer`; the touchscreen binary has neither
  and drives the same ten `showControl` properties we do, so it takes the identical path. Not
  proven: the GUI could rewrite gains afterwards for its own reasons, which a string search cannot
  see. Proving it needs a restore performed AT the touchscreen, which needs sighted help.
  **UNKNOWN: whether non-mic channels suffer it too.** Nothing was plugged into the others.
- **`showControlImport` RESTORES THE WHOLE DESK, and that includes LEVELS.** `outputMonLevel` jumped
  0.25 -> 0.95 on a restore. Any UI that offers import or a profile restore must WARN FIRST.
  NOT an ear-proof when first written (29th August 2026): Shaun heard the ANNOUNCER say the new
  value, not the level itself, because nothing was plugged into the rear line outs. **That gap is
  closed: speakers went onto the monitor output later the same day and `outputMonLevel` and
  `outputMonFixed` are now both ear-proven.** The warning stands on its own merits.
- **`showControlImport` does NOT refuse a show whose UUID matches the loaded one. The opposite was
  recorded here for days and it made the whole feature look useless.** A uuid changes only on a
  factory reset, so EVERY backup carries the loaded show's uuid; believing the refusal meant a show
  could only ever restore straight after a reset, and the app pre-refused the ordinary case. Proven
  twice on hardware 2nd September 2026: with the desk drifted on purpose, importing a backup sharing
  the loaded uuid put Mic 4's gain back from 30 to 50, and again from 42 to 50, inside four seconds,
  `showControlLastError` staying 0. **`showControlLastError` = 1 is real (seen twice, 29th August
  2026) but its cause is UNKNOWN.** Send the write and report the unit's refusal; never predict it.
- **A REFUSED IMPORT STICKS UNTIL A POWER CYCLE, AND ITS CODE OUTLIVES THAT** (Pro II, fw 1.7.6,
  1st November 2026). The first refusal came importing a backup of the pre-reset show straight
  after a clean restore. After it, five imports across four backups were refused, a backup exported
  minutes earlier of the loaded state included (Mic 4 drifted 50 to 40 stayed at 40), and importing
  a different backup did not clear it. After a power cycle the same drift test restored inside 3 s.
  `showControlLastError` read 1 through the power cycle and went to 0 on that success. So
  `action_show_import` counts only a code that CHANGED as the import's answer, and reads one still
  standing at the timeout as a refusal; its wording says to restart the desk.
- **THE SUCCESS TEST FOR AN IMPORT MUST WATCH THE DESK STATE, not the uuid and not the command
  field.** Both fail, in opposite directions: the uuid already equals the target in the ordinary
  case, so a uuid test reports success before anything happens (it shipped that way); the command
  field self-clears to the same -1 it held BEFORE the write, so a poll that misses the four-second
  window cannot tell consumed from never-latched. `shows.restore_fingerprint` watches a handful of
  restorable properties instead. **"Nothing moved" is only benign for a backup of the SAME show**,
  where it honestly means the desk already matched; for a different show it is a real failure,
  because a successful import would have changed the loaded uuid.
- **`showUID` IS POSITIONAL and is reassigned on every export.** Newest is always 0. Resolve the uid
  from a fresh read immediately before importing or deleting.
- **`showUUID` IS NOT A FILE HANDLE: it names the SHOW, and several backups share one.** Measured
  29th August 2026: three exports of one desk state gave three SHOW nodes with identical uuid AND
  identical name. Anything keying a backup by uuid picks the wrong file or fails to see a new one
  (it shipped that way for an hour and reported every repeat export as a failure).
  **`showSnapshotIndex` is the stable per-file handle** - a monotonic export counter that survives
  both an export and a delete while uids renumber around it. Key on that, resolve to a uid at the
  moment of the write. Corollary for the UI: backups of one state differ ONLY by that index, so an
  option list showing names alone reads as several identical lines.
- **SHOWS come back NEWEST FIRST, unlike the recordings list.** `recorder.read_recordings` reverses
  the stream because RECORDINGS are oldest first; copying that into `shows.read_shows` put the
  oldest at the top labelled "the newest". Sort on the snapshot index, never on stream order.
- **THE ADDRESS TABLE IS THE SINGLE SOURCE OF WHICH UNIT SETTINGS EXIST.** `rcp_transfer.
  UNIT_SETTING_SEL` says what can be written; `desk_labels.UNIT_SETTINGS` says how one is presented. That
  split is deliberate (the address table is shared with the Duo and must not carry UI copy), but
  MEMBERSHIP is not: every entry in the address table either appears on the Device page or is named
  in `server.UNIT_SETTINGS_NOT_ON_PAGE` with a reason. `server.unit_settings_off_page()` must return
  empty, and `DevTools/test_specs.py` asserts it. Four settings drifted for a day without this
  (2026-08-28) and nothing failed except a test that looked broken.
- **`outputMonAutoMute` WORKS, and ships OFF; the app used to be able to show it ON regardless.**
  Ear-proven 29th August 2026: written True, the monitor speakers duck on a mic fader move. The
  fault was `action_set_auto_mute` reporting success from the write returning, with no read-back, so
  a switch could sit there claiming a setting the desk did not have while Shaun's speakers howled.
  **It reads back BY BLOCK now and returns the state, and the window's switch follows the DESK, not
  the click.** It occurs once, in OUTPUT: this was never the `btVisible` duplicate trap, and the
  monitor and Bluetooth toggles are not crossed.
- **THERE IS ONE READER: `model_tree.parse`, indexed once per dump by `rcp_read._index`, AND NO
  FALLBACK.** Do not add a second way to get a value out of a dump, and do not bring back a byte
  scan for when the parse fails: a truncated dump read that way returns plausible wrong settings. An
  EMPTY blob reads as an empty model (a desk that stopped answering); any other unparseable blob
  raises `ModelUnreadable`, and `read_model_stable` retries until a dump parses. `find_all` matches
  WHOLE names (a prefix match puts `padActiveEdit` in front of the pad list). Test fixtures build
  real trees with `DevTools/fake_model.py`.
- **A PROPERTY'S TYPE IS PER OCCURRENCE, not per name.** `storageVolumeCapacity` is a 32-bit int on
  two volumes and 64-bit on the third. Anything assuming one type per name will read a stale zero.
- **AUTO-MUTE SILENCES THE SCREEN READER, if the screen reader shares the monitor output.** Shaun,
  29th August 2026: he turned monitor auto-mute back OFF minutes after it was proven working,
  because bringing a mic fader up muted the speakers his screen reader speaks through, and he was
  not on headphones. So the feature that prevents feedback also removes the only way a blind
  operator has of knowing what the desk is doing. **Never present auto-mute as simply "on = safe",
  and never turn it on for someone as a fix without saying this.** The real answer is to put the
  screen reader on a DIFFERENT output from the desk's monitor mix, which is what the announcement
  output picker exists for. **This is a WORDING rule, not a live problem: Shaun, 29th August 2026,
  "it's just not a problem, it's all working as expected."** Keep the UI honest; do not re-raise it
  as a blocker or propose fixing it for him.
- **THE SOFTWARE FACTORY RESET AND THE SHOW IMPORT BOTH WORK, proven end to end 29th August 2026.**
  The reset put every checked value back to the known factory dump exactly, and the import restored
  the desk with **ZERO real differences** against a pre-reset model dump (104 raw differences, all of
  them self-clearing `mixLink`/`mixUnlinkRequest` flags, shows-list metadata or free disk space).
  It is the most exact undo there is, but NOT the default one: an import takes the desk off the bus
  about one time in six (see "A show import can take the desk off the bus"), so the app restores
  from a profile and imports only a backup it holds no profile for.
- **A RECORDING'S FORMAT IS READABLE WITHOUT THE DRIVE.** `recordingContent` is a pipe-joined string
  whose field 2 is the FILE PATH, so its extension names the format. Field 0 is the name. **The
  timestamp and duration have no fixed index**: 1.7.3 has them at 3 and 4, 1.7.6 inserts an empty
  field after the path and moves them to 4 and 5. `recorder.parse_recording` finds the epoch-ms
  field and takes the duration from the one after it; a fixed index reads the timestamp as the length.
  **THE SEVEN TRAILING FIELDS CARRY NOTHING ABOUT FORMAT, SIZE OR BITRATE, and this was checked**
  (30th August 2026). Across takes 19-22, which are one WAV and three MP3s at qualities 1, 2 and 3,
  every one of them reads identically: `0|0|1|0|0|1|1`. So there is NO size anywhere in the model.
  Do not re-read the trailing fields hoping for it. The desk's `Meta.xml` beside each take carries
  no format detail either, only name, path, duration, creation and show uuid.
- **`recordingCompressionQuality` IS A TWO-MEMBER ENUM: 0 = WAV, 1 = MP3. WRITE ONLY 0 OR 1.**
  Settled three independent ways on 30th August 2026, and the "it has four values" reading was
  wrong from the start:
    * **The files.** Read off the recordings drive on a PC. 0 gives 24-bit 48 kHz stereo WAV
      (2304 kbps); 1, 2 and 3 give the IDENTICAL 128 kbps CBR 48 kHz stereo MP3, confirmed from the
      MP3 frame headers and from size-over-duration (128.1 kbps each).
    * **The firmware.** The type is `CompressionQualityType`, the touchscreen has a
      `CompressionQualityPage`, and it ships exactly TWO icons for it: `IconCompressionMP3.svg` and
      `IconCompressionWAV.svg`. There is no third state to draw.
    * **RODE's own copy**, which says the desk records "MP3 or WAV". Two options.
  2 and 3 are simply OUT OF RANGE. The property is an unclamped raw store (99, -1 and 255 stick
  too), so the desk keeps whatever you send and the recorder takes any non-zero as MP3. Do not read
  a value sticking as evidence it means anything, nor as evidence it means nothing:
  `recordBytesPerSecond` never moves for it, and the FILES still showed it picks the format. The question "what separates 1, 2 and 3" is CLOSED.
  **Beware the JUCE red herring:** both binaries carry a `64 kbps ... 500 kbps` list next to WAV
  chunk ids (TCOD, ISBJ, INAM). That is JUCE's own audio-format boilerplate, not this desk's menu.
- **THE FORMAT APPLIES TO THE STEREO MIX ONLY. SEPARATE TRACKS ARE ALWAYS 24-BIT WAV.** Proven by a
  real take on 30th August 2026 (post-fader multitrack, raw on, format MP3): the folder held
  `Stereo Mix.mp3` at 128 kbps beside nine `Track*.wav` files, every one 24-bit 48 kHz - mono for
  the four mic tracks, stereo for the rest. **So "raw tracks plus MP3" is NOT self-defeating**, and
  an earlier note here that worried it might be was wrong: the tracks you would edit are lossless
  either way, and the MP3 is just a small reference mix. Say this in UI copy, because "save
  recordings as MP3" reads as if it applies to everything.
- **A TAKE'S `Meta.xml` SAYS `multichannel: false` EVEN ON A MULTITRACK TAKE.** Take 26 had nine
  separate track files and still reported false, and its `path` names only the stereo mix. Do not
  use that field to detect a multitrack recording; count the files.
- **THE MODEL LISTS AT MOST 20 `RECORDING` NODES.** Going from 18 to 22 recordings grew the model by
  exactly 2 nodes and 4 properties and then stopped. Do not read that growth as a stray: check the
  RECORDING count before reaching for the stray alarm, since the totals move for a benign reason.
- **`selector_map.py`'s "DERIVABLE ... no writer yet" IS NOT A TO-DO LIST.** It means "not in that
  tool's ANCHORS table", and ANCHORS holds properties whose selector was CAPTURED. 55 of the 137 it
  listed on 29th August 2026 were already wired in the app, `outputMonMute` among them, which has
  shipped since the 28th. Cross-reference against `Project/` before picking anything off it.
- **SILENCE THE PC BEFORE ANY AUDIO MEASUREMENT.** When Windows' default playback is the desk,
  everything the PC plays goes into the desk and back out on the capture endpoint, so a "silence
  baseline" can be a system sound or a notification cue. Silence the PC, or point playback
  elsewhere, rather than reasoning around the noise afterwards.
- **THE 22 `METER` NODES DESCRIBE THE FRONT PANEL; THEY DO NOT CARRY VALUES.** `PHYSICALINTERFACE`
  (path [0]) is a description of the hardware surface - POT x4, FADER x9, METER x22, PADBUTTON x64,
  SOLOMUTEBUTTON x6 - and the meters are children 13-34, straight after the faders. All 88 meter
  properties sit at exactly 0.0 (48) or exactly 1.0 (40) and not one moves, mic unmuted and
  speaking, across 29 samples. Ten mono ones reading 1.0 would be permanently clipping, so those are
  unpopulated defaults, not readings. **What is disproved is that meter values are in the MODEL
  DUMP** - the RODE app does show moving meters, so the desk has the data and sends it another way.
  The remaining lead is a CAPTURE job (find the subscription or streaming report), not a dump job.
- **THE ANNOUNCEMENT OUTPUT PICKER WORKS, both halves, proven through the real UI 29th August 2026.**
  Announcements set to `Speakers (Realtek(R) Audio)` were heard on the laptop's own speakers, and
  they KEPT COMING while a channel was soloed on the desk - which is the entire reason the picker
  exists, since a solo hijacks the monitor path and would silence anything still going to the desk.
  Shaun set it back to the system default afterwards, so the default remains "announcements go to
  the desk". Do not treat the picker as untested, and do not remove it as unused.
- **`DevTools/say.ps1` IS FOR DEVTOOLS ONLY**; the app's speech is `speech.py`. It speaks
  SYNCHRONOUSLY, so the process cannot exit mid-phrase. **CHECK ITS OUTPUT FOR "SAY spoken"** rather
  than assuming, especially in anything that uses sound as evidence. The app's speech has no focus
  gate: that is `DevTools/say_gated.ps1`, for development sessions only.
- **`outputMonLevel` AND `outputMonFixed` BOTH WORK, ear-proven 29th August 2026.** Fixed OFF, the
  monitor output follows the level; Fixed ON, six alternations between 10% and 100% produced no
  audible change whatever. The UI copy for Fixed is accurate, so do not soften it to "stored".
- **WRITING `outputMonFixed` CLEARS `outputMonMute`, and writing it back does NOT re-mute.** Measured
  on a Pro II 31st August 2026. It is the audible twin of the already-recorded fact that Fixed takes
  the encoder's job away and blanks `encoderColour`: it OPENS THE MONITOR SPEAKERS, with a live mic in
  the room. Anything offering Fixed has to re-assert the mute itself.
- **A DevTools SCRIPT THAT WRITES THROUGH THE SERVER MUST READ WITH `fresh=True`.** `rcp_read`'s dump
  cache is invalidated by a write-generation counter that is PER PROCESS. Writes made by the SERVER
  process do not invalidate the SCRIPT's cache, so a read after a write returns the OLD value until
  the TTL expires. On 29th August 2026 that produced a confident "the monitor level writes are
  landing nowhere" when every one of them had landed.
- **NO WRITE MAY REPORT SUCCESS FROM THE WRITE RETURNING.** This is the whole app's rule and
  auto-mute was the one place still breaking it. If a property is readable, read it back and say
  what the desk holds; if it self-clears (the record commands, the show commands, the marker), say
  what was sent and check the thing it should have changed instead.
- **A FIELD THAT PUSHES NOTHING BACK HAS NOT NECESSARILY DONE NOTHING.** `requestRecordState` and
  `requestDropMarker` push a cleared value when consumed, but other fields do their work silently
  (a wifi scan's results only appear in a later dump). Test for the RESULT, not the
  acknowledgement, before writing a capability off, and check the result came from the write and
  not from something else the desk does on its own.
- **THE SETUP WIZARD AFTER A FACTORY RESET IS TOUCH-ONLY.** The encoder does nothing on its first
  screen (it does work on the time step). So a factory reset strands a blind user until somebody
  sighted taps through it: say so in any UI that offers a reset, and prefer
  `showControlNewFromDefault`, which resets settings and pads without the wizard.

## A show import can take the desk off the bus, and it IS the USB dropout

- **A READ-BACK-ONLY PROOF IS NOT A PROOF, ON EITHER DESK.** Asking whether a value landed at its
  own address never asks whether anything HAPPENED, which is how the Duo's mic gain sat "working"
  for two months while writing a dead address. `test_duo_caps_live.py` only asks the weak question.
  Two ways to ask the real one without ears: make the ENGINE
  answer, by writing something the desk DERIVES from and then diffing the whole tree, or watch the
  DESK move a control and compare the address it pushes to the address you write. And run the
  CONTROL, because the engine answers for hardware the desk does not have.
- **A SECOND CABLE IN USB 2 IS A USEFUL MITIGATION, found by accident.** When the USB 1 side drops,
  the Secondary audio endpoints stay alive, so Windows keeps playing: you lose control without
  losing your ears, which for a blind operator is the difference that matters. The Duo occurrences
  had no such fallback.
- **THEY ARE ONE FAULT, NOT TWO, AND THE "IT LOGS NOTHING" DISTINCTION IS DEAD** (reproduced and
  instrumented 21st September 2026). This was carried for a morning as a separate silent HANG on
  the grounds that Windows logged nothing. The captured occurrence logged the dropout's exact
  fingerprint: event 1010 naming `VID_19F7`, then event 1011 for `VID_0000&PID_0002` **22 seconds
  later**. Whatever was checked on the first two occurrences did not find what was there.
- **THE DESK STOPS ANSWERING LONG BEFORE WINDOWS DROPS IT.** Measured: zero-byte model reads from
  10:57:55, surprise removal at 10:58:59 - **64 seconds**, not the 4 seconds inferred from outside
  on the first attempt. So "the cable or the driver dropped it" starts in the wrong place.
- **THE REMOVAL TAKES THE FILE-STOR GADGET VOLUME WITH IT**, which is the mass-storage gadget the
  USB dropout section below names as its one untested candidate. Not sufficient on its own: it is
  mounted on a healthy desk too.
- **IT IS INTERMITTENT: 5 FAILURES IN 31 IMPORTS, against 0 in 9 profile restores.** An import is
  a SAFE trial - reversible, one replug per drop - so this can be run until it is caught.
- **THE OPEN INVESTIGATION IS TRACKED PRIVATELY**, with the timeline and the dead
  theories. Read the dead ones before theorising: both were measurement artefacts of watching the
  app from outside rather than logging from inside.

## The headphone knobs ARE readable, and they push

- **`potLevel` on the four POT nodes under PHYSICALINTERFACE[0] IS the headphone knob**, and the
  desk PUSHES it per step as the knob turns (199 pushes in 12 s, measured 21st September 2026, the
  pot identified by its path `[0, n]`). So it needs no polling: the announcer speaks it through the
  ordinary level machinery, like a fader.
- **Each pot publishes its own `potMin` and `potMax`** (0 and 127 on a Pro II). Read those before
  trusting the constant on a new device rather than assuming 127.
- **The HEADPHONE blocks carry no level at all** - only `headphoneType` and `headphoneColour`. The
  level is on the POT, which is a different node under a different parent; looking under HEADPHONE
  and concluding the knobs are unreadable is the obvious wrong turn.
- **The knobs have NO TACTILE INDICATOR**, which is why this matters: there is no way to know where
  one is set by touch, so reading it out is the only route.

## Bluetooth pairing is NOT in a show, and the model cannot see pairing at all

- **A SHOW DOES NOT CARRY BLUETOOTH PAIRING** (21st September 2026, on a Pro II with an iPhone
  paired). Importing a show made when nothing had ever been paired to that desk left the pairing
  untouched: it stayed connected through the import, and reconnected afterwards with no pairing
  code. An empty pairing table in the show file would have wiped it. **This was the headline
  argument for keeping shows over profiles**, so it is a decision, not a detail.
- **THERE IS NO READABLE PAIRED-DEVICES LIST.** The bare `btPairedNumber` in RODE's name table is
  not published, and `btPairedNumber1` to `5` stay EMPTY with a phone paired, so any test of
  pairing has to be BEHAVIOURAL.
- **`btConnectedAddress` IS CONNECTION, NOT PAIRING.** It empties the moment a phone disconnects
  and says nothing about what the desk still remembers. Do not read one for the other.

## Zero bytes is not an answer

- **A DESK THAT HAS LEFT THE BUS RETURNS `b""` RATHER THAN RAISING.** `read_model_raw` ends on
  silence and hands back what it collected, which is nothing, and every "did the desk change?" test
  then reads that as a perfectly good answer that differs from the last one. **`rcp_read.is_model_dump`
  is the guard, and anything deciding whether the desk DID something must ask it first.**
- **THAT IS HOW A RESTORE REPORTED SUCCESS WITH THE DESK ALREADY GONE**, cue and all, which is what
  made two occurrences look like they had completed normally. `shows.wait_for` now skips any read
  that is not a dump and hands back no blob at all when none ever arrived, so an import that lost
  the desk says so and tells the user to replug it.
- **THE TEST FAKES MUST OPEN WITH THE ROOT NODE'S NAME** (`Rodecaster\0`), as a real dump does, or
  they are indistinguishable from a dead desk and every check over them is ignored.
  `fake_model.encode`, which the offline fakes are built with, does.

## wake_channels reads the gain from the DESK

- **NEVER FROM THE DESK MODEL.** Its only callers are a factory reset and a show import, i.e.
  exactly the two moments the app's own picture is least to be trusted, because changing the desk is
  what they are for. It took the "put it straight back" half of its nudge from that cache, so **every
  successful restore quietly wrote the app's last displayed gain over the restored one** - measured
  21st September 2026, a backup holding Mic 1 at 55 dB restored correctly and then had 45 written
  over it. `channels.mic_gain` is the paired reader, at the same address `set_mic_gain` writes to.
- It was invisible for as long as it existed because the occurrence that exposed the area had the
  desk off the bus, so the writes went nowhere and the restored value survived by accident. **A bug
  masked by a second fault is the argument for fixing the reporting first**: it only showed up once
  a restore succeeded with the desk alive.
- It now refuses outright when the desk is not answering, rather than writing into the dark.

## The app's lifecycle, and why it cannot be reasoned about from outside

- **INTERVALS AND DEADLINES USE `time.monotonic()`.** A clock change makes wall-clock arithmetic
  fire a timeout early or never. `time.time()` is only for a time shown to a person (`read_at`).
- **EACH ANNOUNCER LOOP CARRIES A GENERATION (`announcer._loop_gen`)** and leaves as soon as it is not the
  newest, without touching `announcer.live_state`. Off and on within one wait otherwise leaves two
  loops running, with two subscriptions and every push handled twice.

- **THE TRAY ICON MUST OPEN ON `WM_LBUTTONUP`, NOT ONLY ON `NIN_SELECT`.** On Windows 11 an icon in
  the hidden-icons overflow receives only `WM_LBUTTONDOWN`/`WM_LBUTTONUP` when pressed, by mouse,
  Enter, a screen reader or UIA Invoke (measured 17th October 2026, `NOTIFYICON_VERSION_4`), so an
  icon answering only `NIN_SELECT`/`NIN_KEYSELECT` does nothing there. One taskbar click can send
  both, so `TrayApp._on_tray` drops repeat open events within `_OPEN_DEBOUNCE_S`.
- **THE TRAY EXE IS WINDOWED, SO EVERYTHING PRINTED GOES NOWHERE**, including the reason anything
  stops. That is why both of 21st September's diagnoses were inferred from the outside and both
  were wrong. **SO READ `%LOCALAPPDATA%\AccessCaster\logs\diagnostic.log` BEFORE THEORISING ABOUT
  ANY LIFECYCLE OR DEVICE FAULT.** `Project/diagnostics.py` tees every `print` into it along with
  the jobs, the records sent, the handles opened and closed, transfer mode, and the desk going
  quiet. Tray menu > Diagnostic log opens it.
- **THE FLUSHER CLOSES A REPEAT GROUP ONLY ONCE IT HAS GONE QUIET** (`diagnostics.flush`). Closed on
  every two-second tick, an event recurring every two seconds or slower never folded: the recorder
  watcher wrote "no control interface" 34 times in the minute a firmware restart took.
  `test_diagnostics.py` pins it.
- **THE LOG IS STARTED IN EXACTLY ONE PLACE, `service.start`,** and that is what keeps it
  a single-writer file: the tray and every DevTools script that runs the service go through there,
  and only the holder of the single-instance lock gets that far. It is SILENT until then, so the
  offline suites write nothing.
- **THE LOG NEVER HOLDS A CREDENTIAL.** It is meant to be safe to send to somebody, and every record
  sent is logged decoded, so `diagnostics.describe_record` hides the value of `wifiPSK` and every
  `*Password` (`_is_secret`). A new credential property whose name matches neither needs adding.
- **MONITOR AUTO-MUTE NEEDS A MIC THAT IS BOTH UNMUTED AND FADED UP, AND RODE'S GUIDE ONLY MENTIONS
  THE FADER.** Their wording is "muted whenever a microphone channel fader is turned up", which is
  incomplete and sends you the wrong way twice: a muted mic with its fader up does NOT engage it.
  The condition is ANY mic channel unmuted with its fader above zero, so with several mics open
  muting one appears to do nothing - the others still hold it - and the monitors only return when
  the LAST one is muted or faded down. That is why it can look like one particular fader is special:
  it is simply the only one in play. Worth knowing when a user reports silent monitors, because
  blind the cause is invisible and the obvious fix (mute the mic you are using) does not work.
- **ONE COPY PER PC, AND THE LOCK IS A `Global\` NAMED MUTEX** (`instance.claim`), which every
  Windows session sees. The rest of the app belongs to ONE session, so a second signed-in account
  is REFUSED rather than served: the announcer speaks into the session that started it, the global
  shortcuts are registered there, the tray icon lives there and the settings written are that
  account's. Windows releases the mutex when the process ends, however it ends, so a crash leaves
  no stale lock and nothing is ever killed to take it.
- **THE LOCK SAYS SOMEBODY HAS THE APP, NOT WHO.** `tray.main` tells the two apart with
  `_running_instance`: `FindWindow` sees only this desktop's windows, so a held lock with this
  session's icon window is our own copy, which is asked to show its window, and a held lock with
  none is another account's, which is refused in a message box. A copy of ours that has only just
  started holds the lock before its icon window exists, so the search waits `_FIND_RUNNING_S`
  before concluding. A DevTools script driving the desk holds the same lock (`_desk.service`, and
  `_bootstrap` for `*_live.py`), so the app started meanwhile is refused as if another account
  had it.
- **AN IDENTICAL EVENT REPEATED BECOMES ONE `repeated ... xN` LINE.** The window polls, the recorder
  watches and the announcer reopens its handle, so a line per event is thousands of identical lines
  with the signal buried in them. The FIRST of anything is always written the moment it happens, so
  nothing is hidden, only counted. A flusher thread closes the group within two seconds, because
  occurrence one of the hang ended with the power lead being pulled and anything still in a buffer
  would have been the part worth reading.

## USB dropout

- **WHAT IT IS.** The desk vanishes from USB, Windows shows ZERO devices with vendor id `19F7`, and
  only a physical replug returns it. Nothing is lost by it.
- **READ THE EVENT LOG FIRST. It answers in seconds what took a whole session to guess at.**
  `Microsoft-Windows-Kernel-PnP/Device Management` (enabled by default) logs event **1010** "surprise
  removed" naming `VID_19F7`. A FAILED return then adds `USB\VID_0000&PID_0002` with **error 411** in
  `.../Configuration` **exactly 22 seconds later**. The 22 s is the fingerprint.
- **IT IS THE RETURN THAT FAILS, NOT THE REMOVAL.** 14 removals since 26th May, 3 of them failed. A
  DELIBERATE reboot logs the identical 1010 pair, so the log cannot tell a dropout from the desk
  re-enumerating itself for its own reasons. Roughly 1 in 5 on a small sample. A replug works because
  it is simply another attempt.
- **WHAT WE THOUGHT IT WAS. All five wrong, and each one sent a session the wrong way:**
  - A DevTools script racing the running server. No second process was near the Duo cases.
  - The dock and its three Realtek hubs. It then happened on a direct port.
  - Shows. 20th July is six weeks before shows existed in the app (`7e4e76b`, 29th August).
  - A full-config rewrite re-enumerating USB "by design". Nothing measured supports it.
  - "Two of two Duo resets dropped." A miscount: that reset was recorded as clean.
- **WHAT DOES NOT REPRODUCE IT:** 45 factory resets and 11 exports, isolated one variable at a time
  (command alone / one open read handle / two / a full dump every 1.5 s through the rebuild window /
  an export). Not one logged a removal at all. `DevTools/test_usb_drop_live.py --layers ...`.
- **WHAT DOES NOT RECOVER IT**, all tried elevated: removing the dead port nodes,
  `pnputil /scan-devices`, restarting every Realtek hub, restarting the root hub ("System reboot is
  needed"), `pnputil /restart-device` on the xHCI controller (succeeded, changed nothing).
  `Disable-PnpDevice` on it answers "Not supported". **And nothing can be SENT to the desk**: it is
  off the bus, so `powerOffRequest` is unreachable, and there is no power-ON property because a
  powered-off desk has no USB either. Only a replug, or switched mains power.
- **STILL OPEN: what makes the desk leave the bus during app activity - but there is now a
  REPEATABLE TRIGGER.** A SHOW IMPORT does it, about one time in six, reproduced and instrumented
  21st September 2026 with the event-log fingerprint (see "A show import can take the desk off the
  bus" above). An import reads the show off the removable drive and
  the removal takes the File-Stor Gadget volume with it, which is the mass-storage lead this entry
  always pointed at. **An import is a SAFE trial** - reversible, one replug per drop - unlike the
  factory resets `test_usb_drop_live.py` uses, so this can finally be run until it is caught.
- **`updateRebootRequested` = True on the SYSTEM block REBOOTS THE DESK** (Duo, 3rd September 2026:
  off the bus at t+12 s, back at t+38 s, firmware unchanged, flag self-clearing). It is the reboot
  trigger the NTP work wanted, and it is a repeatable way to exercise a re-enumeration - so every use
  is a fresh roll of the 1-in-5 above. It stays on the never-sweep list BECAUSE it works.
- **`updateResetAfterFWURequested` TURNS THAT RESTART INTO A FACTORY WIPE** (read from the engine,
  3rd November 2026): while it reads True, `updateRebootRequested` runs the device reset instead,
  deleting the wifi networks, Bluetooth pairings, CallMe sign-in and touchscreen settings.
  `updateResetDeviceRequested` runs the same wipe directly. Never write either, and read the first
  before any restart request: `desk_firmware._restart` and `refusal` both do.
- **THE UPDATE REQUEST FLAGS STAY TRUE AFTER USE** (`downloadInitiateRequested` and
  `downloadCancelRequested`, measured 3rd November 2026). The engine acts on a change to True, so a
  second request written True onto True does nothing. `desk_firmware` writes each False first;
  False is inert. `downloadCancelRequested` leaves `updateDownloaded` True, so a later download's
  end may push nothing: `desk_firmware._download` reads it back after 99 percent and quiet.
- **A CANCEL DURING A DOWNLOAD HOLDS THE DESK'S ENGINE FOR ABOUT TWO AND A HALF MINUTES** (measured
  3rd November 2026, sent at 20 percent): the desk stays on USB but every model dump comes back with
  no root node until it lets go. `desk_firmware` never sends `downloadCancelRequested`; a download
  left behind installs nothing.
- **`updateDownloadProgress` IS A PERCENTAGE, 0 TO 100**, pushed at every percent (measured);
  `updateInstalledProgress` is the one on 0 to 10000. A cancel threshold written for 10000 let the
  1.7.7 download run to the end.
- **AN OS INSTALL DOES NOT RESTART THE DESK; AN APPLICATION INSTALL DOES** (engine, and measured
  on 1.7.7: nothing restarted it in the 45 s after `updateComplete`). After `updateComplete` the
  desk is given `LEAVES_BY_ITSELF_S` to restart on its own, then asked.
- **A DOWNLOAD LEFT UNINSTALLED PUTS AN INSTALL PROMPT ON THE TOUCHSCREEN** until it is answered,
  so a test of the download half leaves a question on the unit's screen for whoever is next.
- **NEVER WRITE `systemBetaMode`**: it moves the desk onto RODE's beta manifest, and a beta OS
  cannot be undone either.

## The encoder

- **The encoder is NOT accessible and that is SETTLED. Do not re-investigate.** It is assignable, but
  its target is chosen BY TOUCH, and touch is not on the USB bus. `encoderColour` and
  `systemChannelSelected` were both tried as a "which target" indicator and ruled out.
  **Re-confirmed for `encoderColour` on 4th September 2026**: seven presses around the ring moved it
  not once. The ring POSITION cannot be known until something moves.

- **`encoderSignal` IS A COMPOSITE (value type 0x08), so its step is the FIRST MEMBER, not the
  value.** Hexdumped off the wire 4th September 2026:

      encoderSignal | 00 | 01 0b 08 | 01 05 01 <int32 LE> | 01 ...    +1 clockwise, -1 anti

  `parse_push` returns it as a list of (value, kind) pairs. Read it as anything else and every turn
  has a delta of 0, which `announcer._at_limit` treats as "do not know", so turning past 100% reads
  as a dead turn. `test_announcer` builds the real composite and pins the delta.
  The desk publishes eight composites: `encoderSignal`, `mixLinkRequest`, `mixUnlinkRequest`,
  `padRecordClear`, `padRecordMemoryFull`, `padRecordStateRequest`, `playerJumpToSample`,
  `sipSlotCallDisconnect`.

- **THE CHANNEL BUTTON TOGGLES, AND THE ENCODER PRESS DOES NOT RELEASE IT.** Measured 4th September
  2026: `systemChannelSelected` went to 0 on the
  press, stayed at 0 across two encoder presses, and returned to -1 only on a second press of the
  same channel button. So while a channel is selected the announcer must NOT advance its ring - doing
  so announced USB 1 and then Chat while the desk had never left Mic 1, and left `enc_pos` pointing
  at an entry `announcer._at_limit` then read.

- **THE ENCODER'S KNOWN POSITION IS A DOUBLE PRESS, AND -1 ALONE IS NOT ONE.** The touchscreen can
  hold the encoder on a target with `systemChannelSelected` already -1 (it was turning Mic 1's gain,
  Pro II, 4th November 2026), so writing -1 changes nothing. Writing 0 and then -1, a channel button
  pressed twice, put it on the virtual faders and monitor: the next turn moved the monitor level.
  That is `server.action_reset_encoder`, which `encoder_home` runs on a dead turn with no channel
  selected, whatever announcements are on, and says nothing: the next turn works. It is not a
  shortcut: telling someone a key exists is no better than telling them to press the channel button
  twice. The strip is selected, never turned, so no gain moves.
  **AN INJECTED `encoderSignal` TURN MOVES WHATEVER THE TOUCHSCREEN HAS SELECTED**, Mic 1's gain
  included, so a probe that turns it (`DevTools/probe_encoder_bind.py`) runs only with the owner's
  say-so. A software bank change and back did NOT leave the encoder dead (same day).

- **AN ENCODER HOLD MUTES THE ENCODER'S RING TARGET, NEVER A CHANNEL-BUTTON SELECTION.** On a
  virtual fader it toggles `channelOutputMute`; on the monitor level, `outputMonMute`. With a
  channel button holding the encoder, a hold of three seconds mutes nothing (Pro II,
  3rd November 2026), so `encoder_hold` does that one itself: it toggles the selected strip's mute
  and speaks it with `announcer.announce_mute`. No echo of that write reaches the announcer, so it
  cannot rely on one (Pro II, 3rd November 2026). The announcer speaks both mutes (`change_words.OUTPUT_MUTES` for the monitor), counts
  either one during a press as a hold, and seats its ring position from it (`_ring_place`): a hold
  moves no level, so without that a hold straight after a restart leaves the next press saying only
  "Select".

- **SAY WHAT A CHANNEL HAS, NEVER WHAT IT LACKS**, in the dead-turn message as well as the selection
  one. It used to assert "has nothing for the encoder to adjust" for any turn the ring did not see,
  which included a mic gain that was moving perfectly well. Both go through
  `announcer._selected_phrase` now, which works it out from the strip and so cannot claim it wrongly.
- Two rules follow: **never write "use the encoder" for a named level** in UI copy or docs, and **key
  every spoken announcement off the PROPERTY THAT CHANGED**, never off an assumption about the
  encoder. Done that way, turning the encoder on a channel correctly announces that channel.

## Global keyboard shortcuts

- **A PAD SHORTCUT WRITES TO THE DESK UNDER `desk_access.hid_lock`, THROUGH `pad_shortcuts._desk`**, which gives
  up after `_DESK_WAIT_S` and says the desk is busy. A `padActive` or bank write sent while a profile
  restore has the desk in transfer mode lands inside the transfer.
  **A SOUND PAD PLAYS THROUGH A RESTORE, BY DESIGN.** Once MIDI is armed and the bank is cached, a
  sound-pad press is a MIDI note and touches no HID, so it neither waits nor is refused: measured on
  the Pro II, 31st October 2026, pads played during a profile restore with a second or two of lag at
  most. The restore re-arms MIDI when it ends, so the first press after it writes
  `systemMidiControl` again. Only voice-FX and mixer pads, a bank change and the arming write go
  through `_desk`.

- **THE WINDOW SHORTCUT MUST RUN ON THE PUMP THREAD, AND THAT IS THE WHOLE FIX. DO NOT ADD A
  WORKAROUND TO THE RAISE.** Windows grants a process the right to set the foreground while it is
  handling a hotkey it registered with `RegisterHotKey`, and that right is transient. `_fire`
  dispatches every shortcut to a freshly spawned thread so a slow device write cannot delay the
  next press, and that thread hop SPENDS THE RIGHT: by the time it calls `SetForegroundWindow`
  Windows refuses. `hotkeys._FOREGROUND_COMMANDS` is the exception list that runs such a command
  inline instead; `app.window` is on it.
  **A FORCED RAISE IS NOT AN ACTIVATION, AND EVERY MEASUREMENT SAYS IT WORKED.** With the right
  spent, the only way up is to force it - a minimize/restore flip, a synthetic Alt tap,
  `AttachThreadInput`. All of them put the window in front and move the keyboard focus, so
  `GetForegroundWindow` returns it, `GetGUIThreadInfo` agrees the focus window is it, and it holds
  for seconds. **And a screen reader goes on reading the app you came from**, because Windows never
  raised a normal activation. Alt-Tabbing to the same window by hand announced it perfectly, which
  is what proves the window and the screen reader are both innocent.
  So the test for this is never "is it the foreground" - that passes while broken. It is whether a
  screen reader follows, or failing that whether a plain permitted `SetForegroundWindow` was enough.
  **Do not answer a refusal by forcing it, and do not answer the silence by speaking**: bringing a
  window forward is a Windows action, the screen reader announces it, and the app's own announcer
  is for the DESK.

- **A FRESH INSTALL GETS SIXTEEN SHORTCUTS, NOT FIVE.** `shortcuts.defaults` is the five app ones plus
  the whole of `shortcuts._pad_scheme` (what is playing, bank up and down, one per pad slot). The pad
  half is a separate function only because `shortcuts.action_add_pad_shortcuts` can lay it onto a list that
  already exists. The Help guide said "you start with five" for weeks after that changed.

- **A DISCONNECTED WINDOWS SESSION CANNOT FIRE A HOTKEY, AND IT LOOKS EXACTLY LIKE A BUG.** With the
  RDP window closed there is no input desktop, so `keybd_event` injects nothing: registration still
  reports success, a posted WM_HOTKEY still dispatches, and only REAL presses do nothing. That cost
  an hour on 30th August 2026 and nearly caused a working rework to be rewritten. `query session`
  first (the current session's line starts with ">"; "Disc" is the tell).
- **`problem == ""` IS NOT PROOF A SHORTCUT IS REGISTERED.** It is also what an empty status dict
  says before `_apply_now` has ever run. When a press does nothing, do not read an empty problem as
  "registration is fine".
- **THE COMMAND CATALOGUE IS GENERATED, NEVER HAND-WRITTEN.** `commands.py` builds it from the
  channel list, fader spec, pad grid, mix schemes and presets. Do not add a command by typing it in
  a list: add it to the spec table it belongs to and it appears here. What may be offered: one
  press, no arguments, no decision, and nothing destructive.
- **A COMMAND ID IS A PERSISTED HANDLE** ("chan.mute:Mic1", "pad.fire:14"). Renaming one breaks
  every saved shortcut using it. `run()` splits on the FIRST DOT for the namespace, so a colon may
  only separate a command from its target.
- **The shipped defaults are laid down ONLY when nothing has been saved.** Deleting every shortcut
  is a choice; re-applying the defaults over an empty list would undo it on the next start.
- **REGISTRATION AND THE MESSAGE PUMP MUST BE THE SAME THREAD.** RegisterHotKey delivers WM_HOTKEY
  to the thread that registered it, and only while that thread is in GetMessage. `hotkeys.py` posts
  `_WM_RELOAD` to its own pump instead of registering from the caller's thread; register from an
  HTTP thread and the presses go to a thread that never pumps, so nothing ever fires.
- **A SHORTCUT MUST CARRY CTRL, ALT OR WINDOWS.** Shift alone is not enough (Shift+A is a capital
  A). A hotkey takes that key from every program on the machine, screen reader included, so
  `parse_binding` refuses the rest rather than letting someone break their own typing. MOD_NOREPEAT
  is always set, or a held key fires the action over and over.
- **The cough key (`commands._cough_key`) OPENS ONLY THE MICS IT MUTED.** It keeps `_muted_mics`. A plain
  "any open? mute all, else unmute all" toggle silently opens channels somebody deliberately shut:
  measured 30th August 2026, one open mic and three muted went to four OPEN after two presses.
  A mic on no fader has no strip and so no mute: it is SKIPPED (`channels.strip_on_fader`), not
  refused, or one mic taken off its fader stops the key for every other (`test_cough_key`).
- **Hotkey actions must speak their result**, via `speech.say`, which is independent of whether the
  announcer is on. The window is not in front of you when a shortcut fires; the speech is the only
  feedback. Silence is what a broken shortcut sounds like. **EXCEPT where the user has silenced that
  shortcut** (per row, `speak`), and except the announcements shortcut turning announcements OFF,
  which is ALWAYS silent: the reason to press it is that announcements are reaching a recording. **A
  FAILURE is spoken whatever the flag says** - silencing hides a confirmation, not a fault. With
  announcements off it is the "failed" cue instead (`shortcuts._failed`, `speech.tone`'s
  `despite_off`), the one sound made then; during a take it stays silent.
- **THERE ARE TWO WAYS TO FIRE A PAD AND THEY COVER DIFFERENT PAD TYPES. USE MIDI FOR A SOUND PAD
  AND HID FOR THE REST.**
  - **HID, `padActive`** (`soundpad.engage_pad` / `unlatch_pad`): voice-FX pads engage and release,
    and a MIXER pad performs its whole action, because for those two the FLAG IS THE STATE. **A
    SOUND PAD SETS THE FLAG AND PLAYS NOTHING** (ear-proven negative, 30th August 2026): a physical
    press pushes `padButtonPressed`, then `padActive`, then a stream of `padProgress`, and this
    reproduces the middle event only, so the engine never starts playback.
  - **MIDI, CC 35 on the pad's own channel** (`DevTools/play_pad.py`, 2nd September 2026): plays a
    SOUND pad, which is the case HID cannot do. Requires `systemMidiControl` ON, which is an
    ordinary HID write. Reaches only the bank the desk is currently on.
  - **FOR A SOUND PAD, `padActive` IS INERT IN BOTH DIRECTIONS.** It does not start one, and
    measured 2nd September 2026 it does not STOP one either: a Music_Bed started over MIDI kept
    pushing `padProgress` at full rate (21 pushes in the next second) after `unlatch_pad` sent
    `padActive` OFF, and only a second MIDI press stopped it. So a stop has to go over MIDI too.
    Do not reach for `unlatch_pad` as a sound-pad stop.
  - **WHETHER A SECOND MIDI PRESS STOPS OR RESTARTS IS THE SOUND PAD'S OWN `padPlayMode`, and there
    is NO stop for a One Shot pad.** (A MIXER pad, type 3, uses `padMixerTriggerMode` instead and
    `padPlayMode` is meaningless to it.) Measured 2nd September 2026: on a TOGGLE pad the second press stops
    it (progress to 0.0 and the pushes cease); on a ONE SHOT pad the second press RESTARTS it
    (progress jumps back to 0.0 and climbs again), and so does the third. **And there is no
    global stop-all**: nothing in the property list is a pad stop action, only per-pad envelope
    values. So "every pad stops on a second press" is NOT achievable without changing the pad's
    stored `padPlayMode`, which is a DESK setting - it changes the physical pad and the RODE app
    too, so it can never be "just for the shortcut".
  - **HOLD MODE NEEDS THE NOTE HELD.** `padPlayMode` 1 plays only while the pad is held, so a
    fire-and-forget 127-then-0 would give a blip. Send 127 on the press and 0 on the stop. Untested,
    but it follows from the mode and costs nothing to get right first time.
- **NORMALISING A PAD'S MODE ON THE FLY WORKS, BUT ONLY BEFORE THE SOUND, NEVER DURING IT.**
  Measured 2nd September 2026, because writing `padPlayMode` to Toggle and reverting it afterwards
  is the obvious way to make every pad stoppable from a shortcut.
  - **WRITE FIRST, THEN FIRE: works.** Airhorn (One Shot) set to Toggle in 0.87 s, then a second
    MIDI press really did stop it, then reverted in 0.99 s, read back clean both ways. So the
    mechanism is sound and the state is recoverable.
  - **FIRE FIRST, THEN WRITE WHILE IT PLAYS: THE WRITE KILLS THE SOUND.** Same test the other way
    round: progress ran 0.007, 0.064, then 0.0 as the write landed at 0.92 s, and playback ended.
    The next press then STARTED it again rather than stopping it. So the appealing design - honk
    instantly, fix the mode in the background - does not exist.
  - **THE COST IS THE TRANSFER-MODE SESSION, NOT THE PAYLOAD, so writing fewer properties does not
    make it cheaper.** `set_pad_properties` wraps every write in one transfer-mode session with
    fixed dwells either side, and going to Toggle already writes `padPlayMode` ALONE (the `padLoop`
    write only happens on the way TO One Shot). Shortening those sleeps is a settled negative: they
    are what make the write commit.
  - **So the honest choice is 0.87 s of latency in front of every sound-pad shortcut, or respecting
    the pad's own mode.** A live airhorn nearly a second late is the thing to weigh. Note also that
    the desk's own factory assignment already matches the use case - the long beds are Toggle and
    the short stingers are One Shot - so the pads you actually want to stop mostly already stop.
  - **NEVER inject `padButtonPressed`.** It reads back exactly as written and moves nothing else in
    the whole model, on either address encoding, for a sound pad or a mixer pad. The addressing
    behind that negative is settled, so it is real and not a wrong address.
  - **`commands.PAD_TYPES` NOW OFFERS ALL THREE** (sound, voice FX, mixer). It encoded the HID-only
    limit, and widening it was the whole pad-shortcut job of 2nd September 2026. The dispatch is
    `pad_shortcuts.fire_pad`, per pad and never per bank: the kinds are mixed inside a bank.
- **THERE IS NO REFRACTORY WINDOW ON A SECOND PRESS, and two probes "proved" one that was not
  there.** A second press stops a Toggle sound pad at every gap down to 0.3 s (measured 2nd
  September 2026, `DevTools/probe_pad_timeline.py`). The two probes that found a one-second dead
  window both measured the push rate BETWEEN the presses without ever confirming press 1 had
  STARTED anything - and a pass that began with a pad already playing (press 1 stops it, press 2
  starts it) reads identically to an ignored second press. **A playing sound pad survives an
  ordinary teardown**, because `padActive` OFF does not stop one, so "already playing" is the
  NORMAL state at the start of a pad test, not a rare one. Time pad presses off a timestamped
  stream read afterwards; never off a rate measured in a window.
- **`padActive` / `padProgress` ARE PUSHED AT THE STREAM POSITION**, path `[666, position]`, which
  is the same address our own write uses. Do not read the old "8 + padIdx" claim as an alternative:
  on a factory Pro II every user pad sits at exactly that (the 8 system pads hold positions 0-7),
  so the two agree there and only disagree where it matters. **Only SOUND pads publish progress**,
  and **a HID-engaged pad pushes nothing at all** - the desk does not echo our own write, so a
  voice effect switched on from software is knowable only from `pad_shortcuts._pads_on`.
- **A PAD SHORTCUT MUST NOT REFRESH THE PAD MAP ON EVERY PRESS, and doing so is what made pad
  firing flaky** (Shaun, 2nd September 2026: "sound pad playback and stopping is flakey"). A
  refresh is a FULL MODEL DUMP, 1.5 to 3 seconds holding the HID lock, so a row of number keys at
  performance speed queued one per press - and `HotkeyManager._fire` DROPS a re-press while the
  last one is still running, so presses went missing rather than merely arriving late. It is a TTL
  now (`pad_shortcuts._PAD_CACHE_S`) plus an explicit `pads_stale()` from `_pad_write` and from a
  profile restore. It was never needed on the MIDI path anyway: MIDI addresses a pad by bank and slot,
  which does not move.
- **FIRING A PAD IS THE ONE SHORTCUT THAT DOES NOT SPEAK** (Shaun, 2nd September 2026:
  "announcements for each pad has to go"). The pad is its own feedback, it is loud, and it is often
  going out on air. Decided in ONE place, `commands.speaks_by_default`, so the shipped set and a
  hand-added shortcut agree. Bank changes still speak (where you are is not audible) and failures
  speak whatever the flag says.
- **THE APP'S MODIFIER CHORD IS CTRL+ALT+SHIFT, AND IT IS ONE CHORD FOR EVERYTHING** (Shaun, 2nd
  September 2026: "having 2 different chords isn't good"). It was Ctrl+Alt+Windows until the pad
  scheme needed the NUMBER ROW, which that chord could not have. **The holder was NOT Windows**,
  though Windows does own Win+digit for the taskbar: `Ctrl+Alt+Win+digit` is not a Windows shortcut
  at all, and on Shaun's PC it was `InstaAccess.exe`, his own webcam project, using it for camera
  presets. So the lesson is not "avoid the Windows key" but that a popular chord plus the number
  row is contested and you cannot know by whom. `DevTools/probe_free_chords.py` asks
  `RegisterHotKey`, which is the only authority; **a combination our own server holds reads as
  taken, so stop it before trusting a refusal.**
- **`systemMidiControl` IS NOT ON BY DEFAULT and A PROFILE RESTORE CAN SWITCH IT OFF.** It read OFF
  on the Duo, both in an earlier probe and on the attached desk (2nd September 2026); unread on
  a Pro II. And it is saved into a profile and re-applied by `profiles.apply`, so restoring a
  profile taken while MIDI was off silently kills every pad shortcut. So anything depending on MIDI
  must ENABLE IT AT THE POINT OF USE rather than once at startup - the write is idempotent and
  measured at 0.004 s, so there is no reason to be clever about it. Do not read-then-write: a read
  costs a dump, and a remembered value can be stale in the one direction that matters.
- **LEAVING THE DESK OPEN TO MIDI IS NOT A HAZARD, measured 2nd September 2026 rather than assumed.**
  The port is USB, so LOCAL ONLY: nothing off the machine can reach it. It is also SHARED, not
  exclusive - two `midiOutOpen` calls on it both succeeded - which mostly helps, because our own
  sends can never be blocked by another app holding the port. MIDI here is CONTROL, not
  CONFIGURATION: nothing is written persistently, so the worst case is an unwanted pad trigger or a
  control moving, nowhere near the stray-property class. The app already exposes a MIDI switch on
  the Device settings tab, so the user's escape hatch needs no new UI.
- **READ PAD FIELDS OFF THE TREE (`soundpad.pad_nodes`), NEVER BY SEARCHING THE DUMP FOR A NAME.**
  Field order inside a pad record varies (factory records put `padName` first, records written by
  us or the RODE app put `padType` and `padColourIndex` before it), so a reader that windows the
  dump on `padName` hands those fields to the neighbouring pad; measured 3rd October 2026, it
  misread the colour of every pad and Rim_Shot's `padReplay`. Pairing a name's k-th occurrence with
  the k-th pad is no better: a pad that lacks a field shifts every pad after it. The enumeration
  itself is in `SOUNDPADS.md`; note that **type 3 is MIXER, not a voice effect**, which is why
  "Censor" and "Ducking" read as 3.
- **SELECTING THE PAD BANK IS AN ORDINARY HID WRITE: `selectedBank`, an int on the GUI block,
  selector 0x07** - the same mapped node as `metering`, which has been written and restored for
  months. Proven 2nd September 2026 with the ENGINE as the judge, not a read-back: with the bank at
  0, MIDI pad 1 fired padIdx 0; after writing `selectedBank` 1, the identical MIDI message fired
  padIdx 6, which is bank 2's first pad on a Duo. Restored clean. **So the MIDI bank-select question
  (CC 0 or CC 1, marked "verify" in RODE's spec) never needs answering** - do not spend time on it.
  Banks are 0-indexed here and bank 8 (index 7) is the system pads, which are video switching
  controls for a paired RODECaster Video.
- **A captured `cmutePressed` is `mutePressed` at path [0, 0x63].** The 'c' is the address
  byte, not part of the name, and 0x63 is child 99 of PHYSICALINTERFACE - the first SOLOMUTEBUTTON.
  PADBUTTON is children 35 to 98. Do not go looking for a property called `cmutePressed`.
- **A pad shortcut's target is the padIdx, never the stream position.** padIdx is the pad's fixed
  place in the grid; position moves when any pad is created or cleared. `pad_shortcuts._pad_map`
  caches padIdx -> position so a press is instant, refreshes in the background once it is stale, and
  CHECKS that the pad that engaged is the one that was asked for. Keep that check: without it a
  stale map silently fires a stranger's voice effect onto a live mic.
- **`shortcuts.manager()` is LAZY.** Importing a module must never register a system-wide hotkey.
  Only `service.start` asks, and never for a DevTools run.
- **`test_hotkeys.py` PRESSES NO KEYS, AND MUST NOT AGAIN.** It fires shortcuts by posting
  WM_HOTKEY to the pump. Injected presses proved only that Windows delivers a registered key, which
  any real press shows, and a run killed between a press and its release left Ctrl and Shift held
  down system-wide on the machine in use. `DevTools/press_keys.py` uses `keybd_event`, not
  SendInput, which injects nothing from an RDP session here.

## The server's shared shapes

Four shapes in `server.py` each existed as many hand-written copies until 30th August 2026. Use the
shared one; a new copy is how they drifted apart the first time.

- **A LONG JOB IS A `desk_access.BackgroundJob`.** `start(work, message=..., ...)` claims it (refusing plainly if one is already
  running) and runs `work(job)` on a daemon thread; `work` REPORTS with `job.step` (message only)
  or `job.tick` (message and count) and RETURNS its result dict. The class writes the finish and
  turns a crash into a sentence, so a failure can never leave a progress dialog that never closes.
  The page only reads `message`, `done` and `ok`.
- **A LIVE PAD EDIT IS A `_pad_write`**: range-check the grid, take the HID lock, call, turn a crash
  into a sentence. `invalid=` is a refusal for a value the helper cannot check itself and is
  checked AFTER the range and BEFORE the lock (the pad number is the more basic complaint, and
  nothing should take the device lock to find out a value was wrong). `plain_value_error=True`
  surfaces a ValueError's own text, because soundpad raises those with a finished user-facing
  sentence and wrapping it buries the part that says what to do.
- **A RISKY WRITE FAMILY IS GATED BY `desk_access.cap_gate`**, which holds the shared reasoning: a selector
  proven on a Pro II is NOT proven on a Duo, and a wrong one can append a stray only a factory reset
  clears (it happened, 26th August 2026). Each gate keeps its OWN refusal sentence, because what the
  user can and cannot do differs per family. **`_level_write_gate` is deliberately not on it**: it
  has a second condition beyond the cap (the device must carry confirmed faderLevel selectors).
- **The master Compellor and output delay come from `rcp_read.master_processing`**, shared by the
  Routing page and a profile. They were built twice, so a property added to one and not the other
  would have been a setting the page could change and a profile could not put back.

## The announcer, speech and pad-shortcut modules

- **THE ANNOUNCER, SPEECH AND THE PAD SHORTCUTS NEVER IMPORT THE SERVER.** `announcer.Hooks` is
  everything the announcer needs from the app (the device lock, the mixer pads, what a reconnect
  means), and the server installs `_AnnouncerHooks`, whose methods look the state up when called,
  so a test that patches `pad_shortcuts.mixer_positions` still reaches the decoder. The pad
  shortcuts need only the device lock, `desk_access.hid_lock`; a module that took its own lock
  would let a pad press write in the middle of a restore. Call them as `speech.say(...)`
  and read `announcer.live_state`, never `from speech import say`: the tests patch the module
  attribute, and a name imported into another module keeps the original.
- **THE SERVER IMPORTS NOTHING ABOVE IT.** `commands`, `shortcuts`, `app_settings_actions` and
  `service` import the server, and the server imports none of them: that is what lets `commands`
  import it at the top. A server function that needs one of them belongs in that module.
- **A CHANGE MADE ELSEWHERE IS SPOKEN AS ONE SENTENCE PER BURST** (`announcer._note_change`,
  `change_words.summarise`). Speech keeps only the newest phrase waiting, so a phrase per push
  leaves a preset's thirty parameters, or a batch revealed on return from RODE's app, heard as its
  last one.
- **A CHANGE IS A VALUE DIFFERENT FROM `push.was`, never a push alone.** The desk re-pushes values
  it already had, and this app's own writes reach the desk model before the desk echoes them, so
  the echo arrives with `was` equal to the value and is not spoken over the window's own reply.
- **AN UNLINKED CROSS-POINT IS A SEND, NOT ITS FADER.** `mixLevelWithAnchor` with level and anchor
  equal follows the fader; unequal, it is that source's level in that one output. Read as a fader,
  a send of Mic 3 to Headphone 4 is announced as Mic 3's own level.
- **A FADER'S SOURCE CHANGING PUSHES THE LEVELS OF THE NEW LAYOUT**, which read as movements ("100
  percent, Bluetooth", "0 percent, Bluetooth"). For `_LAYOUT_QUIET_S` after `channelInputSource`
  they are recorded as said. A move from the touchscreen is unmeasured: if the desk pushes the
  levels before the source, or no source at all as it does for RODE's app, they are still spoken.
- **WHILE RODE'S APP IS OPEN, CHANGES ARE GATHERED, NOT SPOKEN** (`announcer.gather_changes`,
  released by `speak_gathered` after the read on return and spoken as RODE's app closes). Moving a
  channel to another fader there pushes the levels and sends it rewrites but NOT the
  `channelInputSource` itself (Pro II, firmware 1.7.7), so spoken live they are stray lines with
  no move to explain them.
- **A CHANNEL THAT MOVED FADER CARRIES ITS STRIP AND ITS SENDS** (`change_words._without_what_moved`):
  with a move in the burst, the processing, effects, mute and solo of the strips involved and the
  levels and sends of the sources moved are not news. Unfiltered, a swap reads as "USB 2: 21
  processing settings changed".
- **WHAT A SETTING IS CALLED LIVES IN `desk_labels` ONLY**: the window's controls, the server's
  replies and the announcer's words all read it, so a label is never copied into one of them.

--------------------------------------------------------------------------------
## Tooling

- **EVERY NUMBER IN `CodeMap.md` IS GENERATED. Never edit one by hand.**
  `python DevTools/codemap_lines.py` rewrites them; `--check` reports drift and exits 1. It covers
  the `name` (NNN) references, the file table's line counts and the DevTools file count, and it
  touches no prose. They were regenerated BY HAND on 29th August 2026 and all 96 were wrong again by
  the 30th, drifting up to 755 lines, because one day's work moves server.py by hundreds.
  A confidently wrong line number is worse than none: you jump, land elsewhere, and grep anyway.
- **THE RELEASE NOTES ARE GENERATED FROM `version.py` HISTORY, which is a LIST, not prose.**
  `ProjectBuilds/make_release_notes.py` writes `Project/ReleaseNotes.html` before the freeze; the
  spec bundles it and the installer offers it, ticked, as a post-install action. It is gitignored:
  it is derived. A hand-written notes file beside version.py is the second copy this project always
  ends up disagreeing with, which is why the history became data. HTML rather than text because a
  screen reader can jump heading to heading through it and cannot through Notepad.
- **A SCRIPT THAT FACTORY-RESETS A DESK MUST CALL `_bootstrap.require_expendable_desk` AND REFUSE
  WITHOUT `--expendable-desk`.** One does: `test_usb_drop_live.py` (one reset per trial). The
  exception is `test_clean_restore_live.py`, which restores itself: it takes `--yes`, and once its
  clean restore has started it puts the desk back from its own profile, however the run ends. A
  capitalised warning in the docstring is NOT the guard, and this is measured, not theoretical: `test_usb_drop_live.py` opened with "EXPENDABLE DESKS ONLY" and was still run
  over Shaun's working Pro II on 16th September 2026 by a sweep that selected tests by filename. The
  reader it warned was not the thing that ran it.
- **A `test_*.py` SCRIPT GETS A THROWAWAY DATA DIR, AUTOMATICALLY.** `_bootstrap` sets
  `ACCESSCASTER_DATA_DIR` to a temp folder when `sys.argv[0]` starts with `test_`, before the first
  Project import, which is the only moment it can work: `profiles.PROFILES_DIR` and
  `routing_schemes.SCHEMES_DIR` are derived from `paths.DATA_DIR` at import. It is keyed on the NAME
  rather than an opt-in call because an opt-in is what a new test forgets. The probes and the
  capture tools are not tests and keep the real data dir, which is the point of running them.
- **A `Project/` function called only by a probe or a live test in `DevTools/` is in use, not dead.**
  A dead-code sweep that searches `Project/` alone reports it as unused.
- **THE GENERATED REFERENCES ARE ONE FILE PER DESK, and must stay that way:**
  `docs/PROPERTY_LIST_<DESK>.md`, plus the private per-desk coverage reports, where
  `<DESK>` is `devices.DeviceModel.slug` (`PRO2` / `DUO`). They shared ONE device-agnostic path until
  31st August 2026, so regenerating on the Duo silently rewrote the Pro II reference with Duo data,
  and **`property_list.py --check` could not catch it because it compares NAMES only, and two
  desks on the same firmware publish identical names** (463 each, measured on 1.7.3). It printed "unchanged" while the file had
  swapped desks. Anything that needs only the NAME universe can stay device-agnostic and should
  (`test_specs.py`, `check_notes.py` both glob); anything reading the occurrence COUNTS must be
  device-scoped, because counts differ per desk AND move with ordinary use.
- **`property_list.py --blob` takes the desk from the DUMP, not from what is plugged in**, via the
  dump's own `systemName` and `devices.device_from_system_name`. Reading a Pro II capture while a Duo
  is attached would otherwise write the Duo's file with Pro II data. It refuses rather than guess if
  the dump names neither desk.
- **THE TREE CAN CONFIRM A SELECTOR WITHOUT A CAPTURE, and that is how a blocked feature gets
  unblocked.** Ask the live dump: does a node exist at that root-child index, is it the block you
  expect, and does it carry the property you mean to write? `rcp_read.node_at` / `path_value` answer
  all three. That is STRONGER than a capture of one property, because it checks every property you
  would write rather than the one somebody happened to click. It is what turned the Duo's
  `unit_settings` cap on (31st August 2026): all 20 settings across GUI 0x07, OUTPUT 0x0D and SYSTEM
  0x0F resolved, then one write per block proved it. **Before parking anything as "no capture on this
  desk", run this check.** It does not license a blind write to a node that does NOT resolve - that
  is still the stray hazard.
- **A LIVE TEST MUST NOT WAIT A FIXED TIME FOR THE DESK, and must not compare DERIVED state.** Poll
  until two consecutive whole-tree reads agree (`settled` in `test_duo_caps_live.py`); a `sleep` is a
  race, because a write can drag other properties along a beat later. And `encoderColour` is derived
  live state that DECAYS to -1 about 30 seconds after the encoder goes idle, so it will differ
  between two runs of the same test in the same order and read as flakiness. Exclude derived values
  from a restore comparison, with evidence, not because they are inconvenient.
- **AN OFFLINE TEST MUST PIN ITS DESK: `devices.force_device(devices.PRO2)` at the top.** Otherwise
  `get_active_device()` answers from the USB port, so the same test passes on one machine and fails
  on the other with the code untouched, and the failure reads exactly like a hardware finding.
  `test_announcer`, `test_shows_sim` and `test_level_check` all did this, and each had to be re-run
  against unmodified code to prove it meant nothing. **Never call it from app code:** the desk in
  front of you decides. A test that needs BOTH desks passes the device explicitly instead, the way
  `test_input_read.py` does.
- **OCCURRENCE COUNTS ARE NOT A DEVICE SIGNATURE.** They move with how the desk has been used: the
  effects family reads x17-18 on a working Pro II and x10-12 on a factory Duo, and the show and
  recording CONTENT rows appear only when the drive holds recordings or shows. Before reading a count
  difference as a model difference, ask whether the two desks were in comparable states. That error
  was made twice in one session on 31st August 2026.

- **Import `DevTools/_bootstrap.py` FIRST in any new DevTools script** that touches the Project
  modules. 19 of 48 scripts once could not run as documented without it.
- **`transferModeType` = 2 MOUNTS PAD STORAGE ONLY.** Not the card, not the USB drive: F:, 5033 MB,
  a `\pads` folder. Value 1 mounts nothing. Measured 29th August 2026, so do not reach for
  transfer mode to get at recordings.
- **THE DESK NEVER PUSHES A `STORAGEVOLUME` RECORD.** Neither a record start/stop nor an unplug and
  replug of the drive produces one; you get `recordState` (0x03) and `lastRecordingID` (0x0F) and
  nothing else. So `state_observatory.py listen` cannot learn that block's address, however long
  it listens.
- **`storageVolumeState` IS THE LIVE SOURCE, `storageVolumeCapacity` / `Free` / `Inserted` /
  `Mounted` READ STALE ZEROS.** The string is `capacity|free|?|inserted|mounted`. A fourth volume
  appears while recording: it is the unit's live view of the drive being written to, already handled
  by `recorder.read_volumes(include_extra=True)`, and it is NOT a real extra device.
- **THE CAPTURE TASK MUST BE RUNNING.** `AccessCasterCapServer` in state **Ready is NOT polling**;
  `cmd.txt` is read only once `Start-ScheduledTask` puts it in Running.
- **REMOVE THE CAPTURE TASK WHEN CAPTURING IS FINISHED.** While `AccessCasterCapServer` is
  registered, anything running as the user can edit `cap_server.ps1` or the Python it runs, all in
  the user-writable `DevTools` folder, then start the task and get an elevated process with no
  prompt. It is kept registered between sessions on purpose, because registering it costs a UAC
  prompt, so check whether it is still there (`Get-ScheduledTask AccessCasterCapServer`) and remove
  it once USB capture is no longer in use (`Unregister-ScheduledTask AccessCasterCapServer`, elevated).
- **A DEVTOOLS SCRIPT THAT DRIVES THE DESK NEEDS THE APP QUIT, and refuses to start while it
  runs.** The app holds the desk, and there is no way in from outside the process: a script runs
  the service itself (`_desk.service`, with `start=True` for one that needs the watchers), and the
  single-instance lock refuses it while the app is up. Quit from the tray, run the script, then
  start the app again. A change to the app's code needs the app restarted to take effect.
- **When driving the GUI: the Windows "System tray overflow window" can sit OVER the app and silently
  swallow clicks.** It is topmost, does not show up in a plain EnumWindows sweep, and `WindowFromPoint`
  returns 0 until the probe sets DPI awareness. `PrintWindow(h, dc, 2)` captures the app's own pixels
  underneath any overlay, which is how it was diagnosed.
- **`SetCursorPos` is a NO-OP over RDP.** Use SendInput absolute-move.
- **`.bin` model dumps and `duo_matrix_*.json` are gitignored** because they carry plaintext wifi and
  SIP state. Never commit one.
