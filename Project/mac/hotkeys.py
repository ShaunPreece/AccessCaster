"""macOS's `hotkeys`: Carbon's RegisterEventHotKey, which needs no permission from the user and
refuses a combination another program has registered.

THE PUMP IS THE MAIN THREAD. A Carbon hotkey is delivered to the application's event target, which
only the main thread's event loop dispatches, and that loop is wx's. So registering, unregistering
and every press happen there, and a request from another thread is queued onto it.

A press is delivered once however long the key is held: Carbon sends no repeats for a hotkey.

The modifier flags are Carbon's cmdKey, shiftKey, optionKey and controlKey, listed in the order a
Mac writes them. The key codes are the kVK_ codes, which name physical keys on an ANSI keyboard.
"""
from __future__ import annotations

import ctypes

from Foundation import NSThread
from PyObjCTools import AppHelper

import diagnostics
from mac.cocoa import on_main

MODIFIERS = [
    ("ctrl", "Control", 0x1000),
    ("alt", "Option", 0x0800),
    ("shift", "Shift", 0x0200),
    ("win", "Command", 0x0100),
]

# THE HOUSE CHORD (`hotkeys`), Option+Shift+Command. Not Control+Option+Shift: Control+Option is
# VoiceOver's own key, so every combination holding both is VoiceOver's while it runs. Not
# Control+Shift+Command either: with 3, 4 and 5 it takes screenshots, and the pad scheme needs
# the number row. RegisterEventHotKey refusing is the only authority on what is free.
DEFAULT_MODS = ("alt", "shift", "win")


def _key_codes() -> dict:
    letters = {"A": 0x00, "S": 0x01, "D": 0x02, "F": 0x03, "H": 0x04, "G": 0x05, "Z": 0x06,
               "X": 0x07, "C": 0x08, "V": 0x09, "B": 0x0B, "Q": 0x0C, "W": 0x0D, "E": 0x0E,
               "R": 0x0F, "Y": 0x10, "T": 0x11, "O": 0x1F, "U": 0x20, "I": 0x22, "P": 0x23,
               "L": 0x25, "J": 0x26, "K": 0x28, "N": 0x2D, "M": 0x2E}
    digits = {"1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "6": 0x16, "5": 0x17, "9": 0x19,
              "7": 0x1A, "8": 0x1C, "0": 0x1D}
    codes = {**letters, **digits}
    codes.update({"Dash": 0x1B, "Equals": 0x18})
    codes.update({"F1": 0x7A, "F2": 0x78, "F3": 0x63, "F4": 0x76, "F5": 0x60, "F6": 0x61,
                  "F7": 0x62, "F8": 0x64, "F9": 0x65, "F10": 0x6D, "F11": 0x67, "F12": 0x6F})
    codes.update({
        "Space": 0x31, "Enter": 0x24, "Backspace": 0x33, "Tab": 0x30,
        "Left arrow": 0x7B, "Up arrow": 0x7E, "Right arrow": 0x7C, "Down arrow": 0x7D,
        "Home": 0x73, "End": 0x77, "Page up": 0x74, "Page down": 0x79,
        # A Mac keyboard's Help key sits where Insert is on a PC one.
        "Insert": 0x72, "Delete": 0x75,
    })
    codes.update({"Numpad 0": 0x52, "Numpad 1": 0x53, "Numpad 2": 0x54, "Numpad 3": 0x55,
                  "Numpad 4": 0x56, "Numpad 5": 0x57, "Numpad 6": 0x58, "Numpad 7": 0x59,
                  "Numpad 8": 0x5B, "Numpad 9": 0x5C})
    codes.update({"Numpad plus": 0x45, "Numpad minus": 0x4E, "Numpad star": 0x43,
                  "Numpad slash": 0x4B})
    return codes


KEY_CODES = _key_codes()


def _code(text: str) -> int:
    return int.from_bytes(text.encode("ascii"), "big")


_SIGNATURE = _code("ACst")              # marks the hotkeys as this app's
_KEYBOARD_EVENTS = _code("keyb")
_HOTKEY_PRESSED = 5
_DIRECT_OBJECT = _code("----")
_HOTKEY_ID_TYPE = _code("hkid")
_NOT_HANDLED = -9874                      # eventNotHandledErr: let the next handler see it


class _HotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)

_carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
_carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
_carbon.InstallEventHandler.argtypes = [ctypes.c_void_p, _HANDLER, ctypes.c_uint32,
                                        ctypes.POINTER(_EventTypeSpec), ctypes.c_void_p,
                                        ctypes.POINTER(ctypes.c_void_p)]
_carbon.InstallEventHandler.restype = ctypes.c_int32
_carbon.GetEventParameter.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                      ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                                      ctypes.c_void_p]
_carbon.GetEventParameter.restype = ctypes.c_int32
_carbon.RegisterEventHotKey.argtypes = [ctypes.c_uint32, ctypes.c_uint32, _HotKeyID,
                                        ctypes.c_void_p, ctypes.c_uint32,
                                        ctypes.POINTER(ctypes.c_void_p)]
_carbon.RegisterEventHotKey.restype = ctypes.c_int32
_carbon.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
_carbon.UnregisterEventHotKey.restype = ctypes.c_int32


class Pump:
    def __init__(self, apply, press):
        self._apply = apply
        self._press = press
        self._running = False
        self._refs = {}
        self._handler = None                 # kept: Carbon holds only the C pointer to it

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        on_main(self._begin, timeout=5.0)

    def running(self) -> bool:
        return self._running

    def on_pump(self) -> bool:
        return bool(NSThread.isMainThread())

    def request_apply(self) -> None:
        if self._running:
            AppHelper.callAfter(self._apply_if_running)

    def stop(self) -> None:
        self._running = False
        if self.on_pump():
            self.unregister_all()
        else:
            AppHelper.callAfter(self.unregister_all)

    def register(self, hotkey_id: int, flags: int, code) -> bool:
        ref = ctypes.c_void_p()
        status = _carbon.RegisterEventHotKey(code, flags, _HotKeyID(_SIGNATURE, hotkey_id),
                                             _carbon.GetApplicationEventTarget(), 0,
                                             ctypes.byref(ref))
        if status or not ref.value:
            return False
        self._refs[hotkey_id] = ref
        return True

    def unregister_all(self) -> None:
        for ref in self._refs.values():
            _carbon.UnregisterEventHotKey(ref)
        self._refs.clear()

    def _begin(self) -> None:
        if self._handler is None:
            self._handler = _HANDLER(self._on_event)
            spec = _EventTypeSpec(_KEYBOARD_EVENTS, _HOTKEY_PRESSED)
            _carbon.InstallEventHandler(_carbon.GetApplicationEventTarget(), self._handler, 1,
                                        ctypes.byref(spec), None, None)
        self._apply()

    def _apply_if_running(self) -> None:
        if self._running:
            self._apply()

    def _on_event(self, _call, event, _data) -> int:
        hotkey = _HotKeyID()
        if _carbon.GetEventParameter(event, _DIRECT_OBJECT, _HOTKEY_ID_TYPE, None,
                                     ctypes.sizeof(hotkey), None, ctypes.byref(hotkey)):
            return _NOT_HANDLED
        if hotkey.signature != _SIGNATURE or hotkey.id not in self._refs:
            return _NOT_HANDLED
        try:
            self._press(hotkey.id)
        except Exception as exc:               # never back across the C boundary
            diagnostics.event("hotkeys.press.failed", str(hotkey.id), error=str(exc))
        return 0
