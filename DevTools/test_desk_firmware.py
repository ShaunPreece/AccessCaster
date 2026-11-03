"""Offline: the firmware install sequence (`desk_firmware`) against a simulated desk.

    python DevTools/test_desk_firmware.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
import desk_firmware as fw
from _testkit import check, check_in, patched

PATH = (15,)
FAST = {"PRESENCE_POLL_S": 0.001, "LEAVES_BY_ITSELF_S": 0.05, "LEAVES_AFTER_ASK_S": 0.05,
        "BACK_WITHIN_S": 0.5, "CHECK_WAIT_S": 0.01, "DOWNLOAD_STALL_S": 0.05,
        "FINISHED_QUIET_S": 0.01, "INSTALL_WAIT_S": 0.5}


def base(**over):
    v = {p: False for p in fw.PROPS}
    v.update(systemFirmwareVersion="1.7.6", osUpdateAvailable=True, updateVersion="1.7.7",
             updateDownloadProgress=0, updateInstalledProgress=10000)
    v.update(over)
    return v


class Listener:
    def __init__(self, desk):
        self.desk = desk
        self.lost = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def _next(self, names):
        while self.desk.events:
            name, value = self.desk.events.pop(0)
            if name in names:
                return name, value
        return None

    def wait_any(self, names, timeout):
        return self._next(names)

    def wait_for(self, name, timeout, match=None):
        got = self._next((name,))
        return got[1] if got else None


class Desk:
    """A desk that answers each request the way the engine does, unless told otherwise."""

    def __init__(self, values, leaves_by_itself=True, comes_back="1.7.7", download=True):
        self.values = values
        self.writes = []
        self.events = []
        self.leaves_by_itself = leaves_by_itself
        self.comes_back = comes_back
        self.download = download
        self.gone = False
        self.back_after = 0

    def state(self):
        if self.gone:
            raise OSError("no device")
        return {k: (PATH, v) for k, v in self.values.items()}

    def write(self, state, name, value):
        self.writes.append((name, value))
        self.values[name] = value
        if not value:
            return
        if name == "updateCheckRequested":
            self.events.append(("updateChecking", False))
        elif name == "downloadInitiateRequested" and self.download:
            self.events += [("updateDownloadProgress", n) for n in range(0, 100, 7)]
            self.events += [("updateDownloadProgress", 99), ("updateDownloaded", True)]
        elif name == "updateInitiateRequested":
            self.events += [("updateInstalledProgress", 5000), ("updateInstalledProgress", 10000),
                            ("updateComplete", True)]
            self.gone = self.leaves_by_itself
            self.back_after = 2
        elif name == "updateRebootRequested":
            self.gone = True
            self.back_after = 2

    def listener(self):
        return Listener(self)

    def present(self):
        if self.gone:
            if self.back_after > 0:
                self.back_after -= 1
                return None
            self.gone = False
            self.values["systemFirmwareVersion"] = self.comes_back
        return 0x0037

    def flashing(self):
        return False


def run(desk, target="1.7.7"):
    steps = []
    with patched((fw, FAST)):
        res = fw.install(desk, steps.append, target)
    return res, steps


# ---- refusals, before anything is written --------------------------------------------------------
state = {k: (PATH, v) for k, v in base().items()}
check("an offered update with the desk online refuses nothing", fw.refusal(state, True, False, False),
      "")
check("the offered version is read", fw.offered(state), "1.7.7")
check_in("offline is refused", "not connected to the internet",
         fw.refusal(state, False, False, False))
check_in("a recording is refused", "recording", fw.refusal(state, True, True, False))
check_in("a call is refused", "CallMe call", fw.refusal(state, True, False, True))
wipe = {k: (PATH, v) for k, v in base(updateResetAfterFWURequested=True).items()}
check_in("a pending erase is refused first", "erase itself", fw.refusal(wipe, True, False, False))
none = {k: (PATH, v) for k, v in base(osUpdateAvailable=False).items()}
check_in("no update is refused", "no firmware update waiting", fw.refusal(none, True, False, False))

# ---- the whole update, the desk restarting by itself ---------------------------------------------
desk = Desk(base(downloadInitiateRequested=True, downloadCancelRequested=True))
res, steps = run(desk)
check("it succeeds on the version the desk comes back on", (res["ok"], res["version"]),
      (True, "1.7.7"))
check("stuck request flags are cleared before anything is asked",
      desk.writes[:2], [("downloadInitiateRequested", False), ("downloadCancelRequested", False)])
asked = [name for name, value in desk.writes if value]
check("then check, download and install, in that order",
      asked, ["updateCheckRequested", "downloadInitiateRequested", "updateInitiateRequested"])
check("a desk that restarts by itself is not asked to", "updateRebootRequested" in asked, False)
check("download progress is reported as each ten is passed",
      "Downloading firmware 1.7.7: 42 percent." in steps, True)
check("each ten is said once, and nought not at all", sum("percent" in s for s in steps), 9)
check_in("the restart is announced", "audio stops", " ".join(steps))

# ---- a desk that stays put after the install is asked to restart ---------------------------------
desk = Desk(base(), leaves_by_itself=False)
res, _steps = run(desk)
check("the restart is requested once the install is over",
      [n for n, v in desk.writes if v][-1], "updateRebootRequested")
check("and the update completes", res["ok"], True)

# ---- the erase flag appearing after the install stops the restart --------------------------------
desk = Desk(base(), leaves_by_itself=False)
real_write = desk.write


def write_then_flag(state, name, value):
    real_write(state, name, value)
    if name == "updateInitiateRequested" and value:
        desk.values["updateResetAfterFWURequested"] = True


desk.write = write_then_flag
res, _steps = run(desk)
check("no restart is asked for while the erase flag is set",
      ("updateRebootRequested", True) in desk.writes, False)
check_in("and the user is told why", "erase itself", res["message"])

# ---- a stalled download stops the update, and nothing installs -----------------------------------
desk = Desk(base(), download=False)
res, _steps = run(desk)
check("a download that never moves is not cancelled, which holds the engine",
      ("downloadCancelRequested", True) in desk.writes, False)
check("nothing is installed", ("updateInitiateRequested", True) in desk.writes, False)
check_in("and the user is told nothing was installed", "Nothing was installed", res["message"])

# ---- a download that ends without pushing updateDownloaded (already True) ------------------------
desk = Desk(base(updateDownloaded=True))
real = desk.write


def no_downloaded_push(state, name, value):
    real(state, name, value)
    if name == "downloadInitiateRequested" and value:
        desk.events = [e for e in desk.events if e[0] != "updateDownloaded"]


desk.write = no_downloaded_push
res, _steps = run(desk)
check("a finished download is read back when its end is not pushed", res["ok"], True)

# ---- the offer changing between the question and the start ---------------------------------------
desk = Desk(base())
res, _steps = run(desk, target="1.7.8")
check_in("a different offer stops before downloading", "no longer offers firmware 1.7.8",
         res["message"])
check("and downloads nothing", ("downloadInitiateRequested", True) in desk.writes, False)

# ---- the desk coming back on the old version -----------------------------------------------------
desk = Desk(base(), comes_back="1.7.6")
res, _steps = run(desk)
check("a desk back on the old version is a failure", res["ok"], False)
check_in("named as such", "reports firmware 1.7.6, not 1.7.7", res["message"])

sys.exit(_testkit.finish())
