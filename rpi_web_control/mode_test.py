"""Short capture-only measurements supervised by the camera controller."""

import copy
import json
import subprocess
import sys
import time
from pathlib import Path


PROBE = Path(__file__).resolve().parents[1] / "tools" / "check-camera-modes.py"
SAMPLE_SECONDS = 1.0
WARMUP_SECONDS = 0.5
WORKER_TIMEOUT = WARMUP_SECONDS + SAMPLE_SECONDS + 20


class CameraBusy(RuntimeError):
    pass


class ModeTest:
    """The camera thread owns the worker and publishes a new report on each transition."""

    def __init__(self, backend):
        self.backend = backend
        self.modes = []
        self.worker = None
        self.report = {"state": "idle", "total": 0, "current": None, "results": [], "error": None}

    @property
    def running(self):
        return self.report["state"] == "running"

    def status(self):
        return copy.deepcopy(self.report)

    def start(self, caps, selected):
        if selected is not None and (
            not isinstance(selected, dict) or selected.keys() != {"width", "height", "bit_depth"}
            or any(type(value) is not int or value <= 0 for value in selected.values())
        ):
            raise ValueError("Select an advertised sensor width, height and bit depth.")
        self.selected = selected
        if caps is None:
            self.modes = []
            self.report = {"state": "running", "total": 0, "current": None, "results": [], "error": None}
        else:
            self._plan(caps)
        return self.status()

    def _plan(self, caps):
        selected = self.selected
        modes = caps["modes"]
        if modes and selected is not None:
            modes = [mode for mode in modes if all(mode[key] == value for key, value in selected.items())]
            if not modes:
                raise ValueError("The selected sensor mode is unavailable.")
        self.modes = list(modes)
        self.report = {"state": "running" if modes else "failed", "total": len(modes),
                       "current": modes[0] if modes else None, "results": [],
                       "error": None if modes else (caps.get("error") or "Camera advertised no modes.")}

    def _stop_worker(self):
        if self.worker is not None:
            # Closing camera handles on worker exit also releases a stalled driver call.
            self.worker.kill()
            try:
                self.worker.communicate(timeout=0.5)
            except subprocess.TimeoutExpired:
                return False
            self.worker = None
        return True

    def cancel(self, reason=None):
        if self.running:
            if not self._stop_worker():
                raise CameraBusy("Waiting for the camera probe to release the device.")
            self.report = {**self.report, "state": "cancelled", "current": None, "error": reason}
        return self.status()

    def _poll(self, mode):
        if self.worker is None:
            self.deadline = time.monotonic() + WORKER_TIMEOUT
            self.worker = subprocess.Popen(
                [sys.executable, "-sP", str(PROBE), "--web-probe", self.backend, json.dumps(mode),
                 "--seconds", str(SAMPLE_SECONDS), "--warmup", str(WARMUP_SECONDS)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)
        try:
            output, _ = self.worker.communicate(timeout=0.05)
        except subprocess.TimeoutExpired:
            if time.monotonic() >= self.deadline and self._stop_worker():
                raise RuntimeError("Timed out waiting for the camera probe.") from None
            return None
        code = self.worker.returncode
        self.worker = None
        if code:
            raise RuntimeError(f"Camera probe exited with code {code}.")
        data = json.loads(output)
        if not isinstance(data, dict):
            raise ValueError("Invalid result from the camera probe.")
        return data

    def tick(self):
        mode = self.report["current"]
        try:
            data = self._poll(mode)
            if data is None:
                return
            if mode is None:
                self._plan(data)
                return data
            measurement = data["measurement"]
            result = {"mode": mode, "status": data["status"], "achieved_fps": measurement["achieved_fps"],
                      "frames": measurement["frames"], "queue_drops": data["sample_queue_drops"],
                      "timestamp_gaps": measurement["gaps_over_1_5_periods"] if measurement["frames"] > 1 else None,
                      "error": " ".join(data["issues"]) or None}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
            if not self._stop_worker():
                return
            if mode is None:
                self.report = {**self.report, "state": "failed", "error": str(exc)}
                return
            result = {"mode": mode, "status": "failed", "achieved_fps": None, "frames": 0,
                      "queue_drops": None, "timestamp_gaps": None, "error": str(exc)}
        results = self.report["results"] + [result]
        current = self.modes[len(results)] if len(results) < len(self.modes) else None
        state = "running"
        if current is None:
            state = "completed" if all(r["status"] == "passed" for r in results) else "failed"
        self.report = {**self.report, "state": state, "current": current, "results": results}
