"""Device: the unit itself. The state it is in, its hardware, its own settings, and its wireless
connections.

Nothing here is in the desk state. Each page reads its own part of the unit (`read`): every page
after the desk state's first read and after a job that rewrites the desk, the page shown on F5, and
Hardware, Unit settings and Wireless connections once the desk has pushed a change to what they
show (`FollowDesk`). The reads at start share one dump through the model cache. The overview also
gives the window's status bar its summary, built again from the desk model shortly after the desk
state changes, and read from the desk whenever Current state is opened.
"""
import wx
import wx.lib.scrolledpanel as scrolled

import desk_labels
import device_page
import firmware
import network
from native import a11y
from native.calls import call
from native.controls import Choice, Slider, Switch
from native.dialogs import JobDialog, confirm, follow_job, report
from native.firmware_dialog import FirmwareDialog
from native.pages import FollowDesk, Page, Reader, add_button, group, page_body, read_only, set_text
from native.wifi_dialogs import JoinDialog, NetworksDialog

TITLE = "Device"
UNREAD = "Not read from the desk yet."
UNIT_UNREAD = "The desk's settings have not been read yet."
NETWORK_UNREAD = "The wireless connections have not been read yet."
NETWORK_READ_ONLY = ("Changing the network settings is not enabled on this device yet. You can "
                     "still read what it has.")
UNIT_SETTINGS_OFF = ("Changing the desk's settings is not enabled on this device yet. You can "
                     "still read what it has.")
NOT_REPORTED = (" The desk did not report this setting, so the control shows a default rather "
                "than its real value.")
WIFI_OFF = "WiFi is off. Turn it on to join a network."
# Desk state events that can change the status bar's summary, and how long after the last of a
# burst it is read again.
SUMMARY_EVENTS = {"read", "changed", "mix", "faders", "recorder"}
SUMMARY_AFTER_MS = 1500
NOT_SCANNED = ("Nothing found. The desk only fills this in after a scan, so this usually means no "
               "scan has been run rather than nothing being in range.")


# ---- the wording ----------------------------------------------------------------------------

def _sentence(text):
    clean = str(text).strip().rstrip(".")
    return clean[:1].upper() + clean[1:] + "."


def network_line(n):
    """The unit's internet connection in one sentence. Offline is not a fault in itself, so the
    sentence says what it costs."""
    if n.get("wired") and n.get("wifi_ip"):
        return f"Network: online by cable, and on wifi to {n.get('wifi_ssid')}."
    if n.get("wired"):
        return "Network: online by cable."
    if n.get("wifi_ip"):
        return f"Network: online over wifi, connected to {n.get('wifi_ssid')}."
    if n.get("wifi_on") and n.get("wifi_ssid"):
        wifi = f"Wifi is on but not connected to {n['wifi_ssid']}. "
    elif n.get("wifi_on"):
        wifi = "Wifi is on but not joined to a network. "
    else:
        wifi = "Wifi is off. "
    return f"Network: offline. {wifi}CallMe and the firmware check need wifi or a network cable."


def wifi_status(wifi):
    """Where the unit's wifi stands, in one sentence. Joined means an address: a name alone is
    only where the unit is trying to go."""
    ssid = wifi.get("ssid")
    if not wifi.get("on"):
        return WIFI_OFF
    if wifi.get("ip"):
        return f"Connected to {ssid or 'a network'}. The desk's address is {wifi['ip']}."
    if ssid:
        refused = " It refused the password." if (wifi.get("incorrect_psk")
                                                  or wifi.get("wrong_password")) else ""
        return f"Set to join {ssid}, but not connected.{refused}"
    return "Not connected to a wifi network."


def _labels(items):
    return ", ".join(item["label"] for item in items)


