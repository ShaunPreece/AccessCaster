"""
phrases.py

The update feature's sentences, in one place, so the wording is reviewed
together and unit-tested rather than scattered across the tray, `app_updates`
and the window.

The tone rule this file follows: a screen reader is always talking, so lots of
short sentences melt into one. A long sentence carrying the thing that matters
stands out.

A version is always "version 2.64.0", never "v2.64.0", which a screen reader
reads as "volt".
"""

# The key that checks now. THE SPOKEN SENTENCES NAME IT, because it is one
# press from anywhere in the window. F12 is the same key in Hydra and Claudio, so it is
# learnt once rather than per app.
KEY = "F12"

# The tray menu's two labels. The menu offers the install directly once a
# version is waiting, so the item says what pressing it will do.
TRAY_CHECK = "Check for &updates"


def tray_install(version):
    return f"Install &update to version {version}"


# The title of every Windows notification this feature raises.
NOTIFICATION_TITLE = "AccessCaster update"


def available(version):
    """The unprompted notice, raised as a Windows notification from the tray
    icon, so it neither moves focus nor talks over the screen reader in the
    app's own voice.

    IT NAMES BOTH WAYS TO ACT, because a notice nobody can act on is just
    noise, and the page may not be open when it arrives."""
    return (f"AccessCaster version {version} is available. Press {KEY} in the "
            "AccessCaster window, or choose Install update from its tray "
            "menu, to hear what changed and install it.")


def confirm_title(version):
    """The decision dialog's title, which is also its accessible name. A
    question, because it is a decision."""
    return f"Install AccessCaster version {version}?"


def confirm_summary(running, index, today):
    """The line the dialog's description reads first: what you are on, what is
    on offer and how old it is. The two version numbers come first because
    somebody who already knows what is in the release wants to press the
    button, not listen to it."""
    return (f"You are running AccessCaster version {running}. Version "
            f"{index.version} was released {age(index.released, today)}.")


# Under the release notes, so it is the last thing read before the buttons.
CONFIRM_AFTER = ("Installing closes AccessCaster for a moment, runs the "
                 "installer and starts it again. Shortcut keys and "
                 "announcements pause until it is back. Nothing on the desk "
                 "changes.")


def age(released, today):
    """How long ago a release was, in words somebody can picture. Exact dates
    make a reader do arithmetic; "three weeks ago" is the thing they actually
    wanted to know.

    A date in the FUTURE reads as today rather than as "in 3 days": an index
    written in another time zone can legitimately be a few hours ahead, and a
    release cannot be forthcoming."""
    days = (today - released).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{_cardinal(days)} days ago"
    if days < 14:
        return "last week"
    if days < 60:
        return f"{_cardinal(days // 7)} weeks ago"
    return f"{_cardinal(days // 30)} months ago"


# Said by the page when a check it asked for has not answered within half a
# second. A check against the real bucket answers in well under that, so the
# usual outcome is one sentence, the answer, rather than two; only a slow or
# dead connection reaches this, which is when silence reads as the app
# ignoring the key.
CHECKING = "Checking for an AccessCaster update."

# Said while the installer downloads. Progress rather than an outcome: a wait
# nobody acknowledges is indistinguishable from a dead app.
DOWNLOADING = "Downloading the update. This can take a minute."

# Said while the bar stops moving for the hash of the downloaded installer.
# NOT OPTIONAL: the one wait that shows no movement must not look like the one
# that has hung.
CHECKING_STAGE = "Checking the download."

# The last thing said before AccessCaster closes, once every control has been
# taken away. A button under a sentence that says the app is about to close
# itself reads as the step that does it, whatever it is called.
READY_STAGE = ("The update is ready. AccessCaster will now close, install it "
               "and start again. Please wait.")

# The user pressed Cancel. The offer still stands: the next check finds the
# same version and asks again.
CANCELLED_DOWNLOAD = ("The update was cancelled. AccessCaster has not "
                      f"changed, and {KEY} offers it again whenever you want "
                      "it.")

# After the desk's own sentence naming a change part-way through, which an
# update must not interrupt: the installer closes the app as Windows does at
# shutdown. The first refuses before the download, the second once it is ready.
AFTER_DESK_WORK = "Install the update once it finishes."
AFTER_DESK_WORK_KEPT = ("Install the update again once it finishes. The "
                        "download is kept.")

