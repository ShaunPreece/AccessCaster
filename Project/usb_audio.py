"""
AccessCaster - USB audio: what the desk sends this computer (the output half of `outputMultiMode`),
Standard or Expanded USB input (its other half), the stream's raw switch, and RODE's Virtual Devices
driver, whose endpoints are kept in line with the mode and which can be fetched on request. The
driver's state and Windows' endpoints are virtual_devices.py, the download driver_install.py.
"""

from __future__ import annotations

import threading
import time
import webbrowser

import announcer
import channels
import desk_access
import devices
import diagnostics
import driver_install
import faders
import master_outputs
import rcp_hid
import rcp_read
import rcp_transfer
import recorder
import speech
import virtual_devices
import wording


def read_usb_driver() -> dict:
    """The Routing page's driver line on its own, for its poll. No device read: the service's
    status block and Windows' endpoint list, with the desk's USB mode as last published (every
    re-enumeration republishes it), so the line follows a bind landing, a retry, or the desk
    leaving Expanded without the page being re-read."""
    known = channels.usb_expanded_known()
    return {"ok": True,
            "usb_driver": virtual_devices.describe(channels.usb_expanded() if known else None),
            # The page holds both USB dropdowns while this is True: a switch made now would
            # land in the middle of the bind wait or its retries.
            "switching": _virtual_devices_following.locked()}


def action_set_usb_multitrack(mode: int) -> dict:
    """What the desk sends this computer over USB: the mix pair, or every channel.

    Changing this makes the desk tear down and re-advertise its whole USB audio function: every
    RODECaster input and output on this machine disappears for a second or two. THE WARNING IS
    THE CONTROL'S HINT, read before Enter, NOT THE REPLY: the reply lands while the audio is gone,
    on top of Windows and RODE's driver announcing the same change, so it says the result and no
    more."""
    gate = master_outputs.write_gate()
    if gate is not None:
        return gate
    if mode not in recorder.USB_MULTITRACK_LABEL:
        return {"ok": False, "message": f"USB multitrack mode {mode} is not valid."}
    with desk_access.hid_lock:
        # READ FIRST so the input half is carried through. The desk holds both in one property, so
        # writing the mode alone would take a desk out of Expanded without being asked to.
        old = rcp_read.first(rcp_read.read_model_stable(fresh=True), "outputMultiMode", "int")
        _, expanded = recorder.split_usb_multitrack(old)
        value = recorder.join_usb_multitrack(mode, expanded)
        arrived = _write_usb_mode_parked(value, old)
    return {"ok": True,
            "message": (f"Receiving {recorder.USB_MULTITRACK_LABEL[mode].lower()}."
                        + _virtual_devices_follow(value, arrived))}


def _clear_extended_faders() -> list:
    """Take Game, Music, A and B off every fader that carries one, and return their names.

    THE TOUCHSCREEN DOES THIS AND THE DESK DOES NOT. Leaving Expanded on the unit's screen asks
    "All extended inputs will be unassigned" and empties those faders; the same change written over
    USB leaves them assigned to inputs nothing can feed, so the faders go dead. Each channel's
    processing is parked by `faders.follow_chains`, exactly as when a channel is moved off a fader by
    hand. Holds the HID lock. Writes nothing when no fader carries one of the four."""
    spec = faders.active_faders()
    extended = {code for code, _label in spec.expanded_sources}
    if not extended or faders.fader_write_gate() is not None:
        return []
    with desk_access.hid_lock:
        blob = rcp_read.read_model()
        old = faders.read_fader_vector(blob, spec)
        dead = [i for i, code in enumerate(old) if code in extended]
        if not dead:
            return []
        chains_before = channels.read_chains(blob) if faders.chain_follow_on() else None
        for i in dead:
            rcp_transfer.set_fader_source(i, -1)
        new = faders.read_fader_vector(spec=spec)
        if chains_before is not None:
            faders.follow_chains(old, new, chains_before)
    labels = spec.labels_for(True)
    return [labels.get(old[i], f"source {old[i]}") for i in dead]


