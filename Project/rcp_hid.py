"""
AccessCaster - the HID transport: finding the desk, opening it, and the raw command channel.

Everything that talks to the desk sits on top of this module. It imports nothing heavier than
hidapi and the diagnostic log, so it is safe to use from a REPL or a small script.

  Device:   vendor id 0x19F7 (RODE). The control interface is vendor-defined (usage page 0xFF00,
            usage 0x01) and is MI_09 on the desk's USB 1 port; see find_control_interface.

  Report ids on the control interface:
    0x01  Output  host -> device   command, 63 data bytes (RodeCaster.command)
    0x02  Input   device -> host   response, [0x02] [0x41 'A' = ACK] [data ...]
    0x03  Output  host -> device   an addressed property record (built in rcp_transfer)
    0x04  Input   device -> host   the model dump and the desk's own pushes (read in rcp_read)

Nothing here changes device state on import or on open. The property protocol that does is
documented in docs/RODECASTER_PROTOCOL.txt.
"""

from __future__ import annotations

import contextlib
import os
import time
from dataclasses import dataclass
from typing import Optional

import diagnostics            # the app's own account of what it did; silent until started

# AN OFFLINE TEST MUST NOT BE ABLE TO WRITE TO SOMEBODY'S DESK, and a docstring saying so is not a
# guard. `DevTools/_bootstrap` sets this for any script named `test_*`, exactly as it redirects the
# data dir and silences the announcer, so the rule holds for a suite that never thought about it.
#
# THE FAILURE IT PREVENTS IS SILENT AND AUDIBLE AT THE SAME TIME: a suite that reaches a real write
# fires a pad, engages a voice effect or drops the audio on a desk somebody is working on, and the
# test still passes. Raising is the only honest answer - a write that is quietly swallowed would
# make the test prove something it never did.
#
# Set ACCESSCASTER_ALLOW_DEVICE_WRITE to override it for a `test_*_live` suite that means it.
_NO_WRITE = bool(os.environ.get("ACCESSCASTER_NO_DEVICE_WRITE")) and not os.environ.get(
    "ACCESSCASTER_ALLOW_DEVICE_WRITE")


def refuse_write_in_tests(what: str = "write to the device") -> None:
    """Raise if this process is an offline test. Called from every device-write choke point."""
    if _NO_WRITE:
        raise RuntimeError(
            f"REFUSING TO {what.upper()}: this is an offline test (ACCESSCASTER_NO_DEVICE_WRITE). "
            f"Stub the device call, or set ACCESSCASTER_ALLOW_DEVICE_WRITE if it is a live suite.")

try:
    import hid
except ImportError as exc:  # pragma: no cover - environment problem, surfaced to caller
    raise ImportError(
        "hidapi is required. Install with: pip install hidapi"
    ) from exc


RODE_VENDOR_ID = 0x19F7

# ---- write generation (cache invalidation for model reads) ------------------
# Every property WRITE bumps this counter (rcp_transfer.send_record / send_records_paced).
# rcp_read caches the last full model dump and throws that cache away the moment the
# generation moves, so a read-back after a write is always a genuine fresh dump while a
# burst of reads from one page load shares a single dump. Lives here because this is the
# lowest layer both rcp_read and rcp_transfer already import (no circular import).
_write_generation = 0


def bump_write_generation() -> int:
    """Note that something was written to the device (invalidates any cached model read)."""
    global _write_generation
    _write_generation += 1
    return _write_generation


def write_generation() -> int:
    """The current write counter. A cached read is stale as soon as this changes."""
    return _write_generation


def write_report(dev, record: bytes) -> int:
    """Write one raw record to an ALREADY-OPEN handle and note it as a device write.

    Use this instead of dev.write() anywhere outside rcp_transfer.send_record: a write that does
    not bump the generation leaves the cached model dump looking valid, so a read-back straight
    afterwards returns the PRE-write state and a working write looks like a no-op.
    """
    refuse_write_in_tests()
    n = dev.write(record)
    bump_write_generation()
    diagnostics.command(record, "write_report")
    return n

# Report ids (see the module docstring).
REPORT_CMD = 0x01
REPORT_RESP = 0x02
REPORT_BULK_OUT = 0x03
ACK = 0x41  # 'A', the second byte of a normal response

# The control interface uses 64-byte reports (1 report-id byte + 63 data).
CMD_REPORT_LEN = 64
# The vendor-defined control interface looks like this when enumerated.
CONTROL_USAGE_PAGE = 0xFF00
CONTROL_USAGE = 0x01


