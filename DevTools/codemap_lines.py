"""Regenerate (or check) every number in docs/CodeMap.md from the source itself.

    python DevTools/codemap_lines.py            rewrite the file in place
    python DevTools/codemap_lines.py --check    report drift and exit 1, change nothing

CodeMap carries a line count per file, and a hand-kept number is wrong within the day. A derived
number is derived: like the two coverage reports (property_coverage.py, profile_coverage.py), this
is regenerable, with a --check mode that exits 1 on drift.

WHAT IT TOUCHES, and nothing else:
  * | `file.py` | NNNN |  the line count in the file table
  * "## DevTools/ (NN files..." the file count
The PROSE IS NEVER TOUCHED. Everything CodeMap says about what code MEANS is written by a person
and cannot be derived; only the numbers are mechanical, and only the numbers are rewritten here.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

import _bootstrap

REPO = pathlib.Path(_bootstrap.REPO)
CODEMAP = pathlib.Path(_bootstrap.docs_path("CodeMap.md"))

TABLE_ROW = re.compile(r"^(\|\s*`([A-Za-z_][\w./]*\.(?:py|js|ps1|html|css))`\s*\|\s*)(\d+)(\s*\|)")
DEVTOOLS_COUNT = re.compile(r"(## DevTools/ \()(\d+)( files)")


def tracked_devtools_files() -> int:
    """How many files git tracks directly in DevTools/. Files on disk that git ignores (the probe
    stubs a generator writes) are left out, so the count is the same on every clone."""
    out = subprocess.run(["git", "ls-files", "DevTools"], cwd=REPO, capture_output=True,
                         text=True, check=True).stdout
    return sum(1 for line in out.splitlines() if line.count("/") == 1)


def line_count(path: pathlib.Path) -> int:
    return len(path.read_text(encoding="utf-8", errors="replace").splitlines())


def find_source(name: str) -> pathlib.Path | None:
    """The file a table row names, in Project/, where the file table's rows live."""
    p = REPO / "Project" / name
    return p if p.exists() else None


def rewrite(text: str) -> tuple[str, list]:
    """The corrected CodeMap, plus a list of every number that was wrong."""
    lines = text.splitlines(keepends=True)
    drift = []

    for n, line in enumerate(lines):
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
            real = tracked_devtools_files()
            if int(c.group(2)) != real:
                drift.append(f"line {n + 1}: DevTools says {c.group(2)} files, has {real}")
                lines[n] = DEVTOOLS_COUNT.sub(lambda mm: mm.group(1) + str(real) + mm.group(3),
                                              line, count=1)

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