def _tidy_after_standard(spoken: bool) -> str:
    """Empty the faders the Expanded four left behind once the desk is in Standard, and word it.

    For the cases the app did not cause: the desk dropping out of Expanded on its own, or being
    found in Standard at start-up with the four still on faders. `spoken` sends the sentence to the
    announcer as well as returning it."""
    try:
        cleared = _clear_extended_faders()
    except Exception as exc:
        diagnostics.event("usb_mode.tidy_failed", f"{exc}")
        return ""
    if not cleared:
        return ""
    msg = (f"USB input is Standard, so {wording.join_and(cleared)} cannot carry anything. Took "
           f"them off their faders; their processing is kept.")
    if spoken:
        speech.say(msg)
    return msg


def action_set_usb_expanded(on: bool) -> dict:
    """The USB 1 INPUT half of the same property: Standard, or Expanded with four more app inputs.

    Both costs are in the control's hint and the reply says only the result, as for the output
    mode above. Turning it OFF also takes Game, Music, A and B off their faders, as the unit's own
    screen does, and that is done FIRST, while the desk is still on the bus: the mode change takes
    it off USB for a couple of seconds."""
    gate = master_outputs.write_gate()
    if gate is not None:
        return gate
    if on is None:
        return {"ok": False, "message": "Expanded needs on or off."}
    cleared = [] if on else _clear_extended_faders()
    with desk_access.hid_lock:
        old = rcp_read.first(rcp_read.read_model_stable(fresh=True), "outputMultiMode", "int")
        mode, _ = recorder.split_usb_multitrack(old)
        value = recorder.join_usb_multitrack(mode or 0, bool(on))
        arrived = _write_usb_mode_parked(value, old)
    tail = ("" if on or not cleared else
            f" {wording.join_and(cleared)} came off their faders.")
    # The one step left to the user, said once the driver's state is known.
    next_step = f" {recorder.USB_EXPANDED_NEXT}" if on else ""
    return {"ok": True,
            "message": (f"Expanded is {'on' if on else 'off'}.{tail}"
                        + _virtual_devices_follow(value, arrived) + next_step)}


def seed_usb_expanded() -> None:
    """Publish the desk's USB mode once at startup, so no page has to be opened first.

    Takes the HID lock like any other read and never raises: a desk that is not there yet simply
    leaves the flag unpublished, and the next routing read or announcer seed fills it in."""
    try:
        if not channels.usb_expanded_known():
            with desk_access.hid_lock:
                rcp_read.read_model_stable(fresh=True)
        # A desk found in Standard with the Expanded four still on faders: tidy it now.
        if channels.usb_expanded_known() and not channels.usb_expanded():
            _tidy_after_standard(spoken=True)
    except Exception as exc:
        diagnostics.event("usb_mode.seed_failed", f"{exc}")


_virtual_devices_busy = threading.Lock()
_virtual_devices_watch_stop = threading.Event()
_virtual_devices_watch = None
# Held for a follow-up's whole run, retries included, and taken BEFORE its thread starts, so the
# reply to a mode write never goes out with the switch looking finished. It is what keeps a second
# follow-up from starting, and what the Routing page reads as "a USB switch is still in progress".
_virtual_devices_following = threading.Lock()


