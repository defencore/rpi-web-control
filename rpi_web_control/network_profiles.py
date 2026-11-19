"""Validate network actions and translate them to a bounded NetworkManager settings schema."""

import ipaddress
import re
import socket
import uuid
from pathlib import Path

NM = "org.freedesktop.NetworkManager"
OWNER = "org.rpi-web-control.managed"
BRIDGE = "br-rpi"
KERNEL_INTERFACES = Path("/sys/class/net")
WIFI = "802-11-wireless"
ETHERNET = "802-3-ethernet"
PROPERTIES = "org.freedesktop.DBus.Properties"
DEVICE_TYPES = {1: "ethernet", 2: "wifi", 13: "bridge"}
STATES = {10: "unmanaged", 20: "unavailable", 30: "disconnected", 100: "connected",
          110: "disconnecting", 120: "failed"}


def validate_interface(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}", name):
        raise ValueError("Select a valid network interface.")


def _credentials(settings):
    ssid, password = settings["ssid"], settings["password"]
    if not isinstance(ssid, str) or not ssid.isprintable() or not 1 <= len(ssid.encode("utf-8")) <= 32:
        raise ValueError("SSID must be 1 to 32 UTF-8 bytes of printable text.")
    security = settings.get("security", "wpa2-personal")
    if security == "open":
        if password != "":
            raise ValueError("Leave the password empty for an open Wi-Fi network.")
    elif security == "wpa3-personal":
        # SAE accepts a passphrase of any length. D-Bus strings must be valid UTF-8 without NUL.
        if not isinstance(password, str) or not re.fullmatch(r"[^\x00\ud800-\udfff]+", password):
            raise ValueError("Enter the WPA3-Personal passphrase.")
    elif security != "wpa2-personal":
        raise ValueError("Select open, WPA2-Personal or WPA3-Personal security.")
    elif not isinstance(password, str) or not re.fullmatch(r"[ -~]{8,63}|[a-fA-F0-9]{64}", password):
        raise ValueError("Enter an 8 to 63 character WPA passphrase or a 64-digit hexadecimal key.")


def _ip(value, field):
    try:
        if not isinstance(value, str):
            raise ValueError
        address = ipaddress.IPv4Address(value)
        if address.is_multicast or address.is_unspecified or int(address) == 0xffffffff:
            raise ValueError
        return address
    except ValueError:
        raise ValueError(f"Enter a unicast IPv4 address for {field}.") from None


def _validate_ipv4(settings):
    method = settings["ipv4_method"]
    mode = settings["mode"]
    if (method not in ("auto", "manual", "shared") or (mode == "station" and method == "shared")
            or (mode == "ap" and method == "auto")):
        raise ValueError("This IPv4 assignment is unavailable in the selected mode.")
    address, gateway, dns = (settings[key] for key in ("address", "gateway", "dns"))
    if not isinstance(address, str) or not isinstance(gateway, str) or not isinstance(dns, list):
        raise ValueError("Supply the IPv4 address, gateway and DNS server list.")
    if method == "auto" and (address or gateway):
        raise ValueError("Leave the address and gateway empty for automatic IPv4.")
    if method == "shared" and (gateway or dns):
        raise ValueError("Leave the gateway and DNS list empty for shared IPv4.")
    for value in dns:
        _ip(value, "DNS")
    if not address:
        if method == "manual" or (method == "shared" and mode != "ap"):
            raise ValueError("Enter the IPv4 address and prefix length.")
        return
    try:
        if not re.fullmatch(r"[0-9.]+/[0-9]{1,2}", address):
            raise ValueError
        cidr = ipaddress.IPv4Interface(address)
        subnet = cidr.network
        host = _ip(str(cidr.ip), "this interface")
        if (not subnet.prefixlen or host.is_loopback
                or (subnet.prefixlen < 31 and host in (subnet.network_address, subnet.broadcast_address))
                or (method == "shared" and subnet.prefixlen > 30)):
            raise ValueError
    except ValueError:
        raise ValueError("Enter a usable IPv4 host address with its network prefix.") from None
    if gateway:
        router = _ip(gateway, "the gateway")
        if (router not in subnet or router == host
                or (subnet.prefixlen < 31 and router in (subnet.network_address, subnet.broadcast_address))):
            raise ValueError("The gateway must be another host on the selected IPv4 subnet.")


