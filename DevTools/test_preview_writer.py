"""The live preview's writes (soundpad.preview_write, pad_actions._PreviewWriter), offline: edits made
close together share one transfer-mode session, the newest value per setting wins, the pad is
found from one model read, an edit for a pad no longer named Preview is refused before anything is
sent, and a failed write is reported by the next change. The desk is replaced: nothing is sent.

Run: python DevTools/test_preview_writer.py
"""
from __future__ import annotations

import contextlib
import sys
import threading
import time
from types import SimpleNamespace

import _bootstrap  # noqa: F401  (puts Project/ on sys.path)
import _testkit
import devices
import pad_actions
import soundpad
from _testkit import as_device, check, check_true, patched

reads, sessions, sent = [], [], []


@contextlib.contextmanager
def session(_settle):
    sessions.append("open")
    yield
    sessions.append("closed")


def nodes(name="Preview"):
    return [SimpleNamespace(idx=16, position=24, name=name, type=2),
            SimpleNamespace(idx=0, position=0, name="Applause", type=1)]


def fakes(name="Preview"):
    return ((soundpad, {"pad_nodes": lambda blob=None: nodes(name),
                        "effect_objects": lambda blob=None: [SimpleNamespace(index=7,
                                                                             effects_idx=16)],
                        "_transfer_mode": session}),
            (soundpad.rr, {"read_model": lambda *a, **k: reads.append(1) or b""}),
            (soundpad.t, {"send_records_paced": lambda recs, delay=0: sent.append(len(recs))}),
            (soundpad.time, {"sleep": lambda _s: None}))


with as_device(devices.PRO2):
    with patched(*fakes()):
        batches = [{(17, "reverbMix"): (0.9, "float")}, {}]
        soundpad.preview_write({(17, "reverbOn"): (True, "bool"), (17, "reverbMix"): (0.5, "float")},
                               more=lambda: batches.pop(0))
    check("one model read finds the pad", len(reads), 1)
    check("everything goes in one session", sessions, ["open", "closed"])
    check("each batch re-asserts the pad and carries its edits", sent, [3, 2])

    reads.clear(), sessions.clear(), sent.clear()
    with patched(*fakes(name="Applause")):
        try:
            soundpad.preview_write({(17, "reverbOn"): (True, "bool")}, more=dict)
            refused = False
        except ValueError:
            refused = True
    check_true("a pad no longer named Preview is refused before anything is sent",
               refused and not sessions and not sent)
    with patched(*fakes(name="Robot")):
        soundpad.preview_update(17, "echoMix", 0.4)
    check("an in-place edit of any pad still writes", sent, [2])

    # ---- the writer: batching, newest wins, failures reported --------------------------------
    batches_seen = []
    gate = threading.Event()

    def preview_write(edits, more):
        gate.wait(2)                     # the first session is still opening
        batches_seen.append(dict(edits))
        while True:
            edits = more()
            if not edits:
                return
            batches_seen.append(dict(edits))

    with patched((soundpad, {"preview_write": preview_write}),
                 (pad_actions._PreviewWriter, {"IDLE_S": 0.2})):
        writer = pad_actions._PreviewWriter()
        writer.submit(17, "reverbOn", True, "bool")
        time.sleep(0.1)
        writer.submit(17, "reverbMix", 0.6, "float")
        writer.submit(17, "reverbMix", 0.8, "float")
        writer.submit(17, "echoOn", True, "bool")
        gate.set()
        time.sleep(0.6)
        check("edits made while a session runs join it, the newest value winning", batches_seen,
              [{(17, "reverbOn"): (True, "bool")},
               {(17, "reverbMix"): (0.8, "float"), (17, "echoOn"): (True, "bool")}])
        writer.forget(17)

    def failing(edits, more):
        raise ValueError("The preview pad is no longer there.")

    with patched((soundpad, {"preview_write": failing})):
        writer = pad_actions._PreviewWriter()
        check("a change is accepted at once", writer.submit(17, "echoMix", 0.5, "float"),
              {"ok": True})
        time.sleep(0.3)
        reply = writer.submit(17, "echoMix", 0.6, "float")
        check_true("and a failed write is reported by the next one",
                   not reply["ok"] and "no longer there" in reply["message"])
        # That submit's own write fails too. Wait for it inside the fake: left to run after the
        # patch is undone, the thread calls the real writer, which opens the desk during shutdown.
        deadline = time.monotonic() + 2
        while writer._failure is None and time.monotonic() < deadline:
            time.sleep(0.01)

    writer = pad_actions._PreviewWriter()
    writer._pending = {(17, "echoMix"): (0.5, "float"), (5, "robotMix"): (0.2, "float")}
    writer.forget(17)
    check("an ended preview's waiting edits are dropped", list(writer._pending), [(5, "robotMix")])

sys.exit(_testkit.finish())
