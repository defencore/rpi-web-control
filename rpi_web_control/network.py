"""NetworkManager discovery and the appliance's restricted network control socket."""

import json
import re
import socket
import sys

from .network_profiles import NM, inventory, validate_apply, validate_interface
from .network_transaction import JOURNAL_NAME, STATE_DIR, NetworkConflict, NetworkTransaction
from .recovery_config import read_status
from .wifi_scan import scan_results

ROOT = "/org/freedesktop/NetworkManager"
SOCKET = "/run/rpi-web-control-network.sock"
TRIAL_JOURNAL = STATE_DIR / JOURNAL_NAME
MAX_REQUEST = 4096
LEGACY_FIELDS = {"interface", "mode", "ssid", "password"}


def normalize_request(settings):
    if not isinstance(settings, dict):
        raise ValueError("Expected a network configuration object.")
    if settings == {"action": "enable_wifi"}:
        return settings, False
    if settings.get("action") == "scan_wifi":
        if settings.keys() - {"action", "interface", "allow_ap_scan"}:
            raise ValueError("Unexpected Wi-Fi scan options.")
        validate_interface(settings.get("interface"))
        if type(settings.get("allow_ap_scan", False)) is not bool:
            raise ValueError("Set allow_ap_scan to true or false.")
        return settings, False
    legacy = settings.keys() == LEGACY_FIELDS
    if legacy:
        if settings["mode"] not in ("station", "ap"):
            raise ValueError("Select station or access point mode.")
        settings = {**settings, "action": "apply", "address": "", "gateway": "", "dns": [],
                    "ipv4_method": "shared" if settings["mode"] == "ap" else "auto"}
    if settings.get("action") == "apply":
        validate_apply(settings)
    elif settings.get("action") in ("confirm", "rollback"):
        if (settings.keys() != {"action", "id"} or not isinstance(settings["id"], str)
                or not re.fullmatch(r"[0-9a-f]{32}", settings["id"])):
            raise ValueError("Supply the pending network change ID.")
    else:
        raise ValueError("Select a network operation.")
    return settings, legacy


def _helper(request):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(4 if request.get("action") == "status" else 120)
            sock.connect(SOCKET)
            sock.sendall(json.dumps(request).encode() + b"\n")
            with sock.makefile("rb") as stream:
                line = stream.readline(MAX_REQUEST + 1)
                if len(line) > MAX_REQUEST or not line.endswith(b"\n"):
                    raise ValueError("Invalid network helper reply size.")
                reply = json.loads(line)
    except (OSError, ValueError) as exc:
        raise RuntimeError("The network helper is unavailable or timed out. Check the current connection.") from exc
    if not isinstance(reply, dict):
        raise RuntimeError("Invalid response from the network helper.")
    if reply.get("ok") is not True:
        error_type = {"invalid": ValueError, "conflict": NetworkConflict}.get(reply.get("kind"), RuntimeError)
        raise error_type(reply.get("error", "Network operation failed."))
    if request.get("action") == "scan_wifi":
        ticket = reply.get("scan")
        if not isinstance(ticket, dict):
            raise RuntimeError("The network helper did not return a scan request.")
        return ticket
    return reply.get("pending")


class Network:
    """Unprivileged API used by the web service."""

    def status(self):
        pending = None
        recovery = read_status()
        try:
            with _Manager() as manager:
                interfaces = inventory(manager)
                radio = manager.wifi_radio()
        except (RuntimeError, ImportError, OSError):
            return {"available": False, "interfaces": [], "pending": None, "recovery": recovery,
                    "message": "NetworkManager is unavailable."}
        # The service can stat the journal, but only the helper can read it.
        # Ask it to reconcile an interrupted change even if NM's timer has expired.
        if TRIAL_JOURNAL.is_file():
            try:
                pending = _helper({"action": "status"})
            except RuntimeError as exc:
                return {"available": False, "interfaces": interfaces, "wifi_radio": radio, "pending": None,
                        "recovery": recovery, "message": str(exc)}
        usable = any(row["physical"] and row["managed"] and row["state"] not in ("unmanaged", "unavailable")
                     for row in interfaces)
        return {"available": usable or pending is not None, "interfaces": interfaces, "wifi_radio": radio,
                "pending": pending, "recovery": recovery,
                "message": "" if usable else "No available managed Ethernet or Wi-Fi adapter."}

    def apply(self, settings):
        normalize_request(settings)
        response = _helper(settings)
        if settings.get("action") == "scan_wifi":
            with _Manager() as manager:
                return scan_results(manager, settings["interface"], response, retry=lambda: _helper(settings))
        return self.status()


class _Manager:
    def __enter__(self):
        import dbus

        self.dbus = dbus
        try:
            self.bus = dbus.SystemBus(private=True)
            self.manager = self.iface(ROOT, NM)
        except dbus.DBusException:
            if hasattr(self, "bus"):
                self.bus.close()
            raise RuntimeError("NetworkManager is unavailable.") from None
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.bus.close()
        if isinstance(exc, self.dbus.DBusException):
            # Some D-Bus errors include submitted profile properties.
            raise RuntimeError("NetworkManager could not complete the request. Check the adapter and connection settings.") from None

    def iface(self, path, interface):
        return self.dbus.Interface(self.bus.get_object(NM, path), interface)

    def prop(self, path, interface, name):
        return self.iface(path, "org.freedesktop.DBus.Properties").Get(interface, name)

    def wifi_radio(self):
        return {"enabled": bool(self.prop(ROOT, NM, "WirelessEnabled")),
                "hardware_enabled": bool(self.prop(ROOT, NM, "WirelessHardwareEnabled"))}


def helper_main():
    """Handle one bounded JSON request on a systemd-accepted Unix socket."""
    try:
        line = sys.stdin.buffer.readline(MAX_REQUEST + 1)
        if len(line) > MAX_REQUEST or not line.endswith(b"\n"):
            raise ValueError("Invalid network request size.")
        try:
            request = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            raise ValueError("Invalid network request.") from None
        status_only = request == {"action": "status"}
        if not status_only:
            request, legacy = normalize_request(request)
        with _Manager() as manager:
            change = NetworkTransaction(manager)
            action = request["action"]
            field, value = "pending", None
            if action == "scan_wifi":
                field, value = "scan", change.request_scan(request["interface"],
                                                           allow_ap_scan=request.get("allow_ap_scan", False))
            elif status_only:
                value = change.status()
            elif action == "enable_wifi":
                change.enable_wifi()
            elif action == "apply":
                value = change.stage(request)
                if legacy:
                    change.confirm(value["id"])
                    value = None
            elif action == "confirm":
                change.confirm(request["id"])
            else:
                change.cancel(request["id"])
        result = {"ok": True, field: value}
    except (ValueError, NetworkConflict, RuntimeError) as exc:
        kind = "invalid" if isinstance(exc, ValueError) else "conflict" if isinstance(exc, NetworkConflict) else "failed"
        result = {"ok": False, "kind": kind, "error": str(exc)}
    except Exception:
        result = {"ok": False, "kind": "failed", "error": "The network helper could not complete the operation."}
    print(json.dumps(result), flush=True)
