"""
AccessCaster - the desk's network: wifi and Bluetooth on Device > Wireless connections, read and
written with no touchscreen, and the wireless mics the same page lists.
"""

from __future__ import annotations

import time

import desk_access
import devices
import diagnostics
import rcp_read
import rcp_transfer
import wording


# What Device > Wireless connections shows from `read_status`, for following the desk. The
# wifi scan results are left out: the desk never pushes them (see the scan, below).
PROPS = frozenset(
    ["wifi", "wifiSSID", "wifiIpAddress", "wifiIncorrectPSK", "wifiPSK", "btConnectedAddress",
     "btVisible", "txConnected", "txBatteryLevel", "txRssi"]
    + [f"btPairedNumber{n}" for n in range(1, 6)] + [f"btScan{n}" for n in range(1, 11)])


def wireless_mics(blob) -> dict:
    """RODE wireless transmitters (Series IV etc.) as the unit reports them, for Wireless
    connections.

    PRESENT and CONNECTED are different facts and the page needs both. An empty slot list means the
    dump carried no txConnected at all, which is not the same as "no mic is on" and must not be
    read out as if it were. Unverified with a paired transmitter.
    """
    def col(name, kind=None):
        return rcp_read.values(blob, name, kind)

    conn = col("txConnected")
    batt = col("txBatteryLevel", "int")
    rssi = col("txRssi", "int")
    tx = [{"index": i, "connected": bool(c),
           "battery_pct": batt[i] if i < len(batt) else None,
           "rssi": rssi[i] if i < len(rssi) else None}
          for i, c in enumerate(conn)]
    return {"transmitters": tx,
            "present": bool(tx),
            "any_connected": any(t["connected"] for t in tx)}


def connection(blob: bytes) -> dict:
    """How the unit reaches the internet, read BY BLOCK (see read_status for why). A wifi
    address means joined; the radio being on does not."""
    def net(name):
        return rcp_read.block_value(blob, "NETWORK", name)
    wired = next((v for v in rcp_read.values(blob, "wiredConnected") if v is not None), None)
    ip = net("wifiIpAddress") or ""
    return {"wifi_on": bool(net("wifi")), "wifi_ssid": net("wifiSSID") or "", "wifi_ip": ip,
            "wired": bool(wired), "online": bool(ip) or bool(wired)}


