"""
indexfile.py

The index document: the one small file per track that AccessCaster reads to decide
whether it is out of date. It says which version is current, where to get it,
what it should hash to, what changed, and when it was released.

The file names and the read cap are shared with the publishing tool and with
every other app reading the same scheme, and copied from the publishing tool.

PARSED STRICTLY, AND NONE RATHER THAN AN EXCEPTION. These are bytes off the
network: an index that is truncated, half-written by a failed upload, or from
a later version of the format must end the check quietly, because a failed
check is not the user's problem to hear about. The caller treats None exactly
as it treats a connection that died.
"""

import json
from datetime import datetime

from .versions import UpdateVersion


FILE_NAME = "index.json"
SIGNATURE_FILE_NAME = "index.json.sig"

# The index and its signature are fetched on every check, so each is read
# under a cap rather than measured afterwards: copying a whole stream and then
# checking its length consumes an endless response in full before anything
# notices. A real index is well under 4 KB; this is generous enough that a
# long set of release notes is never what breaks an update.
MAX_BYTES = 256 * 1024


class UpdateIndex:
    """What one track's index file says.

    A REPLAYED OLD INDEX IS NOT A HOLE. Someone who could put files in the
    bucket could serve yesterday's index and its matching signature, but the
    only thing that buys them is AccessCaster deciding it is already up to date:
    versions are only ever compared for NEWER, so a rollback cannot be offered
    as an update."""

    __slots__ = ("version", "url", "sha256", "notes", "released")

    def __init__(self, version, url, sha256, notes, released):
        self.version = version
        self.url = url
        self.sha256 = sha256
        self.notes = notes
        self.released = released

    def __repr__(self):
        return f"UpdateIndex({self.version!r}, {self.url!r})"


def parse(data):
    """Read an index, or None if it is not one."""
    try:
        document = json.loads(bytes(data).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(document, dict):
        return None

    version = UpdateVersion.parse(_text(document.get("version")))
    if version is None:
        return None

    # HTTPS IS REQUIRED, and it is checked HERE rather than at the fetch. The
    # index names its own download URL, so an index that could say "http://"
    # would hand an attacker on the network the installer download even though
    # the index itself arrived safely.
    url = _text(document.get("url"))
    if not url or not url.lower().startswith("https://"):
        return None

    sha256 = _text(document.get("sha256"))
    if not _looks_like_a_sha256(sha256):
        return None

    # An index with no release notes is refused: them being read before the
    # decision is the point of the whole feature.
    notes = _text(document.get("notes"))
    if not notes:
        return None

    released = _date(_text(document.get("released")))
    if released is None:
        return None

    # Anything else in the document is IGNORED rather than fatal, so an index
    # from a later format still reads here. That matters most to the users
    # furthest behind, who are exactly the ones this feature is for.
    return UpdateIndex(version, url, sha256.lower(), notes, released)


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _looks_like_a_sha256(hex_text):
    """64 hex characters. Checked rather than assumed, because the comparison
    against a downloaded file is the last thing standing between the user and
    running an unknown exe: a hash field that is empty or truncated must fail
    the index, not quietly match nothing."""
    return (len(hex_text) == 64
            and all(c in "0123456789abcdefABCDEF" for c in hex_text))


def _date(iso):
    """The release date. strptime rather than date.fromisoformat, which since
    Python 3.11 also accepts compact forms like "20261024" and week dates; the
    scheme pins this field to one shape and reading a second one here is how
    the two implementations start to differ."""
    try:
        return datetime.strptime(iso, "%Y-%m-%d").date()
    except ValueError:
        return None
