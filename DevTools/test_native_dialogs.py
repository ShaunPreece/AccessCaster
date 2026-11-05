"""The native window's dialogs, offline: each is read from the focus Windows gives it and from
nothing else. A dialog activating focuses its first focusable control; a constructor that also
calls SetFocus raises a second focus event, and the screen reader reads the control twice. So the
control to land on comes first, and no dialog's constructor focuses anything. The source check
walks every wx.Dialog in Project/native, so a new dialog is held to it without being listed here.
The pad dialogs' first control is checked in test_native_pads. A job dialog's ticking runs for
exactly as long as the dialog, and not at all when it is told not to. Built hidden: nothing is
shown.

Run: python DevTools/test_native_dialogs.py
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil
import sys

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _a11y
import _testkit
import native
import wx
from _testkit import check, check_true, patched
from native import callme_section, dialogs, routing_mixes, shortcuts_page, update_dialogs

app = wx.App()


def first_focusable(window):
    for child in window.GetChildren():
        if isinstance(child, (wx.Panel, wx.StaticBox)):
            found = first_focusable(child)
            if found is not None:
                return found
        elif child.AcceptsFocus() and child.IsShown() and child.IsEnabled():
            return child
    return None


modules = [importlib.import_module(f"native.{info.name}")
           for info in pkgutil.iter_modules(native.__path__)]
classes = {cls for module in modules for _name, cls in inspect.getmembers(module, inspect.isclass)
           if issubclass(cls, wx.Dialog) and cls.__module__.startswith("native.")}
check_true("the walk finds the dialogs", len(classes) >= 6, str(classes))
for cls in sorted(classes, key=lambda c: c.__qualname__):
    check_true(f"{cls.__qualname__} focuses nothing from its constructor",
               "SetFocus" not in inspect.getsource(cls.__init__))

frame = wx.Frame(None)

dialog = dialogs.TextDialog(frame, "Clear this pad?", "This empties pad 1, bank 1.",
                            [(wx.ID_OK, "Clear"), (wx.ID_CANCEL, "Cancel")],
                            default=wx.ID_CANCEL, escape=wx.ID_CANCEL)
check("a question lands on its default, here Cancel", first_focusable(dialog).GetLabel(), "Cancel")
dialog.Destroy()

dialog = dialogs.TextDialog(frame, "Gain set", "Mic 1 is at 40 dB.",
                            [(wx.ID_OK, "OK"), (wx.ID_UNDO, "Undo")],
                            default=wx.ID_OK, escape=wx.ID_OK)
check("a default already first stays first", first_focusable(dialog).GetLabel(), "OK")
dialog.Destroy()

for closable, label in ((True, "Close"), (False, "Please wait")):
    dialog = dialogs.JobDialog(frame, "Changing pad 1, bank 1", "Clearing.", lambda _d: None,
                               stop=None, closable=closable)
    check(f"a job dialog lands on {label}", first_focusable(dialog).GetLabel(), label)
    dialog.Destroy()


class FakeLoop:
    stopped = False

    def stop(self):
        self.stopped = True


for ticks in (True, False):
    started, loop = [], FakeLoop()
    dialog = dialogs.JobDialog(frame, "Clearing all pads", "Clearing.",
                               lambda d: d.finish({"ok": True}), stop=None, closable=False,
                               ticks=ticks)
    with patched((dialogs.speech, {"start_loop": lambda name, delay: started.append(name) or loop})):
        dialog.run()
    dialog.Destroy()
    if ticks:
        check_true("a job ticks while it runs and stops when it ends",
                   started == ["working"] and loop.stopped)
    else:
        check("a job told not to tick never starts", started, [])

# A dialog opened over a running job (the tray's Still working) holds the innermost loop, so a job
# finishing meanwhile has to wait for it to close: ending the job's loop first is a wx assertion
# that leaves it running.
order = []


def open_over(job):
    over = dialogs.TextDialog(frame, "Still working", "A backup is running.", [(wx.ID_OK, "OK")],
                              default=wx.ID_OK, escape=wx.ID_OK)

    def finish_under():
        job.finish({"ok": True})
        order.append("finished")
        wx.CallLater(300, lambda: (order.append("closed"), over.EndModal(wx.ID_OK)))

    wx.CallAfter(finish_under)
    over.ShowModal()
    over.Destroy()


def unstick():
    # Turns a regression into a failure instead of a hang.
    for window in reversed(dialogs.modal_dialogs()):
        order.append("stuck")
        window.EndModal(wx.ID_CANCEL)
        return


loop = FakeLoop()
dialog = dialogs.JobDialog(frame, "Backing up", "Backing up.", open_over, stop=None,
                           closable=False)
watchdog = wx.CallLater(5000, unstick)
with patched((dialogs.speech, {"start_loop": lambda name, delay: loop})):
    result = dialog.run()
watchdog.Stop()
dialog.Destroy()
check("a job finished under another dialog ends once that one closes",
      (result, order, loop.stopped), ({"ok": True}, ["finished", "closed"], True))

dialog = dialogs.DeletePresetDialog(frame, ["Show"])
check_true("Delete a preset lands on its list", first_focusable(dialog) is dialog.list)
dialog.Destroy()

dialog = routing_mixes.SavePresetDialog(frame, "Headphones 1")
check_true("Save as a preset lands on the name", first_focusable(dialog) is dialog.name_box)
dialog.Destroy()

dialog = callme_section.ContactDialog(frame, None)
check_true("Add a contact lands on the name", first_focusable(dialog) is dialog.name_box)
dialog.Destroy()

shortcut_state = {"keys": ["A", "B"], "mods": ["ctrl"], "shortcuts": [],
                  "modifiers": [{"id": "ctrl", "label": "Ctrl"}, {"id": "alt", "label": "Alt"}]}
shortcut = {"id": "1", "label": "Record", "key": "B", "enabled": True, "speak": True,
            "problem": "Another program has it."}
dialog = shortcuts_page.EditDialog(frame, shortcut_state, shortcut)
check_true("Edit shortcut lands on its key, after the problem", first_focusable(dialog) is dialog.key)
check("on the key it has", dialog.key.GetSelection(), 1)
dialog.Destroy()

dialog = shortcuts_page.ModsDialog(frame, shortcut_state)
check("Keys to hold lands on the first key", first_focusable(dialog).GetLabel(), "Ctrl")
dialog.Destroy()

dialog = shortcuts_page.AddDialog(frame, shortcut_state, {"complete": True, "groups": [
    {"category": "Recording", "commands": [{"id": "rec", "label": "Record"}]}]})
check_true("Add a shortcut lands on the command type", first_focusable(dialog) is dialog.category)
dialog.Destroy()

dialog = update_dialogs.DecisionDialog(frame, {"title": "Install AccessCaster 2.64.0?",
                                               "summary": "A new version.", "notes": "Fixes.",
                                               "after": "It restarts."})
check("The update lands on what changed", first_focusable(dialog).GetValue(), "Fixes.")
dialog.Destroy()

frame.Destroy()
_a11y.check_windows("Dialogs")
sys.exit(_testkit.finish())