def overview_lines(d):
    """The Current state report from `device_page.overview`: by exception, so a short report
    means all is well. Fader levels are the exception to the exception, since a level has no
    normal value to infer from silence."""
    if not d.get("ok"):
        return [d.get("message") or "Could not read the desk."]
    lines = [f"{d.get('device_name') or 'The desk'} connected."]
    # A latched pad first: a voice effect makes no noise of its own, so the only symptom is
    # sounding wrong.
    for pad in d.get("pads_engaged") or []:
        lines.append(f"Pad {pad['grid']}, {pad['name']}, is switched on and processing a live "
                     "channel. Press that pad on the desk to switch it off, or clear it from the "
                     "SMART pads page.")
    if d.get("recording"):
        lines.append("A recording is PAUSED. The Recording section carries on or stops it."
                     if d["recording"].get("state") == 1 else
                     "The desk is RECORDING. The Recording section pauses or stops it.")
    for volume in d.get("storage_low") or []:
        lines.append(f"{volume['label']} is nearly full: {volume['free_pct']} percent left, "
                     f"about {volume['free_mb']} MB.")
    muted, soloed = d.get("muted") or [], d.get("soloed") or []
    lines.append(f"Muted: {_labels(muted)}." if muted else "Nothing is muted.")
    lines.append(f"Soloed: {_labels(soloed)}. While anything is soloed, your headphones hear only "
                 "the soloed channels." if soloed else "Nothing is soloed.")
    faders = d.get("faders") or []
    if not d.get("levels_ok"):
        lines.append("Fader levels could not be read: the matrix changed shape, which usually "
                     "means a phone caller is connected.")
    elif faders:
        def level(f):
            if f["label"] == "empty":
                return f"fader {f['fader']} empty"
            if f.get("muted"):
                return f"{f['label']} muted"
            return f"{f['label']} " + ("unknown" if f.get("percent") is None
                                       else f"{f['percent']} percent")
        lines.append("Fader levels: " + ", ".join(level(f) for f in faders) + ".")
    if d.get("monitor_percent") is not None:
        lines.append(f"Monitor speaker level: {d['monitor_percent']} percent.")
    if d.get("network"):
        lines.append(network_line(d["network"]))
    routing = d.get("routing") or {}
    if not routing.get("ok"):
        lines.append(routing.get("reason") or "The routing could not be read.")
    elif not routing.get("notes"):
        lines.append("Routing: nothing unusual, every output is on its usual mix.")
    else:
        lines.append("Routing, only what differs from the usual:")
        lines.extend(routing["notes"])
    # "Currently selected": on its own, "SMART pad bank: 1" sounds like a setting being offered.
    if d.get("bank") is not None:
        lines.append(f"Currently selected SMART pad bank: {d['bank']}.")
    return lines


def desk_summary(d):
    """The overview reduced to what is WRONG, for the status bar. The reference facts (every fader
    level, the monitor level) stay on Current state. Empty when the unit could not be read."""
    if not d or not d.get("ok"):
        return ""
    alerts = [f"Pad {p['grid']}, {p['name']}, is switched on and processing a live channel"
              for p in d.get("pads_engaged") or []]
    if d.get("recording"):
        alerts.append("a recording is paused" if d["recording"].get("state") == 1
                      else "the desk is recording")
    alerts += [f"{v['label']} is nearly full, {v['free_pct']} percent left"
               for v in d.get("storage_low") or []]
    # Offline is a problem only when guests expect to reach the unit.
    net = d.get("network") or {}
    if net.get("online") is False and net.get("callme_hosting"):
        alerts.append("the desk is offline, so CallMe guests cannot reach it")
    update = d.get("firmware_update") or {}
    if update.get("available"):
        alerts.append((f"firmware {update['version']}" if update.get("version")
                       else "a firmware update") + " is available, on the Device page under "
                      "Hardware")
    muted = [c["label"] for c in d.get("muted") or []]
    soloed = [c["label"] for c in d.get("soloed") or []]
    if muted:
        alerts.append("muted: " + ", ".join(muted))
    if soloed:
        alerts.append("soloed: " + ", ".join(soloed) + ", so your headphones hear only those")
    routing = d.get("routing") or {}
    broken = routing.get("ok") is False
    if broken:
        alerts.append(routing.get("reason") or "the routing could not be read")
    else:
        alerts += routing.get("notes") or []
    # What was checked and found clear is said too, so a short summary cannot be mistaken for one
    # that stopped at the first problem.
    clear = []
    if not muted and not soloed:
        clear.append("nothing is muted or soloed")
    elif not soloed:
        clear.append("nothing is soloed")
    if not broken and not routing.get("notes"):
        clear.append("every output is on its usual mix")
    parts = [_sentence(alert) for alert in alerts]
    if clear:
        parts.append(_sentence(" and ".join(clear)))
    # Always said: no bank is wrong, but it changes what every pad does and has no outward sign.
    if d.get("bank") is not None:
        parts.append(f"SMART pad bank {d['bank']}.")
    return " ".join(parts)


