"""The native window's Device section (Project/native/device_section.py), offline: the Current state
report and the status bar's summary from an overview, kept current from the desk's pushes, the
hardware and wireless readings in words, the unit settings built from their spec (a value
off the list kept as it is, a read-only desk refused and said), the wifi controls shown only with
the radio on, a scan filling in the name to join, and F5 reading only the page shown. Nothing is
shown and nothing touches a desk.

Run: python DevTools/test_native_device.py
"""
from __future__ import annotations

import inspect
import sys

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _a11y
import _testkit
import device_page
import desk_labels
import network
import wx
from _native_fakes import FakeFrame as BaseFrame, FakeJob
from _testkit import check, check_in, check_not_in, check_true
from native import device_section, firmware_dialog, wifi_dialogs
from native.firmware_dialog import FirmwareDialog

app = wx.App()


class FakeFrame(BaseFrame):
    def __init__(self):
        super().__init__()
        self.status = []

    def set_status(self, text):
        self.status.append(text)


def overview(**values):
    data = {"ok": True, "device_name": "RODECaster Pro II", "muted": [], "soloed": [],
            "storage_low": [], "recording": None, "pads_engaged": [],
            "firmware_update": {"available": False}, "network": None,
            "faders": [{"fader": 1, "label": "Mic 1", "percent": 70, "muted": False},
                       {"fader": 2, "label": "empty", "percent": None, "muted": False}],
            "monitor_percent": 40, "bank": 2, "levels_ok": True,
            "routing": {"ok": True, "notes": []}}
    data.update(values)
    return data


# ---- the Current state report and the desk summary ---------------------------------------------
lines = device_section.overview_lines(overview(muted=[{"label": "Mic 2"}]))
check("the report names the desk first", lines[0], "RODECaster Pro II connected.")
check_in("a mute is named", "Muted: Mic 2.", "\n".join(lines))
check_in("no solo is said", "Nothing is soloed.", "\n".join(lines))
check_in("every fader level is listed",
         "Fader levels: Mic 1 70 percent, fader 2 empty.", "\n".join(lines))
check_in("the bank says it is the one selected", "Currently selected SMART pad bank: 2.",
         "\n".join(lines))
check("a failed read is its message", device_section.overview_lines(
    {"ok": False, "message": "No desk."}), ["No desk."])

check("a clean desk's summary says what was checked, then the bank",
      device_section.desk_summary(overview()),
      "Nothing is muted or soloed and every output is on its usual mix. SMART pad bank 2.")
summary = device_section.desk_summary(overview(
    muted=[{"label": "Mic 2"}], recording={"state": 1},
    routing={"ok": True, "notes": ["Chat carries only Mic 1."]}))
check_in("a paused take is an alert", "A recording is paused.", summary)
check_in("so is a mute", "Muted: Mic 2.", summary)
check_in("a routing note is said as it is", "Chat carries only Mic 1.", summary)
check_not_in("the levels are left to the report", "percent", summary)
check("offline is an alert only while guests expect to reach the desk",
      [device_section.desk_summary(overview(network={"online": False,
                                                        "callme_hosting": hosting})).count(
          "offline") for hosting in (False, True)], [0, 1])
check("an unread desk has no summary", device_section.desk_summary({"ok": False}), "")

check("an offline network says what it costs",
      device_section.network_line({"wifi_on": True, "wifi_ssid": "Home"}),
      "Network: offline. Wifi is on but not connected to Home. CallMe and the firmware check "
      "need wifi or a network cable.")

# ---- the hardware and wireless readings ------------------------------------------------------
hardware = device_section.hardware_lines({"ok": True, "storage": {"volumes": [
    {"label": "Memory card", "inserted": False},
    {"label": "USB drive", "inserted": True, "free_mb": 10, "capacity_mb": 100, "free_pct": 10,
     "low": True},
    {"label": "Internal storage", "inserted": True, "free_mb": 50, "capacity_mb": 100,
     "free_pct": 50}]},
    "recorder": {"state_label": "Ready", "total_recordings": 3}})
check("an empty slot says nothing is in it", hardware[0], "Memory card: nothing in it.")
check("a nearly full drive says so", hardware[1],
      "USB drive: in use, 10 MB free of 100 MB, 10 percent left. This is nearly full.")
check("the internal storage comes after the slots", hardware[2],
      "Internal storage: 50 MB free of 100 MB, 50 percent left.")
