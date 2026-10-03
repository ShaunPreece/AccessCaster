"""The show commands driven against a SIMULATED desk, faults included.

WHY THIS EXISTS. All four commands are hardware-proven now, but the FAULTS are
not and never will be: a desk that takes a command and silently does nothing, a delete that does not
delete, an error code nobody has seen. Those are the branches that decide whether the app tells the
truth, and they cannot be staged on real hardware. This drives the REAL `server.action_*` functions
against a fake desk so the logic, the verification and above all the REFUSALS are exercised.

The fake desk models ONE restorable value (a mic gain) as well as the show list, because the app's
evidence that an import ACTED is the desk state moving. It cannot be the uuid: a uuid only changes
on a factory reset, so in the ordinary case it already equals the target before anything happens.

WHAT IT DOES NOT PROVE: that the bytes are right. Only hardware proves that, and this file must
never be cited as though it had. It proves the decisions.

Run: python DevTools/test_shows_sim.py
"""
import _bootstrap  # noqa: F401  (puts Project/ on the path; import FIRST)

import threading
import time

import devices
import rcp_read
import server
import shows

# PIN THE DESK. This suite simulates Pro II show behaviour, so with a Duo attached it would fail on
# a legitimate refusal and look like a regression. Offline tests must not depend on what happens to
# be plugged into the machine running them.
devices.force_device(devices.PRO2)

PASSED = []


def check(label, got, want):
    if got != want:
        raise AssertionError(f"{label}:\n  got  {got!r}\n  want {want!r}")
    PASSED.append(label)
    print(f"  ok  {label}")


def check_in(label, needle, haystack):
    if needle.lower() not in (haystack or "").lower():
        raise AssertionError(f"{label}:\n  {needle!r} not in {haystack!r}")
    PASSED.append(label)
    print(f"  ok  {label}")


# ---------------------------------------------------------------------------
# The fake desk.

def _count(n: int) -> bytes:
    if n == 0:
        return b"\x00"
    ln = 1 if n <= 0xFF else 2
    return bytes([ln]) + n.to_bytes(ln, "little")


def _prop(name: str, value) -> bytes:
    # BOOL IS TESTED BEFORE INT ON PURPOSE: bool subclasses int in Python, so the int branch would
    # swallow True and emit it with the wrong KIND, and rcp_read.values filters on kind.
    if isinstance(value, bool):
        payload = bytes([0x02 if value else 0x03])
    elif isinstance(value, int):
        payload = b"\x01" + value.to_bytes(4, "little", signed=True)
    else:
        payload = b"\x05" + value.encode("latin1") + b"\x00"
    return name.encode("latin1") + b"\x00" + _count(len(payload)) + payload


def encode(entries):
    """Render (block, property, value) triples as a model dump in the real tree grammar (see
    model_tree): the root, then one child node per run of a block. A property name repeating inside
    a run starts a new instance, so consecutive SHOW rows become one SHOW node per backup.

    It opens with the root node's name, as every real dump does, because `rcp_read.is_model_dump`
    uses that marker to tell a real dump from the ZERO BYTES a desk that has stopped answering
    returns.
    """
    nodes = []                                   # [(block, {name: value})]
    for block, name, value in entries:
        if not nodes or nodes[-1][0] != block or name in nodes[-1][1]:
            nodes.append((block, {}))
        nodes[-1][1][name] = value
    out = bytearray(b"Rodecaster\x00" + _count(0) + _count(len(nodes)))
    for block, props in nodes:
        out += block.encode() + b"\x00" + _count(len(props))
        for name, value in props.items():
            out += _prop(name, value)
        out += _count(0)
    return bytes(out)


