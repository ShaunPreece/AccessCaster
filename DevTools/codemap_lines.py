"""Regenerate (or check) every number in docs/CodeMap.md from the source itself.

    python DevTools/codemap_lines.py            rewrite the file in place
    python DevTools/codemap_lines.py --check    report drift and exit 1, change nothing

WHY THIS EXISTS. CodeMap carries ~100 line references and a line count per file, and they are the
first thing a session reads to find its way around. Maintained by hand they go stale within the
DAY - measured at 96 of 96 wrong, drifting by up to 755 lines - because one day's work moves
server.py and app.js by hundreds of lines. A note that is confidently wrong costs more than no
note: you jump to line 7724, land in the middle of something else, and have to grep anyway, having
first been misled.

A derived number has to be DERIVED, not hand-kept. Same shape as the two
coverage reports (property_coverage.py, profile_coverage.py): regenerable, with a --check mode that
exits 1 so drift is a thing you are TOLD about rather than something you discover.

WHAT IT TOUCHES, and nothing else:
  * `name` (NNN)          a line reference, in whichever section it sits under (see SECTIONS)
  * | `file.py` | NNNN |  the line count in the file table
  * "## DevTools/ (NN files..." the file count
The PROSE IS NEVER TOUCHED. Everything CodeMap says about what code MEANS is written by a person
and cannot be derived; only the numbers are mechanical, and only the numbers are rewritten here.
"""
from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _bootstrap  # noqa: E402  (the notes live in a private sibling repo, not in this one)

REPO = pathlib.Path(__file__).resolve().parent.parent
CODEMAP = pathlib.Path(_bootstrap.docs_path("CodeMap.md"))

# Which source a `name` (NNN) reference resolves against, keyed by the heading it sits under.
# A section runs until the next heading listed here (or the end of the mapped region).
SECTIONS = [
    ("### server.py landmarks", REPO / "Project" / "server.py"),
    ("## Project/static (the web UI)", REPO / "Project" / "static" / "app.js"),
    ("## DevTools/", None),                      # nothing below here carries line references
]

REF = re.compile(r"(`([A-Za-z_$][\w$]*)`\s*\()(\d{2,5})([,)])")
TABLE_ROW = re.compile(r"^(\|\s*`([A-Za-z_][\w.]*\.(?:py|js|ps1|html|css))`\s*\|\s*)(\d+)(\s*\|)")
DEVTOOLS_COUNT = re.compile(r"(## DevTools/ \()(\d+)( files)")


def py_defs(path: pathlib.Path) -> dict:
    """{name: 1-based line} for every top-level def, class and module constant."""
    out = {}
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = re.match(r"(?:def|class)\s+([A-Za-z_]\w*)", line)
        if not m:
            m = re.match(r"([A-Za-z_]\w*)\s*[:=]", line)
        if m and m.group(1) not in out:
            out[m.group(1)] = i
    return out


def js_defs(path: pathlib.Path) -> dict:
    """{name: 1-based line} for every top-level function and const/let binding."""
    out = {}
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = re.match(r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", line)
        if not m:
            m = re.match(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", line)
        if m and m.group(1) not in out:
            out[m.group(1)] = i
    return out


def defs_for(path: pathlib.Path) -> dict:
    return js_defs(path) if path.suffix == ".js" else py_defs(path)


def line_count(path: pathlib.Path) -> int:
    return len(path.read_text(encoding="utf-8", errors="replace").splitlines())


def find_source(name: str) -> pathlib.Path | None:
    """The file a table row names, looked for where the map says things live."""
    for folder in ("Project", "Project/static"):
        p = REPO / folder / name
        if p.exists():
            return p
    return None


def rewrite(text: str) -> tuple[str, list]:
    """The corrected CodeMap, plus a list of every number that was wrong."""
    lines = text.splitlines(keepends=True)
    drift = []
    source = None                        # which file the current section's references belong to
    cache: dict = {}

    for n, line in enumerate(lines):
        for heading, path in SECTIONS:
            if line.startswith(heading):
                source = path
                break

        # --- the file table's line counts ---
        m = TABLE_ROW.match(line)
        if m:
            src = find_source(m.group(2))
            if src is not None:
                real = line_count(src)
                if int(m.group(3)) != real:
                    drift.append(f"line {n + 1}: {m.group(2)} says {m.group(3)} lines, has {real}")
                    lines[n] = TABLE_ROW.sub(lambda mm: mm.group(1) + str(real) + mm.group(4),
                                             line, count=1)
            continue

        # --- the DevTools file count ---
        c = DEVTOOLS_COUNT.search(line)
        if c:
            real = sum(1 for p in (REPO / "DevTools").iterdir() if p.is_file())
            if int(c.group(2)) != real:
                drift.append(f"line {n + 1}: DevTools says {c.group(2)} files, has {real}")
                lines[n] = DEVTOOLS_COUNT.sub(lambda mm: mm.group(1) + str(real) + mm.group(3),
                                              line, count=1)
            continue

        # --- line references ---
        if source is None:
            continue
        if source not in cache:
            cache[source] = defs_for(source)
        known = cache[source]

        def fix(mm, _n=n, _known=known, _src=source):
            name, claimed = mm.group(2), int(mm.group(3))
            real = _known.get(name)
            if real is None:
                # Not a member of this file: a plain "(2026)" or a name that has been renamed or
                # removed. Left alone and REPORTED, because guessing at it would be worse.
                drift.append(f"line {_n + 1}: {name} ({claimed}) is not in {_src.name} - check by hand")
                return mm.group(0)
            if real != claimed:
                drift.append(f"line {_n + 1}: {name} said {claimed}, is {real}")
            return mm.group(1) + str(real) + mm.group(4)

        lines[n] = REF.sub(fix, line)

    return "".join(lines), drift


def main() -> int:
    check = "--check" in sys.argv[1:]
    text = CODEMAP.read_text(encoding="utf-8")
    fixed, drift = rewrite(text)
    if not drift:
        print("CodeMap.md: every number matches the source.")
        return 0
    print(f"CodeMap.md: {len(drift)} number(s) out of date")
    for d in drift:
        print("  " + d)
    if check:
        print("\n--check: nothing written. Run without --check to correct them.")
        return 1
    CODEMAP.write_text(fixed, encoding="utf-8")
    print(f"\nRewritten. {len(drift)} corrected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
