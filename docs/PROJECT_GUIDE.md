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
of firmware 1.7.6 on a RODECaster Pro II and 1.7.3 on a RODECaster Duo.

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

A Windows system-tray app (`Project/tray.py`) that starts a local HTTP server (`server.py`) and opens
a web page. The UI is plain HTML and JavaScript talking to a JSON API. The server binds all
interfaces so a phone in another room can drive the desk, and a token on the link keeps everyone
else on that network out: requests from the machine itself are always allowed, anything else needs
the token once, and a cookie carries it from then on. Everything that touches USB is
behind one lock, because HID is a single resource and the page, the announcer, the meter watcher and
the shortcut thread all want it.

The layers, bottom up:

- `rcp_hid.py` opens the interface and owns the transport.
- `model_tree.py` parses a dump into a tree. **There is one parser and one index per dump**, and
  two real bugs came from the cruder readers it replaced. Do not add another. The one exception left
  is `soundpad.py`, which still windows the raw dump with `blob.find` (see the `padType` entry).
- `rcp_transfer.py` builds every write record, and owns Transfer Mode.
- `rcp_read.py` serves every lookup from the cached parse.
- Feature modules on top: `channels.py`, `soundpad.py`, `profiles.py`, `routing_schemes.py`,
  `recorder.py`, `shows.py`, `meters.py`, `hotkeys.py`, `commands.py`.
- `devices.py` holds one model per product id, so Pro II and Duo differences are data rather than
  branches, with a capability set that refuses a write BY NAME rather than writing blind when a
  selector has never been observed on that desk.
- `server.py` exposes it as an API, and `static/` is the accessible UI.

Two rules do most of the work of keeping it honest:

- **One spec drives everything.** A setting is defined once, and that definition produces the read,
  the write validation, the control the browser builds, and what a profile backs up. Adding a
  setting in one place adds it in four, so the UI and the backup cannot drift apart.
- **Read back every write.** The address model predicts which BLOCK a property is in, not that a
  given field accepts a write. A write that lands nowhere can append a permanent stray property to
  the model that only a factory reset removes. So nothing reports success on the strength of having
  been sent.

## The accessibility decisions that shaped the UI

This is a blind user's app, and several of these are non-obvious enough to be worth copying:

- **Any dropdown that changes what you HEAR waits for an Apply button.** A closed `<select>` fires a
  change event on every arrow press, and arrowing through a closed select is exactly how a screen
  reader user reads one, so browsing a list WAS performing the action. A commit delay makes it worse,
  not better: it fires precisely when you pause to think. Dropdowns that only change what you are
  LOOKING at stay instant.
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
  `btDoScan` also sticks, yet scans on a Duo, so a flag that never self-clears is not proof of a
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
  (`DevTools/probe_unclaimed_props.py`, 1st October 2026). `eqParamModeHigh` / `Mid` / `Low` are in
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
- **`btDoScan` WAS ON THAT LIST AND IS NOT DEAD: IT SCANS.** Written True on a Duo it filled
  `btScan1` with `'<MAC> - <device name>'` for a real device in range, so Bluetooth discovery is
  reachable from software. The old entry read "no results on either block, phone in pairing mode",
  which is the weakest kind of negative: a scan that finds nothing is indistinguishable from a scan
  that did not run, because the answer depends on what happened to be in range. **A NEGATIVE THAT
  DEPENDS ON THE ENVIRONMENT DOES NOT BELONG ON THIS LIST AT ALL** - "the desk ignored the value" is
  a fact about the desk, "the desk found nothing" is a fact about the room.
  **THE DUO SCANS AND THE PRO 2 DOES NOT, AND THAT IS THE POINT OF THE ENTRY.** The Duo half is not
  in doubt: a dump taken minutes earlier has `btScan1` empty, so the write caused it. The Pro 2 half
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
  `rcp_transfer.send_record`, `rcp_transfer.send_records_paced` and `midi_out.cc`. A `test_*_live`
  suite opts back in automatically, because driving the desk is what it is for.
  **WHY IT RAISES RATHER THAN SWALLOWING THE WRITE:** a silently dropped write leaves the test
  passing while proving nothing. Earned on 21st September 2026, when a new check in
  `test_latched_pads` called the real `_hk_pad_toggle` and disengaged a voice-FX pad on Shaun's
  live desk twice - he heard the tone and the audio drop from another room, and the suite reported
  ALL CHECKS PASSED both times.
  **THE SECOND HALF IS THE BACKGROUND THREAD.** `_hk_pad_toggle` starts `_hk_pad_verify` on its own
  thread, which READS the desk, so stubbing only the write still left the interpreter exiting with a
  HID read in flight: every check printed ok and the process then segfaulted (exit 139). Stub the
  verify too. A suite that passes and then crashes is worse than one that fails.

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
  `systemHapticSetting` in RECBUTTON has a known origin: the `build_global_prop_int` argument-order
  mistake below, which turned a haptics write into selector 2. The `btVisible` copy was written up
  in `server.py` as possibly belonging to a sibling RODE product sharing this schema - reasonable,
  and wrong.
  **A STRAY THEREFORE COSTS MORE THAN A BAD READING: IT GROWS A THEORY.** Nobody doubts a property
  they can see in the dump, so the explanation gets written into the code and outlives the stray.
  When one is found, grep for anything written to explain it.
- **`build_global_prop_int(name, selector, value)` takes the SELECTOR SECOND**, unlike
  `build_global_prop_bool(name, on, selector)`. Getting them the wrong way round writes an arbitrary
  value on an arbitrary selector; on 29th August 2026 that turned a haptics write into selector 2,
  value 15. Use `rcp_transfer.set_unit_setting(prop, value)`, which looks the selector up by name and
  refuses anything not in `UNIT_SETTING_SEL`.
- **A selector past 0xFF needs NO special builder.** Every global builder passes it to
  `build_address` whole and `encode_path` widens the index, so `build_global_prop_bool(prop, on,
  258)` addresses root child 258. `build_channel_prop_int/bool(prop, value, lo, hi)` splits it by
  hand instead, which is why `SEL_SHOWCONTROL` and `SEL_RECORDINGS` go through those; either shape
  reaches the same node.
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
- **Read back the first write on any NEW string property.** `build_global_prop_string` combines two
  separately-proven halves, and the combination itself is only proven per property.
- **`wifiPSK` is READABLE off the desk.** Keep it out of the API, the browser, profiles and log
  lines. `wifi_join.py` takes it on stdin, never argv, and its read-back reports only "a password is
  stored".
- **`rcp_transfer.py` with NO argument defaults to `enter`** (enters transfer mode and remounts pad
  storage), not the selftest. Use `rcp_transfer.py selftest`; clean up with `exit`.
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
  is stated per entry, because membership genuinely differs (CallMe 1 takes a fader and has a
  channel strip, but no matrix row yet). `test_specs.py` asserts the surfaces agree and that the grid
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
  every reopen, as `_announce_loop` and `recorder.Watcher` both do. Caching it is what made the
  announcer go silent for good after a USB setting was changed in the app itself: the read failed,
  the reopen was tried on the old path for ever, and to a screen-reader user that is
  indistinguishable from the app having died. Reconnection now takes about 3 seconds and says
  "Control announcer reconnected", because silence is the one thing that cannot be interpreted.
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
- **THE ANNOUNCER SAYS WHEN THE USB MODE MOVED WHILE IT WAS DEAF, AND A PUSH CANNOT DO THIS JOB.**
  The desk can leave Expanded unprompted (measured: off the bus with nothing written to it for
  nine minutes, back two seconds later in Standard with the setting reset), and it changes as it
  leaves the bus, so any push announcing it is sent into the gap. `_ann_seed_layout` therefore
  records `outputMultiMode` and the reconnect compares. It names the half that MOVED rather than
  reciting both, and Expanded going away is the consequential one: the operator's Game, Music, A
  and B inputs go dead and every RODECaster device on the computer is renamed.
- **THE THIRD STORAGEVOLUME IS THE INTERNAL PAD STORAGE, AND ERASE MUST NEVER REACH IT.** Eject and
  erase are per-volume switches; `_storage_target` takes the volume from `recorder.read_volumes` on
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
  a user-mode service, `RODECasterVADService`. **The service binds on SOME arrivals of the desk and
  never later**: twelve arrivals in one afternoon, six bound (five of the first six, one of the
  last six), in every mode, with our announcer running and with nothing of ours open, and a desk
  left unbound stays so (five quiet minutes, measured). When it binds it does so within three
  seconds, twelve on one occasion, and creates its endpoints within three more. **Nothing writable
  from user mode re-runs the bind**: not its Enable switch (re-creates `System` only), not its
  command block, not a service restart, not stopping our HID handle; a PnP restart of the desk is
  vetoed by `usbaudio2` while Windows has streams open, which is the normal state of a default
  sound device. What does re-run it is another arrival, so `server._virtual_devices_follow` makes
  one itself, flipping the output mode off and back up to three times, and then reports the state
  and asks for NOTHING. **Never put a replug back in that sentence**: it is one more arrival at
  whatever the odds currently are, in a bad spell it binds no better than the retries just did
  (measured at post-fader and at mix only), and phrasing it as the remedy tells the operator to go
  and pull a cable that mostly will not help. The "only a replug brings it back" rule this
  replaced was a description of the same coin toss. The one recovery seen for a bad spell is a service restart FOLLOWED
  by an arrival, which bound one second after the desk returned; the restart alone, with the desk
  present, did not. One sample, and it needs elevation, so it is not built.
  `virtual_devices.py` reads the service's own status block (shared memory
  `Global\RODECaster_VAD{...}`, readable by anyone: connected flag, Enable, heartbeat, desk,
  firmware), which is the detector, and `reconcile` makes Windows agree with it. The follow-up
  runs after every USB mode write, on every announcer reconnect (the desk re-enumerates for its
  own reasons too) and at startup, and speaks the outcome.
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
  across four arrivals (`DevTools/bind_timeline.py`), the desk's KSCATEGORY_AUDIO, CAPTURE, RENDER
  and TOPOLOGY interfaces and its HID interface go ACTIVE within 0.15 s of each other, and the
  audio ones sometimes go first. There is no window in which HID is up and audio is not.
- **THE SERVICE'S OWN TRACE IS COMPILED OUT, SO DEBUGVIEW WILL NEVER SHOW A BIND.** The binary
  carries the whole trace string table (`%s Enter`, `%s Leave, returning %#08x`, `Found Device:
  %s`) and imports `OutputDebugStringW`, which reads as a way in. It emits nothing: a global
  DBWIN listener held across four arrivals caught 49,507 lines from `audiodg` and 114 from
  `svchost`, both session 0, and zero from the service. Its registry key holds only `Version`,
  `Path`, `Enable` and `Latency`, so there is no flag to switch tracing on either.
  `DevTools/vad_trace.py` is the listener, kept because it proves the negative in one run.