def _virtual_devices_repair() -> None:
    """Windows' endpoints moved: make them agree with the driver again. Silent, and never speaks.

    THE LOCK IS WHY THIS IS SAFE DURING A MODE CHANGE. `_virtual_devices_follow` holds it for the
    whole of its wait and its retries (it waits for a repair to finish before taking it), and
    those retries re-enumerate the desk on purpose, so every endpoint change they cause is skipped
    here rather than fought over. What reaches this is a change with no follow-up behind it.

    `move_default` stays off: a disable is not a reason to take the output device away from
    wherever the user has put it, and the mode-change path already moves it when it has to.

    A repair is itself an endpoint change, so it wakes the watcher again. That settles rather than
    runs away because `reconcile` is idempotent: the second pass finds Windows already agreeing and
    changes nothing, so nothing wakes a third."""
    if not _virtual_devices_busy.acquire(blocking=False):
        return
    try:
        out = virtual_devices.reconcile(move_default=False)
        if out.enabled or out.disabled:
            diagnostics.event("virtual_devices.repaired", out.status.phrase(),
                              enabled=len(out.enabled), disabled=len(out.disabled))
    except Exception as exc:
        diagnostics.event("virtual_devices.repair_failed", f"{exc}")
    finally:
        _virtual_devices_busy.release()


def start_virtual_devices() -> None:
    """Make Windows agree with RODE's driver now, and keep it agreeing.

    The first pass applies a mode change made while this app was not running. It waits so the
    announcer's greeting comes first, and is silent unless something changes. The pass after a
    mode write acts on a single sample, so a bind that lands a moment later leaves Windows holding
    endpoints this app switched off itself: Windows announces every endpoint change, and the
    watcher listens and repairs."""
    global _virtual_devices_watch
    threading.Thread(target=lambda: (time.sleep(4), _virtual_devices_follow(None, False)),
                     daemon=True, name="virtual-devices-start").start()
    _virtual_devices_watch = threading.Thread(
        target=virtual_devices.watch_endpoints,
        args=(_virtual_devices_repair, _virtual_devices_watch_stop),
        daemon=True, name="virtual-devices-watch")
    _virtual_devices_watch.start()


def stop_virtual_devices() -> None:
    """Unregister the endpoint watcher's callback, rather than leave Windows' audio service holding
    a pointer into a process that is going away."""
    _virtual_devices_watch_stop.set()
    if _virtual_devices_watch is not None:
        _virtual_devices_watch.join(timeout=2.0)


# Set while the follow-up is re-enumerating the desk on purpose, so the announcer's reconnect
# does not narrate each flip of the mode as news.
_virtual_devices_retrying = threading.Event()

# RODE'S SERVICE LOSES THE DESK TO ANYTHING PLAYING TO THE DESK'S MAIN OUTPUT WHEN IT ARRIVES
# (`virtual_devices.park_output` has the mechanism). Parking Windows' default elsewhere for the
# arrival prevents it where there is somewhere to park; the retries are for where there is not, and
# for a program set to the desk's Main output by name. A retry is a fresh arrival, so a fresh race.
_VIRTUAL_DEVICES_RETRIES = 3
_VIRTUAL_DEVICES_RETRY_HOLD_S = 3.0     # at the other mode: long enough for the service to let go

# The parked default, while one is in force. Module-level because a park outlives the action that
# made it: the follow-up moves the default on to `System (RØDECaster)` once the service binds, and a
# follow-up already running when a second switch parks has to see that park too.
_virtual_devices_parking = None
_PARK_ARRIVAL_S = 20.0      # longest a guard waits for the desk to come back on the bus
_PARK_BIND_S = 6.0          # then how long it holds for the bind, which lands 1.0 to 1.4 s after
_PARK_SYSTEM_S = 3.0        # and for System (RØDECaster) to appear once bound


