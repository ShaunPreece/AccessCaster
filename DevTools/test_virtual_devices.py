"""Offline checks for virtual_devices.py: the driver's status block is parsed as its tray icon
reads it, and the spoken outcomes say the right thing. No desk, no driver, no COM.

    python DevTools/test_virtual_devices.py
"""
import _bootstrap  # noqa: F401

import virtual_devices as vd


def block(connected: int, enabled: int, desk: str, tail: str = "") -> bytes:
    """A status block as the service lays it out: three dwords, the desk string at byte 12,
    then whatever an earlier, longer string left behind it."""
    head = connected.to_bytes(4, "little") + enabled.to_bytes(4, "little") + (79184).to_bytes(4, "little")
    body = desk.encode("cp1252") + b"\0" + tail.encode("cp1252")
    return (head + body).ljust(vd._BLOCK_BYTES, b"\0")


CONNECTED = block(1, 1, "RØDECaster Pro II (GV0064612)",
                  "\0Firmware Version: 1.76\0\0App Version: 1.0.5.0")
# Disconnected: the shorter string overwrites the start of the longer one and its tail stays.
ABSENT = block(0, 1, "No RØDECaster Connected", "4612)\0\0\0irmware Version: 1.76\0\0App Version: 1.0.5.0")
OFF = block(0, 0, "No RØDECaster Connected")

checks = 0


def check(cond, what):
    global checks
    checks += 1
    if not cond:
        raise SystemExit(f"FAIL at check {checks}: {what}")


def parse(raw: bytes, installed=True) -> vd.DriverStatus:
    saved = (vd._read_block, vd._installed)
    vd._read_block, vd._installed = (lambda: raw), (lambda: installed)
    try:
        return vd.read_status()
    finally:
        vd._read_block, vd._installed = saved


st = parse(CONNECTED)
check(st.running and st.connected and st.enabled, "connected block reads connected")
check(st.desk == "RØDECaster Pro II (GV0064612)", f"desk string: {st.desk!r}")
check(st.firmware == "1.76" and st.driver_version == "1.0.5.0", "versions found by label")
check(st.phrase().startswith("RODE's Virtual Devices driver is connected to the RØDECaster"), st.phrase())

st = parse(ABSENT)
check(st.running and not st.connected and st.enabled, "absent block reads not connected")
check(st.desk == "", "no desk string while disconnected")
check(st.firmware == "1.76", "firmware survives the overwritten first letter")
check("not connected" in st.phrase(), st.phrase())

st = parse(OFF)
check(not st.enabled and "switched off" in st.phrase(), st.phrase())

# No block at all is the service not running; the registry key alone says installed.
st = parse(None, installed=False)
check(not st.installed and not st.running and "not installed" in st.phrase(), st.phrase())
st = parse(None, installed=True)
check(st.installed and not st.running and "service is not running" in st.phrase(), st.phrase())

# Wording.
# The bind and the release are RODE's notification to say, never ours.
o = vd.Outcome(status=vd.DriverStatus(), enabled=["Fader1", "Fader2", "System"],
               faders_ready=9, system_ready=True)
check(o.sentence() == "", o.sentence())
o = vd.Outcome(status=vd.DriverStatus(), disabled=["System"])
check(o.sentence() == "", o.sentence())
o = vd.Outcome(status=vd.DriverStatus(), faders_ready=9, system_ready=True,
               default_moved_to="System (RØDECaster)")
check(o.sentence() == "Windows sound is on System.", o.sentence())
o = vd.Outcome(status=vd.DriverStatus(),
               default_moved_to="Speakers (RODECaster Pro II Main Stereo)")
check(o.sentence() == "Windows sound is back on the unit.", o.sentence())
o = vd.Outcome(status=vd.DriverStatus(), system_ready=True, faders_missing=True)
check(o.sentence().startswith("RODE's driver is connected but has not created its fader inputs."),
      o.sentence())

# WINDOWS FALLING BACK IS TOLD APART FROM THE USER CHOOSING. Endpoints and the default are faked.
def ep(id_, state="active", desk=False, virtual=False):
    return vd.Endpoint(id=id_, name=id_, flow="playback", state=state, virtual=virtual, desk=desk)


def note(default, eps):
    saved = (vd._default_id, vd._endpoints)
    vd._default_id, vd._endpoints = (lambda enum, role: default), (lambda enum: eps)
    try:
        vd._note_default(None)
    finally:
        vd._default_id, vd._endpoints = saved


vd._last_default, vd._displaced = "", ""
note("system", [ep("system", virtual=True), ep("speakers")])
check(vd._displaced == "", "the first sample records nothing")
note("speakers", [ep("system", state="not present", virtual=True), ep("speakers")])
check(vd._displaced == "speakers", "System went away and Windows fell back: displaced")
note("main", [ep("speakers"), ep("main", desk=True)])
check(vd._displaced == "", "moving on from a live device clears it")
note("speakers", [ep("speakers"), ep("main", desk=True)])
check(vd._displaced == "", "the user picking the speakers while the desk is live is a choice")
vd._last_default, vd._displaced = "", ""

print(f"virtual_devices: {checks} checks passed")