def firmware_line(version, update):
    update = update or {}
    if not update.get("available"):
        return version or "Unknown"
    return (f"{version or 'Unknown'}. "
            + (f"Version {update['version']}" if update.get("version") else "An update")
            + " is available.")


def hardware_lines(d):
    """Storage and the recorder from `device_page.read_hardware`, as sentences."""
    if not d.get("ok"):
        return [d.get("message") or "Could not read the desk."]
    volumes = (d.get("storage") or {}).get("volumes") or []
    cards = [v for v in volumes if not (v.get("label") or "").startswith("Internal")]
    internal = next((v for v in volumes if (v.get("label") or "").startswith("Internal")), None)

    def space(v):
        return (f"{v['free_mb']} MB free of {v['capacity_mb']} MB"
                + ("" if v.get("free_pct") is None else f", {v['free_pct']} percent left")
                + (". This is nearly full" if v.get("low") else ""))
    lines = [] if cards else ["Storage: the desk reported no card slot or drive."]
    lines += [f"{v['label']}: in use, {space(v)}." if v.get("inserted")
              else f"{v['label']}: nothing in it." for v in cards]
    if internal:
        lines.append(f"{internal['label']}: {space(internal)}.")
    rec = d.get("recorder") or {}
    lines.append(f"Recorder: {(rec.get('state_label') or 'unknown').lower()}. "
                 f"{rec.get('total_recordings') or 0} recordings on the desk. Start and stop it "
                 "in the Recording section.")
    return lines


def wired_text(connected):
    return {True: "Connected", False: "Not connected"}.get(connected, "Unknown")


def connection_text(status):
    if not status.get("connected"):
        return "Not connected"
    return "Connected and responding" if status.get("online") else "Connected"


def mic_lines(mics):
    """The wireless mic reading. No slots at all means the unit said nothing, which is not the
    same as none connected."""
    mics = mics or {}
    if not mics.get("present"):
        return ["Wireless mics: the desk did not report any, so there is nothing to show."]
    if not mics.get("any_connected"):
        return ["Wireless mics: none connected."]
    lines = []
    for t in mics.get("transmitters") or []:
        if t.get("connected"):
            lines.append(f"Wireless mic {t['index'] + 1}: connected"
                         + ("" if t.get("battery_pct") is None
                            else f", battery {t['battery_pct']} percent")
                         + ("" if t.get("rssi") is None else f", signal {t['rssi']}") + ".")
    return lines


def network_facts(data):
    """What the unit reports about its radios, one fact to a line. The wifi lines go with the
    radio: off, they describe nothing."""
    wifi, bt = data.get("wifi") or {}, data.get("bluetooth") or {}

    def scan(found):
        return ", ".join(found) if found else NOT_SCANNED
    lines = []
    if wifi.get("on"):
        lines += [
            "WiFi network: " + ((wifi["ssid"] + (" (has a password saved)"
                                                if wifi.get("has_password") else ""))
                               if wifi.get("ssid") else "None set") + ".",
            "WiFi connected: " + ("Yes" if wifi.get("connected") else "No") + ".",
            f"WiFi IP address: {wifi.get('ip') or 'None'}.",
            f"WiFi gateway: {wifi.get('gateway') or 'None'}.",
            "WiFi address assigned: " + {True: "Automatically (DHCP)", False: "Fixed"}.get(
                wifi.get("dhcp"), "Unknown") + ".",
            f"WiFi networks last scanned for: {scan(wifi.get('scan'))}"]
    visible = bt.get("visible")
    lines += [
        "Bluetooth: " + (f"Connected to {bt['connected_address']}" if bt.get("connected")
                         else "No device connected") + ".",
        "Bluetooth discoverable: " + {True: "Yes, other devices can find it",
                                      False: "No, other devices cannot find it"}.get(
                                          visible, "Unknown") + ".",
        "Bluetooth paired devices: " + (", ".join(bt["paired"]) if bt.get("paired")
                                        else "None paired") + ".",
        f"Bluetooth devices last scanned for: {scan(bt.get('scan'))}"]
    if data.get("still_read_only"):
        lines.append(data["still_read_only"])
    return lines



