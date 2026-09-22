"""Offline checks for the desk's own shows (export / import / reset / delete) and the wifi scan
parser. No device needed: everything here works on a synthetic model blob or a plain string.

Run: python DevTools/test_shows.py
"""
import _bootstrap  # noqa: F401  (puts Project/ on the path; import FIRST)

import server
import shows


def check(label, got, want):
    if got != want:
        raise AssertionError(f"{label}: got {got!r}, wanted {want!r}")
    print(f"  ok  {label}")


def fake_blob(entries):
    """A minimal model blob: (block header, property, value) in stream order.

    Mirrors the real layout that rcp_read.iter_block_properties walks. A name is any alphanumeric
    run immediately followed by `00 01`; an ALL-CAPS one is a section header, anything else is a
    property whose value block follows as `<vlen> <vtype> <payload>` (rcp_read._val_after):
    vtype 0x01 = int32 LE, 0x05 = string. Every record is separated by a 0x00 so the parser's
    walk-back over alphanumerics cannot absorb a payload byte into the next name.

    IT OPENS WITH THE ROOT NODE'S NAME, as every real dump does, because `rcp_read.is_model_dump`
    uses that marker to tell a dump from the ZERO BYTES a desk that has stopped answering returns.
    Without it a fake dump is indistinguishable from a dead desk, and `shows.wait_for` correctly
    ignores it.
    """
    out = bytearray(b"Rodecaster\x00")
    header = None
    for block, name, value in entries:
        if block != header:
            out += b"\x00" + block.encode() + b"\x00\x01\x00\x00\x00"
            header = block
        out += b"\x00" + name.encode() + b"\x00\x01"
        if isinstance(value, int):
            out += b"\x05\x01" + value.to_bytes(4, "little", signed=True)
        else:
            raw = value.encode() + b"\x00"
            out += bytes([len(raw), 0x05]) + raw
    return bytes(out)


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

print("show list grouping")
blob = fake_blob([
    ("SHOW", "showUID", 0), ("SHOW", "showName", "Newest"), ("SHOW", "showUUID", "uuid-new"),
    ("SHOW", "showSnapshotIndex", 7),
    ("SHOW", "showUID", 1), ("SHOW", "showName", "Older"), ("SHOW", "showUUID", "uuid-old"),
    ("SHOW", "showSnapshotIndex", 6),
])
got = shows.read_shows(blob)
check("two shows", len(got), 2)
# NEWEST FIRST, by snapshot index. The unit emits newest first, but the sort does not rely on that:
# an earlier draft reversed the stream on a belief borrowed from the recordings list and put the
# OLDEST at the top, labelled "the newest".
check("newest first", [s["name"] for s in got], ["Newest", "Older"])
check("uuid carried", got[0]["uuid"], "uuid-new")
check("uid carried", got[0]["uid"], 0)
check("snapshot carried", got[0]["snapshot"], 7)
# A name must never be paired with another node's uid: grouping is on the block boundary.
check("no mis-pairing", got[1]["uid"], 1)
# Sorted, not merely reversed: the same nodes fed in the other order must come out the same way.
rev = shows.read_shows(fake_blob([
    ("SHOW", "showUID", 1), ("SHOW", "showName", "Older"), ("SHOW", "showUUID", "uuid-old"),
    ("SHOW", "showSnapshotIndex", 6),
    ("SHOW", "showUID", 0), ("SHOW", "showName", "Newest"), ("SHOW", "showUUID", "uuid-new"),
    ("SHOW", "showSnapshotIndex", 7),
]))
check("order does not depend on the firmware's", [s["name"] for s in rev], ["Newest", "Older"])
# A node whose snapshot index did not decode must sort LAST, never claim to be the newest.
odd = shows.read_shows(fake_blob([
    ("SHOW", "showUID", 0), ("SHOW", "showName", "NoIndex"), ("SHOW", "showUUID", "x"),
    ("SHOW", "showUID", 1), ("SHOW", "showName", "Indexed"), ("SHOW", "showUUID", "y"),
    ("SHOW", "showSnapshotIndex", 5),
]))
check("unplaceable node sorts last", [s["name"] for s in odd], ["Indexed", "NoIndex"])

# A UUID IS NOT A FILE HANDLE. Measured on hardware: three exports of one desk state produce three
# SHOW nodes with the SAME uuid and the same name, so uuid-keyed code cannot detect a repeat backup
# and a delete can never verify itself. Only the snapshot index tells these apart.
print("duplicate uuids: the case a uuid-keyed design cannot see")
dupes = fake_blob([
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
blob = fake_blob([("SHOW", "showUID", 0), ("SHOW", "showUUID", "u")])
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
import devices
check("Pro II may write shows", "shows" in devices.PRO2.caps, True)
# The Duo MAY TOO. The address carries a factory reset, so it needed settling rather than guessing:
# root child 0x28E on a live Duo IS SHOWCONTROL in the tree, and the engine then proved the address
# live by answering an impossible export with error 8 in one second. See the cap comment in
# devices.py.
check("the Duo may too", "shows" in devices.DUO.caps, True)

print("\nALL SHOW AND SCAN CHECKS PASSED")
