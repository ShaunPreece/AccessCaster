"""
AccessCaster - the global keyboard shortcuts: the manager that joins the keys to what they do, the
shipped defaults, and the Keyboard shortcuts page's state and actions.

The Windows plumbing is hotkeys.py, what a shortcut can do is commands.py, and the pad shortcuts are
pad_shortcuts.py. This module does not import the server; commands does, to run what a key asks
for.
"""

from __future__ import annotations

import announcer
import commands  # the catalogue of what a shortcut can do, from the spec tables
import devices
import diagnostics
import hotkeys  # system-wide keyboard shortcuts: the Win32 half (keys, no meaning)
import settings_file
import speech
import wording

# WHICH PAD TYPES A SHORTCUT MAY BE POINTED AT lives in `commands.PAD_TYPES`: sound, voice FX and
# mixer.

# WHAT A FRESH INSTALL GETS. (command, modifiers, key, speak). Only laid down when nothing has been
# saved, so deleting them stays deleted.
#
# THE NUMBER ROW IS THE PAD SCHEME, and it is one row you can find by touch: 1 to N play the pads of
# the current bank, and Dash and Equals - which sit at the END of that same row - step the bank down
# and up. Deliberately NO NUMPAD KEYS, which rules out the otherwise obvious numpad pair for bank
# down/up; brackets lose to Dash and Equals because they are not on the number row.
#
# N IS PER-DEVICE. Eight on a Pro II, six on a Duo, taken from the device table and never
# hard-coded, so a Duo owner is not given two keys that can only ever say "there is no pad 7".
def _pad_scheme() -> list:
    """The pad half, on its own, because it can also be added to a list that already exists
    (`action_add_pad_shortcuts`) rather than only shipped to a fresh install."""
    per_bank = devices.get_active_device().pad.pads_per_bank
    rows = ([("pad.playing", hotkeys.DEFAULT_MODS, "P"),
             ("pad.bank:down", hotkeys.DEFAULT_MODS, hotkeys.KEY_DASH),
             ("pad.bank:up", hotkeys.DEFAULT_MODS, hotkeys.KEY_EQUALS)]
            + [(f"pad.slot:{n}", hotkeys.DEFAULT_MODS, str(n)) for n in range(1, per_bank + 1)])
    # Which of these speak is `commands.speaks_by_default`, so the shipped set and a hand-added
    # shortcut agree. The pad keys arrive silent: the pad IS the feedback.
    return [(cid, mods, key, commands.speaks_by_default(cid)) for cid, mods, key in rows]


def defaults() -> list:
    return [
        ("app.announcer", hotkeys.DEFAULT_MODS, "A", True),
        ("app.mics", hotkeys.DEFAULT_MODS, "M", True),
        ("rec.toggle", hotkeys.DEFAULT_MODS, "R", True),
        ("app.state", hotkeys.DEFAULT_MODS, "S", True),
        ("app.window", hotkeys.DEFAULT_MODS, "O", True),
        ("callme.answer", hotkeys.DEFAULT_MODS, "C", True),
        ("callme.end", hotkeys.DEFAULT_MODS, "D", True),
    ] + _pad_scheme()


_manager = None


def manager():
    """The one manager, built on first use and given the saved shortcuts. Built lazily so importing
    this module never registers a system-wide hotkey by surprise: only `start` asks for it, and
    only when the app is really running.

    The manager holds keys and knows nothing about the desk; `commands` holds what a shortcut can
    DO. The two meet here, and only here."""
    global _manager
    if _manager is None:
        # Replies go through speech.say, which enforces the announcer switch: a recording's
        # suspension silences the very keys you reach for mid-take, and a shortcut that FAILS is
        # silent too. Turning announcements back ON still speaks, because announcer.action_set_announce(True)
        # has run by the time the reply is handed over.
        mgr = hotkeys.HotkeyManager(speak=speech.say,
                                    runner=commands.run,
                                    labeller=_label,
                                    fail=_failed)
        mgr.load(settings_file.load().get("hotkeys") or {},
                 defaults=defaults())
        _manager = mgr
    return _manager


