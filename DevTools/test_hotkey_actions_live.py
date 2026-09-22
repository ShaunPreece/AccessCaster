"""Run each hotkey ACTION against the real desk and print the sentence it would speak.

The Win32 half is covered by DevTools/test_hotkeys.py (no desk needed). This is the other half: the
actions themselves, which compose the app's own writes and then have to describe what happened in
one spoken line. A wrong field name here does not crash anything - it comes out of the speakers as a
Python error - so it is worth reading the sentences once.

EXCLUSIVE: stop the running service first (this opens the same USB device). Nothing here is left
changed: the cough key is pressed twice, and the recording test deletes its own take.

Run:  python DevTools/test_hotkey_actions_live.py [--record]
"""

import sys
import time

import _bootstrap  # noqa: F401  - puts Project/ on sys.path

import server


def main():
    do_record = "--record" in sys.argv

    print("--- say what the unit is doing ---")
    print(repr(server._hk_state()))

    print("\n--- announcements on/off (twice, so it ends as it started) ---")
    was = server._announce_on.is_set()
    print(repr(server._hk_announcer()))
    print(repr(server._hk_announcer()))
    print(f"announcer back where it started: {server._announce_on.is_set() == was}")

    print("\n--- the cough key (twice: mute what is open, then open exactly those) ---")
    before = _mic_mutes()
    print("before:", before)
    print(repr(server._hk_mics()))
    print("after press 1:", _mic_mutes())
    print(repr(server._hk_mics()))
    after = _mic_mutes()
    print("after press 2:", after)
    print(f"round trip clean: {before == after}")

    if do_record:
        print("\n--- start and stop a recording ---")
        server._recorder_watcher()
        time.sleep(2.0)                       # let the watcher hear the current state
        print(repr(server._hk_record()))
        time.sleep(4.0)
        print(repr(server._hk_record()))
        print("delete the take from the Recording page: it is a throwaway.")
    else:
        print("\n(recording not tested; pass --record to include it)")
    return 0


def _mic_mutes() -> dict:
    import channels
    import rcp_read
    with server._hid_lock:
        blob = rcp_read.read_model_stable()
    mutes = [v for v, _k in rcp_read.find_all(blob, "channelOutputMute")]
    out = {}
    for c in channels.active_ui_channels():
        if c.get("kind") != "mic":
            continue
        strip = channels.channel_strip_index(c["name"])
        out[c["name"]] = bool(mutes[strip]) if strip < len(mutes) else None
    return out


if __name__ == "__main__":
    raise SystemExit(main())
