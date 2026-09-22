"""
AccessCaster - USB capture analyzer.

Purpose: once we capture RODE Central talking to the RODECaster (with Wireshark
+ USBPcap on Windows), this tool pulls the raw USB HID reports back out of the
.pcapng, sorts host->device commands from device->host responses, and diffs two
captures so we can isolate the exact command that, for example, assigns a sound
to a SMART pad.

It reads .pcapng files directly in pure Python (no tshark needed) for the
USBPcap link-layer type. If you prefer, it can also read a tshark JSON export.

The RODECaster control protocol (see rcp_hid.py) puts the report id in the
first data byte:
    0x01  host -> device  command       (e.g. 'VER', mute, pad assign command)
    0x02  device -> host  response/ACK
    0x03  host -> device  bulk data      (audio upload to a pad, most likely)
    0x04  device -> host  bulk response

Typical workflow:

    # See everything the host sent, grouped:
    python capture_analyze.py summary assign_pad.pcapng

    # Just the control commands (report id 1) the host sent, de-duplicated:
    python capture_analyze.py commands assign_pad.pcapng

    # Compare two captures to find what is different. Capture A assigns sound
    # X to pad 1; capture B assigns sound Y to pad 2. The reports that differ
    # are the pad-assign command and the audio payload:
    python capture_analyze.py diff assign_pad1.pcapng assign_pad2.pcapng

    # Prove the parser works on this machine with a synthetic capture:
    python capture_analyze.py selftest
"""

from __future__ import annotations

import json
import struct
import sys
from dataclasses import dataclass
from typing import Iterator, Optional


RODE_VENDOR_ID = 0x19F7

# pcap/pcapng link-layer type for USBPcap captures.
LINKTYPE_USBPCAP = 249

# pcapng block types we care about.
BT_SECTION_HEADER = 0x0A0D0D0A
BT_INTERFACE_DESC = 0x00000001
BT_ENHANCED_PACKET = 0x00000006
BT_SIMPLE_PACKET = 0x00000003


@dataclass
class UsbPacket:
    """One USB transfer extracted from a capture."""

    frame: int
    from_device: bool        # True = device -> host (IN), False = host -> device (OUT)
    endpoint: int
    device_addr: int
    transfer_type: int       # 0 iso, 1 interrupt, 2 control, 3 bulk
    data: bytes              # the URB data payload (the HID report bytes)

    @property
    def report_id(self) -> Optional[int]:
        return self.data[0] if self.data else None

    @property
    def direction_str(self) -> str:
        return "IN  device->host" if self.from_device else "OUT host->device"

    def hex(self, limit: int = 64) -> str:
        return self.data[:limit].hex()

    def ascii(self, limit: int = 64) -> str:
        d = self.data[:limit].rstrip(b"\x00")
        return "".join(chr(b) if 32 <= b < 127 else "." for b in d)


# ---------------------------------------------------------------------------
# pcapng reader (pure Python)
# ---------------------------------------------------------------------------

def _read_pcapng_blocks(raw: bytes) -> Iterator[tuple[int, bytes, str]]:
    """Yield (block_type, block_body, endian) for each block in a pcapng file.

    block_body excludes the 8-byte header (type + total length) and the
    trailing length field.
    """
    off = 0
    endian = "<"  # resolved from the section header magic
    n = len(raw)
    while off + 8 <= n:
        block_type = struct.unpack_from("<I", raw, off)[0]
        if block_type == BT_SECTION_HEADER:
            # Byte-order magic decides endianness for this section.
            magic = struct.unpack_from("<I", raw, off + 8)[0]
            endian = "<" if magic == 0x1A2B3C4D else ">"
        total_len = struct.unpack_from(endian + "I", raw, off + 4)[0]
        if total_len < 12 or off + total_len > n:
            break
        body = raw[off + 8: off + total_len - 4]
        yield block_type, body, endian
        off += total_len


