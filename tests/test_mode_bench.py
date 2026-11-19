"""Fixture tests for the physical-camera diagnostic. No simulated performance results."""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import redirect_stderr
from functools import partial
from pathlib import Path
from unittest.mock import patch


path = Path(__file__).resolve().parents[1] / "tools" / "check-camera-modes.py"
spec = importlib.util.spec_from_file_location("check_camera_modes", path)
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


class Frame:
    def __init__(self, ns):
        self.sensor_ns = ns
        self.releases = 0

    def release(self):
        self.releases += 1


class Source:
    def __init__(self, timestamps, depth=8, error=None):
        self.frames = [Frame(ns) for ns in timestamps]
        self.stream = iter(self.frames)
        self.depth = depth
        self.error = error
        self.camera = self
        self.queue_drops = 0
        self.closed = False

    def start(self, settings, mode):
        self.mode = mode
        self.settings = settings
        if self.error:
            raise RuntimeError(self.error)

    def camera_configuration(self):
        return {"sensor": {"output_size": (self.mode["width"], self.mode["height"]), "bit_depth": self.depth},
                "main": {"size": (self.settings["width"], self.settings["height"]), "format": "YUV420"}}

    def next_frame(self, timeout):
        return next(self.stream, None)

    def close(self):
        self.closed = True

    def capabilities(self):
        return {"model": "fixture", "modes": [sensor(1280, 720, 100, self.depth)]}


def sensor(width, height, fps, depth):
    return {"width": width, "height": height, "max_fps": fps, "bit_depth": depth}


def wait_for_driver():
    time.sleep(30)


def exit_from_driver():
    os._exit(3)


