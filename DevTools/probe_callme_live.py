"""CALLME, LIVE: switch call hosting, read the desk's call state, and watch what it pushes.

Every address comes off the live tree, so a write goes to a confirmed member of a confirmed node.
`sipCallHostingEnabled` sits on the SIPCALLING node, a root child, so its path is one index.

The guest link is the touchscreen's own template, `https://callme.rode.com/s?c=` followed by the
desk's `sipRodeCode`. The code is empty while hosting is off and is reissued each time hosting is
switched back on, so a link shared earlier stops working after an off/on.

    python DevTools/probe_callme_live.py state            read every sip* property (passwords masked)
    python DevTools/probe_callme_live.py host on|off      write hosting, read it back, show the link
    python DevTools/probe_callme_live.py watch [seconds]  print each sip* push as it arrives
    python DevTools/probe_callme_live.py speak [seconds]  the same, and speak call events aloud
    python DevTools/probe_callme_live.py answer [slot] [seconds]   answer the next call (-1 declines)
    python DevTools/probe_callme_live.py hangup <slot> <0|1> <0|1>  write the disconnect pair
    python DevTools/probe_callme_live.py dial <dest> <false|true|str>  place an outgoing call
"""
from __future__ import annotations
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401

import model_tree
import rcp_hid
import rcp_read
import rcp_transfer

GUEST_LINK = "https://callme.rode.com/s?c="
SECRET = ("Password",)


def tree():
    root, _c, _t = model_tree.parse(rcp_read.read_model_stable(fresh=True))
    return root


def locate(root, prop):
    """(path, kind, value) of the first occurrence of `prop`, found by walking the tree."""
    for n in model_tree.walk(root):
        for p, v, k in n.props:
            if p == prop:
                return n.path(), k, v
    return None, None, None


def show(name, val):
    if any(s in name for s in SECRET):
        val = "(set)" if val else "(empty)"
    return f"{name:30s} {val!r}"


def state(root=None):
    root = root or tree()
    for n in model_tree.walk(root):
        for p, v, _k in n.props:
            if p.startswith("sip"):
                print("  " + show(p, v))
    code = locate(root, "sipRodeCode")[2]
    print(f"\n  guest link: {GUEST_LINK + code if code else '(none: hosting is off)'}")


def host(on: bool):
    root = tree()
    path, kind, was = locate(root, "sipCallHostingEnabled")
    if path is None or kind != "bool" or len(path) != 1:
        raise SystemExit(f"sipCallHostingEnabled not where expected: path {path}, kind {kind}")
    print(f"sipCallHostingEnabled at path {path}: was {was}, writing {on}")
    rcp_transfer.send_record(rcp_transfer.build_path_prop_bool("sipCallHostingEnabled", on, path))
    # The code is issued by RODE's service over the network, so give it time to arrive.
    for _ in range(10):
        time.sleep(1.0)
        root = tree()
        got = locate(root, "sipCallHostingEnabled")[2]
        code = locate(root, "sipRodeCode")[2]
        if got == on and (bool(code) == on):
            break
    print(f"read back: hosting {got}, code {code!r}")
    print(f"guest link: {GUEST_LINK + code if code else '(none)'}")
    return 0 if got == on else 1


SAY = Path(__file__).resolve().parent.parent / "Project" / "say.ps1"


def say(text: str):
    subprocess.Popen(["powershell", "-NoProfile", "-File", str(SAY), "-Text", text, "-Force"])


def announce(name, val):
    text = val[0][0] if isinstance(val, list) and val and isinstance(val[0], tuple) else val
    if name == "sipIncomingCallDetails":
        # The clear arrives in the same instant as an answer or decline, which is announced on its
        # own, so only the arrival is spoken.
        if text:
            call = json.loads(text)
            # The model decoder reads strings as latin-1; the desk sends UTF-8 ("RØDE-CallMe").
            name_ = (call.get("peername") or "unknown caller").encode("latin1").decode("utf-8", "replace")
            say(f"Incoming CallMe call from {name_}")
    elif name == "sipIncomingCallAccept" and text:
        slot = json.loads(text).get("sip_slot", -1)
        say("Declined" if slot < 0 else "Answered, stop tapping")
    elif name == "sipSlotCallState":
        # Idle when nothing has happened yet, otherwise JSON: {"state": ..., "reason": ...}.
        try:
            st = json.loads(text)
            say(f"Call {st.get('state', '')}" + (f", {st['reason']}" if st.get("reason") else ""))
        except (ValueError, TypeError):
            say(f"Call {text}")


