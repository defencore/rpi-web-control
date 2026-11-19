#!/usr/bin/env python3
"""Render a Raspberry Pi recording ZIP or session directory as an H.264 MP4 with timestamps."""

import argparse
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from contextlib import ExitStack, suppress
from fractions import Fraction
from pathlib import Path

CLOCKS = ("sensor_ns", "monotonic_ns", "realtime_ns")


class Telemetry:
    """Read sorted samples once and hold the latest observation at each exposure."""

    def __init__(self, csv_file, time_column, fields, boot_id):
        reader = csv.DictReader(csv_file)
        columns = reader.fieldnames or []
        if len(set(columns)) != len(columns) or time_column not in columns:
            raise ValueError(f"Telemetry needs unique columns including {time_column!r}.")
        self.fields = []
        for item in fields:
            key, _, label = item.partition("=")
            self.fields.append((key, label or key))
        if not self.fields:
            self.fields = [(name, name) for name in columns if name not in (*CLOCKS, time_column, "boot_id")]
        if not self.fields or any(key not in columns for key, _ in self.fields):
            raise ValueError("Select existing telemetry columns with --field COLUMN=LABEL.")
        self.samples = self._samples(reader, time_column, boot_id)
        self.upcoming = next(self.samples, None)
        self.current = None
        self.last_lookup = None

    def _samples(self, reader, time_column, boot_id):
        previous = None
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"Malformed telemetry row at line {reader.line_num}.")
            stamp = int(row[time_column])
            if previous is not None and stamp < previous:
                raise ValueError(f"Telemetry timestamps decrease at line {reader.line_num}.")
            if boot_id and "boot_id" in row:
                if row["boot_id"].replace("-", "").lower() != boot_id.replace("-", "").lower():
                    raise ValueError(f"Telemetry boot ID differs from the recording at line {reader.line_num}.")
            previous = stamp
            yield stamp, [row[key] for key, _ in self.fields]

    def lines(self, stamp, max_age_ns):
        if self.last_lookup is not None and stamp < self.last_lookup:
            raise ValueError("Frame correlation clock moved backwards. Choose a monotonic clock.")
        self.last_lookup = stamp
        while self.upcoming is not None and self.upcoming[0] <= stamp:
            self.current = self.upcoming
            self.upcoming = next(self.samples, None)
        fresh = self.current is not None and stamp - self.current[0] <= max_age_ns
        return [f"{label}: {self.current[1][i] if fresh and self.current[1][i] else '--'}"
                for i, (_, label) in enumerate(self.fields)]


def frame_rate(value):
    rate = Fraction(str(value)).limit_denominator(100000)
    if not 0 < rate <= 1000:
        raise ValueError("Frame rate must be positive and at most 1000.")
    return rate


def open_session(path, resources):
    """Open just the recorder's three members and read frames sequentially in place."""
    archive = None if path.is_dir() else resources.enter_context(zipfile.ZipFile(path))
    if archive is not None:
        names = archive.namelist()
        if (len(names) != 3 or "frames.csv" not in names or "session.json" not in names
                or len(set(names) & {"video.mjpeg", "video.y4m"}) != 1):
            raise ValueError("Expected session.json, frames.csv and one recording video at the ZIP root.")

    def member(name):
        stream = archive.open(name) if archive is not None else (path / name).open("rb")
        return resources.enter_context(stream)

    data = member("session.json").read(65537)
    if len(data) > 65536:
        raise ValueError("Recording manifest exceeds the supported size.")
    manifest = json.loads(data)
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
            or manifest.get("status") != "complete" or manifest.get("video") not in ("video.y4m", "video.mjpeg")):
        raise ValueError("Choose a completed schema version 1 recording.")
    name = manifest["video"]
    video = member(name)
    size = archive.getinfo(name).file_size if archive is not None else os.fstat(video.fileno()).st_size
    index = resources.enter_context(io.TextIOWrapper(member("frames.csv"), encoding="utf-8", newline=""))
    return manifest, video, size, index