class FakeDesk:
    """Enough of a RODECaster to answer the show commands.

    `backups` is a list of dicts in the unit's own emission order (newest first), each with a
    snapshot index, a uuid and a name. `uid` is NOT stored: it is derived on every read, which is
    exactly what the real unit does and is the trap the app has to survive.
    """

    def __init__(self):
        # Each backup carries the GAIN that was live when it was exported, so an import has
        # something to put back. Without a restorable value the fake desk cannot exercise the
        # app's real success test (see the module docstring).
        self.backups = [{"snapshot": 2, "uuid": "show-A", "name": "Default Show", "gain": 50},
                        {"snapshot": 1, "uuid": "show-A", "name": "Default Show", "gain": 50}]
        self.current_uuid = "show-A"
        self.gain = 50
        self.last_error = 0
        self.next_snapshot = 3
        self.calls = []
        # Fault switches, each one a real failure mode rather than an invented one.
        self.export_does_nothing = False
        self.import_does_nothing = False
        self.reset_does_nothing = False
        self.delete_does_nothing = False
        self.import_sets_error = 0
        # THE DESK LEAVES THE BUS PART WAY THROUGH. It returns ZERO BYTES rather than raising,
        # which is what a real one does: it stops answering about 30 s after the import command and
        # Windows drops it a minute later. Staged here because an empty dump differs from anything,
        # so this is the fault that reads as a cheerful success unless something checks for it.
        self.goes_quiet_on_import = False   # take the command, then stop answering entirely
        self.quiet = False
        # SOMEWHERE TO WRITE, which is now part of every export decision rather than a detail of the
        # page: `_export_and_wait` refuses before sending anything if there is no card or drive in.
        # Defaulting this to False models a desk with no storage, which is the state a Duo is
        # always in and the state the clean-restore bug hides in - so the DEFAULT is True and the
        # no-storage case is asked for explicitly.
        self.storage = True

    def blob(self):
        if self.quiet:
            return b""                            # off the bus: no error raised, just nothing
        rows = []
        for n, b in enumerate(self.backups):
            rows += [("SHOW", "showName", b["name"]),
                     ("SHOW", "showUUID", b["uuid"]),
                     ("SHOW", "showSnapshotIndex", b["snapshot"]),
                     ("SHOW", "showUID", n)]        # positional, newest = 0, like the real unit
        rows += [("INPUT", "inputMicrophoneGain", self.gain)]
        # `storageVolumeState` is "capacity|free|?|inserted|mounted" and is the ONLY honest source
        # (recorder.read_volumes says why). With `storage` off the slot reads present-but-empty,
        # which is a desk with nowhere to write rather than a desk with no slots.
        rows += [("STORAGE", "storageVolumeState",
                  "5276434432|4160749568|0|1|1" if self.storage else "0|0|0|0|0"),
                 ("STORAGE", "storageVolumeRecDestination", True)]
        rows += [("SHOWCONTROL", "currentShowName", "Loaded"),
                 ("SHOWCONTROL", "currentShowUUID", self.current_uuid),
                 ("SHOWCONTROL", "showControlLastError", self.last_error)]
        return encode(rows)

    def uid_of(self, snapshot):
        for n, b in enumerate(self.backups):
            if b["snapshot"] == snapshot:
                return n
        return None

    # -- the four commands, as the unit would act on them --------------------
    def export(self):
        self.calls.append("export")
        if self.export_does_nothing:
            return
        self.backups.insert(0, {"snapshot": self.next_snapshot, "uuid": self.current_uuid,
                                "name": "Default Show", "gain": self.gain})
        self.next_snapshot += 1

    def import_(self, uid):
        self.calls.append(("import", uid))
        if self.goes_quiet_on_import:
            self.quiet = True                     # took the command and left the bus
            return
        if self.import_sets_error:
            self.last_error = self.import_sets_error
            return
        if self.import_does_nothing:
            return
        if 0 <= uid < len(self.backups):
            self.current_uuid = self.backups[uid]["uuid"]
            self.gain = self.backups[uid].get("gain", self.gain)

    def new_from_default(self):
        self.calls.append("reset")
        if self.reset_does_nothing:
            return
        self.current_uuid = "factory-fresh"      # a NEW uuid, which is what makes the undo legal

    def delete(self, uid):
        self.calls.append(("delete", uid))
        if self.delete_does_nothing:
            return
        if 0 <= uid < len(self.backups):
            self.backups.pop(uid)


DESK = FakeDesk()


