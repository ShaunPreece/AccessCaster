"""Offline checks on the server's guards: what it serves, what it writes, and what it puts back.

THE RULES THIS PINS:
  * /static/ serves only files inside static/, so an absolute or climbing path is refused;
  * a request body that is not a JSON object is refused rather than read as {};
  * settings.json is replaced whole, and one that will not parse is kept as settings.json.bad;
  * a saved presets file that will not parse is kept as .bad, so the next save cannot replace it;
  * a fader level write names its channel from the layout held, and reads the desk only under the
    HID lock;
  * the level watch, with no stream chosen, takes a capture stream rather than playback;
  * however the app stops, a Hold-mode pad a shortcut is holding is released;
  * a Chat isolation whose setup fails part way puts back what it had already changed;
  * auto gain stopped before anything was said reports the stop, not a silent microphone;
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
import _testkit
import announcer
import server
import settings_file
import speech
from _testkit import as_device, check, patched

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
# Held for the whole run: the folder is removed when this object goes, at exit however it ends.
settings_dir = tempfile.TemporaryDirectory(prefix="ac-settings-", ignore_cleanup_errors=True)
folder = settings_dir.name
settings_file.FILE = os.path.join(folder, "settings.json")
settings_file.update(a=1)
settings_file.update(b=2)
check("changes merge", settings_file.load(), {"a": 1, "b": 2})
check("no temporary file is left behind", sorted(os.listdir(folder)), ["settings.json"])
with open(settings_file.FILE, "w", encoding="utf-8") as f:
    f.write('{"hotkeys": {"enabled": tr')
settings_file.update(x=1)
check("a fresh file is started", settings_file.load(), {"x": 1})
with open(settings_file.FILE + ".bad", encoding="utf-8") as f:
    check("and the one that would not parse is kept", f.read(), '{"hotkeys": {"enabled": tr')


print("\nSAVED PRESETS")
server._USER_PRESET_FILE = os.path.join(folder, "presets.json")
with open(server._USER_PRESET_FILE, "w", encoding="utf-8") as f:
    f.write('{"Morning": {"eqLowGain": 0.5}, "Ev')
check("a file that will not parse reads as no presets", server._load_user_presets(), {})
with open(server._USER_PRESET_FILE + ".bad", encoding="utf-8") as f:
    check("and is kept as .bad, so a save cannot overwrite it", f.read(),
          '{"Morning": {"eqLowGain": 0.5}, "Ev')
with open(server._USER_PRESET_FILE, "w", encoding="utf-8") as f:
    f.write("[1, 2]")
check("a file holding a list reads as no presets", server._load_user_presets(), {})


print("\nFADER LEVEL")
reads = []
with as_device(server.devices.PRO2), patched(
        (server.rcp_read, {"read_model":
                           lambda *a, **k: reads.append(server._hid_lock.locked()) or b""}),
        (server.rcp_transfer, {"set_fader_level": lambda sel, value: None}),
        (announcer, {"publish_level": lambda *a: None})):
    server.channels.publish_strip_vector([0, 1, 2, 3, 4, 5, 6, 7, 8])
    res = server.action_set_fader_level(6, 50)
    check("the level is set", res.get("ok"), True)
    check("with a layout held, the reply names the channel without a read", reads, [])
    server.channels._strip_vec["vec"] = None
    server.channels._strip_vec_last["vec"] = None
    server.action_set_fader_level(6, 40)
    check("with none held, the read is made under the HID lock", reads, [True])


print("\nLEVEL WATCH")
built = []


class _Watcher:
    def __init__(self, stream, *a, **k):
        built.append(stream)
        self.name, self.running, self.error = stream, True, None

    def start(self):
        return True

    def stop(self):
        self.running = False


real_prefs = server._meter_prefs
with patched(
        (server.meters, {
            "desk_streams": lambda: {"ok": True, "streams": [
                {"id": "out", "name": "Speakers (RODECaster Main Stereo)", "flow": "playback"},
                {"id": "in", "name": "Microphone (RODECaster Main Stereo)", "flow": "recording"}]},
            "default_recording_name": lambda: "",
            "Watcher": _Watcher}),
        (server, {"_meter_prefs": lambda: {**real_prefs(), "stream": ""}})):
    server.action_meter_watch(True)
    check("with no stream chosen, the watch takes the capture stream", built, ["in"])
    server.action_meter_watch(False)


print("\nSHUTTING DOWN")
released = []
with patched((server.midi_out, {"release_pad": released.append, "close": lambda: None})):
    server._hk_pads_held.add(2)            # a Hold-mode pad a shortcut is holding: pad 3 of bank 1
    server._shut_down("test")
    check("a held pad is released however the app stops", released, [3])
    check("and is no longer held", server._hk_pads_held, set())


print("\nCHAT ISOLATION")
writes = []


class _Matrix:
    all_sources = [{"index": 0}, {"index": 1}, {"index": 2}, {"index": 3}]


def excluded(out, src, on):
    writes.append((src, on))
    if len(writes) == 3:
        raise OSError("the unit stopped answering")


with patched(
        (server.rcp_transfer, {"set_output_routing": lambda out, mode: writes.append(("mode", mode)),
                               "set_cross_point_excluded": excluded}),
        (server.time, {"sleep": lambda s: None}),
        (server, {"_active_matrix": lambda: _Matrix()})):
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


print("\nAUTO GAIN STOPPED BEFORE ANYTHING WAS SAID")


class _Isolation:
    level, restored = 1.0, True

    def __init__(self, keep):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def stopped_silent_take(endpoint_id, seconds, should_stop=None):
    server._autogain_set(cancel=True)          # Stop pressed during the take
    return {"seconds": 0.0, "samples": 0, "loud": 0.0, "loud_db": -120.0, "max": 0.0,
            "max_db": -120.0, "muted": False, "ceiling": 1.0}


server._autogain = {"phase": "starting", "channel": "Mic1", "done": False, "cancel": False}
with patched(
        (server, {"_read_gain_and_processing": lambda mic: (30, False),
                  "_chat_endpoint_to_unity": lambda endpoint_id: {},
                  "_ChatIsolation": _Isolation}),
        (server.autogain, {"chat_endpoint": lambda: {"id": "chat"},
                           "measure_endpoint": stopped_silent_take}),
        (server.meters, {"cue": lambda name: None})):
    server._autogain_run("Mic1", 1, 5.0)
gain_result = server._autogain_state()
check("a take stopped before anything was said changes nothing", gain_result.get("phase"), "failed")
check("and says it was stopped, not that the microphone may be faulty",
      gain_result.get("message"), "Stopped. The gain was not changed.")
server._autogain = None


print("\nANNOUNCER LOOPS")
with patched((server.rcp_transfer, {"find_control_interface": lambda fresh=False: object()}),
             (announcer, {"seed_layout": lambda st: None})):
    try:
        newest = {"bank": 1}
        announcer.live_state = newest
        announcer._loop_gen = 5
        speech.enabled.set()
        announcer._loop(4)
        check("a superseded loop leaves the newest loop's state alone",
              announcer.live_state is newest, True)
    finally:
        speech.enabled.clear()
        announcer.live_state = None

sys.exit(_testkit.finish())