- **A BAD SPELL OUTLASTS THE FOLLOW-UP'S THREE RETRIES, AND THE ANNOUNCER'S RECONNECT IS WHAT
  STARTS THE CYCLE THAT WINS.** Measured through one: nine arrivals at product id 0x0094 produced
  two binds, and each came after a cycle had already run out of retries and reported failure.
  Nothing the first cycle did recovered it; the next follow-up, started by the announcer's own
  reconnect, did. So the failure is reported while the app is still capable of fixing it itself,
  which is the other reason that sentence must not ask the operator to do anything.
- **WINDOWS' DEFAULT OUTPUT SITTING ON THE DRIVER'S OWN ENDPOINT DOES NOT AFFECT THE BIND.** It is
  a good hypothesis, because `System (RØDECaster)` as the default means the audio engine holds a
  render stream on the endpoint the service is rebuilding. Measured against its own matched
  control on the same afternoon, six mode flips each way: six binds out of six with the default
  moved to the Realtek speakers, and six out of six with it left on the driver, every one within
  1.0 to 1.2 seconds. `DevTools/probe_bind_default_output.py --control` is the control, and it
  earns its own flag because the first run alone looked like a cure: the rate had been two binds
  in nine arrivals an hour earlier. **A GOOD SPELL WILL VALIDATE ANY CHANGE YOU MAKE DURING ONE.**
  Nothing about this bind may be claimed from a run without a control beside it.
- **THE BIND IS NOT A PER-ARRIVAL COIN TOSS, SO THERE IS A FACTOR NOBODY HAS NAMED.** Measured in
  one afternoon: 1 bind in 8 arrivals, then 18 in 18 across three runs of identical flips. No
  fixed probability produces both, so the failures cluster on something that persists between
  arrivals rather than being drawn fresh each time. The service process is the obvious place for
  that state to live, and it is never restarted; a service restart FOLLOWED by an arrival is the
  one recovery ever seen for a run of failures, which fits. **Do not write "it binds about half
  the time" as though it were a property of an arrival.**
- **THE DIAGNOSTIC TOOLS ARE NOT THE CAUSE, and it was worth asking.** Every failure of that
  afternoon fell inside a window where `bind_timeline.py` or `vad_trace.py` was running, which
  reads as an observer effect: the first enumerates five device-interface classes eight times a
  second, and the second throttles `audiodg` by making it wait on a Python reader for 330 debug
  lines a second. The test is to run the flips WITH the observer on purpose, and that gives 6
  binds out of 6. The correlation was with the time of day, not the instrument.
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
  prefers `System (RØDECaster)` while the driver is connected, then the desk's Main output, and
  moves the console and multimedia roles together, leaving communications alone. A default that is
  alive and elsewhere was chosen by the user and stays.
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
  like an Alt-Tab fault because the page's status read sent both on every load and reload.
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
  that string, and it is the whole of what makes the download safe to run.
- **THE DRIVER DOWNLOAD IS THE APP'S ONLY OUTBOUND INTERNET CALL, AND KEEPING IT SO IS A FEATURE.**
  Everything else talks to localhost and the USB cable. It runs only from the button press: never
  at startup, never on a timer, never a version check. `test_driver_install.py` asserts the claim
  rather than trusting it, by scanning `Project/*.py` for a non-localhost `urlopen`; `server.py`'s
  ping of its own `/api/announce` is the one hit it is right to ignore. Said plainly in the README,
  in Help and on the control itself, because somebody who chose this app partly for what it does
  not do deserves to be told where the exception is.
- **CHECK THE SIGNATURE, NEVER A HASH.** The URL carries no version and RODE replace the file in
  place, so a pinned hash refuses every release they ship, which trains people to skip the check.
- **PASS A PATH TO POWERSHELL BY ENVIRONMENT VARIABLE, NOT AS AN ARGUMENT.**
  `powershell -Command "... $args[0] ..." -args <path>` does NOT populate `$args`: the signature
  check ran against an empty path, reported its status as "-args", and refused RODE's genuine MSI.
  An env var cannot be mis-parsed and cannot be injected into either.
- **THE DRIVER'S STATE IS IN THE LIVE PAYLOAD, NOT THE STRUCTURE ONE, because it is read from
  Windows rather than from the desk.** So a control whose EXISTENCE is decided from `usb_driver` in
  `loadRouting`'s structure pass is never created at all. Build such controls with the rest and let
  the live pass label and show them, which is what every other control on the page does. The same
  goes for anything else Windows-side.
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
  lost 4 values to exactly that.
- **Derivation fires on the MACRO WRITE, not on entering simple mode.** A strip can sit in simple
  mode holding a chain its own macros would not produce.
- **The preset marker is a NAME only.** Any parameter write blanks it to -1, and there is no HTTP
  marker writer, so a settings-only profile is the only way to put it back.
- **`channelBypassProcessing` is deliberately NOT in a preset.** The channel's master on/off is the
  operator's control. Asserted both ways by a test. RODE's own app behaves the same.
- **`faderLevel` is addressed per STRIP.** Written before the layout it lands on whatever channel was
  there before, so restore writes levels straight AFTER the layout.
- **`profiles._apply_v2` restores THE FADER LAYOUT FIRST**, before channel processing. It used to be
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
  voicing's own 0.59375. `profiles._apply_v2` waits for the strip to settle after a type that
  CHANGES. On a strip in SIMPLE mode it is worse than a straight overwrite: the desk derives EQ,
  compressor, gate and de-esser from the three VoxLab knobs, so values written mid-apply come back as
  neither voicing's - 8 of 13 did, at values belonging to nothing. **Restore a chain with
  `apply_processing_preset`, never a loop of per-param writes**: it puts the MODE on first, which is
  what stops the derivation regenerating everything behind you.
- **CHOOSING A MIC VOICING SETS THAT VOICING'S OWN DEFAULT GAIN**, so `profiles._apply_v2` writes the
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
  same `_ann_publish_mute` / `_ann_publish_solo` as the Channels page, which is what stops the two
  pages disagreeing.
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
- **THE DESK UNMUTES WHAT IT REARRANGES.** Linking clears `channelOutputMute` on both inputs and
  unlinking clears it on the mic it hands back; the RODE app does not restore it either. `_carry_mute`
  does, and it only ever RE-mutes. Muting something the user has open is the same surprise in
  reverse, which is why `profiles.UNIT_NEVER_RESTORE` refuses mutes outright; restoring what our own
  write disturbed is a different thing, but only in that one direction.
- **A CROSS-POINT SELECTOR LEAVES BYTE RANGE AT SOURCE 14.** It is
  `CROSS_POINT_BASE + 13*source + output`, so Game (12) is 232 and Music (13) is 245 but Virtual A
  (14) is 258 and Virtual B (15) is 271, and the desk addresses those two with a TWO-byte index.
  Read as one byte both come back as 1, below the base, so the push is silently discarded and the
  fader moves with no level and no name - while mute and listen, addressed by strip index, still
  speak, which is the signature that tells the two apart. Take the address as a whole path element
  from `rcp_read.parse_push`, never as a byte. Only codes 14 and 15 reach that far, and only in
  Expanded, which is why this was invisible until a virtual channel was first put on a fader.
- **THE WRITE SIDE WIDENS THE SAME WAY, AND A GLOBAL SELECTOR IS AN INDEX RATHER THAN A BYTE.**
  `build_global_prop_int` / `_bool` / `_string` / `_float`, `build_mix_link_request` and
  `build_cross_point_level` hand the selector to `build_address` whole, so `encode_path` emits
  `01 0f` for 15 and `02 02 01` for 258 - the same two-byte form the desk pushes. Masking to 0xFF
  truncated SILENTLY, which is worse than raising: 258 became 2, a valid node 256 cross-points
  away. Proven on the Pro II for both Virtual A (258) and Virtual B (271): `mixDisabled` written,
  read back, restored, and the model's node, property and MIX counts unchanged, so no stray
  (`DevTools/test_cross_point_wide_live.py`, 1st October 2026). The pad builders keep their mask
  because a pad index cannot reach 256, and `SEL_SHOWCONTROL` / `SEL_RECORDINGS` are unaffected:
  they split the index by hand through `build_channel_prop_*` and always did.
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
  (`describe(scheme, expanded=...)`): the stored scheme always holds 13 rows, and saying "1 of 13"
  to somebody looking at 9 would name rows they cannot see.
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
  the monitor outputs only and recorded levels for excluded cells. `output_modes`,
  `matrix_excluded` and `monitor_unlinked` are WRITE-FOR-OLD-BUILDS + read-for-old-profiles only;
  nothing new should be added to them. **They also keep the NARROW source set while `mix_scheme`
  does not**, because v2.00 masks a cross-point selector to one byte and cannot address source 14
  or 15 at all: handing it those rows would be a restore reporting failures for cells it could
  never write.

## Channel properties

- **Channel processing is PER FADER STRIP.** Slot N processes whatever sits on strip N. Resolve the
  strip from the LIVE fader vector (`channels._strip_index`), never from a pinned per-channel index.
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
  exactly the nodes `channelOutputMute` and `channelCueEnable` sit on (`proc_sel_base + strip`, ten
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
  (a mute moves nodes 284 to 296), but the Routing page has no row for it yet.
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
  gets `None` for the whole chain) and `app.js renderChannelPanel` (a bus gets one tab). Pinned by
  `ui_checks_padsbus.js`.
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

