"""Enforce the notes rules that a script can settle. Run before finishing a session.

    python DevTools/check_notes.py            report
    python DevTools/check_notes.py --check    report and exit 1 if anything fails

WHY IT EXISTS. The notes rules split into two kinds, and only one kind holds on its own. The
MECHANICAL ones - no em dashes, no ISO dates, headings, Now/Next length - are kept reliably. The
ones that need a THRESHOLD JUDGED are not, because judgement gets stretched in the writer's favour:
a refactor ends up in the FEATURE log, a session runs long with no /clear nudge. So the judged ones
are converted into checks here. Same move as deriving the CodeMap line numbers.

It does NOT check taste. It cannot tell a good invariant from a bad one, and does not try. It
catches the specific ways these files have actually gone wrong before.
"""
from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _bootstrap  # noqa: E402  (finds the notes, which live outside this repo)

REPO = pathlib.Path(__file__).resolve().parent.parent
# THE NOTES ARE IN THE PRIVATE SIBLING REPO, `_Notes\Access Caster\`, so this one can be public.
# CLAUDE.md and README.md stay here, which is why REPO is still needed alongside NOTES_DIR.
NOTES_DIR = pathlib.Path(_bootstrap.notes_path())
MEM = NOTES_DIR / "ProjectMemory"
DOCS = pathlib.Path(_bootstrap.docs_path())      # the PUBLIC reference docs, in this repo
FAILS: list[str] = []
NOTES: list[str] = []


def fail(where: str, msg: str) -> None:
    FAILS.append(f"{where}: {msg}")


def note(msg: str) -> None:
    NOTES.append(msg)


def read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 1. Completed.md is a FEATURE log
# ---------------------------------------------------------------------------
# The failure this catches: a 93-line write-up of a code audit and duplication sweep added as the
# top entry. That is not a feature. The rule says "features and reworks", and "rework" stretches to
# cover a cleanup if nothing stops it.
REFACTOR_WORDS = re.compile(
    r"\b(refactor\w*|duplication sweep|dead code|code audit|cleanup pass|tidy-?up|"
    r"consolidat\w+|merged? (?:the )?(?:two|three|four|\w+) (?:files|helpers|copies))\b", re.I)
# A user-facing entry says what the USER can now do. These are the words that prove it.
USER_WORDS = re.compile(
    r"\b(page|button|shipped|the user|blind operator|by ear|screen reader|NVDA|announce\w*|"
    r"dialog|tab|control|hear|heard|UI|endpoint)\b", re.I)


def check_completed() -> None:
    p = MEM / "Completed.md"
    if not p.exists():
        return fail(p.name, "missing")
    body = read(p)
    entries = re.split(r"^## ", body, flags=re.M)[1:]
    for e in entries:
        title = e.splitlines()[0].strip()
        if REFACTOR_WORDS.search(title) and not USER_WORDS.search(e[:1500]):
            fail(p.name, f'entry reads as a cleanup, not a feature: "{title[:70]}". '
                         f"Its invariant belongs in PROJECT_GUIDE part 2 and its reasoning at the "
                         f"code site; nothing belongs here.")


# ---------------------------------------------------------------------------
# 2. No status column in a reference file
# ---------------------------------------------------------------------------
# PARITY.md is the TARGET, not a tracker. A status column is a hand-maintained derived fact and it
# rotted: 14 features listed as outstanding, 8 of them already built.
STATUS_CODE = re.compile(r"\|\s*(DONE|DECODED|CAPTURE|BLOCKED|TODO|WIP|IN PROGRESS)\b", re.I)


def check_no_status() -> None:
    # PROPERTY_LIST is device-scoped (PROPERTY_LIST_PRO2.md / _DUO.md), so lint whichever exist.
    # THESE TWO LIVE IN THE PUBLIC `docs/`, not in the private notes. Globbing the wrong folder
    # makes this check pass by finding nothing, which is worse than failing.
    for p in [DOCS / "PARITY.md"] + sorted(DOCS.glob("PROPERTY_LIST_*.md")):
        name = p.name
        if not p.exists():
            continue
        hits = STATUS_CODE.findall(read(p))
        if hits:
            fail(name, f"{len(hits)} status marker(s) have crept back in. This file is a reference, "
                       f"not a tracker; what is LEFT to build goes in ProjectNotes.md.")