@dataclass
class RodeInterface:
    """One enumerated RODECaster HID interface."""

    path: bytes
    product_id: int
    interface_number: int
    usage_page: int
    usage: int
    product_string: str
    serial_number: str

    @property
    def is_control(self) -> bool:
        """True if this looks like a vendor control interface we can talk to."""
        return self.usage_page == CONTROL_USAGE_PAGE and self.usage == CONTROL_USAGE

    def describe(self) -> str:
        return (
            f"PID=0x{self.product_id:04x} MI_{self.interface_number:02d} "
            f"usage_page=0x{self.usage_page:04x} usage=0x{self.usage:04x} "
            f"product={self.product_string!r}"
        )


def enumerate_interfaces() -> list[RodeInterface]:
    """Return every RODE HID interface currently connected.

    TIMED, because this is where the app goes when a dying desk freezes it. `hid.enumerate` opens
    each matching device on Windows to read its strings, so a desk half-way off the bus blocks it
    for tens of seconds - 30 measured between two model reads - and it runs BEFORE any handle is
    opened, so nothing else in the log marks the time. Only a slow call is logged; see
    diagnostics.slow.
    """
    at = time.monotonic()
    out: list[RodeInterface] = []
    for d in hid.enumerate(RODE_VENDOR_ID, 0):
        out.append(
            RodeInterface(
                path=d["path"],
                product_id=d["product_id"],
                interface_number=d.get("interface_number", -1),
                usage_page=d.get("usage_page", 0),
                usage=d.get("usage", 0),
                # The O of RØDE arrives as U+FFFD when hidapi decodes the string.
                product_string=(d.get("product_string") or "").replace("�", "O"),
                serial_number=d.get("serial_number") or "",
            )
        )
    diagnostics.slow("hid.enumerate.slow", time.monotonic() - at,
                     "listing the RODE interfaces blocked", found=len(out))
    return out


def find_control_interface(
    prefer_pid: Optional[int] = None,
    prefer_interface: Optional[int] = None,
) -> Optional[RodeInterface]:
    """The MI_09 vendor control interface, or None. Callers can override with a specific product id
    or interface number; nothing else is ever returned by default.

    MI_09 IS THE ONLY INTERFACE THIS PROTOCOL WORKS OVER, and it is on the desk's USB 1 port alone
    (the one nearest the power inlet). USB 2 presents its own device, PID 0x0026 MI_03, which is a
    control interface by shape and silent in fact: opened on hardware with both cables in,
    `get_version` returns '' and `ping` returns False.

    SO THERE IS NO FALLBACK TO "the first control interface we found". That fallback hands MI_03 to
    any caller with only USB 2 plugged in, and every read then returns nothing while the app looks
    connected, which presents as a freeze. None is the honest answer, and every caller handles it.

    Diagnosing WHICH kind of nothing (no desk at all, or a desk on the wrong port) is
    `server.read_device_status`'s job, because that is a message for a person rather than a value
    for a caller.
    """
    candidates = [i for i in enumerate_interfaces() if i.is_control]
    if not candidates:
        return None

    if prefer_pid is not None:
        for i in candidates:
            if i.product_id == prefer_pid:
                return i
    if prefer_interface is not None:
        for i in candidates:
            if i.interface_number == prefer_interface:
                return i

    for i in candidates:
        if i.interface_number == 9:
            return i
    return None


# ---- OPENING THE DEVICE, IN ONE PLACE ------------------------------------------------------
# Every handle the app opens goes through these three, so the diagnostic log can say who holds
# the device and for how long. That matters more than it sounds: a desk that has stopped answering
# is indistinguishable from a handle nobody closed, and a call site that opens its own
# `hid.device()` leaves nowhere to find out which it is.
#
# `purpose` is what appears in the log - name it after the JOB, not the function ("model dump",
# "pad create", "announcer"), because the log is read by somebody asking what the app was doing.

def open_handle(iface: RodeInterface, purpose: str):
    """Open a raw handle on `iface` and record it. The caller owns closing it."""
    dev = hid.device()
    dev.open_path(iface.path)
    diagnostics.event("hid.open", purpose, pid=f"0x{iface.product_id:04x}",
                      interface=f"MI_{iface.interface_number:02d}")
    return dev


def close_handle(dev, purpose: str = "") -> None:
    """Close a handle opened by open_handle. Never raises: a failed close is logged rather than
    thrown, because every existing call site closes in a `finally` and must keep doing so."""
    try:
        dev.close()
    except Exception as exc:
        diagnostics.event("hid.close.failed", purpose, error=f"{exc}")
        return
    diagnostics.event("hid.close", purpose)


