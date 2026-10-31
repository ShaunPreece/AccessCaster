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
    python DevTools/probe_callme_live.py hangup <slot>    end the call on a 0-based call slot
    python DevTools/probe_callme_live.py dial <code>      call another desk's CallMe code

Every write is built by `callme`, the module the app sends them with.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401
import callme
import model_tree
import rcp_read
import rcp_transfer
from _desk import push_handle, pushes

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
    print(f"\n  guest link: {callme.GUEST_LINK + code if code else '(none: hosting is off)'}")


def host(on: bool):
    root = tree()
    path, kind, was = locate(root, "sipCallHostingEnabled")
    if path is None or kind != "bool" or len(path) != 1:
        raise SystemExit(f"sipCallHostingEnabled not where expected: path {path}, kind {kind}")
    print(f"sipCallHostingEnabled at path {path}: was {was}, writing {on}")
    rcp_transfer.send_record(callme.build_hosting(on, path))
    # The code is issued by RODE's service over the network, so give it time to arrive.
    for _ in range(10):
        time.sleep(1.0)
        root = tree()
        got = locate(root, "sipCallHostingEnabled")[2]
        code = locate(root, "sipRodeCode")[2]
        if got == on and (bool(code) == on):
            break
    print(f"read back: hosting {got}, code {code!r}")
    print(f"guest link: {callme.GUEST_LINK + code if code else '(none)'}")
    return 0 if got == on else 1


SAY = Path(__file__).resolve().parent.parent / "Project" / "say.ps1"


def say(text: str):
    subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SAY),
                      "-Text", text])


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
    print(f"watching sip* pushes for {seconds:.0f} s")
    try:
        with push_handle("callme watch") as dev:
            for at, path, name, val in pushes(dev, seconds):
                if name.startswith(("sip", "icecast")):
                    print(f"  +{at:6.1f}s {path} " + show(name, val), flush=True)
                    if speak:
                        announce(name, val)
    except KeyboardInterrupt:
        pass


def hangup(slot: int):
    """End the call on call slot `slot` (0-based), then watch the slot's state for 6 s."""
    root = tree()
    slots = [n for n in model_tree.walk(root)
             if any(p == "sipSlotCallState" for p, _v, _k in n.props)]
    node = slots[slot]
    was = next(v for p, v, _k in node.props if p == "sipSlotCallDisconnect")
    print(f"slot {slot} at {node.path()}: sipSlotCallDisconnect was {was}, hanging up")
    for rec in callme.build_hangup(node.path()):
        rcp_transfer.send_record(rec)
    for _ in range(6):
        time.sleep(1.0)
        n2 = next(n for n in model_tree.walk(tree()) if n.path() == node.path())
        props = {p: v for p, v, _k in n2.props}
        print(f"  state {props.get('sipSlotCallState')!r}, disconnect "
              f"{props.get('sipSlotCallDisconnect')}", flush=True)
    return 0


def dial(code: str, slot: int = 0, seconds: float = 20.0):
    """Call another desk's CallMe `code` from `slot` and log what the desk pushes back."""
    root = tree()
    path, kind, was = locate(root, "sipOutgoingCallDetails")
    if path is None or len(path) != 1:
        raise SystemExit(f"sipOutgoingCallDetails not where expected: path {path}")
    code = callme.normalise_code(code)
    print(f"sipOutgoingCallDetails at {path} ({kind}, was {was!r}): dialling {code}")
    with push_handle("callme dial") as dev:
        for rec in callme.build_dial(code, slot, path):
            rcp_transfer.send_record(rec)
        for at, p, name, val in pushes(dev, seconds):
            if name.startswith("sip") and "Bitrate" not in name and "Jitter" not in name:
                print(f"  +{at:5.1f}s {p} " + show(name, val), flush=True)
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
    print(f"waiting up to {seconds:.0f} s for a call; will send slot {slot}", flush=True)
    call = None
    with push_handle("callme answer") as dev:
        for _at, _p, name, val in pushes(dev, seconds):
            if name == "sipIncomingCallDetails":
                call = callme.parse_call(val)
                if call:
                    break
    if not call:
        print("no call arrived")
        return 1
    print(f"call from {call['name']!r} ({call['type']}), answering into slot "
          f"{slot}", flush=True)
    rcp_transfer.send_record(callme.build_answer(call["id"], slot, path))
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
    if argv[0] == "dial" and len(argv) == 2:
        return dial(argv[1])
    if argv[0] == "hangup" and len(argv) == 2:
        return hangup(int(argv[1]))
    if argv[0] == "answer":
        return answer(int(argv[1]) if len(argv) > 1 else 0, float(argv[2]) if len(argv) > 2 else 300)
    if argv[0] in ("watch", "speak"):
        watch(float(argv[1]) if len(argv) > 1 else 120, speak=argv[0] == "speak")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
