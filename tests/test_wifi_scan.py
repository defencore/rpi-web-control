"""Check NetworkManager's asynchronous scan protocol without using the host radio."""

import io
import json
import unittest
from unittest.mock import MagicMock, patch

from rpi_web_control import wifi_scan as scan
from rpi_web_control.network import Network, _helper, helper_main, normalize_request


REQUEST = {"action": "scan_wifi", "interface": "wlan0"}
DEVICE_PATH = "/org/freedesktop/NetworkManager/Devices/7"
ROW = {"name": "wlan0", "physical": True, "managed": True, "state": "connected", "mode": "station",
       "type": "wifi", "controller": ""}
RADIO = {"enabled": True, "hardware_enabled": True}
TICKET = {"path": DEVICE_PATH, "owner": ":1.30", "cached": False, "last_scan_ms": -1,
          "busy": False, "hosting": False, "own_ap": "/"}


class DBusError(Exception):
    def get_dbus_name(self):
        return str(self)


def ap(ssid=b"Cafe", strength=50, *, flags=1, rsn=0x100, wpa=0, seen=120, mode=2):
    return {"Ssid": ssid, "Strength": strength, "Flags": flags, "RsnFlags": rsn, "WpaFlags": wpa,
            "LastSeen": seen, "Mode": mode}