def _parse_usbpcap_payload(payload: bytes) -> Optional[UsbPacket]:
    """Decode one USBPcap pseudo-header + data into a UsbPacket.

    USBPCAP_BUFFER_PACKET_HEADER layout (little-endian):
        uint16 headerLen
        uint64 irpId
        uint32 status
        uint16 function
        uint8  info        (bit0 set = PDO->FDO, i.e. device -> host)
        uint16 bus
        uint16 device
        uint8  endpoint
        uint8  transfer
        uint32 dataLength
    followed by dataLength bytes of payload.
    """
    if len(payload) < 27:
        return None
    header_len = struct.unpack_from("<H", payload, 0)[0]
    if header_len < 27 or header_len > len(payload):
        return None
    # Field offsets in the packed USBPCAP_BUFFER_PACKET_HEADER:
    # headerLen@0(2) irpId@2(8) status@10(4) function@14(2) info@16(1)
    # bus@17(2) device@19(2) endpoint@21(1) transfer@22(1) dataLength@23(4)
    info = payload[16]
    device = struct.unpack_from("<H", payload, 19)[0]
    endpoint = payload[21]
    transfer = payload[22]
    from_device = bool(info & 0x01) or bool(endpoint & 0x80)
    data = payload[header_len:]
    return UsbPacket(
        frame=0,
        from_device=from_device,
        endpoint=endpoint,
        device_addr=device,
        transfer_type=transfer,
        data=data,
    )


def read_pcapng(path: str) -> list[UsbPacket]:
    """Read a USBPcap .pcapng and return the USB packets that carry data."""
    with open(path, "rb") as f:
        raw = f.read()
    packets: list[UsbPacket] = []
    frame = 0
    link_type = None
    for block_type, body, endian in _read_pcapng_blocks(raw):
        if block_type == BT_INTERFACE_DESC:
            link_type = struct.unpack_from(endian + "H", body, 0)[0]
        elif block_type == BT_ENHANCED_PACKET:
            frame += 1
            # Enhanced packet block body: interface_id(4) ts_high(4) ts_low(4)
            # cap_len(4) orig_len(4) packet_data...
            cap_len = struct.unpack_from(endian + "I", body, 12)[0]
            pkt_data = body[20: 20 + cap_len]
            usb = _parse_usbpcap_payload(pkt_data)
            if usb and usb.data:
                usb.frame = frame
                packets.append(usb)
        elif block_type == BT_SIMPLE_PACKET:
            frame += 1
            cap_len = struct.unpack_from(endian + "I", body, 0)[0]
            pkt_data = body[4: 4 + cap_len]
            usb = _parse_usbpcap_payload(pkt_data)
            if usb and usb.data:
                usb.frame = frame
                packets.append(usb)
    if link_type is not None and link_type != LINKTYPE_USBPCAP:
        print(
            f"Note: interface link type is {link_type}, expected "
            f"{LINKTYPE_USBPCAP} (USBPcap). Parsing may be wrong.",
            file=sys.stderr,
        )
    return packets


# ---------------------------------------------------------------------------
# tshark JSON reader (optional alternative input)
# ---------------------------------------------------------------------------