def install():
    """Point the app's device layer at the fake desk, and make the waits quick."""
    rcp_read.read_model_stable = lambda *a, **k: DESK.blob()
    rcp_read.read_model = lambda *a, **k: DESK.blob()
    shows.send_export = DESK.export
    shows.send_import = DESK.import_
    shows.send_new_from_default = DESK.new_from_default
    shows.send_delete = DESK.delete
    shows.POLL_S = 0.01
    shows.COMMAND_TIMEOUT_S = 0.25

    # THE LISTENER IS STUBBED OR THIS SUITE IS NOT OFFLINE. `rcp_read.PushListener` resolves the
    # control interface itself, and `find_control_interface` is not faked here - so on a machine
    # with a desk plugged in, an "offline" test opens a REAL handle to it and waits for a push that
    # cannot come, because the command went to the fake desk. It presents as "the job never
    # finished". A stub keeps the suite honest AND exercises the no-push fallback, which is the
    # branch that matters when a push is missed on real hardware.
    class _NoListener:
        lost = False

        def start(self):
            pass

        def wait_for(self, *a, **k):
            return None

        def wait_until_quiet(self, *a, **k):
            return 0.0

        def stop(self):
            pass

    rcp_read.PushListener = _NoListener


def run_job(start_result):
    """Start a command and wait for the one job to finish, then return its final status."""
    if not start_result.get("ok"):
        return start_result                      # refused before the job ever started
    deadline = time.time() + 20
    while time.time() < deadline:
        st = server.show_job_status()
        if st.get("done"):
            return st
        time.sleep(0.02)
    raise AssertionError("the job never finished")


def reset_desk():
    global DESK
    DESK = FakeDesk()
    install()


install()

# ---------------------------------------------------------------------------
print("export")
reset_desk()
res = run_job(server.action_show_export())
check("export reports success", res["ok"], True)
check("a backup really was added", len(DESK.backups), 3)
check_in("and it says how many there are now", "3 backups", res["message"])
check_in("and warns that each one costs space", "megabytes", res["message"])
# THE CASE A UUID CHECK GETS WRONG: the new backup shares its uuid with the two already there, so
# anything detecting it by uuid sees nothing and calls a good export a failure.
check("the new backup shares the others' uuid", len({b["uuid"] for b in DESK.backups}), 1)

print("no storage: refuse BEFORE sending, do not ask the impossible")
# THE BUG THIS BLOCK EXISTS FOR. `can_backup`
# was computed for the PAGE only, so every non-UI route still sent an export to a desk with nowhere
# to write, waited the full timeout for a file that could never appear, and only then refused. On a
# Duo, which has no removable storage at all, that made every CLEAN profile restore do it.
reset_desk()
DESK.storage = False
res = run_job(server.action_show_export())
check("export refuses when there is nowhere to write", res["ok"], False)
check("and NOTHING was sent to the desk", DESK.calls, [])
check_in("and it says why", "no memory card or usb drive", res["message"])

reset_desk()
DESK.storage = False
res = run_job(server.action_show_reset(backup_first=True))
check("a reset WITH a backup refuses too", res["ok"], False)
check("and does not reset the desk anyway", DESK.calls, [])
check_in("and offers the unbacked reset as the alternative", "cannot be undone", res["message"])

reset_desk()
DESK.storage = False
res = run_job(server.action_show_reset(backup_first=False))
check("but an UNBACKED reset still works with no storage", res["ok"], True)
check("and it really reset", DESK.calls, ["reset"])

print("export that silently does nothing")
reset_desk()
DESK.export_does_nothing = True
res = run_job(server.action_show_export())
check("a silent no-op is reported as a failure", res["ok"], False)
check_in("and says what to check", "drive", res["message"])

print("import")
reset_desk()
DESK.backups.insert(0, {"snapshot": 9, "uuid": "show-B", "name": "Yesterday"})
res = run_job(server.action_show_import(9))
check("import reports success", res["ok"], True)
check("the desk really loaded it", DESK.current_uuid, "show-B")
check_in("and warns about the monitor level", "monitor", res["message"])
# The uid is resolved at the moment of the write, not held from an earlier read.
check("it was addressed by uid, not by snapshot", DESK.calls[-1], ("import", 0))

print("import refusals")
reset_desk()
res = run_job(server.action_show_import(999))
check("an unknown backup is refused", res["ok"], False)
check_in("and says the list was refreshed", "no longer on the drive", res["message"])

