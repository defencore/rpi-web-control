"""Reconcile the wired recovery address without editing or activating any network profile."""

import argparse
import fcntl
import ipaddress
import json
import secrets
import subprocess
import sys
import time
from pathlib import Path

from .firmware import atomic_text
from .network_profiles import BRIDGE, NM, OWNER
from .recovery_arp import announce_address, probe_address
from .recovery_config import RECOVERY_DIR, get_config, write_status


TABLE = 55455
PRIORITY = 15555
PROTOCOL = 222
METRIC = 55240
IP = "/usr/sbin/ip"


class Unavailable(Exception):
    def __init__(self, state, message, interface=""):
        super().__init__(message)
        self.state = state
        self.interface = interface


class IPCommandError(RuntimeError):
    """The kernel rejected an iproute2 operation."""


def _ip(*args):
    result = subprocess.run([IP, *map(str, args)], capture_output=True, text=True, timeout=5,
                            env={"LC_ALL": "C"})
    if result.returncode:
        raise IPCommandError(result.stderr.strip()[:240] or "iproute2 operation failed.")
    return result.stdout


def _bridge_owned():
    from .network import _Manager

    with _Manager() as nm:
        device = nm.manager.GetDeviceByIpIface(BRIDGE)
        active = nm.prop(device, NM + ".Device", "ActiveConnection")
        if active == "/":
            return False
        path = nm.prop(active, NM + ".Connection.Active", "Connection")
        data = nm.iface(path, NM + ".Settings.Connection").GetSettings()
        connection = data.get("connection", {})
        return (data.get("user", {}).get("data", {}).get(OWNER) == "1"
                and connection.get("type") == "bridge" and connection.get("interface-name") == BRIDGE)


class SystemNetwork:
    def __init__(self, sysfs=Path("/sys/class/net")):
        self.sysfs = Path(sysfs)

    def target(self, selector, previous):
        def physical(path):
            return ((path / "device").exists() and not (path / "wireless").exists()
                    and not (path / "phy80211").exists() and (path / "type").read_text().strip() == "1")

        devices = [p for p in self.sysfs.iterdir() if (selector == "auto" or p.name == selector) and physical(p)]
        if not devices:
            raise Unavailable("waiting", "Waiting for the selected physical Ethernet interface.")
        port = min(devices, key=lambda p: (p.name != "eth0", p.name != "end0", p.name))
        flags = int((port / "flags").read_text().strip(), 16)
        if not flags & 1 or (port / "carrier").read_text().strip() != "1":
            raise Unavailable("waiting", f"Waiting for an Ethernet link on {port.name}.", port.name)
        master = port / "master"
        target = master.resolve(strict=True) if master.is_symlink() else port
        if target != port and (target.name != BRIDGE or not (target / "bridge").is_dir()):
            raise Unavailable("waiting", f"{port.name} belongs to an administrator-managed controller.", port.name)
        identity = {
            "physical": port.name, "physical_index": int((port / "ifindex").read_text()),
            "interface": target.name, "ifindex": int((target / "ifindex").read_text()),
            "mac": (target / "address").read_text().strip(),
            "carrier_changes": int((port / "carrier_changes").read_text()),
        }
        if target != port:
            try:
                owned = _bridge_owned()
            except (OSError, ImportError, RuntimeError):
                # Continue an already verified link through a temporary D-Bus outage.
                owned = previous is not None and previous.get("link") == identity
            if not owned:
                raise Unavailable("waiting", "Waiting for an application-managed br-rpi bridge.", target.name)
        return identity

    def addresses(self):
        return json.loads(_ip("-j", "-N", "-4", "address", "show"))

    def routes(self):
        rows = json.loads(_ip("-j", "-N", "-4", "route", "show", "table", "all"))
        return [row for row in rows if str(row.get("table")) == str(TABLE)]

    def rules(self):
        rows = json.loads(_ip("-j", "-N", "-4", "rule", "show"))
        return [row for row in rows if str(row.get("priority")) == str(PRIORITY) or str(row.get("table")) == str(TABLE)]

    def change(self, kind, operation, claim):
        cidr = ipaddress.IPv4Interface(claim["cidr"])
        dev = claim["link"]["interface"]
        if kind == "addr":
            args = [f"{cidr.ip}/32", "dev", dev, "label", claim["label"]]
            if operation == "add":
                args.append("noprefixroute")
        elif kind == "route":
            args = ["table", TABLE, str(cidr.network), "dev", dev, "scope", "link", "proto", PROTOCOL,
                    "metric", METRIC, "src", str(cidr.ip)]
        else:
            args = ["priority", PRIORITY, "from", f"{cidr.ip}/32", "to", str(cidr.network),
                    "table", TABLE, "protocol", PROTOCOL]
        _ip("-4", kind, operation, *args)


