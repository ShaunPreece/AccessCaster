"""Offline checks for the desk's own shows (export / import / reset / delete). No device needed:
everything here works on a synthetic model blob.

Run: python DevTools/test_shows.py
"""
import inspect

import _bootstrap  # noqa: F401  (puts Project/ on the path; import FIRST)
import _testkit
import devices
import fake_model
import server
import shows
from _testkit import check

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

print("what each action takes")
def params(fn):
    return list(inspect.signature(fn).parameters)


# ONE backup takes a NAME, not a handle: the user names it, and the app decides underneath whether
# that means a show plus a profile or a profile alone.
check("backup takes a name", params(server.action_backup_now), ["name"])
check("restore takes a name, then clean", params(server.action_restore_backup), ["name", "clean"])
# Import and delete take a SNAPSHOT INDEX: a uid is a position any export renumbers, and a uuid
# names the show rather than the file so several backups share one.
check("import takes snapshot", params(server.action_show_import)[0], "snapshot")
check("delete takes snapshot", params(server.action_show_delete)[0], "snapshot")
check("restore takes clean", params(server.restore_profile), ["name", "clean"])

print("the write gate")
check("Pro II may write shows", "shows" in devices.PRO2.caps, True)
# The Duo MAY TOO. The address carries a factory reset, so it is confirmed rather than guessed:
# root child 0x28E on a Duo is SHOWCONTROL in the tree, and the engine answers an impossible export
# there with error 8. See the cap comment in devices.py.
check("the Duo may too", "shows" in devices.DUO.caps, True)

raise SystemExit(_testkit.finish())