# ---- the section ----------------------------------------------------------------------------

class DeviceSection:
    title = TITLE
    route = "status"

    def __init__(self, frame):
        self.frame = frame
        self._started = False
        self.state = StatePage(self)
        self.hardware = HardwarePage(self)
        self.unit = UnitPage(self)
        self.network = NetworkPage(self)
        self.views = [self.state, self.hardware, self.unit, self.network]
        self.pages = [Page("state", "Current state", self.state),
                      Page("hardware", "Hardware", self.hardware),
                      Page("unit", "Desk settings", self.unit),
                      Page("network", "Wireless connections", self.network)]
        self._summary_timer = None
        frame.desk.subscribe(self._desk_event)
        for view, names in ((self.hardware, device_page.HARDWARE_PROPS),
                            (self.unit, device_page.UNIT_SETTING_PROPS),
                            (self.network, network.PROPS)):
            FollowDesk(frame, names, view.follow)

    def _desk_event(self, event, _detail):
        if event == "read" and not self._started:
            self._started = True
            for view in self.views:
                view.read()
        elif event in SUMMARY_EVENTS:
            # A burst of pushes (a restore, a fader moved across) is one read, after the last.
            if self._summary_timer is None:
                self._summary_timer = wx.CallLater(SUMMARY_AFTER_MS, self._summary_due)
            else:
                self._summary_timer.Start(SUMMARY_AFTER_MS)

    def _summary_due(self):
        if self.state:          # False once the window is gone
            self.state.read(full=False)

    def read(self):
        for view in self.views:
            view.read()

    def refresh(self, announce):
        shown = [view for view in self.views if view.IsShown()]
        for view in shown or self.views:
            view.read(announce)


class _Reader(Reader):
    """A Device page's read. `fetch(full)` has `full` False for a read the desk's pushes ask for
    (`follow`); `shown(reply)` puts a reply on the page and returns what F5 says."""

    def read(self, announce=False, full=True):
        super().read(announce, full=full)

    def follow(self):
        self.read(full=False)

    def merge(self, earlier, later):
        return {"full": earlier["full"] or later["full"]}

    def read_done(self, reply, announce, full):
        said = self.shown(reply)
        if announce:
            self.section.frame.say(said)


class StatePage(_Reader, wx.Panel):
    """The overview: what is muted, soloed and engaged, the levels, the network and the routing,
    by exception."""

    def __init__(self, section):
        super().__init__(section.frame.host)
        self.Hide()
        self._init_reader()
        self.section = section
        sizer, parent = page_body(self)
        self.report = read_only(parent, sizer, "Now", height=300, scroll=True)
        self.report.SetValue(UNREAD)
        self.Bind(wx.EVT_SHOW, self._on_show)

    def _on_show(self, event):
        # Opened, it reads the desk: a card's free space is the one fact here no push updates.
        event.Skip()
        if event.IsShown():
            self.read()

    def fetch(self, full):
        """`full` reads the desk; a re-build after a push uses the desk model (`fresh=False`)."""
        return device_page.overview(fresh=full)

    def shown(self, reply):
        set_text(self.report, "\n".join(overview_lines(reply)))
        summary = (desk_summary(reply) if reply.get("ok")
                   else reply.get("message") or "Could not read the desk.")
        self.section.frame.set_status(summary)
        return summary


