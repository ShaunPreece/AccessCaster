"""AccessCaster application version. SINGLE SOURCE OF TRUTH.

Bump `__version__` ONLY when Shaun explicitly says so. It NEVER changes automatically
(not per build, not per commit). Everything that shows or stamps a version reads from
here so there is exactly one number to change:
  - the server:    /api/version (and /api/status) -> the Help & About page's version line.
  - the build:     ProjectBuilds/build_installer.ps1 reads it and names the installer
                   Output/AccessCasterSetup-v<version>.exe.
  - the installer: ProjectBuilds/installer.iss takes it as the /DAppVer define and sets
                   AppVersion + the output filename from it.
  - the release notes: ProjectBuilds/make_release_notes.py turns HISTORY below into the page
                   the installer offers to open when it finishes.

Format: plain "MAJOR.MINOR" string, e.g. "2.00". The UI says "version 2.50" in words: never "v2.50",
which a screen reader reads as "volt 2.50".

HISTORY IS DATA, NOT PROSE, and that is the whole point of it being a list rather than a paragraph.
The release notes are GENERATED from it, so there is no second copy to fall out of step: a
hand-written ReleaseNotes file beside this one diverges within two releases.

Write each entry for a USER, not for the project: what they can now do, what stopped going wrong,
and anything they must know about their own settings. Newest first.

A CORRECTION TO THE APP'S OWN HELP EARNS NO ENTRY. Release notes say
what CHANGED for the reader; a help page that now describes the app accurately is the help page
doing its job, and listing it tells them only that it used to be wrong. Same for a typo, a reworded
hint or a tidied label. If the fix came with a real change in what the app does, the entry is about
the change.
"""
from __future__ import annotations

__version__ = "2.53"