def read_tshark_json(path: str) -> list[UsbPacket]:
    """Read a `tshark -T json` export. Extracts usb.capdata / usbhid.data."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        doc = json.load(f)
    packets: list[UsbPacket] = []
    for i, entry in enumerate(doc, 1):
        layers = entry.get("_source", {}).get("layers", {})
        usb = layers.get("usb", {})
        capdata = (
            layers.get("usbhid.data")
            or usb.get("usb.capdata")
            or layers.get("usb.capdata")
        )
        if not capdata:
            continue
        if isinstance(capdata, list):
            capdata = capdata[0]
        hexstr = capdata.replace(":", "").replace(" ", "")
        try:
            data = bytes.fromhex(hexstr)
        except ValueError:
            continue
        endpoint_raw = usb.get("usb.endpoint_address", "0")
        try:
            endpoint = int(str(endpoint_raw), 0)
        except ValueError:
            endpoint = 0
        src = str(usb.get("usb.src", ""))
        from_device = bool(endpoint & 0x80) or src.endswith(".0") is False and "host" not in src.lower()
        # usb.endpoint_address.direction is the reliable field when present.
        d = usb.get("usb.endpoint_address.direction") or usb.get("usb.dst")
        if d is not None:
            try:
                from_device = bool(int(str(d), 0)) if str(d) in ("0", "1") else from_device
            except ValueError:
                pass
        packets.append(
            UsbPacket(
                frame=i,
                from_device=bool(endpoint & 0x80),
                endpoint=endpoint,
                device_addr=int(str(usb.get("usb.device_address", "0")), 0)
                if str(usb.get("usb.device_address", "0")).isdigit() else 0,
                transfer_type=int(str(usb.get("usb.transfer_type", "0")), 0)
                if str(usb.get("usb.transfer_type", "0")).replace("0x", "").isalnum() else 0,
                data=data,
            )
        )
    return packets


# ---------------------------------------------------------------------------
# Classic libpcap reader (USBPcapCMD writes classic .pcap, not pcapng)
# ---------------------------------------------------------------------------

# Classic pcap global-header magic numbers (and their byte-swapped forms).
PCAP_MAGIC_US = 0xA1B2C3D4   # microsecond timestamps
PCAP_MAGIC_NS = 0xA1B23C4D   # nanosecond timestamps


def read_pcap(path: str) -> list[UsbPacket]:
    """Read a classic libpcap (.pcap) USBPcap capture.

    USBPcapCMD.exe writes this format, not pcapng. Layout:
        global header (24 bytes): magic, ver_major, ver_minor, thiszone,
                                  sigfigs, snaplen, network (linktype)
        then records: ts_sec, ts_usec, incl_len, orig_len, then incl_len bytes.
    The per-record data is the same USBPcap pseudo-header + payload that the
    pcapng path decodes, so we reuse _parse_usbpcap_payload.
    """
    with open(path, "rb") as f:
        raw = f.read()
    if len(raw) < 24:
        return []
    magic = struct.unpack_from("<I", raw, 0)[0]
    if magic in (PCAP_MAGIC_US, PCAP_MAGIC_NS):
        endian = "<"
    elif struct.unpack_from(">I", raw, 0)[0] in (PCAP_MAGIC_US, PCAP_MAGIC_NS):
        endian = ">"
    else:
        print(f"Note: {path} is not a classic pcap (magic 0x{magic:08x}).",
              file=sys.stderr)
        endian = "<"
    link_type = struct.unpack_from(endian + "I", raw, 20)[0]
    if link_type != LINKTYPE_USBPCAP:
        print(f"Note: {path} link type is {link_type}, expected "
              f"{LINKTYPE_USBPCAP} (USBPcap). Parsing may be wrong.",
              file=sys.stderr)
    packets: list[UsbPacket] = []
    off = 24
    frame = 0
    n = len(raw)
    while off + 16 <= n:
        incl_len = struct.unpack_from(endian + "I", raw, off + 8)[0]
        off += 16
        if off + incl_len > n:
            break
        frame += 1
        usb = _parse_usbpcap_payload(raw[off:off + incl_len])
        if usb and usb.data:
            usb.frame = frame
            packets.append(usb)
        off += incl_len
    return packets


def _looks_like_pcapng(raw_head: bytes) -> bool:
    return len(raw_head) >= 4 and struct.unpack_from("<I", raw_head, 0)[0] == BT_SECTION_HEADER


def load_capture(path: str) -> list[UsbPacket]:
    """Load a capture from .pcapng/.pcap (USBPcap) or .json (tshark)."""
    if path.lower().endswith(".json"):
        return read_tshark_json(path)
    # Decide pcap vs pcapng by content, not extension: USBPcapCMD writes classic
    # pcap, tshark writes pcapng, and either can carry a .pcap/.pcapng name.
    with open(path, "rb") as f:
        head = f.read(4)
    if _looks_like_pcapng(head):
        return read_pcapng(path)
    return read_pcap(path)


# ---------------------------------------------------------------------------
# Analysis commands
# ---------------------------------------------------------------------------

def cmd_summary(path: str) -> None:
    pkts = load_capture(path)
    out = [p for p in pkts if not p.from_device]
    inp = [p for p in pkts if p.from_device]
    print(f"{path}: {len(pkts)} data packets ({len(out)} OUT host->device, "
          f"{len(inp)} IN device->host)\n")

    def group(pkts: list[UsbPacket], label: str) -> None:
        by_rid: dict[int, list[UsbPacket]] = {}
        for p in pkts:
            by_rid.setdefault(p.report_id if p.report_id is not None else -1, []).append(p)
        print(f"  {label} by report id:")
        for rid in sorted(by_rid):
            ps = by_rid[rid]
            lens = sorted({len(p.data) for p in ps})
            print(f"    report 0x{rid:02x}: {len(ps):4d} packets, lengths {lens}")
        print()

    group(out, "OUT (commands the host sent)")
    group(inp, "IN  (responses the device sent)")


def cmd_commands(path: str) -> None:
    """Show distinct report-id-1 control commands the host sent, in order."""
    pkts = load_capture(path)
    seen: set[str] = set()
    print(f"Distinct host->device control commands (report id 0x01) in {path}:\n")
    n = 0
    for p in pkts:
        if p.from_device or p.report_id != 0x01:
            continue
        key = p.data.hex()
        if key in seen:
            continue
        seen.add(key)
        n += 1
        print(f"  [{p.frame:5d}] {p.hex(32)}   {p.ascii(32)!r}")
    print(f"\n{n} distinct commands.")


def rode_signature_count(pkts: list[UsbPacket]) -> int:
    """Count packets that are unmistakably RODECaster control traffic.

    Used to pick the right USBPcap root hub: a hub capture that contains our
    'VER' commands or '02 41' (ACK) responses is the one the unit is on.
    """
    n = 0
    for p in pkts:
        if p.from_device and p.report_id == 0x02 and len(p.data) >= 2 and p.data[1] == 0x41:
            n += 1
        elif (not p.from_device) and p.report_id == 0x01 and p.data[1:4] == b"VER":
            n += 1
    return n


def cmd_rode(path: str) -> int:
    """Report whether a capture contains RODECaster control traffic.

    Prints a count and returns process exit code 0 if found, 1 if not, so a
    shell can test which hub capture caught the unit.
    """
    pkts = load_capture(path)
    n = rode_signature_count(pkts)
    addrs = sorted({p.device_addr for p in pkts
                    if (p.from_device and p.report_id == 0x02
                        and len(p.data) >= 2 and p.data[1] == 0x41)})
    print(f"{path}: {len(pkts)} packets, {n} RODECaster control packets, "
          f"device addrs {addrs}")
    return 0 if n > 0 else 1


def cmd_props(path: str) -> None:
    """Extract ASCII property names and the bytes around them.

    The RODECaster config protocol (Transfer Mode) carries named properties in
    plain ASCII inside its bulk reports, e.g. 'encoderColour', 'transferModeType'.
    This prints each distinct name with the report id, direction, and a window of
    bytes from the name onward (the value usually follows the name + a small
    header), so we can map the config vocabulary and start decoding values.
    """
    import re as _re
    pkts = load_capture(path)
    seen: dict[str, tuple] = {}
    order: list[str] = []
    for p in pkts:
        for m in _re.finditer(rb"[A-Za-z][A-Za-z0-9_]{3,}", p.data):
            name = m.group().decode()
            # window: from the byte before the name to 20 bytes after it
            start = max(0, m.start() - 2)
            end = min(len(p.data), m.end() + 20)
            window = p.data[start:end]
            key = name
            if key not in seen:
                seen[key] = (p.report_id, p.from_device, p.frame, window, m.start())
                order.append(key)
    print(f"Property/ASCII names in {path} ({len(order)} distinct):\n")
    for name in order:
        rid, fromdev, frame, window, off = seen[name]
        direction = "IN " if fromdev else "OUT"
        print(f"  {name!r}")
        print(f"      rid=0x{rid:02x} {direction} frame={frame} name_off={off}")
        print(f"      ctx={window.hex()}")


def cmd_bulk(path: str) -> None:
    """Summarize bulk (report id 3) host->device traffic, i.e. likely audio upload."""
    pkts = load_capture(path)
    bulk = [p for p in pkts if not p.from_device and p.report_id == 0x03]
    total = sum(len(p.data) for p in bulk)
    print(f"Bulk OUT (report id 0x03) in {path}: {len(bulk)} packets, "
          f"{total} bytes total (~{total/1024:.1f} KiB).")
    if bulk:
        print(f"  first frame {bulk[0].frame}, last frame {bulk[-1].frame}")
        print(f"  first packet head: {bulk[0].hex(16)}")


def _command_set(pkts: list[UsbPacket]) -> dict[str, UsbPacket]:
    return {p.data.hex(): p for p in pkts if not p.from_device and p.report_id == 0x01}


def cmd_diff(path_a: str, path_b: str) -> None:
    """Diff the report-id-1 commands between two captures.

    Capture A and B should differ in exactly one intended action (e.g. assign
    sound to pad 1 vs pad 2). The commands unique to each capture are the
    candidates for the action-specific command.
    """
    a = _command_set(load_capture(path_a))
    b = _command_set(load_capture(path_b))
    only_a = [a[k] for k in a if k not in b]
    only_b = [b[k] for k in b if k not in a]
    print(f"A = {path_a}  ({len(a)} distinct commands)")
    print(f"B = {path_b}  ({len(b)} distinct commands)")
    print(f"\nCommands only in A ({len(only_a)}):")
    for p in only_a:
        print(f"  {p.hex(32)}   {p.ascii(32)!r}")
    print(f"\nCommands only in B ({len(only_b)}):")
    for p in only_b:
        print(f"  {p.hex(32)}   {p.ascii(32)!r}")
    print("\nByte-level diff of first unique command in each (if same length):")
    if only_a and only_b and len(only_a[0].data) == len(only_b[0].data):
        da, db = only_a[0].data, only_b[0].data
        diffs = [(i, da[i], db[i]) for i in range(len(da)) if da[i] != db[i]]
        for i, x, y in diffs:
            print(f"  byte {i:3d}: A=0x{x:02x} B=0x{y:02x}")
        if not diffs:
            print("  (identical)")


# ---------------------------------------------------------------------------
# Self test: build a tiny synthetic USBPcap pcapng and parse it back.
# ---------------------------------------------------------------------------

def _build_usbpcap_payload(from_device: bool, endpoint: int, data: bytes) -> bytes:
    header_len = 27
    info = 0x01 if from_device else 0x00
    hdr = struct.pack(
        "<HQIHBHHBBI",
        header_len,      # headerLen
        0,               # irpId
        0,               # status
        0x0009,          # function
        info,            # info
        0,               # bus
        5,               # device
        endpoint,        # endpoint
        1,               # transfer (interrupt)
        len(data),       # dataLength
    )
    return hdr + data


def _build_pcapng(packets: list[tuple[bool, int, bytes]]) -> bytes:
    def block(btype: int, body: bytes) -> bytes:
        total = 12 + len(body)
        pad = (-len(body)) % 4
        total += pad
        return struct.pack("<II", btype, total) + body + b"\x00" * pad + struct.pack("<I", total)

    # Section header block.
    shb_body = struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1)
    out = block(BT_SECTION_HEADER, shb_body)
    # Interface description block: linktype USBPCAP, snaplen 0.
    idb_body = struct.pack("<HHI", LINKTYPE_USBPCAP, 0, 0)
    out += block(BT_INTERFACE_DESC, idb_body)
    # Enhanced packet blocks.
    for from_device, endpoint, data in packets:
        payload = _build_usbpcap_payload(from_device, endpoint, data)
        epb_body = struct.pack("<IIIII", 0, 0, 0, len(payload), len(payload)) + payload
        out += block(BT_ENHANCED_PACKET, epb_body)
    return out


def cmd_selftest() -> None:
    print("Building a synthetic USBPcap pcapng and parsing it back...\n")
    synthetic = [
        (False, 0x01, b"\x01VER" + b"\x00" * 60),      # host->device command
        (True, 0x81, b"\x02A" + b"97B7-DECE" + b"\x00" * 51),  # device->host resp
        (False, 0x01, b"\x01\x49" + b"\x00" * 61),     # host->device INFO
        (False, 0x03, b"\x03" + bytes(range(64))),     # host->device bulk
    ]
    blob = _build_pcapng(synthetic)
    import tempfile, os
    fd, p = tempfile.mkstemp(suffix=".pcapng")
    os.close(fd)
    with open(p, "wb") as f:
        f.write(blob)
    try:
        pkts = read_pcapng(p)
        assert len(pkts) == 4, f"expected 4 packets, got {len(pkts)}"
        out = [x for x in pkts if not x.from_device]
        inp = [x for x in pkts if x.from_device]
        assert len(out) == 3 and len(inp) == 1, (len(out), len(inp))
        assert out[0].report_id == 0x01 and out[0].data[1:4] == b"VER", out[0].hex()
        assert inp[0].report_id == 0x02 and inp[0].ascii().startswith(".A97B7-DECE"), inp[0].ascii()
        assert out[2].report_id == 0x03 and len(out[2].data) == 65, len(out[2].data)
        print("  parsed 4 packets correctly:")
        for x in pkts:
            print(f"    frame {x.frame} {x.direction_str} rid=0x{x.report_id:02x} "
                  f"{x.hex(12)} {x.ascii(16)!r}")
        print("\nSELFTEST PASSED.")
    finally:
        os.remove(p)


# ---------------------------------------------------------------------------

USAGE = """AccessCaster USB capture analyzer

Commands:
  summary  <capture>            counts of OUT/IN reports by report id
  commands <capture>            distinct report-id-1 commands the host sent
  bulk     <capture>            summary of report-id-3 bulk (audio) traffic
  rode     <capture>            does this capture contain RODECaster traffic?
  diff     <captureA> <captureB>  commands unique to each + byte diff
  selftest                      verify the pcapng parser on synthetic data

<capture> may be a USBPcap .pcapng or a tshark -T json export (.json).
"""


def main(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 0
    cmd = argv[0]
    try:
        if cmd == "summary":
            cmd_summary(argv[1])
        elif cmd == "commands":
            cmd_commands(argv[1])
        elif cmd == "bulk":
            cmd_bulk(argv[1])
        elif cmd == "rode":
            return cmd_rode(argv[1])
        elif cmd == "props":
            cmd_props(argv[1])
        elif cmd == "diff":
            cmd_diff(argv[1], argv[2])
        elif cmd == "selftest":
            cmd_selftest()
        else:
            print(USAGE)
            return 1
    except IndexError:
        print(USAGE)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