def _park_for_arrival():
    """Park Windows' default output off the desk before an arrival, and start a guard that holds it
    there across the arrival. Returns the Event to set once the desk is back (or the write has
    failed), or None when nothing was parked."""
    global _virtual_devices_parking
    parking = _virtual_devices_parking or virtual_devices.park_output()
    if parking is None:
        return None
    _virtual_devices_parking = parking
    arrived = threading.Event()

    # NEVER MUTED. A muted parking device is silence for anyone whose speech follows the default,
    # and if this process dies mid-park nothing puts it back.
    def guard():
        """Windows moves the default back onto the desk the moment the desk re-appears; this puts
        it back each time until the service has bound. "Bound" only counts once the service has
        been seen unbound, because its flag still reads connected for a moment after the desk
        leaves the bus."""
        seen_down = False
        t = time.monotonic()
        while not arrived.is_set() and time.monotonic() - t < _PARK_ARRIVAL_S:
            virtual_devices.keep_parked(parking)
            seen_down = seen_down or not virtual_devices.read_status().connected
            time.sleep(0.05)
        t = time.monotonic()
        while time.monotonic() - t < _PARK_BIND_S:
            virtual_devices.keep_parked(parking)
            connected = virtual_devices.read_status().connected
            if connected and seen_down:
                virtual_devices.default_to_system(_PARK_SYSTEM_S)
                break
            seen_down = seen_down or not connected
            time.sleep(0.05)

    threading.Thread(target=guard, daemon=True, name="park-guard").start()
    return arrived


def _usb_mode_arrives_expanded(old, new: int) -> bool:
    """Whether writing `new` over `old` brings the desk back as an Expanded USB device, which is
    when RODE's service races Windows for it. Expanded with the mix alone and Expanded with every
    channel are different devices; pre-fader and post-fader are the same one. An unknown `old`
    counts as an arrival."""
    new_mode, new_exp = recorder.split_usb_multitrack(new)
    if not new_exp:
        return False
    old_mode, old_exp = recorder.split_usb_multitrack(old)
    return not old_exp or (old_mode == 0) != (new_mode == 0)


def _usb_mode_leaves_expanded(old, new: int) -> bool:
    """Whether writing `new` over `old` takes the desk out of Expanded, which takes `System
    (RØDECaster)` away with it."""
    return bool(recorder.split_usb_multitrack(old)[1]) and not recorder.split_usb_multitrack(new)[1]


def _write_usb_mode_parked(value: int, old) -> bool:
    """`_write_usb_mode` with the default parked across the arrival when the desk is coming back
    as an Expanded device (`_usb_mode_arrives_expanded`). Caller holds the HID lock.

    LEAVING EXPANDED PARKS TOO, with no guard. `System` goes before the desk's Main output comes
    back, so Windows falls back to another live device, and a default found there afterwards looks
    like the user's own choice. Parking first marks it as ours, and the follow-up in Standard moves
    it on to the desk's Main output once that appears."""
    global _virtual_devices_parking
    if _usb_mode_leaves_expanded(old, value):
        # A fresh park first: one left from the arrival names a device the default has since left.
        _virtual_devices_parking = virtual_devices.park_output() or _virtual_devices_parking
    arrived = _park_for_arrival() if _usb_mode_arrives_expanded(old, value) else None
    try:
        return _write_usb_mode(value)
    finally:
        if arrived is not None:
            arrived.set()


def _desk_pid():
    """The desk's USB product id now, or None when it is off the bus."""
    iface = rcp_hid.find_control_interface()
    return iface.product_id if iface is not None else None


def _write_usb_mode(value: int) -> bool:
    """Write `outputMultiMode` and return whether the desk came back as a different USB device.

    Only that is an ARRIVAL for RODE's service, which matches the desk by product id. Pre-fader
    and post-fader in Expanded are the same device (0x0094) and the desk does not leave the bus
    between them, so that switch gives the service nothing to bind to and is not a bind attempt.
    Caller holds the HID lock."""
    global _usb_mode_written
    before = _desk_pid()
    _usb_mode_written = (value, time.monotonic())
    rcp_transfer.set_usb_multitrack_mode(value)
    return _desk_pid() != before


# The last mode this app wrote, and when. The announcer's reconnect stays quiet about a mode the
# app's own switch set, since the page's reply has said it; one from the touchscreen is still said.
_usb_mode_written = (None, 0.0)
_USB_MODE_WRITTEN_S = 30.0      # a reconnect lands within seconds; older than this is not ours