def watch(seconds: float, speak: bool = False):
    dev = rcp_hid.open_handle(rcp_hid.find_control_interface(), "callme watch")
    print(f"watching sip* pushes for {seconds:.0f} s")
    t0 = time.time()
    try:
        dev.set_nonblocking(False)
        while time.time() - t0 < seconds:
            rep = dev.read(rcp_read.BULK_LEN, timeout_ms=400)
            if not rep or rep[0] != 0x04:
                continue
            frame = bytes(rep)
            n = rcp_read._push_record_len(frame)
            if n is None:
                continue
            try:
                path, name, val, _k, _e = model_tree.parse_record(frame[5:5 + n])
            except ValueError:
                continue
            if name.startswith("sip") or name.startswith("icecast"):
                print(f"  +{time.time() - t0:6.1f}s {path} " + show(name, val), flush=True)
                if speak:
                    announce(name, val)
    except KeyboardInterrupt:
        pass
    finally:
        rcp_hid.close_handle(dev, "callme watch")


def _block(payload: bytes) -> bytes:
    """One value block: a length-prefixed count, then the type byte and its data."""
    return bytes([0x01, len(payload)]) + payload


def build_accept(value: str, path) -> bytes:
    """`sipIncomingCallAccept` as the touchscreen writes it: a COMPOSITE (type 0x08) of the JSON
    string and a FALSE bool. The string alone is stored and never acted on; with a TRUE bool the
    call is set up but its audio stream never opens, and it fails after 60 s as "rtp stream error"."""
    inner = _block(b"\x05" + value.encode("ascii") + b"\x00") + _block(b"\x03")
    payload = (rcp_transfer.build_address(list(path)) + b"sipIncomingCallAccept\x00"
               + _block(b"\x08" + inner))
    return rcp_transfer._frame(payload)


def hangup(slot: int, first: bool, second: bool):
    """Write `sipSlotCallDisconnect` on call slot `slot` (0-based) as a two-bool composite, then
    watch the slot's state for 6 s. A slot that has never carried a call reads (True, False)."""
    root = tree()
    slots = [n for n in model_tree.walk(root)
             if any(p == "sipSlotCallState" for p, _v, _k in n.props)]
    node = slots[slot]
    was = next(v for p, v, _k in node.props if p == "sipSlotCallDisconnect")
    print(f"slot {slot} at {node.path()}: sipSlotCallDisconnect was {was}, writing "
          f"({first}, {second})")
    inner = _block(b"\x02" if first else b"\x03") + _block(b"\x02" if second else b"\x03")
    payload = (rcp_transfer.build_address(node.path()) + b"sipSlotCallDisconnect\x00"
               + _block(b"\x08" + inner))
    rcp_transfer.send_record(rcp_transfer._frame(payload))
    for _ in range(6):
        time.sleep(1.0)
        n2 = next(n for n in model_tree.walk(tree()) if n.path() == node.path())
        props = {p: v for p, v, _k in n2.props}
        print(f"  state {props.get('sipSlotCallState')!r}, disconnect "
              f"{props.get('sipSlotCallDisconnect')}", flush=True)
    return 0


