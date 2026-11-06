"""Run AccessCaster against a simulated RODECaster Pro II, to work on the window with no desk.

    ProjectBuilds\\.venv\\Scripts\\python.exe DevTools\\simulate_desk.py               # factory Pro II
    ProjectBuilds\\.venv\\Scripts\\python.exe DevTools\\simulate_desk.py --dump X.bin  # a saved dump
    python DevTools/simulate_desk.py --check     # self-check: the synthetic dump parses, no app

The app is the real one (`tray.main`); only `rcp_hid.bus` is replaced, so every enumeration, open,
read and write reaches the simulated desk and the real USB bus is never opened. A desk that is
plugged in stays untouched.

THE FACTORY PRO II IS BUILT FROM THREE COMMITTED FILES: the layout fixture (which nodes exist and
the properties each carries), docs/FACTORY_DEFAULTS_PRO2.txt (their values, instance by instance in
stream order) and docs/PROPERTY_LIST_PRO2.md (their types). A value the defaults withhold, or a
property the defaults' older firmware lacks, is empty or zero. A real dump (`--dump`) is the
faithful alternative, and carries the wifi password, so it never belongs in the repo.

WHAT THE DESK DOES: answers the model-dump trigger with a dump of its current model, applies every
property write so the next dump reads it back, and ACKs every report-1 command. Like the real desk,
a write to a property a node lacks appends it.

WHAT IT DOES NOT: push anything of its own (no knob, fader, meter or pad activity), act on a
request property (recording, firmware, shows, wifi scans and pad creation store the request and
nothing follows), or present audio endpoints to Windows, so the level meters see no desk.

ITS DATA DIR IS %LOCALAPPDATA%\\AccessCaster-Simulated, so profiles, backups and settings made
against it never mix with the real ones. Only one copy of AccessCaster runs at a time, so quit the
real one first: with a copy already running, this opens that copy's window instead.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import threading
from collections import Counter, deque

import _bootstrap  # puts Project/ on sys.path; import FIRST

DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                        "AccessCaster-Simulated")
# Set before the first Project import: paths.DATA_DIR is read once, at import.
os.environ.setdefault("ACCESSCASTER_DATA_DIR", DATA_DIR)

import model_tree  # noqa: E402
import rcp_hid  # noqa: E402

FIXTURE = "layout_pro2_1.7.7.json"
FIRMWARE = "1.7.7"                  # the fixture's firmware, which the synthetic desk reports
PRODUCT_ID = 0x0037                 # devices.PID_PRO2
SERIAL = "SIMULATED"
PATH = b"simulated-desk-MI_09"

# Nodes the factory dump holds below a root node. The layout fixture lists root nodes and
# PHYSICALINTERFACE's children only, so these are hung on the root node they follow in the dump.
CHILD_OF = {"STORAGEVOLUME": "SYSTEM", "WIFISCANRESULT": "NETWORK", "SHOW": "SHOWS",
            "RECORDING": "RECORDINGS", "RADIOTX": "RADIO", "SIPREGISTRATION": "SIPCALLING",
            "FXPRESET": "FXPRESETS"}


# ---- the synthetic model ---------------------------------------------------------------------

# One value in the defaults file: a quoted string, a composite, a withheld marker or a bare token,
# then an optional run count. A string ends at a quote that is followed by a count, a comma or the
# end of the line, because the file does not escape quotes inside one.
_TOKEN = re.compile(r'(".*?"(?=(?: \(x\d+\))?(?:, |$))|\[.*?\](?=(?: \(x\d+\))?(?:, |$))'
                    r'|<[^>]*>|[^,]+?)(?: \(x(\d+)\))?(?:, |$)')
_TYPE = re.compile(r"^- `([^`]+)` - ([a-z0-9/?]+), x\d+")


def _defaults() -> dict:
    """{block: {property: [value token per instance]}}, both in stream order."""
    blocks, block = {}, None
    with open(_bootstrap.docs_path("FACTORY_DEFAULTS_PRO2.txt"), encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    start = next(i for i, line in enumerate(lines) if line == "PHYSICALINTERFACE")
    for line in lines[start:]:
        if line and not line.startswith(" "):
            block = blocks.setdefault(line.strip(), {})
        elif line.startswith("  ") and " = " in line:
            name, text = line.strip().split(" = ", 1)
            tokens, i = [], 0
            while i < len(text):
                m = _TOKEN.match(text, i)
                if not m or m.end() == i:
                    raise SystemExit(f"FACTORY_DEFAULTS_PRO2.txt: cannot read {name} = {text!r}")
                tokens += [m.group(1)] * int(m.group(2) or 1)
                i = m.end()
            block[name] = tokens
    return blocks


def _types() -> dict:
    """{property: type as the property list names it}."""
    with open(_bootstrap.docs_path("PROPERTY_LIST_PRO2.md"), encoding="utf-8") as fh:
        return {m.group(1): m.group(2) for m in map(_TYPE.match, fh) if m}


def _zero(type_name: str):
    return {"bool": (False, "bool"), "int": (0, "int"), "int/int64": (0, "int"),
            "int64": (0, "int64"), "float": (0.0, "float"), "str": ("", "str")}.get(
                type_name, (None, "?"))


def _typed(token: str, type_name: str):
    """(value, kind) for one token of the defaults file. A withheld value reads as zero.

    A token that names its own type (a bool, a string, a composite) keeps it: a property's type is
    per occurrence, and the property list names one type for every occurrence on a later firmware.
    The list decides only what a bare number is."""
    if token.startswith("<"):
        return _zero(type_name)
    if token in ("True", "False"):
        return token == "True", "bool"
    if token.startswith('"'):
        # The parser hands UTF-8 over as latin-1, so the model holds it that way too.
        return token[1:-1].encode("utf-8").decode("latin1"), "str"
    if token.startswith("["):
        return ast.literal_eval(token), "pair"
    if type_name == "float" or "." in token or "e" in token:
        return float(token), "float"
    v = int(token)
    return v, "int64" if type_name == "int64" or not -2**31 <= v < 2**31 else "int"


def synthetic_model() -> model_tree.Node:
    """A factory Pro II as a model tree. See the module docstring for where each part comes from."""
    with open(os.path.join(_bootstrap.devtools_path("fixtures"), FIXTURE), encoding="utf-8") as fh:
        runs = json.load(fh)
    defaults, types = _defaults(), _types()
    taken = Counter()

    def value(block, prop):
        tokens = defaults.get(block, {}).get(prop, [])
        i = taken[block, prop]
        taken[block, prop] += 1
        type_name = types.get(prop, "?")
        return _typed(tokens[i], type_name) if i < len(tokens) else _zero(type_name)

    def ordered(block, props):
        known = list(defaults.get(block, {}))
        return sorted(props, key=lambda p: (known.index(p) if p in known else len(known), p))

    def build(parent, runs):
        for name, count, props, kids in runs:
            for _ in range(count):
                node = model_tree.Node(name)
                node.props = [(p, *value(name, p)) for p in ordered(name, props)]
                _adopt(parent, node)
                build(node, kids)

    root = model_tree.Node("Rodecaster")
    build(root, runs)
    for child, parent_name in CHILD_OF.items():
        parent = next(n for n in root.kids if n.name == parent_name)
        props = defaults.get(child, {})
        for i in range(max((len(v) for v in props.values()), default=0)):
            node = model_tree.Node(child)
            node.props = [(p, *value(child, p)) for p, tokens in props.items() if i < len(tokens)]
            _adopt(parent, node)
    system = next(n for n in root.kids if n.name == "SYSTEM")
    system.props = [(p, FIRMWARE, "str") if p == "systemFirmwareVersion" else (p, v, k)
                    for p, v, k in system.props]
    return root


def _adopt(parent: model_tree.Node, node: model_tree.Node) -> None:
    node.parent, node.index = parent, len(parent.kids)
    parent.kids.append(node)


# ---- the simulated desk and its bus ----------------------------------------------------------

class SimulatedDesk:
    """The desk behind the simulated bus: one model tree, and every open handle's input queue.

    HID input reports reach every open handle, as they do on Windows, so the app's listener sees
    each dump go past exactly as it does on a real desk."""

    def __init__(self, root: model_tree.Node, log=None):
        self.root = root
        self.log = log or (lambda text: None)
        self.lock = threading.Lock()
        self.handles: set = set()

    # The bus, as rcp_hid.bus calls it.
    def enumerate(self):
        return [rcp_hid.RodeInterface(path=PATH, product_id=PRODUCT_ID, interface_number=9,
                                      usage_page=rcp_hid.CONTROL_USAGE_PAGE,
                                      usage=rcp_hid.CONTROL_USAGE,
                                      product_string="RODECaster Pro II", serial_number=SERIAL)]

    def open(self, path: bytes):
        if path != PATH:
            raise OSError("open failed: no such simulated device")
        handle = SimulatedHandle(self)
        with self.lock:
            self.handles.add(handle)
        return handle

    # What a handle's write asks of the desk.
    def received(self, report: bytes) -> None:
        if report[:1] == bytes([rcp_hid.REPORT_CMD]):
            self._send(bytes([rcp_hid.REPORT_RESP, rcp_hid.ACK]).ljust(rcp_hid.CMD_REPORT_LEN,
                                                                       b"\x00"))
        elif report[:1] == bytes([rcp_hid.REPORT_BULK_OUT]):
            n = int.from_bytes(report[1:5], "little")
            payload = report[5:5 + n]
            if payload == bytes.fromhex("ad10a7b0"):
                self._dump()
            else:
                self._write(payload)

    def _dump(self) -> None:
        with self.lock:
            tree = b"\x02" + model_tree.encode_node(self.root)
        stream = len(tree).to_bytes(4, "little") + tree
        for i in range(0, len(stream), 255):
            self._send(b"\x04" + stream[i:i + 255].ljust(255, b"\x00"))

    def _write(self, payload: bytes) -> None:
        try:
            path, name, value, kind, _end = model_tree.parse_record(payload)
        except ValueError as exc:
            self.log(f"unreadable write ignored: {exc}: {payload.hex()}")
            return
        with self.lock:
            node = self.root
            for i in path:
                if i >= len(node.kids):
                    self.log(f"write to a node the model lacks ignored: {path} {name}={value!r}")
                    return
                node = node.kids[i]
            for j, (p, _v, _k) in enumerate(node.props):
                if p == name:
                    node.props[j] = (name, value, kind)
                    break
            else:
                node.props.append((name, value, kind))
                self.log(f"STRAY: {node.name}{path} had no {name}, appended as the desk would")
        self.log(f"write {node.name}{path} {name} = {value!r}")

    def _send(self, report: bytes) -> None:
        with self.lock:
            handles = list(self.handles)
        for h in handles:
            h.deliver(report)

    def closed(self, handle) -> None:
        with self.lock:
            self.handles.discard(handle)


class SimulatedHandle:
    """One open handle: hidapi's `write`, `read`, `set_nonblocking` and `close`."""

    def __init__(self, desk: SimulatedDesk):
        self.desk = desk
        self.queue: deque = deque()
        self.ready = threading.Condition()
        self.blocking = True
        self.open = True

    def deliver(self, report: bytes) -> None:
        with self.ready:
            self.queue.append(report)
            self.ready.notify_all()

    def write(self, report) -> int:
        if not self.open:
            raise OSError("write on a closed handle")
        report = bytes(report)
        self.desk.received(report)
        return len(report)

    def read(self, size: int, timeout_ms: int = -1):
        if not self.open:
            raise OSError("read on a closed handle")
        with self.ready:
            if not self.queue and self.blocking:
                self.ready.wait(None if timeout_ms is None or timeout_ms < 0
                                else timeout_ms / 1000.0)
            return list(self.queue.popleft()[:size]) if self.queue else []

    def set_nonblocking(self, flag) -> int:
        self.blocking = not flag
        return 0

    def close(self) -> None:
        self.open = False
        self.desk.closed(self)