def start() -> None:
    """Register the saved shortcuts system-wide. A failure is reported and never raised: no
    shortcut is worth refusing to start the app."""
    try:
        mgr = manager()
        mgr.start()
        live = [a["combo"] for a in mgr.state()["shortcuts"]
                if a["enabled"] and not a["problem"]]
        print(f"Keyboard shortcuts registered: {len(live)}.")
    except Exception as exc:
        print(f"Keyboard shortcuts unavailable: {exc}")


def stop() -> None:
    """Unregister them. Left registered, they keep taking those keys from every other program with
    nothing left running to act on them. Never builds a manager just to stop it."""
    if _manager is not None:
        _manager.stop()


# The catalogue is generated from live spec tables, and generating it can touch the desk (pads,
# presets), so labels are CACHED: `state()` asks for one per shortcut and the page reads state
# often. Cleared whenever the catalogue is re-read.
_labels = {}


def _failed(message: str) -> None:
    """A shortcut that failed: spoken, or with announcements off, which silence every phrase so no
    words reach the mix, the "failed" cue, so a press that did nothing never passes for one that
    worked. Silent while a take holds announcements off (`suspended_for_record`): nothing the app
    plays may land in the recording."""
    if speech.enabled.is_set():
        speech.say(message)
    elif announcer.suspended_for_record or not speech.tone("failed", despite_off=True):
        diagnostics.event("say", message[:90] + "  [suppressed: announcements off]")


def _label(command_id: str) -> str:
    """The label a stored command id shows. A miss fills the cache from ONE catalogue, so a list of
    uncached shortcuts costs one build rather than one each.

    A shortcut bound to something that no longer exists (a deleted mix scheme, a cleared pad) keeps
    its place in the list and SAYS so, rather than vanishing and leaving a key mysteriously taken."""
    label = _labels.get(command_id)
    if label is None:
        try:
            for c in commands.catalogue(include_device=True):
                _labels[c["id"]] = c["label"]
            label = _labels.get(command_id, f"{command_id} (not available now)")
        except Exception as exc:
            diagnostics.event("hotkeys.labels_unread", "shortcut labels could not be built",
                              error=str(exc))
            label = command_id
        _labels[command_id] = label
    return label


def read_state() -> dict:
    return _page_state(manager())


def _pad_set_commands() -> set:
    return {cid for cid, _mods, _key, _speak in _pad_scheme()}


def _page_state(mgr) -> dict:
    """The manager's state plus `pad_set`: "all", "some" or "none" of the pad scheme is bound,
    which is what the "Use the SMART pad keys" switch shows (ticked, mixed or clear)."""
    state = mgr.state()
    want = _pad_set_commands()
    have = want & {row["command"] for row in state["shortcuts"]}
    state["pad_set"] = "all" if have == want else ("some" if have else "none")
    # What "Restore default shortcuts" would leave, for its confirmation to say.
    state["default_count"] = len(defaults()) - len(want)
    state["default_pad_count"] = len(want)
    return state


def read_catalogue() -> dict:
    """The whole catalogue of bindable commands, grouped by category for the Add dialog.

    THIS ONE READS THE DESK (pads and presets), which is why it is not part of `read_state`: the
    shortcut list has to answer at once and with the unit unplugged, and only the Add dialog needs
    the catalogue. A read that fails still returns the device-free half rather than nothing."""
    try:
        items = commands.catalogue(include_device=True)
        complete = True
    except Exception:
        items = commands.catalogue(include_device=False)
        complete = False
    _labels.clear()
    for c in items:
        _labels[c["id"]] = c["label"]
    groups = []
    for cat in commands.CATEGORIES:
        rows = [c for c in items if c["category"] == cat]
        if rows:
            groups.append({"category": cat, "commands": rows})
    msg = f"{len(items)} things can be put on a shortcut."
    if not complete:
        msg += " The desk could not be read, so pads and presets are not listed."
    return {"ok": True, "groups": groups, "count": len(items), "complete": complete,
            "message": msg}


