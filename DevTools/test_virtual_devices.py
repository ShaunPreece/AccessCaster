"""Offline checks for virtual_devices.py: the driver's status block is parsed as its tray icon
reads it, and the spoken outcomes say the right thing. No desk, no driver, no COM.

    python DevTools/test_virtual_devices.py
"""
import _bootstrap  # noqa: F401
import _testkit
import virtual_devices as vd
from _testkit import check, check_true, patched


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



def parse(raw: bytes, installed=True) -> vd.DriverStatus:
    with patched((vd, {"_read_block": lambda: raw, "_installed": lambda: installed})):
        return vd.read_status()


st = parse(CONNECTED)
check_true("connected block reads connected", st.running and st.connected and st.enabled)
check_true(f"desk string: {st.desk!r}", st.desk == "RØDECaster Pro II (GV0064612)")
check_true("versions found by label", st.firmware == "1.76" and st.driver_version == "1.0.5.0")
check_true(st.phrase(), st.phrase().startswith("RODE's Virtual Devices driver is connected to the RØDECaster"))

st = parse(ABSENT)
check_true("absent block reads not connected", st.running and not st.connected and st.enabled)
check_true("no desk string while disconnected", st.desk == "")
check_true("firmware survives the overwritten first letter", st.firmware == "1.76")
check_true(st.phrase(), "not connected" in st.phrase())

st = parse(OFF)
check_true(st.phrase(), not st.enabled and "switched off" in st.phrase())

# No block at all is the service not running; the registry key alone says installed.
st = parse(None, installed=False)
check_true(st.phrase(), not st.installed and not st.running and "not installed" in st.phrase())
st = parse(None, installed=True)
check_true(st.phrase(), st.installed and not st.running and "service is not running" in st.phrase())

# Wording.
# The bind and the release are RODE's notification to say, never ours.
o = vd.Outcome(status=vd.DriverStatus(), enabled=["Fader1", "Fader2", "System"],
               faders_ready=9)
check_true(o.sentence(), o.sentence() == "")
o = vd.Outcome(status=vd.DriverStatus(), disabled=["System"])
check_true(o.sentence(), o.sentence() == "")
o = vd.Outcome(status=vd.DriverStatus(), faders_ready=9,
               default_moved_to="System (RØDECaster)")
check_true(o.sentence(), o.sentence() == "Windows sound is on System.")
o = vd.Outcome(status=vd.DriverStatus(),
               default_moved_to="Speakers (RODECaster Pro II Main Stereo)")
check_true(o.sentence(), o.sentence() == "Windows sound is back on the desk.")
o = vd.Outcome(status=vd.DriverStatus(), faders_missing=True)
check_true(o.sentence(), o.sentence().startswith("RODE's driver is connected but has not created its fader inputs."))

# WINDOWS FALLING BACK IS TOLD APART FROM THE USER CHOOSING. Endpoints and the default are faked.
def ep(id_, state="active", desk=False, virtual=False):
    return vd.Endpoint(id=id_, name=id_, flow="playback", state=state, virtual=virtual, desk=desk)


def note(default, eps):
    with patched((vd, {"_default_id": lambda enum, role: default, "_endpoints": lambda enum: eps})):
        vd._note_default(None)


vd._last_default, vd._displaced = "", ""
note("system", [ep("system", virtual=True), ep("speakers")])
check_true("the first sample records nothing", vd._displaced == "")
note("speakers", [ep("system", state="not present", virtual=True), ep("speakers")])
check_true("System went away and Windows fell back: displaced", vd._displaced == "speakers")
note("main", [ep("speakers"), ep("main", desk=True)])
check_true("moving on from a live device clears it", vd._displaced == "")
note("speakers", [ep("speakers"), ep("main", desk=True)])
check_true("the user picking the speakers while the desk is live is a choice", vd._displaced == "")
vd._last_default, vd._displaced = "", ""


# WHILE THE SERVICE IS BOUND, ONLY SYSTEM WILL DO: it holds the desk's Main output, which plays nothing.
class Policy:
    def __init__(self):
        self.set = []

    def SetDefaultEndpoint(self, id_, role):
        self.set.append(id_)


def restore(default, eps, connected, parked="", standard=False):
    policy = Policy()
    with patched((vd, {"_default_id": lambda enum, role: default})):
        moved = vd._restore_default(None, policy, eps, connected, parked, standard=standard)
    return moved, policy.set


speakers, main = ep("speakers"), ep("Main Multitrack", desk=True)
system = ep("System (RØDECaster)", virtual=True)
check("bound with no System yet: the default stays where Windows put it",
      restore("speakers", [speakers, main], True, parked="speakers"), ("", []))
check("bound with System live: it goes to System",
      restore("speakers", [speakers, main, system], True, parked="speakers"),
      (system.name, [system.id, system.id]))
check("back in Standard: it goes to the desk's Main output",
      restore("speakers", [speakers, ep("Main Stereo", desk=True)], False, parked="speakers",
              standard=True), ("Main Stereo", ["Main Stereo", "Main Stereo"]))

raise SystemExit(_testkit.finish())