def _usb_mode_written_by_app(mode) -> bool:
    """Whether the desk's mode `mode` is one this app has just written. Consumed on a match, so a
    later change to the same mode from the touchscreen is still said."""
    global _usb_mode_written
    value, at = _usb_mode_written
    if mode is None or value != mode or time.monotonic() - at > _USB_MODE_WRITTEN_S:
        return False
    _usb_mode_written = (None, 0.0)
    return True


def _desk_usb_mode():
    """`outputMultiMode` as the desk holds it now, or None when it cannot be read. Takes the HID
    lock."""
    try:
        with desk_access.hid_lock:
            return rcp_read.first(rcp_read.read_model_stable(fresh=True), "outputMultiMode", "int")
    except Exception as exc:
        diagnostics.event("device.read_failed", "USB mode", error=str(exc))
        return None


def _virtual_devices_expectation(raw_mode):
    """What `virtual_devices.reconcile` should expect of the service in this mode. True: the
    desk is sending every channel in Expanded, so a bind should come and is waited for. None:
    the mode is unknown, or Expanded with the mix alone, where the service may bind and its flag
    is trusted as read. False: Standard, where it cannot be bound at all."""
    mode, expanded = recorder.split_usb_multitrack(raw_mode)
    if expanded is None:
        return None
    if not expanded:
        return False
    return True if mode else None


