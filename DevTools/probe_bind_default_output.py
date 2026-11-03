"""Does Windows' default output sitting on the driver's own endpoint stop the service binding?

RODE's service binds to the desk on some arrivals and not others. One candidate nobody has ruled
out: while `System (RØDECaster)` is the default playback device, Windows' audio engine holds a
render stream open on the very endpoint the service is trying to build, and the desk's audio
interfaces are in flux at exactly that moment.

This moves the default output to a device that is neither the desk nor the driver, drives the USB
output mode off and back to make arrivals, and counts how many arrivals produce a bind. Compare
the rate against the same count with the default left on `System`.

    python DevTools/probe_bind_default_output.py --cycles 6
    python DevTools/probe_bind_default_output.py --cycles 6 --keep-default

It restores the default output and the USB output mode it found, whatever happens, and every flip
is a mode the desk already supports. It runs the app's service itself, the watchers included, so
quit AccessCaster first.
"""
from __future__ import annotations

import argparse
import ctypes
import threading
import time
from ctypes import wintypes

import _bootstrap  # noqa: F401
import comtypes
import comtypes.client
import meters
import virtual_devices as vd
import _desk

EXPANDED_PID = "pid_0094"       # post-fader multitrack AND Expanded: the mode with fader inputs
HID_INTERFACE_CLASS = "{4D1E55B2-F16F-11CF-88CB-001111000030}"
DIGCF_DEVICEINTERFACE = 0x10        # no DIGCF_PRESENT: inactive interfaces are the point
SPINT_ACTIVE = 0x01

_setupapi = ctypes.WinDLL("setupapi", use_last_error=True)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("InterfaceClassGuid", GUID),
                ("Flags", wintypes.DWORD), ("Reserved", ctypes.POINTER(ctypes.c_ulonglong))]


_setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
_setupapi.SetupDiGetClassDevsW.argtypes = (ctypes.POINTER(GUID), wintypes.LPCWSTR,
                                           wintypes.HWND, wintypes.DWORD)
_setupapi.SetupDiEnumDeviceInterfaces.argtypes = (
    wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID), wintypes.DWORD,
    ctypes.POINTER(SP_DEVICE_INTERFACE_DATA))
_setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = (
    wintypes.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p,
    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
_setupapi.SetupDiDestroyDeviceInfoList.argtypes = (wintypes.HANDLE,)

_ole32 = ctypes.WinDLL("ole32")
_ole32.CLSIDFromString.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(GUID))


def hid_interfaces(match: str = "vid_19f7") -> dict:
    """Every HID interface whose path holds `match`, mapped to whether it is ACTIVE.

    The detail struct's cbSize is the size of the FIXED part, 8 on x64 (a DWORD plus one WCHAR,
    padded), never the size of the buffer: Windows rejects anything else."""
    guid = GUID()
    _ole32.CLSIDFromString(HID_INTERFACE_CLASS, ctypes.byref(guid))
    h = _setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, DIGCF_DEVICEINTERFACE)
    if h == wintypes.HANDLE(-1).value:
        return {}
    found = {}
    try:
        data = SP_DEVICE_INTERFACE_DATA()
        data.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
        index = 0
        buf = ctypes.create_string_buffer(2048)
        while _setupapi.SetupDiEnumDeviceInterfaces(h, None, ctypes.byref(guid), index,
                                                    ctypes.byref(data)):
            index += 1
            ctypes.memset(buf, 0, ctypes.sizeof(buf))
            ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0] = 8
            need = wintypes.DWORD()
            if _setupapi.SetupDiGetDeviceInterfaceDetailW(h, ctypes.byref(data), buf,
                                                          ctypes.sizeof(buf),
                                                          ctypes.byref(need), None):
                path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
                if match in path.lower():
                    found[path.lower()] = bool(data.Flags & SPINT_ACTIVE)
    finally:
        _setupapi.SetupDiDestroyDeviceInfoList(h)
    return found


server = None


def post_mode(mode: int) -> dict:
    """Set the desk's USB output mode. The desk re-enumerates, so the reply can take a while."""
    return server.action_set_usb_multitrack(mode)


