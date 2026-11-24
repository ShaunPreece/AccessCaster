"""Windows' `hotkeys`: RegisterHotKey, and the thread that pumps its messages.

RegisterHotKey delivers WM_HOTKEY to the THREAD that registered it, and only while that thread pumps
messages. So `Pump` owns one daemon thread that registers everything and then sits in GetMessage.
Everything from outside is marshalled onto it by posting a message, because registering from
another thread would deliver the hotkeys to that thread instead.

MOD_NOREPEAT is always set. Without it, holding a key fires the action over and over.
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000            # a held key fires ONCE (Windows 7+)

WM_HOTKEY = 0x0312
WM_APP = 0x8000
_WM_APPLY = WM_APP + 1           # posted to the pump: run `apply` again
_WM_STOP = WM_APP + 2            # posted to the pump: unregister everything and end

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
_user32.RegisterHotKey.restype = wintypes.BOOL
_user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.UnregisterHotKey.restype = wintypes.BOOL
_user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                wintypes.UINT, wintypes.UINT]
_user32.GetMessageW.restype = ctypes.c_int
_user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                       wintypes.WPARAM, wintypes.LPARAM]
_user32.PostThreadMessageW.restype = wintypes.BOOL

MODIFIERS = [
    ("ctrl", "Ctrl", MOD_CONTROL),
    ("alt", "Alt", MOD_ALT),
    ("win", "Windows", MOD_WIN),
    ("shift", "Shift", MOD_SHIFT),
]

# THE HOUSE CHORD (`hotkeys`), Ctrl+Alt+Shift: clear of NVDA's Insert and Caps Lock layers, JAWS'
# Insert layer and Narrator's Caps Lock layer.
#
# IT IS NOT CTRL+ALT+WINDOWS, because the pad scheme needs the NUMBER ROW. Ctrl+Alt+Windows is
# commonly refused all nine digits, and Ctrl+Alt+Shift is given every key the app ships on: the six
# letters, the nine digits, Dash and Equals. Ctrl+Alt alone is no good either; 1 to 4 are commonly
# held. `DevTools/probe_free_chords.py` checks which chords a machine grants.
#
# THE HOLDER IS NOT WINDOWS, and the obvious explanation is wrong. Windows does own Win+digit for
# the taskbar, and Ctrl+Win+digit and Alt+Win+digit with it, but Ctrl+Alt+Win+digit is not a Windows
# shortcut at all: an ordinary application can be holding it. So the rule is not "avoid the Windows
# key", it is that a popular chord plus the number row is contested and you cannot know by whom.
# RegisterHotKey refusing is the only authority; do not reason about what "should" be free.
DEFAULT_MODS = ("ctrl", "alt", "shift")


def _key_codes() -> dict:
    codes = {chr(c): c for c in range(ord("A"), ord("Z") + 1)}
    codes.update((chr(c), c) for c in range(ord("0"), ord("9") + 1))
    codes.update({"Dash": 0xBD, "Equals": 0xBB})        # VK_OEM_MINUS / VK_OEM_PLUS
    codes.update((f"F{n}", 0x70 + n - 1) for n in range(1, 13))
    codes.update({
        "Space": 0x20, "Enter": 0x0D, "Backspace": 0x08, "Tab": 0x09,
        "Left arrow": 0x25, "Up arrow": 0x26, "Right arrow": 0x27, "Down arrow": 0x28,
        "Home": 0x24, "End": 0x23, "Page up": 0x21, "Page down": 0x22,
        "Insert": 0x2D, "Delete": 0x2E,
    })
    codes.update((f"Numpad {n}", 0x60 + n) for n in range(10))
    codes.update({"Numpad plus": 0x6B, "Numpad minus": 0x6D, "Numpad star": 0x6A,
                  "Numpad slash": 0x6F})
    return codes


# Virtual-key codes. Dash and Equals are the PHYSICAL keys next to zero on a UK or US keyboard,
# whatever a different layout prints on them.
KEY_CODES = _key_codes()


class Pump:
    def __init__(self, apply, press):
        self._apply = apply
        self._press = press
        self._thread = None
        self._tid = 0
        self._ready = threading.Event()
        self._ids = set()

    def start(self) -> None:
        if self.running():
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def running(self) -> bool:
        return bool(self._tid and self._thread and self._thread.is_alive())

    def on_pump(self) -> bool:
        return self._thread is not None and threading.get_ident() == self._thread.ident

    def request_apply(self) -> None:
        self._post(_WM_APPLY)

    def stop(self) -> None:
        self._post(_WM_STOP)

    def register(self, hotkey_id: int, flags: int, code: int) -> bool:
        if not _user32.RegisterHotKey(None, hotkey_id, flags | MOD_NOREPEAT, code):
            return False
        self._ids.add(hotkey_id)
        return True

    def unregister_all(self) -> None:
        for hotkey_id in self._ids:
            _user32.UnregisterHotKey(None, hotkey_id)
        self._ids.clear()

    def _post(self, message: int, wparam: int = 0) -> None:
        if self.running():
            _user32.PostThreadMessageW(self._tid, message, wparam, 0)

    def _run(self) -> None:
        self._tid = _kernel32.GetCurrentThreadId()
        self._apply()
        self._ready.set()
        msg = wintypes.MSG()
        while True:
            got = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if got in (0, -1):                # WM_QUIT, or an error we cannot recover from
                break
            if msg.message == WM_HOTKEY:
                self._press(int(msg.wParam))
            elif msg.message == _WM_APPLY:
                self._apply()
            elif msg.message == _WM_STOP:
                break
        self.unregister_all()
        self._tid = 0
