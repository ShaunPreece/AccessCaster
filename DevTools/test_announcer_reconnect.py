"""Offline check: the announcer survives the desk re-enumerating under a new USB identity.

WHAT IT PINS. Writing `outputMultiMode`, `engineMode` or `systemMainMode` takes the desk off the
bus for about three seconds, and it comes back with a DIFFERENT product id and HID path, because
the product id encodes the audio configuration (0x0037 in mode 0, 0x0094 in mode 5, and so on).
A reader that remembers the interface it opened therefore retries a path that will never open
again, for as long as the app runs. Those three properties are ones the app itself writes, so the
failure lands on the user at the moment they change a USB setting - and an announcer that has
stopped speaking is indistinguishable from an app that has died.

So the loop must resolve the interface AGAIN on every reopen, and must say when it comes back. A
blip too short to have swallowed a press stays quiet, since a reconnect announcement is only useful
when there was a silence to explain.

Offline: the interface list, the handle and the speech are all stubbed, so no unit is needed and
no USB setting is touched.

    python DevTools/test_announcer_reconnect.py
"""
from __future__ import annotations
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import rcp_hid
import rcp_transfer
import server

FAILS = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILS.append(f"{name}: got {got!r}, wanted {want!r}")
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")


class FakeIface:
    """Just enough of rcp_hid.RodeInterface to be opened and logged."""

    def __init__(self, product_id, path):
        self.product_id = product_id
        self.path = path
        self.interface_number = 9


class FakeDev:
    """A handle that reads nothing until it is told the desk has gone, then fails like hidapi."""

    def __init__(self, iface):
        self.iface = iface
        self.dead = False

    def read(self, _len, timeout_ms=0):
        if self.dead:
            raise OSError("read error")
        time.sleep(0.005)
        return []


STANDARD = FakeIface(0x0037, b"path-mode-0")
EXPANDED = FakeIface(0x0094, b"path-mode-5")


class Rig:
    """Runs the real `_announce_loop` over a fake desk that can be re-enumerated under it."""

    def __init__(self, speak_after):
        self.iface = STANDARD
        self.opened = []          # every interface the loop opened, in order
        self.said = []
        self.seeded = 0
        self.dev = None
        self.speak_after = speak_after
        self.seeded_mode = None   # outputMultiMode a re-seed would read; None = unreadable
        self.tidied = []          # every _tidy_after_standard call, by its `spoken` argument
        self.followed = []        # every _virtual_devices_follow call, as (mode, arrived)

    # -- the stubs the loop calls --------------------------------------------
    def find(self, *a, **k):
        return self.iface

    def open(self, iface, purpose):
        if iface is not self.iface:
            # Exactly the shipped bug: a remembered path outlives the device it named.
            raise OSError("open failed")
        self.opened.append(iface)
        self.dev = FakeDev(iface)
        return self.dev

    def say(self, text):
        self.said.append(text)

    def seed(self, st):
        self.seeded += 1
        st["usb_mode"] = self.seeded_mode      # what a real dump would have found

    def tidy(self, spoken):
        self.tidied.append(spoken)
        return ""

    def follow(self, raw_mode, arrived):
        self.followed.append((raw_mode, arrived))
        return ""

    def re_enumerate(self, to_iface, gap):
        """Take the desk off the bus, wait, and bring it back as a different USB device."""
        self.dev.dead = True
        self.iface = None                      # nothing on the bus at all
        time.sleep(gap)
        self.iface = to_iface

    def __enter__(self):
        self.saved = {
            "find_t": rcp_transfer.find_control_interface,
            "open": rcp_hid.open_handle, "close": rcp_hid.close_handle,
            "say": server._ann_say, "seed": server._ann_seed_layout,
            "retry": server._ANN_RETRY_S, "speak": server._ANN_RECONNECT_SPEAK_AFTER_S,
            "tidy": server._tidy_after_standard, "follow": server._virtual_devices_follow,
        }
        rcp_transfer.find_control_interface = self.find
        rcp_hid.open_handle = self.open
        rcp_hid.close_handle = lambda dev, purpose="": None
        server._ann_say = self.say
        server._ann_seed_layout = self.seed
        # BOTH FOLLOW-UPS OF A RECONNECT ARE STUBBED: the real ones read the desk and move
        # Windows' default audio device, which is the user's screen reader.
        server._tidy_after_standard = self.tidy
        server._virtual_devices_follow = self.follow
        server._ANN_RETRY_S = 0.02             # the real 0.3 s only paces the retries
        server._ANN_RECONNECT_SPEAK_AFTER_S = self.speak_after
        server._announce_on.set()
        self.thread = threading.Thread(target=server._announce_loop, daemon=True)
        self.thread.start()
        time.sleep(0.15)                       # let it open and greet
        return self

    def __exit__(self, *exc):
        server._announce_on.clear()
        self.thread.join(timeout=3.0)
        rcp_transfer.find_control_interface = self.saved["find_t"]
        rcp_hid.open_handle = self.saved["open"]
        rcp_hid.close_handle = self.saved["close"]
        server._ann_say = self.saved["say"]
        server._ann_seed_layout = self.saved["seed"]
        server._tidy_after_standard = self.saved["tidy"]
        server._virtual_devices_follow = self.saved["follow"]
        server._ANN_RETRY_S = self.saved["retry"]
        server._ANN_RECONNECT_SPEAK_AFTER_S = self.saved["speak"]


