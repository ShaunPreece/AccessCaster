"""TEST: inject physical-button events from the host.

The device reports button presses as report-04 named properties. We replay them as
report-03 (host->device) to emulate the press:
  mute : 'cmutePressed'  compact-bool  (down=02, up=03) -> device toggles Mic mute
  solo : 'csoloPressed'  compact-bool  (down=02, up=03) -> device toggles channel cue
  bank : 'selectedBank'  global int selector 0x07       -> sets the pad bank

Usage:
  python inject_button.py mute
  python inject_button.py solo
  python inject_button.py bank <n>
"""
import sys
import time

import hid
import _bootstrap  # noqa: F401  - puts Project/ on sys.path
import rcp_hid          # write_report: bumps the model-read cache so read-back is honest
import rcp_read as rr
import rcp_transfer as t


def rec(payload: bytes) -> bytes:
    r = bytes([t.REPORT_BULK_OUT]) + len(payload).to_bytes(4, "little") + payload
    return r + bytes(t.BULK_LEN - len(r))


def compact_press(name: str, flag: int) -> bytes:
    # captured prefix for the press events: 01 01 02 00 01 <name> 00 01 01 <flag>
    return bytes([0x01, 0x01, 0x02, 0x00, 0x01]) + name.encode() + b"\x00" + bytes([0x01, 0x01, flag])


def write_record(record: bytes) -> int:
    iface = t.find_control_interface()
    dev = hid.device(); dev.open_path(iface.path)
    try:
        return rcp_hid.write_report(dev, record)
    finally:
        dev.close()


def send_payload(payload: bytes) -> int:
    return write_record(rec(payload))


def press(name: str):
    send_payload(compact_press(name, 0x02)); time.sleep(0.12)   # down
    send_payload(compact_press(name, 0x03)); time.sleep(0.12)   # up


cmd = sys.argv[1] if len(sys.argv) > 1 else "mute"

if cmd == "mute":
    print("injecting cmutePressed (down+up) ...")
    press("cmutePressed")
    print("sent. ASK USER: did the mic mute or unmute?")
elif cmd == "solo":
    before = [v for v, k in rr.find_all(rr.read_model_stable(), "channelCueEnable")]
    print(f"channelCueEnable before: {before[:4]}")
    press("csoloPressed")
    time.sleep(0.3)
    after = [v for v, k in rr.find_all(rr.read_model_stable(), "channelCueEnable")]
    print(f"channelCueEnable after:  {after[:4]}")
    print("CHANGED" if before != after else "no change")
elif cmd == "bank":
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    before = [v for v, k in rr.find_all(rr.read_model_stable(), "selectedBank")]
    print(f"selectedBank before: {before}")
    write_record(t.build_global_prop_int("selectedBank", 0x07, n))
    time.sleep(0.3)
    after = [v for v, k in rr.find_all(rr.read_model_stable(), "selectedBank")]
    print(f"selectedBank after:  {after}  (asked for {n})")
    print("CHANGED" if before != after else "no change")