def validate_apply(settings):
    if not isinstance(settings, dict):
        raise ValueError("Expected network settings.")
    mode = settings.get("mode")
    if mode not in ("ethernet", "station", "ap", "bridge"):
        raise ValueError("Select a supported network mode.")
    validate_interface(settings.get("interface"))
    wifi = mode in ("station", "ap", "bridge")
    fields = {"interface", "mode"}
    if wifi:
        fields.update(("ssid", "password"))
    if mode == "bridge":
        fields.add("wifi_interface")
        validate_interface(settings.get("wifi_interface"))
        if settings["wifi_interface"] == settings["interface"]:
            raise ValueError("Select separate Ethernet and Wi-Fi interfaces.")
    if mode in ("station", "ap") and settings.keys() == fields:
        _credentials(settings)
        return
    fields.update(("action", "ipv4_method", "address", "gateway", "dns"))
    if mode == "station":
        fields.update(settings.keys() & {"security", "hidden"})
        if type(settings.get("hidden", False)) is not bool:
            raise ValueError("Set hidden to true or false for a station network.")
    if settings.keys() != fields or settings["action"] != "apply":
        raise ValueError("Unexpected or missing fields for the selected network mode.")
    if wifi:
        _credentials(settings)
    _validate_ipv4(settings)


def build_profiles(settings, dbus):
    """Build the IP-bearing profile first, followed by its two ports in bridge mode."""
    validate_apply(settings)
    mode, iface = settings["mode"], settings["interface"]
    method = settings.get("ipv4_method", "shared" if mode == "ap" else "auto")

    def profile(name, kind):
        return {
            "connection": {"type": kind, "uuid": str(uuid.uuid4()), "autoconnect": True,
                           "autoconnect-priority": dbus.Int32(10), "interface-name": name,
                           "id": "RPi Web Control " + name},
            "user": {"data": dbus.Dictionary({OWNER: "1"}, signature="ss")},
            kind: dbus.Dictionary({}, signature="sv"),
        }

    def configure_wifi(p):
        p[WIFI].update(ssid=dbus.ByteArray(settings["ssid"].encode("utf-8")),
                       mode="infrastructure" if mode == "station" else "ap")
        if settings.get("hidden"):
            p[WIFI]["hidden"] = True
        security = settings.get("security", "wpa2-personal")
        if security != "open":
            p[WIFI + "-security"] = {"key-mgmt": "sae" if security == "wpa3-personal" else "wpa-psk",
                                     "psk": settings["password"], "proto": dbus.Array(["rsn"], signature="s")}

    if mode == "bridge":
        main = profile(BRIDGE, "bridge")
        main["connection"]["autoconnect-slaves"] = dbus.Int32(1)
        wired = profile(iface, ETHERNET)
        radio = profile(settings["wifi_interface"], WIFI)
        for port in (wired, radio):
            # These aliases also work on Bookworm's NetworkManager 1.42.
            port["connection"].update({"master": main["connection"]["uuid"], "slave-type": "bridge"})
            port["bridge-port"] = dbus.Dictionary({}, signature="sv")
        configure_wifi(radio)
        profiles = [main, wired, radio]
    else:
        main = profile(iface, ETHERNET if mode == "ethernet" else WIFI)
        if mode != "ethernet":
            configure_wifi(main)
        profiles = [main]
    ip4 = {"method": method, "may-fail": False}
    if settings.get("address"):
        cidr = ipaddress.IPv4Interface(settings["address"])
        item = dbus.Dictionary({"address": str(cidr.ip), "prefix": dbus.UInt32(cidr.network.prefixlen)},
                               signature="sv")
        ip4["address-data"] = dbus.Array([item], signature="a{sv}")
    if settings.get("gateway"):
        ip4["gateway"] = settings["gateway"]
    if settings.get("dns"):
        # D-Bus ipv4.dns uses network byte order in a native uint32.
        servers = [dbus.UInt32(socket.htonl(int(ipaddress.IPv4Address(value)))) for value in settings["dns"]]
        ip4["dns"] = dbus.Array(servers, signature="u")
        if method == "auto":
            ip4["ignore-auto-dns"] = True
    main["ipv4"] = ip4
    main["ipv6"] = {"method": "disabled" if mode == "ap" or method == "shared" else "auto"}
    return profiles


