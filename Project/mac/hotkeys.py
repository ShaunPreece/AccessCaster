"""A STAND-IN for macOS's global hotkeys: the pump runs, and the system refuses every combination.

The modifier labels and flags are macOS's own (Carbon's cmdKey, shiftKey, optionKey and controlKey).
The key codes are the key names themselves until the Mac's virtual key codes are filled in: a code
is only ever handed back to `Pump.register`.
"""
from __future__ import annotations

import threading

MODIFIERS = [
    ("ctrl", "Control", 0x1000),
    ("alt", "Option", 0x0800),
    ("win", "Command", 0x0100),
    ("shift", "Shift", 0x0200),
]


def _key_names() -> list:
    keys = [chr(c) for c in range(ord("A"), ord("Z") + 1)]
    keys += [chr(c) for c in range(ord("0"), ord("9") + 1)]
    keys += ["Dash", "Equals"]
    keys += [f"F{n}" for n in range(1, 13)]
    keys += ["Space", "Enter", "Backspace", "Tab",
             "Left arrow", "Up arrow", "Right arrow", "Down arrow",
             "Home", "End", "Page up", "Page down", "Insert", "Delete"]
    keys += [f"Numpad {n}" for n in range(10)]
    keys += ["Numpad plus", "Numpad minus", "Numpad star", "Numpad slash"]
    return keys


KEY_CODES = {name: name for name in _key_names()}


class Pump:
    def __init__(self, apply, press):
        self._apply = apply
        self._press = press
        self._thread = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._ready = threading.Event()

    def start(self) -> None:
        if self.running():
            return
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def on_pump(self) -> bool:
        return self._thread is not None and threading.get_ident() == self._thread.ident

    def request_apply(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def register(self, hotkey_id: int, flags: int, code) -> bool:
        return False

    def unregister_all(self) -> None:
        pass

    def _run(self) -> None:
        self._apply()
        self._ready.set()
        while True:
            self._wake.wait()
            self._wake.clear()
            if self._stop.is_set():
                break
            self._apply()
