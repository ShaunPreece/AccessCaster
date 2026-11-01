"""Press a real key combination on this machine, so a global hotkey can be TESTED.

Why it exists: a shortcut registered with RegisterHotKey can only be proven by an ACTUAL key press.
Posting WM_HOTKEY at the pump (DevTools/test_hotkeys.py does that) proves the dispatch and the
action, but not the registration - the part where Windows decides whether we own that combination.

USES keybd_event, NOT SendInput. From an RDP session a correctly formed SendInput call (INPUT 32
bytes, KEYBDINPUT at offset 8) returns 0 with GetLastError 87 and injects nothing, while
keybd_event fires the same hotkey every time. Both APIs feed the same input queue, so there is
nothing to gain from the newer one here.

Usage:  python DevTools/press_keys.py ctrl alt F9
        python DevTools/press_keys.py ctrl alt win S
        python DevTools/press_keys.py ctrl alt "Page up"

Any key the app offers (`hotkeys.KEYS`) can be named, in any case, plus the four modifiers.

Prefer a combination WITHOUT the Windows key in a test: a stray Win press can open the Start menu
and steal focus from whatever is being watched.
"""

import ctypes
import sys
import time

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import hotkeys

KEYEVENTF_KEYUP = 0x0002
_user32 = ctypes.WinDLL("user32", use_last_error=True)

# Lower-cased names, so "f9" and "F9" both work.
VK = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B}
VK.update((name.lower(), vk) for name, vk in hotkeys.KEYS)


def press(names, gap: float = 0.03) -> None:
    """Hold the keys in the order given and release them in reverse, the way a hand would."""
    vks = []
    for n in names:
        if n.lower() not in VK:
            raise SystemExit(f"unknown key: {n}")
        vks.append(VK[n.lower()])
    # RELEASED IN A finally, AND EVERY KEY, not only those already pressed: a key left down by an
    # interrupted run stays logically held system-wide, and to the user Ctrl or Shift simply stops
    # behaving in every application. A hard kill between the two halves still leaves them down,
    # which is why no test suite calls this.
    try:
        for vk in vks:
            _user32.keybd_event(vk, 0, 0, 0)
            time.sleep(gap)
    finally:
        for vk in reversed(vks):
            _user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(gap)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    press(sys.argv[1:])
    print("pressed: " + " ".join(sys.argv[1:]))
