"""Simulate Ethernet, Wi-Fi and bridge changes without touching host interfaces."""

from copy import deepcopy
from ipaddress import IPv4Interface
import json
import math
from pathlib import Path
from threading import Lock
from time import monotonic
import uuid

from .firmware import atomic_text
from .network import normalize_request
from .network_profiles import BRIDGE
from .network_transaction import CONFIRM_TIMEOUT, NetworkConflict
from .recovery_config import get_config


def _interface(name, kind, number):
    return {
        "name": name, "type": kind, "path": f"/org/freedesktop/NetworkManager/Devices/{number}",
        "managed": True, "physical": kind != "bridge", "ap": kind == "wifi",
        "mode": "disconnected", "state": "disconnected", "ssid": "", "addresses": [],
        "gateway": "", "dns": [], "dns_override": [], "controller": "", "ports": [], "active_uuid": "",
        "ipv4_method": "", "configured_primary": "",
    }


def _activate(row, mode, *, controller="", ssid=""):
    row.update(mode=mode, state="connected", controller=controller, ssid=ssid,
               active_uuid=str(uuid.uuid4()), addresses=[], gateway="", dns=[], dns_override=[],
               ipv4_method="", configured_primary="")


def _set_ipv4(row, settings):
    method = settings["ipv4_method"]
    address, gateway, dns = settings["address"], settings["gateway"], list(settings["dns"])
    if method == "auto":
        # Documentation networks distinguish a lease on the LAN from a station lease.
        subnet = "198.51.100" if row["type"] == "wifi" else "192.0.2"
        address, gateway = subnet + ".10/24", subnet + ".1"
        dns = dns or [gateway]
    elif method == "shared" and not address:
        address = "10.42.0.1/24"
    row.update(ipv4_method=method, configured_primary=settings["address"], addresses=[address],
               gateway=gateway, dns=dns, dns_override=list(settings["dns"]) if method == "auto" else [])


def _recovery(rows):
    try:
        address, selector = get_config()
    except ValueError as error:
        return {"address": "", "interface": "", "state": "error", "message": str(error)}
    if address is None:
        return {"address": "", "interface": "", "state": "disabled", "message": "Wired recovery is disabled."}

    result = {"address": str(address), "interface": ""}
    if selector not in ("auto", "eth0"):
        return {**result, "state": "waiting", "message": "The selected Ethernet interface is unavailable."}
    target = rows["eth0"]["controller"] or "eth0"
    result["interface"] = target
    for name, row in rows.items():
        if name != target and any(IPv4Interface(cidr).ip == address.ip for cidr in row["addresses"]):
            return {**result, "state": "conflict", "message": f"{name} already uses this IPv4 address."}
    effective = rows[target]["addresses"]
    if any(IPv4Interface(cidr).ip == address.ip for cidr in effective):
        return {**result, "state": "native", "message": "The network profile already provides this address."}
    effective.append(f"{address.ip}/32")
    return {**result, "state": "active", "message": "The simulated wired recovery address is active."}