def read_status() -> dict:
    """Live-read wifi, Bluetooth and the wireless mics from the device model. Read-only.

    These all live in the property model (rcp_read), so one model dump answers
    everything. Never changes device state.
    """
    try:
        with desk_access.hid_lock:
            blob = rcp_read.read_model()
    except Exception as exc:
        return {"ok": False,
                "message": f"Could not read network info from the device: {exc}"}

    def first(name):
        return rcp_read.first(blob, name)

    def net(name):
        """A NETWORK-block value, by block rather than by "first in the stream".

        `first()` cannot be used for a name that might occur in more than one block: False is a
        meaningful value, so it has no way to tell a real answer from the wrong block's. A stray
        can be appended to any block by one malformed write (a stray `btVisible` in SYSTEM reads
        False while the radio is discoverable), so a name is only unambiguous with its block.
        """
        return rcp_read.block_value(blob, "NETWORK", name)

    def slots(prefix, count, start=1):
        """The non-blank entries of a numbered slot array (btScan1..10, wifiScan1..10)."""
        out = []
        for n in range(start, start + count):
            v = first(f"{prefix}{n}")
            if v:
                out.append(str(v))
        return out

    ssid = first("wifiSSID")
    ip = first("wifiIpAddress")
    gateway = first("wifiGateway")
    dhcp = first("wifiDHCP")
    incorrect = first("wifiIncorrectPSK")
    bt_addr = first("btConnectedAddress")
    bt_visible = net("btVisible")
    bt_paired = slots("btPairedNumber", 5)
    return {
        "ok": True,
        "wifi": {
            # The RADIO, which is not the same fact as "connected": it can be on and not yet
            # joined to anything. Read BY BLOCK, like everything else here that has a duplicate.
            "on": bool(net("wifi")),
            "ssid": ssid or "",
            "ip": ip or "",
            "gateway": gateway or "",
            "dhcp": bool(dhcp) if dhcp is not None else None,
            "connected": bool(ip),               # has an IP => joined a network
            "incorrect_psk": bool(incorrect) if incorrect is not None else None,
            # Whatever the unit's last scan found, by the app, the touchscreen or the radio coming
            # up unjoined. Empty means "nothing scanned for yet" as often as "nothing in range".
            "scan": wifi_scan_slots(blob),
            # Never returned, and this is deliberate rather than an oversight: firmware before
            # 1.7.6 publishes wifiPSK, and putting a password into a reply, the window and a log
            # line is not a thing to do casually. Whether the network HAS a password is
            # enough. On 1.7.6 it reads empty, so this is False there whatever is stored.
            "has_password": bool(first("wifiPSK")),
        },
        "bluetooth": {
            "connected_address": bt_addr or "",
            "connected": bool(bt_addr),
            "visible": bool(bt_visible) if bt_visible is not None else None,
            "paired": bt_paired,
            "scan": slots("btScan", 10),
        },
        # The third radio on the Wireless connections tab. The channels reply uses the same
        # helper, so the two readings cannot drift apart.
        "wireless_mics": wireless_mics(blob),
        # WHAT CAN BE CHANGED HERE: the wifi radio, the network joined and discoverability.
        # Everything else on this page is read-only, and the page says WHICH is which rather than
        # leaving a user to find out by pressing things.
        "writable": "network_write" in devices.get_active_device().caps,
        # Bluetooth scanning is stated as what the app offers, never what the desk can do: it
        # behaves differently on the two desks. The fixed IP IS a fact about the desk: `ipAddress`
        # takes no write (PROJECT_GUIDE part 2).
        "still_read_only": (
            "Scanning for Bluetooth devices is not available here. Make the desk discoverable "
            "above and pair from the phone instead. Pairing from here is still being worked on. "
            "A fixed IP address cannot be set: the desk reports its address but takes no other."),
        "message": "Read the network from the device.",
    }


# ----------------------------------------------------------------------------
# NETWORK WRITES. Joining wifi and making the unit discoverable, with no touchscreen.
#
# THE NETWORK BLOCK: `btVisible` written there reads back in the NETWORK block (not the
# SYSTEM duplicate), leaves no stray, and makes the unit discoverable. `wifiSSID` and `wifiPSK`
# write as strings, and with the radio on the unit holds a DHCP address about 15 seconds later.
# The RODE app's Network screen being read-only says nothing about what the bus can write.
#
# WHAT IS DELIBERATELY NOT OFFERED, and it is not caution:
#   * **Bluetooth scanning.** `btDoScan` is not built. Its SYSTEM occurrence is a STRAY, so any
#     "the write lands but nothing happens" reading taken there is a write into a stray. Making the
#     unit discoverable and pairing FROM the phone is the route offered. Wifi scanning works only
#     while unjoined: see action_scan_wifi.
#   * **Pairing, connecting and unpairing.** `btDoPair` / `btDoConnect` / `btDoDisconnect` /
#     `btDoUnPair` are addressable, but they are ACTIONS taking an address we have never seen the
#     format of, and nothing is paired here to test against. Making the unit DISCOVERABLE and
#     pairing from the phone is the route that is proven and needs no address at all.
#   * **Static IP and DNS.** Derived but untried, and a bad write drops the unit off the network,
#     which is exactly the state that then needs the touchscreen to fix.