def _dns_override(ipv4):
    if ipv4.get("method") != "auto" or not ipv4.get("ignore-auto-dns"):
        return []
    if ipv4.get("dns-data"):
        servers = []
        for value in ipv4["dns-data"]:
            try:
                servers.append(str(ipaddress.IPv4Address(str(value))))
            except ValueError:
                # The IPv4 form accepts plain addresses. Advanced DNS URIs belong to NM.
                continue
        return servers
    return [str(ipaddress.IPv4Address(socket.ntohl(int(value)))) for value in ipv4.get("dns", [])]


def _configured_primary(ipv4):
    if ipv4.get("method") not in ("manual", "shared"):
        return ""
    modern = ipv4.get("address-data")
    if modern:
        return f"{modern[0]['address']}/{int(modern[0]['prefix'])}"
    legacy = ipv4.get("addresses")
    if legacy:
        value, prefix = legacy[0][:2]
        return f"{ipaddress.IPv4Address(socket.ntohl(int(value)))}/{int(prefix)}"
    return ""


def _station_security(settings):
    security = settings.get(WIFI + "-security")
    if security is None:
        return "open"
    key_mgmt = security.get("key-mgmt")
    if key_mgmt == "sae":
        return "wpa3-personal"
    if key_mgmt == "wpa-psk" and (not security.get("proto") or "rsn" in security["proto"]):
        return "wpa2-personal"
    return "unsupported"


def inventory(manager):
    """Read status without requesting secrets. Keep kernel port membership even on unmanaged controllers."""
    devices = {str(path): manager.iface(path, PROPERTIES).GetAll(NM + ".Device")
               for path in manager.manager.GetDevices()}
    members = {str(port): str(dev["Interface"]) for dev in devices.values() for port in dev.get("Ports", [])}
    rows = []
    for path, dev in devices.items():
        kind = DEVICE_TYPES.get(int(dev["DeviceType"]))
        if not kind:
            continue
        name, state = str(dev["Interface"]), int(dev["State"])
        wireless = manager.iface(path, PROPERTIES).GetAll(NM + ".Device.Wireless") if kind == "wifi" else {}
        mode = kind if state == 100 else "disconnected"
        ssid = ""
        if state == 100 and kind == "wifi":
            mode = {2: "station", 3: "ap"}.get(int(wireless["Mode"]), "unknown")
            ap = str(wireless["ActiveAccessPoint"])
            if ap != "/":
                ssid = bytes(manager.prop(ap, NM + ".AccessPoint", "Ssid")).decode("utf-8", "replace")
        ip_path = str(dev.get("Ip4Config", "/"))
        ip = manager.iface(ip_path, PROPERTIES).GetAll(NM + ".IP4Config") if ip_path != "/" else {}
        active_uuid = method = configured_primary = ""
        station = {}
        dns_override = []
        controller = members.get(path, "")
        active_path = str(dev.get("ActiveConnection", "/"))
        if active_path != "/":
            active = manager.iface(active_path, PROPERTIES).GetAll(NM + ".Connection.Active")
            active_uuid = str(active["Uuid"])
            master = str(active.get("Controller", active.get("Master", "/")))
            if master != "/" and not controller:
                controller = str(devices[master]["Interface"]) if master in devices else "unknown"
            try:
                settings = manager.iface(active["Connection"], NM + ".Settings.Connection").GetSettings()
            except manager.dbus.DBusException:
                # A user-private profile can hide its settings from the web service.
                # Effective addresses and controller membership are still public.
                pass
            else:
                ipv4 = settings.get("ipv4", {})
                method = str(ipv4.get("method", ""))
                configured_primary = _configured_primary(ipv4)
                dns_override = _dns_override(ipv4)
                if kind == "wifi" and state == 100 and not ssid:
                    ssid = bytes(settings.get(WIFI, {}).get("ssid", b"")).decode("utf-8", "replace")
                if mode == "station":
                    station = {"security": _station_security(settings), "hidden": bool(settings.get(WIFI, {}).get("hidden"))}
        rows.append({
            "name": name, "path": path, "type": kind, "physical": kind != "bridge" and not int(dev["Capabilities"]) & 4,
            "state": STATES.get(state, "connecting" if 40 <= state <= 90 else "unknown"),
            "managed": bool(dev["Managed"]), "ap": bool(int(wireless.get("WirelessCapabilities", 0)) & 0x40),
            "mode": mode, "ssid": ssid, "controller": controller, "active_uuid": active_uuid, "ipv4_method": method,
            "ports": [str(devices[str(p)]["Interface"]) if str(p) in devices else "unknown" for p in dev.get("Ports", [])],
            "addresses": [f"{a['address']}/{a['prefix']}" for a in ip.get("AddressData", [])],
            "gateway": str(ip.get("Gateway", "")), "dns": [str(n["address"]) for n in ip.get("NameserverData", [])],
            "dns_override": dns_override, "configured_primary": configured_primary, **station,
        })
    return rows


