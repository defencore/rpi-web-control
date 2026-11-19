"""A session binds one index row to the bytes written for the same completed request."""

import contextlib
import csv
import json
import os
import time
import uuid
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

from .capture import boot_ns
from .storage import RecordingTarget


def utc_from_ns(realtime_ns):
    seconds, nanos = divmod(realtime_ns, 1_000_000_000)
    return datetime.fromtimestamp(seconds, timezone.utc).replace(microsecond=nanos // 1000)


def correlate(sensor_ns, realtime_ns=time.time_ns):
    before = boot_ns()
    mono = time.monotonic_ns()
    real = realtime_ns()
    after = boot_ns()
    delta = sensor_ns - (before + after) // 2
    return mono + delta, real + delta, after - before


class Recording:
    def __init__(self, parent, settings, backend, model, reason, *, sensor_mode=None, realtime_ns=time.time_ns):
        self._realtime_ns = realtime_ns
        self.start_sensor_ns = boot_ns()
        now = utc_from_ns(self._realtime_ns())
        name = now.strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
        target = parent if isinstance(parent, RecordingTarget) else None
        parent_path = target.path if target else Path(parent)
        self.path = parent_path / name
        flags = os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW
        parent_fd = target.fd if target else os.open(parent_path, flags)
        try:
            os.mkdir(name, mode=0o750, dir_fd=parent_fd)
            self.session_fd = os.open(name, flags, dir_fd=parent_fd)
        finally:
            if target is None:
                os.close(parent_fd)
        self.count = 0
        self.raw = settings["fps"] > 60
        self.files = contextlib.ExitStack()
        try:
            try:
                boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            except OSError:
                boot_id = None
            self.manifest = {
                "schema_version": 1, "status": "recording", "started_at": now.isoformat(),
                "settings": dict(settings), "osd_effective": settings["osd"] and not self.raw,
                "backend": backend, "model": model, "sensor_mode": sensor_mode, "reason": reason, "boot_id": boot_id,
                "sensor_clock": "CLOCK_BOOTTIME" if hasattr(time, "CLOCK_BOOTTIME") else "simulated monotonic",
                "timestamp_source": "SensorTimestamp" if backend == "picamera2" else "simulator",
                "start_sensor_ns": self.start_sensor_ns,
                "video": "video.y4m" if self.raw else "video.mjpeg",
            }
            self._manifest()
            self.video = self.files.enter_context(self._open(self.manifest["video"], "xb"))
            self.index = self.files.enter_context(self._open("frames.csv", "x", newline=""))
            self.rows = csv.writer(self.index, lineterminator="\n")
            self.rows.writerow(("frame_index", "sensor_ns", "monotonic_ns", "realtime_ns", "offset", "size",
                                "clock_sample_ns"))
            if self.raw:
                rate = Fraction(str(settings["fps"])).limit_denominator(100000)
                header = (f"YUV4MPEG2 W{settings['width']} H{settings['height']} "
                          f"F{rate.numerator}:{rate.denominator} Ip A1:1 C420jpeg\n")
                self.video.write(header.encode("ascii"))
            self.flush_deadline = time.monotonic() + 1
        except Exception as exc:
            try:
                self.files.close()
                if hasattr(self, "manifest"):
                    self.manifest.update(status="incomplete", error=str(exc), frames=0)
                    with contextlib.suppress(OSError):
                        self._manifest()
            finally:
                os.close(self.session_fd)
            raise

    def _open(self, name, mode, **options):
        def opener(path, flags):
            return os.open(path, flags | os.O_NOFOLLOW, 0o640, dir_fd=self.session_fd)

        return open(name, mode, opener=opener, **options)

    def _manifest(self):
        with self._open("session.json.tmp", "w", encoding="utf-8") as file:
            json.dump(self.manifest, file, indent=2, allow_nan=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace("session.json.tmp", "session.json", src_dir_fd=self.session_fd, dst_dir_fd=self.session_fd)

    def write(self, frame, jpeg):
        mono, real, uncertainty = correlate(frame.sensor_ns, self._realtime_ns)
        if self.raw:
            self.video.write(b"FRAME\n")
        start = self.video.tell()
        if self.raw:
            frame.write_yuv(self.video)
        else:
            self.video.write(jpeg)
        self.rows.writerow((self.count, frame.sensor_ns, mono, real, start, self.video.tell() - start,
                            uncertainty))
        self.count += 1
        if time.monotonic() >= self.flush_deadline:
            self.video.flush()
            self.index.flush()
            self.flush_deadline = time.monotonic() + 1

    def close(self, error=None):
        flush_error = None
        try:
            try:
                with self.files:
                    for file in (self.video, self.index):
                        file.flush()
                        os.fsync(file.fileno())
            except OSError as exc:
                flush_error = exc
                error = error or str(exc)
            self.manifest.update(status="incomplete" if error or not self.count else "complete",
                                 error=error, frames=self.count,
                                 ended_at=utc_from_ns(self._realtime_ns()).isoformat())
            if flush_error:
                # Preserve the original delivery error even if the volume can no
                # longer accept an incomplete manifest.
                with contextlib.suppress(OSError):
                    self._manifest()
                raise flush_error
            self._manifest()
        finally:
            os.close(self.session_fd)
