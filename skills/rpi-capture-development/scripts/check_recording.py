"""Check the frame index, video boundaries, and measured cadence of an exported session."""

import argparse
import csv
import json
import math
import os
import sys
from pathlib import Path


def y4m_payload_size(video):
    fields = video.readline(512).split()
    if not fields or fields[0] != b"YUV4MPEG2":
        raise ValueError("Invalid Y4M stream header.")
    properties = {field[:1]: field[1:] for field in fields[1:]}
    width, height = int(properties[b"W"]), int(properties[b"H"])
    return width * height * 3 // 2


def check_session(directory, fps):
    with (directory / "session.json").open(encoding="utf-8") as stream:
        manifest = json.load(stream)
    if manifest.get("status") != "complete":
        raise ValueError(f"Session is not marked complete: {manifest.get('status')}.")
    videos = [p for p in (directory / "video.y4m", directory / "video.mjpeg") if p.is_file()]
    if len(videos) != 1:
        raise ValueError("Expected exactly one video.y4m or video.mjpeg file.")
    path = videos[0]
    frames = gaps = largest_interval = 0
    first_sensor = previous_sensor = None
    with path.open("rb") as video, (directory / "frames.csv").open(newline="") as index:
        length = os.fstat(video.fileno()).st_size
        raw_size = y4m_payload_size(video) if path.suffix == ".y4m" else None
        for row in csv.DictReader(index):
            frame, offset, size, sensor = (int(row[key]) for key in (
                "frame_index", "offset", "size", "sensor_ns"
            ))
            for key in ("monotonic_ns", "realtime_ns", "clock_sample_ns"):
                if int(row[key]) < 0:
                    raise ValueError(f"Negative {key} at frame {frames}.")
            if raw_size is not None:
                if video.readline(64) != b"FRAME\n" or size != raw_size:
                    raise ValueError(f"Invalid Y4M header or payload size at frame {frames}.")
            if frame != frames or size <= 0 or video.tell() != offset or offset + size > length:
                raise ValueError(f"Invalid index or file boundary at frame {frames}.")
            if raw_size is None:
                if size < 4 or video.read(2) != b"\xff\xd8":
                    raise ValueError(f"Missing JPEG start marker at frame {frames}.")
                video.seek(offset + size - 2)
                if video.read(2) != b"\xff\xd9":
                    raise ValueError(f"Missing JPEG end marker at frame {frames}.")
            else:
                video.seek(offset + size)
            if first_sensor is None:
                first_sensor = sensor
            if previous_sensor is not None:
                interval = sensor - previous_sensor
                if interval <= 0:
                    raise ValueError(f"Sensor time did not advance at frame {frames}.")
                largest_interval = max(largest_interval, interval)
                gaps += interval > 1_500_000_000 / fps
            previous_sensor = sensor
            frames += 1
        if video.tell() != length:
            raise ValueError("Unindexed video bytes follow the last frame.")
    if manifest["frames"] != frames:
        raise ValueError("Manifest frame count differs from the frame index.")
    if frames < 2:
        raise ValueError("A cadence measurement requires at least two indexed frames.")
    seconds = (previous_sensor - first_sensor) / 1e9
    return {
        "backend": manifest.get("backend"), "session_status": manifest.get("status"),
        "frames": frames, "sensor_duration_s": seconds, "measured_fps": (frames - 1) / seconds,
        "nominal_fps": fps, "largest_interval_ms": largest_interval / 1e6,
        "intervals_over_1_5_periods": gaps,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--fps", required=True, type=float, help="Requested nominal frame rate.")
    args = parser.parse_args()
    if not math.isfinite(args.fps) or args.fps <= 0:
        parser.error("FPS must be positive and finite.")
    try:
        result = check_session(args.directory, args.fps)
    except (OSError, ValueError, TypeError, KeyError, csv.Error) as exc:
        print(f"Invalid recording: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