class ModeBenchTests(unittest.TestCase):
    def setUp(self):
        self.case = next(bench.cases([sensor(1280, 720, 100, 8)]))

    def test_advertised_and_extra_720p_without_duplicate_native_240(self):
        native = [(1280, 720, 364.7), (1920, 1080, 175.47), (4056, 2160, 42.43), (4056, 3040, 30.34)]
        modes = [sensor(w, h, fps, d) for d in (8, 10) for w, h, fps in native]
        plan = list(bench.cases(modes))
        self.assertEqual([case["mode"] for case in plan if case["test"] == "advertised_max"], modes)
        self.assertEqual([row["mode"]["bit_depth"] for row in plan if row["test"] == "720p240"], [8, 10])
        self.assertEqual(len(plan), 10)
        self.assertEqual(len(list(bench.cases([sensor(1280, 720, 240, 10)]))), 1)
        high_resolution = list(bench.cases([sensor(1920, 1080, 300, 10)]))
        self.assertEqual(high_resolution[1]["output_size"], [1280, 720])

    def test_warmed_capture_uses_sensor_time_and_releases_every_request(self):
        base = 17_000_000_000_000_000
        source = Source([base + i * 10_000_000 for i in range(8)])
        result = bench.probe(self.case, .05, .02, lambda: source)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["negotiated"]["bit_depth"], 8)
        self.assertEqual(result["measurement"]["first_sensor_boottime_ns"], base + 20_000_000)
        self.assertEqual(result["measurement"]["frames"], 6)
        self.assertEqual(result["measurement"]["achieved_fps"], 100)
        self.assertEqual(result["measurement"]["gaps_over_1_5_periods"], 0)
        self.assertTrue(source.closed)
        self.assertTrue(all(frame.releases == 1 for frame in source.frames))

    def test_repeat_native_mode_at_its_planned_rates(self):
        modes = [sensor(w, h, fps, depth) for depth in (8, 10)
                 for w, h, fps in ((1280, 720, 364.70), (1920, 1080, 175.47))]
        for width, height, depth, rates in [(1920, 1080, 8, [175.47]), (1280, 720, 10, [364.70, 240.0])]:
            selected = f"{width}x{height}:{depth}"
            answers = [{"modes": modes}, *({"status": "passed"} for _ in rates)]
            with (self.subTest(selected=selected), patch.object(bench, "supervised", side_effect=answers),
                  redirect_stderr(io.StringIO())):
                report, code = bench.run(2, 1, selected)
                self.assertEqual(code, 0)
                self.assertEqual(report["mode_filter"], selected)
                self.assertEqual(report["planned"], len(rates))
                self.assertEqual([r["requested_fps"] for r in report["results"]], rates)
                for row in report["results"]:
                    self.assertEqual((row["mode"]["width"], row["mode"]["height"], row["mode"]["bit_depth"]),
                                     (width, height, depth))

    def test_unadvertised_mode_does_not_probe_or_pass(self):
        inventory = {"modes": [sensor(1920, 1080, 175.47, 10)]}
        with patch.object(bench, "supervised", return_value=inventory) as worker, redirect_stderr(io.StringIO()):
            report, code = bench.run(2, 1, "1920x1080:8")
        self.assertEqual(worker.call_count, 1)
        self.assertEqual((code, report["status"], report["planned"], report["results"]),
                         (2, "mode_unavailable", 0, []))

    def test_negotiation_mismatch_and_start_error_keep_actual_and_release(self):
        for error in (None, "Camera changed the requested stream geometry"):
            with self.subTest(error=error):
                source = Source([], depth=10, error=error)
                result = bench.probe(self.case, .01, 0, lambda: source)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["negotiated"]["bit_depth"], 10)
                self.assertEqual(result["measurement"]["frames"], 0)
                self.assertTrue(source.closed)

    def test_drops_and_incomplete_measurements_cannot_pass(self):
        tests = [
            ([0, 10_000_000, 40_000_000, 50_000_000], 1, 2),
            ([0, 10_000_000], 0, 0),
            ([0, 10_000_000, 10_000_000], 0, 0),
        ]
        for timestamps, gaps, missing in tests:
            with self.subTest(timestamps=timestamps):
                source = Source(timestamps)
                result = bench.probe(self.case, .05, 0, lambda: source)
                self.assertEqual(result["status"], "failed")
                stats = result["measurement"]
                self.assertEqual(stats["frames"], len(timestamps))
                self.assertEqual(stats["gaps_over_1_5_periods"], gaps)
                self.assertEqual(stats["estimated_missing_periods"], missing)
                self.assertTrue(source.closed)
                self.assertTrue(all(frame.releases == 1 for frame in source.frames))

    def test_busy_discovery_timeout_and_crash(self):
        def busy():
            raise RuntimeError("Camera already in use")
        with self.assertRaisesRegex(RuntimeError, "already in use"):
            bench.discover(busy)
        self.assertEqual(bench.supervised(wait_for_driver, (), .05)["status"], "timeout")
        self.assertEqual(bench.supervised(exit_from_driver, (), 2)["status"], "error")

    def test_media_locks_match_inode_and_exclude_waiters_and_unrelated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)

            def inode(name):
                node = directory / name
                node.touch()
                info = node.stat()
                return f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino}"

            media, unrelated = inode("media3"), inode("media3-backup")
            table = directory / "locks"
            table.write_text(f"1: POSIX ADVISORY WRITE 407 {media} 0 EOF\n"
                             f"1: -> POSIX ADVISORY WRITE 408 {media} 0 EOF\n"
                             f"2: POSIX ADVISORY WRITE 409 {unrelated} 0 EOF\n")
            self.assertEqual(bench.visible_media_locks(directory, table),
                             [{"device": str(directory / "media3"), "pid": 407}])
            self.assertIsNone(bench.visible_media_locks(directory, directory / "unavailable"))

    def test_discovery_diagnostics_preserve_original_error_when_tools_unavailable(self):
        error = {"status": "error", "error": "Camera unavailable: Camera __init__ sequence did not complete."}
        stderr = io.StringIO()
        with (patch.object(bench, "supervised", return_value=error),
              patch.object(bench.subprocess, "check_output", side_effect=FileNotFoundError),
              patch.object(bench, "visible_media_locks", return_value=None),
              redirect_stderr(stderr)):
            report, code = bench.run(2, 1)
        self.assertEqual(code, 2)
        self.assertEqual(report["results"], [])
        self.assertEqual(report["camera"], error)
        self.assertEqual(report["status"], "discovery_failed")
        self.assertEqual(report["camera_access"], {"controller_main_pid": None, "visible_media_locks": None})
        self.assertIn("If libcamera reports a busy pipeline", stderr.getvalue())

    def test_native_driver_output_keeps_cli_json_parseable(self):
        fixture = textwrap.dedent("""
            import os, runpy, sys, types

            def unavailable():
                os.write(1, b"Native driver diagnostic\\n")
                raise RuntimeError("Fixture camera busy")

            driver = types.ModuleType("picamera2")
            driver.Picamera2 = unavailable
            sys.modules["picamera2"] = driver
            script = sys.argv.pop()
            runpy.run_path(script, run_name="__main__")
        """)
        cli = subprocess.run([sys.executable, "-", str(path)], input=fixture, text=True,
                             cwd=path.parents[1], capture_output=True, timeout=10)
        self.assertEqual(cli.returncode, 2)
        self.assertEqual(json.loads(cli.stdout)["status"], "discovery_failed")
        self.assertIn("Native driver diagnostic", cli.stderr)

    def test_failed_case_does_not_hide_next_case_or_count_as_a_pass(self):
        inventory = {"model": "fixture", "modes": [sensor(1280, 720, 300, 8)]}
        with patch.object(bench, "supervised", side_effect=[
            inventory, {"status": "timeout", "error": "deadline"}, {"status": "passed"},
        ]), redirect_stderr(io.StringIO()):
            report, code = bench.run(1, 0)
        self.assertEqual(code, 1)
        self.assertEqual(report["passed"], 1)
        self.assertEqual(report["planned"], 2)
        self.assertEqual(len(report["results"]), 2)
        with (patch.object(bench, "supervised", return_value={"status": "error", "error": "busy"}),
              redirect_stderr(io.StringIO())):
            report, code = bench.run(1, 0)
        self.assertEqual(code, 2)
        self.assertEqual(report["results"], [])

    def test_supervised_matrix_with_injected_source(self):
        factory = partial(Source, [i * 10_000_000 for i in range(30)])
        discover = partial(bench.discover, source_factory=factory)
        probe = partial(bench.probe, source_factory=factory)
        with patch.multiple(bench, discover=discover, probe=probe), redirect_stderr(io.StringIO()):
            report, code = bench.run(.1, .02)
        self.assertEqual(code, 0)
        self.assertEqual(report["camera"]["model"], "fixture")
        self.assertEqual(report["passed"], report["planned"])
        self.assertEqual(report["results"][0]["measurement"]["frames"], 11)


if __name__ == "__main__":
    unittest.main()
