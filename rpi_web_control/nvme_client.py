"""Bound the JSON protocol between the web account and the managed recording disk helper."""

import json
import socket
import subprocess


SOCKET = "/run/rpi-web-control-nvme.sock"
MOUNTPOINT = "/srv/rpi-web-control-nvme"
USB_MOUNTPOINT = "/srv/rpi-web-control-usb"
REQUEST_LIMIT = 2048
REPLY_LIMIT = 128 * 1024
_FIELDS = {
    "status": {"action"},
    "prepare": {"action", "device", "revision", "confirmation"},
    "mount": {"action", "device", "revision", "volume"},
}


def _read_message(connection, limit):
    with connection.makefile("rb") as stream:
        data = stream.readline(limit + 1)
    if len(data) > limit or not data.endswith(b"\n"):
        raise ValueError("Incomplete or oversized storage message.")
    message = json.loads(data)
    if not isinstance(message, dict):
        raise ValueError("Expected a storage message object.")
    return message


def _encode(message, limit):
    data = json.dumps(message, separators=(",", ":"), allow_nan=False).encode("ascii") + b"\n"
    if len(data) > limit:
        raise ValueError("storage message is too large.")
    return data


def _action(body):
    if not isinstance(body, dict) or not isinstance(body.get("action"), str):
        raise ValueError("Expected a storage action.")
    action = body["action"]
    if _FIELDS.get(action) != body.keys() or not all(isinstance(v, str) for v in body.values()):
        raise ValueError("Invalid storage action or fields.")
    return action


def serve_request(connection, manager_factory):
    """Read and answer a single request on an accepted Unix stream."""
    connection.settimeout(5)
    try:
        body = _read_message(connection, REQUEST_LIMIT)
        medium = body.pop("medium", "nvme")
        if medium not in ("nvme", "usb"):
            raise ValueError("Choose PCIe NVMe or USB storage.")
        action = _action(body)
        manager = manager_factory(medium="usb") if medium == "usb" else manager_factory()
        result = manager.status() if action == "status" else manager.apply(body)
        response = _encode({"result": result}, REPLY_LIMIT)
    except (ValueError, RecursionError) as exc:
        response = _encode({"kind": "invalid", "error": str(exc)[:1024]}, REPLY_LIMIT)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        response = _encode({"kind": "unavailable", "error": str(exc)[:1024]}, REPLY_LIMIT)
    connection.sendall(response)


class NvmeClient:
    def __init__(self, path=SOCKET, *, medium="nvme"):
        if medium not in ("nvme", "usb"):
            raise ValueError("Choose PCIe NVMe or USB storage.")
        self.path = path
        self.medium = medium
        self.mountpoint = USB_MOUNTPOINT if medium == "usb" else MOUNTPOINT

    def _call(self, body):
        request = _encode({**body, "medium": "usb"} if self.medium == "usb" else body, REQUEST_LIMIT)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(5)
                connection.connect(self.path)
                connection.sendall(request)
                connection.settimeout(35 if body["action"] == "status" else 550)
                reply = _read_message(connection, REPLY_LIMIT)
        except (OSError, ValueError, RecursionError) as exc:
            raise RuntimeError("Cannot reach the recording disk helper or its response was incomplete. "
                               "Refresh the drive state before retrying.") from exc
        if isinstance(reply.get("result"), dict):
            return reply["result"]
        error = ValueError if reply.get("kind") == "invalid" else RuntimeError
        raise error(reply.get("error", "Storage operation failed."))

    def status(self):
        try:
            return self._call({"action": "status"})
        except (ValueError, RuntimeError) as exc:
            return {"available": False, "error": str(exc), "devices": [], "ready": False, "mountpoint": self.mountpoint,
                    "directory": self.mountpoint + "/recordings"}

    def apply(self, body):
        if _action(body) == "status":
            raise ValueError("Select a mount or prepare operation.")
        return self._call(body)