# ---------------------------------------------------------------------------
# 3. ProjectNotes stays current-only and short
# ---------------------------------------------------------------------------
def check_projectnotes() -> None:
    p = NOTES_DIR / "ProjectNotes.md"
    if not p.exists():
        return fail("ProjectNotes.md", "missing")
    body = read(p)
    lines = body.splitlines()
    # Now / Next past ~150 lines means finished work has piled up in it (measured on Hydra).
    try:
        a = next(i for i, l in enumerate(lines) if l.startswith("## Now / Next"))
        b = next(i for i, l in enumerate(lines[a + 1:], a + 1) if l.startswith("## "))
        if b - a > 150:
            fail("ProjectNotes.md", f"Now / Next is {b - a} lines. Past ~150 means finished work has "
                                    f"collected in it; move it to its home and delete the note.")
    except StopIteration:
        fail("ProjectNotes.md", "no '## Now / Next' section")
    # A session summary reforming under any heading. The rule that failed on its own, twice.
    for bad in ("Last session", "Recently completed", "Session summary", "This session"):
        if re.search(r"^#+.*" + re.escape(bad), body, re.M | re.I):
            fail("ProjectNotes.md", f'"{bad}" heading. No session summaries: Completed.md is '
                                    f"newest-first, so its top entry already is one.")


# ---------------------------------------------------------------------------
# 4. Punctuation and dates, across everything we write
# ---------------------------------------------------------------------------
ISO = re.compile(r"\b20\d\d-[01]\d-[0-3]\d\b")


def check_style() -> None:
    # BOTH sides: an em dash reads just as badly in a public doc as in a private note.
    targets = (sorted(MEM.glob("*.md")) + sorted(DOCS.glob("*.md"))
               + [NOTES_DIR / "ProjectNotes.md", REPO / "CLAUDE.md", REPO / "README.md"])
    for p in targets:
        if not p.exists():
            continue
        body = read(p)
        n = body.count("—") + body.count("–")
        if n:
            fail(p.name, f"{n} em/en dash(es). A screen reader skips or mangles them; use a comma, "
                         f"full stop, colon or plain hyphen.")
        # ISO dates are ambiguous read aloud (07-11 announces as 7th November). Generated files and
        # the protocol spec carry measured timestamps, so they are exempt.
        if "GENERATED FILE" in body[:400]:
            continue
        iso = ISO.findall(body)
        if len(iso) > 20:
            note(f"{p.name}: {len(iso)} ISO dates. New ones should be '30th August 2026'.")


# ---------------------------------------------------------------------------
# 5. Nothing points at a file that no longer exists
# ---------------------------------------------------------------------------
# The failure this catches: deleting a spent plan and leaving 48 references to it, or replacing them
# with a phrase that reads as nonsense ("See earlier measurement for why").
LINK = re.compile(r"`?(?:ProjectMemory/)?([A-Z][A-Z0-9_]{3,})\.md`?")


def check_dead_links() -> None:
    # A REFERENCE DOC IS NOT A DEAD LINK JUST BECAUSE IT IS PUBLIC. The hardware findings live in
    # the repo's `docs/` and the working notes in the private ProjectMemory, so BOTH count as homes
    # or every citation of the protocol guide reads as broken.
    # Archive/ counts too: it is frozen history, and pointing into it is legitimate.
    have = ({p.stem for p in MEM.glob("*.md")} | {p.stem for p in (MEM / "Archive").glob("*")}
            | {p.stem for p in DOCS.glob("*")}
            | {"ProjectNotes", "CLAUDE", "README", "RODECASTER_PROTOCOL"})
    scan = (sorted(MEM.glob("*.md")) + sorted(DOCS.glob("*.md"))
            + sorted((REPO / "Project").glob("*.py"))
            + sorted((REPO / "DevTools").glob("*.py"))
            + [NOTES_DIR / "ProjectNotes.md", REPO / "CLAUDE.md", REPO / "README.md"])
    dead: dict = {}
    for p in scan:
        if not p.exists():
            continue
        # Completed.md is HISTORY: it names files as they were when the entry was written, so a
        # since-deleted name is accurate there and rewriting it would falsify the record.
        if p.name == "Completed.md":
            continue
        for m in LINK.finditer(read(p)):
            name = m.group(1)
            if name not in have:
                dead.setdefault(name, []).append(p.name)
    for name, where in sorted(dead.items()):
        fail("dead link", f"{name}.md no longer exists but is named in: "
                          f"{', '.join(sorted(set(where))[:5])}. State the fact inline and date it.")


def main() -> int:
    check_completed()
    check_no_status()
    check_projectnotes()
    check_style()
    check_dead_links()
    for n in NOTES:
        print("  note: " + n)
    if not FAILS:
        print("check_notes: clean.")
        return 0
    print(f"check_notes: {len(FAILS)} problem(s)")
    for f in FAILS:
        print("  " + f)
    return 1 if "--check" in sys.argv[1:] else 0


if __name__ == "__main__":
    raise SystemExit(main())