check_in("the recorder is a reading", "Recorder: ready. 3 recordings on the desk.", hardware[3])
check("a firmware update is named after the version",
      device_section.firmware_line("1.7.6", {"available": True, "version": "1.7.7"}),
      "1.7.6. Version 1.7.7 is available.")
check("firmware older than tested says so, before the update",
      device_section.firmware_line("1.2.1", {"available": True, "version": "1.7.7"},
                                   {"version": "1.2.1", "tested": "1.7.3"}),
      "1.2.1. This app is tested on 1.7.3 and later, so some settings may not read or take. "
      "Version 1.7.7 is available.")
check("a current firmware is just its version", device_section.firmware_line("1.7.7", None),
      "1.7.7")
check_in("firmware older than tested is an alert",
         "The desk's firmware is 1.2.1 and this app is tested on 1.7.3 and later",
         device_section.desk_summary(overview(firmware_untested={"version": "1.2.1",
                                                                 "tested": "1.7.3"})))
check("no mic slots is not none connected", device_section.mic_lines({}),
      ["Wireless mics: the desk did not report any, so there is nothing to show."])
facts = device_section.network_facts({"wifi": {"on": False}, "bluetooth": {"visible": True},
                                      "still_read_only": "A fixed IP cannot be set."})
check_true("with the radio off no wifi fact is listed",
           not any(line.startswith("WiFi") for line in facts))
check_in("discoverable is said in words", "Bluetooth discoverable: Yes, other devices can find it.",
         "\n".join(facts))
check("what stays read-only comes last", facts[-1], "A fixed IP cannot be set.")

# ---- the section reads every page once the desk is read ---------------------------------------
frame = FakeFrame()
section = device_section.DeviceSection(frame)
reads = []
for view in section.views:
    # A re-build from the desk model (`full` False) is told apart from a read of the desk.
    view.read = (lambda name: lambda announce=False, full=True: reads.append(
        (name, announce) if full else (name, announce, "model")))(type(view).__name__)
frame.desk.listener("read", None)
check("the desk state's first read reads every page", reads,
      [("StatePage", False), ("HardwarePage", False), ("UnitPage", False),
       ("NetworkPage", False)])
frame.desk.listener("read", None)
frame.desk.listener("changed", {"Mic 1"})
check("later desk changes read nothing at once", len(reads), 4)
check("but the summary is due", section._summary_timer is not None, True)
section._summary_timer.Stop()
section._summary_due()
check("and then re-builds the overview alone, from the desk model", reads[4:],
      [("StatePage", False, "model")])
frame.desk.listener("levels", None)
check("a level change is not in the summary, so it reads nothing", section._summary_timer.IsRunning(),
      False)
section.network.Show()
section.refresh(True)
check("F5 reads only the page shown, and says so", reads[-1], ("NetworkPage", True))
check("and nothing else", len(reads), 6)
section.network.Hide()
asked = []
with _testkit.patched((device_page, {"overview": lambda fresh=True: asked.append(fresh) or {}})):
    type(section.state).fetch(section.state, True)
    type(section.state).fetch(section.state, False)
check("a read asks the desk, a re-build the desk model", asked, [True, False])
section.state.Show()
check("opening Current state reads the desk: free space is never pushed", reads[-1],
      ("StatePage", False))
section.state.Hide()
followed = {type(fn.__self__._read.__self__).__name__: names
            for names, fn in frame.desk.watched}
check("Hardware, Desk settings and Wireless connections follow what they show", followed,
      {"HardwarePage": set(device_page.HARDWARE_PROPS), "UnitPage": set(device_page.UNIT_SETTING_PROPS),
       "NetworkPage": set(network.PROPS)})
taken = []
for page in section.views:
    try:
        inspect.signature(type(page).fetch).bind(page, full=True)
        taken.append(type(page).__name__)
    except TypeError:
        pass
check("every page's fetch takes `full` by keyword, as the reader passes it", taken,
      ["StatePage", "HardwarePage", "UnitPage", "NetworkPage"])

