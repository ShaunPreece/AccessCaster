"""Offline checks on the server's guards: what it serves, what it writes, and what it puts back.

THE RULES THIS PINS:
  * /static/ serves only files inside static/, so an absolute or climbing path is refused;
  * a request body that is not a JSON object is refused rather than read as {};
  * settings.json is replaced whole, and one that will not parse is kept as settings.json.bad;
  * a Chat isolation whose setup fails part way puts back what it had already changed;
  * an announcer loop that is no longer the newest leaves without touching the live state.

No device and no speech: the desk calls are stubbed.

    python DevTools/test_server_safety.py
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import server

FAILS = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILS.append(f"{name}: got {got!r}, wanted {want!r}")
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")


print("STATIC FILES")
check("a file in static/ is served", server._static_file("app.js"),
      os.path.join(server.STATIC_DIR, "app.js"))
check("a drive path is refused", server._static_file("C:/Windows/win.ini"), None)
check("a climbing path is refused", server._static_file("../server.py"), None)
check("a climbing path in backslashes is refused", server._static_file("..\\server.py"), None)
check("the folder itself is refused", server._static_file(""), None)


print("\nREQUEST BODIES")


def body_of(raw: bytes):
    h = server.Handler.__new__(server.Handler)
    h.headers = {"Content-Length": str(len(raw))}
    h.rfile = io.BytesIO(raw)
    try:
        return h._read_json()
    except ValueError:
        return "refused"


check("an empty body is {}", body_of(b""), {})
check("an object is read", body_of(b'{"on": true}'), {"on": True})
check("JSON that does not parse is refused", body_of(b"{on: true"), "refused")
check("JSON that is not an object is refused", body_of(b"[1, 2]"), "refused")
check("a body that is not UTF-8 is refused", body_of(b"\xff\xfe"), "refused")


print("\nSETTINGS FILE")
folder = tempfile.mkdtemp(prefix="ac-settings-")
server._SETTINGS_FILE = os.path.join(folder, "settings.json")
server.update_settings(a=1)
server.update_settings(b=2)
check("changes merge", server.load_settings(), {"a": 1, "b": 2})
check("no temporary file is left behind", sorted(os.listdir(folder)), ["settings.json"])
with open(server._SETTINGS_FILE, "w", encoding="utf-8") as f:
    f.write('{"hotkeys": {"enabled": tr')
server.update_settings(x=1)
check("a fresh file is started", server.load_settings(), {"x": 1})
with open(server._SETTINGS_FILE + ".bad", encoding="utf-8") as f:
    check("and the one that would not parse is kept", f.read(), '{"hotkeys": {"enabled": tr')


print("\nCHAT ISOLATION")
writes = []
real = (server.rcp_transfer.set_output_routing, server.rcp_transfer.set_cross_point_excluded,
        server.time.sleep, server._active_matrix)


class _Matrix:
    all_sources = [{"index": 0}, {"index": 1}, {"index": 2}, {"index": 3}]


def excluded(out, src, on):
    writes.append((src, on))
    if len(writes) == 3:
        raise OSError("the unit stopped answering")


server.rcp_transfer.set_output_routing = lambda out, mode: writes.append(("mode", mode))
server.rcp_transfer.set_cross_point_excluded = excluded
server.time.sleep = lambda s: None
server._active_matrix = lambda: _Matrix()
try:
    iso = server._ChatIsolation(0)
    before = {0: False, 1: False, 2: False, 3: False}
    iso._snapshot = lambda: (2, dict(before), {0: {"level": 1.0}})
    try:
        with iso:
            pass
        raised = None
    except OSError as exc:
        raised = str(exc)
    check("the failed write is reported", raised, "the unit stopped answering")
    check("and what had changed is put back",
          writes[3:], [(0, False), (1, False), (2, False), (3, False)])
    check("and the restore is confirmed", iso.restored, True)
finally:
    (server.rcp_transfer.set_output_routing, server.rcp_transfer.set_cross_point_excluded,
     server.time.sleep, server._active_matrix) = real


print("\nANNOUNCER LOOPS")
opened = []
real = (server.rcp_transfer.find_control_interface, server._ann_seed_layout)
server.rcp_transfer.find_control_interface = lambda fresh=False: object()
server._ann_seed_layout = lambda st: None
try:
    newest = {"bank": 1}
    server._ann_live_state = newest
    server._ann_loop_gen = 5
    server._announce_on.set()
    server._announce_loop(4)
    check("a superseded loop leaves the newest loop's state alone",
          server._ann_live_state is newest, True)
finally:
    server._announce_on.clear()
    server._ann_live_state = None
    server.rcp_transfer.find_control_interface, server._ann_seed_layout = real

print()
if FAILS:
    print(f"{len(FAILS)} FAILED")
    for f in FAILS:
        print("  " + f)
    sys.exit(1)
print("all passed")