def validate_selection(settings, rows, existing_profiles):
    """Recheck hardware identity and ownership before creating the checkpoint."""
    validate_apply(settings)
    mode = settings["mode"]
    by_name = {row["name"]: row for row in rows}
    targets = {settings["interface"]: "wifi" if mode in ("ap", "station") else "ethernet"}
    if mode == "bridge":
        targets[settings["wifi_interface"]] = "wifi"
    for name, kind in targets.items():
        row = by_name.get(name)
        if not row or row["type"] != kind or not row["physical"]:
            raise ValueError(f"Select a physical {kind} interface.")
        if not row["managed"] or row["state"] in ("unmanaged", "unavailable", "unknown"):
            raise ValueError("The selected interface is disabled or unmanaged.")
        if kind == "wifi" and mode in ("ap", "bridge") and not row["ap"]:
            raise ValueError("This radio does not support access point mode.")
        if row["controller"] and row["controller"] != BRIDGE:
            raise ValueError("The selected interface belongs to another controller.")

    owned = set()
    bridges = set()
    saved = []
    for profile in existing_profiles:
        con = profile.get("connection", {})
        owned_profile = profile.get("user", {}).get("data", {}).get(OWNER) == "1"
        uid = con.get("uuid", "")
        name = con.get("interface-name") or profile.get("bridge", {}).get("interface-name", "")
        parent = con.get("controller") or con.get("master", "")
        saved.append((name, parent, owned_profile))
        if owned_profile and uid:
            owned.add(uid)
            if name == BRIDGE and con.get("type") == "bridge":
                bridges.add(uid)

    controllers = bridges | {BRIDGE}
    touches_bridge = (mode == "bridge" or any(by_name[n]["controller"] == BRIDGE for n in targets)
                      or any(n in targets and parent in controllers for n, parent, _ in saved))
    if mode == "station" and touches_bridge:
        raise ValueError("This Wi-Fi interface is a bridge port. Select an independent Wi-Fi adapter "
                         "or reconfigure the bridge before joining a network.")
    for name, parent, service_owned in saved:
        if not service_owned and ((name in targets and parent)
                                  or (touches_bridge and (name == BRIDGE or parent in controllers))):
            raise ValueError("An administrator-managed profile owns the bridge or a selected port.")
    if touches_bridge:
        bridge = by_name.get(BRIDGE)
        if bridge is None:
            if (KERNEL_INTERFACES / BRIDGE).exists():
                raise ValueError("The bridge name is already in use outside NetworkManager.")
        elif bridge["type"] != "bridge" or not bridge["managed"] or bridge["active_uuid"] not in bridges:
            raise ValueError("The existing bridge is not managed by this application.")
        else:
            ports = set(bridge["ports"]) | {r["name"] for r in rows if r["controller"] == BRIDGE}
            if any(by_name.get(port, {}).get("active_uuid") not in owned for port in ports):
                raise ValueError("The bridge has ports not managed by this application.")
