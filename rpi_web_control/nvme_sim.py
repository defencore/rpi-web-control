"""Simulate separate NVMe and USB drives using private directories in the development state."""

import contextlib
import fcntl
import hashlib
import os
import uuid
from pathlib import Path
from typing import NamedTuple


DEVICE = "/dev/nvme0n1"
PARTITION = DEVICE + "p1"
SIZE = 128_000_000_000


class _Disk(NamedTuple):
    label: str
    device: str
    partition: str
    size: int
    model: str


_MEDIA = {
    "nvme": _Disk("NVMe", DEVICE, PARTITION, SIZE, "Simulated PCIe NVMe SSD"),
    "usb": _Disk("USB", "/dev/sda", "/dev/sda1", 32_000_000_000, "Simulated USB flash drive"),
}


@contextlib.contextmanager
def _directory(path, parent=None):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


class SimulatedNvme:
    simulated = True

    def __init__(self, state_dir, medium="nvme"):
        if medium not in _MEDIA:
            raise ValueError("Select the NVMe or USB simulator.")
        self.medium = medium
        self.disk = _MEDIA[medium]
        self._base = Path(state_dir).resolve() / f"{medium}-sim"
        self.mountpoint = self._base / "volume"
        self.directory = self.mountpoint / "recordings"
        self._base.mkdir(mode=0o700, parents=True, exist_ok=True)
        with _directory(self._base) as base:
            fcntl.flock(base, fcntl.LOCK_EX)
            try:
                os.stat("volume", dir_fd=base, follow_symlinks=False)
            except FileNotFoundError:
                # Keep the fixture data hidden until the operator mounts it.
                with contextlib.suppress(FileExistsError):
                    os.mkdir("existing", mode=0o700, dir_fd=base)
                with _directory("existing", base) as existing:
                    with contextlib.suppress(FileExistsError):
                        os.mkdir("recordings", mode=0o750, dir_fd=existing)

    def _snapshot(self, base):
        try:
            os.stat("volume", dir_fd=base, follow_symlinks=False)
            mounted = True
        except FileNotFoundError:
            mounted = False
        with _directory("volume" if mounted else "existing", base) as volume:
            with _directory("recordings", volume):
                info = os.fstat(volume)
        # Directory identity survives mount (rename) and changes on preparation.
        fs_uuid = str(uuid.uuid5(uuid.NAMESPACE_OID, f"simulated-{self.medium}:{info.st_dev}:{info.st_ino}"))
        revision = hashlib.sha256(f"{fs_uuid}:{mounted}".encode()).hexdigest()
        reason = "The simulated recording volume is already mounted." if mounted else None
        partition = {"device": self.disk.partition, "size_bytes": self.disk.size - 2**20, "filesystem": "ext4",
                     "uuid": fs_uuid, "can_mount": not mounted, "reason": reason, "mounted": mounted,
                     "ready": mounted, "mountpoints": [str(self.mountpoint)] if mounted else []}
        disk = {"device": self.disk.device, "model": self.disk.model, "serial": f"SIM-{self.medium.upper()}-001",
                "size_bytes": self.disk.size, "revision": revision, "can_prepare": not mounted,
                "reason": reason, "volumes": [partition]}
        return {"available": True, "simulated": True, "error": None, "ready": mounted,
                "mountpoint": str(self.mountpoint), "directory": str(self.directory), "devices": [disk]}

    def status(self):
        try:
            with _directory(self._base) as base:
                fcntl.flock(base, fcntl.LOCK_SH)
                return self._snapshot(base)
        except OSError as error:
            return {"available": False, "simulated": True, "error": str(error), "ready": False,
                    "mountpoint": str(self.mountpoint), "directory": str(self.directory), "devices": []}

    @property
    def ready(self):
        return self.status()["ready"]

    def apply(self, body):
        if not isinstance(body, dict) or body.get("action") not in ("prepare", "mount"):
            raise ValueError(f"Choose a supported {self.disk.label} operation.")
        prepare = body["action"] == "prepare"
        fields = {"action", "device", "revision", "confirmation" if prepare else "volume"}
        if set(body) != fields or body["device"] != self.disk.device:
            raise ValueError(f"Invalid {self.disk.label} device or operation fields.")
        if prepare and body["confirmation"] != self.disk.device:
            raise ValueError(f"Type the full device path to confirm erasing the {self.disk.label} drive.")
        if not prepare and body["volume"] != self.disk.partition:
            raise ValueError(f"Select an ext4 volume on this {self.disk.label} drive.")

        with _directory(self._base) as base:
            fcntl.flock(base, fcntl.LOCK_EX)
            disk = self._snapshot(base)["devices"][0]
            if disk["revision"] != body["revision"]:
                raise ValueError(f"The {self.disk.label} device or its configuration changed. Reload the drive list.")
            if not disk["can_prepare"]:
                raise ValueError(disk["reason"])
            source = "existing"
            if prepare:
                # Publish a fresh empty volume. The original fixture and host files stay private.
                source = f"new-{uuid.uuid4().hex}"
                os.mkdir(source, mode=0o700, dir_fd=base)
                with _directory(source, base) as volume:
                    os.mkdir("recordings", mode=0o750, dir_fd=volume)
            os.rename(source, "volume", src_dir_fd=base, dst_dir_fd=base)
            return self._snapshot(base)
