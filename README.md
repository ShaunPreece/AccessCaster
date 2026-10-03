# AccessCaster

A Windows app that makes the **RODECaster Pro II** and **RODECaster Duo** accessible to screen
reader users, operable entirely from the keyboard.

AccessCaster lets you set up and control the mixer entirely from your PC, with no need to use the
device's touchscreen or RODE's desktop app, both of which are difficult or impossible to operate
with a screen reader.

## Why this exists

The RODECaster Pro II and Duo are superb audio mixers, but almost everything is configured through
their touchscreen, and the companion app (RODE Central / RODE Connect) is not built for
screen-reader users. For someone who is blind, that leaves large parts of the device hard to reach: setting mic
gain and phantom power, channel processing, routing, faders, the SMART pads, and saving or
restoring your settings.

AccessCaster talks to the unit over the same USB control channel the official app uses, and
presents those controls as a plain, standards-based web page, operable entirely from the keyboard.
It is built and used with NVDA; see Accessibility below for what has and has not been tested
elsewhere.

## What it does

AccessCaster runs as a small Windows system-tray app. It starts a local web server and opens an
accessible page where you can:

- **Microphone channels**: input gain, 48V phantom power, polarity, and mic type on each mic input.
- **Channel processing**: EQ, compressor, noise gate, de-esser, high-pass filter, pan, Aphex, and
  the VoxLab depth / sparkle / punch controls, per channel.
- **Effects**: echo and reverb per channel.
- **Levels and routing**: non-mic channel levels, the output routing matrix (Monitor Mix and
  per-output source levels), and the fader layout (which channel sits on each fader).
- **USB audio, both directions**: what the unit sends this computer (one mixed pair, or every
  channel separately), and what this computer sends the unit. The second of those is RODE's
  Expanded mode: instead of one stereo input carrying everything Windows plays, you get four
  separate ones (Game, Music, A and B), each assignable to its own fader. Music, a call, a game and
  your screen reader then arrive as four channels rather than one, so you can fade or mute any of
  them on its own, keep one out of what a caller hears, or record them as separate tracks. Needs
  RODE's Virtual Devices driver installed; the app switches Windows' sound output over for you.
  **One of the four appears in Windows only while that channel is on a fader**, which is the unit's
  own rule and the usual answer to "the driver is installed, so where are my devices".
- **Getting that driver**, if it is missing: a button on the Routing page downloads RODE's own file,
  verifies it is signed by RODE and runs their installer, with the normal Windows permission prompt.
  **This is the only thing AccessCaster does that reaches the internet**, and it happens only when
  you press that button: never at startup, never on a timer, never to check for a newer version.
  A second button simply opens RODE's software page if you would rather do it yourself.
- **SMART pads**: create, clear and reassign sound pads, voice-FX pads, mixer pads and MIDI
  pads, with dynamic banks, and play any pad from the keyboard.
- **Recording**: start, pause and stop the recorder, choose multitrack or mixed, see what
  storage is in the unit and how much room is left, and eject or erase the card or drive.
- **CallMe**: RODE's remote calls without the touchscreen. Switch hosting on and share the guest
  link, hear who is calling and answer or decline from a keyboard shortcut in any program, call
  another RODECaster by its code or from a contacts list, extend a call before the free plan's
  one-hour limit (or have it extended for you), and set the caller's level and processing like
  any other channel.
- **Backups**: one button to back the unit up, and one list to restore it from. A copy of your
  settings and pads is kept on this computer and that is what a restore uses, because asking the
  unit to reload its own copy can lock it up. The unit's own copy is still made alongside, and is
  the fallback when this computer has none.
- **Presets and mix presets**: mic-processing presets, and mix presets that save the output mixes
  under a name, either the whole desk or a single output.
- **Firmware**: check whether RODE has a newer firmware for the unit, and hear the answer.
- **Device settings**: the unit's own display, buttons and haptics, and how its clock is shown
  (24-hour or 12-hour, whether the time appears on its screen). The time itself is not settable
  from here: the unit sets its own over the network, and only its touchscreen applies a timezone
  or daylight saving change.
- **Wireless**: wifi and Bluetooth, both readable and changeable, including joining a network and
  making the unit discoverable so a phone can pair with it.
- **MIDI control mode**: the unlock used by Stream Deck and similar setups.
- **Global keyboard shortcuts**: bind anything the app can do to a key combination that works
  from any application.
- **Spoken announcer**: an optional feature that speaks physical button presses on the device
  (bank changes, mute, solo, encoder select, fader moves) aloud, so you know what a press did
  without seeing the screen. Mute can be a short tone instead of a sentence.

The longer-term goal is for the software to take over as many of the touchscreen-only functions as
possible, so the device can be set up and run without ever needing the screen.

## Accessibility

Accessibility is the entire point of this project, not an afterthought.

- Built on native HTML controls and standard ARIA, to WCAG 2.2 Level AA and the ARIA Authoring
  Practices Guide, rather than to any one screen reader's quirks.
- Fully keyboard operable, with a logical tab order.
- Live regions announce the result of every action.
- Decision dialogs are true modal dialogs with proper focus handling.
- The system-tray menu is a standard Windows menu, read correctly by screen readers.

**What has actually been tested, as opposed to built for**, because those are not the same claim:

- **NVDA: developed and used with it daily**, on a real desk, by a blind user. This is the one that
  is genuinely exercised.
- **JAWS, Narrator and VoiceOver: not tested, and feedback is wanted.** Building to the standards
  above should mean it behaves the same, but "should" is not "does", and nobody has walked the
  interface with a second screen reader.

