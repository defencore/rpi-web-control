"""Inventory recording media and bind each recording to its selected filesystem."""

import contextlib
import os
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path


_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_ESCAPE = re.compile(r"\\(040|011|012|134)")
_MISMATCH = "The selected recording filesystem changed. Select the destination again to use the new volume."


def _read(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


@dataclass(frozen=True)
class Mount:
    number: str
    dev: str
    root: str
    path: Path
    fs: str
    source: str
    read_only: bool


def _unescape(text):
    return _ESCAPE.sub(lambda match: chr(int(match[1], 8)), text)


def resolve_directory(path, managed_roots):
    resolved = path.resolve()
    for root in managed_roots:
        if path.is_relative_to(root) and not resolved.is_relative_to(root):
            raise ValueError("The managed recording directory points outside the volume.")
    return resolved


def read_mounts(path):
    mounts = {}
    for line in path.read_text(errors="surrogateescape").splitlines():
        left, right = line.split(" - ", 1)
        fields, tail = left.split(), right.split()
        # ext4 may retain rw in the mount flags after a journal failure while
        # its superblock reports emergency_ro or shutdown.
        options = set(fields[5].split(",")) | set(tail[2].split(","))
        mount = Mount(fields[0], fields[2], _unescape(fields[3]), Path(_unescape(fields[4])),
                      tail[0], _unescape(tail[1]), bool(options & {"ro", "emergency_ro", "shutdown"}))
        mounts[mount.number] = mount
    if not mounts:
        raise ValueError("The mounted filesystem table is unavailable.")
    return mounts


def covering(mounts, path):
    choices = (m for m in mounts.values() if path.is_relative_to(m.path))
    match = max(choices, key=lambda m: (len(m.path.parts), int(m.number)), default=None)
    if match is None:
        raise ValueError("The recording filesystem is unavailable.")
    return match


@dataclass(frozen=True)
class RecordingTarget:
    path: Path
    fd: int
    identity: dict


class Storage:
    def __init__(self, roots, *, required_mounts=(), proc=Path("/proc"), sysfs=Path("/sys"),
                 dev=Path("/dev"), fstab=Path("/etc/fstab"), linux=None,
                 managed_root=None, simulated_nvme=None, managed_usb_root=None, simulated_usb=None):
        self.media = {root: (kind, simulator) for root, kind, simulator in (
            (managed_root, "NVMe", simulated_nvme), (managed_usb_root, "USB", simulated_usb)
        ) if root is not None}
        self.roots = tuple(sorted({Path(p) if Path(p) in self.media else Path(p).resolve()
                                   for p in roots}, key=lambda p: -len(p.parts)))
        self.required = tuple(Path(p).resolve() for p in required_mounts)
        self.proc, self.sysfs, self.dev = Path(proc), Path(sysfs), Path(dev)
        self.linux = sys.platform.startswith("linux") if linux is None else linux
        if self.linux:
            # Treat a configured but absent mount as unavailable on the first boot
            # too, before the first recording has captured its persistent identity.
            planned = []
            for line in _read(Path(fstab)).splitlines():
                fields = line.split("#", 1)[0].split()
                if len(fields) >= 3 and fields[1].startswith("/") and fields[1] != "/":
                    planned.append(Path(_unescape(fields[1])).resolve())
            self.required = tuple(dict.fromkeys((*self.required, *planned)))
        self._cache_lock = threading.Lock()
        self._expires = 0
        self._rows = []

    def _policy(self, path):
        path = Path(path)
        if not path.is_absolute():
            raise ValueError("Select an absolute recording directory.")
        path = resolve_directory(path, self.media)
        root = next((p for p in self.roots if path.is_relative_to(p)), None)
        if root is None:
            raise ValueError("The recording directory must be inside an allowed storage root.")
        if not root.is_dir():
            raise ValueError("The selected storage root is unavailable.")
        return path, root

    def _verify_location(self, fd, path):
        if self.linux:
            # Disallow a replaced ancestor symlink even on the same filesystem.
            try:
                actual = Path(os.readlink(self.proc / f"self/fd/{fd}"))
            except OSError as exc:
                raise ValueError("The recording directory cannot be verified.") from exc
            if actual != path:
                raise ValueError("The recording directory moved. Select its current location.")

    def _mounts(self):
        return read_mounts(self.proc / "self/mountinfo") if self.linux else {}

    def _required_check(self, path, mounts):
        for root, (kind, simulator) in self.media.items():
            if not path.is_relative_to(root):
                continue
            if simulator is not None:
                if not simulator.ready:
                    raise ValueError(f"The simulated {kind} volume is offline.")
            else:
                mount = covering(mounts, path)
                if (mount.path != root.parent or mount.root != "/" or mount.fs != "ext4"
                        or self._kind(self._mount_block(mount)) != kind):
                    raise ValueError(f"The managed {kind} volume is not mounted.")
        for expected in self.required:
            if path.is_relative_to(expected):
                present = any(m.path == expected for m in mounts.values()) if self.linux else expected.is_mount()
                if not present:
                    raise ValueError(f"The required recording mount is unavailable: {expected}.")

    def _block(self, devno):
        node = self.sysfs / "dev/block" / devno
        return node.resolve() if node.exists() else None

    def _mount_block(self, mount):
        node = self._block(mount.dev)
        if node is None and mount.source.startswith("/dev/"):
            # FUSE block filesystems and Btrfs can report a virtual major:minor.
            device = self.dev / mount.source.removeprefix("/dev/")
            source = self.sysfs / "class/block" / device.resolve().name
            node = source.resolve() if source.exists() else None
        return node

    def _mtd(self, mount):
        name = Path(mount.source).name
        match = re.fullmatch(r"mtd(?:block)?(\d+)", name)
        if match:
            return self.sysfs / "class/mtd" / ("mtd" + match[1])
        if mount.fs == "ubifs":
            ubi = re.match(r"ubi(\d*)(?:_|:)", name)
            if ubi:
                index = _read(self.sysfs / "class/ubi" / ("ubi" + (ubi[1] or "0")) / "mtd_num")
                if index.isdecimal():
                    return self.sysfs / "class/mtd" / ("mtd" + index)
        if mount.fs == "jffs2" and name.startswith("mtd:"):
            return next((p for p in (self.sysfs / "class/mtd").glob("mtd[0-9]*")
                         if _read(p / "name") == name[4:]), None)
        return None

    def _key(self, mount):
        node = self._mount_block(mount)
        if node:
            # Query udev's stable identifiers without a subprocess or opening a block device.
            for by in ("by-uuid", "by-partuuid", "by-id"):
                for link in sorted((self.dev / "disk" / by).glob("*")):
                    if link.resolve().name == node.name:
                        return by + "/" + link.name
        mtd = self._mtd(mount)
        if mtd and mtd.exists():
            return f"mtd/{mtd.resolve()}:{_read(mtd / 'name')}:{mount.source}"
        # Some filesystems expose no persistent ID. Require deliberate reselection
        # after remount/reboot instead of assuming that a reused device name is safe.
        boot = _read(self.proc / "sys/kernel/random/boot_id")
        return f"mount/{boot}/{mount.dev}/{mount.number}"

    def fd_mount_id(self, fd):
        if not self.linux:
            return os.fstat(fd).st_dev
        info = _read(self.proc / f"self/fdinfo/{fd}")
        match = re.search(r"^mnt_id:\s*(\d+)$", info, re.MULTILINE)
        if not match:
            raise ValueError("The open file's mount cannot be verified.")
        return match[1]

    def _identity(self, path, mounts, fd=None, *, writable=True):
        if not self.linux:
            parent = path
            while not parent.exists():
                parent = parent.parent
            mountpoint = next(p for p in (parent, *parent.parents) if p.is_mount())
            stat = os.fstat(fd) if fd is not None else parent.stat()
            return {"mountpoint": str(mountpoint), "volume": f"local/{stat.st_dev}"}
        if fd is None:
            mount = covering(mounts, path)
        else:
            mount = mounts.get(self.fd_mount_id(fd))
            if mount is None or not path.is_relative_to(mount.path):
                raise ValueError("The recording directory is no longer on its mounted filesystem.")
        if writable and mount.read_only:
            raise ValueError(f"The recording filesystem at {mount.path} is read-only or shut down. "
                             "Check the drive connection and filesystem before recording here.")
        return {"mountpoint": str(mount.path), "filesystem": mount.fs, "root": mount.root,
                "volume": self._key(mount)}

    def identity(self, directory, expected=None, *, writable=True):
        path, _ = self._policy(directory)
        mounts = self._mounts()
        self._required_check(path, mounts)
        result = self._identity(path, mounts, writable=writable)
        if expected is not None and expected != result:
            raise ValueError(_MISMATCH)
        return result

    @contextlib.contextmanager
    def open_target(self, directory, expected=None, *, create=True):
        """Pin the selected volume. Readers only open existing directories."""
        path, root = self._policy(directory)
        reference = self.identity(path, expected, writable=create)
        fd = os.open(root, _DIRECTORY)
        try:
            parent = root
            for name in path.relative_to(root).parts:
                try:
                    child = os.open(name, _DIRECTORY, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    # Check the parent before creating a path exposed by an unmount.
                    self._verify_location(fd, parent)
                    if self._identity(parent, self._mounts(), fd) != reference:
                        raise ValueError(_MISMATCH)
                    try:
                        os.mkdir(name, mode=0o750, dir_fd=fd)
                    except FileExistsError:
                        pass
                    child = os.open(name, _DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = child
                parent = parent / name
            mounts = self._mounts()
            self._required_check(path, mounts)
            self._verify_location(fd, path)
            current = self._identity(path, mounts, fd, writable=create)
            if current != reference:
                raise ValueError(_MISMATCH)
            if create and not os.access(".", os.W_OK | os.X_OK, dir_fd=fd):
                raise ValueError("The recording directory is not writable.")
            yield RecordingTarget(path, fd, current)
        finally:
            os.close(fd)

    def _flash_kind(self, node):
        parts = node.resolve().parts
        spi = any(re.fullmatch(r"spi\d+(?:\.\d+)?", part) for part in parts)
        return "SPI flash" if spi else "MTD"

    def _kind(self, node, seen=frozenset()):
        if node is None or node in seen:
            return "Block device"
        disk = node.parent if (node / "partition").exists() else node
        if any(re.fullmatch(r"usb\d+", part) for part in disk.parts):
            return "USB"
        if re.fullmatch(r"nvme\d+(?:c\d+)?n\d+", disk.name):
            return "NVMe"
        if re.fullmatch(r"mmcblk\d+", disk.name):
            return {"SD": "microSD", "MMC": "eMMC"}.get(_read(disk / "device/type"), "SD/MMC")
        kinds = {self._kind(s.resolve(), seen | {node}) for s in (disk / "slaves").glob("*")}
        return kinds.pop() if len(kinds) == 1 else "Block device"

    def _roots_for(self, mount, mounts):
        roots = []
        if mount.read_only:
            return roots
        for allowed in self.roots:
            if allowed.is_relative_to(mount.path) and covering(mounts, allowed) == mount:
                path = allowed
            elif mount.path.is_relative_to(allowed):
                path = mount.path
            else:
                continue
            try:
                self._required_check(path, mounts)
            except ValueError:
                continue
            if path.is_dir() and os.access(path, os.W_OK | os.X_OK):
                roots.append(str(path))
        return sorted(set(roots))

    @staticmethod
    def _row(kind, device, label, mount=None, free=None, total=None, roots=(), raw=False):
        return {"kind": kind, "device": device, "label": label, "mountpoint": str(mount.path) if mount else None,
                "filesystem": mount.fs if mount else None, "read_only": mount.read_only if mount else None,
                "free_bytes": free, "total_bytes": total, "roots": list(roots),
                "system": bool(mount and mount.path == Path("/")), "raw": raw}

    def _local_rows(self):
        rows = []
        for path in self.roots:
            try:
                identity = self.identity(path)
                usage = shutil.disk_usage(path)
            except (OSError, ValueError):
                continue
            mount = Mount("0", "", "/", Path(identity["mountpoint"]), "local", "", False)
            rows.append(self._row("Local filesystem", str(path), path.name, mount, usage.free, usage.total,
                                  [str(path)] if os.access(path, os.W_OK | os.X_OK) else []))
        return rows

    def _inventory(self):
        if not self.linux:
            return self._local_rows()
        mounts = self._mounts()
        chosen = {covering(mounts, root).number for root in self.roots}
        rows, mounted = [], set()
        for mount in mounts.values():
            node = self._mount_block(mount)
            mtd = self._mtd(mount)
            kind = self._flash_kind(mtd) if mtd else self._kind(node)
            if not node and not mtd:
                if mount.number not in chosen and mount.path != Path("/"):
                    continue
                kind = "Filesystem"
            if node:
                mounted.add(node.name)
            label = _read(mtd / "name") if mtd else ""
            if node and not label:
                disk = node.parent if (node / "partition").exists() else node
                label = _read(disk / "device/model") or _read(disk / "device/name")
            try:
                usage = shutil.disk_usage(mount.path)
                free, total = usage.free, usage.total
            except OSError:
                free = total = None
            rows.append(self._row(kind, mount.source, label, mount, free, total, self._roots_for(mount, mounts)))
        block = self.sysfs / "class/block"
        nodes = [p.resolve() for p in block.glob("*")]
        partitioned = {p.parent.name for p in nodes if (p / "partition").exists()}
        for node in nodes:
            if node.name in mounted or node.name in partitioned:
                continue
            kind = self._kind(node)
            if kind not in ("NVMe", "microSD", "SD/MMC", "eMMC", "USB"):
                continue
            if re.search(r"(?:boot\d+|rpmb)$", node.name):
                continue
            sectors = _read(node / "size")
            total = int(sectors) * 512 if sectors.isdecimal() else None
            rows.append(self._row(kind, "/dev/" + node.name, "", total=total))
        for node in sorted((self.sysfs / "class/mtd").glob("mtd[0-9]*")):
            if not re.fullmatch(r"mtd\d+", node.name):
                continue
            size = _read(node / "size")
            rows.append(self._row(self._flash_kind(node), "/dev/" + node.name, _read(node / "name"),
                                  total=int(size) if size.isdecimal() else None, raw=True))
        for point in self.required:
            if any(root.is_relative_to(point) or point.is_relative_to(root) for root in self.roots):
                if not any(m.path == point for m in mounts.values()):
                    rows.append(self._row("Unavailable mount", "", str(point)))
        return sorted(rows, key=lambda r: (not r["system"], r["kind"], r["device"], r["mountpoint"] or ""))

    def inventory(self):
        with self._cache_lock:
            now = time.monotonic()
            if now >= self._expires:
                self._rows = self._inventory()
                self._expires = now + 5
            return self._rows

    def invalidate_inventory(self):
        with self._cache_lock:
            self._expires = 0
