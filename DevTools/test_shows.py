"""Offline checks for the desk's own shows (export / import / reset / delete), and the wifi scan
parser and its scan and disconnect flows. No device needed: everything here works on a synthetic
model blob or a plain string.

Run: python DevTools/test_shows.py
"""
import _bootstrap  # noqa: F401  (puts Project/ on the path; import FIRST)
import _testkit
import devices
import fake_model
import server
import shows
from _testkit import check, check_in

print("wifi scan slot parsing")
check("plain", server.parse_scan_slot("MyNet----WPA2")["ssid"], "MyNet")
check("security kept", server.parse_scan_slot("MyNet----WPA2")["security"], "WPA2")
# An SSID may contain hyphens; the split must be on the LAST four-hyphen run, not the first.
check("hyphen in ssid", server.parse_scan_slot("Guest-House----WPA2")["ssid"], "Guest-House")
check("open network", server.parse_scan_slot("Cafe----none")["open"], True)
check("closed network", server.parse_scan_slot("MyNet----WPA2")["open"], False)
# No separator: keep it as a bare SSID rather than throwing away a joinable network.
check("no separator", server.parse_scan_slot("Bare")["ssid"], "Bare")
check("blank", server.parse_scan_slot(""), {})
check("empty security reads open", server.parse_scan_slot("A----")["open"], True)
check("spoken names the need", "needs a password" in server.parse_scan_slot("A----WPA2")["spoken"],
      True)

print("wifi scan results, and the scan and disconnect flows")


def net_blob(ssid="", ip="", radio=True, numbered=(), blocks=()):
    rows = [("NETWORK", "wifi", radio), ("NETWORK", "wifiSSID", ssid),
            ("NETWORK", "wifiIpAddress", ip)]
    rows += [("NETWORK", f"wifiScan{n}", v) for n, v in enumerate(numbered, 1)]
    rows += [("WIFISCANRESULT", "wifiScanResultSSID", v) for v in blocks]
    return fake_model.encode(rows)


check("the numbered slots are read",
      server.wifi_scan_slots(net_blob(numbered=["A----PSK", "", "B----PSK"])),
      ["A----PSK", "B----PSK"])
check("the result blocks are read when the slots are empty",
      server.wifi_scan_slots(net_blob(numbered=["", ""], blocks=["C----PSK", ""])), ["C----PSK"])


def run(action, reads):
    """`action` against a desk that answers each model read with the next of `reads`, the last
    repeating. Returns (reply, what was written)."""
    sent, queue, clock = [], list(reads), [0.0]

    def read(**_k):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def sleep(seconds):
        clock[0] += seconds
    with _testkit.patched((server, {"_network_write_gate": lambda: None}),
                          (server.rcp_read, {"read_model_stable": read}),
                          (server.rcp_transfer, {"send_record": sent.append}),
                          (server.time, {"sleep": sleep, "monotonic": lambda: clock[0]})):
        return action(), sent


reply, sent = run(server.action_scan_wifi, [net_blob("Home", "10.0.0.2")])
check("a joined unit is not scanned", (reply["ok"], reply.get("joined"), sent), (False, True, []))
check_in("and the refusal says to disconnect", "Disconnect", reply["message"])
old = net_blob(numbered=["Stale----PSK"])
one = net_blob(numbered=["A----PSK"])
reply, sent = run(server.action_scan_wifi,
                  [net_blob(), old, net_blob(), one, one, net_blob(numbered=["A----PSK", "B----PSK"])])
check("an unjoined scan cycles the radio", len(sent), 2)
check("and waits past the stale list, and past a network arriving late",
      [n["ssid"] for n in reply["networks"]], ["A", "B"])
reply, _ = run(server.action_scan_wifi, [net_blob(), old])
check("a list that never cleared is not passed off as a scan", reply["networks"], [])

scanned = net_blob(numbered=["Home----PSK", "Cafe----none"])
reply, sent = run(lambda: server.action_join_wifi("Home", ""), [scanned])
check("joining a network the scan says needs a password, with none, sends nothing",
      (reply["ok"], reply.get("needs_password"), sent), (False, True, []))
