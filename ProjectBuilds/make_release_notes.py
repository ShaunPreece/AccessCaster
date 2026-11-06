"""Generate release-notes.txt, the newest release's notes, from Project/version.py HISTORY.

WHY GENERATED. The history already exists, in version.py, which is the single source of truth for
what is in each release. A hand-written notes file beside it is a second copy, and a second copy
diverges from the first within two releases. So this reads the list and writes the file, and the
only place to edit a release note is version.py.

TWO READERS, ONE FILE. The installer shows it on a page of its own once the files are in place
(`InfoAfterFile`), and the update publisher (UpdateKit) puts it in the signed index, from which the
update dialog reads it aloud before the user decides. Its first line names the version, which the
publisher checks against the installer it is publishing: the file is rewritten by every build, and
the output folder keeps older installers.

UTF-8 WITH A BYTE ORDER MARK, because Inno Setup reads a text file without one in the system's
ANSI code page, which mangles any character outside it. The publisher's version check skips the
mark, since it tries every word of the first line.

    python ProjectBuilds/make_release_notes.py [release-notes.txt]
"""
from __future__ import annotations

import os
import sys

BUILDS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(BUILDS), "Project"))

from version import HISTORY, __version__  # noqa: E402


def text_notes(history=HISTORY) -> str:
    """The newest release as plain text: a line naming the version, then each change as its heading
    on one line and its body on the next, with a blank line between changes. No bullet markers, which
    a screen reader reads aloud at the start of every line."""
    version, paragraphs = history[0]
    lines = [f"What is new in AccessCaster {version}"]
    for para in paragraphs:
        heading, body = para if isinstance(para, (tuple, list)) else ("", para)
        lines.append("")
        if heading:
            lines.append(heading)
        lines.append(body)
    return "\n".join(lines) + "\n"


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BUILDS, "release-notes.txt")
    if HISTORY[0][0] != __version__:
        # The installer would show, and the publisher refuse, another release's notes.
        print(f"HISTORY starts at {HISTORY[0][0]}, not {__version__}: no release notes written")
        return 1
    with open(out, "w", encoding="utf-8-sig") as f:
        f.write(text_notes())
    print(f"Wrote {out} (version {__version__})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
