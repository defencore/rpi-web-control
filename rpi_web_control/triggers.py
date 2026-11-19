"""Validated recording rules and their independent I/O workers."""

import logging
import re
import threading
from pathlib import PurePosixPath

from .trigger_inputs import INPUTS

LOG = logging.getLogger(__name__)
FIELDS = {
    "timer": {"interval_s", "duration_s"},
    "gpio": {"pin", "contact", "debounce_ms"},
    "sensor": {"path", "comparison", "threshold", "hysteresis", "poll_ms"},
    "uart": {"device", "baud", "start_command", "stop_command"},
}
UART_DEVICE = re.compile(
    r"/dev/(?:(?:tty(?:AMA|S|USB|ACM)|serial)[0-9]+|serial/by-id/[A-Za-z0-9._:+-]+)"
)
SENSOR_ROOTS = tuple(map(PurePosixPath, (
    "/sys/class/thermal", "/sys/class/hwmon", "/sys/bus/iio/devices",
    "/sys/bus/w1/devices", "/sys/bus/i2c/devices",
)))


def _number(value, field, low, high, integer=False):
    if type(value) not in ((int,) if integer else (int, float)) or not low <= value <= high:
        raise ValueError(f"{field} must be a number between {low} and {high}.")
    return value


def _choice(value, field, options):
    if value not in options:
        raise ValueError(f"{field} must be one of {', '.join(map(str, options))}.")
    return value


def _rule_fields(rule):
    kind = rule["type"]
    if kind == "timer":
        interval = _number(rule.get("interval_s", 60), "interval_s", 0.1, 31536000)
        duration = _number(rule.get("duration_s", 5), "duration_s", 0.1, 31536000)
        if duration >= interval:
            raise ValueError("duration_s must be shorter than interval_s.")
        return {"interval_s": interval, "duration_s": duration}
    if kind == "gpio":
        return {
            "pin": _number(rule.get("pin"), "pin", 0, 27, integer=True),
            "contact": _choice(rule.get("contact", "NO"), "contact", ("NO", "NC")),
            "debounce_ms": _number(rule.get("debounce_ms", 30), "debounce_ms", 0, 1000, True),
        }
    if kind == "sensor":
        raw = rule.get("path")
        if not isinstance(raw, str) or not raw.isascii() or not raw.isprintable() or len(raw) > 512:
            raise ValueError("path must be an absolute sysfs sensor attribute.")
        path = PurePosixPath(raw)
        if ".." in path.parts or not any(root in path.parents for root in SENSOR_ROOTS):
            raise ValueError("Choose a thermal, hwmon, IIO, 1-Wire or I2C sysfs sensor attribute.")
        return {
            "path": str(path),
            "comparison": _choice(rule.get("comparison", "above"), "comparison", ("above", "below")),
            "threshold": _number(rule.get("threshold"), "threshold", -1e300, 1e300),
            "hysteresis": _number(rule.get("hysteresis", 0), "hysteresis", 0, 1e300),
            "poll_ms": _number(rule.get("poll_ms", 250), "poll_ms", 20, 60000, True),
        }
    device = rule.get("device", "/dev/serial0")
    if not isinstance(device, str) or len(device) > 256 or not UART_DEVICE.fullmatch(device):
        raise ValueError("device must name a UART or a USB serial port under /dev.")
    if device.rsplit("/", 1)[-1] in (".", ".."):
        raise ValueError("device must name a serial port.")
    baud = _choice(rule.get("baud", 115200), "baud",
                   (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600))
    if type(baud) is not int:
        raise ValueError("baud must be an integer.")
    start, stop = rule.get("start_command", "START"), rule.get("stop_command", "STOP")
    for command in (start, stop):
        if not isinstance(command, str) or not re.fullmatch(r"[ -~]{1,64}", command):
            raise ValueError("UART commands must contain 1 to 64 printable ASCII characters.")
    if start == stop:
        raise ValueError("Start and stop commands must differ.")
    return {"device": device, "baud": baud, "start_command": start, "stop_command": stop}


