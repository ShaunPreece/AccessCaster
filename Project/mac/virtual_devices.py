"""macOS's `virtual_devices`: RODE's Virtual Devices app, which is installed and running or not,
and nothing more this app can see or change.

What RODE ship for the Mac (2.0.0 beta) is not a driver and gives the system no devices of its own,
unlike the Windows driver's Fader and System devices, so the endpoint half of the contract has
nothing to act on, and the app's binding to the desk cannot be read (`SEES_BINDING`):
  - `/Applications/RODE Virtual Devices.app`, bundle `com.rode.RODEVirtualChannels`, a menu bar
    app (JUCE) started at sign-in by `/Library/LaunchAgents/com.rode.RODEVirtualChannels.plist`,
    which sends its output to /tmp/RODEVirtualDevices.out and .err. It needs macOS 14.2.
  - It captures the audio of apps the user picks with ScreenCaptureKit (so it asks for screen
    recording) and plays each into one of four slots, Game, Music, A and B, on the desk's
    "RODECaster Pro II Main Multitrack" or "RODECaster Duo Main Multitrack" device, which the
    desk presents in Expanded mode. Slot assignments are kept per desk serial number.
  - Its installer removes the 1.x HAL plug-in, which did create devices:
    /Library/Audio/Plug-Ins/HAL/RodeVirtualChannels.driver.
Unknown without a desk: whether its window and menu are usable with VoiceOver, and where it
stores the slot assignments.
"""
import os
import plistlib
import threading

from mac.rode_app import running as _bundle_running

APP = "/Applications/RODE Virtual Devices.app"
SEES_BINDING = False


def driver_state() -> dict:
    if not os.path.isdir(APP):
        return {"installed": False}
    try:
        with open(os.path.join(APP, "Contents", "Info.plist"), "rb") as file:
            version = str(plistlib.load(file).get("CFBundleShortVersionString", ""))
    except (OSError, plistlib.InvalidFileException):
        version = ""
    try:
        running = _bundle_running(os.path.basename(APP))
    except OSError:
        running = False
    return {"installed": True, "running": running, "driver_version": version}


def service_state() -> dict:
    return {}


def sync_endpoints(status, expect_connected, move_default: bool, parked: str) -> dict:
    return {}


def park_default():
    return None


def hold_default(endpoint_id: str) -> bool:
    return False


def default_to_system(timeout_s: float) -> str:
    return ""


def virtual_endpoint_counts() -> tuple:
    return 0, 0


def watch_endpoints(on_change, stop: threading.Event) -> None:
    stop.wait()