class SimulatedNetwork:
    def __init__(self, state_dir, *, wifi=True, dhcp=True, wifi_enabled=True, wifi_hardware_enabled=True):
        self._path = Path(state_dir) / "network-sim.json"
        self._lock = Lock()
        self._dhcp = dhcp
        self._wifi_enabled = wifi_enabled
        self._wifi_hardware_enabled = wifi_hardware_enabled
        wired = _interface("eth0", "ethernet", 1)
        _activate(wired, "ethernet")
        _set_ipv4(wired, {"ipv4_method": "auto", "address": "", "gateway": "", "dns": []})
        self._defaults = {"eth0": wired}
        if wifi:
            self._defaults["wlan0"] = _interface("wlan0", "wifi", 2)
        self._confirmed = deepcopy(self._defaults)
        try:
            saved = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        else:
            # A saved bridge requires both ports to be present in this fixture.
            if wifi or BRIDGE not in saved:
                for row in saved.values():
                    # Older fixtures saved only effective DNS. Each simulated DHCP
                    # lease supplies its gateway as the sole DNS server by default.
                    if "dns_override" not in row:
                        custom = row["ipv4_method"] == "auto" and row["dns"] != [row["gateway"]]
                        row["dns_override"] = list(row["dns"]) if custom else []
                    if "configured_primary" not in row:
                        primary = row["addresses"][0] if row["addresses"] else ""
                        method = row["ipv4_method"]
                        if method not in ("manual", "shared") or (
                                method == "shared" and row["mode"] == "ap" and primary == "10.42.0.1/24"):
                            primary = ""
                        row["configured_primary"] = primary
                self._confirmed.update({name: row for name, row in saved.items()
                                        if name in self._defaults or name == BRIDGE})
        self._candidate = None
        self._candidate_requires_dhcp = False
        self._id = ""
        self._deadline = 0.0

    def _expire(self, now):
        if self._candidate is not None and now >= self._deadline:
            self._candidate = None

    def _status(self, now):
        self._expire(now)
        pending = None
        rows = self._confirmed
        if self._candidate is not None:
            rows = self._candidate
            pending = {"id": self._id, "remaining_s": math.ceil(self._deadline - now)}
        effective = deepcopy(rows)
        if not self._dhcp:
            for row in effective.values():
                if row["ipv4_method"] == "auto":
                    row.update(state="connecting", addresses=[], gateway="", dns=[])
        if not self._wifi_enabled or not self._wifi_hardware_enabled:
            for row in effective.values():
                if row["type"] == "wifi":
                    row.update(state="unavailable", mode="disconnected", addresses=[], ssid="",
                               gateway="", dns=[], controller="", active_uuid="", ipv4_method="", configured_primary="")
        recovery = _recovery(effective)
        return {"available": True, "simulated": True, "message": "", "pending": pending,
                "interfaces": list(effective.values()), "recovery": recovery,
                "wifi_radio": {"enabled": self._wifi_enabled, "hardware_enabled": self._wifi_hardware_enabled}}

    def status(self):
        with self._lock:
            return self._status(monotonic())

    def _stage(self, settings):
        mode, name = settings["mode"], settings["interface"]
        kind = "ethernet" if mode in ("ethernet", "bridge") else "wifi"
        if self._defaults.get(name, {}).get("type") != kind:
            raise ValueError("The selected network interface is unavailable.")
        if mode == "bridge" and self._defaults.get(settings["wifi_interface"], {}).get("type") != "wifi":
            raise ValueError("The selected Wi-Fi interface is unavailable.")
        if mode != "ethernet" and not (self._wifi_enabled and self._wifi_hardware_enabled):
            raise ValueError("Enable the Wi-Fi radio before applying wireless settings.")
        # Releasing a managed bridge restores default profiles on its unselected ports.
        rows = deepcopy(self._defaults if BRIDGE in self._confirmed else self._confirmed)
        if mode == "bridge":
            wireless = settings["wifi_interface"]
            _activate(rows[name], "ethernet", controller=BRIDGE)
            _activate(rows[wireless], "ap", controller=BRIDGE, ssid=settings["ssid"])
            name = BRIDGE
            rows[name] = _interface(BRIDGE, "bridge", 3)
            rows[name]["ports"] = [settings["interface"], wireless]
        _activate(rows[name], mode, ssid=settings["ssid"] if mode in ("ap", "station") else "")
        _set_ipv4(rows[name], settings)
        return rows

    def _save(self, rows):
        # Interface rows contain the public inventory, including the SSID, and no PSK.
        atomic_text(self._path, json.dumps(rows) + "\n", 0o600)
        self._confirmed = rows

    def apply(self, settings):
        request, legacy = normalize_request(settings)
        with self._lock:
            now = monotonic()
            self._expire(now)
            action = request["action"]
            if action == "enable_wifi":
                if self._candidate is not None:
                    raise NetworkConflict("Finish the pending network change before enabling Wi-Fi.")
                if not any(row["type"] == "wifi" for row in self._defaults.values()):
                    raise ValueError("NetworkManager has no physical Wi-Fi adapter.")
                if not self._wifi_hardware_enabled:
                    raise ValueError("Wi-Fi is hardware-blocked. Unblock the radio on the device.")
                self._wifi_enabled = True
            elif action == "apply":
                if self._candidate is not None:
                    raise NetworkConflict("Confirm or roll back the pending network change first.")
                rows = self._stage(request)
                requires_dhcp = request["ipv4_method"] == "auto"
                if requires_dhcp and not self._dhcp:
                    raise RuntimeError("No DHCP lease is available for the requested network profile.")
                if legacy:
                    self._save(rows)
                else:
                    self._candidate = rows
                    self._candidate_requires_dhcp = requires_dhcp
                    self._id = uuid.uuid4().hex
                    self._deadline = now + CONFIRM_TIMEOUT
            else:
                if self._candidate is None or self._id != request["id"]:
                    raise NetworkConflict("The pending network change is no longer available.")
                if action == "confirm":
                    if self._candidate_requires_dhcp and not self._dhcp:
                        self._candidate = None
                        raise RuntimeError("The DHCP lease was lost. The previous network configuration was restored.")
                    self._save(self._candidate)
                self._candidate = None
            return self._status(monotonic())
