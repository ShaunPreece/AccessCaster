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
NOW_NEXT_MAX_LINES = 250


def check_projectnotes() -> None:
    p = NOTES_DIR / "ProjectNotes.md"
    if not p.exists():
        return fail("ProjectNotes.md", "missing")
    body = read(p)
    lines = body.splitlines()
    # A long Now / Next is the symptom of finished work piling up in it. The threshold is a smell
    # test, not a budget: this project legitimately carries several open items that each need a
    # mechanism written out, so it sits higher than the 150 the rule was first measured at.
    try:
        a = next(i for i, l in enumerate(lines) if l.startswith("## Now / Next"))
        b = next(i for i, l in enumerate(lines[a + 1:], a + 1) if l.startswith("## "))
        if b - a > NOW_NEXT_MAX_LINES:
            fail("ProjectNotes.md", f"Now / Next is {b - a} lines. Past ~{NOW_NEXT_MAX_LINES} means "
                                    f"finished work has collected in it; move it to its home and "
                                    f"delete the note.")
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
            + sorted((REPO / "Project" / "static").glob("*.js"))
            + sorted((REPO / "DevTools").glob("*.js"))
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


# ---------------------------------------------------------------------------
# 6. No entry written twice
# ---------------------------------------------------------------------------
# The failure this catches: an invariant pasted into a second section, after which one copy is
# corrected and the other is not, so the file states both sides of a settled question. An entry is a
# top-level bullet; its KEY is the bold lead-in, or the first 80 characters when there is none.
ENTRY = re.compile(r"^- (.+)$", re.M)
BOLD = re.compile(r"\*\*(.+?)\*\*")


def check_duplicates() -> None:
    targets = (sorted(DOCS.glob("*.md")) + sorted(DOCS.glob("*.txt")) + sorted(MEM.glob("*.md"))
               + [NOTES_DIR / "ProjectNotes.md"])
    for p in targets:
        if not p.exists() or p.name == "Completed.md" or "GENERATED FILE" in read(p)[:400]:
            continue
        body = read(p)
        # PROJECT_GUIDE's part 1 summarises its worst gotchas on purpose; only part 2 is the record.
        body = body[body.find("# PART 2"):] if "# PART 2" in body else body
        seen: dict = {}
        for m in ENTRY.finditer(body):
            first = m.group(1)
            b = BOLD.match(first)
            key = re.sub(r"\W+", " ", (b.group(1) if b else first[:80])).strip().lower()
            if len(key) < 25:
                continue
            seen[key] = seen.get(key, 0) + 1
        for key, n in seen.items():
            if n > 1:
                fail(p.name, f'entry written {n} times: "{key[:70]}". Keep one and delete the rest.')


# ---------------------------------------------------------------------------
# 7. Code comments describe the code, not its history
# ---------------------------------------------------------------------------
# Comments are written in the present tense with no dates: git holds the story. A date wrapped
# across two comment lines ("4th" / "# September 2026") still matches, because the comment marker
# between the halves is allowed. A date inside quotes is data (a format example, a test
# expectation), not narrative, and is skipped.
_MONTH = ("January|February|March|April|May|June|July|August|September|October|November|"
          "December")
CODE_DATE = re.compile(
    r"(?:\b\d{1,2}(?:st|nd|rd|th)\s*(?:#|;|//|\*)?\s*)?\b(?:" + _MONTH + r"),?\s+20\d\d\b")
NARRATIVE = re.compile(
    r"shaun (?:said|asked|wanted|ruled)|\(Shaun[,:]|it turned out|earlier session|"
    r"we (?:tried|thought|assumed|discovered)|\boriginally\b|\bat the time\b|"
    r"\bused to (?:be|do|keep|check|go|drop|have|say|work|happen|fail)\b", re.I)
# User-facing text, where a date or a credit is content: the release history and the help page.
# This file is exempt too, because its own patterns spell out what they match.
CODE_EXEMPT = {"version.py", "help.html", "check_notes.py"}


def check_code_comments() -> None:
    for folder in ("Project", "DevTools", "ProjectBuilds"):
        for p in sorted((REPO / folder).rglob("*")):
            if (p.suffix not in (".py", ".js", ".ps1", ".iss", ".css", ".html")
                    or p.name in CODE_EXEMPT or "dist" in p.parts or "build" in p.parts):
                continue
            body = read(p)
            for rx in (CODE_DATE, NARRATIVE):
                for m in rx.finditer(body):
                    if m.start() and body[m.start() - 1] in "\"'":
                        continue
                    line = body.count("\n", 0, m.start()) + 1
                    fail(f"{p.relative_to(REPO).as_posix()}:{line}",
                         f'"{m.group(0)}" reads as history. Say what is true now, in the present '
                         f"tense, with no date.")


# ---------------------------------------------------------------------------
# 8. Public files never cite a private one
# ---------------------------------------------------------------------------
# The notes are not in this repo, so a citation of one is a dead link for everyone else. DevTools is
# not scanned: its generators write their reports into the notes and name them as their output.
PRIVATE_NAME = re.compile(
    r"ProjectNotes|ProjectMemory|Completed\.md|DUO_SUPPORT|PROFILE_COVERAGE|PROPERTY_COVERAGE|"
    r"RodeDocs")


def check_private_citations() -> None:
    files = [p for p in sorted((REPO / "Project").rglob("*"))
             if p.is_file() and p.suffix in (".py", ".js", ".html", ".css", ".ps1")
             and "dist" not in p.parts and "build" not in p.parts]
    files += sorted(DOCS.glob("*.md")) + sorted(DOCS.glob("*.txt")) + [REPO / "README.md"]
    for p in files:
        if not p.exists():
            continue
        body = read(p)
        for m in PRIVATE_NAME.finditer(body):
            line = body.count("\n", 0, m.start()) + 1
            fail(f"{p.relative_to(REPO).as_posix()}:{line}",
                 f'cites the private "{m.group(0)}". State the fact instead.')


def main() -> int:
    check_completed()
    check_no_status()
    check_projectnotes()
    check_style()
    check_dead_links()
    check_duplicates()
    check_code_comments()
    check_private_citations()
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