# ---- a read the desk's pushes ask for leaves the USB facts alone --------------------------------
frame = FakeFrame()
hw = device_section.DeviceSection(frame).hardware
calls = []
real = device_page.read_hardware, device_page.read_status
device_page.read_hardware = lambda: calls.append("state") or {"ok": True, "firmware": "1.7.7"}
device_page.read_status = lambda: calls.append("status") or {"connected": True}
try:
    check("a followed read reads the desk's state alone", (sorted(hw.fetch(False)), calls),
          (["ok", "state"], ["state"]))
    check("a full read rescans the USB as well", (sorted(hw.fetch(True)), calls[1:]),
          (["ok", "state", "status"], ["state", "status"]))
finally:
    device_page.read_hardware, device_page.read_status = real
hw.connection.SetValue("Connected and responding")
hw.shown({"ok": True, "state": {"ok": True, "firmware": "1.7.8", "firmware_update": {}}})
check("and putting it on the page leaves the USB facts as they were",
      (hw.firmware.GetValue(), hw.connection.GetValue()), ("1.7.8", "Connected and responding"))

# ---- the summary goes to the status bar, not to speech -----------------------------------------
frame = FakeFrame()
state = device_section.DeviceSection(frame).state
said = state.shown(overview(muted=[{"label": "Mic 2"}]))
check_in("the status bar carries the summary", "Muted: Mic 2.", frame.status[-1])
check("and F5 says the same", said, frame.status[-1])
check("nothing is spoken on its own", frame.said, [])
check_in("the report is on the page", "Muted: Mic 2.", state.report.GetValue())
state.shown({"ok": False, "message": "No desk."})
check("an unread desk's status is the reason", frame.status[-1], "No desk.")

# ---- the firmware install ----------------------------------------------------------------------
frame = FakeFrame()
hw = device_section.DeviceSection(frame).hardware
check("with no update offered there is no install button", hw.install.IsShown(), False)
hw._show_install({"available": True, "version": "1.7.7"})
check("an offered update shows its button, named after it",
      (hw.install.IsShown(), hw.install.GetLabel()), (True, "Install firmware 1.7.7"))
hw._show_install({"available": False})
check("and it goes once nothing is offered", hw.install.IsShown(), False)
hw._ask_install({"ok": True, "version": "1.7.7", "refusal": "The desk is recording."})
check("a refusal is spoken and asks nothing", frame.said[-1], "The desk is recording.")
with FirmwareDialog(frame.window, "1.7.7", "1.7.6", "Before firmware update") as ask:
    buttons = [c for c in ask.GetChildren() if isinstance(c, (wx.Button, wx.CheckBox))]
    check("the question lands on Cancel", buttons[0].GetId(), wx.ID_CANCEL)
    check("and backs up first unless unticked", ask.wants_backup(), True)
    check_in("it says there is no way back", "no way back",
             firmware_dialog.question_text("1.7.7", "1.7.6"))

# ---- unit settings ---------------------------------------------------------------------------
frame = FakeFrame()
section = device_section.DeviceSection(frame)
unit = section.unit
dim = unit.controls["screenDimAfterSeconds"]
check("before a read a setting refuses", unit._guard(), device_section.UNIT_UNREAD)
values = {s["prop"]: None for s in desk_labels.UNIT_SETTINGS}
values.update(screenDimAfterSeconds=45, outputMonFixed=1, screenBrightness=180)
settings = [dict(s, value=values[s["prop"]]) for s in desk_labels.UNIT_SETTINGS]
unit.shown({"ok": True, "settings": settings, "midi": True, "writable": True,
            "device_name": "RODECaster Pro II"})
check("a dim time off the list is listed as it is",
      dim.control.GetString(dim.control.GetSelection()), "Currently 45")
check("a switch shows the desk's value", unit.controls["outputMonFixed"].box.GetValue(), True)
check("a brightness lands on its slider", unit.controls["screenBrightness"].slider.GetValue(), 180)
check("and is read as a fraction and a percentage",
      unit.controls["screenBrightness"]._value_words(), "180 of 255, about 71 percent")
check("MIDI rides on the same read", unit.midi.box.GetValue(), True)
check_in("a setting the desk did not report says so", "did not report",
         unit.controls["metering"].acc.description)
check("a writable desk hides the note", unit.note.IsShown(), False)
check("and refuses nothing", unit._guard(), None)
unit.shown({"ok": True, "settings": settings, "midi": None, "writable": False,
            "device_name": "RODECaster Duo",
            "read_only_note": "These settings can be read but not changed on the RODECaster Duo."})
