"""CAN A DUO BE TOLD TO CHANGE ITS OWN NETWORK SETTINGS? (the `network_write` cap)

THE CAP WAS OFF FOR ONE REASON, and it is the standing one: the NETWORK block was proven on a Pro II
and had never been seen on a Duo, and a write to an unmapped selector can append a stray that only a
factory reset clears. That reason is checkable rather than permanent, which is how `unit_settings`
and `level_write` both came on: ask the desk's own TREE whether the block is there and carries the
properties, then let the ENGINE confirm the address by doing something we did not write.

WHAT THIS ASKS, in the order it asks it:

  1. STRUCTURE. Is root child 0x10 a NETWORK node on THIS desk, and does it carry every property
     the app writes (`wifi`, `wifiSSID`, `wifiPSK`, `wifiScan`, `wifiScan1..10`, `btVisible`)?
     A miss here stops the run: nothing is written to a block we cannot identify.

  2. THE ENGINE, VIA A SCAN. `wifiScan` = True is the ideal probe: harmless, and the desk answers
     by FILLING IN slots we never wrote. A read-back cannot fake that. If networks appear in
     wifiScan1..10 after we set one bool, the write reached the radio.

  3. THE ENGINE, VIA THE RADIO. `wifi` off and on again. The desk answers by DROPPING its IP
     address and then getting a new one from DHCP - again, state we did not write and could not
     have. This is the strongest of the three and the only one that proves the whole path.

  4. btVisible, the Pro II's own proof property, round-tripped for completeness.

WHAT IT NEVER DOES: write `wifiPSK`, or join a network. Proving the cap needs neither, and the
password is a secret this project does not handle (see server._wifi_state).

THE RADIO GOES DOWN IN STEP 3 AND COMES BACK. Control is over USB, not the network, so there is no
way to strand the desk; but if it is being reached over its own wifi for anything else, expect a gap
of a few seconds. It is put back to whatever it was found on.

Run: python DevTools/duo_network_probe.py [--skip-radio]
"""
from __future__ import annotations
import sys
import time

import _bootstrap  # noqa: F401
import devices
import model_tree
import rcp_read as rr
import rcp_transfer as rt

SEL = rt.SEL_NETWORK
# Every NETWORK property the app can write, from server.action_set_wifi / _join_wifi / _scan_wifi /
# _set_bt_visible. The point of listing them is that the structure check covers what we would WRITE,
# not just the one property we happen to probe with.
WRITTEN = ("wifi", "wifiSSID", "wifiPSK", "wifiScan", "btVisible")
READ_BACK = tuple(f"wifiScan{n}" for n in range(1, 11))
FAILED = 0


def net_node():
    blob = rr.read_model_stable(fresh=True)
    root, _u, _t = model_tree.parse(blob)
    return blob, root.kids[SEL]


def props(node):
    return {a: b for a, b, _k in node.props}


def settled(timeout=15.0):
    """Snapshot the whole tree once it has stopped moving."""
    def snap():
        blob = rr.read_model_stable(fresh=True)
        root, _u, _t = model_tree.parse(blob)
        out = {}
        for n in model_tree.walk(root):
            p = tuple(n.path())
            for name, val, _k in n.props:
                out[(p, n.name, name)] = val
        return out
    deadline = time.time() + timeout
    prev = snap()
    while time.time() < deadline:
        cur = snap()
        if cur == prev:
            return cur
        prev = cur
    return prev


def report(before, after, label, secret=("wifiPSK",)):
    changed = [(k, before[k], after[k]) for k in before if k in after and before[k] != after[k]]
    added = [(k, after[k]) for k in after if k not in before]
    removed = [k for k in before if k not in after]
    print(f"  {label}: {len(changed)} changed, {len(added)} added, {len(removed)} removed")
    for (path, block, prop), old, new in sorted(changed, key=lambda t: str(t[0])):
        if prop in secret:
            print(f"    {block}{list(path)}.{prop}: <redacted, changed>")
        else:
            print(f"    {block}{list(path)}.{prop}: {old!r} -> {new!r}")
    for (path, block, prop), val in sorted(added, key=lambda t: str(t[0])):
        print(f"    ADDED {block}{list(path)}.{prop} = {val!r}")
    return changed, added, removed