def indexed_frames(video, video_size, index, manifest):
    settings = manifest["settings"]
    width, height = settings["width"], settings["height"]
    raw = manifest["video"] == "video.y4m"
    if raw:
        fields = video.readline(512).split()
        if (not fields or fields[0] != b"YUV4MPEG2" or f"W{width}".encode() not in fields
                or f"H{height}".encode() not in fields or b"C420jpeg" not in fields):
            raise ValueError("Y4M geometry or pixel format disagrees with the session.")
    reader = csv.DictReader(index)
    required = {"frame_index", "offset", "size", *CLOCKS}
    if not required.issubset(reader.fieldnames or []):
        raise ValueError("Missing frame index columns.")
    count, previous = 0, None
    for row in reader:
        item = {key: int(row[key]) for key in required}
        size = item["size"]
        if item["frame_index"] != count or (previous is not None and item["sensor_ns"] <= previous):
            raise ValueError(f"Invalid sequence or sensor timestamp at frame {count}.")
        if raw and (size != width * height * 3 // 2 or video.readline(64) != b"FRAME\n"):
            raise ValueError(f"Invalid Y4M frame {count}.")
        if item["offset"] != video.tell() or size < 1 or size > video_size - video.tell():
            raise ValueError(f"Invalid indexed byte range at frame {count}.")
        data = video.read(size)
        if len(data) != size or (not raw and not (data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9"))):
            raise ValueError(f"Invalid frame payload at frame {count}.")
        previous = item["sensor_ns"]
        count += 1
        yield item, data
    if video.tell() != video_size or count != manifest["frames"] or count == 0:
        raise ValueError("Frame count or video length differs from the completed session.")


class Overlay:
    def __init__(self, width, height, lines, font_path):
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ModuleNotFoundError as exc:
            if exc.name != "PIL":
                raise
            raise RuntimeError("Pillow is missing. In the Python environment used for export, "
                               "run `python3 -m pip install Pillow` and retry.") from exc

        try:
            self.font = ImageFont.truetype(str(font_path or "DejaVuSans.ttf"), max(11, height // 38))
        except OSError:
            if font_path:
                raise
            self.font = ImageFont.load_default()
        bounds = self.font.getbbox("Ag|0123456789")
        text_height = max(bounds[3], bounds[3] - bounds[1])
        self.line_height = text_height + 3
        panel_height = 1 + (lines - 1) * self.line_height + text_height + 1
        panel_height += panel_height % 2  # YUV420 chroma rows cover two luma rows.
        if panel_height > height:
            raise ValueError("Too many OSD lines for the recorded height. Select fewer fields.")
        self.width, self.height = width, height
        self.panel_height = panel_height
        self.panel = Image.new("L", (width, panel_height))
        self.draw = ImageDraw.Draw(self.panel)
        self.neutral_chroma = bytes([128]) * (width * panel_height // 4)

    def write(self, sink, data, lines, raw):
        from PIL import Image

        self.draw.rectangle((0, 0, self.width, self.panel_height), fill=16 if raw else 0)
        for i, line in enumerate(lines):
            # Make each CSV value occupy one OSD line, even for a quoted multiline cell.
            line = " ".join(line.splitlines())
            self.draw.text((3, 1 + i * self.line_height), line, font=self.font, fill=235 if raw else 255)
        if not raw:
            with Image.open(io.BytesIO(data)) as source:
                if source.format != "JPEG" or source.size != (self.width, self.height):
                    raise ValueError("MJPEG frame geometry or format differs from the manifest.")
                image = source.convert("RGB")
                image.paste(self.panel, (0, self.height - self.panel_height))
                sink.write(image.tobytes())
            return
        # Keep the unaffected Y/U/V image rows in their original pixel representation.
        plane_size = self.width * self.height
        keep_y = plane_size - self.width * self.panel_height
        view = memoryview(data)
        sink.write(view[:keep_y])
        sink.write(self.panel.tobytes())
        for offset in (plane_size, plane_size + plane_size // 4):
            sink.write(view[offset:offset + keep_y // 4])
            sink.write(self.neutral_chroma)


def write_sensor_map(index, manifest, path):
    """FFmpeg fsync maps sequential rawvideo PTS to sensor elapsed time in microseconds."""
    first = previous = None
    count = 0
    with path.open("w", encoding="ascii", newline="\n") as mapping:
        for number, row in enumerate(csv.DictReader(index)):
            sensor = int(row["sensor_ns"])
            if first is None:
                first = sensor
            pts = (sensor - first + 500) // 1000
            if int(row["frame_index"]) != number or (previous is not None and pts <= previous):
                raise ValueError(f"Invalid frame sequence or indistinguishable sensor times at frame {number}.")
            mapping.write(f"{number} {pts} 1/1000000\n")
            previous = pts
            count += 1
    if count != manifest["frames"] or not count:
        raise ValueError("Frame count differs from the completed session.")
    index.seek(0)


def encode(args, clip, video, video_size, index, manifest, telemetry, timing):
    settings = manifest["settings"]
    width, height = settings["width"], settings["height"]
    if (not isinstance(width, int) or not isinstance(height, int) or min(width, height) < 2
            or max(width, height) > 32768 or width % 2 or height % 2):
        raise ValueError("Expected even, positive video dimensions.")
    nominal = frame_rate(settings["fps"])
    rate = frame_rate(args.fps) if args.fps else nominal
    max_age = Fraction(str(args.max_age_ms)) * 1_000_000
    if max_age < 0:
        raise ValueError("Maximum telemetry age must be nonnegative.")
    raw = manifest["video"] == "video.y4m"
    line_count = 1 + bool(args.text) + (len(telemetry.fields) if telemetry else 0)
    overlay = Overlay(width, height, line_count, args.font)
    sensor_timing = timing == "sensor"
    timing_options = []
    if sensor_timing:
        available = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, check=True)
        if not any("fsync" in line.split()[:2] for line in available.stdout.splitlines()):
            raise ValueError("Sensor timing requires FFmpeg 7 or newer with the fsync filter.")
        write_sensor_map(index, manifest, clip.parent / "sensor.map")
        # Consecutive decode/presentation order lets MP4 derive each displayed
        # duration from adjacent timestamps. Give the last frame one nominal period.
        timing_options = ["-vf", "fsync=file=sensor.map", "-enc_time_base", "1:1000000", "-bf", "0",
                          "-bsf:v", f"setts=duration={round(1_000_000 / nominal)}"]
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-xerror",
               "-f", "rawvideo", "-pixel_format", "yuv420p" if raw else "rgb24", "-video_size",
               f"{width}x{height}", "-framerate", str(rate), "-i", "pipe:0", "-an",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
               *timing_options, "-fps_mode", "passthrough",
               "-video_track_timescale", "1000000" if sensor_timing else str(rate.numerator),
               "-movflags", "+faststart", "-f", "mp4", str(clip)]
    gaps = count = 0
    first = previous = None
    with subprocess.Popen(command, stdin=subprocess.PIPE, cwd=clip.parent) as encoder:
        try:
            for row, data in indexed_frames(video, video_size, index, manifest):
                stamp = row[args.clock]
                sensor = row["sensor_ns"]
                if first is None:
                    first = sensor
                if previous is not None and (sensor - previous) * nominal > 1_500_000_000:
                    gaps += 1
                    if args.gap_policy == "reject":
                        raise ValueError(f"Sensor cadence gap before frame {count}.")
                previous = sensor
                lines = [f"#{count}  {args.clock} {stamp}"]
                if args.text:
                    lines.append(args.text)
                if telemetry:
                    lines.extend(telemetry.lines(stamp, max_age))
                overlay.write(encoder.stdin, data, lines, raw)
                count += 1
            encoder.stdin.close()
            if encoder.wait():
                raise ValueError("FFmpeg failed to encode the clip.")
        except BaseException:
            encoder.kill()
            # Flush errors after termination must preserve the frame-validation diagnostic.
            with suppress(OSError):
                encoder.stdin.close()
            raise
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-count_frames", "-show_entries", "stream=avg_frame_rate,time_base,nb_read_frames",
                            "-of", "json", str(clip)], capture_output=True, text=True, check=True)
    stream = json.loads(probe.stdout)["streams"][0]
    if int(stream["nb_read_frames"]) != count or (not sensor_timing and Fraction(stream["avg_frame_rate"]) != rate):
        raise ValueError("Encoded frame count or frame rate failed verification.")
    if sensor_timing and stream["time_base"] != "1/1000000":
        raise ValueError("Encoded time base failed verification.")
    return {"frames": count, "timing": timing, "output_fps": str(Fraction(stream["avg_frame_rate"])),
            "sensor_duration_ns": previous - first,
            "intervals_over_1_5_periods": gaps}


def render(args):
    output = args.output.absolute()
    if output.suffix.lower() != ".mp4":
        raise ValueError("Choose an MP4 output file.")
    if output.exists():
        raise FileExistsError(f"Output already exists: {output}")
    with ExitStack() as resources:
        timing = args.timing or ("constant" if args.fps or args.session.is_dir() or args.gap_policy == "compact"
                                 else "sensor")
        if timing == "sensor" and (args.fps or args.gap_policy == "compact"):
            raise ValueError("Use --timing constant with --fps or --gap-policy compact.")
        manifest, video, video_size, index = open_session(args.session, resources)
        telemetry = None
        if args.telemetry:
            file = resources.enter_context(args.telemetry.open(encoding="utf-8-sig", newline=""))
            telemetry = Telemetry(file, args.time_column or args.clock, args.field, manifest.get("boot_id"))
        elif args.field or args.time_column:
            raise ValueError("--field and --time-column require --telemetry.")
        folder = resources.enter_context(tempfile.TemporaryDirectory(dir=output.parent))
        clip = Path(folder) / "encoded.mp4"
        summary = encode(args, clip, video, video_size, index, manifest, telemetry, timing)
        # Reserve the destination only after encoding succeeds, including for concurrent renders.
        with output.open("xb"):
            pass
        try:
            os.replace(clip, output)
        except OSError:
            output.unlink()
            raise
        return summary


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("session", type=Path, help="Downloaded recording ZIP or completed session directory.")
    cli.add_argument("output", type=Path, help="New .mp4 output path.")
    cli.add_argument("--timing", choices=("sensor", "constant"),
                     help="Use measured sensor intervals (default for ZIPs), or constant rate (default for directories).")
    cli.add_argument("--fps", help="Select constant playback rate, for example 240 or 30000/1001.")
    cli.add_argument("--telemetry", type=Path, help="Time-ordered UTF-8 CSV with integer nanosecond timestamps.")
    cli.add_argument("--clock", choices=CLOCKS, default="monotonic_ns", help="Frame clock for matching and display.")
    cli.add_argument("--time-column", help="Telemetry timestamp column in the selected clock. Defaults to --clock.")
    cli.add_argument("--field", action="append", default=[], metavar="COLUMN[=LABEL]", help="Repeat for each field.")
    cli.add_argument("--max-age-ms", default="1000", help="Maximum age of a held sample. Defaults to 1000 ms.")
    cli.add_argument("--text", help="Additional static OSD line.")
    cli.add_argument("--font", type=Path, help="TrueType font for OSD text.")
    cli.add_argument("--gap-policy", choices=("compact", "reject"),
                     help="Compact gaps at constant rate, or reject sensor intervals above 1.5 periods.")
    return cli


def main():
    args = parser().parse_args()
    try:
        print(json.dumps(render(args), indent=2))
    except (ValueError, OSError, KeyError, TypeError, IndexError, ZeroDivisionError, csv.Error,
            ImportError, subprocess.SubprocessError, zipfile.BadZipFile, RuntimeError) as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
