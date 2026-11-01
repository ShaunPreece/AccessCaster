"""
interval.py

How often AccessCaster looks for a new version of itself, as a number of DAYS,
with 0 meaning never.

Days rather than hours: a release happens every week or two, so a rung
measured in hours would only ever mean "every launch".

NEVER IS OFFERED, AND IT ONLY STOPS THE AUTOMATIC CHECK. Check for updates
now still works, on the page and in the tray menu. An update mechanism a user
cannot switch off is one that decides for them, which is the opposite of the
rule this whole feature is built on.
"""

# No automatic check. Zero rather than a magic negative, so settings.json
# reads honestly and no arithmetic on it can produce a timer that fires
# continuously.
NEVER = 0

# Once a day. Often enough that nobody sits weeks behind, which is the problem
# this feature exists for, and far less often than a release actually happens,
# so it costs one small request a day.
DEFAULT = 1

# (days, dropdown label, fragment for `describe`), IN THE ORDER THE DROPDOWN
# SHOWS THEM. The page's options carry the days as their values.
#
# No "every launch" rung: AccessCaster usually runs in the tray for days, so
# launches are not a period a user can reason about. The app asks `is_due`
# every hour while it runs, so a launch after a day away checks at once.
#
# NEVER's fragment says only how OFTEN. The settings sentence it ends up in
# names the menu item separately, for every rung rather than only this one.
_PERIODS = (
    (DEFAULT, "Once a day", "once a day"),
    (7, "Once a week", "once a week"),
    (30, "Once a month", "once a month"),
    (NEVER, "Only when I ask", "only when you ask it to"),
)

CHOICES = [days for days, _label, _fragment in _PERIODS]
LABELS = [label for _days, label, _fragment in _PERIODS]
_FRAGMENTS = {days: fragment for days, _label, fragment in _PERIODS}


def normalise(days):
    """The stored value, or DEFAULT if it is not one we offer. Anything
    unknown resolves to the default rather than to NEVER: a settings file
    written by a later version with more rungs must not silently leave this
    user never hearing about a fix."""
    try:
        days = int(days)
    except (TypeError, ValueError):
        return DEFAULT
    return days if days in CHOICES else DEFAULT


def is_due(days, last_checked, now):
    """Has enough time passed? `last_checked` and `now` are epoch seconds, and
    `last_checked` is None or 0 when AccessCaster has never checked, which always
    means yes.

    A last-checked time in the FUTURE also means yes. It is what a clock
    correction or a restored backup leaves behind, and treating it as "not
    yet" would stop this user checking until the date caught up, possibly
    never."""
    period = normalise(days)
    if period == NEVER:
        return False
    try:
        last = float(last_checked or 0)
    except (TypeError, ValueError):
        return True
    if last <= 0 or last > now:
        return True
    return now - last >= period * 24 * 3600


def describe(days):
    """The period as a sentence fragment following "AccessCaster looks for a new
    version", so the settings hint reads as a statement rather than a label.
    NEVER says what DOES happen instead of only what does not: a user who
    hears "never" alone has been told the feature is off and not how to use
    it."""
    return _FRAGMENTS[normalise(days)]