def action_set(id: str, key: str, enabled: bool, speak: bool = True) -> dict:
    """Change one shortcut, and persist it only if it was accepted. The manager validates and
    registers first, so the settings file can never hold a binding that does not work."""
    mgr = manager()
    res = mgr.set_binding(str(id or ""), str(key or ""), bool(enabled), bool(speak))
    if res.get("ok"):
        settings_file.update(hotkeys=mgr.saveable())
    res["state"] = _page_state(mgr)
    return res


def action_set_mods(mods) -> dict:
    """Change the keys held for EVERY shortcut."""
    mgr = manager()
    res = mgr.set_mods(list(mods or []))
    if res.get("ok"):
        settings_file.update(hotkeys=mgr.saveable())
    res["state"] = _page_state(mgr)
    return res


def action_add(command: str, key: str = "") -> dict:
    """Add a shortcut for a catalogue command, on `key` or else the first free key, already
    working."""
    mgr = manager()
    res = mgr.add(str(command or ""), speak=commands.speaks_by_default(command),
                  key=str(key or ""))
    if res.get("ok"):
        settings_file.update(hotkeys=mgr.saveable())
    res["state"] = _page_state(mgr)
    return res


def action_add_pad_shortcuts() -> dict:
    """Put the whole pad scheme onto this machine's shortcut list, on request.

    THE ONE THING A SHIPPED DEFAULT CANNOT DO. Defaults land only on a machine that has saved
    nothing, so anybody who has already chosen their own keys never gets it. This is the "Use the SMART pad keys" switch ticked, so it stays an explicit choice
    rather than a start-up surprise, and it can be ticked again after switching desks - a Duo has six
    pads in a bank where a Pro II has eight, and the missing two are added without disturbing
    anything."""
    mgr = manager()
    res = mgr.add_scheme(_pad_scheme())
    settings_file.update(hotkeys=mgr.saveable())
    added, had, clashed = res["added"], res["already_there"], res["clashed"]
    bits = []
    if added:
        bits.append(f"Added {wording.count(len(added), 'pad shortcut')}.")
    if had:
        bits.append(f"{len(had)} {wording.plural(len(had), 'was', 'were')} already there.")
    for command, key, taken_by in clashed:
        bits.append(f"{hotkeys.combo_text(mgr.state()['mods'], key)} is already used for "
                    f"{_label(taken_by)}, so {_label(command)} was not added.")
    if not bits:
        bits.append("Every pad shortcut was already there.")
    return {"ok": True, "message": " ".join(bits), "state": _page_state(mgr)}


def action_restore_defaults() -> dict:
    """Replace every shortcut with what a fresh install has, keys to hold included. The page asks
    first: anything added or changed is lost."""
    mgr = manager()
    res = mgr.reset(defaults())
    settings_file.update(hotkeys=mgr.saveable())
    res["state"] = _page_state(mgr)
    return res


def action_set_pad_shortcuts(on: bool) -> dict:
    """The "Use the SMART pad keys" switch: on adds whatever of the pad scheme is missing, off
    deletes every shortcut for a pad-scheme command, on whatever key it had been moved to."""
    if on:
        return action_add_pad_shortcuts()
    mgr = manager()
    want = _pad_set_commands()
    gone = [row for row in mgr.state()["shortcuts"] if row["command"] in want]
    for row in gone:
        mgr.remove(row["id"])
    settings_file.update(hotkeys=mgr.saveable())
    msg = (f"Removed {wording.count(len(gone), 'pad shortcut')}." if gone
           else "There were no pad shortcuts to remove.")
    return {"ok": True, "message": msg, "state": _page_state(mgr)}


def action_delete(id: str) -> dict:
    mgr = manager()
    res = mgr.remove(str(id or ""))
    if res.get("ok"):
        settings_file.update(hotkeys=mgr.saveable())
    res["state"] = _page_state(mgr)
    return res


def action_set_enabled(on: bool) -> dict:
    """The master switch. Off unregisters every combination, so nothing is held from other
    programs: the point of an off switch is that it really lets go."""
    mgr = manager()
    res = mgr.set_enabled(bool(on))
    settings_file.update(hotkeys=mgr.saveable())
    res["state"] = _page_state(mgr)
    return res
