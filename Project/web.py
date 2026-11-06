"""The one way AccessCaster reaches the internet: an HTTPS request carrying the app's own
User-Agent. Its callers are the RODE driver download (`driver_install`) and the update check and
download (`selfupdate`), and `test_driver_install` holds the list to those.

CLOUDFLARE ANSWERS THE DEFAULT PYTHON USER-AGENT WITH 403. `urllib` sends "Python-urllib/3.12"
unless told otherwise, and the edge in front of the update bucket refuses that as a bot, so a
request with no agent of its own never reaches the index at all.
"""
import urllib.request

from version import __version__

# Named and versioned, so a server's logs can tell which build asked and a rule can allow it
# deliberately rather than by accident.
USER_AGENT = f"AccessCaster/{__version__}"


def opened(url: str, timeout: float):
    """`urlopen` with AccessCaster's own User-Agent. The caller keeps its own timeout and its own
    size limits: a small index, a 25 MB installer and RODE's driver do not wait or weigh the same."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(request, timeout=timeout)
