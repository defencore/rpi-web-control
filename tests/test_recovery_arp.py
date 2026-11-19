"""Exercise the Ethernet conflict probe with a simulated packet socket and clock."""

import ipaddress
import socket
import struct
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from rpi_web_control.recovery_arp import announce_address, probe_address


OWN_MAC = "02:00:00:00:00:01"
OWN = bytes.fromhex("020000000001")
PEER = bytes.fromhex("020000000002")
ADDRESS = "192.168.55.100"
CANDIDATE = ipaddress.IPv4Address(ADDRESS).packed
ZERO = bytes(4)
OTHER = ipaddress.IPv4Address("192.168.55.200").packed


def request(sender=PEER, spa=ZERO, tpa=CANDIDATE, op=1, proto=0x0800):
    return struct.pack("!HHBBH6s4s6s4s", 1, proto, 6, 4, op, sender, spa, bytes(6), tpa)


class PacketLink:
    def __init__(self):
        self.now = 0.0
        self.timeout = None
        self.bound = None
        self.mac = OWN
        self.hardware_type = 1
        self.arrivals = []
        self.sent = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def bind(self, address):
        self.bound = address

    def getsockname(self):
        return (*self.bound, 0, self.hardware_type, self.mac)

    def settimeout(self, seconds):
        self.timeout = seconds

    def sendto(self, data, destination):
        self.sent.append((self.now, data, destination))
        return len(data)

    def recv(self, size):
        if self.arrivals and self.arrivals[0][0] <= self.now + self.timeout:
            self.now, data = self.arrivals.pop(0)
            return data[:size]
        self.now += self.timeout
        raise TimeoutError


class RecoveryArpTests(unittest.TestCase):
    def setUp(self):
        self.link = PacketLink()
        replacements = ExitStack()
        self.addCleanup(replacements.close)
        self.family = replacements.enter_context(patch.object(socket, "AF_PACKET", 17, create=True))
        self.factory = replacements.enter_context(patch("socket.socket", return_value=self.link))
        replacements.enter_context(patch("rpi_web_control.recovery_arp.time.monotonic", side_effect=lambda: self.link.now))
        replacements.enter_context(patch("rpi_web_control.recovery_arp.random.uniform", side_effect=[0.5, 1.25, 1.75]))

    def test_bounded_probe_sequence_despite_unrelated_traffic(self):
        noise = [b"", b"short", request(sender=OWN), request(spa=OTHER), request(tpa=OTHER),
                 request(sender=bytes(6)), request(sender=b"\xff" * 6), request(op=3), request(proto=0x86dd),
                 request(op=2, tpa=CANDIDATE)]
        self.link.arrivals = [(i / 100, noise[i % len(noise)]) for i in range(1, 550)]
        self.assertTrue(probe_address("eth0", ipaddress.IPv4Address(ADDRESS), OWN_MAC))
        expected = request(sender=OWN)
        self.assertEqual(self.link.bound, ("eth0", 0x0806))
        self.factory.assert_called_once_with(self.family, socket.SOCK_DGRAM, 0)
        self.assertEqual(self.link.sent, [(t, expected, ("eth0", 0x0806, 0, 0, b"\xff" * 6))
                                         for t in (0.5, 1.75, 3.5)])
        self.assertEqual(self.link.now, 5.5)
        self.assertTrue(self.link.closed)

    def test_claim_during_initial_delay_prevents_transmission(self):
        self.link.arrivals = [(0.1, request(spa=CANDIDATE, tpa=OTHER))]
        self.assertFalse(probe_address("eth0", ADDRESS, OWN_MAC))
        self.assertEqual(self.link.sent, [])

    def test_reply_in_final_wait_rejects_address(self):
        self.link.arrivals = [(5.49, request(spa=CANDIDATE, tpa=ZERO, op=2) + bytes(14))]
        self.assertFalse(probe_address("eth0", ADDRESS, OWN_MAC))
        self.assertEqual(len(self.link.sent), 3)

    def test_simultaneous_probe_from_another_mac(self):
        self.link.arrivals = [(1.0, request())]
        self.assertFalse(probe_address("eth0", ADDRESS, OWN_MAC))
        self.assertEqual(len(self.link.sent), 1)

    def test_single_announcement_uses_assigned_address_as_sender_and_target(self):
        announce_address("eth0", ADDRESS, OWN)
        self.assertEqual(self.link.sent, [(0, request(sender=OWN, spa=CANDIDATE),
                                           ("eth0", 0x0806, 0, 0, b"\xff" * 6))])
        self.assertTrue(self.link.closed)

    def test_invalid_inputs_do_not_open_socket(self):
        for args in (("../x", ADDRESS, OWN_MAC), ("eth0", "0.0.0.0", OWN_MAC), ("eth0", "224.0.0.1", OWN_MAC),
                     ("eth0", "::1", OWN_MAC), ("eth0", 123, OWN_MAC), ("eth0", "169.254.1.2", OWN_MAC),
                     ("eth0", ADDRESS, "0"), ("eth0", ADDRESS, bytes(6)), ("eth0", ADDRESS, b"\x03" * 6)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                probe_address(*args)
        self.factory.assert_not_called()

    def test_replaced_link_or_socket_error_is_not_a_free_address(self):
        self.link.mac = PEER
        with self.assertRaisesRegex(OSError, "changed"):
            probe_address("eth0", ADDRESS, OWN_MAC)
        self.assertEqual(self.link.sent, [])
        self.assertTrue(self.link.closed)
        self.factory.side_effect = PermissionError("No CAP_NET_RAW")
        with self.assertRaises(PermissionError):
            probe_address("eth0", ADDRESS, OWN_MAC)