def _ours(kind, row, claim):
    if not claim[kind]:
        return False
    cidr = ipaddress.IPv4Interface(claim["cidr"])
    if str(row.get("protocol")) != str(PROTOCOL) or str(row.get("table")) != str(TABLE):
        return False
    if kind == "rule":
        known = {"priority", "src", "srclen", "dst", "dstlen", "table", "protocol", "flags", "action"}
        source = row.get("src", "")
        destination = row.get("dst", "")
        if "/" not in source:
            source += "/" + str(row.get("srclen", 32))
        if "/" not in destination:
            destination += "/" + str(row.get("dstlen", 32))
        return (not row.keys() - known and not row.get("flags")
                and row.get("action") in (None, "to_tbl", "lookup", 1)
                and str(row.get("priority")) == str(PRIORITY)
                and source == f"{cidr.ip}/32" and destination == str(cidr.network))
    known = {"dst", "dev", "scope", "protocol", "table", "metric", "prefsrc", "flags", "type"}
    return (not row.keys() - known and row.get("type") in (None, "unicast", 1, "1")
            and row.get("dst") == str(cidr.network) and row.get("dev") == claim["link"]["interface"]
            and str(row.get("scope")) in ("link", "253") and str(row.get("metric")) == str(METRIC)
            and row.get("prefsrc") == str(cidr.ip) and set(row.get("flags", [])) <= {"linkdown"})


def _address_state(rows, claim):
    ip = str(ipaddress.IPv4Interface(claim["cidr"]).ip)
    link = claim["link"]
    owned = native = elsewhere = False
    for row in rows:
        for address in row.get("addr_info", []):
            if address.get("local") != ip:
                continue
            if row["ifname"] != link["interface"] or row["ifindex"] != link["ifindex"]:
                elsewhere = True
            elif claim["addr"] and address["prefixlen"] == 32 and address.get("label") == claim["label"]:
                owned = True
            else:
                native = True
    return owned, native, elsewhere


