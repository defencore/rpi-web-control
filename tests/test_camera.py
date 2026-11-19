import csv
import io
import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rpi_web_control.camera import Camera
from rpi_web_control.capture import PiFrame, select_mode


class CameraTests(unittest.TestCase):
    def setUp(self):
        self.camera = Camera("simulated")
        self.addCleanup(self.camera.close)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def record(self, fps, frames=6):
        self.camera.configure({"fps": fps, "osd": True})
        self.camera.set_preview(True)
        directory = Path(self.camera.start_recording(Path(self.temp.name), "manual"))
        seq, jpeg = self.camera.preview_frame(0, 2)
        self.assertGreater(seq, 0)
        self.assertTrue(jpeg.startswith(b"\xff\xd8"))
        self.camera.set_preview(False)
        self.assertTrue(self.camera.status()["recording"])
        self.assertIsNone(self.camera.preview_frame(seq, .1))
        self.wait_frames(frames)
        self.camera.stop_recording()
        self.assertFalse(self.camera.status()["active"])
        return directory

    def wait_frames(self, count):
        deadline = time.monotonic() + 3
        while self.camera.status()["frames"] < count and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertGreaterEqual(self.camera.status()["frames"], count)

    def check_artifacts(self, directory, fps):
        manifest = json.loads((directory / "session.json").read_text())
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["osd_effective"], fps <= 60)
        self.assertEqual(manifest["sensor_mode"], {"width": 640, "height": 480, "max_fps": 240, "bit_depth": 8})
        self.assertEqual(manifest["settings"]["sensor_bit_depth"], 0)
        with (directory / "frames.csv").open() as index:
            rows = [dict((k, int(v)) for k, v in row.items()) for row in csv.DictReader(index)]
        self.assertEqual(manifest["frames"], len(rows))
        data = (directory / manifest["video"]).read_bytes()
        position = 0
        if fps > 60:
            header, _ = data.split(b"\n", 1)
            self.assertIn(b"W640 H480 F240:1", header)
            position = len(header) + 1
        last = 0
        for i, row in enumerate(rows):
            self.assertEqual(row["frame_index"], i)
            self.assertGreater(row["sensor_ns"], last)
            self.assertLess(abs(time.time_ns() - row["realtime_ns"]), 10**10)
            self.assertLess(abs(time.monotonic_ns() - row["monotonic_ns"]), 10**10)
            last = row["sensor_ns"]
            if fps > 60:
                self.assertEqual(data[position:position + 6], b"FRAME\n")
                self.assertEqual(row["size"], 640 * 480 * 3 // 2)
                position += 6
            else:
                from PIL import Image

                with Image.open(io.BytesIO(data[position:position + row["size"]])) as image:
                    self.assertEqual(image.size, (640, 480))
                    image.verify()
            self.assertEqual(row["offset"], position)
            position += row["size"]
        self.assertEqual(position, len(data))
        if shutil.which("ffprobe"):
            check = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                                    "-show_entries", "stream=nb_read_frames,width,height", "-of", "json",
                                    str(directory / manifest["video"])], check=True, capture_output=True)
            stream = json.loads(check.stdout)["streams"][0]
            self.assertEqual((stream["width"], stream["height"]), (640, 480))
            self.assertEqual(int(stream["nb_read_frames"]), len(rows))

    def test_mjpeg_records_timestamped_frames_after_preview_stops(self):
        self.check_artifacts(self.record(30), 30)

    def test_240_fps_profile_writes_raw_frames_and_bypasses_osd(self):
        self.check_artifacts(self.record(240), 240)

    def test_changed_settings_validate_modes_and_recording_state(self):
        caps = self.camera.capabilities()
        self.assertTrue(caps["available"])
        self.assertEqual(select_mode(caps["modes"], {"width": 640, "height": 480, "fps": 240})["max_fps"], 240)
        for bad in ({"width": 639}, {"fps": True}, {"fps": 241}, {"fps": float("nan")},
                    {"sensor_bit_depth": False}, {"sensor_bit_depth": 8.0}, {"sensor_bit_depth": 12}):
            with self.assertRaises(ValueError):
                self.camera.configure(bad)
        self.camera.start_recording(Path(self.temp.name), "test")
        with self.assertRaises(RuntimeError):
            self.camera.configure({"osd": True})
        self.camera.set_preview(True)
        self.camera.stop_recording()
        self.assertTrue(self.camera.status()["active"])

    def test_live_frame_limit_applies_only_to_sender_and_requires_stop_to_change(self):
        self.camera.configure({"fps": 240})
        destination = ("127.0.0.1", 5004)
        with patch("rpi_web_control.camera.LiveOutput") as sender:
            sender.return_value.destination = destination
            sender.return_value.max_fps = 1
            self.camera.set_live(True, *destination, max_fps=1)
            self.camera.set_live(True, *destination, max_fps=1)
            sender.assert_called_once_with(*destination, max_fps=1)
            for invalid in (True, 0, 1001, 1.5, "1"):
                with self.subTest(max_fps=invalid), self.assertRaises(ValueError):
                    self.camera.set_live(True, *destination, max_fps=invalid)
            for changed in (30, None):
                with self.subTest(max_fps=changed), self.assertRaisesRegex(RuntimeError, "frame-rate limit"):
                    self.camera.set_live(True, *destination, max_fps=changed)
            self.assertEqual(self.camera.status()["settings"]["fps"], 240)
            self.camera.set_live(False, None, None)
            self.camera.set_live(True, *destination)
            sender.assert_called_with(*destination, max_fps=None)
            self.camera.set_live(False, None, None)

    def test_disabled_preview_wakes_waiting_client(self):
        self.camera.set_preview(True)
        result = []
        thread = threading.Thread(target=lambda: result.append(self.camera.preview_frame(10**6, 10)))
        thread.start()
        self.camera.set_preview(False)
        thread.join(1)
        self.assertEqual(result, [None])

    def test_saved_profile_survives_replaced_sensor(self):
        restored = {"width": 1920, "height": 1080, "fps": 240, "osd": True}
        camera = Camera("simulated", settings=restored)
        self.addCleanup(camera.close)
        self.assertEqual(camera.status()["settings"], {**restored, "sensor_bit_depth": 0})
        with self.assertRaisesRegex(ValueError, "No sensor mode"):
            camera.set_preview(True)
        camera.configure({"fps": 30})
        self.assertEqual(camera.status()["settings"]["fps"], 30)

    def test_unavailable_hardware_can_store_idle_settings(self):
        with patch.object(self.camera, "_factory", side_effect=RuntimeError("No cameras available.")):
            self.camera.configure({"fps": 120})
            self.assertFalse(self.camera.capabilities()["available"])
            with self.assertRaisesRegex(RuntimeError, "No cameras"):
                self.camera.set_preview(True)
            self.assertFalse(self.camera.status()["active"])

    def test_imx577_mode_requests_use_the_selected_sensor_depth(self):
        # Inventory from the Pi 5. Exercise the adapter with no hardware attached.
        geometries = ((1280, 720, 364.70), (1920, 1080, 175.47),
                      (4056, 2160, 42.43), (4056, 3040, 30.34))
        device = Mock(options={}, camera_properties={"Model": "imx577"})
        device.sensor_modes = [{"size": (w, h), "fps": fps, "bit_depth": depth}
                               for w, h, fps in geometries for depth in (10, 8)]
        device.create_video_configuration.side_effect = lambda **fields: fields
        device.camera_configuration.side_effect = lambda: device.configure.call_args.args[0]
        driver = patch.dict("sys.modules", picamera2=SimpleNamespace(Picamera2=lambda: device))
        driver.start()
        self.addCleanup(driver.stop)
        camera = Camera("picamera2")
        self.addCleanup(camera.close)
        modes = camera.capabilities()["modes"]
        self.assertEqual(len(modes), 8)
        for w, h, limit in geometries:
            auto = {"width": w, "height": h, "fps": limit, "osd": False}
            self.assertEqual(select_mode(modes, auto)["bit_depth"], 10)
            for fps in ((limit, 240) if limit >= 240 else (limit,)):
                for depth in (8, 10):
                    with self.subTest(size=(w, h), fps=fps, depth=depth):
                        settings = {**auto, "fps": fps, "sensor_bit_depth": depth}
                        camera.configure(settings)
                        camera.set_preview(True)
                        self.assertEqual(camera.status()["sensor_mode"],
                                         {"width": w, "height": h, "bit_depth": depth, "max_fps": limit})
                        config = device.configure.call_args.args[0]
                        self.assertEqual(config["sensor"], {"output_size": (w, h), "bit_depth": depth})
                        self.assertEqual(config["main"], {"format": "YUV420", "size": (w, h)})
                        interval = round(1_000_000 / fps)
                        self.assertEqual(config["controls"]["FrameDurationLimits"], (interval, interval))
                        camera.set_preview(False)
                        self.assertIsNone(camera.status()["sensor_mode"])
            if limit < 240:
                with self.assertRaisesRegex(ValueError, "No sensor mode"):
                    select_mode(modes, {**auto, "fps": 240})

        # Some drivers substitute another mode even when they accept the request.
        settings = {"width": 1280, "height": 720, "fps": 240, "osd": False, "sensor_bit_depth": 8}
        device.camera_configuration.side_effect = None
        device.camera_configuration.return_value = {
            "main": {"size": (1280, 720), "format": "YUV420"},
            "sensor": {"output_size": (1280, 720), "bit_depth": 10},
        }
        device.start.reset_mock()
        camera.configure(settings)
        with self.assertRaisesRegex(ValueError, "8-bit sensor mode"):
            camera.set_preview(True)
        device.start.assert_not_called()

    def test_capture_failure_finalizes_as_incomplete_and_can_restart(self):
        with patch("rpi_web_control.recording.Recording.write", side_effect=OSError("Storage full")):
            directory = Path(self.camera.start_recording(Path(self.temp.name), "test"))
            deadline = time.monotonic() + 1
            while self.camera.status()["recording"] and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(self.camera.status()["last_error"], "Storage full")
        self.assertEqual(json.loads((directory / "session.json").read_text())["status"], "incomplete")
        self.camera.start_recording(Path(self.temp.name), "retry")
        self.wait_frames(1)
        self.camera.stop_recording()

    def test_padded_chroma_planes_keep_only_image_pixels(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("NumPy is required to emulate a mapped hardware buffer.")
        src = np.array([1, 2, 3, 4, 0, 0, 0, 0] * 2 + [5, 6, 0, 0, 7, 8, 0, 0], dtype=np.uint8)
        from contextlib import nullcontext

        request = SimpleNamespace(config={"main": {"size": (4, 2), "stride": 8}},
                                  get_metadata=lambda: {"SensorTimestamp": 200})
        with patch.dict("sys.modules", picamera2=SimpleNamespace(
                MappedArray=lambda *a, **kw: nullcontext(SimpleNamespace(array=src)))):
            dest = io.BytesIO()
            PiFrame(request).write_yuv(dest)
        self.assertEqual(dest.getvalue(), bytes([1, 2, 3, 4] * 2 + [5, 6, 7, 8]))
