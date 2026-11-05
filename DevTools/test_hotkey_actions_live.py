"""Run each hotkey ACTION against the real desk and print the sentence it would speak.

The Win32 half is covered by DevTools/test_hotkeys.py (no desk needed). This is the other half: the
actions themselves, which compose the app's own writes and then have to describe what happened in
one spoken line. A wrong field name here does not crash anything - it comes out of the speakers as a
Python error - so it is worth reading the sentences once.

EXCLUSIVE: stop the running service first (this opens the same USB device). The announcer and
cough keys are each pressed twice, the second press made however the first part ends, so neither is
left changed. The recording test leaves a throwaway take to delete from the Recording page.

Run:  python DevTools/test_hotkey_actions_live.py [--record]
"""

import sys
import time

import _bootstrap  # noqa: F401  - puts Project/ on sys.path
import commands
import desk_access
import recording
import speech


def main():
    do_record = "--record" in sys.argv

    print("--- say what the desk is doing ---")
    print(repr(commands._say_state()))

    print("\n--- announcements on/off (twice, so it ends as it started) ---")
    was = speech.enabled.is_set()
    try:
        print(repr(commands._toggle_announcer()))
    finally:
        print(repr(commands._toggle_announcer()))
    print(f"announcer back where it started: {speech.enabled.is_set() == was}")

    print("\n--- the cough key (twice: mute what is open, then open exactly those) ---")
    before = _mic_mutes()
    print("before:", before)
    print(repr(commands._cough_key()))
    try:
        print("after press 1:", _mic_mutes())
    finally:
        # The second press opens exactly the mics the first one muted, which commands holds.
        if commands._muted_mics:
            print(repr(commands._cough_key()))
    after = _mic_mutes()
    print("after press 2:", after)
    print(f"round trip clean: {before == after}")

    if do_record:
        print("\n--- start and stop a recording ---")
        recording.watcher()
        time.sleep(2.0)                       # let the watcher hear the current state
        print(repr(commands.run("rec.toggle")))
        time.sleep(4.0)
        print(repr(commands.run("rec.toggle")))
        print("delete the take from the Recording page: it is a throwaway.")
    else:
        print("\n(recording not tested; pass --record to include it)")
    return 0


def _mic_mutes() -> dict:
    import channels
    import rcp_read
    with desk_access.hid_lock:
        blob = rcp_read.read_model_stable()
    mutes = [v for v, _k in rcp_read.find_all(blob, "channelOutputMute")]
    out = {}
    for c in channels.active_ui_channels():
        if c.get("kind") != "mic":
            continue
        strip = channels.strip_on_fader(c["name"])
        if strip is not None:
            out[c["name"]] = bool(mutes[strip]) if strip < len(mutes) else None
    return out


if __name__ == "__main__":
    raise SystemExit(main())
