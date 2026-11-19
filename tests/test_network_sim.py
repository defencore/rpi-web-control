"""Operate the simulated network through the same HTTP handlers as the appliance."""

import http.client
from ipaddress import IPv4Interface
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from rpi_web_control.application import Application
from rpi_web_control.network_sim import SimulatedNetwork
from rpi_web_control.network_transaction import CONFIRM_TIMEOUT
from rpi_web_control.web import Server


ETHERNET = {"action": "apply", "mode": "ethernet", "interface": "eth0", "ipv4_method": "manual",
            "address": "192.0.2.90/24", "gateway": "192.0.2.1", "dns": ["192.0.2.53"]}
WIFI = {"action": "apply", "mode": "station", "interface": "wlan0", "ssid": "Workshop",
        "password": "fixture-wpa-passphrase", "ipv4_method": "auto", "address": "", "gateway": "", "dns": []}
BRIDGE = {**ETHERNET, "mode": "bridge", "wifi_interface": "wlan0", "ssid": WIFI["ssid"],
          "password": WIFI["password"], "ipv4_method": "shared", "address": "192.0.2.91/24",
          "gateway": "", "dns": []}
RESCUE = IPv4Interface("192.168.55.100/24")
ALIAS = f"{RESCUE.ip}/32"


def by_name(status):
    return {row["name"]: row for row in status["interfaces"]}


class SimulatedNetworkTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name).resolve()
        self.recordings = self.state / "recordings"
        self.recordings.mkdir()
        self.token = "fixture-network-operator-token"
        configured = patch.dict(os.environ, {"RPI_CONTROL_RECOVERY_ADDRESS": str(RESCUE),
                                             "RPI_CONTROL_RECOVERY_INTERFACE": "auto"})
        configured.start()
        self.addCleanup(configured.stop)
        self.start()
        self.addCleanup(self.stop)

    def start(self):
        self.app = Application(self.state, [self.recordings], "simulated")
        self.server = Server(("127.0.0.1", 0), self.app, self.token, bind_and_activate=False)

    def stop(self):
        self.server.server_close()
        self.app.close()

    def request(self, data=None, *, expected=200, authenticated=True, origin="http://localhost"):
        client, worker = socket.socketpair()
        client.settimeout(4)
        self.server.process_request(worker, ("127.0.0.1", 1234))
        connection = http.client.HTTPConnection("localhost")
        connection.sock = client
        headers = {"Content-Type": "application/json", "Origin": origin}
        if authenticated:
            headers["Authorization"] = "Bearer " + self.token
        try:
            if data is None:
                connection.request("GET", "/api/status", headers=headers)
            else:
                connection.request("POST", "/api/network", json.dumps(data), headers)
            with connection.getresponse() as response:
                body = response.read().decode()
                self.assertNotIn(WIFI["password"], body)
                self.assertEqual(response.status, expected, body)
                reply = json.loads(body)
                return reply["network"] if data is None and expected == 200 else reply
        finally:
            connection.close()

    def test_wired_only_address_preview_and_rollback(self):
        self.app.network = SimulatedNetwork(self.state, wifi=False)
        initial = self.request()
        self.assertTrue(initial["available"] and initial["simulated"])
        self.assertEqual(set(by_name(initial)), {"eth0"})
        for data in (WIFI, BRIDGE, {**ETHERNET, "interface": "eth7"}):
            self.request(data, expected=400)
        staged = self.request(ETHERNET)
        row = by_name(staged)["eth0"]
        self.assertEqual((row["addresses"], row["gateway"], row["dns"]),
                         ([ETHERNET["address"], ALIAS], ETHERNET["gateway"], ETHERNET["dns"]))
        self.assertEqual(row["configured_primary"], ETHERNET["address"])
        self.assertEqual(row["dns_override"], [])
        pending = staged["pending"]
        self.assertRegex(pending["id"], "^[0-9a-f]{32}$")
        self.assertGreater(pending["remaining_s"], 0)
        restored = self.request({"action": "rollback", "id": pending["id"]})
        self.assertEqual(restored, initial)
        self.request({"action": "confirm", "id": pending["id"]}, expected=409)

    def test_legacy_and_standalone_wifi_preserve_wired_connection(self):
        wired = by_name(self.request())["eth0"]
        legacy = {key: WIFI[key] for key in ("interface", "mode", "ssid", "password")}
        saved = self.request({**legacy, "mode": "ap"})
        self.assertIsNone(saved["pending"])
        self.assertEqual(by_name(saved)["wlan0"]["ipv4_method"], "shared")
        self.assertEqual(by_name(saved)["wlan0"]["configured_primary"], "")
        self.assertEqual(by_name(saved)["eth0"], wired)
        stage = self.request(WIFI)
        radio = by_name(stage)["wlan0"]
        self.assertEqual((radio["mode"], radio["ssid"], radio["ipv4_method"]), ("station", WIFI["ssid"], "auto"))
        self.assertTrue(radio["addresses"])
        self.assertEqual(by_name(stage)["eth0"], wired)
        restored = self.request({"action": "rollback", "id": stage["pending"]["id"]})
        self.assertEqual(by_name(restored), by_name(saved))
        self.assertNotIn(WIFI["password"], (self.state / "network-sim.json").read_text())

    def test_enable_software_blocked_radio_then_confirm_offline_hotspot(self):
        self.app.network = SimulatedNetwork(self.state, dhcp=False, wifi_enabled=False)
        before = self.request()
        wired = by_name(before)["eth0"]
        self.assertEqual(before["wifi_radio"], {"enabled": False, "hardware_enabled": True})
        self.assertEqual(by_name(before)["wlan0"]["state"], "unavailable")
        req = {**WIFI, "mode": "ap", "ipv4_method": "shared", "address": "10.42.0.1/24"}
        self.request(req, expected=400)
        enabled = self.request({"action": "enable_wifi"})
        self.assertEqual(enabled["wifi_radio"]["enabled"], True)
        self.assertEqual(by_name(enabled)["wlan0"]["state"], "disconnected")
        staged = self.request(req)
        ap = by_name(staged)["wlan0"]
        self.assertEqual((ap["mode"], ap["ssid"], ap["addresses"]), ("ap", req["ssid"], [req["address"]]))
        self.request({"action": "enable_wifi"}, expected=409)
        confirmed = self.request({"action": "confirm", "id": staged["pending"]["id"]})
        self.assertEqual(by_name(confirmed)["wlan0"], ap)
        self.assertEqual(by_name(confirmed)["eth0"], wired)

    def test_radio_enable_rejects_missing_or_hardware_blocked_wifi(self):
        for hardware in ({"wifi": False}, {"wifi_hardware_enabled": False}):
            with self.subTest(hardware=hardware):
                self.app.network = SimulatedNetwork(self.state, **hardware)
                self.app.network_until = 0
                before = self.request()
                self.request({"action": "enable_wifi"}, expected=400)
                self.assertEqual(self.request(), before)

    def test_dhcp_dns_override_is_distinct_from_the_lease_and_survives_restart(self):
        inherited = by_name(self.request())["eth0"]
        self.assertEqual(inherited["dns"], [inherited["gateway"]])
        self.assertEqual(inherited["dns_override"], [])
        dns = ["198.51.100.53"]
        staged = self.request({**WIFI, "dns": dns})
        self.request({"action": "confirm", "id": staged["pending"]["id"]})
        self.stop()
        self.start()
        radio = by_name(self.request())["wlan0"]
        self.assertEqual((radio["dns_override"], radio["dns"]), (dns, dns))

        # Exercise upgrades from the fixture format that stored only effective DNS.
        self.stop()
        path = self.state / "network-sim.json"
        saved = json.loads(path.read_text())
        for row in saved.values():
            del row["dns_override"]
        path.write_text(json.dumps(saved))
        self.start()
        rows = by_name(self.request())
        self.assertEqual(rows["eth0"]["dns_override"], [])
        self.assertEqual(rows["wlan0"]["dns_override"], dns)
        cleared = by_name(self.request(WIFI))["wlan0"]
        self.assertEqual((cleared["dns_override"], cleared["dns"]), ([], [cleared["gateway"]]))

    def test_bridge_owns_common_ip_and_survives_restart(self):
        staged = self.request(BRIDGE)
        rows = by_name(staged)
        self.assertEqual(set(rows), {"eth0", "wlan0", "br-rpi"})
        self.assertEqual(rows["br-rpi"]["addresses"], [BRIDGE["address"], ALIAS])
        self.assertEqual(rows["br-rpi"]["configured_primary"], BRIDGE["address"])
        self.assertEqual(staged["recovery"]["interface"], "br-rpi")
        self.assertCountEqual(rows["br-rpi"]["ports"], ["wlan0", "eth0"])
        for name in ["eth0", "wlan0"]:
            self.assertEqual((rows[name]["controller"], rows[name]["addresses"], rows[name]["gateway"],
                              rows[name]["dns"]), ("br-rpi", [], "", []))
        self.assertEqual(rows["wlan0"]["mode"], "ap")
        confirmed = self.request({"action": "confirm", "id": staged["pending"]["id"]})
        self.assertIsNone(confirmed["pending"])
        self.assertNotIn(ALIAS, (self.state / "network-sim.json").read_text())
        self.assertNotIn(WIFI["password"], (self.state / "network-sim.json").read_text())
        self.assertEqual(by_name(confirmed), rows)

        standalone = self.request(ETHERNET)
        self.assertNotIn("br-rpi", by_name(standalone))
        self.assertFalse(by_name(standalone)["wlan0"]["addresses"])
        self.stop()
        self.start()
        self.assertEqual(self.request(), confirmed)

    def test_recovery_without_lease_preserves_manual_profile_and_tracks_current_configuration(self):
        self.app.network = SimulatedNetwork(self.state, dhcp=False)
        initial = self.request()
        recovery = initial["recovery"]
        self.assertEqual((recovery["state"], recovery["address"], recovery["interface"]),
                         ("active", str(RESCUE), "eth0"))
        wired = by_name(initial)["eth0"]
        self.assertEqual((wired["configured_primary"], wired["addresses"], wired["gateway"], wired["dns"]),
                         ("", [ALIAS], "", []))
        native = self.request({**ETHERNET, "address": str(RESCUE), "gateway": "", "dns": []})
        self.assertEqual(native["recovery"]["state"], "native")
        self.assertEqual(by_name(native)["eth0"]["addresses"], [str(RESCUE)])
        self.request({"action": "confirm", "id": native["pending"]["id"]})
        alternate = IPv4Interface("192.168.88.50/24")
        os.environ["RPI_CONTROL_RECOVERY_ADDRESS"] = str(alternate)
        os.environ["RPI_CONTROL_RECOVERY_INTERFACE"] = "eth0"
        self.stop()
        self.start()
        changed = self.request()
        self.assertEqual(changed["recovery"]["address"], str(alternate))
        self.assertEqual(changed["recovery"]["state"], "active")
        self.assertEqual(by_name(changed)["eth0"]["addresses"], [str(RESCUE), f"{alternate.ip}/32"])
        self.assertEqual(by_name(changed)["eth0"]["configured_primary"], str(RESCUE))
        os.environ["RPI_CONTROL_RECOVERY_ADDRESS"] = "off"
        self.app.network_until = 0
        disabled = self.request()
        self.assertEqual((disabled["recovery"]["state"], disabled["recovery"]["address"]), ("disabled", ""))
        self.assertEqual(by_name(disabled)["eth0"]["addresses"], [str(RESCUE)])

    def test_recovery_clash_on_wireless_is_reported_and_rollback_restores_address(self):
        trial = self.request({**WIFI, "ipv4_method": "manual", "address": str(RESCUE)})
        rows = by_name(trial)
        self.assertEqual(trial["recovery"]["state"], "conflict")
        self.assertNotIn(ALIAS, rows["eth0"]["addresses"])
        self.assertEqual(rows["wlan0"]["addresses"], [str(RESCUE)])
        restored = self.request({"action": "rollback", "id": trial["pending"]["id"]})
        self.assertEqual(restored["recovery"]["state"], "active")
        self.assertIn(ALIAS, by_name(restored)["eth0"]["addresses"])
        for address, selector, state in ((str(RESCUE), "eth9", "waiting"), (str(RESCUE), "wlan0", "waiting"),
                                         ("invalid", "eth0", "error")):
            with self.subTest(address=address, selector=selector):
                os.environ["RPI_CONTROL_RECOVERY_ADDRESS"] = address
                os.environ["RPI_CONTROL_RECOVERY_INTERFACE"] = selector
                self.app.network_until = 0
                result = self.request()
                self.assertEqual(result["recovery"]["state"], state)
                self.assertNotIn(ALIAS, by_name(result)["eth0"]["addresses"])

    def test_independent_address_on_same_wired_subnet_preserves_primary_gateway_and_dns(self):
        settings = {**ETHERNET, "address": "192.168.55.15/24", "gateway": "192.168.55.1",
                    "dns": ["192.168.55.50"]}
        staged = self.request(settings)
        committed = self.request({"action": "confirm", "id": staged["pending"]["id"]})
        wired = by_name(committed)["eth0"]
        self.assertEqual(committed["recovery"]["state"], "active")
        self.assertEqual(wired["addresses"], [settings["address"], ALIAS])
        self.assertEqual((wired["configured_primary"], wired["gateway"], wired["dns"]),
                         (settings["address"], settings["gateway"], settings["dns"]))
        self.assertNotIn(ALIAS, (self.state / "network-sim.json").read_text())

    def test_dhcp_trials_require_a_lease_even_when_wired_recovery_is_active(self):
        self.app.network = SimulatedNetwork(self.state, dhcp=False)
        original = self.request()
        self.assertEqual(by_name(original)["eth0"]["addresses"], [ALIAS])
        for settings in (ETHERNET, BRIDGE, WIFI):
            with self.subTest(mode=settings["mode"]):
                self.request({**settings, "ipv4_method": "auto", "address": "", "gateway": "", "dns": []},
                             expected=503)
                self.assertEqual(self.request(), original)
        self.assertFalse((self.state / "network-sim.json").exists())

    def test_lease_loss_before_confirmation_rolls_back_to_confirmed_primary(self):
        staged = self.request(ETHERNET)
        original = self.request({"action": "confirm", "id": staged["pending"]["id"]})
        trial = self.request({**BRIDGE, "ipv4_method": "auto", "address": ""})["pending"]["id"]
        with patch.object(self.app.network, "_dhcp", False):
            self.app.network_until = 0
            pending = self.request()
            self.assertEqual(by_name(pending)["br-rpi"]["addresses"], [ALIAS])
            self.assertEqual(pending["recovery"]["state"], "active")
            self.request({"action": "confirm", "id": trial}, expected=503)
            self.assertEqual(self.request(), original)
            self.request({"action": "confirm", "id": trial}, expected=409)

    def test_saved_profile_before_recovery_upgrade_keeps_primary_and_no_rescue_on_disk(self):
        staged = self.request(ETHERNET)
        self.request({"action": "confirm", "id": staged["pending"]["id"]})
        self.stop()
        path = self.state / "network-sim.json"
        saved = json.loads(path.read_text())
        for row in saved.values():
            del row["configured_primary"]
        path.write_text(json.dumps(saved))
        self.start()
        restored = self.request()
        self.assertEqual(by_name(restored)["eth0"]["configured_primary"], ETHERNET["address"])
        self.assertEqual(by_name(restored)["eth0"]["addresses"], [ETHERNET["address"], ALIAS])
        self.assertNotIn(ALIAS, path.read_text())

    def test_timeout_and_late_confirmation_restore_last_confirmed_configuration(self):
        original = self.request()
        with patch("rpi_web_control.network_sim.monotonic", return_value=500) as clock:
            staged = self.request(ETHERNET)
            token = staged["pending"]["id"]
            self.assertEqual(staged["pending"]["remaining_s"], CONFIRM_TIMEOUT)
            self.request(WIFI, expected=409)
            self.request({"action": "rollback", "id": "0" * 32}, expected=409)
            self.assertEqual(self.request()["pending"]["id"], token)
            clock.return_value = 500 + CONFIRM_TIMEOUT
            self.request({"action": "confirm", "id": token}, expected=409)
            next_change = self.request(WIFI)
            self.assertNotEqual(next_change["pending"]["id"], token)
            self.assertEqual(by_name(next_change)["eth0"], by_name(original)["eth0"])
            self.assertEqual(self.request({"action": "rollback", "id": next_change["pending"]["id"]}), original)

    def test_failed_save_can_be_retried_or_rolled_back(self):
        initial = self.request()
        trial = self.request(ETHERNET)["pending"]["id"]
        with patch("rpi_web_control.network_sim.atomic_text", side_effect=OSError("Unable to save configuration.")):
            self.request({"action": "confirm", "id": trial}, expected=503)
        self.assertEqual(self.request()["pending"]["id"], trial)
        self.assertEqual(self.request({"action": "rollback", "id": trial}), initial)
        self.assertFalse((self.state / "network-sim.json").exists())

    def test_rejects_foreign_origins_and_invalid_inputs_without_changing_state(self):
        before = self.request()
        self.request(BRIDGE, expected=401, authenticated=False)
        self.request(BRIDGE, expected=403, origin="http://external")
        for data in ({**ETHERNET, "password": WIFI["password"]}, {**ETHERNET, "address": "invalid"},
                     {**BRIDGE, "interface": "wlan0"}, {**BRIDGE, "wifi_interface": "eth0"},
                     {**WIFI, "ssid": ""}, {"action": "confirm", "id": []}):
            with self.subTest(data_keys=data.keys()):
                self.request(data, expected=400)
        self.assertEqual(self.request(), before)
