"""
check.py

Whether a fetched index means there is an update to offer, and where a track's
files live.

PURE, and separate from the fetching, so every rule below is a unit test
rather than something proved by pointing the app at a real server once. The
rules are small and all of them are the kind that fail silently in the field:
an off-by-one in the comparison shows up as a user never being offered
anything, which looks exactly like no release having happened.
"""

from . import indexfile
from .versions import UpdateVersion

# Where the index files and installers live. One bucket, app then track
# inside, so the download link is permanent and the version lives in the index
# rather than in a filename.
ROOT = "https://updates.slightperception.com/"

# AccessCaster's folder in the bucket. The publishing tool knows the same name.
APP = "accesscaster"

# The running version is the current one, or newer. Says nothing.
UP_TO_DATE = "up to date"

# A newer version exists and the user has not turned this one down. The ONLY
# verdict that speaks of its own accord.
AVAILABLE = "available"

# A newer version exists and it is the exact one the user chose to skip.
# Silent, which is the entire point of the skip: a notice that comes back is a
# notice that nags.
SKIPPED = "skipped"

# The index could not be fetched, could not be read, or did not verify.
#
# SILENT, and that is a decision rather than laziness. A user cannot act on
# "the update server did not answer", the next check costs nothing, and a
# failure that speaks turns a week offline into a week of being told so. The
# one exception is a check the user ASKED for, where silence would read as the
# app ignoring the button.
COULD_NOT_CHECK = "could not check"


class CheckResult:
    """The answer, with the index that produced it. `index` is None when
    nothing could be read."""

    __slots__ = ("verdict", "index")

    def __init__(self, verdict, index=None):
        self.verdict = verdict
        self.index = index

    @property
    def offer(self):
        """The version to offer, or None. Non-null exactly when the verdict is
        AVAILABLE."""
        return self.index if self.verdict == AVAILABLE else None


def decide(running, index, skipped):
    """What to do about an index. `running` is the version actually running,
    `index` what the index file said or None if it could not be read or
    verified, and `skipped` the version string the user chose to skip."""
    if index is None:
        return CheckResult(COULD_NOT_CHECK)

    # A build whose own version cannot be read is a broken build, not an
    # out-of-date one. Offering an update on the strength of "we do not know
    # what this is" would be the one way this feature could talk somebody into
    # replacing a working app for no reason.
    if running is None:
        return CheckResult(COULD_NOT_CHECK)

    if not index.version > running:
        return CheckResult(UP_TO_DATE, index)

    # THE SKIP IS FOR ONE EXACT VERSION, not "newer than this". The next
    # release is a new decision: somebody who turned down 1.29.0 because they
    # were mid-task has not turned down 1.29.1, and a skip that swallowed
    # every later version would quietly opt them out for good. Compared as a
    # VERSION rather than as text, so a stored "1.29.0+abc123" still matches.
    turned_down = UpdateVersion.parse(skipped)
    if turned_down is not None and turned_down == index.version:
        return CheckResult(SKIPPED, index)

    return CheckResult(AVAILABLE, index)


def track_for(running):
    """Which track a running build follows: "beta" for a pre-release, "stable"
    otherwise.

    READ OFF THE VERSION, not stored as a setting. A tester who installs the
    stable release stops being offered betas by the act of installing it, with
    nothing to remember to change, and there is no way for a setting to
    disagree with the build it is sitting in."""
    return running.track if running is not None else "stable"


def index_url(running, root=ROOT, app=APP):
    """Where this build's index lives, e.g.
    https://updates.slightperception.com/accesscaster/stable/index.json."""
    return f"{root}{app}/{track_for(running)}/{indexfile.FILE_NAME}"


def signature_url(running, root=ROOT, app=APP):
    """The same address with .sig on the end. Built from the track rather than
    written out twice, so a stable build cannot be pointed at the beta folder
    by a typo in one of two constants."""
    return f"{root}{app}/{track_for(running)}/{indexfile.SIGNATURE_FILE_NAME}"