reply, sent = run(lambda: server.action_join_wifi("Cafe", ""), [scanned])
check("an open network joins with no password", len(sent), 2)
reply, sent = run(lambda: server.action_join_wifi("Hidden", ""), [scanned])
check("and so does one the scan never saw, which may be open", len(sent), 2)

reply, sent = run(server.action_disconnect_wifi, [net_blob("Home", "10.0.0.2"), net_blob()])
check("disconnect clears the name and cycles the radio", (reply["ok"], len(sent)), (True, 3))
check_in("and says the password is gone", "password", reply["message"])
reply, sent = run(server.action_disconnect_wifi, [net_blob("Home", radio=False), net_blob(radio=False)])
check("with the radio off it only clears the name", (reply["ok"], len(sent)), (True, 1))
reply, sent = run(server.action_disconnect_wifi, [net_blob("Home", "10.0.0.2")])
check("a unit that keeps its network is reported, not passed off", reply["ok"], False)
reply, sent = run(server.action_disconnect_wifi, [net_blob()])
check("with no network there is nothing to send", (reply["ok"], sent), (True, []))

print("show list grouping")
blob = fake_model.encode([
    ("SHOW", "showUID", 0), ("SHOW", "showName", "Newest"), ("SHOW", "showUUID", "uuid-new"),
    ("SHOW", "showSnapshotIndex", 7),
    ("SHOW", "showUID", 1), ("SHOW", "showName", "Older"), ("SHOW", "showUUID", "uuid-old"),
    ("SHOW", "showSnapshotIndex", 6),
])
got = shows.read_shows(blob)
check("two shows", len(got), 2)
# NEWEST FIRST, by snapshot index. The unit emits newest first, but the sort does not rely on that,
# and the recordings list's order is no guide to this one.
check("newest first", [s["name"] for s in got], ["Newest", "Older"])
check("uuid carried", got[0]["uuid"], "uuid-new")
check("uid carried", got[0]["uid"], 0)
check("snapshot carried", got[0]["snapshot"], 7)
# A name must never be paired with another node's uid: grouping is on the block boundary.
check("no mis-pairing", got[1]["uid"], 1)
# Sorted, not merely reversed: the same nodes fed in the other order must come out the same way.
rev = shows.read_shows(fake_model.encode([
    ("SHOW", "showUID", 1), ("SHOW", "showName", "Older"), ("SHOW", "showUUID", "uuid-old"),
    ("SHOW", "showSnapshotIndex", 6),
    ("SHOW", "showUID", 0), ("SHOW", "showName", "Newest"), ("SHOW", "showUUID", "uuid-new"),
    ("SHOW", "showSnapshotIndex", 7),
]))
check("order does not depend on the firmware's", [s["name"] for s in rev], ["Newest", "Older"])
# A node whose snapshot index did not decode must sort LAST, never claim to be the newest.
odd = shows.read_shows(fake_model.encode([
    ("SHOW", "showUID", 0), ("SHOW", "showName", "NoIndex"), ("SHOW", "showUUID", "x"),
    ("SHOW", "showUID", 1), ("SHOW", "showName", "Indexed"), ("SHOW", "showUUID", "y"),
    ("SHOW", "showSnapshotIndex", 5),
]))
check("unplaceable node sorts last", [s["name"] for s in odd], ["Indexed", "NoIndex"])

