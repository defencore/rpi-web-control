"""A single owner serializes camera changes, frame processing, and recording boundaries."""

import logging
import math
import queue
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor

from .capture import PiSource, SimSource, as_jpeg, select_mode
from .live import LiveOutput, check_geometry, endpoint, idle_status, validate_max_fps
from .mode_test import CameraBusy, ModeTest
from .recording import Recording
from .storage import RecordingTarget

LOG = logging.getLogger(__name__)
DEFAULTS = {"width": 640, "height": 480, "fps": 30, "osd": False, "sensor_bit_depth": 0}


def _storage_failure(message, cause):
    return OSError(f"{message} ({cause}). Check the selected drive and free space in Storage.")


def validate(settings):
    if set(settings) != set(DEFAULTS):
        raise ValueError("Camera settings must contain width, height, fps, osd and sensor_bit_depth.")
    for name in ("width", "height"):
        value = settings[name]
        if type(value) is not int or not 16 <= value <= 8192 or value % 2:
            raise ValueError(f"Camera {name} must be an even integer between 16 and 8192.")
    fps = settings["fps"]
    if type(fps) not in (float, int) or not 1 <= fps <= 1000 or not math.isfinite(fps):
        raise ValueError("Camera frame rate must be between 1 and 1000 FPS.")
    if type(settings["osd"]) is not bool:
        raise ValueError("Camera OSD must be true or false.")
    bit_depth = settings["sensor_bit_depth"]
    if type(bit_depth) is not int or not 0 <= bit_depth <= 32:
        raise ValueError("Sensor bit depth must be 0 (automatic) or an integer between 1 and 32.")


