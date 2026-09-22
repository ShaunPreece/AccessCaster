"""Stdlib-only code audit of Project/: syntax, unused imports, and dead top-level functions.

No external linter needed. Run it from anywhere: python DevTools/audit_code.py
- syntax: compiles every .py in Project/
- unused imports: an imported name never referenced in its own file
- dead functions: a top-level def whose name is never referenced anywhere in Project/ OR DevTools/
  (reported as CANDIDATES - CLI entry points / dynamic use can be false positives)

Paths are resolved from this file, never from the current directory: the project root holds no .py
files, so a cwd-relative glob finds nothing and reports "0/0 compiled clean" as a pass.
"""
import ast
import pathlib
import py_compile

REPO = pathlib.Path(__file__).resolve().parent.parent
FILES = sorted((REPO / "Project").glob("*.py"))
# Reference sweep is WIDER than the audit: a Project function used only by a DevTools script (a
# probe, a live test) is in use, not dead, and reporting it would train the reader to ignore this.
REF_FILES = FILES + sorted((REPO / "DevTools").rglob("*.py"))
KEEP_NAMES = {"main", "_selftest"}  # entry points - never "dead"


def refs_in(tree):
    """All names referenced: Name ids (Load) + Attribute attrs (for module.func use)."""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return out


def imports_in(tree):
    """[(bound_name, lineno, text)] for each import binding (skips `import *`)."""
    binds = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                bound = a.asname or a.name.split(".")[0]
                txt = f"import {a.name}" + (f" as {a.asname}" if a.asname else "")
                binds.append((bound, n.lineno, txt))
        elif isinstance(n, ast.ImportFrom):
            if any(a.name == "*" for a in n.names):
                continue
            mod = ("." * (n.level or 0)) + (n.module or "")
            for a in n.names:
                bound = a.asname or a.name
                txt = f"from {mod} import {a.name}" + (f" as {a.asname}" if a.asname else "")
                binds.append((bound, n.lineno, txt))
    return binds


def dunder_all(tree):
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == "__all__":
                    try:
                        return set(ast.literal_eval(n.value))
                    except Exception:
                        return set()
    return set()


trees = {}
print("=== SYNTAX ===")
for f in FILES:
    try:
        py_compile.compile(str(f), doraise=True)
        trees[f] = ast.parse(f.read_text(encoding="utf-8"), str(f))
    except Exception as e:
        print(f"  SYNTAX ERROR {f}: {e}")
print(f"  {len(trees)}/{len(FILES)} compiled clean")

# Reference set for dead-function detection: Project AND DevTools, so a function only a probe or a
# live test calls is not reported as dead.
all_refs = set()
for f in REF_FILES:
    try:
        all_refs |= refs_in(ast.parse(f.read_text(encoding="utf-8"), str(f)))
    except Exception:
        continue                      # a DevTools script that will not parse is not this tool's job

print("\n=== UNUSED IMPORTS (per file) ===")
any_unused = False
for f, t in trees.items():
    used = refs_in(t)
    keep = dunder_all(t)
    for bound, line, txt in imports_in(t):
        if bound == "annotations":      # from __future__ import annotations
            continue
        if bound not in used and bound not in keep:
            print(f"  {f.name}:{line}  unused: {txt}")
            any_unused = True
if not any_unused:
    print("  none")

print("\n=== DEAD TOP-LEVEL FUNCTIONS (candidates) ===")
any_dead = False
for f, t in trees.items():
    for node in t.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
            if name in KEEP_NAMES or name.startswith("__"):
                continue
            if name not in all_refs:
                print(f"  {f.name}:{node.lineno}  never referenced: def {name}()")
                any_dead = True
if not any_dead:
    print("  none")
