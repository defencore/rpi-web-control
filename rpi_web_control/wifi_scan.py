"""Discover nearby infrastructure networks on a selected NetworkManager Wi-Fi device."""

import time

from .network_profiles import NM, PROPERTIES, inventory


DEVICE = NM + ".Device"
WIRELESS = DEVICE + ".Wireless"
ACCESS_POINT = NM + ".AccessPoint"
SCAN_CACHE_SECONDS = 10
SCAN_TIMEOUT_SECONDS = 15
# NetworkManager retains AP objects between scans. LastSeen is in whole BOOTTIME seconds.
MAX_AP_AGE_SECONDS = 30
MAX_NETWORKS = 128
AP_SCAN_UNAVAILABLE = ("No nearby named networks could be read while this radio hosts its access point. "
                       "Try a second Wi-Fi adapter or enter the router's network name over Ethernet.")

KEY_PSK = 0x100
KEY_EAP = 0x200
KEY_SAE = 0x400
KEY_OWE = 0x800
KEY_OWE_TRANSITION = 0x1000
KEY_SUITE_B = 0x2000


def scan_adapter(name, rows, radio, *, allow_ap_scan=False):
    row = next((row for row in rows if row["name"] == name), None)
    if row is None or row["type"] != "wifi" or not row["physical"]:
        raise ValueError("Choose a physical Wi-Fi adapter to scan.")
    if not radio["hardware_enabled"]:
        raise ValueError("Wi-Fi is hardware-blocked. Unblock the radio on the device.")
    if not radio["enabled"]:
        raise ValueError("Enable Wi-Fi before scanning for networks.")
    if row["controller"]:
        raise ValueError("This Wi-Fi adapter belongs to a bridge or another controller. Choose a standalone adapter.")
    if row["mode"] == "ap" and not allow_ap_scan:
        raise ValueError("This adapter is hosting an access point. Confirm scanning while the access point is active.")
    if not row["managed"] or row["state"] not in ("connected", "disconnected", "failed"):
        raise ValueError("Wait for a managed Wi-Fi adapter to become ready and scan again.")
    return row


def request_scan(manager, interface, *, allow_ap_scan=False):
    # Only the restricted helper invokes RequestScan. It releases its operation lock
    # as soon as NetworkManager accepts the request.
    row = scan_adapter(interface, inventory(manager), manager.wifi_radio(), allow_ap_scan=allow_ap_scan)
    path = row["path"]
    owner = str(manager.bus.get_name_owner(NM))
    hosting = row["mode"] == "ap"
    own_ap = str(manager.prop(path, WIRELESS, "ActiveAccessPoint")) if hosting else "/"
    completed_ms = int(manager.prop(path, WIRELESS, "LastScan"))
    now_ms = time.clock_gettime_ns(time.CLOCK_BOOTTIME) // 1_000_000
    recent = completed_ms >= 0 and 0 <= now_ms - completed_ms < SCAN_CACHE_SECONDS * 1000
    radio = manager.iface(path, WIRELESS)
    # Recent background scans can contain only hidden or synthetic BSS objects.
    # Reuse the result only if it would offer a fresh, displayable network.
    cached = False
    if recent:
        paths = (p for p in radio.GetAllAccessPoints() if str(p) != own_ap)
        cached = any(ssid and int(ap["LastSeen"]) >= completed_ms // 1000 - MAX_AP_AGE_SECONDS
                     for ap, ssid in _observations(manager, paths))
    busy = False
    if not cached:
        options = manager.dbus.Dictionary({}, signature="sv")
        try:
            radio.RequestScan(options, timeout=5)
        except manager.dbus.DBusException as exc:
            if exc.get_dbus_name() not in (NM + ".Device.NotAllowed", NM + ".Device.NotReady",
                                          NM + ".Device.Wireless.NotAllowed"):
                raise
            # The driver may already be scanning or enforcing a short quiet period.
            busy = True
    return {"path": path, "owner": owner, "last_scan_ms": completed_ms, "cached": cached,
            "busy": busy, "hosting": hosting, "own_ap": own_ap}


def _security(ap):
    wpa, rsn = int(ap["WpaFlags"]), int(ap["RsnFlags"])
    if rsn & KEY_PSK:
        return "wpa2-personal"
    if rsn & KEY_SAE:
        return "wpa3-personal"
    if (wpa | rsn) & (KEY_EAP | KEY_SUITE_B):
        return "enterprise"
    if rsn & (KEY_OWE | KEY_OWE_TRANSITION):
        return "enhanced-open"
    if wpa & KEY_PSK:
        return "wpa1-personal"
    if wpa or rsn:
        return "unsupported"
    return "wep" if int(ap["Flags"]) & 1 else "open"


