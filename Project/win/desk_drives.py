"""Windows' `desk_drives`: the desk's drive letters, found through the device tree.

The desk's mass-storage interface (MI_08 on a Pro II) is a Linux USB gadget, so Windows shows it as
a "Linux File-Stor Gadget" disk under the desk's own USB device, and gives it a drive letter whether
or not anything is mounted on it: out of transfer mode the letter is a removable drive with no
media, size 0. Asking the device tree which disk a letter is on, and whose USB device that disk
hangs off, identifies the desk's volume by hardware, which no test of the volume's contents can do:
a USB stick can hold a `pads` folder too.
"""

from __future__ import annotations

import ctypes
import string
import subprocess
import time
from ctypes import wintypes

import diagnostics
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


def desk_roots() -> set[str]:
    """Empty when the device tree cannot be read, so a caller looking for the desk's volume finds
    nothing rather than somebody else's drive."""
    desk_disks = _desk_disk_numbers()
    if not desk_disks:
        return set()
    return {root for root in _drive_roots()
            if _disk_number("\\\\.\\" + root[0] + ":") in desk_disks}


def _drive_roots() -> list[str]:
    """Every drive root from D to Z. A and B are floppy letters and C is the system drive."""
    return [f"{letter}:\\" for letter in string.ascii_uppercase[3:]]


def name(root: str) -> str:
    return root[:2]


def flush(root: str) -> None:
    """Flush Windows' write cache for the pad-storage volume to the device.

    os.sync() does NOT exist on Windows, so a copied file can sit in cache and never reach the unit
    before we exit transfer mode. This calls `FlushFileBuffers` on a volume handle, which is what
    `Write-VolumeCache` does underneath without the 1.2 to 2.3 s cost of starting PowerShell per pad.

    THE FAILURE MODE IS SILENT AND EXPENSIVE, which is why the PowerShell path is KEPT as a
    fallback: a flush that does nothing leaves a pad's audio in Windows' cache, the desk gets a
    truncated file, and nobody finds out until the pad plays. DevTools/test_flush_verify_live.py
    proves the fast path by md5 of the file read back off the volume.
    """
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ_WRITE = 0x00000001 | 0x00000002
    OPEN_EXISTING = 3
    INVALID_HANDLE = ctypes.c_void_p(-1).value
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.FlushFileBuffers.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        device = "\\\\.\\" + root[0] + ":"      # the volume's DOS device path, \\.\X:
        handle = k32.CreateFileW(device, GENERIC_WRITE, FILE_SHARE_READ_WRITE,
                                 None, OPEN_EXISTING, 0, None)
        if handle in (0, INVALID_HANDLE, None):
            diagnostics.event("transfer.flush_fallback", "no volume handle",
                              root=root, error=ctypes.get_last_error())
        else:
            try:
                if k32.FlushFileBuffers(handle):
                    return
                diagnostics.event("transfer.flush_fallback", "FlushFileBuffers failed",
                                  root=root, error=ctypes.get_last_error())
            finally:
                k32.CloseHandle(handle)
    except Exception as exc:
        diagnostics.event("transfer.flush_fallback", str(exc))   # the slow but proven path follows
    _flush_powershell(root)


def dismount(root: str) -> bool:
    """Flush, lock and dismount the pad volume at `root`, so Windows lets go of it before the desk
    takes the media away. True when Windows dismounted it.

    LEAVING TRANSFER MODE PULLS THE MEDIA WITHOUT WARNING. Without a dismount first, Windows keeps
    its mount of the volume, cached FAT and free-space bitmap included, across the desk's own
    changes to the disk, and marks the volume dirty, which is the "There's a problem with this
    drive" notification on the next mount. A refused lock (another process has a file open) still
    dismounts: those handles are invalid either way once the media goes.
    """
    GENERIC_READ_WRITE = 0x80000000 | 0x40000000
    FILE_SHARE_READ_WRITE = 0x00000001 | 0x00000002
    OPEN_EXISTING = 3
    FSCTL_LOCK_VOLUME = 0x00090018
    FSCTL_DISMOUNT_VOLUME = 0x00090020
    INVALID_HANDLE = ctypes.c_void_p(-1).value
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.DeviceIoControl.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                                        ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
                                        ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        k32.FlushFileBuffers.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        with quiet_errors():
            handle = k32.CreateFileW("\\\\.\\" + root[0] + ":", GENERIC_READ_WRITE,
                                     FILE_SHARE_READ_WRITE, None, OPEN_EXISTING, 0, None)
            if handle in (0, INVALID_HANDLE, None):
                return False         # no volume mounted on that letter: nothing to let go of
            try:
                returned = ctypes.c_uint32(0)

                def ioctl(code: int) -> bool:
                    return bool(k32.DeviceIoControl(handle, code, None, 0, None, 0,
                                                    ctypes.byref(returned), None))

                k32.FlushFileBuffers(handle)
                locked = False
                for _ in range(5):
                    if ioctl(FSCTL_LOCK_VOLUME):
                        locked = True
                        break
                    time.sleep(0.1)
                dismounted = ioctl(FSCTL_DISMOUNT_VOLUME)
            finally:
                k32.CloseHandle(handle)    # closing the handle also releases the lock
        diagnostics.event("transfer.dismount", root, locked=locked, dismounted=dismounted)
        return dismounted
    except Exception as exc:
        diagnostics.event("transfer.dismount_failed", f"{root}: {exc}")
        return False


def _flush_powershell(root: str) -> None:
    """Flush through `Write-VolumeCache`: correct, and about two seconds per call."""
    # CREATE_NO_WINDOW: the app runs under pythonw (no console), so without this each call would
    # flash a PowerShell window that steals focus.
    done = subprocess.run(
        ["powershell", "-NoProfile", "-Command", f"Write-VolumeCache {root[0]}"],
        check=False,
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if done.returncode != 0:
        # Both flushes failed: a pad's audio may reach the desk truncated.
        stderr = (done.stderr or b"").decode("utf-8", "replace").strip()
        diagnostics.event("transfer.flush_failed", stderr[-300:], root=root,
                          exit_code=done.returncode)


class quiet_errors:
    """Stop Windows throwing a modal "There is a problem with this drive" box at the user while we
    probe or read the pad volume.

    A half-mounted removable volume makes the OS raise a HARD ERROR dialog on behalf of whichever
    process touched it, and our recovery for that state is simply to wait and retry - so the dialog
    reports a condition we are already handling, and it lands on the user as a modal they have to
    find and dismiss with a screen reader. SEM_FAILCRITICALERRORS turns the dialog back into the
    error code the caller already catches. Thread-scoped (SetThreadErrorMode, not SetErrorMode) so a background
    pad worker cannot suppress dialogs for the rest of the process.
    """

    _SEM_FAILCRITICALERRORS = 0x0001

    def __enter__(self):
        self._old = None
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            old = ctypes.c_uint(0)
            if k32.SetThreadErrorMode(self._SEM_FAILCRITICALERRORS, ctypes.byref(old)):
                self._old = old.value
                self._k32 = k32
        except Exception:
            pass          # the call is unavailable: the dialogs may appear
        return self

    def __exit__(self, *exc):
        if self._old is not None:
            try:
                self._k32.SetThreadErrorMode(self._old, ctypes.byref(ctypes.c_uint(0)))
            except Exception:
                pass      # must not replace the error the block itself is raising
        return False