- **"RESTORE THE FACTORY VOICE EFFECTS" REBUILDS EIGHT PADS, NOT SIX, AND IT IS DESTRUCTIVE.**
  `soundpad.restore_factory_fx_pads` clears and recreates grid positions **9 to 16**: the six voice
  effects (9, 10, 11, 13, 14, 15) plus Censor (12) and Ducking (16). All eight qualify because they
  rebuild from tables in `soundpad.py` with no WAV to find, which is what separates them from the
  eight factory SOUNDS. Nothing at grid 17 or above is touched.
  **THE BUTTON'S NAME IS WHY THIS GETS WRITTEN DOWN AS SIX.** Any UI or help copy describing it has
  to say eight and has to warn that a user's own pad on 12 or 16 is destroyed. `help.html` claimed
  "those six pads and touches nothing else" while its own Pads section correctly said eight.
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
  Scene A (seen 4th September 2026 at the end of a factory-FX rebuild), and `_hk_pad_verify` would
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
- **A voice-FX pad EDITED IN PLACE restores at the recipe's values, not the edited ones.**
- **Keep the pool clean, and remove each kind of orphan only its own way** (`SOUNDPADS.md`
  section 5). An ATTACHED orphan (effectsIdx stored last) goes by bare bind + clear; a delete by
  tree path does not touch it. A DETACHED one (effectsIdx stored first, left by RODE's app) goes only
  by the path delete, and ONLY when it is the last object: a middle delete left the next FX pad's
  clear collecting the wrong thing. `remove_orphan_objects` stops on a pass that removes nothing;
  never turn that into a retry loop.
- **`find_pad_volume` MUST READ BYTES before returning a root, not just stat it.** `isdir` and
  `disk_usage > 0` were each added after a failure of this class and the volume passed BOTH and then
  died on the first real read with EINVAL (29th August 2026, the as-found baseline, pad 1). The
  readiness test has to do the SAME KIND of I/O as the use it gates, or it is not a test. Do not
  "optimise" `pad_volume_readable` out of the wait loop: a not-ready volume returning False is what
  keeps the remount loop going, and this guards backup, restore AND create.
- **Wrap pad-volume I/O in `rcp_transfer.quiet_drive_errors`.** A half-mounted removable volume makes
  Windows raise a MODAL "There is a problem with this drive" box on our behalf, for a condition we
  already retry and already report better. Shaun has to find and dismiss it with a screen reader.
  Thread-scoped on purpose: never swap it for process-wide `SetErrorMode`.

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
- **CallMe has its own Watcher on its own handle, not a branch of the announcer**, because the
  announcer thread stops when announcements are off and a call still has to be tracked and
  answerable. Its speech goes through `_ann_say`, which is where the switch is enforced.
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
- **The call id comes from the `sipIncomingCallDetails` push, never from a counter.** It rises by one
  per call today, but nothing promises that.
- **Both calls that went Active had CallMe 1 (source code 16) on a fader**, so whether a call
  connects with no CallMe fader is unmeasured. The TRUE-bool failure happened either way, so the
  fader is not what decided that.
- **Every off/on of `sipCallHostingEnabled` reissues `sipRodeCode`**, so a guest link shared before
  it stops working. Do not toggle hosting as a way of refreshing state.
- **`sipSlotCallState` is a bare "Idle" until a slot has carried a call, JSON after.** Parse both.
- **Strings in `sipIncomingCallDetails` are UTF-8 and the model decoder reads latin-1**, so a default
  guest name arrives as mojibake unless re-decoded.

## The desk pushes, and does not echo

- **The desk pushes changes made ON the desk and does NOT echo our own writes.** Two consequences,
  and both have bitten more than once:
  - Any announcer state that compares a pushed value against memory must be SEEDED at startup, or
    the desk's first resting push reads as a change (the phantom "Levels" and "Bank 1").
  - Any cache the announcer keeps must be told about OUR writes explicitly
    (`_ann_publish_level`, `_ann_publish_mute`).
- **The exception, proven 28th August 2026: it DOES push our own property writes back on report 04.**
  That is why the announcer needs the 1.5 s echo-suppression window per target. The two facts sit at
  different layers; do not collapse them.
- **LISTEN FOR THE ANSWER, DO NOT RE-READ THE TREE UNTIL IT APPEARS.** `rcp_read.PushListener`: own
  read-only handle, no HID lock, dump frames filtered by `_push_record_len`. A factory reset pushes
  `currentShowUUID` itself at about t+1.75 s. **OPEN IT BEFORE THE WRITE** - the burst starts 0.02 s
  after, so a listener started afterwards loses the race.
- **"IT WORKED" AND "IT HAS FINISHED" ARE DIFFERENT QUESTIONS.** The reset's uuid arrives at t+1.75 s
  and the desk keeps rewriting itself until t+4.96 s, so acting on the answer means writing to a desk
  mid-rebuild. Use `wait_until_quiet()` before the next write, as `_reset_and_wait` does before
  `wake_channels`. The old polling was slow enough to hide this.
- **AN IMPOSSIBLE EXPORT CAN ONLY BE SEEN BY LISTENING.** With no card or drive in, the desk pushes
  three frames and moves NOTHING in the tree, and `showControlLastError` LATCHES at 8 (a reset does
  not clear it). So no read can tell a refused export from one that never happened.
- **A hidapi HANDLE MUST BE CLOSED BY THE THREAD THAT READS IT.** Closing one from elsewhere while a
  read is in flight segfaults the interpreter (exit 139). `recorder.Watcher`, the announcer and
  `PushListener` all close on their own thread and are joined.
- **`channels.last_known_strip_vector` never expires and is for NAMING only.** The TTL'd cache is for
  addressing a WRITE, where staleness is the dangerous case. Do not merge them.
- **A CLEAN PROFILE RESTORE IS A SHOW OPERATION, so anything true of shows is true of it.**
  `restore_profile(clean=True)` sends `shows.send_new_from_default()` (the software factory reset)
  and takes an export first as the undo. Two consequences people keep missing: the profile and show
  features are NOT independent, so "we could drop shows" does not remove the reset or its risks; and
  every warning that belongs on a reset belongs on the clean restore too. This is also why the dead
  mic was a CLEAN-RESTORE bug and not a Shows-page bug. A MERGE restore touches none of it.
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
  announcer-SUSPENSION decisions stay keyed on the new state, correctly: a take is over once the desk
  leaves RECORDING or PAUSED, whatever the reason.

## Reading the model dump

- **A DUMP ENDS AT THE LENGTH IT DECLARES, NOT WHEN THE LINE GOES QUIET, AND PUSH FRAMES ARE
  DROPPED. Both halves of `rcp_read.read_model_raw` are load-bearing, and removing either brings
  back a fault that was live for two months.** Quiet detection alone could not tell the dump from an
  asynchronous push, because both are report-04 frames, so a playing pad (which pushes `padProgress`
  about 20 times a second) meant the line never went quiet: the read ran to its 4000-read ceiling,
  240 s worst case and 172 s measured, AND every push frame was appended, so the blob came back
  contaminated as well as late (264180 and 139485 bytes against a 136170 baseline, both mistaken at
  the time for "an unstable read"). It bit far wider than the pads page, because every per-channel
  write resolves its strip through `channels.strip_vector`, which dumps when its cache has expired.
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
  The announcer, the recorder and CallMe watchers and `PushListener` all receive the model dump's
  frames too, because Windows gives every open handle every input report. A name scan finds
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
- **EVERY INPUT VALUE, ON EVERY DESK, LIVES AT `INPUTSOURCE[source_code]` = path `0x26E + code`.**
  Mic gain, phantom, polarity, mic type, instrument gain, line level, input type and strip colour.
  Read it with `channels.input_value`, write it at `DeviceModel.input_path`, derive nothing else.
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
  switched `inputPower` on by itself and retuned the strip. `DevTools/duo_gain_probe.py` is that
  differential, and the technique generalises to any property the desk derives something from.
- **BUT THE ENGINE'S MODEL IS DEVICE-AGNOSTIC, SO "THE ENGINE ANSWERED" PROVES AN ADDRESS IS LIVE AND
  NEVER THAT A FEATURE EXISTS ON THIS HARDWARE.** Both desks run ONE binary (`app.zip`, "rcp2") and
  publish the same 30-slot `INPUTSOURCE` table and the same 30 matrix rows, so the engine acts on a
  source the desk does not physically have. Measured 2nd September 2026: on a Duo, source 3 is MIC 4,
  which it has no jack for, and it accepts a fader placement, fills all thirteen of its routing
  cross-points with real levels, and answers a mic-type write by moving `inputType` and
  `inputInstrumentGain` EXACTLY as the real Mic 2 does. What is device-specific lives at
  `SYSTEM.boardType` (`RcBoardConfig::HostType`, 1 on a Duo) and in the touchscreen, neither of which
  the property tree exposes. **So run the CONTROL**: make the same write to a slot the desk cannot
  possibly have, and if it behaves the same, the test measured nothing. This retired two successive
  verdicts of `DevTools/duo_stereo_probe.py` before the third stood up.
- **THE FIRMWARE IS THE AUTHORITY THE MODEL IS NOT, and it is public.** `update.rode.com/manifest.json`
  names `rcp2/<version>/app.zip`: 36 MB, the two Linux binaries, md5 published beside it and verified.
  `rc_audio_mixer` is the engine, `rc_gui_juce` the touchscreen. Its string table gives up the
  `InputSourceType` label list ("Input 1", "Input 2", "Headset", ... "1 + 2", "2 + 3", "3 + 4") and
  class names like `InputAssignSourcePage::LinkButton`, which is how the stereo-pair codes were shown
  to be firmware-wide rather than per-desk. Embedded Linux (Amlogic); the touchscreen is JUCE, with
  a 4787-entry UI string table. RODE's, so it stays OUTSIDE the repo; `DevTools/firmware_dig.py`
  expects it at `C:\Users\shaun\rode-firmware`. **Look here before calling anything blocked.**
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

- **THE EXPORT IS AN ALLOW-LIST, NEVER A DUMP OF settings.json, AND THE REASON IS A CREDENTIAL.**
  `remote_token` grants remote access to this app and lives in the same file as the keyboard
  shortcuts, so a dump would put it in a file people email to themselves. `app_settings.EXCLUDED`
  holds it and two others, each with its reason at the site: `baseline_asked` (carrying it would
  silently suppress a desk's one-and-only first-run backup offer on a new machine) and
  `applied_presets` (it describes one desk, not a preference). **Adding a new preference means
  adding it to PORTABLE, MACHINE_SPECIFIC or EXCLUDED** - `test_app_settings.py` fails if a key the
  app writes is in none of them, so a new setting cannot quietly become unbackupable.
- **IMPORT MERGES, IT NEVER REPLACES.** The excluded keys live in the same file, so a replace would
  delete this machine's remote-access token and its first-run record as a side effect of restoring
  somebody's shortcuts.
- **`hotkeys` IS `{"enabled": ..., "shortcuts": [...]}`, NOT A MAPPING OF SHORTCUTS.** `len()` on it
  returns 2 for ever, whatever the user has, which is exactly what shipped for an hour on 2nd
  September 2026 and reported "2 keyboard shortcuts" for a machine with six. Count through
  `app_settings.count_shortcuts`. The first version of the test invented its own fixture shape and
  so passed while the counting was broken: **a fixture that is not the real shape tests nothing.**
- **AN EMPTY "what this machine has" LIST MEANS "CANNOT TELL", NOT "HAS NONE".** SAPI enumeration
  fails over RDP, so the voice list comes back empty there; refusing to restore a voice on that
  basis would silently drop the setting. `merge_import` only rejects a name when it has a real list
  to check it against.

## Web UI and screen reader

- **A STAGED SELECT'S OPTION LIST MUST SHIP IN THE STRUCTURE PAYLOAD, NOT THE LIVE ONE.** The
  controls are built once from `/api/routing`; the live read only sets values. Put the list only in
  the live payload and the control is built EMPTY, and nothing says so: `setStagedValue` falls back
  to inventing one option labelled `Value <device value>`, so it renders and reads as a real
  control that can never be changed away from, with its Apply permanently unavailable. `"What this
  computer sends"` shipped like that and Expanded could not be turned off from the page.
  `ui_checks_staged.js` now FAILS a staged select with one option rather than skipping it; the skip
  was reported as a pass, which is how the dead control stayed green.

- **`_ann_say` IS WHERE THE ANNOUNCER SWITCH IS ENFORCED, because it is the only place every phrase
  passes through.** `_announce_on` gates the read loop, and roughly half this app's speech comes
  from elsewhere: the virtual-devices follow-up after a USB mode change, the recorder watcher. Gate
  it anywhere else and "announcements off" leaves all of that talking. The phrases announcing speech
  is BACK are safe because each sets the flag before it speaks, so no force path is needed. A
  suppressed phrase is still logged, marked: "it said nothing" and "it was never asked to" are
  different faults.

- **THE PAGE IS CALLED "Backups" AND ITS ROUTE KEY IS `profiles`, DELIBERATELY.** The key, the
  section id `sec-profiles` and `/api/profiles` all keep the old word so a remembered tab position
  and the API path do not break. So `profiles` in code is right and "Profiles page" in USER-FACING
  COPY is wrong: it names a page nobody can find. Four such strings survived the rename, in
  `help.html` and in a dialog in `index.html`. The same trap applies to "Shows": the feature is
  `shows` in code, and the user sees backups in one list.
- **`FaderSpec.count` IS 9 ON BOTH DESKS; ONLY `physical` DIFFERS (Pro II 6, Duo 4).** So the Pro II
  has three virtual faders and the **Duo has five**, and copy that says three for the Duo is wrong.
  The Duo's fourth assignable source is Headset, which sits on a virtual fader and is the one
  usually left out of the list. Its ninth fader ships EMPTY (`default_vector` index 8 is -1), and
  the tenth VECTOR SLOT is not a fader at all, which is a separate fact: PHYSICALINTERFACE carries
  four POT nodes then exactly NINE FADER nodes, so one of the ten slots has none.
  Labels and the encoder ring do not shift.
- **INDEX.HTML IS ASSEMBLED, NOT SERVED.** `Project/page.py` splices `static/help.html` in at the
  `<!-- INCLUDE help.html -->` marker, and BOTH servers go through it: `server.py` and
  `DevTools/ui_probe.py`. Read the file straight off disk in a new place and that page loses its
  whole Help & About section, which the UI checks then report as a fault in the markup. It is
  spliced server-side rather than fetched in the browser because the router focuses that page's h1
  and `buildStaticTabs` reads its panes at startup; a fetch races both, and the failure is a screen
  reader landing on nothing.

- **THE HELP GUIDE DRIFTS, AND IT DRIFTS SILENTLY.** `help.html` describes every page and control,
  and nothing fails when a control changes underneath it. Five false claims were found in one spot
  check on 20th September 2026, the oldest dating from a focus change four days earlier. When you
  change a control, a default, a tab or where focus lands, grep `help.html` for it in the SAME edit.
  To settle a claim about what is on screen, render it: `ui_probe.py` with a throwaway checks file
  that dumps the real labels beats reading the code that builds them.

- **THERE IS ONE TAB STRIP AND IT IS `wireTabStrip`.** `buildTabs` (panes built in JS) and
  `buildStaticTabs` (panes found in index.html) are thin adapters over it and differ ONLY in where
  the panes come from. They were duplicated line for line until 30th August 2026, accessibility
  comment and all. Do not "fix" one of them: it is the widget Shaun moves through most, every rule
  in it (roving tabindex, arrow/Home/End, focus the tab BEFORE hiding the pane focus sits in, the
  panel's own tab stop) was found by a screen-reader pass, and a change applied to one copy would
  silently not reach the other.

- **DO NOT MARK CAPABILITY-GATED SHORTCUTS AS UNAVAILABLE IN THE CATALOGUE** (Shaun's ruling, 2nd
  September 2026, rejecting exactly that proposal). The shortcut catalogue IS filtered by what a desk
  HAS - Channels drops from 32 entries on a Pro II to 27 on a Duo - but NOT by what it can DO, so a
  desk lacking a capability still lists its shortcuts. Pressing one explains itself clearly, so it
  is not silent, only late. The reasoning: a capability gate is TEMPORARY, and building UI around a
  gap with a known expiry date is work that has to be rolled back later. Do not re-propose it.
- **A DROPDOWN THAT CHANGES THE DESK'S AUDIO MUST STAGE, AND WAIT FOR AN APPLY.** Shaun's rule, 30th
  August 2026: "where a dropdown changes something that affects audio is where an apply button is
  needed. The pads selector dropdown shows you the info on the pad so instant is fine." A CLOSED
  select fires `change` on EVERY arrow press, and arrowing a closed select is how a screen reader
  user reads one, so browsing the list WAS the action. A delay does not fix it: it fires when you
  pause to listen. Use `makeStagedSelect` / `setStagedValue` / `markStaged` / `blockStaged`; never
  write from a select's own `change`. A dropdown that only changes WHAT YOU ARE LOOKING AT (which
  pad, which output, which shortcut) stays instant. Guarded by `DevTools/ui_checks_staged.js`, which
  must be run with BOTH `--route routing` and `--route channels`.
  **THREE DROPDOWNS ARE DELIBERATELY LEFT INSTANT and it is not an oversight:** pad play mode and
  mixer trigger (nothing is audible until the pad is pressed) and pad colour (cosmetic). Do not
  "finish the job" by converting them.
- **`setStagedValue` must reach its Apply button through `sel.stagedApply`, not `getElementById`.**
  Some callers set the device value at BUILD time, before the row is in the document, and the id
  lookup then returns null - which left mic type's Apply armed with nothing staged.
- **`aria-describedby` can name SEVERAL ids, and two staged dropdowns rely on it** (the USB mode's
  warning plus its staged hint, and the multitrack mode's explanation plus its own). Setting the
  attribute after `makeStagedSelect` OVERWRITES the hint: append, never assign. A checks helper that
  reads only the first id will silently miss the other.
- **A STAGED APPLY MUST RE-RUN WHATEVER DEPENDS ON THE VALUE IT JUST SET.** Staging split the
  moment a value changes from the moment it is written, and anything gated on that value has to be
  re-evaluated at the WRITE, not at the next page read. The raw-tracks switch is available only
  while a multitrack mode is chosen; its Apply did not re-run that decision, so choosing pre-fader
  and applying it left the switch unavailable and still saying "choose pre-fader or post-fader
  above first". Only leaving the page and coming back cleared it (Shaun, 30th August 2026). Fix
  shape: update the cached payload (`recordingModel`) and call the dependent applier, rather than
  re-reading the desk for a value you just set. Guarded in `ui_checks_recording.js`.
- **USER-FACING TEXT SAYS "RODECaster Pro 2", NEVER "Pro II"** (Shaun, 30th August 2026). A screen
  reader speaks the roman numeral as "Pro aye aye". RODE's own branding is "Pro II" and the instinct
  to "correct" it back is strong - it was done once already, in this very rule's session - so:
  **anything spoken or displayed uses the numeral 2.** That includes `devices.PRO2.name`, which
  feeds the status line, the window title and every message that interpolates the device name.
  **THREE PLACES KEEP "Pro II" DELIBERATELY:** Windows' own audio ENDPOINT names, which we do not
  own and must match exactly (`meters.friendly_stream_name` strips the model prefix, so none of it
  is ever spoken); code comments and docstrings, which describe RODE's product; and
  `RODECASTER_PROTOCOL.txt`. Nothing keys off the display name - per-device preferences use the
  hardware id - so changing it broke no stored data, and a profile saved earlier just carries the
  old stamp.
- **A BACKUP IS ABOUT A SECOND A PAD; A RESTORE IS ABOUT FIFTEEN. Never quote one figure for the
  other.** `PAD_BACKUP_SECONDS = 1` (a backup COPIES files off the mounted pad volume with
  `shutil`), `PAD_RESTORE_SECONDS = 15` (a restore pushes each sound back over transfer mode). Both
  are in server.py and both are published, so UI copy must read the right one. Quoting the restore
  figure on the backup path calls a full grid "a job of many minutes", wrong by an order of
  magnitude, and makes people skip the pads.
  **THERE IS NO SETTINGS-BACKUP FIGURE AT ALL, and `SETTINGS_RESTORE_SECONDS = 25` is not it.** The
  backup endpoint publishes `seconds_per_pad` and nothing else. `help.html` quoted the 25 on the
  backup path, so this trap has now been sprung in two different files: check BOTH the dialog copy
  in `index.html`/`app.js` AND the help text whenever either number moves.
  **ALSO UNMEASURED: how long the unit's OWN backup takes.** `action_backup_now` is proven to work
  but has never been timed, so do not put a number on it. The only measured neighbour is the unit's
  own RESTORE at a 6 second median.
- **THE HELP & ABOUT VERSION COMES FROM `/api/version`, NOT `/api/status`.** `loadVersion()` runs
  from `init()` because /api/status is a USB enumeration only the Device page takes, so anyone who
  went straight to Help & About read "Version unknown". Keep /api/version free of device access:
  `ui_checks_about.js` asserts that opening a tab there reads NOTHING.
- **OPENING THE APP PUTS FOCUS IN THE NAV, A ROUTE CHANGE PUTS IT ON THE PAGE'S H1** (Shaun,
  16th September 2026). `render(route, focusTo)` takes that choice as an argument; only `init` passes
  `"nav"`, and it focuses the link carrying `aria-current="page"`, so you hear where you are and what
  else there is before the page itself. Do not "tidy" it into focusing the heading everywhere: the
  two cases want different things, because a route change has a new page to announce and an opening
  window does not.
- **THE APP OPENS ON CHANNELS, AND THE DESK'S STATE IS SPOKEN RATHER THAN NAVIGATED TO** (Shaun,
  16th September 2026). It used to open on Device purely so that page's entry read would say what
  the desk was doing, which put the opening page at nav item 6. `speakStartupSummary` now does that
  independently, so `DEFAULT_ROUTE` is "channels". Do not move it back: the two facts are separable
  and were only ever tangled by accident.
- **THE STARTUP SUMMARY NAMES WHAT IT CHECKED, not only what is wrong.** Told "Muted: Mic 3, Mic 4"
  and nothing else, you cannot tell a clean solo bus from a summary that stops at the first problem,
  which is exactly what Shaun asked on hearing it. So solo and routing say so when they are clear,
  and the pad bank is always stated (no bank is wrong, but it changes what every pad does and has no
  other outward sign). It runs ~1.2 s after load: it costs a full model dump and takes the HID lock,
  so the opening page gets the device first, and the gap keeps it off the connection line in the
  live region.
- **PROSE ABOVE A TAB STRIP SHOULD HAVE ITS OWN HEADING.** H-key navigation goes from the page's h1
  to the selected pane's h2, and Tab goes from the h1 to the strip, because prose is not focusable,
  so an intro sitting between them is reachable only by reading through. The Help & About intro got
  an h2 for that reason on 30th August 2026.
  **IT WAS NOT WHY THE TEXT APPEARED MISSING THAT DAY, and the first version of this entry said it
  was.** Shaun reported not seeing the intro; the actual cause was a STALE BROWSER PAGE, which he
  found himself. Reading line by line reaches unheaded prose perfectly well, so the diagnosis never
  fitted the symptom. **Check the page is current BEFORE theorising about markup**: the version line
  on Help & About names the build, so "Version unknown" with no developer credit means the window
  predates today's static files. A static change needs a window reload, a server change needs the
  service restarted - and both were hit within an hour on the 30th, once by each of us.
- **REQUESTS FROM THIS MACHINE MUST STAY EXEMPT FROM THE ACCESS TOKEN.** `_authorised()` passes
  anything `_is_local()` accepts, and that is deliberate: code already running here can drive the
  desk anyway, so a token would protect nothing while breaking every DevTools script, every
  `ui_probe` suite (the probe proxies to 8770 from localhost) and the tray's own browser launch.
  "Tighten it so even localhost needs the token" is the change that looks responsible and is not.
- **THE TOKEN ROUTES ARE LOCAL ONLY, and that is not the same as the gate.** `/api/access` and
  `/api/access/new` are in `LOCAL_ONLY_ROUTES`, so a REMOTE page that is legitimately signed in
  still cannot read or replace the key that let it in. The guard now runs in `do_GET` as well as
  `do_POST`; it was POST-only before this, which would have handed the token to any signed-in
  phone that asked.
- **THE COOKIE IS SET ON THE PAGE LOAD AND NOWHERE ELSE.** That is what keeps `app.js` ignorant of
  the token: the page arrives once with `?t=...`, and every later fetch carries the cookie. Do not
  add token handling to individual fetches; if a new entry point serves the page, it needs the same
  `Set-Cookie`.
- **DO NOT WRAP A TAB STRIP IN A LANDMARK. Tried and rejected 30th August 2026.** A `<nav>` round
  the strip gives a jump target, but jumping to a landmark leaves NVDA in browse mode, so the arrow
  keys do not move along the tabs until you switch out of it (Shaun: "more confusing than helpful").
  The section headings are the landmarks. `ui_checks_device.js` asserts the strip is NOT wrapped.
- **ONE HINT MUST NOT BE `aria-describedby` ON SEVERAL CONTROLS.** The wifi join paragraph was
  attached to the network-name field, the password field AND the Join button, so four sentences were
  read out three times while moving through three controls (Shaun, 30th August 2026, on joining a
  network for the first time: "the repeated hints in wifi name and password fields is too much").
  The text stays on the page and is read once in normal reading order; the LABELS say what each box
  is, which is what a label is for. Attach a description to at most one control, and only where it
  says something the label cannot.
- **A HINT IS FOR SOMETHING NON-OBVIOUS, OR FOR A CONTROL THAT AFFECTS OTHER THINGS** (Shaun, 30th
  August 2026: "the elements themselves say all you need to know"). A labelled dropdown whose
  options name themselves gets neither a hint nor an `aria-describedby`; a description on a dropdown
  is re-read on every arrow press.
- **"Disable this shortcut" is INVERTED against the server's `enabled`.** Ticked means off. The
  translation lives in exactly two places in app.js (`showHotkey` and `saveHotkey`, plus
  `forceHotkeyValues` for a refusal); the server only ever sees `enabled`. "Use this shortcut" was
  the first wording and it read like a Run button.
- **A dialog must close on ONE Escape and put focus back.** Take the `cancel` event,
  `preventDefault`, then close and restore focus yourself. Letting the dialog close itself left
  focus wherever the browser chose, which read as Escape not working (Shaun: "I have to hit esc 3
  times to dismiss?").
- **A dialog opens onto its first CONTROL, not its heading, and carries no intro paragraph.** The
  Add-a-shortcut dialog read its name, then an intro, then both labels, then the heading, before
  reaching anything you could use.
- **A SHARED HELPER'S CALL SITES ARE NOT ALL THE SAME.** `buildTabs` and `buildStaticTabs` both end
  `paint(); return { strip, panel,` - a blind text replacement changed both, and buildStaticTabs
  (which inserts its own strip) then re-wrapped it into a detached node, so FOUR pages silently lost
  their tab strip. Count the occurrences before replacing, and run every page's checks after.

- **Nothing may replace a subtree a screen reader may be sitting in, whatever triggered the
  replacement:** a read, a tab change, a poll or a write's confirmation. Use `rebuildIfChanged` or
  `fillOptions`, keyed on a fingerprint of the STRUCTURE, never the values.
- **A handler built inside `rebuildIfChanged` must read the LIVE payload (`recordingModel`), never
  the `d` its closure captured.** The builder runs only when the STRUCTURE fingerprint changes, so a
  captured payload is whatever was on screen the first time the page loaded.
- **A control that is currently inert goes `aria-disabled` via `setAvailable`, NEVER the `disabled`
  attribute.** Disabled leaves the tab order, so the control vanishes and reappears as conditions
  change and anyone sitting on it is thrown elsewhere. aria-disabled keeps its place, announces as
  unavailable, and carries the reason. Pressing it anyway must SPEAK that reason, and for a checkbox
  must also put the checkbox BACK: a flipped switch with no write behind it reports a state the unit
  is not in. Both raw-multitrack switches follow this (`applyBypass` / `sendBypass`).
  **THE LAST ELEVEN VIOLATIONS WENT ON 2nd September 2026, and `app.js` now has NO raw `.disabled =`
  assignment at all** - grep for one before adding a control, because the count going back above zero
  is the whole regression. Three things that pass made it hold: the refusal for a whole PAGE belongs
  at the one function its writes already share (`routingPost`), not in a list of handlers a new
  control can be left off; a whole SECTION with no such choke point takes ONE delegated capture-phase
  listener (`installPadsGuard` on the pads page, which has fifteen handlers and two control families
  built from the device payload with no fixed ids); and a control that fires per ARROW KEY - a slider,
  a closed select - must refuse through `refuseChange`, which speaks once for a burst, or the reason
  talks over the value the screen reader is reading back.
  **THEY CANNOT BE TESTED FROM A DESK.** All six gates behind them (`unit_settings`, `pad_write`,
  `fader_write`, `output_write`, `level_write`, `matrix_write`) are ON for both desks, so no desk in
  existence reaches these branches. Feed the UI the payload a capless desk would send:
  `DevTools/make_readonly_stub.py` then `DevTools/ui_checks_readonly.js`.
- **`announce()` REPLACES the live region, it does not queue.** Two announcements in a row leave the
  first unspoken, so an action that also moves the user must return its result and speak ONE message.
- **AUTOSTART IS ONE REGISTRY VALUE WRITTEN BY TWO THINGS THAT CANNOT SHARE CODE**, and nothing at
  runtime would notice them drifting: `ProjectBuilds/installer.iss` and `Project/autostart.py` must
  agree on the key, the value NAME and the command. A different name breaks the installer's
  `uninsdeletevalue`, so an uninstall leaves a Run entry pointing at a deleted exe - a startup error
  every login with nothing left to turn it off. `DevTools/test_autostart.py` reads the `.iss` and
  asserts it. **HKCU, never HKLM**: the server holds a fixed port, so a machine-wide entry starts an
  instance per account and they fight over it (real on scottpc).
  **RENAMING THE APP MEANS MIGRATING FOUR THINGS THAT LIVE ON THE USER'S MACHINE**, learned on 20th
  September 2026 when "Access Caster" became "AccessCaster": the Run value, the firewall rule (netsh
  keys a rule by NAME, so the old one is orphaned for ever otherwise), the Start Menu group and the
  desktop shortcut. All four are handled in `installer.iss`, as `[InstallDelete]` entries and a
  `deletevalue`, and they are deliberately NOT gated on the tasks the user ticks. **The one that is
  not obvious is `UsePreviousGroup=no`**: Inno defaults to reusing the group name the machine
  remembers, so the first build after the rename wrote correctly-named shortcuts into a folder still
  called by the old name. `autostart.py` carries `LEGACY_VALUE_NAME` for the same reason - two Run
  entries naming one exe start the app twice at sign-in.
  **The `--startup` flag is the whole difference between a login launch and a manual one** - at login
  `tray.main` passes `open_browser=False` and comes up in the tray with the announcer and shortcuts
  live. The tray path already runs `watchdog=False`, so a page-less instance does NOT hit the
  "no webpage connected in 120 s, stop" rule; check that first if it ever seems to quit by itself.
  **A source run REFUSES to write it** rather than naming python.exe plus a script path, which would
  break silently at a login.

- **OPENING A FOLDER MUST ALSO PUT THE USER IN IT.** `os.startfile` opens the window BEHIND the
  browser: measured 4th September 2026, the foreground two seconds after the call was still
  "Profiles - AccessCaster". A window you were not moved to is indistinguishable from nothing having
  happened, and that is exactly how it was reported. Windows will not let a background process take
  the foreground unaided, so `server._open_folder_focused` borrows the input state of the window that
  has it (`AttachThreadInput`), which survives a browser being frontmost. Match the window on a title
  PREFIX: Explorer sets the folder name first and appends " - File Explorer" a moment later, so an
  equality test passes or fails on how fast the poll got there. Failing to focus is NOT failing to
  open, so everything after `startfile` swallows its own errors.

- **THE LIGHT THEME'S `--accent` IS A MEASURED VALUE, NOT A TASTE.** `#0d5bd1` gives 5.71:1 on the
  page and 6.11:1 on a card. It was `#0b6efd` until 4th September 2026, which is 4.21:1 and so under
  the 4.5:1 AA needs for normal text - every link in the light theme was failing, and it took adding
  one link to the footer to notice. White on the accent went from 4.50:1 to 6.11:1, so the buttons
  improved with it. The FOCUS RING stays on the brighter blue deliberately: it is a 3px outline
  judged against the 3:1 non-text bar and has to stay visible on the accent itself. Re-measure before
  changing either.

- **A CONTROL'S OUTCOME IS ANNOUNCED LAST**, after `setBusy(btn, false)` and after any refresh it
  triggers. Announce it earlier and the focused button's aria-disabled coming back off (plus the
  device read that follows) lands on top of the live region: the result goes unspoken and all you
  hear is the button's own label again. Measured on the mix schemes control, 29th August 2026.
- **Do NOT put the same words in a section status line AND `announce()`.** A status line that is
  also `role="status"` makes two polite regions change at the same moment and the outcome can be
  dropped. Either the section owns a live region and speaks through it, or it keeps a plain
  paragraph and `announce()` speaks. The mix presets section learnt this the hard way.
  **AND THERE IS A THIRD VOICE, which this rule did not name: `aria-describedby` pointing a BUTTON
  at that same status line.** A screen reader re-reads the description when focus returns to the
  button, which it does as soon as `setBusy(btn, false)` runs, so the outcome is spoken a third time
  in full. Measured on "Save this app's settings to a file", 4th September 2026: one save, three
  complete readings. A status line a control is described by must not also be live.
  **ELEVEN HANDLERS STILL DO THE TWO-VOICE VERSION** across the pad editor, the Levels tab, Reset all
  outputs, Mix presets and stereo link. Do not add a twelfth.
- **THE RECORDING PAGE HAS EXACTLY ONE LIVE REGION (`rec-state`), and its own suite asserts it.**
  Every outcome on that page, deleting included, goes through `setRecState` - not `announce()` and
  not a second status line. A new control there must not bring its own.
- **A panel with no focusable content needs `tabindex="0"`.** `setPanelTabStop()` does this in both
  tab helpers; do not undo it. Without it Tab sails past the whole page.
- **Never write to the device from a combo box's `change` event, and never rebuild a panel from
  one.** A COLLAPSED select fires `change` on every arrow key, so each keypress applied a preset and
  destroyed the select mid-interaction. Dropdown plus an explicit Apply button.
- **`<output>` has an implicit role of `status`, i.e. it is a live region.** A visible value beside a
  slider announced itself on top of the slider's own `aria-valuetext`; use `aria-hidden` +
  `aria-live="off"`.
- **No named `role="group"` anywhere.** It is announced on the way in and again as "out of grouping",
  and the heading above says the same thing for free.
- **NVDA never reads content revealed inside an already-open dialog.** Open a FRESH dialog per stage
  (auto gain has three: confirm, progress, result).
- **Nothing may rebuild the page while a dialog is open.** `loadChannels()` ran there once and broke
  the focus trap.
- **If a label needs cutting, cut the WORDS, never the `label:` key.** A verbosity commit deleted the
  keys from `REC_TRANSPORT`, so `btn.textContent = spec.label` assigned undefined and the record
  button had no accessible name. Nothing looked wrong on screen.
- **Never leave an `aria-describedby` pointing at a deleted id.** Worse than having none.
- **`aria-describedby` is read on EVERY focus,** so length costs more there than anywhere else.
- **A hidden pane, not a destroyed pane.** Both tab helpers build every pane once and flip `hidden`;
  a hidden pane is already out of the accessibility tree, the tab order and heading navigation.
  Focus moves to the incoming tab BEFORE the outgoing pane is hidden.
- **Store per-element state on the element (`dataset.*`), not in a build-time closure.** An in-place
  value update leaves a closure stale: the trap behind both `dataset.prev` and `dataset.known`.
- **A failed read must say so, not leave old numbers looking live.** `markLevelsUnread` puts sliders
  back to "not read yet".
- **The web UI binds 0.0.0.0 with no authentication.** The wifi join is refused for any caller that
  is not this machine, because a password typed into that page would otherwise cross the very network
  it is for. Keep that refusal.

- **A CUE IS A WAV PLAYED THROUGH SAPI, NEVER A BEEP.** `winsound.Beep` and `[Console]::Beep` go to
  the SYSTEM default output, which on these machines is the desk, so a cue would go to the callers
  instead of the operator. `tones.py` generates the WAVs and the speech worker plays them with
  `SpeakStream` on the SAME SpVoice it speaks with, so they inherit the announcer's chosen device and
  volume for nothing. The queue carries them as `!wav <path>`; `_ann_say_oneshot` must strip that
  prefix or the fallback path reads a Windows file path aloud.
- **THE PUMP COALESCES, SO TWO CUES QUEUED TOGETHER BECOME ONE.** Right for speech (the newest state
  is the only one worth hearing), wrong for a pair, and it silently ate the low half of the mute
  confirmation until 16th September 2026. Anything that must play two sounds in order has to space
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

- **Level meters read Windows audio endpoints, so metering cannot work or be tested over Remote
  Desktop.** Run `query session` first; that is the whole precondition.
- **Default to a CAPTURE endpoint.** With no stream chosen the meter defaulted to the PLAYBACK
  endpoint, so it measured this PC's own screen reader instead of the mic. "Microphone (...
  Main Stereo)" is the desk's OUTPUT mix; label devices by direction.
