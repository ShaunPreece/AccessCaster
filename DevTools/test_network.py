"""Offline checks for the desk's network: the wifi scan parser, and the scan, join and disconnect
flows. No device needed: everything here works on a synthetic model blob or a plain string.

Run: python DevTools/test_network.py
"""
import _bootstrap  # noqa: F401  (puts Project/ on the path; import FIRST)
import _testkit
import fake_model
import network
from _testkit import check, check_in

print("wifi scan slot parsing")
check("plain", network.parse_scan_slot("MyNet----WPA2")["ssid"], "MyNet")
check("security kept", network.parse_scan_slot("MyNet----WPA2")["security"], "WPA2")
# An SSID may contain hyphens; the split must be on the LAST four-hyphen run, not the first.
check("hyphen in ssid", network.parse_scan_slot("Guest-House----WPA2")["ssid"], "Guest-House")
check("open network", network.parse_scan_slot("Cafe----none")["open"], True)
check("closed network", network.parse_scan_slot("MyNet----WPA2")["open"], False)
# No separator: keep it as a bare SSID rather than throwing away a joinable network.
check("no separator", network.parse_scan_slot("Bare")["ssid"], "Bare")
check("blank", network.parse_scan_slot(""), {})
check("empty security reads open", network.parse_scan_slot("A----")["open"], True)
check("spoken names the need", "needs a password" in network.parse_scan_slot("A----WPA2")["spoken"],
      True)

print("wifi scan results, and the scan and disconnect flows")


def net_blob(ssid="", ip="", radio=True, numbered=(), blocks=()):
    rows = [("NETWORK", "wifi", radio), ("NETWORK", "wifiSSID", ssid),
            ("NETWORK", "wifiIpAddress", ip)]
    rows += [("NETWORK", f"wifiScan{n}", v) for n, v in enumerate(numbered, 1)]
    rows += [("WIFISCANRESULT", "wifiScanResultSSID", v) for v in blocks]
    return fake_model.encode(rows)


check("the numbered slots are read",
      network.wifi_scan_slots(net_blob(numbered=["A----PSK", "", "B----PSK"])),
      ["A----PSK", "B----PSK"])
check("the result blocks are read when the slots are empty",
      network.wifi_scan_slots(net_blob(numbered=["", ""], blocks=["C----PSK", ""])), ["C----PSK"])


def run(action, reads):
    """`action` against a desk that answers each model read with the next of `reads`, the last
    repeating. Returns (reply, what was written)."""
    sent, queue, clock = [], list(reads), [0.0]

    def read(**_k):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def sleep(seconds):
        clock[0] += seconds
    with _testkit.patched((network, {"_write_gate": lambda: None}),
                          (network.rcp_read, {"read_model_stable": read}),
                          (network.rcp_transfer, {"send_record": sent.append}),
                          (network.time, {"sleep": sleep, "monotonic": lambda: clock[0]})):
        return action(), sent


reply, sent = run(network.action_scan_wifi, [net_blob("Home", "10.0.0.2")])
check("a joined desk is not scanned", (reply["ok"], sent), (False, []))
check_in("and the refusal says to disconnect", "Disconnect", reply["message"])
old = net_blob(numbered=["Stale----PSK"])
one = net_blob(numbered=["A----PSK"])
reply, sent = run(network.action_scan_wifi,
                  [net_blob(), old, net_blob(), one, one, net_blob(numbered=["A----PSK", "B----PSK"])])
check("an unjoined scan cycles the radio", len(sent), 2)
check("and waits past the stale list, and past a network arriving late",
      [n["ssid"] for n in reply["networks"]], ["A", "B"])
reply, _ = run(network.action_scan_wifi, [net_blob(), old])
check("a list that never cleared is not passed off as a scan", reply["networks"], [])

scanned = net_blob(numbered=["Home----PSK", "Cafe----none"])
reply, sent = run(lambda: network.action_join_wifi("Home", ""), [scanned])
check("joining a network the scan says needs a password, with none, sends nothing",
      (reply["ok"], reply.get("needs_password"), sent), (False, True, []))
reply, sent = run(lambda: network.action_join_wifi("Cafe", ""), [scanned])
check("an open network joins with no password", len(sent), 2)
reply, sent = run(lambda: network.action_join_wifi("Hidden", ""), [scanned])
check("and so does one the scan never saw, which may be open", len(sent), 2)

reply, sent = run(network.action_disconnect_wifi, [net_blob("Home", "10.0.0.2"), net_blob()])
check("disconnect clears the name and cycles the radio", (reply["ok"], len(sent)), (True, 3))
check_in("and says the password is gone", "password", reply["message"])
reply, sent = run(network.action_disconnect_wifi, [net_blob("Home", radio=False), net_blob(radio=False)])
check("with the radio off it only clears the name", (reply["ok"], len(sent)), (True, 1))
reply, sent = run(network.action_disconnect_wifi, [net_blob("Home", "10.0.0.2")])
check("a desk that keeps its network is reported, not passed off", reply["ok"], False)
reply, sent = run(network.action_disconnect_wifi, [net_blob()])
check("with no network there is nothing to send", (reply["ok"], sent), (True, []))

raise SystemExit(_testkit.finish())
