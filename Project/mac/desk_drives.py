"""macOS's `desk_drives`: the desk's volumes, found through the I/O Registry.

macOS mounts a volume under /Volumes when its media appears and unmounts it when the media goes,
so a root here exists only while the desk presents its storage: there is no empty drive letter
to wait on, as there is on Windows. The mounted file systems come from `getfsstat`; each one on a
/dev/disk node is looked up in the I/O Registry by its BSD name, and is the desk's when the
nearest USB device or interface above it carries RODE's vendor ID.

macOS raises no dialog for a read that fails on a half-mounted volume, so `quiet_errors` has
nothing to silence. What it does show is "Disk Not Ejected Properly" when media vanishes while
mounted, which `dismount` before transfer mode ends prevents.
"""
from __future__ import annotations

import contextlib
import ctypes
import os
import platform
import subprocess

import diagnostics
import rcp_hid
from mac import cf

_MNT_NOWAIT = 2
_MAXPATHLEN = 1024
_SYNC_VOLUME_FULLSYNC = 0x01           # past the system's cache and the drive's own
_SYNC_VOLUME_WAIT = 0x02               # return once it is on the media
_ITERATE_RECURSIVELY = 0x01
_ITERATE_PARENTS = 0x02
_CF_NUMBER_SINT32 = 3
_DISKUTIL = "/usr/sbin/diskutil"
_UNMOUNT_TIMEOUT_S = 30


class _StatFS(ctypes.Structure):
    """`struct statfs` in its 64-bit-inode layout, the only one on Apple silicon."""
    _fields_ = [("f_bsize", ctypes.c_uint32), ("f_iosize", ctypes.c_int32),
                ("f_blocks", ctypes.c_uint64), ("f_bfree", ctypes.c_uint64),
                ("f_bavail", ctypes.c_uint64), ("f_files", ctypes.c_uint64),
                ("f_ffree", ctypes.c_uint64), ("f_fsid", ctypes.c_int32 * 2),
                ("f_owner", ctypes.c_uint32), ("f_type", ctypes.c_uint32),
                ("f_flags", ctypes.c_uint32), ("f_fssubtype", ctypes.c_uint32),
                ("f_fstypename", ctypes.c_char * 16),
                ("f_mntonname", ctypes.c_char * _MAXPATHLEN),
                ("f_mntfromname", ctypes.c_char * _MAXPATHLEN),
                ("f_flags_ext", ctypes.c_uint32), ("f_reserved", ctypes.c_uint32 * 7)]


_libc = ctypes.CDLL(None, use_errno=True)
# An Intel Mac keeps the old layout under the plain name and this one under the suffixed name.
_getfsstat = (_libc["getfsstat$INODE64"] if platform.machine() == "x86_64"
              else _libc.getfsstat)
_getfsstat.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
_getfsstat.restype = ctypes.c_int
_libc.sync_volume_np.argtypes = [ctypes.c_char_p, ctypes.c_int]
_libc.sync_volume_np.restype = ctypes.c_int

_IOKit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
_IOKit.IOBSDNameMatching.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_char_p]
_IOKit.IOBSDNameMatching.restype = ctypes.c_void_p
_IOKit.IOServiceGetMatchingService.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
_IOKit.IOServiceGetMatchingService.restype = ctypes.c_uint32
_IOKit.IORegistryEntrySearchCFProperty.argtypes = [ctypes.c_uint32, ctypes.c_char_p,
                                                   ctypes.c_void_p, ctypes.c_void_p,
                                                   ctypes.c_uint32]
_IOKit.IORegistryEntrySearchCFProperty.restype = ctypes.c_void_p
_IOKit.IOObjectRelease.argtypes = [ctypes.c_uint32]

_CF = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_CF.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
_CF.CFNumberGetValue.restype = ctypes.c_bool


def _mounts() -> list[tuple[str, str]]:
    """(mount point, device node) for every mounted file system."""
    count = _getfsstat(None, 0, _MNT_NOWAIT)
    if count <= 0:
        return []
    # Room for file systems mounted between the two calls.
    table = (_StatFS * (count + 8))()
    count = _getfsstat(table, ctypes.sizeof(table), _MNT_NOWAIT)
    return [(entry.f_mntonname.decode("utf-8", "replace"),
             entry.f_mntfromname.decode("utf-8", "replace")) for entry in table[:max(0, count)]]


def _vendor_id(bsd_name: str) -> int | None:
    """The USB vendor ID nearest above the disk `bsd_name` in the I/O Registry, or None."""
    matching = _IOKit.IOBSDNameMatching(0, 0, bsd_name.encode("ascii"))
    if not matching:
        return None
    service = _IOKit.IOServiceGetMatchingService(0, matching)     # consumes `matching`
    if not service:
        return None
    key = cf.cfstring("idVendor")
    try:
        found = _IOKit.IORegistryEntrySearchCFProperty(service, b"IOService", key, None,
                                                       _ITERATE_RECURSIVELY | _ITERATE_PARENTS)
        if not found:
            return None
        value = ctypes.c_int32()
        ok = _CF.CFNumberGetValue(found, _CF_NUMBER_SINT32, ctypes.byref(value))
        cf.release(found)
        return value.value if ok else None
    finally:
        cf.release(key)
        _IOKit.IOObjectRelease(service)


def desk_roots() -> set[str]:
    """Empty when the system cannot be asked, so a caller looking for the desk's volume finds
    nothing rather than somebody else's."""
    roots = set()
    try:
        for root, source in _mounts():
            if source.startswith("/dev/disk") and \
                    _vendor_id(source[len("/dev/"):]) == rcp_hid.RODE_VENDOR_ID:
                roots.add(root)
    except OSError as exc:
        diagnostics.event("transfer.roots_failed", str(exc))
    return roots


def name(root: str) -> str:
    return os.path.basename(os.path.normpath(root)) or root


def flush(root: str) -> None:
    """Write everything cached for the volume to the desk's media and wait until it is there.
    A failure falls back to `sync`, which starts the same writes without waiting for them."""
    status = _libc.sync_volume_np(root.encode("utf-8"),
                                  _SYNC_VOLUME_FULLSYNC | _SYNC_VOLUME_WAIT)
    if status:
        diagnostics.event("transfer.flush_failed", os.strerror(status), root=root)
        os.sync()


def dismount(root: str) -> bool:
    """Unmount the volume, which writes out what is cached for it first. A volume a process
    still holds a file open on is unmounted by force: those files are lost either way once the
    desk takes the media."""
    if not os.path.ismount(root):
        return False
    for command in (["unmount"], ["unmount", "force"]):
        try:
            done = subprocess.run([_DISKUTIL, *command, root], capture_output=True, text=True,
                                  timeout=_UNMOUNT_TIMEOUT_S, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            diagnostics.event("transfer.dismount_failed", f"{root}: {exc}")
            return False
        if done.returncode == 0:
            diagnostics.event("transfer.dismount", root, forced=len(command) > 1, dismounted=True)
            return True
    diagnostics.event("transfer.dismount_failed", f"{root}: {done.stderr.strip()[-300:]}")
    return False


def quiet_errors():
    return contextlib.nullcontext()