- **A capture endpoint reads 0.0 until something opens it.** `meters._EngineWaker` is that something.
  The class was once declared with a docstring claiming an `activate()` that did not exist.
- **The clip threshold must be scaled by the endpoint's LIVE volume ceiling.** Windows' endpoint
  volume scales the meter, so at -1.08 dB true full scale can never pass 0.99. `trusted_ceiling()`
  refuses an untrustworthy ceiling and falls back to 1.0, because a failed volume read would
  otherwise announce a whisper as clipping. The healthy/strong/hot boundaries are Shaun's judgements;
  do not rescale those.
- **The Windows recording slider is BOOST above about 55 percent** on this endpoint: 50% is
  -1.08 dB, ~55% is unity, 75% is +7.67 dB, 100% is +30 dB. Best practice, and it is SET on this
  machine: Windows at unity, gain on the desk, peaks of -12 to -6 dBFS.
- **The `... Chat` input REFUSES to move from -1.08 dB**, by dB and by scalar, silently. Both
  endpoints report a hardware volume range of +0.00 to +0.00 dB with increment 0, so this is
  driver-enforced rather than a bug in our call. Immaterial: Chat carries the chat return.
- **Chat cannot be level-checked.** The check listens on the Chat OUTPUT, which is mix-minus, so the
  caller is deliberately not in it. No button is built for it.
