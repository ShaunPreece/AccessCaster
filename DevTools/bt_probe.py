"""bt_probe.py - interactive Bluetooth pairing/connection probe.

RUN THIS WITH SHAUN + HIS iPHONE PRESENT. The iPhone is the oracle: does it discover / pair /
connect, and does audio flow. Every action reads the Bluetooth properties back so we can see the
model change even before audio is confirmed.

Why this tool exists: the "Bluetooth is read-only" verdict was an INFERENCE (from the WiFi
write-ignored result + the touch-not-on-USB finding), never an observed failed BT write. The
btDo* properties are ACTION verbs, and on this device some actions IGNORE a report-3 config write
and only fire via the report-04 named-EVENT channel (proven for mute: cmutePressed). That event
channel is SELECTOR-FREE - it embeds the property name - so it sidesteps the unknown BT selector.
This tool drives the BT actions BOTH ways (event channel B, config channel A) with read-back.

Priority (Shaun's goal): make the unit DISCOVERABLE so the iPhone pairs TO it (audio in), then
mixer-out to the iPhone (existing routing). All operations are reversible; no factory reset.

Channels:
  --via=event  (default) : report-04 named-property injection (selector-free), the inject_button path
  --via=config           : report-3 config write via rcp_transfer (needs a selector; --sel=0xNN)

Usage:
  python bt_probe.py read                         # baseline: dump every BT property
  python bt_probe.py visible on  [--via=event|config] [--sel=0x0f]
  python bt_probe.py visible off [--via=event|config]
  python bt_probe.py scan        [--via=event|config]   # trigger btDoScan, then read btScan1..10
  python bt_probe.py pulse <name> [--via=event|config]  # generic action trigger (true then false)
  python bt_probe.py set <name> on|off [--via=event|config] [--sel=0x0f]   # generic state
  python bt_probe.py connect <addr>    [--via=event|config]   # btDoConnect (string)  [experimental]
  python bt_probe.py disconnect        [--via=event|config]   # btDoDisconnect        [experimental]
  python bt_probe.py pair <addr> [code] [--via=event|config]  # btDoPair (+btPairCode) [experimental]
  python bt_probe.py autoreconnect on|off                     # autoReconnect (config write)
"""
import sys
import time

import hid
import _bootstrap  # noqa: F401  - puts Project/ on sys.path
import rcp_read as rr
import rcp_transfer as t

# Bluetooth property names in the device model (state vs action). btScan1..10 are the scan-RESULT
# slots; btPairedNumber1..5 the remembered-device slots; the btDo* are the action verbs.
STATE_BOOL = ["btVisible", "autoReconnect", "btDoScan"]
STATE_STR = (["btConnectedAddress", "btConnectedType", "btPairCode"]
             + [f"btScan{i}" for i in range(1, 11)]
             + [f"btPairedNumber{i}" for i in range(1, 6)])


# ----------------------------------------------------------------------------------------------
# READ-BACK
# ----------------------------------------------------------------------------------------------

def read_bt() -> dict:
    """Read the whole model once and pull out every Bluetooth property (first value of each)."""
    blob = rr.read_model_stable()
    out = {}
    for name in STATE_BOOL + STATE_STR:
        vals = [v for v, k in rr.find_all(blob, name)]
        out[name] = vals[0] if vals else None
    return out


def show(label: str, data: dict = None) -> dict:
    """Print the BT props that matter, compactly. Returns the data so callers can diff."""
    data = data if data is not None else read_bt()
    scans = [data.get(f"btScan{i}") for i in range(1, 11)]
    scans = [s for s in scans if s]
    paired = [data.get(f"btPairedNumber{i}") for i in range(1, 6)]
    paired = [p for p in paired if p]
    print(f"--- {label} ---")
    print(f"  visible={data.get('btVisible')!r}  autoReconnect={data.get('autoReconnect')!r}  "
          f"btDoScan={data.get('btDoScan')!r}")
    print(f"  connectedAddress={data.get('btConnectedAddress')!r}  "
          f"connectedType={data.get('btConnectedType')!r}  pairCode={data.get('btPairCode')!r}")
    print(f"  scanResults={scans or '(none)'}")
    print(f"  paired={paired or '(none)'}")
    return data


def diff(before: dict, after: dict):
    changed = [k for k in after if before.get(k) != after.get(k)]
    if changed:
        for k in changed:
            print(f"  CHANGED {k}: {before.get(k)!r} -> {after.get(k)!r}")
    else:
        print("  no change in any BT property")
    return changed


# ----------------------------------------------------------------------------------------------
# CHANNEL B - report-04 named-property EVENT injection (selector-free)
# Format proven for buttons (inject_button.py): 01 01 02 00 01 <name> 00 01 01 <flag>
# where the trailing 01 01 <flag> is a compact-bool (0x02 = true/on, 0x03 = false/off).
# ----------------------------------------------------------------------------------------------

