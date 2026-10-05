"""AccessCaster - global keyboard shortcuts (system-wide hotkeys).

WHAT THIS IS FOR. Some things you want from the desk are things you want NOW, from whatever app you
are in: turn the announcer off because it is talking over a guest, mute every mic because you are
about to cough, start the recording. Opening the web UI and finding the control is the wrong shape
for all three. A global hotkey works while the browser window is closed and while another
application has focus, which is exactly the case the rest of this app cannot serve.

WHY IT LIVES IN PYTHON AND NOT IN THE PAGE. A web page only sees keys pressed while it has focus, so
the browser can configure a shortcut but can never be the thing that catches it. Windows' own
RegisterHotKey is the mechanism, and it has two properties worth having: the OS owns the binding, so
no keyboard hook and no key logging is involved, and a combination another program already owns is
REFUSED at registration rather than silently stolen. See `_apply_now` for what we do with that
refusal.

THE THREAD. RegisterHotKey delivers WM_HOTKEY to the THREAD that registered it, and only while that
thread pumps messages. So this module owns one daemon thread that registers everything and then sits
in GetMessage. Everything from outside (start, stop, re-bind) is marshalled onto it by posting a
message, because registering from another thread would deliver the hotkeys to that thread instead.
Actions run on a short-lived worker thread, never on the pump: a device write costs about a second
and a stalled pump would swallow the next press.

ACCESSIBILITY RULES, and they are the reason for the validation in `parse_binding`:

  - AT LEAST ONE OF CTRL, ALT OR WINDOWS is required. A hotkey takes that key away from every other
    program on the machine, screen reader included, so a bare key or a Shift+key is refused rather
    than allowed to break someone's typing.
  - THE DEFAULT COMBINATION IS CTRL+ALT+SHIFT, and it is the ONLY one the app ships on (see
    DEFAULT_MODS for why it is not Ctrl+Alt+Windows). It stays clear of
    NVDA's Insert and Caps Lock layers, JAWS' Insert layer and Narrator's Caps Lock layer, so the
    shortcuts do not have to be re-learnt per screen reader.
  - MOD_NOREPEAT is always set. Without it, holding a key fires the action over and over.
  - EVERY ACTION SPEAKS ITS RESULT, UNLESS THAT SHORTCUT IS SET TO STAY QUIET. When a hotkey fires,
    focus is in another application, so the web UI cannot report anything: the spoken reply is
    normally the only feedback there is. But the case the announcer shortcut exists for is realising
    mid-take that announcements are going into the mix, and speaking
    "announcements off" into that mix is the exact thing you just pressed a key to stop. So each
    shortcut carries its own `speak` flag. A shortcut that FAILED still speaks: the flag silences a
    confirmation, not an error, because the whole point of silencing is that the thing worked.

This module is deliberately device-agnostic: it knows about keys, modifiers and Windows, and nothing
about the RODECaster. The actions themselves are registered by server.py.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

from wording import join_and

# --- Win32 constants ----------------------------------------------------------------------------
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000            # a held key fires ONCE (Windows 7+)

WM_HOTKEY = 0x0312
WM_APP = 0x8000
_WM_RELOAD = WM_APP + 1          # posted to the pump: re-register from the current bindings
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

# --- What can be bound --------------------------------------------------------------------------
# The modifier list is ORDERED, and the order is how a combination is spoken and written down, so
# "Ctrl, Alt, Windows and A" reads the same everywhere in the app.
MODIFIERS = [
    ("ctrl", "Ctrl", MOD_CONTROL),
    ("alt", "Alt", MOD_ALT),
    ("win", "Windows", MOD_WIN),
    ("shift", "Shift", MOD_SHIFT),
]
_MOD_FLAG = {name: flag for name, _label, flag in MODIFIERS}
# A modifier that is enough ON ITS OWN to make a combination safe to take from the whole system.
# Shift is not in here on purpose: Shift+A is a capital A.
_STRONG_MODS = ("ctrl", "alt", "win")

# THE HOUSE CHORD. Every shipped shortcut is on it and every new one starts on it. ONE CHORD FOR
# THE WHOLE APP: two chords is one too many, so there is one thing to hold down and the only
# question left is which key.
#
# IT IS NOT CTRL+ALT+WINDOWS, because the pad scheme needs the NUMBER ROW. Measured rather than
# assumed, by registering each chord and immediately unregistering it: Ctrl+Alt+Windows is refused
# all nine digits, and Ctrl+Alt+Shift is given every key the app ships on - the six letters, the
# nine digits, Dash and Equals. Ctrl+Alt alone is no good either; 1 to 4 are commonly held.
#
# THE HOLDER IS NOT WINDOWS, and the obvious explanation is wrong. Windows does own Win+digit for
# the taskbar, and Ctrl+Win+digit and Alt+Win+digit with it, but Ctrl+Alt+Win+digit is not a Windows
# shortcut at all: an ordinary application can be holding it. So the rule is not "avoid the Windows
# key", it is that a popular chord plus the number row is contested and you cannot know by whom.
# RegisterHotKey refusing is the only authority; do not reason about what "should" be free.
#
# CHANGING THIS DOES NOT MOVE ANYBODY'S SAVED SHORTCUTS, deliberately: a key someone chose is
# theirs, and silently relocating it is worse than an inconsistency they can see.
DEFAULT_MODS = ("ctrl", "alt", "shift")


# THE TWO KEYS AT THE END OF THE NUMBER ROW. Named as constants because the shipped pad scheme
# binds them and a default that referred to a key by a string typed twice is a silent break waiting
# to happen. They are LAYOUT-DEPENDENT: the virtual-key codes are the physical keys next to zero on
# a UK or US keyboard, whatever a different layout happens to print on them, which is exactly what
# is wanted for a scheme found by touch.
KEY_DASH = "Dash"
KEY_EQUALS = "Equals"


def _key_table() -> list:
    """[(name, virtual-key code)] of every key the app offers, in the order the chooser lists them.

    Letters and digits first because that is what anyone reaches for; then the function keys; then
    the navigation and numeric-pad keys, which are worth offering for the level-style shortcuts that
    will want a pair (up and down) rather than a single press."""
    keys = [(chr(c), c) for c in range(ord("A"), ord("Z") + 1)]
    keys += [(chr(c), c) for c in range(ord("0"), ord("9") + 1)]
    # Straight after the digits, because that is where they are on the keyboard and where the pad
    # scheme puts them: the number row plays the pads and these two step the bank.
    keys += [(KEY_DASH, 0xBD), (KEY_EQUALS, 0xBB)]      # VK_OEM_MINUS / VK_OEM_PLUS
    keys += [(f"F{n}", 0x70 + n - 1) for n in range(1, 13)]
    keys += [
        ("Space", 0x20), ("Enter", 0x0D), ("Backspace", 0x08), ("Tab", 0x09),
        ("Left arrow", 0x25), ("Up arrow", 0x26), ("Right arrow", 0x27), ("Down arrow", 0x28),
        ("Home", 0x24), ("End", 0x23), ("Page up", 0x21), ("Page down", 0x22),
        ("Insert", 0x2D), ("Delete", 0x2E),
    ]
    # The numpad is OFFERED and deliberately absent from every shipped default: the scheme must not
    # use numpad keys, which is not the same as nobody being allowed to bind one.
    keys += [(f"Numpad {n}", 0x60 + n) for n in range(10)]
    keys += [("Numpad plus", 0x6B), ("Numpad minus", 0x6D), ("Numpad star", 0x6A),
             ("Numpad slash", 0x6F)]
    return keys


KEYS = _key_table()
_KEY_VK = {name: vk for name, vk in KEYS}


def combo_text(mods, key: str) -> str:
    """A combination as it is spoken and written: "Ctrl, Alt, Windows and A".

    Commas and "and" rather than plus signs, because this is read aloud far more often than it is
    read on screen, and a screen reader announces "+" as "plus" in the middle of the phrase."""
    names = [label for name, label, _flag in MODIFIERS if name in (mods or [])]
    return join_and(names + [key])


def parse_binding(mods, key: str):
    """Validate a binding and return (mod_flags, vk, tidy_mods, message).

    message is "" when the binding is good, and a sentence saying what is wrong when it is not. The
    caller shows that sentence: a shortcut that quietly fails to register is worse than no shortcut,
    because you find out by pressing it and getting nothing."""
    key = str(key or "").strip()
    wanted = [m for m in (mods or []) if m in _MOD_FLAG]
    # Keep our own order, not the order the caller happened to send.
    tidy = [name for name, _label, _flag in MODIFIERS if name in wanted]
    if key not in _KEY_VK:
        return 0, 0, tidy, f"{key or 'That'} is not a key this app can use for a shortcut."
    if not any(m in tidy for m in _STRONG_MODS):
        return 0, 0, tidy, ("A shortcut needs Ctrl, Alt or Windows in it. Without one of those it "
                            "would take that key away from every other program on this computer, "
                            "including your screen reader.")
    flags = MOD_NOREPEAT
    for m in tidy:
        flags |= _MOD_FLAG[m]
    return flags, _KEY_VK[key], tidy, ""



# COMMANDS THAT MUST RUN ON THE PUMP THREAD, because they need the foreground right that the
# keypress itself grants.
#
# WINDOWS GIVES A PROCESS THE RIGHT TO SET THE FOREGROUND WHEN A HOTKEY IT REGISTERED IS PRESSED,
# and that right is transient. Handing the work to a freshly spawned thread spends it: by the time
# that thread calls SetForegroundWindow the moment has passed, Windows refuses, and the window is
# then only raisable by forcing it - which puts it in front WITHOUT a real activation. Measured:
# the window became the foreground window and held it, GetGUIThreadInfo agreed the keyboard focus
# was ours, and NVDA went on reading the app the user came from, because no activation event was
# ever raised. Alt-Tabbing to the same window announced it perfectly.
#
# So the raise runs INLINE, in the WM_HOTKEY handler, where the right still exists. Everything
# else keeps its own thread: a device write takes seconds and would block the next press.
_FOREGROUND_COMMANDS = ("app.window",)


class HotkeyManager:
    """Owns the pump thread, the user's shortcuts and the registration state.

    A SHORTCUT IS A KEY POINTED AT A COMMAND, and the user owns the list: they add one, choose what
    it does, and delete it again. NOT a fixed set of hand-written actions: the standing requirement
    is that any property the app can offer anywhere else can appear in the catalogue of available
    hotkey commands. With a catalogue there is nothing to hand-write, so this class does not hold
    commands at all: it holds shortcuts, and calls out to a RUNNER and a LABELLER given to it.

    Each shortcut is {"id", "command", "mods", "key", "enabled", "speak"}. The id is ours and only
    has to be unique; the command is the caller's string and is opaque here. Pure Windows plumbing:
    nothing in this file knows what a RODECaster is."""

    def __init__(self, speak=None, runner=None, labeller=None):
        self._speak = speak or (lambda text: None)
        self._runner = runner or (lambda command: "")
        self._labeller = labeller or (lambda command: command)
        self._shortcuts = []          # ordered [{id, command, mods, key, enabled, speak}]
        self._status = {}             # shortcut id -> "" (registered) or why it is not
        self._busy = set()            # shortcuts running now, so a re-press is ignored
        self._lock = threading.Lock()
        self._enabled = True          # the master switch; off unregisters everything
        self._thread = None
        self._tid = 0
        self._ready = threading.Event()

    # -- the list ---------------------------------------------------------------------------------
    def load(self, saved: dict, defaults=None) -> None:
        """Restore the saved shortcuts, or lay down `defaults` on a machine that has none.

        A malformed entry is dropped rather than refused: a settings file written by a newer build
        must never stop an older one starting. `defaults` is [(command, mods, key, speak)] and is
        used ONLY when nothing has been saved, so deleting every shortcut stays deleted."""
        saved = saved if isinstance(saved, dict) else {}
        self._enabled = bool(saved.get("enabled", True))
        rows = saved.get("shortcuts")
        out = []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                mods, key = row.get("mods"), row.get("key")
                command = row.get("command")
                if not isinstance(mods, list) or not isinstance(key, str) \
                        or not isinstance(command, str):
                    continue
                out.append({"id": str(row.get("id") or self._new_id(out)),
                            "command": command,
                            "mods": [m for m in mods if m in _MOD_FLAG],
                            "key": key,
                            "enabled": bool(row.get("enabled", True)),
                            "speak": bool(row.get("speak", True))})
        elif defaults:
            for command, mods, key, speak in defaults:
                out.append({"id": self._new_id(out), "command": command, "mods": list(mods),
                            "key": key, "enabled": True, "speak": bool(speak)})
        with self._lock:
            self._shortcuts = out
        self.reload()

    def _new_id(self, existing=None) -> str:
        """A short unique id. Counted rather than random, because this module must not use
        randomness or the clock - both make a saved file impossible to reason about, and one of them
        is unavailable in the workflow sandbox anyway."""
        used = {s["id"] for s in (existing if existing is not None else self._shortcuts)}
        n = 1
        while f"sc{n}" in used:
            n += 1
        return f"sc{n}"

    def saveable(self) -> dict:
        """What belongs in settings.json: the shortcuts and the master switch, no callables."""
        with self._lock:
            return {"enabled": self._enabled,
                    "shortcuts": [dict(s) for s in self._shortcuts]}

    def add(self, command: str, speak: bool = True) -> dict:
        """Create a shortcut for `command`, on the first free key of the default chord.

        It arrives ALREADY USABLE rather than as a blank row waiting to be filled in: a new shortcut
        with no key is a thing you have to notice is unfinished, and the point of Add is to get one
        working in a press or two. If every key in the default chord is taken it comes in switched
        off, with the reason said out loud, instead of silently colliding."""
        command = str(command or "")
        if not command:
            return {"ok": False, "message": "No command was chosen."}
        mods = list(DEFAULT_MODS)
        key = self._free_key(mods)
        # `speak` is the CALLER's judgement, not ours: this module does not know what any command
        # does, and some of them (firing a pad) are their own feedback and want no voice over them.
        row = {"id": self._new_id(), "command": command, "mods": mods,
               "key": key or "A", "enabled": bool(key), "speak": bool(speak)}
        with self._lock:
            self._shortcuts.append(row)
        self.reload()
        label = self._labeller(command)
        if not key:
            return {"ok": True, "id": row["id"],
                    "message": (f"Added {label}, but every key on {combo_text(mods, '')} is already "
                                f"taken, so it is switched off. Choose a key for it.")}
        problem = self._status.get(row["id"], "")
        if problem:
            return {"ok": True, "id": row["id"], "message": f"Added {label}. {problem}"}
        return {"ok": True, "id": row["id"],
                "message": f"Added {label} on {combo_text(mods, key)}."}

    def add_scheme(self, rows) -> dict:
        """Lay a whole named SET of shortcuts onto the list the user already has.

        WHY THIS IS NOT `load(defaults=...)`. Defaults are laid down only on a machine that has
        saved nothing, and that rule has to stay: re-applying them over a saved list would undo
        somebody's deliberate deletion on every start. But that also means a set added in a LATER
        version can never reach anyone who has already set their keys, which is how the pad scheme
        would have shipped invisible to every existing install. So this is the same set, applied
        ONCE, BY ASKING - an explicit action, never a start-up side effect.

        Skips a command that is already bound (you asked for the scheme, not for duplicates) and a
        key that is already taken by something else (yours wins; ours is the newcomer). Reports both,
        because a set that silently dropped half of itself would look like the feature was broken.

        `rows` is [(command, mods, key, speak)], the same shape as `defaults`."""
        added, had, clashed = [], [], []
        with self._lock:
            bound = {s["command"] for s in self._shortcuts}
            for command, mods, key, speak in rows:
                if command in bound:
                    had.append(command)
                    continue
                tidy = [n for n, _l, _f in MODIFIERS if n in list(mods)]
                clash = next((s for s in self._shortcuts
                              if s.get("enabled") and s["key"] == key
                              and sorted(s.get("mods") or []) == sorted(tidy)), None)
                if clash is not None:
                    clashed.append((command, key, clash["command"]))
                    continue
                self._shortcuts.append({"id": self._new_id(), "command": command,
                                        "mods": list(tidy), "key": key,
                                        "enabled": True, "speak": bool(speak)})
                bound.add(command)
                added.append(command)
        self.reload()
        return {"added": added, "already_there": had, "clashed": clashed}

    def _free_key(self, mods):
        """The first key of KEYS not already used with these modifiers. Letters first, so a new
        shortcut lands on something sayable rather than on Numpad 7."""
        taken = {s["key"] for s in self._shortcuts
                 if s.get("enabled") and sorted(s.get("mods") or []) == sorted(mods)}
        for name, _vk in KEYS:
            if name not in taken:
                return name
        return ""

    def remove(self, shortcut_id: str) -> dict:
        with self._lock:
            row = next((s for s in self._shortcuts if s["id"] == shortcut_id), None)
            if row is None:
                return {"ok": False, "message": "That shortcut is not there any more."}
            self._shortcuts = [s for s in self._shortcuts if s["id"] != shortcut_id]
        self.reload()
        return {"ok": True,
                "message": f"Deleted the shortcut for {self._labeller(row['command'])}."}

    def get(self, shortcut_id: str):
        with self._lock:
            row = next((s for s in self._shortcuts if s["id"] == shortcut_id), None)
            return dict(row) if row else None

    def set_binding(self, shortcut_id: str, mods, key: str, enabled: bool, speak=None) -> dict:
        """Change one shortcut. Validated here, so the caller never persists a binding that could
        not work. Returns {"ok", "message"}; the message is what gets spoken and shown."""
        current = self.get(shortcut_id)
        if current is None:
            return {"ok": False, "message": "That shortcut is not there any more."}
        if speak is None:
            speak = current.get("speak", True)
        flags, vk, tidy, problem = parse_binding(mods, key)
        if problem and enabled:
            return {"ok": False, "message": problem}
        clash = self._clash(shortcut_id, tidy, key)
        if clash and enabled:
            return {"ok": False,
                    "message": f"{combo_text(tidy, key)} is already the shortcut for {clash}. "
                               f"Choose a different key."}
        with self._lock:
            for s in self._shortcuts:
                if s["id"] == shortcut_id:
                    s.update({"mods": tidy, "key": key, "enabled": bool(enabled),
                              "speak": bool(speak)})
        self.reload()
        label = self._labeller(current["command"])
        state = self._status.get(shortcut_id, "")
        if state:
            return {"ok": False, "message": state}
        if not enabled:
            return {"ok": True, "message": f"{label}: shortcut off."}
        quiet = "" if speak else ", and it will not say so"
        return {"ok": True, "message": f"{label}: {combo_text(tidy, key)}{quiet}."}

    def set_enabled(self, on: bool) -> dict:
        with self._lock:
            self._enabled = bool(on)
        self.reload()
        return {"ok": True, "enabled": bool(on),
                "message": ("Keyboard shortcuts are on." if on else
                            "Keyboard shortcuts are off. Nothing is listening for them now.")}

    def _clash(self, shortcut_id: str, mods, key: str):
        """The LABEL of another shortcut already on this combination, or None. Our own clash, caught
        before Windows sees it: Windows would just refuse the second registration and the error would
        blame "another program" when the other program is us."""
        with self._lock:
            rows = [dict(s) for s in self._shortcuts]
        for s in rows:
            if s["id"] == shortcut_id or not s.get("enabled"):
                continue
            if s.get("key") == key and sorted(s.get("mods") or []) == sorted(mods):
                return self._labeller(s["command"])
        return None

    # -- what the UI shows -----------------------------------------------------------------------
    def state(self) -> dict:
        with self._lock:
            enabled = self._enabled
            rows = [dict(s) for s in self._shortcuts]
            status = dict(self._status)
        out = []
        for s in rows:
            out.append({
                "id": s["id"], "command": s["command"],
                "label": self._labeller(s["command"]),
                "mods": list(s.get("mods") or []), "key": s.get("key", ""),
                "enabled": bool(s.get("enabled")), "speak": bool(s.get("speak", True)),
                "combo": combo_text(s.get("mods"), s.get("key", "")),
                "problem": status.get(s["id"], ""),
            })
        return {"ok": True, "enabled": enabled,
                "running": bool(self._thread and self._thread.is_alive()),
                "shortcuts": out,
                "default_mods": list(DEFAULT_MODS),
                "modifiers": [{"id": n, "label": l} for n, l, _f in MODIFIERS],
                "keys": [n for n, _vk in KEYS]}

    # -- the pump thread -------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._pump, name="hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def stop(self) -> None:
        self._post(_WM_STOP)

    def reload(self) -> None:
        """Re-register from the current list. Safe to call from any thread and before the pump has
        started: without a live pump there is nothing registered to fix."""
        self._post(_WM_RELOAD)

    def _post(self, message: int) -> None:
        if self._tid and self._thread and self._thread.is_alive():
            _user32.PostThreadMessageW(self._tid, message, 0, 0)

    def _pump(self) -> None:
        self._tid = _kernel32.GetCurrentThreadId()
        registered = {}                       # hotkey id -> shortcut id
        self._apply_now(registered)
        self._ready.set()
        msg = wintypes.MSG()
        while True:
            got = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if got in (0, -1):                # WM_QUIT, or an error we cannot recover from
                break
            if msg.message == WM_HOTKEY:
                sid = registered.get(int(msg.wParam))
                if sid:
                    self._fire(sid)
            elif msg.message == _WM_RELOAD:
                self._apply_now(registered)
            elif msg.message == _WM_STOP:
                break
        for hk_id in list(registered):
            _user32.UnregisterHotKey(None, hk_id)
        registered.clear()
        self._tid = 0

    def _apply_now(self, registered: dict) -> None:
        """Unregister everything and register what the list now says. ONLY called on the pump thread
        (see the module docstring). Rebuilding wholesale rather than diffing is deliberate: there are
        a handful of hotkeys, the call is microseconds, and a diff is where the stale-registration
        bugs would live.

        A registration Windows REFUSES is recorded against the shortcut that caused it and shown on
        the page. That is the whole reason RegisterHotKey is the right mechanism: the clash is
        reported at binding time instead of the shortcut silently doing nothing when you press it."""
        for hk_id in list(registered):
            _user32.UnregisterHotKey(None, hk_id)
        registered.clear()
        with self._lock:
            enabled = self._enabled
            rows = [dict(s) for s in self._shortcuts]
        status = {}
        for i, s in enumerate(rows):
            sid = s["id"]
            if not enabled:
                # No per-shortcut message: the master switch says this once, at the top of the page.
                status[sid] = ""
                continue
            if not s.get("enabled"):
                status[sid] = ""              # off by choice is not a problem to report
                continue
            flags, vk, _tidy, problem = parse_binding(s.get("mods"), s.get("key", ""))
            if problem:
                status[sid] = problem
                continue
            hk_id = i + 1
            if _user32.RegisterHotKey(None, hk_id, flags, vk):
                registered[hk_id] = sid
                status[sid] = ""
            else:
                status[sid] = (f"Windows would not accept {combo_text(s.get('mods'), s.get('key'))}: "
                               f"another program is already using it. Choose a different key.")
        with self._lock:
            self._status = status

    def _fire(self, shortcut_id: str) -> None:
        """Run a shortcut OFF the pump thread, so a device write cannot delay the next press.

        Except the ones that need the foreground right - see _FOREGROUND_COMMANDS."""
        row = self.get(shortcut_id)
        if row is None:
            return
        with self._lock:
            if shortcut_id in self._busy:
                return                        # still working on the last press; ignore this one
            self._busy.add(shortcut_id)
        if row.get("command") in _FOREGROUND_COMMANDS:
            self._run(row)                    # inline: the right does not survive a thread hop
            return
        threading.Thread(target=self._run, args=(row,), daemon=True).start()

    def _run(self, row: dict) -> None:
        try:
            said = self._runner(row["command"])
            if said and row.get("speak", True):
                self._speak(str(said))
        except Exception as exc:
            # ALWAYS spoken, whatever the flag says: silencing a shortcut means "I know what this
            # does, do not narrate it", not "hide it when it breaks".
            self._speak(f"{self._labeller(row['command'])} failed: {exc}")
        finally:
            with self._lock:
                self._busy.discard(row["id"])