def _virtual_devices_follow(raw_mode, arrived: bool) -> str:
    """Bring RODE's virtual devices into line with the USB mode the desk now holds, and say so.

    The work runs on a thread and its outcome is SPOKEN, because the service takes seconds to
    bind after the desk re-enumerates, longer with retries, and a reply held that long reads as
    a hang. The string returned is the reply's tail: what the user should know now, which is
    only the case where nothing is coming (no driver, or a mode it will not be waited for in).

    THE DESK'S MODE IS RE-READ BEFORE EVERY RETRY AND BEFORE ANYTHING IS SAID. A follow-up with
    its retries runs for over a minute, and only one runs at a time, so a mode the person
    changes meanwhile gets no follow-up of its own: this one moves on to it instead. Retrying
    with the mode it started with puts the desk back into a mode the person has just left, and
    judging the outcome against it reports fader inputs missing from a mode that has none.

    `arrived` says the desk has just come back as a different USB device, which is the only
    case where the service gets a chance to bind; only then is the attempt recorded in the bind
    history (`virtual_devices.record_bind`)."""
    mode, expanded = recorder.split_usb_multitrack(raw_mode)

    def rearrive(value: int):
        """Re-enumerate the desk on purpose: the output mode off and back, Expanded kept, with the
        default parked for the arrival. The lock is held across the hold, so a person's own write
        waits and lands after this one."""
        with desk_access.hid_lock:
            rcp_transfer.set_usb_multitrack_mode(recorder.join_usb_multitrack(0, True))
            time.sleep(_VIRTUAL_DEVICES_RETRY_HOLD_S)
            _write_usb_mode_parked(value, recorder.join_usb_multitrack(0, True))

    def parked_id() -> str:
        return _virtual_devices_parking.id if _virtual_devices_parking is not None else ""

    def run():
        global _virtual_devices_parking
        # Waits out a repair, which is short; another follow-up cannot be running (see below).
        _virtual_devices_busy.acquire()
        try:
            value, fresh, pid = raw_mode, arrived, _desk_pid()

            def moved() -> bool:
                """Whether someone has changed the mode since `value`; if so, follow theirs."""
                nonlocal value, fresh, pid
                held = _desk_usb_mode() if value is not None else None
                if held is None or held == value:
                    return False
                now = _desk_pid()
                value, fresh, pid = held, now != pid, now
                return True

            # A move that is not an arrival (pre-fader to post-fader in Expanded) gives the
            # service nothing new to bind to, so the retries it was owed carry on rather than
            # starting again, and the miss is not announced twice.
            attempts = 0
            while True:
                expect, arrival = _virtual_devices_expectation(value), fresh
                # An arrival nobody parked for (the touchscreen, the desk dropping out): park now.
                # The race may already be lost, but the retries below then start clear.
                if (expect and arrival and _virtual_devices_parking is None
                        and not virtual_devices.read_status().connected):
                    late = _park_for_arrival()
                    if late is not None:
                        late.set()
                # Waiting for a bind only makes sense after an arrival; otherwise read it as is.
                out = virtual_devices.reconcile(
                    expect_connected=expect if (arrival or not expect) else None,
                    parked=parked_id())
                left = moved()
                # Recorded even when the mode has since moved: a wait that ran out before the
                # desk left the bus is still a missed bind, and one it left during is cut short.
                if expect and arrival and out.status.running:
                    virtual_devices.record_bind(0, out.status.connected, cut_short=left and fresh)
                if left:
                    attempts = 0 if fresh else attempts
                    continue
                while (expect and out.status.running and out.status.enabled
                       and not out.status.connected and attempts < _VIRTUAL_DEVICES_RETRIES):
                    attempts += 1
                    if attempts == 1:
                        speech.say("RODE's driver missed the desk. Reconnecting it; the audio drops "
                                 "again for a moment.")
                    _virtual_devices_retrying.set()
                    try:
                        rearrive(int(value))
                    except Exception as exc:
                        diagnostics.event("virtual_devices.retry_failed", f"{exc}")
                        break
                    finally:
                        _virtual_devices_retrying.clear()
                    diagnostics.event("virtual_devices.retry", "re-enumerated the desk",
                                      attempt=attempts)
                    out = virtual_devices.reconcile(expect_connected=True, parked=parked_id())
                    left = moved()
                    virtual_devices.record_bind(attempts, out.status.connected,
                                                cut_short=left and fresh)
                    if left:
                        break
                if left:
                    attempts = 0 if fresh else attempts
                    continue
                break
            # Bound, or out of Expanded: the park has done its job, and reconcile has already moved
            # the default on from it. Unbound, it stays parked, so the next arrival starts clear.
            parking = _virtual_devices_parking
            if out.status.connected or not recorder.split_usb_multitrack(value)[1]:
                _virtual_devices_parking = None
            if out.sentence():
                speech.say(out.sentence())
            elif expect and out.status.running and not out.status.connected:
                # STATE, NEVER AN INSTRUCTION. A replug is one more arrival in the same race, so
                # asking for one reads as "this will fix it" when it may well not.
                where = (f" Windows sound stays on {parking.name} until it does."
                         if parking is not None else "")
                speech.say("RODE's driver has not connected to the desk, so the extra inputs are "
                         "not there." + where)
        finally:
            _virtual_devices_busy.release()
            _virtual_devices_following.release()

    # ONE FOLLOW-UP PER RE-ENUMERATION. A mode write and the announcer's reconnect both see the
    # same event a second apart, and two threads waiting on the same bind would announce the same
    # inputs twice. The one already running covers it, retries and later mode changes included.
    if _virtual_devices_following.acquire(blocking=False):
        threading.Thread(target=run, daemon=True, name="virtual-devices").start()
    if not expanded:
        return ""
    st = virtual_devices.read_status()
    if not (st.installed and st.running and st.enabled):
        return f" {st.phrase()} Game, Music, A and B will be silent."
    if not mode:
        # The service can bind here too, but the mix alone carries no fader inputs to expose,
        # so nothing is waited for and the user is told what the four need.
        return " Game, Music, A and B also need this computer to receive every channel."
    if not arrived and st.connected:
        return ""       # the desk stayed on the bus and the service stays bound: nothing to wait for
    # The bind itself is announced by RODE's driver, so only the parked default is ours to say.
    parking = _virtual_devices_parking
    return (f" Windows sound is on {parking.name} until RODE's driver connects."
            if parking is not None else "")


