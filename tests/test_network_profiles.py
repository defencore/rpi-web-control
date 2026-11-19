import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rpi_web_control.network_profiles import (BRIDGE, ETHERNET, NM, OWNER, PROPERTIES, WIFI, build_profiles,
                                              inventory, validate_apply, validate_selection)


WLAN = {"interface": "wlan0", "mode": "station", "ssid": "Field station", "password": "test psk only"}
WIRED = {"action": "apply", "interface": "eth0", "mode": "ethernet", "ipv4_method": "auto",
         "address": "", "gateway": "", "dns": []}
BRIDGED = {**WIRED, "mode": "bridge", "wifi_interface": "wlan0", "ssid": WLAN["ssid"], "password": WLAN["password"]}


class DBus:
    Int32 = UInt32 = int
    ByteArray = bytes
    DBusException = RuntimeError

    @staticmethod
    def Array(items, signature):
        return list(items)

    @staticmethod
    def Dictionary(items, signature):
        return dict(items)


def row(name, kind, **overrides):
    return {"name": name, "type": kind, "physical": kind in ("ethernet", "wifi"), "managed": True,
            "state": "connected", "controller": "", "active_uuid": "", "ports": [], "ap": kind == "wifi",
            **overrides}


class NetworkProfileTests(unittest.TestCase):
    def test_ipv4_values_and_legacy_defaults(self):
        station, = build_profiles(WLAN, DBus)
        self.assertEqual(station["ipv4"], {"method": "auto", "may-fail": False})
        access_point, = build_profiles({**WLAN, "mode": "ap"}, DBus)
        self.assertEqual(access_point["ipv4"], {"method": "shared", "may-fail": False})
        self.assertEqual(access_point[WIFI]["mode"], "ap")
        self.assertEqual(access_point["ipv6"]["method"], "disabled")
        for mode in ("auto", "manual"):
            with self.subTest(mode=mode):
                req = {**WIRED, "ipv4_method": mode, "dns": ["9.9.9.9", "1.2.3.4"]}
                if mode == "manual":
                    req.update(address="192.0.2.10/24", gateway="192.0.2.1")
                profile, = build_profiles(req, DBus)
                ip = profile["ipv4"]
                self.assertEqual(ip.get("ignore-auto-dns", False), mode == "auto")
                self.assertEqual([socket.ntohl(n) for n in ip["dns"]], [0x09090909, 0x01020304])
                if mode == "manual":
                    self.assertEqual(ip["address-data"], [{"address": "192.0.2.10", "prefix": 24}])
                    self.assertEqual(ip["gateway"], req["gateway"])

    def test_bridge_profile_links_ports_and_assigns_ipv4_only_to_controller(self):
        controller, eth, wifi = build_profiles({**BRIDGED, "ipv4_method": "shared", "address": "192.168.9.1/24"}, DBus)
        self.assertEqual([p["connection"]["type"] for p in (controller, eth, wifi)], ["bridge", ETHERNET, WIFI])
        self.assertEqual(controller["connection"]["interface-name"], BRIDGE)
        self.assertEqual(controller["connection"]["autoconnect-slaves"], 1)
        self.assertEqual(controller["ipv4"]["method"], "shared")
        for port in (eth, wifi):
            self.assertEqual(port["connection"]["master"], controller["connection"]["uuid"])
            self.assertEqual(port["connection"]["slave-type"], "bridge")
            self.assertNotIn("ipv4", port)
            self.assertNotIn("ipv6", port)
            self.assertIn("bridge-port", port)
        self.assertEqual(wifi[WIFI]["mode"], "ap")
        self.assertEqual(wifi[WIFI + "-security"]["psk"], WLAN["password"])
        self.assertNotIn(WIFI + "-security", eth)
        for p in (controller, eth, wifi):
            self.assertEqual(p["user"]["data"][OWNER], "1")
            self.assertTrue(p["connection"]["autoconnect"])

    def test_rejects_invalid_fields_secrets_and_ipv4_combinations(self):
        manual = {**WIRED, "ipv4_method": "manual", "address": "10.0.0.8/24"}
        ap = {**WIRED, **WLAN, "mode": "ap", "ipv4_method": "shared"}
        validate_apply(ap)
        validate_apply({**manual, "address": "192.0.2.0/31", "gateway": "192.0.2.1"})
        for bad in ({**WIRED, "password": "spurious"}, {**WIRED, "address": "192.0.2.10/24"},
                    {**BRIDGED, "wifi_interface": "eth0"}, {**BRIDGED, "mode": "station"},
                    {**ap, "ipv4_method": "auto"}, {**ap, "password": 12345678},
                    {**WLAN, "ssid": "é" * 17}, {**ap, "gateway": "1.2.3.4"}, {**ap, "dns": ["1.1.1.1"]},
                    {**WIRED, "ipv4_method": "shared"}, {**WIRED, "dns": [2130706433]},
                    {**manual, "address": "10.0.0.0/24"}, {**manual, "address": "10.0.0.255/24"},
                    {**manual, "address": "::1/64"}, {**manual, "address": "224.0.0.10/24"},
                    {**manual, "gateway": "192.168.1.1"}, {**manual, "address": "10.0.0.7"},
                    {**manual, "ipv4_method": "shared", "address": "192.0.2.0/31"}):
            with self.subTest(mode=bad.get("mode")), self.assertRaises(ValueError):
                validate_apply(bad)

    def test_active_and_saved_admin_bridge_ownership(self):
        eth, wifi = row("eth0", "ethernet"), row("wlan0", "wifi")
        bridge, eth_port, wifi_port = build_profiles(BRIDGED, DBus)
        active_bridge = row(BRIDGE, "bridge", active_uuid=bridge["connection"]["uuid"], ports=["eth0", "wlan0"])
        active_eth = {**eth, "controller": BRIDGE, "active_uuid": eth_port["connection"]["uuid"]}
        active_wifi = {**wifi, "controller": BRIDGE, "active_uuid": wifi_port["connection"]["uuid"]}
        owned = [bridge, eth_port, wifi_port]
        active = [active_bridge, active_eth, active_wifi]
        with tempfile.TemporaryDirectory() as sysfs, patch("rpi_web_control.network_profiles.KERNEL_INTERFACES", Path(sysfs)):
            validate_selection(BRIDGED, [eth, wifi], [])
            validate_selection(WIRED, active, owned)
            validate_selection(BRIDGED, active, owned)
            for rows, profiles in (
                ([eth, {**wifi, "physical": False}], []), ([eth, {**wifi, "ap": False}], []),
                ([{**eth, "managed": False}, wifi], []), ([{**eth, "controller": "br-admin"}, wifi], []),
                ([eth, wifi], [{"connection": {"type": "bridge", "interface-name": BRIDGE}}]),
                ([eth, wifi], [{"connection": {"type": ETHERNET, "controller": BRIDGE}}]),
                ([eth, wifi], [{"connection": {"type": ETHERNET, "interface-name": "eth0", "master": "br-admin"}}]),
                ([{**active_bridge, "active_uuid": "admin"}, active_eth, active_wifi], owned),
                ([active_bridge, {**active_eth, "active_uuid": "admin"}, active_wifi], owned),
                ([{**active_bridge, "ports": ["vlan7"]}, active_eth, active_wifi], owned),
                (active, owned + [{"connection": {"controller": bridge["connection"]["uuid"]}}]),
            ):
                with self.subTest(rows=rows), self.assertRaises(ValueError):
                    validate_selection(BRIDGED, rows, profiles)
            (Path(sysfs) / BRIDGE).mkdir()
            with self.assertRaisesRegex(ValueError, "outside NetworkManager"):
                validate_selection(BRIDGED, [eth, wifi], [])

    def test_inventory_reports_device_properties_and_effective_ip_separately(self):
        mgr = Mock()
        mgr.dbus = DBus
        device = {"Capabilities": 1, "State": 100, "Managed": True, "Ip4Config": "/", "Ports": []}
        types = NM + ".Device"
        data = {("/w", types): {**device, "Interface": "wlan0", "DeviceType": 2, "ActiveConnection": "/active"},
                ("/w", types + ".Wireless"): {"WirelessCapabilities": 0x40, "Mode": 3, "ActiveAccessPoint": "/ap"},
                ("/active", NM + ".Connection.Active"): {"Connection": "/profile", "Uuid": "wifi-uuid", "Master": "/b"},
                ("/b", types): {**device, "Interface": BRIDGE, "DeviceType": 13, "Ports": ["/w"], "Ip4Config": "/ip"},
                ("/ip", NM + ".IP4Config"): {"AddressData": [{"address": "192.0.2.1", "prefix": 24}],
                                             "Gateway": "192.0.2.254", "NameserverData": [{"address": "8.8.8.8"}]},
                ("/virtual", types): {**device, "Interface": "virt0", "DeviceType": 1, "Capabilities": 5}}
        mgr.manager.GetDevices.return_value = ["/w", "/b", "/virtual"]
        profile = Mock()
        profile.GetSettings.return_value = {WIFI + "-security": {"psk": WLAN["password"]}}
        def iface(path, interface):
            obj = Mock()
            if interface == PROPERTIES:
                obj.GetAll.side_effect = lambda key: data[path, key]
            else:
                self.assertEqual((path, interface), ("/profile", NM + ".Settings.Connection"))
                return profile
            return obj
        mgr.iface.side_effect = iface
        mgr.prop.return_value = b"Test AP"
        wifi, bridge, virtual = inventory(mgr)
        self.assertEqual((wifi["controller"], wifi["mode"], wifi["ssid"], wifi["addresses"]), (BRIDGE, "ap", "Test AP", []))
        self.assertEqual((bridge["ports"], bridge["addresses"], bridge["gateway"], bridge["dns"]),
                         (["wlan0"], ["192.0.2.1/24"], "192.0.2.254", ["8.8.8.8"]))
        self.assertFalse(virtual["physical"])
        self.assertNotIn(WLAN["password"], str([wifi, bridge, virtual]))
        for ip, override in (
            ({"method": "auto", "ignore-auto-dns": True, "dns": [socket.htonl(0x01020304)]}, ["1.2.3.4"]),
            ({"method": "auto", "ignore-auto-dns": True, "dns-data": ["9.9.9.9"]}, ["9.9.9.9"]),
            ({"method": "auto", "dns": [socket.htonl(0x01020304)]}, []),
            ({"method": "manual", "dns-data": ["9.9.9.9"]}, []),
        ):
            with self.subTest(ip=ip):
                profile.GetSettings.return_value = {"ipv4": ip}
                self.assertEqual(inventory(mgr)[0]["dns_override"], override)
        for ip, expected in (({"method": "shared"}, ""),
                             ({"method": "manual", "address-data": [{"address": "10.10.10.1", "prefix": 24}]}, "10.10.10.1/24"),
                             ({"method": "manual", "addresses": [[socket.htonl(0x0a000001), 24, 0]]}, "10.0.0.1/24")):
            with self.subTest(ip=ip):
                profile.GetSettings.return_value = {"ipv4": ip}
                self.assertEqual(inventory(mgr)[0]["configured_primary"], expected)
        profile.GetSettings.side_effect = DBus.DBusException("Private connection")
        wifi, bridge, _ = inventory(mgr)
        self.assertEqual((wifi["active_uuid"], wifi["ipv4_method"], wifi["dns_override"]), ("wifi-uuid", "", []))
        self.assertEqual((bridge["addresses"], bridge["dns"]), (["192.0.2.1/24"], ["8.8.8.8"]))
