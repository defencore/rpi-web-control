"""Validate completed-session discovery and descriptor-based access on removable media."""
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from rpi_web_control.downloads import RecordingSourceChanged, list_recordings, open_recording_archive, open_recording_file
from rpi_web_control.storage import Storage


class CatalogTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        parent = Path(temp.name).resolve()
        self.media = parent / "recordings"
        self.media.mkdir()
        self.storage = Storage([self.media], linux=False)
        self.identity = self.storage.identity(self.media)

    def session(self, index=1, status="complete"):
        # This matches the original schema, which predates the sensor_mode field.
        directory = self.media / f"20261118T154500Z-{index:012x}"
        directory.mkdir()
        manifest = {"schema_version": 1, "status": status, "frames": 3,
                    "started_at": "2026-11-18T15:45:00+00:00", "video": "video.y4m"}
        (directory / "session.json").write_text(json.dumps(manifest))
        (directory / "frames.csv").write_bytes(b"frame_index,sensor_ns\n")
        (directory / "video.y4m").write_bytes(b"test recording")
        return directory

    def listing(self, **params):
        return list_recordings(self.storage, self.media, self.identity, **params)

    def open(self, session, name, **params):
        return open_recording_file(self.storage, self.media, self.identity, session, name, **params)

    def archive(self, session, **params):
        return open_recording_archive(self.storage, self.media, self.identity, session, **params)

    def test_archive_keeps_original_fds_and_streams_zip64(self):
        session = self.session()
        clip = session / "video.y4m"
        clip.write_bytes(b"x" * 350_000)
        expected = {file.name: file.read_bytes() for file in session.iterdir()}
        (session / "other").write_text("not part of the recording")
        with patch.object(zipfile, "ZIP64_LIMIT", 256), self.archive(session.name) as chunks:
            clip.rename(session / "moved")
            clip.write_text("replacement")
            parts = list(chunks)
            self.assertLess(max(map(len, parts)), 140_000)
            with zipfile.ZipFile(io.BytesIO(b"".join(parts))) as bundle:
                self.assertEqual(set(bundle.namelist()), set(expected))
                self.assertEqual(bundle.getinfo("video.y4m").extract_version, 45)
                for name, content in expected.items():
                    self.assertEqual(bundle.getinfo(name).compress_type, zipfile.ZIP_STORED)
                    self.assertEqual(bundle.read(name), content)

    def test_archive_only_accepts_completed_stable_recordings(self):
        for index, status in enumerate(("incomplete", "recording")):
            session = self.session(index, status)
            with self.assertRaises(FileNotFoundError), self.archive(session.name):
                pass
        session = self.session(2)
        with self.assertRaises(RecordingSourceChanged), self.archive(session.name, target_id="stale"):
            pass
        video = session / "video.y4m"
        video.write_bytes(b"x" * 300_000)
        with self.archive(session.name) as chunks:
            self.assertTrue(next(chunks))
            video.write_bytes(b"")
            with self.assertRaisesRegex(OSError, "changed"):
                list(chunks)

    def test_completed_sessions_paging_and_stable_targets(self):
        first, second, third = [self.session(n) for n in (1, 2, 3)]
        self.session(4, "incomplete")
        self.session(5, "recording")
        with patch("rpi_web_control.downloads._PAGE_SIZE", 3):
            page = self.listing(source="nvme")
            self.assertEqual([row["name"] for row in page["sessions"]], [third.name])
            self.assertEqual(page["next_cursor"], third.name)
            row = page["sessions"][0]
            self.assertEqual(row["frames"], 3)
            self.assertEqual([f["name"] for f in row["files"]], ["video.y4m", "frames.csv", "session.json"])
            for f in row["files"]:
                self.assertEqual(f["size_bytes"], (third / f["name"]).stat().st_size)
                self.assertIn(f"?source=nvme&target_id={page['target_id']}", f["url"])
                with self.open(third.name, f["name"], target_id=page["target_id"]) as body:
                    self.assertEqual(body.read(), (third / f["name"]).read_bytes())
            trailing = self.listing(cursor=page["next_cursor"], target_id=page["target_id"])
            self.assertIsNone(trailing["next_cursor"])
            self.assertEqual([row["name"] for row in trailing["sessions"]], [second.name, first.name])
        new_directory = self.media / "other"
        new_directory.mkdir()
        with self.assertRaises(RecordingSourceChanged):
            list_recordings(self.storage, new_directory, self.identity, target_id=page["target_id"])
        with self.assertRaises(RecordingSourceChanged):
            self.open(third.name, "video.y4m", target_id="changed")

    def test_unsafe_files_and_malformed_manifests_are_not_exposed(self):
        secret = self.media.parent / "private.key"
        secret.write_bytes(b"private")
        for index, kind in enumerate(["symlink", "hardlink", "fifo", "manifest", "oversize", "other-video"]):
            with self.subTest(kind=kind):
                session = self.session(index)
                target = session / "video.y4m"
                if kind in ("symlink", "hardlink", "fifo"):
                    target.unlink()
                    if kind == "symlink":
                        target.symlink_to(secret)
                    elif kind == "hardlink":
                        os.link(secret, target)
                    else:
                        os.mkfifo(target)
                elif kind == "manifest":
                    (session / "session.json").write_text("[]")
                elif kind == "oversize":
                    with (session / "session.json").open("ab") as data:
                        data.write(b" " * 65536)
                else:
                    manifest = json.loads((session / "session.json").read_text())
                    manifest["video"] = "../private.key"
                    (session / "session.json").write_text(json.dumps(manifest))
                with self.assertRaises(FileNotFoundError):
                    self.open(session.name, "video.y4m")
                with self.assertRaises(FileNotFoundError), self.archive(session.name):
                    pass
                self.assertEqual(self.listing()["sessions"], [])

    def test_traversal_replaced_folders_and_missing_targets(self):
        session = self.session()
        with self.assertRaises(FileNotFoundError):
            self.open(session.name, "../private.key")
        with self.assertRaises(FileNotFoundError):
            self.open("..", "frames.csv")
        with self.assertRaises(ValueError):
            self.listing(cursor="../")
        with self.assertRaises(FileNotFoundError):
            list_recordings(self.storage, self.media / "missing", self.identity)
        self.assertFalse((self.media / "missing").exists())
        with self.open(session.name, "video.y4m") as handle:
            outside = self.media.parent / "moved"
            session.rename(outside)
            session.symlink_to(outside, target_is_directory=True)
            self.assertEqual(handle.read(), b"test recording")
            self.assertEqual(self.listing()["sessions"], [])
            with self.assertRaises(FileNotFoundError):
                self.open(session.name, "video.y4m")

    def test_session_and_payload_must_stay_on_the_target_mount(self):
        session = self.session()
        with patch.object(self.storage, "fd_mount_id", side_effect=["root", "submount"]):
            with self.assertRaises(FileNotFoundError):
                self.open(session.name, "video.y4m")
        with patch.object(self.storage, "fd_mount_id", side_effect=["root", "root", "root", "submount"]):
            with self.assertRaises(FileNotFoundError):
                self.open(session.name, "video.y4m")