def reconnected(was_usb, now_usb) -> None:
    """What a reconnect means for the USB mode and RODE's virtual devices: the announcer's hook.
    Runs on the announcer's thread, which must keep speaking, so anything that writes goes to a
    thread of its own."""
    if _virtual_devices_retrying.is_set():
        return      # our own flip of the mode, already announced once as a retry
    # A mode the app's own switch wrote has been said by the page's reply.
    if not _usb_mode_written_by_app(now_usb):
        speech.say(announcer.usb_mode_change(was_usb, now_usb)
                   or f"{devices.get_active_device().name} reconnected.")
    # Out of Expanded with nobody using the app's switch (the touchscreen tidies its own faders;
    # the desk dropping out on its own does not).
    if (was_usb is not None and now_usb is not None
            and recorder.split_usb_multitrack(was_usb)[1]
            and not recorder.split_usb_multitrack(now_usb)[1]):
        threading.Thread(target=_tidy_after_standard, args=(True,),
                         daemon=True, name="expanded-tidy").start()
    # The desk re-enumerated, whoever caused it: the touchscreen, a profile, or the desk leaving
    # Expanded on its own. RODE's virtual devices need the same follow-up either way, and this is
    # the one place that sees every case.
    _virtual_devices_follow(now_usb, True)


def action_set_usb_bypass(on: bool) -> dict:
    """The USB stream's raw switch: what this computer receives. Routing > USB audio."""
    gate = master_outputs.write_gate()
    if gate is not None:
        return gate
    with desk_access.hid_lock:
        rcp_transfer.set_usb_multitrack_bypass(on)
    return {"ok": True, "message": recorder.bypass_sentence("The tracks sent to this computer", on)}


# RODE'S DRIVER, FETCHED ON REQUEST. Its own job state rather than a shared one: it is the only
# job in the app that touches the network instead of the desk, so it can run while a desk job is
# going and must not be refused by one.
_driver_job = desk_access.BackgroundJob("The driver install is already running.",
                                        "The driver could not be installed",
                                        "The driver install is running.")


def driver_install_status() -> dict:
    return _driver_job.status()


def action_install_driver() -> dict:
    """Fetch RODE's Virtual Devices driver, check its signature and hand it to Windows.

    **Reaches the internet only from this press**, never at startup or on a timer. See
    driver_install.py for why, and for why the signature rather than a hash is what gets checked.

    REFUSED WHEN IT IS ALREADY INSTALLED, because the honest answer there is not "installing" but
    "you already have it": a second install of the same version tells the user nothing and costs
    them a UAC prompt and a reboot prompt. Repairing a broken install is RODE's installer's job,
    reachable through the page button beside this one.
    """
    if virtual_devices.read_status().installed:
        return {"ok": False,
                "message": ("RODE's Virtual Devices driver is already installed on this computer. "
                            "If it is not working, use \"Open RODE's software page\" to reinstall "
                            "or repair it.")}

    def _work(job):
        try:
            got = driver_install.install(on_step=job.step)
        except driver_install.DriverInstallError as exc:
            diagnostics.event("driver.install_failed", f"{exc}")
            return {"ok": False, "message": str(exc)}
        diagnostics.event("driver.handed_over", got["signer"])
        return {"ok": True,
                "message": ("RODE's installer is now running. Follow its prompts, allow the "
                            "Windows permission request, and restart the computer if it asks. "
                            "This page will show the driver once it is in.")}

    return _driver_job.start(_work,
                             message="Downloading RODE's driver, please wait.",
                             started="Install RODE's driver",
                             failed="The driver install failed")


def action_open_driver_page() -> dict:
    """Open RODE's own software page in the browser: the manual route, offered as its own button
    rather than only after a failure, so nobody has to make something go wrong to find it."""
    try:
        webbrowser.open(driver_install.PAGE_URL)
    except Exception as exc:
        return {"ok": False,
                "message": (f"The page could not be opened ({exc}). Go to {driver_install.PAGE_URL} "
                            f"in your browser and look for the RODECaster Virtual Device Driver.")}
    return {"ok": True,
            "message": ("RODE's software page is opening in your browser. Look for the RODECaster "
                        "Virtual Device Driver, download it and run the installer inside.")}
