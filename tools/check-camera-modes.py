#!/usr/bin/python3
"""Measure capture-only sensor modes through the installed Pi camera adapter."""

import argparse
import datetime
import json
import math
import multiprocessing
import os
import platform
import re
import subprocess
import sys
from contextlib import closing
from pathlib import Path


def open_source():
    # SSH can execute this file on stdin without a project checkout on the Pi.
    sys.path.insert(0, "/usr/lib/rpi-web-control")
    from rpi_web_control.capture import PiSource

    return PiSource()


def discover(source_factory=open_source):
    with closing(source_factory()) as source:
        capabilities = source.capabilities()
        return {"model": capabilities["model"], "modes": capabilities["modes"]}


def cases(modes):
    for mode in modes:
        rate = float(mode["max_fps"])
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError(f"Invalid advertised rate {rate}")
        native_size = [mode["width"], mode["height"]]
        yield {"test": "advertised_max", "mode": mode, "output_size": native_size, "requested_fps": rate}
        if mode["width"] >= 1280 and mode["height"] >= 720 and rate >= 240:
            if native_size != [1280, 720] or rate != 240:
                yield {"test": "720p240", "mode": mode, "output_size": [1280, 720], "requested_fps": 240.0}


def negotiated(source):
    config = source.camera.camera_configuration()
    if not config or not config.get("sensor") or not config.get("main"):
        return None
    return {"sensor_size": list(config["sensor"]["output_size"]),
            "bit_depth": config["sensor"]["bit_depth"],
            "output_size": list(config["main"]["size"]),
            "output_format": config["main"]["format"]}


def timestamp(source, timeout):
    frame = source.next_frame(timeout)
    if frame is None:
        raise RuntimeError("Timed out waiting for a completed camera request.")
    try:
        return frame.sensor_ns
    finally:
        frame.release()


def measurement(stamps, fps):
    intervals = [b - a for a, b in zip(stamps, stamps[1:])]
    period_ns = 1e9 / fps
    out_of_order = sum(interval <= 0 for interval in intervals)
    elapsed_ns = (stamps[-1] - stamps[0]) if stamps else 0
    return {"frames": len(stamps),
            "first_sensor_boottime_ns": stamps[0] if stamps else None,
            "last_sensor_boottime_ns": stamps[-1] if stamps else None,
            "sensor_elapsed_ns": elapsed_ns,
            "achieved_fps": len(intervals) * 1e9 / elapsed_ns if elapsed_ns > 0 and not out_of_order else None,
            "largest_interval_ns": max(intervals, default=None),
            "non_increasing_intervals": out_of_order,
            "gaps_over_1_5_periods": sum(dt > 1.5 * period_ns for dt in intervals),
            "estimated_missing_periods": sum(max(0, round(dt / period_ns) - 1) for dt in intervals)}


def probe(case, seconds, warmup, source_factory=open_source, *, verify_negotiated=True):
    mode, fps = case["mode"], case["requested_fps"]
    native_size = [mode["width"], mode["height"]]
    size = case["output_size"]
    settings = {"width": size[0], "height": size[1], "fps": fps, "osd": False}
    result = {"negotiated": None, "requested_frame_duration_us": round(1e6 / fps),
              "sample_queue_drops": None}
    issues = []
    stamps = []
    stage = "open"
    try:
        with closing(source_factory()) as source:
            stage = "configure"
            try:
                source.start(settings, mode)
            except Exception:
                # Preserve the adapter's error even if no configuration is readable.
                try:
                    result["negotiated"] = negotiated(source)
                except Exception:
                    pass
                raise
            if verify_negotiated:
                result["negotiated"] = negotiated(source)
                if result["negotiated"] != {"sensor_size": native_size, "bit_depth": mode["bit_depth"],
                                            "output_size": size, "output_format": "YUV420"}:
                    raise RuntimeError("Negotiated geometry, bit depth or output format differs.")

            stage = "warmup"
            frame_timeout = max(2.0, 3 / fps)
            first = current = timestamp(source, frame_timeout)
            while current - first < warmup * 1e9:
                previous = current
                current = timestamp(source, frame_timeout)
                if current <= previous:
                    raise RuntimeError("SensorTimestamp did not advance during warmup.")

            stage = "sample"
            drops_before = source.queue_drops
            stamps.append(current)
            try:
                while stamps[-1] - stamps[0] < seconds * 1e9:
                    current = timestamp(source, frame_timeout)
                    previous = stamps[-1]
                    stamps.append(current)
                    if current <= previous:
                        raise RuntimeError("SensorTimestamp did not advance.")
            finally:
                result["sample_queue_drops"] = source.queue_drops - drops_before
            stage = "close"
    except Exception as exc:
        issues.append(f"{stage}: {exc}")

    stats = measurement(stamps, fps)
    result["measurement"] = stats
    if stats["achieved_fps"] is not None and abs(stats["achieved_fps"] / fps - 1) > 0.05:
        issues.append("Measured rate differs from the request by more than 5%.")
    if stats["gaps_over_1_5_periods"] or result["sample_queue_drops"]:
        issues.append("A sensor timestamp gap or capture queue drop occurred during sampling.")
    result.update(status="failed" if issues else "passed", issues=issues)
    return result


