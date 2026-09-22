"""Which modifier chord will Windows actually GIVE us for the number row?

WHY. The pad scheme wanted Ctrl, Alt and Windows with 1 to 8, on the reasoning that the house chord
was already Ctrl+Alt+Windows and the number row was unused. All eight were refused. Dash and Equals
on the same chord were accepted, so it is the DIGITS specifically.

ASKS THE ONLY AUTHORITY THAT COUNTS, and that is the point of the tool rather than a detail of it.
`RegisterHotKey` returning false is a refusal WITHOUT A REASON: it does not say who holds the
combination, and the obvious guess is often wrong. Ctrl+Alt+Win+digit looked like Windows keeping
its taskbar shortcuts, and it was actually another of Shaun's own programs, a webcam controller
using that chord for camera presets. There is no list to consult instead, so do not reason about
what "should" be free - run this.

READ ONE CAVEAT INTO EVERY RESULT: a combination THIS APP is already holding reads as taken. Stop
the server before trusting a "refused" for a key our own shortcuts use.

Registers and immediately unregisters, so nothing is taken from anything while this runs.

    python DevTools/probe_free_chords.py
"""
from __future__ import annotations
import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401  (puts Project/ on sys.path)

import hotkeys

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
_user32.RegisterHotKey.restype = wintypes.BOOL
_user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.UnregisterHotKey.restype = wintypes.BOOL

CHORDS = [
    ("ctrl", "alt", "win"),
    ("ctrl", "alt", "win", "shift"),
    ("ctrl", "alt", "shift"),
    ("ctrl", "alt"),
    ("ctrl", "shift"),
    ("ctrl", "win"),
    ("alt", "shift"),
    ("alt", "win"),
    ("ctrl", "win", "shift"),
]
# The keys the shipped scheme wants: the five original letters, the pad number row, the two bank
# keys and P for "what is playing". Override from the command line to ask about others.
KEYS = (["A", "M", "R", "S", "O", "P"] + [str(n) for n in range(1, 10)]
        + [hotkeys.KEY_DASH, hotkeys.KEY_EQUALS])


def free(mods, key: str) -> bool:
    flags, vk, _tidy, problem = hotkeys.parse_binding(list(mods), key)
    if problem:
        return False
    if not _user32.RegisterHotKey(None, 0xBEEF, flags, vk):
        return False
    _user32.UnregisterHotKey(None, 0xBEEF)
    return True


def main() -> int:
    if len(sys.argv) > 1:
        KEYS[:] = sys.argv[1:]
    for mods in CHORDS:
        got = [k for k in KEYS if free(mods, k)]
        missing = [k for k in KEYS if k not in got]
        name = hotkeys.combo_text(mods, "N")
        print(f"  {name:38s} free: {len(got):2d}/{len(KEYS)}"
              + (f"   REFUSED: {', '.join(missing)}" if missing else "   ALL FREE"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
