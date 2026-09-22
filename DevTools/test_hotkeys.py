"""Prove the global-hotkey layer without touching the desk.

Registers real system-wide hotkeys on unlikely combinations, checks Windows accepted them, fires one
with a REAL key press (keybd_event, so registration is proven and not merely dispatch), and checks
the validation and list rules.

IT TYPES INTO WHATEVER IS FOCUSED, so it brings up A WINDOW OF ITS OWN FIRST and presses into that.
`keybd_event` injects at the system level: the keys reach the focused application as well as the
hotkey layer, and there is no way to prove REGISTRATION without a real press. Two rules follow, and
breaking either means a sweep of this suite repeatedly opens the menu bar of whatever window the
user is reading.

  - NO ALT AND NO WINDOWS KEY IN ANYTHING PRESSED. A bare Alt release is Windows' "open the menu
    bar" gesture and a bare Windows release opens the Start menu, so an injected Ctrl+Alt+F9 does
    both jobs. `parse_binding` only requires Ctrl, Alt OR Windows, so Ctrl+Shift satisfies the rule
    and neither half of it opens anything. Keep every PRESSED combination on Ctrl+Shift; the
    validation checks may name any modifiers they like, because they press nothing.
  - THE PRESSES GO INTO OUR OWN WINDOW. `own_the_keyboard()` puts one in front and CONFIRMS the
    foreground window belongs to this process; if it cannot get focus, the press-based checks are
    SKIPPED rather than typed into somebody's document. A skipped check is honest; a check that
    passes by typing into your editor is not.

Run:  python DevTools/test_hotkeys.py
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Project"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ctypes  # noqa: E402
import subprocess  # noqa: E402

import hotkeys  # noqa: E402
import press_keys  # noqa: E402

FAILURES = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}{(': ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)


def skip(name, why):
    print(f"SKIP  {name}: {why}")


def session_connected() -> bool:
    """Is this Windows session attached to a desktop that can receive input?

    A DISCONNECTED session (the RDP window closed, the user gone) has no input desktop, so
    keybd_event injects nothing and NO hotkey can fire. That is not a fault in anything here, and it
    is nearly invisible: registration reports success, dispatch works when a message is posted by
    hand, and only the real presses do nothing. So CHECK IT rather than debugging the hotkey layer.
    Read with `query session`, whose own line for this session is marked with ">"."""
    try:
        out = subprocess.run(["query", "session"], capture_output=True, text=True,
                             timeout=10).stdout
    except Exception:
        return True                       # cannot tell: assume yes and let the checks speak
    for line in out.splitlines():
        if line.startswith(">"):
            return "Disc" not in line
    return True


def foreground_is_ours() -> bool:
    """Does the window with the keyboard belong to THIS process?

    The question every injected press has to answer first. `keybd_event` goes to whatever is
    focused, so if the answer is no, the keys land in somebody's document."""
    user32 = ctypes.windll.user32
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(pid))
    return pid.value == os.getpid()


class own_the_keyboard:
    """Put a window of OUR OWN in front for the duration, so injected presses land on us.

    `ok` is True only when the foreground window really did end up ours - Windows refuses
    SetForegroundWindow to a process the user has not interacted with, and refusing is silent. The
    caller must SKIP its press-based checks when it is False rather than press anyway.

    Deliberately a visible, titled, always-on-top window rather than a hidden one: taking someone's
    keyboard without showing them what did it is the behaviour this class exists to stop."""

    def __init__(self):
        self.root, self.ok = None, False

    def __enter__(self):
        try:
            import tkinter
        except Exception:
            return self                       # no Tk: ok stays False and the presses are skipped
        try:
            self.root = tkinter.Tk()
            self.root.title("AccessCaster hotkey test")
            self.root.geometry("460x90+60+60")
            tkinter.Label(self.root, wraplength=420, justify="left",
                          text="Testing keyboard shortcuts. This window is holding the keyboard "
                               "for a few seconds so the test does not type into your work. It "
                               "closes itself.").pack(padx=12, pady=12)
            self.root.attributes("-topmost", True)
            self.root.update()
            self.root.lift()
            self.root.focus_force()
            for _ in range(20):               # focus can take a moment to actually change hands
                self.root.update()
                if foreground_is_ours():
                    break
                time.sleep(0.05)
            self.ok = foreground_is_ours()
        except Exception:
            self.ok = False
        return self

    def __exit__(self, *exc):
        if self.root is not None:
            try:
                self.root.destroy()
            except Exception:
                pass
        return False