# A UUID IS NOT A FILE HANDLE. Three exports of one desk state produce three
# SHOW nodes with the SAME uuid and the same name, so uuid-keyed code cannot detect a repeat backup
# and a delete can never verify itself. Only the snapshot index tells these apart.
print("duplicate uuids: the case a uuid-keyed design cannot see")
dupes = fake_model.encode([
    ("SHOW", "showUID", 0), ("SHOW", "showName", "Default Show"),
    ("SHOW", "showUUID", "same"), ("SHOW", "showSnapshotIndex", 3),
    ("SHOW", "showUID", 1), ("SHOW", "showName", "Default Show"),
    ("SHOW", "showUUID", "same"), ("SHOW", "showSnapshotIndex", 2),
    ("SHOW", "showUID", 2), ("SHOW", "showName", "Default Show"),
    ("SHOW", "showUUID", "same"), ("SHOW", "showSnapshotIndex", 1),
])
dupe_list = shows.read_shows(dupes)
check("three indistinguishable-by-uuid backups", len(dupe_list), 3)
check("uuids really do collide", len({s["uuid"] for s in dupe_list}), 1)
check("snapshots do not", len({s["snapshot"] for s in dupe_list}), 3)
check("find_by_snapshot picks the right one", shows.find_by_snapshot(dupe_list, 2)["uid"], 1)
check("find_by_snapshot on a missing index", shows.find_by_snapshot(dupe_list, 99), None)
# Snapshot 0 must not be confused with "nothing selected": handles are validated by None, not
# falsiness, on both sides of the wire.
check("snapshot zero is findable",
      shows.find_by_snapshot([{"snapshot": 0, "uid": 4}], 0)["uid"], 4)

print("show list edge cases")
check("empty dump", shows.read_shows(b""), [])
blob = fake_model.encode([("SHOW", "showUID", 0), ("SHOW", "showUUID", "u")])
check("missing name is named", shows.read_shows(blob)[0]["name"], "Unnamed show")
check("missing snapshot is None", shows.read_shows(blob)[0]["snapshot"], None)

print("last error wording")
# The one code ever seen says the unit REFUSED it, and must NOT claim a cause. "The uuid matches
# the loaded show" is the tempting one and it is disproven (shows.py trap 3).
check("known code says it was refused",
      "refused" in shows.error_message(shows.ERROR_REFUSED), True)
check("and does not invent a cause",
      "already loaded" in shows.error_message(shows.ERROR_REFUSED), False)
# An unknown code must NOT be given invented wording.
check("unknown code is honest", "has not seen before" in shows.error_message(97), True)
check("no error, no words", shows.error_message(0), "")

print("modification date is not guessed at")
check("blank stays blank", shows._describe_modified(None), "")
# A number outside a plausible date range is passed through rather than rendered as a wrong date.
check("implausible number passes through", shows._describe_modified(42), "42")
check("text passes through", shows._describe_modified("last Tuesday"), "last Tuesday")

print("routes")
for r in ("/api/shows", "/api/shows/status"):
    check(f"GET {r}", r in server.GET_ROUTES, True)
for r in ("/api/shows/export", "/api/shows/import", "/api/shows/delete", "/api/shows/reset",
          "/api/network/wifi/scan", "/api/backup", "/api/backup/restore", "/api/backup/plan"):
    check(f"POST {r}", r in server.POST_ROUTES, True)
# ONE backup takes a NAME, not a handle: the user names it, and the app decides underneath whether
# that means a show plus a profile or a profile alone.
check("backup takes a name", server.POST_ROUTES["/api/backup"][1][0][1], "name")
check("restore takes a name", server.POST_ROUTES["/api/backup/restore"][1][0][1], "name")
# Import and delete take a SNAPSHOT INDEX: a uid is a position any export renumbers, and a uuid
# names the show rather than the file so several backups share one.
check("import takes snapshot", server.POST_ROUTES["/api/shows/import"][1][0][1], "snapshot")
check("delete takes snapshot", server.POST_ROUTES["/api/shows/delete"][1][0][1], "snapshot")
check("import handle is an int", server.POST_ROUTES["/api/shows/import"][1][0][0], int)
check("restore takes clean", [f[1] for f in server.POST_ROUTES["/api/profiles/restore"][1]],
      ["name", "clean"])

print("the write gate")
check("Pro II may write shows", "shows" in devices.PRO2.caps, True)
# The Duo MAY TOO. The address carries a factory reset, so it is confirmed rather than guessed:
# root child 0x28E on a Duo is SHOWCONTROL in the tree, and the engine answers an impossible export
# there with error 8. See the cap comment in devices.py.
check("the Duo may too", "shows" in devices.DUO.caps, True)

raise SystemExit(_testkit.finish())