If you use JAWS or Narrator and something does not work, please open an issue. Reports from a screen
reader this has not been exercised on are the most useful thing anyone can send.

## Requirements

- A **RODECaster Pro II** or **RODECaster Duo**, connected to the PC by USB.
- **The USB cable must go into the socket nearest the power inlet.** That is the only one the unit
  accepts control over; the other USB socket carries audio only, so a cable there gives you sound
  and no control, and AccessCaster will report that it cannot find the unit.
- **Windows 10 or 11. Windows only, for now.** The app, the local server and all the USB control
  run on the Windows machine the unit is plugged into, and a second computer does not get you round
  that: other devices on your network can open the page as a second screen, which is handy from
  another room, but they are viewing that PC rather than replacing it.

## Install

1. Download `AccessCasterSetup-v<version>.exe` from the [Releases](../../releases) page.
2. Run it. Setup asks whether to install **just for you** or **for all users**, and either is
   fine. Installing for all users needs administrator rights; installing just for you does not,
   which is the only difference that matters. During install it offers to add a Windows Firewall
   rule (so you can open the page from your phone or another device on the same network).
3. When it finishes, AccessCaster starts and opens its page. It also offers to show you the
   release notes.

## Using it

- Closing the web page does **not** quit the app; it keeps running in the system tray.
- To reach the tray with a screen reader, press **Windows + B**, move to "AccessCaster", then
  press the **Applications key** (or Shift + F10) for its menu.
- The tray menu has:
  - **Open AccessCaster** to re-open the page,
  - **Announcer** to turn the spoken button-press announcer on or off,
  - **Diagnostic log** to open a log of what the app has sent to the unit,
  - **Quit** to fully close the app.

The page is also reachable from another device on the same network at `http://<this-pc-ip>:8770`,
which is useful as a second screen from another room. The USB control always happens on the PC
running the app.

## Build from source

You do not need to build it to use it (just download the installer), but if you want to run or
modify the code:

```
# install the runtime dependencies
pip install hidapi pywin32

# run the app from source
python Project/tray.py
```

To build the installer yourself you also need [Inno Setup 6](https://jrsoftware.org/isinfo.php) and:

```
pip install pyinstaller pillow
powershell -ExecutionPolicy Bypass -File ProjectBuilds\build_installer.ps1
```

That produces the standalone app in `ProjectBuilds\dist\AccessCaster\` and the installer in
`ProjectBuilds\Output\AccessCasterSetup-v<version>.exe`.

### Layout

- `Project/` the application (the local server, the tray app, and the web UI).
- `ProjectAssets/` data the app loads at runtime (effect recipes and mic presets). It ships no
  audio: the unit holds its own factory pad sounds and rebuilds them on a factory reset.
- `ProjectBuilds/` the packaging scripts (PyInstaller spec, Inno Setup script, build script).
- `DevTools/` the test suites and the hardware probes used to work the protocol out.

## How it works

AccessCaster controls the device by sending named-property records over the RODECaster's USB HID
control interface, the same mechanism RODE Central uses, and reads the unit's full settings back
the same way. Pad sounds are ordinary files copied to the storage the device exposes in Transfer
Mode. The protocol was reverse-engineered for this project.

**[docs/PROJECT_GUIDE.md](docs/PROJECT_GUIDE.md)** is the one to read first if you are building
something similar, because the protocol reference below is for looking things up rather than for
reading. Part 1 explains what the protocol turned out to be, how it was worked out, and how the app
is layered; it ends with the method boiled down to six steps for **any** device that is only
configurable through a screen you cannot use. Part 2 is the invariants: the non-obvious rules, and
the settled negatives, so you do not spend a week re-testing something already known to be a dead
end.

**[docs/RODECASTER_PROTOCOL.txt](docs/RODECASTER_PROTOCOL.txt)** is the byte-level reference, and it
is here so that you do not have to do any of this again: USB identity, framing and value encodings,
the property tree and how an address is built, the per-device shapes, channels, routing, faders,
pads and transfer mode, what is NOT on the USB bus at all, and the Pro II / Duo differences
throughout. Everything in it was confirmed on real hardware unless it says otherwise.

**[docs/POSSIBLE_LEADS.txt](docs/POSSIBLE_LEADS.txt)** is what we could NOT settle, written for
somebody who has hardware we do not: a second Pro II, a Duo with a card in it, a wireless
transmitter, a headset for the Duo's 3.5mm jack. Several of them are minutes of work if you have
the right desk and impossible for us. Negatives are worth as much as findings.

Also in `docs/`: the code map, the parity target, the soundpad reference, and a generated list of
every property each desk publishes.

All of it is shared freely. Nothing in it is official, and none of it came from RODE.

## Status

Windows only, for now. A macOS version is wanted rather than scheduled: it needs a Mac to develop
and verify against, and shipping accessibility that has never been tested with VoiceOver would be
worse than not shipping it at all.

The project is developed by a blind user for real daily use, so accessibility and reliability come
first.

## Not affiliated with RODE

This is an independent, community project. It is not made by, endorsed by, or affiliated with RODE
or RODE Microphones. "RODECaster" and "RODE" are trademarks of their respective owner. Use it at
your own risk; it talks to your hardware, so while it has been tested carefully, no warranty is
given.

## License

AccessCaster is free software, licensed under the **GNU General Public License version 3
(GPLv3)**. You are free to use, study, share and modify it. Any version you distribute must also
be free software under the same licence, with its source available. See the [LICENSE](LICENSE)
file for the full text.

Copyright (C) 2026 Shaun Preece.

## Contributing

Issues and pull requests are welcome, especially accessibility feedback from JAWS and Narrator
users, and reports of anything that does not behave on your setup.
