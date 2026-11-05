"""Offline checks on the service's guards: what it writes, and what it puts back.

THE RULES THIS PINS:
  * settings.json is replaced whole, and one that will not parse is kept as settings.json.bad;
  * a saved presets file that will not parse is kept as .bad, so the next save cannot replace it;
  * a fader level write names its channel from the layout held, and reads the desk only under the
    HID lock;
  * however the app stops, a Hold-mode pad a shortcut is holding is released;
  * a Chat isolation whose setup fails part way puts back what it had already changed;
  * auto gain stopped before anything was said reports the stop, not a silent microphone;
  * an announcer loop that is no longer the newest leaves without touching the live state.

No device and no speech: the desk calls are stubbed.

    python DevTools/test_server_safety.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401
import _testkit
import announcer
import desk_access
import desk_model
import faders
import mic_presets
import output_mixes
import pad_shortcuts
import server
import service
import settings_file
import speech
from _testkit import as_device, check, patched

print("SETTINGS FILE")
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
mic_presets._USER_PRESET_FILE = os.path.join(folder, "presets.json")
with open(mic_presets._USER_PRESET_FILE, "w", encoding="utf-8") as f:
    f.write('{"Morning": {"eqLowGain": 0.5}, "Ev')
check("a file that will not parse reads as no presets", mic_presets._load_user_presets(), {})
with open(mic_presets._USER_PRESET_FILE + ".bad", encoding="utf-8") as f:
    check("and is kept as .bad, so a save cannot overwrite it", f.read(),
          '{"Morning": {"eqLowGain": 0.5}, "Ev')
with open(mic_presets._USER_PRESET_FILE, "w", encoding="utf-8") as f:
    f.write("[1, 2]")
check("a file holding a list reads as no presets", mic_presets._load_user_presets(), {})


print("\nFADER LEVEL")
reads = []
with as_device(server.devices.PRO2), patched(
        (server.rcp_read, {"read_model":
                           lambda *a, **k: reads.append(desk_access.hid_lock.locked()) or b""}),
        (server.rcp_transfer, {"set_fader_level": lambda sel, value: None}),
        (announcer, {"mark_self_level": lambda *a: None})):
    _testkit.hold_desk([0, 1, 2, 3, 4, 5, 6, 7, 8])
    res = faders.action_set_fader_level(6, 50)
    check("the level is set", res.get("ok"), True)
    check("with a layout held, the reply names the channel without a read", reads, [])
    desk_model._reset()
    faders.action_set_fader_level(6, 40)
    check("with none held, the read is made under the HID lock", reads, [True])


print("\nSHUTTING DOWN")
released = []
with patched((pad_shortcuts.midi_out, {"release_pad": released.append, "close": lambda: None})):
    pad_shortcuts._pads_held.add(2)            # a Hold-mode pad a shortcut is holding: pad 3 of bank 1
    service.stop("test")
    check("a held pad is released however the app stops", released, [3])
    check("and is no longer held", pad_shortcuts._pads_held, set())


print("\nCHAT ISOLATION")
writes = []


class _Matrix:
    all_sources = [{"index": 0}, {"index": 1}, {"index": 2}, {"index": 3}]


def excluded(out, src, on):
    writes.append((src, on))
    if len(writes) == 3:
        raise OSError("the desk stopped answering")


with patched(
        (server.rcp_transfer, {"set_output_routing": lambda out, mode: writes.append(("mode", mode)),
                               "set_cross_point_excluded": excluded}),
        (server.time, {"sleep": lambda s: None}),
        (output_mixes, {"active_matrix": lambda: _Matrix()})):
    iso = server._ChatIsolation(0)
    before = {0: False, 1: False, 2: False, 3: False}
    iso._snapshot = lambda: (2, dict(before), {0: {"level": 1.0}})
    try:
        with iso:
            pass
        raised = None
    except OSError as exc:
        raised = str(exc)
    check("the failed write is reported", raised, "the desk stopped answering")
    check("and what had changed is put back",
          writes[3:], [(0, False), (1, False), (2, False), (3, False)])
    check("and the restore is confirmed", iso.restored, True)


print("\nAUTO GAIN CANCELLED")


class _Isolation:
    level, restored = 1.0, True

    def __init__(self, keep):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def stopped_silent_take(endpoint_id, seconds, should_stop=None):
    server._autogain_set(cancel=True)          # Cancel pressed during the take
    return {"seconds": 0.0, "samples": 0, "loud": 0.0, "loud_db": -120.0, "max": 0.0,
            "max_db": -120.0, "muted": False, "ceiling": 1.0}


def quiet_take(endpoint_id, seconds, should_stop=None):
    """Talking, but 20 dB under target: enough for a verdict that raises the gain."""
    return {"seconds": seconds, "samples": int(seconds * 20), "loud": 0.03, "loud_db": -30.0,
            "max": 0.06, "max_db": -24.0, "muted": False, "ceiling": 1.0}


def autogain_run(takes):
    """Run auto gain on a fake Mic 1 at 30 dB, the takes answered in turn by `takes`, and return
    its end state and every gain written."""
    writes = []
    answers = iter(takes)
    server._autogain = {"phase": "starting", "channel": "Mic1", "done": False, "cancel": False}
    with patched(
            (server, {"_read_gain_and_processing": lambda mic: (30, False),
                      "_chat_endpoint_to_unity": lambda endpoint_id: {},
                      "_ChatIsolation": _Isolation, "_AUTOGAIN_LEAD_S": 0.0,
                      "_AUTOGAIN_LEAD_PER_WORD_S": 0.0,
                      "_autogain_write_gain": lambda mic, db: writes.append(db)}),
            (server.autogain, {"chat_endpoint": lambda: {"id": "chat"},
                               "measure_endpoint": lambda *a, **k: next(answers)(*a, **k)}),
            (server.meters, {"cue": lambda name: None})):
        server._autogain_run("Mic1", 1, 5.0)
    state = server._autogain_state()
    server._autogain = None
    return state, writes


gain_result, writes = autogain_run([stopped_silent_take])
check("a take cancelled before anything was said changes nothing", gain_result.get("phase"),
      "failed")
check("and says it was cancelled, not that the microphone may be faulty",
      gain_result.get("message"), "Cancelled. The gain was not changed.")
check("and writes no gain", writes, [])

gain_result, writes = autogain_run([quiet_take, stopped_silent_take])
check("a cancel during the check take ends the run as failed", gain_result.get("phase"), "failed")
check("and puts back the gain the first take set",
      (len(writes), writes[-1] if writes else None), (2, 30))


print("\nANNOUNCER LOOPS")
with patched((server.rcp_transfer, {"find_control_interface": lambda fresh=False: object()}),
             (announcer, {"seed": lambda st: None})):
    try:
        newest = {"pending": None}
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
