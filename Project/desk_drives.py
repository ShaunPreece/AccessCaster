"""
AccessCaster - which Windows drive letters belong to the desk itself.

The desk's mass-storage interface (MI_08 on a Pro II) is a Linux USB gadget, so Windows shows it as
a "Linux File-Stor Gadget" disk under the desk's own USB device, and gives it a drive letter whether
or not anything is mounted on it: out of transfer mode the letter is a removable drive with no
media, size 0. Asking the device tree which disk a letter is on, and whose USB device that disk
hangs off, identifies the desk's volume by hardware, which no test of the volume's contents can do:
a USB stick can hold a `pads` folder too.

Windows only; elsewhere every lookup returns an empty set.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

import rcp_hid

# Every device-instance ID of the desk's USB device and its interfaces starts with this.
_DESK_USB_PREFIX = f"USB\\VID_{rcp_hid.RODE_VENDOR_ID:04X}&"

_IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x2D1080
_FILE_SHARE_READ_WRITE = 0x00000001 | 0x00000002
_OPEN_EXISTING = 3
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_CR_SUCCESS = 0
_DEVPROP_TYPE_STRING = 0x12
_MAX_DEVICE_ID_LEN = 200


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class _DEVPROPKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", wintypes.ULONG)]


class _STORAGE_DEVICE_NUMBER(ctypes.Structure):
    _fields_ = [("DeviceType", wintypes.DWORD), ("DeviceNumber", wintypes.DWORD),
                ("PartitionNumber", wintypes.DWORD)]


def _guid(d1: int, d2: int, d3: int, d4: bytes) -> _GUID:
    return _GUID(d1, d2, d3, (ctypes.c_ubyte * 8)(*d4))


_GUID_DEVINTERFACE_DISK = _guid(0x53F56307, 0xB6BF, 0x11D0, bytes.fromhex("94f200a0c91efb8b"))
_DEVPKEY_DEVICE_INSTANCEID = _DEVPROPKEY(
    _guid(0x78C34FC8, 0x104A, 0x4ACA, bytes.fromhex("9ea4524d52996e57")), 256)

_k32 = None
_cm = None


def _libs():
    """kernel32 and cfgmgr32 with their prototypes set, loaded on first use."""
    global _k32, _cm
    if _k32 is None:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                    ctypes.c_void_p]
        k32.DeviceIoControl.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                        wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        cm = ctypes.WinDLL("cfgmgr32")
        cm.CM_Get_Device_Interface_List_SizeW.argtypes = [
            ctypes.POINTER(wintypes.ULONG), ctypes.POINTER(_GUID), wintypes.LPCWSTR, wintypes.ULONG]
        cm.CM_Get_Device_Interface_ListW.argtypes = [
            ctypes.POINTER(_GUID), wintypes.LPCWSTR, ctypes.c_void_p, wintypes.ULONG,
            wintypes.ULONG]
        cm.CM_Get_Device_Interface_PropertyW.argtypes = [
            wintypes.LPCWSTR, ctypes.POINTER(_DEVPROPKEY), ctypes.POINTER(wintypes.ULONG),
            ctypes.c_void_p, ctypes.POINTER(wintypes.ULONG), wintypes.ULONG]
        cm.CM_Locate_DevNodeW.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.LPCWSTR,
                                          wintypes.ULONG]
        cm.CM_Get_Parent.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
                                     wintypes.ULONG]
        cm.CM_Get_Device_IDW.argtypes = [wintypes.DWORD, ctypes.c_wchar_p, wintypes.ULONG,
                                         wintypes.ULONG]
        _k32, _cm = k32, cm
    return _k32, _cm


def _disk_number(path: str) -> int | None:
    """The disk number behind a disk interface path or a volume path (`\\\\.\\F:`), or None.

    Zero access rights are enough for this IOCTL, so it answers for a drive with no media too.
    """
    k32, _ = _libs()
    handle = k32.CreateFileW(path, 0, _FILE_SHARE_READ_WRITE, None, _OPEN_EXISTING, 0, None)
    if handle in (None, 0, _INVALID_HANDLE):
        return None
    try:
        number = _STORAGE_DEVICE_NUMBER()
        returned = wintypes.DWORD()
        if not k32.DeviceIoControl(handle, _IOCTL_STORAGE_GET_DEVICE_NUMBER, None, 0,
                                   ctypes.byref(number), ctypes.sizeof(number),
                                   ctypes.byref(returned), None):
            return None
        return number.DeviceNumber
    finally:
        k32.CloseHandle(handle)


def _disk_interfaces() -> list[str]:
    """The interface path of every disk Windows has present."""
    _, cm = _libs()
    size = wintypes.ULONG()
    if cm.CM_Get_Device_Interface_List_SizeW(ctypes.byref(size), ctypes.byref(_GUID_DEVINTERFACE_DISK),
                                             None, 0) != _CR_SUCCESS:
        return []
    buf = ctypes.create_unicode_buffer(size.value)
    if cm.CM_Get_Device_Interface_ListW(ctypes.byref(_GUID_DEVINTERFACE_DISK), None, buf,
                                        size.value, 0) != _CR_SUCCESS:
        return []
    return [p for p in ctypes.wstring_at(ctypes.addressof(buf), size.value).split("\0") if p]


def _instance_id(interface: str) -> str | None:
    _, cm = _libs()
    prop_type = wintypes.ULONG()
    buf = ctypes.create_unicode_buffer(_MAX_DEVICE_ID_LEN)
    size = wintypes.ULONG(ctypes.sizeof(buf))
    if cm.CM_Get_Device_Interface_PropertyW(interface, ctypes.byref(_DEVPKEY_DEVICE_INSTANCEID),
                                            ctypes.byref(prop_type), buf, ctypes.byref(size),
                                            0) != _CR_SUCCESS:
        return None
    return buf.value if prop_type.value == _DEVPROP_TYPE_STRING else None


def _on_desk_usb(instance_id: str) -> bool:
    """Is this device, or any device above it in the tree, the desk's USB device?"""
    _, cm = _libs()
    node = wintypes.DWORD()
    if cm.CM_Locate_DevNodeW(ctypes.byref(node), instance_id, 0) != _CR_SUCCESS:
        return False
    buf = ctypes.create_unicode_buffer(_MAX_DEVICE_ID_LEN)
    while True:
        if cm.CM_Get_Device_IDW(node.value, buf, _MAX_DEVICE_ID_LEN, 0) != _CR_SUCCESS:
            return False
        if buf.value.upper().startswith(_DESK_USB_PREFIX):
            return True
        parent = wintypes.DWORD()
        if cm.CM_Get_Parent(ctypes.byref(parent), node.value, 0) != _CR_SUCCESS:
            return False                # past the root of the tree
        node = parent


def _desk_disk_numbers() -> set[int]:
    numbers = set()
    for interface in _disk_interfaces():
        instance = _instance_id(interface)
        if instance and _on_desk_usb(instance):
            number = _disk_number(interface)
            if number is not None:
                numbers.add(number)
    return numbers


def on_desk(roots: list[str]) -> set[str]:
    """The drive roots (`"F:\\\\"`) in `roots` whose disk is on the desk's own USB device.

    Empty when no desk disk is present, off Windows, or when the device tree cannot be read, so a
    caller looking for the desk's volume finds nothing rather than somebody else's drive.
    """
    if os.name != "nt":
        return set()
    desk_disks = _desk_disk_numbers()
    if not desk_disks:
        return set()
    return {root for root in roots if _disk_number("\\\\.\\" + root[0] + ":") in desk_disks}
