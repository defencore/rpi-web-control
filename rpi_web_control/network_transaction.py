"""Stage and confirm service-owned profiles under a NetworkManager checkpoint."""

import contextlib
import ipaddress
import json
import time
import uuid
from pathlib import Path

from .network_profiles import BRIDGE, NM, OWNER, PROPERTIES, build_profiles, inventory, validate_selection
from .wifi_scan import request_scan as request_wifi_scan

ROOT = "/org/freedesktop/NetworkManager"
SETTINGS = NM + ".Settings"
PROFILE = SETTINGS + ".Connection"
ACTIVE = NM + ".Connection.Active"
ACTIVATION_TIMEOUT = 70
CONFIRM_TIMEOUT = 120
DISCONNECT_NEW_DEVICES = 4
TO_DISK = 1
STATE_DIR = Path("/run/rpi-web-control-network")
JOURNAL_NAME = "pending.json"


class NetworkConflict(RuntimeError):
    """The pending network change has expired or belongs to another request."""


class _ActivationFailed(RuntimeError):
    """NetworkManager could not bring up a candidate connection."""


def _owned(profile):
    return profile.get("user", {}).get("data", {}).get(OWNER) == "1"


def _scope(candidates, rows, existing):
    """Include all ports of an affected owned bridge, including its former ports."""
    selected = {p["connection"]["interface-name"] for p in candidates}
    owned = [p["connection"] for p in existing if _owned(p)]
    controllers = {p["uuid"] for p in owned if p.get("interface-name") == BRIDGE and p["type"] == "bridge"}
    ports = [p for p in owned if (p.get("controller") or p.get("master")) in controllers | {BRIDGE}]
    members = {p["interface-name"] for p in ports if p.get("interface-name")}
    members.update(r["name"] for r in rows if r["controller"] == BRIDGE)
    bridge_change = BRIDGE in selected or bool(members & selected)
    retired = {p["uuid"] for p in owned if p.get("interface-name") in selected}
    if bridge_change:
        retired.update(controllers)
        retired.update(p["uuid"] for p in ports)
        selected.update(members | {BRIDGE})
    return selected, sorted(retired)


