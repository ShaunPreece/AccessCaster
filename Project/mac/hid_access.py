"""macOS's `hid_access`: hidapi opens a device exclusively on macOS unless told otherwise, and an
exclusive open fails while any other handle on the device is open. The desk's listener holds its
control interface open while every request opens it again, and RODE's own apps open it too.

The Python binding does not wrap hidapi's switch, so it is called in the binding's own image,
where hidapi is linked in. `hid_init` comes first: initialising sets the switch back to exclusive.
"""
import ctypes

import hid

import diagnostics


def share_devices() -> None:
    try:
        lib = ctypes.CDLL(hid.__file__)
        lib.hid_init()
        lib.hid_darwin_set_open_exclusive(0)
    except (OSError, AttributeError) as exc:
        diagnostics.event("hid.share_failed", str(exc))