def _wifi_state(blob) -> dict:
    """Just enough of the wifi state to report a join honestly, read BY BLOCK.

    Never returns the password. `wifiPSK` is readable on older firmware, and "there is a password
    stored" is the most that should ever leave this process - not in a JSON payload, not in the
    page, not in a log."""
    def net(name):
        return rcp_read.block_value(blob, "NETWORK", name)
    return {"on": bool(net("wifi")), "ssid": net("wifiSSID") or "",
            "ip": net("wifiIpAddress") or "", "has_password": bool(net("wifiPSK")),
            "wrong_password": bool(net("wifiIncorrectPSK"))}


def action_set_wifi(on: bool) -> dict:
    """Turn the wifi radio on or off. `wifi` is an ordinary bool on NETWORK."""
    gate = _write_gate()
    if gate is not None:
        return gate
    try:
        with desk_access.hid_lock:
            rcp_transfer.write_at(rcp_transfer.NETWORK, "wifi", "bool", bool(on))
            time.sleep(1.0)
            state = _wifi_state(rcp_read.read_model_stable(fresh=True))
    except Exception as exc:
        return {"ok": False, "message": f"Could not change the wifi radio: {exc}"}
    if bool(on) != state["on"]:
        return {"ok": False, "wifi": state,
                "message": "The desk did not take that: the wifi radio is still "
                           + ("on" if state["on"] else "off") + "."}
    if not on:
        return {"ok": True, "wifi": state, "message": "Wifi is off."}
    if not state["ssid"]:
        return {"ok": True, "wifi": state, "message": "Wifi is on, but no network is set up yet. Scan for "
                                                       "networks, or type one in, to join."}
    # The radio is on at once, but the network takes several seconds more, and "on" without
    # "connected" leaves the user checking back by hand. So the reply waits for the address.
    if not state["ip"]:
        state = _wait_for_wifi_address() or state
    if state.get("ip"):
        return {"ok": True, "wifi": state, "message": f"Wifi connected to {state['ssid']}."}
    if state.get("wrong_password"):
        return {"ok": True, "wifi": state,
                "message": f"Wifi is on, but {state['ssid']} refused the stored password."}
    return {"ok": True, "wifi": state,
            "message": f"Wifi is on, but has not connected to {state['ssid']} yet. It may be out "
                       f"of range, or still connecting."}


def _wait_for_wifi_address():
    """Poll until the unit has a wifi address or reports a wrong password, for up to
    WIFI_JOIN_TIMEOUT_S. Polls rather than sleeping the whole timeout so a quick join answers
    quickly. Returns the last state read, or None if no read succeeded."""
    deadline = time.monotonic() + WIFI_JOIN_TIMEOUT_S
    state = None
    while time.monotonic() < deadline:
        time.sleep(2.5)
        try:
            with desk_access.hid_lock:
                state = _wifi_state(rcp_read.read_model_stable(fresh=True))
        except Exception as exc:
            diagnostics.event("wifi.read_failed", "still waiting for an address", error=str(exc))
            continue
        if state.get("wrong_password") or state.get("ip"):
            break
    return state