@contextlib.contextmanager
def handle(iface: RodeInterface, purpose: str):
    """open_handle plus a guaranteed close, for a caller that wants one for a single operation."""
    dev = open_handle(iface, purpose)
    try:
        yield dev
    finally:
        close_handle(dev, purpose)


# ---- WAITING OUT A RE-ENUMERATION ----------------------------------------------------------
# Reconfiguring the desk's USB audio function takes it off the bus for about two and a half
# seconds, and it returns under a DIFFERENT product id and path, because the id encodes that
# configuration. Measured on a Pro II, fw 1.7.6: gone 60 ms after the write, back at t+2.5 s.
REENUM_EXPECT_S = 1.0        # how long to watch for it to LEAVE before deciding it never will
REENUM_TIMEOUT_S = 20.0      # how long to then wait for it to come BACK
REENUM_SETTLE_S = 0.3        # ... and a breath after that, so the next write does not race it


def _path_opens(iface: RodeInterface) -> bool:
    """Whether a handle can still be had on this exact path. A liveness probe, not a job.

    Deliberately NOT through open_handle: it runs many times a second and would bury the
    diagnostic log in opens and closes nobody asked a question about."""
    try:
        dev = hid.device()
        dev.open_path(iface.path)
    except Exception:
        return False
    try:
        dev.close()
    except Exception:
        pass
    return True


def wait_for_reenumeration(before: Optional[RodeInterface],
                           expect_s: float = REENUM_EXPECT_S,
                           timeout_s: float = REENUM_TIMEOUT_S) -> bool:
    """Block until the desk is back on the bus after a write that reconfigures its USB audio.

    WITHOUT THIS, THE WRITES THAT FOLLOW ARE SENT INTO THE GAP AND LOST. A profile restore
    measured 27 settings failing with "no control interface" in the 1.2 s after it set
    `outputMultiMode`, and they are reported to the user as though the desk had been unplugged.

    `before` is the interface as it was BEFORE the write, and watching that exact path die is how
    a real re-enumeration is told from a write the desk ignored: writing a value it already holds
    changes nothing, so a desk still answering after `expect_s` is not going anywhere and this
    returns at once. Returns False only on `timeout_s` elapsing with nothing back, which is the
    dropout the desk does not recover from without a replug.

    The caller should hold whatever device lock it already has across this. Nothing else could
    have succeeded during the gap anyway, and letting other work start against an absent desk is
    how the lost writes happen in the first place.
    """
    at = time.monotonic()
    if before is not None:
        while _path_opens(before):
            if time.monotonic() - at >= expect_s:
                return True                     # it never left: no re-enumeration to wait out
            time.sleep(0.05)
    while time.monotonic() - at < timeout_s:
        iface = find_control_interface()
        if iface is not None and _path_opens(iface):
            time.sleep(REENUM_SETTLE_S)
            diagnostics.event("device.reenumerated", "back on the bus",
                              took=f"{time.monotonic() - at:.1f}s",
                              pid=f"0x{iface.product_id:04x}")
            return True
        time.sleep(0.2)
    diagnostics.event("device.reenumerate_timeout", "the desk did not come back",
                      waited=f"{timeout_s:.0f}s")
    return False