def child_run(send, function, args):
    with send:
        try:
            # Native camera drivers may write to fd 1. Only the parent emits JSON.
            os.dup2(2, 1)
            send.send(function(*args))
        except Exception as exc:
            send.send({"status": "error", "error": str(exc)})


def supervised(function, args, timeout):
    # A driver stall can also occur during configure, start or close. The parent
    # never initializes libcamera. Each child releases its devices at exit.
    context = multiprocessing.get_context("fork")
    receive, send = context.Pipe(duplex=False)
    child = context.Process(target=child_run, args=(send, function, args))
    with receive, send:
        child.start()
        send.close()
        try:
            if not receive.poll(timeout):
                return {"status": "timeout", "error": f"Worker exceeded the {timeout:g} s deadline."}
            try:
                return receive.recv()
            except EOFError:
                return {"status": "error", "error": "Camera worker exited without returning a result."}
        finally:
            child.join(0.2)
            if child.is_alive():
                child.terminate()
                child.join(1)
            if child.is_alive():
                child.kill()
                child.join(1)
            if not child.is_alive():
                child.close()


def read_identity(path):
    try:
        return Path(path).read_text().strip("\0\n ")
    except OSError:
        return None


def package_version(name):
    try:
        return subprocess.check_output(
            ["dpkg-query", "-W", "-f=${Version}", name],
            text=True, stderr=subprocess.DEVNULL, timeout=2).strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def visible_media_locks(devices=Path("/dev"), lock_table=Path("/proc/locks")):
    try:
        nodes = {}
        for path in devices.glob("media[0-9]*"):
            if path.name[5:].isdigit():
                info = path.stat()
                # /proc/locks identifies an inode on its filesystem, even for device nodes.
                key = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino}"
                nodes[key] = str(path)
        lines = lock_table.read_text().splitlines()
    except OSError:
        return None

    locks = []
    for line in lines:
        fields = line.split()
        if len(fields) < 6 or fields[1] == "->" or fields[5] not in nodes:
            continue
        if fields[4].isdigit() and int(fields[4]) > 0:
            locks.append({"device": nodes[fields[5]], "pid": int(fields[4])})
    return locks


def camera_access():
    try:
        pid = int(subprocess.check_output(
            ["systemctl", "show", "--property=MainPID", "--value", "rpi-web-control.service"],
            text=True, stderr=subprocess.DEVNULL, timeout=2))
    except (OSError, ValueError, subprocess.SubprocessError):
        pid = None
    return {"controller_main_pid": pid, "visible_media_locks": visible_media_locks()}