class NetworkTransaction:
    """One operation per socket process, sharing a private, secret-free transaction journal."""

    def __init__(self, manager, state_dir=STATE_DIR):
        self.nm = manager
        self.directory = Path(state_dir)
        self.journal = self.directory / JOURNAL_NAME
        self.settings = manager.iface(ROOT + "/Settings", SETTINGS)

    @contextlib.contextmanager
    def _lock(self):
        import fcntl

        self.directory.mkdir(mode=0o700, exist_ok=True)
        with (self.directory / "lock").open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise NetworkConflict("Another network operation is in progress.") from None
            yield

    def _load(self):
        try:
            return json.loads(self.journal.read_text(encoding="ascii"))
        except FileNotFoundError:
            return None

    def _save(self, trial, phase):
        trial["phase"] = phase
        temp = self.journal.with_suffix(".tmp")
        temp.write_text(json.dumps(trial), encoding="ascii")
        temp.replace(self.journal)

    def _profiles(self):
        result = {}
        for path in self.settings.ListConnections():
            handler = self.nm.iface(path, PROFILE)
            profile = handler.GetSettings()
            result[str(profile["connection"]["uuid"])] = (handler, profile)
        return result

    def _delete_owned(self, uuids):
        profiles = self._profiles()
        for uid in uuids:
            if uid in profiles:
                handler, profile = profiles[uid]
                if _owned(profile):
                    handler.Delete()

    def _live(self, trial):
        # Object paths may be reused after the NetworkManager process restarts.
        return (trial["owner"] == str(self.nm.bus.get_name_owner(NM))
                and trial["checkpoint"] in self.nm.prop(ROOT, NM, "Checkpoints"))

    def _rollback(self, trial):
        self._save(trial, "rolling_back")
        if self._live(trial):
            result = self.nm.manager.CheckpointRollback(trial["checkpoint"])
            if not set(trial["devices"]).issubset(result) or any(code != 0 for code in result.values()):
                self._delete_owned(trial["new"])
                raise RuntimeError("NetworkManager could not confirm rollback on every affected interface.")
        self._delete_owned(trial["new"])
        self.journal.unlink()

    def _ready(self, trial):
        paths = trial["active"]
        if not paths or len(paths) != len(trial["new"]):
            raise RuntimeError("The candidate network is incomplete.")
        records = []
        for active, uid in zip(paths, trial["new"]):
            properties = self.nm.iface(active, PROPERTIES).GetAll(ACTIVE)
            if properties["Uuid"] != uid:
                raise RuntimeError("The active network profile has changed.")
            state = int(properties["State"])
            if state >= 3:
                raise _ActivationFailed("Network activation failed. Check the link and network settings.")
            if state != 2:
                return False
            records.append(properties)
        main, *ports = records
        if ports:
            devices = main["Devices"]
            if len(devices) != 1 or self.nm.prop(devices[0], NM + ".Device", "Interface") != BRIDGE:
                raise RuntimeError("The active bridge device has changed.")
            # Bookworm NM 1.42 exports Master; both properties identify a device.
            if any(port.get("Controller", port.get("Master", "/")) != devices[0] for port in ports):
                raise RuntimeError("A selected port is no longer attached to the requested bridge.")
        # Only the first (standalone or controller) profile owns IPv4.
        if trial.get("ipv4_method") == "auto" and main.get("Dhcp4Config", "/") == "/":
            # An address maintained outside NM cannot establish that DHCP succeeded.
            return False
        ip = main["Ip4Config"]
        addresses = self.nm.prop(ip, NM + ".IP4Config", "AddressData") if ip != "/" else []
        requested = trial.get("requested_cidr")
        if requested:
            return any(f"{item['address']}/{int(item['prefix'])}" == requested for item in addresses)
        return bool(addresses)

    @staticmethod
    def _pending(trial):
        if not trial or trial["phase"] != "pending":
            return None
        left = trial["expires_monotonic_ns"] - time.monotonic_ns()
        if left <= 0:
            return None
        return {"id": trial["id"], "remaining_s": (left + 999_999_999) // 1_000_000_000}

    def _reconcile(self):
        trial = self._load()
        if not trial:
            return None
        live = self._live(trial)
        if trial["phase"] == "pending" and live and self._pending(trial):
            return trial
        if trial["phase"] == "sealing" and not live:
            # A confirming helper can die after Destroy succeeds but before it records success.
            # Treat it as committed only if every saved candidate is still active.
            try:
                if trial["owner"] == str(self.nm.bus.get_name_owner(NM)) and self._ready(trial):
                    self._save(trial, "committed")
            except (self.nm.dbus.DBusException, RuntimeError):
                pass
        if trial["phase"] == "committed":
            self._delete_owned(trial["old"])
            self.journal.unlink()
        else:
            self._rollback(trial)
        return None

    def status(self):
        try:
            with self._lock():
                return self._pending(self._reconcile())
        except NetworkConflict:
            # Status polling must not wait for link negotiation in another helper.
            return self._pending(self._load())

    def enable_wifi(self):
        with self._lock():
            if self._reconcile():
                raise NetworkConflict("Finish the pending network change before enabling Wi-Fi.")
            devices = inventory(self.nm)
            if not any(row["type"] == "wifi" and row["physical"] for row in devices):
                raise ValueError("NetworkManager has no physical Wi-Fi adapter.")
            radio = self.nm.wifi_radio()
            if not radio["hardware_enabled"]:
                raise ValueError("Wi-Fi is hardware-blocked. Unblock the radio on the device.")
            if not radio["enabled"]:
                self.nm.iface(ROOT, PROPERTIES).Set(NM, "WirelessEnabled", self.nm.dbus.Boolean(True))

    def request_scan(self, interface, *, allow_ap_scan=False):
        with self._lock():
            if self._reconcile():
                raise NetworkConflict("Finish the pending network change before scanning Wi-Fi.")
            return request_wifi_scan(self.nm, interface, allow_ap_scan=allow_ap_scan)

    def stage(self, settings):
        with self._lock():
            if self._reconcile():
                raise NetworkConflict("Confirm or roll back the pending network change first.")
            rows = inventory(self.nm)
            existing = [profile for _, profile in self._profiles().values()]
            validate_selection(settings, rows, existing)
            candidates = build_profiles(settings, self.nm.dbus)
            selected, retired = _scope(candidates, rows, existing)
            devices = {r["name"]: r["path"] for r in rows if r["name"] in selected}
            paths = sorted(devices.values())
            # A new software device needs NM-owned disconnection on automatic rollback.
            # The delete-new-profiles flag would also remove unrelated administrator profiles.
            flags = DISCONNECT_NEW_DEVICES if settings["mode"] == "bridge" and BRIDGE not in devices else 0
            owner = str(self.nm.bus.get_name_owner(NM))
            checkpoint = self.nm.manager.CheckpointCreate(paths, ACTIVATION_TIMEOUT + 25, flags)
            trial = {"id": uuid.uuid4().hex, "owner": owner, "checkpoint": str(checkpoint), "devices": paths,
                     "ipv4_method": settings["ipv4_method"],
                     "new": [str(p["connection"]["uuid"]) for p in candidates], "old": retired, "active": []}
            if settings.get("address"):
                trial["requested_cidr"] = str(ipaddress.IPv4Interface(settings["address"]))
            self._save(trial, "staging")
            try:
                if BRIDGE in devices:
                    active = self.nm.prop(devices[BRIDGE], NM + ".Device", "ActiveConnection")
                    if active != "/":
                        if self.nm.prop(active, ACTIVE, "Uuid") not in retired:
                            raise ValueError("The bridge is no longer owned by the application.")
                        self.nm.manager.DeactivateConnection(active)
                deadline = time.monotonic() + ACTIVATION_TIMEOUT
                for profile in candidates:
                    name = profile["connection"]["interface-name"]
                    device = "/" if name == BRIDGE else devices[name]
                    _, active, _ = self.nm.manager.AddAndActivateConnection2(
                        profile, device, "/", {"persist": "volatile"})
                    trial["active"].append(str(active))
                while not self._ready(trial):
                    if time.monotonic() >= deadline:
                        raise _ActivationFailed("Timed out waiting for network activation and IPv4.")
                    time.sleep(0.25)
                trial["expires_monotonic_ns"] = time.monotonic_ns() + CONFIRM_TIMEOUT * 1_000_000_000
                self.nm.manager.CheckpointAdjustRollbackTimeout(checkpoint, CONFIRM_TIMEOUT)
                self._save(trial, "pending")
                return self._pending(trial)
            except Exception as exc:
                self._rollback(trial)
                if settings["mode"] == "station" and isinstance(exc, (_ActivationFailed, self.nm.dbus.DBusException)):
                    raise RuntimeError("Wi-Fi connection failed. Check the network name, security, password "
                                       "and IPv4 settings, then try again.") from None
                raise

    def _requested(self, transaction_id):
        trial = self._reconcile()
        if not trial or transaction_id != trial["id"]:
            raise NetworkConflict("This network change is no longer pending.")
        return trial

    def confirm(self, transaction_id):
        with self._lock():
            trial = self._requested(transaction_id)
            try:
                self.nm.manager.CheckpointAdjustRollbackTimeout(trial["checkpoint"], 60)
                self._save(trial, "saving")
                if not self._ready(trial):
                    raise RuntimeError("The network is no longer active at the requested address.")
                profiles = self._profiles()
                # Persist ports before their controller: on a power loss the new
                # controller cannot autoconnect at boot with an incomplete set of ports.
                uids = trial["new"]
                for uid in uids[1:] + uids[:1]:
                    if uid not in profiles or not _owned(profiles[uid][1]):
                        raise RuntimeError("The candidate network profile has changed.")
                    # Empty settings preserve NM-held settings and secrets. Convert the
                    # active volatile profile to disk without round-tripping the WPA key.
                    profiles[uid][0].Update2({}, TO_DISK, {})
                self._save(trial, "sealing")
                self.nm.manager.CheckpointDestroy(trial["checkpoint"])
            except Exception:
                if trial["phase"] == "sealing" and not self._live(trial):
                    self._reconcile()
                else:
                    self._rollback(trial)
                raise
            self._save(trial, "committed")
            self._delete_owned(trial["old"])
            self.journal.unlink()

    def cancel(self, transaction_id):
        with self._lock():
            self._rollback(self._requested(transaction_id))