- **A channel with its fader down reaches the isolation mix at 0.** That refusal is the common path,
  so it says to bring the fader up rather than describing the mixer.
- **`meters.Watcher` must record a mid-loop exception.** It once died silently, so a broken run was
  summarised as a QUIET CHANNEL. A run is in progress if EITHER the watcher or the isolation exists,
  and each run's end-timer carries its run number.

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
  UNIT_SETTING_SEL` says what can be written; `server.UNIT_SETTINGS` says how one is presented. That
  split is deliberate (the address table is shared with the Duo and must not carry UI copy), but
  MEMBERSHIP is not: every entry in the address table either appears on the Device page or is named
  in `server.UNIT_SETTINGS_NOT_ON_PAGE` with a reason. `server.unit_settings_off_page()` must return
  empty, and `DevTools/test_specs.py` asserts it. Four settings drifted for a day without this
  (2026-08-28) and nothing failed except a test that looked broken.
- **`outputMonAutoMute` WORKS, and ships OFF; the app used to be able to show it ON regardless.**
  Ear-proven 29th August 2026: written True, the monitor speakers duck on a mic fader move. The
  fault was `action_set_auto_mute` reporting success from the write returning, with no read-back, so
  a switch could sit there claiming a setting the desk did not have while Shaun's speakers howled.
  **It reads back BY BLOCK now and returns the state, and the browser switch follows the DESK, not
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
  whose field 2 is the FILE PATH, so its extension names the format. Field 0 is the name, field 4 the
  duration in seconds.
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
- **`say_worker.ps1` HAS NO `-Text`. IT READS STDIN.** It is the announcer's PERSISTENT worker: one
  phrase per line on stdin, and it exists so the server does not pay 1.3-1.7 s of PowerShell startup
  per announcement. Called as `say_worker.ps1 -Text "..."` it starts, selects the device, prints
  "WORKER ready", hits end-of-stdin and exits HAVING SPOKEN NOTHING, with no error. Two DevTools
  scripts did exactly that and their `--speak` had never once spoken; one of them was a WITNESS, so
  its "the audio reached the desk" readings were whatever else was playing. **For a one-shot use
  `say.ps1`**: it takes `-Text` and `-Device`, speaks SYNCHRONOUSLY so the process cannot exit
  mid-phrase, and takes `-Force` to defeat the Claudio focus gate. **CHECK ITS OUTPUT FOR "SAY
  spoken"** rather than assuming, especially in anything that uses sound as evidence.
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
- **A FIELD THAT PUSHES NOTHING BACK HAS NOT NECESSARILY DONE NOTHING.** `wifiScan` was filed
  unproven for a day because it was written and no push came back, reasoning from
  `requestRecordState` and `requestDropMarker`, which do push a cleared value when consumed. It
  fills `wifiScan1..10` silently instead. Test for the RESULT, not the acknowledgement, before
  writing a capability off.
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
  they are indistinguishable from a dead desk and every check over them is ignored. Both
  `test_shows.fake_blob` and `test_shows_sim.encode` do.

## wake_channels reads the gain from the DESK

- **NEVER FROM `_session_state`.** Its only callers are a factory reset and a show import, i.e.
  exactly the two moments the app's cache is guaranteed stale, because changing the desk is what
  they are for. It took the "put it straight back" half of its nudge from that cache, so **every
  successful restore quietly wrote the app's last displayed gain over the restored one** - measured
  21st September 2026, a backup holding Mic 1 at 55 dB restored correctly and then had 45 written
  over it. `channels.mic_gain` is the paired reader, at the same address `set_mic_gain` writes to.
- It was invisible for as long as it existed because the occurrence that exposed the area had the
  desk off the bus, so the writes went nowhere and the restored value survived by accident. **A bug
  masked by a second fault is the argument for fixing the reporting first**: it only showed up once
  a restore succeeded with the desk alive.
- It now refuses outright when the desk is not answering, rather than writing into the dark.

## The server's lifecycle, and why it cannot be reasoned about from outside

- **AN OPEN PAGE OUTLIVES ITS SERVER, so the page watches it** (`noteServer` in app.js). A window
  left open across an upgrade otherwise runs the OLD page against the NEW server, and anything the
  new version added is missing with no error. The `/api/ping` reply carries `_INSTANCE_ID` and the
  version; three missed pings open `server-lost-dialog`, and a different version reloads the page.
  Any HTTP answer counts as alive (a remote page with a stale token is refused, not stopped). The
  dialog's text never changes while open, because NVDA does not read changes inside an open
  dialog: recovery closes it and speaks outside it. A page from before this existed cannot do any
  of it, so the first upgrade past it still needs the old window closed by hand.

- **THE TRAY BUILD HAS NO WATCHDOG.** `start_server_managed` passes `watchdog=False`, deliberately:
  the app lives in the tray until Quit, so closing the page must not stop it. Only
  `python server.py` runs with the watchdog on. **Any theory of the form "the watchdog stopped it"
  is wrong for the installed app**, and it cost an hour on 21st September 2026 before anyone read
  the call site.
- **A RUNNING JOB OUTRANKS EVERY STOP TRIGGER** (added 21st September 2026, `_lifecycle_stop_reason`
  takes `job_running`). A backup is mid-read of the model and a restore is part way through writing
  settings and pads; a page closing must not abandon either. This is right on the
  `python server.py` path and is NOT what was happening in the tray.
- **THE TRAY EXE IS WINDOWED, SO EVERYTHING PRINTED GOES NOWHERE**, including the reason the server
  stops. That is why both of 21st September's diagnoses were inferred from the outside and both
  were wrong. **SO READ `%LOCALAPPDATA%\AccessCaster\logs\diagnostic.log` BEFORE THEORISING ABOUT
  ANY LIFECYCLE OR DEVICE FAULT.** `Project/diagnostics.py` tees every `print` into it along with
  the jobs, the records sent, the handles opened and closed, transfer mode, and the desk going
  quiet. Tray menu > Diagnostic log opens it.
- **THE LOG IS STARTED IN EXACTLY ONE PLACE, `server._bring_up_server`,** and that is what keeps it
  a single-writer file: every entry point (tray, `python server.py`, the dev server) goes through
  there, so only the process that actually owns the server ever writes to it. Starting it in each
  entry point instead would put two processes on one file the moment a second launch raced the
  first. It is SILENT until then, so the offline suites and the DevTools scripts write nothing.
- **MONITOR AUTO-MUTE NEEDS A MIC THAT IS BOTH UNMUTED AND FADED UP, AND RODE'S GUIDE ONLY MENTIONS
  THE FADER.** Their wording is "muted whenever a microphone channel fader is turned up", which is
  incomplete and sends you the wrong way twice: a muted mic with its fader up does NOT engage it.
  The condition is ANY mic channel unmuted with its fader above zero, so with several mics open
  muting one appears to do nothing - the others still hold it - and the monitors only return when
  the LAST one is muted or faded down. That is why it can look like one particular fader is special:
  it is simply the only one in play. Worth knowing when a user reports silent monitors, because
  blind the cause is invisible and the obvious fix (mute the mic you are using) does not work.
- **THE PORT IS THE SINGLE-INSTANCE LOCK, AND `_ExclusiveHTTPServer.allow_reuse_address` MUST STAY
  FALSE.** `HTTPServer` sets it to `1`, and on Windows that flag lets a second process bind a port
  another process already holds - measured, not assumed, by `DevTools/check_exclusive_bind.py`,
  which also proves a restart still binds straight after a clean stop. Put plain `ThreadingHTTPServer`
  back and two servers can sit on 8770 at once, in different Windows sessions, both reaching for the
  one desk.
- **ONLY THE PORT IS MACHINE-WIDE. THE REST OF THE APP BELONGS TO ONE WINDOWS SESSION**, so a second
  signed-in account is REFUSED rather than served. The announcer speaks into the session that
  started it, the global shortcuts are registered there, the tray icon lives there and the settings
  written are that account's - so sharing gives the second user a page that looks right, says
  nothing and answers no key, which for a blind user is the worst kind of failure. The real
  multi-user answer is the remote-access token, which serves the UI to another MACHINE.
- **THE TWO CHECKS THAT CANNOT SEE ACROSS SESSIONS, and why the refusal is its own test.**
  `_app_already_serving` is an HTTP GET to localhost, which is machine-wide, so it answers True for
  another account's instance. `_focus_app_window` enumerates windows, which are per-desktop, so it
  answers False for the same instance. The pair of them therefore ends in "serving but no window",
  whose fallback is to open the UI - somebody else's. `_other_account_holds_port` runs BEFORE both,
  in `_bring_up_server` and again in `tray.main`.
- **NEVER TASKKILL A PID WITHOUT CHECKING ITS SESSION.** `_close_previous_instances` exists to clear
  a stale zombie server of OURS, and `netstat` hands it the port's holder whoever owns it; killing
  another account's process is only refused today because that needs admin. `_session_of_pid`
  (`ProcessIdToSessionId`, a bare PID, no privileges, and no handle on the other process) is the
  filter. Do not "fix" a failure there by elevating.
- **AN IDENTICAL EVENT REPEATED BECOMES ONE `repeated ... xN` LINE.** The page polls, the recorder
  watches and the announcer reopens its handle, so a line per event is thousands of identical lines
  with the signal buried in them. The FIRST of anything is always written the moment it happens, so
  nothing is hidden, only counted. A flusher thread closes the group within two seconds, because
  occurrence one of the hang ended with the power lead being pulled and anything still in a buffer
  would have been the part worth reading.

## USB dropout

- **WHAT IT IS.** The desk vanishes from USB, Windows shows ZERO devices with vendor id `19F7`, and
  only a physical replug returns it. Three known on scottpc: 20th July, 31st August and 3rd
  September 2026. Nothing is lost by it.
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
  is a fresh roll of the 1-in-5 above. It stays on the never-sweep list BECAUSE it works. Its
  neighbours `updateInitiateRequested`, `updateResetDeviceRequested`, `updateResetAfterFWURequested`
  and `updateResetAppRequested` are untested and are NOT the same thing; check `updateDownloaded` and
  `updateVersion` are empty before going near any of them.

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
  has a delta of 0, which `_ann_at_limit` treats as "do not know", so turning past 100% says
  "Encoder not linked to anything". `test_announcer` builds the real composite and pins the delta.
  The desk publishes eight composites: `encoderSignal`, `mixLinkRequest`, `mixUnlinkRequest`,
  `padRecordClear`, `padRecordMemoryFull`, `padRecordStateRequest`, `playerJumpToSample`,
  `sipSlotCallDisconnect`.

- **THE CHANNEL BUTTON TOGGLES, AND THE ENCODER PRESS DOES NOT RELEASE IT.** Measured with
  `DevTools/encoder_press_probe.py`, 4th September 2026: `systemChannelSelected` went to 0 on the
  press, stayed at 0 across two encoder presses, and returned to -1 only on a second press of the
  same channel button. So while a channel is selected the announcer must NOT advance its ring - doing
  so announced USB 1 and then Chat while the desk had never left Mic 1, and left `enc_pos` pointing
  at an entry `_ann_at_limit` then read. **This is why there is no "Free the encoder" control**: it
  wrote `systemChannelSelected` = -1 to undo something the desk already undoes, on the strength of a
  docstring claiming a power cycle was the only way back. Do not rebuild it.

- **SAY WHAT A CHANNEL HAS, NEVER WHAT IT LACKS**, in the dead-turn message as well as the selection
  one. It used to assert "has nothing for the encoder to adjust" for any turn the ring did not see,
  which included a mic gain that was moving perfectly well. Both go through
  `_ann_selected_phrase` now, which works it out from the strip and so cannot claim it wrongly.
- Two rules follow: **never write "use the encoder" for a named level** in UI copy or docs, and **key
  every spoken announcement off the PROPERTY THAT CHANGED**, never off an assumption about the
  encoder. Done that way, turning the encoder on a channel correctly announces that channel.

## Global keyboard shortcuts

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
  is what proves the window, the browser and the screen reader are all innocent.
  So the test for this is never "is it the foreground" - that passes while broken. It is whether a
  screen reader follows, or failing that whether a plain permitted `SetForegroundWindow` was enough.
  **Do not answer a refusal by forcing it, and do not answer the silence by speaking**: bringing a
  window forward is a Windows action, the screen reader announces it, and the app's own announcer
  is for the DESK.

- **A FRESH INSTALL GETS SIXTEEN SHORTCUTS, NOT FIVE.** `_hotkey_defaults` is the five app ones plus
  the whole of `_pad_hotkey_scheme` (what is playing, bank up and down, one per pad slot). The pad
  half is a separate function only because `action_add_pad_shortcuts` can lay it onto a list that
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
- **The cough key (`_hk_mics`) OPENS ONLY THE MICS IT MUTED.** It keeps `_hk_muted_mics`. A plain
  "any open? mute all, else unmute all" toggle silently opens channels somebody deliberately shut:
  measured 30th August 2026, one open mic and three muted went to four OPEN after two presses.
- **Hotkey actions must speak their result**, via `_ann_say`, which is independent of whether the
  announcer is on. The window is not in front of you when a shortcut fires; the speech is the only
  feedback. Silence is what a broken shortcut sounds like. **EXCEPT where the user has silenced that
  shortcut** (per row, `speak`), and except the announcements shortcut turning announcements OFF,
  which is ALWAYS silent: the reason to press it is that announcements are reaching a recording. **A
  FAILURE is spoken whatever the flag says** - silencing hides a confirmation, not a fault.
- **THERE ARE TWO WAYS TO FIRE A PAD AND THEY COVER DIFFERENT PAD TYPES. USE MIDI FOR A SOUND PAD
  AND HID FOR THE REST.**
  - **HID, `padActive`** (`soundpad.engage_pad` / `unlatch_pad`): voice-FX pads engage and release,
    and a MIXER pad performs its whole action, because for those two the FLAG IS THE STATE. **A
    SOUND PAD SETS THE FLAG AND PLAYS NOTHING** (ear-proven negative, 30th August 2026): a physical
    press pushes `padButtonPressed`, then `padActive`, then a stream of `padProgress`, and this
    reproduces the middle event only, so the engine never starts playback.
  - **MIDI, CC 35 on the pad's own channel** (`DevTools/midi_out.py`, 2nd September 2026): plays a
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
    `server._hk_fire_pad`, per pad and never per bank: the kinds are mixed inside a bank.
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
  voice effect switched on from software is knowable only from `_hk_pads_on`.
- **A PAD SHORTCUT MUST NOT REFRESH THE PAD MAP ON EVERY PRESS, and doing so is what made pad
  firing flaky** (Shaun, 2nd September 2026: "sound pad playback and stopping is flakey"). A
  refresh is a FULL MODEL DUMP, 1.5 to 3 seconds holding `_hid_lock`, so a row of number keys at
  performance speed queued one per press - and `HotkeyManager._fire` DROPS a re-press while the
  last one is still running, so presses went missing rather than merely arriving late. It is a TTL
  now (`_HK_PAD_CACHE_S`) plus an explicit `_hk_pads_stale()` from `_pad_write` and from a profile
  restore. It was never needed on the MIDI path anyway: MIDI addresses a pad by bank and slot,
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
  a Pro II. And it is saved into a profile and re-applied by `profiles._apply_v2`, so restoring a
  profile taken while MIDI was off silently kills every pad shortcut. So anything depending on MIDI
  must ENABLE IT AT THE POINT OF USE rather than once at startup - the write is idempotent and
  measured at 0.004 s, so there is no reason to be clever about it. Do not read-then-write:
  `_session_state["midi"]` is not updated by a change made on the desk itself, so it can be stale
  in the one direction that matters.
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
  Banks are 0-indexed here and bank 8 (index 7) is the system / function pads.
- **`inject_button.py`'s `cmutePressed` is `mutePressed` at path [0, 0x63].** The 'c' is the address
  byte, not part of the name, and 0x63 is child 99 of PHYSICALINTERFACE - the first SOLOMUTEBUTTON.
  PADBUTTON is children 35 to 98. Do not go looking for a property called `cmutePressed`.
- **A pad shortcut's target is the padIdx, never the stream position.** padIdx is the pad's fixed
  place in the grid; position moves when any pad is created or cleared. `_hk_pad_map` caches
  padIdx -> position so a press is instant, refreshes in the background after every press, and
  CHECKS that the pad that engaged is the one that was asked for. Keep that check: without it a
  stale map silently fires a stranger's voice effect onto a live mic.
- **`hotkey_manager()` is LAZY.** Importing `server` must never register a system-wide hotkey: every
  DevTools script imports it. Only `_bring_up_server` asks, and never in headless mode.
- **`test_hotkeys.py` PRESSES NO KEYS, AND MUST NOT AGAIN.** It fires shortcuts by posting
  WM_HOTKEY to the pump. Injected presses proved only that Windows delivers a registered key, which
  any real press shows, and a run killed between a press and its release left Ctrl and Shift held
  down system-wide on the machine in use. `DevTools/press_keys.py` uses `keybd_event`, not
  SendInput, which injects nothing from an RDP session here.
- **Adding a Device tab breaks `ui_checks_device.js` in three places**, none of them obvious: the
  tab/title lists, the fixed string of pane headings, and the arrow-key debounce check, which counts
  presses to land on Unit settings (nine rights over seven tabs, not eight over six).

## The server's shared shapes

Four shapes in `server.py` each existed as many hand-written copies until 30th August 2026. Use the
shared one; a new copy is how they drifted apart the first time.

- **A LONG JOB IS A `_BackgroundJob`.** `_profile_job`, `_restore_job`, `_show_job`,
  `_pad_restore_job`. `start(work, message=..., ...)` claims it (refusing plainly if one is already
  running) and runs `work(job)` on a daemon thread; `work` REPORTS with `job.step` (message only)
  or `job.tick` (message and count) and RETURNS its result dict. The class writes the finish and
  turns a crash into a sentence, so a failure can never leave a progress dialog that never closes.
  There were four copies of the state machine and SIX of the finish block. The page only reads
  `message`, `done` and `ok`.
- **A LIVE PAD EDIT IS A `_pad_write`**: range-check the grid, take the HID lock, call, turn a crash
  into a sentence. `invalid=` is a refusal for a value the helper cannot check itself and is
  checked AFTER the range and BEFORE the lock (the pad number is the more basic complaint, and
  nothing should take the device lock to find out a value was wrong). `plain_value_error=True`
  surfaces a ValueError's own text, because soundpad raises those with a finished user-facing
  sentence and wrapping it buries the part that says what to do.
- **A RISKY WRITE FAMILY IS GATED BY `_cap_gate`**, which holds the shared reasoning: a selector
  proven on a Pro II is NOT proven on a Duo, and a wrong one can append a stray only a factory reset
  clears (it happened, 26th August 2026). Each gate keeps its OWN refusal sentence, because what the
  user can and cannot do differs per family. **`_level_write_gate` is deliberately not on it**: it
  has a second condition beyond the cap (the device must carry confirmed faderLevel selectors).
- **The master Compellor and output delay come from `rcp_read.master_processing`**, shared by the
  Routing page and a profile. They were built twice, so a property added to one and not the other
  would have been a setting the page could change and a profile could not put back.

--------------------------------------------------------------------------------
## Tooling

- **EVERY NUMBER IN `CodeMap.md` IS GENERATED. Never edit one by hand.**
  `python DevTools/codemap_lines.py` rewrites them; `--check` reports drift and exits 1. It covers
  the `name` (NNN) references, the file table's line counts and the DevTools file count, and it
  touches no prose. They were regenerated BY HAND on 29th August 2026 and all 96 were wrong again by
  the 30th, drifting up to 755 lines, because one day's work moves server.py and app.js by hundreds.
  A confidently wrong line number is worse than none: you jump, land elsewhere, and grep anyway.
- **THE RELEASE NOTES ARE GENERATED FROM `version.py` HISTORY, which is a LIST, not prose.**
  `ProjectBuilds/make_release_notes.py` writes `Project/ReleaseNotes.html` before the freeze; the
  spec bundles it and the installer offers it, ticked, as a post-install action. It is gitignored:
  it is derived. A hand-written notes file beside version.py is the second copy this project always
  ends up disagreeing with, which is why the history became data. HTML rather than text because a
  screen reader can jump heading to heading through it and cannot through Notepad.
- **A SCRIPT THAT FACTORY-RESETS A DESK MUST CALL `_bootstrap.require_expendable_desk` AND REFUSE
  WITHOUT `--expendable-desk`.** Two do: `test_usb_drop_live.py` (one reset per trial) and
  `probe_show_push.py --reset`. A capitalised warning in the docstring is NOT the guard, and this is
  measured, not theoretical: `test_usb_drop_live.py` opened with "EXPENDABLE DESKS ONLY" and was still run
  over Shaun's working Pro II on 16th September 2026 by a sweep that selected tests by filename. The
  reader it warned was not the thing that ran it.
- **A `test_*.py` SCRIPT GETS A THROWAWAY DATA DIR, AUTOMATICALLY.** `_bootstrap` sets
  `ACCESSCASTER_DATA_DIR` to a temp folder when `sys.argv[0]` starts with `test_`, before the first
  Project import, which is the only moment it can work: `profiles.PROFILES_DIR` and
  `routing_schemes.SCHEMES_DIR` are derived from `paths.DATA_DIR` at import. It is keyed on the NAME
  rather than an opt-in call because an opt-in is what a new test forgets. The dev server, the probes
  and the capture tools are not tests and keep the real data dir, which is the point of running them.
- **A UI PROBE SUITE THAT "NEVER REPORTED BACK WITHIN 90s" WITH 0 PAGE ERRORS IS A SYNTAX ERROR IN
  THE SUITE, NOT A HUNG PAGE.** The whole file is one function body, so a second `const` of a name
  already used throws at PARSE time and no check runs, which reads as a timeout. `ui_checks_boundary.js`
  sat like that from 3rd September to the 16th. `node --check DevTools/ui_checks_*.js` catches it in
  a second and is worth running after editing one.
- **`audit_code.py` runs against `Project/` from anywhere.** It used to glob `*.py` in the cwd while
  telling you to run it from the repo root, which has none, so it reported a clean pass over ZERO
  files and looked like it was working. Its reference sweep deliberately includes `DevTools/`: a
  Project function called only by a probe or a live test is in use, not dead.
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
- **Loading the app's page in a HEADLESS BROWSER STOPS THE SERVER**: `pagehide` fires
  `sendBeacon("/api/quit")`. Use `ui_probe.py` (it stubs POSTs and serves its own copy) or start the
  server with `ACCESS_CASTER_NO_UI=1`.
- **`ui_probe.py` takes a free port per run.** It used to hardcode 8771 with `allow_reuse_address`,
  and two servers bind that port CLEANLY on Windows (measured), so concurrent suites silently swapped
  traffic and one reported "the page never reported back".
- **`ui_probe.py`'s default 90 s timeout is not enough.** Pads needs `--timeout 600` (about nine
  minutes), device 240. A timeout reports as "never reported back", which reads like a fault.
- **`transferModeType` = 2 MOUNTS PAD STORAGE ONLY.** Not the card, not the USB drive: F:, 5033 MB,
  a `\pads` folder. Value 1 mounts nothing. Measured 29th August 2026
  (`DevTools/transfer_volumes.py`), so do not reach for transfer mode to get at recordings.
- **THE DESK NEVER PUSHES A `STORAGEVOLUME` RECORD.** Neither a record start/stop nor an unplug and
  replug of the drive produces one; you get `recordState` (0x03) and `lastRecordingID` (0x0F) and
  nothing else. So `selector_from_push.py` cannot learn that block's address, however long it listens.
- **`storageVolumeState` IS THE LIVE SOURCE, `storageVolumeCapacity` / `Free` / `Inserted` /
  `Mounted` READ STALE ZEROS.** The string is `capacity|free|?|inserted|mounted`. A fourth volume
  appears while recording: it is the unit's live view of the drive being written to, already handled
  by `recorder.read_volumes(include_extra=True)`, and it is NOT a real extra device.
- **THE CAPTURE TASK MUST BE RUNNING.** `AccessCasterCapServer` in state **Ready is NOT polling**;
  `cmd.txt` is read only once `Start-ScheduledTask` puts it in Running.
- **`ui_checks_recording.js` FAILS ON A MISSING PRECONDITION, not on a bug.** It proxies reads to
  whatever is on 8770, so it needs `DevTools/run_dev_server.py` (the installed build predates the
  page), and it needs a card or drive IN: with no destination the unit reports state 3 and the page
  correctly disables the transport. Both met = 72 pass, 0 fail (29th August 2026). Diagnosed as a
  probe bug twice; check `/api/recorder/live` state first.
- **`ui_probe.py` quietens the first-run baseline dialog by default,** because it took focus and
  silently failed 15 checks across two suites. The baseline suite passes `--offer-baseline`.
- **A server-side change needs the SERVICE restarted, not the window reloaded.** A static-file change
  needs only a reload. The INSTALLED build serves its OWN bundled copy of `static/`, so repo edits
  are invisible to it.
- **Never stop `server.py` (port 8770) as routine cleanup.** Only stop it when exclusive device access
  is genuinely needed, then restart it. **A live device test IS that case** (the server owns the HID
  handle), and **`/api/quit` will not stop `run_dev_server.py`**: it only sets a deadline the
  lifecycle watchdog acts on, and the dev server runs with the watchdog off. Kill the process, then
  restart it to pick the new code up.
- **When driving the GUI: the Windows "System tray overflow window" can sit OVER the app and silently
  swallow clicks.** It is topmost, does not show up in a plain EnumWindows sweep, and `WindowFromPoint`
  returns 0 until the probe sets DPI awareness. `PrintWindow(h, dc, 2)` captures the app's own pixels
  underneath any overlay, which is how it was diagnosed.
- **`SetCursorPos` is a NO-OP over RDP.** Use SendInput absolute-move.
- **`.bin` model dumps and `duo_matrix_*.json` are gitignored** because they carry plaintext wifi and
  SIP state. Never commit one.
