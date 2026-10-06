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

__version__ = "2.62"

# (version, [paragraph, ...]). Each paragraph is one <p> in the release notes, in order.
HISTORY = [
    ("2.62", [
        ("Dropdowns apply on Enter",
         "Every dropdown that changes the unit now works the same way: arrowing through it only "
         "browses, Enter applies the choice you are on, and moving away without Enter puts it back "
         "and says so. The Apply buttons beside the fader, output mix, headphone, USB, recording and "
         "mic type dropdowns are gone, and the pad and Unit settings dropdowns no longer change "
         "anything just because you paused on an option. With a mouse, picking an option applies "
         "it straight away."),

        ("A simpler Keyboard shortcuts tab",
         "Your shortcuts are now one list. Arrow through it, press Enter to change a shortcut's "
         "key, Delete to delete it, or the Applications key for a menu that also turns it off or "
         "silences it. Every shortcut is held on the same keys, Control, Alt and Shift unless you "
         "change them with Change the keys to hold, so adding a shortcut only asks what it does "
         "and which key. Use the SMART pad keys adds or removes the pad keys as a set, and Restore "
         "default shortcuts puts the list back as a new install has it."),

        ("Expanded connects reliably",
         "RODE's Virtual Devices driver often failed to connect when Expanded was switched on, so "
         "Game, Music, A and B never appeared. The cause was anything playing through the unit's "
         "Main output at that moment, your screen reader included: the driver needs that output to "
         "itself. While the unit reconnects in Expanded, AccessCaster now moves Windows' sound to "
         "another output, such as your computer's own speakers, for a few seconds, then on to "
         "System (RODECaster) once the driver has connected. RODE's driver announces the connection "
         "itself, so the app no longer repeats it."),

        ("The fader list follows Expanded",
         "After switching Expanded on or off, the Faders tab now offers the right channels straight "
         "away, Game, Music, A and B included, without reloading the page."),

        ("CallMe in the output mixes",
         "On a RODECaster Pro II, CallMe 1 now has its own row in the output mixes on the Routing "
         "page, so a caller's voice can be kept out of a headphone mix or the recording."),

        ("Extend a call without the touchscreen",
         "A call on RODE's free and standard plans ends after an hour unless it is extended. In its "
         "last five minutes, Extend the call on the CallMe page gives it another hour, and so does "
         "the answer shortcut, Control, Alt and Shift with C, when nothing is ringing. Switch on "
         "Extend calls automatically and AccessCaster does it for you at the five-minute warning."),

        ("CallMe contacts",
         "Give a CallMe code a name and it becomes a contact, with its own Call, Edit and Delete "
         "buttons. Each contact can have its own keyboard shortcut, and a call to a contact is "
         "spoken by name. If a contact's code has changed, which happens when they switch incoming "
         "calls off and on, the app says so. Contacts are saved with the app's own settings, so "
         "they travel with a settings export."),

        ("More about the call you are on",
         "The call details now include how long the call has been connected, and a call nobody "
         "picks up is announced as nobody answering rather than as a timeout."),

        ("The CallMe channel on the Channels page",
         "CallMe 1 now has its own Level, mute and processing on the Channels page, like any other "
         "input, while it is on a fader."),

        ("Pan on stereo channels",
         "Pan on a stereo channel, such as USB, Chat, Bluetooth or CallMe, did nothing, because the "
         "unit pans those differently. A stereo channel now has two sliders, one for where each "
         "side sits, and a switch that links them into a single Pan control. A mic keeps its one "
         "Pan slider, which now says left or right as you move it."),

        ("Announcements can stay off after a restart",
         "Announcements now always come back on when AccessCaster starts, so it never opens in "
         "silence. If you would rather they stayed off, tick Start with announcements the way I "
         "left them, on the Announcements tab, and the app starts the way you left it, "
         "whether you switched them from the page, the tray or the keyboard shortcut. If you had "
         "already switched them off from the tray, that is kept and the box comes ticked."),

        ("The page notices when the app restarts",
         "If AccessCaster stops while its window is open, the window now tells you, and says when "
         "it is back. After an update the window reloads itself, so you are never left using the "
         "old page with the new app."),

        ("The firmware check says when you are up to date",
         "Checking for new firmware on the Device page now tells you plainly when the unit already "
         "has the latest, rather than only naming the latest version."),

        ("Daylight saving is off the Device page",
         "The unit stores the setting but only its own touchscreen ever applies it, so the switch "
         "here did nothing. It still comes back with a backup."),
    ]),
    ("2.61", [
        ("CallMe: take guests' calls without the touchscreen",
         "RODE's CallMe lets a guest join your show from another RODECaster or from a web browser, "
         "over the unit's own internet connection, and until now every part of it was on the "
         "touchscreen. It has its own page now, CallMe, on Alt plus 5. Switch on Accept incoming "
         "calls and the page gives you the unit's code, which another RODECaster dials, and a guest "
         "link with a copy button, which anyone else opens in a browser. When someone calls, you "
         "hear who it is wherever you are, and Answer takes the call. The second button is Decline "
         "while a call is ringing and Hang up once you are talking, and the details of a connected "
         "call, how fast the sound is arriving and how much is lost, are listed below them. You can "
         "also call another RODECaster by typing its code. The page shows your plan and the "
         "minutes left."),

        ("Two new keyboard shortcuts for calls",
         "Control, Alt and Shift with C answers a call, and with D declines a ringing call or hangs "
         "up the one you are on, from any program. A new install has them already. If you have set "
         "up your own shortcuts, add them on the Device page under Keyboard shortcuts, from the "
         "CallMe kind."),

        ("You are told before a call reaches the plan's hour",
         "On RODE's free and standard plans a call ends after an hour unless it is extended. "
         "AccessCaster now tells you five minutes before. Extending still has to be done on the "
         "unit's screen."),

        ("The page shortcuts moved up by one",
         "CallMe took Alt plus 5, so Backups is now Alt plus 6, Device Alt plus 7, and Help and "
         "About Alt plus 8."),

        ("Leaving Expanded no longer leaves four dead faders",
         "Switching what this computer sends back to Standard used to leave Game, Music, A and B "
         "on their faders, where nothing could feed them. They now come off, as the unit's own "
         "screen does it, with their processing kept for when you switch back. A unit already in "
         "that state is tidied when AccessCaster starts, and it tells you so. The summary spoken at "
         "start-up also names those four properly instead of calling them by number."),

        ("Eject and erase the card or drive",
         "The Recording page has a new section, The card and drive. Eject one before you pull it "
         "out, and reconnect it from the same button without unplugging it. Erase deletes "
         "everything on it, after asking you first. The unit's own storage, where your pad sounds "
         "live, is never offered."),

        ("Find out when there is new firmware",
         "The Device page, under Hardware, can ask RODE whether there is newer firmware for your "
         "unit, and the summary at start-up says so when there is. Installing it is still done on "
         "the unit's screen or with RODE's app, because an update restarts the unit and your "
         "computer's sound may be going through it."),

        ("Less to tab past on the Recording page",
         "With no recordings on the unit, the delete controls are hidden, and with no card or drive "
         "in, the card and drive section is a single line."),
    ]),
    ("2.60", [
        ("Four more inputs from this computer, each on its own fader",
         "Normally your computer looks to the unit like one stereo input, so everything Windows "
         "plays arrives on a single channel already mixed together. RODE's Expanded mode splits it "
         "into four separate inputs instead, named Game, Music, A and B, and AccessCaster can now "
         "switch it on and put each of them on a fader. Music, a call, a game and your screen "
         "reader then arrive as four channels rather than one, so you can fade or mute any of them "
         "on its own, keep one out of what a caller hears, or record them apart. It is on the "
         "Routing page under Master Mix, as \"What this computer sends\". One thing to know, and "
         "it is the unit's own rule rather than ours: each of the four appears in Windows only "
         "while that channel is sitting on a fader, so turning Expanded on is half the job."),

        ("Nine separate inputs for recording and streaming",
         "Expanded together with \"Every channel, post-fader\" gives this computer nine separate "
         "inputs: your microphones, the pads, the call channels and the four from this computer, "
         "each its own device in Windows and pickable in anything that lets you choose a "
         "microphone. That is the setup for recording everything apart and fixing the balance "
         "afterwards, rather than having to get it right on the night."),

        ("AccessCaster can fetch RODE's driver for you",
         "Expanded needs RODE's Virtual Devices driver on this computer. If it is missing, a button "
         "now appears on the Routing page that downloads RODE's own file, checks it really is "
         "signed by RODE, and runs their installer, which asks Windows for permission in the usual "
         "way. There is a second button beside it that just opens RODE's software page if you would "
         "rather do it by hand, and that one stays once the driver is in, because it is also how "
         "you repair or reinstall it. This download is the only thing AccessCaster does that "
         "connects to the internet, and it only ever happens when you press that button: never when "
         "the app starts, never on a timer, and never to check for a newer version."),

        ("The output mixes cover the new inputs, and so do your mix presets",
         "Game, Music, A and B are sources in the routing matrix and the monitor mix like any "
         "other, so you can include or exclude them per output and set how loud each one is in your "
         "own headphones. Mix presets save and restore them too, whichever mode the unit happened "
         "to be in when you saved. \"Chat = my mic only\" now leaves them out as well, which "
         "matters more than it sounds: in Expanded your screen reader arrives on one of those four, "
         "so without this it would have gone down the line to a caller."),

        ("Turning announcements off now silences all of them",
         "About half the app's speech did not come from the announcer's own reading of the unit, so "
         "switching announcements off left those still talking: a USB mode change would speak to "
         "you with announcements turned off. The switch is now enforced in the one place every "
         "spoken phrase passes through, so off means off."),

        ("A Back Channel pad no longer narrates what you just asked for",
         "One press of a mixer pad moves four channel states at once, both members' mute and their "
         "listen, and a momentary pad brings a second burst when you let go. The announcer spoke "
         "all eight, so a two second talkback push became eight announcements describing the state "
         "you had just chosen. A mixer pad now quietens the mute and listen lines for a moment "
         "around its own press. Censor and Ducking pads get it too."),

        ("The pad editor had Momentary and Latching the wrong way round",
         "Choosing Latching for a mixer pad set it to Momentary and the other way about, so a pad "
         "you set up in the app behaved as the opposite when you pressed it. If you have a mixer "
         "pad that never felt right, set its trigger mode again."),

    ]),

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
