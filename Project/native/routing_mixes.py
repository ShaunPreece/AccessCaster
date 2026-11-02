"""Routing's Output mixes page: which output to look at, the mix type it carries and, on a Custom
mix, a switch per source. Then the mix presets and the reset of every output.
"""
import wx
import wx.lib.scrolledpanel as scrolled

import server
from native import a11y
from native.calls import call
from native.controls import Switch, set_choices
from native.desk_state import CUSTOM, MIX_MINUS
from native.dialogs import confirm
from native.pages import group, labelled, page_box

TITLE = "Output mixes"
READ_ONLY = "Changing output mixes is not enabled on this device yet."


class OutputMixesPage(scrolled.ScrolledPanel):
    """Which output to look at, the mix it carries and, on a Custom mix, a switch per source. Then
    the mix presets and the reset of every output."""

    def __init__(self, section, model, previous=None):
        super().__init__(section.frame.host)
        self.Hide()
        self.section = section
        self.frame = section.frame
        self.model = model
        self.outputs = model["outputs"]
        self.read_only = model["read_only"]
        self._busy = False
        self._schemes = []
        sizer, parent = page_box(self, TITLE)

        self.output = labelled(parent, sizer, "Output to configure", lambda p: wx.Choice(
            p, choices=[o["label"] for o in self.outputs]))
        self.output.SetSelection(previous.output.GetSelection() if previous else 0)
        self.output.Bind(wx.EVT_CHOICE, lambda _e: self._show_output())

        self.mode = labelled(parent, sizer, "Mix type for this output", lambda p: wx.Choice(p))
        self.mode_acc = a11y.describe(self.mode)
        # Applied by the button, never by the list: arrowing a closed list fires a change per item,
        # and leaving Custom discards that output's custom mix.
        apply_mode = wx.Button(parent, label="Confirm mix type change")
        apply_mode.Bind(wx.EVT_BUTTON, self._on_apply_mode)
        sizer.Add(apply_mode, 0, wx.ALL, 6)

        self.sources_sizer, sources = group(parent, sizer, "Sources")
        self.sources_box = sources
        self.switches = {}
        for source in model["sources"]:
            self.switches[source["index"]] = Switch(
                self, sources, self.sources_sizer, source["label"],
                send=lambda on, s=source["index"]: self._send_cross_point(s, on),
                current=lambda s=source["index"]: self._included(s),
                remember=lambda _on: None, accepted=self._cross_point_done,
                guard=lambda: READ_ONLY if self.read_only else None)

        self._build_schemes(parent, sizer)
        self._build_reset(parent, sizer)
        self._show_output()
        self.SetupScrolling(scroll_x=False)
        self.read_schemes()

    # ---- the output shown ------------------------------------------------------------------

    @property
    def current(self):
        return self.outputs[max(self.output.GetSelection(), 0)]

    @property
    def matrix(self):
        return self.section.desk.matrix

    def _mode(self):
        """The current output's mode as the desk last reported it, or None."""
        if not self.matrix.get("ok"):
            return None
        return self.matrix["modes"].get(self.current["index"])

    def _included(self, source):
        excluded = (self.matrix.get("excluded") or {}).get(self.current["index"]) or {}
        return None if source not in excluded else not excluded[source]

    def _send_cross_point(self, source, on):
        """On a worker thread, so the output is the one `_show_output` last put on the page rather
        than a read of the list."""
        output = self._output_index
        reply = server.action_set_cross_point(output, source, not on)
        return dict(reply, output=output, source=source, excluded=not on)

    def _cross_point_done(self, reply):
        excluded = (self.matrix.get("excluded") or {}).get(reply["output"])
        if excluded is not None:
            excluded[reply["source"]] = reply["excluded"]

    def _show_output(self):
        """Put the chosen output on every control: its mix, its sources' names and states, and the
        one-output save button's label."""
        out = self.current
        self._output_index = out["index"]
        for source in self.model["sources"]:
            label = (f"{source['label']} back into itself" if source["key"] == out["key"]
                     else f"{source['label']} in {out['label']}")
            switch = self.switches[source["index"]]
            if switch.box.GetLabel() != label:
                switch.box.SetLabel(label)
        self.sources_box.SetLabel(f"Sources in {out['label']}")
        self.save_output.SetLabel(f"Save only the {out['label']} mix as a preset")
        set_choices(self.mode, [label for _value, label in out["modes"]])
        self._show_mode()

    def _show_mode(self):
        out = self.current
        mode = self._mode()
        values = [value for value, _label in out["modes"]]
        self.mode.SetSelection(values.index(mode) if mode in values else wx.NOT_FOUND)
        label = dict(out["modes"]).get(mode)
        if mode is None:
            self.mode_acc.description = (self.matrix.get("reason")
                                         or "The unit's current mix could not be read.")
        elif mode == MIX_MINUS:
            self.mode_acc.description = f"{label}: every source except this output's own."
        elif mode == CUSTOM:
            self.mode_acc.description = f"{label}: only the sources switched on below."
        else:
            self.mode_acc.description = f"{label}: every source."
        for switch in self.switches.values():
            switch.show()
        # The sources are shown only on a Custom mix. On any other the desk ignores the
        # cross-points it has stored, so the switches would describe a mix the output does not
        # carry.
        custom = mode == CUSTOM
        if custom != self.sources_sizer.GetStaticBox().IsShown():
            focus = wx.Window.FindFocus()
            if not custom and focus is not None and self.sources_box.IsDescendant(focus):
                self.mode.SetFocus()
            self.sources_sizer.ShowItems(custom)
            self.sources_box.Show(custom)
            self.Layout()
            self.SetupScrolling(scroll_x=False, scrollToTop=False)

    def show(self):
        """Put the desk state's mixes on every control, and read the presets again."""
        self._show_mode()
        self.read_schemes()

    def show_outputs(self, outputs):
        """A push changed the mode or mix of `outputs`."""
        if self.current["index"] in outputs:
            self._show_mode()

    # ---- writes ----------------------------------------------------------------------------

    def _refused(self):
        """A sentence when the matrix cannot be written now, or None."""
        if self.read_only:
            return READ_ONLY
        if self._busy:
            return "Still changing the output mixes. Wait for that to finish."
        return None

    def _on_apply_mode(self, event):
        source = event.GetEventObject()
        refusal = self._refused()
        index = self.mode.GetSelection()
        if refusal or index == wx.NOT_FOUND:
            self.frame.say(refusal or "Choose a mix type first.")
            return
        out = self.current
        mode = out["modes"][index][0]
        if mode == self._mode():
            self.frame.say(f"{out['label']} is already on {out['modes'][index][1]}.")
            return

        def applied(reply):
            self.frame.reply(source, reply)
            if reply.get("ok"):
                # Leaving or entering Custom moves the output's cross-points too.
                self.section.desk.refresh()
            else:
                self._show_mode()
        call(self, server.action_set_output_mode, out["index"], mode, done=applied)

    def _run(self, source, busy_text, fn, *args):
        """A write that takes several seconds (a mix preset, the reset): spoken as it starts and as
        it ends, and the mixes read again after it."""
        self._busy = True
        self.frame.say(busy_text)

        def done(reply):
            self._busy = False
            self.frame.reply(source, reply)
            self.section.desk.refresh()
        call(self, fn, *args, done=done)

    # ---- mix presets -----------------------------------------------------------------------

    def _build_schemes(self, parent, sizer):
        box_sizer, box = group(parent, sizer, "Mix presets")
        self.scheme = labelled(box, box_sizer, "Mix preset", lambda p: wx.Choice(p))
        a11y.describe(self.scheme, "The output mixes saved under a name: which sources each "
                                   "output carries, and at what level. Never your channels, "
                                   "processing or pads.")
        self.scheme.Bind(wx.EVT_CHOICE, lambda _e: self._show_scheme())
        row = wx.BoxSizer(wx.HORIZONTAL)
        apply = wx.Button(box, label="Apply this preset")
        apply.Bind(wx.EVT_BUTTON, self._on_apply_scheme)
        # The selected preset's description is read on the buttons that act on it rather than on
        # the list, so arrowing the list reads only the names.
        self.apply_acc = a11y.describe(apply)
        delete = wx.Button(box, label="Delete this preset")
        delete.Bind(wx.EVT_BUTTON, self._on_delete_scheme)
        self.delete_acc = a11y.describe(delete)
        row.Add(apply, 0, wx.RIGHT, 8)
        row.Add(delete, 0)
        box_sizer.Add(row, 0, wx.ALL, 6)

        self.scheme_name = labelled(box, box_sizer, "Name for a new preset",
                                    lambda p: wx.TextCtrl(p, size=(260, -1)))
        self.scheme_name.SetMaxLength(40)
        a11y.describe(self.scheme_name, "1 to 40 letters, digits, spaces, dashes or underscores. "
                                        "Saves the mixes as they are now, levels included.")
        save_all = wx.Button(box, label="Save every output's mix as a preset")
        save_all.Bind(wx.EVT_BUTTON, lambda e: self._on_save_scheme(e, False))
        # One output's preset leaves every other output alone when applied, so it is the kind
        # that is safe mid-show. Its label names the output chosen at the top of the page.
        self.save_output = wx.Button(box, label="Save only this output's mix as a preset")
        self.save_output.Bind(wx.EVT_BUTTON, lambda e: self._on_save_scheme(e, True))
        box_sizer.Add(save_all, 0, wx.ALL, 6)
        box_sizer.Add(self.save_output, 0, wx.ALL, 6)

    def read_schemes(self, select=None):
        """Read the preset list (files, not the desk). `select` is a saved preset's name to choose
        once it is there."""
        def read(reply):
            if reply.get("ok"):
                self._fill_schemes(reply, select)
        call(self, server.read_routing_schemes, done=read)

    def _fill_schemes(self, data, select):
        chosen = self._scheme()
        schemes = [{"builtin": True, "name": b["id"], "label": b["name"],
                    "text": b["name"] + (f" ({b['scope']})" if b.get("scope") else ""),
                    "description": b["description"], "destructive": b["destructive"]}
                   for b in data.get("builtins") or []]
        schemes += [{"builtin": False, "name": s["name"], "label": s["name"],
                     "text": f"{s['name']} (saved" + (f", {s['scope']}" if s.get("scope") else "")
                             + ")",
                     "description": s.get("description") or "", "destructive": False}
                    for s in data.get("saved") or []]
        self._schemes = schemes
        set_choices(self.scheme, [s["text"] for s in schemes])
        keys = [(s["builtin"], s["name"]) for s in schemes]
        want = (False, select) if select else (
            (chosen["builtin"], chosen["name"]) if chosen else None)
        self.scheme.SetSelection(keys.index(want) if want in keys else (0 if keys else
                                                                         wx.NOT_FOUND))
        self._show_scheme()

    def _scheme(self):
        index = self.scheme.GetSelection()
        return self._schemes[index] if 0 <= index < len(self._schemes) else None

    def _show_scheme(self):
        scheme = self._scheme()
        text = scheme["description"] if scheme else "No mix presets on this device."
        self.apply_acc.description = text
        self.delete_acc.description = (
            "Built-in presets cannot be deleted." if scheme and scheme["builtin"] else text)

    def _on_apply_scheme(self, event):
        source = event.GetEventObject()
        scheme = self._scheme()
        refusal = self._refused()
        if refusal or scheme is None:
            self.frame.say(refusal or "There is no mix preset to apply.")
            return
        if scheme["destructive"] and not confirm(
                self, f"Apply {scheme['label']}?",
                f"{scheme['description']}\n\nIt is reversible by setting the mixes again, by "
                "applying another preset, or by restoring a saved profile.", "Apply preset"):
            self.frame.say("Cancelled. The output mixes were not changed.")
            return
        self._run(source, f"Applying {scheme['label']}, please wait.",
                  server.action_apply_routing_scheme, scheme["name"], scheme["builtin"])

    def _on_save_scheme(self, event, one_output):
        source = event.GetEventObject()
        if self.read_only:
            self.frame.say(READ_ONLY)
            return
        name = self.scheme_name.GetValue().strip()
        if not name:
            self.frame.say("Type a name for the preset first.")
            self.scheme_name.SetFocus()
            return
        out = self.current if one_output else None
        exists = any(not s["builtin"] and s["name"] == name for s in self._schemes)
        if exists and not confirm(
                self, f"Replace the mix preset {name}?",
                "A saved preset of that name already exists. Saving replaces it with "
                + (f"the {out['label']} mix" if out else "the unit's mixes") + " as it is now.",
                "Replace preset"):
            self.frame.say("Cancelled. The saved preset was not changed.")
            return

        def saved(reply):
            self.frame.reply(source, reply)
            if reply.get("ok"):
                self.scheme_name.SetValue("")
                self.read_schemes(select=reply.get("name"))
        call(self, server.action_save_routing_scheme, name, out["key"] if out else "",
             done=saved)

    def _on_delete_scheme(self, event):
        source = event.GetEventObject()
        scheme = self._scheme()
        if scheme is None or scheme["builtin"]:
            self.frame.say("Built-in presets cannot be deleted. Choose a saved one.")
            return
        if not confirm(self, f"Delete the mix preset {scheme['label']}?",
                        "This removes the saved preset from this computer. The unit's mixes are "
                        "not changed.", "Delete preset"):
            self.frame.say("Cancelled. The preset was not deleted.")
            return

        def deleted(reply):
            self.frame.reply(source, reply)
            if reply.get("ok"):
                self.read_schemes()
        call(self, server.action_delete_routing_scheme, scheme["name"], done=deleted)

    # ---- reset -----------------------------------------------------------------------------

    def _build_reset(self, parent, sizer):
        box_sizer, box = group(parent, sizer, "Reset all output mixes")
        reset = wx.Button(box, label="Reset all output mixes")
        a11y.describe(reset, "Headphones, monitor, recording and USB 1 go back to Main Mix; "
                             "Bluetooth, Chat, USB 2 and CallMe to Mix Minus, all following the "
                             "faders. Wipes any custom mix.")
        reset.Bind(wx.EVT_BUTTON, self._on_reset)
        box_sizer.Add(reset, 0, wx.ALL, 6)

    def _on_reset(self, event):
        source = event.GetEventObject()
        refusal = self._refused()
        if refusal:
            self.frame.say(refusal)
            return
        if not confirm(self, "Reset all output mixes?",
                        "This puts every output back to its normal mix and every source back in "
                        "each output, following the faders. Any custom mixes you have set are "
                        "wiped, including Chat with only your mic.\n\nIt is reversible by setting "
                        "them again, or by restoring a saved profile.", "Reset outputs"):
            self.frame.say("Cancelled. The output mixes were not changed.")
            return
        self._run(source, "Resetting every output mix, please wait.",
                  server.action_reset_all_outputs)