# THE CASE A UUID CHECK REFUSES WRONGLY. A backup sharing the loaded show's uuid is the ORDINARY
# one, since a uuid only changes on a factory reset, and the unit restores it perfectly well: proven
# on hardware (Mic 4 comes back 30 -> 50, then 42 -> 50, error 0). Refusing it in advance makes
# shows useless for "change something, put it back".
reset_desk()
DESK.gain = 99                                    # the desk has drifted since that backup
res = run_job(server.action_show_import(2))       # snapshot 2 holds the loaded show
check("a backup of the loaded show still restores", res["ok"], True)
check("it really was sent to the desk", DESK.calls[-1], ("import", 0))
check("and the desk was put back", DESK.gain, 50)

# Nothing to do is NOT a failure, and must not be reported as one.
reset_desk()
res = run_job(server.action_show_import(2))       # desk already matches the backup
check("a desk that already matches is not an error", res["ok"], True)
check_in("and says so plainly", "already matched", res["message"])

print("import that the desk rejects")
reset_desk()
DESK.backups.insert(0, {"snapshot": 9, "uuid": "show-B", "name": "Yesterday"})
DESK.import_sets_error = shows.ERROR_UUID_CLASH
res = run_job(server.action_show_import(9))
check("a rejection is reported as one", res["ok"], False)
check_in("saying the unit refused it", "refused", res["message"])

reset_desk()
DESK.backups.insert(0, {"snapshot": 9, "uuid": "show-B", "name": "Yesterday"})
DESK.import_sets_error = 42                       # a code nobody has ever seen
res = run_job(server.action_show_import(9))
check("an unknown error code is not given invented wording", res["ok"], False)
check_in("it says so plainly", "has not seen before", res["message"])

print("the desk leaves the bus part way through an import")
reset_desk()
DESK.backups.insert(0, {"snapshot": 9, "uuid": "show-B", "name": "Yesterday"})
# It TAKES the command and then stops answering, which is the real shape of it: every read after
# the import returns zero bytes rather than raising.
DESK.goes_quiet_on_import = True
res = run_job(server.action_show_import(9))
check("a desk that stopped answering is NOT reported as a success", res["ok"], False)
check_in("and it says the unit stopped responding", "stopped responding", res["message"])
check_in("and it says how to get the unit back", "plug it back in", res["message"])

print("import that silently does nothing")
reset_desk()
DESK.backups.insert(0, {"snapshot": 9, "uuid": "show-B", "name": "Yesterday"})
DESK.import_does_nothing = True
res = run_job(server.action_show_import(9))
check("a silent no-op is not reported as success", res["ok"], False)
check_in("and does not claim anything was lost", "check the list", res["message"])

print("delete")
reset_desk()
res = run_job(server.action_show_delete(1))
check("delete reports success", res["ok"], True)
check("the backup is gone", [b["snapshot"] for b in DESK.backups], [2])
check("and the right one went", DESK.calls[-1], ("delete", 1))

reset_desk()
res = run_job(server.action_show_delete(999))
check("deleting something that is not there is refused", res["ok"], False)

reset_desk()
DESK.delete_does_nothing = True
res = run_job(server.action_show_delete(1))
check("a delete that did not happen is reported as a failure", res["ok"], False)
check_in("and names the backup", "still on the drive", res["message"])

# Deleting a backup that holds the LOADED show is ALLOWED. Refusing it treats a uuid as identifying
# a FILE, and since every backup shares one, that rule refuses to delete any of them - which is
# exactly when a drive fills up.
reset_desk()
res = run_job(server.action_show_delete(2))
check("a backup of the loaded show can still be deleted", res["ok"], True)

print("the factory reset")
reset_desk()
res = run_job(server.action_show_reset(backup_first=True))
check("reset reports success", res["ok"], True)
check("it backed up BEFORE resetting", DESK.calls[0], "export")
check("then reset", DESK.calls[1], "reset")
check("the desk is on a new show", DESK.current_uuid, "factory-fresh")
check_in("it says what survives: wifi", "wifi", res["message"])
check_in("it says what survives: recordings", "recording", res["message"])
check_in("it warns about the monitor level", "monitor", res["message"])
check_in("and it names the undo", "to undo this", res["message"])

print("THE ONE THAT MATTERS: the backup fails, so the reset must not happen")
reset_desk()
DESK.export_does_nothing = True
res = run_job(server.action_show_reset(backup_first=True))
check("the whole thing is refused", res["ok"], False)
check("THE DESK WAS NOT RESET", DESK.current_uuid, "show-A")
check("no reset was ever sent", "reset" in DESK.calls, False)
check_in("and it says nothing was changed", "nothing on the desk has changed", res["message"])

