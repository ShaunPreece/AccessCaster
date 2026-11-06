"""
checker.py

Fetches a track's index and its signature, checks the signature, and says
whether there is an update to offer.

THE ORDER IS FETCH, VERIFY, THEN PARSE, and it is not interchangeable.
Parsing first would mean running a JSON reader over bytes from an
unauthenticated source and then deciding whether to believe them; verifying
first means nothing unsigned is ever interpreted. The parse itself is careful
either way, but the cheapest way to not have a parser bug matter is to not
reach the parser with hostile input.

EVERY FAILURE IS THE SAME SILENT ANSWER. A connection that died, a 404, a
signature that does not verify and an index from a future format all come back
as COULD_NOT_CHECK. The real reason goes to the log; the user is not told,
because none of it is theirs to act on and the next check costs nothing. The
one exception is a check the user asked for, which says so rather than
answering with silence.
"""

import diagnostics
import web

from . import check as checks
from . import indexfile, signing

# Short on purpose: this runs in the background at startup and nothing waits
# on it, so a server that is slow to answer is given up on rather than kept
# alive for minutes.
TIMEOUT = 15

def log(line):
    diagnostics.event("update", line)


def fetch(url, cap=indexfile.MAX_BYTES):
    """Read a small file, or None for every failure, so the caller has one
    case to handle rather than a taxonomy it would only ever log."""
    try:
        with web.opened(url, TIMEOUT) as response:
            # The declared length is checked first where there is one, so an
            # oversized file is refused before it is read at all. It is only a
            # claim, which is why the read below is capped too.
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > cap:
                log(f"{url}: declares {declared} bytes, over the {cap} cap")
                return None
            # READ WITH A LIMIT rather than read() then measure: reading it
            # all first means a server answering with an endless body is
            # consumed into memory in full before anything notices. One byte
            # of headroom, so a file exactly at the cap passes and anything
            # larger is REFUSED rather than silently truncated; a truncated
            # index would simply fail to verify, reporting a tampering-shaped
            # failure for a file that is merely too big.
            data = response.read(cap + 1)
    except Exception as exc:
        log(f"{url}: {type(exc).__name__}: {exc}")
        return None
    if len(data) > cap:
        log(f"{url}: more than {cap} bytes")
        return None
    return data


def run(running, skipped, get=None, public_key_hex=None,
        root=checks.ROOT, app=checks.APP):
    """Check the track this build follows. `running` is the version actually
    running, `skipped` the version the user chose not to hear about again.

    `get` and `public_key_hex` are injected by the tests: the shipped private
    key is offline by design, so the happy path can only be exercised against
    a key pair the test generated itself."""
    # Nothing to check against, so do not ask. Asking and then refusing every
    # answer would look identical to a server that is down, and would hide the
    # real reason in the log.
    if not signing.is_configured(public_key_hex):
        log("no public key is built in, so no request was made")
        return checks.CheckResult(checks.COULD_NOT_CHECK)

    get = get or fetch

    index = get(checks.index_url(running, root, app))
    if index is None:
        return checks.CheckResult(checks.COULD_NOT_CHECK)

    signature = get(checks.signature_url(running, root, app))
    if signature is None:
        return checks.CheckResult(checks.COULD_NOT_CHECK)

    if not signing.verify(index, signature, public_key_hex):
        log("the index did not match its signature, so it was not read")
        return checks.CheckResult(checks.COULD_NOT_CHECK)

    document = indexfile.parse(index)
    if document is None:
        log("the index verified but could not be read")
    result = checks.decide(running, document, skipped)
    log(f"{result.verdict}: ours is {running or 'unreadable'}"
        + (f", theirs is {document.version}" if document else ""))
    return result
