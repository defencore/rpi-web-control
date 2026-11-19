"""Only synthetic files in temporary directories are eligible in these tests."""

import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from rpi_web_control.downloads import RecordingSourceChanged
from rpi_web_control.recording_cleanup import execute_cleanup, plan_cleanup
from rpi_web_control.storage import Storage


class RecordingCleanupTests(unittest.TestCase):
    def setUp(self):
        stack = self.enterContext(ExitStack())
        root = Path(stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.media = root / "recordings"
        self.media.mkdir()
        self.state = root / "state"
        self.state.mkdir()
        self.storage = Storage([self.media], linux=False)
        self.target = stack.enter_context(self.storage.open_target(self.media))
        self.options = {"media_revision": "verified-dedicated-ssd", "state_dir": self.state}

    def recording(self, index=1, status="complete"):
        path = self.media / f"20261118T154500Z-{index:012x}"
        path.mkdir(mode=0o750)
        (path / "session.json").write_text(json.dumps({
            "schema_version": 1, "frames": 1, "status": status, "started_at": "2026-11-18T15:45:00Z",
            "ended_at": "2026-11-18T15:45:01Z", "start_sensor_ns": 123456, "backend": "picamera2",
            "settings": {"fps": 30}, "video": "video.mjpeg"}))
        (path / "frames.csv").write_text("frame_index,sensor_ns\n0,123456\n")
        (path / "video.mjpeg").write_bytes(b"synthetic video")
        return path

    def plan(self, session=None):
        return plan_cleanup(self.storage, self.target, session=session, **self.options)

    def delete(self, plan, session=None):
        return execute_cleanup(self.storage, self.target, session=session, target_id=plan["target_id"],
                               revision=plan["revision"], **self.options)

    def test_single_and_bulk_keep_foreign_unfinished_and_mixed_folders(self):
        first, second = self.recording(), self.recording(2)
        self.recording(3, "incomplete")
        self.recording(4, "recording")
        mixed = self.recording(5)
        (mixed / "unrelated.txt").write_text("keep")
        foreign = self.media / "photos"
        foreign.mkdir()
        linked = self.recording(6)
        (linked / "frames.csv").unlink()
        (linked / "frames.csv").symlink_to(mixed / "frames.csv")
        hard = self.recording(7)
        os.link(hard / "video.mjpeg", foreign / "copy")
        writable = self.recording(8)
        writable.chmod(0o770)

        initial = self.plan()
        self.assertEqual((initial["count"], initial["skipped"]), (2, 7))
        self.assertEqual(initial["bytes"], sum(f.stat().st_size for p in (first, second) for f in p.iterdir()))
        self.assertFalse(any(self.state.iterdir()))
        result = self.delete(self.plan(first.name), first.name)
        self.assertEqual(result["removed_count"], 1)
        self.assertFalse(first.exists())
        with self.assertRaises(RecordingSourceChanged):
            self.delete(initial)
        self.assertEqual(self.delete(self.plan())["removed_count"], 1)
        self.assertFalse(second.exists())
        self.assertEqual((mixed / "unrelated.txt").read_text(), "keep")
        self.assertTrue((linked / "frames.csv").is_symlink())
        self.assertEqual((foreign / "copy").read_bytes(), b"synthetic video")
        self.assertEqual(self.plan()["skipped"], 7)

    def test_shared_writable_root_prevents_cleanup(self):
        session = self.recording()
        approved = self.plan(session.name)
        self.media.chmod(0o770)
        with self.assertRaisesRegex(ValueError, "owned and writable"):
            self.plan()
        with self.assertRaises(RecordingSourceChanged):
            self.delete(approved, session.name)
        self.assertTrue(session.exists())

    def test_new_clip_changed_media_and_changed_file_reject_review(self):
        session = self.recording()
        plan = self.plan()
        self.recording(2)
        with self.assertRaises(RecordingSourceChanged):
            self.delete(plan)
        one = self.plan(session.name)
        self.options["media_revision"] = "another-physical-disk"
        with self.assertRaises(RecordingSourceChanged):
            self.delete(one, session.name)
        self.options["media_revision"] = "verified-dedicated-ssd"
        (session / "video.mjpeg").write_bytes(b"changed")
        with self.assertRaises(RecordingSourceChanged):
            self.delete(one, session.name)
        with self.assertRaises(ValueError):
            self.plan("../etc")
        self.assertTrue((session / "video.mjpeg").exists())

    def test_partial_failure_requires_new_plan_and_preserves_replacements(self):
        session = self.recording()
        original = self.plan(session.name)
        real_unlink = os.unlink

        def fail_index(path, *args, **kwargs):
            if path == "frames.csv":
                raise OSError("injected write failure")
            return real_unlink(path, *args, **kwargs)

        with patch("os.unlink", side_effect=fail_index), self.assertRaisesRegex(RecordingSourceChanged, "interrupted"):
            self.delete(original, session.name)
        self.assertEqual({p.name for p in session.iterdir()}, {"frames.csv", "session.json"})
        with self.assertRaises(RecordingSourceChanged):
            self.delete(original, session.name)
        pending = self.plan(session.name)
        self.assertLess(pending["bytes"], original["bytes"])
        (session / "video.mjpeg").write_bytes(b"a different file")
        with self.assertRaises(RecordingSourceChanged):
            self.delete(pending, session.name)
        self.assertEqual((session / "video.mjpeg").read_bytes(), b"a different file")
        (session / "video.mjpeg").unlink()
        self.assertEqual(self.delete(self.plan())["removed_count"], 1)
        self.assertFalse(session.exists())
        self.assertEqual(list((self.state / "recording-cleanup").iterdir()), [])

    def test_pinned_path_must_still_be_present(self):
        session = self.recording()
        plan = self.plan(session.name)
        moved = self.media.with_name("moved")
        self.media.rename(moved)
        self.media.mkdir()
        with self.assertRaises(RecordingSourceChanged):
            self.delete(plan, session.name)
        self.assertTrue((moved / session.name / "video.mjpeg").exists())
        self.assertEqual(list(self.media.iterdir()), [])
