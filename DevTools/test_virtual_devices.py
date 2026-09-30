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
o = vd.Outcome(status=vd.DriverStatus(), enabled=["Fader1", "Fader2", "System"],
               faders_ready=9, system_ready=True)
check(o.sentence() == "Virtual devices ready: 9 fader inputs and System.", o.sentence())
o = vd.Outcome(status=vd.DriverStatus(), say_ready=True, faders_ready=9, system_ready=True,
               default_moved_to="System (RØDECaster)")
check(o.sentence() == "Virtual devices ready: 9 fader inputs and System. "
                      "Windows sound output is now System (RØDECaster).", o.sentence())
o = vd.Outcome(status=vd.DriverStatus(), say_ready=True, system_ready=True, faders_missing=True)
check(o.sentence().startswith("Virtual devices ready: System. RODE's driver is connected but has not "
                              "created its fader inputs."), o.sentence())
o = vd.Outcome(status=vd.DriverStatus(), disabled=["System"])
check(o.sentence() == "The virtual devices are off.", o.sentence())
o = vd.Outcome(status=vd.DriverStatus(), faders_ready=9, system_ready=True)
check(o.sentence() == "", "nothing awaited and nothing flipped says nothing")

print(f"virtual_devices: {checks} checks passed")
