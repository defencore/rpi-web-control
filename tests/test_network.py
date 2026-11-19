"""Exercise the unprivileged network client and the systemd socket protocol."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from rpi_web_control.network import NM, ROOT, Network, _Manager, helper_main, normalize_request
from rpi_web_control.network_transaction import NetworkConflict
from rpi_web_control import recovery_config


LEGACY = {"interface": "wlan0", "mode": "station", "ssid": "Workshop", "password": "some test passphrase"}
WIRED = {"action": "apply", "interface": "eth0", "mode": "ethernet", "ipv4_method": "auto",
         "address": "", "gateway": "", "dns": []}
TRIAL = {"id": "a" * 32, "remaining_s": 120}


class NetworkTests(unittest.TestCase):
    def helper(self, request):
        raw = json.dumps(request).encode() + b"\n"
        output = io.StringIO()
        with (patch("sys.stdin", MagicMock(buffer=io.BytesIO(raw))), patch("sys.stdout", output),
              patch("rpi_web_control.network._Manager"), patch("rpi_web_control.network.NetworkTransaction") as engine):
            transaction = engine.return_value
            transaction.stage.return_value = TRIAL
            transaction.status.return_value = TRIAL
            helper_main()
        self.assertNotIn(LEGACY["password"], output.getvalue())
        return json.loads(output.getvalue()), transaction

    def test_current_protocol_requires_separate_confirmation(self):
        reply, change = self.helper(WIRED)
        self.assertEqual(reply, {"ok": True, "pending": TRIAL})
        change.stage.assert_called_once_with(WIRED)
        change.confirm.assert_not_called()
        for action, method in (("confirm", "confirm"), ("rollback", "cancel")):
            with self.subTest(action=action):
                reply, change = self.helper({"action": action, "id": TRIAL["id"]})
                self.assertEqual(reply, {"ok": True, "pending": None})
                getattr(change, method).assert_called_once_with(TRIAL["id"])
                change.stage.assert_not_called()

    def test_legacy_wifi_clients_keep_their_activation_semantics(self):
        for mode, method in (("station", "auto"), ("ap", "shared")):
            with self.subTest(mode=mode):
                reply, change = self.helper(dict(LEGACY, mode=mode))
                self.assertEqual(reply, {"ok": True, "pending": None})
                self.assertEqual(change.stage.call_args.args[0]["ipv4_method"], method)
                change.confirm.assert_called_once_with(TRIAL["id"])

    def test_enable_wifi_protocol_has_no_arbitrary_radio_properties(self):
        reply, change = self.helper({"action": "enable_wifi"})
        self.assertEqual(reply, {"ok": True, "pending": None})
        change.enable_wifi.assert_called_once_with()
        change.stage.assert_not_called()
        for extra in ({"enabled": False}, {"property": "NetworkingEnabled"}):
            reply, change = self.helper({"action": "enable_wifi", **extra})
            self.assertEqual(reply["kind"], "invalid")
            change.enable_wifi.assert_not_called()

    def test_networkmanager_reports_radio_and_hardware_switch_separately(self):
        manager = _Manager()
        with patch.object(manager, "prop", side_effect=(0, 1)) as get:
            self.assertEqual(manager.wifi_radio(), {"enabled": False, "hardware_enabled": True})
        self.assertEqual([call.args for call in get.call_args_list],
                         [(ROOT, NM, "WirelessEnabled"), (ROOT, NM, "WirelessHardwareEnabled")])

    def test_rejects_malformed_mutations_before_using_networkmanager(self):
        for request in (dict(LEGACY, command="other"), {"action": "confirm", "id": "/foo"},
                        {"action": "rollback", "id": TRIAL["id"], "interface": "eth0"}, None):
            with self.subTest(request_type=type(request)):
                reply, change = self.helper(request)
                self.assertEqual(reply["kind"], "invalid")
                change.stage.assert_not_called()
                change.confirm.assert_not_called()
                change.cancel.assert_not_called()
        with self.assertRaises(ValueError):
            normalize_request({"action": "status"})

    def test_socket_conflict_reaches_the_api_client(self):
        request = {"action": "confirm", "id": TRIAL["id"]}
        with patch("rpi_web_control.network.socket.socket") as connect:
            sock = connect.return_value.__enter__.return_value
            sock.makefile.return_value = io.BytesIO(b'{"ok":false,"kind":"conflict","error":"Trial expired."}\n')
            with self.assertRaisesRegex(NetworkConflict, "Trial expired"):
                Network().apply(request)
            self.assertEqual(json.loads(sock.sendall.call_args.args[0]), request)

    def test_scan_requires_boolean_ap_opt_in(self):
        base = {"action": "scan_wifi", "interface": "wlan0"}
        for extra in ({}, {"allow_ap_scan": False}, {"allow_ap_scan": True}):
            with self.subTest(extra=extra):
                settings = {**base, **extra}
                self.assertEqual(normalize_request(settings), (settings, False))
        for extra in ({"allow_ap_scan": "false"}, {"allow_ap_scan": 1}, {"restart_ap": True}):
            with self.subTest(extra=extra):
                reply, change = self.helper({**base, **extra})
                self.assertEqual(reply["kind"], "invalid")
                change.request_scan.assert_not_called()

    def test_ethernet_inventory_read_without_activating_the_privileged_helper(self):
        ethernet = {"name": "eth0", "state": "connected", "managed": True, "physical": True, "type": "ethernet"}
        with (patch("rpi_web_control.network._Manager"), patch("rpi_web_control.network._helper") as helper,
              patch("rpi_web_control.network.TRIAL_JOURNAL") as journal,
              patch("rpi_web_control.network.inventory", return_value=[ethernet])):
            journal.is_file.return_value = False
            status = Network().status()
            self.assertTrue(status["available"])
            self.assertEqual(status["interfaces"], [ethernet])
            self.assertIsNone(status["pending"])
            helper.assert_not_called()
            journal.is_file.return_value = True
            helper.return_value = TRIAL
            self.assertEqual(Network().status()["pending"], TRIAL)
            helper.assert_called_once_with({"action": "status"})

    def test_backend_absent(self):
        with (tempfile.TemporaryDirectory() as tmp,
              patch("rpi_web_control.network._Manager.__enter__", side_effect=ImportError),
              patch("rpi_web_control.network.read_status", side_effect=lambda: recovery_config.read_status(Path(tmp))),
              patch.dict("os.environ", {"RPI_CONTROL_RECOVERY_ADDRESS": recovery_config.DEFAULT_ADDRESS,
                                        "RPI_CONTROL_RECOVERY_INTERFACE": "auto"})):
            with patch("time.monotonic", return_value=100):
                recovery_config.write_status(tmp, recovery_config.DEFAULT_ADDRESS, "eth0", "active", "Active.")
            with patch("time.monotonic", return_value=105):
                state = Network().status()
            self.assertFalse(state["available"])
            self.assertEqual(state["recovery"]["state"], "active")
            self.assertEqual(state["recovery"]["interface"], "eth0")
            with patch("time.monotonic", return_value=200):
                self.assertEqual(Network().status()["recovery"]["state"], "waiting")