class RodeCaster:
    """A thin, safe wrapper around one open control interface.

    Usage:
        with RodeCaster.open() as dev:
            print(dev.get_version())

    Only read-only helpers (get_version, get_info, ping) are implemented as
    named methods. Raw command/response is available via command() for
    exploration, but this class never sends an unknown opcode on its own.
    """

    def __init__(self, device: "hid.device", iface: RodeInterface, purpose: str = "probe"):
        self._dev = device
        self.iface = iface
        self.purpose = purpose      # what the diagnostic log calls this handle

    # ----- lifecycle -------------------------------------------------------

    @classmethod
    def open(
        cls,
        prefer_pid: Optional[int] = None,
        prefer_interface: Optional[int] = None,
        purpose: str = "probe",
    ) -> "RodeCaster":
        iface = find_control_interface(prefer_pid, prefer_interface)
        if iface is None:
            raise RuntimeError(
                "No RODECaster control interface found. Is the unit connected "
                "and powered on?"
            )
        return cls.open_path(iface, purpose)

    @classmethod
    def open_path(cls, iface: RodeInterface, purpose: str = "probe") -> "RodeCaster":
        return cls(open_handle(iface, purpose), iface, purpose)

    def close(self) -> None:
        close_handle(self._dev, self.purpose)

    def __enter__(self) -> "RodeCaster":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ----- transport -------------------------------------------------------

    def command(self, payload: bytes, timeout_ms: int = 1000) -> bytes:
        """Send a command (Report ID 1) and return the raw response bytes.

        `payload` is the command bytes WITHOUT the leading report id; it is
        padded out to a full 64-byte report. The returned value is the raw
        response report (including its leading report id byte 0x02).
        """
        if len(payload) > CMD_REPORT_LEN - 1:
            raise ValueError(
                f"command payload too long ({len(payload)} > {CMD_REPORT_LEN - 1})"
            )
        report = bytes([REPORT_CMD]) + payload
        report = report + bytes(CMD_REPORT_LEN - len(report))
        diagnostics.event("send.cmd", self.to_ascii(payload) or payload.hex(),
                          by=self.purpose)
        self._dev.write(report)
        self._dev.set_nonblocking(False)
        resp = self._dev.read(CMD_REPORT_LEN, timeout_ms=timeout_ms)
        return bytes(resp)

    @staticmethod
    def parse_response(resp: bytes) -> tuple[bool, bytes]:
        """Split a response into (is_ack, data-after-the-ack-byte)."""
        if len(resp) >= 2 and resp[0] == REPORT_RESP and resp[1] == ACK:
            return True, resp[2:]
        return False, resp

    @staticmethod
    def to_ascii(data: bytes) -> str:
        """Printable ASCII view of a buffer, '.' for non-printable, trailing NULs trimmed."""
        trimmed = data.rstrip(b"\x00")
        return "".join(chr(b) if 32 <= b < 127 else "." for b in trimmed)

    # ----- known read-only commands ---------------------------------------

    def get_version(self) -> str:
        """Per-session version token, e.g. '97B7-DECE'. Read-only, and NOT sent by the app.

        The token changes across a reboot, so it identifies nothing; the USB serial does. Worse,
        with the desk in Expanded this command makes it drop the virtual `System (RODECaster)`
        playback endpoint about 17 seconds later, which costs a screen-reader user their speech
        for a few seconds. Kept for the command-line probe only. (The single byte 0x57 'W' from
        older references returns nothing on firmware 1.7.x.)
        """
        ok, data = self.parse_response(self.command(b"VER"))
        return self.to_ascii(data) if ok else ""

    def get_info(self) -> bytes:
        """Status/info bytes (meaning still being decoded). Read-only."""
        ok, data = self.parse_response(self.command(b"\x49"))
        return data if ok else b""

    def ping(self) -> bool:
        """True if the device answers with an ACK. Read-only, and NOT sent by the app: like `VER`,
        in Expanded it drops the virtual playback endpoint about 17 seconds later. `get_info`
        answering proves the desk is responding without that cost."""
        resp = self.command(b"\x55")
        return len(resp) >= 2 and resp[0] == REPORT_RESP and resp[1] == ACK


def quick_probe(verbose: bool = True) -> dict:
    """Run the three documented read-only commands and return a result dict.

    Changes nothing on the device, but it is a COMMAND-LINE TOOL, not an app path: `VER` and the
    ping each make a desk in Expanded drop its virtual playback endpoint about 17 seconds later,
    so running this costs a screen reader on that output a few seconds of speech.
    """
    result: dict = {"interfaces": [], "control": None, "version": None,
                    "info_hex": None, "ping": None, "error": None}
    ifaces = enumerate_interfaces()
    result["interfaces"] = [i.describe() for i in ifaces]
    if verbose:
        print("RODE interfaces found:")
        for i in ifaces:
            print("  " + i.describe())
    iface = find_control_interface()
    if iface is None:
        result["error"] = "no control interface"
        if verbose:
            print("No control interface found.")
        return result
    result["control"] = iface.describe()
    if verbose:
        print(f"\nUsing control interface: {iface.describe()}\n")
    try:
        with RodeCaster.open_path(iface) as dev:
            ver = dev.get_version()
            result["version"] = ver
            if verbose:
                print(f"  get_version(): {ver!r}")
            time.sleep(0.05)
            info = dev.get_info()
            result["info_hex"] = info.hex()
            if verbose:
                print(f"  get_info():    {info[:16].hex()} ...")
            time.sleep(0.05)
            p = dev.ping()
            result["ping"] = p
            if verbose:
                print(f"  ping():        {p}")
    except Exception as exc:  # surfaced to caller
        result["error"] = str(exc)
        if verbose:
            print(f"  ERROR: {exc}")
    return result


if __name__ == "__main__":
    quick_probe()