class Recovery:
    def __init__(self, directory=RECOVERY_DIR, system=None):
        self.directory = Path(directory)
        self.system = system or SystemNetwork()
        self.journal = self.directory / "state.json"

    def save(self, claim):
        atomic_text(self.journal, json.dumps(claim) + "\n", 0o600)

    def remove(self, claim):
        if not claim:
            return
        for kind, read in (("rule", self.system.rules), ("route", self.system.routes)):
            if claim[kind]:
                rows = read()
                owned = [row for row in rows if _ours(kind, row, claim)]
                if owned:
                    # A delete with these selectors can also match a more specific
                    # foreign entry. Leave both alone if the protocol/slot overlaps.
                    tagged = [r for r in rows if str(r.get("protocol")) == str(PROTOCOL)
                              and str(r.get("table")) == str(TABLE)
                              and (kind == "route" or str(r.get("priority")) == str(PRIORITY))]
                    if len(tagged) != 1:
                        raise RuntimeError(f"Ambiguous recovery {kind} ownership; inspect the reserved policy.")
                    self.system.change(kind, "del", claim)
                claim[kind] = False
                self.save(claim)
        if claim["addr"] and _address_state(self.system.addresses(), claim)[0]:
            self.system.change("addr", "del", claim)
        self.journal.unlink(missing_ok=True)

    def acquire(self, claim, kind):
        # Record the intent first so interruption between iproute2 and the next
        # journal write cannot leave an untracked object on the running kernel.
        claim[kind] = True
        self.save(claim)
        try:
            self.system.change(kind, "add", claim)
        except IPCommandError:
            # An EEXIST race never grants ownership of the competing object.
            claim[kind] = False
            self.save(claim)
            raise

    def missing_policy(self, claim):
        missing = []
        for kind, read in (("route", self.system.routes), ("rule", self.system.rules)):
            rows = read()
            if rows and (len(rows) != 1 or not _ours(kind, rows[0], claim)):
                self.remove(claim)
                raise Unavailable("conflict", f"The reserved recovery {kind} is already in use.")
            if not rows:
                missing.append(kind)
        return missing

    def ensure(self, cidr, selector, link, claim):
        if claim and (claim["link"] != link or claim["cidr"] != str(cidr)):
            self.remove(claim)
            claim = None
        if claim is None:
            claim = {"cidr": str(cidr), "link": link, "label": "rpiwc-" + secrets.token_hex(4),
                     "addr": False, "route": False, "rule": False, "announced": False}
        dev = link["interface"]
        owned, native, elsewhere = _address_state(self.system.addresses(), claim)
        if elsewhere:
            self.remove(claim)
            raise Unavailable("conflict", f"{cidr.ip} is already assigned to another interface.")
        missing = self.missing_policy(claim)
        if native and owned:
            self.system.change("addr", "del", claim)
            claim["addr"] = False
            self.save(claim)
            # Deleting an address can also remove routes with that preferred source.
            missing = self.missing_policy(claim)
        if not native and not owned:
            if time.monotonic() < claim.get("retry_monotonic", 0):
                raise Unavailable("conflict", f"Another device claimed {cidr.ip}. Waiting to retry.")
            if not probe_address(dev, cidr.ip, link["mac"]):
                self.remove(claim)
                claim.update(addr=False, route=False, rule=False, announced=False, retry_monotonic=time.monotonic() + 60)
                self.save(claim)
                raise Unavailable("conflict", f"Another device claimed {cidr.ip} on {dev}.")
            # A lease, cable or bridge can change while the conflict probe is running.
            try:
                current = self.system.target(selector, claim)
            except Unavailable:
                self.remove(claim)
                raise
            if current != link:
                self.remove(claim)
                raise Unavailable("waiting", "The Ethernet link changed during address probing.")
            _, native, elsewhere = _address_state(self.system.addresses(), claim)
            if elsewhere:
                self.remove(claim)
                raise Unavailable("conflict", f"{cidr.ip} appeared on another interface during probing.")
            missing = self.missing_policy(claim)
            if not native:
                claim["announced"] = False
                self.acquire(claim, "addr")
        for kind in missing:
            self.acquire(claim, kind)
        if not native and not claim["announced"]:
            announce_address(dev, cidr.ip, link["mac"])
            claim["announced"] = True
            self.save(claim)
        return "native" if native else "active"

    def run(self, stop=False):
        self.directory.mkdir(mode=0o755, parents=True, exist_ok=True)
        with (self.directory / "lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            interface = ""
            cidr = None
            try:
                claim = json.loads(self.journal.read_text()) if self.journal.exists() else None
                if stop:
                    self.remove(claim)
                    state, message = "waiting", "Wired recovery is stopped."
                else:
                    cidr, selector = get_config()
                    if cidr is None:
                        self.remove(claim)
                        state, message = "disabled", "Wired recovery is disabled."
                    else:
                        try:
                            link = self.system.target(selector, claim)
                        except Unavailable:
                            self.remove(claim)
                            raise
                        interface = link["interface"]
                        state = self.ensure(cidr, selector, link, claim)
                        message = ("Using the interface's existing address." if state == "native"
                                   else "The wired recovery address is active.")
            except Unavailable as exc:
                state, message = exc.state, str(exc)
                interface = exc.interface or interface
            except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as exc:
                state, message = "error", str(exc)[:240]
            write_status(self.directory, cidr, interface, state, message)
            if state == "error":
                print(f"Wired recovery: {message}", file=sys.stderr)
            return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("reconcile", "stop"))
    return 1 if Recovery().run(stop=parser.parse_args().action == "stop") == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
