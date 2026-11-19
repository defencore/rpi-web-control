import csv
import io
import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from rpi_web_control.capture import SimFrame, SimSource
from rpi_web_control.recording import Recording
from tools.render_recording import Telemetry, parser, render


class TelemetryTests(unittest.TestCase):
    def test_holds_only_previous_fresh_samples(self):
        source = io.StringIO("clock,voltage,boot_id\n10,1.2,abcd\n10,1.3,abcd\n25,1.6,abcd\n")
        data = Telemetry(source, "clock", ["voltage=Voltage"], "ab-cd")
        self.assertEqual(data.lines(8, 10), ["Voltage: --"])
        self.assertEqual(data.lines(10, 10), ["Voltage: 1.3"])
        self.assertEqual(data.lines(20, 10), ["Voltage: 1.3"])
        self.assertEqual(data.lines(23, 10), ["Voltage: --"])
        self.assertEqual(data.lines(27, 10), ["Voltage: 1.6"])
        with self.assertRaisesRegex(ValueError, "backwards"):
            data.lines(26, 10)

    def test_rejects_other_boot_and_unordered_data(self):
        with self.assertRaisesRegex(ValueError, "boot ID"):
            Telemetry(io.StringIO("t,k,boot_id\n1,k,abcd"), "t", [], "1234")
        data = Telemetry(io.StringIO("t,k\n10,a\n2,b"), "t", [], None)
        with self.assertRaisesRegex(ValueError, "decrease"):
            data.lines(10, 10)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg suite unavailable")
class ExportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def recording(self, fps, *, gap=False, height=160):
        settings = {"width": 320, "height": height, "fps": fps, "osd": False}
        source = SimSource()
        source.start(settings, None)
        clip = Recording(self.root, settings, "simulated", "Test", "manual")
        for i in range(4):
            frame = SimFrame(source)
            period = round(1e9 / fps)
            frame.sensor_ns = clip.start_sensor_ns + period * (i + (1 if gap and i > 1 else 0))
            clip.write(frame, frame.jpeg(False) if fps <= 60 else None)
        clip.close()
        return clip.path

    def test_single_timestamp_line_fits_minimum_capture_height(self):
        session = self.recording(240, height=16)
        output = self.root / "narrow.mp4"
        self.assertEqual(render(parser().parse_args([str(session), str(output)]))["frames"], 4)

    def test_render_both_formats_and_pixel_overlay(self):
        for fps in (30, 240):
            with self.subTest(fps=fps):
                session = self.recording(fps)
                out = self.root / f"capture-{fps}.mp4"
                with (session / "frames.csv").open() as index:
                    stamps = [int(row["sensor_ns"]) for row in csv.DictReader(index)]
                telemetry = self.root / "telemetry.csv"
                telemetry.write_text(f"sensor_ns,rpm\n{stamps[0]},1000\n{stamps[2]},1500\n")
                args = parser().parse_args([str(session), str(out), "--telemetry", str(telemetry),
                                            "--clock", "sensor_ns", "--text", "Drive test", "--field", "rpm=RPM"])
                summary = render(args)
                self.assertEqual(summary["frames"], 4)
                self.assertEqual(summary["output_fps"], str(fps))
                probe = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                                 "-show_entries", "frame=best_effort_timestamp:stream=time_base",
                                                 "-of", "json", out], text=True)
                timestamps = json.loads(probe)
                self.assertEqual(timestamps["streams"][0]["time_base"], f"1/{fps}")
                self.assertEqual([f["best_effort_timestamp"] for f in timestamps["frames"]], [0, 1, 2, 3])
                pixels = subprocess.check_output(["ffmpeg", "-v", "error", "-i", out, "-frames:v", "1",
                                                  "-f", "rawvideo", "-pix_fmt", "gray", "-"])
                # The new bottom band has black background and visible white glyphs.
                footer = pixels[-320 * 16:]
                self.assertGreater(footer.count(0), 1000)
                self.assertGreater(sum(value > 190 for value in footer), 10)

    def test_reject_or_compact_capture_gaps_and_protect_existing_output(self):
        session = self.recording(240, gap=True)
        out = self.root / "out.mp4"
        args = parser().parse_args([str(session), str(out), "--fps", "30000/1001", "--gap-policy", "reject"])
        with self.assertRaisesRegex(ValueError, "cadence gap"):
            render(args)
        self.assertFalse(out.exists())
        args.gap_policy = "compact"
        self.assertEqual(render(args)["intervals_over_1_5_periods"], 1)
        self.assertGreater(out.stat().st_size, 0)
        with self.assertRaises(FileExistsError):
            render(args)

    def test_sensor_timed_zip_preserves_jitter_and_gaps_for_both_formats(self):
        filters = subprocess.check_output(["ffmpeg", "-hide_banner", "-filters"], text=True)
        if not any("fsync" in line.split()[:2] for line in filters.splitlines()):
            self.skipTest("FFmpeg fsync filter unavailable")
        for fps in (30, 240):
            with self.subTest(fps=fps):
                session = self.recording(fps, gap=True)
                index = session / "frames.csv"
                with index.open() as stream:
                    reader = csv.DictReader(stream)
                    rows = list(reader)
                rows[2]["sensor_ns"] = str(int(rows[2]["sensor_ns"]) + 123456)
                with index.open("w", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=reader.fieldnames)
                    writer.writeheader()
                    writer.writerows(rows)
                bundle = self.root / (session.name + ".zip")
                with zipfile.ZipFile(bundle, "w") as archive:
                    for file in session.iterdir():
                        archive.write(file, file.name)
                out = bundle.with_suffix(".mp4")
                summary = render(parser().parse_args([str(bundle), str(out)]))
                self.assertEqual((summary["timing"], summary["frames"], summary["intervals_over_1_5_periods"]),
                                 ("sensor", 4, 1))
                result = json.loads(subprocess.check_output([
                    "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                    "frame=best_effort_timestamp:stream=time_base", "-of", "json", out]))
                self.assertEqual(result["streams"][0]["time_base"], "1/1000000")
                first = int(rows[0]["sensor_ns"])
                expected = [(int(row["sensor_ns"]) - first + 500) // 1000 for row in rows]
                self.assertEqual([frame["best_effort_timestamp"] for frame in result["frames"]], expected)

    def test_zip_rejects_unexpected_member_without_publishing_output(self):
        bundle = self.root / "invalid.zip"
        with zipfile.ZipFile(bundle, "w") as archive:
            for name in ("session.json", "video.mjpeg", "frames.csv", "../outside"):
                archive.writestr(name, b"")
        out = self.root / "invalid.mp4"
        with self.assertRaisesRegex(ValueError, "ZIP root"):
            render(parser().parse_args([str(bundle), str(out)]))
        self.assertFalse(out.exists())

    def test_detects_corrupt_index_and_discards_partial_output(self):
        session = self.recording(240)
        with (session / "video.y4m").open("ab") as stream:
            stream.write(b"unindexed")
        out = self.root / "corrupt.mp4"
        with self.assertRaisesRegex(ValueError, "length"):
            render(parser().parse_args([str(session), str(out)]))
        self.assertFalse(out.exists())
