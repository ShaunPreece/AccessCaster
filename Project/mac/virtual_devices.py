"""macOS's `virtual_devices`: whether RODE's Virtual Devices software is installed and running, and
nothing more this app can see or change.

RODE ship two kinds for the Mac, both signed by Freedman Electronics, and neither shows its binding
to the desk (`SEES_BINDING`):
  - 1.1.1, the archive their guide offers first (RODECasterDriver_MACOS.zip): a HAL plug-in,
    /Library/Audio/Plug-Ins/HAL/RodeVirtualChannels.driver, which gives the system ten devices of
    its own, and `/Applications/RODE Virtual Channels.app`. A program is sent to Game, Music, A or
    B by choosing that device as its output in its own settings.
  - 2.0 beta (RODECasterDriver_MACOS-BETA.zip, needs macOS 14.2): no plug-in, which its installer
    removes, and `/Applications/RODE Virtual Devices.app`, a menu bar app (JUCE) that captures the
    programs the user picks with ScreenCaptureKit and plays each into a slot on the desk's
    "RODECaster Pro II Main Multitrack" or "RODECaster Duo Main Multitrack" device, which the desk
    presents in Expanded mode. Slot assignments are kept per desk serial number.
Both apps have the bundle id `com.rode.RODEVirtualChannels`, are started at sign-in by
/Library/LaunchAgents/com.rode.RODEVirtualChannels.plist, and log to /tmp/RODEVirtualDevices.out
and .err. Unknown without a desk: the plug-in's device names, how either is driven with
VoiceOver, and where the slot assignments are stored. Until those are known, the endpoint half of
the contract does nothing here.
"""
import os
import plistlib
import threading

from mac.rode_app import running as _bundle_running

# The 2.0 beta's app, then 1.1.1's, which also installs PLUG_IN.
APPS = ("/Applications/RODE Virtual Devices.app", "/Applications/RODE Virtual Channels.app")
PLUG_IN = "/Library/Audio/Plug-Ins/HAL/RodeVirtualChannels.driver"
SEES_BINDING = False


def _version(bundle: str) -> str:
    try:
        with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as file:
            return str(plistlib.load(file).get("CFBundleShortVersionString", ""))
    except (OSError, plistlib.InvalidFileException):
        return ""


def driver_state() -> dict:
    apps = [app for app in APPS if os.path.isdir(app)]
    if not apps and not os.path.isdir(PLUG_IN):
        return {"installed": False}
    try:
        running = any(_bundle_running(os.path.basename(app)) for app in apps)
    except OSError:
        running = False
    return {"installed": True, "running": running,
            "driver_version": _version(apps[0]) if apps else _version(PLUG_IN)}


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