def action_join_wifi(ssid: str, password: str) -> dict:
    """Join a wifi network: SSID, then password, then the radio on, then wait for an address.

    THE ORDER IS LOAD-BEARING. Bringing the radio up before it knows where to go invites an
    association failure that then has to be told apart from a wrong password.

    THE PASSWORD IS HANDLED AS A SECRET THROUGHOUT: it is used once, never stored by us, never
    logged, never echoed back, and never put in a profile. The unit keeps it (it is readable there,
    which is exactly why this code refuses to repeat it).

    The answer is the unit's own IP address or `wifiIncorrectPSK`, not "the write was sent". A blind
    operator has no other way to find out whether it worked."""
    gate = _write_gate()
    if gate is not None:
        return gate
    ssid = (ssid or "").strip()
    if not ssid:
        return {"ok": False, "message": "Give the name of the network to join."}
    if not ssid.isascii():
        return {"ok": False,
                "message": "The desk only takes plain ASCII in a network name, so this one cannot "
                           "be sent from here."}
    if password and not password.isascii():
        return {"ok": False,
                "message": "The desk only takes plain ASCII in a password, so this one cannot be "
                           "sent from here."}
    try:
        if not password:
            # The unit's last scan says which networks need one. Without it the join can only
            # fail, and writing the name would lose the password the unit has stored (see the
            # scan comment below), so nothing is sent.
            with desk_access.hid_lock:
                blob = rcp_read.read_model_stable(fresh=True)
            scanned = next((e for e in map(parse_scan_slot, wifi_scan_slots(blob))
                            if e.get("ssid") == ssid), None)
            if scanned and not scanned["open"]:
                return {"ok": False, "needs_password": True,
                        "message": f"{ssid} needs a password. Type it in the Password box, then "
                                   f"join again."}
        with desk_access.hid_lock:
            net = rcp_transfer.NETWORK
            rcp_transfer.write_at(net, "wifiSSID", "str", ssid)
            time.sleep(0.4)
            if password:
                rcp_transfer.write_at(net, "wifiPSK", "str", password)
                time.sleep(0.4)
            rcp_transfer.write_at(net, "wifi", "bool", True)
    except Exception as exc:
        return {"ok": False, "message": f"Could not send the network details: {exc}"}
    finally:
        del password           # not kept a moment longer than the write needs it

    # Wait for an ADDRESS, which is the only honest evidence of a join.
    state = _wait_for_wifi_address() or {}
    if state.get("wrong_password"):
        return {"ok": False, "wifi": state,
                "message": f"{ssid} refused the password. The name and password are still stored "
                           f"on the desk, so try again with the password corrected."}
    if state.get("ip"):
        return {"ok": True, "wifi": state,
                "message": f"Joined {ssid}. The desk's address is {state['ip']}."}
    return {"ok": False, "wifi": state,
            "message": f"The desk took the details for {ssid} but has no address yet. It did not "
                       f"say the password was wrong, so it may still be connecting, or the network "
                       f"may be out of range. Check again in a moment."}


# How long to wait for an address before answering. The measured join took about 15 seconds, so
# this is roughly three times it: long enough not to report a failure that is really a slow DHCP
# server, short enough that a page is not left hanging with nothing to say.
WIFI_JOIN_TIMEOUT_S = 45.0


def action_set_bt_visible(on: bool) -> dict:
    """Make the unit discoverable, or stop.

    THE DUPLICATE MATTERS HERE. `btVisible` exists in SYSTEM as well as NETWORK, and only the
    NETWORK copy drives the radio - writing the SYSTEM one flips a stored value and leaves the unit
    invisible, with nothing in the read-back to say so. So the write goes to NETWORK and the read-back
    is taken BY BLOCK, never by "first occurrence in the stream"."""
    gate = _write_gate()
    if gate is not None:
        return gate
    try:
        with desk_access.hid_lock:
            rcp_transfer.write_at(rcp_transfer.NETWORK, "btVisible", "bool", bool(on))
            time.sleep(1.0)
            blob = rcp_read.read_model_stable(fresh=True)
        now = bool(rcp_read.block_value(blob, "NETWORK", "btVisible"))
    except Exception as exc:
        return {"ok": False, "message": f"Could not change Bluetooth discoverability: {exc}"}
    if now != bool(on):
        return {"ok": False, "visible": now,
                "message": "The desk did not take that: it is still "
                           + ("discoverable" if now else "not discoverable") + "."}
    if on:
        return {"ok": True, "visible": True,
                "message": "The desk is now discoverable. On your phone or computer, look for "
                           "RODECaster in its Bluetooth list and pair from there. It stops being "
                           "discoverable on its own after a while, so do it now."}
    return {"ok": True, "visible": False,
            "message": "The desk is no longer discoverable. Anything already paired still works."}


