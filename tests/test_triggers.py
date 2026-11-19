import contextlib
import queue
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rpi_web_control import trigger_inputs as inputs
from rpi_web_control.triggers import TriggerManager, validate_rules


class ValidationTests(unittest.TestCase):
    def test_normalized_rules_round_trip(self):
        rules = [
            {"id": "interval", "type": "timer"},
            {"id": "switch", "type": "gpio", "pin": 17},
            {"id": "sensor", "type": "sensor", "threshold": 42, "path": "/sys/class/hwmon/hwmon0/in1_input"},
            {"id": "serial", "type": "uart"},
        ]
        normalized = validate_rules(rules)
        self.assertEqual(normalized, validate_rules(normalized))
        self.assertEqual(normalized[1]["contact"], "NO")
        self.assertNotIn("contact", rules[1])

    def test_error_cases(self):
        examples = (
            {"type": "sensor", "threshold": 5, "path": "/sys/class/hwmon/../../etc/shadow"},
            {"type": "sensor", "threshold": 5, "path": "/dev/random"},
            {"type": "sensor", "threshold": float("nan"), "path": "/sys/class/thermal/t/temp"},
            {"type": "gpio", "pin": True}, {"type": "gpio", "pin": 28},
            {"type": "gpio", "pin": 4, "contact": "open"},
            {"type": "timer", "interval_s": 0.2, "duration_s": 0.2},
            {"type": "timer", "duration_s": float("inf")},
            {"type": "uart", "device": "/dev/sda"}, {"type": "uart", "baud": 42},
            {"type": "uart", "start_command": "STOP"}, {"type": "uart", "start_command": "X\n"},
            {"type": "uart", "device": "/dev/serial/by-id/.."}, {"type": "timer", "enabled": 1},
            {"type": "timer", "typo": 45}, {"type": []},
        )
        for rule in examples:
            with self.subTest(rule=rule), self.assertRaises(ValueError):
                validate_rules([{"id": "test", **rule}])
        with self.assertRaises(ValueError):
            validate_rules([{"id": "same", "type": "timer"}] * 2)

    def test_exclusive_inputs(self):
        a = {"id": "gate", "type": "gpio", "pin": 17}
        b = dict(a, id="gate2")
        with self.assertRaises(ValueError):
            validate_rules([a, b])
        validate_rules([a, dict(b, enabled=False)])

    def test_toggle_button_configuration(self):
        base = {"id": "button", "type": "gpio", "pin": 17}
        self.assertEqual(validate_rules([base]), validate_rules([dict(base, toggle_on_press=False)]))
        rule, = validate_rules([dict(base, toggle_on_press=True)])
        self.assertTrue(rule["toggle_on_press"])
        self.assertEqual([rule], validate_rules([rule]))
        for extra in ({"toggle_on_press": 1}, {"toggle_on_press": "yes"},
                      {"toggle_on_press": True, "stop": {"type": "gpio", "pin": 27}},
                      {"toggle_on_press": True, "stop_on_return": True}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                validate_rules([{**base, **extra}])

    def test_stop_inputs_normalize_and_reserve_their_resources(self):
        base = {"id": "gate", "type": "gpio", "pin": 17}
        for stop, expected in (
            ({"type": "gpio", "pin": 27}, {"type": "gpio", "pin": 27, "contact": "NO", "debounce_ms": 30}),
            ({"type": "uart"}, {"type": "uart", "device": "/dev/serial0", "baud": 115200, "command": "STOP"}),
        ):
            with self.subTest(stop=stop):
                rules = validate_rules([dict(base, stop=stop)])
                self.assertEqual(rules[0]["stop"], expected)
                self.assertEqual(validate_rules(rules), rules)
                self.assertEqual(validate_rules([dict(base, stop=stop, stop_on_return=False)]), rules)
                other = dict(stop, id="another")
                with self.assertRaises(ValueError):
                    validate_rules([other, *rules])
                validate_rules([*rules, dict(other, enabled=False)])
        for invalid in (None, "gpio", {}, {"type": "timer"}, {"type": "gpio", "pin": 17},
                        {"type": "gpio", "pin": 27, "debounce_ms": -1}, {"type": "gpio", "pin": True},
                        {"type": "uart", "command": "a\nb"}, {"type": "uart", "start_command": "OTHER"}):
            with self.subTest(stop=invalid), self.assertRaises(ValueError):
                validate_rules([dict(base, stop=invalid)])
        for extra in ({"stop_on_return": True}, {"stop_on_return": False},
                      {"stop": {"type": "uart"}, "stop_on_return": 0}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                validate_rules([{**base, **extra}])


class InputTests(unittest.TestCase):
    def test_sensor_hysteresis_above_and_below(self):
        for mode, sign in [("above", 1), ("below", -1)]:
            with self.subTest(mode=mode):
                stop = threading.Event()
                rule = {"comparison": mode, "threshold": 10 * sign, "hysteresis": 2,
                        "path": "fake", "poll_ms": 0}
                remaining = iter([9, 10, 9, 8, 7, 9, 11])
                states = []

                def publish(active):
                    states.append(active)
                    if len(states) == 7:
                        stop.set()

                with patch.object(inputs, "_sensor_value", side_effect=lambda _: next(remaining) * sign):
                    inputs.sensor(rule, stop, lambda: None, publish)
                self.assertEqual(states, [False, True, True, True, False, False, True])

    def test_uart_split_lines_crlf_overflow_and_timeout(self):
        stop = threading.Event()
        chunks = iter([b"STA", b"", b"RT\r\n", b"ST", b"OP\n", b"a" * 66,
                       b"START\n", b"UNRELATED\nSTART\n"])
        port = Mock(in_waiting=1)

        def read(_):
            try:
                return next(chunks)
            except StopIteration:
                stop.set()
                return b""

        port.read.side_effect = read
        serial = Mock()
        serial.Serial.return_value = contextlib.nullcontext(port)
        states = []
        rule, = validate_rules([{"type": "uart", "id": "port"}])
        with patch.dict(sys.modules, {"serial": serial}):
            inputs.uart(rule, stop, lambda: None, states.append)
        self.assertEqual(states, [True, False, True])

    def test_bookworm_gpiod_edge_adapter(self):
        line = Mock(spec=["request", "release", "get_value", "event_get_fd", "event_read"])
        line.get_value.return_value = 0
        line.event_get_fd.return_value = 7
        line.event_read.return_value = SimpleNamespace(type=1, sec=12, nsec=34)
        chip = Mock(spec=["get_line", "label", "close"])
        chip.get_line.return_value = line
        chip.label.return_value = "pinctrl-rp1"
        module = SimpleNamespace(Chip=lambda path: chip, LINE_REQ_EV_BOTH_EDGES=3,
                                 LINE_REQ_FLAG_BIAS_PULL_UP=32, LineEvent=SimpleNamespace(RISING_EDGE=1))
        with patch.dict(sys.modules, {"gpiod": module}), \
             patch.object(inputs.Path, "glob", return_value=["/dev/gpiochip4"]), \
             patch.object(inputs.select, "select", side_effect=[([], [], []), ([7], [], [])]):
            with inputs._gpio_edges(17) as (high, read):
                self.assertFalse(high)
                self.assertEqual(read(0.1), [])
                self.assertEqual(read(0.1), [(12_000_000_034, True)])
            chip.get_line.assert_called_once_with(17)
            line.request.assert_called_once_with(consumer="rpi-web-control", type=3, flags=32)
            line.release.assert_called_once()
            chip.close.assert_called_once()

    def test_trixie_gpiod_edge_adapter(self):
        enums = SimpleNamespace(Bias=SimpleNamespace(PULL_UP=4), Direction=SimpleNamespace(INPUT=1),
                                Edge=SimpleNamespace(BOTH=3), Value=SimpleNamespace(ACTIVE=1))
        request = Mock(spec=["get_value", "wait_edge_events", "read_edge_events", "release"])
        request.get_value.return_value = 1
        request.wait_edge_events.side_effect = [False, True]
        request.read_edge_events.return_value = [
            SimpleNamespace(timestamp_ns=273, event_type=2, Type=SimpleNamespace(RISING_EDGE=1))]
        chips = [Mock(spec=["get_info", "request_lines", "close"]) for _ in range(2)]
        for chip, label in zip(chips, ["soc", "pinctrl-rp1"]):
            chip.get_info.return_value = SimpleNamespace(label=label)
        chips[1].request_lines.return_value = request
        module = SimpleNamespace(Chip=Mock(side_effect=chips), LineSettings=Mock(return_value="settings"))
        with patch.dict(sys.modules, {"gpiod": module, "gpiod.line": enums}), \
             patch.object(inputs.Path, "glob", return_value=["/dev/gpiochip0", "/dev/gpiochip1"]):
            with inputs._gpio_edges(22) as (high, read):
                self.assertTrue(high)
                self.assertEqual(read(0.1), [])
                self.assertEqual(read(0.1), [(273, False)])
            module.LineSettings.assert_called_once_with(direction=1, edge_detection=3, bias=4)
            chips[1].request_lines.assert_called_once_with(config={22: "settings"}, consumer="rpi-web-control")
            request.release.assert_called_once()
            for chip in chips:
                chip.close.assert_called_once()

    def test_gpio_contact_polarity_and_debounce(self):
        for mode in ("NO", "NC"):
            with self.subTest(mode=mode):
                stop = threading.Event()
                states = []
                clock = [0]
                # Two bounces shorter than 10 ms, then a sustained closed contact.
                changes = iter([(0, True), (3, False), (7, True), (23, False)])
                pending = [next(changes, None)]

                def read(timeout):
                    until = clock[0] + round(timeout * 1e9)
                    if pending[0] and pending[0][0] * 1_000_000 <= until:
                        ms, high = pending[0]
                        clock[0] = ms * 1_000_000
                        pending[0] = next(changes, None)
                        return [(clock[0], high)]
                    clock[0] = until
                    if until > 50_000_000:
                        stop.set()
                    return []

                rule = {"pin": 17, "contact": mode, "debounce_ms": 10}
                with patch.object(inputs, "_gpio_edges", return_value=contextlib.nullcontext((True, read))), \
                     patch.object(inputs.time, "monotonic_ns", side_effect=lambda: clock[0]):
                    inputs.gpio(rule, stop, lambda: None, states.append)
                self.assertEqual(states, [mode == "NC", mode == "NO"])


class ManagerTests(unittest.TestCase):
    @contextlib.contextmanager
    def latch(self, stop_rule=None, **options):
        signals = {17: queue.Queue(), 27: queue.Queue(), "uart": queue.Queue()}
        events = []

        def reader(rule, stopping, ready, publish):
            source = signals[rule.get("pin", "uart")]
            ready()
            while not stopping():
                try:
                    value, processed = source.get(timeout=0.02)
                except queue.Empty:
                    continue
                try:
                    if isinstance(value, OSError):
                        raise value
                    publish(value)
                finally:
                    processed.set()

        def send(source, value):
            processed = threading.Event()
            signals[source].put((value, processed))
            self.assertTrue(processed.wait(2))

        def record(source, value):
            self.assertEqual(source, "gate")
            events.append(value)

        rule = {"id": "gate", "type": "gpio", "pin": 17, "contact": "NC", **options}
        if stop_rule is not None:
            rule["stop"] = stop_rule
        manager = TriggerManager(record)
        with patch.object(inputs, "_gpio_levels", reader), patch.object(inputs, "_uart_lines", reader):
            try:
                manager.configure([rule])
                yield send, events, manager
            finally:
                manager.close()

    def test_toggle_button_ignores_held_startup_and_toggles_once_per_activation(self):
        with self.latch(toggle_on_press=True) as (send, events, manager):
            send(17, True)
            self.assertEqual(manager.status()[0]["state"], "ready")
            send(17, True)
            send(17, False)
            self.assertEqual(events, [])
            send(17, True)
            send(17, True)
            send(17, False)
            self.assertEqual(events, [True])
            send(17, True)
            send(17, False)
            self.assertEqual(events, [True, False])
            send(17, True)
            manager.close()
            self.assertEqual(events, [True, False, True, False])

    def test_latched_gpio_stop_inhibits_start_and_requires_a_new_start_edge(self):
        with self.latch({"type": "gpio", "pin": 27, "contact": "NC"}) as (send, events, manager):
            send(17, True)  # Already open at startup.
            self.assertEqual(manager.status()[0]["state"], "starting")
            send(27, True)
            self.assertEqual(manager.status()[0]["state"], "ready")
            send(17, False)
            send(17, True)  # Stop still open.
            send(27, False)
            self.assertEqual(events, [])
            send(17, False)
            send(17, True)
            send(17, False)
            self.assertEqual(events, [True])
            send(17, True)
            send(27, True)
            send(27, False)
            send(17, True)  # A held start does not retrigger after a stop.
            self.assertEqual(events, [True, False])
            send(17, False)
            send(17, True)
            manager.configure([])
            self.assertEqual(events, [True, False, True, False])

    def test_latched_uart_matches_exact_stop_and_releases_on_device_loss(self):
        with self.latch({"type": "uart", "command": "REC OFF"}) as (send, events, manager):
            send("uart", b"REC OFF")
            send(17, False)
            send(17, True)
            send("uart", b"rec off")
            self.assertEqual(events, [True])
            send("uart", b"REC OFF")
            send(17, True)
            self.assertEqual(events, [True, False])
            send(17, False)
            send(17, True)
            with self.assertLogs("rpi_web_control.triggers", "WARNING"):
                send("uart", OSError("UART disconnected"))
                deadline = time.monotonic() + 1
                while events[-1] and time.monotonic() < deadline:
                    time.sleep(0.01)
            self.assertEqual(events, [True, False, True, False])
            self.assertEqual(manager.status()[0]["error"], "UART disconnected")

    def test_start_contact_return_and_external_stop_release_the_same_claim(self):
        with self.latch({"type": "gpio", "pin": 27}, stop_on_return=True) as (send, events, manager):
            send(17, False)
            send(27, False)
            send(17, True)
            send(17, False)  # Returning START alone ends the first recording.
            self.assertEqual(events, [True, False])
            send(17, True)
            send(27, True)   # Separate STOP ends the second while START is still active.
            send(27, False)
            send(17, True)
            self.assertEqual(events, [True, False, True, False])
            send(17, False)
            send(17, True)
            self.assertTrue(manager.status()[0]["active"])

    def test_retains_unmodified_workers_and_releases_before_replacement(self):
        events = queue.Queue()
        manager = TriggerManager(lambda source, active: events.put((source, active)))
        self.addCleanup(manager.close)

        def adapter(rule, stop, ready, publish):
            ready()
            publish(True)
            stop.wait()

        rule = {"id": "door", "type": "gpio", "pin": 17}
        with patch.dict(inputs.INPUTS, {"gpio": adapter}):
            manager.configure([rule])
            self.assertEqual(events.get(timeout=2), ("door", True))
            manager.configure([rule])
            with self.assertRaises(ValueError):
                manager.configure([dict(rule, pin="invalid")])
            self.assertTrue(events.empty())
            manager.configure([dict(rule, pin=18)])
            self.assertEqual(events.get(timeout=2), ("door", False))
            self.assertEqual(events.get(timeout=2), ("door", True))
            manager.configure([dict(rule, enabled=False)])
            self.assertEqual(events.get(timeout=2), ("door", False))
            self.assertEqual(manager.status()[0]["state"], "disabled")
            self.assertTrue(events.empty())

    def test_fault_releases_claim_and_exposes_error(self):
        released = threading.Event()
        manager = TriggerManager(lambda source, active: None if active else released.set())
        self.addCleanup(manager.close)

        def fail(rule, stop, ready, publish):
            ready()
            publish(True)
            raise OSError("Device disconnected")

        with patch.dict(inputs.INPUTS, {"uart": fail}), self.assertLogs("rpi_web_control.triggers", "WARNING"):
            manager.configure([{"id": "serial", "type": "uart"}])
            self.assertTrue(released.wait(2))
            status, = manager.status()
            self.assertEqual((status["state"], status["error"]), ("error", "Device disconnected"))
            manager.close()

    def test_timer_emits_and_shutdown_balances_claims(self):
        changes = queue.Queue()
        manager = TriggerManager(lambda _, active: changes.put(active))
        self.addCleanup(manager.close)
        manager.configure([{"id": "timer", "type": "timer", "interval_s": 0.3, "duration_s": 0.15}])
        self.assertTrue(changes.get(timeout=2))
        manager.close()
        self.assertFalse(changes.get(timeout=2))
        self.assertTrue(changes.empty())


if __name__ == "__main__":
    unittest.main()
