"""Exercise the privileged storage protocol without opening a device."""

import json
import socket
import threading
import unittest
from unittest.mock import Mock, patch

from rpi_web_control.nvme_client import NvmeClient, REPLY_LIMIT, REQUEST_LIMIT, serve_request


class NvmeSocketTests(unittest.TestCase):
    def exchange(self, callback, operation):
        client, peer = socket.socketpair()

        def worker():
            with peer:
                callback(peer)

        thread = threading.Thread(target=worker)
        thread.start()
        try:
            with client:
                return operation(client)
        finally:
            thread.join(timeout=6)
            self.assertFalse(thread.is_alive())

    def send_wire(self, raw, factory):
        def send(client):
            client.sendall(raw)
            client.shutdown(socket.SHUT_WR)
            with client.makefile("rb") as stream:
                return json.loads(stream.readline())

        return self.exchange(lambda peer: serve_request(peer, factory), send)

    def test_exact_privileged_operations(self):
        manager = Mock()
        factory = Mock(return_value=manager)
        manager.status.return_value = {"devices": []}
        manager.apply.return_value = {"mounted": True}
        self.assertEqual(self.send_wire(b'{"action":"status"}\n', factory), {"result": {"devices": []}})
        for action, fields in (("prepare", {"confirmation": "/dev/nvme0n1"}),
                               ("mount", {"volume": "/dev/nvme0n1p1"})):
            body = dict(action=action, device="/dev/nvme0n1", revision="r1", **fields)
            raw = json.dumps(body).encode() + b"\n"
            self.assertEqual(self.send_wire(raw, factory), {"result": {"mounted": True}})
            manager.apply.assert_called_with(body)
        manager.status.assert_called_once()

    def test_usb_dispatch_uses_a_fixed_medium_and_original_action_shape(self):
        manager = Mock()
        factory = Mock(return_value=manager)
        body = {"action": "prepare", "device": "/dev/sdb", "confirmation": "/dev/sdb", "revision": "r"}
        manager.apply.return_value = {"ready": True}
        wire = json.dumps({**body, "medium": "usb"}).encode() + b"\n"
        self.assertEqual(self.send_wire(wire, factory), {"result": {"ready": True}})
        factory.assert_called_once_with(medium="usb")
        manager.apply.assert_called_once_with(body)
        client = NvmeClient("/no-such-storage.sock", medium="usb")
        self.assertEqual(client.status()["directory"], "/srv/rpi-web-control-usb/recordings")
        with self.assertRaises(ValueError):
            client.apply({**body, "medium": "nvme"})
        factory.reset_mock()
        for field in [b'"usb-drive"', b'"/srv/custom"', b'null', b'{}']:
            self.assertEqual(self.send_wire(b'{"action":"status","medium":'+field+b'}\n', factory)["kind"], "invalid")
        factory.assert_not_called()

    def test_malformed_input_never_invokes_manager(self):
        factory = Mock()
        for raw in (b'{}\n', b'{"action":"run","args":[]}\n', b'{"action":"prepare"}\n',
                    b'{"action":"status","device":"/dev/root"}\n', b'{"action":"status"}',
                    b'{"action":[]}\n', b'{}' + b' ' * REQUEST_LIMIT + b'\n'):
            with self.subTest(raw=raw[:40]):
                self.assertEqual(self.send_wire(raw, factory)["kind"], "invalid")
        factory.assert_not_called()

    def test_client_missing_helper_and_invalid_apply(self):
        client = NvmeClient("/no-such-rpi-web-control-nvme.sock")
        status = client.status()
        self.assertFalse(status["available"])
        self.assertEqual(status["devices"], [])
        self.assertEqual(status["directory"], "/srv/rpi-web-control-nvme/recordings")
        with self.assertRaises(ValueError):
            client.apply({"action": "status"})

    def test_client_enforces_reply_size_and_maps_helper_errors(self):
        def run(raw):
            def respond(peer):
                with peer.makefile("rb") as stream:
                    self.assertEqual(json.loads(stream.readline())["action"], "mount")
                try:
                    peer.sendall(raw)
                except BrokenPipeError:
                    pass

            def request(client):
                # The socket pair is connected already; use the normal request path.
                connected = Mock(wraps=client)
                connected.__enter__ = Mock(return_value=connected)
                connected.__exit__ = Mock(return_value=False)
                connected.connect = Mock()
                with patch("rpi_web_control.nvme_client.socket.socket", return_value=connected):
                    return NvmeClient().apply({"action": "mount", "device": "/dev/nvme0n1",
                                               "revision": "r1", "volume": "/dev/nvme0n1p1"})

            return self.exchange(respond, request)

        self.assertEqual(run(b'{"result":{"available":true}}\n'), {"available": True})
        with self.assertRaisesRegex(ValueError, "Drive changed"):
            run(b'{"kind":"invalid","error":"Drive changed."}\n')
        with self.assertRaises(RuntimeError):
            run(b'{"result":{}}' + b' ' * REPLY_LIMIT + b'\n')