class HardwarePage(_Reader, scrolled.ScrolledPanel):
    """Storage and the recorder, the firmware and wired network, and the USB facts for
    troubleshooting. Nothing here can be changed, bar the firmware check and install."""

    def __init__(self, section):
        super().__init__(section.frame.host)
        self.Hide()
        self._init_reader()
        self.section = section
        self.frame = section.frame
        sizer, parent = page_body(self)
        self.status = read_only(parent, sizer, "Storage and recorder", height=100)
        self.status.SetValue(UNREAD)

        self.firmware = read_only(parent, sizer, "Firmware", height=None)
        self.wired = read_only(parent, sizer, "Wired network", height=None)
        self.check = add_button(parent, sizer, "Check for a firmware update", self._on_check)
        # Shown only while the unit offers an update, and named after it.
        self.install = add_button(parent, sizer, "Install firmware", self._on_install)
        self.install.Hide()

        box_sizer, box = group(parent, sizer, "USB interfaces (technical)")
        self.connection = read_only(box, box_sizer, "Connection", height=None)
        self.hardware_id = read_only(box, box_sizer, "Hardware id", height=None)
        self.info = read_only(box, box_sizer, "Status bytes", height=None)
        self.interfaces = read_only(box, box_sizer, "USB interfaces", height=100, scroll=True)
        a11y.describe(self.interfaces, "A low-level list of the USB interfaces seen, for "
                                       "troubleshooting.")
        self.SetupScrolling(scroll_x=False)

    def fetch(self, full):
        """The unit's state, and with `full` its USB status, which no push changes and whose read
        rescans the bus."""
        reply = {"ok": True, "state": device_page.read_hardware()}
        if full:
            reply["status"] = device_page.read_status()
        return reply

    def shown(self, reply):
        # A failed call is one refusal reply, standing for both halves.
        if "state" not in reply:
            state = status = reply
        else:
            state, status = reply["state"], reply.get("status")
        set_text(self.status, "\n".join(hardware_lines(state)))
        if state.get("ok"):
            set_text(self.firmware, firmware_line(state.get("firmware"),
                                                  state.get("firmware_update")))
            set_text(self.wired, wired_text(state.get("wired_connected")))
            self._show_install(state.get("firmware_update"))
        if status is None:
            return "Status read."
        set_text(self.connection, connection_text(status))
        set_text(self.hardware_id, status.get("hardware_id") or "Unknown")
        set_text(self.info, "0x" + status["info_hex"] if status.get("info_hex") else "Unknown")
        set_text(self.interfaces, "\n".join(status.get("interfaces") or ["None found."]))
        return status.get("message") or "Status read."

    def _on_check(self, _event):
        job = JobDialog(self, "Checking for a firmware update",
                        "Checking with RODE. This takes up to twenty seconds.",
                        lambda dialog: call(dialog, firmware.action_check,
                                            done=dialog.finish),
                        stop=None, closable=False)
        with job:
            reply = job.run()
        if reply.get("ok"):
            set_text(self.firmware, firmware_line(reply.get("firmware"),
                                                  reply.get("firmware_update")))
            self._show_install(reply.get("firmware_update"))
            self.section.state.read()       # the summary names a waiting update
        report(self, ("Firmware check", "Firmware check failed"), reply)
        self.check.SetFocus()

    def _show_install(self, update):
        version = (update or {}).get("version") if (update or {}).get("available") else ""
        label = f"Install firmware {version}" if version else "Install firmware"
        if self.install.GetLabel() != label:
            self.install.SetLabel(label)
        if self.install.IsShown() != bool(version):
            self.install.Show(bool(version))
            self.Layout()
            self.FitInside()

    def _on_install(self, _event):
        call(self, firmware.read_install, done=self._ask_install)

    def _ask_install(self, ready):
        if not ready.get("ok") or ready.get("refusal"):
            self.frame.say(ready.get("refusal") or ready.get("message")
                           or "The desk could not be read.")
            return
        version = ready["version"]
        dialog = FirmwareDialog(self, version, ready.get("firmware"), firmware.BACKUP_NAME)
        with dialog:
            if dialog.ShowModal() != wx.ID_OK:
                self.install.SetFocus()
                return
            backup_first = dialog.wants_backup()
        reply = follow_job(self, f"Installing firmware {version}",
                           "Backing the desk up, then updating it." if backup_first
                           else f"Updating the desk to firmware {version}.",
                           lambda: firmware.action_install(backup_first),
                           lambda _started: firmware.job_status)
        report(self, ("Firmware updated", "Firmware not updated"), reply)
        self.check.SetFocus()
        self.frame.desk_rewritten()


