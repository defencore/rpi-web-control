"""Hardware and timer adapters for level-based recording claims."""

from contextlib import ExitStack, contextmanager
import math
from pathlib import Path
import select
import threading
import time


def timer(rule, stop, ready, publish):
    interval = rule["interval_s"]
    start = time.monotonic() + interval
    ready()
    while not stop.wait(max(0, start - time.monotonic())):
        end = start + rule["duration_s"]
        if time.monotonic() < end:
            publish(True)
            stop.wait(max(0, end - time.monotonic()))
            publish(False)
        # A slow callback or suspended process must not replay missed windows.
        start += interval * max(1, 1 + int((time.monotonic() - start) // interval))


def _sensor_value(path):
    with open(path, "r", encoding="ascii") as data:
        text = data.read(129)
    if len(text) > 128:
        raise ValueError("Sensor attribute must contain a single numeric value.")
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("Sensor returned a non-finite value.")
    return value


def sensor(rule, stop, ready, publish):
    active = False
    sign = 1 if rule["comparison"] == "above" else -1
    limit = rule["threshold"] * sign
    while not stop.is_set():
        value = _sensor_value(rule["path"]) * sign
        active = value >= (limit - rule["hysteresis"] if active else limit)
        ready()
        publish(active)
        stop.wait(rule["poll_ms"] / 1000)


def _uart_lines(rule, stopping, ready, on_line):
    import serial

    pending = bytearray()
    overflow = False
    with serial.Serial(rule["device"], rule["baud"], timeout=0.25, exclusive=True) as port:
        ready()
        while not stopping():
            for byte in port.read(min(1024, port.in_waiting or 1)):
                if byte == 10:
                    if not overflow:
                        on_line(pending.removesuffix(b"\r"))
                    pending.clear()
                    overflow = False
                elif len(pending) < 65:
                    pending.append(byte)
                else:
                    overflow = True


def uart(rule, stop, ready, publish):
    start, end = (rule[key].encode("ascii") for key in ("start_command", "stop_command"))

    def on_line(line):
        if line == start:
            publish(True)
        elif line == end:
            publish(False)

    _uart_lines(rule, stop.is_set, ready, on_line)


@contextmanager
def _rp1_chip(gpiod):
    errors = []
    for node in sorted(Path("/dev").glob("gpiochip[0-9]*")):
        try:
            chip = gpiod.Chip(str(node))
        except OSError as exc:
            errors.append(str(exc))
            continue
        try:
            label = chip.get_info().label if hasattr(chip, "get_info") else chip.label()
            if label == "pinctrl-rp1":
                yield chip
                return
        finally:
            chip.close()
    detail = f" ({', '.join(errors)})" if errors else ""
    raise RuntimeError(f"RP1 GPIO controller unavailable{detail}.")


@contextmanager
def _gpio_edges(pin):
    import gpiod

    with _rp1_chip(gpiod) as chip, ExitStack() as cleanup:
        if hasattr(gpiod, "LineSettings"):
            from gpiod.line import Bias, Direction, Edge, Value

            settings = gpiod.LineSettings(direction=Direction.INPUT, edge_detection=Edge.BOTH,
                                          bias=Bias.PULL_UP)
            request = chip.request_lines(config={pin: settings}, consumer="rpi-web-control")
            cleanup.callback(request.release)
            high = request.get_value(pin) == Value.ACTIVE

            def read(timeout):
                if not request.wait_edge_events(timeout):
                    return []
                return [(event.timestamp_ns, event.event_type == event.Type.RISING_EDGE)
                        for event in request.read_edge_events()]
        else:
            line = chip.get_line(pin)
            line.request(consumer="rpi-web-control", type=gpiod.LINE_REQ_EV_BOTH_EDGES,
                         flags=gpiod.LINE_REQ_FLAG_BIAS_PULL_UP)
            cleanup.callback(line.release)
            high = bool(line.get_value())

            def read(timeout):
                if not select.select([line.event_get_fd()], [], [], timeout)[0]:
                    return []
                event = line.event_read()
                return [(event.sec * 10**9 + event.nsec, event.type == gpiod.LineEvent.RISING_EDGE)]
        yield high, read


def _gpio_levels(rule, stopping, ready, publish):
    debounce = rule["debounce_ms"] * 1_000_000
    active_high = rule["contact"] == "NC"
    with _gpio_edges(rule["pin"]) as (high, read):
        ready()
        # Qualify the initial contact state and subsequent edges on the same monotonic clock.
        deadline = time.monotonic_ns() + debounce
        while not stopping():
            timeout = 0.25 if deadline is None else min(0.25, max(0, deadline - time.monotonic_ns()) / 1e9)
            events = read(timeout)
            for timestamp, level in events:
                if deadline is not None and timestamp >= deadline:
                    publish(high == active_high)
                high = level
                deadline = timestamp + debounce
            # Drain queued edges before testing against the current time. Each edge
            # time matters when several transitions happened during a slow callback.
            if not events and deadline is not None and time.monotonic_ns() >= deadline:
                publish(high == active_high)
                deadline = None


def gpio(rule, stop, ready, publish):
    if "stop" not in rule:
        _gpio_levels(rule, stop.is_set, ready, publish)
        return

    # The two independent inputs share one rule ID and one recording claim.
    # Serialize their updates and quiesce both readers on any I/O failure.
    stop_rule = rule["stop"]
    finished = threading.Event()
    lock = threading.Lock()
    start_active = stop_active = None
    stop_error = None

    def stopping():
        return finished.is_set() or stop.is_set()

    def start_contact(active):
        nonlocal start_active
        with lock:
            previous, start_active = start_active, active
            if previous is None and stop_active is not None:
                ready()
            if previous is False and active and stop_active is False and not stopping():
                publish(True)
            elif previous is True and not active and rule.get("stop_on_return", False):
                publish(False)

    def stop_contact(active):
        nonlocal stop_active
        with lock:
            previous, stop_active = stop_active, active
            if previous is None and start_active is not None:
                ready()
            if active:
                publish(False)

    def read_stop():
        nonlocal stop_error
        try:
            if stop_rule["type"] == "gpio":
                _gpio_levels(stop_rule, stopping, lambda: None, stop_contact)
            else:
                command = stop_rule["command"].encode("ascii")

                def on_line(line):
                    if line == command:
                        with lock:
                            publish(False)

                _uart_lines(stop_rule, stopping, lambda: stop_contact(False), on_line)
        except Exception as exc:
            stop_error = exc
        finally:
            finished.set()

    thread = threading.Thread(target=read_stop, name=f"trigger-stop-{rule['id']}", daemon=True)
    thread.start()
    try:
        _gpio_levels(rule, stopping, lambda: None, start_contact)
    finally:
        finished.set()
        thread.join()
    if stop_error is not None:
        raise stop_error


INPUTS = {"timer": timer, "sensor": sensor, "uart": uart, "gpio": gpio}