# An install that failed to start and gave no reason of its own.
COULD_NOT_START = "The update could not start."

# A download that did not finish.
COULD_NOT_DOWNLOAD =("Could not download the update. Nothing has changed, "
                      "and AccessCaster will try again another day.")

# The downloaded file did not match the checksum in the index.
#
# A SEPARATE SENTENCE FROM A FAILED DOWNLOAD, and it must stay separate. A
# truncated download is ordinary; a complete file that hashes wrong is the one
# case this whole signing scheme exists to catch, and calling it a network
# problem would hide it.
WRONG_CHECKSUM = ("The downloaded update did not match what AccessCaster "
                  "expected, so it has not been run. This can mean the "
                  "download was damaged. Nothing has changed.")

# Said only for a check the USER asked for. An automatic check that fails says
# nothing, but a button that answers with silence reads as the app ignoring
# it.
COULD_NOT_CHECK = ("Could not check for an AccessCaster update just now. "
                   "Your connection or the update server may be unavailable.")

# The build has no usable public key, so there is nothing to check an answer
# against. Honest rather than silent: a button that does nothing is
# indistinguishable from a broken app.
CANNOT_CHECK = "This build of AccessCaster cannot check for updates."

# A check or a download is already running, so a second press does not start
# another.
ALREADY_CHECKING = "AccessCaster is already checking for an update."

# The offer the page asked to install is no longer the one held, which only a
# check landing between the dialog opening and Install being pressed can do.
OFFER_CHANGED = ("The update on offer has changed since this was opened. "
                 f"Press {KEY} to see the current one.")

def not_back(version, minutes):
    """Said by the progress dialog when this process is still running
    `minutes` after the installer started, which should have closed it.
    Setup can still show a message of its own under /VERYSILENT, and one
    waiting for an answer is the likeliest reason."""
    return (f"AccessCaster is still running {_cardinal(minutes)} minutes after "
            f"the installer for version {version} started, so the update has "
            "not installed. Setup may be showing a message of its own; if "
            "not, choose Install update again.")


def up_to_date(running):
    """Said for a user-asked check that found nothing."""
    return (f"You are running AccessCaster version {running}, which is the "
            "latest version.")


def skipped(version):
    """Confirms a skip, and says how to get it back, because a choice with no
    visible way out is one people are right to be wary of making."""
    return (f"AccessCaster will not mention version {version} again. {KEY} "
            "still offers it.")


def just_updated(now):
    """Said on the first launch after an update the app installed itself. The
    confirmation has to come from the other side of the restart, because the
    process that started the installer is gone by the time it finishes."""
    return f"AccessCaster has updated to version {now}."


def update_did_not_take(running, installer_path):
    """Said instead when the restart came back on the version it started on:
    the installer ran and nothing changed.

    NAMES THE FILE, because the installer is still on disk and running it by
    hand is the way out. Silence here would leave somebody believing they had
    updated when they had not."""
    return (f"The update did not install, and AccessCaster is still on "
            f"version {running}. The installer was downloaded and is still at "
            f"{installer_path}, if you want to run it yourself.")


def settings_status(how_often, last_checked, skipped, today):
    """The hint under the page's How often dropdown: how often AccessCaster
    looks, when it last did, any version being skipped, and the way to look
    now. Not an announcement; it describes the dropdown and is rewritten
    whenever that changes.

    `how_often` is `interval.describe`, passed in rather than imported, and
    `last_checked` is a date or None."""
    looked = (f" It last looked {age(last_checked, today)}."
              if last_checked else " It has not looked yet.")
    # A skipped version is named, because it is the one reason a check can
    # honestly find nothing while a newer version exists.
    turned_down = f" You chose to skip version {skipped}." if skipped else ""
    return (f"AccessCaster looks for a new version {how_often}.{looked}"
            f"{turned_down} Check for updates now, or {KEY} anywhere in the "
            "AccessCaster window, looks now, whatever this is set to.")


_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven",
          "eight", "nine", "ten", "eleven", "twelve"]


def _cardinal(n):
    """Small numbers as words, which sit better in a spoken sentence; past
    twelve the digits are easier to take in than the words."""
    return _WORDS[n] if 0 <= n < len(_WORDS) else str(n)
