import csv
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rpi_web_control.application import Application, Conflict
from rpi_web_control.capture import boot_ns


def wait_for(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for camera state")


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name).resolve()
        self.root = self.state / "recordings"
        self.root.mkdir()
        self.app = Application(self.state, [self.root], "simulated")
        self.addCleanup(self.app.close)

    def test_recording_owners_and_configuration(self):
        self.app.configure({"camera": {"width": 320, "height": 240, "osd": True}})
        self.app.record("manual", True)
        self.app.trigger("contact", True)
        session = self.app.camera.status()["session"]
        self.app.trigger("contact", False)
        self.assertEqual(self.app.status()["recording_sources"], ["manual"])
        self.assertEqual(self.app.camera.status()["session"], session)
        self.app.trigger("contact", True)
        self.app.record("manual", False)
        self.assertEqual(self.app.status()["recording_sources"], ["trigger:contact"])
        with self.assertRaises(Conflict):
            self.app.configure({"camera": {"fps": 24}})
        wait_for(lambda: self.app.camera.status()["frames"] > 1)
        self.assertEqual(session, self.app.camera.status()["session"])
        self.app.trigger("contact", False)
        self.assertFalse(self.app.camera.status()["active"])
        self.assertTrue((Path(session) / "frames.csv").is_file())

    def test_pause_releases_trigger_owners_and_applies_edits_on_resume(self):
        sample = {"id": "timer", "type": "timer", "interval_s": 90, "duration_s": 1}
        self.app.configure({"triggers": [sample]})
        self.app.record("manual", True)
        self.app.trigger("timer", True)
        session = self.app.camera.status()["session"]
        self.app.pause_recording_triggers(True)
        self.assertEqual(self.app.status()["recording_sources"], ["manual"])
        self.assertEqual(self.app.camera.status()["session"], session)
        self.app.trigger("timer", True)
        self.app.configure({"triggers": [{**sample, "id": "updated"}, {**sample, "enabled": False}]})
        paused = self.app.status()
        self.assertTrue(paused["triggers_paused"])
        self.assertEqual([(r["id"], r["state"]) for r in paused["triggers"]],
                         [("updated", "paused"), ("timer", "disabled")])
        self.assertEqual(self.app.triggers.status(), [])
        self.app.pause_recording_triggers(True, stop_manual=True)
        self.assertEqual(self.app.status()["recording_sources"], [])
        self.assertFalse(self.app.camera.status()["recording"])
        self.app.pause_recording_triggers(False)
        wait_for(lambda: self.app.triggers.status()[0]["state"] == "ready")
        self.assertFalse(self.app.status()["triggers_paused"])
        self.assertEqual(self.app.triggers.status()[0]["id"], "updated")

    def test_stop_all_blocks_recovery_even_when_file_close_fails(self):
        self.app.trigger("Button", True)
        close = self.app.camera.stop_recording

        def fail_close():
            close()
            raise OSError("Could not finalize recording")

        with patch.object(self.app.camera, "stop_recording", side_effect=fail_close):
            with self.assertRaisesRegex(OSError, "Could not finalize"):
                self.app.pause_recording_triggers(True, stop_manual=True)
        self.app.trigger("Button", True)
        result = self.app.status()
        self.assertTrue(result["triggers_paused"])
        self.assertFalse(result["camera"]["recording"])
        self.assertEqual(result["recording_sources"], [])

    def test_cleanup_requires_host_and_pinned_external_device_to_match(self):
        disk, = self.app.usb.status()["devices"]
        ready = self.app.apply_nvme({"action": "mount", "device": disk["device"], "revision": disk["revision"],
                                     "volume": disk["volumes"][0]["device"]}, medium="usb")
        host_disk, = ready["devices"]
        volume, = host_disk["volumes"]
        with self.app.storage.open_target(self.app.usb_root, create=False) as target:
            number = os.fstat(target.fd).st_dev
            target.identity["mountpoint"] = ready["mountpoint"]
            helper = SimpleNamespace(status=lambda: ready)
            with patch.dict(self.app.media_clients, {"usb": helper}):
                volume["devno"] = "99999:99999"
                with self.assertRaisesRegex(ValueError, "verified external device"):
                    self.app._cleanup_disk(target)
                volume["devno"] = f"{os.major(number)}:{os.minor(number)}"
                with patch("rpi_web_control.application.os.stat", return_value=SimpleNamespace(st_dev=number)):
                    with self.assertRaisesRegex(ValueError, "verified external device"):
                        self.app._cleanup_disk(target)
                with patch("rpi_web_control.application.os.stat", return_value=SimpleNamespace(st_dev=number + 1)):
                    self.assertEqual(self.app._cleanup_disk(target), ("usb", host_disk))
                    target.identity["mountpoint"] = "/"
                    with self.assertRaisesRegex(ValueError, "verified external device"):
                        self.app._cleanup_disk(target)

    def test_storage_limits_and_persistence(self):
        self.app.configure({"storage": {"directory": str(self.root / "day")}})
        self.assertTrue((self.root / "day").is_dir())
        (self.root / "outside").symlink_to(self.state)
        with self.assertRaises(ValueError):
            self.app.configure({"storage": {"directory": str(self.root / "outside" / "unsafe")}})
        self.assertEqual(self.app.config, self.app.store.load())
        self.assertEqual(self.app.config["storage"]["directory"], str(self.root / "day"))

    def test_record_to_nvme_and_download_after_switching_to_another_destination(self):
        disk = self.app.nvme_status()["devices"][0]
        operation = {"action": "mount", "device": disk["device"], "revision": disk["revision"],
                     "volume": disk["volumes"][0]["device"]}
        with self.assertRaises(ValueError):
            self.app.configure({"storage": {"directory": str(self.app.nvme_root)}})
        self.assertTrue(self.app.apply_nvme(operation)["ready"])
        self.assertEqual(self.app.config["storage"]["directory"], str(self.root))
        self.app.configure({"storage": {"directory": str(self.app.nvme_root)}})
        self.app.record("manual", True)
        with self.assertRaises(Conflict):
            self.app.apply_nvme(operation)
        wait_for(lambda: self.app.camera.status()["frames"] >= 1)
        self.app.record("manual", False)
        self.app.configure({"storage": {"directory": str(self.root)}})
        self.assertEqual(self.app.recordings()["sessions"], [])
        catalog = self.app.recordings(source="nvme")
        session, = catalog["sessions"]
        self.assertEqual({f["name"] for f in session["files"]}, {"video.mjpeg", "frames.csv", "session.json"})
        with self.app.recording_file(session["name"], "video.mjpeg", source="nvme",
                                     target_id=catalog["target_id"]) as video:
            self.assertEqual(video.read(2), b"\xff\xd8")

    def test_setup_serializes_recording_reboot_and_other_disk_changes(self):
        started, finish = threading.Event(), threading.Event()
        def slow_mount(body):
            started.set()
            finish.wait(5)
            return {"ready": True}
        with patch.object(self.app.nvme, "apply", side_effect=slow_mount):
            worker = threading.Thread(target=self.app.apply_nvme, args=({},))
            worker.start()
            try:
                self.assertTrue(started.wait(2))
                self.assertTrue(self.app.nvme_status()["busy"])
                with self.assertRaises(Conflict):
                    self.app.record("manual", True)
                with self.assertRaises(Conflict):
                    self.app.reboot({"confirm": True})
                with self.assertRaises(Conflict):
                    self.app.apply_nvme({})
            finally:
                finish.set()
                worker.join(5)
        self.assertFalse(self.app.nvme_status()["busy"])

    def test_previous_package_configuration_is_pinned_on_first_use(self):
        directory = self.root / "legacy"
        self.app.store.path.write_text(json.dumps({"storage": {"directory": str(directory)}}))
        upgraded = Application(self.state, [self.root], "simulated")
        self.addCleanup(upgraded.close)
        self.assertIsNone(upgraded.store.storage_identity)
        upgraded.record("manual", True)
        wait_for(lambda: upgraded.camera.status()["frames"] >= 1)
        upgraded.record("manual", False)
        saved = json.loads(upgraded.store.path.read_text())
        self.assertEqual(saved["_storage_identity"], upgraded.storage.identity(directory))
        self.assertNotIn("_storage_identity", upgraded.status()["config"])
        with self.assertRaises(ValueError):
            upgraded.configure({"_storage_identity": {}})

    def test_persisted_depth_selects_the_recorded_sensor_mode(self):
        legacy = {"width": 320, "height": 240, "fps": 30, "osd": False}
        self.app.store.path.write_text(json.dumps({"camera": legacy}))
        self.assertEqual(self.app.store.load()["camera"], {**legacy, "sensor_bit_depth": 0})
        self.app.configure({"camera": {**legacy, "sensor_bit_depth": 8}})
        restored = Application(self.state, [self.root], "simulated")
        self.addCleanup(restored.close)
        restored.record("manual", True)
        wait_for(lambda: restored.camera.status()["frames"] > 0)
        status = restored.camera.status()
        restored.record("manual", False)
        manifest = json.loads((Path(status["session"]) / "session.json").read_text())
        self.assertEqual(manifest["settings"], {**legacy, "sensor_bit_depth": 8})
        self.assertEqual(status["sensor_mode"], {"width": 640, "height": 480, "max_fps": 240, "bit_depth": 8})
        self.assertEqual(manifest["sensor_mode"], status["sensor_mode"])

    def test_scheduled_device_reboot_blocks_a_new_recording(self):
        # Stub the privileged call. The integration path must never reboot this host.
        with patch.object(self.app.firmware, "reboot", return_value={"scheduled": True, "simulated": False}):
            self.app.reboot({"confirm": True})
        with self.assertRaises(Conflict):
            self.app.record("manual", True)
        self.assertFalse(self.app.camera.status()["recording"])

    def test_retired_root_allows_reselection_without_redirecting_recordings(self):
        self.app.configure({"storage": {"directory": str(self.root / "previous")}})
        new_root = self.state / "new-volume"
        new_root.mkdir()
        updated = Application(self.state, [new_root], "simulated")
        self.addCleanup(updated.close)
        self.assertIn("allowed storage root", updated.status()["storage"]["error"])
        with self.assertRaises(ValueError):
            updated.record("manual", True)
        self.assertEqual(list(new_root.iterdir()), [])
        updated.configure({"storage": {"directory": str(new_root)}})
        updated.record("manual", True)
        wait_for(lambda: updated.camera.status()["frames"] > 0)
        updated.record("manual", False)
        self.assertTrue(Path(updated.camera.status()["session"]).is_relative_to(new_root))

    def test_camera_change_rolls_back_if_persistence_fails(self):
        old = self.app.camera.status()["settings"]
        with patch.object(self.app.store, "save", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.app.configure({"camera": {"fps": 25}})
        self.assertEqual(self.app.camera.status()["settings"], old)
        self.assertEqual(self.app.config["camera"], old)

    def test_timer_starts_and_removal_releases_recording(self):
        self.app.configure({"triggers": [{"id": "periodic", "type": "timer",
                                         "interval_s": 1.0, "duration_s": 0.8}]})
        wait_for(lambda: self.app.camera.status()["frames"] > 1)
        self.assertIn("trigger:periodic", self.app.status()["recording_sources"])
        self.app.configure({"triggers": []})
        self.assertFalse(self.app.camera.status()["recording"])
        self.assertEqual(self.app.status()["recording_sources"], [])

    def test_active_source_recovers_from_capture_failure(self):
        self.app.trigger("held", True)
        first = self.app.camera.status()["session"]
        self.app.camera.stop_recording()
        self.assertIn("trigger:held", self.app.status()["recording_sources"])
        wait_for(lambda: self.app.camera.status()["recording"], seconds=5)
        self.assertNotEqual(first, self.app.camera.status()["session"])
        self.app.trigger("held", False)
        self.assertFalse(self.app.camera.status()["active"])

    def test_clock_change_blocked_during_recording_recovery_and_reboot(self):
        body = {"confirm": True, "browser_realtime_ms": time.time_ns() // 1_000_000}
        self.app.trigger("held", True)
        with self.app.record_lock:
            self.app.camera.stop_recording()
            with self.assertRaises(Conflict):
                self.app.set_clock(body)
        self.app.trigger("held", False)
        self.app.reboot_until = time.monotonic() + 60
        with self.assertRaises(Conflict):
            self.app.set_clock(body)

    def test_recovering_recording_keeps_camera_and_destination(self):
        self.app.trigger("held", True)
        previous = self.app.config
        directory = self.root / "another"
        directory.mkdir()
        with self.app.config_lock, self.app.record_lock:
            self.app.camera.stop_recording()
            for change in ({"camera": {"fps": 24}}, {"storage": {"directory": str(directory)}}):
                with self.subTest(change=change), self.assertRaises(Conflict):
                    self.app.configure(change)
            self.assertEqual(self.app.config, previous)
        self.app.trigger("held", False)
        self.app.configure({"camera": {"fps": 24}, "storage": {"directory": str(directory)}})
        self.assertEqual(self.app.config["storage"]["directory"], str(directory))

    def test_new_trigger_rules_reject_aliases_of_the_same_serial_port(self):
        rules = [{"id": "serial", "type": "uart", "device": "/dev/ttyAMA0"},
                 {"id": "gate", "type": "gpio", "pin": 17,
                  "stop": {"type": "uart", "device": "/dev/serial0"}}]
        with patch("rpi_web_control.triggers.realpath", return_value="/dev/ttyAMA0"):
            with self.assertRaisesRegex(ValueError, "cannot share a uart input"):
                self.app.configure({"triggers": rules})
            self.assertEqual(self.app.config["triggers"], [])
            self.app.store.save({**self.app.config, "triggers": rules}, storage_identity=None)
            restored = self.app.store.load()
            self.assertEqual(restored["triggers"][1]["stop"]["device"], "/dev/serial0")

    def test_simulated_clock_correction_timestamps_the_next_recording(self):
        target_ms = 2_000_000_000_000
        self.app.set_clock({"confirm": True, "browser_realtime_ms": target_ms})
        sensor_start = boot_ns()
        mono_start = time.monotonic_ns()
        self.app.record("manual", True)
        directory = Path(self.app.camera.status()["session"])
        wait_for(lambda: self.app.camera.status()["frames"] >= 2)
        self.app.record("manual", False)
        mono_end = time.monotonic_ns()
        sensor_end = boot_ns()
        device_end_ns = self.app.clock.now_ns()

        manifest = json.loads((directory / "session.json").read_text())
        start = datetime.fromisoformat(manifest["started_at"])
        end = datetime.fromisoformat(manifest["ended_at"])
        target = datetime.fromtimestamp(target_ms // 1000, timezone.utc)
        self.assertTrue(directory.name.startswith(start.strftime("%Y%m%dT%H%M%SZ-")))
        self.assertLessEqual(target, start)
        self.assertLessEqual(start, end)
        self.assertLessEqual(end, target + timedelta(microseconds=(device_end_ns - target_ms * 1_000_000) // 1000))
        with (directory / "frames.csv").open() as file:
            rows = list(csv.DictReader(file))
        self.assertGreaterEqual(len(rows), 2)
        for row in rows:
            self.assertTrue(target_ms * 1_000_000 <= int(row["realtime_ns"]) <= device_end_ns)
            self.assertTrue(sensor_start <= int(row["sensor_ns"]) <= sensor_end)
            self.assertTrue(mono_start <= int(row["monotonic_ns"]) <= mono_end)

    def test_recording_admission_locked_through_clock_update(self):
        body = {"confirm": True, "browser_realtime_ms": time.time_ns() // 1_000_000}
        started, finish = threading.Event(), threading.Event()

        def slow_set_time(browser_realtime_ms):
            started.set()
            finish.wait(5)

        with patch.object(self.app.clock, "set_time", side_effect=slow_set_time):
            worker = threading.Thread(target=self.app.set_clock, args=(body,))
            worker.start()
            try:
                self.assertTrue(started.wait(2))
                acquired = self.app.record_lock.acquire(blocking=False)
                if acquired:
                    self.app.record_lock.release()
                self.assertFalse(acquired)
            finally:
                finish.set()
                worker.join(5)