print("\nTHE DESK RETURNS AS A DIFFERENT USB DEVICE")
with Rig(speak_after=0.05) as rig:
    check("it opens the desk that is actually there", rig.opened, [STANDARD])
    check("and greets once", rig.said, ["Control announcer is on."])
    rig.re_enumerate(EXPANDED, gap=0.3)
    time.sleep(0.5)
    check("it reopens on the NEW interface, not the remembered one",
          rig.opened[-1], EXPANDED)
    check("exactly two opens: it does not thrash", len(rig.opened), 2)
    check("it says it is back, because silence cannot be interpreted",
          rig.said[-1], "Control announcer reconnected.")
    check("it does not greet a second time, which would read as a restart",
          rig.said.count("Control announcer is on."), 1)
    check("and it re-seeds, since mutes and levels can move while it is deaf",
          rig.seeded, 2)

print("\nA BLIP TOO SHORT TO HAVE LOST ANYTHING STAYS QUIET")
with Rig(speak_after=5.0) as rig:
    rig.re_enumerate(EXPANDED, gap=0.05)
    time.sleep(0.4)
    check("it still reconnects", rig.opened[-1], EXPANDED)
    check("but says nothing about it", rig.said, ["Control announcer is on."])
    check("and does not pay for a re-seed", rig.seeded, 1)

print("\nA DESK THAT NEVER COMES BACK DOES NOT SPIN")
with Rig(speak_after=0.05) as rig:
    rig.dev.dead = True
    rig.iface = None
    time.sleep(0.4)
    check("no handle is opened while nothing is on the bus", len(rig.opened), 1)
    check("and it is still running, ready for a replug", rig.thread.is_alive(), True)

print("\nA USB MODE THAT MOVED WHILE WE WERE DEAF IS SAID, NOT SWALLOWED")
# The desk can drop out of Expanded on its own, and the change is otherwise invisible: the push
# announcing it is sent while the desk is off the bus, so only a comparison across the reconnect
# can see it. 5 is post-fader multitrack plus Expanded, 2 is the same output mode without it.
# Leaving Expanded says only that: the faders it empties are said by _tidy_after_standard, which
# the reconnect starts on its own thread because it writes.
check("leaving Expanded says so", server._ann_usb_mode_change(5, 2),
      "The unit has left Expanded USB input.")
check("entering it says so", server._ann_usb_mode_change(2, 5), "USB input is now Expanded.")
check("an output-mode change names the new mode",
      server._ann_usb_mode_change(0, 2),
      "USB output to this computer is now every channel, post-fader.")
check("an unchanged mode says nothing, so a plain reconnect is not narrated",
      server._ann_usb_mode_change(5, 5), "")
check("and an unreadable mode never speaks a guess", server._ann_usb_mode_change(None, 2), "")

rig = Rig(speak_after=0.05)
rig.seeded_mode = 5                       # the desk was in Expanded when the announcer started
with rig:
    rig.seeded_mode = 2                   # ...and comes back out of it, as one did unprompted
    rig.re_enumerate(EXPANDED, gap=0.3)
    time.sleep(0.5)
    check("the reconnect reports the change instead of just saying it is back",
          rig.said[-1], "The unit has left Expanded USB input.")
    check("and starts the spoken fader tidy", rig.tidied, [True])
    check("and the virtual-devices follow-up, as an arrival", rig.followed, [(2, True)])

print("\nTHE SHIPPED CONSTANTS ARE SANE")
check("a reconnect is only spoken after a real gap",
      server._ANN_RECONNECT_SPEAK_AFTER_S > 0, True)
check("retries back off, or an absent desk is re-enumerated forever at full speed",
      server._ANN_RETRY_SLOW_S > server._ANN_RETRY_S, True)

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILURES'}")
for f in FAILS:
    print("  " + f)
raise SystemExit(1 if FAILS else 0)
