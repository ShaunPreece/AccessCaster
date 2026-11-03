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
from native.dialogs import DeletePresetDialog, confirm
from native.pages import group, labelled, page_body

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
        sizer, parent = page_body(self)

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
        """Put the chosen output on every control: its mix, and its sources' names and states."""
        out = self.current
        self._output_index = out["index"]
        for source in self.model["sources"]:
            label = (f"{source['label']} back into itself" if source["key"] == out["key"]
                     else f"{source['label']} in {out['label']}")
            switch = self.switches[source["index"]]
            if switch.box.GetLabel() != label:
                switch.box.SetLabel(label)
        self.sources_box.SetLabel(f"Sources in {out['label']}")
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
        self.scheme = labelled(parent, sizer, "Mix preset", lambda p: wx.Choice(p))
        a11y.describe(self.scheme, "Output mixes only, never channels, processing or pads.")
        self.scheme.Bind(wx.EVT_CHOICE, lambda _e: self._show_scheme())
        apply = wx.Button(parent, label="Apply preset")
        apply.Bind(wx.EVT_BUTTON, self._on_apply_scheme)
        # The selected preset's description is read on the button that acts on it rather than on
        # the list, so arrowing the list reads only the names.
        self.apply_acc = a11y.describe(apply)
        save = wx.Button(parent, label="Save as a preset...")
        save.Bind(wx.EVT_BUTTON, self._on_save_scheme)
        delete = wx.Button(parent, label="Delete a preset...")
        delete.Bind(wx.EVT_BUTTON, self._on_delete_scheme)
        for button in (apply, save, delete):
            sizer.Add(button, 0, wx.ALL, 6)

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
                    "text": _scheme_text(b["name"], b.get("scope")),
                    "description": b["description"], "destructive": b["destructive"]}
                   for b in data.get("builtins") or []]
        schemes += [{"builtin": False, "name": s["name"], "label": s["name"],
                     "text": _scheme_text(s["name"], s.get("scope")),
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
        self.apply_acc.description = (scheme["description"] if scheme
                                      else "No mix presets on this device.")

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

    def _on_save_scheme(self, event):
        source = event.GetEventObject()
        if self.read_only:
            self.frame.say(READ_ONLY)
            return
        out = self.current
        with SavePresetDialog(self, out["label"]) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            name, one_output = dialog.name(), dialog.one_output()
        exists = any(not s["builtin"] and s["name"] == name for s in self._schemes)
        if exists and not confirm(
                self, f"Replace the mix preset {name}?",
                "A saved preset of that name already exists. Saving replaces it with "
                + (f"the {out['label']} mix" if one_output else "the unit's mixes")
                + " as it is now.", "Replace preset"):
            self.frame.say("Cancelled. The saved preset was not changed.")
            return

        def saved(reply):
            self.frame.reply(source, reply)
            if reply.get("ok"):
                self.read_schemes(select=reply.get("name"))
        call(self, server.action_save_routing_scheme, name, out["key"] if one_output else "",
             done=saved)

    def _on_delete_scheme(self, event):
        source = event.GetEventObject()
        own = [s for s in self._schemes if not s["builtin"]]
        if not own:
            self.frame.say("There are no presets of your own to delete.")
            return
        with DeletePresetDialog(self, [s["name"] for s in own],
                                [s["text"] for s in own]) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            name = dialog.chosen()

        def deleted(reply):
            self.frame.reply(source, reply)
            if reply.get("ok"):
                self.read_schemes()
        call(self, server.action_delete_routing_scheme, name, done=deleted)

    # ---- reset -----------------------------------------------------------------------------

    def _build_reset(self, parent, sizer):
        reset = wx.Button(parent, label="Reset all output mixes")
        a11y.describe(reset, "Headphones, monitor, recording and USB 1 go back to Main Mix; "
                             "Bluetooth, Chat, USB 2 and CallMe to Mix Minus, all following the "
                             "faders. Wipes any custom mix.")
        reset.Bind(wx.EVT_BUTTON, self._on_reset)
        sizer.Add(reset, 0, wx.ALL, 6)

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


def _scheme_text(name, scope):
    """A preset's name in the list, tagged with its scope unless it writes every output."""
    return name if not scope or scope == "every output" else f"{name}, {scope}"


class SavePresetDialog(wx.Dialog):
    """A name for the new preset, and whether it holds every output's mix or only `output`'s. One
    output's preset leaves every other output alone when applied, so it is the kind that is safe
    mid-show."""

    MAX_NAME = 40

    def __init__(self, parent, output):
        super().__init__(parent, title="Save the mixes as a preset")
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(self, label="Name"), 0, wx.LEFT | wx.TOP, 10)
        self.name_box = wx.TextCtrl(self, size=(300, -1))
        self.name_box.SetMaxLength(self.MAX_NAME)
        a11y.describe(self.name_box, f"Up to {self.MAX_NAME} letters, digits, spaces, dashes or "
                                     f"underscores.")
        sizer.Add(self.name_box, 0, wx.ALL, 10)
        self.scope = wx.RadioBox(self, label="What to save",
                                 choices=["Every output's mix", f"Only the {output} mix"],
                                 style=wx.RA_SPECIFY_ROWS)
        sizer.Add(self.scope, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)
        buttons = wx.StdDialogButtonSizer()
        ok = wx.Button(self, wx.ID_OK, label="Save")
        ok.SetDefault()
        buttons.AddButton(ok)
        buttons.AddButton(wx.Button(self, wx.ID_CANCEL, label="Cancel"))
        buttons.Realize()
        sizer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 10)
        self.SetSizerAndFit(sizer)
        self.Bind(wx.EVT_BUTTON, self._on_save, id=wx.ID_OK)
        self.CentreOnParent()

    def _on_save(self, event):
        if not self.name():
            # The dialog stays open, so the reason gets a message box of its own and the focus
            # goes back to the name.
            wx.MessageBox("Type a name for the preset first.", "No name",
                          wx.OK | wx.ICON_WARNING, self)
            self.name_box.SetFocus()
            return
        event.Skip()

    def name(self):
        return self.name_box.GetValue().strip()

    def one_output(self):
        return self.scope.GetSelection() == 1