class UnitPage(_Reader, scrolled.ScrolledPanel):
    """The settings otherwise reached through the unit's touchscreen, built from
    `desk_labels.UNIT_SETTINGS`, then MIDI control, which has a write of its own."""

    def __init__(self, section):
        super().__init__(section.frame.host)
        self.Hide()
        self._init_reader()
        self.section = section
        self.frame = section.frame
        self.data = None             # the last good `device_page.read_unit_settings`
        self.values = {}             # prop: value as the unit last reported it
        self.controls = {}           # prop: its control
        sizer, parent = page_body(self)
        self._sizer = sizer
        self.note = read_only(parent, sizer, "Desk settings status", height=60)
        self.note.SetValue(UNIT_UNREAD)
        for spec in desk_labels.UNIT_SETTINGS:
            self.controls[spec["prop"]] = self._build(parent, sizer, spec)
        self.midi = Switch(self, parent, sizer, "Enable MIDI control", device_page.action_set_midi,
                           *self.field("midi"),
                           description="Lets a MIDI app or controller handle mute, faders, "
                                       "listen and record, without RODE Central.",
                           guard=self._unread)
        self.SetupScrolling(scroll_x=False)

    def field(self, prop):
        def current():
            return self.values.get(prop)

        def remember(value):
            self.values[prop] = value
        return current, remember

    def _unread(self):
        return UNIT_UNREAD if self.data is None else None

    def _guard(self):
        if self.data is None:
            return UNIT_UNREAD
        return None if self.data.get("writable") else UNIT_SETTINGS_OFF

    def _build(self, parent, sizer, spec):
        prop = spec["prop"]

        def send(value):
            return device_page.action_set_unit_setting(prop, int(value))
        if spec["kind"] == "bool":
            current, remember = self.field(prop)
            return Switch(self, parent, sizer, spec["label"], send,
                          lambda: None if current() is None else current() == 1,
                          lambda on: remember(1 if on else 0),
                          description=spec["detail"], guard=self._guard)
        if spec["kind"] == "choice":
            return Choice(self, parent, sizer, spec["label"],
                          [(value, text) for value, text in spec["choices"]], send,
                          *self.field(prop), description=spec["detail"], guard=self._guard)
        lo, hi = int(spec["min"]), int(spec["max"])
        span = max(1, hi - lo)
        slider = Slider(self, parent, sizer, spec["label"], lo, hi, send, *self.field(prop),
                        words=lambda v: f"{v} of {hi}, about {round((v - lo) * 100 / span)} "
                                        "percent",
                        unread=hi)
        slider.slider.SetLineSize(int(spec.get("step") or 1))
        slider.slider.SetPageSize(int(spec.get("step") or 1) * 5)
        slider.acc.description = spec["detail"]
        return slider

    def fetch(self, _full):
        return device_page.read_unit_settings()

    def shown(self, reply):
        if not reply.get("ok"):
            if self.data is None:
                set_text(self.note, reply.get("message") or "Could not read the desk's settings.")
            return reply.get("message") or "Could not read the desk's settings."
        self.data = reply
        self.values = {s["prop"]: s.get("value") for s in reply.get("settings") or []}
        self.values["midi"] = reply.get("midi")
        for spec in desk_labels.UNIT_SETTINGS:
            self._show(spec)
        self.midi.show()
        note = "" if reply.get("writable") else reply.get("read_only_note", "")
        set_text(self.note, note)
        self._sizer.Show(self.note.GetContainingSizer(), bool(note))
        self.Layout()
        return "Desk settings read."

    def _show(self, spec):
        prop = spec["prop"]
        control = self.controls[prop]
        value = self.values.get(prop)
        if spec["kind"] == "choice":
            # A value the unit holds that is not one of the choices (a dim time of 45 seconds) is
            # listed as it is, never shown as the nearest choice.
            items = [(v, text) for v, text in spec["choices"]]
            if value is not None and value not in [v for v, _text in items]:
                items.append((value, f"Currently {value}"))
            control.set_items(items)
        control.acc.description = spec["detail"] + ("" if value is not None else NOT_REPORTED)
        control.show()


