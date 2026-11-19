"""Read device CLOCK_REALTIME and set it through the local clock helper."""

import json
import socket
import threading
import time


CLOCK_SOCKET = "/run/rpi-web-control-clock.sock"
MESSAGE_LIMIT = 256
MAX_REQUEST_AGE_NS = 3_000_000_000


def validate_realtime_ms(value):
    # Unix milliseconds between 2000-01-01 (inclusive) and 2100-01-01 (exclusive).
    if type(value) is not int or not 946_684_800_000 <= value < 4_102_444_800_000:
        raise ValueError("Expected integer Unix milliseconds from 2000 through 2099.")


def read_message(connection):
    with connection.makefile("rb") as stream:
        line = stream.readline(MESSAGE_LIMIT + 1)
    if len(line) > MESSAGE_LIMIT or not line.endswith(b"\n"):
        raise ValueError("Incomplete or oversized clock message.")
    body = json.loads(line)
    if not isinstance(body, dict):
        raise ValueError("Expected a clock message object.")
    return body


def serve_request(connection):
    """Handle one request on the socket passed to the dedicated clock service."""
    connection.settimeout(3)
    try:
        body = read_message(connection)
        if body.keys() != {"browser_realtime_ms", "request_monotonic_ns"}:
            raise ValueError("Unexpected clock request fields.")
        value = body["browser_realtime_ms"]
        validate_realtime_ms(value)
        requested_ns = body["request_monotonic_ns"]
        if type(requested_ns) is not int or requested_ns < 0:
            raise ValueError("Expected a nonnegative integer CLOCK_MONOTONIC timestamp in nanoseconds.")
        elapsed_ns = time.monotonic_ns() - requested_ns
        if not 0 <= elapsed_ns < MAX_REQUEST_AGE_NS:
            raise ValueError("The clock sample is stale or from the future. Sync again.")
        target_ns = value * 1_000_000 + elapsed_ns
        validate_realtime_ms(target_ns // 1_000_000)
        time.clock_settime_ns(time.CLOCK_REALTIME, target_ns)
        reply = {"ok": True}
    except ValueError as exc:
        reply = {"error": str(exc)[:128]}
    except OSError:
        reply = {"error": "The device clock helper could not set the system time."}
    connection.sendall(json.dumps(reply).encode("ascii") + b"\n")


class DeviceClock:
    def __init__(self, simulated: bool):
        self.simulated = simulated
        self._offset_ns = 0
        self._lock = threading.Lock()

    def now_ns(self):
        with self._lock:
            return time.time_ns() + self._offset_ns

    def now_ms(self):
        return self.now_ns() // 1_000_000

    def set_time(self, browser_realtime_ms):
        validate_realtime_ms(browser_realtime_ms)
        if self.simulated:
            with self._lock:
                self._offset_ns = browser_realtime_ms * 1_000_000 - time.time_ns()
            return
        sample = {"browser_realtime_ms": browser_realtime_ms, "request_monotonic_ns": time.monotonic_ns()}
        request = json.dumps(sample).encode("ascii") + b"\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(5)
                connection.connect(CLOCK_SOCKET)
                connection.sendall(request)
                reply = read_message(connection)
        except (OSError, ValueError) as exc:
            raise RuntimeError("Cannot reach the device clock helper or read its reply.") from exc
        if reply.get("ok") is not True:
            raise RuntimeError(reply.get("error", "The device clock helper did not confirm the update."))