# ----------------------------------------------------------------------------
# SCANNING FOR WIFI NETWORKS, AND DISCONNECTING.
#
# THE UNIT SCANS ON ITS OWN EACH TIME ITS RADIO COMES UP WITH NO NETWORK TO JOIN, and only then.
# The results clear about 3 s after the radio comes on and fill about 12 s after it, in
# `wifiScan1..10` and the WIFISCANRESULT blocks alike, each `<SSID>----<security>`. Nothing is
# pushed, so they appear only in a later model dump. While joined it never scans: `wifiScan` (True,
# or False then True) is stored, never cleared, and no result arrives (Pro II, firmware 1.7.6). So a
# scan here is the radio off and on with the unit unjoined, and is refused while joined.
#
# THERE IS NO DISCONNECT OR FORGET PROPERTY. Clearing `wifiSSID` and cycling the radio leaves the
# unit on, unjoined and scanning. Writing `wifiSSID` also loses the stored password: setting the
# old name back does not rejoin.
WIFI_SCAN_SLOTS = 10
WIFI_RADIO_OFF_S = 2.0          # off long enough for the unit to drop the link and the old results
WIFI_SCAN_TIMEOUT_S = 25.0      # results arrive about 12 s after the radio comes on
WIFI_SCAN_POLL_S = 2.0
WIFI_SCAN_SETTLE_S = 5.0        # a second network has arrived 3 s after the first


def parse_scan_slot(raw: str) -> dict:
    """One scan result: `<SSID>----<security>`.

    An SSID may legitimately contain a hyphen, so the split is on the four-hyphen separator from the
    RIGHT and only once. Anything that does not carry a separator is treated as a bare SSID rather
    than discarded, because a network we cannot classify is still one you may want to join."""
    text = str(raw or "").strip()
    if not text:
        return {}
    ssid, sep, security = text.rpartition("----")
    if not sep:
        return {"ssid": text, "security": "", "open": False, "spoken": text}
    ssid, security = ssid.strip(), security.strip()
    # "the security type is blank or reads as none" is the only claim made here. The exact strings
    # this firmware uses beyond that have not been catalogued, so they are passed through as read.
    is_open = security.lower() in ("", "none", "open", "0")
    return {"ssid": ssid, "security": security, "open": is_open,
            "spoken": f"{ssid}, open network" if is_open
                      else f"{ssid}, needs a password" + (f", {security}" if security else "")}


def wifi_scan_slots(blob) -> list:
    """The raw results of the unit's last wifi scan, blanks dropped: the numbered slots, or the
    WIFISCANRESULT blocks (one unnumbered `wifiScanResultSSID` each) if those are empty."""
    numbered = [rcp_read.block_value(blob, "NETWORK", f"wifiScan{n}")
                for n in range(1, WIFI_SCAN_SLOTS + 1)]
    found = [str(v) for v in numbered if v]
    return found or [str(v) for v in rcp_read.block_values(blob, "WIFISCANRESULT",
                                                           "wifiScanResultSSID") if v]


def _cycle_wifi_radio():
    with desk_access.hid_lock:
        rcp_transfer.write_at(rcp_transfer.NETWORK, "wifi", "bool", False)
    time.sleep(WIFI_RADIO_OFF_S)
    with desk_access.hid_lock:
        rcp_transfer.write_at(rcp_transfer.NETWORK, "wifi", "bool", True)


def _wait_for_scan() -> list:
    """The results of the scan a radio cycle starts. Old results can outlast the radio going off,
    so a list read after the slots have cleared is taken once it has held for WIFI_SCAN_SETTLE_S,
    because networks arrive a few seconds apart. On 1.7.7 a full list can stay through the cycle
    and never clear, which cannot be told from a rescan finding the same networks: that list is
    the unit's own, made while unjoined, and is returned at the deadline."""
    deadline = time.monotonic() + WIFI_SCAN_TIMEOUT_S
    cleared, last, since, found = False, None, 0.0, []
    while time.monotonic() < deadline:
        time.sleep(WIFI_SCAN_POLL_S)
        with desk_access.hid_lock:
            found = wifi_scan_slots(rcp_read.read_model_stable(fresh=True))
        if not found:
            cleared = True
        if found != last:
            last, since = found, time.monotonic()
        elif cleared and found and time.monotonic() - since >= WIFI_SCAN_SETTLE_S:
            return found
    return found