def validate_rules(rules):
    """Normalize a complete rule list without opening hardware or starting workers."""
    if not isinstance(rules, list) or len(rules) > 32:
        raise ValueError("Provide a list of up to 32 trigger rules.")
    normalized = []
    ids = set()
    resources = set()
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("Each trigger rule must be an object.")
        rule_id = rule.get("id")
        if not isinstance(rule_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", rule_id):
            raise ValueError("Trigger IDs require 1 to 48 letters, digits, underscores or hyphens.")
        if rule_id in ids:
            raise ValueError(f"Duplicate trigger ID: {rule_id}.")
        ids.add(rule_id)
        kind = rule.get("type")
        if not isinstance(kind, str) or kind not in FIELDS:
            raise ValueError(f"Unknown trigger type for {rule_id}.")
        enabled = rule.get("enabled", True)
        if type(enabled) is not bool:
            raise ValueError("enabled must be true or false.")
        if rule.keys() - {"id", "type", "enabled"} - FIELDS[kind]:
            raise ValueError(f"Unexpected field for trigger {rule_id}.")
        fields = _rule_fields(rule)
        resource_key = {"gpio": "pin", "uart": "device"}.get(kind)
        if enabled and resource_key:
            resource = (kind, fields[resource_key])
            if resource in resources:
                raise ValueError(f"Enabled rules cannot share a {kind} input.")
            resources.add(resource)
        normalized.append({"id": rule_id, "type": kind, "enabled": enabled, **fields})
    return normalized


class _Worker:
    def __init__(self, rule, callback):
        self.rule = rule
        self.callback = callback
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.active = False
        self.state = "starting" if rule["enabled"] else "disabled"
        self.error = None
        self.thread = threading.Thread(target=self.run, name=f"trigger-{rule['id']}", daemon=True)

    def status(self):
        with self.lock:
            return {key: self.rule[key] for key in ("id", "type", "enabled")} | {
                "active": self.active, "state": self.state, "error": self.error,
            }

    def ready(self):
        with self.lock:
            self.state, self.error = "ready", None

    def publish(self, active):
        # Only the worker thread changes the claim. Driver callbacks never hold manager locks.
        if active == self.active or (active and self.stop.is_set()):
            return
        self.callback(self.rule["id"], active)
        with self.lock:
            self.active = active

    def run(self):
        previous_error = None
        while not self.stop.is_set():
            try:
                INPUTS[self.rule["type"]](self.rule, self.stop, self.ready, self.publish)
            except Exception as exc:
                message = str(exc) or type(exc).__name__
                with self.lock:
                    self.state, self.error = "error", message
                if message != previous_error:
                    LOG.warning("Trigger %s: %s", self.rule["id"], message)
                previous_error = message
            finally:
                try:
                    self.publish(False)
                except Exception as exc:
                    with self.lock:
                        self.state, self.error = "error", str(exc)
                    LOG.exception("Could not release trigger %s", self.rule["id"])
            if not self.stop.is_set():
                self.stop.wait(2)


class TriggerManager:
    def __init__(self, callback):
        self._callback = callback
        self._lifecycle = threading.Lock()
        self._lock = threading.Lock()
        self._workers = {}
        self._closed = False

    def configure(self, rules):
        rules = validate_rules(rules)
        with self._lifecycle:
            if self._closed:
                raise RuntimeError("Trigger manager has been closed.")
            self._replace(rules)

    def _replace(self, rules):
        retained = {
            rule["id"]: self._workers[rule["id"]] for rule in rules
            if rule["id"] in self._workers and self._workers[rule["id"]].rule == rule
        }
        removed = [w for key, w in self._workers.items() if key not in retained]
        for worker in removed:
            worker.stop.set()
        for worker in removed:
            if worker.thread.is_alive():
                worker.thread.join()
        current = {}
        for rule in rules:
            current[rule["id"]] = retained.get(rule["id"]) or _Worker(rule, self._callback)
        with self._lock:
            self._workers = current
        for key, worker in current.items():
            if key not in retained and worker.rule["enabled"]:
                worker.thread.start()

    def status(self):
        with self._lock:
            workers = list(self._workers.values())
        return [worker.status() for worker in workers]

    def close(self):
        with self._lifecycle:
            self._closed = True
            self._replace([])
