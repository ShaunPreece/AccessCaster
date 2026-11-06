"""
http.py

The one way selfupdate reaches the update server.

CLOUDFLARE ANSWERS THE DEFAULT PYTHON USER-AGENT WITH 403. `urllib` sends
"Python-urllib/3.12" unless it is told otherwise, and the edge in front of the
bucket refuses that as a bot, so a request with no agent of its own never
reaches the index at all.

One opener for the index and the installer both, so the header cannot be right
in one place and missing in the other.
"""

import urllib.request

from version import __version__

# Named and versioned, so the server's logs can tell which build asked and a
# future rule can allow it deliberately rather than by accident.
USER_AGENT = f"AccessCaster/{__version__}"


def opened(url, timeout):
    """`urlopen` with AccessCaster's own User-Agent. The caller keeps its own
    timeout: a small index and a 25 MB installer do not wait the same."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(request, timeout=timeout)
