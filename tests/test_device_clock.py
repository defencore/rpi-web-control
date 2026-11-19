"""Verify device clock behavior without altering any operating system clock."""

import io
import json
import socket
import unittest
from unittest.mock import MagicMock, patch

from rpi_web_control import device_clock


class DeviceClockTests(unittest.TestCase):
    def test_simulator_advances_from_selected_epoch_without_host_actions(self):
        clock = device_clock.DeviceClock(True)
        target = 1_895_678_901_234
        with patch.object(device_clock.time, "time_ns", return_value=1_000_999_999) as now, \
                patch.object(device_clock.socket, "socket") as network, \
                patch.object(device_clock.time, "clock_settime_ns", create=True) as set_clock:
            self.assertEqual(clock.now_ms(), 1000)
            clock.set_time(target)
            self.assertEqual(clock.now_ms(), target)
            now.return_value += 125_000_123
            self.assertEqual(clock.now_ns(), target * 1_000_000 + 125_000_123)
            self.assertEqual(clock.now_ms(), target + 125)
            network.assert_not_called()
            set_clock.assert_not_called()

    def exchange(self, raw):
        client, helper = socket.socketpair()
        with client, helper:
            client.settimeout(1)
            client.sendall(raw)
            client.shutdown(socket.SHUT_WR)
            device_clock.serve_request(helper)
            return device_clock.read_message(client)

    def send_sample(self, realtime_ms, monotonic_ns):
        sample = {"browser_realtime_ms": realtime_ms, "request_monotonic_ns": monotonic_ns}
        return self.exchange(json.dumps(sample).encode() + b"\n")

    def test_privileged_boundary_sets_only_realtime_in_integer_nanoseconds(self):
        stamp = 7_000_000_000
        with patch.object(device_clock.time, "clock_settime_ns", create=True) as set_clock, \
                patch.object(device_clock.time, "monotonic_ns", return_value=stamp + 10):
            for value in (946_684_800_000, 4_102_444_799_999):
                self.assertEqual(self.send_sample(value, stamp), {"ok": True})
                set_clock.assert_called_with(device_clock.time.CLOCK_REALTIME, value * 1_000_000 + 10)
            set_clock.side_effect = PermissionError()
            self.assertIn("error", self.send_sample(value, stamp))

    def test_queued_clock_samples(self):
        target = 1_895_678_901_234
        now = 9_000_000_000
        with patch.object(device_clock.time, "clock_settime_ns", create=True) as set_clock, \
                patch.object(device_clock.time, "monotonic_ns", return_value=now):
            for age in (0, 2_999_999_999):
                self.assertEqual(self.send_sample(target, now - age), {"ok": True})
                set_clock.assert_called_with(device_clock.time.CLOCK_REALTIME, target * 1_000_000 + age)
            set_clock.reset_mock()
            for stamp in (now - 3_000_000_000, now - 5_000_000_000, now + 1, -1, True, 1.2):
                with self.subTest(stamp=stamp):
                    self.assertIn("error", self.send_sample(target, stamp))
            self.assertIn("error", self.send_sample(4_102_444_799_999, now - 1_000_000))
            set_clock.assert_not_called()

    def test_invalid_times_rejected_at_both_boundaries(self):
        clock = device_clock.DeviceClock(False)
        with patch.object(device_clock.time, "clock_settime_ns", create=True) as set_clock:
            for value in (None, True, "1895678901234", 1_895_678_901_234.0,
                          946_684_799_999, 4_102_444_800_000):
                with self.subTest(value=value):
                    with self.assertRaises(ValueError):
                        clock.set_time(value)
                    self.assertIn("error", self.send_sample(value, device_clock.time.monotonic_ns()))
            for raw in (b'{}\n', b'{}', b'[]\n', b'bad\n', b' ' * 255 + b'{}\n',
                        b'{"browser_realtime_ms":1895678901234}\n',
                        b'{"browser_realtime_ms":1895678901234,"request_monotonic_ns":0,"action":"other"}\n'):
                self.assertIn("error", self.exchange(raw))
            set_clock.assert_not_called()

    def test_helper_client_confirmation_and_failures(self):
        clock = device_clock.DeviceClock(False)
        target = 1_895_678_901_234
        connection = MagicMock()
        connection.__enter__.return_value = connection
        with patch.object(device_clock.socket, "socket", return_value=connection), \
                patch.object(device_clock.time, "monotonic_ns", return_value=8_000_000_000):
            connection.makefile.return_value = io.BytesIO(b'{"ok":true}\n')
            clock.set_time(target)
            connection.connect.assert_called_with(device_clock.CLOCK_SOCKET)
            self.assertEqual(json.loads(connection.sendall.call_args.args[0]),
                             {"browser_realtime_ms": target, "request_monotonic_ns": 8_000_000_000})
            for raw in (b'{"error":"Clock update failed."}\n', b'{}\n', b'{}', b'x' * 257):
                connection.makefile.return_value = io.BytesIO(raw)
                with self.assertRaises(RuntimeError):
                    clock.set_time(target)
            connection.connect.side_effect = TimeoutError()
            with self.assertRaises(RuntimeError):
                clock.set_time(target)
