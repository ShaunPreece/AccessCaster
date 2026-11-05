"""RODE's app being open (Project/rode_app.py), offline.

Seen once per run of it: the spoken notice's hook fires as it opens; while it is open a write
addressed by the fader layout reads the desk rather than trusting the desk model, and every read is
compared with the model, because no write of RODE's app is pushed; and as it closes the desk is
read once more, still compared, for what it changed since.

    python DevTools/test_rode_app.py
"""
from __future__ import annotations

import sys

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
import channels
import desk_model
import rcp_read
import rode_app
import server
from _testkit import check, check_true, hold_desk, patched

LAYOUT = [0, 7, 2, 3, 10, 11, 1, 16, 9, 9]

seen = []
rode_app.on_seen = lambda: seen.append("seen")
rode_app.note(False)

print("RODE'S APP OPENING AND CLOSING")
check("it reads as closed", rode_app.is_open(), False)
rode_app.note(True)
check("opening fires the notice hook once", seen, ["seen"])
check_true("and it reads as open", rode_app.is_open())
rode_app.note(True)
check("staying open fires nothing more", seen, ["seen"])
rode_app.note(False)
check("closing fires nothing", seen, ["seen"])
rode_app.note(True)
check("a second run is told of again", seen, ["seen", "seen"])
rode_app.note(False)

print("\nTHE NOTICE HOOK FAILING")
rode_app.on_seen = lambda: 1 / 0
rode_app.note(True)
check_true("costs nothing but itself", rode_app.is_open())
rode_app.note(False)

print("\nCOMPARING READS, AND THE READ AS IT CLOSES")
reads_done = []
with patched((server, {"_read_unheard": lambda: reads_done.append(desk_model._reveal)})):
    server._follow_rode_app(True)
    server._follow_rode_app(True)
    check("while it is open, reads are compared and nothing is read on a timer",
          (desk_model._reveal, reads_done), (True, []))
    server._follow_rode_app(False)
    check("as it closes, one read, still compared, then comparing stops",
          (reads_done, desk_model._reveal), ([True], False))
    server._follow_rode_app(False)
    check("and nothing more while it stays closed", reads_done, [True])

print("\nADDRESSING A WRITE")
reads = []


def read_model(*_a, **k):
    reads.append(k.get("fresh"))
    return b""


with patched((rcp_read, {"read_model": read_model, "values": lambda blob, name: list(LAYOUT)})):
    hold_desk(LAYOUT)
    check("with RODE's app closed, the trusted model is used", (channels.strip_vector(), reads),
          (LAYOUT, []))
    rode_app.note(True)
    check("with it open, the desk is read, fresh", (channels.strip_vector(), reads),
          (LAYOUT, [True]))
    check_true("though the model is still trusted", desk_model.trusted())
    rode_app.note(False)

print("\nTHE PROCESS CHECK")
with patched((rode_app, {"EXE": "python.exe"})):
    check_true("this test's own interpreter is found by name", rode_app.running())
with patched((rode_app, {"EXE": "no such program.exe"})):
    check("a name nothing runs under is not", rode_app.running(), False)

sys.exit(_testkit.finish())