class Sampler(threading.Thread):
    """Count arrivals of the desk in the Expanded multitrack mode, and binds of the service.

    An arrival is the HID interface going ACTIVE, which is the interface the service polls for.
    Counting arrivals rather than flips is what makes the rate comparable: the app's own follow-up
    makes extra arrivals whenever it misses, and every one of those is another trial."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.stop = threading.Event()
        self.arrivals = 0
        self.binds = 0
        self.counted = 0        # the arrival the last counted bind followed
        self.events: list = []

    def run(self) -> None:
        at = time.monotonic()
        was_here = False
        was_bound = vd.read_status().connected
        while not self.stop.is_set():
            here = any(EXPANDED_PID in path and active
                       for path, active in hid_interfaces().items())
            bound = vd.read_status().connected
            now = time.monotonic() - at
            if here and not was_here:
                self.arrivals += 1
                self.events.append(f"{now:7.1f}s  arrival {self.arrivals}")
            if bound and not was_bound:
                # The mix-only step of a cycle also unbinds and rebinds the service; only the first
                # bind after each multitrack arrival counts towards the rate.
                if self.arrivals > self.counted:
                    self.binds += 1
                    self.counted = self.arrivals
                self.events.append(f"{now:7.1f}s  BOUND after arrival {self.arrivals}")
            if was_bound and not bound:
                self.events.append(f"{now:7.1f}s  unbound")
            was_here, was_bound = here, bound
            time.sleep(0.2)


def pick_target(eps: list) -> "vd.Endpoint | None":
    """A live playback device that is neither the desk's nor the driver's."""
    return next((e for e in eps if e.flow == "playback" and e.state == "active"
                 and not e.desk and not e.virtual), None)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cycles", type=int, default=6)
    ap.add_argument("--keep-default", action="store_true",
                    help="leave the default output where this probe put it")
    ap.add_argument("--control", action="store_true",
                    help="THE MATCHED CONTROL: move nothing, so the same cycles run with the "
                         "default output wherever it already is. Without this run the rate means "
                         "nothing, because a run of failures alone would produce the same drop.")
    args = ap.parse_args()
    global server
    # The watchers too, as the app runs them: this measures the bind with them in place.
    server = _desk.service(start=True)

    comtypes.CoInitialize()
    enum = comtypes.client.CreateObject(meters.CLSID_MMDeviceEnumerator,
                                        interface=meters.IMMDeviceEnumerator)
    policy = comtypes.client.CreateObject(vd.CLSID_PolicyConfigClient, interface=vd.IPolicyConfig)
    eps = vd._endpoints(enum)
    by_id = {e.id: e for e in eps}
    was_default_id = vd._default_id(enum, vd.E_CONSOLE)
    was_default = by_id.get(was_default_id)
    target = pick_target(eps)
    if target is None:
        print("No playback device outside the desk and the driver. Nothing to move to.")
        return
    # Every cycle ends in post-fader multitrack, so the mode found here is put back at the end.
    # The arrivals counted only happen in Expanded, so a desk found in Standard is switched to
    # Expanded first and back afterwards.
    live = server.read_routing_live()
    mode_before, expanded_before = live.get("usb_multitrack"), live.get("usb_expanded")
    if mode_before is None or expanded_before is None:
        print("Could not read the desk's USB output mode, so it could not be put back. Stopping.")
        return
    print(f"default was : {was_default.name if was_default else '?'}")
    if args.control:
        print("control run : the default output is not moved")
    else:
        print(f"moving to   : {target.name}")
        policy.SetDefaultEndpoint(target.id, vd.E_CONSOLE)
        policy.SetDefaultEndpoint(target.id, vd.E_MULTIMEDIA)

    if not expanded_before:
        print("switching USB 1 input to Expanded ...", flush=True)
        server.action_set_usb_expanded(True)
        time.sleep(10)
    sampler = Sampler()
    sampler.start()
    try:
        for i in range(1, args.cycles + 1):
            print(f"cycle {i}: mix only ...", flush=True)
            post_mode(0)
            time.sleep(4)
            print(f"cycle {i}: post-fader ...", flush=True)
            post_mode(2)
            # The service binds within about three seconds of an arrival or not at all, but the
            # app's own follow-up may still be making arrivals of its own, so give the cycle room
            # rather than calling a miss early.
            time.sleep(25)
            st = vd.read_status()
            print(f"cycle {i}: connected={st.connected}  "
                  f"arrivals so far={sampler.arrivals} binds={sampler.binds}", flush=True)
    finally:
        sampler.stop.set()
        sampler.join(timeout=3)
        if not expanded_before:
            res = server.action_set_usb_expanded(False)
            print("USB 1 input restored to Standard" if res.get("ok") else
                  f"USB 1 INPUT NOT RESTORED to Standard: {res.get('message')}")
            time.sleep(10)
        res = post_mode(mode_before)
        print(f"USB output mode restored to {mode_before}" if res.get("ok") else
              f"USB OUTPUT MODE NOT RESTORED to {mode_before}: {res.get('message')}")
        if not args.control and not args.keep_default and was_default is not None:
            policy.SetDefaultEndpoint(was_default_id, vd.E_CONSOLE)
            policy.SetDefaultEndpoint(was_default_id, vd.E_MULTIMEDIA)
            print(f"default restored to: {was_default.name}")

    print()
    for line in sampler.events:
        print(" ", line)
    rate = (sampler.binds / sampler.arrivals) if sampler.arrivals else 0.0
    where = "ON the driver (control)" if args.control else "off the driver"
    print(f"\n{sampler.binds} binds in {sampler.arrivals} arrivals  ({rate:.0%}), "
          f"default output {where}")


if __name__ == "__main__":
    main()
