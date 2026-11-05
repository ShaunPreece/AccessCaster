"""
AccessCaster's composition root: what runs whether or not the window is open, brought up and let go.

`start` joins the listeners and watchers to the code that acts on them, and `stop` lets them go.
`tray.py` calls both, and a DevTools run calls `start` through `_desk.service`. Nothing imports this
module but those two: the features never reach up into it.
"""

from __future__ import annotations

import threading

import announcer
import app_updates
import callme_actions
import channels
import clip_warning
import desk_access
import desk_live
import desk_model
import diagnostics
import encoder_hold
import encoder_home
import faders
import mute_solo
import pad_actions
import pad_shortcuts
import rcp_read
import recording
import rode_app
import rode_follow
import shortcuts
import speech
import usb_audio
from version import __version__ as APP_VERSION


class _AnnouncerHooks(announcer.Hooks):
    """The app's side of the announcer's interface: the device lock, the shortcut layer's mixer
    pads, and what a reconnect means for the USB mode and RODE's virtual devices. Each looks its
    state up when called."""

    lock = desk_access.hid_lock

    def mixer_positions(self):
        return pad_shortcuts.mixer_positions

    def reconnected(self, was_usb, now_usb):
        usb_audio.reconnected(was_usb, now_usb)


def install_announcer_hooks() -> None:
    """Join the announcer to the app. `start` does it first; a DevTools test that runs the
    announcer without the rest of the service calls it alone."""
    announcer.hooks = _AnnouncerHooks()


def read_desk_once() -> None:
    """The first read of the desk, adopted by the desk model, so every page and listener starts
    from the desk as it is. Raises when the desk cannot be read."""
    with desk_access.hid_lock:
        rcp_read.read_model_stable()


def start(announce=True, register_shortcuts=True, greeting=announcer.GREETING_STARTED) -> None:
    """Bring up what runs whether or not the window is open. Returns once the desk has been read
    once, so the window's first pages show real state.

      - announce:           turn the control announcer on at startup, and the clip warning with it.
      - greeting:           what the announcer says once the desk is open; empty for nothing.
      - register_shortcuts: register the global keyboard shortcuts. A DevTools run passes False:
                            a tooling run must never take keys away from the whole machine.
    """
    # THE ONE PLACE THE DIAGNOSTIC LOG IS STARTED, because every entry point comes through here.
    diagnostics.start("AccessCaster starting", version=APP_VERSION, announcer=announce,
                      shortcuts=register_shortcuts)
    install_announcer_hooks()
    # The one listener, and what follows it whatever else is running: the naming layout, and the
    # pad bank and playback the shortcuts fire by.
    desk_live.start()
    channels.follow_desk()
    pad_shortcuts.follow_desk()
    rode_app.on_look = rode_follow.follow
    rode_app.start()
    if announce and announcer.starts_on():
        # App settings can still turn it off.
        announcer.action_set_announce(True, chosen=False, greeting=greeting)
    if announce:
        clip_warning.start_clip_warning()
    # SEED THE EXPANDED FLAG BEFORE ANYTHING ASKS FOR IT. It is a published cache, and unpublished
    # it answers False: the fader list then omits Game, Music, A and B and the write gate refuses
    # them, so on a desk that IS in Expanded the four cannot be assigned until something reads the
    # routing. The announcer seeds it too, but only while announcements are ON. Skipped when
    # something has already published, so this costs one dump at most.
    threading.Thread(target=usb_audio.seed_usb_expanded, daemon=True, name="usb-mode-seed").start()
    usb_audio.start_virtual_devices()
    if register_shortcuts:
        shortcuts.start()
    # The recording state follower: it is what makes the Recording page free to poll, and it is
    # how a press of the unit's own REC button is noticed. Started on first use, so this starts it.
    recording.watcher()
    # A hold on the encoder while a channel button holds it mutes that channel, as the desk's own
    # hold does for the encoder's ring target.
    encoder_hold.Watcher(mute_solo.encoder_hold_mute).start()
    # A turn that moves nothing, with no channel selected, puts the encoder back on the virtual
    # faders and monitor.
    encoder_home.Watcher(faders.action_reset_encoder).start()
    # AccessCaster's own update: says what the last one did, then checks now and then. Silent
    # unless a version is found, and never from a source run.
    app_updates.start()
    # The CallMe listener: a call rings whatever page is open, and it has to be heard whether or
    # not anyone is announcing. It seeds itself from a dump whenever the desk is opened.
    callme_actions.watcher()
    try:
        read_desk_once()
        midi = desk_model.first("systemMidiControl", "bool")
        print(f"Read the desk (MIDI {'on' if midi else 'off'}, "
              f"firmware {desk_model.firmware()}).")
    except Exception as exc:
        print(f"Startup read skipped: {exc}")


def stop(reason: str) -> None:
    """What the app lets go of on the way out. Safe to call more than once.

    The endpoint watcher and the global shortcuts go first. Then any Hold-mode pad a shortcut is holding, which would
    otherwise keep playing with nothing left to release it, and any live preview still engaged on
    a mic. The speech output closes, since a device left open holds the process's exit up, and the
    desk listener closes its handle on its own thread."""
    diagnostics.event("app.stopping", reason)
    usb_audio.stop_virtual_devices()
    shortcuts.stop()
    pad_shortcuts.release_held()
    pad_actions.cleanup_preview_on_exit()
    speech.stop()
    rode_app.stop()
    desk_live.stop()
    diagnostics.stop(reason)
