import errno
import json
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from rpi_web_control.camera import Camera
from rpi_web_control.capture import PiSource, as_jpeg
from rpi_web_control.live import LiveOutput, RtpJpeg, endpoint


class DatagramLink:
    """Keep datagram boundaries while exercising the sender without a network interface."""

    def __init__(self, sock):
        self.sock = sock
        self.destination = None

    def __getattr__(self, name):
        return getattr(self.sock, name)

    def sendmsg(self, parts, ancillary, flags, destination):
        self.destination = destination
        return self.sock.sendmsg(parts)


class TrackedFrame:
    def __init__(self, timestamp, jpeg=None):
        self.sensor_ns = timestamp
        self.jpeg = jpeg
        self.refs = 0

    def acquire(self):
        self.refs += 1

    def release(self):
        self.refs -= 1

    def live_jpeg(self, osd):
        return self.jpeg


class LiveTests(unittest.TestCase):
    def test_rtp_fragments_and_sensor_clock(self):
        source = Image.effect_noise((256, 128), 55).convert("RGB")
        jpeg = as_jpeg(source)
        rtp = RtpJpeg()
        rtp.sequence = 65530
        rtp.time_offset = 0
        base = 1_000_000_000
        packets = [head + data for head, data in rtp.packets(jpeg, base)]
        self.assertGreater(len(packets), 6)
        scan = bytearray()
        for i, packet in enumerate(packets):
            v, pt, seq, ts, ssrc = struct.unpack_from("!BBHII", packet)
            self.assertEqual((v, pt & 127, seq, ts, ssrc), (128, 26, (65530 + i) % 65536, 90000, rtp.ssrc))
            self.assertEqual(bool(pt & 128), i == len(packets) - 1)
            self.assertLessEqual(len(packet), 1200)
            self.assertEqual(int.from_bytes(packet[12:16], "big"), len(scan))
            self.assertEqual(packet[16:20], bytes([1, 255, 32, 16]))
            if i == 0:
                self.assertEqual(packet[20:24], b"\x00\x00\x00\x80")
            scan.extend(packet[152 if i == 0 else 20:])
        self.assertTrue(jpeg.endswith(bytes(scan) + b"\xff\xd9"))
        next_header, _ = next(rtp.packets(jpeg, base + 4_166_667))
        self.assertEqual(struct.unpack_from("!I", next_header, 4)[0], 90375)

    def test_rejects_unrepresentable_size_or_invalid_destination(self):
        camera = Camera("simulated")
        self.addCleanup(camera.close)
        camera.configure({"width": 638})
        with self.assertRaisesRegex(ValueError, "divisible by 8"):
            camera.set_live(True, "127.0.0.1", 5004)
        self.assertFalse(camera.status()["active"])
        self.assertEqual(endpoint("::ffff:192.0.2.4", 5004), ("192.0.2.4", 5004))
        for host, port in [("0.0.0.0", 5004), ("239.1.1.1", 5004), ("::", 5004),
                           ("255.255.255.255", 5004), ("example.org", 5004), ("127.0.0.1", 22),
                           ("fe80::12", 5004), ("fe80::1%0", 5004), ("fe80::1%bad scope", 5004),
                           (True, 5004)]:
            with self.assertRaises(ValueError):
                endpoint(host, port)

    def test_record_preview_and_rtp_are_independent_at_240(self):
        sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(sender.close)
        self.addCleanup(receiver.close)
        receiver.settimeout(2)
        camera = Camera("simulated")
        self.addCleanup(camera.close)
        camera.configure({"fps": 240})
        wire = DatagramLink(sender)
        with patch("rpi_web_control.live.socket.socket", return_value=wire):
            camera.set_live(True, "fe80::12%1", 5004)
            with self.assertRaisesRegex(RuntimeError, "different receiver"):
                camera.set_live(True, "192.0.2.2", 5008)
        images = 0
        while images < 4:
            packet = receiver.recv(1500)
            images += bool(packet[1] & 128)
        self.assertEqual(wire.destination, ("fe80::12", 5004, 0, 1))
        self.assertEqual(camera.status()["live"]["host"], "fe80::12%1")
        self.assertTrue(camera.status()["live"]["enabled"])
        self.assertFalse(camera.status()["preview"])
        with self.assertRaisesRegex(RuntimeError, "live stream"):
            camera.configure({"fps": 30})
        with tempfile.TemporaryDirectory() as output:
            session = camera.start_recording(Path(output), "manual")
            camera.set_preview(True)
            self.assertIsNotNone(camera.preview_frame(0, 2))
            camera.set_preview(False)
            self.assertTrue(camera.status()["recording"])
            camera.set_live(False, None, None)
            self.assertFalse(camera.status()["live"]["enabled"])
            self.assertTrue(camera.status()["recording"])
            camera.stop_recording()
            self.assertTrue((Path(session) / "video.y4m").is_file())
        self.assertFalse(camera.status()["active"])

    def test_disk_failure_while_streaming_preserves_preview_and_live(self):
        sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(sender.close)
        self.addCleanup(receiver.close)
        camera = Camera("simulated")
        self.addCleanup(camera.close)
        with patch("rpi_web_control.live.socket.socket", return_value=DatagramLink(sender)):
            camera.set_live(True, "127.0.0.1", 5004)
        camera.set_preview(True)
        with tempfile.TemporaryDirectory() as output:
            with patch("rpi_web_control.recording.Recording.write", side_effect=OSError("Volume removed")):
                session = camera.start_recording(Path(output), "manual")
                deadline = time.monotonic() + 2
                while camera.status()["recording"] and time.monotonic() < deadline:
                    time.sleep(.01)
            status = camera.status()
            self.assertFalse(status["recording"])
            self.assertIn("Could not write recording " + session, status["last_error"])
            self.assertIn("Volume removed", status["last_error"])
            self.assertTrue(status["live"]["enabled"])
            self.assertTrue(status["preview"])
            self.assertEqual(json.loads((Path(session) / "session.json").read_text())["status"], "incomplete")

    def test_unusable_encoder_releases_camera_ownership(self):
        sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(sender.close)
        self.addCleanup(receiver.close)
        camera = Camera("simulated")
        self.addCleanup(camera.close)
        with patch("rpi_web_control.live.socket.socket", return_value=DatagramLink(sender)), patch(
                "rpi_web_control.capture.SimFrame.jpeg", side_effect=ValueError("Unexpected encoder format")):
            camera.set_live(True, "127.0.0.1", 5004)
            deadline = time.monotonic() + 2
            while camera.status()["active"] and time.monotonic() < deadline:
                time.sleep(.01)
        self.assertFalse(camera.status()["active"])
        self.assertFalse(camera.status()["live"]["enabled"])
        self.assertEqual(camera.status()["live"]["last_error"], "Unexpected encoder format")

    def test_backpressure_replaces_pending_request_and_releases_every_reference(self):
        sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(receiver.close)
        with patch("rpi_web_control.live.socket.socket", return_value=DatagramLink(sender)):
            stream = LiveOutput("127.0.0.1", 5004)
        self.addCleanup(stream.close)
        waiting = threading.Event()
        resume = threading.Event()
        jpeg = as_jpeg(Image.new("RGB", (64, 64)))

        class Frame:
            def __init__(self, timestamp):
                self.sensor_ns = timestamp
                self.refs = 0

            def acquire(self):
                self.refs += 1

            def release(self):
                self.refs -= 1

            def live_jpeg(self, osd):
                waiting.set()
                resume.wait(2)
                return jpeg

        frames = [Frame(n * 4_166_667) for n in range(4)]
        stream.offer(frames[0], None, False)
        self.assertTrue(waiting.wait(1))
        for frame in frames[1:]:
            stream.offer(frame, None, False)
        self.assertEqual([f.refs for f in frames], [1, 0, 0, 1])
        resume.set()
        receiver.settimeout(2)
        for _ in range(2):
            self.assertTrue(receiver.recv(1500)[1] & 128)
        stream.close()
        self.assertEqual([f.refs for f in frames], [0, 0, 0, 0])
        self.assertEqual(stream.status()["dropped_frames"], 2)
        self.assertEqual(stream.status()["queue_drops"], 2)
        self.assertEqual(stream.status()["congestion_drops"], 0)
        self.assertEqual(stream.status()["sent_frames"], 2)

    def test_transient_pressure_retries_exact_fragment_after_releasing_camera(self):
        jpeg = as_jpeg(Image.effect_noise((128, 128), 55).convert("RGB"))
        frame = TrackedFrame(42, jpeg)
        attempts = []
        accepted = []
        complete = threading.Event()
        refs = []

        class RecoveringLink(DatagramLink):
            def sendmsg(self, parts, *args):
                attempts.append(b"".join(parts))
                refs.append(frame.refs)
                if len(attempts) == 2:
                    raise BlockingIOError(errno.EAGAIN, "socket busy")
                if len(attempts) == 3:
                    raise OSError(errno.ENOBUFS, "socket busy")
                accepted.append(attempts[-1])
                if accepted[-1][1] & 128:
                    complete.set()
                return len(accepted[-1])

        sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(receiver.close)
        with patch("rpi_web_control.live.socket.socket", return_value=RecoveringLink(sender)):
            stream = LiveOutput("127.0.0.1", 5004)
        self.addCleanup(stream.close)
        with patch("rpi_web_control.live._FRAME_RETRY_SECONDS", 1):
            stream.offer(frame, None, False)
            self.assertTrue(complete.wait(2))
        stream.close()
        self.assertGreater(len(accepted), 2)
        self.assertEqual(attempts[1:4], [accepted[1]] * 3)
        self.assertEqual([attempts[0]] + attempts[3:], accepted)
        self.assertEqual(set(refs), {0})
        state = stream.status()
        self.assertEqual((state["send_retries"], state["sent_frames"], state["dropped_frames"]), (2, 1, 0))
        self.assertIsNone(state["last_error"])

    def test_sustained_pressure_and_hard_error_leave_sender_ready_for_next_frame(self):
        class FaultLink(DatagramLink):
            fault = errno.EWOULDBLOCK

            def sendmsg(self, parts, *args):
                if self.fault:
                    raise OSError(self.fault, "injected socket error")
                return super().sendmsg(parts, *args)

        sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(receiver.close)
        receiver.settimeout(2)
        link = FaultLink(sender)
        with patch("rpi_web_control.live.socket.socket", return_value=link):
            stream = LiveOutput("127.0.0.1", 5004)
        self.addCleanup(stream.close)
        jpeg = as_jpeg(Image.new("RGB", (64, 64)))
        for dropped, fault in enumerate((errno.EWOULDBLOCK, errno.ENETUNREACH), 1):
            link.fault = fault
            stream.offer(TrackedFrame(dropped), jpeg, False)
            deadline = time.monotonic() + 1
            while stream.status()["dropped_frames"] < dropped and time.monotonic() < deadline:
                time.sleep(.001)
            state = stream.status()
            self.assertEqual((state["congestion_drops"], state["dropped_frames"]), (1, dropped))
            self.assertEqual(state["queue_drops"], 0)
            self.assertTrue(state["enabled"])
            if fault == errno.EWOULDBLOCK:
                self.assertIsNone(state["last_error"])
                retries = state["send_retries"]
                self.assertGreaterEqual(retries, 1)
            else:
                self.assertIn("injected socket error", state["last_error"])
                self.assertEqual(state["send_retries"], retries)
        link.fault = None
        stream.offer(TrackedFrame(3), jpeg, False)
        self.assertTrue(receiver.recv(1500)[1] & 128)
        stream.close()
        self.assertEqual(stream.status()["sent_frames"], 1)
        self.assertIsNone(stream.status()["last_error"])

    def test_rate_limit_follows_sensor_clock_and_skips_before_queue_or_acquire(self):
        sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(receiver.close)
        # Keep the queue under pressure so its accepted requests are observable.
        with patch("rpi_web_control.live.socket.socket", return_value=DatagramLink(sender)), patch.object(
                LiveOutput, "_run", return_value=None):
            stream = LiveOutput("127.0.0.1", 5004, max_fps=60)
        self.addCleanup(stream.close)
        timestamps = [round(n * 1e9 / 240.14) + n % 3 * 1000 for n in range(2402)]
        frames = [TrackedFrame(ts) for ts in timestamps]
        selected = []
        for i, frame in enumerate(frames):
            stream.offer(frame, None, False)
            if frame.refs:
                selected.append(i)
        self.assertEqual(len(selected), 600)
        self.assertTrue(all(3 <= b - a <= 5 for a, b in zip(selected, selected[1:])))
        self.assertEqual(sum(frame.refs for frame in frames), 1)
        state = stream.status()
        self.assertEqual(state["max_fps"], 60)
        self.assertEqual(state["rate_limited_frames"], len(frames) - len(selected))
        self.assertEqual(state["queue_drops"], len(selected) - 1)
        self.assertEqual(state["dropped_frames"], state["queue_drops"])

        # Restart the sampling period after a gap or a clock reset.
        for ts, accepted in [(20_000_000_000, 1), (20_004_000_000, 0), (0, 1), (4_000_000, 0)]:
            frame = TrackedFrame(ts)
            stream.offer(frame, None, False)
            self.assertEqual(frame.refs, accepted)

    def test_live_encoder_uses_visible_yuv_planes(self):
        from contextlib import contextmanager
        import io
        import mmap
        from types import SimpleNamespace
        import numpy as np
        from rpi_web_control.capture import PiFrame

        raw = bytes([1, 2, 3, 4, 0, 0, 0, 0] * 2 + [5, 6, 0, 0, 7, 8, 0, 0])

        @contextmanager
        def map_frame(*args, **kwargs):
            with mmap.mmap(-1, len(raw)) as buffer:
                buffer.write(raw)
                mapped = SimpleNamespace(array=np.frombuffer(buffer, dtype=np.uint8))
                try:
                    yield mapped
                finally:
                    del mapped.array

        def encode(y, u, v, quality):
            self.assertEqual(y.tolist(), [[1, 2, 3, 4]] * 2)
            self.assertEqual(u.tolist(), [[5, 6]])
            self.assertEqual(v.tolist(), [[7, 8]])
            self.assertEqual(quality, 82)
            return b"jpeg"
        request = SimpleNamespace(config={"main": {"size": (4, 2), "stride": 8}},
                                  get_metadata=lambda: {"SensorTimestamp": 5})
        mapping = SimpleNamespace(MappedArray=map_frame)
        with patch.dict("sys.modules", picamera2=mapping, simplejpeg=SimpleNamespace(encode_jpeg_yuv_planes=encode)):
            frame = PiFrame(request)
            self.assertEqual(frame.live_jpeg(False), b"jpeg")
            output = io.BytesIO()
            frame.write_yuv(output)
            self.assertEqual(output.getvalue(), bytes([1, 2, 3, 4] * 2 + [5, 6, 7, 8]))

    def test_sensor_properties_available_without_installing_libcamera(self):
        from types import SimpleNamespace
        source = PiSource.__new__(PiSource)
        source.camera = SimpleNamespace(
            sensor_modes=[{"size": (640, 480), "fps": 240.2, "bit_depth": 10}],
            camera_properties={"Model": "Test Sensor", "PixelArraySize": (1920, 1080),
                               "UnitCellSize": SimpleNamespace(width=1200, height=1200),
                               "Location": 2, "Rotation": 180})
        caps = source.capabilities()
        self.assertEqual(caps["model"], "Test Sensor")
        self.assertEqual(caps["properties"], {"pixel_array_size": [1920, 1080], "unit_cell_size_nm": [1200, 1200],
                                             "rotation_degrees": 180, "location": "External"})