def _observations(manager, paths):
    for path in paths:
        try:
            ap = manager.iface(path, PROPERTIES).GetAll(ACCESS_POINT)
        except manager.dbus.DBusException as exc:
            if exc.get_dbus_name() in ("org.freedesktop.DBus.Error.UnknownObject",
                                       "org.freedesktop.DBus.Error.UnknownMethod",
                                       "org.freedesktop.DBus.Error.UnknownInterface"):
                continue  # The AP disappeared while reading the device's scan.
            raise
        if int(ap["LastSeen"]) < 0 or int(ap["Mode"]) != 2:
            continue
        name = bytes(ap["Ssid"])
        try:
            ssid = name.decode("utf-8") if 1 <= len(name) <= 32 else ""
        except UnicodeDecodeError:
            ssid = ""  # The station profile takes a UTF-8 SSID.
        yield ap, ssid if ssid.isprintable() else ""


def scan_snapshot(manager, paths, completed_ms):
    recent, previous = {}, {}
    seen = 0
    for ap, ssid in _observations(manager, paths):
        seen += 1
        if not ssid:
            continue
        security = _security(ap)
        key = (ssid, security)
        strength = max(0, min(100, int(ap["Strength"])))
        strongest = previous if int(ap["LastSeen"]) < completed_ms // 1000 - MAX_AP_AGE_SECONDS else recent
        if key not in strongest or strength > strongest[key]["strength"]:
            strongest[key] = {"ssid": ssid, "strength": strength, "security": security,
                              "supported": security in ("open", "wpa2-personal", "wpa3-personal")}
    # If the latest scan contains no usable names, retain the daemon's previously
    # seen networks and tell the operator that these observations are older.
    networks = sorted((recent or previous).values(), key=lambda row: (-row["strength"], row["ssid"], row["security"]))
    return {"networks": networks[:MAX_NETWORKS], "previous_results": bool(previous and not recent),
            "access_points_seen": seen, "truncated": len(networks) > MAX_NETWORKS}


def scan_results(manager, interface, ticket, retry=None):
    # LastScan advances on completion, even when the result is an empty AP list.
    # Read properties in the web account and keep nearby SSIDs out of the root socket.
    path = ticket["path"]
    owner = ticket["owner"]
    started = time.monotonic()
    retry_at = started + SCAN_CACHE_SECONDS if ticket["busy"] and not ticket["hosting"] and retry else None
    deadline = (retry_at or started) + SCAN_TIMEOUT_SECONDS
    timed_out = False
    while True:
        if (str(manager.bus.get_name_owner(NM)) != owner
                or str(manager.prop(path, DEVICE, "Interface")) != interface):
            raise RuntimeError("The Wi-Fi adapter changed. Scan again.")
        completed_ms = int(manager.prop(path, WIRELESS, "LastScan"))
        if completed_ms >= 0 and (ticket["cached"] or completed_ms > ticket["last_scan_ms"]):
            break
        now = time.monotonic()
        if now >= deadline:
            timed_out = True
            break
        if retry_at is not None and now >= retry_at:
            # Recheck radio mode and pending operations in the locked helper.
            # Retrying once also handles a refusal with no scan already in flight.
            ticket = retry()
            if ticket["owner"] != owner or ticket["path"] != path:
                raise RuntimeError("The Wi-Fi adapter changed. Scan again.")
            retry_at = None
            continue
        time.sleep(0.25)
    paths = (p for p in manager.iface(path, WIRELESS).GetAllAccessPoints() if str(p) != ticket["own_ap"])
    snapshot = scan_snapshot(manager, paths, completed_ms)
    if ticket["hosting"] and not snapshot["networks"]:
        raise RuntimeError(AP_SCAN_UNAVAILABLE)
    if timed_out:
        if not snapshot["networks"]:
            if ticket["busy"]:
                raise RuntimeError("The Wi-Fi adapter is busy and has no saved scan results. Wait a few seconds and retry.")
            raise RuntimeError("Wi-Fi scan timed out. Wait for the adapter and scan again.")
        snapshot["previous_results"] = True
    return {"interface": interface, "cached": ticket["cached"] or snapshot["previous_results"], **snapshot}