def run(seconds, warmup, mode_filter=None):
    report = {"schema_version": 1, "scope": "physical camera, YUV420 capture only",
              "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "board": read_identity("/proc/device-tree/model"), "kernel": platform.release(),
              "boot_id": read_identity("/proc/sys/kernel/random/boot_id"),
              "controller_package": package_version("rpi-web-control"),
              "picamera2_package": package_version("python3-picamera2"),
              "libcamera_python_package": package_version("python3-libcamera"),
              "sample_seconds": seconds,
              "warmup_seconds": warmup, "results": []}
    if mode_filter is not None:
        report["mode_filter"] = mode_filter
    inventory = supervised(discover, (), 30)
    report["camera"] = inventory
    if "error" in inventory:
        report["status"] = "discovery_failed"
        access = report["camera_access"] = camera_access()
        print("Camera discovery failed before any mode was tested.", file=sys.stderr)
        for lock in access["visible_media_locks"] or []:
            print(f"Media device lock: {lock['device']} PID {lock['pid']}", file=sys.stderr)
        if access["controller_main_pid"]:
            print(f"Controller MainPID: {access['controller_main_pid']}", file=sys.stderr)
        print("If libcamera reports a busy pipeline, use fuser -v on the /dev/media paths in its log. "
              "Finish the active camera operation and retry.", file=sys.stderr)
        return report, 2
    try:
        modes = inventory["modes"]
        if mode_filter is not None:
            modes = [m for m in modes
                     if f"{m['width']}x{m['height']}:{m['bit_depth']}" == mode_filter]
            if not modes:
                report.update(status="mode_unavailable", planned=0,
                              error=f"No advertised sensor mode matches {mode_filter}.")
                print(report["error"], file=sys.stderr)
                return report, 2
        plan = list(cases(modes))
    except (KeyError, TypeError, ValueError) as exc:
        report.update(status="discovery_failed", error=f"Invalid sensor mode list: {exc}")
        return report, 2
    if not plan:
        report.update(status="discovery_failed", error="Camera advertised no modes.")
        return report, 2
    report["planned"] = len(plan)
    try:
        for case in plan:
            mode = case["mode"]
            width, height = case["output_size"]
            print(f"Testing {width}x{height} at {case['requested_fps']:g} fps "
                  f"on {mode['width']}x{mode['height']} {mode['bit_depth']}-bit", file=sys.stderr, flush=True)
            result = supervised(probe, (case, seconds, warmup), seconds + warmup + 20)
            report["results"].append({**case, **result})
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        return report, 130
    passed = sum(row["status"] == "passed" for row in report["results"])
    report["passed"] = passed
    report["status"] = "passed" if passed == len(plan) else "failed"
    return report, 0 if passed == len(plan) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", metavar="WIDTHxHEIGHT:BITS",
                        help="measure only this exact native sensor mode, e.g. 1920x1080:8")
    parser.add_argument("--seconds", type=float, default=2, help="sample length per test (0.1 to 300 seconds)")
    parser.add_argument("--warmup", type=float, default=1, help="warmup per test (0 to 60 seconds)")
    parser.add_argument("--web-probe", nargs=2, metavar=("BACKEND", "MODE_JSON"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 0.1 <= args.seconds <= 300 or not 0 <= args.warmup <= 60:
        parser.error("Require --seconds between 0.1 and 300 and --warmup between 0 and 60.")
    if args.mode is not None and not re.fullmatch(r"[1-9][0-9]*x[1-9][0-9]*:[1-9][0-9]*", args.mode):
        parser.error("--mode requires WIDTHxHEIGHT:BITS, e.g. 1920x1080:8.")
    if args.web_probe:
        backend, encoded_mode = args.web_probe
        if backend not in ("picamera2", "simulated"):
            parser.error("Unknown camera backend.")
        # A fresh interpreter isolates libcamera from the running controller.
        # Reserve stdout for the single result before importing the native driver.
        with os.fdopen(os.dup(1), "w") as output:
            os.dup2(2, 1)
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from rpi_web_control.capture import PiSource, SimSource

            mode = json.loads(encoded_mode)
            simulated = backend == "simulated"
            factory = SimSource if simulated else PiSource
            if mode is None:
                try:
                    with closing(factory()) as source:
                        result = source.capabilities()
                except Exception as exc:
                    result = {"available": False, "model": "", "properties": {}, "modes": [], "error": str(exc)}
            else:
                case = next(cases([mode]))
                result = probe(case, args.seconds, args.warmup, factory, verify_negotiated=not simulated)
            print(json.dumps(result, allow_nan=False), file=output)
        return 0
    report, code = run(args.seconds, args.warmup, args.mode)
    print(json.dumps(report, indent=2, allow_nan=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