class NetworkPage(_Reader, scrolled.ScrolledPanel):
    """Every radio link the unit can make: wifi, Bluetooth and wireless mics. Wifi on and off,
    joining a network and Bluetooth discoverability are writable; the rest is a reading."""

    def __init__(self, section):
        super().__init__(section.frame.host)
        self.Hide()
        self._init_reader()
        self.section = section
        self.frame = section.frame
        self.data = None             # the last good `network.read_status`
        sizer, parent = page_body(self)

        self.wifi = Switch(self, parent, sizer, "WiFi on", network.action_set_wifi,
                           *self.field("wifi", "on"),
                           description="Turn the desk's wifi on or off, and join a network.",
                           guard=self._guard, accepted=self._wifi_switched,
                           working=lambda on: ("Turning the wifi on and waiting for it to "
                                               "connect. Please wait." if on else
                                               "Turning the wifi off. Please wait."))
        self.wifi_status = read_only(parent, sizer, "WiFi status", height=None)
        self.wifi_status.SetValue(WIFI_OFF)
        # One of the two at a time: the unit only scans while it has no network to join.
        self.disconnect = add_button(parent, sizer, "Disconnect", self._on_disconnect)
        self.scan = add_button(parent, sizer, "Scan for networks", self._on_scan)

        box_sizer, box = group(parent, sizer, "Bluetooth")
        self.bluetooth = Switch(self, box, box_sizer, "Let other devices find this desk",
                                network.action_set_bt_visible,
                                *self.field("bluetooth", "visible"),
                                description="Switch this on, then pair from the phone's own "
                                            "Bluetooth screen. The desk stops being discoverable "
                                            "on its own after a few minutes.",
                                guard=self._guard)

        self.mics = read_only(parent, sizer, "Wireless microphones", height=60)
        a11y.describe(self.mics, "RODE wireless transmitters the desk can see, and how much "
                                 "battery each has left. Pairing one is done on the transmitter "
                                 "and the desk.")
        self.facts = read_only(parent, sizer, "What the desk reports", height=180, scroll=True)
        for box_ in (self.mics, self.facts):
            box_.SetValue(UNREAD)
        self._show_radio()
        self.SetupScrolling(scroll_x=False)

    def field(self, part, key):
        def current():
            return ((self.data or {}).get(part) or {}).get(key)

        def remember(value):
            if self.data:
                self.data[part][key] = value
        return current, remember

    def _guard(self):
        if self.data is None:
            return NETWORK_UNREAD
        return None if self.data.get("writable") else NETWORK_READ_ONLY

    def fetch(self, _full):
        return network.read_status()

    def shown(self, reply):
        if not reply.get("ok"):
            if self.data is None:
                set_text(self.facts, reply.get("message") or "Could not read the device.")
            return reply.get("message") or "Could not read the device."
        self.data = reply
        self.wifi.show()
        self.bluetooth.show()
        set_text(self.mics, "\n".join(mic_lines(reply.get("wireless_mics"))))
        set_text(self.facts, "\n".join(network_facts(reply)))
        self._show_radio()
        return "Wireless connections read."

    def _show_radio(self):
        wifi = (self.data or {}).get("wifi") or {}
        on, ssid = bool(wifi.get("on")), wifi.get("ssid") or ""
        set_text(self.wifi_status, wifi_status(wifi))
        self.disconnect.Show(on and bool(ssid))
        self.scan.Show(on and not ssid)
        label = f"Disconnect from {ssid}"
        if ssid and self.disconnect.GetLabel() != label:
            self.disconnect.SetLabel(label)
        self.Layout()
        self.FitInside()

    def _wifi_switched(self, reply):
        if reply.get("wifi"):
            self.data["wifi"].update(reply["wifi"])
        self._show_radio()
        # Back on, the readings it has just revealed are from before it went off.
        if self.wifi.box.GetValue():
            self.read()
        self.section.state.read()           # the summary says when the unit is offline

    def _refused(self):
        refusal = self._guard()
        if refusal:
            self.frame.say(refusal)
        return refusal

    def _on_scan(self, _event):
        if self._refused():
            return
        job = JobDialog(self, "Scanning for networks",
                        "Scanning for networks. This takes about twenty seconds.",
                        lambda dialog: call(dialog, network.action_scan_wifi, done=dialog.finish),
                        stop=None, closable=False)
        with job:
            reply = job.run()
        if not reply.get("ok"):
            report(self, ("Scan", "Scan failed"), reply)
            self._focus_wifi()
            return
        # After the scan's dialog has gone and the focus has settled: opened at once, the list's
        # first item is never read.
        wx.CallAfter(self._choose, reply)

    def _choose(self, scan):
        """The networks `scan` found, then the chosen one's password. Cancel on the password goes
        back to the list, so trying another network is one arrow."""
        dialog = NetworksDialog(self, scan.get("networks") or [], scan.get("message") or "")
        with dialog:
            dialog.ShowModal()
            chosen = dialog.choice
        if chosen is None:
            self._focus_wifi()
            return
        wx.CallAfter(self._ask_join, chosen, scan)

    def _ask_join(self, chosen, scan=None):
        dialog = JoinDialog(self, chosen)
        with dialog:
            joining = dialog.ShowModal() == wx.ID_OK
            ssid, password = dialog.ssid, dialog.password
        if not joining:
            if scan is not None:
                wx.CallAfter(self._choose, scan)
            else:
                self._focus_wifi()
            return
        self._join(ssid, password, chosen, scan)

    def _on_disconnect(self, _event):
        if self._refused():
            return
        ssid = ((self.data or {}).get("wifi") or {}).get("ssid") or "the network"
        if not confirm(self, f"Disconnect from {ssid}?",
                       f"The desk leaves {ssid} and forgets it, password included, so joining it "
                       f"again needs the password.", "Disconnect"):
            return
        job = JobDialog(self, "Disconnecting", f"Disconnecting from {ssid}.",
                        lambda dialog: call(dialog, network.action_disconnect_wifi,
                                            done=dialog.finish),
                        stop=None, closable=False)
        with job:
            reply = job.run()
        report(self, ("Disconnected", "Not disconnected"), reply)
        if reply.get("wifi") and self.data:
            self.data["wifi"].update(reply["wifi"])
        self._show_radio()
        self._focus_wifi()
        if reply.get("ok"):
            self.read()
            self.section.state.read()

    def _focus_wifi(self):
        """The wifi step there is now: Disconnect while a network is set up, Scan while not, the
        switch while the radio is off. A hidden button never keeps the focus."""
        for control in (self.disconnect, self.scan):
            if control.IsShown():
                control.SetFocus()
                return
        self.wifi.box.SetFocus()

    def _join(self, ssid, password, chosen, scan):
        job = JobDialog(self, f"Joining {ssid}",
                        f"Joining {ssid}. This can take up to 45 seconds.",
                        lambda dialog: call(dialog, network.action_join_wifi, ssid, password,
                                            done=dialog.finish),
                        stop=None, closable=False)
        with job:
            reply = job.run()
        report(self, ("Joined", "Not joined"), reply)
        wifi = reply.get("wifi") or {}
        if wifi and self.data:
            self.data["wifi"].update(wifi)
        self._show_radio()
        # A refused or missing password is put right in the same dialog, for the same network.
        if reply.get("needs_password") or wifi.get("wrong_password"):
            wx.CallAfter(self._ask_join, dict(chosen, ssid=ssid, open=False), scan)
            return
        self._focus_wifi()
        # A join that fails still leaves the name stored on the unit, which Disconnect follows.
        self.read()
        self.section.state.read()