check("a read-only desk refuses the writes", unit._guard(), device_section.UNIT_SETTINGS_OFF)
check_in("and the note names it", "RODECaster Duo", unit.note.GetValue())

# ---- wireless connections --------------------------------------------------------------------
net = section.network


def network_reply(on, ssid="Home"):
    return {"ok": True, "writable": True,
            "wifi": {"on": on, "ssid": ssid, "connected": on and bool(ssid),
                     "ip": "10.0.0.2" if on and ssid else "",
                     "scan": []},
            "bluetooth": {"visible": False, "paired": [], "scan": []},
            "wireless_mics": {"present": True, "any_connected": False}, "still_read_only": ""}


check("before a read the wifi switch refuses", net._guard(), device_section.NETWORK_UNREAD)
net.shown(network_reply(False))
check("with the radio off there is neither button", (net.scan.IsShown(), net.disconnect.IsShown()),
      (False, False))
check("and the status says it is off", net.wifi_status.GetValue(), device_section.WIFI_OFF)
check("a one-line reading is reached by Tab", net.wifi_status.AcceptsFocusFromKeyboard(), True)
net.shown(network_reply(True))
check("joined, the status names the network and address", net.wifi_status.GetValue(),
      "Connected to Home. The desk's address is 10.0.0.2.")
check("and Disconnect is offered, named after it, with no scan",
      (net.disconnect.IsShown(), net.disconnect.GetLabel(), net.scan.IsShown()),
      (True, "Disconnect from Home", False))
net.shown(network_reply(True, ssid=""))
check("with no network set up, Scan replaces Disconnect",
      (net.scan.IsShown(), net.disconnect.IsShown()), (True, False))
check("and the status says so", net.wifi_status.GetValue(), "Not connected to a wifi network.")
stuck = network_reply(True)
stuck["wifi"].update(ip="", incorrect_psk=True)
net.shown(stuck)
check("a name with no address is not called connected", net.wifi_status.GetValue(),
      "Set to join Home, but not connected. It refused the password.")

# The dialogs: a listed network lands on its password, an open one has none, one not listed on
# its name.
secured = {"ssid": "Home", "open": False, "spoken": "Home, needs a password"}
with wifi_dialogs.JoinDialog(frame.window, secured) as join:
    check("a listed network's dialog is named after it", join.GetTitle(), "Join Home")
    check("its name is filled in", join.name_box.GetValue(), "Home")
    first = next(c for c in join.GetChildren() if isinstance(c, wx.TextCtrl))
    check("and the password is the box it lands on", first is join.password_box, True)
with wifi_dialogs.JoinDialog(frame.window, {"ssid": "Cafe", "open": True}) as join:
    check("an open network has no password box", join.password_box, None)
with wifi_dialogs.JoinDialog(frame.window, wifi_dialogs.NOT_LISTED) as join:
    first = next(c for c in join.GetChildren() if isinstance(c, wx.TextCtrl))
    check("a network not listed lands on an empty name",
          (first is join.name_box, join.name_box.GetValue(), join.password_box is not None),
          (True, "", True))
with wifi_dialogs.NetworksDialog(frame.window, [secured], "Found 1 network.") as pick:
    pick.EndModal = lambda _code: None
    pick._chosen()
    check("choosing from the list gives its scan entry", pick.choice, secured)
with wifi_dialogs.NetworksDialog(frame.window, [], "Found no networks.") as pick:
    check("with nothing found there is no list", pick.list, None)


joins, reports = [], []
joined = network_reply(True)
with _testkit.patched(
        (device_section, {"JobDialog": FakeJob,
                          "call": lambda _o, fn, *a, done=None: done(fn(*a)),
                          "report": lambda _p, titles, reply: reports.append(
                              titles[0] if reply.get("ok") else titles[1])}),
        (network, {"action_join_wifi": lambda ssid, password: joins.append((ssid, password))
                   or {"ok": True, "message": "Joined Home.", "wifi": joined["wifi"]}}),
        (net, {"read": lambda: None})):
    net._join("Home", "secret", secured, None)
check("joining hands the name and password to the network module", joins, [("Home", "secret")])
check("and the dialog finishes with its reply", reports, ["Joined"])
ro = network_reply(True)
ro["writable"] = False
net.shown(ro)
check("a read-only desk refuses the network writes", net._guard(),
      device_section.NETWORK_READ_ONLY)

_a11y.check_windows("Device")
sys.exit(_testkit.finish())
