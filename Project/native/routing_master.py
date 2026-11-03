"""Routing's Master Mix page: what the desk and this computer send each other over USB 1, RODE's
driver for the Expanded inputs, and the processing the whole mix goes through on its way out (the
Compellor and the output delay).
"""
import wx
import wx.lib.scrolledpanel as scrolled

import server
from native import a11y
from native.calls import call
from native.controls import Slider, Switch
from native.dialogs import JobDialog, confirm
from native.pages import group, labelled, page_box

TITLE = "Master Mix"
READ_ONLY = "Changing this is not enabled on this device yet."
USB_SWITCHING = ("The last USB change is still being finished. RODE's driver can take up to a "
                 "minute to connect, longer if it has to retry.")
# The driver line reads Windows and the service's status block, never the desk, so polling it
# while the page is shown costs no device access. It follows a bind landing, a retry, or the desk
# leaving Expanded from its own screen.
DRIVER_POLL_MS = 3000
DRIVER_UNKNOWN = "Whether RODE's driver is installed could not be read."
DRIVER_MISSING = ("Expanded needs this driver. Downloading it connects to RODE's website, and only "
                  "ever when you press that button. The file is checked to be signed by RODE, and "
                  "their own installer then runs and asks Windows for permission in the usual way.")
DRIVER_INSTALLED = "The driver is installed. If it is not working, RODE's own installer can repair it."
COMPELLOR = [("threshold", "Compellor threshold"), ("attack", "Compellor attack"),
             ("release", "Compellor release"), ("gain", "Compellor make-up gain")]
DELAY_MAX_MS = 2000


def percent(value):
    return f"{value} percent"


def milliseconds(value):
    return f"{value} milliseconds"


def _percent(level):
    return None if level is None else round(level * 100)


