"""Offline: every top-level module that picks a platform's half takes the same names from Windows'
and from macOS's, and each half defines them. Read from the source, never imported, so a Mac half
is checked on Windows and the other way round.

    python DevTools/test_platform_parity.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import _bootstrap  # noqa: F401
import _testkit
from _testkit import check

PROJECT = Path(__file__).resolve().parent.parent / "Project"


def _branch(test) -> str | None:
    """"win32" or "darwin" for an `if sys.platform == "..."` test, else None."""
    if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Attribute)
            and test.left.attr == "platform" and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)):
        return test.comparators[0].value
    return None


def platform_imports(tree) -> dict:
    """{platform: {name used here: (module, name there)}} from the module's top-level if-chain.
    A final `else` that defines functions supplies them for every other platform, under "else"."""
    found = {}
    for node in tree.body:
        while isinstance(node, ast.If):
            platform = _branch(node.test)
            if platform is None:
                break
            names = found.setdefault(platform, {})
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom):
                    for alias in stmt.names:
                        names[alias.asname or alias.name] = (stmt.module, alias.name)
            rest = node.orelse
            node = rest[0] if len(rest) == 1 and isinstance(rest[0], ast.If) else None
            if node is None and rest:
                found["else"] = {stmt.name: (None, stmt.name) for stmt in rest
                                 if isinstance(stmt, ast.FunctionDef)}
    return found


def defined(module: str) -> set:
    path = PROJECT.joinpath(*module.split(".")).with_suffix(".py")
    names = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
    return names


chosen = 0
for path in sorted(PROJECT.rglob("*.py")):
    rel = path.relative_to(PROJECT).as_posix()
    if rel.startswith(("win/", "mac/", "native/win/", "native/mac/")) or "__pycache__" in rel:
        continue
    imports = platform_imports(ast.parse(path.read_text(encoding="utf-8")))
    if "win32" not in imports or not any(m.startswith(("win.", "native.win."))
                                         for m, _n in imports["win32"].values()):
        continue
    chosen += 1
    win, mac = imports["win32"], imports.get("darwin") or imports.get("else", {})
    check(f"{rel}: macOS supplies what Windows does", sorted(mac), sorted(win))
    for platform, names in (("win32", win), ("darwin", mac)):
        missing = [f"{module}.{name}" for module, name in names.values()
                   if module is not None and name not in defined(module)]
        check(f"{rel}: every {platform} name is defined", missing, [])

check("the platform modules were found", chosen >= 17, True)

for half, other in (("mac", "win"), ("win", "mac")):
    for path in sorted(PROJECT.joinpath(half).glob("*.py")):
        text = path.read_text(encoding="utf-8")
        check(f"{half}/{path.name} takes nothing from {other}/",
              f"from {other} import" in text or f"from {other}." in text, False)

print()
sys.exit(_testkit.finish())