# (version, [paragraph, ...]). Each paragraph is one <p> in the release notes, in order.
HISTORY = [
    ("2.53", [
        ("The routing screen works again after the unit's 1.7.6 update",
         "RODE's firmware 1.7.6 broke the Routing page. It stopped showing your mix and said the "
         "matrix had changed shape, as though a caller were connected, and the fader levels lost "
         "their reference points with it. Nothing was actually wrong with your unit or your "
         "settings. AccessCaster was counting the unit's cross-points in a way that also swept up a "
         "second, unrelated grid belonging to RODECaster Sync, and 1.7.6 made that grid bigger to "
         "support the RODECaster Video. AccessCaster now counts only the grid it means, so the page "
         "reads correctly and will not be thrown by RODE adding another Sync device later. This "
         "affects the Duo the same way, so take this update before you take 1.7.6."),
    ]),

    ("2.52", [
        ("AccessCaster is public",
         "AccessCaster version 2.52 released to public. Thanks to Michael Babcock and Steven Scott "
         "for testing and feedback."),

        ("The headphone knobs tell you where they are",
         "The headphone level knobs on the unit have no notch or marker, so there is no way to tell "
         "by touch how far up one is set. AccessCaster now reads the level out as you turn one, the "
         "same way it does a fader. It follows your level announcement setting, so if you have "
         "those switched off the knobs stay quiet too."),

        ("One Backups page, and one list instead of two",
         "The Profiles page is now the Backups page, and it has one list instead of two. Before, "
         "your own saved profiles and the unit's own backups sat in separate sections with separate "
         "buttons, and nothing told you which of them would actually put your desk back. Now one "
         "button backs the unit up and one list holds everything you can restore from. Underneath, "
         "the unit keeps its own complete copy on its card or drive whenever there is one, and this "
         "computer always keeps a copy of your settings and pads, so a backup survives the drive "
         "being pulled, erased or filled. Restoring uses this computer's copy, for the reason in "
         "the next note."),

        ("Restoring no longer asks the unit to load its own backup",
         "The unit can crash when it restores its own copy of a backup. I measured it over a full "
         "day on my own Pro 2, with the app recording every command it sent: restoring the unit's "
         "own copy crashed it five times in thirty-one attempts, across three different backups. "
         "Restoring from this computer's copy did not fail once in nine. When it crashes, audio "
         "stops, every control stops answering, "
         "and unplugging the USB cable is not enough - the power lead has to come out, because the "
         "unit itself has stopped rather than the connection. If your screen reader plays through "
         "the RODECaster, you lose your speech until it is back, which is why this was worth "
         "changing rather than warning about. So the Restore button now always uses this "
         "computer's copy. It takes about two minutes instead of a few seconds and restores a "
         "little less. Backing up has not changed: the unit still makes its own copy alongside, "
         "because backing up has never caused this. Everything points to the unit rather than "
         "anything the app does wrong, though so far that is one desk on one computer, so I am not "
         "claiming it as settled. I am still working on it; if a safe way is found, the faster "
         "route will come back. Two places can still reach it, and you have to choose them "
         "deliberately: resetting the unit to factory settings, and restoring a backup the unit "
         "made on its own with no copy on this computer beside it. The dialog tells you when that "
         "is the case."),

        ("Backups the unit made itself are in the same list",
         "Backups made on the unit itself, at its touchscreen or in RODE's software, appear in the "
         "same list marked as made on the unit. They cannot be renamed, because the unit names its "
         "own and offers no way to change that. There is no copy of one on this computer, so "
         "restoring it has to ask the unit to load it - the one case that can still crash it, and "
         "the dialog says so before you press anything."),

        ("Deleting a backup clears it from the unit too",
         "Deleting a backup now removes both copies, this computer's and the unit's, so you are no "
         "longer left with a large file on the drive you believed you had deleted. The as-found "
         "backup still asks twice before it goes, since it is the only record of how your desk was "
         "before this app touched it."),

        ("Nothing you have saved needs converting",
         "Everything you have already saved is still there and still works. Your profiles appear in "
         "the new list without anything needing to be converted or saved again."),

        ("Mix presets now say which outputs they change",
         "A mix preset, on the Routing page, remembers where every sound is going: which sources "
         "each output carries, whether each one follows its fader, and the level of any you have "
         "set by hand. Applying one puts those mixes back in a single "
         "press. The problem was that some presets rewrite every output on the desk and others "
         "touch a single one, and nothing in the list told you which you were about to press. Now "
         "every preset ends with its answer. \"Every output\" means the whole desk goes back to "
         "what that preset holds, including any mix you have set by hand somewhere else. A named "
         "output, such as \"Chat only\", means that one mix changes and nothing else is "
         "touched. Thanks to Michael Babcock, who reported that there was no way to tell them "
         "apart."),

        ("You can save a preset for one output on its own",
         "Until now, saving a preset always captured the whole desk. There is a second save "
         "button on the Routing page which saves just the output you have chosen at the top of the "
         "tab, and its label names that output so you can hear what it will do before you press "
         "it. A single-output preset is the only kind that cannot disturb a mix you are currently "
         "listening to, which makes it the one worth putting on a keyboard shortcut for use "
         "mid-show. Presets you saved before this still cover every output, because that is what "
         "they have always done, and they now say so in the list."),

        ("A log of what the app has done",
         "The tray menu has a Diagnostic log entry. It opens a plain text record of what "
         "AccessCaster has sent to the unit and what came back. You do not need it day to day; it "
         "is there for when something goes wrong and you want to say what actually happened rather "
         "than describe it from memory."),
    ]),
    ("2.51", [
        ("The app is one word now: AccessCaster",
         "The app is now called AccessCaster, one word, wherever its name appears: the Start Menu, "
         "Installed apps, the window, and its own pages. Nothing you have saved is affected - your "
         "profiles, presets, keyboard shortcuts and settings are all kept where they always were and "
         "are not touched. If you had it set to start when you sign in, that still works, and this "
         "version tidies away the startup entry the old name left behind so it cannot start twice."),

        ("Build a mix minus by hand, without the touchscreen",
         "Custom routing now offers the source that matches the output you are configuring: USB 2 "
         "in the USB 2 mix, Chat in Chat, and so on. That switch was hidden, which made it "
         "impossible to build a mix minus by hand in the app - you had to use the unit's "
         "touchscreen. Untick it to take a computer's own audio back out of the feed that returns "
         "to it, so a second machine is audible in your headphones without hearing itself. Thanks to "
         "Michael Babcock, who found this and reported it in enough detail to fix it the same day."),

        ("It tells you the state of the desk when it opens",
         "The app opens on Channels and Processing rather than the Device page, and tells you the "
         "desk's state out loud instead: anything muted or soloed, a voice-effect pad left switched "
         "on, a recording already running, a drive nearly full, an output off its usual mix, and "
         "which SMART pad bank you are on. On a clean desk it is one sentence. There is a switch "
         "for it under Device, Announcements."),

        ("Mute can be a tone instead of a sentence",
         "Mute can play a tone instead of being spoken, for anyone who finds a sentence arrives too "
         "late: two notes falling for muted, two rising for unmuted. It is off by default and lives "
         "beside the other announcement settings. Thanks to Steven Scott, whose idea it was."),

        ("The window says which build you are running",
         "The window opens on a heading that names the app and its version, so you can always hear "
         "which build you are running, with the connected desk announced underneath it."),

        ("It names the right USB socket when it cannot find the unit",
         "If the app cannot find your unit it now tells you which socket the cable belongs in: the "
         "one nearest the power inlet. The other USB socket carries audio only, and a cable there "
         "looks connected while nothing can be controlled. The same is true after a factory reset, "
         "which occasionally leaves the unit off the USB connection until you unplug that cable and "
         "plug it back in; the reset warning and the Help page now say so, with RØDE's own recovery "
         "procedure if a replug is not enough."),

        ("Two things about your own settings",
         "Two things worth knowing about your own settings. Units are now identified by their USB "
         "serial number, which does not change, rather than by a value that turned out to move when "
         "the unit is reset - so the app no longer loses track of which desk it is talking to. If "
         "you had mic presets showing as applied, that record starts fresh. And Help has a new "
         "section on what to do if the unit stops responding."),
    ]),
    ("2.50", [
        ("Pro 2: the random dead microphone, fixed",
         "THE PRO II: a random dead microphone, found and fixed - a software factory reset followed "
         "by a show import left the channel silent with every control reading correct, which had "
         "been written off as a glitch twice. Restoring the unit's own backup works properly. Any "
         "SMART pad can be played from the keyboard, and controls no longer go dead while a pad "
         "plays. Global keyboard shortcuts, which can now be backed up with everything else. Web UI "
         "authentication. WAV or MP3 as a real control. Help & About rewritten. Dropdowns that "
         "change the audio wait for an Apply, so arrowing through one no longer changes the sound. "
         "Mute and solo for the virtual faders. Stereo linking two mic inputs. Wireless mic mute, "
         "and two side effects the desk used to perform silently are now spoken. Plugging a drive "
         "in says \"Recording now available\" instead of \"Recording stopped\"."),

        ("Duo: everything the Pro 2 can do",
         "THE DUO REACHED FULL PARITY: it holds every capability the Pro II does. Its mic gain and "
         "line level actually do something (they wrote a dead address for two months), it reads its "
         "inputs correctly, and it gained the Device settings page, the network page both ways, "
         "stereo linking, virtual fader and monitor levels, moving a channel to another fader, the "
         "Recording page and the desk's own show backups. Nobody has yet witnessed a Duo record or "
         "write a show; both need storage in that desk."),
    ]),
    ("2.10", [
        ("The desk's own backups arrived",
         "The desk's own backups (export, restore, delete and a software factory reset), a profile "
         "restore that offers CLEAN or MERGE, wifi scanning with a picker, the Recording page and "
         "its multitrack controls, a writable Network page, auto gain, the per-channel level check, "
         "mix schemes, the four-tab Routing page and the six-tab Device page."),

        ("Under the surface: a real parser for the desk's model",
         "Under it: the model dump is read by a real TREE PARSER and every write addresses by tree "
         "path. The SMART pads bus is a channel now, so its processing can be set and a profile "
         "carries it. Fixes: a profile restore wrote the mic gain before the mic type and lost it; "
         "a pad backup could start before the pad volume was readable; backup dates, recording "
         "times and several controls that fell out of the tab order."),
    ]),
    ("2.00", [
        ("RODECaster Duo support",
         "RODECaster Duo support added (full Pro II + Duo parity). First versioned release."),
    ]),
]
