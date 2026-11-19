"""Reconcile a private model of kernel networking. No host network or raw sockets."""

import copy
import ipaddress
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rpi_web_control import network_recovery as recovery


CIDR = ipaddress.IPv4Interface("192.168.55.100/24")
LINK = {"physical": "eth0", "physical_index": 3, "interface": "eth0", "ifindex": 3,
        "mac": "02:00:00:00:00:01", "carrier_changes": 1}


class Kernel:
    def __init__(self):
        self.link = LINK.copy()
        self.address_rows = [{"ifname": "eth0", "ifindex": 3, "addr_info": []},
                             {"ifname": "br-rpi", "ifindex": 4, "addr_info": []}]
        self.policy_routes = []
        self.policy_rules = []
        self.operations = []
        self.fail_once = None

    def target(self, selector, previous):
        if not self.link:
            raise recovery.Unavailable("waiting", "No carrier.")
        return self.link.copy()

    def addresses(self):
        return copy.deepcopy(self.address_rows)

    def routes(self):
        return copy.deepcopy(self.policy_routes)

    def rules(self):
        return copy.deepcopy(self.policy_rules)

    def change(self, kind, operation, claim):
        self.operations.append((kind, operation, claim["link"]["interface"]))
        if self.fail_once == (kind, operation):
            self.fail_once = None
            raise recovery.IPCommandError("Simulated command failure.")
        link = claim["link"]
        cidr = ipaddress.IPv4Interface(claim["cidr"])
        if kind == "addr":
            items = next(row["addr_info"] for row in self.address_rows if row["ifindex"] == link["ifindex"])
            value = {"local": str(cidr.ip), "prefixlen": 32, "label": claim["label"]}
        elif kind == "route":
            items = self.policy_routes
            value = {"table": recovery.TABLE, "dst": str(cidr.network), "dev": link["interface"],
                     "protocol": str(recovery.PROTOCOL), "scope": 253, "metric": recovery.METRIC,
                     "prefsrc": str(cidr.ip), "flags": []}
        else:
            items = self.policy_rules
            value = {"src": str(cidr.ip), "dst": str(cidr.network.network_address), "dstlen": cidr.network.prefixlen,
                     "priority": recovery.PRIORITY, "table": str(recovery.TABLE), "protocol": str(recovery.PROTOCOL)}
        if operation == "add":
            items.append(value)
        else:
            items.remove(value)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.kernel = Kernel()
        self.engine = recovery.Recovery(self.directory, self.kernel)
        for name, value in (("get_config", (CIDR, "auto")), ("probe_address", True), ("announce_address", None)):
            mock = patch.object(recovery, name, return_value=value)
            setattr(self, name, mock.start())
            self.addCleanup(mock.stop)

    def test_idempotence_cleanup_and_reserved_return_policy(self):
        native = {"local": "10.40.0.5", "prefixlen": 24, "label": "eth0"}
        self.kernel.address_rows[0]["addr_info"].append(native)
        self.assertEqual(self.engine.run(), "active")
        self.assertEqual(self.engine.journal.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(self.kernel.address_rows[0]["addr_info"]), 2)
        rule, = self.kernel.policy_rules
        self.assertEqual((rule["src"], rule["dst"], rule["dstlen"]), (str(CIDR.ip), "192.168.55.0", 24))
        first = json.loads((self.directory / "status.json").read_text())["observed_monotonic"]
        self.assertEqual(self.engine.run(), "active")
        self.assertGreater(json.loads((self.directory / "status.json").read_text())["observed_monotonic"], first)
        self.assertEqual(len(self.kernel.operations), 3)
        self.probe_address.assert_called_once()
        self.announce_address.assert_called_once()
        self.assertEqual(self.engine.run(stop=True), "waiting")
        self.assertEqual(self.kernel.address_rows[0]["addr_info"], [native])
        self.assertEqual(self.kernel.policy_routes, [])
        self.assertEqual(self.kernel.policy_rules, [])
        self.assertFalse(self.engine.journal.exists())

    def test_native_same_address_including_host_prefix(self):
        for prefix in (24, 32):
            with self.subTest(prefix=prefix):
                address = {"local": str(CIDR.ip), "prefixlen": prefix, "label": "eth0"}
                self.kernel.address_rows[0]["addr_info"] = [address]
                self.assertEqual(self.engine.run(), "native")
                self.assertEqual(self.kernel.address_rows[0]["addr_info"], [address])
                self.assertEqual(len(self.kernel.policy_routes), 1)
                self.engine.run(stop=True)
                self.assertEqual(self.kernel.address_rows[0]["addr_info"], [address])
        self.probe_address.assert_not_called()
        self.assertFalse(any(op[0] == "addr" for op in self.kernel.operations))

    def test_native_address_arrives_after_alias(self):
        self.assertEqual(self.engine.run(), "active")
        native = {"local": str(CIDR.ip), "prefixlen": 24, "label": "eth0"}
        self.kernel.address_rows[0]["addr_info"].append(native)
        change = self.kernel.change
        def change_and_remove_prefsrc(kind, operation, claim):
            change(kind, operation, claim)
            if (kind, operation) == ("addr", "del"):
                self.kernel.policy_routes.clear()
        with patch.object(self.kernel, "change", side_effect=change_and_remove_prefsrc):
            self.assertEqual(self.engine.run(), "native")
        self.assertEqual(self.kernel.address_rows[0]["addr_info"], [native])
        self.assertEqual(len(self.kernel.policy_rules), 1)
        self.assertEqual(len(self.kernel.policy_routes), 1)
        self.get_config.return_value = (None, "auto")
        self.assertEqual(self.engine.run(), "disabled")
        self.assertEqual(self.kernel.address_rows[0]["addr_info"], [native])

    def test_move_between_physical_and_owned_bridge_then_reprobe_on_carrier_flap(self):
        self.assertEqual(self.engine.run(), "active")
        self.kernel.link.update(interface="br-rpi", ifindex=4)
        self.assertEqual(self.engine.run(), "active")
        self.assertEqual(self.kernel.address_rows[0]["addr_info"], [])
        self.assertEqual(len(self.kernel.address_rows[1]["addr_info"]), 1)
        self.kernel.link["carrier_changes"] += 2
        self.assertEqual(self.engine.run(), "active")
        self.assertEqual(self.probe_address.call_count, 3)
        self.kernel.link = LINK.copy()
        self.assertEqual(self.engine.run(), "active")
        self.assertEqual(self.kernel.address_rows[1]["addr_info"], [])
        self.kernel.link = None
        self.assertEqual(self.engine.run(), "waiting")
        self.assertEqual(self.kernel.address_rows[0]["addr_info"], [])

    def test_conflicts_do_not_adopt_policy_or_addresses(self):
        foreign = {"local": str(CIDR.ip), "prefixlen": 24, "label": "br-rpi"}
        self.kernel.address_rows[1]["addr_info"].append(foreign)
        self.assertEqual(self.engine.run(), "conflict")
        self.assertEqual(self.kernel.address_rows[1]["addr_info"], [foreign])
        self.kernel.address_rows[1]["addr_info"].clear()
        # Even an exact matching rule is external without an ownership record.
        existing = {"src": str(CIDR.ip), "dst": str(CIDR.network.network_address), "dstlen": 24,
                    "protocol": recovery.PROTOCOL, "table": recovery.TABLE, "priority": recovery.PRIORITY}
        self.kernel.policy_rules.append(existing)
        self.assertEqual(self.engine.run(), "conflict")
        self.assertEqual(self.kernel.policy_rules, [existing])
        self.kernel.policy_rules.clear()
        self.kernel.policy_routes.append({"dst": "default", "table": recovery.TABLE, "dev": "eth2"})
        self.assertEqual(self.engine.run(), "conflict")
        self.assertEqual(self.kernel.operations, [])
        self.probe_address.assert_not_called()

    def test_duplicate_probe_throttled_and_races_rechecked(self):
        self.probe_address.return_value = False
        self.assertEqual(self.engine.run(), "conflict")
        self.assertEqual(self.engine.run(), "conflict")
        self.assertEqual(self.probe_address.call_count, 1)
        self.assertEqual(self.kernel.operations, [])
        self.kernel.link["carrier_changes"] += 2
        def lease_arrived(*args):
            self.kernel.address_rows[0]["addr_info"].append({"local": str(CIDR.ip), "prefixlen": 32, "label": "eth0"})
            return True
        self.probe_address.side_effect = lease_arrived
        self.assertEqual(self.engine.run(), "native")
        self.assertFalse(any(op[0] == "addr" for op in self.kernel.operations))

    def test_recover_after_partial_configuration_or_external_flush(self):
        self.kernel.fail_once = ("rule", "add")
        with patch("sys.stderr"):
            self.assertEqual(self.engine.run(), "error")
        self.assertEqual(len(self.kernel.policy_routes), 1)
        self.assertEqual(len(self.kernel.policy_rules), 0)
        self.assertEqual(self.engine.run(), "active")
        self.assertEqual(len(self.kernel.policy_routes), 1)
        self.assertEqual(len(self.kernel.policy_rules), 1)
        self.kernel.address_rows[0]["addr_info"].clear()
        self.assertEqual(self.engine.run(), "active")
        self.assertEqual(self.probe_address.call_count, 2)
        self.assertEqual(self.announce_address.call_count, 2)

    def test_reserved_policy_taken_during_probe_preserved(self):
        def new_policy(*args):
            self.kernel.policy_rules.append({"src": "all", "table": recovery.TABLE, "priority": 333})
            return True
        self.probe_address.side_effect = new_policy
        self.assertEqual(self.engine.run(), "conflict")
        self.assertEqual(self.kernel.operations, [])
        self.assertEqual(len(self.kernel.policy_rules), 1)

    def test_link_changes_while_reprobing_external_flush(self):
        self.assertEqual(self.engine.run(), "active")
        self.kernel.address_rows[0]["addr_info"].clear()
        def flap(*args):
            self.kernel.link["carrier_changes"] += 2
            return True
        self.probe_address.side_effect = flap
        self.assertEqual(self.engine.run(), "waiting")
        self.assertFalse(self.engine.journal.exists())
        self.assertEqual(self.kernel.policy_routes, [])
        self.assertEqual(self.kernel.policy_rules, [])

    def test_select_physical_port_and_verify_bridge_ownership(self):
        sysfs = self.directory / "net"
        sysfs.mkdir()
        for name, index in (("eth0", 1), ("end0", 2), ("wlan0", 3), ("br-rpi", 4)):
            link = sysfs / name
            link.mkdir()
            for filename, value in (("type", "1"), ("flags", "0x1003"), ("carrier", "1"), ("ifindex", str(index)),
                                    ("address", "02:00:00:00:00:01"), ("carrier_changes", "3")):
                (link / filename).write_text(value)
            (link / ("bridge" if name == "br-rpi" else "device")).mkdir()
        (sysfs / "wlan0/wireless").mkdir()
        system = recovery.SystemNetwork(sysfs)
        self.assertEqual(system.target("auto", None)["interface"], "eth0")
        self.assertEqual(system.target("end0", None)["interface"], "end0")
        with self.assertRaises(recovery.Unavailable):
            system.target("wlan0", None)
        (sysfs / "eth0/master").symlink_to(sysfs / "br-rpi")
        with patch.object(recovery, "_bridge_owned", return_value=False):
            with self.assertRaises(recovery.Unavailable):
                system.target("auto", None)
        with patch.object(recovery, "_bridge_owned", return_value=True):
            identity = system.target("auto", None)
            self.assertEqual(identity["interface"], "br-rpi")
        with patch.object(recovery, "_bridge_owned", side_effect=RuntimeError()):
            self.assertEqual(system.target("auto", {"link": identity}), identity)
            with self.assertRaises(recovery.Unavailable):
                system.target("auto", None)
