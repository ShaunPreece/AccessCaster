# AccessCaster: the parity target

**THE FULL FEATURE UNIVERSE OF THE RODECASTER PRO II AND THE RODE APP: everything an accessible
replacement would have to be able to do.** This is what we are aiming AT, not a record of where we
have got to.

**THERE IS NO STATUS COLUMN, and that is the point** (Shaun's ruling, 30th August 2026: "it
shouldn't be a to do list"). It had one, and it decayed exactly as any hand-maintained derived fact
does: two rows still claimed work was outstanding months after it shipped. What is BUILT is
demonstrated by the app itself; what is NEXT is tracked privately. A target list does not go
stale, because the desk's feature set barely changes, so this file can be trusted years from now in
a way the old table could not be trusted a fortnight on.

**WHERE IT CAME FROM, because it changes how much to trust a line.** Built 19th June 2026 from a
full read of RODE's own user guide at `rode.com/en-us/user-guides/rodecaster-pro-ii`, later cross-checked
against the desk's own property table and against a self-drive sweep of the app's screens. The guide
was a good map of the TERRITORY and an unreliable map of the MECHANISM: it was silent on roughly one
feature in eight (anything with no control of its own, and readable state generally), and wrong
about how a fair number of the rest actually work. **Every one of those errors was caught by the
desk, never by another document.** So treat a line here as "this exists and we want it", never as a
description of how it works.

**Nothing about mechanism belongs in this file.** How something behaves goes in
`PROJECT_GUIDE.md`; byte-level detail in `RODECASTER_PROTOCOL.txt`; pads and voice-FX in
`SOUNDPADS.md`; every property the desk exposes in the generated `PROPERTY_LIST_<DESK>.md`.

**NOT EVERY SCREEN OF THE RODE APP HAS BEEN ENUMERATED.** The app sweep covered the mixer and
device-settings screens; the recordings area was never reached, because Transfer Recordings is
greyed out with no card in. So a claim about an app screen that is not below has no source behind
it and must be checked against the app before it is believed.

## Channels / mixer

- Mic gain (preamp level)
- Line level (BT/USB/Chat)
- 48V phantom
- Polarity / phase
- Mic type (full 0-10)
- Line-in vs Instrument input type
- Channel colour
- Stereo link inputs (1+2/2+3/3+4)
- Fader assignment (all 9)
- Virtual fader LEVEL (faders 7-9)
- Chain follows the channel on a fader move
- Reset all faders to default
- Clear all fader assignments
- Mute - SETTING it
- Muting a paired RODE wireless mic at the transmitter, which is not the channel mute
- Virtual fader level - SETTING it
- Listen (solo) - SETTING it
- Mute / solo / fader position - READING them

## On-board processing

- Processing on/off (every INPUT channel; the SMART pads bus has none, and that is the desk's own
  limit - RODE: "You can add processing to any audio channel except the SMART pads")
- VoxLab Depth/Sparkle/Punch
- Advanced: 7 modules + on/offs
- Advanced: all params (33)
- Simple vs Advanced processing mode
- Preset NAME marker
- Mic presets (Neutral/Podcast/Broadcast + custom)
- Channel Effects on/off (reverb + echo)
- Channel Effects PARAMS
- Master Compellor on/off
- Master Compellor params
- Output Delay (0-2s, A/V sync)
- Metering mode (Default/Broadcast)
- dB/Hz/ms real-value labels on sliders

## SMART pads / voice-FX

- Pad grid read (24 pads, type, replay)
- Add/replace a sound (WAV/MP3, no convert)
- Clear a pad
- Assign factory voice-FX recipe
- Region-B orphan removal + pool self-heal
- Per-pad volume (padGain)
- Rename pad (padName)
- Pad colour (padColourIndex 0-11)
- Loop (padLoop)
- Replay/Continue toggle per pad
- Play mode Toggle/OneShot/Hold
- MIDI trigger mapping (what a trigger pad sends)
- Create single voice effect
- Create Mixer-action pad
- Create MIDI Trigger pad
- Restore factory FX pad(s)
- Custom FX editor (full param control)
- Export a pad's sound to computer
- Monitor or headphones on a FADER
- Fire a pad from software
- Record directly to a pad
- Trim / fade waveform
- Quick Dial pad type

## Routing / outputs / monitoring

- Per-output mix mode (Main/MixMinus/Custom)
- Custom mix cross-point include/exclude
- Cross-point LEVEL (per source per output)
- Live matrix state read
- "Chat = my mic only" co-host preset
- SAVED MIX PRESETS (the output mixes under a name)
- Multitrack mode (Off/Pre/Post)
- Monitor auto-mute (output + BT)
- Headphone type (HP1 + HP2, sensitivity)
- Monitor output level
- Monitor Fixed Output Level
- Bluetooth output level, and the BT / monitor MUTES
- Listen mode (After-fader vs Pre-fader)
- USB / multi output mode

## Recording & export

- Multitrack mode
- Record start/stop/pause
- Bypass processing on multitrack
- Recording quality/format
- REC button mode (Pause vs Stop)
- Markers
- Recordings list, and deleting one
- Platform/custom export presets

## Device / system / network

- Connection / firmware / device info read
- WiFi / BT / wired status read
- MIDI control toggle
- Button-press announcer (SAPI)
- Test tone generator
- Time / Date
- Language
- Screen brightness / dim / button LEDs
- Touchscreen haptics
- WiFi SCAN + network picker
- WiFi credentials WRITE
- Bluetooth pair/connect
- Shows: New / Import / Export
- Factory reset (full)
- Network configuration
- Profiles (our snapshot/restore)

## Live state / readable feedback

- What a blind operator cannot otherwise know
- Which channels are muted
- Which channels are soloed
- Where each fader is
- Monitor speaker level
- Bank / encoder press / screen touch / REC press
- What the ENCODER is currently assigned to
- LEVELS: is a mic live, am I clipping
- Recording state, elapsed time
- Card inserted / space left
- Wireless mic battery / signal
- Which preset a strip claims
- A fader source changed on the desk
- Anything latched I cannot see (pads, emergency mute, what the encoder did)

## Out of scope (Shaun's call)

- Callers / SIP dialler / Quick Dial pads. The mix-minus a call needs is reachable through the
  routing matrix instead, so the dialler itself is not a target.
- Desktop-app-only export and distribution presets.
