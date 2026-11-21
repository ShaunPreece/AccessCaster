"""The native window's source rules that a hidden build cannot show, offline:

  * NO SetFocus IN A DIALOG'S CONSTRUCTOR. A dialog activating focuses its first focusable control;
    a SetFocus in the constructor is a second focus event, and the screen reader reads the control
    twice. Focus is placed by creation order.
  * NO wx.Simplebook TO SWITCH PAGES, and a wx.Notebook only for a channel page's tabs: both move the
    focus into each page they show, which pulls it out of the Categories tree.
  * NO wx.Slider: JAWS reads a native slider as a percent of its range and ignores its value text,
    so every slider is `native.slider.SliderCtrl`.
  * THE PLATFORM LAYER IS IMPORTED IN ONE PLACE EACH: `native.win` only by `native.a11y`,
    `native.announce` and `native.display`, so a port to another platform has one file to
    replace for each.
  * NO SIZE WRITTEN AS BARE PIXELS. The process is DPI aware, so a literal size is that many real
    pixels, half what was meant on a display at 200 percent. Sizes go through `pages.dip` and
    sizer borders through `pages.gap`.

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
WIN_IMPORTERS = {"a11y.py", "announce.py", "display.py"}
SIZER_ADDERS = {"Add", "Insert", "Prepend"}
SIZE_SETTERS = {"SetSize", "SetMinSize", "SetMaxSize", "SetInitialSize", "SetClientSize"}


def _pixel_size(node):
    """Is `node` a literal tuple of numbers, one of them more than a pixel?"""
    return (isinstance(node, ast.Tuple)
            and all(isinstance(e, (ast.Constant, ast.UnaryOp)) for e in node.elts)
            and any(isinstance(e, ast.Constant) and isinstance(e.value, int) and e.value > 1
                    for e in node.elts))


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


focus_in_init, books, win_imports, dialogs, sliders, pixel_sizes = [], [], [], [], [], []
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
        if isinstance(node, ast.Attribute) and node.attr == "Slider" and (
                isinstance(node.value, ast.Name) and node.value.id == "wx"):
            sliders.append(f"{path.name}:{node.lineno}")
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "Notebook" and path.name not in NOTEBOOK_ALLOWED):
            books.append(f"{path.name}:{node.lineno} Notebook")
        if isinstance(node, ast.Call):
            literal = [kw.value for kw in node.keywords if kw.arg == "size"]
            if isinstance(node.func, ast.Attribute) and node.func.attr in SIZE_SETTERS:
                literal += node.args[:1]
            if any(_pixel_size(value) for value in literal):
                pixel_sizes.append(f"{path.name}:{node.lineno}")
            border = (node.args[3] if isinstance(node.func, ast.Attribute)
                      and node.func.attr in SIZER_ADDERS and len(node.args) > 3 else None)
            if isinstance(border, ast.Constant) and isinstance(border.value, int) and border.value:
                pixel_sizes.append(f"{path.name}:{node.lineno}")
        if isinstance(node, (ast.Import, ast.ImportFrom)) and path.name not in WIN_IMPORTERS:
            names = [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            names += [alias.name for alias in node.names]
            if any(n == "native.win" or n.startswith("native.win.") for n in names):
                win_imports.append(f"{path.name}:{node.lineno}")

print("THE NATIVE WINDOW'S SOURCE RULES")
check("no dialog sets the focus from its constructor", focus_in_init, [])
check("no Simplebook, and a Notebook only for a channel page's tabs", books, [])
check("no native wx.Slider", sliders, [])
check("only a11y, announce and display import the platform layer", win_imports, [])
check("no size or sizer border written as bare pixels", pixel_sizes, [])
check("the rules saw the dialogs", len(dialogs) > 10, True)

sys.exit(_testkit.finish())
