"""Time the desk's device interfaces against RODE's service binding to it.

RODE's service has no device-notification import at all: it polls for the desk's HID interface
(`HidD_GetHidGuid`, `HidD_GetSerialNumberString`) and then opens the KS audio filters named
`usb1main` and `usb1chat` by a device-interface path holding one of the four Expanded product
ids. If the audio interface is not ACTIVE yet at the instant it looks, it logs
"DeviceInterfaceAlias disabled." and gives up for that arrival.

This samples, several times a second, which of the desk's interfaces exist and which carry
SPINT_ACTIVE, alongside the service's own connected flag, and prints every change with a
millisecond offset. A bound arrival and an unbound one, compared, say whether the bind is a race
between the HID interface and the audio interface or something else entirely.

    python DevTools/bind_timeline.py                 watch for 60 s, print changes
    python DevTools/bind_timeline.py --seconds 90
    python DevTools/bind_timeline.py --snapshot      what exists right now, then exit

Read-only: it enumerates Windows' device tree and reads shared memory, and writes nothing. Make
the arrival yourself (the app's USB mode control, or the cable).
"""
from __future__ import annotations

import argparse
import ctypes
import time
from ctypes import wintypes

import _bootstrap  # noqa: F401  (puts Project/ on the path)

import virtual_devices

# The interface classes an audio device publishes. The service wants the audio one; Windows
# registers a category and its aliases separately, so they can come up at different moments.
CATEGORIES = {
    "audio":   "{6994AD04-93EF-11D0-A3CC-00A0C9223196}",
    "capture": "{65E8773D-8F56-11D0-A3B9-00A0C9223196}",
    "render":  "{65E8773E-8F56-11D0-A3B9-00A0C9223196}",
    "topology": "{DDA54A40-1E4C-11D1-A050-405705C10000}",
    "hid":     "{4D1E55B2-F16F-11CF-88CB-001111000030}",
}

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


def _guid(text: str) -> GUID:
    g = GUID()
    _ole32.CLSIDFromString(text, ctypes.byref(g))
    return g


def interfaces(category: str, match: str = "vid_19f7") -> dict:
    """Every interface of one category whose path holds `match`, mapped to its active flag.

    The detail struct's cbSize is the size of the FIXED part, 8 on x64 (a DWORD plus one WCHAR,
    padded), never the size of the buffer: Windows rejects anything else."""
    guid = _guid(CATEGORIES[category])
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


def sample() -> dict:
    """One row: every matching interface keyed `category path`, plus the service's own flags."""
    row = {}
    for cat in CATEGORIES:
        for path, active in interfaces(cat).items():
            row[f"{cat} {path}"] = "active" if active else "INACTIVE"
    st = virtual_devices.read_status()
    row["service connected"] = str(st.connected)
    row["service desk"] = st.desk or "-"
    return row


def short(key: str) -> str:
    """The category and the interesting tail of a device path: the product id and the reference
    string, which is what names one of several interfaces on the same device."""
    cat, _, path = key.partition(" ")
    if not path:
        return key
    parts = path.split("#")
    pid = next((p for p in parts if p.startswith("vid_")), "?")
    ref = parts[-1] if parts[-1].startswith("{") is False else ""
    return f"{cat:8} {pid} {ref}".strip()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--interval", type=float, default=0.15)
    ap.add_argument("--snapshot", action="store_true", help="print the current state and exit")
    args = ap.parse_args()

    first = sample()
    print(f"--- {len(first)} rows at start ---")
    for k in sorted(first):
        print(f"    {short(k):58} {first[k]}")
    if args.snapshot:
        return

    print(f"\n--- watching {args.seconds:.0f} s, changes only (make the arrival now) ---")
    at = time.monotonic()
    previous = first
    while time.monotonic() - at < args.seconds:
        time.sleep(args.interval)
        now = sample()
        stamp = f"{time.monotonic() - at:7.2f}s"
        for key in sorted(set(previous) | set(now)):
            was, is_ = previous.get(key), now.get(key)
            if was == is_:
                continue
            if was is None:
                print(f"{stamp}  + {short(key):58} {is_}")
            elif is_ is None:
                print(f"{stamp}  - {short(key):58} (gone)")
            else:
                print(f"{stamp}  ~ {short(key):58} {was} -> {is_}")
        previous = now
    print("--- done ---")


if __name__ == "__main__":
    main()
