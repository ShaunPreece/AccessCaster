"""Prove the global-hotkey layer without touching the desk or the keyboard.

Registers real system-wide hotkeys on unlikely combinations, checks Windows accepted them, fires
them by POSTING the hotkey message to the pump thread, and checks the validation and list rules.

IT PRESSES NO KEYS, deliberately. Injected presses prove one more thing, that Windows delivers a
registered combination, and they cost the machine's keyboard: a run killed between a press and its
release leaves Ctrl or Shift held down in every application. Delivery is checked by hand instead,
by pressing a real shortcut.

Run:  python DevTools/test_hotkeys.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Project"))

import ctypes  # noqa: E402

import hotkeys  # noqa: E402

FAILURES = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}{(': ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)


def post(mgr, hotkey_id):
    """Fire a shortcut the way Windows does, by posting WM_HOTKEY to the pump thread."""
    ctypes.windll.user32.PostThreadMessageW(mgr._tid, hotkeys.WM_HOTKEY, hotkey_id, 0)


def main():
    return _run_checks()


def _run_checks():
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
    # THE WHOLE RUN IS ON CTRL+SHIFT, a chord nothing ships on: the real app is very likely RUNNING
    # while this suite is, holding its own chord (hotkeys.DEFAULT_MODS). A saved chord with no
    # saved shortcuts is how a test gets there, and the defaults land on it.
    mgr.load({"mods": ["ctrl", "shift"]},
             defaults=[("one", hotkeys.DEFAULT_MODS, "F9", True),
                       ("two", hotkeys.DEFAULT_MODS, "F10", True)])
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

    # --- firing one ------------------------------------------------------------------------------
    fired.clear()
    post(mgr, 1)
    for _ in range(30):
        if fired:
            break
        time.sleep(0.1)
    check("a fired hotkey ran the command", fired == ["one"], str(fired))
    time.sleep(0.3)
    check("and its reply was spoken", said and said[-1] == "ran one", str(said))

    # --- adding, and the clash rules -------------------------------------------------------------
    res = mgr.add("two")
    check("a shortcut can be added, on a free key", res["ok"], res["message"])
    added = mgr.get(res["id"])
    check("the new one is on the shared chord and switched on",
          added["enabled"] and added["mods"] == ["ctrl", "shift"], str(added))
    # The default chord's first free key may be held by ANOTHER PROGRAM on this machine (a running
    # AccessCaster holds its own shortcuts), so only a clash with our own list is a failure here.
    problem = mgr.state()["shortcuts"][-1]["problem"]
    check("adding does not collide with our own shortcuts",
          not problem or "another program" in problem, problem)
    check("and what add() reports is the registration it just made, not the previous one",
          (problem in res["message"]) if problem else ("Added Test two on" in res["message"]),
          res["message"])
    first = mgr.state()["shortcuts"][0]["id"]
    added_id = res["id"]
    res = mgr.set_binding(added_id, "F9", True)
    check("a clash with our OWN shortcut is refused, and names it",
          not res["ok"] and "Test one" in res["message"], res["message"])
    mgr.set_binding(added_id, "F8", True)
    res = mgr.add("one", key="F8")
    check("adding on a key already in use is refused, and names it",
          not res["ok"] and "Test two" in res["message"], res["message"])
    res = mgr.add("one", key="F7")
    check("adding on a chosen free key puts it there",
          res["ok"] and mgr.get(res["id"])["key"] == "F7", res["message"])
    mgr.remove(res["id"])

    # --- staying quiet, per shortcut -------------------------------------------------------------
    said.clear()
    fired.clear()
    mgr.set_binding(first, "F9", True, speak=False)
    post(mgr, 1)
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
    mgr.set_binding(boom["id"], "F11", True, speak=False)
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
    # Read from the manager's own state: a posted message runs the action whatever the
    # registration says, so it cannot show the switch working.
    mgr.set_enabled(False)
    time.sleep(0.4)               # the re-registration happens on the pump, not in this call
    check("the master switch reports off", not mgr.state()["enabled"])
    mgr.set_enabled(True)
    time.sleep(0.4)
    check("and on again, with everything re-registered", mgr.state()["enabled"]
          and not [s for s in mgr.state()["shortcuts"] if s["problem"]],
          str([(s["combo"], s["problem"]) for s in mgr.state()["shortcuts"]]))

    # --- the shared chord -----------------------------------------------------------------------
    res = mgr.set_mods(["shift"])
    check("a chord without Ctrl, Alt or Windows is refused",
          not res["ok"] and mgr.state()["mods"] == ["ctrl", "shift"], res["message"])
    res = mgr.set_mods(["ctrl", "alt", "win"])
    check("changing the chord moves EVERY shortcut to it", res["ok"] and all(
        s["mods"] == ["ctrl", "alt", "win"] for s in mgr.state()["shortcuts"]), res["message"])
    mgr.set_mods(["ctrl", "shift"])

    # --- what gets persisted, and reloading it ---------------------------------------------------
    saved = mgr.saveable()
    check("only data is persisted, no callables",
          set(saved) == {"enabled", "mods", "shortcuts"}
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
    check("the saved chord loads back", fresh.state()["mods"] == ["ctrl", "shift"])
    res = fresh.reset([("one", hotkeys.DEFAULT_MODS, "F9", True)])
    check("restoring the defaults replaces the list and the chord",
          res["ok"] and [s["command"] for s in fresh.state()["shortcuts"]] == ["one"]
          and fresh.state()["mods"] == list(hotkeys.DEFAULT_MODS), res["message"])

    mgr.stop()
    time.sleep(0.3)
    check("stopping releases the keys", not mgr.state()["running"])

    print()
    print(f"{len(FAILURES)} failed" if FAILURES else "All checks passed.")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
