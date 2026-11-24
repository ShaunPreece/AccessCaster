"""The desk's own storage volume as this computer mounts it, and letting go of it cleanly.

The desk's mass-storage interface (MI_08 on a Pro II) is a Linux USB gadget, presented while the
desk is in transfer mode. Only a volume on the desk's own USB device can be its pad storage, so the
volume is found by hardware, which no test of its contents can do: a USB stick can hold a `pads`
folder too.

  - `desk_roots()`: the root path of every volume on the desk's own USB device, possibly with no
    media in it yet. Empty when none is found or the system cannot be asked.
  - `name(root)`: the volume as the user knows it, for a sentence.
  - `flush(root)`: push the system's write cache for the volume to the desk, so a copied file is
    complete before transfer mode ends. A flush that does nothing is silent and expensive: the desk
    gets a truncated file, and nobody finds out until the pad plays.
  - `dismount(root)`: flush and let go of the volume before the desk takes the media away. True
    when the system let go of it.
  - `quiet_errors()`: a context manager in which touching a half-mounted volume raises the error
    the caller catches, never a dialog the user has to find and dismiss. Thread-scoped.

Each platform supplies its own.
"""
import sys

if sys.platform == "win32":
    from win.desk_drives import desk_roots, dismount, flush, name, quiet_errors
else:
    raise ImportError(f"No desk drive lookup for {sys.platform}: the Windows one is "
                      "win/desk_drives.py")

__all__ = ["desk_roots", "dismount", "flush", "name", "quiet_errors"]
