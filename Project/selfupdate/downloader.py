"""
downloader.py

Fetches the installer named by a verified index and proves it is the right one
before anything can run it.

THE HASH IS TAKEN FROM THE FINISHED FILE ON DISK, not computed while
streaming. Hashing the bytes as they go past proves what arrived on the wire;
hashing the file afterwards proves what is actually sitting there, which is
what will be executed. Between the two sits antivirus, which really does
quarantine, truncate and replace freshly downloaded executables. Re-reading
the file costs well under a second and is the only version of this check that
tests the thing it is about to run.
"""

import hashlib
import os

import web

from .checker import log

# The installer is on disk and its SHA-256 matches the index.
READY = "ready"

# It did not finish: no connection, a server error, a connection that died
# part way. Ordinary, and the next check tries again.
COULD_NOT_DOWNLOAD = "could not download"

# It finished, and hashed to something other than what the signed index said.
# Either the file was damaged in transit or it is not the file we were
# promised. Nothing is run.
WRONG_CHECKSUM = "wrong checksum"

# The user asked for it to stop. Separate from a failure, because nothing has
# gone wrong and the offer is still good: the next check finds the same
# version and asks again.
CANCELLED = "cancelled"

# Generous on purpose: nobody is waiting on this, and a short timeout turns a
# slow connection into a user who can never update. It is a per-read timeout
# rather than a budget for the whole transfer, so a working connection is
# never cut off part way.
TIMEOUT = 60

_BLOCK = 1 << 20


class Watcher:
    """The stage changes a caller wants to hear about while a download runs,
    and the one question the download asks back. The default answers nothing
    and refuses nothing, so a caller with no interface to drive passes none of
    this.

    `checking` marks the hash of a file, a wait long enough to need
    explaining, or it reads as the one that has hung. An installer already on
    disk is checked first, so a retry can go checking, downloading, checking.
    """

    def downloading(self):
        """The bytes are about to be fetched."""

    def checking(self):
        """The bytes are in and the file on disk is about to be hashed."""

    def cancelled(self):
        """Asked once per block and once more before the file is handed
        back, so a cancel during the hash is still honoured."""
        return False


class _Stopped(Exception):
    """Unwinds the read loop on a cancel. Private: a cancel is an outcome
    rather than a fault, and it never leaves this module."""


def file_name_for(version):
    """The installer's name on disk. NAMED AFTER THE VERSION, though the one
    in the bucket deliberately is not: the bucket needs a permanent link,
    while a user who has to run one of these by hand needs to be able to tell
    which is which. The same shape the build script produces, so the two are
    recognisably the same file."""
    return f"AccessCasterSetup-v{version}.exe"


def download(index, folder, open_url=None, watcher=None):
    """Download the installer into `folder` and check it. Returns
    (outcome, path), where path is set only for READY.

    Raises nothing. Everything that can go wrong here is ordinary - no
    network, a full disk, a folder that cannot be written - and none of it is
    worth faulting the app over when the answer is to try again tomorrow."""
    open_url = open_url or _open_url
    watcher = watcher or Watcher()
    target = os.path.join(folder, file_name_for(index.version))

    try:
        os.makedirs(folder, exist_ok=True)

        # ALREADY HERE AND ALREADY RIGHT: say so rather than fetching it
        # again. This is the retry path after an install that failed, which is
        # exactly when the user is least willing to wait for a second download
        # of a file they already have.
        if os.path.isfile(target):
            watcher.checking()
            if _hash_matches(target, index.sha256):
                log(f"already downloaded and matching: {target}")
                return _ready_unless_stopped(target, watcher)

        # A PART FILE, RENAMED ONLY ONCE IT HAS PASSED. A half-written
        # download left under the final name would be picked up by the line
        # above, and worse, offered to the user as "the installer is still on
        # disk, run it by hand" after a failure.
        part = target + ".part"
        watcher.downloading()
        try:
            _write_to(index.url, part, open_url, watcher)
        except _Stopped:
            log("the download was cancelled")
            _delete(part)
            return CANCELLED, None
        except Exception as exc:
            log(f"download failed: {type(exc).__name__}: {exc}")
            _delete(part)
            return COULD_NOT_DOWNLOAD, None

        watcher.checking()
        if not _hash_matches(part, index.sha256):
            log(f"{part} did not match the checksum in the index")
            _delete(part)
            return WRONG_CHECKSUM, None

        # Replaced rather than moved onto: a leftover from an earlier attempt
        # would make the move fail, and the file we are holding is the one
        # that has just passed.
        os.replace(part, target)
        log(f"downloaded and verified: {target}")
        return _ready_unless_stopped(target, watcher)
    except OSError as exc:
        log(f"download failed: {type(exc).__name__}: {exc}")
        return COULD_NOT_DOWNLOAD, None


def _ready_unless_stopped(target, watcher):
    """The last chance to stop. The hash of a large file runs for a second or
    so with the bar still, which is exactly when a cancel gets pressed, and a
    cancel answered by starting the installer anyway would be the worst
    possible reading of the button."""
    if watcher.cancelled():
        log("the download was cancelled after it had been checked")
        return CANCELLED, None
    return READY, target


def _open_url(url):
    return web.opened(url, TIMEOUT)


def _write_to(url, path, open_url, watcher):
    with open_url(url) as response, open(path, "wb") as out:
        while True:
            if watcher.cancelled():
                raise _Stopped()
            block = response.read(_BLOCK)
            if not block:
                break
            out.write(block)


def _hash_matches(path, expected):
    """Does the file on disk hash to what the index promised? Compared
    case-insensitively against a hash the index has already lower-cased, which
    is belt and braces rather than redundancy: this is the comparison that
    decides whether an executable runs, and a case mismatch silently failing
    EVERY update is a worse failure than comparing one extra way."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(_BLOCK), b""):
                digest.update(block)
    except OSError:
        # Unreadable counts as not matching. A file we cannot hash is a file
        # we must not run.
        return False
    return digest.hexdigest().lower() == (expected or "").lower()


def _delete(path):
    """Best-effort. A file we cannot remove is not worth failing an update
    over, and the part file is overwritten by the next attempt anyway."""
    try:
        os.remove(path)
    except OSError:
        pass