def _send(payload: bytes) -> int:
    rec = bytes([t.REPORT_BULK_OUT]) + len(payload).to_bytes(4, "little") + payload
    rec = rec + bytes(t.BULK_LEN - len(rec))
    iface = t.find_control_interface()
    dev = hid.device(); dev.open_path(iface.path)
    try:
        return dev.write(rec)
    finally:
        dev.close()


def event_bool(name: str, on: bool) -> int:
    """Inject a named compact-bool EVENT (true/false). Used to SET a state (visible) or, paired
    with its opposite, to PULSE an action."""
    flag = 0x02 if on else 0x03
    payload = bytes([0x01, 0x01, 0x02, 0x00, 0x01]) + name.encode("ascii") + b"\x00" \
        + bytes([0x01, 0x01, flag])
    return _send(payload)


def event_pulse(name: str):
    """Fire an action verb the way a button press fires (down=true, then up=false)."""
    event_bool(name, True); time.sleep(0.15)
    event_bool(name, False); time.sleep(0.15)


def event_string(name: str, value: str) -> int:
    """EXPERIMENTAL: inject a named STRING event (no captured reference for this; best-effort
    using the config string value-block inside the event envelope)."""
    value_b = value.encode("ascii") + b"\x00"
    vblock = bytes([0x01, 1 + len(value_b), 0x05]) + value_b
    payload = bytes([0x01, 0x01, 0x02, 0x00, 0x01]) + name.encode("ascii") + b"\x00" + vblock
    return _send(payload)


# ----------------------------------------------------------------------------------------------
# CHANNEL A - report-3 config write (needs a selector; SEL_SYSTEM 0x0F is the system-global guess)
# ----------------------------------------------------------------------------------------------

def config_bool(name: str, on: bool, sel: int) -> int:
    return t.send_record(t.build_global_prop_bool(name, on, sel))


def config_string(name: str, value: str, sel: int) -> int:
    """Global string config write (no helper in rcp_transfer; built inline, same value-block as
    build_pad_prop_string but with the global 01 01 01 01 <sel> address)."""
    value_b = value.encode("ascii") + b"\x00"
    vblock = bytes([0x01, 1 + len(value_b), 0x05]) + value_b
    payload = bytes([0x01, 0x01, 0x01, 0x01, sel & 0xFF]) + name.encode("ascii") + b"\x00" + vblock
    rec = bytes([t.REPORT_BULK_OUT]) + len(payload).to_bytes(4, "little") + payload
    return t.send_record(rec + bytes(t.BULK_LEN - len(rec)))


# ----------------------------------------------------------------------------------------------
# DRIVER
# ----------------------------------------------------------------------------------------------

def _opt(args, key, default=None):
    for a in args:
        if a.startswith(key + "="):
            return a.split("=", 1)[1]
    return default


def main():
    args = sys.argv[1:]
    if not args or args[0] == "read":
        show("baseline")
        return

    cmd = args[0]
    via = _opt(args, "--via", "event")
    sel = int(_opt(args, "--sel", "0x0f"), 0)
    pos = [a for a in args[1:] if not a.startswith("--")]

    before = show("before")
    print(f">>> {cmd} (via {via})")

    if cmd == "visible":
        on = pos and pos[0] == "on"
        if via == "config":
            config_bool("btVisible", on, sel)
        else:
            event_bool("btVisible", on)          # set-and-hold (state), not a pulse
    elif cmd == "scan":
        if via == "config":
            config_bool("btDoScan", True, sel)
        else:
            event_pulse("btDoScan")
        time.sleep(3.0)                           # give the radio a moment to find devices
    elif cmd == "pulse":
        name = pos[0]
        if via == "config":
            config_bool(name, True, sel)
        else:
            event_pulse(name)
        time.sleep(2.0)
    elif cmd == "set":
        name, on = pos[0], (len(pos) > 1 and pos[1] == "on")
        if via == "config":
            config_bool(name, on, sel)
        else:
            event_bool(name, on)
    elif cmd == "connect":
        addr = pos[0]
        if via == "config":
            config_string("btDoConnect", addr, sel)
        else:
            event_string("btDoConnect", addr)
        time.sleep(2.0)
    elif cmd == "disconnect":
        if via == "config":
            config_string("btDoDisconnect", "1", sel)
        else:
            event_string("btDoDisconnect", "1")
        time.sleep(1.5)
    elif cmd == "pair":
        addr = pos[0]
        code = pos[1] if len(pos) > 1 else ""
        if code:
            if via == "config":
                config_string("btPairCode", code, sel)
            else:
                event_string("btPairCode", code)
            time.sleep(0.3)
        if via == "config":
            config_string("btDoPair", addr, sel)
        else:
            event_string("btDoPair", addr)
        time.sleep(2.5)
    elif cmd == "autoreconnect":
        on = pos and pos[0] == "on"
        config_bool("autoReconnect", on, t.SEL_SYSTEM)   # this one is a stored setting
    else:
        print(f"unknown command {cmd!r}; see the module docstring")
        return

    time.sleep(0.4)
    after = show("after")
    diff(before, after)
    print("ASK SHAUN: what does the iPhone show now (discoverable list / paired / connected)?")


if __name__ == "__main__":
    main()