def dial(dest: str, shape: str, slot: int = 0, seconds: float = 20.0):
    """Write an outgoing call to `sipOutgoingCallDetails` and log what the desk pushes back.

    The template is the touchscreen's own, `{"dest":"...","sip_slot":N}`. `shape` is how the value
    is sent: "false" or "true" for a composite with that bool (the shape an answer takes), "str"
    for a plain string."""
    root = tree()
    path, kind, was = locate(root, "sipOutgoingCallDetails")
    if path is None or len(path) != 1:
        raise SystemExit(f"sipOutgoingCallDetails not where expected: path {path}")
    value = json.dumps({"dest": dest, "sip_slot": slot}, separators=(",", ":"))
    text = _block(b"\x05" + value.encode("ascii") + b"\x00")
    if shape == "str":
        block = text
    else:
        block = _block(b"\x08" + text + _block(b"\x02" if shape == "true" else b"\x03"))
    rec = rcp_transfer._frame(rcp_transfer.build_address(path) + b"sipOutgoingCallDetails\x00"
                              + block)
    print(f"sipOutgoingCallDetails at {path} ({kind}, was {was!r}): sending {value} as {shape}")
    dev = rcp_hid.open_handle(rcp_hid.find_control_interface(), "callme dial")
    t0 = time.time()
    try:
        dev.set_nonblocking(False)
        rcp_transfer.send_record(rec)
        while time.time() - t0 < seconds:
            rep = dev.read(rcp_read.BULK_LEN, timeout_ms=400)
            if not rep or rep[0] != 0x04:
                continue
            frame = bytes(rep)
            n = rcp_read._push_record_len(frame)
            if n is None:
                continue
            try:
                p, name, val, _k, _e = model_tree.parse_record(frame[5:5 + n])
            except ValueError:
                continue
            if name.startswith("sip") and "Bitrate" not in name and "Jitter" not in name:
                print(f"  +{time.time() - t0:5.1f}s {p} " + show(name, val), flush=True)
    finally:
        rcp_hid.close_handle(dev, "callme dial")
    after = locate(tree(), "sipOutgoingCallDetails")[2]
    print(f"sipOutgoingCallDetails now {after!r}")
    return 0


def answer(slot: int, seconds: float):
    """Wait for an incoming call and answer it into `slot`, or decline it with slot -1.

    The value is the touchscreen's own template, `{"id":<id>,"sip_slot":<slot>}`, with the id taken
    from the `sipIncomingCallDetails` push. An unanswered call clears itself after 120 s."""
    root = tree()
    path, kind, _v = locate(root, "sipIncomingCallAccept")
    # A plain string until the touchscreen first answers a call, a composite from then on.
    if path is None or kind not in ("str", "pair") or len(path) != 1:
        raise SystemExit(f"sipIncomingCallAccept not where expected: path {path}, kind {kind}")
    dev = rcp_hid.open_handle(rcp_hid.find_control_interface(), "callme answer")
    print(f"waiting up to {seconds:.0f} s for a call; will send slot {slot}", flush=True)
    t0 = time.time()
    call = None
    try:
        dev.set_nonblocking(False)
        while call is None and time.time() - t0 < seconds:
            rep = dev.read(rcp_read.BULK_LEN, timeout_ms=400)
            if not rep or rep[0] != 0x04:
                continue
            frame = bytes(rep)
            n = rcp_read._push_record_len(frame)
            if n is None:
                continue
            try:
                _p, name, val, _k, _e = model_tree.parse_record(frame[5:5 + n])
            except ValueError:
                continue
            if name == "sipIncomingCallDetails":
                text = val[0][0] if isinstance(val, list) else val
                if text:
                    call = json.loads(text)
    finally:
        rcp_hid.close_handle(dev, "callme answer")
    if call is None:
        print("no call arrived")
        return 1
    value = json.dumps({"id": call["id"], "sip_slot": slot}, separators=(",", ":"))
    print(f"call from {call.get('peername')!r} ({call.get('calltype')}), sending {value}", flush=True)
    rcp_transfer.send_record(build_accept(value, path))
    for _ in range(8):
        time.sleep(1.0)
        root = tree()
        states = [v for nd in model_tree.walk(root) for p, v, _k in nd.props
                  if p == "sipSlotCallState"]
        print(f"  slot states {states}", flush=True)
    return 0


def main(argv):
    if not argv or argv[0] == "state":
        state()
        return 0
    if argv[0] == "host" and len(argv) == 2 and argv[1] in ("on", "off"):
        return host(argv[1] == "on")
    if argv[0] == "dial" and len(argv) >= 3:
        return dial(argv[1], argv[2])
    if argv[0] == "hangup" and len(argv) == 4:
        return hangup(int(argv[1]), argv[2] == "1", argv[3] == "1")
    if argv[0] == "answer":
        return answer(int(argv[1]) if len(argv) > 1 else 0, float(argv[2]) if len(argv) > 2 else 300)
    if argv[0] in ("watch", "speak"):
        watch(float(argv[1]) if len(argv) > 1 else 120, speak=argv[0] == "speak")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