def press_or_post(mgr, keys, hotkey_id, keyboard=None):
    """Press the keys for real; otherwise post the message instead and say why.

    The real press is what proves REGISTRATION; the posted message only proves dispatch. They are
    not the same check, so which one ran is always printed rather than glossed over. Two things
    force the fallback: a DISCONNECTED session, which has no input desktop to inject into, and a
    foreground window that is not ours, which would mean typing into somebody else's program."""
    if session_connected() and keyboard is not None and keyboard.ok:
        press_keys.press(keys)
        return True
    ctypes.windll.user32.PostThreadMessageW(mgr._tid, hotkeys.WM_HOTKEY, hotkey_id, 0)
    return False


def main():
    with own_the_keyboard() as keyboard:
        return _run_checks(keyboard)


def _run_checks(keyboard):
    said = []
    fired = []

    def runner(command):
        fired.append(command)
        if command == "boom":
            raise RuntimeError("nope")
        return f"ran {command}"

    labels = {"one": "Test one", "two": "Test two", "boom": "Broken"}
    mgr = hotkeys.HotkeyManager(speak=said.append, runner=runner,
                                labeller=lambda c: labels.get(c, c))

    # --- validation, before anything is registered ---------------------------------------------
    _f, _v, _m, problem = hotkeys.parse_binding(["shift"], "A")
    check("a Shift-only combination is refused", bool(problem), problem)
    _f, _v, _m, problem = hotkeys.parse_binding([], "A")
    check("a bare key is refused", bool(problem), problem)
    _f, _v, _m, problem = hotkeys.parse_binding(["ctrl", "alt", "win"], "A")
    check("Ctrl+Alt+Win+A is accepted", not problem, problem)
    _f, _v, _m, problem = hotkeys.parse_binding(["ctrl"], "Not a key")
    check("an unknown key is refused", bool(problem), problem)
    check("a combination reads as a sentence",
          hotkeys.combo_text(["win", "ctrl", "alt"], "A") == "Ctrl, Alt, Windows and A",
          hotkeys.combo_text(["win", "ctrl", "alt"], "A"))

    # --- the defaults are laid down when nothing has been saved ---------------------------------
    mgr.load({}, defaults=[("one", ("ctrl", "shift"), "F9", True),
                           ("two", ("ctrl", "shift"), "F10", True)])
    mgr.start()
    time.sleep(0.4)
    st = mgr.state()
    check("the pump is running", st["running"])
    check("the defaults became two shortcuts", len(st["shortcuts"]) == 2,
          str([s["combo"] for s in st["shortcuts"]]))
    problems = [(s["label"], s["problem"]) for s in st["shortcuts"] if s["problem"]]
    check("both registered with Windows", not problems, str(problems))
    check("each carries its command's label",
          [s["label"] for s in st["shortcuts"]] == ["Test one", "Test two"],
          str([s["label"] for s in st["shortcuts"]]))

    # --- A REAL KEY PRESS, which is the only thing that proves REGISTRATION ---------------------
    fired.clear()
    real = press_or_post(mgr, ["ctrl", "shift", "F9"], 1, keyboard)
    for _ in range(30):
        if fired:
            break
        time.sleep(0.1)
    if real:
        check("a real key press ran the command", fired == ["one"], str(fired))
    else:
        check("a posted hotkey ran the command", fired == ["one"], str(fired))
        skip("a REAL key press runs the command",
             "this session is disconnected, so injected input goes nowhere"
             if session_connected() is False else
             "this test could not put its own window in front, and pressing keys into somebody "
             "else's window is worse than a skipped check. Run it with the desktop free.")
    time.sleep(0.3)
    check("and its reply was spoken", said and said[-1] == "ran one", str(said))

    # --- adding, and the clash rules -------------------------------------------------------------
    res = mgr.add("two")
    check("a shortcut can be added, on a free key", res["ok"], res["message"])
    added = mgr.get(res["id"])
    check("the new one is on the default chord and switched on",
          added["enabled"] and sorted(added["mods"]) == sorted(hotkeys.DEFAULT_MODS),
          str(added))
    check("adding does not collide with what is already there",
          not mgr.state()["shortcuts"][-1]["problem"],
          mgr.state()["shortcuts"][-1]["problem"])
    first = mgr.state()["shortcuts"][0]["id"]
    added_id = res["id"]
    res = mgr.set_binding(added_id, ["ctrl", "shift"], "F9", True)
    check("a clash with our OWN shortcut is refused, and names it",
          not res["ok"] and "Test one" in res["message"], res["message"])
    # MOVE IT OFF THE DEFAULT CHORD. `add` puts a new shortcut on the first free key of the app's
    # own chord, and the real app is very likely RUNNING while this suite is, holding exactly those.
    # So the rest of the run goes on a chord NOTHING ships on. Ctrl+Alt+Shift is not that chord -
    # it is `hotkeys.DEFAULT_MODS`, the app's own.
    mgr.set_binding(added_id, ["ctrl", "shift"], "F8", True)

    # --- staying quiet, per shortcut -------------------------------------------------------------
    said.clear()
    fired.clear()
    mgr.set_binding(first, ["ctrl", "shift"], "F9", True, speak=False)
    press_or_post(mgr, ["ctrl", "shift", "F9"], 1, keyboard)
    for _ in range(30):
        if fired:
            break
        time.sleep(0.1)
    check("a silenced shortcut still RUNS", fired == ["one"], str(fired))
    time.sleep(0.4)
    check("and says nothing", said == [], str(said))
    check("the page is told it is silent",
          mgr.state()["shortcuts"][0]["speak"] is False)

    # A FAILURE is spoken whatever the flag says: silencing hides a confirmation, not a fault.
    boom = mgr.add("boom")
    mgr.set_binding(boom["id"], ["ctrl", "shift"], "F11", True, speak=False)
    said.clear()
    mgr._run(mgr.get(boom["id"]))
    check("a silenced shortcut still speaks a FAILURE",
          any("nope" in s for s in said), str(said))

    # --- deleting --------------------------------------------------------------------------------
    before = len(mgr.state()["shortcuts"])
    res = mgr.remove(boom["id"])
    check("a shortcut can be deleted", res["ok"] and
          len(mgr.state()["shortcuts"]) == before - 1, res["message"])
    check("deleting something already gone says so, rather than throwing",
          not mgr.remove(boom["id"])["ok"])

    # --- the master switch -----------------------------------------------------------------------
    # The master switch UNREGISTERS, so this one can only be checked with a real press: a posted
    # message would run the action whatever the registration says.
    if session_connected() and keyboard.ok:
        fired.clear()
        mgr.set_enabled(False)
        time.sleep(0.3)
        press_keys.press(["ctrl", "shift", "F9"])
        time.sleep(1.0)
        check("with shortcuts switched off, a press does NOTHING", fired == [], str(fired))
        mgr.set_enabled(True)
        time.sleep(0.3)
        fired.clear()
        press_keys.press(["ctrl", "shift", "F9"])
        for _ in range(30):
            if fired:
                break
            time.sleep(0.1)
        check("and switching them back on restores it", fired == ["one"], str(fired))
    else:
        mgr.set_enabled(False)
        time.sleep(0.4)               # the re-registration happens on the pump, not in this call
        check("the master switch reports off", not mgr.state()["enabled"])
        mgr.set_enabled(True)
        time.sleep(0.4)
        check("and on again, with everything re-registered", mgr.state()["enabled"]
              and not [s for s in mgr.state()["shortcuts"] if s["problem"]],
              str([(s["combo"], s["problem"]) for s in mgr.state()["shortcuts"]]))
        skip("switching them off really releases the keys",
             "needs a real key press, and " + ("this session is disconnected."
                                               if not session_connected() else
                                               "this test could not take the keyboard safely."))

    # --- what gets persisted, and reloading it ---------------------------------------------------
    saved = mgr.saveable()
    check("only data is persisted, no callables",
          set(saved) == {"enabled", "shortcuts"}
          and all(set(s) == {"id", "command", "mods", "key", "enabled", "speak"}
                  for s in saved["shortcuts"]),
          str(saved))
    fresh = hotkeys.HotkeyManager(labeller=lambda c: labels.get(c, c))
    fresh.load(saved, defaults=[("never", ("ctrl", "alt"), "F1", True)])
    check("saved shortcuts load back, and the defaults are NOT re-applied over them",
          [s["command"] for s in fresh.state()["shortcuts"]]
          == [s["command"] for s in saved["shortcuts"]],
          str([s["command"] for s in fresh.state()["shortcuts"]]))
    empty = hotkeys.HotkeyManager()
    empty.load({"enabled": True, "shortcuts": []},
               defaults=[("never", ("ctrl", "alt"), "F1", True)])
    check("an empty saved list stays empty: deleting them all is a choice, not a reset",
          empty.state()["shortcuts"] == [])

    mgr.stop()
    time.sleep(0.3)
    check("stopping releases the keys", not mgr.state()["running"])

    print()
    print(f"{len(FAILURES)} failed" if FAILURES else "All checks passed.")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
