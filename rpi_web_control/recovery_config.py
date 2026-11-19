"""Wired recovery configuration and the read-only status shared with the console."""

import ipaddress
import json
import os
import re
import time
from pathlib import Path

from .firmware import atomic_text


RECOVERY_DIR = Path("/run/rpi-web-control-recovery")
DEFAULT_ADDRESS = "192.168.55.100/24"
STATES = {"active", "native", "disabled", "waiting", "conflict", "error"}


def get_config():
    value = os.environ.get("RPI_CONTROL_RECOVERY_ADDRESS", DEFAULT_ADDRESS).strip()
    selector = os.environ.get("RPI_CONTROL_RECOVERY_INTERFACE", "auto").strip()
    if value.lower() == "off":
        return None, selector
    try:
        if "/" not in value:
            raise ValueError
        address = ipaddress.IPv4Interface(value)
        if (address.network.prefixlen not in range(1, 31)
                or address.ip in (address.network.network_address, address.network.broadcast_address)
                or address.ip.is_loopback or address.ip.is_link_local or address.ip.is_reserved
                or address.ip.is_multicast or int(address.ip) >> 24 == 0):
            raise ValueError
    except ValueError:
        raise ValueError("Set RPI_CONTROL_RECOVERY_ADDRESS to a usable IPv4 host/prefix or off.") from None
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,14}", selector):
        raise ValueError("Set RPI_CONTROL_RECOVERY_INTERFACE to auto or a physical Ethernet name.")
    return address, selector


def _status(address, interface, state, message):
    return {"address": str(address or ""), "interface": interface, "state": state, "message": message}


def write_status(directory, address, interface, state, message):
    result = _status(address, interface, state, message)
    result["observed_monotonic"] = time.monotonic()
    atomic_text(Path(directory) / "status.json", json.dumps(result) + "\n", 0o644)


def read_status(directory=RECOVERY_DIR):
    try:
        data = json.loads((directory / "status.json").read_text())
        age = time.monotonic() - data["observed_monotonic"]
        if data["state"] in STATES and 0 <= age <= 45:
            return {key: data[key] for key in ("address", "interface", "state", "message")}
    except (OSError, ValueError, TypeError, KeyError):
        pass
    try:
        address, _ = get_config()
    except ValueError as exc:
        return _status(None, "", "error", str(exc))
    if address is None:
        return _status(None, "", "disabled", "Wired recovery is disabled.")
    return _status(address, "", "waiting", "Waiting for the wired recovery service to report its status.")
