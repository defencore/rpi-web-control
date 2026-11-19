"""Hardware and timer adapters for level-based recording claims."""

from contextlib import ExitStack, contextmanager
import math
from pathlib import Path
import select
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


def uart(rule, stop, ready, publish):
    import serial

    start, end = (rule[key].encode("ascii") for key in ("start_command", "stop_command"))
    pending = bytearray()
    overflow = False
    with serial.Serial(rule["device"], rule["baud"], timeout=0.25, exclusive=True) as port:
        ready()
        while not stop.is_set():
            for byte in port.read(min(1024, port.in_waiting or 1)):
                if byte == 10:
                    if not overflow:
                        line = pending.removesuffix(b"\r")
                        if line == start:
                            publish(True)
                        elif line == end:
                            publish(False)
                    pending.clear()
                    overflow = False
                elif len(pending) < 65:
                    pending.append(byte)
                else:
                    overflow = True


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


def gpio(rule, stop, ready, publish):
    debounce = rule["debounce_ms"] * 1_000_000
    active_high = rule["contact"] == "NC"
    with _gpio_edges(rule["pin"]) as (high, read):
        ready()
        # Qualify the initial contact state and subsequent edges on the same monotonic clock.
        deadline = time.monotonic_ns() + debounce
        while not stop.is_set():
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


INPUTS = {"timer": timer, "sensor": sensor, "uart": uart, "gpio": gpio}