class MasterMixPage(scrolled.ScrolledPanel):
    def __init__(self, section, model, previous=None):
        super().__init__(section.frame.host)
        self.Hide()
        self.section = section
        self.frame = section.frame
        self.model = model
        self.read_only = model["read_only"]
        self._shown = []             # every Switch and Slider, which `show` refreshes
        self._writing = False        # a USB mode write from this page has not returned
        self._switching = False      # the server is still finishing a USB change
        self._driver = None          # the driver's last status, from the poll
        sizer, parent = page_box(self, TITLE)
        self._build_usb(parent, sizer)
        self._build_driver(parent, sizer)
        self._build_processing(parent, sizer)
        self.show()
        self.SetupScrolling(scroll_x=False)
        self._poll_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda _e: self._poll_driver(), self._poll_timer)
        self.Bind(wx.EVT_SHOW, self._on_show)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)

    @property
    def master(self):
        return self.section.desk.master

    def field(self, key, scale=1):
        """`current` and `remember` for `key` in the desk state's master values, looked up on every
        call (a full read replaces them), shown as a whole number of 1/`scale` units."""
        def current():
            value = self.master[key]
            return value if scale == 1 or value is None else round(value * scale)

        def remember(value):
            self.master[key] = value if scale == 1 else value / scale
        return current, remember

    def _add(self, control):
        self._shown.append(control)
        return control

    def _guard(self):
        return READ_ONLY if self.read_only else None

    # ---- USB audio -------------------------------------------------------------------------

    def _build_usb(self, parent, sizer):
        """The two halves of the desk's USB mode, each applied by its button: a change tears the
        desk's USB audio down and rebuilds it, and arrowing a closed list fires a change per
        item."""
        box_sizer, box = group(parent, sizer, "USB audio")
        self.receives = labelled(box, box_sizer, "What this computer receives (Multitrack)",
                                 lambda p: wx.Choice(p, choices=[
                                     label for _mode, label in self.model["usb_modes"]]))
        apply = wx.Button(box, label="Confirm what this computer receives")
        a11y.describe(apply, self.model["usb_drop_hint"])
        apply.Bind(wx.EVT_BUTTON, self._on_apply_receives)
        box_sizer.Add(apply, 0, wx.ALL, 6)

        self.sends = labelled(box, box_sizer, "What this computer sends (USB 1 Input)",
                              lambda p: wx.Choice(p, choices=[
                                  label for _on, label in self.model["usb_expanded"]]))
        a11y.describe(self.sends, self.model["usb_expanded_hint"])
        apply = wx.Button(box, label="Confirm what this computer sends")
        a11y.describe(apply, self.model["usb_expanded_drop_hint"])
        apply.Bind(wx.EVT_BUTTON, self._on_apply_sends)
        box_sizer.Add(apply, 0, wx.ALL, 6)

        self.bypass = self._add(Switch(
            self, box, box_sizer, "Send the separate tracks raw",
            server.action_set_usb_bypass, *self.field("usb_bypass"), guard=self._bypass_refused))

    def _bypass_refused(self):
        if self.read_only:
            return READ_ONLY
        # Off, unknown and not read yet all mean no separate tracks are being made.
        return None if self.master["usb_multitrack"] else self.model["bypass_needs_multitrack"]

    def _show_usb(self):
        for choice, options, value in (
                (self.receives, self.model["usb_modes"], self.master["usb_multitrack"]),
                (self.sends, self.model["usb_expanded"], self.master["usb_expanded"])):
            values = [option for option, _label in options]
            choice.SetSelection(values.index(value) if value in values else wx.NOT_FOUND)
        self.bypass.acc.description = (self.model["bypass_hint"] if self.master["usb_multitrack"]
                                       else self.model["bypass_needs_multitrack"])

    def _usb_refused(self):
        if self.read_only:
            return READ_ONLY
        if self._writing or self._switching:
            return USB_SWITCHING
        return None

    def _on_apply_receives(self, event):
        index = self.receives.GetSelection()
        if index == wx.NOT_FOUND:
            self.frame.say("Choose what this computer receives first.")
            return
        mode, label = self.model["usb_modes"][index]
        self._apply_usb(event.GetEventObject(), "usb_multitrack", mode,
                        f"This computer already receives {label.lower()}.",
                        server.action_set_usb_multitrack)

    def _on_apply_sends(self, event):
        index = self.sends.GetSelection()
        if index == wx.NOT_FOUND:
            self.frame.say("Choose what this computer sends first.")
            return
        on, _label = self.model["usb_expanded"][index]
        self._apply_usb(event.GetEventObject(), "usb_expanded", on,
                        f"Expanded is already {'on' if on else 'off'}.",
                        server.action_set_usb_expanded)

    def _apply_usb(self, source, key, value, already, write):
        refusal = self._usb_refused()
        if refusal:
            self.frame.say(refusal)
            return
        if value == self.master[key]:
            self.frame.say(already)
            return
        self._writing = True

        def applied(reply):
            self._writing = False
            self.frame.reply(source, reply)
            if reply.get("ok"):
                self.master[key] = value
            self._show_usb()
            # The server is now following the change with RODE's driver: the poll says when it
            # has finished.
            self._poll_driver()
        call(self, write, value, done=applied)

    # ---- RODE's driver ---------------------------------------------------------------------

    def _build_driver(self, parent, sizer):
        """The driver's side of Expanded, read from Windows: without it a desk in Expanded with no
        driver looks the same as one that is working. Then a way to get it."""
        box_sizer, box = group(parent, sizer, "RODE's Virtual Devices driver")
        self.driver_text = labelled(box, box_sizer, "Driver", lambda p: wx.TextCtrl(
            p, size=(460, 60), style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_NO_VSCROLL))
        self.install = wx.Button(box, label="Download and install RODE's Virtual Devices driver")
        self.install_acc = a11y.describe(self.install, DRIVER_UNKNOWN)
        self.install.Bind(wx.EVT_BUTTON, self._on_install)
        self.install.Hide()
        box_sizer.Add(self.install, 0, wx.ALL, 6)
        self.driver_page = wx.Button(box, label="Open RODE's software page")
        self.driver_page_acc = a11y.describe(self.driver_page, DRIVER_UNKNOWN)
        self.driver_page.Bind(wx.EVT_BUTTON, self._on_driver_page)
        box_sizer.Add(self.driver_page, 0, wx.ALL, 6)

    def _on_show(self, event):
        event.Skip()
        if event.IsShown():
            self._poll_driver()
            self._poll_timer.Start(DRIVER_POLL_MS)
        else:
            self._poll_timer.Stop()

    def _on_destroy(self, event):
        if event.GetEventObject() is self:
            self._poll_timer.Stop()
        event.Skip()

    def _poll_driver(self):
        call(self, server.read_usb_driver, done=self._driver_read)

    def _driver_read(self, reply):
        if not reply.get("ok"):
            return
        # A switch has just finished, from this page, the unit's screen or anywhere else: it can
        # have changed the fader channel list and cleared faders, so read the desk again.
        finished = self._switching and not reply.get("switching")
        self._switching = bool(reply.get("switching"))
        self._show_driver(reply.get("usb_driver") or {})
        if finished:
            self.section.desk.refresh()

    def _show_driver(self, driver):
        text = driver.get("text") or ""
        if text != self.driver_text.GetValue():
            # Said while the page is shown: a bind landing is worth hearing.
            if self._driver is not None and self.IsShownOnScreen() and text:
                self.frame.say(text)
            self.driver_text.SetValue(text)
        self._driver = driver
        installed = driver.get("installed")
        known = isinstance(installed, bool)
        missing = known and not installed
        hint = DRIVER_UNKNOWN if not known else (DRIVER_MISSING if missing else DRIVER_INSTALLED)
        self.install_acc.description = self.driver_page_acc.description = hint
        if self.install.IsShown() != missing:
            if not missing and wx.Window.FindFocus() is self.install:
                self.driver_page.SetFocus()
            self.install.Show(missing)
            self.Layout()
            self.SetupScrolling(scroll_x=False, scrollToTop=False)
        label = ("Open RODE's software page instead" if missing
                 else "Open RODE's software page, to reinstall or repair the driver")
        if self.driver_page.GetLabel() != label:
            self.driver_page.SetLabel(label)

    def _on_install(self, event):
        source = event.GetEventObject()
        if not confirm(self, "Download RODE's driver?",
                       "AccessCaster will connect to RODE's website, download their Virtual "
                       "Devices driver, check it is signed by RODE, and then run their installer. "
                       "Windows will ask your permission, and the installer may want to restart "
                       "the computer.", "Download and install"):
            self.frame.say("Cancelled. Nothing was downloaded.")
            return

        def begin(job):
            def started(reply):
                if not reply.get("ok"):
                    job.finish(reply)
                    return
                job.say(reply.get("message") or "Downloading.")
                job.poll(server.driver_install_status, lambda st: status(job, st))
            call(job, server.action_install_driver, done=started)

        def status(job, st):
            if st.get("done"):
                job.finish(st)
            elif st.get("message"):
                job.say(st["message"])

        job = JobDialog(self, "Installing RODE's driver", "Starting.", begin, stop=None)
        with job:
            st = job.run()
        (source if source.IsShown() else self.driver_page).SetFocus()
        self.frame.say("The install carries on. The driver line says when it is in." if st is None
                       else st.get("message") or "Done.")
        self._poll_driver()

    def _on_driver_page(self, event):
        source = event.GetEventObject()
        call(self, server.action_open_driver_page,
             done=lambda reply: self.frame.reply(source, reply))

    # ---- master output processing ----------------------------------------------------------

    def _build_processing(self, parent, sizer):
        """The Compellor and the output delay, on every Main Mix output."""
        box_sizer, box = group(parent, sizer, "Master output processing")
        self._add(Switch(self, box, box_sizer, "Master Compellor",
                         server.action_set_master_compellor, *self.field("compellor_on"),
                         description="Evens out the whole mix, on every Main Mix output.",
                         guard=self._guard))
        for key, label in COMPELLOR:
            self._add(Slider(self, box, box_sizer, label, 0, 100,
                             lambda pct, k=key: server.action_set_compellor_param(k, pct / 100),
                             *self.field(f"compellor_{key}", scale=100), words=percent))
        self._add(Switch(self, box, box_sizer, "Output delay", self._send_delay_on,
                         *self.field("delay_on"),
                         description=(f"Lines the Main Mix outputs up with video, by up to "
                                      f"{DELAY_MAX_MS} milliseconds."),
                         guard=self._guard))
        delay = self._add(Slider(self, box, box_sizer, "Output delay time", 0, DELAY_MAX_MS,
                                 self._send_delay_ms, *self.field("delay_seconds", scale=1000),
                                 words=milliseconds))
        delay.slider.SetLineSize(10)
        delay.slider.SetPageSize(100)

    def _send_delay_on(self, on):
        """Turning it on sends the time shown with it, so the reply says what the delay is."""
        ms = self.field("delay_seconds", scale=1000)[0]() if on else None
        return server.action_set_output_delay(on, ms)

    def _send_delay_ms(self, ms):
        return server.action_set_output_delay(self.master["delay_on"] is True, ms)

    # ---- showing the desk state ------------------------------------------------------------

    def show(self):
        """Put the desk state's master values on every control."""
        self._show_usb()
        for control in self._shown:
            control.show()