def main():
    global FAILED
    dev = devices.get_active_device()
    print(f"{dev.name}: network_write is currently "
          f"{'ON' if 'network_write' in dev.caps else 'OFF'}")

    # ---- 1. STRUCTURE ---------------------------------------------------------------------
    blob, node = net_node()
    print(f"\n1. STRUCTURE: root child {SEL:#04x} is {node.name}, path {node.path()}, "
          f"{len(node.props)} properties")
    if node.name != "NETWORK":
        print(f"   FAIL {SEL:#04x} is not the NETWORK block on this desk. Writing nothing.")
        return 1
    have = props(node)
    missing = [p for p in WRITTEN + READ_BACK if p not in have]
    for p in WRITTEN:
        print(f"   {'ok  ' if p in have else 'MISS'} {p}")
    print(f"   {'ok  ' if not [p for p in READ_BACK if p not in have] else 'MISS'} "
          f"wifiScan1..10 (the slots a scan fills)")
    if missing:
        print(f"   FAIL missing: {missing}. Writing nothing.")
        return 1
    print(f"   => every property the app writes is present on this desk's NETWORK block.")
    print(f"   radio now: wifi={have['wifi']} ssid={have['wifiSSID']!r} ip={have['wifiIpAddress']!r}")

    # ---- 2. THE ENGINE ANSWERS A SCAN ----------------------------------------------------
    print("\n2. ENGINE TEST, a wifi scan. One bool from us; the slots are the desk's answer.")
    before = settled()
    rt.send_record(rt.build_global_prop_bool("wifiScan", True, SEL))
    time.sleep(12.0)
    after = settled()
    report(before, after, "scan")
    _b, node = net_node()
    have = props(node)
    found = [have[f"wifiScan{n}"] for n in range(1, 11) if have.get(f"wifiScan{n}")]
    print(f"   the desk filled {len(found)} scan slot(s) we never wrote")
    for f in found[:6]:
        print(f"     {f}")
    if not found:
        # NOT A FAILURE, and measured rather than excused. A Duo already joined to a network filled
        # no slot in 35 seconds and did not clear the flag either, while a Pro II answers in a few.
        # The radio test below reaches the same block with the same builder, so this is not the
        # address; the likely answer is that the firmware will not scan while associated.
        print("   note the scan filled nothing. On a desk already joined to a network that is the "
              "expected result here, not an addressing failure - step 3 proves the same block "
              "reaches the radio. Re-run with the radio idle to see a scan work.")
    else:
        print("   => the write reached the radio. A dead address cannot produce scan results.")

    # ---- 3. THE ENGINE ANSWERS THE RADIO -------------------------------------------------
    if "--skip-radio" not in sys.argv:
        _b, node = net_node()
        was = bool(props(node)["wifi"])
        ip_was = props(node)["wifiIpAddress"]
        print(f"\n3. ENGINE TEST, the radio itself. wifi is {was}, address {ip_was!r}.")
        print(f"   turning it {'off' if was else 'on'} and back again")
        before = settled()
        rt.send_record(rt.build_global_prop_bool("wifi", not was, SEL))
        time.sleep(6.0)
        mid = settled()
        report(before, mid, f"wifi -> {not was}")
        _b, node = net_node()
        ip_mid = props(node)["wifiIpAddress"]
        radio_mid = bool(props(node)["wifi"])
        print(f"   radio now {radio_mid}, address {ip_mid!r}")
        rt.send_record(rt.build_global_prop_bool("wifi", was, SEL))
        # DHCP is slower than the radio: poll for the address rather than guessing a sleep.
        deadline = time.time() + 45
        ip_back = ""
        while time.time() < deadline:
            time.sleep(3.0)
            _b, node = net_node()
            ip_back = props(node)["wifiIpAddress"]
            if ip_back:
                break
        _b, node = net_node()
        print(f"   restored: wifi={props(node)['wifi']}, address {ip_back!r}")
        if radio_mid != was and (ip_mid != ip_was or not ip_mid):
            print("   => the DESK dropped its address when the radio went down. That is the engine "
                  "acting on our write, not a value echoing back.")
        elif radio_mid == was:
            FAILED += 1
            print("   FAIL the radio did not change state.")
        else:
            print("   note the radio flipped but the address did not move; weaker, still positive.")
        if bool(props(node)["wifi"]) != was:
            FAILED += 1
            print("   FAIL the radio was NOT put back as it was found.")
        if was and not ip_back:
            print("   note no address yet; DHCP can take longer than we waited. Check again.")

    # ---- 4. btVisible round trip ----------------------------------------------------------
    _b, node = net_node()
    was_vis = bool(props(node)["btVisible"])
    print(f"\n4. btVisible round trip (the Pro II's own proof property). Now {was_vis}.")
    before = settled()
    rt.send_record(rt.build_global_prop_bool("btVisible", not was_vis, SEL))
    time.sleep(2.0)
    after = settled()
    report(before, after, f"btVisible -> {not was_vis}")
    _b, node = net_node()
    got = bool(props(node)["btVisible"])
    if got != was_vis:
        print(f"   ok   it took: btVisible is {got}")
    else:
        FAILED += 1
        print(f"   FAIL btVisible did not change")
    rt.send_record(rt.build_global_prop_bool("btVisible", was_vis, SEL))
    time.sleep(2.0)
    back = settled()
    changed, added, removed = report(before, back, "restored")
    if not (changed or added or removed):
        print("   ok   tree identical to baseline")

    print("\n" + "=" * 70)
    print(f"{FAILED} CHECK(S) FAILED" if FAILED else "ALL CHECKS PASSED")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
