"""Offline checks for virtual_devices.py: the driver's state and the spoken outcomes say the
right thing. No desk, no driver.

    python DevTools/test_virtual_devices.py
"""
import _bootstrap  # noqa: F401
import _testkit
import virtual_devices as vd
from _testkit import check, check_true, patched
from wording import OS_NAME

# Where the binding can be seen (Windows), whatever runs this.
vd.SEES_BINDING = True
st = vd.DriverStatus(installed=True, running=True, connected=True, desk="RØDECaster Pro II (GV0064612)")
check_true(st.phrase(), st.phrase().startswith("RODE's Virtual Devices driver is connected to the RØDECaster"))
st = vd.DriverStatus(installed=True, running=True)
check_true(st.phrase(), "not connected" in st.phrase())
st = vd.DriverStatus(installed=True, running=True, enabled=False)
check_true(st.phrase(), "switched off" in st.phrase())
st = vd.DriverStatus()
check_true(st.phrase(), "not installed" in st.phrase())
st = vd.DriverStatus(installed=True)
check_true(st.phrase(), "service is not running" in st.phrase())

# Where it cannot (a Mac): RODE's app, installed and running or not, and nothing waited for.
with patched((vd, {"SEES_BINDING": False})):
    check_true(vd.DriverStatus().phrase(), "app is not installed" in vd.DriverStatus().phrase())
    st = vd.DriverStatus(installed=True)
    check_true(st.phrase(), "installed but not running" in st.phrase())
    st = vd.DriverStatus(installed=True, running=True)
    check_true(st.phrase(), st.phrase().startswith("RODE's Virtual Devices app is running."))
    reads = []
    with patched((vd, {"read_status": lambda: reads.append(1) or vd.DriverStatus(
            installed=True, running=True)})):
        vd.wait_connected(timeout_s=5)
    check("a bind it cannot see is not waited for", len(reads), 1)

# Wording.
# The bind and the release are RODE's notification to say, never ours.
o = vd.Outcome(status=vd.DriverStatus(), enabled=["Fader1", "Fader2", "System"],
               faders_ready=9)
check_true(o.sentence(), o.sentence() == "")
o = vd.Outcome(status=vd.DriverStatus(), disabled=["System"])
check_true(o.sentence(), o.sentence() == "")
o = vd.Outcome(status=vd.DriverStatus(), faders_ready=9,
               default_moved_to="System (RØDECaster)")
check_true(o.sentence(), o.sentence() == f"{OS_NAME} sound is on System.")
o = vd.Outcome(status=vd.DriverStatus(),
               default_moved_to="Speakers (RODECaster Pro II Main Stereo)")
check_true(o.sentence(), o.sentence() == f"{OS_NAME} sound is back on the desk.")
o = vd.Outcome(status=vd.DriverStatus(), faders_missing=True)
check_true(o.sentence(), o.sentence().startswith("RODE's driver is connected but has not created its fader inputs."))

raise SystemExit(_testkit.finish())
