"""AccessCaster - assembles index.html from its parts before it is served.

static/index.html carries an INCLUDE marker where the Help & About section belongs, and the
section itself lives in static/help.html. This module splices the two together.

WHY SERVER-SIDE AND NOT A FETCH IN THE BROWSER. The About page's h1 is what the router focuses
when you arrive there, and buildStaticTabs reads that section's panes at startup to build the tab
strip. Both would race a fetch, and the failure mode is a screen reader landing on nothing. A
splice here means the browser receives exactly what it received when the section was written out
inline, so no focus order, tab stop or live region can behave differently.

WHY A MODULE OF ITS OWN. Two things serve this page: server.py and DevTools/ui_probe.py, which
stands up its own small server for the UI checks. If only one of them knew how to splice, the
About page would be missing from every probe run - so the assembly lives in one place and both
call it.

The result is cached on the file's modification times, because the page is served on every window
reload and a dev session edits these files constantly.
"""

from __future__ import annotations

import os

from paths import resource_path

# The marker in index.html. Matched as a whole line's worth of text rather than parsed as HTML: it
# is a comment, so a browser would ignore it if this ever failed to run, which is the safe way for
# it to fail.
INCLUDE_MARKER = "<!-- INCLUDE help.html -->"

_cache: tuple[tuple[float, float], str] | None = None


def _mtimes(index: str, fragment: str) -> tuple[float, float]:
    return (os.path.getmtime(index), os.path.getmtime(fragment))


def render_index() -> bytes:
    """index.html with static/help.html spliced in at the marker, as UTF-8 bytes.

    Raises if the marker is missing rather than serving the page without its Help section: a
    silently helpless About page is far harder to notice than a page that does not load.
    """
    global _cache
    index_path = resource_path("static", "index.html")
    frag_path = resource_path("static", "help.html")
    stamp = _mtimes(index_path, frag_path)
    if _cache is not None and _cache[0] == stamp:
        return _cache[1].encode("utf-8")

    with open(index_path, encoding="utf-8-sig") as fh:
        index = fh.read()
    with open(frag_path, encoding="utf-8-sig") as fh:
        fragment = fh.read()
    if INCLUDE_MARKER not in index:
        raise RuntimeError(
            "static/index.html no longer contains %s, so the Help & About section "
            "(static/help.html) has nowhere to go." % INCLUDE_MARKER)
    html = index.replace(INCLUDE_MARKER, fragment.strip(), 1)
    _cache = (stamp, html)
    return html.encode("utf-8")