class Camera:
    def __init__(self, backend="picamera2", settings=None, *, realtime_ns=time.time_ns):
        settings = dict(DEFAULTS if settings is None else settings)
        settings.setdefault("sensor_bit_depth", 0)
        validate(settings)
        if backend not in ("picamera2", "simulated", "simulator"):
            raise ValueError("Unknown camera backend.")
        self.backend = "picamera2" if backend == "picamera2" else "simulated"
        self._factory = PiSource if self.backend == "picamera2" else SimSource
        self._mode_test = ModeTest(self.backend)
        self._realtime_ns = realtime_ns
        self._state = {"backend": self.backend, "settings": settings, "preview": False,
                       "recording": False, "active": False, "sensor_mode": None,
                       "osd_effective": settings["osd"] and settings["fps"] <= 60, "frames": 0,
                       "recording_video_bytes": 0,
                       "actual_fps": 0.0, "dropped_frames": 0, "queue_drops": 0,
                       "session": None, "last_error": None}
        self._condition = threading.Condition()
        self._recording_started_monotonic = 0.0
        self._commands = queue.Queue()
        self._closed = False
        self._sensor = self._record = self._caps = self._jpeg = None
        self._live = None
        self._last_live = idle_status()
        self._preview_id = self._epoch = 0
        self._preview_job = None
        self._encoder = ThreadPoolExecutor(max_workers=1, thread_name_prefix="preview")
        self._thread = threading.Thread(target=self._run, daemon=True, name="camera")
        self._thread.start()

    def _call(self, action, *args):
        future = Future()
        with self._condition:
            if self._closed:
                if action == self._shutdown:
                    return
                raise RuntimeError("Camera is closed.")
            if action == self._shutdown:
                self._closed = True
            self._commands.put((action, args, future))
        return future.result()

    def _update(self, **fields):
        with self._condition:
            self._state.update(fields)
            if "preview" in fields:
                self._condition.notify_all()

    def status(self):
        with self._condition:
            output = self._live
            mode = self._state["sensor_mode"]
            video_rate = None
            if self._state["recording"] and self._state["frames"]:
                elapsed = time.monotonic() - self._recording_started_monotonic
                if elapsed >= 1:
                    video_rate = self._state["recording_video_bytes"] / elapsed
            return {**self._state, "settings": dict(self._state["settings"]),
                    "recording_video_bytes_per_second": video_rate,
                    "sensor_mode": mode.copy() if mode else None,
                    "mode_test": self._mode_test.status(),
                    "live": output.status() if output else dict(self._last_live)}

    def capabilities(self):
        return self._call(self._capabilities)

    def configure(self, settings):
        return self._call(self._configure, dict(settings))

    def start_mode_test(self, mode=None):
        return self._call(self._start_mode_test, mode)

    def cancel_mode_test(self):
        return self._call(self._mode_test.cancel)

    def set_preview(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("Preview must be true or false.")
        return self._call(self._set_preview, enabled)

    def set_live(self, enabled, host, port, max_fps=None):
        if type(enabled) is not bool:
            raise ValueError("Live stream enabled must be true or false.")
        return self._call(self._set_live, enabled, host, port, max_fps)

    def preview_frame(self, after, timeout):
        with self._condition:
            self._condition.wait_for(lambda: self._preview_id > after and self._jpeg is not None
                                     or not self._state["preview"], timeout)
            if self._state["preview"] and self._preview_id > after and self._jpeg is not None:
                return self._preview_id, self._jpeg
        return None

    def start_recording(self, directory, reason):
        return self._call(self._start_recording, directory, reason)

    def stop_recording(self):
        return self._call(self._stop_recording)

    def close(self):
        try:
            self._call(self._shutdown)
        finally:
            self._thread.join()
            self._encoder.shutdown()

    def _capabilities(self):
        if self._caps:
            return self._caps
        if self._mode_test.running:
            return {"available": False, "model": "", "modes": [], "properties": {},
                    "error": "Detecting sensor modes for the current test."}
        try:
            source = self._factory()
            try:
                self._caps = source.capabilities()
            finally:
                source.close()
            return self._caps
        except Exception as exc:
            return {"available": False, "model": "", "modes": [], "properties": {}, "error": str(exc)}

    def _configure(self, changes):
        old = self._state["settings"]
        settings = {**old, **changes}
        validate(settings)
        if old == settings:
            return
        self._check_mode_test()
        if self._record:
            raise RuntimeError("Stop recording before changing camera settings.")
        if self._live:
            raise RuntimeError("Stop the live stream before changing camera settings.")
        caps = self._capabilities()
        if caps["available"]:
            select_mode(caps["modes"], settings)
        if self._sensor:
            self._end_capture()
            try:
                self._start_capture(settings)
            except Exception:
                try:
                    self._start_capture(old)
                except Exception as recovery:
                    self._failed(recovery)
                raise
        self._update(settings=settings, osd_effective=settings["osd"] and settings["fps"] <= 60)

    def _check_mode_test(self):
        if self._mode_test.running:
            raise CameraBusy("Finish or cancel the current sensor mode test first.")

    def _start_mode_test(self, mode):
        self._check_mode_test()
        if self._sensor or self._live or self._record or self._state["preview"]:
            raise CameraBusy("Stop preview, streaming and recording before testing sensor modes.")
        return self._mode_test.start(self._caps, mode)

    def _start_capture(self, settings):
        self._check_mode_test()
        source = self._factory()
        try:
            caps = self._caps or source.capabilities()
            mode = select_mode(caps["modes"], settings)
            source.start(settings, mode)
            self._caps = caps
        except BaseException:
            self._caps = None
            source.close()
            raise
        self._sensor = source
        self._last_frame = time.monotonic()
        self._last_sensor_ns = self._window_ns = None
        self._intervals = 0
        self._preview_due = 0
        self._update(active=True, sensor_mode=dict(mode), last_error=None, dropped_frames=0,
                     queue_drops=0, actual_fps=0.0)

    def _end_capture(self):
        source, self._sensor = self._sensor, None
        try:
            if source:
                source.close()
        finally:
            self._invalidate_preview()
            self._update(active=False, sensor_mode=None, actual_fps=0.0)

    def _invalidate_preview(self):
        with self._condition:
            self._epoch += 1
            self._jpeg = None
            self._condition.notify_all()

    def _set_preview(self, enabled):
        if enabled and not self._sensor:
            self._start_capture(self._state["settings"])
        self._update(preview=enabled)
        if not enabled:
            self._invalidate_preview()
            if not self._record and not self._live:
                self._end_capture()

    def _set_live(self, enabled, host, port, max_fps=None):
        validate_max_fps(max_fps)
        if not enabled:
            self._stop_live()
            if not self._record and not self._state["preview"]:
                self._end_capture()
            return
        self._check_mode_test()
        destination = endpoint(host, port)
        if self._live:
            if self._live.destination == destination and self._live.max_fps == max_fps and self._live.running:
                return
            raise RuntimeError("Stop the current live stream before selecting a different receiver or frame-rate limit.")
        settings = self._state["settings"]
        check_geometry(settings["width"], settings["height"])
        output = LiveOutput(*destination, max_fps=max_fps)
        try:
            if not self._sensor:
                self._start_capture(settings)
        except BaseException:
            output.close()
            raise
        with self._condition:
            self._live = output

    def _stop_live(self):
        if self._live:
            output = self._live
            output.close()
            with self._condition:
                self._last_live = output.status()
                self._live = None

    def _start_recording(self, directory, reason):
        if self._record:
            return self._state["session"]
        self._mode_test.cancel("A recording request took priority.")
        if not self._sensor:
            self._start_capture(self._state["settings"])
        try:
            self._record = Recording(directory, self._state["settings"], self.backend, self._caps["model"],
                                     reason, sensor_mode=self._state["sensor_mode"], realtime_ns=self._realtime_ns)
        except Exception as exc:
            if not self._state["preview"] and not self._live:
                self._end_capture()
            if isinstance(exc, OSError):
                path = directory.path if isinstance(directory, RecordingTarget) else directory
                raise _storage_failure(f"Could not start a recording in {path}", exc) from exc
            raise
        with self._condition:
            self._recording_started_monotonic = time.monotonic()
            self._state.update(recording=True, session=str(self._record.path), frames=0,
                               recording_video_bytes=self._record.video_bytes, last_error=None)
        return str(self._record.path)

    def _stop_recording(self, error=None):
        record, self._record = self._record, None
        try:
            if record:
                record.close(error)
        except OSError as exc:
            raise _storage_failure(f"Recording stopped. Could not finish saving {record.path}", exc) from exc
        finally:
            self._update(recording=False, recording_video_bytes=0)
            if not self._state["preview"] and not self._live:
                self._end_capture()

    def _shutdown(self):
        self._mode_test.cancel()
        self._stop_live()
        self._update(preview=False)
        self._stop_recording()

    def _failed(self, exc):
        LOG.error("Capture stopped: %s", exc)
        self._stop_live()
        self._update(last_error=str(exc), preview=False)
        try:
            self._stop_recording(str(exc))
        except Exception as cleanup:
            LOG.error("Capture cleanup failed: %s", cleanup)

    def _run(self):
        while True:
            try:
                action, args, future = self._commands.get(block=self._sensor is None and not self._mode_test.running)
            except queue.Empty:
                if self._mode_test.running:
                    caps = self._mode_test.tick()
                    if caps is not None and caps.get("available"):
                        self._caps = caps
                    continue
                try:
                    self._tick()
                except Exception as exc:
                    self._failed(exc)
                continue
            try:
                result = action(*args)
            except CameraBusy as exc:
                future.set_exception(exc)
            except Exception as exc:
                self._update(last_error=str(exc))
                future.set_exception(exc)
            else:
                future.set_result(result)
            if action == self._shutdown:
                return

    def _tick(self):
        if self._live and not self._live.running:
            self._set_live(False, None, None)
            if not self._sensor:
                return
        frame = self._sensor.next_frame(0.05)
        now = time.monotonic()
        if frame is None:
            if now - self._last_frame > 5:
                raise RuntimeError("The camera has not delivered a frame for five seconds.")
            return
        try:
            self._last_frame = now
            self._measure(frame.sensor_ns)
            preview = self._state["preview"] and now >= self._preview_due
            settings = self._state["settings"]
            # A preview buffer may already be queued when a trigger starts recording.
            record_frame = self._record is not None and frame.sensor_ns >= self._record.start_sensor_ns
            jpeg = None
            if settings["fps"] <= 60 and (record_frame or preview):
                jpeg = frame.jpeg(settings["osd"])
            if self._live:
                self._live.offer(frame, jpeg, settings["osd"] and settings["fps"] <= 60)
            if record_frame:
                try:
                    self._record.write(frame, jpeg)
                except OSError as exc:
                    failure = _storage_failure(f"Could not write recording {self._record.path}", exc)
                    if not self._live and not self._state["preview"]:
                        raise failure from exc
                    self._update(last_error=str(failure))
                    try:
                        self._stop_recording(str(failure))
                    except OSError as cleanup:
                        LOG.error("Recording flush failed: %s", cleanup)
                else:
                    self._update(frames=self._record.count, recording_video_bytes=self._record.video_bytes)
            if preview:
                self._preview_due = now + 0.125
                if jpeg is not None:
                    self._publish(jpeg, self._epoch)
                elif self._preview_job is None or self._preview_job.done():
                    self._preview_job = self._encoder.submit(self._encode_preview, frame.luma_preview(), self._epoch)
        finally:
            frame.release()

    def _publish(self, jpeg, epoch):
        with self._condition:
            if self._epoch == epoch and self._state["preview"]:
                self._preview_id += 1
                self._jpeg = jpeg
                self._condition.notify_all()

    def _encode_preview(self, image, epoch):
        try:
            self._publish(as_jpeg(image), epoch)
        except Exception as exc:
            LOG.warning("Preview encoding failed: %s", exc)

    def _measure(self, sensor_ns):
        if self._last_sensor_ns is not None:
            dt = sensor_ns - self._last_sensor_ns
            if dt <= 0:
                raise RuntimeError("Camera timestamps did not increase.")
            missed = max(0, round(dt * self._state["settings"]["fps"] / 1e9) - 1)
            if missed:
                self._update(dropped_frames=self._state["dropped_frames"] + missed)
        self._last_sensor_ns = sensor_ns
        if self._window_ns is None:
            self._window_ns = sensor_ns
        else:
            self._intervals += 1
            elapsed = sensor_ns - self._window_ns
            if elapsed >= 500_000_000:
                self._update(actual_fps=round(self._intervals * 1e9 / elapsed, 2),
                             queue_drops=self._sensor.queue_drops)
                self._window_ns = sensor_ns
                self._intervals = 0