print("reset without a backup, chosen deliberately")
reset_desk()
DESK.export_does_nothing = True                  # would fail, but must not be called at all
res = run_job(server.action_show_reset(backup_first=False))
check("it proceeds", res["ok"], True)
check("no backup was attempted", "export" in DESK.calls, False)
check("the desk was reset", DESK.current_uuid, "factory-fresh")
check("and no undo is offered, because there is none", "to undo this" in res["message"].lower(),
      False)

print("a reset that silently does nothing")
reset_desk()
DESK.reset_does_nothing = True
res = run_job(server.action_show_reset(backup_first=False))
check("it is not reported as success", res["ok"], False)
check_in("and it reassures rather than alarms", "nothing has been lost", res["message"])
# A DESK THAT IS STILL TALKING MUST NOT BE TOLD TO REPLUG ITSELF. Those are two different failures
# and one draft of _reset_and_wait gave both the same words.
check("and does NOT tell them to unplug anything",
      "unplug" in res["message"].lower(), False)

print("a reset where the desk stops answering altogether")
# THE DESK GOES AWAY BETWEEN THE COMMAND AND THE VERIFICATION, which is what a USB dropout looks
# like from inside a job: the baseline read works, the write goes out, and the read that would
# confirm it never answers. Counting the calls is what stages it - the first is the baseline.
reset_desk()
DESK.reset_does_nothing = True
_reads = [0]


def _dies_after_first(*a, **k):
    _reads[0] += 1
    if _reads[0] > 1:
        raise OSError("device gone")
    return DESK.blob()


rcp_read.read_model_stable = _dies_after_first
res = run_job(server.action_show_reset(backup_first=False))
check("it is not reported as success", res["ok"], False)
check_in("and it says to replug the cable", "plug it back in", res["message"])
check_in("and does not claim the reset failed", "almost certainly carried out", res["message"])
check("and does not claim nothing was lost, which it cannot know",
      "nothing has been lost" in res["message"].lower(), False)
install()                                                     # put the working fake back

print("the clean restore's reset half")
reset_desk()
out = server._clean_restore_reset(lambda msg: None)
check("it backs up then resets", DESK.calls[:2], ["export", "reset"])
check("and reports ok", out["ok"], True)
check_in("naming the backup it took", "after backing it up", out["message"])

reset_desk()
DESK.export_does_nothing = True
out = server._clean_restore_reset(lambda msg: None)
check("a failed backup stops the clean restore", out["ok"], False)
check("THE DESK WAS NOT RESET", DESK.current_uuid, "show-A")
check("no reset was sent", "reset" in DESK.calls, False)

print("one job at a time")
reset_desk()
# A REAL job is held open rather than the state flag being poked, so this exercises the actual
# claim in _BackgroundJob.start. All four show commands share one job state because they are
# mutually exclusive on the desk.
_held = threading.Event()
server._show_job.start(lambda job: (_held.wait(10), {"ok": True, "message": "held"})[1],
                       message="holding the job open")
busy = server.action_show_export()
check("a second command is refused while one runs", busy["ok"], False)
check_in("and says why", "already running", busy["message"])
_held.set()
_deadline = time.time() + 10
while time.time() < _deadline and server._show_job.busy():
    time.sleep(0.02)
check("and the held job releases it again", server._show_job.busy(), False)

print("the write gate")
# A SYNTHETIC DESK, not the Duo. The gate is about the CAP, not about any particular desk, so it is
# proved with a device that lacks the cap. Gating on a real device instead makes the whole block
# fail on correct behaviour the day that desk turns the cap on.
reset_desk()
import dataclasses
import devices
CAPLESS = dataclasses.replace(devices.DUO, name="RODECaster Nocaps",
                              caps=devices.DUO.caps - {"shows"})
