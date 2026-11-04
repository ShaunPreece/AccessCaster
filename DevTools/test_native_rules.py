"""The native window's source rules that a hidden build cannot show, offline:

  * NO SetFocus IN A DIALOG'S CONSTRUCTOR. A dialog activating focuses its first focusable control;
    a SetFocus in the constructor is a second focus event, and the screen reader reads the control
    twice. Focus is placed by creation order.
  * NO wx.Simplebook TO SWITCH PAGES, and a wx.Notebook only for a channel page's tabs: both move the
    focus into each page they show, which pulls it out of the Categories tree.
  * THE PLATFORM LAYER IS IMPORTED IN ONE PLACE EACH: `native.win` only by `native.a11y` and
    `native.announce`, so a port to another platform has one file to replace for each.

Run: python DevTools/test_native_rules.py
"""
from __future__ import annotations

import ast
import pathlib
import sys

import _bootstrap
import _testkit
from _testkit import check

NATIVE = pathlib.Path(_bootstrap.PROJECT) / "native"
FILES = sorted(p for p in NATIVE.glob("*.py"))
NOTEBOOK_ALLOWED = {"channels_section.py"}
WIN_IMPORTERS = {"a11y.py", "announce.py"}


def _dialog_classes(tree):
    """Classes deriving, directly or through another class in the same file, from a Dialog."""
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}

    def is_dialog(node, seen=()):
        for base in node.bases:
            name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
            if "Dialog" in name or (name in classes and name not in seen
                                    and is_dialog(classes[name], (*seen, name))):
                return True
        return False
    return [node for node in classes.values() if is_dialog(node)]


def _calls(node, name):
    return [n.lineno for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == name]


focus_in_init, books, win_imports, dialogs = [], [], [], []
for path in FILES:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for cls in _dialog_classes(tree):
        dialogs.append(cls.name)
        for item in cls.body:
            if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                focus_in_init += [f"{path.name}:{line} {cls.name}" for line in
                                  _calls(item, "SetFocus")]
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "Simplebook":
            books.append(f"{path.name}:{node.lineno} Simplebook")
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "Notebook" and path.name not in NOTEBOOK_ALLOWED):
            books.append(f"{path.name}:{node.lineno} Notebook")
        if isinstance(node, (ast.Import, ast.ImportFrom)) and path.name not in WIN_IMPORTERS:
            names = [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            names += [alias.name for alias in node.names]
            if any(n == "native.win" or n.startswith("native.win.") for n in names):
                win_imports.append(f"{path.name}:{node.lineno}")

print("THE NATIVE WINDOW'S SOURCE RULES")
check("no dialog sets the focus from its constructor", focus_in_init, [])
check("no Simplebook, and a Notebook only for a channel page's tabs", books, [])
check("only a11y and announce import the platform layer", win_imports, [])
check("the rules saw the dialogs", len(dialogs) > 10, True)

sys.exit(_testkit.finish())
