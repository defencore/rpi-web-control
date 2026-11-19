"""Exercise the checkpoint protocol through a simulated NetworkManager D-Bus adapter."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rpi_web_control import network_transaction as tx
from rpi_web_control.network_profiles import BRIDGE, ETHERNET, OWNER, WIFI


BRIDGE_REQUEST = {"action": "apply", "mode": "bridge", "interface": "eth0", "wifi_interface": "wlan0",
                  "ipv4_method": "manual", "address": "192.0.2.6/24", "gateway": "", "dns": [],
                  "ssid": "CameraLab", "password": "sample-network-pass"}
WIRED_REQUEST = {k: v for k, v in BRIDGE_REQUEST.items() if k not in ("wifi_interface", "ssid", "password")}
WIRED_REQUEST["mode"] = "ethernet"
STATION_REQUEST = {"action": "apply", "mode": "station", "interface": "wlan0", "security": "wpa2-personal",
                   "ssid": "Office", "password": "test passphrase", "ipv4_method": "auto",
                   "address": "", "gateway": "", "dns": []}


class DBusTypes:
    DBusException = OSError
    Int32 = UInt32 = int
    ByteArray = bytes
    Boolean = bool

    @staticmethod
    def Array(value, signature):
        return list(value)

    @staticmethod
    def Dictionary(value, signature):
        return dict(value)


class TransactionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.profiles = {}
        self.active = {}
        self.addresses = [{"address": "192.0.2.6", "prefix": 24}]
        self.saved_names = []
        self.devices = [self.row("eth0", "ethernet"), self.row("wlan0", "wifi")]
        self.nm = Mock(dbus=DBusTypes)
        self.nm.bus.get_name_owner.return_value = ":1.35"
        self.nm.prop.side_effect = self.property
        self.settings = Mock()
        self.settings.ListConnections.side_effect = lambda: list(self.profiles)
        self.nm.iface.side_effect = self.interface
        self.checkpoints = []
        self.nm.manager.CheckpointCreate.side_effect = self.checkpoint
        self.nm.manager.CheckpointRollback.side_effect = self.rollback
        self.nm.manager.CheckpointDestroy.side_effect = self.checkpoints.remove
        self.nm.manager.AddAndActivateConnection2.side_effect = self.activate
        self.engine = self.new_engine()
        mock_inventory = patch.object(tx, "inventory", side_effect=lambda nm: self.devices)
        mock_inventory.start()
        self.addCleanup(mock_inventory.stop)
        self.admin = self.profile("administrator", "eth0", ETHERNET, managed=False)
        self.previous = self.profile("previous", "wlan0", WIFI)

    def new_engine(self):
        return tx.NetworkTransaction(self.nm, self.directory)

    def row(self, name, kind):
        return {"name": name, "path": "/" + name, "type": kind, "managed": True, "state": "connected",
                "physical": kind != "bridge", "ap": kind == "wifi", "controller": "", "active_uuid": "", "ports": []}

    def profile(self, uid, name, kind, managed=True, **extra):
        data = {"connection": {"uuid": uid, "interface-name": name, "type": kind, **extra},
                "user": {"data": {OWNER: "1"} if managed else {}}}
        profile = Mock()
        profile.GetSettings.return_value = data
        profile.Delete.side_effect = lambda: self.profiles.pop(uid)
        profile.Update2.side_effect = lambda *args: self.saved_names.append(name)
        self.profiles[uid] = profile
        return profile

    def interface(self, path, iface):
        if iface == tx.PROPERTIES:
            proxy = Mock()
            proxy.GetAll.side_effect = lambda iface: self.active[path]
            return proxy
        return self.settings if iface == tx.SETTINGS else self.profiles[path]

    def property(self, path, iface, key):
        if key == "Checkpoints":
            return self.checkpoints
        if key == "ActiveConnection":
            return "/active-br"
        if key == "Interface":
            return path.removeprefix("/")
        if key == "AddressData":
            return self.addresses
        return self.active[path][key]

    def checkpoint(self, devices, timeout, flags):
        self.checkpoints.append("/checkpoint")
        self.affected = devices
        return "/checkpoint"

    def rollback(self, checkpoint):
        self.checkpoints.remove(checkpoint)
        return {path: 0 for path in self.affected}

    def activate(self, profile, device, specific, options):
        data = copy.deepcopy(profile)
        con = data["connection"]
        uid = con["uuid"]
        # NetworkManager's GetSettings never returns secrets.
        data.pop(WIFI + "-security", None)
        handler = self.profile(uid, con["interface-name"], con["type"])
        handler.GetSettings.return_value = data
        active = "/active-" + uid
        self.active[active] = {"Uuid": uid, "State": 2, "Devices": ["/" + con["interface-name"]],
                               "Ip4Config": "/ipv4" if "ipv4" in data else "/"}
        if data.get("ipv4", {}).get("method") == "auto":
            self.active[active]["Dhcp4Config"] = "/lease"
        if con.get("master"):
            # Exercise both the current and the Bookworm property names.
            field = "Controller" if con["type"] == ETHERNET else "Master"
            self.active[active][field] = "/" + BRIDGE
        return uid, active, {}

    def test_trial_survives_helper_exit_and_confirms_all_ports_without_reading_secrets(self):
        result = self.engine.stage(BRIDGE_REQUEST)
        self.assertEqual(set(result), {"id", "remaining_s"})
        self.assertEqual(result["remaining_s"], 120)
        self.assertRegex(result["id"], "^[0-9a-f]{32}$")
        self.nm.manager.CheckpointCreate.assert_called_once_with(["/eth0", "/wlan0"], 95, tx.DISCONNECT_NEW_DEVICES)
        self.nm.manager.CheckpointAdjustRollbackTimeout.assert_called_with("/checkpoint", 120)
        add = self.nm.manager.AddAndActivateConnection2
        self.assertEqual([c.args[1] for c in add.call_args_list], ["/", "/eth0", "/wlan0"])
        self.assertTrue(all(c.args[3] == {"persist": "volatile"} for c in add.call_args_list))
        journal = self.engine.journal.read_text()
        self.assertNotIn(BRIDGE_REQUEST["password"], journal)
        self.assertNotIn(BRIDGE_REQUEST["ssid"], journal)
        for profile in self.profiles.values():
            profile.Update2.assert_not_called()
            profile.Delete.assert_not_called()
        resumed = self.new_engine()
        self.assertEqual(resumed.status()["id"], result["id"])
        with self.assertRaises(tx.NetworkConflict):
            resumed.stage(WIRED_REQUEST)
        with self.assertRaises(tx.NetworkConflict):
            resumed.confirm("stale")
        with self.engine._lock():
            self.assertEqual(resumed.status()["id"], result["id"])
            with self.assertRaises(tx.NetworkConflict):
                resumed.cancel(result["id"])
        resumed.confirm(result["id"])
        self.assertEqual(self.saved_names, ["eth0", "wlan0", BRIDGE])
        for uid, profile in self.profiles.items():
            profile.GetSecrets.assert_not_called()
            if uid != "administrator":
                profile.Update2.assert_called_once_with({}, 1, {})
        self.previous.Delete.assert_called_once()
        self.admin.Delete.assert_not_called()
        self.assertIsNone(self.engine.status())
        with self.assertRaises(tx.NetworkConflict):
            resumed.confirm(result["id"])

    def test_leave_bridge_retires_owned_controller_and_ports(self):
        bridge = self.profile("br-uuid", BRIDGE, "bridge")
        wired = self.profile("eth-port", "eth0", ETHERNET, master="br-uuid")
        radio = self.profile("wifi-port", "wlan0", WIFI, controller=BRIDGE)
        self.active["/active-br"] = {"Uuid": "br-uuid", "State": 2}
        self.devices.append(dict(self.row(BRIDGE, "bridge"), active_uuid="br-uuid", ports=["eth0", "wlan0"]))
        for device, uid in zip(self.devices, ("eth-port", "wifi-port")):
            device.update(controller=BRIDGE, active_uuid=uid)
        trial = self.engine.stage(WIRED_REQUEST)
        self.nm.manager.CheckpointCreate.assert_called_once_with(["/br-rpi", "/eth0", "/wlan0"], 95, 0)
        self.nm.manager.DeactivateConnection.assert_called_once_with("/active-br")
        self.engine.confirm(trial["id"])
        for profile in (bridge, wired, radio):
            profile.Delete.assert_called_once()
        self.previous.Delete.assert_not_called()
        self.admin.Delete.assert_not_called()

    def test_station_checkpoint_and_persistence_are_limited_to_the_wifi_device(self):
        wired = self.profile("wired-static", "eth0", ETHERNET)
        wired.GetSettings.return_value["ipv4"] = {"method": "manual",
                                                  "address-data": [{"address": "192.168.55.100", "prefix": 24}]}
        original_settings = copy.deepcopy(wired.GetSettings.return_value)
        self.devices[0].update(active_uuid="wired-static", addresses=["192.168.55.100/24"])
        original_eth = copy.deepcopy(self.devices[0])
        self.previous.GetSettings.return_value[WIFI] = {"mode": "ap"}
        for action in ("rollback", "confirm"):
            trial = self.engine.stage(STATION_REQUEST)
            self.nm.manager.CheckpointCreate.assert_called_with(["/wlan0"], 95, 0)
            self.assertEqual(self.nm.manager.AddAndActivateConnection2.call_args.args[1], "/wlan0")
            if action == "confirm":
                self.engine.confirm(trial["id"])
                self.previous.Delete.assert_called_once()
                self.assertEqual(self.saved_names, ["wlan0"])
            else:
                self.engine.cancel(trial["id"])
                self.previous.Delete.assert_not_called()
            self.nm.manager.DeactivateConnection.assert_not_called()
            self.assertEqual(self.devices[0], original_eth)
            self.assertEqual(wired.GetSettings.return_value, original_settings)
            for profile in (wired, self.admin):
                profile.Delete.assert_not_called()
                profile.Update2.assert_not_called()

    def test_station_join_rejects_active_or_saved_bridge_port_before_checkpoint(self):
        self.devices[1]["controller"] = BRIDGE
        with self.assertRaisesRegex(ValueError, "bridge port"):
            self.engine.stage(STATION_REQUEST)
        self.devices[1]["controller"] = ""
        self.profile("bridge-uuid", BRIDGE, "bridge")
        self.profile("wifi-port", "wlan0", WIFI, master="bridge-uuid")
        with self.assertRaisesRegex(ValueError, "bridge port"):
            self.engine.stage(STATION_REQUEST)
        self.nm.manager.CheckpointCreate.assert_not_called()
        self.nm.manager.DeactivateConnection.assert_not_called()

        # A second radio can join while the original radio and Ethernet stay on their bridge.
        self.devices.append(self.row("wlan1", "wifi"))
        trial = self.engine.stage(dict(STATION_REQUEST, interface="wlan1"))
        self.nm.manager.CheckpointCreate.assert_called_once_with(["/wlan1"], 95, 0)
        self.engine.confirm(trial["id"])
        self.assertEqual(self.saved_names, ["wlan1"])
        self.profiles["bridge-uuid"].Delete.assert_not_called()
        self.profiles["wifi-port"].Delete.assert_not_called()
        self.nm.manager.DeactivateConnection.assert_not_called()

    def test_station_activation_failure_rolls_back_only_radio_and_preserves_settings(self):
        wired = self.profile("fixed-address", "eth0", ETHERNET)

        def failed_auth(*args):
            uid, active, options = self.activate(*args)
            self.active[active]["State"] = 4  # NetworkManager deactivated the candidate.
            return uid, active, options

        self.nm.manager.AddAndActivateConnection2.side_effect = failed_auth
        with self.assertRaisesRegex(RuntimeError, "security, password") as caught:
            self.engine.stage(STATION_REQUEST)
        self.assertNotIn(STATION_REQUEST["password"], str(caught.exception))
        self.assertEqual(self.affected, ["/wlan0"])
        self.nm.manager.CheckpointRollback.assert_called_once_with("/checkpoint")
        self.assertIsNone(self.engine.status())
        self.assertEqual(self.saved_names, [])
        for profile in (wired, self.previous):
            profile.Delete.assert_not_called()
            profile.Update2.assert_not_called()

    def test_recovery_address_alone_cannot_confirm_a_dhcp_trial(self):
        trial = self.engine.stage(dict(WIRED_REQUEST, ipv4_method="auto", address=""))
        next(iter(self.active.values()))["Dhcp4Config"] = "/"
        self.addresses = [{"address": "192.168.55.100", "prefix": 32}]
        with self.assertRaisesRegex(RuntimeError, "no longer active"):
            self.engine.confirm(trial["id"])
        self.nm.manager.CheckpointRollback.assert_called_once()

    def test_cancel_and_expiry_preserve_administrator_and_previous_settings(self):
        first = self.engine.stage(BRIDGE_REQUEST)
        self.engine.cancel(first["id"])
        self.nm.manager.CheckpointRollback.assert_called_once_with("/checkpoint")
        self.assertEqual(set(self.profiles), {"administrator", "previous"})
        second = self.engine.stage(BRIDGE_REQUEST)
        self.checkpoints.clear()  # NetworkManager's own timeout while no helper process is running.
        self.assertIsNone(self.new_engine().status())
        self.assertEqual(set(self.profiles), {"administrator", "previous"})
        with self.assertRaises(tx.NetworkConflict):
            self.engine.confirm(second["id"])

    def test_enabling_wifi_changes_only_the_radio_and_respects_pending_trials(self):
        props = Mock()
        original_iface = self.nm.iface.side_effect
        self.nm.iface.side_effect = lambda path, name: (props if (path, name) == (tx.ROOT, tx.PROPERTIES)
                                                       else original_iface(path, name))
        self.nm.wifi_radio.return_value = {"enabled": False, "hardware_enabled": True}
        self.engine.enable_wifi()
        props.Set.assert_called_once_with(tx.NM, "WirelessEnabled", True)
        self.nm.manager.CheckpointCreate.assert_not_called()
        self.nm.manager.AddAndActivateConnection2.assert_not_called()
        self.nm.wifi_radio.return_value["enabled"] = True
        self.engine.enable_wifi()
        self.assertEqual(props.Set.call_count, 1)
        self.nm.wifi_radio.return_value.update(enabled=False, hardware_enabled=False)
        with self.assertRaisesRegex(ValueError, "hardware-blocked"):
            self.engine.enable_wifi()
        self.engine.stage(WIRED_REQUEST)
        with self.assertRaises(tx.NetworkConflict):
            self.engine.enable_wifi()
        self.assertEqual(props.Set.call_count, 1)

    def test_scan_request_respects_network_trials(self):
        with patch.object(tx, "request_wifi_scan") as request:
            self.assertIs(self.engine.request_scan("wlan0"), request.return_value)
            self.engine.stage(WIRED_REQUEST)
            with self.assertRaises(tx.NetworkConflict):
                self.engine.request_scan("wlan0", allow_ap_scan=True)
            request.assert_called_once_with(self.nm, "wlan0", allow_ap_scan=False)

    def test_partial_commit_is_reversed_under_the_same_checkpoint(self):
        trial = self.engine.stage(BRIDGE_REQUEST)
        bridge, wired, wifi = [p for p in self.profiles.values() if p not in (self.admin, self.previous)]
        wifi.Update2.side_effect = OSError("Persistence failed")
        with self.assertRaises(OSError):
            self.engine.confirm(trial["id"])
        wired.Update2.assert_called_once()
        bridge.Update2.assert_not_called()
        self.nm.manager.CheckpointRollback.assert_called_once_with("/checkpoint")
        self.assertEqual(set(self.profiles), {"administrator", "previous"})
        self.assertIsNone(self.engine.status())

    def test_confirm_rejects_activated_port_with_a_different_controller(self):
        trial = self.engine.stage(BRIDGE_REQUEST)
        wifi, = (active for active in self.active.values() if "Master" in active)
        wifi["Master"] = "/other-bridge"
        with self.assertRaisesRegex(RuntimeError, "port is no longer attached"):
            self.engine.confirm(trial["id"])
        self.assertEqual(self.saved_names, [])
        self.nm.manager.CheckpointRollback.assert_called_once_with("/checkpoint")

    def test_resumed_confirm_requires_the_requested_address_and_prefix(self):
        for actual in ({"address": "192.0.2.50", "prefix": 24}, {"address": "192.0.2.6", "prefix": 25}):
            with self.subTest(actual=actual):
                self.addresses = [actual, {"address": "192.0.2.6", "prefix": 24}]
                trial = self.engine.stage(BRIDGE_REQUEST)
                self.addresses = [actual]
                with self.assertRaisesRegex(RuntimeError, "requested address"):
                    self.new_engine().confirm(trial["id"])
                self.assertEqual(set(self.profiles), {"administrator", "previous"})
                self.assertEqual(self.saved_names, [])

    def test_rollback_reports_incomplete_or_failed_device_results(self):
        for result in ({"/eth0": 0}, {"/eth0": 3, "/wlan0": 0},
                       {"/eth0": 0, "/wlan0": 0, "/br-rpi": 2}):
            with self.subTest(result=result):
                trial = self.engine.stage(BRIDGE_REQUEST)
                def incomplete(checkpoint):
                    self.checkpoints.remove(checkpoint)
                    return result
                self.nm.manager.CheckpointRollback.side_effect = incomplete
                with self.assertRaisesRegex(RuntimeError, "could not confirm rollback"):
                    self.engine.cancel(trial["id"])
                self.assertEqual(set(self.profiles), {"administrator", "previous"})

    def test_failure_during_second_port_activation_restores_previous_network(self):
        def fail_on_radio(profile, device, specific, options):
            if device == "/wlan0":
                raise OSError("Radio disappeared")
            return self.activate(profile, device, specific, options)

        self.nm.manager.AddAndActivateConnection2.side_effect = fail_on_radio
        with self.assertRaises(OSError):
            self.engine.stage(BRIDGE_REQUEST)
        self.nm.manager.CheckpointRollback.assert_called_once_with("/checkpoint")
        self.assertEqual(set(self.profiles), {"administrator", "previous"})

    def test_restarted_networkmanager_does_not_rollback_reused_checkpoint(self):
        trial = self.engine.stage(BRIDGE_REQUEST)
        self.nm.bus.get_name_owner.return_value = ":1.50"
        with self.assertRaises(tx.NetworkConflict):
            self.new_engine().cancel(trial["id"])
        self.nm.manager.CheckpointRollback.assert_not_called()
        self.assertEqual(set(self.profiles), {"administrator", "previous"})

    def test_external_bridge_collision_rejected_before_checkpoint(self):
        self.profile("external-br", BRIDGE, "bridge", managed=False)
        with self.assertRaises(ValueError):
            self.engine.stage(BRIDGE_REQUEST)
        self.nm.manager.CheckpointCreate.assert_not_called()