check("the synthetic desk really lacks the cap", "shows" in CAPLESS.caps, False)
real = devices.get_active_device
devices.get_active_device = lambda: CAPLESS
try:
    gate = server.action_show_export()
    check("a desk without the cap refuses to export", gate["ok"], False)
    check_in("naming the device", "Nocaps", gate["message"])
    check_in("and saying the reset is why it will not guess", "factory reset", gate["message"])
    check("it refuses to reset", server.action_show_reset()["ok"], False)
    check("it refuses to import", server.action_show_import(1)["ok"], False)
    check("nothing was sent to it", DESK.calls, [])
    # A CLEAN restore is refused up front rather than quietly downgraded to a merge: somebody who
    # asked for an exact match must not be told they got one.
    #
    # Reaching that gate needs a profile this desk would otherwise ACCEPT, since restore_profile
    # checks, in order: does the profile exist, was it saved on this model, and only then can this
    # model do a clean restore. Both earlier checks are right to come first, so the test supplies a
    # matching profile rather than the code being reordered to suit it. CAPLESS keeps the Duo's
    # product_id, so the "saved on this model" check passes and the CAP is what refuses.
    import profiles as profiles_mod
    real_load = profiles_mod.load
    profiles_mod.load = lambda name: {"ok": True, "safe": "duo-profile",
                                      "prof": {"product_id": CAPLESS.product_id, "pads": []}}
    try:
        clean = server.restore_profile("duo-profile", clean=True)
        check("a clean restore without the cap is refused, not downgraded", clean["ok"], False)
        check_in("and it offers the alternative", "restore without the reset", clean["message"])
        check_in("and says what that alternative leaves alone", "does not cover", clean["message"])
        check("no restore was started", server._restore_job.busy(), False)
        # The MERGE restore is still allowed on the same device and the same profile: the gate is
        # about the reset, not about restoring.
        merged = server.restore_profile("duo-profile", clean=False)
        check("but a merge restore is still allowed", merged.get("ok"), True)
        deadline = time.time() + 20
        while time.time() < deadline and server._restore_job.busy():
            time.sleep(0.05)
        check("and it ran to completion", server._restore_job.busy(), False)
    finally:
        profiles_mod.load = real_load
finally:
    devices.get_active_device = real

print("a CLEAN restore, end to end")
reset_desk()
import profiles as profiles_mod
real_load = profiles_mod.load
profiles_mod.load = lambda name: {"ok": True, "safe": "p", "prof": {"pads": []}}
try:
    started = server.restore_profile("p", clean=True)
    check("it starts", started.get("ok"), True)
    deadline = time.time() + 30
    while time.time() < deadline and server._restore_job.busy():
        time.sleep(0.05)
    final = server._restore_job.status()
    check("it finishes", final["running"], False)
    # ORDER IS THE WHOLE POINT: back up, reset, THEN write the profile. A reset after the writes
    # would erase exactly what the restore had just put back.
    check("it backed up first", DESK.calls[0], "export")
    check("then reset", DESK.calls[1], "reset")
    check("the desk really was reset", DESK.current_uuid, "factory-fresh")
    check_in("and the result says the reset happened", "reset the desk to factory settings first",
             final["message"])
    check_in("naming the backup that undoes it", "after backing it up", final["message"])
finally:
    profiles_mod.load = real_load

print("a clean restore whose backup fails changes NOTHING")
reset_desk()
profiles_mod.load = lambda name: {"ok": True, "safe": "p", "prof": {"pads": []}}
try:
    server.restore_profile("p", clean=True)
    deadline = time.time() + 30
    while time.time() < deadline and server._restore_job.busy():
        time.sleep(0.05)
    DESK.export_does_nothing = True
    reset_desk()
    DESK.export_does_nothing = True
    server.restore_profile("p", clean=True)
    deadline = time.time() + 30
    while time.time() < deadline and server._restore_job.busy():
        time.sleep(0.05)
    final = server._restore_job.status()
    check("the restore is reported as failed", final["ok"], False)
    check("THE DESK WAS NOT RESET", DESK.current_uuid, "show-A")
    check("and no reset was sent", "reset" in DESK.calls, False)
    check_in("and it says nothing changed", "nothing on the desk has changed", final["message"])
finally:
    profiles_mod.load = real_load

print("bad handles")
reset_desk()
check("import with no handle is refused", server.action_show_import(None)["ok"], False)
check("delete with no handle is refused", server.action_show_delete("abc")["ok"], False)
check("nothing was sent", DESK.calls, [])

print(f"\nALL {len(PASSED)} SIMULATED-DESK CHECKS PASSED")
print("REMEMBER: this proves the decisions, not the bytes. All four commands are hardware-proven "
      "; the FAULTS above are what only a fake desk can stage.")
