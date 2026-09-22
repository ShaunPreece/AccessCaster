"""Generate ReleaseNotes.html from Project/version.py HISTORY.

WHY GENERATED. The installer offers to open the release notes when it finishes, so they have to
ship with the app; and the history already exists, in version.py, which is the single source of
truth for what is in each release. A hand-written ReleaseNotes file beside it is a second copy, and
a second copy diverges from the first within two releases. So this reads the list and writes the
page, and the only place to edit a release note is version.py.

WHY HTML RATHER THAN TEXT. A text file opens as one undifferentiated block: reaching the version
you care about means reading everything above it. HTML gives real headings, so a screen reader user
presses H to jump version to version, the document title is announced when the window opens, and
the page can follow the reader's own light/dark preference. It also opens in the browser, which is
where the app itself lives.

THE PAGE SAYS "RELEASE NOTES" IN ITS TITLE AND ITS HEADING. Headed with the version and introduced
as "what is new" instead, the words somebody would go looking for exist only in the file name.

ACCESSIBILITY IS THE POINT OF THE MARKUP, not decoration: one h1 saying what the page is, the
newest release at h2 with the installed one NAMED as such rather than merely styled, a heading per
change so a release can be walked by subject instead of by paragraph, ordinary paragraphs, a real
<html lang>, and colours taken from prefers-color-scheme so it matches whatever the reader has set.
No script, no webfonts, nothing that needs a network: this page is opened moments after an install
and must work offline and from a Program Files folder.

EVERY RELEASE BEFORE THE NEWEST IS NESTED UNDER ONE h2, so the whole back catalogue is ONE heading
press to skip rather than twenty. Those versions drop to h3 and their changes to h4, which is what
makes the nesting true rather than merely visual: a flat list of sibling h2s would say the older
releases and the current one are peers, and there would be no single heading to jump over.
THE PAGE IS RELEASE NOTES AND NOTHING ELSE. No overview, no getting started: the installer's final
step offers this page as "see what is new", so anything else here is a link that does not match its
destination, and a quick start has to stay reachable long after this page is read once. That lives
in the app's Help & About, which is also the only copy of it - a fact written in two places here
disagrees with itself within two releases.

    python ProjectBuilds/make_release_notes.py [output.html]

Run by ProjectBuilds/build_installer.ps1, which then copies the page BESIDE the exe rather than
letting the spec bundle it: PyInstaller 6 puts datas in an _internal folder, and the
installer's post-install action opens ReleaseNotes.html in the app folder itself.
"""
from __future__ import annotations

import html
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "Project"))

from version import HISTORY, __version__  # noqa: E402

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AccessCaster {version} release notes</title>
<style>
  /* Deliberately minimal, and colour-scheme aware rather than themed: this page is read once,
     often by someone who has just installed the app, and it should look like the rest of their
     system rather than like a brand. */
  :root {{ color-scheme: light dark; }}
  body {{
    font-family: system-ui, "Segoe UI", sans-serif;
    font-size: 1.05rem; line-height: 1.55;
    max-width: 44rem; margin: 0 auto; padding: 1.5rem 1.25rem 4rem;
  }}
  h1 {{ font-size: 1.6rem; }}
  h2 {{ font-size: 1.25rem; margin-top: 2.25rem; }}
  /* Each level smaller than the one above and none smaller than the body, so the nesting is
     visible as well as announced. h3 is a change under the newest release AND a version inside
     the back catalogue, which is why it sits between the two rather than looking like either. */
  h3 {{ font-size: 1.1rem; margin-top: 1.7rem; margin-bottom: 0.2rem; }}
  h4 {{ font-size: 1rem; margin-top: 1.4rem; margin-bottom: 0.2rem; }}
  h3 + p, h4 + p {{ margin-top: 0; }}
  footer {{ margin-top: 3rem; font-size: 0.95rem; }}
</style>
</head>
<body>
<h1>AccessCaster release notes</h1>
<p>What is new in each version, newest first. The one you have just installed is
version {version}. Earlier versions are grouped together further down, so you can skip
them in one go.</p>
{body}
<footer>
<p>AccessCaster is free software by Shaun Preece. Everything here is also in the app itself, under
Help &amp; About.</p>
</footer>
</body>
</html>
"""


EARLIER_HEADING = "Pre-release builds"
EARLIER_INTRO = ("The versions below went to a handful of testers before AccessCaster was public. "
                 "They are kept so you can see what has been fixed and when.")


def _release(version: str, paragraphs, level: int, current: str) -> list[str]:
    """One release: its version heading at `level`, each change one level deeper."""
    # The installed version is NAMED as such in its own heading rather than marked with styling
    # alone: a screen reader reads the heading, not the boldness (WCAG 1.4.1, and it is the one
    # thing on this page somebody actually needs to find).
    suffix = " (the version you just installed)" if version == current else ""
    out = [f"<h{level}>Version {html.escape(version)}{suffix}</h{level}>"]
    for para in paragraphs:
        # AN ENTRY IS (HEADING, BODY). Under one version the changes are separate subjects, and with
        # nothing between them each paragraph blends into the one above: a reader can only walk the
        # release a paragraph at a time and hope to notice where one ends. A heading per change
        # makes each a jump target.
        # A BARE STRING IS STILL RENDERED, so an entry written without a heading cannot fail a
        # build; it simply has nothing to jump to.
        if isinstance(para, (tuple, list)):
            heading, body = para
            out.append(f"<h{level + 1}>{html.escape(heading)}</h{level + 1}>")
        else:
            body = para
        out.append(f"<p>{html.escape(body)}</p>")
    return out


def render(history=HISTORY, current: str = __version__) -> str:
    if not history:
        return ""
    newest, *earlier = history
    parts = _release(newest[0], newest[1], 2, current)
    if earlier:
        # ONE h2 FOR THE WHOLE BACK CATALOGUE, so it can be skipped with a single heading press.
        # Its releases sit at h3 to say they belong to it; no level is skipped on the way down.
        parts.append(f"<h2>{html.escape(EARLIER_HEADING)}</h2>")
        parts.append(f"<p>{html.escape(EARLIER_INTRO)}</p>")
        for version, paragraphs in earlier:
            parts.extend(_release(version, paragraphs, 3, current))
    return "\n".join(parts)


def main() -> int:
    # DEFAULTS TO THE REAL TARGET, not to the current directory. Writing "ReleaseNotes.html"
    # wherever the script is run from drops an untracked copy in the repo root whenever it is run
    # by hand to check the notes render, and `.gitignore` covers only the one in Project/. The
    # build script passes the path explicitly and is unaffected either way.
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Project", "ReleaseNotes.html")
    page = PAGE.format(version=html.escape(__version__), body=render())
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"Wrote {out} ({len(page)} bytes, {len(HISTORY)} releases, current {__version__})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