# ---- running it ------------------------------------------------------------------------------

def _check() -> int:
    root = synthetic_model()
    desk = SimulatedDesk(root)
    rcp_hid.bus = desk
    import rcp_read
    blob = rcp_read.read_model_raw()
    parsed, _used, _tail = model_tree.parse(blob)
    nodes = sum(1 for _ in model_tree.walk(parsed))
    print(f"synthetic Pro II: {len(blob)} bytes, {nodes} nodes, firmware "
          f"{rcp_read.block_value(blob, 'SYSTEM', 'systemFirmwareVersion')}, "
          f"named {rcp_read.first(blob, 'systemName', 'str')!r}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dump", help="a saved model dump (.bin) to simulate instead of a factory "
                                   "Pro II")
    ap.add_argument("--check", action="store_true", help="build the desk, read one dump back "
                                                         "through the app's reader, and stop")
    args = ap.parse_args()
    if args.check:
        return _check()

    import diagnostics
    import instance
    if not instance.claim():
        # tray.main hands over to the running copy, which opens its window.
        print("AccessCaster is already running, so its window opens instead. If that copy is the "
              "real one, quit it from its tray icon and start this again.", flush=True)
        import tray
        return tray.main()
    if args.dump:
        with open(args.dump, "rb") as fh:
            root = model_tree.parse(fh.read())[0]
    else:
        root = synthetic_model()
    rcp_hid.bus = SimulatedDesk(root, lambda text: diagnostics.event("sim.desk", text))
    print(f"Simulated desk ready ({args.dump or 'factory Pro II'}); data in "
          f"{os.environ['ACCESSCASTER_DATA_DIR']}. Quit from the tray icon to stop.", flush=True)
    import tray
    return tray.main()


if __name__ == "__main__":
    sys.exit(main())