def action_scan_wifi() -> dict:
    """Scan for wifi networks and return what the unit found: the radio off and on, then the
    results read back (see the comment above)."""
    gate = _write_gate()
    if gate is not None:
        return gate
    try:
        with desk_access.hid_lock:
            state = _wifi_state(rcp_read.read_model_stable(fresh=True))
        # The scan uses the wifi radio, so with it off there is nothing to scan with; a wired
        # connection does not change that.
        if not state["on"]:
            return {"ok": False, "message": "The wifi is off. Turn it on to scan for networks."}
        if state["ip"]:
            return {"ok": False,
                    "message": (f"The desk only looks for networks while it is not connected to "
                                f"one, and it is connected to {state['ssid']}. Disconnect from it "
                                f"first, or type the name of the network you want.")}
        _cycle_wifi_radio()
        raw = _wait_for_scan()
    except Exception as exc:
        return {"ok": False, "message": f"Could not scan for networks: {exc}"}

    found, seen = [], set()
    for entry in map(parse_scan_slot, raw):
        # The unit can report the same SSID twice (two bands, or two access points on one network),
        # and a picker listing a name twice is unusable by ear.
        if entry.get("ssid") and entry["ssid"] not in seen:
            seen.add(entry["ssid"])
            found.append(entry)
    if not found:
        return {"ok": True, "networks": [],
                "message": "The scan finished but found no networks in range."}
    current = state["ssid"]
    for entry in found:
        entry["current"] = bool(current and entry["ssid"] == current)
    return {"ok": True, "networks": found, "current": current,
            "message": (f"Found {wording.count(len(found), 'network')}. "
                        f"The desk lists ten at most, so a network in range can still be missing "
                        f"from this list.")}


def action_disconnect_wifi() -> dict:
    """Leave the wifi network and forget it: `wifiSSID` cleared, then the radio off and on if it
    was on, so the unit stays on wifi with nothing to join (see the comment above)."""
    gate = _write_gate()
    if gate is not None:
        return gate
    try:
        with desk_access.hid_lock:
            state = _wifi_state(rcp_read.read_model_stable(fresh=True))
        ssid = state["ssid"]
        if not ssid and not state["ip"]:
            return {"ok": True, "wifi": state, "message": "The desk is not set up for any network."}
        with desk_access.hid_lock:
            rcp_transfer.write_at(rcp_transfer.NETWORK, "wifiSSID", "str", "")
        if state["on"]:
            _cycle_wifi_radio()
        time.sleep(WIFI_RADIO_OFF_S)
        with desk_access.hid_lock:
            state = _wifi_state(rcp_read.read_model_stable(fresh=True))
    except Exception as exc:
        return {"ok": False, "message": f"Could not disconnect from the network: {exc}"}
    if state["ssid"] or state["ip"]:
        return {"ok": False, "wifi": state,
                "message": f"The desk did not let go of {ssid or 'its network'}."}
    return {"ok": True, "wifi": state,
            "message": f"Disconnected from {ssid or 'the network'}."}


def _write_gate():
    """The NETWORK block. Proven on both the Pro II and the Duo, so this gate refuses no desk
    we know; it stays for the next one. See desk_access.cap_gate."""
    return desk_access.cap_gate("network_write", lambda name:
                                f"AccessCaster can read the network settings on the {name} but "
                                f"not change them, so it refuses rather than risk the wrong "
                                f"setting.", confirmed=True)
