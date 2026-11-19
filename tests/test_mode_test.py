import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from rpi_web_control.camera import Camera, CameraBusy
from rpi_web_control.mode_test import ModeTest


class ModeTestTests(unittest.TestCase):
    def setUp(self):
        self.mode = {"width": 640, "height": 480, "bit_depth": 8, "max_fps": 240}
        self.selected = {k: v for k, v in self.mode.items() if k != "max_fps"}

    def test_selects_exact_depth_and_keeps_completed_rows_after_a_driver_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = Path(directory) / "camera-probe.py"
            worker.write_text(
                "import json, sys, time\n"
                "mode = json.loads(sys.argv[3])\n"
                "if mode['bit_depth'] == 8: time.sleep(60)\n"
                "print(json.dumps({'measurement': {'achieved_fps': 240, 'frames': 241, 'gaps_over_1_5_periods': 0}, "
                "'issues': [], 'sample_queue_drops': 0, 'status': 'passed'}))\n")
            high_depth = dict(self.mode, bit_depth=10)
            caps = {"modes": [self.mode, high_depth]}
            test = ModeTest("simulated")
            self.addCleanup(test.cancel)
            with (patch("rpi_web_control.mode_test.PROBE", worker),
                  patch("rpi_web_control.mode_test.WORKER_TIMEOUT", 0.6)):
                self.assertEqual(test.start(caps, None)["total"], 2)
                while test.running:
                    test.tick()
                report = test.status()
                self.assertEqual(report["state"], "failed")
                first, second = report["results"]
                self.assertEqual(first["mode"], self.mode)
                self.assertIsNone(first["achieved_fps"])
                self.assertIsNone(first["timestamp_gaps"])
                self.assertIn("Timed out", first["error"])
                self.assertEqual(second["mode"], high_depth)
                self.assertEqual(second["status"], "passed")
                self.assertEqual(second["timestamp_gaps"], 0)
                self.assertEqual(test.start(caps, dict(self.selected, bit_depth=10))["total"], 1)
                while test.running:
                    test.tick()
                self.assertEqual(test.status()["results"], [second])
            with self.assertRaises(ValueError):
                test.start(caps, dict(self.selected, bit_depth=12))
            self.assertEqual(test.start({"modes": [], "error": "Device busy"}, None)["state"], "failed")
            self.assertEqual(test.status()["error"], "Device busy")

    def test_camera_test_excludes_capture_users_and_recording_preempts_worker(self):
        camera = Camera("simulated")
        self.addCleanup(camera.close)
        original = camera.status()["settings"]
        camera.set_preview(True)
        with self.assertRaises(CameraBusy):
            camera.start_mode_test()
        self.assertTrue(camera.status()["preview"])
        camera.set_preview(False)
        self.assertEqual(camera.start_mode_test(self.selected)["state"], "running")
        for action in (lambda: camera.set_preview(True), lambda: camera.set_live(True, "127.0.0.1", 5004),
                       lambda: camera.configure({"fps": 60}), camera.start_mode_test):
            with self.assertRaises(CameraBusy):
                action()
        self.assertIsNone(camera.status()["last_error"])
        deadline = time.monotonic() + 1
        while camera._mode_test.worker is None and time.monotonic() < deadline:
            time.sleep(0.005)
        active_probe = camera._mode_test.worker
        self.assertIsNotNone(active_probe)
        with tempfile.TemporaryDirectory() as directory:
            camera.start_recording(Path(directory), "trigger:gpio")
            state = camera.status()
            self.assertTrue(state["recording"])
            self.assertEqual(state["mode_test"]["state"], "cancelled")
            self.assertIn("recording", state["mode_test"]["error"])
            self.assertIsNone(camera._mode_test.worker)
            self.assertIsNotNone(active_probe.returncode)
            with self.assertRaises(CameraBusy):
                camera.start_mode_test()
            camera.stop_recording()
            self.assertEqual(camera.status()["settings"], original)

    def test_simulator_worker_reports_observed_cadence_and_releases_camera(self):
        camera = Camera("simulated")
        self.addCleanup(camera.close)
        camera.start_mode_test(self.selected)
        deadline = time.monotonic() + 8
        while camera.status()["mode_test"]["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.05)
        report = camera.status()["mode_test"]
        self.assertIn(report["state"], ("completed", "failed"))
        row, = report["results"]
        self.assertEqual(row["mode"], self.mode)
        self.assertGreater(row["frames"], 1)
        self.assertGreater(row["achieved_fps"], 0)
        self.assertEqual(row["queue_drops"], 0)
        camera.set_preview(True)
        self.assertIsNotNone(camera.preview_frame(0, 2))

    def test_stalled_discovery_can_be_cancelled_without_opening_a_second_camera(self):
        camera = Camera("simulated")
        self.addCleanup(camera.close)
        with tempfile.TemporaryDirectory() as directory:
            worker = Path(directory) / "slow_discovery.py"
            worker.write_text("import time\ntime.sleep(60)\n")
            with patch("rpi_web_control.mode_test.PROBE", worker), patch.object(camera, "_factory") as factory:
                self.assertEqual(camera.start_mode_test()["state"], "running")
                self.assertIn("Detecting", camera.capabilities()["error"])
                self.assertEqual(camera.cancel_mode_test()["state"], "cancelled")
                self.assertIsNone(camera._mode_test.worker)
                factory.assert_not_called()
        self.assertTrue(camera.capabilities()["available"])
