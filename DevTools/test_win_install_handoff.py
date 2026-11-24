"""Windows' installer hand-off (Project/win/install_handoff.py), offline: nothing is launched.

    python DevTools/test_win_install_handoff.py
"""
from __future__ import annotations

import sys

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
from _testkit import check, check_true, patched
from win import install_handoff

INSTALLER = r"C:\Temp\AccessCaster-Setup.exe"
calls = []


def popen(args, **kwargs):
    calls.append(("launch", args, kwargs))


def refuse(args, **kwargs):
    raise OSError("refused")


print("THE HAND-OFF")
with patched((install_handoff.subprocess, {"Popen": popen}),
             (install_handoff, {"register_restart": lambda: calls.append("register") or True,
                                "unregister_restart": lambda: calls.append("unregister")})):
    install_handoff.start(INSTALLER)
    check("the restart is registered before the installer starts", calls[0], "register")
    _, args, kwargs = calls[1]
    check("silently, restarting the app, never rebooting", args,
          [INSTALLER, "/VERYSILENT", "/RESTARTAPPLICATIONS", "/NORESTART"])
    check_true("detached, so it outlives this process",
               kwargs.get("creationflags", 0) & install_handoff.subprocess.DETACHED_PROCESS)
    check("from the installer's own folder", kwargs.get("cwd"), r"C:\Temp")

calls.clear()
with patched((install_handoff.subprocess, {"Popen": refuse}),
             (install_handoff, {"register_restart": lambda: calls.append("register") or True,
                                "unregister_restart": lambda: calls.append("unregister")})):
    try:
        install_handoff.start(INSTALLER)
        raised = False
    except OSError:
        raised = True
    check_true("an installer that cannot start raises", raised)
    check("and leaves nothing registered", calls, ["register", "unregister"])

print("\nTHE RESTART")
check("a source run never registers a restart", install_handoff.register_restart(), False)

sys.exit(_testkit.finish())