class WifiScanTests(unittest.TestCase):
    def manager(self, properties):
        manager = MagicMock()
        manager.bus.get_name_owner.return_value = ":1.30"
        manager.wifi_radio.return_value = RADIO
        manager.dbus.DBusException = DBusError
        manager.dbus.Dictionary.side_effect = lambda values, signature: (values, signature)

        def iface(path, interface):
            if interface == scan.WIRELESS:
                return manager.radio
            reader = MagicMock()
            if isinstance(properties[path], Exception):
                reader.GetAll.side_effect = properties[path]
            else:
                reader.GetAll.return_value = properties[path]
            return reader

        manager.iface.side_effect = iface
        manager.radio.GetAllAccessPoints.return_value = list(properties)
        return manager

    def test_scan_reports_strongest_distinct_networks_hides_stale_or_unjoinable_names(self):
        properties = {"ap1": ap(strength=88), "ap2": ap(strength=87, rsn=0x500),
                      "ap3": ap(strength=35, flags=0, rsn=0), "ap4": ap(b"", 99),
                      "ap5": ap(b"\xff"), "ap6": ap(b"old", seen=80), "ap7": ap(b"never", seen=-1),
                      "ap8": ap(b"peer", mode=1), "ap9": ap(b"control\x00name"),
                      "ap10": DBusError("org.freedesktop.DBus.Error.UnknownObject"),
                      "ap11": ap(b"Campus", strength=91, rsn=0x200)}
        snapshot = scan.scan_snapshot(self.manager(properties), properties.keys(), 120_999)
        self.assertEqual(snapshot["access_points_seen"], 8)
        self.assertFalse(snapshot["previous_results"])
        rows = snapshot["networks"]
        self.assertEqual(rows, [
            {"ssid": "Campus", "security": "enterprise", "strength": 91, "supported": False},
            {"ssid": "Cafe", "security": "wpa2-personal", "strength": 88, "supported": True},
            {"ssid": "Cafe", "security": "open", "strength": 35, "supported": True}])

    def test_encrypted_and_unsupported_modes_are_never_reported_as_open(self):
        for flags, wpa, rsn, expected in (
            (1, 0x100, 0x100, "wpa2-personal"), (1, 0, 0x400, "wpa3-personal"),
            (1, 0, 0x500, "wpa2-personal"), (1, 0, 0x2000, "enterprise"),
            (0, 0, 0x1000, "enhanced-open"), (1, 0, 0x800, "enhanced-open"),
            (1, 0x100, 0, "wpa1-personal"), (1, 0, 0, "wep"),
            (0, 0, 0x8000, "unsupported"), (0, 0, 0, "open")):
            with self.subTest(expected=expected):
                self.assertEqual(scan._security(ap(flags=flags, wpa=wpa, rsn=rsn)), expected)

    def test_supported_managed_station_only(self):
        self.assertEqual(scan.scan_adapter("wlan0", [ROW], RADIO), ROW)
        failed = {**ROW, "state": "failed"}
        self.assertEqual(scan.scan_adapter("wlan0", [failed], RADIO), failed)
        for changes, switches in (({"type": "ethernet"}, {}), ({"physical": False}, {}),
                                  ({"managed": False}, {}), ({"controller": "br-rpi"}, {}),
                                  ({"mode": "ap"}, {}), ({"state": "connecting"}, {}),
                                  ({}, {"enabled": False}), ({}, {"hardware_enabled": False})):
            with self.subTest(changes=changes, switches=switches), self.assertRaises(ValueError):
                scan.scan_adapter("wlan0", [{**ROW, **changes}], {**RADIO, **switches})
        with self.assertRaises(ValueError):
            normalize_request({**REQUEST, "interface": "../wlan0"})

    def test_async_scan_waits_for_completion_then_reads_results_as_web_account(self):
        manager = self.manager({"/ap": ap(seen=123)})
        manager.prop.return_value = -1
        with (patch.object(scan, "inventory", return_value=[{**ROW, "path": DEVICE_PATH}]),
              patch.object(scan.time, "CLOCK_BOOTTIME", 7, create=True),
              patch.object(scan.time, "clock_gettime_ns", return_value=120_000_000_000)):
            ticket = scan.request_scan(manager, "wlan0")
            manager.radio.RequestScan.assert_called_once_with(({}, "sv"), timeout=5)
            self.assertFalse(ticket["cached"])

        manager.prop.side_effect = ["wlan0", -1, "wlan0", 123000]
        with (patch("rpi_web_control.network._helper", return_value=ticket) as helper,
              patch("rpi_web_control.network._Manager") as factory, patch.object(scan.time, "sleep") as sleep):
            factory.return_value.__enter__.return_value = manager
            result = Network().apply(REQUEST)
        helper.assert_called_once_with(REQUEST)
        self.assertEqual(result["networks"][0]["ssid"], "Cafe")
        self.assertFalse(result["truncated"])
        self.assertFalse(result["cached"])
        sleep.assert_called_once()

    def test_recent_scan_deferred_request_and_timeout_or_device_restart(self):
        manager = self.manager({"ap1": ap()})
        with (patch.object(scan, "inventory", return_value=[{**ROW, "path": DEVICE_PATH}]),
              patch.object(scan.time, "CLOCK_BOOTTIME", 7, create=True),
              patch.object(scan.time, "clock_gettime_ns", return_value=121_000_000_000)):
            manager.prop.return_value = 120000
            ticket = scan.request_scan(manager, "wlan0")
        self.assertTrue(ticket["cached"])
        manager.radio.RequestScan.assert_not_called()
        manager.prop.side_effect = ["wlan0", 120000]
        result = scan.scan_results(manager, "wlan0", ticket)
        self.assertTrue(result["cached"])
        self.assertFalse(result["previous_results"])
        self.assertEqual(result["access_points_seen"], 1)
        manager.radio.GetAllAccessPoints.return_value = []
        manager.prop.side_effect = ["wlan0", 120000]
        with (patch.object(scan.time, "monotonic", side_effect=[3, 25]),
              self.assertRaisesRegex(RuntimeError, "timed out")):
            scan.scan_results(manager, "wlan0", {**ticket, "cached": False})
        manager.bus.get_name_owner.return_value = ":1.31"
        with self.assertRaisesRegex(RuntimeError, "adapter changed"):
            scan.scan_results(manager, "wlan0", ticket)

    def test_recent_empty_or_unusable_background_scan_triggers_a_real_scan_for_a_named_network(self):
        unusable = {"hidden": ap(b""), "old": ap(seen=80), "synthetic": ap(seen=-1),
                    "control": ap(b"\x00"), "binary": ap(b"\xff")}
        for cached_paths in ([], list(unusable)):
            with self.subTest(paths=cached_paths):
                manager = self.manager({**unusable, "ap1": ap()})
                manager.prop.return_value = 120_000
                manager.radio.GetAllAccessPoints.side_effect = [cached_paths, ["ap1"]]
                with (patch.object(scan, "inventory", return_value=[{**ROW, "path": DEVICE_PATH}]),
                      patch.object(scan.time, "CLOCK_BOOTTIME", 7, create=True),
                      patch.object(scan.time, "clock_gettime_ns", return_value=121_000_000_000)):
                    ticket = scan.request_scan(manager, "wlan0")
                self.assertFalse(ticket["cached"])
                manager.radio.RequestScan.assert_called_once()
                manager.prop.side_effect = ["wlan0", 125_000]
                result = scan.scan_results(manager, "wlan0", ticket)
                self.assertEqual(result["networks"][0]["ssid"], "Cafe")
                self.assertFalse(result["cached"])

    def test_busy_driver_waits_for_scan_in_flight_without_restarting_it(self):
        manager = self.manager({"ap1": ap()})
        manager.prop.return_value = -1
        manager.radio.RequestScan.side_effect = DBusError(scan.NM + ".Device.NotAllowed")
        with (patch.object(scan, "inventory", return_value=[{**ROW, "path": DEVICE_PATH}]),
              patch.object(scan.time, "CLOCK_BOOTTIME", 7, create=True),
              patch.object(scan.time, "clock_gettime_ns", return_value=121_000_000_000)):
            ticket = scan.request_scan(manager, "wlan0")
        self.assertTrue(ticket["busy"])
        manager.prop.side_effect = ["wlan0", -1, "wlan0", 122000]
        retry = MagicMock()
        with patch.object(scan.time, "sleep"):
            result = scan.scan_results(manager, "wlan0", ticket, retry=retry)
        self.assertFalse(result["cached"])
        retry.assert_not_called()

    def test_rate_limited_station_retries_once_after_quiet_period(self):
        manager = self.manager({"ap1": ap()})
        manager.prop.side_effect = ["wlan0", -1, "wlan0", -1, "wlan0", 120000]
        retry = MagicMock(return_value=TICKET)
        with (patch.object(scan.time, "monotonic", side_effect=[2, 9, 12]),
              patch.object(scan.time, "sleep")):
            result = scan.scan_results(manager, "wlan0", {**TICKET, "busy": True}, retry=retry)
        retry.assert_called_once_with()
        self.assertFalse(result["previous_results"])
        self.assertEqual(result["networks"][0]["ssid"], "Cafe")

    def test_rejected_retry_and_empty_driver_scan_are_reported_separately(self):
        manager = self.manager({})
        manager.prop.side_effect = ["wlan0", -1, "wlan0", -1]
        retry = MagicMock(return_value={**TICKET, "busy": True})
        with (patch.object(scan.time, "monotonic", side_effect=[1, 11, 27]),
              self.assertRaisesRegex(RuntimeError, "adapter is busy")):
            scan.scan_results(manager, "wlan0", {**TICKET, "busy": True}, retry=retry)
        retry.assert_called_once()
        manager.prop.side_effect = ["wlan0", 120000]
        self.assertEqual(scan.scan_results(manager, "wlan0", TICKET),
                         {"interface": "wlan0", "cached": False, "networks": [], "truncated": False,
                          "access_points_seen": 0, "previous_results": False})

    def test_retry_waits_for_its_scan_when_an_empty_background_scan_finished_before_request(self):
        manager = self.manager({"ap1": ap(seen=123)})
        completions = iter([-1, 120000, 123000])
        visible = []

        def prop(path, iface, name):
            if name == "Interface":
                return "wlan0"
            completed = next(completions)
            if completed == 123000:
                visible.append("ap1")
            return completed

        manager.prop.side_effect = prop
        manager.radio.GetAllAccessPoints.return_value = visible
        retry = MagicMock(return_value={**TICKET, "last_scan_ms": 120000})
        with (patch.object(scan.time, "monotonic", side_effect=[1, 11, 12]),
              patch.object(scan.time, "sleep")):
            result = scan.scan_results(manager, "wlan0", {**TICKET, "busy": True}, retry=retry)
        self.assertEqual([row["ssid"] for row in result["networks"]], ["Cafe"])
        retry.assert_called_once_with()

    def test_cached_named_entries_are_identified_when_scan_has_no_fresh_names_or_times_out(self):
        properties = {"ap1": ap(seen=80), "ap2": ap(b"hidden", seen=-1), "ap3": ap(b"")}
        manager = self.manager(properties)
        for updated in (True, False):
            with self.subTest(updated=updated):
                manager.prop.side_effect = ["wlan0", 120_000]
                with patch.object(scan.time, "monotonic", side_effect=[1, 17]):
                    result = scan.scan_results(manager, "wlan0", {**TICKET, "last_scan_ms": 119_000 if updated else 120_000})
                self.assertTrue(result["cached"])
                self.assertTrue(result["previous_results"])
                self.assertEqual(result["access_points_seen"], 2)
                self.assertEqual([r["ssid"] for r in result["networks"]], ["Cafe"])

    def test_opt_in_hosting_scan_excludes_own_ssid_and_protects_bridged_port(self):
        manager = self.manager({"own": ap(b"PiAP"), "other": ap(b"Router", seen=122)})
        manager.radio.GetAllAccessPoints.side_effect = [["own"], ["own", "other"]]
        manager.prop.side_effect = ["own", 120_000, "wlan0", 122_000]
        row = {**ROW, "mode": "ap", "path": DEVICE_PATH}
        with (patch.object(scan, "inventory", return_value=[row]),
              patch.object(scan.time, "CLOCK_BOOTTIME", 7, create=True),
              patch.object(scan.time, "clock_gettime_ns", return_value=121_000_000_000)):
            ticket = scan.request_scan(manager, "wlan0", allow_ap_scan=True)
        self.assertFalse(ticket["cached"])
        manager.radio.RequestScan.assert_called_once()
        result = scan.scan_results(manager, "wlan0", ticket)
        self.assertEqual([r["ssid"] for r in result["networks"]], ["Router"])
        self.assertEqual(result["access_points_seen"], 1)
        with self.assertRaisesRegex(ValueError, "controller"):
            scan.scan_adapter("wlan0", [{**row, "controller": "br-rpi"}], RADIO, allow_ap_scan=True)

    def test_refusal_while_hosting_does_not_retry_or_offer_local_hotspot(self):
        manager = self.manager({"own": ap(b"PiAP")})
        manager.prop.side_effect = ["wlan0", 120000]
        retry = MagicMock()
        ticket = {**TICKET, "own_ap": "own", "hosting": True, "busy": True, "last_scan_ms": 120000}
        with (patch.object(scan.time, "monotonic", side_effect=[1, 17]),
              self.assertRaisesRegex(RuntimeError, "second Wi-Fi adapter")):
            scan.scan_results(manager, "wlan0", ticket, retry=retry)
        retry.assert_not_called()

    def test_helper_transfers_only_a_bounded_scan_ticket(self):
        ticket = {"path": DEVICE_PATH, "owner": ":1.30", "cached": False, "last_scan_ms": 123}
        request = io.BytesIO(json.dumps(REQUEST).encode() + b"\n")
        response = io.StringIO()
        with (patch("sys.stdin", MagicMock(buffer=request)), patch("sys.stdout", response),
              patch("rpi_web_control.network._Manager"), patch("rpi_web_control.network.NetworkTransaction") as tx):
            tx.return_value.request_scan.return_value = ticket
            helper_main()
            tx.return_value.request_scan.assert_called_once_with("wlan0", allow_ap_scan=False)
            tx.return_value.stage.assert_not_called()
        self.assertEqual(json.loads(response.getvalue()), {"ok": True, "scan": ticket})
        with patch("rpi_web_control.network.socket.socket") as connect:
            sock = connect.return_value.__enter__.return_value
            sock.makefile.return_value = io.BytesIO(response.getvalue().encode())
            self.assertEqual(_helper(REQUEST), ticket)
            sock.makefile.return_value = io.BytesIO(b'{"ok":true}' + b" " * 4100 + b"\n")
            with self.assertRaises(RuntimeError):
                _helper(REQUEST)

    def test_limit_keeps_strongest_entries(self):
        properties = {str(n): ap(ssid=f"SSID{n:03}".encode(), strength=max(0, n-50)) for n in range(140)}
        manager = self.manager(properties)
        manager.prop.side_effect = ["wlan0", 120000]
        result = scan.scan_results(manager, "wlan0", {**TICKET, "cached": True, "last_scan_ms": 120000})
        self.assertEqual(len(result["networks"]), scan.MAX_NETWORKS)
        self.assertEqual(result["networks"][0]["ssid"], "SSID139")
        self.assertTrue(result["truncated"])
