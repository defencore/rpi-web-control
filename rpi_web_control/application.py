"""Coordinate camera ownership, trigger levels, and persisted settings."""

import logging
import os
import re
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .camera import Camera
from .config import Settings
from .device_clock import DeviceClock
from .downloads import list_recordings, open_recording_archive, open_recording_file
from .firmware import Firmware
from .network import Network
from .network_sim import SimulatedNetwork
from .nvme_client import MOUNTPOINT, USB_MOUNTPOINT, NvmeClient
from .nvme_sim import SimulatedNvme
from .recording_cleanup import execute_cleanup, plan_cleanup, private_recording_directory
from .storage import Storage
from .triggers import TriggerManager

LOG = logging.getLogger(__name__)


class Conflict(RuntimeError):
    """The requested change conflicts with an active operation."""


class Application:
    def __init__(self, state_dir, roots, backend="picamera2", *, required_mounts=()):
        simulated = backend in ("simulated", "simulator")
        self.clock = DeviceClock(simulated)
        self.nvme = SimulatedNvme(state_dir) if simulated else NvmeClient()
        self.nvme_root = self.nvme.directory if simulated else Path(MOUNTPOINT) / "recordings"
        self.usb = SimulatedNvme(state_dir, medium="usb") if simulated else NvmeClient(medium="usb")
        self.usb_root = self.usb.directory if simulated else Path(USB_MOUNTPOINT) / "recordings"
        self.media_clients = {"nvme": self.nvme, "usb": self.usb}
        self.media_roots = {"nvme": self.nvme_root, "usb": self.usb_root}
        self.disk_busy = False
        self.store = Settings(state_dir, roots, managed_roots=self.media_roots.values())
        self.config = self.store.load()
        self.storage = Storage(self.store.roots, required_mounts=required_mounts,
                               managed_root=self.nvme_root, simulated_nvme=self.nvme if simulated else None,
                               managed_usb_root=self.usb_root, simulated_usb=self.usb if simulated else None)
        self.firmware = Firmware(Path(state_dir) / "firmware-sim" if simulated else None)
        self.reboot_until = 0
        self.camera = Camera(backend, settings=self.config["camera"],
                             realtime_ns=self.clock.now_ns if simulated else time.time_ns)
        self.config_lock = threading.RLock()
        self.record_lock = threading.Condition()
        self.sources = set()
        self.closing = False
        self.network = SimulatedNetwork(state_dir) if simulated else Network()
        self.network_lock = threading.Lock()
        self.network_cache = {}
        self.network_until = 0
        self.triggers = TriggerManager(self.trigger)
        self.recovery = threading.Thread(target=self._recover_recording, name="recording-recovery", daemon=True)
        self.recovery.start()
        self.triggers.configure(self.config["triggers"])

    def trigger(self, source, active):
        self.record("trigger:" + source, active)

    def record(self, source, enabled):
        with self.record_lock:
            if self.closing:
                return
            if enabled:
                self._check_reboot()
                self._check_disk_idle()
                if not self.camera.status()["recording"]:
                    self._start_recording(source)
                self.sources.add(source)
            else:
                self.sources.discard(source)
                if not self.sources and self.camera.status()["recording"]:
                    self.camera.stop_recording()
            self.record_lock.notify_all()
            LOG.info("Recording source %s active=%s", source, enabled)

    def _start_recording(self, source):
        directory = self.store.directory(self.config["storage"]["directory"])
        with self.storage.open_target(directory, self.store.storage_identity) as target:
            if self.store.storage_identity is None:
                self.store.save(self.config, storage_identity=target.identity)
            self.camera.start_recording(target, source)

    def _recover_recording(self):
        # A level-triggered input may stay asserted through a camera or storage fault.
        # Keep its demand until the input releases, with backoff between reopen attempts.
        delay = 2
        with self.record_lock:
            while not self.closing:
                if not self.sources:
                    delay = 2
                    self.record_lock.wait()
                    continue
                if self.record_lock.wait_for(lambda: self.closing or not self.sources, timeout=delay):
                    continue
                if self.camera.status()["recording"]:
                    delay = 2
                    continue
                try:
                    self._start_recording(sorted(self.sources)[0])
                except (OSError, RuntimeError, ValueError) as exc:
                    LOG.warning("Recording recovery failed: %s", exc)
                delay = min(60, delay * 2)

    def _check_reboot(self):
        if time.monotonic() < self.reboot_until:
            raise Conflict("A device reboot is in progress.")

    def _check_disk_idle(self):
        if self.disk_busy:
            raise Conflict("A storage operation is in progress.")

    def nvme_status(self, medium="nvme"):
        result = self.media_clients[medium].status()
        with self.record_lock:
            return {**result, "busy": self.disk_busy}

    @contextmanager
    def _disk_operation(self):
        with self.record_lock:
            self._check_reboot()
            self._check_disk_idle()
            if self.closing or self.sources or self.camera.status()["recording"]:
                raise Conflict("Finish all recordings before changing storage.")
            self.disk_busy = True
        try:
            yield
        finally:
            with self.record_lock:
                self.disk_busy = False
                self.record_lock.notify_all()

    def apply_nvme(self, body, medium="nvme"):
        with self._disk_operation():
            try:
                return self.media_clients[medium].apply(body)
            finally:
                self.storage.invalidate_inventory()

    def _recording_directories(self):
        return {"current": self.config["storage"]["directory"],
                "default": self.store.defaults["storage"]["directory"],
                **{name: str(root) for name, root in self.media_roots.items()}}

    def _lookup_target(self, source):
        directories = self._recording_directories()
        if source not in directories:
            raise ValueError("Unknown recordings source.")
        directory = self.store.directory(directories[source])
        identity = self.store.storage_identity if directories[source] == directories["current"] else None
        return directory, identity

    def _medium(self, directory):
        return next((name for name, root in self.media_roots.items() if directory.is_relative_to(root)), None)

    def _download_target(self, source):
        with self.record_lock:
            directory, identity = self._lookup_target(source)
            if self._medium(directory):
                self._check_disk_idle()
            return directory, identity

    def _cleanup_disk(self, target):
        medium = self._medium(target.path)
        if not medium:
            raise ValueError("Deletion is limited to a managed external USB or NVMe recording volume.")
        if not private_recording_directory(target.fd):
            raise ValueError("Cleanup requires a service-owned directory writable only by the service account.")
        manager = self.media_clients[medium]
        status = manager.status()
        if not status.get("ready"):
            raise ValueError("The external disk could not be verified as a dedicated recording volume.")
        mountpoint = str(self.media_roots[medium].parent)
        candidates = [(disk, volume) for disk in status["devices"] for volume in disk["volumes"]
                      if volume.get("ready") and mountpoint in volume["mountpoints"]]
        if len(candidates) != 1:
            raise ValueError("Cannot identify a single managed external recording volume.")
        disk, volume = candidates[0]
        if not isinstance(manager, SimulatedNvme):
            number = os.fstat(target.fd).st_dev
            if (number == os.stat("/").st_dev or target.identity["mountpoint"] != mountpoint
                    or volume.get("devno") != f"{os.major(number)}:{os.minor(number)}"):
                raise ValueError("The open recording directory does not match the verified external device.")
        return medium, disk

    def recordings(self, cursor=None, *, source="current", target_id=None):
        directory, identity = self._download_target(source)
        result = list_recordings(self.storage, directory, identity, cursor, source=source, target_id=target_id)
        result["cleanup_allowed"] = False
        if self._medium(directory):
            try:
                with self.storage.open_target(directory, identity, create=False) as target:
                    self._cleanup_disk(target)
                    result["cleanup_allowed"] = True
            except (OSError, ValueError, RuntimeError):
                pass  # A failed disk check still permits read-only browsing.
        return result

    def cleanup_recordings(self, body):
        action = body.get("action")
        if action not in ("plan", "delete"):
            raise ValueError("Choose a cleanup operation.")
        required = {"action", "target_id", "source"}
        if action == "delete":
            required.update(("revision", "confirmation"))
        if "session" in body:
            required.add("session")
        if set(body) != required or not all(isinstance(value, str) for value in body.values()):
            raise ValueError("Invalid recording cleanup fields.")
        session = body.get("session")
        if session is not None and not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}", session):
            raise ValueError("Invalid recording session name.")
        confirmation = "DELETE" if session else "CLEAN"
        if not re.fullmatch(r"[0-9a-f]{64}", body["target_id"]):
            raise ValueError("Refresh the recordings list before deleting.")
        if action == "delete" and (body["confirmation"] != confirmation
                                   or not re.fullmatch(r"[0-9a-f]{64}", body["revision"])):
            raise ValueError("Supply the reviewed cleanup plan and its exact confirmation.")
        with self._disk_operation():
            directory, identity = self._lookup_target(body["source"])
            with self.storage.open_target(directory, identity, create=False) as target:
                medium, disk = self._cleanup_disk(target)
                options = {"session": session, "target_id": body["target_id"], "media_revision": disk["revision"],
                           "state_dir": self.store.path.parent}
                if action == "delete":
                    return execute_cleanup(self.storage, target, revision=body["revision"], **options)
                plan = plan_cleanup(self.storage, target, **options)
                return {**plan, "directory": str(directory), "medium": medium,
                        "device": disk["device"], "confirmation": confirmation}

    def recording_file(self, session, name, *, source="current", target_id=None):
        directory, identity = self._download_target(source)
        return open_recording_file(self.storage, directory, identity, session, name, target_id=target_id)

    def recording_archive(self, session, *, source="current", target_id=None):
        directory, identity = self._download_target(source)
        return open_recording_archive(self.storage, directory, identity, session, target_id=target_id)

    def set_live(self, enabled, host, port, max_fps=None):
        with self.config_lock:
            self._check_reboot()
            self.camera.set_live(enabled, host, port, max_fps=max_fps)

    def camera_mode_test(self, body):
        action = body.get("action")
        if action == "cancel" and set(body) == {"action"}:
            return self.camera.cancel_mode_test()
        if action != "start" or set(body) - {"action", "mode"}:
            raise ValueError("Choose start with an optional sensor mode, or cancel.")
        with self.config_lock, self.record_lock:
            self._check_reboot()
            if self.closing or self.sources:
                raise Conflict("Release all recording sources before testing camera modes.")
            return self.camera.start_mode_test(body.get("mode"))

    def set_firmware(self, body):
        with self.config_lock:
            self._check_reboot()
            return self.firmware.apply(body)

    def set_clock(self, body):
        if set(body) != {"confirm", "browser_realtime_ms"} or body["confirm"] is not True:
            raise ValueError("Supply browser_realtime_ms and confirm the device clock change.")
        with self.record_lock:
            self._check_reboot()
            if self.closing or self.sources or self.camera.status()["recording"]:
                raise Conflict("Finish all recordings before changing the device clock.")
            self.clock.set_time(body["browser_realtime_ms"])
            return {"device_realtime_ms": self.clock.now_ms()}

    def reboot(self, body):
        with self.config_lock, self.record_lock:
            self._check_reboot()
            self._check_disk_idle()
            if self.sources or self.camera.status()["recording"]:
                raise Conflict("Release all recording sources before rebooting the device.")
            result = self.firmware.reboot(body)
            if result["scheduled"] and not result["simulated"]:
                # systemd will stop the service. Keep new triggers from opening a
                # recording while the reboot job is queued.
                self.reboot_until = time.monotonic() + 60
            return result

    def preview(self, enabled):
        with self.config_lock:
            self._check_reboot()
            self.camera.set_preview(enabled)

    def configure(self, patch):
        with self.config_lock:
            self._check_reboot()
            previous = self.config
            candidate = self.store.merge(previous, patch)
            with self.record_lock:
                camera_changed = candidate["camera"] != previous["camera"]
                storage_changed = candidate["storage"] != previous["storage"]
                camera_status = self.camera.status()
                if camera_changed and camera_status["live"]["enabled"]:
                    raise Conflict("Stop the live stream before changing camera settings.")
                if (camera_changed or storage_changed) and camera_status["recording"]:
                    raise Conflict("Stop recording before changing the camera or recording directory.")
                identity = self.store.storage_identity
                if "storage" in patch:
                    self._check_disk_idle()
                    directory = self.store.directory(candidate["storage"]["directory"])
                    # A full config save can include an unchanged storage section.
                    # Only a deliberate destination selection can adopt replacement media.
                    reselect = set(patch) == {"storage"}
                    expected = None if storage_changed else identity
                    if reselect and not storage_changed and identity:
                        expected = self.storage.identity(directory)
                        if expected["mountpoint"] != identity.get("mountpoint"):
                            raise ValueError("The selected volume is no longer mounted at its original mount point.")
                    with self.storage.open_target(directory, expected) as target:
                        identity = target.identity
                    if identity != self.store.storage_identity and camera_status["recording"]:
                        raise Conflict("Stop recording before selecting another volume.")
                if candidate == previous and identity == self.store.storage_identity:
                    return
                if camera_changed:
                    self.camera.configure(candidate["camera"])
                try:
                    self.store.save(candidate, storage_identity=identity)
                except Exception:
                    if camera_changed:
                        self.camera.configure(previous["camera"])
                    raise
                self.config = candidate
            if candidate["triggers"] != previous["triggers"]:
                self.triggers.configure(candidate["triggers"])

    def network_status(self):
        with self.network_lock:
            if time.monotonic() >= self.network_until:
                self.network_cache = self.network.status()
                self.network_until = time.monotonic() + 5
            return self.network_cache

    def switch_network(self, settings):
        try:
            return self.network.apply(settings)
        finally:
            with self.network_lock:
                self.network_until = 0

    def status(self):
        with self.config_lock, self.record_lock:
            camera = self.camera.status()
            identity = self.store.storage_identity
            state = {
                "config": self.config,
                "camera": camera,
                "recording_sources": sorted(self.sources),
                "recording_directories": self._recording_directories(),
                "storage_roots": [str(root) for root in self.store.roots],
            }
        try:
            directory = self.store.directory(state["config"]["storage"]["directory"])
            self.storage.identity(directory, identity)
            usage = shutil.disk_usage(directory)
            state["storage"] = {"free_bytes": usage.free, "total_bytes": usage.total}
        except (OSError, ValueError) as exc:
            state["storage"] = {"free_bytes": None, "total_bytes": None, "error": str(exc)}
        state["triggers"] = self.triggers.status()
        state["network"] = self.network_status()
        state["device_realtime_ms"] = self.clock.now_ms()
        return state

    def close(self):
        with self.config_lock:
            with self.record_lock:
                self.closing = True
                self.record_lock.notify_all()
            self.recovery.join()
            self.triggers.close()
            self.camera.close()
