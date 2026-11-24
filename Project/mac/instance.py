"""macOS's `instance.claim`: an exclusive `flock` on a file every account can open.

The file is in /tmp, which every account shares, and is opened read-only, which is all `flock`
needs, so an account that did not create it can still take the lock. The descriptor is held for
the life of the process and the system drops the lock when the process ends, however it ends.
"""
import fcntl
import os

LOCK_PATH = "/tmp/AccessCaster.lock"
_held = None


def claim() -> bool:
    global _held
    if _held is not None:
        return True
    fd = os.open(LOCK_PATH, os.O_RDONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    _held = fd
    return True
